#!/usr/bin/env python3
"""Self-test of scorer.py. Every answer type has cases. All cases must pass (100 percent).
Run: python3 test_scorer.py            (fixed cases)
     python3 test_scorer.py --data DIR (also checks every cached row: a perfect answer scores 1)
Exit 0 only if every case passes."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scorer  # noqa: E402

L, C, N, U, D = ("ANSWER_TYPE.LABEL", "ANSWER_TYPE.COMPARISON", "ANSWER_TYPE.NUMERIC", "ANSWER_TYPE.USER", "ANSWER_TYPE.DATE")
CASES = [
    # (name, answer field, answer type, model output, expected score)
    ("label exact", "['abbreviation']", L, "Label: abbreviation", 1.0),
    ("label wrong", "['abbreviation']", L, "Label: entity", 0.0),
    ("label bold and brackets", "['entity']", L, "Reasoning...\n**Label: [entity]**", 1.0),
    ("label multiword", "['numeric value']", L, "Label: numeric value", 1.0),
    ("label case differs", "['entity']", L, "Label: Entity", 0.0),
    ("label no colon, short", "['entity']", L, "entity", 1.0),
    ("label no colon, long takes last word", "['entity']", L, "The most common label in the data is clearly entity", 1.0),
    ("comparison more", "['more common than']", C, "Answer: location is more common than numeric value", 1.0),
    ("comparison less", "['less common than']", C, "Answer: location is less common than numeric value", 1.0),
    ("comparison same", "['same frequency as']", C, "Answer: location is same frequency as numeric value", 1.0),
    ("comparison wrong", "['less common than']", C, "Answer: location is more common than numeric value", 0.0),
    ("numeric exact", "[127]", N, "Answer: 127", 1.0),
    ("numeric off by one", "[127]", N, "Answer: 126", 0.75),
    ("numeric off by two", "[127]", N, "Answer: 129", 0.5625),
    ("numeric off by ten", "[100]", N, "Answer: 110", 0.75 ** 10),
    ("numeric not a number", "[127]", N, "Answer: many", 0.0),
    ("numeric trailing period fails as in the source", "[127]", N, "Answer: 127.", 0.0),
    ("user exact", "[30970]", U, "User: 30970", 1.0),
    ("user wrong", "[30970]", U, "User: 30971", 0.0),
    ("date exact", "[datetime.date(2022, 3, 1)]", D, "Date: Mar 01, 2022", 1.0),
    ("date iso", "[datetime.date(2022, 3, 1)]", D, "Date: 2022-03-01", 1.0),
    ("date wrong", "[datetime.date(2022, 3, 1)]", D, "Date: Mar 02, 2022", 0.0),
    ("date not a date", "[datetime.date(2022, 3, 1)]", D, "Date: soon", 0.0),
    ("empty output", "['entity']", L, "", 0.0),
]


def perfect_output(row):
    """Build a perfect model output for a data row, from its question and gold."""
    g = scorer.gold_value(row["answer"])
    q = row["question"]
    t = row["answer_type"]
    if t == L:
        return "Label: %s" % g
    if t == U:
        return "User: %s" % g
    if t == C:
        return "Answer: x is %s y" % g
    if t == N:
        return "Answer: %s" % g
    return "Answer: %s" % g


def main():
    bad = []
    for name, ans, typ, out, exp in CASES:
        got = scorer.score(ans, typ, out)["score"]
        if abs(got - exp) > 1e-9:
            bad.append((name, exp, got))
    total = len(CASES)
    if "--data" in sys.argv:
        d = sys.argv[sys.argv.index("--data") + 1]
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            for ln in open(os.path.join(d, fn), encoding="utf-8"):
                r = json.loads(ln)
                total += 2
                if scorer.score(r["answer"], r["answer_type"], perfect_output(r))["score"] != 1.0:
                    bad.append((fn + " id %s perfect" % r["id"], 1.0, "not 1"))
                wrong = "Label: zzz" if r["answer_type"] == L else "Answer: -999" if r["answer_type"] == N else "User: 0" if r["answer_type"] == U else "Answer: x is nonsense y"
                if scorer.score(r["answer"], r["answer_type"], wrong)["score"] == 1.0:
                    bad.append((fn + " id %s wrong" % r["id"], 0.0, "1"))
    for b in bad:
        print("FAIL", b)
    print("scorer self-test: %d of %d passed (%.0f%%)" % (total - len(bad), total, 100.0 * (total - len(bad)) / total))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
