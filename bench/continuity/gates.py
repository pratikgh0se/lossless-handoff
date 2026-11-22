#!/usr/bin/env python3
"""Mid-run abort gates of the continuity benchmark. The runner calls abort_reasons() after each finished seed.
The thresholds are fixed here and in README.md (ladder), before any data. A gate stops the run with a clear message.
The run can resume from the last finished seed (run.sh resume <run-id>; add --ignore-gates only after you fixed the cause).
"""

INVALID_RATE_MAX = 0.10      # invalid sessions / finished sessions
COST_RATIO_MAX = 1.3         # spent / estimate so far
PARSE_ERROR_RATE_MAX = 0.02  # probe rows that did not parse / probe rows (arm Z excluded: it may answer in prose)


def abort_reasons(sessions, rows, est_per_seed, finished_seeds, session_costs=None):
    """Reasons to stop. Empty list: go on.
    sessions: one summary dict for each finished session (keys seed, status, nc_valid, name).
    rows: result rows. est_per_seed: USD of one seed, or None (no cost gate). finished_seeds: seeds with every unit finished.
    session_costs: {session name: USD} of the sessions of the finished seeds."""
    fin = set(finished_seeds)
    if not fin:
        return []
    why = []
    mine = [s for s in sessions if s.get("seed") in fin]
    bad = [s for s in mine if s.get("status") == "invalid" or s.get("nc_valid") is False]
    if mine and len(bad) / len(mine) > INVALID_RATE_MAX:
        why.append("invalid-session rate %.0f%% (%d of %d) is above %.0f%%: %s" % (
            100 * len(bad) / len(mine), len(bad), len(mine), 100 * INVALID_RATE_MAX, ", ".join(sorted(s.get("name", "?") for s in bad))))
    if est_per_seed and session_costs is not None:
        spent, est = sum(session_costs.values()), est_per_seed * len(fin)
        if spent > COST_RATIO_MAX * est:
            why.append("cost $%.2f is above %.1f x the estimate so far ($%.2f for %d seed(s))" % (spent, COST_RATIO_MAX, est, len(fin)))
    sel = [r for r in rows if r.get("seed") in fin]
    nofire = [r for r in sel if r.get("arm") in ("R", "K") and r.get("phrasing", "cued") == "cued" and r.get("auto_recall_fired") is False]
    if nofire:
        forks = sorted({(r["seed"], r["arm"], r["type"], r["fork_sid"]) for r in nofire})
        why.append("arm R or K: %d cued probe fork(s) had no auto-recall fire, for example seed %s arm %s type %s" % (len(forks), forks[0][0], forks[0][1], forks[0][2]))
    graded = [r for r in sel if r.get("arm") != "Z"]
    badp = [r for r in graded if not r.get("parse_ok")]
    if graded and len(badp) / len(graded) > PARSE_ERROR_RATE_MAX:
        why.append("parse error rate %.1f%% (%d of %d probe rows) is above %.0f%%" % (100 * len(badp) / len(graded), len(badp), len(graded), 100 * PARSE_ERROR_RATE_MAX))
    return why
