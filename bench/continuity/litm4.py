#!/usr/bin/env python3
"""Report section of probe design 4 (update chains, no-compaction condition). report.py calls section4().
Rows of design 4 have the type L1 to L4 (a level), the fields m1, m2, m3, error_class, lag and position (the third that holds the chain).
Primary cell: the (metric, level) that the calibration picked and committed in thresholds.json (litm.design4.pick). If none is committed,
the provisional cell is M1 at L3 and the report says so. Hypotheses use the primary cell, arm A or the named arms, no compaction."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design4  # noqa: E402

PROVISIONAL = ("m1", "L3")


def is_d4(r):
    return str(r.get("type", "")).startswith("L") and r.get("condition", "compact") == "nocompact"


def pick_cell(th):
    p = (th.get("litm", {}).get("design4", {}) or {}).get("pick") or {}
    if p.get("metric") and p.get("level"):
        return (p["metric"].lower(), p["level"]), True
    return PROVISIONAL, False


class Vec:
    def __init__(self, rows):
        self.rows = rows
        self.seeds = sorted({r["seed"] for r in rows})

    def vec(self, flt, metric):
        cs, ts = [], []
        for s in self.seeds:
            sel = [r for r in self.rows if r["seed"] == s and flt(r)]
            cs.append(sum(1 for r in sel if r.get(metric) is True))
            ts.append(len(sel))
        return cs, ts


def pc(x):
    return "%.0f%%" % (100 * x)


def cellfmt(c, t):
    return "no data" if not t else "%s (%d/%d)" % (pc(c / float(t)), c, t)


def section4(rows_all, sessions, th, draws):
    from report import Boot, fmt_diff
    from litm import mean_ends_diff
    rows = [r for r in rows_all if is_d4(r)]
    if not rows:
        return [], {}
    lt = th.get("litm", {})
    d4t = lt.get("design4", {})
    V = Vec(rows)
    boot = Boot(max(1, len(V.seeds)), draws or th["bootstrap_draws"], th["bootstrap_rng_seed"], th["ci_level"])
    arms = [a for a in ("A", "B", "R", "K", "Z", "O") if any(r["arm"] == a for r in rows)]
    (metric, level), committed = pick_cell(th)
    md = ["### Design 4: update chains with interference (no-compaction condition)", "",
          "Levels L1 to L4 are in one session. A chain is tight: all its statements are in one third, so the position is that third. "
          "M1 = `current` exact. M2 = M1 and `previous`. M3 = M2 and `replaced` as an exact set. No partial credit. An abstain (null `current`) is wrong and counted apart.", ""]
    md.append("Primary cell: **%s %s**%s." % (metric.upper(), level, "" if committed else " (provisional: the calibration pick is not committed in thresholds.json)"))
    out = {"primary": [metric, level], "committed": committed, "hypotheses": [], "gates": []}
    # table D4-1: per arm, level x phrasing x position x metric
    for arm in arms:
        md += ["", "#### Table D4-1 (%s): accuracy per level, phrasing and position (cell = M1 / M2 / M3, correct/total)" % arm, "",
               "| Level | Phrasing | Start | Middle | End | All positions |", "|---|---|---|---|---|---|"]
        for lv in design4.D4_LEVELS + ("L5",):
            if not any(r["arm"] == arm and r["type"] == lv for r in rows):
                continue
            for ph in design4.PHRASINGS:
                parts = []
                for pos in ("start", "middle", "end", None):
                    sel = [r for r in rows if r["arm"] == arm and r["type"] == lv and r.get("phrasing") == ph and (pos is None or r.get("position") == pos)]
                    if not sel:
                        parts.append("no data")
                        continue
                    parts.append(" / ".join("%s" % pc(sum(1 for r in sel if r.get(m) is True) / float(len(sel))) for m in design4.METRICS) + " (%d)" % len(sel))
                if any(p != "no data" for p in parts):
                    md.append("| %s | %s | %s |" % (lv, ph, " | ".join(parts)))
    # table D4-2: error classes
    md += ["", "#### Table D4-2: error classes of the wrong `current` answers (count and share of all wrong rows; lag = changes between the stale value and the current one)", "",
           "| Arm | wrong | " + " | ".join(design4.ERROR_CLASSES) + " | mean stale lag |", "|---|---|" + "---|" * (len(design4.ERROR_CLASSES) + 1)]
    for arm in arms:
        mix = design4.error_mix(rows, arm)
        wrong = sum(mix.values())
        lags = [r["lag"] for r in rows if r["arm"] == arm and r.get("error_class") == "stale" and isinstance(r.get("lag"), int)]
        md.append("| %s | %d | %s | %s |" % (arm, wrong, " | ".join("%d (%s)" % (mix[c], pc(mix[c] / float(wrong)) if wrong else "0%") for c in design4.ERROR_CLASSES),
                                            "%.1f" % (sum(lags) / float(len(lags))) if lags else "n/a"))
    # gates
    scripts = []
    for s in V.seeds:
        try:
            scripts.append(design4.build_litm4(s))
        except Exception:       # a seed that this version cannot build: skip the solver gate for it
            pass
    gl = design4.gate_lines(rows, scripts)
    md += ["", "#### Table D4-3: design-4 gates", "", "| Gate | Result | Detail |", "|---|---|---|"]
    for name, st, det in gl:
        md.append("| %s | %s | %s |" % (name, st, det))
        out["gates"].append({"name": name, "status": st})
    # hypotheses
    md += ["", "#### Table D4-4: pre-registered hypotheses of design 4 (thresholds in thresholds.json, committed before any data)", "",
           "| Hypothesis | Threshold | Measured | Result |", "|---|---|---|---|"]

    def add(name, thr, measured, result):
        md.append("| %s | %s | %s | %s |" % (name, thr, measured, result))
        out["hypotheses"].append({"name": name, "result": result})

    def v(arm, ph, lvl=level, met=metric, pos=None):
        return V.vec(lambda r: r["arm"] == arm and r["type"] == lvl and (ph is None or r.get("phrasing") == ph) and (pos is None or r.get("position") == pos), met)

    def ends(arm, ph):
        a, b = v(arm, ph, pos="start"), v(arm, ph, pos="end")
        return [x + y for x, y in zip(a[0], b[0])], [x + y for x, y in zip(a[1], b[1])]

    ceil = d4t.get("h1_ceiling_max_accuracy", design4.CEILING)
    # H1 primary: the phrasings pooled (3 chains for each position and seed is too few for one phrasing). The rows of each phrasing stay as secondary.
    for name, ph in (("H1 lost in the middle, primary (arm A, no compaction, primary cell, the 3 phrasings pooled): middle minus mean(start, end)", None),
                     ("H1c secondary, cued only: middle minus mean(start, end)", "cued"),
                     ("H1s secondary, neutral only: middle minus mean(start, end)", "neutral"),
                     ("H1n secondary, non-literal only: middle minus mean(start, end)", "nonliteral")):
        mid, st_, en_ = v("A", ph, pos="middle"), v("A", ph, pos="start"), v("A", ph, pos="end")
        d = mean_ends_diff(boot, mid, st_, en_)
        se, te = ends("A", ph)
        ends_acc = sum(se) / float(sum(te)) if sum(te) else None
        if d[0] is None:
            res = "no data"
        elif ends_acc is not None and ends_acc > ceil:
            res = "TOO EASY: no claim (start/end accuracy %s is above %s)" % (pc(ends_acc), pc(ceil))
        else:
            res = "SUPPORTED" if (d[0] <= -0.10 and d[2] is not None and d[2] < 0) else "NOT SUPPORTED"
        add(name, "<= -10 pts and CI upper bound < 0; TOO EASY if mean(start, end) > %s" % pc(ceil), fmt_diff(d), res)
    # H8 dose: A, L1 minus L4, M1
    c1, t1 = V.vec(lambda r: r["arm"] == "A" and r["type"] == "L1", "m1")
    c4, t4 = V.vec(lambda r: r["arm"] == "A" and r["type"] == "L4", "m1")
    d = boot.diff(c1, t1, c4, t4)
    add("H8 dose (arm A, M1): L1 minus L4", ">= +15 pts and CI lower bound > 0", fmt_diff(d),
        "no data" if d[0] is None else ("SUPPORTED" if d[0] >= 0.15 and d[1] is not None and d[1] > 0 else "NOT SUPPORTED"))
    # H9 interference: share of A's wrong answers (abstains excluded) that are stale, lure or sibling, primary level
    wrong = [r for r in rows if r["arm"] == "A" and r["type"] == level and r.get("m1") is not True and r.get("error_class") not in ("abstain", "unreadable", None)]
    inter = [r for r in wrong if r.get("error_class") in ("stale", "lure", "sibling")]
    add("H9 interference (arm A, primary level, abstains excluded): share of wrong answers that are stale, lure or sibling", ">= 60%",
        "no wrong answers" if not wrong else "%s (%d/%d)" % (pc(len(inter) / float(len(wrong))), len(inter), len(wrong)),
        "no data" if not wrong else ("SUPPORTED" if len(inter) / float(len(wrong)) >= 0.60 else "NOT SUPPORTED"))
    # H10 length: O minus A
    co, to = v("O", None)
    ca, ta = v("A", None)
    d = boot.diff(co, to, ca, ta)
    hm = th.get("headroom_min_points", 15)
    add("H10 length (primary cell, phrasings pooled): O minus A", ">= +%d pts (headroom_min_points)" % hm, fmt_diff(d),
        "no data" if d[0] is None else ("SUPPORTED" if d[0] * 100 >= hm else "NOT SUPPORTED"))
    # H6 and H7 (kept): K minus B
    ck, tk = V.vec(lambda r: r["arm"] == "K" and r["type"] == level and r.get("phrasing") == "neutral", "used_lookup")
    cb, tb = V.vec(lambda r: r["arm"] == "B" and r["type"] == level and r.get("phrasing") == "neutral", "used_lookup")
    d = boot.diff(ck, tk, cb, tb)
    add("H6 search rate: K minus B, no compaction, neutral probes (primary level)", "> 0 pts and CI lower bound > 0", fmt_diff(d),
        "no data" if d[0] is None else ("SUPPORTED" if d[0] > 0 and d[1] is not None and d[1] > 0 else "NOT SUPPORTED"))
    ck, tk = v("K", "neutral")
    cb, tb = v("B", "neutral")
    d = boot.diff(ck, tk, cb, tb)
    b_acc = sum(cb) / float(sum(tb)) if sum(tb) else None
    h7c = lt.get("h7_ceiling_max_accuracy", 0.95)
    res = "no data" if d[0] is None else ("TOO EASY: no claim" if b_acc is not None and b_acc > h7c else ("SUPPORTED" if d[0] >= 0.05 and d[1] is not None and d[1] > 0 else "NOT SUPPORTED"))
    add("H7 accuracy: K minus B, no compaction, neutral (primary cell)", ">= +5 pts and CI lower bound > 0; TOO EASY if B > %s" % pc(h7c), fmt_diff(d), res)
    # H11 harm guard: R minus B error rate (stale plus lure), cued, primary level. Paired by seed: the same seeds and the same forked session of arm A.
    # HARM if the lower bound of the interval is above 0 (a point estimate alone can pass or fail by noise).
    def errv(arm):
        cs, ts = [], []
        for sd in V.seeds:
            sel = [r for r in rows if r["seed"] == sd and r["arm"] == arm and r["type"] == level and r.get("phrasing") == "cued"]
            cs.append(sum(1 for r in sel if r.get("error_class") in ("stale", "lure")))
            ts.append(len(sel))
        return cs, ts
    cr, tr = errv("R")
    cb, tb = errv("B")
    d = boot.diff(cr, tr, cb, tb)
    if d[0] is None:
        add("H11 harm guard: R minus B stale-plus-lure error rate, cued (primary level), paired by seed", "HARM if the interval lower bound > 0", "no data", "no data")
    else:
        add("H11 harm guard: R minus B stale-plus-lure error rate, cued (primary level), paired by seed",
            "HARM if the interval lower bound > 0 (auto-recall injects old values)", fmt_diff(d),
            "HARM: auto-recall injects old values" if d[1] is not None and d[1] > 0 else "OK (no harm shown)")
    md += ["", "Notes. K's gain (H6, H7) is an upper bound: every statement has one fixed sentence form with the key in backticks, so a search by the key finds the "
           "statements almost like an oracle. Real notes do not look like this."]
    return md, out
