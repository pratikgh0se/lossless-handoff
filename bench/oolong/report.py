#!/usr/bin/env python3
"""Report of an OOLONG run: python3 report.py <run-id> [--draws N]. Writes results/<run-id>/summary.md and prints it.

Per arm and length: mean score (raw and cleaned) with 95 percent bootstrap CI, paired difference B-A and C-A,
cost per task, wall time, turns, workflow calls (arm A must show 0), compactions.
Cleaned set: a task is removed (for all arms, to keep the pairs) if the no-context control got it right
or any arm's transcript has a contamination hit. The removed counts are printed.
"""
import json, math, os, random, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402  (HIT_PATTERNS, scan_records)
import scorer  # noqa: E402
from harness import cont  # noqa: E402

LEN_LABEL = {32768: "32K", 131072: "128K"}


def boot_ci(vals, draws, rng):
    if not vals:
        return (float("nan"), float("nan"))
    if len(vals) == 1:
        return (vals[0], vals[0])
    means = sorted(sum(rng.choice(vals) for _ in vals) / len(vals) for _ in range(draws))
    return (means[int(0.025 * draws)], means[min(draws - 1, int(0.975 * draws))])


def fmt(m, ci):
    return "%.2f [%.2f, %.2f]" % (m, ci[0], ci[1]) if vals_ok(m) else "-"


def vals_ok(x):
    return x == x


def mean(v):
    return sum(v) / len(v) if v else float("nan")


def load(run):
    d = os.path.join(HERE, "results", run)
    rows = [json.loads(x) for x in open(os.path.join(d, "results.jsonl"), encoding="utf-8")]
    cfg = json.load(open(os.path.join(d, "config.json"), encoding="utf-8"))
    return d, cfg, rows


def rescan(d, rows):
    """Search the stored transcript copy again, so the hit rules can change without a new run."""
    for r in rows:
        p = os.path.join(d, "transcripts", r["unit"] + ".jsonl")
        if os.path.isfile(p):
            r["hits"] = harness.scan_records(cont.read_jsonl(p), r.get("full_answer") or "")
    return rows


def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    draws = int(sys.argv[sys.argv.index("--draws") + 1]) if "--draws" in sys.argv else 10000
    if "--draws" in sys.argv:
        args = [x for x in args if x != str(draws)]
    run = args[0] if args else open(os.path.join(HERE, "results", ".last")).read().strip()
    d, cfg, rows = load(run)
    rows = rescan(d, rows)
    rng = random.Random(20261118)
    out = []
    P = out.append
    P("# OOLONG run %s" % run)
    P("")
    P("- Model: %s. Claude Code %s. Window setting: %s. Bench commit %s, decision.json commit %s, Lossless Handoff commit %s." % (
        cfg["model"], cfg["claude_version"], cfg["window"], cfg["bench_commit"], cfg["decision_commit"], cfg["lh_commit"]))
    P("- Data: %s, split validation (trec_coarse is a validation task), Hugging Face revision %s, OOLONG repo commit %s." % (
        cfg["dataset"], cfg["hf_revision"], cfg["oolong_commit"]))
    P("- Scorer: bench/oolong/scorer.py, a port of src/eval/eval_helpers.py of the OOLONG repo (commit above). Code only, no model grader.")
    P("- Config records: one per unit in config_records.json. The harness checked before the run that only the intended setting differs.")
    P("- The Claude Code version differs from the one in the LCM paper (v2.1.4), so these numbers are not a copy of the paper's run.")
    P("")
    by = {}
    for r in rows:
        by.setdefault((r["task_id"], r["arm"]), r)
    arms = [a for a in "ABC" if any(k[1] == a for k in by)]
    lengths = sorted({r["length"] for r in rows})
    tasks = {}
    for r in rows:
        tasks.setdefault(r["task_id"], r["length"])
    # contamination flags per task
    ctrl_right, trace_hit, hit_list = set(), set(), []
    for tid in tasks:
        n = by.get((tid, "N"))
        if n is not None and n["score"] >= 1.0:
            ctrl_right.add(tid)
        for arm in arms + ["N"]:
            r = by.get((tid, arm))
            if r and r["hits"]:
                trace_hit.add(tid)
                hit_list.append((tid, arm, r["hits"]))
    removed = ctrl_right | trace_hit
    P("## Contamination guards")
    P("")
    P("- Tasks: %d. Removed from the cleaned score: %d (control right without context: %d; transcript hit: %d; both: %d)." % (
        len(tasks), len(removed), len(ctrl_right), len(trace_hit), len(ctrl_right & trace_hit)))
    P("- A control hit by chance also removes the task (a label question has about 1 in 6 chance). The rule is the same for all arms.")
    P("")
    P("| Task | Length | Control (no context) score | Control parsed | Gold |")
    P("|---|---|---|---|---|")
    for tid, ln in tasks.items():
        n = by.get((tid, "N"))
        P("| %s | %s | %s | %s | %s |" % (tid, LEN_LABEL.get(ln, ln), "%.2f" % n["score"] if n else "no control", n["parsed"][:30] if n else "", (n or by[(tid, arms[0])])["gold"]))
    P("")
    P("Transcript hits (search of the text the model wrote, and its tool inputs; patterns: %s, urls in commands):" % ", ".join(n for n, _ in harness.HIT_PATTERNS))
    P("")
    if not hit_list:
        P("- none")
    for tid, arm, hs in hit_list:
        for h in hs[:6]:
            P("- task %s arm %s: %s in %s: `...%s...`" % (tid, arm, h["pattern"], h["where"], h["excerpt"][:140].replace("`", "'")))
    P("")
    P("## Scores")
    P("")
    P("| Length | Arm | n raw | Raw mean [95% CI] | n clean | Clean mean [95% CI] | Cost/task | Wall s/task | Turns/task | Workflow calls | Agent calls (tasks with 1+) | Compactions | Errors |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    flags = []
    for ln in lengths:
        tids = [t for t, l in tasks.items() if l == ln]
        for arm in arms:
            rs = [by[(t, arm)] for t in tids if (t, arm) in by]
            cl = [by[(t, arm)] for t in tids if (t, arm) in by and t not in removed]
            raw = [r["score"] for r in rs]
            cln = [r["score"] for r in cl]
            wf = sum(r["workflow_calls"] for r in rs)
            wft = sum(1 for r in rs if r["subagent_calls"] > 0)
            if wf:
                flags.append("arm %s shows %d workflow calls at %s: the Workflow tool must be disallowed" % (arm, wf, LEN_LABEL.get(ln, ln)))
            P("| %s | %s | %d | %s | %d | %s | $%.3f | %.0f | %.1f | %d | %d (%d) | %d | %d |" % (
                LEN_LABEL.get(ln, ln), arm, len(raw), fmt(mean(raw), boot_ci(raw, draws, rng)), len(cln), fmt(mean(cln), boot_ci(cln, draws, rng)),
                mean([r["cost_usd"] for r in rs]), mean([r["wall_s"] for r in rs]), mean([r["num_turns"] or 0 for r in rs]),
                wf, sum(r["subagent_calls"] for r in rs), wft, sum(r["compactions"] for r in rs), sum(1 for r in rs if r["is_error"])))
    P("")
    P("## Paired difference (same tasks, score of the arm minus score of A)")
    P("")
    P("| Length | Pair | n raw | Raw diff [95% CI] | n clean | Clean diff [95% CI] |")
    P("|---|---|---|---|---|---|")
    for ln in lengths:
        tids = [t for t, l in tasks.items() if l == ln]
        for arm in [a for a in arms if a != "A"]:
            pr = [t for t in tids if (t, arm) in by and (t, "A") in by]
            dr = [by[(t, arm)]["score"] - by[(t, "A")]["score"] for t in pr]
            dc = [by[(t, arm)]["score"] - by[(t, "A")]["score"] for t in pr if t not in removed]
            P("| %s | %s-A | %d | %s | %d | %s |" % (LEN_LABEL.get(ln, ln), arm, len(dr), fmt(mean(dr), boot_ci(dr, draws, rng)), len(dc), fmt(mean(dc), boot_ci(dc, draws, rng))))
    P("")
    P("## Notes")
    P("")
    for a_ in arms:
        if a_ == "B":
            used = sum(1 for r in rows if r["arm"] == "B" and r["subagent_calls"] > 0)
            total = sum(1 for r in rows if r["arm"] == "B")
            P("- Arm B subagent use rate: %d of %d tasks (%.0f%%). At 0%% arm B equals arm A in practice. Arm A also may use subagents; see its Agent calls." % (used, total, 100.0 * used / max(total, 1)))
        if a_ == "C":
            comp = sum(r["compactions"] for r in rows if r["arm"] == "C")
            P("- Arm C compactions: %d. At lengths under the compaction window, arm C equals arm A apart from the session-start text; with 0 compactions it tests only that text." % comp)
    P("- Wall time and cost are per task, one `claude -p` call. Cost is `total_cost_usd` (API-equivalent).")
    P("- Sample size: 30 paired tasks see only a gap of 0.15 or more (paired SD about 0.3, 95%% half-width about 0.11). A smaller gap is not a result.")
    for f in flags:
        P("- FLAG: " + f)
    errs = [r for r in rows if r["is_error"]]
    for r in errs:
        P("- Error unit %s: subtype %s, stderr %s" % (r["unit"], r.get("subtype"), r.get("stderr", "")[:100]))
    P("")
    P("## Decision rule (decision.json)")
    P("")
    dec = json.load(open(os.path.join(HERE, "decision.json"), encoding="utf-8"))
    th = dec["thresholds"]
    dl = 131072
    tids = [t for t, l in tasks.items() if l == dl and t not in removed]
    npair = len([t for t in tids if (t, "A") in by and (t, "B") in by])
    if npair < 10:
        P("- Rule not applied: %d paired clean tasks at 128K (10 or more needed). This run does not decide P4-1." % npair)
    else:
        best, bm, bc = None, -1, 0
        for arm in ("A", "B"):
            rs = [by[(t, arm)] for t in tids if (t, arm) in by]
            if rs and mean([r["score"] for r in rs]) > bm:
                best, bm, bc = arm, mean([r["score"] for r in rs]), mean([r["cost_usd"] for r in rs])
        est = dec["map_plan_estimate_usd_per_task"]["128K"]
        ratio = bc / est
        if bm <= th["build_score_max"] or ratio > th["build_cost_ratio_min"]:
            verdict = "BUILD llm_map"
        elif bm >= th["no_build_score_min"] and ratio <= th["no_build_cost_ratio_max"]:
            verdict = "DO NOT build llm_map"
        else:
            verdict = "the user decides (between the thresholds)"
        P("- Best plain arm at 128K: %s, clean mean %.2f, cost per task $%.3f = %.2f x the map-plan estimate ($%.2f). Verdict by the rule: **%s**." % (best, bm, bc, ratio, est, verdict))
    txt = "\n".join(out) + "\n"
    open(os.path.join(d, "summary.md"), "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
