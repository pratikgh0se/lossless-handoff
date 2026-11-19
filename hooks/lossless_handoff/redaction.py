# Secret masking: the fallback copy, redact, redact_tree and the handoff file writer.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

# F65 fallback: a copy of the redaction code of recall.py, used only when recall.py is not next to this hook
# (the install check and the smoke test keep both copies equal). Normal runs use recall.py.
_FB_SECRET_RES = [
    ("aws", re.compile(r"(?<![A-Za-z0-9_-])(?:AKIA|ASIA)[0-9A-Z]{16}"), 0),
    ("github", re.compile(r"(?<![A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9]{20,}"), 0),
    ("github", re.compile(r"(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{20,}"), 0),
    ("slack", re.compile(r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("anthropic", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{16,}"), 0),
    ("openai", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"), 0),
    ("google", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"), 0),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), 0),
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----(?:[^-]|-(?!----))*-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----"), 0),
    # URL credentials: scheme://user:PASSWORD@host (the password goes)
    ("url", re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]{1,100}:([^\s/@]{3,200})@"), 1),
    # Authorization: Bearer VALUE
    ("secret", re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})"), 1),
    # key=value or key: value; the key name ends with a secret word; the key stays, the value goes.
    # No prefix scan before the key word, so the match time is linear. _fb_value_ok() filters the value.
    ("secret", re.compile(r"(?i)(?:password|passwd|passphrase|secret(?:[_-]?key)?|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)"
                          r"[\"']?\s*[:=][ \t]*(?:bearer[ \t]+)?(\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|[^\s\"'`,;]+)"), 1),
]
_FB_MARK = "[redacted]"  # legacy mark of earlier versions; readers still treat it as redacted


def _fb_mark_for(kind):
    return "[REDACTED:%s]" % kind


def _fb_value_ok(v):
    """True when a key=value value looks like a secret: 8 or more characters, not only letters, not only
    digits, not a call or an index (get_secret(name), os.environ[...]) and not a variable reference."""
    if len(v) < 8 or v.isdigit() or re.fullmatch(r"[A-Za-z_.]+", v):
        return False
    if re.match(r"[A-Za-z_][\w.]*[(\[]", v) or re.fullmatch(r"\$\{?\w+\}?|<[A-Za-z_ -]*>|\{\{.*\}\}", v):
        return False
    return True


def _fb_secret_spans(text, kv=True):
    """-> merged [[start, end, kind], ...] of the secrets in text (end-exclusive)."""
    spans = []
    for kind, rx, grp in _FB_SECRET_RES:
        if not kv and rx is _FB_SECRET_RES[-1][1]:
            continue  # kv=False: leave out the key=value form (used on text whose lines were already joined)
        for m in rx.finditer(text):
            if not grp:
                spans.append((m.start(), m.end(), kind))
                continue
            s, e = m.start(grp), m.end(grp)
            if text.startswith(("[REDACTED", _FB_MARK), s):
                continue  # already masked: keep the function idempotent
            if kind == "secret":
                if text[s] in "\"'" and e - s >= 2 and text[e - 1] == text[s]:
                    s, e = s + 1, e - 1  # a quoted value: mask what is inside the quotes
                if not _fb_value_ok(text[s:e]):
                    continue
            spans.append((s, e, kind))
    spans.sort(key=lambda x: (x[0], -x[1]))
    merged = []
    for s, e, k in spans:
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, k])
    return merged


def _fb_redact(text, kv=True):
    """F65: mask secrets as [REDACTED:<kind>]. Text without secrets is returned unchanged."""
    if not text:
        return text
    spans = _fb_secret_spans(text, kv)
    if not spans:
        return text
    out, pos = [], 0
    for s, e, k in spans:
        out.append(text[pos:s])
        out.append(_fb_mark_for(k))
        pos = e
    out.append(text[pos:])
    return "".join(out)


_RECALL_MOD = []


def redact(text, kv=True):
    """F65: mask secrets with the shared function in recall.py. A missing or stub recall.py (no redact(), an
    error, or an exit at import) never costs a handoff: the copy above is used instead."""
    if not _RECALL_MOD:
        try:
            _RECALL_MOD.append(_load_recall())
        except BaseException as e:  # a stub that calls sys.exit(0) raises SystemExit at import
            if isinstance(e, KeyboardInterrupt):
                raise
            _RECALL_MOD.append(None)
    fn = getattr(_RECALL_MOD[0], "redact", None)
    if not callable(fn):
        return _fb_redact(text, kv)
    try:
        out = fn(text, kv)
    except Exception:
        return _fb_redact(text, kv)
    return out if isinstance(out, str) else _fb_redact(text, kv)


def redact_tree(v):
    """F65: one guard for all sections. Mask every string inside the parsed transcript data (key=value included)
    BEFORE any section cuts, flattens or joins it, so a section cannot forget to do it."""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, list):
        return [redact_tree(x) for x in v]
    if isinstance(v, tuple):
        return tuple(redact_tree(x) for x in v)
    if isinstance(v, dict):
        return {k: redact_tree(x) for k, x in v.items()}
    return v


def write_handoff_files(sid, p, body):
    """Write the dated handoff file and copy it to <sid>-latest.md, each atomically. F65: secrets are masked first."""
    # the key=value form is masked per field before lines are joined (see one_line, _f_users); a pass over the
    # finished body would mask a path that follows a key word once its line break is gone
    body = redact(body, kv=False)
    write_atomic(p, body)
    write_atomic(os.path.join(HANDOFFS_DIR, f"{sid}-latest.md"), body)
