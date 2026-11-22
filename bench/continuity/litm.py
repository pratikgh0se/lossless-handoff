#!/usr/bin/env python3
"""Report section of the lost-in-the-middle design: position, phrasing, condition, auto-recall (see README.md).

section(rows, sessions, th, draws) returns (markdown lines, dict). It returns no lines when the rows hold no data of the
new design (no neutral rows, no no-compaction rows, no arm R). So the report of an old run does not change.
It uses the same bootstrap as report.py: the seed is the unit, 95% interval, the draws and the RNG seed from thresholds.json.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCORED = ["R", "D", "X", "S", "E", "C"]      # scored types that have one plant turn, so a position tag. M has no position.
POS = ["start", "middle", "end"]
NAMES = {"A": "A plain", "B": "B Lossless Handoff", "R": "R auto-recall", "Z": "Z no history", "O": "O no compaction"}


def cond_of(r):
    return r.get("condition", "compact")


def phr_of(r):
    return r.get("phrasing", "cued")


def is_new(rows):
    return any(phr_of(r) == "neutral" or cond_of(r) == "nocompact" or r["arm"] == "R" for r in rows)


class Cells:
    """Per-seed (correct, total) vectors of the rows that pass a filter."""

    def __init__(self, rows):
        self.rows = [r for r in rows if r["type"] in SCORED]
        self.seeds = sorted({r["seed"] for r in self.rows})

    def vec(self, flt):
        cs, ts = [], []
        for s in self.seeds:
            sel = [r for r in self.rows if r["seed"] == s and flt(r)]
            cs.append(sum(1 for r in sel if r["correct"]))
            ts.append(len(sel))
        return cs, ts


def full_n(r, ns):
    """Compaction rows at the largest N of the run (it asks all four plant epochs when N >= 5)."""
    return bool(ns) and r["n_target"] == max(ns)


def section(rows, sessions, th, draws, helpers=None):
    from report import Boot, fmt_cell, fmt_diff, pct
    if not is_new(rows):
        return [], {}
    lt = th.get("litm", {})
    cells = Cells(rows)
    S = len(cells.seeds)
    if not S:
        return [], {}
    boot = Boot(S, draws or th["bootstrap_draws"], th["bootstrap_rng_seed"], th["ci_level"])
    arms = [a for a in ("A", "B", "R", "Z", "O") if any(r["arm"] == a for r in rows)]
    md, out = [], {"tables": {}, "hypotheses": []}
    ns = sorted({r["n_target"] for r in rows if cond_of(r) == "compact" and r["n_target"] > 0})

    def cell(flt):
        cs, ts = cells.vec(flt)
        return cs, ts, fmt_cell(sum(cs), sum(ts), boot.one(cs, ts) if sum(ts) else (None, None))

    def base(arm, cond, phr):
        # compaction: only probes with all four plant epochs. no compaction: the one probe at the end.
        return lambda r: r["arm"] == arm and cond_of(r) == cond and phr_of(r) == phr and (full_n(r, ns) if cond == "compact" else True)

    md.append("## Lost in the middle, phrasing and auto-recall (probe design 2)\n")
    md.append("Types: %s (M has no single plant turn, so no position). Position = third of the nominal session timeline of the plant turn "
              "(start, middle, end). Compaction rows (condition `compact`): probes at the largest N only, and the position is nominal, so read it as 'compactions survived', not as a distance in tokens. "
              "No-compaction rows: the one probe after the full session. Cell = rate [95%% bootstrap interval over seeds] (correct/total).\n" % ",".join(SCORED))

    # ---- hook fire rate
    md.append("### Table L0: did the auto-recall hook fire? (arm R, from auto-recall.jsonl of the arm)\n")
    md.append("The hook shows at most 3 hits (TOP_HITS) per prompt. A probe asks up to 4 facts, so one fire cannot cover every fact. "
              "\"Fired with hits\" counts forks whose log line has hits above 0.\n")
    md.append("| Condition | Phrasing | Probe forks | Fired | Rate | Fired with hits > 0 | Timed out |")
    md.append("|---|---|---|---|---|---|---|")
    fire = {}
    for cond in ("compact", "nocompact"):
        for phr in ("cued", "neutral"):
            forks = {}
            for r in rows:
                if r["arm"] == "R" and cond_of(r) == cond and phr_of(r) == phr and r.get("auto_recall_fired") is not None:
                    forks[(r["seed"], r["n_target"], r["type"], r["fork_sid"])] = (
                        bool(r["auto_recall_fired"]), (r.get("auto_recall_hits") or 0) > 0, bool(r.get("auto_recall_timed_out")))
            if forks:
                f, h, to = (sum(v[i] for v in forks.values()) for i in range(3))
                n_ = len(forks)
                fire["%s/%s" % (cond, phr)] = [f, n_, h, to]
                md.append("| %s | %s | %d | %d | %s | %d (%s) | %d (%s) |" % (cond, phr, n_, f, pct(f / n_), h, pct(h / n_), to, pct(to / n_)))
    if not fire:
        md.append("| no data | | | | | | |")
    else:
        tc = [sum(v[0] for k, v in fire.items() if k.endswith("/" + p)) for p in ("cued", "neutral")]
        tt = [sum(v[1] for k, v in fire.items() if k.endswith("/" + p)) for p in ("cued", "neutral")]
        md.append("\nThe hook fired on %s of cued and %s of neutral probes (arm R)." % (
            "%s (%d/%d)" % (pct(tc[0] / tt[0]), tc[0], tt[0]) if tt[0] else "no data",
            "%s (%d/%d)" % (pct(tc[1] / tt[1]), tc[1], tt[1]) if tt[1] else "no data"))
    out["tables"]["fire"] = fire
    md.append("")

    # ---- position table
    md.append("### Table L1: accuracy by position (arm, condition, phrasing)\n")
    md.append("| Arm | Condition | Phrasing | start | middle | end | middle minus mean(start, end) |")
    md.append("|---|---|---|---|---|---|---|")
    t1 = {}
    for a in arms:
        for cond in ("compact", "nocompact"):
            for phr in ("cued", "neutral"):
                if a in ("Z", "O") and (cond == "nocompact" or phr == "neutral"):
                    continue
                f0 = base(a, cond, phr)
                if not any(f0(r) for r in cells.rows):
                    continue
                row, vecs = [], {}
                for p in POS:
                    cs, ts, txt = cell(lambda r, p=p, f0=f0: f0(r) and r.get("position") == p)
                    row.append(txt)
                    vecs[p] = (cs, ts)
                    t1["%s/%s/%s/%s" % (a, cond, phr, p)] = [sum(cs), sum(ts)]
                d = mean_ends_diff(boot, vecs["middle"], vecs["start"], vecs["end"])
                t1["%s/%s/%s/mid_minus_ends" % (a, cond, phr)] = [d[0], d[1], d[2], d[3]]
                md.append("| %s | %s | %s | %s | %s |" % (NAMES[a], "compact (compactions survived)" if cond == "compact" else cond, phr, " | ".join(row), fmt_diff(d)))
    out["tables"]["position"] = t1
    md.append("")

    # ---- phrasing table
    md.append("### Table L2: accuracy by phrasing (all positions, types R D X S E only, so cued and neutral ask the same facts)\n")
    md.append("| Arm | Condition | cued | neutral | neutral minus cued |")
    md.append("|---|---|---|---|---|")
    NT = ("R", "D", "X", "S", "E")
    t2 = {}
    for a in [x for x in arms if x not in ("Z", "O")]:
        for cond in ("compact", "nocompact"):
            fc, fn = base(a, cond, "cued"), base(a, cond, "neutral")
            if not any(fn(r) for r in cells.rows):
                continue
            cc, ct, ctx_ = cell(lambda r: fc(r) and r["type"] in NT)
            nc_, nt, ntx = cell(lambda r: fn(r) and r["type"] in NT)
            d = boot.diff(nc_, nt, cc, ct)
            t2["%s/%s" % (a, cond)] = {"cued": [sum(cc), sum(ct)], "neutral": [sum(nc_), sum(nt)]}
            md.append("| %s | %s | %s | %s | %s |" % (NAMES[a], cond, ctx_, ntx, fmt_diff(d)))
    out["tables"]["phrasing"] = t2
    md.append("\nL2 is not a pure cue contrast. The neutral head also drops \"Answer from this session's history only\" and adds a rule to answer null when unsure. "
              "The cued head of design 2 adds a cue sentence. So L2 compares two prompt styles, not only the cue.\n")

    # ---- curves: compaction vs no compaction
    md.append("### Table L3: position curve, compaction minus no compaction (cued probes, paired by seed)\n")
    md.append("| Arm | start | middle | end |")
    md.append("|---|---|---|---|")
    t3 = {}
    for a in [x for x in arms if x not in ("Z", "O")]:
        fcm, fnc = base(a, "compact", "cued"), base(a, "nocompact", "cued")
        if not (any(fcm(r) for r in cells.rows) and any(fnc(r) for r in cells.rows)):
            continue
        row = []
        for p in POS:
            cc, ct = cells.vec(lambda r, p=p: fcm(r) and r.get("position") == p)
            nc_, nt = cells.vec(lambda r, p=p: fnc(r) and r.get("position") == p)
            d = boot.diff(cc, ct, nc_, nt)
            t3["%s/%s" % (a, p)] = [d[0], d[1], d[2], d[3]]
            row.append(fmt_diff(d))
        md.append("| %s | %s |" % (NAMES[a], " | ".join(row)))
    out["tables"]["curve_diff"] = t3
    md.append("")

    # ---- hypotheses
    md.append("### Table L4: pre-registered hypotheses (thresholds in thresholds.json, committed before any data)\n")
    md.append("| Hypothesis | Threshold | Measured | Result |")
    md.append("|---|---|---|---|")

    def hyp(name, thr, measured, result):
        md.append("| %s | %s | %s | %s |" % (name, thr, measured, result))
        out["hypotheses"].append({"name": name, "threshold": thr, "measured": measured, "result": result})

    # H1: A, no compaction: the middle is worse than the mean of start and end. TOO EASY above the ceiling.
    f1 = base("A", "nocompact", "cued")
    vm, vs, ve = (cells.vec(lambda r, p=p: f1(r) and r.get("position") == p) for p in (POS[1], POS[0], POS[2]))
    d = mean_ends_diff(boot, vm, vs, ve)
    p = lt.get("h1_middle_minus_ends_max_points", -10)
    ceil = lt.get("h1_ceiling_max_accuracy", 0.95)
    rs, re_ = (sum(v[0]) / sum(v[1]) if sum(v[1]) else None for v in (vs, ve))
    ends_mean = None if rs is None or re_ is None else (rs + re_) / 2
    if d[0] is None:
        res = "NO DATA"
    elif ends_mean > ceil:
        res = "TOO EASY: no claim (start/end accuracy %s is above %s)" % (pct(ends_mean), pct(ceil))
    else:
        res = "SUPPORTED" if 100 * d[0] <= p and d[2] < 0 else "NOT SUPPORTED"
    hyp("H1 lost in the middle: arm A, no compaction, cued: middle minus mean(start, end)",
        "<= %+d pts and CI upper bound < 0; TOO EASY if mean(start, end) > %s" % (p, pct(ceil)), fmt_diff(d), res)
    # H2: compaction loses facts that long context keeps
    fcm, fnc = base("A", "compact", "cued"), base("A", "nocompact", "cued")
    d = boot.diff(*cells.vec(fcm), *cells.vec(fnc))
    p = lt.get("h2_compaction_minus_nocompaction_max_points", -15)
    hyp("H2 (%s) compaction loses facts: arm A cued, compaction minus no compaction" % lt.get("h2_label", "expected, not blind; mixes compaction with session length"), "<= %+d pts and CI upper bound < 0" % p, fmt_diff(d),
        "NO DATA" if d[0] is None else ("SUPPORTED" if 100 * d[0] <= p and d[2] < 0 else "NOT SUPPORTED"))
    # H3: R beats B on cued
    d = boot.diff(*cells.vec(base("R", "compact", "cued")), *cells.vec(base("B", "compact", "cued")))
    p = lt.get("h3_r_minus_b_cued_min_points", 5)
    hyp("H3 auto-recall helps on cued probes: R minus B, compaction, cued", ">= %+d pts and CI lower bound > 0" % p, fmt_diff(d),
        "NO DATA" if d[0] is None else ("SUPPORTED" if 100 * d[0] >= p and d[1] > 0 else "NOT SUPPORTED"))
    # H4: R does not beat B on neutral. INVALID if the hook fired on neutral probes.
    d = boot.diff(*cells.vec(lambda r: base("R", "compact", "neutral")(r)), *cells.vec(lambda r: base("B", "compact", "neutral")(r)))
    p = lt.get("h4_r_minus_b_neutral_no_gain_below_points", 5)
    nmax4 = lt.get("h4_invalid_if_neutral_fire_rate_above", 0.05)
    nfire = [sum(v[0] for k, v in fire.items() if k.endswith("/neutral")), sum(v[1] for k, v in fire.items() if k.endswith("/neutral"))]
    if d[0] is None:
        res = "NO DATA"
    elif nfire[1] and nfire[0] / nfire[1] > nmax4:
        res = "INVALID (the hook fired on %s of neutral probes, above %s)" % (pct(nfire[0] / nfire[1]), pct(nmax4))
    elif 100 * d[0] >= p and d[1] > 0:
        res = "REFUTED (R gains on neutral probes)"
    elif 100 * d[2] < p:
        res = "no gain of %d points or more" % p
    else:
        res = "INCONCLUSIVE"
    hyp("H4 no gain without a cue: R minus B, compaction, neutral", "CI upper bound < %+d pts (no gain of %d points or more); INVALID if neutral fire rate > %s" % (p, p, pct(nmax4)), fmt_diff(d), res)
    # H5: the mechanism
    cf = [sum(v[0] for k, v in fire.items() if k.endswith("/cued")), sum(v[1] for k, v in fire.items() if k.endswith("/cued"))]
    nf = [sum(v[0] for k, v in fire.items() if k.endswith("/neutral")), sum(v[1] for k, v in fire.items() if k.endswith("/neutral"))]
    cmin, nmax = lt.get("h5_fire_rate_cued_min", 0.90), lt.get("h5_fire_rate_neutral_max", 0.05)
    if not cf[1] or not nf[1]:
        res, meas = "NO DATA", "no arm R data"
    else:
        res = "SUPPORTED" if cf[0] / cf[1] >= cmin and nf[0] / nf[1] <= nmax else "NOT SUPPORTED"
        meas = "cued %s (%d/%d), neutral %s (%d/%d)" % (pct(cf[0] / cf[1]), cf[0], cf[1], pct(nf[0] / nf[1]), nf[0], nf[1])
    hyp("H5 (%s) the hook fires on cued probes and not on neutral probes" % lt.get("h5_label", "plumbing check"),
        "cued >= %s and neutral <= %s" % (pct(cmin), pct(nmax)), meas, res)
    md.append("\nA hypothesis that is NOT SUPPORTED or REFUTED stays in the report. The thresholds do not change after a run.\n")

    # ---- gate: no-compaction sessions must not compact
    nc = [s for s in sessions if s.get("condition") == "nocompact"]
    if nc:
        bad = [s for s in nc if s.get("boundaries", 0) != 0 or s.get("missed") or s.get("nc_valid") is False or s.get("status") == "invalid"]
        md.append("Invalid no-compaction sessions (a compaction, a cleared tool-result marker, or a last-turn context below the expected size; must be 0): **%d of %d**%s\n" % (
            len(bad), len(nc), "" if not bad else " (INVALID: drop these sessions from every claim)"))
        for s_ in bad:
            if s_.get("nc_reasons"):
                md.append("- %s: %s" % (s_["name"], "; ".join(s_["nc_reasons"])))
        out["nc_invalid"] = [s["name"] for s in bad]
    return md, out


def mean_ends_diff(boot, mid, start, end):
    """middle rate minus the mean of the start rate and the end rate. Each argument is (correct list, total list) over seeds.
    The seed is the resampling unit (same draws as every other interval). Returns (point, lo, hi, seeds with all three cells)."""
    def stat(idx):
        r = []
        for cs, ts in (mid, start, end):
            t = sum(ts[i] for i in idx)
            if not t:
                return None
            r.append(sum(cs[i] for i in idx) / t)
        return r[0] - (r[1] + r[2]) / 2
    allidx = list(range(len(mid[0])))
    point = stat(allidx)
    lo, hi = boot._ci([stat(d) for d in boot.draws])
    return point, lo, hi, sum(1 for i in allidx if mid[1][i] and start[1][i] and end[1][i])


def _ends(cells, f0):
    """Per-seed (correct, total) of start plus end, as two lists."""
    sc, st = cells.vec(lambda r: f0(r) and r.get("position") == "start")
    ec, et = cells.vec(lambda r: f0(r) and r.get("position") == "end")
    return [a + b for a, b in zip(sc, ec)], [a + b for a, b in zip(st, et)]
