#!/usr/bin/env python3
"""Runner of the continuity benchmark (see SPEC.md and README.md).

Subcommands:
  run <slice|full> [options]   start a new run
  resume <run-id> [options]    continue a paused run; finished work is not repeated
  cleanup [--all] [--dry-run]  delete only the bench's own project folders

It starts real headless `claude -p` sessions with the real HOME (a temp HOME gives "Not logged in").
Each session has a fresh temp project folder and project settings only (--setting-sources project).
Arm B registers the Lossless Handoff hooks from a pinned checkout, with HANDOFF_STATE_DIR in a temp folder.
It never touches ~/.claude settings or hooks, ~/.local/state/claude-handoffs, or launchd.

Exit: 0 finished, 1 a session failed, 2 usage error, 3 paused (budget reached; resume later).
"""
import argparse, concurrent.futures, datetime, glob, hashlib, json, os, re, shutil, signal, subprocess
import sys, tempfile, threading, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen, grade  # noqa: E402

REPO = os.path.realpath(os.path.join(HERE, "..", ".."))
RESULTS = os.path.join(HERE, "results")
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
PREFIX = "lhbench-"
ALL_TYPES = gen.TYPES + ["F"]
ALLOWED_TOOLS = "Read Grep Task Agent Bash(grep:*) Bash(cat:*) Bash(ls:*) Bash(head:*) Bash(tail:*) Bash(python3:*)"
MAX_NO_BOUNDARY = 10        # filler turns in a row with no new compaction: the session is invalid
KINDS = {
    "slice": {"model": "haiku", "window": 30000, "n": [1, 3, 10], "arms": "A,B,Z,O", "seeds": 5, "seed_file": "seeds-dev.txt"},
    "full": {"model": "sonnet", "window": 400000, "n": [1, 3, 10, 30], "arms": "A,B,Z,O", "seeds": 20, "seed_file": "seeds-report.txt"},
}


class Pause(Exception):
    pass


class Fatal(Exception):
    pass


class CallError(Exception):
    pass


def slug(path):
    """Claude Code names a project folder by the cwd with every non-alphanumeric character replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path, obj):
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def count_boundaries(path):
    """(count of compact_boundary records, count of those with compactMetadata.precomputed)."""
    n = p = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "compact_boundary" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("subtype") == "compact_boundary":
                    n += 1
                    cm = r.get("compactMetadata")
                    if isinstance(cm, dict) and cm.get("precomputed"):
                        p += 1
    except OSError:
        pass
    return n, p


def work_base(arg=None):
    return os.path.realpath(arg or os.environ.get("LHBENCH_WORK") or tempfile.gettempdir())


def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def sh(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


# ------------------------------------------------------------------ run context
class Ctx:
    def __init__(self, run_dir, cfg):
        self.dir, self.cfg = run_dir, cfg
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state_path = os.path.join(run_dir, "state.json")
        self.state = {"spent": 0.0, "status": "running", "sessions": {}}
        if os.path.exists(self.state_path):
            self.state = json.load(open(self.state_path, encoding="utf-8"))
        self.scripts = {}

    def save(self):
        with self.lock:
            write_json(self.state_path, self.state)

    def append(self, fname, obj):
        with self.lock:
            with open(os.path.join(self.dir, fname), "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, sort_keys=True) + "\n")

    def sess(self, name):
        with self.lock:
            return self.state["sessions"].setdefault(name, {"status": "new", "next_turn": 0, "ckpt_i": 0, "boundaries": 0,
                                                            "last_cost": 0.0, "cost": 0.0, "done_forks": [], "f_planted": [],
                                                            "missed": [], "fork_leaks": 0, "turns": 0, "first": True})

    def charge(self, st, delta):
        with self.lock:
            self.state["spent"] += delta
            st["cost"] = st.get("cost", 0.0) + delta
            self.save()

    def check_budget(self):
        b = self.cfg.get("budget_usd")
        if self.stop.is_set():
            raise Pause()
        if b is not None and self.state["spent"] >= b:
            self.stop.set()
            raise Pause()

    def script(self, seed):
        with self.lock:
            if seed not in self.scripts:
                s = gen.build(seed, self.cfg["gen_version"])
                self.scripts[seed] = s
                os.makedirs(os.path.join(self.dir, "scripts"), exist_ok=True)
                write_json(os.path.join(self.dir, "scripts", "%d.json" % seed), s)
            return self.scripts[seed]


# ------------------------------------------------------------------ one claude call
def lh_settings(hooks_dir):
    """Project settings of arm B: the hooks of the default install (install.sh, no --canary)."""
    pi = "python3 %s/postcompact-inject.py" % hooks_dir
    led = [{"type": "command", "command": "%s --ledger --part %d" % (pi, n), "timeout": 10} for n in (1, 2, 3)]
    chk = {"type": "command", "command": pi + " --check", "timeout": 5}
    return {"hooks": {
        "PreCompact": [{"hooks": [{"type": "command", "command": "python3 %s/precompact-handoff.py" % hooks_dir, "timeout": 150}]}],
        "SessionStart": [
            {"matcher": "compact", "hooks": [{"type": "command", "command": "%s --part %d" % (pi, n), "timeout": 10} for n in (1, 2, 3)] + led},
            {"matcher": "startup", "hooks": led + [chk]},
            {"matcher": "resume", "hooks": led + [chk]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 %s/recall.py session-end" % hooks_dir, "timeout": 5}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 %s/handoff-nudge.py" % hooks_dir, "timeout": 5}]}],
    }}


def deny_rules(base):
    """Permission deny rules for every arm. They block direct reads of any transcript and of bench state folders.
    A project's own filler files stay readable, because they are in the cwd and not under these patterns."""
    proj = os.path.join(HOME, ".claude", "projects")
    rules = []
    for tool in ("Read", "Grep", "Glob"):
        rules += ["%s(~/.claude/projects/**)" % tool, "%s(/%s/**)" % (tool, proj),
                  "%s(/%s/lhbench-*-state/**)" % (tool, base)]
    rules += ["Bash(*.claude/projects*)", "Bash(*.claude*projects*)", "Bash(*lhbench*)", "Bash(*-state*)"]
    return rules


class Session:
    def __init__(self, ctx, name, seed, arm, st):
        self.ctx, self.name, self.seed, self.arm, self.st = ctx, name, seed, arm, st
        cfg = ctx.cfg
        base = work_base(cfg["work_base"])
        stem = "%s%s-s%d-%s" % (PREFIX, cfg["run_id"], seed, arm)
        self.proj = os.path.join(base, stem)
        self.statedir = os.path.join(base, stem + "-state")
        self.window = cfg["o_window"] if arm == "O" else cfg["window"]
        self.script = ctx.script(seed)
        ov = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.window), "RECALL_EMBED_URL": "http://127.0.0.1:9"}
        if arm == "B":
            ov["HANDOFF_STATE_DIR"] = self.statedir
            ov["HANDOFF_SUMMARY_MODEL"] = cfg["model"]
        self.overrides = ov

    # -- folders
    def prepare(self):
        if not os.path.isdir(self.proj):
            os.makedirs(os.path.join(self.proj, ".claude"))
            os.makedirs(self.statedir, exist_ok=True)
            settings = lh_settings(os.path.join(self.ctx.cfg["lh_path"], "hooks")) if self.arm == "B" else {}
            settings["permissions"] = {"deny": deny_rules(work_base(self.ctx.cfg["work_base"]))}
            write_json(os.path.join(self.proj, ".claude", "settings.json"), settings)
            # make the folder its own git root, so that recall.py cannot search outside it
            subprocess.run(["git", "init", "-q", self.proj], capture_output=True, timeout=30)
            if self.st["next_turn"] > 0 or not self.st["first"]:
                # the folder is gone: the old session cannot continue. Start again from turn 0.
                self.st.update({"next_turn": 0, "ckpt_i": 0, "boundaries": 0, "last_cost": 0.0, "done_forks": [],
                                "f_planted": [], "missed": [], "first": True, "sid": None})
        self.proj = os.path.realpath(self.proj)
        if not self.st.get("sid"):
            self.st["sid"] = str(uuid.uuid4())

    def transcript(self, sid):
        return os.path.join(PROJECTS, slug(self.proj), sid + ".jsonl")

    # -- the call
    def env(self):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith("HANDOFF_") or k == "CLAUDE_CODE_AUTO_COMPACT_WINDOW":
                env.pop(k)
        env.update(self.overrides)
        return env

    def call(self, prompt, kind, sid=None, fork_of=None, first=False):
        """One `claude -p` call. kind: turn | fork | z. Returns the parsed JSON object."""
        ctx, cfg = self.ctx, self.ctx.cfg
        for attempt in (1, 2):
            ctx.check_budget()
            cmd = ["claude", "-p"]
            if fork_of:
                cmd += ["--resume", fork_of, "--fork-session"]
            else:
                cmd += ["--session-id" if first else "--resume", sid]
            cmd += ["--model", cfg["model"], "--setting-sources", "project",
                    "--settings", json.dumps({"env": self.overrides}),
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
                    "--output-format", "json", "--permission-mode", "acceptEdits",
                    "--allowedTools", ALLOWED_TOOLS, "--max-budget-usd", "%.2f" % cfg["call_cap_usd"], prompt]
            t0 = time.time()
            p = subprocess.Popen(cmd, cwd=self.proj, env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                out, err = p.communicate(timeout=cfg["turn_timeout_s"])
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    pass
                p.communicate()
                err_txt = "timeout"
                out = ""
            else:
                err_txt = err
            try:
                o = json.loads(out)
                if not isinstance(o, dict):
                    raise ValueError
            except ValueError:
                low = (out + err_txt).lower()
                if "not logged in" in low or "please run /login" in low:
                    raise Fatal("claude is not logged in")
                if attempt == 2:
                    raise CallError("bad output: " + (out + err_txt)[:200].replace("\n", " "))
                continue
            if o.get("is_error") and "not logged in" in str(o.get("result")).lower():
                raise Fatal("claude is not logged in")
            c = o.get("total_cost_usd")
            c = float(c) if isinstance(c, (int, float)) else 0.0
            if fork_of:      # a fork reports the parent's cost plus its own
                delta = max(c - self.st["last_cost"], 0.0)
            elif kind == "z":
                delta = c
            else:
                delta = max(c - self.st["last_cost"], 0.0)
                self.st["last_cost"] = max(c, self.st["last_cost"])
            ctx.charge(self.st, delta)
            u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
            ctx.append("calls.jsonl", {"ts": now_iso(), "name": self.name, "kind": kind, "sid": o.get("session_id"),
                                       "fork_of": fork_of, "raw_total_cost_usd": c, "delta_usd": round(delta, 6),
                                       "seconds": round(time.time() - t0, 1), "num_turns": o.get("num_turns"),
                                       "in": u.get("input_tokens"), "out": u.get("output_tokens"),
                                       "cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
                                       "is_error": o.get("is_error")})
            o["_delta"] = delta
            if o.get("is_error") or p.returncode != 0:
                if attempt == 2:
                    raise CallError("is_error: " + str(o.get("result"))[:200])
                continue
            return o
        raise CallError("unreachable")

    # -- turns of the main session
    def turn(self, prompt):
        st = self.st
        first = st["first"]
        o = self.call(prompt, "turn", sid=st["sid"], first=first)
        st["first"] = False
        st["turns"] += 1
        n, pre = count_boundaries(self.transcript(st["sid"]))
        st["boundaries"] = n
        self.ctx.save()
        return o

    def probe(self, ptype, ckpt, fork_of=None, z_sid=None):
        """Run one probe type. Fork of the main session (arms A, B, O) or a fresh session (arm Z). Write the result lines."""
        cfg, st, script = self.ctx.cfg, self.st, self.script
        marker = "PROBE-%d-%s-%d-%s" % (self.seed, self.arm, ckpt, ptype)
        prompt = gen.probe_prompt(ptype, script, marker, ckpt)
        if fork_of:
            o = self.call(prompt, "fork", fork_of=fork_of)
        else:
            o = self.call(prompt, "z", sid=z_sid, first=True)
        raw = o.get("result") if isinstance(o.get("result"), str) else ""
        fsid = o.get("session_id")
        items = script["fresh"][str(ckpt)] if ptype == "F" else script["items"][ptype]
        graded = grade.grade(ptype, items, raw)
        used = grade.lookup_used(read_jsonl(self.transcript(fsid)), marker) if fsid else False
        if fsid:    # a later recall must not find this answer: delete the probe transcript
            ft = self.transcript(fsid)
            for pth in (ft, ft[:-6]):
                if os.path.isfile(pth):
                    os.remove(pth)
                elif os.path.isdir(pth) and not os.path.islink(pth):
                    shutil.rmtree(pth)
        u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
        n_actual, pre = (st["boundaries"], st.get("precomputed", 0)) if self.arm != "Z" else (0, 0)
        for g in graded:
            self.ctx.append("results.jsonl", {
                "run": cfg["run_id"], "gen_version": cfg["gen_version"], "seed": self.seed, "arm": self.arm,
                "n_target": ckpt, "n_actual": n_actual, "precomputed_boundaries": pre, "type": ptype, "item": g["id"],
                "fork_sid": fsid, "raw": raw, "parsed": g["parsed"], "parse_ok": g["parse_ok"], "correct": g["correct"],
                "used_lookup": used, "tokens": {"in": u.get("input_tokens"), "out": u.get("output_tokens")},
                "fork_cost_usd": round(o["_delta"], 6), "claude_version": cfg["claude_version"], "model": cfg["model"],
                "window": self.window, "commit": cfg["lh_commit"], "turn_index": st["turns"]})
        st["done_forks"].append("%d:%s" % (ckpt, ptype))
        self.ctx.save()


# ------------------------------------------------------------------ session logic
def checkpoints(cfg, arm):
    if arm == "O":
        return [(0, cfg["oracle_turns"])]
    return [(n, 0) for n in cfg["n"]]


def run_main_session(ctx, ss):
    cfg, st, script = ctx.cfg, ss.st, ss.script
    P = len(script["plant_turns"])
    cks = checkpoints(cfg, ss.arm)
    per = cfg["files_per_turn"]
    quiet = 0           # filler turns in a row with no new boundary
    while st["ckpt_i"] < len(cks):
        target, min_fill = cks[st["ckpt_i"]]
        b = st["boundaries"]
        fill_done = max(0, st["next_turn"] - P)
        if b > target:  # a turn added two compactions: this checkpoint is missed
            st["missed"].append({"target": target, "actual": b})
            st["ckpt_i"] += 1
            ctx.save()
            continue
        if st["next_turn"] >= P and b == target and fill_done >= min_fill:
            checkpoint(ctx, ss, target)
            st["ckpt_i"] += 1
            ctx.save()
            continue
        ctx.check_budget()
        t = st["next_turn"]
        before = b
        if t < P:
            pt = script["plant_turns"][t]
            for fname, content in (pt.get("files") or {}).items():
                with open(os.path.join(ss.proj, fname), "w", encoding="utf-8") as f:
                    f.write(content)
            ss.turn(pt["prompt"])
            for fname in pt.get("delete_after") or []:
                try:
                    os.remove(os.path.join(ss.proj, fname))   # the key must live in the transcript only
                except OSError:
                    pass
        else:
            ft = t - P
            names = []
            for j in range(per):
                name, text = gen.filler_file(ss.seed, ft, j, version=cfg["gen_version"])
                if any(s in text for s in script["secrets"]):
                    raise Fatal("a filler file holds an answer token")
                with open(os.path.join(ss.proj, name), "w", encoding="utf-8") as f:
                    f.write(text)
                names.append(name)
            ss.turn(gen.filler_prompt(script, ft, names))
            quiet = quiet + 1 if st["boundaries"] == before else 0
            if quiet >= MAX_NO_BOUNDARY:
                st["status"], st["reason"] = "invalid", "no compaction in %d filler turns" % MAX_NO_BOUNDARY
                return
        st["next_turn"] = t + 1
        if st["next_turn"] == P:      # every item is planted: record the compactions so far (must be 0)
            st["boundaries_at_plant_end"] = st["boundaries"]
        ctx.save()


def checkpoint(ctx, ss, target):
    cfg, st, script = ctx.cfg, ss.st, ss.script
    types = cfg["types"]
    if "F" in types and target not in st["f_planted"]:
        lines = " ".join(x["plant"] for x in script["fresh"][str(target)])
        ss.turn("Remember this for the next question. %s Reply only `ok`." % lines)
        st["f_planted"].append(target)
    n, pre = count_boundaries(ss.transcript(st["sid"]))
    st["boundaries"], st["precomputed"] = n, pre
    if n != target:
        st["missed"].append({"target": target, "actual": n})
        return
    for t in types:
        if "%d:%s" % (target, t) in st["done_forks"]:
            continue
        ss.probe(t, target, fork_of=st["sid"])
    # leak check: no probe marker may appear in the main timeline
    prefix = "PROBE-%d-%s-" % (ss.seed, ss.arm)
    leaks = 0
    try:
        with open(ss.transcript(st["sid"]), encoding="utf-8", errors="replace") as f:
            leaks = sum(1 for ln in f if prefix in ln)
    except OSError:
        pass
    st["fork_leaks"] = leaks


def run_z(ctx, ss):
    st = ss.st
    for t in ctx.cfg["types"]:
        if "0:%s" % t in st["done_forks"]:
            continue
        ss.probe(t, 0, z_sid=str(uuid.uuid4()))


def session_summary(ctx, ss):
    """Facts from the transcript, for the report. The main transcript only; subagent compactions are counted apart."""
    st = ss.st
    out = {"name": ss.name, "seed": ss.seed, "arm": ss.arm, "status": st["status"], "turns": st["turns"],
           "boundaries": st["boundaries"], "precomputed": st.get("precomputed", 0), "missed": st["missed"],
           "fork_leaks": st["fork_leaks"], "boundaries_at_plant_end": st.get("boundaries_at_plant_end"), "cost_usd": round(st.get("cost", 0.0), 4), "reason": st.get("reason")}
    if ss.arm != "Z" and st.get("sid"):
        main = ss.transcript(st["sid"])
        reads = 0
        for r in read_jsonl(main):
            m = r.get("message")
            if isinstance(m, dict) and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read" and "led-" in json.dumps(b.get("input")):
                        reads += 1
        subs = sorted(glob.glob(os.path.join(main[:-6], "subagents", "agent-*.jsonl")))
        out["u_main_reads"] = reads
        out["subagent_files"] = len(subs)
        out["sub_boundaries"] = sum(count_boundaries(p)[0] for p in subs)
        if ss.arm == "B":
            out["hook_receipts"] = len(read_jsonl(os.path.join(ss.statedir, "health.jsonl")))
    return out


def run_unit(ctx, seed, arm):
    name = "s%d-%s" % (seed, arm)
    st = ctx.sess(name)
    if st["status"] == "done":
        return
    ss = Session(ctx, name, seed, arm, st)
    try:
        ss.prepare()
        st["status"] = "running"
        ctx.save()
        if arm == "Z":
            run_z(ctx, ss)
        else:
            run_main_session(ctx, ss)
        if st["status"] == "running":
            st["status"] = "done"
        ctx.save()
        ctx.append("sessions.jsonl", session_summary(ctx, ss))
    except Pause:
        st["status"] = "paused"
        ctx.save()
    except CallError as e:
        st["status"], st["reason"] = "failed", str(e)
        ctx.save()
        ctx.append("sessions.jsonl", session_summary(ctx, ss))
    except Fatal as e:
        st["status"], st["reason"] = "failed", str(e)
        ctx.save()
        ctx.stop.set()
        raise


# ------------------------------------------------------------------ commands
def seed_list(kind, n, path=None):
    f = path or os.path.join(HERE, KINDS[kind]["seed_file"])
    seeds = [int(x) for x in open(f).read().split() if x.strip()]
    return seeds[:n]


def parse_common(ap):
    ap.add_argument("--model", help="claude model alias (slice: haiku, full: sonnet)")
    ap.add_argument("--window", type=int, help="CLAUDE_CODE_AUTO_COMPACT_WINDOW (slice 30000, full 400000)")
    ap.add_argument("--n", help="comma list of compaction counts, for example 1,3,10")
    ap.add_argument("--arms", help="comma list from A,B,Z,O")
    ap.add_argument("--types", help="comma list from R,D,X,S,E,U,C,F (default all)")
    ap.add_argument("--seeds", type=int, help="how many seeds to take from the seed list")
    ap.add_argument("--seed-file", help="seed list file (default: seeds-dev.txt for slice, seeds-report.txt for full)")
    ap.add_argument("--lh-path", help="pinned checkout of Lossless Handoff for arm B (default: this repo)")
    ap.add_argument("--files-per-turn", type=int, help="filler files read in one turn (default from the window)")
    ap.add_argument("--o-window", type=int, default=None, help="window of arm O (default 200000)")
    ap.add_argument("--oracle-turns", type=int, default=None, help="filler turns of arm O before its probes (default 6)")
    ap.add_argument("--work-base", help="folder for the temp project folders (default: the temp dir)")
    ap.add_argument("--call-cap-usd", type=float, default=None, help="--max-budget-usd of each claude call (default 1.50)")
    ap.add_argument("--turn-timeout-s", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=None, help="sessions in parallel (default 2)")
    ap.add_argument("--budget-usd", type=float, default=None, help="pause cleanly when the total reaches this value")


def build_cfg(kind, a):
    k = KINDS[kind]
    window = a.window or k["window"]
    seeds = seed_list(kind, a.seeds or k["seeds"], a.seed_file)
    types = [t for t in (a.types.split(",") if a.types else ALL_TYPES)]
    bad = [t for t in types if t not in ALL_TYPES]
    arms = (a.arms or k["arms"]).split(",")
    bad += [x for x in arms if x not in ("A", "B", "Z", "O")]
    if bad:
        raise SystemExit("unknown type or arm: %s" % bad)
    lh = os.path.realpath(a.lh_path or REPO)
    if "B" in arms and not os.path.isfile(os.path.join(lh, "hooks", "precompact-handoff.py")):
        raise SystemExit("arm B: no hooks in %s/hooks" % lh)
    dirty = bool(sh(["git", "status", "--porcelain", "--", "hooks"], cwd=lh))
    rid = "%s-%s" % (datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), kind)
    return {
        "run_id": rid, "kind": kind, "gen_version": gen.GEN_VERSION, "model": a.model or k["model"], "window": window,
        "n": [int(x) for x in a.n.split(",")] if a.n else k["n"], "arms": arms, "types": types, "seeds": seeds,
        "lh_path": lh, "lh_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=lh) + ("+dirty" if dirty else ""),
        "bench_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=REPO),
        "files_per_turn": a.files_per_turn or max(1, min(12, round(window * 0.4 / 6000))),
        "o_window": a.o_window or 200000, "oracle_turns": a.oracle_turns or 6,
        "work_base": work_base(a.work_base), "call_cap_usd": a.call_cap_usd or 1.50,
        "turn_timeout_s": a.turn_timeout_s or (420 if "haiku" in (a.model or k["model"]) else 900),
        "jobs": a.jobs or 2, "budget_usd": a.budget_usd, "claude_version": sh(["claude", "--version"]),
        "allowed_tools": ALLOWED_TOOLS, "started": now_iso(),
        "command": " ".join(sys.argv),
    }


def execute(ctx):
    cfg = ctx.cfg
    snap0 = sha256_file(os.path.join(HOME, ".claude", "settings.json"))
    units = [(s, arm) for s in cfg["seeds"] for arm in cfg["arms"]]
    code = 0
    ctx.state["status"] = "running"
    ctx.save()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["jobs"]) as ex:
            futs = [ex.submit(run_unit, ctx, s, arm) for s, arm in units]
            for f in futs:
                try:
                    f.result()
                except Fatal as e:
                    print("FATAL:", e, file=sys.stderr)
                    code = 1
                except Exception as e:  # a bug in one unit must not hide the others
                    print("ERROR:", repr(e), file=sys.stderr)
                    code = 1
    except KeyboardInterrupt:
        ctx.stop.set()
        code = 3
    sess = ctx.state["sessions"].values()
    if any(s["status"] == "paused" for s in sess) or (ctx.stop.is_set() and any(s["status"] in ("new", "running") for s in sess)):
        ctx.state["status"] = "paused"
        code = 3
    elif any(s["status"] == "failed" for s in sess):
        ctx.state["status"] = "failed"
        code = code or 1
    else:
        ctx.state["status"] = "done"
    ctx.state["ended"] = now_iso()
    ctx.state["settings_hash_changed"] = sha256_file(os.path.join(HOME, ".claude", "settings.json")) != snap0
    ctx.save()
    unfinished = [n for n, s in ctx.state["sessions"].items() if s["status"] != "done"]
    print("run %s: %s, spent $%.4f, sessions not finished: %s" % (cfg["run_id"], ctx.state["status"], ctx.state["spent"], unfinished or "none"))
    if code == 3:
        print("paused. Continue with: run.sh resume %s [--budget-usd X]" % cfg["run_id"])
    if ctx.state["settings_hash_changed"]:
        print("warning: ~/.claude/settings.json changed during the run (another chat may have written it)", file=sys.stderr)
    return code


def cmd_run(a):
    cfg = build_cfg(a.kind, a)
    run_dir = os.path.join(RESULTS, cfg["run_id"])
    os.makedirs(run_dir)
    write_json(os.path.join(run_dir, "config.json"), cfg)
    open(os.path.join(RESULTS, ".last"), "w").write(cfg["run_id"])
    print("run", cfg["run_id"], "model", cfg["model"], "window", cfg["window"], "arms", cfg["arms"], "seeds", cfg["seeds"], "n", cfg["n"], "types", cfg["types"])
    return execute(Ctx(run_dir, cfg))


def cmd_resume(a):
    run_dir = os.path.join(RESULTS, a.run_id)
    if not os.path.isfile(os.path.join(run_dir, "config.json")):
        print("no such run:", a.run_id, file=sys.stderr)
        return 2
    cfg = json.load(open(os.path.join(run_dir, "config.json"), encoding="utf-8"))
    if a.budget_usd is not None:
        cfg["budget_usd"] = a.budget_usd
    if a.jobs:
        cfg["jobs"] = a.jobs
    write_json(os.path.join(run_dir, "config.json"), cfg)
    ctx = Ctx(run_dir, cfg)
    for s in ctx.state["sessions"].values():
        if s["status"] in ("paused", "running", "failed"):
            s["status"] = "new"        # unfinished: continue from the saved turn
            s.pop("reason", None)
    ctx.save()
    return execute(ctx)


def cmd_cleanup(a):
    """Delete only the bench's own folders: <base>/lhbench-* and ~/.claude/projects/<slug(base/lhbench-)>*.
    Folders of a paused or running run stay, unless --all. Nothing else is touched."""
    base = work_base(a.work_base)
    work_prefix = os.path.join(base, PREFIX)
    proj_prefix = slug(work_prefix)
    if not (proj_prefix.startswith("-") and proj_prefix.endswith("-" + PREFIX) and len(proj_prefix) > len(PREFIX) + 5):
        print("refuse: unsafe prefix", proj_prefix, file=sys.stderr)
        return 2
    protected = []
    if not a.all:
        for d in glob.glob(os.path.join(RESULTS, "*", "state.json")):
            try:
                stt = json.load(open(d, encoding="utf-8"))
                cfg = json.load(open(os.path.join(os.path.dirname(d), "config.json"), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if stt.get("status") in ("paused", "running", "failed") and cfg.get("work_base") == base:
                protected.append(PREFIX + cfg["run_id"] + "-")
    todo = []
    for p in sorted(glob.glob(work_prefix + "*")):
        if os.path.isdir(p) and not os.path.islink(p) and not any(os.path.basename(p).startswith(x) for x in protected):
            todo.append(p)
    try:
        for n in sorted(os.listdir(PROJECTS)):
            p = os.path.join(PROJECTS, n)
            if n.startswith(proj_prefix) and os.path.isdir(p) and not os.path.islink(p) \
                    and not any(n.startswith(slug(os.path.join(base, x))) for x in protected):
                todo.append(p)
    except OSError:
        pass
    for p in todo:
        print(("would delete " if a.dry_run else "delete ") + p)
        if not a.dry_run:
            shutil.rmtree(p)
    print("%s %d folder(s); kept %d run(s) that can still resume" % ("would delete" if a.dry_run else "deleted", len(todo), len(protected)))
    return 0


def cmd_selfcheck(a):
    """Tiny live check of the transcript deny rules, in arm A and arm B (and recall scope in arm B). A few cents."""
    cfg = {"run_id": "selfcheck", "model": a.model, "window": 30000, "work_base": work_base(a.work_base), "call_cap_usd": 0.5,
           "turn_timeout_s": 300, "lh_path": os.path.realpath(a.lh_path or REPO), "budget_usd": None, "gen_version": gen.GEN_VERSION,
           "o_window": 200000, "claude_version": ""}
    d = tempfile.mkdtemp(prefix="selfcheck-")
    ctx = Ctx(d, cfg)
    ok_all = True
    tokA, tokO = "DENYTOK-" + uuid.uuid4().hex[:10].upper(), "OTHERTOK-" + uuid.uuid4().hex[:10].upper()
    for arm in ("A", "B"):
        ss = Session(ctx, "chk-" + arm, 1, arm, ctx.sess("chk-" + arm))
        ss.prepare()
        sid = ss.st["sid"]
        tdir = os.path.dirname(ss.transcript(sid))
        os.makedirs(tdir, exist_ok=True)
        decoy = os.path.join(tdir, "decoy.jsonl")
        open(decoy, "w").write(json.dumps({"cwd": ss.proj, "note": tokA}) + "\n")
        other = os.path.join(cfg["work_base"], PREFIX + "selfcheck-other")        # another bench folder with a secret
        os.makedirs(other, exist_ok=True)
        subprocess.run(["git", "init", "-q", other], capture_output=True)
        odir = os.path.join(PROJECTS, slug(os.path.realpath(other)))
        os.makedirs(odir, exist_ok=True)
        open(os.path.join(odir, str(uuid.uuid4()) + ".jsonl"), "w").write(json.dumps(
            {"type": "user", "cwd": os.path.realpath(other), "timestamp": "2026-11-18T10:00:00Z", "sessionId": "x",
             "message": {"role": "user", "content": "other folder secret " + tokO}}) + "\n")
        o = ss.call("Remember the word ZEBRAQUARTZ. Reply only `ok`.", "turn", sid=sid, first=True)
        prompt = ("Do these four things, each in its own tool call, and report each result in one short line. "
                  "1) Read the file %s with the Read tool. 2) Run `cat %s` with Bash. 3) Use Grep for `note` in %s. "
                  "4) Run `python3 %s/hooks/recall.py search --fixed ZEBRAQUARTZ` with Bash. Then run "
                  "`python3 %s/hooks/recall.py search --fixed OTHERTOK-` with Bash. Report the output of each, and say DENIED if a tool was denied."
                  % (decoy, decoy, tdir, cfg["lh_path"], cfg["lh_path"]))
        o = ss.call(prompt, "turn", sid=sid)
        res = o.get("result") or ""
        denials = o.get("permission_denials") or []
        names = sorted({x.get("tool_name") for x in denials if isinstance(x, dict)})
        leaked = tokA in res or tokO in res
        print("arm %s: denied tools %s; secret in reply: %s" % (arm, names, leaked))
        print("  reply:", res.replace("\n", " | ")[:600])
        good = leaked is False and "Read" in names and "Bash" in names
        ok_all = ok_all and good
    shutil.rmtree(d, ignore_errors=True)
    print("selfcheck:", "PASS" if ok_all else "FAIL", "(clean up with: run.sh cleanup)")
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("kind", choices=list(KINDS))
    parse_common(r)
    rs = sp.add_parser("resume")
    rs.add_argument("run_id")
    rs.add_argument("--budget-usd", type=float, default=None)
    rs.add_argument("--jobs", type=int, default=None)
    sc = sp.add_parser("selfcheck")
    sc.add_argument("--model", default="haiku")
    sc.add_argument("--lh-path")
    sc.add_argument("--work-base")
    c = sp.add_parser("cleanup")
    c.add_argument("--all", action="store_true", help="also delete folders of paused runs")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--work-base")
    a = ap.parse_args()
    return {"run": cmd_run, "resume": cmd_resume, "cleanup": cmd_cleanup, "selfcheck": cmd_selfcheck}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
