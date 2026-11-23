#!/usr/bin/env python3
"""Mid-run abort gates of the continuity benchmark. The runner calls them after each finished session and after each finished seed.
The thresholds are fixed here and in README.md (ladder), before any data. A gate stops the run with a clear message.
The run can resume from the last finished seed (run.sh resume <run-id>). To switch the gates off you need --ignore-gates --i-know.
"""
import json
import os
import re

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


# Any model name (Haiku, Sonnet, Opus, Fable): the wording is matched, not the model. The AUP link is in every refusal text seen so far.
SAFETY_REFUSAL_RE = re.compile(r"API Error:(.{0,120}(can't help with this|safeguards flagged this message|can't respond to this message)|.{0,600}anthropic\.com/legal/aup)", re.I | re.S)


RUNAWAY_RE = re.compile(r"exceeded the \d+ output token maximum", re.I)


def failure_class(reason):
    """Class of a failed session: safety_refusal (the API refused the content: "API Error: ... can't help with this", for example Details `[bio]`,
    a false positive of a safety filter) or other. A report uses it to tell this failure from a crash or a timeout."""
    if RUNAWAY_RE.search(str(reason or "")):
        return "runaway"        # a reply that hit the output cap ("exceeded the N output token maximum"): a probe outcome, not a crash
    return "safety_refusal" if SAFETY_REFUSAL_RE.search(str(reason or "")) else "other"


def session_reasons(statuses, unit_costs, classes=None):
    """Reasons to stop after any finished session. statuses: {name: status}. unit_costs: {name: (cost USD, estimate USD or None)}."""
    why = []
    fin = [s for s in statuses.values() if s in ("done", "invalid", "failed")]
    failed = sorted(n for n, s in statuses.items() if s == "failed")
    if fin and len(failed) / len(fin) > FAILED_RATE_MAX:
        refused = [n for n in failed if (classes or {}).get(n) == "safety_refusal"]
        why.append("failed-session rate %.0f%% (%d of %d) is above %.0f%%: %s%s" % (100 * len(failed) / len(fin), len(failed), len(fin), 100 * FAILED_RATE_MAX, ", ".join(failed),
                   "; %d safety_refusal (an API safety filter refused the content, not a crash; not retried): %s" % (len(refused), ", ".join(refused)) if refused else ""))
    for n, (c, e) in sorted(unit_costs.items()):
        if e and c > max(UNIT_COST_FACTOR * e, e + UNIT_COST_FLOOR_USD):
            why.append("session %s cost $%.2f is above %.1f x its estimate $%.2f" % (n, c, UNIT_COST_FACTOR, e))
    return why


RUNAWAY_SEED_MAX = 0.10      # runaway probe forks / probe forks of one seed (a runaway fork wastes money)


def fork_id(r):
    return (r.get("seed"), r.get("arm"), r.get("condition"), r.get("type"), r.get("phrasing"), r.get("chunk"), r.get("fork_sid") if r.get("chunk") is None else None)


def flag_rate(rows, flag):
    """(forks with the flag, forks) of rows. A fork is one (seed, arm, condition, type, phrasing, chunk); its rows all carry the same flag."""
    forks = {}
    for r in rows:
        if "runaway" in r or "refused" in r:
            forks[fork_id(r)] = bool(r.get(flag)) or forks.get(fork_id(r), False)
    return sum(1 for v in forks.values() if v), len(forks)


CACHE_WRITE_MAX = 0.20      # cache_write / cache_read of the forks of one session, on average (a fork that shares the cached prefix writes only its probe)
CACHE_MIN_FORKS = 4         # forks of one parent before the in-run guard judges (the first fork of a parent may write the whole prefix)
FORK_COST_RATIO_MAX = 3.0   # average cost of a no-compaction fork / the cost.py estimate of a fork


def cache_write_fraction(calls):
    """calls: rows of calls.jsonl of the forks of ONE session, in order. -> (average of cache_write / cache_read over the forks after the first, number of those forks).
    A fork that shares the cached prefix of its parent reads it and writes about its probe (a few hundred tokens). A fork that misses the cache reads only the
    system prompt (about 20k tokens) and writes the whole history (about 280k tokens): a 14 times higher write than read. Cause seen in run 20261123T033514:
    the parent session was idle for more than the 5 minute cache time, and a fork writes its cache at the END of its own probe, so no other fork can read it."""
    ratios = []
    for c in calls[1:]:
        rd, wr = c.get("cache_read") or 0, c.get("cache_write") or 0
        ratios.append(wr / float(rd) if rd else (float("inf") if wr else 0.0))
    return (sum(ratios) / len(ratios) if ratios else 0.0), len(ratios)


def runaway_rate(rows):
    return flag_rate(rows, "runaway")


REFUSED_SEED_MAX = 0.05      # refused probe forks / probe forks of one seed (a safety filter that refuses often makes the data biased)


def refused_rate(rows):
    return flag_rate(rows, "refused")


HAIKU_INFO_LEVELS = ("L3", "L4")      # the Haiku preflight cannot reason through these chains (Haiku capability; the Sonnet calibration measures them)


def abort_reasons(sessions, rows, est_per_seed, finished_seeds, session_costs=None, expected=None, haiku_l12=False):
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
    # preflight4 (Haiku): the runaway, refused and parse rates count L1 and L2 rows only. Sonnet runs keep every level.
    sel_g = [r for r in sel if r.get("type") not in HAIKU_INFO_LEVELS] if haiku_l12 else sel
    for sd in sorted(fin):
        bad_f, all_f = runaway_rate([r for r in sel_g if r.get("seed") == sd])
        if all_f and bad_f / float(all_f) > RUNAWAY_SEED_MAX:
            why.append("seed %s: runaway rate %.0f%% (%d of %d probe forks hit the output cap) is above %.0f%%: a runaway fork wastes money" % (
                sd, 100.0 * bad_f / all_f, bad_f, all_f, 100 * RUNAWAY_SEED_MAX))
    for sd in sorted(fin):
        bad_f, all_f = refused_rate([r for r in sel_g if r.get("seed") == sd])
        if all_f and bad_f / float(all_f) > REFUSED_SEED_MAX:
            why.append("seed %s: refused rate %.0f%% (%d of %d probe forks refused by a safety filter) is above %.0f%%" % (
                sd, 100.0 * bad_f / all_f, bad_f, all_f, 100 * REFUSED_SEED_MAX))
    nofire = [r for r in sel if r.get("arm") in ("R", "K", "G", "P") and r.get("phrasing", "cued") == "cued" and r.get("auto_recall_fired") is False]
    if nofire:
        forks = sorted({(r["seed"], r["arm"], r["type"], r["fork_sid"]) for r in nofire})
        why.append("arm R or K: %d cued probe fork(s) had no auto-recall fire, for example seed %s arm %s type %s" % (len(forks), forks[0][0], forks[0][1], forks[0][2]))
    graded = [r for r in sel_g if r.get("arm") != "Z"]
    graded = [r for r in graded if not r.get("runaway") and not r.get("refused")]      # runaway and refused rows are counted apart
    badp = [r for r in graded if not r.get("parse_ok")]
    if graded and len(badp) / len(graded) > PARSE_ERROR_RATE_MAX:
        why.append("parse error rate %.1f%% (%d of %d probe rows) is above %.0f%%" % (100 * len(badp) / len(graded), len(badp), len(graded), 100 * PARSE_ERROR_RATE_MAX))
    if expected:
        miss = missing_forks(expected, sel, fin)
        if miss:
            why.append(describe_missing(miss))
    return why


def version_reasons(cfg_version, versions):
    """The Claude Code version must not change inside a run (the updater is off, but check). versions: {session name: version}."""
    seen = {v for v in list(versions.values()) + [cfg_version] if v}
    if len(seen) > 1:
        return ["claude version changed inside the run: %s" % ", ".join(sorted(seen))]
    return []


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
            t = str(c.get("type", "")).lower()
            if "instructions" in t and "mcp" not in t:          # mcp_instructions_delta is a record of MCP server instructions, not a file
                if isinstance(c.get("files"), list):          # the real record shape: files[].path (each file has a type such as Project or User)
                    out += [f["path"] if isinstance(f, dict) and isinstance(f.get("path"), str) and f.get("path") else "?" for f in c["files"]]
                    break
                p = next((c[k] for k in ("path", "filePath", "file_path", "source", "file") if isinstance(c.get(k), str) and c.get(k)), None)
                out.append(p or "?")
                break
    return out


def mcp_instruction_servers(records):
    """Names of the MCP servers whose instructions Claude Code added to the session. The real record (claude 2.1.326) has the type
    'mcp_instructions_delta' inside the key attachment, with the keys type, addedNames (list of server names), addedBlocks (list of text), removedNames."""
    out = []
    for r in records:
        a = r.get("attachment") if isinstance(r, dict) else None
        if isinstance(a, dict) and a.get("type") == "mcp_instructions_delta" and isinstance(a.get("addedNames"), list):
            out += [n for n in a["addedNames"] if isinstance(n, str)]
    return out


def rule_line_sources(records, text):
    """Where does a text (the recall rule sentence) appear in the records of a transcript? Returns a sorted list of labels:
    'mcp_instructions' (an mcp_instructions_delta record), 'hook' (a record whose type holds the word hook), 'other'."""
    found = set()
    for r in records:
        if not isinstance(r, dict) or text not in json.dumps(r):
            continue
        a = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
        t = (str(a.get("type", "")) + " " + str(r.get("type", ""))).lower()
        found.add("mcp_instructions" if "mcp_instructions" in t else "hook" if "hook" in t else "other")
    return sorted(found)


def project_instructions(records, proj):
    """Instruction paths that are inside the project folder of the session (expected: none, the bench folders hold no CLAUDE.md)."""
    base = os.path.realpath(proj)
    return [p for p in instruction_paths(records) if p != "?" and (os.path.realpath(p) == base or os.path.realpath(p).startswith(base + os.sep))]


def foreign_instructions(records, proj):
    """Instruction paths that are not inside the project folder of the session."""
    base = os.path.realpath(proj)
    bad = []
    for p in instruction_paths(records):
        if p == "?" or not (os.path.realpath(p) == base or os.path.realpath(p).startswith(base + os.sep)):
            bad.append(p)
    return bad
