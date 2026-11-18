#!/usr/bin/env python3
"""Reporter of the continuity benchmark: tables by arm x type x N, seed-level bootstrap intervals.

Usage: report.py <run-id> [--publish] [--draws N]
Writes results/<run-id>/summary.md and summary.json. With --publish it also writes
docs/proof/<date>-continuity-<sha>.md and .jsonl.

Unit of resampling: the seed (items inside one seed are not independent).
B minus A is paired by seed: both arms use the same drawn seeds.
Pass thresholds come from thresholds.json (committed before any run).
"""
import argparse, datetime, json, os, random, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.realpath(os.path.join(HERE, "..", ".."))
RESULTS = os.path.join(HERE, "results")
ALL_TYPES = ["R", "D", "X", "S", "E", "U", "C"]
TYPES = ["R", "D", "X", "S", "E", "C"]            # scored and gated types. F is a gate. U is reported apart:
U_LABEL = "U needs subagent search (Phase 3)"      # its key lives in a subagent transcript only, so no arm can answer it today


def load_jsonl(path):
    out = []
    try:
        for ln in open(path, encoding="utf-8"):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


class Agg:
    """Counts of correct and total items for each (arm, type, n, seed). Cells with a wrong n_actual are dropped."""

    def __init__(self, rows):
        self.c, self.t = {}, {}
        self.parse, self.look, self.dropped = {}, {}, 0
        self.seeds = set()
        for r in rows:
            if r["arm"] in ("A", "B", "O") and r["n_actual"] != r["n_target"]:
                self.dropped += 1
                continue
            k = (r["arm"], r["type"], r["n_target"], r["seed"])
            self.c[k] = self.c.get(k, 0) + (1 if r["correct"] else 0)
            self.t[k] = self.t.get(k, 0) + 1
            pa = self.parse.setdefault(r["arm"], [0, 0])
            pa[0] += 1 if r["parse_ok"] else 0
            pa[1] += 1
            la = self.look.setdefault(r["arm"], [0, 0])
            la[0] += 1 if r.get("used_lookup") else 0
            la[1] += 1
            self.seeds.add(r["seed"])
        self.seeds = sorted(self.seeds)

    def vec(self, arm, types, n):
        """Per-seed (correct, total) lists for the arm, the types and the compaction count n (None = any)."""
        cs, ts = [], []
        for s in self.seeds:
            c = t = 0
            for ty in types:
                for (a, ty2, n2, s2), tv in self.t.items():
                    if a == arm and ty2 == ty and s2 == s and (n is None or n2 == n):
                        t += tv
                        c += self.c[(a, ty2, n2, s2)]
            cs.append(c)
            ts.append(t)
        return cs, ts


def pct(x):
    return "%.0f%%" % (100 * x)


def rate(c, t):
    return c / t if t else None


class Boot:
    def __init__(self, n_seeds, draws, rng_seed, level):
        r = random.Random(rng_seed)
        self.draws = [[r.randrange(n_seeds) for _ in range(n_seeds)] for _ in range(draws)]
        self.lo_q, self.hi_q = (1 - level) / 2, 1 - (1 - level) / 2

    def _ci(self, vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None, None
        return vals[int(self.lo_q * (len(vals) - 1))], vals[int(round(self.hi_q * (len(vals) - 1)))]

    def one(self, cs, ts):
        out = []
        for d in self.draws:
            t = sum(ts[i] for i in d)
            out.append(sum(cs[i] for i in d) / t if t else None)
        return self._ci(out)

    def diff(self, bc, bt, ac, at):
        """Paired B minus A. Seeds that lack data in one arm are dropped from both."""
        keep = [i for i in range(len(bc)) if bt[i] > 0 and at[i] > 0]
        mask = set(keep)
        bc2 = [bc[i] if i in mask else 0 for i in range(len(bc))]
        bt2 = [bt[i] if i in mask else 0 for i in range(len(bc))]
        ac2 = [ac[i] if i in mask else 0 for i in range(len(bc))]
        at2 = [at[i] if i in mask else 0 for i in range(len(bc))]
        out = []
        for d in self.draws:
            tb, ta = sum(bt2[i] for i in d), sum(at2[i] for i in d)
            out.append(sum(bc2[i] for i in d) / tb - sum(ac2[i] for i in d) / ta if tb and ta else None)
        point = None
        tb, ta = sum(bt2), sum(at2)
        if tb and ta:
            point = sum(bc2) / tb - sum(ac2) / ta
        lo, hi = self._ci(out)
        return point, lo, hi, len(keep)


def fmt_cell(c, t, ci):
    if not t:
        return "no data"
    lo, hi = ci
    iv = "" if lo is None else " [%s, %s]" % (pct(lo), pct(hi))
    return "%s%s (%d/%d)" % (pct(c / t), iv, c, t)


def fmt_diff(d):
    point, lo, hi, n = d
    if point is None:
        return "no data"
    return "%+.0f pts [%+.0f, %+.0f] (%d seeds)" % (100 * point, 100 * lo, 100 * hi, n)


def build(run_id, draws=None):
    rd = os.path.join(RESULTS, run_id)
    cfg = json.load(open(os.path.join(rd, "config.json"), encoding="utf-8"))
    state = json.load(open(os.path.join(rd, "state.json"), encoding="utf-8"))
    th = json.load(open(os.path.join(HERE, "thresholds.json"), encoding="utf-8"))
    rows = load_jsonl(os.path.join(rd, "results.jsonl"))
    sess = load_jsonl(os.path.join(rd, "sessions.jsonl"))
    ag = Agg(rows)
    S = len(ag.seeds)
    boot = Boot(max(S, 1), draws or th["bootstrap_draws"], th["bootstrap_rng_seed"], th["ci_level"]) if S else None
    ns = sorted({r["n_target"] for r in rows if r["arm"] in ("A", "B") and r["n_target"] > 0})
    arms = [a for a in ("A", "B") if any(r["arm"] == a for r in rows)]
    names = {"A": "A plain", "B": "B Lossless Handoff", "Z": "Z no history", "O": "O no compaction"}
    out = {"run": run_id, "config": cfg, "spent_usd": round(state["spent"], 4), "status": state["status"], "tables": {}, "gates": [], "gain": []}
    md = []
    small = cfg["window"] < 100000
    label = "%s window %s, model %s, Claude Code %s, %d seeds, gen v%s, LH %s" % (
        "small" if small else "real", cfg["window"], cfg["model"], cfg["claude_version"], S, cfg["gen_version"], cfg["lh_commit"])

    def pooled(arm, n):
        return ag.vec(arm, TYPES, n)

    # ---- table 1
    md.append("### Table 1: accuracy by arm and compaction count (types R D X S E C pooled; U is in its own row in Table 2)\n")
    md.append("Label: %s. Exploratory unless the pair count passes the threshold. Interval: 95%% bootstrap over seeds.\n" % label)
    md.append("| Arm | " + " | ".join("N=%d" % n for n in ns) + " |")
    md.append("|---|" + "---|" * len(ns))
    t1 = {}
    for a in arms:
        row = []
        for n in ns:
            cs, ts = pooled(a, n)
            row.append(fmt_cell(sum(cs), sum(ts), boot.one(cs, ts) if sum(ts) else (None, None)))
            t1["%s/%d" % (a, n)] = [sum(cs), sum(ts)]
        md.append("| %s | %s |" % (names[a], " | ".join(row)))
    if "A" in arms and "B" in arms:
        row = []
        for n in ns:
            (bc, bt), (ac, at) = pooled("B", n), pooled("A", n)
            row.append(fmt_diff(boot.diff(bc, bt, ac, at)))
        md.append("| B minus A | %s |" % " | ".join(row))
    out["tables"]["t1"] = t1

    # ---- table 2
    md.append("\n### Table 2: accuracy by probe type (cell = rate [interval] (correct/total))\n")
    t2 = {}
    for n in ns:
        md.append("**N = %d**\n" % n)
        md.append("| Type | " + " | ".join(names[a] for a in arms) + (" | B minus A |" if len(arms) == 2 else " |"))
        md.append("|---|" + "---|" * (len(arms) + (1 if len(arms) == 2 else 0)))
        for ty in ALL_TYPES:
            cells = []
            for a in arms:
                cs, ts = ag.vec(a, [ty], n)
                cells.append(fmt_cell(sum(cs), sum(ts), boot.one(cs, ts) if sum(ts) else (None, None)))
                t2["%d/%s/%s" % (n, ty, a)] = [sum(cs), sum(ts)]
            if len(arms) == 2:
                (bc, bt), (ac, at) = ag.vec("B", [ty], n), ag.vec("A", [ty], n)
                cells.append(fmt_diff(boot.diff(bc, bt, ac, at)))
            md.append("| %s | %s |" % (U_LABEL if ty == "U" else ty, " | ".join(cells)))
        md.append("")
    out["tables"]["t2"] = t2

    # ---- table 3: gates
    md.append("### Table 3: gates (the benchmark must prove itself first)\n")
    md.append("| Gate | Threshold | Measured | Result |")
    md.append("|---|---|---|---|")

    def gate(name, thr, measured, result):
        md.append("| %s | %s | %s | %s |" % (name, thr, measured, result))
        out["gates"].append({"gate": name, "threshold": thr, "measured": measured, "result": result})

    def ratio_gate(name, arm, types, n, kind, limit):
        cs, ts = ag.vec(arm, types, n)
        c, t = sum(cs), sum(ts)
        if not t:
            gate(name, "%s %s" % ("<=" if kind == "max" else ">=", pct(limit)), "0/0", "NO DATA")
            return
        ok = c / t <= limit if kind == "max" else c / t >= limit
        gate(name, "%s %s" % ("<=" if kind == "max" else ">=", pct(limit)), "%s (%d/%d)" % (pct(c / t), c, t), "PASS" if ok else "FAIL")

    ratio_gate("Amnesia arm Z (no history)", "Z", TYPES, None, "max", th["amnesia_max_accuracy"])
    ratio_gate("Oracle arm O (no compaction)", "O", TYPES, None, "min", th["oracle_min_accuracy"])
    for a in arms + ["O"]:
        if any(r["arm"] == a for r in rows):
            ratio_gate("Fresh control F, arm %s" % a, a, ["F"], None, "min", th["fresh_min_accuracy"])
    for a in ("A", "B", "O", "Z"):
        if a in ag.parse:
            ok, t = ag.parse[a]
            gate("Parse rate, arm %s" % a, ">= %s" % pct(th["parse_rate_min"]), "%s (%d/%d)" % (pct(ok / t), ok, t), "PASS" if ok / t >= th["parse_rate_min"] else "FAIL")
    scored = [s for s in sess if s["arm"] in ("A", "B")]
    planned = sum(len([n for n in cfg["n"]]) for _ in scored)
    missed = sum(len(s["missed"]) for s in scored)
    if planned:
        gate("Missed checkpoints (A, B)", "<= %s" % pct(th["missed_checkpoint_rate_max"]), "%s (%d/%d)" % (pct(missed / planned), missed, planned),
             "PASS" if missed / planned <= th["missed_checkpoint_rate_max"] else "FAIL")
    else:
        gate("Missed checkpoints (A, B)", "<= %s" % pct(th["missed_checkpoint_rate_max"]), "0/0", "NO DATA")
    pairs_total = pairs_bad = 0
    if "A" in arms and "B" in arms:
        for s in ag.seeds:
            for n in ns:
                pairs_total += 1
                have = [any(k[0] == a and k[2] == n and k[3] == s for k in ag.t) for a in ("A", "B")]
                pairs_bad += 0 if all(have) else 1
        gate("Invalid pairs (seed x N)", "<= %s" % pct(th["invalid_pair_rate_max"]), "%s (%d/%d)" % (pct(pairs_bad / pairs_total), pairs_bad, pairs_total) if pairs_total else "0/0",
             "NO DATA" if not pairs_total else ("PASS" if pairs_bad / pairs_total <= th["invalid_pair_rate_max"] else "FAIL"))
    pe = [x.get("boundaries_at_plant_end") for x in sess if x["arm"] in ("A", "B", "O")]
    pe = [x for x in pe if x is not None]
    gate("Items planted before compaction 1", "0 compactions at plant end", "%d of %d sessions with 0" % (sum(1 for x in pe if x == 0), len(pe)),
         "NO DATA" if not pe else ("PASS" if all(x == 0 for x in pe) else "FAIL"))
    leaks = sum(s.get("fork_leaks", 0) for s in sess)
    gate("Probe leaks into the main timeline", "0", str(leaks), "PASS" if leaks == 0 else "FAIL")
    bsess = [s for s in sess if s["arm"] == "B" and s["status"] == "done"]
    if bsess:
        for s in bsess:
            pass
        rec = sum(s.get("hook_receipts", 0) for s in bsess)
        comp = sum(s["boundaries"] for s in bsess)
        gate("Arm B hook receipts vs compactions (info)", "receipts >= compactions", "%d vs %d" % (rec, comp), "PASS" if rec >= comp and comp else "CHECK")
    usess = [s for s in sess if s["arm"] in ("A", "B", "O")]
    if usess:
        gate("Type U planted by a subagent (info)", "main reads of led-* files = 0", "reads %d, subagent files %d" % (
            sum(s.get("u_main_reads", 0) for s in usess), sum(s.get("subagent_files", 0) for s in usess)),
            "PASS" if sum(s.get("u_main_reads", 0) for s in usess) == 0 else "CHECK")
    for a in ("A", "B"):
        if a in ag.look and ag.look[a][1]:
            gate("Lookup rate, arm %s (info)" % a, "none", "%s (%d/%d)" % (pct(ag.look[a][0] / ag.look[a][1]), ag.look[a][0], ag.look[a][1]), "INFO")
    md.append("")

    # ---- gain verdicts
    md.append("### Gain verdict (pre-registered in thresholds.json)\n")
    md.append("| Comparison | B minus A | Pairs | Verdict |")
    md.append("|---|---|---|---|")
    comps = [{"name": "B minus A at N=%d, all types" % n, "n": n, "type": None} for n in ns]
    for pc in th["primary_comparisons"]:
        if pc["name"] not in [c["name"] for c in comps]:
            comps.append(pc)
    for pc in comps:
        types = [pc["type"]] if pc["type"] else TYPES
        if not ("A" in arms and "B" in arms):
            md.append("| %s | no data | 0 | NO DATA |" % pc["name"])
            continue
        (bc, bt), (ac, at) = ag.vec("B", types, pc["n"]), ag.vec("A", types, pc["n"])
        point, lo, hi, pairs = boot.diff(bc, bt, ac, at)
        if point is None:
            verdict = "NO DATA"
        elif pairs < th["min_pairs"]:
            verdict = "SMALL SAMPLE (%d pairs, minimum %d): no claim" % (pairs, th["min_pairs"])
        elif 100 * point >= th["gain_min_points"] and lo > th["gain_ci_lower_bound_above"]:
            verdict = "GAIN"
        else:
            verdict = "NO GAIN PROVEN"
        md.append("| %s | %s | %d | %s |" % (pc["name"], fmt_diff((point, lo, hi, pairs)) if point is not None else "no data", pairs, verdict))
        out["gain"].append({"name": pc["name"], "point": point, "lo": lo, "hi": hi, "pairs": pairs, "verdict": verdict})
    md.append("\nA result with no gain is stored and shown in the same way as a gain.\n")

    # ---- lookup split
    md.append("### Accuracy with and without lookup (items where the probe used a transcript search or recall.py)\n")
    md.append("| Arm | no lookup | with lookup |")
    md.append("|---|---|---|")
    for a in ("A", "B", "O"):
        rr = [r for r in rows if r["arm"] == a and r["type"] in TYPES and (a == "O" or r["n_actual"] == r["n_target"])]
        if not rr:
            continue
        cells = []
        for flag in (False, True):
            x = [r for r in rr if bool(r.get("used_lookup")) is flag]
            cells.append("%s (%d/%d)" % (pct(sum(1 for r in x if r["correct"]) / len(x)), sum(1 for r in x if r["correct"]), len(x)) if x else "no data")
        md.append("| %s | %s |" % (names[a], " | ".join(cells)))
    md.append("")

    # ---- cost and turns per compaction
    calls = load_jsonl(os.path.join(rd, "calls.jsonl"))
    md.append("### Cost per compaction (to predict the cost of a full run)\n")
    md.append("| Arm | Sessions | Main turns | Compactions | Turns per compaction | Main-turn cost per compaction | Cost per fork | Total cost |")
    md.append("|---|---|---|---|---|---|---|---|")
    cost_tab = {}
    for a in ("A", "B", "O", "Z"):
        ss = [s for s in sess if s["arm"] == a]
        if not ss:
            continue
        names_a = {s["name"] for s in ss}
        cl = [c for c in calls if c["name"] in names_a]
        turns = sum(s["turns"] for s in ss)
        comp = sum(s["boundaries"] for s in ss)
        mcost = sum(c["delta_usd"] for c in cl if c["kind"] == "turn")
        fk = [c["delta_usd"] for c in cl if c["kind"] in ("fork", "z")]
        tot = sum(c["delta_usd"] for c in cl)
        cost_tab[a] = {"sessions": len(ss), "turns": turns, "compactions": comp, "main_cost": mcost, "forks": len(fk), "fork_cost": sum(fk), "total": tot}
        md.append("| %s | %d | %d | %d | %s | %s | %s | $%.4f |" % (
            names[a], len(ss), turns, comp, "%.1f" % (turns / comp) if comp else "n/a",
            "$%.4f" % (mcost / comp) if comp else "n/a", "$%.4f" % (sum(fk) / len(fk)) if fk else "n/a", tot))
    out["cost"] = cost_tab
    md.append("\nThe plant turns (epoch 0) are part of the main-turn cost, so a short run overstates the cost per compaction. Predict a full run as: sessions x (plant cost + compactions x cost per compaction + forks x cost per fork). Take the numbers from a pilot at the same model and window.\n")

    # ---- head
    results = [g["result"] for g in out["gates"] if "(info)" not in g["gate"]]
    controls = "FAIL" if "FAIL" in results else ("NO DATA" if "NO DATA" in results else "PASS")
    head = ["# Continuity benchmark: run %s\n" % run_id,
            "**Controls: %s. Cost: $%.4f. Status: %s.** %s\n" % (controls, state["spent"], state["status"], label),
            "Command: `%s`. Seeds: %s. Types: %s. Arms: %s.\n" % (cfg["command"], ",".join(map(str, cfg["seeds"])), ",".join(cfg["types"]), ",".join(cfg["arms"]))]
    if ag.dropped:
        head.append("Items dropped for a wrong compaction count: %d.\n" % ag.dropped)
    out["controls"] = controls
    text = "\n".join(head + md) + "\n"
    return out, text


def aa(id1, id2, arm, draws):
    """A/A noise floor: the same arm and the same seeds in two runs. Prints run2 minus run1 and its 95% CI."""
    th = json.load(open(os.path.join(HERE, "thresholds.json"), encoding="utf-8"))
    ag = []
    for rid in (id1, id2):
        rows = [r for r in load_jsonl(os.path.join(RESULTS, rid, "results.jsonl")) if r["arm"] == arm]
        for r in rows:
            r["arm"] = "X%s" % (1 if rid == id1 else 2)     # relabel so that one Agg holds both runs
        ag.append(rows)
    both = Agg(ag[0] + ag[1])
    S = len(both.seeds)
    if not S:
        print("no data")
        return 1
    boot = Boot(S, draws or th["bootstrap_draws"], th["bootstrap_rng_seed"], th["ci_level"])
    lines = ["A/A noise floor, arm %s: run %s (1) against run %s (2)" % (arm, id1, id2), "| N | run 2 minus run 1 | seeds | CI holds 0 |", "|---|---|---|---|"]
    ok_all = True
    for n in [None] + sorted({r["n_target"] for r in ag[0] + ag[1] if r["n_target"] > 0}):
        (c2, t2), (c1, t1) = both.vec("X2", TYPES, n), both.vec("X1", TYPES, n)
        point, lo, hi, k = boot.diff(c2, t2, c1, t1)
        if point is None:
            lines.append("| %s | no data | 0 | n/a |" % ("all" if n is None else n))
            continue
        holds = lo <= 0 <= hi
        ok_all = ok_all and holds
        lines.append("| %s | %+.1f pts [%+.1f, %+.1f] | %d | %s |" % ("all" if n is None else "N=%d" % n, 100 * point, 100 * lo, 100 * hi, k, "yes" if holds else "NO"))
    lines.append("The width of this interval is the noise floor. Pass: the CI holds 0 (verification plan, P1-3). With few seeds the interval is wide and weak.")
    print("\n".join(lines))
    return 0 if ok_all else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "aa":
        ap = argparse.ArgumentParser(prog="report.py aa")
        ap.add_argument("cmd")
        ap.add_argument("run1")
        ap.add_argument("run2")
        ap.add_argument("--arm", default="A")
        ap.add_argument("--draws", type=int)
        x = ap.parse_args()
        return aa(x.run1, x.run2, x.arm, x.draws)
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--draws", type=int)
    a = ap.parse_args()
    rd = os.path.join(RESULTS, a.run_id)
    if not os.path.isdir(rd):
        print("no such run:", a.run_id, file=sys.stderr)
        return 2
    out, text = build(a.run_id, a.draws)
    open(os.path.join(rd, "summary.md"), "w", encoding="utf-8").write(text)
    json.dump(out, open(os.path.join(rd, "summary.json"), "w", encoding="utf-8"), indent=1, sort_keys=True)
    print(text)
    if a.publish:
        d = os.path.join(REPO, "docs", "proof")
        os.makedirs(d, exist_ok=True)
        stem = "%s-continuity-%s" % (datetime.date.today().isoformat(), out["config"]["bench_commit"] or "nosha")
        open(os.path.join(d, stem + ".md"), "w", encoding="utf-8").write(text)
        shutil.copyfile(os.path.join(rd, "results.jsonl"), os.path.join(d, stem + ".jsonl"))
        print("published docs/proof/%s.md and .jsonl" % stem)
    return 1 if out["controls"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
