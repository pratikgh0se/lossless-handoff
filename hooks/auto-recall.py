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

HOOK_VERSION = "1.17.0"
HOME = os.path.expanduser("~")
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

_state = {"lock": threading.Lock(), "done": False, "t0": time.monotonic(), "sid": "", "hits": 0, "logged": False, "cue": "quote"}


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
        if k not in seen:
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
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "auto-recall.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": str(_state["sid"])[:36],
                                "fired": True, "cue": _state["cue"], "hits": int(hits),
                                "ms": int((time.monotonic() - _state["t0"]) * 1000), "timed_out": bool(timed_out)}) + "\n")
    except BaseException:
        pass


def watchdog():
    with _state["lock"]:
        if _state["done"]:
            return
        _state["done"] = True  # from here the main thread prints nothing
    write_health(0, True)
    os._exit(0)


def load_recall():
    for p in (os.path.join(HOME, ".claude", "hooks", "recall.py"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")):
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


def is_current_prompt(h, prompt, sid):
    """Exclude the current prompt itself (Claude Code may already have written it to the transcript)."""
    if h.get("role") != "user":
        return False
    p = collapse(prompt).lower()
    s = collapse(h.get("snippet")).lower()
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
        phrase_hits += mod.keyword_hits(a, ts, re.compile(re.escape(ph), re.I), ph, True)
    alt = "|".join(re.escape(w) for w in words)
    # rg gets the plain alternation (a fast pre-filter); the match itself needs whole words
    word_rx = re.compile(r"\b(?:%s)\b" % alt, re.I)
    kw = mod.keyword_hits(a, ts, word_rx, alt, False)[:200]
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
    picked = [h for h in picked if not is_current_prompt(h, prompt, sid)][:TOP_HITS * 3]
    # meaning-only hits carry no text yet; fill them for the few we may show
    mod.fill_meaning_snippets(picked[:TOP_HITS * 3], ts)
    picked = [h for h in picked if not is_current_prompt(h, prompt, sid)][:TOP_HITS]
    for h in picked:
        h["snippet"] = collapse(mod.redact(collapse(h.get("snippet"))))[:SNIPPET_MAX]
        h["snippet"] = collapse(mod.redact(h["snippet"]))  # a cut can split a secret: mask again
    return picked


def format_output(mod, hits):
    lines = [HEADER]
    for h in hits:
        lines.append("- %s:%d %s %s: %s" % (h["sid8"], h["line"], mod.fmt_ts(h["ts"]), h["role"], h["snippet"]))
    lines.append(FOOTER)
    return "\n".join(lines) + "\n"


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
    if not triggered(prompt):
        return
    _state["sid"] = d.get("session_id") or ""
    timer = threading.Timer(WATCHDOG_S, watchdog)
    timer.daemon = True
    timer.start()
    out, hits = "", []
    try:
        words = query_words(prompt)
        cwd = d.get("cwd")
        if words and isinstance(cwd, str) and cwd.strip():
            mod = load_recall()
            if mod is not None:
                hits = search(mod, prompt, words, cwd, _state["sid"], d.get("transcript_path"))
                if hits:
                    out = format_output(mod, hits)
    except BaseException:
        out, hits = "", []
    timer.cancel()
    with _state["lock"]:
        if _state["done"]:
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
