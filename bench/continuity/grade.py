#!/usr/bin/env python3
"""Code grader for the continuity benchmark. It never calls a model.

grade(ptype, items, raw) returns one dict for each item:
  {"id", "parse_ok", "correct", "parsed", "abstained"}
Each item is right (True) or wrong (False). There is no partial credit.
An explicit null in the main field is an abstention: parse_ok True, correct False, abstained True.
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


def s_label_match(a, want):
    """Type S only: the reply is `<anything> = <value>` or `<anything>: <value>`.
    The value must be the whole text after the last ` = ` or `: `. A longer text after the label is wrong."""
    cuts = [(a.rfind(sep), len(sep)) for sep in (" = ", ": ")]
    cuts = [c for c in cuts if c[0] != -1]
    if not cuts:
        return False
    i, n = max(cuts)
    return norm(a[i + n:]) == want


def as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


ABSTAIN_FIELD = {"R": "answer", "D": "answer", "U": "answer", "F": "answer", "S": "answer",
                 "X": "current", "E": "reason_code", "M": "order", "C": "earlier"}


def is_abstain(ptype, e):
    """True if the main field of the entry is present with the value null (the model says it does not know)."""
    f = ABSTAIN_FIELD.get(ptype)
    return isinstance(e, dict) and f in e and e[f] is None


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
        ok = a == want or (ptype == "R" and a == "TAG-" + want) or (ptype == "S" and s_label_match(a, want))
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
    if ptype == "M":
        order = e.get("order")
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            return False, False
        return True, [norm(x) for x in order] == item["order"]     # exact order, all ids
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
            out.append({"id": it["id"], "parse_ok": False, "correct": ok, "parsed": None, "abstained": False})
        return out
    for it in items:
        e = entry_for(answers, it["id"])
        if e is None:
            out.append({"id": it["id"], "parse_ok": False, "correct": False, "parsed": None, "abstained": False})
            continue
        if is_abstain(ptype, e):
            out.append({"id": it["id"], "parse_ok": True, "correct": False, "parsed": e, "abstained": True})
            continue
        pok, ok = check(ptype, it, e)
        out.append({"id": it["id"], "parse_ok": pok, "correct": bool(ok), "parsed": e, "abstained": False})
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


MCP_PREFIX = "mcp__lossless-recall__"
INJECTION_TEXT = "Lossless Handoff auto-recall:"


def request_stats(records, marker):
    """(requests, largest output tokens of one request) of a fork, from its transcript. Only records from the probe message on count (a fork copies
    the parent history). A request is an assistant message id; a streamed message is written more than once, so the largest usage per id counts.
    CLAUDE_CODE_MAX_OUTPUT_TOKENS caps ONE request, not the fork: the sum over a fork that uses tools can pass the cap with no request near it."""
    seen, by_id = False, {}
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            continue
        m = r.get("message")
        if not seen:
            c = m.get("content") if isinstance(m, dict) else None
            txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
            if marker in txt and r.get("type") == "user":
                seen = True
            continue
        if r.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
            n = m["usage"].get("output_tokens")
            if isinstance(n, (int, float)):
                k = m.get("id") or ("rec%d" % i)
                by_id[k] = max(by_id.get(k, 0), int(n))
    return len(by_id), (max(by_id.values()) if by_id else None)


def search_used(records, marker):
    """Probe design 3. (used, kinds): did the fork search for the fact? kinds holds "bash" (a recall.py call or a transcript read),
    "mcp" (an mcp__lossless-recall__ call) and "injection" (an auto-recall injection in the probe turn).
    Only records from the probe message on count, because a fork copies the parent history."""
    kinds, seen = [], False
    for r in records:
        if not isinstance(r, dict):
            continue
        if not seen:
            m = r.get("message")
            c = m.get("content") if isinstance(m, dict) else None
            txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
            if marker in txt and r.get("type") == "user":
                seen = True
            else:
                continue
        blob = json.dumps(r, ensure_ascii=False)
        if INJECTION_TEXT in blob and "injection" not in kinds and "tool_use" not in blob:
            kinds.append("injection")
        if "Lossless Handoff history check" in blob and "guard" not in kinds and "tool_use" not in blob:
            kinds.append("guard")       # F77 (Stop) or F80 (tool call) added the history to the fork
        m = r.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    s = json.dumps(b.get("input"))
                    if str(b.get("name") or "").startswith(MCP_PREFIX) and "mcp" not in kinds:
                        kinds.append("mcp")
                    if (".claude/projects" in s or ".jsonl" in s or "recall.py" in s) and "bash" not in kinds:
                        kinds.append("bash")
    return bool(kinds), kinds


def lookup_calls(records, marker, limit=300):
    """Tool calls after the probe message (the one with the marker), in order:
    [{"tool": name, "input": input as JSON text cut to `limit` characters}]. Information only."""
    out = []
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
                    out.append({"tool": b.get("name"), "input": json.dumps(b.get("input"), ensure_ascii=False)[:limit]})
    return out


# ---- summary retention: how many planted answers a compaction summary still holds
RETAIN_TYPES = ("R", "D", "S", "X", "C", "U")
RETAIN_FIELD = {"R": "answer", "D": "answer", "S": "answer", "U": "answer", "X": "new", "C": "tok1"}


def planted_answers(script):
    """[(type, item id, value)] for every probe answer with one value: R, D, S, X new value, C, U."""
    out = []
    for t in RETAIN_TYPES:
        for it in script["items"].get(t, []):
            v = it.get(RETAIN_FIELD[t])
            if isinstance(v, str) and v:
                out.append((t, it["id"], v))
    return out


def summary_texts(records):
    """Text of each isCompactSummary record, in order."""
    out = []
    for r in records:
        if not (isinstance(r, dict) and r.get("isCompactSummary")):
            continue
        m = r.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
        out.append(c if isinstance(c, str) else json.dumps(m, ensure_ascii=False))
    return out


def retention(records, script):
    """Count the planted answers found in each summary. Returns
    {"total", "summaries", "found" (per summary, in order), "last", "by_type_last", "collapse"}.
    collapse is True when the last summary keeps fewer than half of what the best earlier summary kept."""
    ans = planted_answers(script)
    texts = summary_texts(records)
    found = [sum(1 for (_t, _i, v) in ans if v in tx) for tx in texts]
    by_type = {}
    if texts:
        for (t, _i, v) in ans:
            c = by_type.setdefault(t, [0, 0])
            c[1] += 1
            c[0] += 1 if v in texts[-1] else 0
    collapse = len(found) >= 2 and max(found[:-1]) > 0 and found[-1] < max(found[:-1]) / 2
    return {"total": len(ans), "summaries": len(texts), "found": found, "last": found[-1] if found else None,
            "by_type_last": by_type, "collapse": collapse}
