#!/usr/bin/env python3
"""Mid-run abort gates of the continuity benchmark. The runner calls them after each finished session and after each finished seed.
The thresholds are fixed here and in README.md (ladder), before any data. A gate stops the run with a clear message.
The run can resume from the last finished seed (run.sh resume <run-id>). To switch the gates off you need --ignore-gates --i-know.
"""
import os

INVALID_RATE_MAX = 0.10      # invalid sessions / finished sessions
FAILED_RATE_MAX = 0.10       # failed sessions / finished sessions (done, invalid or failed). A failed session never finishes its seed.
COST_RATIO_MAX = 1.3         # spent / estimate so far (after each seed)
UNIT_COST_FACTOR = 2.0       # one session: cost above this factor times its own estimate
UNIT_COST_FLOOR_USD = 0.25   # ... and above its estimate plus this amount (small sessions are noisy)
PARSE_ERROR_RATE_MAX = 0.02  # probe rows that did not parse / probe rows (arm Z excluded: it may answer in prose)
DEFAULT_BUDGET_FACTOR = 1.5  # default budget of a preset: this factor times the estimate of the whole run


def dedupe_rows(rows):
    """A session that restarted (its temp folder was lost) writes its rows again with a higher `attempt`. Keep the rows of the
    highest attempt of each (seed, arm, condition). Rows without `attempt` (all old runs) come back unchanged."""
    if not any(r.get("attempt") for r in rows):
        return rows
    top = {}
    for r in rows:
        k = (r.get("seed"), r.get("arm"), r.get("condition", "compact"))
        top[k] = max(top.get(k, 0), r.get("attempt") or 0)
    return [r for r in rows if (r.get("attempt") or 0) == top[(r.get("seed"), r.get("arm"), r.get("condition", "compact"))]]


def fork_keys(rows):
    return {(r.get("seed"), r.get("arm"), r.get("condition", "compact"), r.get("phrasing", "cued"), r.get("type"), r.get("n_target")) for r in rows}


def missing_forks(expected, rows, seeds):
    """Probe fork classes that left no row. expected: {"exact": [(seed, arm, cond, phrasing, type, n_target)], "classes": [(seed, arm, cond, phrasing)]}.
    Returns a list of missing keys (a class key starts with the word 'class'). A phrasing that never ran (for example nonliteral) shows up here."""
    seeds = set(seeds)
    have = fork_keys(rows)
    have_cls = {(k[0], k[1], k[2], k[3]) for k in have}
    miss = [tuple(k) for k in expected.get("exact", []) if k[0] in seeds and tuple(k) not in have]
    miss += [("class",) + tuple(c) for c in expected.get("classes", []) if c[0] in seeds and tuple(c) not in have_cls]
    return miss


def describe_missing(miss, limit=4):
    def one(k):
        if k[0] == "class":
            return "seed %s arm %s %s %s" % (k[1], k[2], k[3], k[4])
        return "seed %s arm %s %s %s type %s" % (k[0], k[1], k[2], k[3], k[4])
    return "%d probe fork(s) have no row, for example: %s" % (len(miss), "; ".join(one(k) for k in miss[:limit]))


def session_reasons(statuses, unit_costs):
    """Reasons to stop after any finished session. statuses: {name: status}. unit_costs: {name: (cost USD, estimate USD or None)}."""
    why = []
    fin = [s for s in statuses.values() if s in ("done", "invalid", "failed")]
    failed = sorted(n for n, s in statuses.items() if s == "failed")
    if fin and len(failed) / len(fin) > FAILED_RATE_MAX:
        why.append("failed-session rate %.0f%% (%d of %d) is above %.0f%%: %s" % (100 * len(failed) / len(fin), len(failed), len(fin), 100 * FAILED_RATE_MAX, ", ".join(failed)))
    for n, (c, e) in sorted(unit_costs.items()):
        if e and c > max(UNIT_COST_FACTOR * e, e + UNIT_COST_FLOOR_USD):
            why.append("session %s cost $%.2f is above %.1f x its estimate $%.2f" % (n, c, UNIT_COST_FACTOR, e))
    return why


def abort_reasons(sessions, rows, est_per_seed, finished_seeds, session_costs=None, expected=None):
    """Reasons to stop after a finished seed. Empty list: go on.
    sessions: one summary dict for each finished session (keys seed, status, nc_valid, name).
    rows: result rows. est_per_seed: USD of one seed, or None (no cost gate). finished_seeds: seeds with every unit finished.
    session_costs: {session name: USD} of the sessions of the finished seeds. expected: see missing_forks (the row-count check)."""
    fin = set(finished_seeds)
    if not fin:
        return []
    rows = dedupe_rows(rows)
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
    if expected:
        miss = missing_forks(expected, sel, fin)
        if miss:
            why.append(describe_missing(miss))
    return why


def instruction_paths(records):
    """Paths of the instruction files (CLAUDE.md and rules) that Claude Code loaded, from the records of one transcript.
    A record counts when its type, or the type of its attachment, holds the word 'instructions'. The record shape of Claude Code is not
    documented. Each record is read for a path in any of the keys path, filePath, file_path, source, file. A record with no path gives
    the path '?' (unknown): the caller treats it as a failure."""
    out = []
    for r in records:
        if not isinstance(r, dict):
            continue
        cands = [r] + [r[k] for k in ("attachment", "data", "message") if isinstance(r.get(k), dict)]
        for c in cands:
            if "instructions" in str(c.get("type", "")).lower():
                p = next((c[k] for k in ("path", "filePath", "file_path", "source", "file") if isinstance(c.get(k), str) and c.get(k)), None)
                out.append(p or "?")
                break
    return out


def foreign_instructions(records, proj):
    """Instruction paths that are not inside the project folder of the session."""
    base = os.path.realpath(proj)
    bad = []
    for p in instruction_paths(records):
        if p == "?" or not (os.path.realpath(p) == base or os.path.realpath(p).startswith(base + os.sep)):
            bad.append(p)
    return bad
