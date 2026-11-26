#!/usr/bin/env python3
"""UserPromptSubmit hook (F67, opt-in): search earlier chats when the prompt refers to the past.

Reads the hook JSON on stdin (prompt, transcript_path, cwd, session_id). It fires only when the prompt does not
start with "/" and holds a past-reference cue (or a double-quoted phrase of 3 or more words). It then runs the
recall.py search code (word, or word + meaning when the meaning index exists) over this project's transcripts and
prints the top 3 hits as additional context: one line per hit, secrets masked by redact().
Time budget: 2.0 seconds for the whole hook. Never fails a prompt: every error means no output and exit 0.
Off switch: HANDOFF_AUTO_RECALL=0. The health log (auto-recall.jsonl) holds no prompt text and no snippets.
"""
import json, os, re, sys, threading, time

HOOK_VERSION = "1.25.0"
HOME = os.path.expanduser("~")


def hooks_home():
    """F72: HANDOFF_HOOKS_HOME when it names an existing folder, else None (then ~/.claude/hooks as before)."""
    v = (os.environ.get("HANDOFF_HOOKS_HOME") or "").strip()
    return v if v and os.path.isdir(v) else None


def hook_candidates(name, first, second):
    """F72: with the override, only that folder (no fallback, no mixing of two trees); else (first, second) as before."""
    h = hooks_home()
    return [os.path.join(h, name)] if h else [first, second]

STATE_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")
BUDGET_S = 2.0
WATCHDOG_S = 1.85
MEANING_TIMEOUT_S = 1.0
TOP_HITS = 3
SNIPPET_MAX = 300
MAX_WORDS = 8

CUES = ["what did i say", "what did you say", "what did we decide", "did we decide", "did we agree", "did i say", "did i tell you",
        "we decided", "we agreed", "i told you", "i said", "you said", "you mentioned", "i mentioned", "last time", "previously",
        "as we discussed", "as discussed", "which one did", "earlier you", "earlier i", "earlier we", "said earlier", "said before",
        "as before", "like before", "same as before", "do you remember", "remember when", "remember what"]
CUE_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(c) for c in sorted(CUES, key=len, reverse=True)), re.I)
QUOTE_RE = re.compile(r'"([^"\n]+)"')
WORD_RE = re.compile(r"[A-Za-z0-9_]{4,}")

HEADER = "Lossless Handoff auto-recall: earlier messages that match your words. Verify them before you rely on them."
FOOTER = "Open one in full: python3 ~/.claude/hooks/recall.py open <id>"

_state = {"lock": threading.Lock(), "done": False, "t0": time.monotonic(), "sid": "", "hits": 0, "logged": False, "mode": "search", "idents": 0, "cue": "quote"}


STOP = {"previous", "replaced", "rejected", "proposal", "remember", "current", "earlier", "value", "values", "answer",
        "answers", "setting", "settings", "before", "which", "there", "their", "about", "would", "could", "should"}
HIST_HEADER = ("Lossless Handoff auto-recall: the history of the identifiers in your prompt, oldest first. A later mention "
               "can be rejected, a dry run or undone. This can be partial; search for the rest.")
MAX_IDENTS, PER_IDENT_CAP, TOTAL_CAP = 3, 2000, 4000
HYPHEN_WORD_RE = re.compile(r"[a-z]+(?:-[a-z]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
IDENT_TOKEN_RE = re.compile(r"`([^`\s]+)`|([A-Za-z0-9_./-]+)")
IDENT_FULL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*[_./-][A-Za-z0-9_./-]*[A-Za-z0-9]")
NOT_IDENT = {"e.g", "i.e", "and/or"}  # abbreviations, not identifiers
PLAIN_WORD_RE = re.compile(r"[a-z]{1,8}")
BUDGET_IDENT_S = 1.2  # no new identifier after this many seconds


def identifiers(prompt):
    """F75 (amendment 2). Order: (1) backticked regex-form tokens; (2) other regex-form tokens that are not plain
    lowercase hyphen compounds; (3) plain lowercase hyphen compounds such as dry-run; (4) other backticked tokens.
    Prompt order inside a group. A regex-form token holds a letter. Not identifiers: a URL, a cue word, a stop word,
    `e.g`, `i.e`, `and/or`, and a backticked plain lowercase word of 8 or fewer letters with no digit."""
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
            g = 0
        elif form:
            g = 2 if HYPHEN_WORD_RE.fullmatch(tok) else 1
        elif ticks and k not in STOP and not (PLAIN_WORD_RE.fullmatch(tok) and not any(c.isdigit() for c in tok)):
            g = 3
        else:
            continue
        seen.add(k)
        groups[g].append(tok)
    return [t for grp in groups for t in grp]


def quoted_phrases(prompt):
    return [m.group(1).strip() for m in QUOTE_RE.finditer(prompt) if len(m.group(1).split()) >= 3]


def triggered(prompt):
    """F67.b: True when the prompt may refer to the past."""
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("/"):
        return False
    m = CUE_RE.search(prompt)
    if m:
        _state["cue"] = re.sub(r"\s+", " ", m.group(0).lower())  # a fixed list value, never prompt text
        return True
    _state["cue"] = "quote"
    return bool(quoted_phrases(prompt))


def query_words(prompt):
    """F67.c: the prompt without the cue phrases, then the 8 longest words of 4+ characters (None if fewer than 2)."""
    rest = CUE_RE.sub(" ", prompt)
    seen, words = set(), []
    for w in WORD_RE.findall(rest):
        k = w.lower()
        if k not in seen and k not in STOP:
            seen.add(k)
            words.append(w)
    words = sorted(words, key=lambda w: -len(w))[:MAX_WORDS]  # stable: ties keep prompt order
    return words if len(words) >= 2 else None


def write_health(hits, timed_out):
    """F67.d: one JSON line, no prompt text, no snippets. Never raises. Written once per run."""
    with _state["lock"]:
        if _state["logged"]:
            return
        _state["logged"] = True
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(STATE_DIR, "auto-recall.jsonl"), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fchmod(fd, 0o600)  # an existing log with looser rights is tightened
        except OSError:
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": str(_state["sid"])[:36],
                                "fired": True, "cue": _state["cue"], "mode": _state["mode"],
                                "identifiers": int(_state["idents"]), "hits": int(hits),
                                "ms": int((time.monotonic() - _state["t0"]) * 1000), "timed_out": bool(timed_out)}) + "\n")
    except BaseException:
        pass


def watchdog():
    with _state["lock"]:
        if _state["done"]:
            return
        _state["done"] = True  # from here the main thread prints nothing
    write_health(0, True)
    os._exit(0)  # a child process of the search (rg, git) can stay alive; it ends by itself


def load_recall():
    for p in hook_candidates("recall.py", os.path.join(HOME, ".claude", "hooks", "recall.py"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")):
        if os.path.isfile(p):
            import importlib.util
            spec = importlib.util.spec_from_file_location("lh_recall", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for n in ("redact", "in_scope_transcripts", "keyword_hits", "meaning_hits", "rrf_merge", "fill_meaning_snippets",
                      "project_root", "fmt_ts"):
                if not hasattr(mod, n):
                    return None  # a stub or an older recall.py: print nothing
            return mod
    return None


def collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm(t):
    """recall writes a newline as ' / '; undo that, collapse spaces, lower case."""
    return collapse((t or "").replace(" / ", " ")).lower()


def is_current_prompt(h, prompt, transcript_path):
    """Exclude the current prompt itself (Claude Code may already have written it to the transcript).
    Only a user hit in the current transcript file counts."""
    if h.get("role") != "user" or not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        if os.path.realpath(h.get("path") or "") != os.path.realpath(transcript_path):
            return False
    except Exception:
        return False
    p, s = norm(prompt), norm(h.get("snippet"))
    return bool(p) and bool(s) and (s in p or p[:80] in s)


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


def search(mod, prompt, words, cwd, sid, transcript_path=None):
    """-> list of hit dicts, best first (at most TOP_HITS), snippets redacted and cut."""
    from types import SimpleNamespace
    root = mod.project_root(cwd)
    narrow_scope(mod, root, transcript_path)
    ts = mod.in_scope_transcripts(root, True)
    if not ts:
        return []
    a = SimpleNamespace(subagents=True, role="any", pattern=" ".join(words), keyword=False, meaning=False)
    phrase_hits = []
    for ph in quoted_phrases(prompt):
        phrase_hits += [h for h in mod.keyword_hits(a, ts, re.compile(re.escape(ph), re.I), ph, True)
                        if h["role"] in ("user", "assistant")]
    alt = "|".join(re.escape(w) for w in words)
    # rg gets the plain alternation (a fast pre-filter); the match itself needs whole words
    word_rx = re.compile(r"\b(?:%s)\b" % alt, re.I)
    kw = [h for h in mod.keyword_hits(a, ts, word_rx, alt, False) if h["role"] in ("user", "assistant")][:200]
    one_rx = [re.compile(r"\b%s\b" % re.escape(w), re.I) for w in words]

    def score(h):
        s = h.get("snippet") or ""
        return sum(1 for r in one_rx if r.search(s))
    kw.sort(key=lambda h: (h["ts"], h["path"], h["line"]), reverse=True)
    kw.sort(key=score, reverse=True)  # stable: most distinct words first, then newest
    mh = []
    if os.path.exists(mod.index_paths(root)[1]):
        try:
            mod.QUERY_TIMEOUT = MEANING_TIMEOUT_S  # a cold or absent model must not eat the budget
            mh, _mode, _off = mod.meaning_hits(a, root, ts)
        except Exception:
            mh = []
    merged = mod.rrf_merge(kw, mh) if mh else kw
    seen, picked = set(), []
    for h in phrase_hits + merged:
        k = (h["path"], h["line"], h["role"])
        if k in seen or h.get("role") not in ("user", "assistant"):
            continue
        seen.add(k)
        picked.append(h)
    picked = [h for h in picked if not is_current_prompt(h, prompt, transcript_path)][:TOP_HITS * 3]
    # meaning-only hits carry no text yet; fill them for the few we may show
    mod.fill_meaning_snippets(picked[:TOP_HITS * 3], ts)
    picked = [h for h in picked if not is_current_prompt(h, prompt, transcript_path)][:TOP_HITS]
    for h in picked:
        h["snippet"] = collapse(mod.redact(collapse(h.get("snippet"))))[:SNIPPET_MAX]
        h["snippet"] = collapse(mod.redact(h["snippet"]))  # a cut can split a secret: mask again
    return picked


def recall_cmd():
    h = hooks_home()
    return (h.rstrip("/") + "/recall.py") if h else "~/.claude/hooks/recall.py"


def history_output(mod, prompt, idents, cwd, transcript_path):
    """F75: -> (text, number of mentions); ('', 0) when no identifier has a mention. Same scope and exclusions as
    `recall.py history`, with the current prompt and everything after it left out."""
    if not hasattr(mod, "history_for") or not hasattr(mod, "current_cutoff"):
        return "", 0
    root = mod.project_root(cwd)
    narrow_scope(mod, root, transcript_path)
    ts = mod.in_scope_transcripts(root, True)
    if not ts:
        return "", 0
    cutoff = mod.current_cutoff(ts, transcript_path if isinstance(transcript_path, str) and transcript_path else None, prompt)
    blocks, total, mentions, first = [], len(HIST_HEADER) + 1, 0, None
    for ident in idents:
        if len(blocks) >= MAX_IDENTS or time.monotonic() - _state["t0"] > BUDGET_IDENT_S:
            break  # 3 blocks with mentions, or the time is short: print what we have
        lines, n = mod.history_for(ts, ident, "any", cutoff, PER_IDENT_CAP)
        if n == 0:
            continue
        block = "\n".join(lines)
        foot = len("Full history: python3 %s history %s --all" % (recall_cmd(), first or ident)) + 1
        if blocks and total + len(block) + 1 + foot > TOTAL_CAP:
            break
        blocks.append(block)
        total += len(block) + 1
        mentions += n
        first = first or ident
    if not blocks:
        return "", 0
    out = HIST_HEADER + "\n" + "\n".join(blocks) + "\nFull history: python3 %s history %s --all\n" % (recall_cmd(), first)
    return out, mentions


def format_output(mod, hits):
    lines = [HEADER]
    for h in hits:
        lines.append("- %s:%d %s %s: %s" % (h["sid8"], h["line"], mod.fmt_ts(h["ts"]), h["role"], h["snippet"]))
    h = hooks_home()
    lines.append(FOOTER.replace("~/.claude/hooks/recall.py", h.rstrip("/") + "/recall.py") if h else FOOTER)
    return "\n".join(lines) + "\n"


def size_trigger(prompt, d):
    """F78: with the marker .recall-size-trigger and no cue, -> the identifiers of the prompt that are subjects in this
    chat (not existing paths below cwd), or []. The chat must hold 100,000 tokens or more (HANDOFF_AUTO_RECALL_TOKENS)."""
    if (os.environ.get("HANDOFF_AUTO_RECALL_SIZE") or "").strip() == "0":
        return []
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith("/"):
        return []
    h = hooks_home() or os.path.join(HOME, ".claude", "hooks")
    if not os.path.isfile(os.path.join(h, ".recall-size-trigger")):
        return []
    tp, cwd = d.get("transcript_path"), d.get("cwd")
    if not isinstance(tp, str) or not os.path.isfile(tp) or not isinstance(cwd, str) or not cwd.strip():
        return []
    import importlib.util
    path = next((p for p in hook_candidates("guard_common.py", os.path.join(HOME, ".claude", "hooks", "guard_common.py"),
                                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard_common.py")) if os.path.isfile(p)), None)
    if not path:
        return []
    spec = importlib.util.spec_from_file_location("lh_guard_common", path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    s = (os.environ.get("HANDOFF_AUTO_RECALL_TOKENS") or "").strip()
    need = int(s) if s.isdigit() and 10000 <= int(s) <= 1000000 else 100000
    tk = g.context_tokens(tp)
    if tk is None or tk < need:
        return []
    idents = identifiers(prompt)
    if not idents:
        return []
    r = g.read_session(tp, _state["t0"] + 1.5, prompt)
    if r is None:
        return []
    idx, _sent = g.build_index(r[0])
    return [t for t in idents if g.is_subject(idx, t.lower()) and not os.path.exists(os.path.join(cwd, t))]


def main():
    if (os.environ.get("HANDOFF_AUTO_RECALL") or "").strip() == "0":
        return
    try:
        d = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if not isinstance(d, dict):
        return
    prompt = d.get("prompt")
    size_idents = []
    if not triggered(prompt):
        try:
            size_idents = size_trigger(prompt, d)
        except BaseException:
            size_idents = []
        if not size_idents:
            return
        _state["cue"] = "size"
    _state["sid"] = d.get("session_id") or ""
    timer = threading.Timer(WATCHDOG_S, watchdog)
    timer.daemon = True
    timer.start()
    out, hits = "", []
    try:
        cwd = d.get("cwd")
        idents = size_idents or identifiers(prompt)
        _state["idents"] = len(idents)
        mod = load_recall() if isinstance(cwd, str) and cwd.strip() else None
        if mod is not None and idents:
            out, n = history_output(mod, prompt, idents, cwd, d.get("transcript_path"))
            if out:
                _state["mode"] = "history"
                hits = [None] * n
        if not out and not size_idents:
            _state["mode"] = "search"
            words = query_words(prompt)
            if words and mod is not None:
                hits = search(mod, prompt, words, cwd, _state["sid"], d.get("transcript_path"))
                if hits:
                    out = format_output(mod, hits)
    except BaseException:
        out, hits = "", []
    timer.cancel()
    with _state["lock"]:
        if _state["done"]:
            timer.join(0.3)  # the watchdog is writing the timed_out row; let it finish
            return
        _state["done"] = True
    write_health(len(hits) if out else 0, False)
    if out and (time.monotonic() - _state["t0"]) <= BUDGET_S:
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except BaseException:
            pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
