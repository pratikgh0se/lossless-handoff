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
import functools, sys, tempfile, threading, time, uuid

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
# "Read" is not in the list on purpose. A bare Read allow rule lets the Read tool open any path, also the folders of other units.
# Without it the Read tool opens only the working folder of the unit (a path outside it is refused in `claude -p`). The deny rules
# then need no list of sibling folders, so the size of the settings does not grow with the number of units (E2BIG, run 20261118T160603).
ALLOWED_TOOLS = "Write Edit Bash Task"
# Run stops at once if a unit shows this text: every Bash call of the unit failed to start (see README, "E2BIG").
E2BIG_MARKERS = ("E2BIG", "exceed the OS exec argument limit", "Argument list too long")
PREFLIGHT_MAX_BYTES = 64 * 1024
PREFLIGHT_MAX_ENV = 256 * 1024     # parent environment plus overrides; the macOS limit for arguments plus environment is 1 MiB
# Same in every arm. WebSearch and WebFetch: no network. Workflow: it runs in the background (see above).
# ScheduleWakeup, Monitor and the Cron tools start background jobs or wake-ups. Under `claude -p` they keep the session alive after
# the answer (a unit once ran to the time limit) or end it before the result returns. So they are off in every arm.
DISALLOWED_BASE = ["WebSearch", "WebFetch", "Workflow", "ScheduleWakeup", "Monitor", "CronCreate", "CronDelete", "CronList"]
# Silent-failure guards (README, "Silent-failure guards"). A Bash error whose text does not start with "Exit code" is an infrastructure
# error: the shell did not start. A permission denial is not one: the tool worked and refused the command.
INFRA_UNIT_MIN_CALLS = 3           # a unit with this many Bash calls and none started stops the run
INFRA_WINDOW = 10                  # the last units finished
INFRA_WINDOW_MIN_CALLS = 10        # no rate check below this many Bash calls in the window (one error of two calls is noise)
INFRA_RATE = 0.20                  # more than this share of infrastructure errors in the window stops the run
DENIAL_PREFIXES = ("Permission to use", "Claude requested permissions", "This command requires approval", "The user doesn't want")
WORKFLOW_LINE = "You may split the work across parallel subagents (Agent tool) if it helps; wait for all their results before your final answer."
PROMPT_CORE = ("The file context.txt in the current folder holds a data set. Read it with your tools as needed. "
               "Do not use the network. Keep every scratch file in the current folder and use relative paths; do not use /tmp or $TMPDIR. "
               "Answer the question below about the data set.\n\n{question}\n\n"
               "Write the final answer on the last line, in the form the question asks. "
               "Add no period and no other text after it.")
# What may differ between arms. Any other difference stops the run before the first call.
ALLOWED_DIFF = {"A": set(), "B": {"prompt_extra"},
                "C": {"hooks", "env_handoff", "recall_agent", "settings_sha", "llm_map_skill", "llm_map_sandbox_exception"}}
LLM_MAP_SKILL_REL = os.path.join("skills", "llm-map", "SKILL.md")
LLM_MAP_HOOK_REL = os.path.join("hooks", "llm_map.py")
LLM_MAP_SKILL_PATHS = ("~/.claude/hooks/llm_map.py", "$HOME/.claude/hooks/llm_map.py")
LLM_MAP_SUMMARY = re.compile(r"llm_map:[^\n]*?cost \$([0-9]+(?:\.[0-9]+)?)")
LLM_MAP_OK = re.compile(r"llm_map: (\d+) items, (\d+) ok")
SCRATCH_WORDS = ["TMPDIR", "mktemp", "tempfile"]
DENY_BASH = ["curl", "wget", "pip", "pip3", "huggingface", "hf_hub", "load_dataset", "git clone", "urllib", "requests", "http.client", "urlretrieve"]
# Contamination patterns, searched in the text the model wrote and in its tool inputs. Tool results are not searched:
# they print lines of the context (for example "hair curl"), which are not evidence.
# Rule of 2026-11-18 (made after the decision run, before the full eval): the plain word "TREC" is a recognition note,
# not a contamination hit. Models write it when they recognise the label set, also inside their own llm_map --prompt text.
# HIT_PATTERNS_OLD is the earlier rule. The report shows the clean score under both rules.
HIT_PATTERNS = [
    ("data set name", r"\btrec_coarse\b"), ("oolong", r"oolong"), ("ground truth", r"ground[ _-]?truth"),
    ("huggingface", r"hugging[ _-]?face|hf_hub|load_dataset|datasets\.load"),
    ("download command", r"\b(curl|wget)\b\s+(-|https?:)|pip3? install|git clone|urllib|requests\.(get|post)|urlretrieve"),
]
URL_PATTERN = ("url in a command", r"https?://")
HIT_PATTERNS_OLD = [("data set name", r"\btrec(_coarse)?\b")] + [p for p in HIT_PATTERNS if p[0] != "data set name"]
RECOGNITION_PATTERNS = [("TREC word", r"\btrec\b")]
HIT_RULE_ID = "2026-11-18-recognition-split"
# Bash tool time limit (ms), the same in every arm. Claude Code 2.1.326 reads both names (checked in the binary).
BASH_TIMEOUT_MS = "600000"
COMMON_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS", "CLAUDE_CODE_TMPDIR")
FINAL_LINE = re.compile(r"^\W*(answer|label|user|date)\s*:\s*\S", re.I)


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
    bad = [w for w in DENY_BASH + SCRATCH_WORDS + ["oolong"] if w in pre]
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
def deny_rules(cfg):
    """The rules are the same for every unit, so their size does not depend on the number of units. The other task folders are
    protected without a rule per folder: Bash by the sandbox (denyRead of the work base, allowRead of the own folder), the Read tool
    by the missing Read allow rule (see ALLOWED_TOOLS), and both by the random folder name (see unit_stem)."""
    rules = list(cont.deny_rules(cfg["work_base"]))
    gold = [os.path.realpath(HERE), os.path.realpath(cfg["cache"])]
    for g in gold:
        for tool in ("Read", "Grep", "Glob", "Edit", "Write"):
            rules.append("%s(/%s/**)" % (tool, g))
        rules.append("Bash(*%s*)" % g)
    # Temp-file words: a unit must keep scratch files in its own folder. Claude Code sets TMPDIR of every Bash call to the shared
    # /tmp/claude-<uid>, where parallel units overwrite each other's files (a review found 41 of 120 units wrote there).
    # /tmp itself is not denied: the unit folder and the llm_map path are under /private/tmp.
    rules += ["Bash(*%s*)" % w for w in DENY_BASH + SCRATCH_WORDS] + ["Bash(*oolong*)", "WebSearch", "WebFetch"]
    return sorted(set(rules))


@functools.lru_cache(maxsize=None)
def repo_roots():
    """The git top level of this checkout and every worktree path (gold copies of tasks.json live in them). Real paths."""
    out = {os.path.realpath(sh(["git", "rev-parse", "--show-toplevel"], cwd=REPO) or REPO)}
    for ln in sh(["git", "worktree", "list", "--porcelain"], cwd=REPO).splitlines():
        if ln.startswith("worktree "):
            out.add(os.path.realpath(ln[len("worktree "):]))
    return tuple(sorted(out))


def shared_tmp_dirs():
    """denyRead patterns for the session folders in the shared Claude Code temp folder (/tmp/claude-<uid>/-<slug of a cwd>/...). Other
    sessions keep their scratchpads and task output there. A unit keeps its own session files in CLAUDE_CODE_TMPDIR.
    Not the whole shared folder: Claude Code 2.1.326 sets TMPDIR of every Bash call to /tmp/claude-<uid> (probed: it ignores
    CLAUDE_CODE_TMPDIR and an own TMPDIR for this), so denying all of it would stop a unit from reading back its own temp files.
    Limit: a loose file directly in /tmp/claude-<uid> (not in a session folder) stays readable."""
    u = os.getuid()
    return {os.path.join(os.path.realpath("/tmp/claude-%d" % u), "-*"), os.path.join(os.path.realpath(os.path.join(tempfile.gettempdir(), "claude-%d" % u)), "-*")}


def project_settings(cfg, arm, sandbox_only=False, proj=None):
    """sandbox_only is for selfcheck: it drops the Bash deny words so that a curl really runs and the sandbox must stop it."""
    s = {}
    if arm == "C":
        s = cont.lh_settings(os.path.join(cfg["lh_path"], "hooks"))
    s["permissions"] = {"deny": [r for r in deny_rules(cfg) if not (sandbox_only and r.startswith("Bash(*") and any(w in r for w in DENY_BASH))]}
    s["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                    "network": {"allowedDomains": []}}
    if proj:   # Bash may read only its own folder: not the other task folders, the gold, the results or the transcripts
        deny = {cfg["work_base"], os.path.realpath(HERE), os.path.realpath(cfg["cache"]), PROJECTS} | set(repo_roots()) | shared_tmp_dirs()
        # denyWrite: the deny rule Bash(*TMPDIR*) does not see a redirect target (a redirect to the TMPDIR folder runs), so the sandbox
        # blocks writes to the shared folder. The unit writes in its own folder (cwd) and in CLAUDE_CODE_TMPDIR, both outside it.
        s["sandbox"]["filesystem"] = {"denyRead": sorted(deny), "allowRead": [proj] + ([os.path.join(cfg["lh_path"], LLM_MAP_HOOK_REL)] if arm == "C" else []),
                                      "denyWrite": sorted({os.path.realpath("/tmp/claude-%d" % os.getuid()), os.path.realpath(os.path.join(tempfile.gettempdir(), "claude-%d" % os.getuid()))})}
    return s


def env_for(cfg, arm, statedir, proj=None):
    """proj: the folder of the unit. CLAUDE_CODE_TMPDIR points inside it, so the unit does not use the shared /tmp/claude-<uid>
    (the sandbox denies that folder). The variable exists in Claude Code 2.1.326 (strings of the binary; proved by selfcheck)."""
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(cfg["window"]),
           "BASH_DEFAULT_TIMEOUT_MS": BASH_TIMEOUT_MS, "BASH_MAX_TIMEOUT_MS": BASH_TIMEOUT_MS}
    if proj:
        env["CLAUDE_CODE_TMPDIR"] = os.path.join(proj, ".cctmp")
    if arm == "C":
        env.update({"HANDOFF_STATE_DIR": statedir, "HANDOFF_SUMMARY_MODEL": cfg["model"], "RECALL_EMBED_URL": "http://127.0.0.1:9"})
    return env


def config_record(cfg, task, arm):
    """What this unit runs with. Paths that differ per folder are left out. Compared across arms by check_fairness."""
    s = project_settings(cfg, arm)
    core_arm = "A" if arm == "N" else arm
    env = env_for(cfg, core_arm, "<state>", "<proj>")
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
        "env_handoff": sorted(k for k in env if k not in COMMON_ENV),
        "env_bash_timeout_ms": [env["BASH_DEFAULT_TIMEOUT_MS"], env["BASH_MAX_TIMEOUT_MS"]],
        "harness_rules": {"final_text": "last assistant text of the session", "turns": "assistant messages of the session",
                          "cost": "total_cost_usd of the result (cumulative for the session)",
                          "timeout": "last assistant text is scored if it ends with a labelled answer; row timed_out true",
                          "hit_rule": HIT_RULE_ID},
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


def scan_records(records, final_text="", rule="new"):
    """rule new: HIT_PATTERNS and urls. rule old: the earlier patterns. rule recognition: only the plain word TREC (no urls)."""
    hits = []
    table = {"new": HIT_PATTERNS, "old": HIT_PATTERNS_OLD, "recognition": RECOGNITION_PATTERNS}[rule]
    pats = [(n, re.compile(p, re.I)) for n, p in table]
    url = (URL_PATTERN[0], re.compile(URL_PATTERN[1], re.I)) if rule != "recognition" else ("", re.compile(r"(?!x)x"))
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
    calls, costs, oks = 0, [], []
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
                oks += [int(k) for _n, k in LLM_MAP_OK.findall(txt)]
    # calls = attempts. A success is a summary line with 1 or more ok items. A call that only failed or crashed is no success.
    return {"llm_map_calls": calls, "llm_map_ok_calls": sum(1 for k in oks if k > 0), "llm_map_cost_usd": round(sum(costs), 6),
            "llm_map_summaries": len(costs)}


def assistant_texts(records):
    """Text blocks the model wrote, in order (main session records)."""
    out = []
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if r.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                    out.append(b["text"])
    return out


def count_turns(records):
    """Turns over the whole session: distinct assistant messages (one per model call). A background wake-up adds turns."""
    ids, n = set(), 0
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if r.get("type") == "assistant" and isinstance(msg, dict):
            if msg.get("id"):
                ids.add(msg["id"])
            else:
                n += 1
    return len(ids) + n


def last_cost_state(records):
    """totalCostUSD of the last cost-state record (session total). None if the session wrote none (for example it was killed)."""
    v = None
    for r in records:
        if isinstance(r, dict) and r.get("type") == "cost-state" and r.get("totalCostUSD") is not None:
            v = float(r["totalCostUSD"])
    return v


def has_final_answer(text):
    """True if the last non-empty line is a labelled final answer (for example `Answer: more common`)."""
    lines = [x for x in (text or "").splitlines() if x.strip()]
    return bool(lines) and bool(FINAL_LINE.match(lines[-1]))


def tool_counts(records):
    n = {}
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if r.get("type") == "assistant" and isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n[b.get("name")] = n.get(b.get("name"), 0) + 1
    return n


SHARED_TMP_REF = re.compile(r"(?:(?:/private)?/tmp/claude-\d+/(?!-)|\$\{?TMPDIR\}?/)[^\s\"'\\;|&)<>]+")


def shared_tmp_refs(records, exclude=()):
    """Inputs of Bash, Write, Edit and Read tool calls (main and subagent records) that name a file or folder directly in the shared
    /tmp/claude-<uid> (a name not starting with '-') outside the exclude prefixes (the unit folder). Returns a list of excerpts."""
    out = []
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if r.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Bash", "Write", "Edit", "Read"):
                for m in SHARED_TMP_REF.finditer(json.dumps(b.get("input", {}))):
                    if not any(m.group(0).startswith(x) or m.group(0).replace("/private", "", 1).startswith(x) for x in exclude if x):
                        out.append("%s: %s" % (b.get("name"), m.group(0)[:120]))
    return out


def result_text_of(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def tool_error_stats(records):
    """Over main and subagent records. errors: tool name -> number of tool results with is_error. bash: calls (tool_use), started (the
    Bash tool answered: success, an "Exit code" failure or a permission denial), infra (an error that does not start with "Exit code"
    and is no denial: the shell did not start), examples (first 3 infra texts)."""
    names, errors = {}, {}
    bash = {"calls": 0, "started": 0, "infra": 0, "denied": 0, "examples": []}
    for r in records:
        msg = r.get("message") if isinstance(r, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if not isinstance(b, dict):
                continue
            if r.get("type") == "assistant" and b.get("type") == "tool_use":
                names[b.get("id")] = b.get("name")
                if b.get("name") == "Bash":
                    bash["calls"] += 1
            elif r.get("type") == "user" and b.get("type") == "tool_result":
                name = names.get(b.get("tool_use_id"))
                err = bool(b.get("is_error"))
                if err:
                    errors[name or "?"] = errors.get(name or "?", 0) + 1
                if name != "Bash":
                    continue
                txt = result_text_of(b).lstrip()
                if err and txt.startswith(DENIAL_PREFIXES):
                    bash["denied"] += 1
                    bash["started"] += 1
                elif err and not txt.startswith("Exit code"):
                    bash["infra"] += 1
                    if len(bash["examples"]) < 3:
                        bash["examples"].append(txt[:160])
                else:
                    bash["started"] += 1
    return errors, bash


def infra_unit_trip(bash, min_calls=INFRA_UNIT_MIN_CALLS):
    """Message if the unit had min_calls or more Bash calls and none started, else None."""
    if bash["calls"] >= min_calls and bash["started"] == 0:
        return "%d Bash calls, none started (infrastructure errors: %d; first: %s)" % (bash["calls"], bash["infra"], "; ".join(bash.get("examples", [])[:1]))
    return None


def infra_window_trip(window):
    """window: (calls, started, infra) tuples of the last finished units. Message if more than INFRA_RATE of the Bash calls are infra errors."""
    calls, infra = sum(w[0] for w in window), sum(w[2] for w in window)
    if calls >= INFRA_WINDOW_MIN_CALLS and infra > INFRA_RATE * calls:
        return "%d of %d Bash calls in the last %d units are infrastructure errors (limit %d%%)" % (infra, calls, len(window), int(INFRA_RATE * 100))
    return None


def infra_scan(rows):
    """For the report: apply both rules again over stored rows (in finish order). Return a list of messages, empty if no rule trips."""
    msgs, win = [], []
    for r in rows:
        b = {"calls": r.get("bash_calls", 0), "started": r.get("bash_started", 0), "infra": r.get("bash_infra", 0), "examples": r.get("infra_examples", [])}
        m = infra_unit_trip(b)
        if m:
            msgs.append("unit %s: %s" % (r["unit"], m))
        win = (win + [(b["calls"], b["started"], b["infra"])])[-INFRA_WINDOW:]
        m = infra_window_trip(win)
        if m:
            msgs.append("at unit %s: %s" % (r["unit"], m))
            win = []
    return msgs


# ------------------------------------------------------------------ one unit
class Ctx:
    def __init__(self, run_dir, cfg):
        self.dir, self.cfg = run_dir, cfg
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state_path = os.path.join(run_dir, "state.json")
        self.fatal = None
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

    def trip(self, msg):
        """Stop the run for a silent-failure reason: the message goes to state.json (error) and stderr. Raises Fatal."""
        with self.lock:
            self.fatal = msg
            self.stop.set()
        raise Fatal(msg)

    def check_budget(self):
        b = self.cfg.get("budget_usd")
        if self.stop.is_set():
            raise Pause()
        # headroom: one call can pass its cap (a $0.40 cap once spent $0.49), so stop early by the largest unit cost seen
        if b is not None and self.state["spent"] >= b - self.state.get("max_unit_cost", 0.0):
            self.stop.set()
            raise Pause()


def unit_stem(run_id):
    """Folder name of one unit: the run prefix and a random 128-bit part. A unit cannot guess the folder of another unit."""
    return "%s%s-%s" % (PREFIX, run_id, uuid.uuid4().hex)


def e2big_hit(recs, result_text=""):
    """True if a Bash tool error (is_error, text not starting with "Exit code": the shell did not start), the result text or stderr
    holds an E2BIG marker. A command that only prints "Argument list too long" ends with "Exit code" or no error, so it is no hit."""
    if any(m in result_text for m in E2BIG_MARKERS):
        return True
    names = {}
    for r in recs:
        msg = r.get("message") if isinstance(r, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                names[b.get("id")] = b.get("name")
            elif b.get("type") == "tool_result" and b.get("is_error") and names.get(b.get("tool_use_id")) == "Bash":
                txt = result_text_of(b).lstrip()
                if not txt.startswith("Exit code") and any(m in txt for m in E2BIG_MARKERS):
                    return True
    return False


def env_size(env):
    return sum(len(k) + len(str(v)) + 2 for k, v in env.items())


def preflight(cfg, arms):
    """Build the settings of the largest unit (longest folder path, every arm) and fail early if one is over the limit.
    Returns a list of problems, empty if all is fine. Each size is the serialized JSON in bytes."""
    longest = os.path.join(cfg["work_base"], unit_stem(cfg["run_id"]) + "-state")
    probs = []
    for arm in sorted(set("A" if x == "N" else x for x in arms)):
        s = project_settings(cfg, arm, proj=longest)
        t = trusted_settings(cfg, arm, env_for(cfg, arm, longest, longest[:-len("-state")]))
        sizes = {"project settings": len(json.dumps(s)), "sandbox profile": len(json.dumps(s["sandbox"])), "--settings argument": len(json.dumps(t))}
        for name, n in sizes.items():
            if n > PREFLIGHT_MAX_BYTES:
                probs.append("arm %s: %s is %d bytes, over the limit of %d. Bash would fail with E2BIG." % (arm, name, n, PREFLIGHT_MAX_BYTES))
        envn = env_size(os.environ) + env_size(env_for(cfg, arm, longest, longest[:-len("-state")]))
        if envn > PREFLIGHT_MAX_ENV:
            probs.append("arm %s: the environment of a unit is %d bytes, over the limit of %d. Bash would fail with E2BIG. Unset large variables." % (arm, envn, PREFLIGHT_MAX_ENV))
    return probs


def run_unit(ctx, task, arm, rec, canary=False):
    cfg = ctx.cfg
    uname = "%s-%s" % (task["task_id"], arm)
    if ctx.state["units"].get(uname, {}).get("status") == "done":
        return
    ctx.check_budget()
    core_arm = "A" if arm == "N" else arm
    stem = unit_stem(cfg["run_id"])
    proj = os.path.join(cfg["work_base"], stem)
    statedir = proj + "-state"
    if os.path.isdir(proj):
        shutil.rmtree(proj)
    os.makedirs(os.path.join(proj, ".claude"))
    os.makedirs(statedir, exist_ok=True)
    proj = os.path.realpath(proj)
    cont.write_json(os.path.join(proj, ".claude", "settings.json"), project_settings(cfg, core_arm, proj=proj))
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
    env_over = env_for(cfg, core_arm, statedir, proj)
    os.makedirs(env_over["CLAUDE_CODE_TMPDIR"], exist_ok=True)
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
    timed_out = err_txt == "timeout"
    main, _all = transcript_files(proj, sid)
    recs = []
    for f in _all:
        recs += cont.read_jsonl(f)
    main_recs = cont.read_jsonl(main) if os.path.isfile(main) else []
    # cost: total_cost_usd of the result is the session total (the cost-state record of the transcript holds the same number)
    cost_known = o is not None
    cost = float(o.get("total_cost_usd") or 0) if o else 0.0
    if o is None:
        cs = last_cost_state(main_recs)
        if cs is not None:
            cost, cost_known = cs, True
    lm_cost = llm_map_stats(recs)["llm_map_cost_usd"]       # child `claude -p` calls of llm_map: not in the main total_cost_usd
    with ctx.lock:
        ctx.state["spent"] += cost + lm_cost                # the budget and the pause use main plus llm_map cost
        ctx.state["max_unit_cost"] = max(ctx.state.get("max_unit_cost", 0.0), cost + lm_cost)
    result_text = (o or {}).get("result") or ""
    result_text = result_text if isinstance(result_text, str) else ""
    texts = assistant_texts(main_recs)
    final_text = texts[-1] if texts else result_text       # last assistant text of the whole session
    salvaged = False
    if o is None:
        if timed_out and has_final_answer(final_text):
            salvaged = True
        else:
            final_text = ""
    sc = scorer.score(task["answer"], task["answer_type"], final_text)
    boundaries, _pre = cont.count_boundaries(main)
    tc = tool_counts(recs)
    lm = llm_map_stats(recs)
    hits = scan_records(recs, final_text)
    hits_old = scan_records(recs, final_text, "old")
    recog = scan_records(recs, final_text, "recognition")
    turns = count_turns(main_recs) or (o or {}).get("num_turns")
    tdir = os.path.join(ctx.dir, "transcripts")
    os.makedirs(tdir, exist_ok=True)
    if os.path.isfile(main):
        shutil.copyfile(main, os.path.join(tdir, uname + ".jsonl"))
    pdir = os.path.dirname(main)
    for f in _all:             # subagent transcripts: transcripts/<unit>/<path below the project folder>, so a report can be derived again
        if f != main:
            dst = os.path.join(tdir, uname, os.path.relpath(f, pdir))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(f, dst)
    terrors, bash = tool_error_stats(recs)
    tmprefs = shared_tmp_refs(recs, (proj, cfg["work_base"]))
    denials = [{"tool": x.get("tool_name"), "input": json.dumps(x.get("tool_input"))[:300]} for x in ((o or {}).get("permission_denials") or []) if isinstance(x, dict)]
    usage = (o or {}).get("modelUsage") or {}
    row = {
        "unit": uname, "task_id": task["task_id"], "length": task["length"], "arm": arm, "answer_type": task["answer_type"],
        "task": task["task"], "score": sc["score"], "parsed": sc["parsed"], "parse_confidence": sc["parse_confidence"],
        "gold": sc["gold"], "full_answer": final_text, "result_text": result_text, "cost_usd": cost, "cost_known": cost_known, "wall_s": round(wall, 1),
        "num_turns": turns, "num_turns_result": (o or {}).get("num_turns"), "timed_out": timed_out, "workflow_calls": tc.get("Workflow", 0),
        "subagent_calls": tc.get("Task", 0) + tc.get("Agent", 0), "tool_counts": tc, "compactions": boundaries,
        **lm, "permission_denials": denials, "tool_errors": terrors, "bash_calls": bash["calls"], "bash_started": bash["started"],
        "bash_infra": bash["infra"], "bash_denied": bash["denied"], "shared_tmp_refs": tmprefs[:10], "shared_tmp_ref_count": len(tmprefs), "infra_examples": bash["examples"], "hits": hits, "hits_old_rule": hits_old, "recognition": recog, "session_id": sid,
        "is_error": bool((o or {}).get("is_error")) or (o is None and not salvaged),
        "subtype": "timeout_answered" if salvaged else (o or {}).get("subtype"), "stderr": (err_txt or "")[:300] if o is None and not salvaged else "",
        "models": sorted(usage), "config": rec, "config_sha": sha(rec), "finished": now_iso(),
    }
    ctx.append("results.jsonl", row)
    bad = e2big_hit(recs, result_text + " " + err_txt)
    for dd in (proj, statedir):      # the task folder is no longer needed; the transcript is copied
        shutil.rmtree(dd, ignore_errors=True)
    # Silent-failure guards, in this order. The first that trips stops the run.
    fatal = None
    if bad:
        fatal = ("unit %s: Bash could not start (E2BIG, the command line or environment is too large). Every Bash call fails, so the "
                 "run stops now. Nothing more is spent. Check the size of the settings (README, E2BIG)." % uname)
    elif o is not None and not main_recs:
        fatal = ("unit %s: the result exists but the main transcript is missing or empty (%s). The harness cannot check the tool calls, "
                 "so the run stops now. Check ~/.claude/projects and the disk." % (uname, main))
    else:
        fatal = infra_unit_trip(bash, 1 if canary else INFRA_UNIT_MIN_CALLS)
        if fatal:
            fatal = "unit %s%s: %s. The Bash tool does not work in this run, so the scores would be wrong. The run stops now." % (uname, " (first unit of its arm)" if canary else "", fatal)
        else:
            with ctx.lock:
                win = ctx.state.setdefault("bash_window", [])
                win.append([bash["calls"], bash["started"], bash["infra"]])
                del win[:-INFRA_WINDOW]
                m = infra_window_trip([tuple(w) for w in win])
            if m:
                fatal = "after unit %s: %s. The Bash tool fails too often in this run, so the scores would be wrong. The run stops now." % (uname, m)
    with ctx.lock:
        ctx.state["units"][uname] = {"status": "failed" if fatal else ("done" if (o is not None or salvaged) else "failed"), "cost": cost + lm_cost}
    ctx.save()
    if fatal:
        try:
            ctx.trip(fatal)
        finally:
            ctx.save()
    print("  %-22s score %.2f cost $%.3f %5.0fs turns %s wf %d hits %d llm_map %d ($%.3f)%s" % (
        uname, sc["score"], cost, wall, row["num_turns"], row["workflow_calls"], len(hits), lm["llm_map_calls"], lm["llm_map_cost_usd"],
        ("  TIMEOUT-SCORED" if salvaged else "  ERROR") if row["is_error"] or salvaged else ""), flush=True)


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
    r.add_argument("--timeout-s", type=int, default=1200, help="time limit of each call, the same in all arms")
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--quarantine-tmp", action="store_true", help="move loose files of the shared temp folder away first (see tmpcheck)")
    r.add_argument("--tmp-root", help=argparse.SUPPRESS)
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
    pre = preflight(cfg, arms)
    if pre:
        print("PRE-FLIGHT FAILED:\n  " + "\n  ".join(pre), file=sys.stderr)
        return 1
    print("pre-flight: settings size is under %d bytes and the environment under %d bytes for every arm" % (PREFLIGHT_MAX_BYTES, PREFLIGHT_MAX_ENV))
    code = 0
    ctx.state["status"] = "running"
    ctx.save()
    units = [(t, arm) for t in tasks for arm in arms]

    def collect(f):
        nonlocal code
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
    try:
        # First unit of each arm (A, B, C; not the control) runs alone, before the pool. The Bash rule is strict for it: one Bash call
        # and none started stops the run. So a broken arm costs one unit, not a pool of them.
        firsts = []
        for arm in arms:
            if arm != "N" and units:
                firsts.append(next((t, a) for t, a in units if a == arm))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            for t, arm in firsts:
                collect(ex.submit(run_unit, ctx, t, arm, recs[(t["task_id"], arm)], True))
                if ctx.stop.is_set():
                    break
        if not ctx.stop.is_set():
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["jobs"]) as ex:
                futs = [ex.submit(run_unit, ctx, t, arm, recs[(t["task_id"], arm)]) for t, arm in units]
                for f in futs:
                    collect(f)
    except KeyboardInterrupt:
        ctx.stop.set()
    undone = [u for t, arm in units for u in ["%s-%s" % (t["task_id"], arm)] if ctx.state["units"].get(u, {}).get("status") != "done"]
    if ctx.fatal:
        ctx.state["status"], ctx.state["error"], code = "failed", ctx.fatal, 1
        print("RUN STOPPED:", ctx.fatal, file=sys.stderr)
    elif ctx.stop.is_set() and undone:
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


def tmp_root_default():
    return "/tmp/claude-%d" % os.getuid()


def loose_tmp_items(root):
    """Entries directly in the shared temp folder whose names do not start with '-' (session folders start with '-'). (name, bytes, mtime)."""
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        if n.startswith("-"):
            continue
        p = os.path.join(root, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        size = st.st_size
        if os.path.isdir(p) and not os.path.islink(p):
            size = 0
            for r_, _d, fs in os.walk(p):
                for f in fs:
                    try:
                        size += os.lstat(os.path.join(r_, f)).st_size
                    except OSError:
                        pass
        out.append((n, size, st.st_mtime))
    return out


def tmp_busy(path):
    """True if a process has the item open (lsof). A failing lsof counts as busy: do not move what cannot be checked."""
    isdir = os.path.isdir(path) and not os.path.islink(path)
    try:
        r = subprocess.run(["lsof", "+D", path] if isdir else ["lsof", path], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool(r.stdout.strip())


def tmp_guard(root, quarantine, list_file=None):
    """Return (ok, messages). Loose items make it not ok. With quarantine they are MOVED (never deleted) to <root>-quarantine-<time>,
    after an lsof check; an item that a process has open stays and keeps the result not ok."""
    items = loose_tmp_items(root)
    if list_file:
        with open(list_file, "w", encoding="utf-8") as f:
            for n, sz, mt in items:
                f.write("%s\t%d\t%s\n" % (n, sz, datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")))
    if not items:
        return True, ["shared temp folder %s: no loose items" % root]
    if not quarantine:
        return False, ["%d loose item(s) directly in %s (names not starting with '-'; first: %s). Parallel units could share them. "
                       "Run: run.sh tmpcheck --quarantine-tmp (it moves them, it deletes nothing)." % (len(items), root, ", ".join(n for n, _s, _m in items[:5]))]
    dest = "%s-quarantine-%s" % (os.path.realpath(root), datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    moved, busy = 0, []
    for n, _s, _m in items:
        src = os.path.join(root, n)
        if tmp_busy(src):
            busy.append(n)
            continue
        shutil.move(src, os.path.join(dest, n))
        moved += 1
    msgs = ["moved %d loose item(s) from %s to %s" % (moved, root, dest)]
    if busy:
        msgs.append("%d item(s) are open in a process and stayed: %s" % (len(busy), ", ".join(busy[:10])))
    return not busy, msgs


def cmd_tmpcheck(a):
    ok, msgs = tmp_guard(a.tmp_root or tmp_root_default(), a.quarantine_tmp, a.list_file)
    print("\n".join(msgs), file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_run(a):
    ok, msgs = tmp_guard(a.tmp_root or tmp_root_default(), a.quarantine_tmp)
    print("\n".join(msgs), file=sys.stdout if ok else sys.stderr)
    if not ok:
        return 2
    cfg = build_cfg(a)
    tasks, arms, recs = plan(cfg)
    run_dir = os.path.join(RESULTS, cfg["run_id"])
    os.makedirs(run_dir)
    cont.write_json(os.path.join(run_dir, "config.json"), cfg)
    save_tasks(run_dir, tasks)
    cont.write_json(os.path.join(run_dir, "config_records.json"), {"%s|%s" % k: v for k, v in recs.items()})
    open(os.path.join(RESULTS, ".last"), "w").write(cfg["run_id"])
    print("run_id: %s" % cfg["run_id"], flush=True)      # machine-readable: run.sh reads it
    print("run", cfg["run_id"], "model", cfg["model"], "lengths", cfg["lengths"], "tasks", [t["task_id"] for t in tasks], "arms", arms)
    return execute(Ctx(run_dir, cfg), tasks, arms, recs)


def resume_problems(cfg, run_dir, recs):
    """Why this run may not resume: another bench commit, another Claude Code version, or another config record than the stored one."""
    probs = []
    now = sh(["git", "rev-parse", "--short", "HEAD"], cwd=REPO)
    if cfg.get("bench_commit") != now:
        probs.append("bench_commit is %s in the run, %s now" % (cfg.get("bench_commit"), now))
    ver = sh(["claude", "--version"])
    if cfg.get("claude_version") != ver:
        probs.append("claude_version is %r in the run, %r now" % (cfg.get("claude_version"), ver))
    try:
        stored = json.load(open(os.path.join(run_dir, "config_records.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return probs + ["config_records.json is missing or unreadable"]
    cur = json.loads(json.dumps({"%s|%s" % k: v for k, v in recs.items()}))
    if sha(cur) != sha(stored):
        keys = sorted({k for k in set(cur) | set(stored) if cur.get(k) != stored.get(k)})
        fields = sorted({f for k in keys for f in set(cur.get(k) or {}) | set(stored.get(k) or {}) if (cur.get(k) or {}).get(f) != (stored.get(k) or {}).get(f)})
        probs.append("the config record hash differs (%s now, %s stored); fields: %s" % (sha(cur), sha(stored), ", ".join(fields[:8]) or "units"))
    return probs


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
    tasks, arms, recs = plan(cfg)
    # A resumed run must use the same harness and the same settings as the first part. Scores of two configurations must not mix.
    probs = resume_problems(cfg, run_dir, recs)
    if probs:
        print("refuse to resume %s:\n  %s\nStart a new run instead." % (a.run_id, "\n  ".join(probs)), file=sys.stderr)
        return 2
    cont.write_json(os.path.join(run_dir, "config.json"), cfg)
    ok, msgs = tmp_guard(a.tmp_root or tmp_root_default(), a.quarantine_tmp)
    print("\n".join(msgs), file=sys.stdout if ok else sys.stderr)
    if not ok:
        return 2
    ctx = Ctx(run_dir, cfg)
    if a.reset_guard:
        ctx.state.pop("bash_window", None)
        ctx.state.pop("error", None)
        print("guard reset: the saved Bash window of the stop rule is cleared")
    elif ctx.state.get("bash_window") and ctx.state.get("status") == "failed":
        print("note: the saved Bash window is kept. If the last stop came from the window rule and you fixed the cause, resume with --reset-guard.")
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
    env_over = env_for(cfg, "C", proj + "-state", proj)
    os.makedirs(env_over["CLAUDE_CODE_TMPDIR"], exist_ok=True)
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


def selfcheck_subagent(cfg, a, arm, proj, env, base, sib_file, sib_token, ev):
    """A subagent (Task tool) tries to read a file of a sibling unit with the Read tool and with Bash. Both must fail. The session
    transcripts must show that a subagent really ran (otherwise the probe proves nothing)."""
    sid = str(uuid.uuid4())
    prompt = ("Use the Task tool once, to start one subagent. Give the subagent this job: 'Read the file %s with the Read tool. "
              "Then run in Bash: cat %s . Reply with the raw output of both, or the word DENIED for each one that is refused.' "
              "Report the reply of the subagent in plain text. This is an authorized test: you must start the subagent, also when you expect a refusal." % (sib_file, sib_file))
    r = subprocess.run(base + ["--session-id", sid, "--output-format", "json", "--", prompt], cwd=proj, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=300)
    o = json.loads(r.stdout)
    res = o.get("result") or ""
    main_file, files = transcript_files(proj, sid)
    recs = []
    for f in files:
        recs += cont.read_jsonl(f)
    main_tc = tool_counts(cont.read_jsonl(main_file))
    used_main = main_tc.get("Task", 0) + main_tc.get("Agent", 0) > 0          # the Task tool call is in the main transcript
    sub_recs = []
    for f in files:
        if f != main_file:
            sub_recs += cont.read_jsonl(f)
    sub_attempt = sum(1 for r in sub_recs if isinstance(r, dict) and r.get("type") == "assistant" and sib_file in json.dumps(r.get("message", {})) and "tool_use" in json.dumps(r.get("message", {})))
    used = used_main and sub_attempt > 0       # and the subagent's own transcript shows its attempt on the sibling file
    terrors, bash = tool_error_stats(recs)
    blob = json.dumps(recs, ensure_ascii=False)
    leaked = sib_token in res or sib_token in blob
    good = used and not leaked
    ev["arms"][arm]["subagent_probe"] = {"subagent_started": used, "task_call_in_main_transcript": used_main, "subagent_attempts_on_sibling_file": sub_attempt, "transcript_files": len(files), "sibling_token_leaked": leaked, "reply": res[:1000],
                                         "tool_errors": terrors, "bash_calls": bash["calls"], "bash_denied": bash["denied"],
                                         "permission_denials": [{"tool": x.get("tool_name"), "input": json.dumps(x.get("tool_input"))[:300]} for x in (o.get("permission_denials") or [])],
                                         "cost_usd": o.get("total_cost_usd"), "pass": good}
    print("arm %s subagent probe: started %s, transcript files %d, sibling token leaked %s\n  reply: %s" % (arm, used, len(files), leaked, res.replace("\n", " | ")[:400]))
    return good


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
    # a sibling unit folder: its token must stay unreadable by the Read tool and by Bash
    sib_dir = os.path.join(cfg["work_base"], unit_stem("selfchecksib"))
    os.makedirs(sib_dir, exist_ok=True)
    sib_file = os.path.join(sib_dir, "secret.txt")
    sib_token = "SIBTOKEN-" + uuid.uuid4().hex[:8]
    open(sib_file, "w").write(sib_token)
    sib_state_dir = sib_dir + "-state"      # the -state folder of a sibling unit (arm C keeps the handoff state there)
    os.makedirs(sib_state_dir, exist_ok=True)
    sib_state_file = os.path.join(sib_state_dir, "secret.txt")
    sib_state_token = "SIBSTATE-" + uuid.uuid4().hex[:8]
    open(sib_state_file, "w").write(sib_state_token)
    shared_tmp = "/tmp/claude-%d" % os.getuid()      # the shared Claude Code temp folder: the unit must not read it
    os.makedirs(shared_tmp, exist_ok=True)
    tmp_sess = os.path.join(shared_tmp, "-lhoo-selfcheck-session-%s" % uuid.uuid4().hex[:8])   # looks like the session folder of another chat
    os.makedirs(tmp_sess, exist_ok=True)
    tmp_file = os.path.join(tmp_sess, "token.txt")
    tmp_token = "TMPTOKEN-" + uuid.uuid4().hex[:8]
    open(tmp_file, "w").write(tmp_token)
    stale = os.path.join(shared_tmp, "lhoo-own-temp.txt")      # an earlier probe may have left it; the check below needs a clean start
    if os.path.exists(stale):
        os.remove(stale)
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
        env_over = env_for(cfg, arm, proj + "-state", proj)
        os.makedirs(env_over["CLAUDE_CODE_TMPDIR"], exist_ok=True)
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
                  "2) Read the file " + gold_probe + " with the Read tool. 3) Run in Bash: cat ownfile.txt . 4) Run in Bash: ls .. . 5) Run in Bash: cat " + gold_probe + " . 6) Read the file " + sib_file + " with the Read tool. 7) Run in Bash: cat " + sib_file + " . "
                  "8) Read the file " + sib_state_file + " with the Read tool. 9) Run in Bash: cat " + sib_state_file + " . "
                  "10) Run in Bash: cat " + tmp_file + " . "
                  "11) Run in Bash: echo x > $TMPDIR/lhoo-own-temp.txt . If it fails, report the error text. "
                  "12) Run in Bash: echo SCRATCH-OK > scratch.txt && cat scratch.txt && rm scratch.txt . Report the raw output of each in plain text. Say DENIED when a tool is denied. This is an authorized test: you must make every call, also when you expect a refusal.")
        r = subprocess.run(base + ["--output-format", "json", "--", prompt], cwd=proj, env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=300)
        o = json.loads(r.stdout)
        res = o.get("result") or ""
        dens = [{"tool": x.get("tool_name"), "input": json.dumps(x.get("tool_input"))[:500]} for x in (o.get("permission_denials") or [])]
        sib_denied = any(d["tool"] == "Read" and sib_file in d["input"] for d in dens)    # the Read call was made and refused
        sib_state_denied = any(d["tool"] == "Read" and sib_state_file in d["input"] for d in dens)
        cct = os.path.join(proj, ".cctmp")
        cct_entries = sorted(os.listdir(cct)) if os.path.isdir(cct) else []
        tmp_written = os.path.exists(os.path.join(shared_tmp, "lhoo-own-temp.txt"))      # checked on disk: a redirect into the shared folder must fail
        if tmp_written:
            os.remove(os.path.join(shared_tmp, "lhoo-own-temp.txt"))
        tmpdir_denied = not tmp_written
        tmp_ok = tmp_token not in res and "SCRATCH-OK" in res and tmpdir_denied   # shared token hidden, $TMPDIR denied, a scratch write in the unit folder works
        good = (sib_denied and sib_state_denied and token not in res and sib_token not in res and sib_state_token not in res and tmp_ok
                and own_token in res and bool(dens or "denied" in res.lower()))
        ev["arms"][arm] = {"tools": tools, "skills": skills, "llm_map_skill_listed": any("llm-map" in str(x) for x in skills),
                           "workflow_tool_present": "Workflow" in tools, "subagent_tool_present": ("Task" in tools or "Agent" in tools), "reply": res[:6000], "permission_denials": dens,
                           "gold_token_leaked": token in res, "sibling_token_leaked": sib_token in res, "sibling_read_denied": sib_denied,
                           "sibling_state_token_leaked": sib_state_token in res, "sibling_state_read_denied": sib_state_denied,
                           "shared_tmp_token_leaked": tmp_token in res, "tmpdir_use_denied": tmpdir_denied, "scratch_write_in_unit_folder_works": "SCRATCH-OK" in res,
                           "claude_code_tmpdir": cct, "claude_code_tmpdir_entries_after_run": cct_entries,
                           "extra_tools_absent": not any(x in tools for x in DISALLOWED_BASE), "own_folder_readable_by_bash": own_token in res, "cost_usd": o.get("total_cost_usd")}
        ok = ok and good and not any(x in tools for x in DISALLOWED_BASE) and bool(cct_entries) and ("Task" in tools or "Agent" in tools)
        ok = selfcheck_subagent(cfg, a, arm, proj, env, base, sib_file, sib_token, ev) and ok
        if arm == "C":
            ok = ok and ev["arms"][arm]["llm_map_skill_listed"] and "Skill" in tools
        print("arm %s: Workflow tool present %s; denials %s; gold leaked %s\n  reply: %s" % (arm, "Workflow" in tools, [d["tool"] for d in dens], token in res, res.replace("\n", " | ")[:500]))
    os.remove(gold_probe)
    shutil.rmtree(tmp_sess, ignore_errors=True)
    shutil.rmtree(sib_dir, ignore_errors=True)
    shutil.rmtree(sib_state_dir, ignore_errors=True)
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
    rs.add_argument("--reset-guard", action="store_true", help="clear the saved Bash window of the stop rule (only after you fixed the cause of a window-rule stop)")
    rs.add_argument("--quarantine-tmp", action="store_true", help="move loose files of the shared temp folder away first (see tmpcheck)")
    rs.add_argument("--tmp-root", help=argparse.SUPPRESS)
    tc_ = sp.add_parser("tmpcheck")
    tc_.add_argument("--quarantine-tmp", action="store_true", help="MOVE (not delete) loose items to /private/tmp/claude-<uid>-quarantine-<time>/")
    tc_.add_argument("--tmp-root", help="folder to check (default /tmp/claude-<uid>); for tests")
    tc_.add_argument("--list-file", help="write the full list (name, bytes, mtime) to this file")
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
    return {"run": cmd_run, "resume": cmd_resume, "cleanup": cmd_cleanup, "selfcheck": cmd_selfcheck, "tmpcheck": cmd_tmpcheck}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
