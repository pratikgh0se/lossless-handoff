#!/usr/bin/env python3
"""Real Claude Code layers of tests/local-proof.sh (layers 5 and 6).

It starts real headless `claude -p` sessions that compact for real, against hooks that
live in a TEMP install. It never touches the real ~/.claude hooks, the real state folder
or the real launchd. Called by local-proof.sh; not meant to be run by hand.

Usage: local-proof-real.py --hooks DIR --settings FILE --work DIR --out FILE
                           [--runs N] [--only e2e|stress|all] [--cap USD]
Exit: 0 when every layer passes, 1 when a layer fails, 3 when the cost cap stopped the run.
"""
import argparse, concurrent.futures, glob, json, os, random, re, shutil, signal, subprocess
import sys, tempfile, threading, time, uuid

WINDOW = "30000"
TURN_TIMEOUT = 420          # watchdog for one claude turn, seconds
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
         "quebec romeo sierra tango uniform victor whiskey xray yankee zulu ember quartz willow cobalt "
         "marble thistle harbor lantern meadow orchid pewter saddle timber velvet walnut zephyr").split()
HOME = os.path.expanduser("~")
LOCK = threading.Lock()


class CapReached(Exception):
    pass


class Budget:
    def __init__(self, cap):
        self.cap, self.spent, self.turns = cap, 0.0, []

    def add(self, label, cost):
        with LOCK:
            self.spent += cost
            self.turns.append((label, cost))
            if self.spent > self.cap:
                raise CapReached("cost %.2f passed the cap %.2f" % (self.spent, self.cap))


def log(msg):
    with LOCK:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def rand_hex(n=6):
    return uuid.uuid4().hex[:n].upper()


def make_files(proj, prefix, count, lines=250, seed=0):
    rnd = random.Random(seed)
    for i in range(1, count + 1):
        with open(os.path.join(proj, "%s%d.txt" % (prefix, i)), "w", encoding="utf-8") as f:
            for ln in range(1, lines + 1):
                f.write("%d.%04d " % (i, ln) + " ".join(rnd.choice(WORDS) for _ in range(14)) + "\n")


def build_settings(src, dst, hooks_dir, mode="normal", precompact_timeout=None):
    """Copy the temp install's settings and point every hook at the TEMP hook files by absolute path."""
    d = json.load(open(src, encoding="utf-8"))
    real_hooks = os.path.join(HOME, ".claude", "hooks")
    for groups in (d.get("hooks") or {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                c = h.get("command", "")
                c = c.replace("~/.claude/hooks", hooks_dir).replace("$HOME/.claude/hooks", hooks_dir)
                c = c.replace(real_hooks, hooks_dir)
                h["command"] = c
    env = d.setdefault("env", {})
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = WINDOW
    if mode == "missing":
        d["hooks"].pop("PreCompact", None)
    if precompact_timeout is not None:
        for g in d["hooks"].get("PreCompact", []):
            for h in g.get("hooks", []):
                h["timeout"] = precompact_timeout
    for groups in d["hooks"].values():
        for g in groups:
            for h in g.get("hooks", []):
                if real_hooks in h.get("command", "") or "~/.claude/hooks" in h.get("command", ""):
                    raise SystemExit("isolation: a hook command still points at the real hooks: " + h["command"])
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    return d


class Session:
    def __init__(self, label, proj, settings, state, budget, extra_env=None):
        self.label, self.proj, self.settings, self.state, self.budget = label, proj, settings, state, budget
        self.sid, self.first, self.cost, self.turns = str(uuid.uuid4()), True, 0.0, 0
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith("HANDOFF_") or k in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW",):
                self.env.pop(k)
        self.env.update({"HANDOFF_STATE_DIR": state, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": WINDOW,
                         "RECALL_EMBED_URL": "http://127.0.0.1:9", "HANDOFF_SUMMARY_MODEL": "haiku"})
        self.env.update(extra_env or {})
        self.errors = []

    def turn(self, prompt):
        cmd = ["claude", "-p", "--session-id" if self.first else "--resume", self.sid, "--model", "haiku",
               "--setting-sources", "local", "--settings", self.settings,
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
               "--output-format", "json", "--permission-mode", "acceptEdits",
               "--allowedTools", "Read Task Agent", "--max-budget-usd", "1.50", prompt]
        self.first = False
        p = subprocess.Popen(cmd, cwd=self.proj, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = p.communicate(timeout=TURN_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
            p.communicate()
            self.errors.append("turn timeout")
            return None
        try:
            o = json.loads(out)
        except ValueError:
            self.errors.append("bad json: " + (out + err)[:200].replace("\n", " "))
            return None
        c = o.get("total_cost_usd")
        c = float(c) if isinstance(c, (int, float)) else 0.0
        self.turns += 1
        delta = max(c - self.cost, 0.0)   # total_cost_usd is cumulative for a resumed session
        self.cost = max(c, self.cost)
        self.budget.add("%s#%d" % (self.label, self.turns), delta)
        if o.get("is_error"):
            self.errors.append("is_error: " + str(o.get("result"))[:200])
        return o

    # transcripts
    def main_transcript(self):
        g = glob.glob(os.path.join(HOME, ".claude", "projects", "*", self.sid + ".jsonl"))
        return g[0] if g else None

    def sub_transcripts(self):
        t = self.main_transcript()
        if not t:
            return []
        return sorted(glob.glob(os.path.join(t[:-len(".jsonl")], "subagents", "agent-*.jsonl")))

    def main_boundaries(self):
        return count_boundaries(self.main_transcript())

    def sub_boundaries(self):
        return sum(count_boundaries(p) for p in self.sub_transcripts())


def count_boundaries(path):
    n = 0
    if not path:
        return 0
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
    except OSError:
        pass
    return n


def jsonl(path):
    rows, bad = [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        pass
    return rows, bad


def read_turn(files, code, note):
    return ("Session note %s: remember this exact code for later: %s. Use the Read tool to read these files in full, "
            "one file per call: %s. Then reply with the code and the last line number of each file. Keep the reply short."
            % (note, code, ", ".join(files)))


def sub_turn(files, code, main_code):
    inner = ("Your seed code is %s. Use the Read tool to read these files in full, one file per call: %s. "
             "Then reply with your seed code and the last line number of each file. Keep the reply short."
             % (code, ", ".join(files)))
    return ("Session note: remember this exact code for later: %s. Then call the Agent tool exactly once, with "
            "subagent_type general-purpose and exactly this prompt: \"%s\" "
            "When the subagent returns, reply with its answer in one short line." % (main_code, inner))


def drive_main(s, codes, max_turns=5, need_sub=False):
    """Turn by turn until the session has the compactions it needs. Returns turns used.
    A subagent turn comes first (it reads many files), then main reading turns."""
    mi, si = 1, 1
    for t in range(max_turns):
        sub_done = (not need_sub) or s.sub_boundaries() >= 1
        if s.main_boundaries() >= 1 and sub_done:
            break
        main_code = "MAINSEED-%s" % rand_hex()
        codes.setdefault("main", []).append(main_code)
        if not sub_done and (t == 0 or s.main_boundaries() >= 1):
            code = "SUBSEED-%s" % rand_hex()
            codes.setdefault("sub", []).append(code)
            files = ["sub%d.txt" % (((si - 1 + k) % 8) + 1) for k in range(6)]
            si += 6
            o = s.turn(sub_turn(files, code, main_code))
        else:
            files = ["main%d.txt" % (((mi - 1 + k) % 8) + 1) for k in range(3)]
            mi += 3
            o = s.turn(read_turn(files, main_code, "t%d" % t))
        if o is None:
            break
    return s.turns


def run_tool(hooks, args, env_extra, home=None, timeout=240):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("HANDOFF_"):
            env.pop(k)
    env.update(env_extra)
    env["RECALL_EMBED_URL"] = "http://127.0.0.1:9"
    if home:
        env["HOME"] = home
    p = subprocess.run(["python3", os.path.join(hooks, "precompact-handoff.py")] + args, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return (p.stdout + p.stderr).strip()


def prove(hooks, state):
    out = run_tool(hooks, ["--prove", "--days", "1"], {"HANDOFF_STATE_DIR": state})
    m = [ln for ln in out.splitlines() if ln.startswith("proof ")]
    return (m[0] if m else ""), out


def parse_prove(line):
    def frac(name):
        m = re.search(name + r" (\d+)/(\d+)", line)
        return (int(m.group(1)), int(m.group(2))) if m else None
    f = re.search(r"failed (\d+)", line)
    r = re.search(r"receipts (\d+)", line)
    return {"receipts": int(r.group(1)) if r else None, "lossless": frac("lossless"),
            "right_agent": frac("right-agent"), "delivered": frac("delivered"),
            "decisions_safe": frac("decisions-safe"), "failed": int(f.group(1)) if f else None}


def reconcile_view(hooks, state, sessions, work):
    """Run --reconcile on a symlinked copy of the transcripts under a folder name without `handoff-canary`
    (the real reconcile skips that name on purpose). Read only; uses a temp HOME."""
    rh = os.path.join(work, "reconcile-home-%s" % rand_hex(4))
    proj_root = os.path.join(rh, ".claude", "projects")
    os.makedirs(proj_root)
    seen = set()
    for s in sessions:
        t = s.main_transcript()
        if t and os.path.dirname(t) not in seen:
            seen.add(os.path.dirname(t))
            os.symlink(os.path.dirname(t), os.path.join(proj_root, "-proof-%s" % rand_hex(6)))
    out = run_tool(hooks, ["--reconcile", "--days", "1"], {"HANDOFF_STATE_DIR": state}, home=rh)
    rj = {}
    try:
        rj = json.load(open(os.path.join(state, "reconcile.json"), encoding="utf-8"))
    except Exception:
        pass
    return out, rj


def settle(hooks, secs=100):
    """Wait until no hook process of this temp install is still running (a killed hook may finish later)."""
    end = time.time() + secs
    while time.time() < end:
        p = subprocess.run(["pgrep", "-f", os.path.join(hooks, "precompact-handoff.py")], capture_output=True, text=True)
        if not p.stdout.strip():
            return True
        time.sleep(3)
    return False


def receipts_of(state):
    rows, bad = jsonl(os.path.join(state, "receipts.jsonl"))
    return rows, bad


def rkey(r):
    return (str(r.get("sid") or r.get("session_id") or ""), r.get("agent_id") or "")


def state_jsonl_check(state):
    bad_total, files = 0, 0
    for p in glob.glob(os.path.join(state, "**", "*.jsonl"), recursive=True):
        files += 1
        bad_total += jsonl(p)[1]
    return files, bad_total


def new_proj(work, label):
    d = os.path.join(work, "handoff-canary-%s-%s" % (label, rand_hex(4)))
    os.makedirs(d)
    return d


# ---------------------------------------------------------------- layer 5
def e2e_run(i, args, budget, work):
    hooks = args.hooks
    state = os.path.join(work, "state-e2e%d" % i)
    os.makedirs(state)
    settings = os.path.join(work, "settings-e2e%d.json" % i)
    build_settings(args.settings, settings, hooks)
    proj = new_proj(work, "e2e%d" % i)
    make_files(proj, "main", 8, seed=100 + i)
    make_files(proj, "sub", 8, seed=200 + i)
    s = Session("e2e%d" % i, proj, settings, state, budget)
    codes = {}
    # first turn always holds a main-only code that the subagent never sees
    drive_main(s, codes, max_turns=5, need_sub=True)
    settle(hooks, 60)
    res = {"run": i, "sid": s.sid[:8], "cost": round(s.cost, 4), "turns": s.turns, "errors": s.errors,
           "codes": codes, "checks": {}}
    nm, ns = s.main_boundaries(), s.sub_boundaries()
    res["main_compactions"], res["sub_compactions"] = nm, ns
    rows, bad = receipts_of(state)
    rm = sum(1 for r in rows if rkey(r)[0].startswith(s.sid[:8]) and not rkey(r)[1])
    rs = sum(1 for r in rows if rkey(r)[0].startswith(s.sid[:8]) and rkey(r)[1])
    res["receipts_main"], res["receipts_sub"] = rm, rs
    sub_files = glob.glob(os.path.join(state, s.sid + "-a-*"))
    leak = []
    for fp in sub_files:
        try:
            txt = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for c in codes.get("main", []):
            if c in txt:
                leak.append("%s holds %s" % (os.path.basename(fp)[:60], c))
    res["sub_handoff_files"], res["leaks"] = len(sub_files), leak
    line, full = prove(hooks, state)
    res["prove_line"], res["prove"] = line, parse_prove(line)
    p = res["prove"]
    ok_ratio = lambda t: t is not None and t[0] == t[1] and t[1] > 0  # noqa: E731
    c = res["checks"]
    c["main_compaction"] = nm >= 1
    c["sub_compaction"] = ns >= 1
    c["main_receipts"] = rm >= nm and nm >= 1
    c["sub_receipts"] = rs >= ns and ns >= 1
    c["sub_handoff_file"] = len(sub_files) >= 1
    c["no_main_code_in_sub_handoff"] = not leak and len(sub_files) >= 1
    c["prove_right_agent"] = ok_ratio(p["right_agent"])
    c["prove_delivered"] = ok_ratio(p["delivered"])
    c["prove_failed_0"] = p["failed"] == 0
    c["no_turn_errors"] = not s.errors
    res["ok"] = all(c.values())
    res["state"] = state
    res["sessions"] = [s]
    return res


# ---------------------------------------------------------------- layer 6
def stress_parallel(args, budget, work):
    hooks = args.hooks
    state = os.path.join(work, "state-par")
    os.makedirs(state)
    settings = os.path.join(work, "settings-par.json")
    build_settings(args.settings, settings, hooks)
    shared = new_proj(work, "par-shared")
    solo = new_proj(work, "par-solo")
    for d, sd in ((shared, 300), (solo, 400)):
        make_files(d, "main", 8, seed=sd)
    ss = [Session("par%d" % k, d, settings, state, budget) for k, d in enumerate((shared, shared, solo))]
    codes = [{} for _ in ss]
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda a: drive_main(a[0], a[1], max_turns=5), zip(ss, codes)))
    settle(hooks, 90)
    res = {"name": "parallel", "sessions_info": [], "checks": {}, "errors": [e for s in ss for e in s.errors]}
    rows, bad_r = receipts_of(state)
    healths, _ = jsonl(os.path.join(state, "health.jsonl"))
    own = True
    for s in ss:
        n = s.main_boundaries()
        has_h = os.path.exists(os.path.join(state, s.sid + "-latest.md")) or bool(glob.glob(os.path.join(state, s.sid + "*")))
        rc = sum(1 for r in rows if rkey(r)[0].startswith(s.sid[:8]))
        res["sessions_info"].append({"sid": s.sid[:8], "compactions": n, "handoff": has_h, "receipts": rc})
        own = own and n >= 1 and has_h and rc >= 1
    nfiles, nbad = state_jsonl_check(state)
    line, full = prove(hooks, state)
    p = parse_prove(line)
    res.update({"prove_line": line, "prove": p, "jsonl_files": nfiles, "jsonl_bad_lines": nbad,
                "cost": round(sum(s.cost for s in ss), 4)})
    c = res["checks"]
    c["all_compacted_with_own_handoff_and_receipt"] = own
    c["every_jsonl_line_parses"] = nbad == 0 and nfiles > 0
    c["prove_failed_0"] = p["failed"] == 0
    c["prove_decisions_safe_all"] = p["decisions_safe"] is not None and p["decisions_safe"][0] == p["decisions_safe"][1] and p["decisions_safe"][1] > 0
    c["no_turn_errors"] = not res["errors"]
    res["ok"] = all(c.values())
    res["state"] = state
    res["sessions"] = ss
    return res


def stress_killed(args, budget, work):
    hooks = args.hooks
    state = os.path.join(work, "state-killed")
    os.makedirs(state)
    settings = os.path.join(work, "settings-killed.json")
    build_settings(args.settings, settings, hooks, precompact_timeout=2)
    proj = new_proj(work, "killed")
    make_files(proj, "main", 8, seed=500)
    s = Session("killed", proj, settings, state, budget, {"HANDOFF_SUMMARY_TIMEOUT": "60"})
    codes = {}
    drive_main(s, codes, max_turns=5)
    settled = settle(hooks, 110)
    res = {"name": "killed-hook", "checks": {}, "errors": s.errors, "cost": round(s.cost, 4), "settled": settled}
    n = s.main_boundaries()
    rows, _ = receipts_of(state)
    mine = [r for r in rows if rkey(r)[0].startswith(s.sid[:8])]
    healths, _ = jsonl(os.path.join(state, "health.jsonl"))
    hrows = [h for h in healths if str(h.get("sid") or "").startswith(s.sid[:8])]
    out, rj = reconcile_view(hooks, state, [s], work)
    line, full = prove(hooks, state)
    p = parse_prove(line)
    res.update({"compactions": n, "receipts": len(mine), "health_rows": len(hrows),
                "health_status": [str(h.get("status") or h.get("trigger")) for h in hrows][:4],
                "reconcile": out.splitlines()[:3], "prove_line": line, "prove": p})
    c = res["checks"]
    c["compacted"] = n >= 1
    c["handoff_injected"] = len(mine) >= 1
    c["health_or_reconcile_shows_it"] = bool(hrows) or ("%s" % s.sid[:8]) in out or bool(rj)
    c["prove_failed_0"] = p["failed"] == 0
    c["no_turn_errors"] = not s.errors
    res["ok"] = all(c.values())
    res["state"] = state
    res["sessions"] = [s]
    return res


def stress_missing(args, budget, work):
    hooks = args.hooks
    state = os.path.join(work, "state-missing")
    os.makedirs(state)
    settings = os.path.join(work, "settings-missing.json")
    build_settings(args.settings, settings, hooks, mode="missing")
    proj = new_proj(work, "missing")
    make_files(proj, "main", 8, seed=600)
    s = Session("missing", proj, settings, state, budget)
    codes = {}
    drive_main(s, codes, max_turns=5)
    settle(hooks, 30)
    res = {"name": "missing-hook", "checks": {}, "errors": s.errors, "cost": round(s.cost, 4)}
    n = s.main_boundaries()
    rows, _ = receipts_of(state)
    mine = [r for r in rows if rkey(r)[0].startswith(s.sid[:8])]
    first = (mine[0].get("part1_head") or "") if mine else ""
    out, rj = reconcile_view(hooks, state, [s], work)
    res.update({"compactions": n, "receipts": len(mine), "first_text": first[:60], "reconcile": out.splitlines()[:3],
                "receipt_keys": sorted(mine[0].keys()) if mine else []})
    c = res["checks"]
    c["compacted"] = n >= 1
    c["receipt_exists"] = len(mine) >= 1
    c["starts_with_handoff_recovered"] = first.startswith("HANDOFF RECOVERED")
    c["reconcile_reports_it"] = s.sid[:8] in out or any(x.get("sid") == s.sid[:8] for x in (rj.get("sessions") or []) if isinstance(x, dict))
    c["no_turn_errors"] = not s.errors
    res["ok"] = all(c.values())
    res["state"] = state
    res["sessions"] = [s]
    return res


def strip(res):
    r = dict(res)
    r.pop("sessions", None)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hooks", required=True)
    ap.add_argument("--settings", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="all")
    ap.add_argument("--cap", type=float, default=4.0)
    args = ap.parse_args()
    os.makedirs(args.work, exist_ok=True)
    budget = Budget(args.cap)
    out = {"e2e": None, "stress": None, "cost_total": 0.0, "cap": args.cap, "stopped": None, "turn_costs": []}
    try:
        if args.only in ("e2e", "all"):
            runs = []
            out["e2e"] = {"runs": runs}
            for i in range(1, args.runs + 1):
                log("e2e run %d start" % i)
                r = e2e_run(i, args, budget, args.work)
                runs.append(strip(r))
                log("e2e run %d ok=%s checks=%s" % (i, r["ok"], json.dumps(r["checks"])))
            out["e2e"]["ok"] = all(r["ok"] for r in runs)
        if args.only in ("stress", "all"):
            st = {"scenarios": []}
            out["stress"] = st
            for fn in (stress_parallel, stress_killed, stress_missing):
                log("stress %s start" % fn.__name__)
                r = fn(args, budget, args.work)
                st["scenarios"].append(strip(r))
                log("stress %s ok=%s checks=%s" % (fn.__name__, r["ok"], json.dumps(r["checks"])))
            st["ok"] = all(r["ok"] for r in st["scenarios"])
    except CapReached as e:
        out["stopped"] = "cost cap: %s" % e
        log(out["stopped"])
    out["cost_total"] = round(budget.spent, 4)
    out["turn_costs"] = [(a, round(b, 4)) for a, b in budget.turns]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    if out["stopped"]:
        return 3
    bad = [k for k in ("e2e", "stress") if out[k] is not None and not out[k].get("ok")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
