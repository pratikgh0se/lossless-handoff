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

    def vecf(self, flt, fn):
        """Like vec, with a function of the row as the hit test."""
        cs, ts = [], []
        for s in self.seeds:
            sel = [r for r in self.rows if r["seed"] == s and flt(r)]
            cs.append(sum(1 for r in sel if fn(r)))
            ts.append(len(sel))
        return cs, ts

    def vec(self, flt, metric):
        cs, ts = [], []
        for s in self.seeds:
            sel = [r for r in self.rows if r["seed"] == s and flt(r)]
            cs.append(sum(1 for r in sel if r.get(metric) is True))
            ts.append(len(sel))
        return cs, ts


def gates_runaway(rows):
    import gates
    return gates.runaway_rate(rows)


def pc(x):
    return "%.0f%%" % (100 * x)


def cellfmt(c, t):
    return "no data" if not t else "%s (%d/%d)" % (pc(c / float(t)), c, t)


def section4(rows_all, sessions, th, draws):
    from report import Boot, fmt_diff
    from litm import mean_ends_diff
    rows_d4 = [r for r in rows_all if is_d4(r)]
    rows = [r for r in rows_d4 if not r.get("refused")]        # items of a refused probe fork are left out of every accuracy denominator
    if not rows_d4:
        return [], {}
    lt = th.get("litm", {})
    d4t = lt.get("design4", {})
    V = Vec(rows)
    boot = Boot(max(1, len(V.seeds)), draws or th["bootstrap_draws"], th["bootstrap_rng_seed"], th["ci_level"])
    arms = [a for a in ("A", "B", "R", "K", "G", "P", "Z", "O") if any(r["arm"] == a for r in rows)]
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
            for ph in design4.ALL_PHRASINGS:
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
    # table D4-6: runaway probe forks (the fork hit the output cap: an outcome, all its items wrong, class runaway)
    md += ["", "#### Table D4-6: runaway probe forks (forks that hit the output cap of %d tokens / all probe forks)" % 8000, "", "| Arm | all levels | " + " | ".join(design4.D4_LEVELS + ("L5",)) + " |", "|---|---|" + "---|" * 5]
    rw = {}
    for arm in arms:
        cols = []
        for lv in (None,) + design4.D4_LEVELS + ("L5",):
            sel = [r for r in rows if r["arm"] == arm and (lv is None or r["type"] == lv)]
            b, n = gates_runaway(sel)
            cols.append("%d/%d" % (b, n) if n else "no data")
            rw[(arm, lv)] = (b, n)
        md.append("| %s | %s |" % (arm, " | ".join(cols)))
    md += ["", "By phrasing (all arms, all levels): " + ", ".join("%s %d/%d" % ((ph,) + gates_runaway([r for r in rows if r.get("phrasing") == ph])) for ph in design4.ALL_PHRASINGS) + "."]
    out["runaway"] = {"%s|%s" % (k[0], k[1]): list(v) for k, v in rw.items()}
    # table D4-7: refused probe forks (a safety filter refused the fork: an outcome, items left out of the accuracy)
    md += ["", "#### Table D4-7: refused probe forks (a safety filter refused the fork; its items are left out of the accuracy) as refused forks / forks (refused items / items)", "",
           "| Arm | all levels | " + " | ".join(design4.D4_LEVELS + ("L5",)) + " |", "|---|---|" + "---|" * 5]
    rf = {}
    for arm in arms:
        cols = []
        for lv in (None,) + design4.D4_LEVELS + ("L5",):
            t = design4.refused_forks(rows_d4, lv, arm)
            cols.append("%d/%d (%d/%d)" % t if t[1] else "no data")
            rf[(arm, lv)] = t
        md.append("| %s | %s |" % (arm, " | ".join(cols)))
    md += ["", "By phrasing (all arms, all levels): " + ", ".join("%s %d/%d" % ((ph,) + design4.refused_forks(rows_d4, None, None, ph)[:2]) for ph in design4.ALL_PHRASINGS) + "."]
    out["refused"] = {"%s|%s" % (k[0], k[1]): list(v) for k, v in rf.items()}
    # place of the last update inside a third (what part of the context H1 can test)
    pt = design4.place_table(scripts) if scripts else {}
    md += ["", "#### Table D4-5: place of the last update (the deciding statement) inside its third", "",
           "| Level | chains | last update in the first half of its third | mean place in third 1 / 2 / 3 (0 = start, 1 = end) |", "|---|---|---|---|"]
    for lv, v in pt.items():
        md.append("| %s | %d | %s | %s |" % (lv, v["n"], pc(v["first_half"]), " / ".join("%.2f" % x for x in v["mean_by_third"])))
    out["place"] = pt
    if pt.get(level) and pt[level]["first_half"] < 0.10:
        md += ["", "**H1 does not test the first part of a third at %s: no last update of this level is there.** The result says nothing about the start of that part of the context." % level]
    # hypotheses
    md += ["", "#### Table D4-4: pre-registered hypotheses of design 4 (thresholds in thresholds.json, committed before any data)", "",
           "| Hypothesis | Threshold | Measured | Result |", "|---|---|---|---|"]

    def add(name, thr, measured, result):
        md.append("| %s | %s | %s | %s |" % (name, thr, measured, result))
        out["hypotheses"].append({"name": name, "result": result})

    def hyps(rows, V, boot, add):
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
        wrong = [r for r in rows if r["arm"] == "A" and r["type"] == level and r.get("m1") is not True and r.get("error_class") not in ("abstain", "unreadable", "refused", None)]
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
        from litm import model_search_val
        ck, tk = V.vecf(lambda r: r["arm"] == "K" and r["type"] == level and r.get("phrasing") == "neutral", model_search_val)
        cb, tb = V.vecf(lambda r: r["arm"] == "B" and r["type"] == level and r.get("phrasing") == "neutral", model_search_val)
        d = boot.diff(ck, tk, cb, tb)
        add("H6 search rate (model_search): K minus B, no compaction, neutral probes (primary level)", "> 0 pts and CI lower bound > 0", fmt_diff(d),
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
                "HARM: auto-recall injects old values" if d[1] is not None and d[1] > 0 else "OK: no harm shown (%d seeds)" % d[3])
        # F79: H12-H17 and the P leak check (arms G and P; thresholds registered before any data)
        if any(r["arm"] in ("G", "P") for r in rows):
            from litm import model_search_val

            def got_history(r):
                ks = r.get("search_kinds") or []
                return "guard" in ks or any(k in ("bash", "mcp") for k in ks)

            def share(flt, fn):
                c, t = V.vecf(flt, fn)
                return (sum(c) / float(sum(t))) if sum(t) else None

            def cell(arm, ph, typ=None):
                return lambda r: r["arm"] == arm and r["type"] == (typ or level) and r.get("phrasing") == ph

            def diff_cell(a, b, ph, met="m1"):
                return boot.diff(*V.vec(cell(a, ph), met), *V.vec(cell(b, ph), met))

            def k_acc(ph):
                return share(cell("K", ph), lambda r: r.get("m1") is True)

            easy = d4t.get("h13_too_easy_k_max", 0.95)
            min13 = d4t.get("h13_min_points", 10) / 100.0
            fr = {ph: share(cell("G", ph), got_history) for ph in d4t.get("h12_phrasings", ["nonliteral", "neutral"])}
            add("H12 trigger (arm G): share of items that got the history (guard fired or model search)", ">= %s%% for each of %s" % (int(100 * d4t.get("h12_min_share", 0.9)), ", ".join(fr)),
                ", ".join("%s %s" % (ph, "no data" if x is None else pc(x)) for ph, x in fr.items()),
                "no data" if all(x is None for x in fr.values()) else ("SUPPORTED" if all(x is not None and x >= d4t.get("h12_min_share", 0.9) for x in fr.values()) else "NOT SUPPORTED"))
            for name, ph in (("H13 gain, primary (action probes): G minus K", "action"), ("H13n gain, secondary (non-literal probes): G minus K", "nonliteral")):
                d = diff_cell("G", "K", ph)
                ka = k_acc(ph)
                if d[0] is None:
                    res = "no data"
                elif ka is not None and ka > easy:
                    res = "TOO EASY: no claim (K is at %s)" % pc(ka)
                else:
                    res = "SUPPORTED" if d[0] >= min13 and d[1] is not None and d[1] > 0 else "NOT SUPPORTED"
                add(name, ">= +%d pts and CI lower bound > 0; TOO EASY if K > %s" % (int(100 * min13), pc(easy)), fmt_diff(d), res)
            d = diff_cell("G", "K", "cued")
            lim14 = d4t.get("h14_cued_min_points", -2) / 100.0
            add("H14 harm: G minus K on cued probes (the share of right answers that became wrong needs the first reply: not measured)", ">= %d pts" % d4t.get("h14_cued_min_points", -2), fmt_diff(d),
                "no data" if d[0] is None else ("SUPPORTED: no harm shown" if d[0] >= lim14 else "HARM: G is below K on cued probes"))
            ctl = share(lambda r: r["arm"] == "G" and r["type"] == "CT", lambda r: bool(r.get("guard_fired")))
            add("H15 false fire (arm G): share of control items (facts stated one time) whose fork fired the guard", "<= %s%%; the replay of real transcripts <= %s%% of the stops (run.sh retrieval-gate --replay)" % (
                int(100 * d4t.get("h15_control_fire_max", 0.10)), int(100 * d4t.get("h15_replay_fire_max", 0.05))),
                "no control rows" if ctl is None else pc(ctl), "no data" if ctl is None else ("SUPPORTED" if ctl <= d4t.get("h15_control_fire_max", 0.10) else "NOT SUPPORTED"))
            ff = lambda flag: (lambda r: r["arm"] == "G" and r["type"] == level and bool(r.get("free_form")) is flag and r.get("phrasing") in ("nonliteral", "neutral", "action"))
            f_free, f_fixed = share(ff(True), lambda r: bool(r.get("guard_fired"))), share(ff(False), lambda r: bool(r.get("guard_fired")))
            gain_free = boot.diff(*V.vec(lambda r: r["arm"] == "G" and r["type"] == level and r.get("phrasing") == "action" and bool(r.get("free_form")), "m1"),
                                  *V.vec(lambda r: r["arm"] == "K" and r["type"] == level and r.get("phrasing") == "action" and bool(r.get("free_form")), "m1"))
            gain_main = diff_cell("G", "K", "action")
            if f_free is None or not f_fixed:
                res16, meas16 = "no data", "no free-form rows" if f_free is None else "no fixed-form fire rate"
            else:
                ok_fire = f_free >= d4t.get("h16_fire_ratio_min", 0.8) * f_fixed
                ok_gain = gain_free[0] is not None and gain_main[0] is not None and gain_free[0] >= d4t.get("h16_gain_ratio_min", 0.5) * gain_main[0]
                res16 = "SUPPORTED" if (ok_fire and ok_gain) else "NOT SUPPORTED"
                meas16 = "fire free-form %s, fixed-form %s; gain free-form %s" % (pc(f_free), pc(f_fixed), fmt_diff(gain_free))
            add("H16 not fitted (free-form cell, no backticks, some statements in assistant text, arm G)", "fire rate >= %s%% of the fixed-form rate and gain >= %s of H13" % (
                int(100 * d4t.get("h16_fire_ratio_min", 0.8)), d4t.get("h16_gain_ratio_min", 0.5)), meas16, res16)
            d = diff_cell("G", "K", "action")
            gshare = share(cell("G", "action"), lambda r: bool(r.get("guard_fired")))
            if d[0] is None:
                res17 = "no data"
            elif k_acc("action") is not None and k_acc("action") > easy:
                res17 = "TOO EASY: no claim"
            else:
                res17 = "SUPPORTED" if (d[0] >= d4t.get("h17_min_points", 10) / 100.0 and d[1] is not None and d[1] > 0 and (gshare or 0) >= d4t.get("h17_guard_fired_min_share", 0.5)) else "NOT SUPPORTED"
            add("H17 autonomous action (the owner's main case): G minus K on action probes, and the guard fired in G action items",
                ">= +%d pts, CI lower bound > 0, guard fired >= %s%%" % (d4t.get("h17_min_points", 10), int(100 * d4t.get("h17_guard_fired_min_share", 0.5))),
                "%s; guard fired %s" % (fmt_diff(d), "no data" if gshare is None else pc(gshare)), res17)
            d = diff_cell("P", "K", "nonliteral")
            add("P equals K on non-literal probes (leak check): P minus K", "CI lower bound not above 0", fmt_diff(d),
                "no data" if d[0] is None else ("HARNESS LEAK: P gains over K without a trigger" if d[1] is not None and d[1] > 0 else "OK"))
    hyps(rows, V, boot, add)
    refused_n = sum(1 for r in rows_d4 if r.get("refused"))
    if refused_n:
        # sensitivity: the same hypotheses with every refused item counted as wrong
        sens = []
        V2 = Vec(rows_d4)
        boot2 = Boot(max(1, len(V2.seeds)), draws or th["bootstrap_draws"], th["bootstrap_rng_seed"], th["ci_level"])
        hyps(rows_d4, V2, boot2, lambda name, thr, measured, result: sens.append((name, thr, measured, result)))
        main_res = {h["name"]: h["result"] for h in out["hypotheses"]}
        md += ["", "#### Table D4-4s: sensitivity, the same hypotheses with the %d refused item(s) counted as wrong (the main table leaves them out)" % refused_n, "",
               "| Hypothesis | Measured with refused as wrong | Result | Main table result |", "|---|---|---|---|"]
        differ = []
        for name, thr, measured, result in sens:
            md.append("| %s | %s | %s | %s |" % (name, measured, result, main_res.get(name, "")))
            if main_res.get(name, result).split(":")[0] != result.split(":")[0]:
                differ.append(name)
        out["sensitivity"] = [{"name": n, "result": r} for n, _t, _m, r in sens]
        md += ["", ("**The two readings DISAGREE for %d hypothesis(es): %s. Do not claim these without saying that refused probes change the result.**" % (len(differ), "; ".join(differ)))
               if differ else "The two readings (refused items left out, refused items counted as wrong) agree for every hypothesis."]
        out["readings_disagree"] = differ
    md += ["", "Notes. K's gain (H6, H7) is an upper bound: every statement has one fixed sentence form with the key in backticks, so a search by the key finds the "
           "statements almost like an oracle. Real notes do not look like this."]
    return md, out
