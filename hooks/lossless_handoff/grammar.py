# Shared grammars (F90): G1 decisions-list line, G2 recall hit id. One parser for each format.
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
