#!/usr/bin/env python3
"""SessionStart(compact) hook: re-inject the pre-compaction handoff. Silent on any problem,
except a DEGRADED handoff, which is flagged to the model (first line) and recorded in
<sid>-degraded.json for the compact plugin."""
import datetime, json, os, re, sys, time
os.umask(0o077)  # every file this hook creates is private: mode 0600, folders 0700

HANDOFFS_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(
    os.path.expanduser("~"), ".local", "state", "claude-handoffs")
MAX_AGE_S = 15 * 60
TEXTS = {"agent_absent": "no agent handoff", "agent_stale": "agent handoff stale",
         "fallback_failed": "Haiku fallback failed", "fallback_timeout": "Haiku fallback timed out",
         "no_health": "no health record for this compaction", "handoff_missing": "no handoff file for this session",
         "handoff_old": "handoff not refreshed for this compaction"}
CODE_ORDER = ["agent_absent", "agent_stale", "fallback_failed", "fallback_timeout", "section_errors",
              "no_health", "handoff_missing", "handoff_old"]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            o = json.load(f)
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def age_ok(ts):
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (time.time() - t.timestamp()) <= MAX_AGE_S
    except Exception:
        return False


def age_ok_s(ts, seconds):
    try:
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (time.time() - t.timestamp()) <= seconds
    except Exception:
        return False


def warning_text(codes, errors):
    parts = []
    for c in CODE_ORDER:
        if c not in codes:
            continue
        if c == "section_errors":
            names = [e for e in (errors or []) if isinstance(e, str)]
            parts.append("sections unavailable: " + ", ".join(names))
        else:
            parts.append(TEXTS[c])
    return "; ".join(parts)


def write_degraded(sid, codes, trigger):
    tmp = None
    try:
        path = os.path.join(HANDOFFS_DIR, f"{sid}-degraded.json")
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "sid": sid, "ts": utc_now(), "reasons": codes,
                       "trigger": trigger if isinstance(trigger, str) else "unknown",
                       "detected_by": "inject"}, f)
        os.replace(tmp, path)
    except BaseException:
        pass


PREFIX = "Pre-compaction handoff (auto-saved):\n"
LIMIT = 10000      # additionalContext of ONE hook must stay under this (Claude Code spills larger values to a file)
HARD_MAX = 9900    # each part's total output (warning + header + content + continuation line) stays <= this
INLINE_MAX = 9700  # content target per part
NAMES_MAX = 150    # the section-name list in a continuation line is cut to this many chars
MAX_PARTS = 3      # F37.6
HOOK_VERSION = "1.24.0"


def hooks_home():
    """F72: HANDOFF_HOOKS_HOME when it names an existing folder, else None (then ~/.claude/hooks as before)."""
    v = (os.environ.get("HANDOFF_HOOKS_HOME") or "").strip()
    return v if v and os.path.isdir(v) else None


def hook_candidates(name, first, second):
    """F72: with the override, only that folder (no fallback, no mixing of two trees); else (first, second) as before."""
    h = hooks_home()
    return [os.path.join(h, name)] if h else [first, second]

_SID = ["unknown"]


def log_error(exc):
    """F42.6: one JSON line {ts, hook, version, sid, error, trace} in <state>/hooks-error.jsonl (rotated to .1 above
    1,000,000 bytes). Never raises."""
    try:
        import traceback
        path = os.path.join(HANDOFFS_DIR, "hooks-error.jsonl")
        os.makedirs(HANDOFFS_DIR, mode=0o700, exist_ok=True)
        try:
            if os.path.getsize(path) > 1000000:
                os.replace(path, path + ".1")
        except OSError:
            pass
        tr = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip("\n").splitlines()[-20:]
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": utc_now(), "hook": "inject", "version": HOOK_VERSION, "sid": str(_SID[0])[:36],
                                "error": f"{type(exc).__name__}: {exc}"[:300], "trace": tr}) + "\n")
    except BaseException:
        pass


try:
    _a = sys.argv[1:]
    PART = int(_a[_a.index("--part") + 1]) if "--part" in _a else 1  # no arg = part 1 (old installs keep working)
except Exception:
    PART = 1
if PART not in (1, 2, 3):
    PART = 1


_NOTE = [""]  # F51: the "prepared at" line for a precomputed compaction; part 1 only


def header(k, first=""):
    return first + PREFIX + _NOTE[0] if k == 1 else f"Pre-compaction handoff (auto-saved, part {k}):\n"


def cont_line(k, n, names, path):
    where = f"part {k + 1} (injected separately; parts may appear in any order) and in {path}" if k < MAX_PARTS else path
    return f"[Handoff continues in {where}: {n} more chars (sections: {names}). Read it before acting.]"


def names_of(rest):
    names = []
    for m in re.finditer(r"(?m)^## (.+)$", rest):
        nm = re.sub(r"\s*\(.*$", "", m.group(1)).strip()
        if nm and nm not in names:
            names.append(nm)
    out = ", ".join(names) if names else "rest of file"
    if len(out) > NAMES_MAX:
        cut = out.rfind(", ", 0, NAMES_MAX - 5)
        out = (out[:cut] if cut > 0 else out[:NAMES_MAX - 5]) + ", ..."
    return out


def agent_end(txt):
    """Index where the agent (or Haiku) section ends; 1 when there is none. Its own F35.A headings
    (## Goal ... ## Exact next step) belong to it."""
    am = re.search(r"(?m)^## (Agent handoff|Model-written handoff)", txt)
    if not am:
        return 1
    inner = ("Goal", "Parallel work running", "Decisions and why", "Done", "In progress", "Dead ends", "Exact next step")
    nxt = [m.start() for m in re.finditer(r"(?m)^## (.+?)\s*$", txt)
           if m.start() > am.start() and m.group(1) not in inner]
    return nxt[0] if nxt else len(txt) + 1


def render(first, txt, path):
    """F37.3/F37.5/F37.6: list of up to MAX_PARTS additionalContext strings. Part 1 equals the plain
    section 6 string when it fits under LIMIT. Every part is <= HARD_MAX chars."""
    parts, rest = [], txt
    for k in range(1, MAX_PARTS + 1):
        head = header(k, first)
        if len(head) + len(rest) < LIMIT:
            parts.append(head + rest)
            return parts
        # reserve the continuation line first (N has at most len(str(len(rest))) digits), then fit content
        reserve = len(cont_line(k, "9" * len(str(len(rest))), "x" * NAMES_MAX, path)) + 1
        budget = min(INLINE_MAX - len(head), HARD_MAX - len(head) - reserve)
        need = agent_end(rest) if k == 1 else 1
        fit = [m.start() for m in re.finditer(r"(?m)^## ", rest) if need <= m.start() <= budget]
        if fit:
            cut = max(fit)  # last whole-section boundary that fits
        else:  # no whole section fits (oversize section / agent section): last line boundary
            nl = rest.rfind("\n", 0, budget + 1)
            cut = nl + 1 if nl > 0 else max(budget, 0)
        inl = rest[:cut].rstrip("\n")
        left = rest[len(inl):]
        parts.append(head + inl + "\n" + cont_line(k, len(left), names_of(left), path))
        rest = left.lstrip("\n")
        if not rest:  # (cannot normally happen: the loop only cuts when content overflowed)
            return parts
    return parts


def emit(ctx):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


RECALL_LINE = ("- recall: earlier chats in this project are searchable word for word: python3 ~/.claude/hooks/recall.py "
               "search \"<words or regex>\" (same full path for: open <id> [<id> ...]; user --last 20. One call takes several patterns or several ids. Do not use cd, a loop or a shell variable). Use it before acting on "
               "anything vague here (exact commands, numbers, paths, names, the user's wording) and whenever the user "
               "refers to something said earlier; quote what you find instead of guessing. "
               "If recall and the decisions list disagree, the decisions list and the user's latest words win: "
               "recall shows history, not current rules. "
               "If a hit is a tool call that shows `-> result <id>`, open that id to see what the tool returned.\n")


RECALL_AGENT_MARK = "<!-- lossless-handoff:recall-agent -->"
RECALL_AGENT_SENTENCE = ("For wide lookups (many hits, long messages, or a whole node), ask the recall subagent "
                         "(Agent tool, subagent_type \"recall\") so the main context stays small; "
                         "for one exact fact, search directly.")


def recall_agent_ours():
    """F55.c: ~/.claude/agents/recall.md exists and holds our marker line. F72: with the override, only
    <HANDOFF_HOOKS_HOME>/agents/recall.md."""
    try:
        h = hooks_home()
        ap = os.path.join(h, "agents", "recall.md") if h else os.path.join(os.path.expanduser("~"), ".claude", "agents", "recall.md")
        with open(ap, encoding="utf-8",
                  errors="replace") as f:
            return RECALL_AGENT_MARK in f.read()
    except OSError:
        return False


def recall_path():
    for p in hook_candidates("recall.py", os.path.join(os.path.expanduser("~"), ".claude", "hooks", "recall.py"),
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")):
        if os.path.isfile(p):
            return p
    return None


RECALL_RULE_LINE = ("- recall rule: if your context does not clearly show an earlier number, path, name, command or decision, "
                    "run the recall command above before you answer. Copy it exactly: full path, no cd. "
                    "A failed or denied command is not \"not found\": run the exact command again. "
                    "If no search can run, say so.")
RECALL_RULE_MCP = " If Bash is not allowed, call search_earlier_chat."
RECALL_MCP_SENTENCE = "Or call the search_earlier_chat tool."


def _hooks_dir():
    return hooks_home() or os.path.join(os.path.expanduser("~"), ".claude", "hooks")  # F72 override, else HOME from the environment


def rpath(text):
    """F72: with the override, every printed recall command names <HANDOFF_HOOKS_HOME>/recall.py."""
    h = hooks_home()
    return text.replace("~/.claude/hooks/recall.py", h.rstrip("/") + "/recall.py") if h else text



def recall_rules_on():
    """F68: the marker file ~/.claude/hooks/.recall-rules exists and HANDOFF_RECALL_RULES is not 0."""
    if (os.environ.get("HANDOFF_RECALL_RULES") or "").strip() == "0":
        return False
    return os.path.isfile(os.path.join(_hooks_dir(), ".recall-rules"))


def recall_mcp_ours():
    """F83.e: the recall MCP server (name `lossless-recall`, as `install.sh --recall-mcp` registers it) is in the user MCP
    configuration (`mcpServers` in ~/.claude.json, HOME from the environment). A file in the hooks folder is not enough."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return "lossless-recall" in (cfg.get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        return False


def rule_line():
    """F83.d + F83.e: the rule line, with the MCP sentence at the end when the recall MCP server is registered."""
    return RECALL_RULE_LINE + (RECALL_RULE_MCP if recall_mcp_ours() else "")


def recall_line():
    """F44.11(a): the F44.6 line, only when recall.py exists in ~/.claude/hooks (HOME from the environment) or next to this hook.
    F55.c: ends with one more sentence when the recall subagent file is installed. F69: and one more when recall-mcp.py is
    installed. F68: the strict rule line follows the recall line when the marker is there."""
    if recall_path():
        return rpath(RECALL_LINE[:-1] + (" " + RECALL_AGENT_SENTENCE if recall_agent_ours() else "")
                     + (" " + RECALL_MCP_SENTENCE if recall_mcp_ours() else "") + "\n"
                     + (rule_line() + "\n" if recall_rules_on() else ""))
    return ""


_NODE_LINE = [""]  # F54.d: the "summary node" line for this compaction; part 1 only


def sync_nodes(d):
    """F54.b/F54.d: sync this event's own transcript into nodes.jsonl; return the F54.d line for the newest leaf, or ''.
    Any error is logged and gives ''. Only for source compact (or no source)."""
    try:
        src = d.get("source")
        if isinstance(src, str) and src.strip() and src.strip() != "compact":
            return ""
        tp = own_transcript(d)
        cwd = d.get("cwd")
        rp = recall_path()
        if not rp or not tp or not os.path.isfile(tp) or not isinstance(cwd, str) or not cwd.strip():
            return ""
        import importlib.util
        spec = importlib.util.spec_from_file_location("lh_recall", rp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ldir = mod.ledger_dir_for(mod.project_root(cwd))
        mod.sync_transcript(ldir, tp, 3.0)
        rtp = os.path.realpath(tp)
        leaves = [n for n in mod.read_nodes(ldir) if os.path.realpath(n["transcript"]) == rtp]
        if not leaves:
            return ""
        n = max(leaves, key=lambda x: x.get("seq") or 0)
        return rpath(f"- summary node: this chat continues after node {n['id']} (compaction #{n.get('seq')}, lines "
                     f"{n['first_line']}-{n['last_line']}). See all nodes: python3 ~/.claude/hooks/recall.py map; "
                     f"one node: recall.py describe {n['id']}.\n")
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        log_error(e)
        return ""


def decorate(txt):
    """F54.d + F55.c on the handoff text: put the node line right after the recall line (adding the recall line after
    `- authority:` when the text has none, but only when a node line or the recall subagent file asks for it), and add the
    recall subagent sentence to an existing recall line."""
    try:
        txt = rpath(txt) if txt else txt  # F72: a stored recall line names the override path
        node = _NODE_LINE[0]
        agent = recall_agent_ours()
        mcp = recall_mcp_ours()
        rules = recall_rules_on()
        if not txt or not (node or agent or mcp or rules):
            return txt
        lines = txt.split("\n")
        ri = next((i for i, x in enumerate(lines) if x.startswith("- recall:")), None)
        if ri is None:
            if not recall_path():
                ri = None
            else:
                ai = next((i for i, x in enumerate(lines) if x.startswith("- authority:")), None)
                at = ai + 1 if ai is not None else min(1, len(lines))
                lines.insert(at, recall_line().rstrip("\n"))
                ri = at
        else:
            if agent and RECALL_AGENT_SENTENCE not in lines[ri]:
                lines[ri] = lines[ri].rstrip() + " " + RECALL_AGENT_SENTENCE
            if mcp and RECALL_MCP_SENTENCE not in lines[ri]:
                lines[ri] = lines[ri].rstrip() + " " + RECALL_MCP_SENTENCE
            if rules and not any(x.startswith(RECALL_RULE_LINE) for x in lines):
                lines.insert(ri + 1, rule_line())
        if node:
            at = min(1, len(lines)) if ri is None else ri + 1
            if ri is not None and at < len(lines) and lines[at].startswith(RECALL_RULE_LINE):
                at += 1  # the node line goes after the rule line, which follows the recall line
            lines.insert(at, node.rstrip("\n"))
        return "\n".join(lines)
    except Exception as e:
        log_error(e)
        return txt


def ledger_part_head(k, path):
    return (f"Project decisions for this repo (part {k} of the ledger; standing rules first, then decisions, then "
            f"tasks, newest first within each; full ledger: {path}). Standing unless the user says otherwise:"
            + (" This list is written by the hooks; do not edit decisions.md yourself." if k == 1 else "")  # F90.d: same line
            + "\n"
            + (recall_line() if k == 1 else ""))


def index_note(path):
    # F88.b: the pointer names the lookup command (the path argument stays for callers)
    return rpath("Index entries are summaries: before you act on one, read the full entry: "
                 "python3 ~/.claude/hooks/recall.py decisions D<n>\n")


def more_entries_line(k):
    """F88.b: the count line of the ledger index."""
    return rpath(f"- ({k} more active entries not listed; search them: "
                 f"python3 ~/.claude/hooks/recall.py decisions \"<words>\")")


# F90.b: the G1 and G2 parsers are in hooks/lossless_handoff/grammar.py. This hook loads that file by path. If the
# file is missing, it runs a copy of it (GRAMMAR_SRC); a smoke check proves that the copy is byte-identical to the file.
GRAMMAR_SRC = r'''# Shared grammars (F90): G1 decisions-list line, G2 recall hit id. One parser for each format.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file. postcompact-inject.py and recall.py load this file by path as a module, and keep a
# byte-identical fallback copy (GRAMMAR_SRC) that a smoke check compares with this file (F90.b).
# This file must not hold three single quotes in a row, and must import only `re` (the entry file has it).
import re

_DATE = r"\d{4}-\d{2}-\d{2}"
_RES_RE = re.compile(r"^- D(\d+) \[(%s)\] RESOLVED \[(%s)\]: (.+?) \| was: (.+)$" % (_DATE, _DATE))
_SUP_RE = re.compile(r"^- D(\d+) \[(%s)\] SUPERSEDED by D(\d+) \[(%s)\]: (.+?) \| because: (.+)$" % (_DATE, _DATE))
_ACT_RE = re.compile(r"^- D(\d+) \[(%s)\] (.+?) \| why: (.+)$" % _DATE)
_LOOSE_RE = re.compile(r"^- D(\d+) \[(%s)\] (.+)$" % _DATE)
_TAG_RE = re.compile(r"^\[(rule|decision|task)\] ")
_CAPS_RE = re.compile(r"^(?:SUPERSEDED|RESOLVED|[A-Z]{2,}(?: \[|:| \d{4}-\d{2}-\d{2}))")  # RETIRED [..], DONE: or DONE 2026-11-15: is not active text (F90.a)
_ID_RE = re.compile(r"^- D(\d+) ")
_RID_RE = re.compile(r"^([A-Za-z0-9_-]+)(?:/([A-Za-z0-9_-]+))?:(\d+)$")
_HIT_TAIL_RE = re.compile(r"^\d{4}-")


def parse_decision(line):
    """G1: one decisions-list line -> a dict, or None for a malformed line. The dict has `form`: skip (header, blank
    or comment line), act, nowhy (legacy active line with no `| why:`, F44.11(e)), sup or res; and `id`, `date`, `line`.
    act and nowhy also have `text` (with its tag) and `tag` (rule|decision|task|None); act has `why`; sup has `by`,
    `bdate`, `text` (old text) and `why` (the because text); res has `rdate`, `reason` and `was`."""
    s = (line or "").rstrip("\r\n").strip()
    if not s or s.startswith("#") or s.startswith("<!--"):
        return {"form": "skip", "line": s}
    m = _RES_RE.match(s)
    if m:
        d = {"form": "res", "id": int(m.group(1)), "date": m.group(2), "rdate": m.group(3), "reason": m.group(4), "was": m.group(5)}
    else:
        m = _SUP_RE.match(s)
        if m:
            d = {"form": "sup", "id": int(m.group(1)), "date": m.group(2), "by": int(m.group(3)), "bdate": m.group(4),
                 "text": m.group(5), "why": m.group(6)}
        else:
            m = _ACT_RE.match(s)
            form = "act"
            if not m:
                m = _LOOSE_RE.match(s)
                form = "nowhy"
            if not m:
                return None
            text = m.group(3)
            tm = _TAG_RE.match(text)
            if _CAPS_RE.match(text[tm.end():] if tm else text):
                return None
            d = {"form": form, "id": int(m.group(1)), "date": m.group(2), "text": text, "tag": tm.group(1) if tm else None,
                 "why": m.group(4) if form == "act" else None}
    if d["id"] < 1:
        return None
    d["line"] = s
    return d


def scan_decisions(text):
    """G1: a decisions list -> [(line number from 1, raw line, parse_decision result)]."""
    return [(n, raw, parse_decision(raw)) for n, raw in enumerate((text or "").splitlines(), 1)]


def malformed_decisions(text):
    """G1: [(line number from 1, raw line)] of the malformed lines of a decisions list."""
    return [(n, raw) for n, raw, p in scan_decisions(text) if p is None]


def decision_line_id(line):
    """The id n of a line that starts with `- D<n> `, else None. For a line kept as it is (F90.c)."""
    m = _ID_RE.match((line or "").strip())
    return int(m.group(1)) if m else None


def parse_recall_id(s):
    """G2: `<sid8>:<line>` or `<sid8>/<agent8>:<line>` -> (sid, agent or None, line), or None. The parts are the
    characters of a session or agent id (a prefix of any length is read, as `recall.py open` always did)."""
    m = _RID_RE.match(s or "")
    if not m or int(m.group(3)) < 1:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def parse_recall_hit(ln):
    """G2: one search-output line `<id> <timestamp> ...` -> (sid, agent or None, line), or None."""
    head, _, rest = (ln or "").partition(" ")
    r = parse_recall_id(head)
    return r if r and _HIT_TAIL_RE.match(rest) else None
'''
_GRAMMAR = []


def grammar():
    """The F90 grammar module: the file hooks/lossless_handoff/grammar.py, else a module made from GRAMMAR_SRC."""
    if _GRAMMAR:
        return _GRAMMAR[0]
    import importlib.util
    mod = None
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (here, os.path.dirname(os.path.realpath(__file__))):
        p = os.path.join(base, "lossless_handoff", "grammar.py")
        if os.path.isfile(p):
            try:
                spec = importlib.util.spec_from_file_location("lh_grammar", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                break
            except Exception:
                mod = None
    if mod is None:
        import types
        mod = types.ModuleType("lh_grammar")
        exec(compile(GRAMMAR_SRC, "grammar.py", "exec"), mod.__dict__)
    _GRAMMAR.append(mod)
    return mod


KIND_ORDER = {"rule": 0, "decision": 1, "task": 2}


def parse_ledger_file(path):
    """F42.1: active entries as dicts (id, kind rule|decision|task, line, text), in priority order: rules, then
    decisions (untagged count as decision), then tasks, each newest id first. SUPERSEDED and RESOLVED lines are skipped."""
    act, other, nowhy = {}, set(), []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f.read().splitlines():
            d = grammar().parse_decision(ln)  # F90: G1; a malformed line gives None and is never injected
            if not d or d["form"] == "skip":
                continue
            i = d["id"]
            if d["form"] in ("sup", "res"):
                other.add(i)
                continue
            if i in act or i in other:
                continue
            if d["form"] == "nowhy":  # F44.11(e): inject an active line that lacks `| why:`; log its id
                nowhy.append(i)
            text = d["text"][len(d["tag"]) + 3:] if d["tag"] else d["text"]
            act[i] = {"id": i, "kind": d["tag"] or "decision", "line": d["line"], "text": text}
    if nowhy and PART == 1:  # one log line per entry, from the part-1 call only (parts 2-3 re-read the same file)
        for i in nowhy:
            if i in act:
                log_error(ValueError(f"active entry D{i} has no '| why:' (injected anyway)"))
    ents = sorted(act.values(), key=lambda e: (KIND_ORDER[e["kind"]], -e["id"]))
    return [e for e in ents if e["id"] not in other]


def index_line(e, width):
    t = " ".join(e["text"].split())
    return f"- D{e['id']} [{e['kind']}] " + (t[:width] + "\u2026" if len(t) > width else t)


def pack(full, idx, count_line, path):
    """Greedy whole-line packing of the stream (full-text lines, then the INDEX region) into <= MAX_PARTS parts of
    <= HARD_MAX chars. Returns the list of part texts or None when it does not fit."""
    stream = [("f", x) for x in full]
    if idx or count_line:
        stream.append(("m", "INDEX (active entries not shown in full above; same priority order):"))
        stream += [("i", x) for x in idx]
        if count_line:
            stream.append(("c", count_line))
    parts, k, pos = [], 1, 0
    while pos < len(stream) and k <= MAX_PARTS:
        in_idx = stream[pos][0] in ("m", "i", "c")
        head = ledger_part_head(k, path) + (index_note(path) if in_idx else "")
        out, used = [], len(head)
        while pos < len(stream):
            kind, x = stream[pos]
            if kind == "m" and not in_idx:  # the index starts in this part: its note must fit too
                need = len(index_note(path)) + len(x) + 1
                nxt = stream[pos + 1][1] if pos + 1 < len(stream) else ""
                if used + need + len(nxt) + 1 >= HARD_MAX:
                    break
                used += len(index_note(path))
                head += index_note(path)
                in_idx = True
            if used + len(x) + 1 >= HARD_MAX:
                break
            out.append(x)
            used += len(x) + 1
            pos += 1
        if not out:
            return None
        parts.append(head + "\n".join(out))
        k += 1
    return parts if pos >= len(stream) else None


def plan_ledger(ents, path):
    """F42.3: choose how many entries get full text (largest n whose layout fits with an index for the rest, index
    lines at 90 chars, else 50, else the oldest tasks and decisions dropped from the index, never rules). Rules
    keep full text first: n never drops below the rule count unless even that cannot fit."""
    N = len(ents)
    full_lines = [e["line"] for e in ents]
    nrules = sum(1 for e in ents if e["kind"] == "rule")
    for width in (90, 50):  # every standing rule keeps its full text while the index of the rest still fits
        idx_all = [index_line(e, width) for e in ents]
        for n in range(N, nrules - 1, -1):
            r = pack(full_lines[:n], idx_all[n:], "", path)
            if r is not None:
                return r
    idx_all = [index_line(e, 50) for e in ents]
    for n in range(min(nrules, N), -1, -1):
        rest = list(range(n, N))
        keep_floor = sum(1 for i in rest if ents[i]["kind"] == "rule")  # rules are never dropped
        lo, hi, best = 0, len(rest) - keep_floor, None  # d = number of oldest non-rule entries dropped
        while lo <= hi:
            mid = (lo + hi) // 2
            drop = set([i for i in reversed(rest) if ents[i]["kind"] != "rule"][:mid])
            kept = [idx_all[i] for i in rest if i not in drop]
            cl = more_entries_line(mid) if mid else ""
            r = pack(full_lines[:n], kept, cl, path)
            if r is not None:
                best, hi = r, mid - 1
            else:
                lo = mid + 1
        if best is not None:
            return best
    return []


def ledger_mode(lpart=1):
    """F39.8/F42.3: SessionStart (compact, startup, resume). Print part `lpart` (1-3) of the project's active
    decisions ledger (read-only, no claude); each part < 10,000 chars."""
    import hashlib, subprocess
    d = json.load(sys.stdin)
    cwd = d.get("cwd") if isinstance(d, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        return
    root = cwd
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and (r.stdout or "").strip():
            root = r.stdout.strip()
    except Exception:
        pass
    key = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(root.rstrip("/")))[:40] + "-" + \
        hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    path = os.path.abspath(os.path.join(HANDOFFS_DIR, "projects", key, "decisions.md"))
    try:
        ents = parse_ledger_file(path)
    except FileNotFoundError:
        ents = []
    if not ents:
        # no ledger: nothing to say, except that F68 asks for the recall line and the rule line (part 1 only)
        if lpart == 1 and recall_rules_on():
            rl = recall_line().rstrip("\n")
            if rl:
                emit(rl)
        return
    parts = plan_ledger(ents, path)
    if len(parts) >= lpart:
        emit(parts[lpart - 1])


SUBAGENT_PREAMBLE = ("These are the user's standing rules for the main session of this project. Apply only the rules "
                     "that fit your task. Your task brief comes first.")
SUBAGENT_HEAD = ("Project rules for this repo (standing; from the Lossless Handoff decisions list; the user's latest "
                 "words win):")
SUBAGENT_MAX = 4000
SUBAGENT_TOTAL = 9900  # F88.c: the whole additionalContext, with the rule index


def ledger_path_for(cwd):
    """The decisions.md path of the project of `cwd` (same key as ledger_mode and F39.1)."""
    import hashlib, subprocess
    root = cwd
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=1)
        if r.returncode == 0 and (r.stdout or "").strip():
            root = r.stdout.strip()
    except Exception:
        pass
    key = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(root.rstrip("/")))[:40] + "-" + \
        hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    return os.path.abspath(os.path.join(HANDOFFS_DIR, "projects", key, "decisions.md"))


def subagent_context(path, preamble=True, index=True):
    """F58.a + F88.c: -> (text, number of full-text rule lines, number of index lines). Only active `rule` entries,
    newest first. Full text is cut at an entry boundary at 4,000 chars; the rules left get an index (90 chars per
    line, else 50, else the oldest lines go) and a pointer line, 9,900 chars in all."""
    try:
        rules = [e for e in parse_ledger_file(path) if e["kind"] == "rule"]
    except OSError:
        rules = []
    rl = recall_line().rstrip("\n")
    if not rules:
        return rl, 0, 0
    pointer = (f"[more rules: python3 ~/.claude/hooks/recall.py decisions --kind rule, "
               f"or the decisions list at {path}]")
    more = rpath(pointer)
    head = (SUBAGENT_PREAMBLE + "\n" if preamble else "") + SUBAGENT_HEAD + "\n"
    tail = ("\n" + rl) if rl else ""
    full = head + "\n".join(e["line"] for e in rules) + tail
    if len(full) <= SUBAGENT_MAX:
        return full, len(rules), 0
    room = SUBAGENT_MAX - len(head) - len(more) - 1 - len(tail)
    kept, used = [], 0
    for e in rules:
        if used + len(e["line"]) + 1 > room:
            break
        kept.append(e["line"])
        used += len(e["line"]) + 1
    if not index:  # F70 reanchor block: the F58.a text as before (full-text rules and the pointer line), no rule index
        return head + "\n".join(kept + [more]) + tail, len(kept), 0
    left = rules[len(kept):]

    def build(idx, dropped):
        ptr = rpath(pointer.replace("[more rules: ", f"[more rules: {dropped} not listed above; ", 1)) if dropped else more
        return (head + "\n".join(kept) + tail + "\nINDEX (rules not shown in full above):\n"
                + "\n".join(idx + [ptr]))
    for width in (90, 50):
        idx = [index_line(e, width) for e in left]
        if len(build(idx, 0)) <= SUBAGENT_TOTAL:
            return build(idx, 0), len(kept), len(idx)
    idx = [index_line(e, 50) for e in left]
    for drop in range(1, len(idx) + 1):
        if len(build(idx[:len(idx) - drop], drop)) <= SUBAGENT_TOTAL:
            return build(idx[:len(idx) - drop], drop), len(kept), len(idx) - drop
    return build([], len(idx)), len(kept), 0


def subagent_health(d, rules, chars, rules_index=0):
    """F58.d: one health.jsonl row. These rows are skipped by --prove and --reconcile. Never raises."""
    try:
        at = d.get("agent_type")
        os.makedirs(HANDOFFS_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(HANDOFFS_DIR, "health.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": utc_now(), "event": "subagent_start", "hook": "inject",
                                "sid": str(d.get("session_id") or "")[:36],
                                "agent_type": at if isinstance(at, str) else "", "rules": rules, "rules_index": rules_index,
                                "chars": chars, "hook_version": HOOK_VERSION}) + "\n")
        if os.path.getsize(path) > 2000000:  # keep the file small: drop the oldest SubagentStart rows
            with open(path, "rb") as f:
                lines = f.readlines()
            sub = [i for i, x in enumerate(lines) if b'"event": "subagent_start"' in x]
            drop = set(sub[:-500])
            tmp = path + f".tmp{os.getpid()}"
            with open(tmp, "wb") as f:
                f.writelines(x for i, x in enumerate(lines) if i not in drop)
            os.replace(tmp, path)
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        log_error(e)


def subagent_start_mode():
    """F58: SubagentStart hook. Prints the project's standing rules and the recall line as additionalContext of the
    new subagent. Never exits non-zero. No decisions, tasks, handoff text or agent notes (D3)."""
    if os.environ.get("HANDOFF_SUBAGENT_RULES") == "0":
        return
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    cwd = d.get("cwd") if isinstance(d, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        return
    _SID[0] = d.get("session_id") or "unknown"
    ctx, nrules, nidx = subagent_context(ledger_path_for(cwd))
    subagent_health(d, nrules, len(ctx), nidx)
    if ctx:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}}))


def rules_block_mode():
    """F70: print the project's standing rules block (the same text as the SubagentStart output) for reanchor.py.
    Prints nothing when the project has no active rule. Writes no health row. Never exits non-zero."""
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    cwd = d.get("cwd") if isinstance(d, dict) else None
    if not isinstance(cwd, str) or not cwd.strip():
        return
    ctx, nrules, _nidx = subagent_context(ledger_path_for(cwd), preamble=False, index=False)  # F70: no SubagentStart preamble
    if ctx and nrules > 0:
        sys.stdout.write(ctx)


RECOVER_CODES = ("handoff_missing", "no_health", "handoff_old")
RECOVERED_FIRST = ("HANDOFF RECOVERED: the pre-compaction hook did not run for this compaction; this handoff was "
                   "rebuilt from the transcript without a model summary. Use recall for detail.\n")


def precompact_script():
    for q in hook_candidates("precompact-handoff.py", os.path.join(os.path.dirname(os.path.abspath(__file__)), "precompact-handoff.py"),
                             os.path.join(os.path.expanduser("~"), ".claude", "hooks", "precompact-handoff.py")):
        if os.path.isfile(q):
            return q
    return None


def fit_recovered(txt):
    """RECOVERED_FIRST + PREFIX + txt under HARD_MAX: drop whole sections other than the user's words from the end,
    then the oldest user lines (the newest are kept), then cut."""
    def total(t):
        return len(RECOVERED_FIRST) + len(PREFIX) + len(t)
    if total(txt) <= HARD_MAX:
        return txt
    first = re.search(r"(?m)^## ", txt)
    head = txt[:first.start()] if first else txt
    secs = [x for x in re.split(r"(?m)^(?=## )", txt[len(head):]) if x]

    def join():
        return head + "".join(secs)
    while total(join()) > HARD_MAX:
        idx = [i for i, x in enumerate(secs) if not x.startswith("## Last user messages")]
        if not idx:
            break
        del secs[idx[-1]]
    while total(join()) > HARD_MAX:
        ui = [i for i, x in enumerate(secs) if x.startswith("## Last user messages")]
        if not ui:
            break
        lines = secs[ui[0]].split("\n")
        body = [i for i, ln in enumerate(lines) if ln.startswith("- ")]
        if len(body) <= 1:
            break
        del lines[body[0]]
        secs[ui[0]] = "\n".join(lines)
    out = join()
    return out if total(out) <= HARD_MAX else out[: max(HARD_MAX - len(RECOVERED_FIRST) - len(PREFIX) - 1, 0)]


def write_json_atomic(path, obj):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def last_boundary_ts(path):
    """`timestamp` of the last compact_boundary record, read from the tail of the file (None if none)."""
    size = os.path.getsize(path)
    pos, buf = size, b""
    with open(path, "rb") as f:
        while pos > 0 and size - pos < 16 * 1024 * 1024:
            step = min(1 << 18, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            for ln in reversed(lines[1:] if pos > 0 else lines):
                if b"compact_boundary" in ln:
                    try:
                        o = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(o, dict) and (o.get("subtype") == "compact_boundary" or o.get("type") == "compact_boundary"):
                        return o.get("timestamp") if isinstance(o.get("timestamp"), str) else None
    return None


def own_transcript(d):
    """The transcript of this event: for a subagent, its own file (the rule of precompact-handoff.py agent_view)."""
    aid = d.get("agent_id")
    aid = aid.strip() if isinstance(aid, str) and aid.strip() else None
    tp = d.get("transcript_path") if isinstance(d.get("transcript_path"), str) else ""
    if aid and tp and "/subagents/" not in tp:
        tp = os.path.join(os.path.dirname(tp), str(d.get("session_id")), "subagents", f"agent-{aid}.jsonl")
    return tp


def boundary_before(path, cutoff):
    """F51: epoch seconds of the newest compact_boundary record whose timestamp is at most `cutoff`
    (None if there is none or the file cannot be read). Reads the file from the end in 1 MiB steps."""
    try:
        pos, carry = os.path.getsize(path), b""
        with open(path, "rb") as f:
            while pos > 0:
                step = min(1 << 20, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + carry
                if pos > 0:  # the first line of this block may be cut: keep it for the next block
                    nl = buf.find(b"\n")
                    carry, body = (buf, b"") if nl < 0 else (buf[:nl], buf[nl + 1:])
                else:
                    body = buf
                if b"compact_boundary" not in body:
                    continue
                best = None
                for ln in body.split(b"\n"):
                    if b"compact_boundary" not in ln:
                        continue
                    try:
                        o = json.loads(ln)
                        if o.get("subtype") != "compact_boundary" and o.get("type") != "compact_boundary":
                            continue
                        t = datetime.datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if t <= cutoff and (best is None or t > best):
                        best = t
                if best is not None:
                    return best
    except Exception:
        pass
    return None


def belongs_to_compaction(d, hts):
    """F51: True when a handoff older than MAX_AGE_S still belongs to this compaction: it is newer than the previous
    compaction boundary (older than 60 s, because the current one may not be flushed yet, Ruling F47), or there is none."""
    try:
        h = datetime.datetime.strptime(hts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return False
    tp = own_transcript(d)
    if not tp or not os.path.isfile(tp) or not os.access(tp, os.R_OK):
        return False  # F51: no readable transcript, so keep the 1.10.0 rule (AC 63)
    b = boundary_before(tp, time.time() - 60)
    return b is None or h > b


def write_receipt(d, key, kind, text):
    """F47.a: append one line to <state>/receipts.jsonl for a handoff part 1 emitted. Never raises, never changes output."""
    try:
        import hashlib
        aid = d.get("agent_id")
        aid = aid.strip() if isinstance(aid, str) and aid.strip() else None
        psid = str(d.get("session_id") or "unknown").replace("/", "_")
        tp = own_transcript(d)
        try:
            bts = last_boundary_ts(tp) if tp else None
        except Exception:
            bts = None
        with open(os.path.join(HANDOFFS_DIR, f"{key}-latest.md"), "rb") as f:
            hsha = hashlib.sha256(f.read()).hexdigest()
        hs = read_json(os.path.join(HANDOFFS_DIR, f"{key}-last-health.json"))  # F87.d: which health record this row used
        hts = hs.get("ts") if hs and isinstance(hs.get("ts"), str) else None
        row = {"ts": utc_now(), "sid": psid[:8], "agent_id": aid, "key": key, "transcript": tp, "boundary_ts": bts,
               "health_ts": hts,
               "kind": kind, "handoff_sha256": hsha, "part1_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
               "part1_head": text[:300], "recall_rules": bool(recall_rules_on())}
        with open(os.path.join(HANDOFFS_DIR, "receipts.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            log_error(e)


def handoff_used(key, health_ts):
    """F87.d: True when receipts.jsonl (last 256 KiB) holds a row with the same key and health_ts and a ts more than
    60 s old: this handoff already served a compaction. A row without health_ts never matches. Never raises."""
    try:
        if not isinstance(health_ts, str) or not health_ts:
            return False
        with open(os.path.join(HANDOFFS_DIR, "receipts.jsonl"), "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 262144))
            data = f.read().decode("utf-8", errors="replace")
        for ln in data.splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if not (isinstance(r, dict) and r.get("key") == key and r.get("health_ts") == health_ts):
                continue
            try:
                t = datetime.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if time.time() - t.timestamp() > 60:
                return True
    except Exception:
        pass
    return False


def try_recover(d, sid):
    """F45.b: run `precompact-handoff.py --recover` with the same hook JSON (deadline 6 s). Returns the text to emit, or
    None after logging to hooks-error.jsonl (the caller then falls back to the DEGRADED text)."""
    try:
        import subprocess
        script = precompact_script()
        if not script:
            raise RuntimeError("precompact-handoff.py not found for recovery")
        r = subprocess.run([sys.executable, script, "--recover"], input=json.dumps(d), capture_output=True, text=True,
                           timeout=6)
        res = None
        for ln in reversed((r.stdout or "").splitlines()):
            try:
                res = json.loads(ln)
                break
            except Exception:
                continue
        if not isinstance(res, dict) or res.get("ok") is not True:
            raise RuntimeError("recovery failed: " + str((res or {}).get("error") or f"exit {r.returncode}")[:200])
        with open(os.path.join(HANDOFFS_DIR, f"{sid}-latest.md"), encoding="utf-8", errors="replace") as f:
            txt = f.read().strip()
        if not txt:
            raise RuntimeError("recovery wrote an empty handoff")
        return RECOVERED_FIRST + PREFIX + fit_recovered(decorate(txt))
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        log_error(e)
        return None


def _vers(path):
    try:
        m = re.search(r'(?m)^HOOK_VERSION = "([^"]*)"', open(path, encoding="utf-8", errors="replace").read())
        return m.group(1) if m else None
    except Exception:
        return None


def effective_cleanup_days(user, managed):
    """F52.a: the managed value if that file has the key, else the user value, else 30. A value that is not an
    integer or is below 0 counts as 30."""
    raw = 30
    if isinstance(managed, dict) and "cleanupPeriodDays" in managed:
        raw = managed["cleanupPeriodDays"]
    elif isinstance(user, dict) and "cleanupPeriodDays" in user:
        raw = user["cleanupPeriodDays"]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 30
    return raw


RECALL_ALLOW_RULE = "Bash(python3 ~/.claude/hooks/recall.py:*)"


def decisions_stats(path):
    """F88.d: numbers for the decisions list at `path`, or None (no list, no rule, or any error). `room` is the number of
    rule characters (each line plus its newline) that plan_ledger can show in full: the 3 parts minus their headers.
    `index_only` is the number of rules that plan_ledger shows as an index line or not at all."""
    try:
        ents = parse_ledger_file(path)
        rules = [e for e in ents if e["kind"] == "rule"]
        if not rules:
            return None
        room = sum(HARD_MAX - len(ledger_part_head(k, path)) - 1 for k in range(1, MAX_PARTS + 1))
        chars = sum(len(e["line"]) + 1 for e in rules)
        text = "\n".join(plan_ledger(ents, path))
        shown = set(i for i in (grammar().decision_line_id(x) for x in text.split("\n")) if i)
        lines = set(text.split("\n"))
        full = sum(1 for e in rules if e["line"] in lines)
        return {"rules": len(rules), "chars": chars, "room": room, "pct": int(100 * chars / room),
                "active": len(ents), "unlisted": sum(1 for e in ents if e["id"] not in shown),
                "index_only": len(rules) - full}
    except Exception as e:
        log_error(e)
        return None


def check_mode(verbose=False):
    """F45.d, F86: one line `HANDOFF CHECK: ...` when something is wrong, nothing when all is fine. F86.a: install
    problems get the installer advice, data problems do not. `verbose` (F86.b) prints one line for each check."""
    import hashlib
    home = os.path.expanduser("~")
    hooks_dir = hooks_home() or os.path.join(home, ".claude", "hooks")
    allp = []  # (class, text): class "i" is an install problem, "d" a data problem
    checks = []  # (name, [problem texts]) in order
    seen = [0]

    class _Probs:
        def __init__(self, cls):
            self.cls = cls

        def append(self, text):
            allp.append((self.cls, text))

        def __iter__(self):
            return iter([t for c, t in allp if c == self.cls])

        def clear(self):
            allp[:] = [x for x in allp if x[0] != self.cls]
            seen[0] = min(seen[0], len(allp))

    problems, data = _Probs("i"), _Probs("d")

    def mark(name):
        checks.append((name, [t for _c, t in allp[seen[0]:]]))
        seen[0] = len(allp)
    try:
        with open(os.path.join(home, ".claude", "settings.json"), encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("not an object")
    except Exception:
        st = None
        problems.append("settings.json unreadable")
    managed = None  # F46.g: managed settings (system level) count as well as user settings
    try:
        mpath = (os.environ.get("LOSSLESS_HANDOFF_MANAGED_SETTINGS") or "").strip() or (
            os.environ.get("HANDOFF_MANAGED_SETTINGS_PATH") or "").strip() or (
            "/Library/Application Support/ClaudeCode/managed-settings.json" if sys.platform == "darwin"
            else "/etc/claude-code/managed-settings.json")
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as f:
                managed = json.load(f)
            if not isinstance(managed, dict):
                managed = None
    except Exception:
        managed = None
    if st is None and managed is not None and not os.path.exists(os.path.join(home, ".claude", "settings.json")):
        problems.clear()  # no user settings file at all, but the managed file is there: not a problem
    mark("settings readable")

    def runs(event, script, need_compact, word="compact"):
        for src in (st, managed):
            try:
                for g in (src.get("hooks") or {}).get(event) or []:
                    if need_compact and word not in str(g.get("matcher") or ""):
                        continue
                    for h in g.get("hooks") or []:
                        if script in str(h.get("command") or ""):
                            return True
            except Exception:
                pass
        return False
    if st is not None or managed is not None:
        if not runs("PreCompact", "precompact-handoff.py", False):
            problems.append("no PreCompact hook runs precompact-handoff.py")
        mark("PreCompact hook wired")
        if not runs("SessionStart", "postcompact-inject.py", True):
            problems.append("no SessionStart compact hook runs postcompact-inject.py")
        mark("SessionStart hook wired")
        if not runs("SessionStart", "postcompact-inject.py", True, "clear"):  # F88.e
            problems.append("no SessionStart clear hook runs postcompact-inject.py")
        mark("SessionStart clear hook wired")
    pre, post = os.path.join(hooks_dir, "precompact-handoff.py"), os.path.join(hooks_dir, "postcompact-inject.py")
    for fp in (pre, post):
        if not os.path.isfile(fp):
            problems.append(f"hook file missing: {os.path.basename(fp)}")
    mark("hook files present")
    if os.path.isfile(pre) and os.path.isfile(post):
        a, b = _vers(pre), _vers(post)
        if a != b:
            problems.append(f"version mismatch (precompact-handoff.py {a}, postcompact-inject.py {b})")
    mark("hook versions match")
    if not os.path.isfile(os.path.join(hooks_dir, "recall.py")):  # F86.a
        problems.append("recall.py missing")
    mark("recall.py present")
    if st is not None or managed is not None:  # F86.a: the allow rule that lets Claude run recall without a prompt
        def _allows(src):
            try:
                return RECALL_ALLOW_RULE in ((src.get("permissions") or {}).get("allow") or [])
            except Exception:
                return False
        if not (_allows(st) or _allows(managed)):
            problems.append(f"allow rule {RECALL_ALLOW_RULE} missing")
        mark("recall allow rule")
    for _sd in (HANDOFFS_DIR, os.path.join(HANDOFFS_DIR, "projects")):  # F87.c: chat text is stored here
        try:
            if os.path.isdir(_sd) and os.stat(_sd).st_mode & 0o077:
                problems.append(f"state folder is open to other users: {_sd}")
        except OSError:
            pass
    mark("state folders private")
    try:
        mp = os.path.join(hooks_dir, ".handoff-manifest.json")
        if os.path.isfile(mp):
            with open(mp, encoding="utf-8") as f:
                files = (json.load(f) or {}).get("files") or {}
            for name, want in sorted(files.items()):
                fp = os.path.join(hooks_dir, name)
                if not os.path.isfile(fp):
                    if not any(name in x for x in problems):
                        problems.append(f"hook file missing: {name}")
                    continue
                with open(fp, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() != want:
                        problems.append(f"{name} differs from the installed manifest")
    except Exception as e:
        log_error(e)
    mark("files match the manifest")
    rp = os.path.join(HANDOFFS_DIR, "reconcile.json")
    try:
        rj = read_json(rp)
        if rj and rj.get("format_drift") is True:  # F63: shown on every start while the newest reconcile.json says so
            ds = rj.get("format_drift_sessions") if isinstance(rj.get("format_drift_sessions"), list) else []
            sys.stdout.write(f"HANDOFF CHECK: format drift: {len(ds)} session(s) have post-compaction injects but no "
                             "compact_boundary record; Claude Code may have changed its transcript format; "
                             "see precompact-handoff.py --reconcile\n")
        if rj:
            mi, kl = rj.get("missed_interactive") or 0, rj.get("killed") or 0
            if (mi > 0 or kl > 0) and (verbose or str(rj.get("ts") or "") > str(rj.get("noticed_ts") or "")):
                data.append(f"{mi if mi > 0 else kl} compactions without a handoff in the last "
                            f"{rj.get('days', 7)} days; see precompact-handoff.py --reconcile")
                if not verbose:
                    rj["noticed_ts"] = utc_now()
                    tmp = f"{rp}.tmp{os.getpid()}"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(rj, f)
                    os.replace(tmp, rp)
    except Exception as e:
        log_error(e)
    mark("no compaction without a handoff")
    try:  # F47.c: a proof run with failures, shown once
        prows = []
        with open(os.path.join(HANDOFFS_DIR, "proofs.jsonl"), encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if isinstance(o, dict) and isinstance(o.get("ts"), str):
                    prows.append(o)
        if prows:
            newest = max(r["ts"] for r in prows)
            pnp = os.path.join(HANDOFFS_DIR, "proof-notice.json")
            pn = read_json(pnp) or {}
            if verbose or newest > str(pn.get("noticed_ts") or ""):
                nf = sum(1 for r in prows if r["ts"] == newest for v in (r.get("checks") or {}).values() if v == "fail")
                if nf > 0:
                    data.append(f"proof failed: {nf} checks; see proof-report.md")
                if not verbose:
                    write_json_atomic(pnp, {"noticed_ts": newest})
    except FileNotFoundError:
        pass
    except Exception as e:
        log_error(e)
    mark("latest proof run has no failed check")
    try:  # F48.d: failing or stale claims, shown once per change of the set
        cs = read_json(os.path.join(HANDOFFS_DIR, "claims-status.json"))
        if cs:
            bad = sorted(c.get("id") for c in (cs.get("claims") or []) if isinstance(c, dict) and c.get("status") in ("fail", "stale"))
            cnp = os.path.join(HANDOFFS_DIR, "claims-notice.json")
            cn = read_json(cnp) or {}
            if verbose or bad != (cn.get("set") if isinstance(cn.get("set"), list) else []):
                if bad:
                    data.append(f"claims: {len(bad)} failing or stale; see claims-report.md")
                if not verbose:
                    write_json_atomic(cnp, {"set": bad})
    except Exception as e:
        log_error(e)
    mark("no failing or stale claim")
    dstat = decisions_stats(ledger_path_for(os.getcwd()))  # F88.d
    if dstat:
        if dstat["pct"] >= 90:
            data.append(f"decisions list: rules use {dstat['pct']}% of the room; retire or merge rules "
                        "(recall.py decisions --kind rule)")
        if dstat["index_only"]:
            data.append(f"decisions list: {dstat['index_only']} rules are index lines only")
    mark("decisions list has room")
    bad_lines = []  # F90.c: malformed lines of the decisions list of this project (a data problem, F86.a)
    try:
        lp = ledger_path_for(os.getcwd())
        if os.path.isfile(lp):
            with open(lp, encoding="utf-8", errors="replace") as f:
                bad_lines = grammar().malformed_decisions(f.read())
    except Exception as e:
        log_error(e)
    if bad_lines:
        data.append(f"decisions list: {len(bad_lines)} malformed lines (first: line {bad_lines[0][0]}); "
                    "fix them to a known form (docs/decisions-list.md)")
    mark("decisions list lines are in a known form")
    inst_t, data_t = list(problems), list(data)
    _CLEAN_ST[0], _CLEAN_ST[1] = st, managed
    if verbose:
        cleanup_line()  # F52.b, before the last line
    if verbose:  # F86.b
        for name, texts in checks:
            sys.stdout.write(("problem: " + "; ".join(texts) if texts else "ok: " + name) + "\n")
        sys.stdout.write("info: recall rule %s\n" % ("on" if recall_rules_on() else "off"))
        if dstat:  # F88.d
            sys.stdout.write(f"info: decisions list: {dstat['rules']} rules, {dstat['chars']} of {dstat['room']} "
                             f"characters of full-text room ({dstat['pct']}%); {dstat['active']} active entries; "
                             f"{dstat['unlisted']} not listed\n")
        if bad_lines:  # F90.c: the first 3, cut to 120 characters, after redact()
            try:
                import importlib.util
                _s = importlib.util.spec_from_file_location("lh_recall_redact", recall_path())
                _m = importlib.util.module_from_spec(_s)
                _s.loader.exec_module(_m)
                _red = _m.redact
            except Exception:
                _red = None
            for n, raw in bad_lines[:3]:
                shown = _red(raw.strip())[:120] if _red else "(text hidden: recall.py did not load)"
                sys.stdout.write(f"info: malformed line {n}: {shown}\n")
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("lh_recall_check",os.path.join(hooks_dir, "recall.py"))
            rm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(rm)
            root = rm.project_root(os.getcwd())
            sys.stdout.write("info: recall covers %d sessions for %s\n" % (len(rm.in_scope_transcripts(root, True)), root))
        except Exception:
            sys.stdout.write("info: recall covers unknown sessions (recall.py did not load)\n")
        import shutil
        sys.stdout.write("info: claude on PATH: %s\n" % ("yes" if shutil.which("claude") else "no"))
    line = None
    if inst_t and data_t:  # F86.a
        line = ("HANDOFF CHECK: " + "; ".join(inst_t) + ". Run the installer again, then restart Claude Code. Also: "
                + "; ".join(data_t) + ".")
    elif inst_t:
        line = "HANDOFF CHECK: " + "; ".join(inst_t) + ". Run the installer again, then restart Claude Code."
    elif data_t:
        line = "HANDOFF CHECK: " + "; ".join(data_t) + "."
    if line:
        sys.stdout.write(line + "\n")
    elif verbose:
        sys.stdout.write("HANDOFF CHECK: all ok (%d checks)\n" % len(checks))
    if not verbose:
        cleanup_line()


def cleanup_line():
    try:  # F52.b: Claude Code deletes old transcripts; recall needs them
        days = effective_cleanup_days(_CLEAN_ST[0], _CLEAN_ST[1])
        if days < 90:
            sys.stdout.write(f"HANDOFF CHECK: Claude Code deletes chat transcripts after {days} days (cleanupPeriodDays), "
                             "so recall loses older messages. Set \"cleanupPeriodDays\": 365 in ~/.claude/settings.json, "
                             "or run ./install.sh --keep-transcripts.\n")
    except Exception as e:
        log_error(e)


_CLEAN_ST = [None, None]


if "--check" in sys.argv[1:]:
    try:
        check_mode("--verbose" in sys.argv[1:])
    except BaseException as _e:
        if not isinstance(_e, (SystemExit, KeyboardInterrupt)):
            log_error(_e)
    sys.exit(0)

if "--subagent-start" in sys.argv[1:]:
    try:
        subagent_start_mode()
    except BaseException as _e:
        if not isinstance(_e, (SystemExit, KeyboardInterrupt)):
            log_error(_e)
    sys.exit(0)

if "--rules-block" in sys.argv[1:]:
    try:
        rules_block_mode()
    except BaseException as _e:
        if not isinstance(_e, (SystemExit, KeyboardInterrupt)):
            log_error(_e)
    sys.exit(0)

if "--ledger" in sys.argv[1:]:
    if os.environ.get("HANDOFF_LEDGER") == "0":  # AC 100: ledger disabled, print nothing
        sys.exit(0)
    try:
        ledger_mode(PART)  # plain --ledger = --part 1 (F42.3)
    except BaseException as _e:
        log_error(_e)
    sys.exit(0)

try:
    d = json.load(sys.stdin)
    sid = d.get("session_id")
    sid = sid.replace("/", "_") if isinstance(sid, str) else ""  # same rule as precompact-handoff.py; non-string = silent
    _aid = d.get("agent_id")
    if sid and isinstance(_aid, str) and _aid.strip():  # F46.c: a subagent reads and writes only its own <sid>-a-<id> files
        sid = f"{sid}-a-{_aid.strip().replace('/', '_')}"
    _SID[0] = sid or "unknown"
    if sid:  # every part syncs (cheap, locked) so that all parts cut the same text
        _NODE_LINE[0] = sync_nodes(d)
    if sid:
        p = os.path.join(HANDOFFS_DIR, f"{sid}-latest.md")
        root = os.path.realpath(HANDOFFS_DIR)
        txt = ""
        if os.path.realpath(p).startswith(root + os.sep) and os.path.isfile(p):
            try:
                txt = open(p, encoding="utf-8", errors="replace").read().strip()
            except Exception:
                txt = ""
        txt = decorate(txt)
        plain = PREFIX + txt
        try:
            S = read_json(os.path.join(HANDOFFS_DIR, f"{sid}-last-health.json"))
            trig = S.get("trigger") if S else "unknown"
            # F45.b: parts run in parallel, so parts 2 and 3 decide from the state at their start
            if S and S.get("trigger") == "recovered" and age_ok_s(S.get("ts"), 60) and PART in (2, 3):
                raise SystemExit(0)
            # F87.d: a handoff that a receipt shows as already served is handled as a record older than 15 minutes
            used = bool(txt and S is not None and handoff_used(sid, S.get("ts")))
            need = not txt or S is None or not isinstance(S.get("ts"), str) or not age_ok(S["ts"]) or used
            precomputed = False
            if need and not used and txt and S is not None and isinstance(S.get("ts"), str) and belongs_to_compaction(d, S["ts"]):
                need, precomputed = False, True  # F51: prepared at T0, applied later; the kept messages cover T0..now
                _NOTE[0] = ("Note: this compaction was prepared at " + S["ts"][:10] + " " + S["ts"][11:19]
                            + "Z; the messages after that time are kept word for word after the summary.\n")
            if need and PART in (2, 3):
                raise SystemExit(0)
            if need:
                rec = try_recover(d, sid)
                if rec is not None:
                    emit(rec)
                    write_receipt(d, sid, "recovered", rec)
                    raise SystemExit(0)
            if not txt:
                write_degraded(sid, ["handoff_missing"], trig)
                emit("HANDOFF DEGRADED: no handoff file for this session. "
                     "Read the transcript (see session metadata) before acting.")
            else:
                codes, errs = [], []
                if S is None or not isinstance(S.get("ts"), str):
                    codes = ["no_health"]
                else:
                    if S.get("degraded") is True:
                        codes = [c for c in (S.get("reasons") or []) if c in CODE_ORDER]
                        errs = S.get("errors") or []
                        if not codes:
                            codes = ["no_health"]
                    if not precomputed and (used or not age_ok(S["ts"])):
                        codes.append("handoff_old")
                first = ("HANDOFF DEGRADED: " + warning_text(codes, errs)
                         + ". Verify the handoff against the transcript before acting.\n") if codes else ""
                if PART == 1 and codes:  # the warning and the degraded file belong to part 1
                    write_degraded(sid, codes, trig)
                outs = render(first, txt, os.path.abspath(p))
                if len(outs) >= PART:
                    emit(outs[PART - 1])
                    if PART == 1:
                        write_receipt(d, sid, "normal", outs[0])
        except SystemExit:
            raise
        except BaseException:
            if txt and PART == 1:
                emit(plain)  # last resort: section 6 behaviour (uncut)
except BaseException as _e:
    if not isinstance(_e, (SystemExit, KeyboardInterrupt)):
        log_error(_e)
sys.exit(0)
