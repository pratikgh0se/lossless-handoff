#!/usr/bin/env python3
"""Report of an OOLONG run: python3 report.py <run-id> [--draws N]. Writes results/<run-id>/summary.md and prints it.

Per arm and length: mean score (raw and cleaned) with 95 percent bootstrap CI, paired difference B-A and C-A,
cost per task, wall time, turns, workflow calls (arm A must show 0), compactions.
Cleaned set: a task is removed (for all arms, to keep the pairs) if the no-context control got it right
or any arm's transcript has a contamination hit. The removed counts are printed.
"""
import glob, json, math, os, random, statistics, sys

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


def task_cost(r):
    """Main session total_cost_usd plus the child llm_map calls (the main figure does not include them)."""
    return r["cost_usd"] + r.get("llm_map_cost_usd", 0.0)


def load(run):
    d = os.path.join(HERE, "results", run)
    rows = [json.loads(x) for x in open(os.path.join(d, "results.jsonl"), encoding="utf-8") if x.strip()]
    last = {}
    for r in rows:        # after a resume a unit can have two rows. Keep the last one (file order is finish order).
        last.pop(r["unit"], None)
        last[r["unit"]] = r
    rows = list(last.values())
    cfg = json.load(open(os.path.join(d, "config.json"), encoding="utf-8"))
    return d, cfg, rows


def rescan(d, rows):
    """Derive again from the stored transcript copy, so the rules can change without a new run: final text (last assistant text),
    score, turns (assistant messages), timeout salvage, and the hit rules (new, old, recognition). Cost stays as stored."""
    tp = os.path.join(d, "tasks.json")
    gold = {t["task_id"]: t for t in json.load(open(tp, encoding="utf-8"))} if os.path.isfile(tp) else {}
    try:
        work_base = json.load(open(os.path.join(d, "config.json"), encoding="utf-8")).get("work_base", "")
    except (OSError, ValueError):
        work_base = ""
    for r in rows:
        p = os.path.join(d, "transcripts", r["unit"] + ".jsonl")
        if not os.path.isfile(p):
            r.setdefault("hits_old_rule", r.get("hits", []))
            r.setdefault("recognition", [])
            r.setdefault("timed_out", r.get("stderr") == "timeout")
            continue
        main_recs = cont.read_jsonl(p)
        recs = list(main_recs)
        for sp in sorted(glob.glob(os.path.join(d, "transcripts", r["unit"], "**", "*.jsonl"), recursive=True)):
            recs += cont.read_jsonl(sp)          # subagent transcripts
        errors, bash = harness.tool_error_stats(recs)
        r.update(tool_errors=errors, bash_calls=bash["calls"], bash_started=bash["started"], bash_infra=bash["infra"],
                 bash_denied=bash["denied"], infra_examples=bash["examples"])
        lm = harness.llm_map_stats(recs)
        r.update(llm_map_calls=lm["llm_map_calls"], llm_map_ok_calls=lm["llm_map_ok_calls"])
        recs_all, recs = recs, main_recs
        refs = harness.shared_tmp_refs(recs_all, (work_base,))
        r.update(shared_tmp_refs=refs[:10], shared_tmp_ref_count=len(refs))
        texts = harness.assistant_texts(recs)
        was_timeout = r.get("stderr") == "timeout" or r.get("timed_out")
        r["timed_out"] = bool(was_timeout)
        final = texts[-1] if texts else (r.get("full_answer") or "")
        if was_timeout and not harness.has_final_answer(final):
            final = ""
        t = gold.get(r["task_id"])
        if t and (final or not r["is_error"]):
            sc = scorer.score(t["answer"], t["answer_type"], final)
            r.update(score=sc["score"], parsed=sc["parsed"], parse_confidence=sc["parse_confidence"], full_answer=final)
            if was_timeout and final:
                r["is_error"], r["subtype"], r["stderr"] = False, "timeout_answered", ""
        n = harness.count_turns(recs)
        if n:
            r["num_turns"] = n
        r["hits"] = harness.scan_records(recs_all, final)
        r["hits_old_rule"] = harness.scan_records(recs_all, final, "old")
        r["recognition"] = harness.scan_records(recs_all, final, "recognition")
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
    infra_msgs = harness.infra_scan(rows)
    ref_units = [r for r in rows if r.get("shared_tmp_ref_count", 0) > 0]
    ref_tasks = {r["task_id"] for r in ref_units}
    tmp_invalid = bool(rows) and len(ref_units) > 0.02 * len(rows)
    P("# OOLONG run %s" % run)
    P("")
    if tmp_invalid:
        P("# INVALID RUN: units used the shared temp folder /tmp/claude-<uid>. Do not use these scores.")
        P("")
        P("%d of %d units (%.1f%%, limit 2%%) named a file directly in the shared temp folder in a Bash, Write, Edit or Read input. Parallel units can overwrite such files." % (
            len(ref_units), len(rows), 100.0 * len(ref_units) / len(rows)))
        P("")
        for r in ref_units[:10]:
            P("- unit %s: %s" % (r["unit"], "; ".join(r.get("shared_tmp_refs", [])[:2])))
        P("")
    if infra_msgs:
        P("# INVALID RUN: the Bash tool failed to start in this run. Do not use these scores.")
        P("")
        P("The stop rule of the harness trips on the stored transcripts (main and subagent). A Bash error whose text does not start with `Exit code` is an infrastructure error. Rules: a unit with %d or more Bash calls and none started, or more than %d%% infrastructure errors in %d finished units." % (
            harness.INFRA_UNIT_MIN_CALLS, int(harness.INFRA_RATE * 100), harness.INFRA_WINDOW))
        P("")
        for m in infra_msgs[:10]:
            P("- " + m)
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
    ctrl_right, trace_hit, trace_hit_old, hit_list, recog_list = set(), set(), set(), [], []
    for tid in tasks:
        n = by.get((tid, "N"))
        if n is not None and n["score"] >= 1.0:
            ctrl_right.add(tid)
        for arm in arms + ["N"]:
            r = by.get((tid, arm))
            if r and r["hits"]:
                trace_hit.add(tid)
                hit_list.append((tid, arm, r["hits"]))
            if r and r.get("hits_old_rule"):
                trace_hit_old.add(tid)
            if r and r.get("recognition"):
                recog_list.append((tid, arm, r["recognition"]))
    removed = ctrl_right | trace_hit            # new rule (2026-11-18): the plain word TREC is not a hit
    removed_old = ctrl_right | trace_hit_old    # old rule: the word TREC was a hit
    recog_tasks = {t for t, _, _ in recog_list}
    P("## Contamination guards")
    P("")
    P("- Hit rule of 2026-11-18 (new): `trec_coarse`, oolong, ground truth, Hugging Face, download commands and urls in commands are hits. The plain word TREC is a recognition note only (below). The score table shows both the new-rule and the old-rule clean score.")
    P("- Tasks: %d. Removed from the cleaned score, new rule: %d (control right without context: %d; transcript hit: %d; both: %d). Old rule: %d (transcript hit: %d)." % (
        len(tasks), len(removed), len(ctrl_right), len(trace_hit), len(ctrl_right & trace_hit), len(removed_old), len(trace_hit_old)))
    P("- Recognition notes (plain word TREC, no removal): %d task(s), %d unit(s)." % (len(recog_tasks), len(recog_list)))
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
    P("Recognition notes (the plain word TREC in text the model wrote or in its tool inputs; counted and listed, not removed):")
    P("")
    if not recog_list:
        P("- none")
    for tid, arm, hs in recog_list:
        for h in hs[:3]:
            P("- task %s arm %s: %s in %s: `...%s...`" % (tid, arm, h["pattern"], h["where"], h["excerpt"][:140].replace("`", "'")))
    P("")
    P("## Scores")
    P("")
    P("| Length | Arm | n raw | Raw mean [95% CI] | n clean (new rule) | Clean mean, new rule [95% CI] | n clean (old rule) | Clean mean, old rule [95% CI] | Cost/task | Wall s/task | Turns/task | Workflow calls | Agent calls (tasks with 1+) | llm_map attempts / successes (tasks with 1+) | Compactions | Timed out (scored) | Errors | Shared tmp refs (units) | Raw mean without tmp-ref tasks [95% CI] (n) |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    flags = []
    for ln in lengths:
        tids = [t for t, l in tasks.items() if l == ln]
        for arm in arms:
            rs = [by[(t, arm)] for t in tids if (t, arm) in by]
            cl = [by[(t, arm)] for t in tids if (t, arm) in by and t not in removed]
            raw = [r["score"] for r in rs]
            nr = [r["score"] for r in rs if r["task_id"] not in ref_tasks]
            cln = [r["score"] for r in cl]
            clo = [by[(t, arm)]["score"] for t in tids if (t, arm) in by and t not in removed_old]
            nto = sum(1 for r in rs if r.get("timed_out"))
            ntos = sum(1 for r in rs if r.get("timed_out") and not r["is_error"])
            wf = sum(r["workflow_calls"] for r in rs)
            wft = sum(1 for r in rs if r["subagent_calls"] > 0)
            if wf:
                flags.append("arm %s shows %d workflow calls at %s: the Workflow tool must be disallowed" % (arm, wf, LEN_LABEL.get(ln, ln)))
            lmc = sum(r.get("llm_map_calls", 0) for r in rs)
            lmt = sum(1 for r in rs if r.get("llm_map_calls", 0) > 0)
            lmok = sum(r.get("llm_map_ok_calls", 0) for r in rs)
            if arm == "C" and rs and lmok == 0:
                flags.append("arm C has 0 llm_map successes at %s (%d attempt(s)): the llm_map tool changed nothing at this length" % (LEN_LABEL.get(ln, ln), lmc))
            P("| %s | %s | %d | %s | %d | %s | %d | %s | $%.3f | %.0f | %.1f | %d | %d (%d) | %d / %d (%d) | %d | %d (%d) | %d | %d | %s (%d) |" % (
                LEN_LABEL.get(ln, ln), arm, len(raw), fmt(mean(raw), boot_ci(raw, draws, rng)), len(cln), fmt(mean(cln), boot_ci(cln, draws, rng)),
                len(clo), fmt(mean(clo), boot_ci(clo, draws, rng)),
                mean([task_cost(r) for r in rs]), mean([r["wall_s"] for r in rs]), mean([r["num_turns"] or 0 for r in rs]),
                wf, sum(r["subagent_calls"] for r in rs), wft, lmc, lmok, lmt, sum(r["compactions"] for r in rs), nto, ntos, sum(1 for r in rs if r["is_error"]),
                sum(1 for r in rs if r.get("shared_tmp_ref_count", 0) > 0), fmt(mean(nr), boot_ci(nr, draws, rng)), len(nr)))
    P("")
    P("## Tool errors (main and subagent transcripts)")
    P("")
    P("A tool error is a tool result with `is_error`. Infrastructure errors are Bash errors whose text does not start with `Exit code` (the shell did not start); they must be 0. Denials are permission refusals (the tool worked).")
    P("")
    tools_seen = sorted({t for r in rows for t in (r.get("tool_errors") or {})})
    P("| Length | Arm | Bash calls | Bash started | Infrastructure errors | Denials (permission_denials) | " + " | ".join("%s errors" % t for t in tools_seen) + " |")
    P("|---|---|---|---|---|---|" + "---|" * len(tools_seen))
    for ln in lengths:
        for arm in arms + (["N"] if any(r["arm"] == "N" for r in rows) else []):
            rs = [r for r in rows if r["arm"] == arm and r["length"] == ln]
            if not rs:
                continue
            P("| %s | %s | %d | %d | %d | %d | %s |" % (LEN_LABEL.get(ln, ln), arm, sum(r.get("bash_calls", 0) for r in rs), sum(r.get("bash_started", 0) for r in rs),
                                                       sum(r.get("bash_infra", 0) for r in rs), sum(len(r.get("permission_denials") or []) for r in rs),
                                                       " | ".join(str(sum((r.get("tool_errors") or {}).get(t, 0) for r in rs)) for t in tools_seen)))
    P("")
    P("## Paired difference (same tasks, score of the arm minus score of A)")
    P("")
    P("| Length | Pair | n raw | Raw diff [95% CI] | n clean (new rule) | Clean diff, new rule [95% CI] | n clean (old rule) | Clean diff, old rule [95% CI] |")
    P("|---|---|---|---|---|---|---|---|")
    for ln in lengths:
        tids = [t for t, l in tasks.items() if l == ln]
        for arm in [a for a in arms if a != "A"]:
            pr = [t for t in tids if (t, arm) in by and (t, "A") in by]
            dr = [by[(t, arm)]["score"] - by[(t, "A")]["score"] for t in pr]
            dc = [by[(t, arm)]["score"] - by[(t, "A")]["score"] for t in pr if t not in removed]
            do = [by[(t, arm)]["score"] - by[(t, "A")]["score"] for t in pr if t not in removed_old]
            P("| %s | %s-A | %d | %s | %d | %s | %d | %s |" % (LEN_LABEL.get(ln, ln), arm, len(dr), fmt(mean(dr), boot_ci(dr, draws, rng)), len(dc), fmt(mean(dc), boot_ci(dc, draws, rng)),
                                                          len(do), fmt(mean(do), boot_ci(do, draws, rng))))
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
            lmu = sum(1 for r in rows if r["arm"] == "C" and r.get("llm_map_calls", 0) > 0)
            lmn = sum(1 for r in rows if r["arm"] == "C")
            lmok = sum(r.get("llm_map_ok_calls", 0) for r in rows if r["arm"] == "C")
            lmat = sum(r.get("llm_map_calls", 0) for r in rows if r["arm"] == "C")
            P("- Arm C llm_map use rate: %d of %d tasks; %d attempt(s), %d success(es) (a success is a summary line with 1 or more ok items). At 0 the llm_map tool changed nothing in practice." % (lmu, lmn, lmat, lmok))
            P("- Arm C compactions: %d. At lengths under the compaction window, arm C equals arm A apart from the session-start text; with 0 compactions it tests only that text." % comp)
    P("- Timed out (scored): rows that hit the unit time limit; the number in brackets is how many of them held a labelled final answer in the last assistant text and were scored (marked `timed_out`). A killed session has no result, so its cost is the last cost-state record or 0 (a lower bound).")
    P("- Turns are assistant messages over the whole session (a background wake-up adds turns). The final answer is the last assistant text of the session.")
    P("- Wall time and cost are per task, one `claude -p` call. Cost is `total_cost_usd` (API-equivalent) of the main session plus the llm_map child calls.")
    lm_rows = [r for r in rows if r.get("llm_map_calls", 0) > 0]
    if lm_rows:
        unparsed = sum(1 for r in lm_rows if r.get("llm_map_summaries", 0) < r["llm_map_calls"])
        P("- llm_map cost is read from the `llm_map: ... cost $x` line in the tool results: $%.3f in total over %d unit(s). In %d unit(s) fewer summary lines than calls were found, so the cost is a lower bound there." % (
            sum(r.get("llm_map_cost_usd", 0.0) for r in lm_rows), len(lm_rows), unparsed))
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
    for label, rem in (("new hit rule (2026-11-18)", removed), ("old hit rule", removed_old)):
        tids = [t for t, l in tasks.items() if l == dl and t not in rem]
        npair = len([t for t in tids if (t, "A") in by and (t, "B") in by])
        if npair < 10:
            P("- %s: rule not applied: %d paired clean tasks at 128K (10 or more needed). This run does not decide P4-1." % (label, npair))
            continue
        best, bm, bc = None, -1, 0
        for arm in ("A", "B"):
            rs = [by[(t, arm)] for t in tids if (t, arm) in by]
            if rs and mean([r["score"] for r in rs]) > bm:
                best, bm, bc = arm, mean([r["score"] for r in rs]), mean([task_cost(r) for r in rs])
        est = dec["map_plan_estimate_usd_per_task"]["128K"]
        ratio = bc / est
        if bm <= th["build_score_max"] or ratio > th["build_cost_ratio_min"]:
            verdict = "BUILD llm_map"
        elif bm >= th["no_build_score_min"] and ratio <= th["no_build_cost_ratio_max"]:
            verdict = "DO NOT build llm_map"
        else:
            verdict = "the user decides (between the thresholds)"
        P("- %s: best plain arm at 128K: %s, clean mean %.2f (n %d), cost per task $%.3f = %.2f x the map-plan estimate ($%.2f). Verdict by the rule: **%s**." % (label, best, bm, len(tids), bc, ratio, est, verdict))
    txt = "\n".join(out) + "\n"
    open(os.path.join(d, "summary.md"), "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
