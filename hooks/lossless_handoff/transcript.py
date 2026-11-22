# Settings, transcript parsing and the git facts.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

HOME = os.path.expanduser("~")


def _hook_candidates(name, first, second):
    """F72: with HANDOFF_HOOKS_HOME naming an existing folder, only that folder (no fallback, no mixing of two trees);
    else (first, second) as before."""
    h = (os.environ.get("HANDOFF_HOOKS_HOME") or "").strip()
    return [os.path.join(h, name)] if h and os.path.isdir(h) else [first, second]

HANDOFFS_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")
HEALTH_FILE = os.path.join(HANDOFFS_DIR, "health.jsonl")
HEALTH_MAX_LINES = 5000
SECTION_NAMES = ["warning", "agent", "fallback", "user_messages", "decisions", "files", "tasks",
                 "open_tasks", "background_agents", "git", "bash", "assistant"]
FAULTS = {x.strip() for x in (os.environ.get("HANDOFF_TEST_FAULT") or "").split(",") if x.strip()}
CAP = 14000  # F39.6 (was 20,000, F37.1): file cap. The inject hook inlines at most ~9,700 chars per part.
INJECT_PREFIX = "Pre-compaction handoff (auto-saved):\n"  # same literal as postcompact-inject.py


def trunc(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def genuine(t):
    t = t.strip()
    if not t:
        return False
    for p in ("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<command-name",
              "<local-command", "<command-message", "Caveat:",
              "<artifact-content-authored-by-others", "<bash-input>", "<bash-stdout>", "<bash-stderr>",  # F40.3
              "This session is being continued"):
        if t.startswith(p):
            return False
    return True


def ts_ms(v):
    """ISO-8601 string -> epoch ms, or None."""
    try:
        if not isinstance(v, str) or not v:
            return None
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def one_line(v, n):
    return trunc(redact(str(v if v is not None else "")).replace("\r", " ").replace("\n", " "), n)  # F65: mask before the lines are joined


QA_RE = re.compile(r'"([^"]*)"="([^"]*)"')


def is_boundary(o):
    return isinstance(o, dict) and (o.get("subtype") == "compact_boundary" or o.get("type") == "compact_boundary")


def parse(path, before_last_boundary=False):
    """Single pass over the transcript. Returns a dict; never raises. F45.b: with before_last_boundary only the
    records before the last compact_boundary are read (what was said before the compaction)."""
    P = {"users": [], "asst": "", "files": [], "tasks": {}, "cmds": [], "todos": None,
         "qa": [], "bg": [], "user_events": [], "tool_events": [], "last_user": None}
    n_created = 0
    asks = {}  # tool_use_id -> list of (header, question)
    bgs = {}   # tool_use_id -> bg record
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except Exception:
        return P
    with fh:
        src = fh
        if before_last_boundary:
            src = fh.readlines()
            cut = -1
            for i in range(len(src) - 1, -1, -1):
                if "compact_boundary" in src[i]:
                    try:
                        if is_boundary(json.loads(src[i])):
                            cut = i
                            break
                    except Exception:
                        pass
            if cut >= 0:
                src = src[:cut]
        for line in src:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            if "<task-id>" in line and re.search(r"completed|failed|killed", line):
                for r in P["bg"]:
                    if not r["done"] and r["id"] and f"<task-id>{r['id']}</task-id>" in line:
                        r["done"] = True
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            t = o.get("type")
            ts = ts_ms(o.get("timestamp"))
            try:
                if t == "user":
                    c = msg.get("content")
                    tx_all = text_of(c).strip() if not isinstance(c, str) else c.strip()
                    if tx_all.startswith("<task-notification"):
                        for r in P["bg"]:
                            if r["done"]:
                                continue
                            if (r["id"] and r["id"] in tx_all) or (not r["id"] and r["desc"] and r["desc"] in tx_all):
                                r["done"] = True
                    if o.get("isMeta"):
                        continue
                    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        tur = o.get("toolUseResult")
                        for b in c:
                            if not isinstance(b, dict) or b.get("type") != "tool_result":
                                continue
                            tid = b.get("tool_use_id")
                            if tid in asks:
                                extract_answers(P, asks.pop(tid), b, tur)
                            if tid in bgs:
                                r = bgs.pop(tid)
                                aid = tur.get("agentId") if isinstance(tur, dict) else None
                                if not isinstance(aid, str) or not aid:
                                    m = re.search(r"agent_?[iI]d:\s*(\S+)", result_text(b.get("content")))
                                    aid = m.group(1).strip(".,;") if m else None
                                r["id"] = aid
                        continue
                    tx = text_of(c)
                    if genuine(tx):
                        P["users"].append(tx.strip())
                        if ts is not None:
                            P["last_user"] = ts
                            P["user_events"].append(ts)
                elif t == "assistant":
                    c = msg.get("content")
                    tx = text_of(c).strip()
                    if tx:
                        P["asst"] = tx
                    if not isinstance(c, list):
                        continue
                    for b in c:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        if ts is not None:
                            P["tool_events"].append(ts)
                        name, inp = b.get("name", ""), b.get("input") or {}
                        if not isinstance(inp, dict):
                            continue
                        files, tasks = P["files"], P["tasks"]
                        if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if fp:
                                if fp in files:
                                    files.remove(fp)
                                files.append(fp)
                        elif name == "Bash" and inp.get("command"):
                            P["cmds"].append(inp["command"])
                        elif name == "TaskCreate":
                            n_created += 1
                            tasks[str(n_created)] = {"subject": inp.get("subject", ""), "status": "pending"}
                        elif name == "TaskUpdate":
                            tid = str(inp.get("taskId", ""))
                            d = tasks.setdefault(tid, {"subject": "", "status": "pending"})
                            if inp.get("subject"):
                                d["subject"] = inp["subject"]
                            if inp.get("status"):
                                d["status"] = inp["status"]
                        elif name == "TodoWrite":
                            td = inp.get("todos")
                            if isinstance(td, list):
                                P["todos"] = [x for x in td if isinstance(x, dict)]
                        elif name == "AskUserQuestion":
                            qs = inp.get("questions")
                            if isinstance(qs, list) and isinstance(b.get("id"), str):
                                asks[b["id"]] = [(q.get("header"), q.get("question")) for q in qs
                                                 if isinstance(q, dict) and isinstance(q.get("question"), str)]
                        elif (name == "Agent" or (name == "Task" and inp.get("subagent_type"))) \
                                and inp.get("run_in_background") is True:
                            r = {"desc": str(inp.get("description") or ""), "model": inp.get("model"),
                                 "id": None, "done": False}
                            P["bg"].append(r)
                            if isinstance(b.get("id"), str):
                                bgs[b["id"]] = r
            except Exception:
                continue
    return P


def answer_pairs(qs, block, tur):
    """Pair the questions of one AskUserQuestion call with their answers: [(header, question, answer, note)]."""
    out = []
    if block.get("is_error") is True:
        return out
    answers, ann = None, {}
    if isinstance(tur, dict):
        if isinstance(tur.get("answers"), dict):
            answers = tur["answers"]
        if isinstance(tur.get("annotations"), dict):
            ann = tur["annotations"]
    if answers is None:
        answers = dict(QA_RE.findall(result_text(block.get("content"))))
    for header, q in qs:
        a = answers.get(q)
        if isinstance(a, list):
            a = ", ".join(str(x) for x in a)
        if a is None or not isinstance(a, (str, int, float)) or str(a).strip() == "":
            continue
        note = ann.get(q).get("notes") if isinstance(ann.get(q), dict) else None
        out.append((header, q, a, note))
    return out


def extract_answers(P, qs, block, tur):
    for header, q, a, note in answer_pairs(qs, block, tur):
        line = "- " + (f"[{one_line(header, 200)}] " if isinstance(header, str) and header.strip() else "")
        line += f"{one_line(q, 200)} -> {one_line(a, 200)}"
        if isinstance(note, str) and note.strip():
            line += f" (notes: {one_line(note, 200)})"
        P["qa"].append(line)


def git(cwd):
    try:
        if not cwd or not os.path.isdir(cwd):
            return None
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return None
        b = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        s = subprocess.run(["git", "-C", cwd, "status", "--short"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return b, s
    except Exception:
        return None
