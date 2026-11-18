#!/usr/bin/env python3
"""OOLONG benchmark harness for Lossless Handoff (Phase 4, P4-1). See README.md.

Subcommands:
  run [options]               start a run
  resume <run-id> [options]   continue a paused run; finished units are not repeated
  selfcheck [options]         prove the sandbox (a stored curl failure) and list the tools of each arm
  cleanup [--all] [--dry-run] delete only this harness's own task folders (exact prefix)

Arms: A plain, Workflow tool disallowed. B plain, task prompt adds one line that permits a workflow.
      C Lossless Handoff hooks, the recall agent and the llm_map skill from --lh-path (alias --lh). N no-context control (arm A settings, no context file).
Each unit is one `claude -p` call in a fresh temp folder. It uses the real HOME (a temp HOME cannot log in).
It never touches ~/.claude settings or hooks, ~/.local/state/claude-handoffs, or launchd.
Exit: 0 finished, 1 a unit failed, 2 usage error, 3 paused (budget reached; resume later).
"""
import argparse, concurrent.futures, datetime, glob, hashlib, json, os, random, re, shutil, signal, subprocess
import sys, tempfile, threading, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "continuity"))
import scorer  # noqa: E402
import runner as cont  # noqa: E402  (reuse of lh_settings, deny_rules, count_boundaries, slug, cleanup pattern)

RESULTS = os.path.join(HERE, "results")
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
PREFIX = "lhoo-"
LOCK = json.load(open(os.path.join(HERE, "dataset.lock.json"), encoding="utf-8"))
LEN_NAMES = {"32K": 32768, "128K": 131072, "8K": 8192, "16K": 16384, "64K": 65536, "256K": 262144, "512K": 524288, "1M": 1048576}
# Same tools in every arm. The subagent tool (named Task in this Claude Code version) is allowed in A, B and C.
# The Workflow tool is disallowed in every arm: under `claude -p` a workflow runs in the background and the session can end
# before its result returns (pilot evidence, see README). WebSearch and WebFetch are off everywhere.
ALLOWED_TOOLS = "Read Write Edit Bash Task"
DISALLOWED_BASE = ["WebSearch", "WebFetch", "Workflow"]
WORKFLOW_LINE = "You may split the work across parallel subagents (Agent tool) if it helps; wait for all their results before your final answer."
PROMPT_CORE = ("The file context.txt in the current folder holds a data set. Read it with your tools as needed. "
               "Do not use the network. Answer the question below about the data set.\n\n{question}\n\n"
               "Write the final answer on the last line, in the form the question asks. "
               "Add no period and no other text after it.")
# What may differ between arms. Any other difference stops the run before the first call.
ALLOWED_DIFF = {"A": set(), "B": {"prompt_extra"},
                "C": {"hooks", "env_handoff", "recall_agent", "settings_sha", "llm_map_skill", "llm_map_sandbox_exception"}}
LLM_MAP_SKILL_REL = os.path.join("skills", "llm-map", "SKILL.md")
LLM_MAP_HOOK_REL = os.path.join("hooks", "llm_map.py")
LLM_MAP_SKILL_PATHS = ("~/.claude/hooks/llm_map.py", "$HOME/.claude/hooks/llm_map.py")
LLM_MAP_SUMMARY = re.compile(r"llm_map:[^\n]*?cost \$([0-9]+(?:\.[0-9]+)?)")
DENY_BASH = ["curl", "wget", "pip", "pip3", "huggingface", "hf_hub", "load_dataset", "git clone", "urllib", "requests", "http.client", "urlretrieve"]
# Contamination patterns, searched in the text the model wrote and in its tool inputs. Tool results are not searched:
# they print lines of the context (for example "hair curl"), which are not evidence.
HIT_PATTERNS = [
    ("data set name", r"\btrec(_coarse)?\b"), ("oolong", r"oolong"), ("ground truth", r"ground[ _-]?truth"),
    ("huggingface", r"hugging[ _-]?face|hf_hub|load_dataset|datasets\.load"),
    ("download command", r"\b(curl|wget)\b\s+(-|https?:)|pip3? install|git clone|urllib|requests\.(get|post)|urlretrieve"),
]
URL_PATTERN = ("url in a command", r"https?://")


class Pause(Exception):
    pass


class Fatal(Exception):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ tasks
def load_tasks(cache, dataset, length, n, seed, atypes):
    p = os.path.join(cache, "%s_%d.jsonl" % (dataset, length))
    if not os.path.isfile(p):
        raise SystemExit("no data in %s. Run: python3 fetch.py --lens %d --cache %s" % (p, length, cache))
    rows = [json.loads(x) for x in open(p, encoding="utf-8")]
    if atypes:
        rows = [r for r in rows if r["answer_type"].split(".")[-1] in atypes]
    random.Random("%s-%d-%s" % (dataset, length, seed)).shuffle(rows)
    return [{"task_id": str(r["id"]), "length": length, "window_id": r["context_window_id"], "question": r["question"],
             "answer": r["answer"], "answer_type": r["answer_type"], "task": r["task"], "context": r["context_window_text"]}
            for r in rows[:n]]


def llm_map_prefix(lh):
    """The exact command start that arm C may run outside the sandbox."""
    return "python3 %s" % os.path.join(lh, LLM_MAP_HOOK_REL)


def llm_map_excluded(cfg, arm):
    """sandbox.excludedCommands of arm C: the one llm_map command, and nothing else. Empty for the other arms."""
    return [llm_map_prefix(cfg["lh_path"]) + " *"] if arm == "C" else []


def require_llm_map(lh):
    """Arm C needs the llm_map hook and skill in the checkout. Stop with a clear message if one is missing or unsafe."""
    for rel in (LLM_MAP_SKILL_REL, LLM_MAP_HOOK_REL):
        if not os.path.isfile(os.path.join(lh, rel)):
            raise SystemExit("arm C: %s is missing in %s. Use --lh-path with a checkout that has llm_map (Lossless Handoff 0.12.0 or later)." % (rel, lh))
    pre = llm_map_prefix(lh)
    bad = [w for w in DENY_BASH + ["oolong"] if w in pre]
    if bad:
        raise SystemExit("arm C: the llm_map path %s holds the denied word %r. The Bash deny rules would block it. Use a checkout under another path." % (pre, bad[0]))
    for g in (os.path.realpath(HERE),):
        if g in pre:
            raise SystemExit("arm C: the llm_map path is inside the bench folder, which is denied.")


def install_llm_map_skill(lh, proj):
    """Copy the skill into the task project. Replace the user-level hook path with the absolute path of this checkout."""
    require_llm_map(lh)
    text = open(os.path.join(lh, LLM_MAP_SKILL_REL), encoding="utf-8").read()
    hook = os.path.join(lh, LLM_MAP_HOOK_REL)
    for old in LLM_MAP_SKILL_PATHS:
        text = text.replace(old, hook)
    if hook not in text:
        raise SystemExit("arm C: the skill file in %s does not name llm_map.py by its install path (%s). The harness cannot point it at the checkout." % (lh, LLM_MAP_SKILL_PATHS[0]))
    d = os.path.join(proj, ".claude", "skills", "llm-map")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(text)


def trusted_settings(cfg, arm, env_over):
    """The --settings JSON. excludedCommands counts only from a trusted tier (the --settings file), not from the project file."""
    s = {"env": env_over}
    ex = llm_map_excluded(cfg, arm)
    if ex:
        s["sandbox"] = {"excludedCommands": ex}
    return s


def claude_flags(arm):
    """Skills are off in A and B (--disable-slash-commands). Arm C needs the project skill, so the flag is dropped there."""
    return [] if arm == "C" else ["--disable-slash-commands"]


def prompt_for(arm, question):
    core = PROMPT_CORE.format(question=question)
    return core + ("\n\n" + WORKFLOW_LINE if arm == "B" else "")


# ------------------------------------------------------------------ settings and config record
def deny_rules(cfg, siblings=()):
    """Base rules are the same for every unit. siblings: folders of the other units of this run (Read tool cannot be
    limited by the sandbox, so each other folder gets its own deny rule)."""
    rules = list(cont.deny_rules(cfg["work_base"]))
    for sdir in siblings:
        for tool in ("Read", "Grep", "Glob"):
            rules.append("%s(/%s/**)" % (tool, sdir))
    gold = [os.path.realpath(HERE), os.path.realpath(cfg["cache"])]
    for g in gold:
        for tool in ("Read", "Grep", "Glob", "Edit", "Write"):
            rules.append("%s(/%s/**)" % (tool, g))
        rules.append("Bash(*%s*)" % g)
    rules += ["Bash(*%s*)" % w for w in DENY_BASH] + ["Bash(*oolong*)", "WebSearch", "WebFetch"]
    return sorted(set(rules))


def project_settings(cfg, arm, sandbox_only=False, proj=None, siblings=()):
    """sandbox_only is for selfcheck: it drops the Bash deny words so that a curl really runs and the sandbox must stop it."""
    s = {}
    if arm == "C":
        s = cont.lh_settings(os.path.join(cfg["lh_path"], "hooks"))
    s["permissions"] = {"deny": [r for r in deny_rules(cfg, siblings) if not (sandbox_only and r.startswith("Bash(*") and any(w in r for w in DENY_BASH))]}
    s["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                    "network": {"allowedDomains": []}}
    if proj:   # Bash may read only its own folder: not the other task folders, the gold, the results or the transcripts
        s["sandbox"]["filesystem"] = {"denyRead": sorted({cfg["work_base"], os.path.realpath(HERE), os.path.realpath(cfg["cache"]), PROJECTS}),
                                      "allowRead": [proj]}
    return s


def env_for(cfg, arm, statedir):
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(cfg["window"])}
    if arm == "C":
        env.update({"HANDOFF_STATE_DIR": statedir, "HANDOFF_SUMMARY_MODEL": cfg["model"], "RECALL_EMBED_URL": "http://127.0.0.1:9"})
    return env


def config_record(cfg, task, arm):
    """What this unit runs with. Paths that differ per folder are left out. Compared across arms by check_fairness."""
    s = project_settings(cfg, arm)
    core_arm = "A" if arm == "N" else arm
    env = env_for(cfg, core_arm, "<state>")
    disallowed = list(DISALLOWED_BASE)
    base_settings = {k: v for k, v in s.items() if k != "hooks"}
    return {
        "model": cfg["model"], "claude_version": cfg["claude_version"], "permission_mode": "acceptEdits",
        "allowed_tools": ALLOWED_TOOLS, "disallowed_tools": sorted(disallowed), "call_cap_usd": cfg["call_cap_usd"],
        "timeout_s": cfg["timeout_s"], "window": cfg["window"], "setting_sources": "project", "mcp": "none",
        "slash_commands": "disabled", "sandbox": s["sandbox"], "deny_rules_sha": sha(s["permissions"]),
        "prompt_core_sha": sha(PROMPT_CORE.format(question=task["question"])),
        "prompt_extra": WORKFLOW_LINE if core_arm == "B" else "",
        "env_window": env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
        "env_handoff": sorted(k for k in env if k != "CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
        "hooks": sorted(s.get("hooks", {}).keys()), "recall_agent": core_arm == "C",
        "llm_map_skill": ({"project_skill": "llm-map", "skills_flag_dropped": True} if core_arm == "C" else None),
        "llm_map_sandbox_exception": llm_map_excluded(cfg, core_arm) or None,
        "base_settings_sha": sha(base_settings), "settings_sha": sha(s),
        "context_file": arm != "N", "arm": arm,
    }


def check_fairness(records):
    """records: {(task_id, arm): record}. Return a list of problems. Empty list means only the intended setting differs."""
    problems = []
    for (tid, arm), rec in records.items():
        if arm in ("A", "N"):
            continue
        ref = records.get((tid, "A"))
        if ref is None:
            continue
        for k in sorted(set(rec) | set(ref)):
            if k in ("arm", "context_file"):
                continue
            if rec.get(k) != ref.get(k) and k not in ALLOWED_DIFF[arm]:
                problems.append("task %s arm %s differs from A in '%s': %r vs %r" % (tid, arm, k, rec.get(k), ref.get(k)))
    for (tid, arm), rec in records.items():
        if arm == "N":
            ref = records.get((tid, "A"))
            if ref is not None:
                for k in sorted(set(rec) | set(ref)):
                    if k not in ("arm", "context_file") and rec.get(k) != ref.get(k):
                        problems.append("task %s control differs from A in '%s'" % (tid, k))
    return problems


# ------------------------------------------------------------------ transcript analysis
def transcript_files(proj, sid):
    d = os.path.join(PROJECTS, cont.slug(proj))
    main = os.path.join(d, sid + ".jsonl")
    return main, sorted(glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True))


def iter_text_fields(records):
    """Yield (where, text) for text the model wrote and its tool inputs. Tool results are left out."""
    tool_names = {}
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if r.get("type") == "assistant":
                if t == "text":
                    yield "assistant text", b.get("text", "")
                elif t == "thinking":
                    yield "assistant thinking", b.get("thinking", "")
                elif t == "tool_use":
                    tool_names[b.get("id")] = b.get("name")
                    yield "tool input (%s)" % b.get("name"), json.dumps(b.get("input", {}))


def scan_records(records, final_text=""):
    hits = []
    pats = [(n, re.compile(p, re.I)) for n, p in HIT_PATTERNS]
    url = (URL_PATTERN[0], re.compile(URL_PATTERN[1], re.I))
    fields = list(iter_text_fields(records)) + [("final answer", final_text or "")]
    for where, text in fields:
        for name, rx in pats + ([url] if where.startswith("tool input") else []):
            m = rx.search(text)
            if m:
                a = max(0, m.start() - 50)
                hits.append({"pattern": name, "where": where, "excerpt": text[a:m.end() + 50].replace("\n", " ")})
    return hits


def llm_map_stats(records):
    """llm_map calls: Bash tool calls whose command holds llm_map.py. Cost: summed from the summary lines in the tool results
    (`llm_map: ... cost $x`). The main session's total_cost_usd does not include these child calls."""
    calls, costs = 0, []
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if not isinstance(b, dict):
                continue
            if r.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("name") == "Bash" \
                    and "llm_map.py" in str((b.get("input") or {}).get("command", "")):
                calls += 1
            elif r.get("type") == "user" and b.get("type") == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else ""
                costs += [float(m) for m in LLM_MAP_SUMMARY.findall(txt)]
    return {"llm_map_calls": calls, "llm_map_cost_usd": round(sum(costs), 6), "llm_map_summaries": len(costs)}


def tool_counts(records):
    n = {}
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if r.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n[b.get("name")] = n.get(b.get("name"), 0) + 1
    return n


# ------------------------------------------------------------------ one unit
class Ctx:
    def __init__(self, run_dir, cfg):
        self.dir, self.cfg = run_dir, cfg
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state_path = os.path.join(run_dir, "state.json")
        self.stems = []
        self.state = {"spent": 0.0, "status": "running", "units": {}}
        if os.path.exists(self.state_path):
            self.state = json.load(open(self.state_path, encoding="utf-8"))

    def save(self):
        with self.lock:
            cont.write_json(self.state_path, self.state)

    def append(self, fname, obj):
        with self.lock:
            with open(os.path.join(self.dir, fname), "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, sort_keys=True) + "\n")

    def check_budget(self):
        b = self.cfg.get("budget_usd")
        if self.stop.is_set():
            raise Pause()
        # headroom: one call can pass its cap (a $0.40 cap once spent $0.49), so stop early by the largest unit cost seen
        if b is not None and self.state["spent"] >= b - self.state.get("max_unit_cost", 0.0):
            self.stop.set()
            raise Pause()


def run_unit(ctx, task, arm, rec):
    cfg = ctx.cfg
    uname = "%s-%s" % (task["task_id"], arm)
    if ctx.state["units"].get(uname, {}).get("status") == "done":
        return
    ctx.check_budget()
    core_arm = "A" if arm == "N" else arm
    stem = "%s%s-%s-%s" % (PREFIX, cfg["run_id"], task["task_id"], arm)
    proj = os.path.join(cfg["work_base"], stem)
    statedir = proj + "-state"
    if os.path.isdir(proj):
        shutil.rmtree(proj)
    os.makedirs(os.path.join(proj, ".claude"))
    os.makedirs(statedir, exist_ok=True)
    proj = os.path.realpath(proj)
    sibs = sorted(os.path.join(cfg["work_base"], x) for x in ctx.stems if x != stem)
    sibs += [x + "-state" for x in sibs]
    cont.write_json(os.path.join(proj, ".claude", "settings.json"), project_settings(cfg, core_arm, proj=proj, siblings=sibs))
    subprocess.run(["git", "init", "-q", proj], capture_output=True, timeout=30)
    if core_arm == "C":
        install_llm_map_skill(cfg["lh_path"], proj)
        src = os.path.join(cfg["lh_path"], "agents", "recall.md")
        if os.path.isfile(src):
            os.makedirs(os.path.join(proj, ".claude", "agents"), exist_ok=True)
            shutil.copyfile(src, os.path.join(proj, ".claude", "agents", "recall.md"))
    if arm != "N":
        with open(os.path.join(proj, "context.txt"), "w", encoding="utf-8") as f:
            f.write(task["context"])
    sid = str(uuid.uuid4())
    env_over = env_for(cfg, core_arm, statedir)
    disallowed = list(DISALLOWED_BASE)
    cmd = ["claude", "-p", "--session-id", sid, "--model", cfg["model"], "--setting-sources", "project",
           "--settings", json.dumps(trusted_settings(cfg, core_arm, env_over)), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           *claude_flags(core_arm), "--output-format", "json", "--permission-mode", "acceptEdits",
           "--allowedTools", ALLOWED_TOOLS, "--disallowedTools", " ".join(disallowed),
           "--max-budget-usd", "%.2f" % cfg["call_cap_usd"], "--", prompt_for(core_arm, task["question"])]
    env = {k: v for k, v in os.environ.items() if not (k.startswith("HANDOFF_") or k == "CLAUDE_CODE_AUTO_COMPACT_WINDOW")}
    env.update(env_over)
    ctx.state["units"][uname] = {"status": "running"}
    o, err_txt, wall = None, "", 0.0
    for attempt in (1, 2):
        ctx.check_budget()
        t0 = time.time()
        p = subprocess.Popen(cmd, cwd=proj, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        try:
            out, err_txt = p.communicate(timeout=cfg["timeout_s"])
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
            p.communicate()
            out, err_txt = "", "timeout"
        wall = time.time() - t0
        try:
            o = json.loads(out)
            if not isinstance(o, dict):
                raise ValueError
            break
        except ValueError:
            o = None
            if "not logged in" in (out + err_txt).lower():
                raise Fatal("claude is not logged in")
            if err_txt == "timeout":
                break           # a timeout is a result (score 0), not a reason to run twice
    cost = float(o.get("total_cost_usd") or 0) if o else 0.0
    with ctx.lock:
        ctx.state["spent"] += cost
        ctx.state["max_unit_cost"] = max(ctx.state.get("max_unit_cost", 0.0), cost)
    result_text = (o or {}).get("result") or ""
    sc = scorer.score(task["answer"], task["answer_type"], result_text if isinstance(result_text, str) else "")
    main, _all = transcript_files(proj, sid)
    recs = []
    for f in _all:
        recs += cont.read_jsonl(f)
    boundaries, _pre = cont.count_boundaries(main)
    tc = tool_counts(recs)
    lm = llm_map_stats(recs)
    hits = scan_records(recs, result_text if isinstance(result_text, str) else "")
    tdir = os.path.join(ctx.dir, "transcripts")
    os.makedirs(tdir, exist_ok=True)
    if os.path.isfile(main):
        shutil.copyfile(main, os.path.join(tdir, uname + ".jsonl"))
    usage = (o or {}).get("modelUsage") or {}
    row = {
        "unit": uname, "task_id": task["task_id"], "length": task["length"], "arm": arm, "answer_type": task["answer_type"],
        "task": task["task"], "score": sc["score"], "parsed": sc["parsed"], "parse_confidence": sc["parse_confidence"],
        "gold": sc["gold"], "full_answer": result_text, "cost_usd": cost, "wall_s": round(wall, 1),
        "num_turns": (o or {}).get("num_turns"), "workflow_calls": tc.get("Workflow", 0),
        "subagent_calls": tc.get("Task", 0) + tc.get("Agent", 0), "tool_counts": tc, "compactions": boundaries,
        **lm, "hits": hits, "session_id": sid, "is_error": bool((o or {}).get("is_error")) or o is None,
        "subtype": (o or {}).get("subtype"), "stderr": (err_txt or "")[:300] if o is None else "",
        "models": sorted(usage), "config": rec, "config_sha": sha(rec), "finished": now_iso(),
    }
    ctx.append("results.jsonl", row)
    for dd in (proj, statedir):      # the task folder is no longer needed; the transcript is copied
        shutil.rmtree(dd, ignore_errors=True)
    with ctx.lock:
        ctx.state["units"][uname] = {"status": "done" if o is not None else "failed", "cost": cost}
    ctx.save()
    print("  %-22s score %.2f cost $%.3f %5.0fs turns %s wf %d hits %d llm_map %d ($%.3f)%s" % (
        uname, sc["score"], cost, wall, row["num_turns"], row["workflow_calls"], len(hits), lm["llm_map_calls"], lm["llm_map_cost_usd"],
        "  ERROR" if row["is_error"] else ""), flush=True)


# ------------------------------------------------------------------ commands
def decision_committed():
    p = os.path.join("bench", "oolong", "decision.json")
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", p], cwd=REPO, capture_output=True).returncode == 0
    clean = not sh(["git", "status", "--porcelain", "--", p], cwd=REPO)
    return tracked and clean, sh(["git", "log", "-1", "--format=%h", "--", p], cwd=REPO)


def parse_common(r):
    r.add_argument("--model", default="haiku", help="pilot haiku; slice sonnet; full opus")
    r.add_argument("--lengths", default="32K", help="comma list: 32K,128K (the data set's own windows)")
    r.add_argument("--tasks", type=int, default=2, help="tasks per length")
    r.add_argument("--arms", default="A,B", help="comma list from A,B,C")
    r.add_argument("--no-control", action="store_true", help="skip the no-context control (not for a scored run)")
    r.add_argument("--answer-types", help="optional filter, e.g. LABEL,NUMERIC,COMPARISON,USER")
    r.add_argument("--seed", default="1")
    r.add_argument("--window", type=int, default=200000, help="CLAUDE_CODE_AUTO_COMPACT_WINDOW, the same in all arms")
    r.add_argument("--lh-path", "--lh", dest="lh_path", help="Lossless Handoff checkout for arm C: hooks, recall agent, llm_map hook and skill (default: this repo)")
    r.add_argument("--cache", default=os.environ.get("OOLONG_CACHE") or os.path.join(HERE, "cache"), help="folder made by fetch.py; holds the gold")
    r.add_argument("--work-base", help="folder for task folders (default: the temp dir)")
    r.add_argument("--call-cap-usd", type=float, default=0.60, help="--max-budget-usd of each claude call")
    r.add_argument("--timeout-s", type=int, default=900, help="time limit of each call, the same in all arms")
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--budget-usd", type=float, default=None, help="pause cleanly: no new unit starts when the total reaches this value minus the largest unit cost seen so far")


def build_cfg(a):
    arms = a.arms.split(",")
    if any(x not in ("A", "B", "C") for x in arms) or "A" not in arms:
        raise SystemExit("--arms: a comma list from A,B,C that includes A (the base for pairing)")
    lens = [LEN_NAMES[x] if x in LEN_NAMES else int(x) for x in a.lengths.split(",")]
    lh = os.path.realpath(a.lh_path or REPO)
    if "C" in arms and not os.path.isfile(os.path.join(lh, "hooks", "precompact-handoff.py")):
        raise SystemExit("arm C: no hooks in %s/hooks" % lh)
    if "C" in arms:
        require_llm_map(lh)
    ok, dcommit = decision_committed()
    if not ok:
        raise SystemExit("refuse: bench/oolong/decision.json is not committed. Commit it before any scored run.")
    return {
        "run_id": datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), "model": a.model, "lengths": lens, "tasks": a.tasks,
        "arms": arms, "control": not a.no_control, "answer_types": a.answer_types.split(",") if a.answer_types else None,
        "seed": a.seed, "window": a.window, "lh_path": lh, "cache": os.path.realpath(a.cache),
        "lh_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=lh), "bench_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=REPO),
        "decision_commit": dcommit, "dataset": LOCK["dataset"], "hf_revision": LOCK["hf_revision"], "oolong_commit": LOCK["oolong_commit"],
        "work_base": os.path.realpath(a.work_base or os.environ.get("LHOO_WORK") or tempfile.gettempdir()),
        "call_cap_usd": a.call_cap_usd, "timeout_s": a.timeout_s, "jobs": a.jobs, "budget_usd": a.budget_usd,
        "claude_version": sh(["claude", "--version"]), "started": now_iso(), "command": " ".join(sys.argv),
    }


def plan(cfg):
    tasks = []
    for ln in cfg["lengths"]:
        tasks += load_tasks(cfg["cache"], cfg["dataset"], ln, cfg["tasks"], cfg["seed"], cfg["answer_types"])
    arms = cfg["arms"] + (["N"] if cfg["control"] else [])
    recs = {(t["task_id"], arm): config_record(cfg, t, arm) for t in tasks for arm in arms}
    return tasks, arms, recs


def execute(ctx, tasks, arms, recs):
    cfg = ctx.cfg
    probs = check_fairness(recs)
    if probs:
        print("FAIRNESS CHECK FAILED:\n  " + "\n  ".join(probs), file=sys.stderr)
        return 1
    print("fairness check: only the intended settings differ across arms (%d config records)" % len(recs))
    code = 0
    ctx.state["status"] = "running"
    ctx.save()
    units = [(t, arm) for t in tasks for arm in arms]
    ctx.stems = ["%s%s-%s-%s" % (PREFIX, cfg["run_id"], t["task_id"], arm) for t, arm in units]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["jobs"]) as ex:
            futs = [ex.submit(run_unit, ctx, t, arm, recs[(t["task_id"], arm)]) for t, arm in units]
            for f in futs:
                try:
                    f.result()
                except Pause:
                    pass
                except Fatal as e:
                    print("FATAL:", e, file=sys.stderr)
                    code = 1
                except Exception as e:
                    print("ERROR:", repr(e), file=sys.stderr)
                    code = 1
    except KeyboardInterrupt:
        ctx.stop.set()
    undone = [u for t, arm in units for u in ["%s-%s" % (t["task_id"], arm)] if ctx.state["units"].get(u, {}).get("status") != "done"]
    if ctx.stop.is_set() and undone:
        ctx.state["status"], code = "paused", 3
    elif undone:
        ctx.state["status"], code = "failed", code or 1
    else:
        ctx.state["status"] = "done"
    ctx.state["ended"] = now_iso()
    ctx.save()
    print("run %s: %s, spent $%.4f, units not finished: %s" % (cfg["run_id"], ctx.state["status"], ctx.state["spent"], undone or "none"))
    if code == 3:
        print("paused. Continue with: run.sh resume %s [--budget-usd X]" % cfg["run_id"])
    return code


def save_tasks(run_dir, tasks):
    """The gold stays in the results folder (denied to task sessions). The context text is not copied."""
    cont.write_json(os.path.join(run_dir, "tasks.json"), [{k: v for k, v in t.items() if k != "context"} for t in tasks])


def cmd_run(a):
    cfg = build_cfg(a)
    tasks, arms, recs = plan(cfg)
    run_dir = os.path.join(RESULTS, cfg["run_id"])
    os.makedirs(run_dir)
    cont.write_json(os.path.join(run_dir, "config.json"), cfg)
    save_tasks(run_dir, tasks)
    cont.write_json(os.path.join(run_dir, "config_records.json"), {"%s|%s" % k: v for k, v in recs.items()})
    open(os.path.join(RESULTS, ".last"), "w").write(cfg["run_id"])
    print("run", cfg["run_id"], "model", cfg["model"], "lengths", cfg["lengths"], "tasks", [t["task_id"] for t in tasks], "arms", arms)
    return execute(Ctx(run_dir, cfg), tasks, arms, recs)


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
    cont.write_json(os.path.join(run_dir, "config.json"), cfg)
    tasks, arms, recs = plan(cfg)
    ctx = Ctx(run_dir, cfg)
    for u in ctx.state["units"].values():
        if u["status"] in ("running", "failed"):
            u["status"] = "new"
    ctx.save()
    return execute(ctx, tasks, arms, recs)


def cmd_cleanup(a):
    """Delete only <base>/lhoo-* and ~/.claude/projects/<slug(base/lhoo-)>*. Folders of a paused or failed run stay, unless --all."""
    base = os.path.realpath(a.work_base or os.environ.get("LHOO_WORK") or tempfile.gettempdir())
    work_prefix = os.path.join(base, PREFIX)
    proj_prefix = cont.slug(work_prefix)
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
                    and not any(n.startswith(cont.slug(os.path.join(base, x))) for x in protected):
                todo.append(p)
    except OSError:
        pass
    for p in todo:
        print(("would delete " if a.dry_run else "delete ") + p)
        if not a.dry_run:
            shutil.rmtree(p)
    print("%s %d folder(s); kept %d run(s) that can still resume" % ("would delete" if a.dry_run else "deleted", len(todo), len(protected)))
    return 0


def selfcheck_llm_map(cfg, a, ev):
    """The excluded command must be a Bash call of its own: a chain (a && b, a ; b), a cd, a redirect or a command substitution keeps the call
    in the sandbox. Arm C, one session with no Bash deny words (so a real curl runs): llm_map on 3 items must succeed from inside the sandbox
    (it is the one excluded command), and a curl to example.com in the same session must still fail."""
    proj = os.path.realpath(os.path.join(cfg["work_base"], PREFIX + "selfcheck-llmmap"))
    os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
    cont.write_json(os.path.join(proj, ".claude", "settings.json"), project_settings(cfg, "C", sandbox_only=True, proj=proj))
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    install_llm_map_skill(cfg["lh_path"], proj)
    env_over = env_for(cfg, "C", proj + "-state")
    env = {k: v for k, v in os.environ.items() if not (k.startswith("HANDOFF_") or k == "CLAUDE_CODE_AUTO_COMPACT_WINDOW")}
    env.update(env_over)
    items = ["apple", "carrot", "salmon"]
    open(os.path.join(proj, "items.txt"), "w").write("\n".join(items) + "\n")
    cmd_lm = "%s --in items.txt --prompt 'Say fruit, vegetable or fish for each item.' --out labels.jsonl" % llm_map_prefix(cfg["lh_path"])
    prompt = ("Do three things, each in its own Bash call (never chain them), and report the raw output of each. "
              "1) Run exactly this command and nothing else: %s . 2) Run: cat labels.jsonl . "
              "3) Run: curl -sS -m 10 -o /dev/null -w '%%{http_code}' https://example.com ; echo EXIT=$? . Do not change the commands." % cmd_lm)
    r = subprocess.run(["claude", "-p", "--model", a.model, "--setting-sources", "project", "--settings", json.dumps(trusted_settings(cfg, "C", env_over)),
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format", "json", "--permission-mode", "acceptEdits",
                        "--allowedTools", ALLOWED_TOOLS, "--disallowedTools", " ".join(DISALLOWED_BASE), "--max-budget-usd", "0.30", "--", prompt],
                       cwd=proj, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    o = json.loads(r.stdout)
    res = o.get("result") or ""
    labels = []
    try:
        labels = [json.loads(x) for x in open(os.path.join(proj, "labels.jsonl")).read().splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    lm_ok = len(labels) == 3 and all(x.get("out") for x in labels) and bool(re.search(r"llm_map: 3 items, 3 ok", res))
    curl_failed = not re.search(r"(?<!\d)200(?!\d)", res) and bool(re.search(r"000|EXIT=[1-9]|denied|refused|resolve|blocked", res, re.I))
    ev["llm_map_in_sandbox"] = {"reply": res[:1800], "labels_file": labels, "llm_map_ok": lm_ok, "curl_still_fails": curl_failed,
                                "permission_denials": o.get("permission_denials"), "cost_usd": o.get("total_cost_usd"),
                                "excluded_commands": llm_map_excluded(cfg, "C"), "settings_tier": "--settings JSON (trusted tier)"}
    print("llm_map in sandbox: ok %s; curl still fails %s\n  reply: %s" % (lm_ok, curl_failed, res.replace("\n", " | ")[:500]))
    shutil.rmtree(proj, ignore_errors=True)
    return lm_ok and curl_failed


def cmd_selfcheck(a):
    """Live proof of the sandbox. For each arm: list the tools, run one curl from inside a task session (it must fail),
    and try to read a file in the gold cache (it must be denied). Stores evidence/sandbox-<time>.json."""
    cfg = {"run_id": "selfcheck" + uuid.uuid4().hex[:6], "model": a.model, "window": 200000, "lh_path": os.path.realpath(a.lh_path or REPO),
           "cache": os.path.realpath(a.cache), "work_base": os.path.realpath(a.work_base or os.environ.get("LHOO_WORK") or tempfile.gettempdir()),
           "call_cap_usd": 0.30, "timeout_s": 300, "claude_version": sh(["claude", "--version"])}
    ev = {"time": now_iso(), "claude_version": cfg["claude_version"], "model": a.model, "arms": {}}
    ok = True
    if "C" in a.arms.split(","):
        require_llm_map(cfg["lh_path"])
        ev["lh_path"], ev["lh_commit"] = cfg["lh_path"], sh(["git", "rev-parse", "--short", "HEAD"], cwd=cfg["lh_path"])
    gold_probe = os.path.join(cfg["cache"], "probe-gold.txt")
    os.makedirs(cfg["cache"], exist_ok=True)
    open(gold_probe, "w").write("GOLDPROBE-" + uuid.uuid4().hex[:8])
    token = open(gold_probe).read()
    for arm in a.arms.split(","):
        stem = "%sselfcheck-%s" % (PREFIX, arm)
        proj = os.path.join(cfg["work_base"], stem)
        os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
        proj = os.path.realpath(proj)
        cont.write_json(os.path.join(proj, ".claude", "settings.json"), project_settings(cfg, arm, proj=proj))
        subprocess.run(["git", "init", "-q", proj], capture_output=True)
        if arm == "C":
            install_llm_map_skill(cfg["lh_path"], proj)
        own_token = "OWNTOKEN-" + uuid.uuid4().hex[:8]
        open(os.path.join(proj, "ownfile.txt"), "w").write(own_token)
        disallowed = list(DISALLOWED_BASE)
        env_over = env_for(cfg, arm, proj + "-state")
        base = ["claude", "-p", "--model", a.model, "--setting-sources", "project", "--settings", json.dumps(trusted_settings(cfg, arm, env_over)),
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', *claude_flags(arm), "--permission-mode", "acceptEdits",
                "--allowedTools", ALLOWED_TOOLS, "--disallowedTools", " ".join(disallowed), "--max-budget-usd", "0.30"]
        env = {k: v for k, v in os.environ.items() if not (k.startswith("HANDOFF_") or k == "CLAUDE_CODE_AUTO_COMPACT_WINDOW")}
        env.update(env_over)
        tools, skills = [], []
        r = subprocess.run(base + ["--output-format", "stream-json", "--verbose", "--", "Say ok."], cwd=proj, env=env,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
        for ln in r.stdout.splitlines():
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if j.get("subtype") == "init":
                tools = j.get("tools", [])
                skills = j.get("skills", []) or []
        prompt = ("Do two things in separate tool calls. 1) Run this in Bash: curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.com; echo EXIT=$? . "
                  "If the Bash tool denies the command, try this one instead: python3 -c \"print(__import__('socket').create_connection(('example.com',443),5))\" . "
                  "2) Read the file " + gold_probe + " with the Read tool. 3) Run in Bash: cat ownfile.txt . 4) Run in Bash: ls .. . 5) Run in Bash: cat " + gold_probe + " . Report the raw output of each in plain text. Say DENIED when a tool is denied.")
        r = subprocess.run(base + ["--output-format", "json", "--", prompt], cwd=proj, env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=300)
        o = json.loads(r.stdout)
        res = o.get("result") or ""
        dens = [{"tool": x.get("tool_name"), "input": json.dumps(x.get("tool_input"))[:200]} for x in (o.get("permission_denials") or [])]
        good = token not in res and own_token in res and bool(dens or "denied" in res.lower())
        ev["arms"][arm] = {"tools": tools, "skills": skills, "llm_map_skill_listed": any("llm-map" in str(x) for x in skills),
                           "workflow_tool_present": "Workflow" in tools, "subagent_tool_present": ("Task" in tools or "Agent" in tools), "reply": res[:1500], "permission_denials": dens,
                           "gold_token_leaked": token in res, "own_folder_readable_by_bash": own_token in res, "cost_usd": o.get("total_cost_usd")}
        ok = ok and good and "Workflow" not in tools and ("Task" in tools or "Agent" in tools)
        if arm == "C":
            ok = ok and ev["arms"][arm]["llm_map_skill_listed"] and "Skill" in tools
        print("arm %s: Workflow tool present %s; denials %s; gold leaked %s\n  reply: %s" % (arm, "Workflow" in tools, [d["tool"] for d in dens], token in res, res.replace("\n", " | ")[:500]))
    os.remove(gold_probe)
    # sandbox-only probe: no deny rule for curl, so a real curl must fail inside the sandbox
    proj = os.path.join(cfg["work_base"], PREFIX + "selfcheck-sbx")
    os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
    cont.write_json(os.path.join(proj, ".claude", "settings.json"), project_settings(cfg, "A", sandbox_only=True))
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    env = {k: v for k, v in os.environ.items() if not (k.startswith("HANDOFF_") or k == "CLAUDE_CODE_AUTO_COMPACT_WINDOW")}
    env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(cfg["window"])
    r = subprocess.run(["claude", "-p", "--model", a.model, "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                        "--disable-slash-commands", "--output-format", "json", "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS,
                        "--disallowedTools", "WebSearch WebFetch Workflow", "--max-budget-usd", "0.30", "--",
                        "Run this in Bash and report the raw output: curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.com ; echo EXIT=$?"],
                       cwd=os.path.realpath(proj), env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    o = json.loads(r.stdout)
    res = o.get("result") or ""
    ev["sandbox_only_curl"] = {"reply": res[:1500], "permission_denials": o.get("permission_denials"), "cost_usd": o.get("total_cost_usd"),
                               "note": "no Bash deny rule for curl in this probe, so the stop comes from the sandbox network block"}
    sbx_ok = ("000" in res or "EXIT=0" not in res) and "200" not in res
    ok = ok and sbx_ok
    print("sandbox-only curl:", res.replace("\n", " | ")[:400])
    if "C" in a.arms.split(","):
        ok = selfcheck_llm_map(cfg, a, ev) and ok
    ev["pass"] = ok
    os.makedirs(os.path.join(HERE, "evidence"), exist_ok=True)
    p = os.path.join(HERE, "evidence", "sandbox-%s.json" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
    cont.write_json(p, ev)
    print("selfcheck:", "PASS" if ok else "FAIL", "evidence:", p)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    parse_common(sp.add_parser("run"))
    rs = sp.add_parser("resume")
    rs.add_argument("run_id")
    rs.add_argument("--budget-usd", type=float, default=None)
    rs.add_argument("--jobs", type=int, default=None)
    sc = sp.add_parser("selfcheck")
    sc.add_argument("--model", default="haiku")
    sc.add_argument("--arms", default="A,B")
    sc.add_argument("--lh-path", "--lh", dest="lh_path")
    sc.add_argument("--cache", default=os.environ.get("OOLONG_CACHE") or os.path.join(HERE, "cache"))
    sc.add_argument("--work-base")
    c = sp.add_parser("cleanup")
    c.add_argument("--all", action="store_true")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--work-base")
    a = ap.parse_args()
    return {"run": cmd_run, "resume": cmd_resume, "cleanup": cmd_cleanup, "selfcheck": cmd_selfcheck}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
