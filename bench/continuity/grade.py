#!/usr/bin/env python3
"""Code grader for the continuity benchmark. It never calls a model.

grade(ptype, items, raw) returns one dict for each item:
  {"id", "parse_ok", "correct", "parsed"}
Each item is right (True) or wrong (False). There is no partial credit.
"""
import json, re

TOKEN_RE = re.compile(r"\b[A-HJ-NP-Z2-9]{4}-\d{4}-[A-HJ-NP-Z2-9]{4}\b")
FALLBACK_TYPES = ("R", "D", "U", "F")   # types whose answer is one bare token


def find_answers(raw):
    """First JSON object in the text that holds an "answers" list. Returns the list or None."""
    if not isinstance(raw, str):
        return None
    dec = json.JSONDecoder()
    i = raw.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(raw, i)
            if isinstance(obj, dict) and isinstance(obj.get("answers"), list):
                return obj["answers"]
        except ValueError:
            pass
        i = raw.find("{", i + 1)
    return None


def norm(v):
    """Trim. Remove one set of surrounding backticks or quotes. Case stays. Returns None if v is not text."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "`\"'":
        s = s[1:-1].strip()
    return s


def as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def entry_for(answers, item_id):
    for a in answers or []:
        if isinstance(a, dict) and str(a.get("id")).strip() == str(item_id):
            return a
    return None


def check(ptype, item, e):
    """Return (parse_ok, correct) for one item and its entry e (a dict)."""
    if ptype in ("R", "D", "U", "F", "S"):
        a = norm(e.get("answer"))
        if a is None:
            return False, False
        want = item["answer"]
        ok = a == want or (ptype == "R" and a == "TAG-" + want)
        return True, ok
    if ptype == "X":
        cur = norm(e.get("current"))
        rep = e.get("replaced")
        if isinstance(rep, str):
            rep = [rep]
        if cur is None or not isinstance(rep, list):
            return False, False
        reps = [norm(x) for x in rep if isinstance(x, str)]
        return True, cur == item["new"] and item["old"] in reps and cur != item["old"]
    if ptype == "E":
        pr, rc = as_bool(e.get("proceed")), norm(e.get("reason_code"))
        if pr is None or rc is None:
            return False, False
        return True, pr is False and rc == item["ecode"]
    if ptype == "C":
        cf, ea = as_bool(e.get("conflict")), norm(e.get("earlier"))
        if cf is None or ea is None:
            return False, False
        return True, cf is True and ea == item["tok1"]
    raise ValueError("unknown probe type " + str(ptype))


def grade(ptype, items, raw):
    answers = find_answers(raw)
    out = []
    if answers is None:
        # Parse failed. Fixed fallback for bare-token types: the Nth token-shaped text is the Nth answer.
        found = TOKEN_RE.findall(raw) if isinstance(raw, str) else []
        for n, it in enumerate(items):
            ok = False
            if ptype in FALLBACK_TYPES and len(found) == len(items):
                ok = found[n] == it["answer"]
            out.append({"id": it["id"], "parse_ok": False, "correct": ok, "parsed": None})
        return out
    for it in items:
        e = entry_for(answers, it["id"])
        if e is None:
            out.append({"id": it["id"], "parse_ok": False, "correct": False, "parsed": None})
            continue
        pok, ok = check(ptype, it, e)
        out.append({"id": it["id"], "parse_ok": pok, "correct": bool(ok), "parsed": e})
    return out


def lookup_used(records, marker):
    """True if, after the probe message (the one with the marker), a tool call reads a transcript or runs recall.py.
    records: parsed transcript lines (dicts) of the fork."""
    seen = False
    for r in records:
        m = r.get("message") if isinstance(r, dict) else None
        if not isinstance(m, dict) or not isinstance(m.get("content"), (list, str)):
            continue
        c = m["content"]
        if not seen:
            txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            if marker in txt and r.get("type") == "user":
                seen = True
            continue
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    s = json.dumps(b.get("input"))
                    if ".claude/projects" in s or ".jsonl" in s or "recall.py" in s:
                        return True
    return False
