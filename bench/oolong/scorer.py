#!/usr/bin/env python3
"""The OOLONG-synth scoring rule, ported without change of logic.

Source: github.com/abertsch72/oolong, file src/eval/eval_helpers.py, commit 0bb7eabe839218fee7fe8d007f41cfc2fd3ae24c,
functions synth_attempt_answer_parse and synth_process_response.
The rule:
  - Exact string match of the parsed answer with the gold answer: score 1.
  - 'more common', 'less common', 'same frequency': score 1 if the phrase is inside the gold text.
  - Number (ANSWER_TYPE.NUMERIC): 0.75 ** abs(gold - answer).
  - Date (ANSWER_TYPE.DATE): 1 if the parsed date equals the gold date.
  - Everything else (labels, user ids): exact match only.
Changes from the source (no change of the score):
  - No litellm, datasets or dateutil import. If dateutil is not installed, a short list of date formats is used.
  - The function returns a plain dict and does not write files.
"""
import ast
import datetime as dt

SELFTEST_ONLY_NOTE = "python3 test_scorer.py runs the self-test"


def parse_answer(answer):
    """Source: synth_attempt_answer_parse. Returns (candidate, parse_confidence)."""
    parse_confidence = "low"
    if ":" not in answer:
        if len(answer) < 20:
            return answer, parse_confidence
        return (answer.split() or [""])[-1], parse_confidence
    cand = answer.split(":")[-1].strip()
    cand = cand.replace("*", "").replace("[", "").replace("]", "")
    parse_confidence = "med"
    if "User:" in answer or "Answer:" in answer or "Date:" in answer or "Label" in answer:
        parse_confidence = "high"
    if len(cand) < 20:
        parse_confidence = "vhigh"
    elif "more common" in cand:
        cand = "more common"
    elif "less common" in cand:
        cand = "less common"
    elif "same frequency" in cand:
        cand = "same frequency"
    return cand, parse_confidence


def _parse_date(text):
    try:
        from dateutil import parser as dp
        return dp.parse(text)
    except ImportError:
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(text.strip(), fmt)
        except ValueError:
            pass
    raise ValueError("date")


def gold_value(answer_field):
    """The gold of a row. The row field looks like "['label']", "[127]" or "[datetime.date(2022, 3, 1)]"."""
    if "datetime" in answer_field:
        return dt.datetime.strptime(answer_field, "[datetime.date(%Y, %m, %d)]")
    return ast.literal_eval(answer_field)[0]


def score(answer_field, answer_type, output):
    """Score one model output. Returns a dict with score, parsed answer, parse confidence, gold."""
    gold = gold_value(answer_field)
    out, conf = parse_answer(output)
    s = 0
    if str(out) == str(gold):
        s = 1
    elif str(out) in ("more common", "less common", "same frequency"):
        if str(out) in str(gold):
            s = 1
    elif answer_type == "ANSWER_TYPE.NUMERIC":
        try:
            s = 0.75 ** abs(int(gold) - int(out))
        except Exception:
            conf = "low"
    elif answer_type == "ANSWER_TYPE.DATE":
        try:
            s = 1 if _parse_date(str(out)) == gold else 0
        except Exception:
            conf = "low"
    return {"score": float(s), "parsed": str(out), "parse_confidence": conf, "gold": str(gold)}
