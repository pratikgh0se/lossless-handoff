"""Shared code of history-guard.py (F77) and action-guard.py (F80). Not a hook: both hooks load it by path from the
folder of the hook file. It holds the subject, evidence, cooldown and state rules, so the two hooks do not drift.
Stdlib only. Nothing here prints; the hooks print."""
import hashlib, json, os, re, time

HOOK_VERSION = "1.20.0"
HOME = os.path.expanduser("~")
BUDGET_S = 3.0
MAX_ANCHORS, MAX_SUBJECTS = 60, 4
LO, HI = 2, 40
PER_SUBJECT_CAP, TOTAL_CAP = 1500, 6000
COOLDOWN_TOKENS = 50000
TAILS = (65536, 400000)
NOT_USER = ("<task-notification", "<bash-input>", "<bash-stdout>", "<local-command-", "<command-name", "[SYSTEM", "Caveat:",
            "Lossless Handoff ")
REM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
TOKEN_RE = re.compile(r"`([^`\s]+)`|([A-Za-z0-9_./-]+)")
FULL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*[_./-][A-Za-z0-9_./-]*[A-Za-z0-9]")
SENT = re.compile(r"(?<=[.?!])\s+|\n+")


def state_dir():
    return (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")


def hooks_home():
    """F72: HANDOFF_HOOKS_HOME when it names an existing folder, else None."""
    v = (os.environ.get("HANDOFF_HOOKS_HOME") or "").strip()
    return v if v and os.path.isdir(v) else None


def recall_candidates(hook_file):
    h = hooks_home()
    if h:
        return [os.path.join(h, "recall.py")]
    return [os.path.join(HOME, ".claude", "hooks", "recall.py"), os.path.join(os.path.dirname(os.path.abspath(hook_file)), "recall.py")]


_AR = []


def _auto_recall():
    """The module auto-recall.py: the one place of the identifier rule (F75 and its amendments). Looked up like the
    other parts (F72): the override folder only, or ~/.claude/hooks, or the folder of this file. None if it is absent."""
    if _AR:
        return _AR[0]
    mod = None
    h = hooks_home()
    cands = [os.path.join(h, "auto-recall.py")] if h else [os.path.join(HOME, ".claude", "hooks", "auto-recall.py"),
                                                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto-recall.py")]
    for p in cands:
        if os.path.isfile(p):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("lh_auto_recall", p)
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                if callable(getattr(m, "identifiers", None)):
                    mod = m
            except Exception:
                mod = None
            break
    _AR.append(mod)
    return mod


# Fallback only, for an install that has no auto-recall.py. tests/smoke.sh checks that it gives the same result as
# auto-recall.identifiers on a corpus, so the two cannot drift.
CUES = ["what did i say", "what did you say", "what did we decide", "did we decide", "did we agree", "did i say", "did i tell you",
        "we decided", "we agreed", "i told you", "i said", "you said", "you mentioned", "i mentioned", "last time", "previously",
        "as we discussed", "as discussed", "which one did", "earlier you", "earlier i", "earlier we", "said earlier", "said before",
        "as before", "like before", "same as before", "do you remember", "remember when", "remember what"]
STOP = {"previous", "replaced", "rejected", "proposal", "remember", "current", "earlier", "value", "values", "answer",
        "answers", "setting", "settings", "before", "which", "there", "their", "about", "would", "could", "should"}
HYPHEN_WORD_RE = re.compile(r"[a-z]+(?:-[a-z]+)+")
IDENT_TOKEN_RE = TOKEN_RE
IDENT_FULL_RE = FULL_RE
PLAIN_WORD_RE = re.compile(r"[a-z]{1,8}")
NOT_IDENT = {"e.g", "i.e", "and/or"}


def _identifiers_fallback(prompt):
    rest = URL_RE.sub(" ", prompt)
    seen, groups = set(), ([], [], [], [])
    for m in IDENT_TOKEN_RE.finditer(rest):
        if m.group(1) is not None:
            tok, ticks = m.group(1).strip(), True
        else:
            tok, ticks = m.group(2).strip("._/-"), False
            if not IDENT_FULL_RE.fullmatch(tok):
                continue
        k = tok.lower()
        if len(tok) < 1 or k in seen or k in NOT_IDENT or k in CUES:
            continue
        form = bool(IDENT_FULL_RE.fullmatch(tok)) and any(c.isalpha() for c in tok)
        if form and ticks:
            gi = 0
        elif form:
            gi = 2 if HYPHEN_WORD_RE.fullmatch(tok) else 1
        elif ticks and k not in STOP and not (PLAIN_WORD_RE.fullmatch(tok) and not any(c.isdigit() for c in tok)):
            gi = 3
        else:
            continue
        seen.add(k)
        groups[gi].append(tok)
    return [t for grp in groups for t in grp]


def identifiers(text):
    """The identifier rule: auto-recall.identifiers (F75 with its amendments), loaded by path; the fallback copy only if
    auto-recall.py is not installed."""
    m = _auto_recall()
    return m.identifiers(text) if m is not None else _identifiers_fallback(text)


def threshold(env, default=100000):
    s = os.environ.get(env, "").strip()
    if s.isdigit() and 10000 <= int(s) <= 1000000:
        return int(s)
    return default


def context_tokens(path):
    """Same rule as reanchor.py: usage of the last assistant entry with a total above 0."""
    size = os.path.getsize(path)
    for n in TAILS:
        with open(path, "rb") as f:
            f.seek(max(0, size - n))
            data = f.read().decode("utf-8", "replace")
        for line in reversed(data.splitlines()):
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("type") != "assistant":
                continue
            u = (e.get("message") or {}).get("usage")
            if not isinstance(u, dict):
                continue
            t = 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                v = u.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    t += int(v)
            if t == 0:
                continue
            return t
        if size <= n:
            break
    return None


def name_tokens(text):
    """Every name-like token of a tool input or result, lower case: the whole-token rule of the history search, but
    without the identifier form (a plain word such as Zeta, in a result, still excludes the anchor Zeta)."""
    out = set()
    for m in TOKEN_RE.finditer(URL_RE.sub(" ", text)):
        t = (m.group(1) if m.group(1) is not None else m.group(2)).strip("._/-` ").lower()
        if t:
            out.add(t)
    return out


def strings(o, depth=0):
    if depth > 6:
        return
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v, depth + 1)


def read_session(path, deadline, prompt=None, include_turn=False):
    """-> (messages, turn), or None on a timeout. messages = [(role, text)]: the text messages before the current user
    prompt (no tool input, no tool result); with `include_turn` also the current prompt and the messages of the current
    turn (Stop and PostToolBatch callers: the turn in progress is part of the chat). With `prompt` (a UserPromptSubmit caller): the whole chat, minus the current
    prompt when the transcript holds it already. turn = dict(uuid, tool: identifiers of tool inputs in this turn, calls:
    key_history / recall.py history calls in this turn, strs: other strings of this turn)."""
    msgs, pending = [], []
    turn = {"uuid": "", "tool": set(), "calls": [], "strs": []}
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i % 500 == 0 and time.monotonic() > deadline:
                return None
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
                continue
            typ = e.get("type")
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            c = m.get("content")
            if typ == "user":
                tools, user_text = [], None
                if isinstance(c, str):
                    user_text = REM.sub("", c).strip()
                elif isinstance(c, list):
                    tools = [b for b in c if isinstance(b, dict) and b.get("type") == "tool_result"]
                    if not tools:
                        user_text = REM.sub("", "\n".join(b.get("text", "") for b in c if isinstance(b, dict)
                                                           and b.get("type") == "text" and isinstance(b.get("text"), str))).strip()
                if tools:
                    for b in tools:
                        for s in strings(b.get("content")):
                            turn["strs"].append(s)
                            turn["tool"].update(name_tokens(s))  # a tool result excludes an anchor
                    continue
                if user_text and not user_text.startswith(NOT_USER):
                    msgs.extend(pending)  # the previous turn is complete: its messages count
                    pending = [("user", user_text)]
                    turn = {"uuid": e.get("uuid") if isinstance(e.get("uuid"), str) else "", "tool": set(), "calls": [], "strs": []}
            elif typ == "assistant" and isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                        pending.append(("assistant", b["text"]))
                    elif b.get("type") == "tool_use":
                        inp = b.get("input")
                        try:
                            s = json.dumps(inp, ensure_ascii=False)
                        except Exception:
                            s = str(inp)
                        turn["tool"].update(name_tokens(s))
                        name = b.get("name") if isinstance(b.get("name"), str) else ""
                        cmd = str(inp.get("command")) if isinstance(inp, dict) else ""
                        if name.startswith("mcp__lossless-recall__") and name.endswith("key_history"):
                            turn["calls"].append(s.lower())
                        elif name == "Bash" and "recall.py" in cmd and "history" in cmd:
                            turn["calls"].append(cmd.lower())
            elif typ not in ("user", "assistant"):
                turn["strs"].extend(strings(e))  # hook attachments: an injected history block may sit here
    if include_turn or (prompt is not None and not (pending and pending[0] == ("user", prompt.strip()))):
        msgs.extend(pending)  # the transcript does not hold the current prompt yet: the last turn is complete
    return msgs, turn  # else the messages of the turn in progress are not counted


def build_index(msgs):
    idx, sent = {}, {}
    for n, (role, text) in enumerate(msgs):
        for s in SENT.split(text):
            ids = {t.lower() for t in identifiers(s)}
            for t in ids:
                if not idx.get(t) or idx[t][-1][0] != n:
                    idx.setdefault(t, []).append((n, role))
                sent.setdefault(t, set()).update(ids - {t})
    return idx, sent


def is_subject(idx, t):
    m = idx.get(t, [])
    return LO <= len(m) <= HI and sum(1 for _n, role in m if role == "user") >= 2


def pick_subjects(anchors, cwd, idx, sent, turn, st, tk, skip_tool_ids):
    """-> (subjects in anchor order, at most 4, with their written form). Conditions 3 to 6 of F77: an anchor that is
    not an existing path (and, if skip_tool_ids, not a name from a tool input or result of this turn) gives a subject, or
    the subjects in the sentences that held an earlier mention of it; no history evidence in this turn; cooldown."""
    subs, written = [], {}
    for a in anchors:
        al = a.lower()
        if (skip_tool_ids and al in turn["tool"]) or os.path.exists(os.path.join(cwd, a)):
            continue
        if is_subject(idx, al):
            cand = [al]
        elif idx.get(al):
            cand = [k for k in sorted(sent.get(al, ())) if is_subject(idx, k) and not (skip_tool_ids and k in turn["tool"])]
        else:
            cand = []
        for k in cand:
            if k not in subs:
                subs.append(k)
                written[k] = a if al == k else k
    shown = st.get("shown") if isinstance(st.get("shown"), dict) else {}
    keep = []
    for k in subs:
        if any(k in c for c in turn["calls"]) or any(("history of %s:" % k) in s.lower() for s in turn["strs"]):
            continue  # evidence in this turn
        old = shown.get(key_hash(k))
        if isinstance(old, dict) and old.get("n") == len(idx[k]) and tk - int(old.get("tokens") or 0) < COOLDOWN_TOKENS:
            continue  # cooldown: shown, no new mention, the context did not grow by 50,000
        keep.append(k)
    keep = keep[:MAX_SUBJECTS]
    return keep, {k: written[k] for k in keep}


def key_hash(k):
    """The state file holds a hash of a key, not its text."""
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16]


def state_path(sid):
    return os.path.join(state_dir(), "history-guard-%s.json" % sid)


def read_state(sid):
    try:
        with open(state_path(sid), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_shown(sid, keep, idx, tk, turn_uuid=None):
    """Update the shared cooldown table (and the F77 turn marker when `turn_uuid` is given). Mode 0600."""
    st = read_state(sid)  # read again: the other guard may have written since
    shown = st.get("shown") if isinstance(st.get("shown"), dict) else {}
    for k in keep:
        shown[key_hash(k)] = {"n": len(idx[k]), "tokens": tk}
    st["shown"] = shown
    if turn_uuid is not None:
        st["turn"] = turn_uuid
    os.makedirs(state_dir(), exist_ok=True)
    path = state_path(sid)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def log_row(name, row):
    os.makedirs(state_dir(), exist_ok=True)
    fd = os.open(os.path.join(state_dir(), name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def load_recall(hook_file):
    for p in recall_candidates(hook_file):
        if os.path.isfile(p):
            import importlib.util
            spec = importlib.util.spec_from_file_location("lh_recall", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for n in ("history_for", "current_cutoff", "in_scope_transcripts", "project_root"):
                if not hasattr(mod, n):
                    return None
            return mod
    return None


def narrow_scope(mod, root, transcript_path):
    """Speed only: Claude Code names a project folder after the working directory (non-letters and digits become -).
    List just the folders whose name starts with the name of the project root, so the scope code of recall.py does not
    glob thousands of transcripts of other projects. The scope code still decides by the cwd inside each transcript.
    If no folder matches, nothing is narrowed."""
    import glob as _glob
    from types import SimpleNamespace
    try:
        enc = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))
        dirs = {d for d in os.listdir(mod.PROJECTS_DIR) if d.startswith(enc)}
        if isinstance(transcript_path, str) and transcript_path:
            dirs.add(os.path.basename(os.path.dirname(os.path.realpath(transcript_path))))
        dirs = {d for d in dirs if os.path.isdir(os.path.join(mod.PROJECTS_DIR, d))}
        if not dirs:
            return
        star = os.path.join(mod.PROJECTS_DIR, "*") + os.sep

        def narrow(pattern, *a, **k):
            if not pattern.startswith(star):
                return _glob.glob(pattern, *a, **k)
            out = []
            for d in sorted(dirs):
                out += _glob.glob(os.path.join(_glob.escape(mod.PROJECTS_DIR), _glob.escape(d), pattern[len(star):]), *a, **k)
            return out
        mod.glob = SimpleNamespace(glob=narrow)
    except Exception:
        pass


def history_text(header, names, cwd, tp, hook_file):
    """-> header line and the F73 block of each subject (1,500 characters each, 6,000 in total), or ''."""
    mod = load_recall(hook_file)
    if mod is None:
        return ""
    root = mod.project_root(cwd)
    narrow_scope(mod, root, tp)
    ts = mod.in_scope_transcripts(root, True)
    cutoff = {}  # the current turn is part of the history: nothing is cut
    blocks, total = [], len(header) + 1
    for name in names:
        lines, n = mod.history_for(ts, name, "any", cutoff, PER_SUBJECT_CAP)
        if n == 0:
            continue
        b = "\n".join(lines)
        if total + len(b) + 1 > TOTAL_CAP:
            break
        blocks.append(b)
        total += len(b) + 1
    return (header + "\n" + "\n".join(blocks)) if blocks else ""
