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
import cost, design4, gates, gen, grade  # noqa: E402
import math  # noqa: E402

REPO = os.path.realpath(os.path.join(HERE, "..", ".."))
RESULTS = os.path.join(HERE, "results")
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
PREFIX = "lhbench-"
ALL_TYPES = gen.TYPES + ["F"]
ALL_ARMS = ("A", "B", "Z", "O", "R", "K")
LH_ARMS = ("B", "R", "K")   # arms with Lossless Handoff hooks: B default install, R plus --auto-recall, K plus auto-recall, recall rules, recall MCP and reanchor
MCP_TOOLS = "mcp__lossless-recall__search_earlier_chat mcp__lossless-recall__open_message"
K_FILES = ("auto-recall.py", "recall-mcp.py", "reanchor.py")
ALLOWED_TOOLS = "Read Grep Task Agent Bash(grep:*) Bash(cat:*) Bash(ls:*) Bash(head:*) Bash(tail:*) Bash(python3:*)"
TOOL_SEARCH = "ToolSearch"      # arm K only: Claude Code may defer MCP tools and load them through ToolSearch (env ENABLE_TOOL_SEARCH exists in 2.1.326)
# Allow-listed environment of every `claude` call. The full shell environment is not passed (it holds CLAUDE_EFFORT, CLAUDE_CODE_CHILD_SESSION and more).
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "ANTHROPIC_API_KEY",
             "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
# Auto-memory off for every arm. Source: the installed binary (claude 2.1.326) holds the environment variable name CLAUDE_CODE_DISABLE_AUTO_MEMORY
# and the settings key autoMemoryEnabled. We set both (the env var in the overrides, the key in the project settings).
MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
# Every variable the runner itself sets for a call (Session overrides). The preflight env check allows exactly these plus ENV_ALLOW.
OVERRIDE_ENV = ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "RECALL_EMBED_URL", MEMORY_ENV, "DISABLE_AUTOUPDATER", "DISABLE_AUTO_COMPACT",
                "HANDOFF_STATE_DIR", "HANDOFF_SUMMARY_MODEL", "HANDOFF_HOOKS_HOME", "CLAUDE_CODE_MAX_OUTPUT_TOKENS")
# Output cap of a probe fork. A probe answers in short JSON; the run 20261122T142851-preflight4 had a fork that wrote about 41,600 tokens and was
# refused by a safety filter. CLAUDE_CODE_MAX_OUTPUT_TOKENS is read by claude 2.1.326 (found in the installed binary: the value is checked
# against the model limit, `effective`). It is set for probe forks and for the arm Z probe only, never for the turns of a main session.
PROBE_MAX_OUTPUT_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
PROBE_MAX_OUTPUT_TOKENS = 8000
FAILED_REPLY_CHARS = 4000


def redact(text):
    """Mask keys, bearer tokens and the home folder in a text that is kept in the run folder."""
    t = re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "sk-REDACTED", str(text))
    t = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}", r"\1REDACTED", t)
    return t.replace(HOME, "~") if HOME else t
# Big preflight turn (Sonnet). Measured in the first preflight run: 961706 characters of filler gave 467684 tokens in total, of which about
# O_OVERHEAD_TOKENS were system prompt and tools, so the paste is about 0.46 tokens per character (random words). Target: 300k tokens in total.
BIG_TOKENS_PER_CHAR = 0.46
BIG_TARGET_TOKENS = 300000
BIG_CAP_USD = 2.5        # cap of this one turn (a cold-cache write of 300k tokens costs about $1.2; the measured turn of 468k tokens cost $1.80)
K_FORK_CAP_USD = 3.0     # per-call cap of a fork in arm K. MCP tools change the tool list, so the first K fork may miss the cache and write about 300k tokens ($1.1 to $2.3), above the normal cap of $1.50
NC3_FORK_ARMS = ("B", "R", "K")  # design 3, no compaction: these arms fork the session of arm A with their own settings
MAX_NO_BOUNDARY = 10        # filler turns in a row with no new compaction: the session is invalid
KINDS = {
    "slice": {"model": "haiku", "window": 30000, "n": [1, 3, 10], "arms": "A,B,Z,O", "seeds": 5, "seed_file": "seeds-dev.txt"},
    "full": {"model": "sonnet", "window": 400000, "n": [1, 3, 10, 30], "arms": "A,B,Z,O", "seeds": 20, "seed_file": "seeds-report.txt"},
    # Lost-in-the-middle design (probe design 2). The pilot uses dev seeds. Never report from it.
    "litm-pilot": {"model": "sonnet", "window": 400000, "n": [1, 3, 10], "arms": "A,B,R", "seeds": 2, "seed_file": "seeds-dev.txt",
                   "conditions": "compact,nocompact", "phrasings": "cued,neutral", "probe_design": 2},
    # One seed, arms A and R, no compaction only: the cheapest live check of DISABLE_AUTO_COMPACT, the validity rule and the hook log.
    "litm-smoke": {"model": "sonnet", "window": 400000, "n": [1, 3, 10], "arms": "A,R", "seeds": 1, "seed_file": "seeds-dev.txt",
                   "conditions": "nocompact", "phrasings": "cued,neutral", "probe_design": 2},
    # Probe design 3: real long context (about 300k of a 400k window), balanced positions, non-literal probes, arm K.
    "litm3-smoke": {"model": "sonnet", "window": 400000, "n": [1, 3], "arms": "A,B,R,K", "seeds": 1, "seed_file": "seeds-dev.txt",
                    "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3},
    "litm3-nc-smoke": {"model": "sonnet", "window": 400000, "n": [1, 3, 10], "arms": "A,R", "seeds": 1, "seed_file": "seeds-dev.txt",
                       "conditions": "nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3},
    # Preflight: the cheapest check of every part of the run. Haiku, small window, short no-compaction session. The checklist is preflight.py.
    "preflight": {"model": "haiku", "window": 30000, "n": [1], "arms": "A,B,R,K,Z,O", "seeds": 1, "seed_file": "seeds-dev.txt", "types": "R,X",
                  "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3, "nc_window": 120000,
                  "mcp_check": True, "selfcheck": True, "big_check": True},
    "litm3-pilot": {"model": "sonnet", "window": 400000, "n": [1, 3, 10], "arms": "A,B,R,K", "seeds": 2, "seed_file": "seeds-dev.txt",
                    "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3},
    "litm3-full": {"model": "sonnet", "window": 400000, "n": [1, 3, 10, 30], "arms": "A,B,R,K,Z,O", "seeds": 23, "seed_file": "seeds-report.txt",
                   "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3},
    # V3 (the default full run): A and B to N=30, R and K to N=10 in the compaction condition. V5: A and B to N=30, K at N=10 only, no R. litm3-full is V1 (as designed).
    "litm3-full-v3": {"model": "sonnet", "window": 400000, "n": [1, 3, 10, 30], "n_by_arm": {"R": [1, 3, 10], "K": [1, 3, 10]}, "arms": "A,B,R,K,Z,O", "seeds": 23,
                      "seed_file": "seeds-report.txt", "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3},
    "litm3-full-v5": {"model": "sonnet", "window": 400000, "n": [1, 3, 10, 30], "n_by_arm": {"K": [10]}, "arms": "A,B,K,Z,O", "seeds": 23,
                      "seed_file": "seeds-report.txt", "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 3},
    "litm-full": {"model": "sonnet", "window": 400000, "n": [1, 3, 10], "arms": "A,B,R,Z,O", "seeds": 20, "seed_file": "seeds-report.txt",
                  "conditions": "compact,nocompact", "phrasings": "cued,neutral", "probe_design": 2},
    # Probe design 4 (update chains with interference, no-compaction condition). The compaction condition stays as in design 3.
    # Calibration: arms A, O, Z on Sonnet, no compaction only. One dev seed; `design4.py decide <run-id>` says if a second seed (seeds-dev-2.txt) is needed.
    "litm4-calibrate": {"model": "sonnet", "window": 400000, "n": [1], "arms": "A,O,Z", "seeds": 1, "seed_file": "seeds-dev.txt",
                        "conditions": "nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 4},
    # The L5 escalation (only if `decide` says add_l5): the whole calibration again with levels L1 to L5 in one session of 165 turns. It replaces all cells.
    "litm4-calibrate-l5": {"model": "sonnet", "window": 400000, "n": [1], "arms": "A,O,Z", "seeds": 1, "seed_file": "seeds-dev.txt", "levels": "L1,L2,L3,L4,L5",
                           "conditions": "nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 4},
    "litm4-smoke": {"model": "sonnet", "window": 400000, "n": [1, 3], "arms": "A,B,R,K", "seeds": 1, "seed_file": "seeds-dev.txt",
                    "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 4},
    # The pilot seeds (3 and 4) are dev seeds that the calibration did not use (seeds 1 and 2): the pick is checked on new seeds.
    "litm4-pilot": {"model": "sonnet", "window": 400000, "n": [1, 3, 10], "arms": "A,B,R,K", "seeds": 2, "seed_file": "seeds-pilot.txt",
                    "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 4},
    "litm4-full-v3": {"model": "sonnet", "window": 400000, "n": [1, 3, 10, 30], "n_by_arm": {"R": [1, 3, 10], "K": [1, 3, 10]}, "arms": "A,B,R,K,Z,O", "seeds": 23,
                      "seed_file": "seeds-report.txt", "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 4},
    "preflight4": {"model": "haiku", "window": 30000, "n": [1], "arms": "A,B,R,K,Z,O", "seeds": 1, "seed_file": "seeds-dev.txt", "types": "R,X",
                   "conditions": "compact,nocompact", "phrasings": "cued,neutral,nonliteral", "probe_design": 4, "nc_window": 120000,
                   "mcp_check": True, "selfcheck": True, "big_check": True},
}
NC_SESSION_WINDOW = 200000      # window of the no-compaction condition
AUTO_RECALL_LOG = "auto-recall.jsonl"


class Pause(Exception):
    pass


class Fatal(Exception):
    pass


class CallError(Exception):
    pass


class SafetyRefusal(CallError):
    """The API refused the content (a safety filter): not retried, class safety_refusal."""


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
    p = os.path.realpath(arg or os.environ.get("LHBENCH_WORK") or tempfile.gettempdir())
    home = os.path.realpath(HOME)
    if p == home or p.startswith(home + os.sep):
        raise SystemExit("the work base %s is below HOME (%s). Claude Code reads the CLAUDE.md files of parent folders, so the real user "
                         "instructions would enter every arm. Use a folder outside HOME (default: the system temp folder)." % (p, home))
    return p


def claude_version():
    """`claude --version` (no model call)."""
    return sh(["claude", "--version"])


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


def count_reads(path):
    """Count of Read tool calls on filler files (pad-*) in a main transcript."""
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"Read"' not in line or "pad-" not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                m = r.get("message") if isinstance(r, dict) else None
                if isinstance(m, dict) and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read" \
                                and "pad-" in json.dumps(b.get("input")):
                            n += 1
    except OSError:
        pass
    return n


RETRY_PROMPT = "Use the Read tool on each file; do not answer from memory."


def run_filler_turn(ss, prompt):
    """One filler turn. If it has no Read call, send one retry turn. Same rule for every arm.
    Counts go to the session state: filler_no_read_turns, filler_retries."""
    st = ss.st
    path = ss.transcript(st["sid"])
    r0 = count_reads(path)
    ss.turn(prompt)
    if count_reads(path) == r0:
        st["filler_no_read_turns"] = st.get("filler_no_read_turns", 0) + 1
        st["filler_retries"] = st.get("filler_retries", 0) + 1
        r1 = count_reads(path)
        ss.turn(RETRY_PROMPT)
        if count_reads(path) == r1:
            st["filler_retry_no_read"] = st.get("filler_retry_no_read", 0) + 1


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

    def script3(self, seed):
        """Script of the no-compaction condition: design 3, or design 4 (update chains) when the run has probe design 4."""
        d4 = self.cfg.get("probe_design", 1) >= 4
        tag = "litm4" if d4 else "litm3"
        key = (tag, seed)
        with self.lock:
            if key not in self.scripts:
                s = design4.build_litm4(seed, tuple(self.cfg.get("levels") or design4.D4_LEVELS)) if d4 else gen.build_litm3(seed)
                self.scripts[key] = s
                os.makedirs(os.path.join(self.dir, "scripts"), exist_ok=True)
                write_json(os.path.join(self.dir, "scripts", "%d-%s.json" % (seed, tag)), s)
            return self.scripts[key]

    def script(self, seed):
        with self.lock:
            if seed not in self.scripts:
                s = gen.build(seed, self.cfg["gen_version"])
                self.scripts[seed] = s
                os.makedirs(os.path.join(self.dir, "scripts"), exist_ok=True)
                write_json(os.path.join(self.dir, "scripts", "%d.json" % seed), s)
            return self.scripts[seed]


# ------------------------------------------------------------------ one claude call
def lh_settings(hooks_dir, auto_recall=False, reanchor=False):
    """Project settings of arm B: the hooks of the default install (install.sh, no --canary).
    Arm R (auto_recall=True) adds the UserPromptSubmit hook that `install.sh --auto-recall` registers (contract F67.a)."""
    pi = "python3 %s/postcompact-inject.py" % hooks_dir
    led = [{"type": "command", "command": "%s --ledger --part %d" % (pi, n), "timeout": 10} for n in (1, 2, 3)]
    chk = {"type": "command", "command": pi + " --check", "timeout": 5}
    hooks = {
        "PreCompact": [{"hooks": [{"type": "command", "command": "python3 %s/precompact-handoff.py" % hooks_dir, "timeout": 150}]}],
        "SessionStart": [
            {"matcher": "compact", "hooks": [{"type": "command", "command": "%s --part %d" % (pi, n), "timeout": 10} for n in (1, 2, 3)] + led},
            {"matcher": "startup", "hooks": led + [chk]},
            {"matcher": "resume", "hooks": led + [chk]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 %s/recall.py session-end" % hooks_dir, "timeout": 5}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 %s/handoff-nudge.py" % hooks_dir, "timeout": 5}]}],
    }
    if reanchor:        # F70: PostToolUse, matcher *, timeout 5
        hooks["PostToolUse"].append({"matcher": "*", "hooks": [{"type": "command", "command": "python3 %s/reanchor.py" % hooks_dir, "timeout": 5}]})
    if auto_recall:
        hooks["UserPromptSubmit"] = [{"hooks": [{"type": "command", "command": "python3 %s/auto-recall.py" % hooks_dir, "timeout": 5}]}]
    return {"hooks": hooks}


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
    def __init__(self, ctx, name, seed, arm, st, cond="compact"):
        self.ctx, self.name, self.seed, self.arm, self.st, self.cond = ctx, name, seed, arm, st, cond
        cfg = ctx.cfg
        base = work_base(cfg["work_base"])
        stem = "%s%s-s%d-%s%s" % (PREFIX, cfg["run_id"], seed, arm, "-nc" if cond == "nocompact" else "")
        self.proj = os.path.join(base, stem)
        self.statedir = os.path.join(base, stem + "-state")
        self.window = cfg["o_window"] if arm == "O" else (cfg.get("nc_window", NC_SESSION_WINDOW) if cond == "nocompact" else cfg["window"])
        self.script = ctx.script3(seed) if (cond == "nocompact" and cfg.get("probe_design", 1) >= 3) else ctx.script(seed)
        self.d3 = self.script.get("design") in (3, 4)
        self.d4 = self.script.get("design") == 4
        self.view_of = None     # design 3, no compaction, arms B R K: the main session of arm A, which this arm forks
        ov = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(self.window), "RECALL_EMBED_URL": "http://127.0.0.1:9", MEMORY_ENV: "1", "DISABLE_AUTOUPDATER": "1"}
        if cond == "nocompact":
            ov["DISABLE_AUTO_COMPACT"] = "1"      # same session length as the compaction run, compaction off
        if arm in LH_ARMS:
            ov["HANDOFF_STATE_DIR"] = self.statedir
            ov["HANDOFF_SUMMARY_MODEL"] = cfg["model"]
            ov["HANDOFF_HOOKS_HOME"] = os.path.join(self.statedir, "hooks")      # F72: the hooks read only this private copy, never ~/.claude/hooks
        self.overrides = ov

    def hooks_dir(self):
        """Hooks folder of the arm. Every arm with hooks (B, R, K) gets its own copy of the hooks of --lh-path in its state folder, and
        HANDOFF_HOOKS_HOME (F72) points the hooks to it. Arm K also gets the F68 marker .recall-rules. A live install cannot change a run."""
        src = os.path.join(self.ctx.cfg["lh_path"], "hooks")
        if self.arm not in LH_ARMS:
            return src
        dst = os.path.join(self.statedir, "hooks")
        if not os.path.isdir(dst):
            os.makedirs(self.statedir, exist_ok=True)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            if self.arm == "K":
                open(os.path.join(dst, ".recall-rules"), "w").close()
        return dst

    def hook_hashes(self):
        """sha256 of every file of the hooks folder of the arm (relative path -> hash). None for arms without hooks."""
        if self.arm not in LH_ARMS:
            return None
        d, out = self.hooks_dir(), {}
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            for f in sorted(files):
                p = os.path.join(root, f)
                out[os.path.relpath(p, d)] = sha256_file(p)
        return out

    def settings_json(self):
        """--settings text: the env overrides. A view (arm B, R or K forking the session of arm A) also gets the hooks of its arm."""
        s = {"env": self.overrides}
        if self.view_of is not None and self.arm in LH_ARMS:
            s["hooks"] = lh_settings(self.hooks_dir(), auto_recall=(self.arm in ("R", "K")), reanchor=(self.arm == "K"))["hooks"]
        return json.dumps(s)

    def mcp_config(self):
        """--mcp-config text. Arm K gets the recall MCP server (F69), from its own hooks copy. Every other arm gets none."""
        if self.arm != "K":
            return '{"mcpServers":{}}'
        return json.dumps({"mcpServers": {"lossless-recall": {"command": "python3", "args": [os.path.join(self.hooks_dir(), "recall-mcp.py")],
                                                               "env": {"HANDOFF_STATE_DIR": self.statedir, "RECALL_EMBED_URL": "http://127.0.0.1:9"}}}})

    def allowed_tools(self):
        return ALLOWED_TOOLS + (" " + MCP_TOOLS + " " + TOOL_SEARCH if self.arm == "K" else "")

    # -- folders
    def prepare(self):
        if not os.path.isdir(self.proj):
            os.makedirs(os.path.join(self.proj, ".claude"))
            os.makedirs(self.statedir, exist_ok=True)
            settings = lh_settings(self.hooks_dir(), auto_recall=(self.arm in ("R", "K")), reanchor=(self.arm == "K")) if self.arm in LH_ARMS else {}
            settings["permissions"] = {"deny": deny_rules(work_base(self.ctx.cfg["work_base"]))}
            settings["autoMemoryEnabled"] = False
            write_json(os.path.join(self.proj, ".claude", "settings.json"), settings)
            # make the folder its own git root, so that recall.py cannot search outside it
            subprocess.run(["git", "init", "-q", self.proj], capture_output=True, timeout=30)
            if self.arm in LH_ARMS:
                src = os.path.join(self.ctx.cfg["lh_path"], "agents", "recall.md")
                if os.path.isfile(src):     # arms B and R only: the recall agent of the pinned checkout
                    os.makedirs(os.path.join(self.proj, ".claude", "agents"), exist_ok=True)
                    shutil.copyfile(src, os.path.join(self.proj, ".claude", "agents", "recall.md"))
            if self.st["next_turn"] > 0 or not self.st["first"]:
                # the folder is gone: the old session cannot continue. Start again from turn 0.
                self.st.update({"next_turn": 0, "ckpt_i": 0, "boundaries": 0, "last_cost": 0.0, "done_forks": [],
                                "f_planted": [], "missed": [], "first": True, "sid": None, "events_done": 0, "fill_done": 0,
                                "filler_no_read_turns": 0, "filler_retries": 0, "filler_retry_no_read": 0, "nc_done": 0})
                self.st["restarts"] = self.st.get("restarts", 0) + 1       # rows of the lost run stay in results.jsonl; gates.dedupe_rows drops them
        self.proj = os.path.realpath(self.proj)
        if not self.st.get("sid"):
            self.st["sid"] = str(uuid.uuid4())

    def transcript(self, sid):
        return os.path.join(PROJECTS, slug(self.proj), sid + ".jsonl")

    # -- the call
    def env(self, probe=False):
        env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
        env.update(self.overrides)
        if probe:
            env[PROBE_MAX_OUTPUT_ENV] = str(PROBE_MAX_OUTPUT_TOKENS)       # probe forks only, not the turns of a main session
        return env

    def save_failed(self, o, out, err_txt, kind, fork_of, attempt):
        """A failed call: keep the transcript and the reply text (redacted, cut) in failed/ of the run folder, to diagnose the next failure."""
        try:
            d = os.path.join(self.ctx.dir, "failed")
            os.makedirs(d, exist_ok=True)
            o = o if isinstance(o, dict) else {}
            text = o.get("result") if isinstance(o.get("result"), str) else (out or "")
            u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
            base = "%s-%s-a%d-%s" % (self.name, kind, attempt, time.strftime("%H%M%S"))
            write_json(os.path.join(d, base + ".json"), {
                "ts": now_iso(), "name": self.name, "kind": kind, "sid": o.get("session_id"), "fork_of": fork_of, "attempt": attempt,
                "failure_class": gates.failure_class(text), "output_tokens": u.get("output_tokens"), "reply": redact(text)[:FAILED_REPLY_CHARS],
                "reply_chars": len(text), "stderr": redact(str(err_txt))[:1000]})
            sid = o.get("session_id")
            if sid and os.path.isfile(self.transcript(sid)):
                shutil.copyfile(self.transcript(sid), os.path.join(d, base + ".jsonl"))
        except Exception:         # keeping the evidence must never hide the real failure
            pass

    def recorded(self, sid, prompt):
        """(prompt already in the transcript of the session, text of its last assistant record). For an idempotent retry."""
        path = self.transcript(sid)
        if not os.path.isfile(path):
            return False, ""
        seen, text = False, ""
        for r in read_jsonl(path):
            m = r.get("message")
            c = m.get("content") if isinstance(m, dict) else None
            if r.get("type") == "user" and prompt[:300] in (c if isinstance(c, str) else json.dumps(c)):
                seen, text = True, ""
            elif seen and r.get("type") == "assistant" and isinstance(c, list):
                text = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text") or text
        return seen, text

    def call(self, prompt, kind, sid=None, fork_of=None, first=False, model=None, cap=None):
        """One `claude -p` call. kind: turn | fork | z. Returns the parsed JSON object.
        A retry (attempt 2) is idempotent: if the transcript already holds the prompt, it is not sent again; the recorded answer comes back."""
        ctx, cfg = self.ctx, self.ctx.cfg
        for attempt in (1, 2):
            ctx.check_budget()
            if attempt == 2 and not fork_of and sid:
                seen, text = self.recorded(sid, prompt)
                if seen and not text:
                    # the prompt is in the transcript but no assistant text follows: attempt 1 failed (an API error, a budget cap, a timeout).
                    # Do not call this a success: an error on every turn would look like a good session.
                    raise CallError("retry: the prompt is recorded but no assistant answer follows it (attempt 1 failed)")
                if seen:
                    self.st["replays"] = self.st.get("replays", 0) + 1
                    return {"result": text, "session_id": sid, "total_cost_usd": self.st["last_cost"], "usage": {}, "_delta": 0.0, "is_error": False, "_replayed": True}
                first = first and not os.path.isfile(self.transcript(sid))
            big = len(prompt) > 100000          # argv is limited (about 1 MB on macOS, 128 KB for one argument on Linux): a big prompt goes through stdin
            cmd = ["claude", "-p"]
            if fork_of:
                cmd += ["--resume", fork_of, "--fork-session"]
            else:
                cmd += ["--session-id" if first else "--resume", sid]
            cmd += ["--model", model or cfg["model"], "--setting-sources", "project",
                    "--settings", self.settings_json(),
                    "--strict-mcp-config", "--mcp-config", self.mcp_config(), "--disable-slash-commands",
                    "--output-format", "json", "--permission-mode", "acceptEdits",
                    "--allowedTools", self.allowed_tools(), "--max-budget-usd", "%.2f" % (cap or (K_FORK_CAP_USD if (self.arm == "K" and fork_of) else cfg["call_cap_usd"]))] + ([] if big else [prompt])
            t0 = time.time()
            p = subprocess.Popen(cmd, cwd=self.proj, env=self.env(probe=bool(fork_of) or kind == "z"), stdin=subprocess.PIPE if big else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                out, err = p.communicate(input=prompt if big else None, timeout=cfg["turn_timeout_s"])
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
                    self.save_failed(None, out, err_txt, kind, fork_of, attempt)
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
                refusal = gates.failure_class(o.get("result")) == "safety_refusal"
                if refusal or attempt == 2:
                    self.save_failed(o, out, err_txt, kind, fork_of, attempt)
                if refusal:         # a safety refusal is not retried: the retry gets the same refusal and costs the same
                    self.st["safety_refusals"] = self.st.get("safety_refusals", 0) + 1
                    raise SafetyRefusal("is_error: " + str(o.get("result"))[:200])
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

    def auto_recall_fired(self, fsid):
        """Arm R: did the auto-recall hook run in this fork? The hook appends one line to auto-recall.jsonl for each prompt
        that passes its trigger (contract F67.d). The hook (hooks/auto-recall.py, STATE_DIR) writes to $HANDOFF_STATE_DIR when it
        is set, and logs the session id of its hook input, which is the id of the fork. This arm sets HANDOFF_STATE_DIR to its own
        folder, so we read only that file. We never read or write the real ~/.local/state/claude-handoffs.
        Returns (fired, hits, timed_out); (None, None, None) for arms other than R."""
        if self.arm not in ("R", "K") or not fsid:
            return None, None, None
        return recall_log_fired(read_jsonl(os.path.join(self.statedir, AUTO_RECALL_LOG)), fsid)

    def probe(self, ptype, ckpt, fork_of=None, z_sid=None, phrasing="cued"):
        """Run one probe type. Fork of the main session (arms A, B, O) or a fresh session (arm Z). Write the result lines."""
        cfg, st, script = self.ctx.cfg, self.st, self.script
        marker = "PROBE-%d-%s-%d-%s%s" % (self.seed, self.arm, ckpt, ptype, {"neutral": "n", "nonliteral": "l"}.get(phrasing, ""))
        if ptype == "F":
            items = script["fresh"][str(ckpt)]
        elif self.d4:
            items = script["items"][ptype]      # design 4: a level of update chains, all planted at the probe point
        else:
            em = None if (self.arm == "Z" or self.cond == "nocompact") else need_epoch(cfg, self.arm, ckpt)
            items = gen.select(script, ptype, em)      # planted so far, oldest first
        if not items:
            return      # nothing of this type is planted yet (M needs 3 epochs)
        if self.d3 and ptype != "F":
            items = gen.shuffle3(items, self.seed, ptype, phrasing)     # question order is not position
        if self.d4:
            prompt = design4.probe_prompt(ptype, script, marker, phrasing, items=items)
        else:
            prompt = gen.probe_prompt(ptype, script, marker, ckpt, items=items, phrasing=phrasing, cue=cfg.get("probe_design", 1) >= 2)
        if fork_of:
            o = self.call(prompt, "fork", fork_of=fork_of)
        else:
            o = self.call(prompt, "z", sid=z_sid, first=True)
        raw = o.get("result") if isinstance(o.get("result"), str) else ""
        fsid = o.get("session_id")
        graded = design4.grade_level(items, raw, script) if self.d4 else grade.grade(ptype, items, raw)
        epochs = {x["id"]: x.get("epoch") for x in items}
        frecs = read_jsonl(self.transcript(fsid)) if fsid else []
        used = grade.lookup_used(frecs, marker) if fsid else False
        view_fields = {}
        if self.view_of is not None and fsid:       # B, R and K fork the session of arm A: did the SessionStart hooks of the arm run at the start of the fork?
            blob = json.dumps(frecs)
            view_fields = {"fork_injected": any(m in blob for m in ("Project decisions for this repo", "Project rules for this repo", "searchable word for word")),
                           "fork_rule_line": "recall rule: before you state an earlier number" in blob,
                           "fork_rule_source": gates.rule_line_sources(frecs, "recall rule: before you state an earlier number")}
        kinds = []
        if (cfg.get("probe_design", 1) >= 3 or cfg.get("search_used")) and fsid:      # design 3 and preflight: a search by recall.py, by the MCP tool, or by auto-recall counts for every arm
            used, kinds = grade.search_used(frecs, marker)
        calls_after = grade.lookup_calls(frecs, marker) if fsid else []   # kept: the fork transcript is deleted below
        if fsid:    # a later recall must not find this answer: delete the probe transcript
            ft = self.transcript(fsid)
            for pth in (ft, ft[:-6]):
                if os.path.isfile(pth):
                    os.remove(pth)
                elif os.path.isdir(pth) and not os.path.islink(pth):
                    shutil.rmtree(pth)
        fired, hits, timed_out = self.auto_recall_fired(fsid)
        if self.arm in ("R", "K") and phrasing == "cued" and cfg.get("probe_design", 1) >= 2 and fsid and not fired:
            raise Fatal("arm %s: a cued probe left no line in %s for session %s. The hook did not run, or it logs elsewhere. "
                        "Stop. Check hooks/auto-recall.py in --lh-path." % (self.arm, os.path.join(self.statedir, AUTO_RECALL_LOG), fsid))
        posmap = {x["id"]: (x.get("position") or (None if self.d4 else gen.position_of(script, ptype, x))) for x in items}
        u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
        n_actual, pre = (st["boundaries"], st.get("precomputed", 0)) if self.arm != "Z" else (0, 0)
        for g in graded:
            self.ctx.append("results.jsonl", {
                "run": cfg["run_id"], "gen_version": cfg["gen_version"], "seed": self.seed, "arm": self.arm,
                "n_target": ckpt, "n_actual": n_actual, "precomputed_boundaries": pre, "type": ptype, "item": g["id"],
                "epoch": epochs.get(g["id"]), "age": (ckpt - epochs[g["id"]] if self.arm in ("A", "B") and epochs.get(g["id"]) is not None else None),
                "fork_sid": fsid, "raw": raw, "parsed": g["parsed"], "parse_ok": g["parse_ok"], "correct": g["correct"],
                "abstained": g["abstained"],
                "used_lookup": used, "search_kinds": kinds, "lookup_calls": calls_after, "tokens": {"in": u.get("input_tokens"), "out": u.get("output_tokens")},
                "output_tokens": u.get("output_tokens"), "fork_cost_usd": round(o["_delta"], 6), "claude_version": cfg["claude_version"], "model": cfg["model"],
                "window": self.window, "commit": cfg["lh_commit"], "turn_index": st["turns"],
                "condition": self.cond, "phrasing": phrasing, "position": posmap.get(g["id"]), "attempt": st.get("restarts", 0), **view_fields,
                "auto_recall_fired": fired, "auto_recall_hits": hits, "auto_recall_timed_out": timed_out,
                **({k_: g[k_] for k_ in ("m1", "m2", "m3", "error_class", "lag", "alt_ok", "undo_last")} if self.d4 else {})})
        if self.view_of is not None:
            st.setdefault("first_fork_cost_usd", round(o["_delta"], 6))      # K: the cache-miss check of the preflight
        st["done_forks"].append(fork_key(ckpt, ptype, phrasing))
        self.ctx.save()


def fork_key(ckpt, ptype, phrasing="cued"):
    """Key of a finished probe fork in the state file. A cued key is the old key, so old state files still work."""
    return "%d:%s" % (ckpt, ptype) + (":" + phrasing if phrasing in ("neutral", "nonliteral") else "")


def probe_plan(cfg, ptype, d3=False):
    """Phrasings to ask for a probe type. Neutral exists for R D X S E only. Old runs have cued only."""
    ph = [p for p in cfg.get("phrasings", ["cued"]) if p == "cued" or ((ptype in gen.NEUTRAL_TYPES or ptype in design4.LEVELS) and (p == "neutral" or d3))]
    return ph or ["cued"]


def recall_log_fired(rows, fsid):
    """From the lines of auto-recall.jsonl: (fired, hits, timed_out) for one session id. fired is False when no line has this id.
    The hook logs one line for each prompt that passes its trigger, and shows at most 3 hits (TOP_HITS)."""
    mine = [r for r in rows if isinstance(r, dict) and r.get("session_id") == fsid and r.get("fired")]
    return bool(mine), (max((r.get("hits") or 0) for r in mine) if mine else 0), any(bool(r.get("timed_out")) for r in mine)


# ---- no-compaction validity. DISABLE_AUTO_COMPACT stops auto compaction only. The binary also clears old tool results
# (microcompact: "[Old tool result content cleared]"). A session with cleared results is not a long-context session.
CLEARED_MARKERS = ("Old tool result content cleared", "tool result content cleared")
NC_CONTEXT_TOLERANCE = 0.75       # the last turn must hold at least this share of the expected context (nc_estimate_tokens)


def count_cleared(path):
    """Count of transcript lines that hold a cleared-tool-result marker."""
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(m in line for m in CLEARED_MARKERS):
                    n += 1
    except OSError:
        pass
    return n


def last_turn_context(calls, name):
    """Context size of the last main turn of a session: input_tokens + cache_read_input_tokens of its last `turn` call. None if unknown."""
    last = [c for c in calls if c.get("name") == name and c.get("kind") == "turn"]
    if not last:
        return None
    c = last[-1]
    if c.get("in") is None and c.get("cache_read") is None:
        return None
    return (c.get("in") or 0) + (c.get("cache_read") or 0)


def nc_validity(boundaries, cleared, last_ctx, expected, tol=NC_CONTEXT_TOLERANCE):
    """(valid, reasons). A no-compaction session is valid only if it has no compaction, no cleared marker,
    and its last turn has at least tol * expected tokens of context."""
    why = []
    if boundaries:
        why.append("%d compaction(s)" % boundaries)
    if cleared:
        why.append("%d cleared-result marker line(s)" % cleared)
    if last_ctx is None:
        why.append("no token data for the last turn")
    elif expected and last_ctx < tol * expected:
        why.append("last-turn context %d is below %d (%.0f%% of expected %d)" % (last_ctx, tol * expected, 100 * tol, expected))
    return not why, why


# ------------------------------------------------------------------ session logic
def checkpoints(cfg, arm):
    if arm == "O":
        return [(0, cfg["oracle_turns"])]
    return [(n, 0) for n in cfg.get("n_by_arm", {}).get(arm, cfg["n"])]


def need_epoch(cfg, arm, target):
    """Items of epochs up to this value must be planted before the probe."""
    return (max(cfg["n"]) - 1) if arm == "O" else target - 1


def event_ready(cfg, ss, ev, b):
    """Plant event of epoch e goes in after compaction e. Arm O never compacts, so all its events go in at once."""
    return ev["epoch"] <= (max(cfg["n"]) - 1 if ss.arm == "O" else b)


def run_event(ctx, ss, ev):
    for fname, content in (ev.get("files") or {}).items():
        path = os.path.join(ss.proj, fname)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    ss.turn(ev["prompt"])
    for fname in ev.get("delete_after") or []:
        try:
            os.remove(os.path.join(ss.proj, fname))   # the fact must live in the transcript only
        except OSError:
            pass


def run_main_session(ctx, ss):
    cfg, st, script = ctx.cfg, ss.st, ss.script
    events = script["events"]
    n0 = sum(1 for e in events if e["epoch"] == 0)
    cks = checkpoints(cfg, ss.arm)
    per = (cfg.get("o_files_per_turn") or cfg["files_per_turn"]) if ss.arm == "O" else cfg["files_per_turn"]
    quiet = 0           # filler turns in a row with no new boundary
    while st["ckpt_i"] < len(cks):
        target, min_fill = cks[st["ckpt_i"]]
        b = st["boundaries"]
        ei = st.get("events_done", 0)
        fill_done = st.get("fill_done", 0)
        if b > target:  # a turn added two compactions: this checkpoint is missed
            st["missed"].append({"target": target, "actual": b})
            st["ckpt_i"] += 1
            ctx.save()
            continue
        pending = ei < len(events) and events[ei]["epoch"] <= need_epoch(cfg, ss.arm, target)
        if not pending and b == target and fill_done >= min_fill:
            checkpoint(ctx, ss, target)
            st["ckpt_i"] += 1
            ctx.save()
            continue
        ctx.check_budget()
        if ei < len(events) and event_ready(cfg, ss, events[ei], b):
            run_event(ctx, ss, events[ei])
            st["events_done"] = ei + 1
            st["next_turn"] += 1
            if st["events_done"] == n0:      # all epoch-0 items are planted: no compaction may have happened yet
                st["boundaries_at_plant_end"] = st["boundaries"]
            ctx.save()
            continue
        before = b
        ft = fill_done
        names = []
        for j in range(per):
            name, text = gen.filler_file(ss.seed, ft, j, version=cfg["gen_version"])
            if any(x in text for x in script["secrets"]):
                raise Fatal("a filler file holds an answer token")
            with open(os.path.join(ss.proj, name), "w", encoding="utf-8") as f:
                f.write(text)
            names.append(name)
        run_filler_turn(ss, gen.filler_prompt(script, ft, names))
        quiet = quiet + 1 if st["boundaries"] == before else 0
        if quiet >= MAX_NO_BOUNDARY:
            st["status"], st["reason"] = "invalid", "no compaction in %d filler turns" % MAX_NO_BOUNDARY
            return
        st["fill_done"] = ft + 1
        st["next_turn"] += 1
        ctx.save()


def nc_plan_steps(script):
    """Steps of the no-compaction session: the nominal timeline (see gen.nominal_timeline). ("ev", i) or ("fill", k)."""
    steps, k = [], 0
    for e in range(script["max_epoch"] + 1):
        steps += [("ev", i) for i, ev in enumerate(script["events"]) if ev["epoch"] == e]
        for _ in range(gen.FILL_PER_EPOCH):
            steps.append(("fill", k))
            k += 1
    return steps


def filler_prompt(script, k, lines):
    """The filler turn of a script: design 4 has its own neutral words (see design4.SWAP_WORDS); design 3 uses gen.py."""
    return (design4.filler_doc_prompt if script.get("design") == 4 else gen.filler_doc_prompt)(script, k, lines)


def nc_types(cfg):
    """Probe types of the design-3 or design-4 no-compaction condition: the five types of design 3, or the levels of design 4."""
    return list(cfg.get("levels") or design4.D4_LEVELS) if cfg.get("probe_design", 1) >= 4 else list(gen.L3_TYPES)


def run_nc3_session(ctx, ss):
    """Design 3 no-compaction session. ONE main session for each seed, run as arm A (no Lossless Handoff hooks). Every turn is a user turn
    (plant turns, and filler turns with a pasted document and one small task). No file reads, so the binary has no old tool result to clear.
    Arm O runs the same plan without the filler (a short oracle: zero distance). After the main session, arm A and then arms B, R and K
    fork it at the probe point, each with its own settings (run_fork_view), so all arms answer on one context. The same validity rule applies."""
    cfg, st, script = ctx.cfg, ss.st, ss.script
    steps = script["plan"] if ss.arm != "O" else [x for x in script["plan"] if x["kind"] != "fill"]
    st.setdefault("nc_done", 0)       # a fresh session has no counter yet
    while st["nc_done"] < len(steps):
        ctx.check_budget()
        step = steps[st["nc_done"]]
        if step["kind"] == "fill":
            prompt, expect = filler_prompt(script, step["k"], cfg["nc_lines"])
            o = ss.turn(prompt)
            st["filler_task_total"] = st.get("filler_task_total", 0) + 1
            st["filler_task_ok"] = st.get("filler_task_ok", 0) + (1 if expect in str(o.get("result") or "") else 0)    # information only, not graded
            st["fill_done"] = step["k"] + 1
        else:
            ss.turn(step["prompt"])
            st["events_done"] = st.get("events_done", 0) + 1
        st["nc_done"] += 1
        st["next_turn"] += 1
        ctx.save()
    st["boundaries_at_plant_end"] = st["boundaries"]
    ok, why = nc_validity(st["boundaries"], count_cleared(ss.transcript(st["sid"])),
                          last_turn_context(read_jsonl(os.path.join(ctx.dir, "calls.jsonl")), ss.name), cfg.get("nc_estimate_tokens") if ss.arm != "O" else None)
    st["nc_valid"], st["nc_reasons"] = ok, why
    if not ok:
        st["status"], st["reason"] = "invalid", "no-compaction session is not valid: " + "; ".join(why)
    else:
        for t in nc_types(cfg):
            for ph in probe_plan(cfg, t, d3=True):
                if fork_key(0, t, ph) in st["done_forks"]:
                    continue
                ss.probe(t, 0, fork_of=st["sid"], phrasing=ph)
    if ss.arm == "A":
        for arm in [x for x in cfg["arms"] if x in NC3_FORK_ARMS]:
            run_fork_view(ctx, ss, arm, valid=ok)


def run_fork_view(ctx, main, arm, valid=True):
    """Arm B, R or K in the design-3 no-compaction condition: fork the main session of arm A at the probe point, with the settings of the arm:
    its hooks (own private copy, HANDOFF_HOOKS_HOME), its env, its MCP config and tools, its own state folder. The fork has the source `resume`,
    so the SessionStart hooks of the arm (decisions ledger, recall line, F68 rule line) run at the start of the fork. Whether Claude Code runs
    them for a fork is checked by the preflight (K: the rule line must be in the fork transcript). The main session had no hooks, so no
    compaction handoff and no reanchor ran in it: B here tests the startup injection only, R adds auto-recall, K adds recall rules and the MCP tool."""
    cfg = ctx.cfg
    name = "s%d-%s-nc" % (main.seed, arm)
    st = ctx.sess(name)
    main_r = main.st.get("restarts", 0)
    if st["status"] == "done" and st.get("main_restarts", 0) == main_r:
        return
    if st.get("main_restarts", 0) != main_r:      # the main session restarted (new attempt): the old forks are stale, run them again
        st.update({"done_forks": [], "status": "new"})
        st.pop("mcp_check", None)
        st.pop("first_fork_cost_usd", None)
    st["main_restarts"] = st["restarts"] = main_r      # the rows of the view carry the attempt of the main session
    v = Session(ctx, name, main.seed, arm, st, "nocompact")
    v.proj, v.view_of = main.proj, main
    os.makedirs(v.statedir, exist_ok=True)
    v.hooks_dir()
    st.update({"sid": main.st["sid"], "boundaries": main.st["boundaries"], "turns": main.st["turns"], "last_cost": main.st["last_cost"],
               "nc_valid": main.st.get("nc_valid"), "nc_reasons": main.st.get("nc_reasons"), "first": False, "status": "running",
               "claude_version": main.st.get("claude_version")})
    ctx.save()
    if valid:
        for t in nc_types(cfg):
            for ph in probe_plan(cfg, t, d3=True):
                if fork_key(0, t, ph) not in st["done_forks"]:
                    v.probe(t, 0, fork_of=main.st["sid"], phrasing=ph)
        if arm == "K" and cfg.get("mcp_check") and "mcp_check" not in st:
            mcp_check(ctx, v)
        st["status"] = "done"
    else:
        st["status"], st["reason"] = "invalid", main.st.get("reason")
    ctx.save()
    ctx.append("sessions.jsonl", session_summary(ctx, v))


def drop_fork(ss, fsid):
    """A later recall must not find a probe answer: delete the transcript of a probe fork."""
    if fsid:
        ft = ss.transcript(fsid)
        for pth in (ft, ft[:-6]):
            if os.path.isfile(pth):
                os.remove(pth)
            elif os.path.isdir(pth) and not os.path.islink(pth):
                shutil.rmtree(pth)


def mcp_check(ctx, v):
    """Arm K: one turn that must make an mcp__lossless-recall__ call (preflight). The result goes to the state of the session (st["mcp_check"])."""
    marker = "MCPCHECK-%d-%s" % (v.seed, v.arm)
    prompt = ("[%s] Call the search_earlier_chat tool of the lossless-recall server one time, with the query \"the closing marker\". "
              "Then reply with the single word DONE." % marker)
    o = v.call(prompt, "fork", fork_of=v.view_of.st["sid"] if v.view_of else v.st["sid"])
    fsid = o.get("session_id")
    used, kinds = grade.search_used(read_jsonl(v.transcript(fsid)) if fsid else [], marker)
    drop_fork(v, fsid)
    v.st["mcp_check"] = {"called": "mcp" in kinds, "kinds": kinds, "fork_cost_usd": round(o["_delta"], 6)}
    ctx.save()


def big_docs(chars_per_doc):
    """Number of filler documents so that the big turn has about BIG_TARGET_TOKENS tokens in total (measured tokens per character)."""
    return int(math.ceil((BIG_TARGET_TOKENS - O_OVERHEAD_TOKENS) / (chars_per_doc * BIG_TOKENS_PER_CHAR)))


def run_big_check(ctx):
    """Preflight: one Sonnet turn with a paste of about 300k tokens (the size of the real session). It records the real cost, whether the price
    per token is above the standard price (long-context price above 200k tokens), and whether one cold-cache call stays under the call cap."""
    cfg = ctx.cfg
    if "big" in ctx.state:
        return
    seed = 9999
    ss = Session(ctx, "big", seed, "A", ctx.sess("big"), "nocompact")
    ss.prepare()
    lines, _ = nc3_plan(NC3_WINDOW, cfg.get("probe_design", 3))
    script = ctx.script3(seed)
    docs = big_docs(len(filler_prompt(script, 0, lines)[0]))
    prompt = "\n\n".join(filler_prompt(script, k, lines)[0] for k in range(docs)) + "\n\nReply with only the word OK."
    o = ss.call(prompt, "turn", sid=ss.st["sid"], first=True, model=cfg.get("big_model", "sonnet"), cap=cfg.get("big_cap_usd", BIG_CAP_USD))
    u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
    i_, cr, cw, out = (u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
    std = (i_ * 3.0 + cr * 0.30 + cw * 3.75 + out * 15.0) / 1e6          # standard Sonnet list price (assumed)
    ctx.state["big"] = {"cost_usd": round(o["_delta"], 4), "tokens": i_ + cr + cw, "standard_price_usd": round(std, 4),
                        "price_ratio": round(o["_delta"] / std, 2) if std else None, "call_cap_usd": cfg.get("big_cap_usd", BIG_CAP_USD),
                        "over_call_cap": o["_delta"] > cfg.get("big_cap_usd", BIG_CAP_USD), "docs": docs, "chars": len(prompt)}
    ss.st["status"] = "done"        # the call finished and its numbers are recorded: the session is done (the preflight checks the numbers)
    ctx.save()


def run_nc_session(ctx, ss):
    """No-compaction condition: the same plant turns and the same number of filler turns as the nominal timeline.
    Filler files are small (cfg nc_lines lines), so the whole session fits in the window. Compaction is off (DISABLE_AUTO_COMPACT)."""
    cfg, st, script = ctx.cfg, ss.st, ss.script
    steps = nc_plan_steps(script)
    st.setdefault("nc_done", 0)       # a fresh session has no counter yet
    while st["nc_done"] < len(steps):
        ctx.check_budget()
        kind, i = steps[st["nc_done"]]
        if kind == "ev":
            run_event(ctx, ss, script["events"][i])
            st["events_done"] = st.get("events_done", 0) + 1
        else:
            name, text = gen.filler_file(ss.seed, i, 0, lines=cfg["nc_lines"], version=cfg["gen_version"])
            if any(x in text for x in script["secrets"]):
                raise Fatal("a filler file holds an answer token")
            with open(os.path.join(ss.proj, name), "w", encoding="utf-8") as f:
                f.write(text)
            run_filler_turn(ss, gen.filler_prompt(script, i, [name]))
            st["fill_done"] = i + 1
        st["nc_done"] += 1
        st["next_turn"] += 1
        ctx.save()
    st["boundaries_at_plant_end"] = st["boundaries"]
    ok, why = nc_validity(st["boundaries"], count_cleared(ss.transcript(st["sid"])),
                          last_turn_context(read_jsonl(os.path.join(ctx.dir, "calls.jsonl")), ss.name), cfg.get("nc_estimate_tokens"))
    st["nc_valid"], st["nc_reasons"] = ok, why
    if not ok:
        st["status"], st["reason"] = "invalid", "no-compaction session is not valid: " + "; ".join(why)
        return
    checkpoint(ctx, ss, 0)          # n_target 0: a compaction in this session makes it a missed checkpoint


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
        for ph in probe_plan(cfg, t):
            if fork_key(target, t, ph) in st["done_forks"]:
                continue
            ss.probe(t, target, fork_of=st["sid"], phrasing=ph)
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
    for t in (nc_types(ctx.cfg) if ss.d3 else ctx.cfg["types"]):
        for ph in probe_plan(ctx.cfg, t, d3=ss.d3):
            if fork_key(0, t, ph) in st["done_forks"]:
                continue
            ss.probe(t, 0, z_sid=str(uuid.uuid4()), phrasing=ph)


def session_summary(ctx, ss):
    """Facts from the transcript, for the report. The main transcript only; subagent compactions are counted apart."""
    st = ss.st
    out = {"name": ss.name, "seed": ss.seed, "arm": ss.arm, "condition": ss.cond, "nc_valid": st.get("nc_valid"), "nc_reasons": st.get("nc_reasons"), "status": st["status"], "turns": st["turns"],
           "boundaries": st["boundaries"], "precomputed": st.get("precomputed", 0), "missed": st["missed"],
           "fork_leaks": st["fork_leaks"], "boundaries_at_plant_end": st.get("boundaries_at_plant_end"), "cost_usd": round(st.get("cost", 0.0), 4), "reason": st.get("reason"), "failure_class": st.get("failure_class"), "safety_refusals": st.get("safety_refusals", 0),
           "filler_no_read_turns": st.get("filler_no_read_turns", 0), "filler_retries": st.get("filler_retries", 0),
           "filler_retry_no_read": st.get("filler_retry_no_read", 0)}
    out["restarts"] = st.get("restarts", 0)
    out["replays"] = st.get("replays", 0)                    # retries that replayed a recorded answer
    out["claude_version"] = st.get("claude_version")
    out["first_fork_cost_usd"] = st.get("first_fork_cost_usd")
    out["env_keys"] = sorted(ss.env())            # names only, never values
    out["hook_sha256"] = ss.hook_hashes()
    out["mcp_check"] = st.get("mcp_check")
    out["view_of_arm_a"] = ss.view_of is not None
    out["memory_files"] = memory_files(ss)
    if ss.arm != "Z" and st.get("sid"):
        out["foreign_instructions"] = gates.foreign_instructions(read_jsonl(ss.transcript(st["sid"])), ss.proj)
        out["project_instructions"] = gates.project_instructions(read_jsonl(ss.transcript(st["sid"])), ss.proj)
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
        if ss.arm in LH_ARMS:
            out["hook_receipts"] = len(read_jsonl(os.path.join(ss.statedir, "health.jsonl")))
        if ss.arm == "R":
            out["auto_recall_lines"] = len(read_jsonl(os.path.join(ss.statedir, AUTO_RECALL_LOG)))
    return out


def memory_files(ss):
    """Files in the auto-memory folder of the project of the session. Any file makes the session invalid (memory survives a compaction)."""
    d = os.path.join(PROJECTS, slug(ss.proj), "memory")
    out = []
    for root, dirs, files in os.walk(d):
        out += [os.path.join(root, f) for f in files]
    return sorted(out)


def machine_state_problems(ss):
    """Reasons to call a finished session invalid because machine state entered it."""
    why = []
    mf = memory_files(ss)
    if mf:
        why.append("auto-memory folder holds %d file(s)" % len(mf))
    if ss.arm != "Z" and ss.st.get("sid"):
        fi = gates.foreign_instructions(read_jsonl(ss.transcript(ss.st["sid"])), ss.proj)
        if fi:
            why.append("instructions loaded from outside the project folder: %s" % ", ".join(sorted(set(fi))[:3]))
    return why


def write_retention(ctx, ss):
    """Count the planted answers in each compaction summary of the main transcript. Runs before cleanup."""
    if ss.arm == "Z" or not ss.st.get("sid"):
        return
    try:
        ret = grade.retention(read_jsonl(ss.transcript(ss.st["sid"])), ss.script)
    except Exception as e:      # a diagnostic must never fail a run
        print("warning: retention failed for %s: %s" % (ss.name, e), file=sys.stderr)
        return
    ret.update({"run": ctx.cfg["run_id"], "seed": ss.seed, "arm": ss.arm})
    ctx.append("retention.jsonl", ret)


def run_unit_gated(ctx, seed, arm, cond):
    """run_unit, then the abort gates after the last unit of a seed (see gates.py)."""
    run_unit(ctx, seed, arm, cond)
    if not ctx.cfg.get("gates", True):
        return
    name = "s%d-%s%s" % (seed, arm, "-nc" if cond == "nocompact" else "")
    with ctx.lock:
        if ctx.state["sessions"].get(name, {}).get("status") in ("done", "invalid", "failed") and not ctx.state.get("gate_abort"):
            why = check_session_gates(ctx)
            if why:
                ctx.state["gate_abort"] = why
                ctx.save()
                print("ABORT GATE after session %s: %s" % (name, " | ".join(why)), file=sys.stderr)
                ctx.stop.set()
                return
        if ctx.state["sessions"].get(name, {}).get("status") not in ("done", "invalid"):
            return
        done = [n for n, st in ctx.state["sessions"].items() if st.get("status") in ("done", "invalid")]
        left = [u for u in work_units(ctx.cfg) if u[0] == seed and ("s%d-%s%s" % (u[0], u[1], "-nc" if u[2] == "nocompact" else "")) not in done]
        if left or ctx.state.get("gate_abort"):
            return
        why = check_gates(ctx)
        if why:
            ctx.state["gate_abort"] = why
            ctx.save()
            print("ABORT GATE after seed %d: %s" % (seed, " | ".join(why)), file=sys.stderr)
            ctx.stop.set()


def check_session_gates(ctx):
    """Gates after any finished session: failed-session rate, and the cost of one session against its own estimate."""
    sess = ctx.state["sessions"]
    statuses = {n: st.get("status") for n, st in sess.items() if n != "big"}
    costs = {}
    for n, st in sess.items():
        m = re.match(r"s(\d+)-([A-Z])(-nc)?$", n)
        if m and st.get("status") in ("done", "invalid", "failed"):
            costs[n] = (st.get("cost", 0.0), cost.unit_estimate(ctx.cfg, m.group(2), "nocompact" if m.group(3) else "compact"))
    # a view (arm B, R or K forking arm A) is paid inside the unit of arm A: judge the sum
    for n in list(costs):
        m = re.match(r"s(\d+)-A-nc$", n)
        if m:
            costs[n] = (sum(sess.get("s%s-%s-nc" % (m.group(1), x), {}).get("cost", 0.0) for x in ("A",) + NC3_FORK_ARMS), costs[n][1])
    for n in list(costs):
        if re.match(r"s\d+-[BRK]-nc$", n) and ctx.cfg.get("probe_design", 1) >= 3:
            del costs[n]
    versions = {n: st.get("claude_version") for n, st in sess.items() if st.get("claude_version")}
    return gates.session_reasons(statuses, costs, {n: st.get("failure_class") for n, st in sess.items() if st.get("failure_class")}) + gates.version_reasons(ctx.cfg.get("claude_version"), versions)


def expected_forks(cfg, seeds=None):
    """The probe forks a finished run must have rows for. exact: (seed, arm, condition, phrasing, type, n_target) of the design-3 no-compaction
    condition. classes: (seed, arm, condition, phrasing) of every other condition (the number of checkpoints is not fixed)."""
    seeds = seeds or cfg["seeds"]
    d3 = cfg.get("probe_design", 1) >= 3
    conds = cfg.get("conditions", ["compact"])
    exact, classes = [], []
    for arm in cfg["arms"]:
        cs = list(conds if arm in ("A", "B", "R", "K") else ["compact"])
        if d3 and arm in ("Z", "O") and "nocompact" in conds:
            cs.append("nocompact")
        for cond in cs:
            if cond == "nocompact" and d3:
                exact += [(s, arm, cond, ph, t, 0) for s in seeds for t in nc_types(cfg) for ph in probe_plan(cfg, t, d3=True)]
            else:
                phs = sorted({ph for t in cfg["types"] for ph in probe_plan(cfg, t)})
                classes += [(s, arm, cond, ph) for s in seeds for ph in phs]
    return {"exact": exact, "classes": classes}


def finished_seeds(ctx):
    done = {n for n, st in ctx.state["sessions"].items() if st.get("status") in ("done", "invalid")}
    out = []
    for sd in ctx.cfg["seeds"]:
        units = [("s%d-%s%s" % (u[0], u[1], "-nc" if u[2] == "nocompact" else "")) for u in work_units(ctx.cfg) if u[0] == sd]
        if units and all(u in done for u in units):
            out.append(sd)
    return out


def check_gates(ctx):
    """Reasons to stop after a finished seed. Empty list: go on."""
    fin = finished_seeds(ctx)
    last = {}
    for s in read_jsonl(os.path.join(ctx.dir, "sessions.jsonl")):
        last[s["name"]] = s
    costs = {n: st.get("cost", 0.0) for n, st in ctx.state["sessions"].items() if n != "big" and int(n[1:].split("-")[0]) in fin}
    return gates.abort_reasons(list(last.values()), read_jsonl(os.path.join(ctx.dir, "results.jsonl")), cost.estimate_per_seed(ctx.cfg), fin, costs,
                               expected=expected_forks(ctx.cfg, fin))


def run_unit(ctx, seed, arm, cond="compact"):
    name = "s%d-%s%s" % (seed, arm, "-nc" if cond == "nocompact" else "")
    st = ctx.sess(name)
    if st["status"] == "done":
        return
    ss = Session(ctx, name, seed, arm, st, cond)
    st["claude_version"] = claude_version()
    try:
        ss.prepare()
        st["status"] = "running"
        ctx.save()
        if arm == "Z":
            run_z(ctx, ss)
        elif cond == "nocompact":
            (run_nc3_session if ss.d3 else run_nc_session)(ctx, ss)
        else:
            run_main_session(ctx, ss)
        if st["status"] == "running":
            bad = machine_state_problems(ss)
            if bad:
                st["status"], st["reason"] = "invalid", "machine state: " + "; ".join(bad)
                for x in ctx.cfg["arms"]:       # the arms that forked this session share its folder
                    vs = ctx.state["sessions"].get("s%d-%s-nc" % (seed, x))
                    if arm == "A" and cond == "nocompact" and x in NC3_FORK_ARMS and vs and vs["status"] == "done":
                        vs["status"], vs["reason"] = "invalid", st["reason"]
            else:
                st["status"] = "done"
        ctx.save()
        ctx.append("sessions.jsonl", session_summary(ctx, ss))
        write_retention(ctx, ss)
    except Pause:
        st["status"] = "paused"
        ctx.save()
    except CallError as e:
        st["status"], st["reason"] = "failed", str(e)
        st["failure_class"] = gates.failure_class(st["reason"])
        ctx.save()
        ctx.append("sessions.jsonl", session_summary(ctx, ss))
        write_retention(ctx, ss)
    except Fatal as e:
        st["status"], st["reason"] = "failed", str(e)
        st["failure_class"] = gates.failure_class(st["reason"])
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
    ap.add_argument("--arms", help="comma list from A,B,Z,O,R (R = Lossless Handoff with --auto-recall)")
    ap.add_argument("--conditions", help="comma list from compact,nocompact (default compact; litm kinds: both)")
    ap.add_argument("--phrasings", help="comma list from cued,neutral (default cued; litm kinds: both)")
    ap.add_argument("--nc-window", type=int, default=None, help="window of the no-compaction condition (default 200000)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the cost estimate, then stop. No model call")
    ap.add_argument("--types", help="comma list from R,D,X,S,E,U,C,F (default all)")
    ap.add_argument("--levels", help="design 4: comma list of levels (default L1,L2,L3,L4; the L5 escalation: L1,L2,L3,L4,L5 as its own run)")
    ap.add_argument("--seeds", type=int, help="how many seeds to take from the seed list")
    ap.add_argument("--seed-file", help="seed list file (default: seeds-dev.txt for slice, seeds-report.txt for full)")
    ap.add_argument("--lh-path", help="pinned checkout of Lossless Handoff for arm B (default: this repo)")
    ap.add_argument("--files-per-turn", type=int, help="filler files read in one turn (default from the window)")
    ap.add_argument("--o-window", type=int, default=None, help="window of arm O (default 200000)")
    ap.add_argument("--oracle-turns", type=int, default=None, help="filler turns of arm O before its probes (default 3)")
    ap.add_argument("--o-files-per-turn", type=int, default=None, help="filler files per turn of arm O (default: from --o-window, under 60%% of it; not from --window)")
    ap.add_argument("--work-base", help="folder for the temp project folders (default: the temp dir)")
    ap.add_argument("--call-cap-usd", type=float, default=None, help="--max-budget-usd of each claude call (default 1.50)")
    ap.add_argument("--turn-timeout-s", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=None, help="sessions in parallel (default 2)")
    ap.add_argument("--budget-usd", type=float, default=None, help="pause cleanly when the total reaches this value")


# Arm O size model. Arm O never compacts, so its whole context must fit in its own window.
# It does not depend on the main window. Numbers are estimates and they err on the high side.
O_TARGET_FRACTION = 0.60      # default settings keep O under this share of its window
O_FAIL_FRACTION = 0.75        # pre-flight stops the run above this share
O_OVERHEAD_TOKENS = 25000     # system prompt, tool definitions, project context
O_PROBE_TOKENS = 12000        # one probe fork: question text and reply
TOKENS_PER_FILE = 6500        # one filler file is about 20 KB of random words
CHARS_PER_TOKEN = 3.3


def o_planted_tokens(seeds):
    """Largest planted content (prompts and files of all epochs that arm O plants) over the seeds, in tokens."""
    worst = 0
    for sd in seeds:
        sc = gen.build(sd)
        chars = sum(len(e["prompt"]) + sum(len(c) for c in (e.get("files") or {}).values()) for e in sc["events"])
        worst = max(worst, chars)
    return int(worst / CHARS_PER_TOKEN)


def o_estimate(planted, files_per_turn, turns):
    return O_OVERHEAD_TOKENS + planted + O_PROBE_TOKENS + files_per_turn * turns * TOKENS_PER_FILE


def o_plan(o_window, planted, files_per_turn, turns):
    """Fill the unset values. Default turns 3; default files per turn is the most that keeps O under 60% of its window."""
    turns = turns or 3
    if not files_per_turn:
        room = O_TARGET_FRACTION * o_window - O_OVERHEAD_TOKENS - planted - O_PROBE_TOKENS
        files_per_turn = max(1, int(room // (turns * TOKENS_PER_FILE)))
    est = o_estimate(planted, files_per_turn, turns)
    if est > O_FAIL_FRACTION * o_window:
        raise SystemExit(
            "arm O would be too large: estimate %d tokens (planted %d, filler %d files x %d turns) is above %d%% of --o-window %d. "
            "Lower --o-files-per-turn or --oracle-turns, or raise --o-window. Arm O must not compact."
            % (est, planted, files_per_turn, turns, int(O_FAIL_FRACTION * 100), o_window))
    return files_per_turn, turns, est


NC_TURNS = None     # filler turns of the no-compaction session: 5 epochs x gen.FILL_PER_EPOCH
NC3_WINDOW = 400000             # design 3: the owner's real window
NC3_TARGET_FRACTION = 0.75      # design 3 aims at about 300k tokens in a 400k window
NC3_FAIL_FRACTION = 0.90


# Measured rate of design 3 and 4. The smoke (run 20261122T123857-litm3-smoke) aimed at 299,860 tokens with 19.7 tokens per line
# (6500 tokens per 330 lines) and reached 393,352 tokens at the last turn of s1-A-nc: 362 lines in 36 filler turns, so 26.6 tokens per
# line (the planted prose counted at 0.46 tokens per character, random words). The model below reproduces that figure within 1%.
NC_TOKENS_PER_LINE = 26.6
PLANT_TOKENS_PER_CHAR = 0.46
NC3_TOLERANCE = 0.10            # the estimate must be inside the target, plus or minus 10%


def nc3_estimate(sc, lines):
    """Estimated tokens at the last turn of a design 3 or 4 no-compaction session with `lines` lines in each filler document."""
    planted = sum(len(st.get("prompt", "")) for st in sc["plan"] if st["kind"] != "fill") * PLANT_TOKENS_PER_CHAR
    n_fill = sum(1 for st in sc["plan"] if st["kind"] == "fill")
    return int(O_OVERHEAD_TOKENS + planted + O_PROBE_TOKENS + n_fill * lines * NC_TOKENS_PER_LINE)


def nc3_plan(window, design=3, levels=None):
    """Design 3 or 4: lines of each pasted filler document, so that the whole session holds about 75% of the window. Fails above 90%.
    Planted text is measured from a real script; the rate of a filler line is the measured one (NC_TOKENS_PER_LINE). Returns (lines, estimated tokens)."""
    sc = design4.build_litm4(1, tuple(levels or design4.D4_LEVELS)) if design >= 4 else gen.build_litm3(1)
    planted = sum(len(st.get("prompt", "")) for st in sc["plan"] if st["kind"] != "fill") * PLANT_TOKENS_PER_CHAR
    n_fill = sum(1 for st in sc["plan"] if st["kind"] == "fill")
    room = NC3_TARGET_FRACTION * window - O_OVERHEAD_TOKENS - planted - O_PROBE_TOKENS
    lines = int(room / n_fill / NC_TOKENS_PER_LINE)
    if lines < 20:
        raise SystemExit("design 3 no-compaction session too large for --nc-window %d: only %d lines per filler document." % (window, lines))
    est = nc3_estimate(sc, lines)
    if est > NC3_FAIL_FRACTION * window:
        raise SystemExit("design 3 no-compaction session estimate %d tokens is above %d%% of --nc-window %d" % (est, int(NC3_FAIL_FRACTION * 100), window))
    return lines, est


def nc_plan(window, planted, turns=None):
    """Lines of each filler file (one file per filler turn) so that the no-compaction session fits in its window.
    Same size model as arm O: stay under 60% of the window. Fails above 75%."""
    turns = turns or (gen.build(1)["max_epoch"] + 1) * gen.FILL_PER_EPOCH
    room = O_TARGET_FRACTION * window - O_OVERHEAD_TOKENS - planted - O_PROBE_TOKENS
    lines = int(room / turns / (TOKENS_PER_FILE / 330.0))
    if lines < 20:
        raise SystemExit("the no-compaction condition would be too large for --nc-window %d: only %d lines per filler file. Raise --nc-window." % (window, lines))
    est = int(O_OVERHEAD_TOKENS + planted + O_PROBE_TOKENS + turns * lines * TOKENS_PER_FILE / 330.0)
    return lines, est


def build_cfg(kind, a):
    k = KINDS[kind]
    window = a.window or k["window"]
    seeds = seed_list(kind, a.seeds or k["seeds"], a.seed_file)
    types = [t for t in ((a.types or k.get("types") or "").split(",") if (a.types or k.get("types")) else ALL_TYPES)]
    bad = [t for t in types if t not in ALL_TYPES]
    arms = (a.arms or k["arms"]).split(",")
    bad += [x for x in arms if x not in ALL_ARMS]
    conds = (a.conditions or k.get("conditions", "compact")).split(",")
    phr = (a.phrasings or k.get("phrasings", "cued")).split(",")
    bad += [x for x in conds if x not in ("compact", "nocompact")] + [x for x in phr if x not in gen.PHRASINGS]
    if bad:
        raise SystemExit("unknown type, arm, condition or phrasing: %s" % bad)
    lh = os.path.realpath(a.lh_path or REPO)
    if any(x in arms for x in LH_ARMS) and not os.path.isfile(os.path.join(lh, "hooks", "precompact-handoff.py")):
        raise SystemExit("arm B or R: no hooks in %s/hooks" % lh)
    for f_ in K_FILES:
        if "K" in arms and not os.path.isfile(os.path.join(lh, "hooks", f_)):
            raise SystemExit("arm K: no hooks/%s in %s. Arm K needs auto-recall.py, recall-mcp.py and reanchor.py. Use --lh-path with a checkout that has them." % (f_, lh))
    if "R" in arms and not os.path.isfile(os.path.join(lh, "hooks", "auto-recall.py")):
        raise SystemExit("arm R: no hooks/auto-recall.py in %s. Use --lh-path with a checkout that has install.sh --auto-recall." % lh)
    design = k.get("probe_design", 1)
    if design >= 3 and "nocompact" in conds and "A" not in arms:
        raise SystemExit("design 3, no compaction: arm A runs the main session and arms B, R and K fork it. Add arm A to --arms.")
    levels = None
    if design >= 4:       # the levels of the update chains. L5 is the escalation: its own session (165 turns), it replaces all cells of the L1-L4 run.
        levels = [x for x in (a.levels or k.get("levels") or ",".join(design4.D4_LEVELS)).split(",") if x]
        if any(x not in design4.LEVELS for x in levels) or levels != sorted(set(levels)):
            raise SystemExit("--levels: a comma list of levels in order from %s, for example L1,L2,L3,L4,L5" % ",".join(design4.LEVELS))
    nc_window = a.nc_window or k.get("nc_window") or (NC3_WINDOW if design >= 3 else NC_SESSION_WINDOW)
    if "nocompact" in conds and any(x in arms for x in ("A", "B", "R", "K")):
        nc_lines, nc_est = nc3_plan(nc_window, design, levels) if design >= 3 else nc_plan(nc_window, o_planted_tokens(seeds))
    else:
        nc_lines, nc_est = None, None
    dirty = bool(sh(["git", "status", "--porcelain", "--", "hooks"], cwd=lh))
    o_window = a.o_window or 200000
    o_fpt, o_turns, o_est = o_plan(o_window, o_planted_tokens(seeds), a.o_files_per_turn, a.oracle_turns) if "O" in arms else (None, None, None)
    rid = "%s-%s" % (datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), kind)
    cfg = {
        "run_id": rid, "kind": kind, "gen_version": gen.GEN_VERSION, "model": a.model or k["model"], "window": window,
        "n": [int(x) for x in a.n.split(",")] if a.n else k["n"], "arms": arms, "types": types, "seeds": seeds,
        "lh_path": lh, "lh_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=lh) + ("+dirty" if dirty else ""),
        "bench_commit": sh(["git", "rev-parse", "--short", "HEAD"], cwd=REPO),
        "files_per_turn": a.files_per_turn or max(1, min(12, round(window * 0.4 / 6000))),
        "o_window": o_window, "oracle_turns": o_turns or a.oracle_turns or 3, "o_files_per_turn": o_fpt,
        "o_estimate_tokens": o_est,
        "conditions": conds, "phrasings": phr, "probe_design": k.get("probe_design", 1), "search_used": bool(k.get("search_used")), "gates": True,
        "mcp_check": bool(k.get("mcp_check")), "selfcheck": bool(k.get("selfcheck")), "big_check": bool(k.get("big_check")), "big_model": "sonnet", "big_cap_usd": BIG_CAP_USD,
        "n_by_arm": k.get("n_by_arm", {}),
        "live_recall_sha256": sha256_file(os.path.join(HOME, ".claude", "hooks", "recall.py")),      # the injected text still names ~/.claude/hooks/recall.py
        "pinned_recall_sha256": sha256_file(os.path.join(lh, "hooks", "recall.py")),
        "env_allowlist": list(ENV_ALLOW), "env_overrides": list(OVERRIDE_ENV), "auto_memory": "off (%s=1 and autoMemoryEnabled=false)" % MEMORY_ENV,
        "nc_window": nc_window, "nc_lines": nc_lines, "nc_estimate_tokens": nc_est, "levels": levels,
        "work_base": work_base(a.work_base), "call_cap_usd": a.call_cap_usd or 1.50,
        "turn_timeout_s": a.turn_timeout_s or (420 if "haiku" in (a.model or k["model"]) else 900),
        "jobs": a.jobs or 2, "budget_usd": a.budget_usd, "claude_version": sh(["claude", "--version"]),
        "allowed_tools": ALLOWED_TOOLS, "started": now_iso(),
        "command": " ".join(sys.argv),
    }
    if cfg["budget_usd"] is None:       # default budget: 1.5 x the estimate of the whole run (none for the slice preset)
        est = cost.estimate_per_seed(cfg)
        if est:
            cfg["budget_usd"] = round(gates.DEFAULT_BUDGET_FACTOR * est * (1 if cfg["kind"].startswith("preflight") else len(cfg["seeds"])), 2)
            cfg["budget_default"] = True
    return cfg


def work_units(cfg):
    """(seed, arm, condition). Arms A, B and R run each condition. Z and O are controls with no condition: one unit each."""
    conds = cfg.get("conditions", ["compact"])
    d3 = cfg.get("probe_design", 1) >= 3
    out = []
    for s in cfg["seeds"]:
        for arm in cfg["arms"]:
            if arm in ("A", "B", "R", "K"):
                # design 3, no compaction: arms B, R and K fork the session of arm A (run_fork_view), so they have no unit of their own
                out += [(s, arm, c) for c in conds if not (d3 and c == "nocompact" and arm != "A")]
            else:
                out.append((s, arm, "compact"))
                if d3 and "nocompact" in conds:       # design-3 controls: arm O (short oracle) and arm Z (amnesia)
                    out.append((s, arm, "nocompact"))
    return out


def execute(ctx):
    cfg = ctx.cfg
    snap0 = sha256_file(os.path.join(HOME, ".claude", "settings.json"))
    units = work_units(cfg)
    code = 0
    ctx.state["status"] = "running"
    ctx.save()
    try:
        if cfg.get("selfcheck") and "selfcheck" not in ctx.state:
            ok_, lines_ = selfcheck_core(cfg["model"], cfg["lh_path"], cfg["work_base"])
            ctx.state["selfcheck"] = {"ok": ok_, "lines": lines_}
            ctx.save()
            if not ok_:
                raise Fatal("deny-rule selfcheck failed: " + " | ".join(lines_)[:300])
        if cfg.get("big_check"):
            run_big_check(ctx)
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["jobs"]) as ex:
            futs = [ex.submit(run_unit_gated, ctx, s, arm, cond) for s, arm, cond in units]
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
    if ctx.state.get("gate_abort"):
        ctx.state["status"] = "aborted"
        code = 4
        print("run aborted by a gate: %s" % " | ".join(ctx.state["gate_abort"]), file=sys.stderr)
    ctx.state["ended"] = now_iso()
    ctx.state["settings_hash_changed"] = sha256_file(os.path.join(HOME, ".claude", "settings.json")) != snap0
    ctx.save()
    unfinished = [n for n, s in ctx.state["sessions"].items() if s["status"] != "done"]
    print("run %s: %s, spent $%.4f, sessions not finished: %s" % (cfg["run_id"], ctx.state["status"], ctx.state["spent"], unfinished or "none"))
    if code == 4:
        print("Fix the cause, then continue from the last finished seed with: run.sh resume %s [--ignore-gates]" % cfg["run_id"])
    if code == 3:
        print("paused. Continue with: run.sh resume %s [--budget-usd X]" % cfg["run_id"])
    if ctx.state["settings_hash_changed"]:
        print("warning: ~/.claude/settings.json changed during the run (another chat may have written it)", file=sys.stderr)
    return code


def cmd_estimate(a):
    """Print the cost of the pilot and of the full run of the lost-in-the-middle design. No model call."""
    return cost.main([])


def cmd_run(a):
    cfg = build_cfg(a.kind, a)
    if a.dry_run:
        units = work_units(cfg)
        print("dry run: no model call. run %s, kind %s, model %s, window %s, nc window %s (%s lines per filler file)" % (
            cfg["run_id"], cfg["kind"], cfg["model"], cfg["window"], cfg["nc_window"], cfg["nc_lines"]))
        print("units (seed, arm, condition):", len(units), "; conditions", cfg["conditions"], "; phrasings", cfg["phrasings"], "; probe design", cfg["probe_design"])
        print("budget_usd: %s%s" % (cfg["budget_usd"], " (default: %.1f x the estimate of the whole run; the run pauses there, exit code 3)" % gates.DEFAULT_BUDGET_FACTOR if cfg.get("budget_default") else ""))
        c_ = {"levels": cfg.get("levels"), "seeds": len(cfg["seeds"]), "arms": cfg["arms"], "n": cfg["n"], "conditions": cfg["conditions"], "phrasings": cfg["phrasings"], "n_by_arm": cfg.get("n_by_arm"), "design": cfg["probe_design"]}
        if cfg["kind"].startswith("preflight"):
            tot, ln = cost.estimate_preflight(cfg["arms"], cfg["conditions"])
            print("ESTIMATE FOR THIS RUN (Haiku, assumptions in cost.py):\n" + "\n".join("  " + x for x in ln) + "\n  total about $%.2f (target: under $3)" % tot)
            return 0
        print((cost.render3 if cfg["probe_design"] >= 3 else cost.render)("ESTIMATE FOR THIS RUN", c_)[1])
        if cfg["probe_design"] >= 3:
            print("\n".join(cost.ASSUMPTIONS3))
        return 0
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
    if getattr(a, "ignore_gates", False):
        if not getattr(a, "i_know", False):
            print("--ignore-gates switches the abort gates off for the rest of the run. Add --i-know if you really mean it.", file=sys.stderr)
            return 2
        cfg["gates"] = False
        print("WARNING: abort gates are OFF for this run (--ignore-gates --i-know). The run can spend money on a broken design.", file=sys.stderr)
    write_json(os.path.join(run_dir, "config.json"), cfg)
    ctx = Ctx(run_dir, cfg)
    ctx.state.pop("gate_abort", None)
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
    ok, lines = selfcheck_core(a.model, os.path.realpath(a.lh_path or REPO), a.work_base, echo=True)
    return 0 if ok else 1


def selfcheck_core(model, lh_path, work_base_arg, echo=False):
    """Returns (ok, lines)."""
    out = []

    def say(*x):
        out.append(" ".join(str(i) for i in x))
        if echo:
            print(*x)
    cfg = {"run_id": "selfcheck", "model": model, "window": 30000, "work_base": work_base(work_base_arg), "call_cap_usd": 0.5,
           "turn_timeout_s": 300, "lh_path": lh_path, "budget_usd": None, "gen_version": gen.GEN_VERSION,
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
        say("arm %s: denied tools %s; secret in reply: %s" % (arm, names, leaked))
        say("  reply:", res.replace("\n", " | ")[:600])
        good = leaked is False and "Read" in names and "Bash" in names
        ok_all = ok_all and good
    shutil.rmtree(d, ignore_errors=True)
    say("selfcheck:", "PASS" if ok_all else "FAIL", "(clean up with: run.sh cleanup)")
    return ok_all, out


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
    rs.add_argument("--ignore-gates", action="store_true", help="continue after a gate stopped the run (fix the cause first; needs --i-know)")
    rs.add_argument("--i-know", action="store_true", help="confirm --ignore-gates")
    sp.add_parser("estimate")
    sc = sp.add_parser("selfcheck")
    sc.add_argument("--model", default="haiku")
    sc.add_argument("--lh-path")
    sc.add_argument("--work-base")
    c = sp.add_parser("cleanup")
    c.add_argument("--all", action="store_true", help="also delete folders of paused runs")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--work-base")
    a = ap.parse_args()
    return {"run": cmd_run, "resume": cmd_resume, "cleanup": cmd_cleanup, "selfcheck": cmd_selfcheck, "estimate": cmd_estimate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
