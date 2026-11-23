#!/usr/bin/env python3
"""The proof test (run.sh proof): a small, cheap run that shows two things.

P1 (loss): plain Claude Code (arm A) loses information across compactions.
P2 (fix): Lossless Handoff (arm B) keeps it.

This module holds the logic that has no model call:
  - check_seeds: the 4 proof seeds must be new (never used by any seeds file or any earlier run);
  - verdict: the pre-registered pass rule (thresholds.json, block "proof") and the validity gates;
  - examples: a deterministic side-by-side list of items (no cherry-picking);
  - section: the markdown that report.py puts at the top of summary.md.
Types R D X S E C M are pooled and the probes are the cued ones, as in Table 1 of the report. Refused probe forks are left out of the scores.
"""
import glob, json, os, random

HERE = os.path.dirname(os.path.abspath(__file__))
TYPES = ["R", "D", "X", "S", "E", "C", "M"]
SEED_FILE = "seeds-proof.txt"


# ---- the seeds must be new

def used_seeds(here=HERE, skip=(SEED_FILE,)):
    """{seed: [where it was used]} from every seeds-*.txt (except the proof file), from the config.json of every run in results/ and of every fixture run."""
    used = {}
    for f in sorted(glob.glob(os.path.join(here, "seeds*.txt"))):
        if os.path.basename(f) in skip:
            continue
        for x in open(f).read().split():
            if x.strip().lstrip("-").isdigit():
                used.setdefault(int(x), []).append(os.path.basename(f))
    for pat in (os.path.join(here, "results", "*", "config.json"), os.path.join(here, "fixtures", "*", "config.json")):
        for f in sorted(glob.glob(pat)):
            try:
                cfg = json.load(open(f, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for s in cfg.get("seeds") or []:
                if isinstance(s, int):
                    used.setdefault(s, []).append(os.path.relpath(f, here))
    return used


def check_seeds(seeds, here=HERE, want=4):
    """Problems with the proof seeds, as a list of text lines. Empty list: the seeds are fine."""
    problems = []
    if len(seeds) != want:
        problems.append("the proof test needs exactly %d seeds, got %d" % (want, len(seeds)))
    if len(set(seeds)) != len(seeds):
        problems.append("the proof seeds are not all different: %s" % seeds)
    used = used_seeds(here)
    for s in seeds:
        if s in used:
            problems.append("seed %d was used before (%s)" % (s, ", ".join(used[s][:3])))
    return problems


# ---- data

def _cells(rows, n_low, n_high):
    """{(arm, seed, n): [correct, total]} for the cued probes of the compaction condition, scored types, refused forks left out."""
    c = {}
    for r in rows:
        if r.get("phrasing", "cued") != "cued" or r.get("condition", "compact") != "compact" or r.get("type") not in TYPES or r.get("refused"):
            continue
        if r["arm"] in ("A", "B") and r.get("n_actual", r.get("n_target")) != r.get("n_target"):
            continue
        k = (r["arm"], r["seed"], r.get("n_target"))
        v = c.setdefault(k, [0, 0])
        v[0] += 1 if r.get("correct") else 0
        v[1] += 1
    return c


def _pool(cells, arm, n, seeds):
    c = t = 0
    for s in seeds:
        v = cells.get((arm, s, n))
        if v:
            c += v[0]
            t += v[1]
    return c, t


def _rate(c, t):
    return c / t if t else None


def _boot_lower(cells, seeds, n, th):
    """Lower bound of the paired bootstrap interval of B minus A at n (resample the seeds; same draws for both arms)."""
    rng = random.Random(th["bootstrap_rng_seed"])
    k = len(seeds)
    if not k:
        return None
    vals = []
    for _ in range(int(th["bootstrap_draws"])):
        d = [seeds[rng.randrange(k)] for _ in range(k)]
        bc, bt = _pool(cells, "B", n, d)
        ac, at = _pool(cells, "A", n, d)
        if bt and at:
            vals.append(bc / bt - ac / at)
    if not vals:
        return None
    vals.sort()
    lo_q = (1 - th["ci_level"]) / 2
    return vals[int(lo_q * (len(vals) - 1))]


def _gate(name, thr, measured, ok):
    return {"gate": name, "threshold": thr, "measured": measured, "result": "NO DATA" if ok is None else ("PASS" if ok else "FAIL")}


def _pct(x):
    return "%.0f%%" % (100 * x)


def validity_gates(rows, sessions, th, seeds, n_list, arms=("A", "B", "O", "Z")):
    """The validity gates: fresh control F, oracle O, amnesia Z, parse rate, refusals, missed checkpoints. Any result but PASS makes the verdict INVALID."""
    import gates as g
    rs = [r for r in rows if r.get("phrasing", "cued") == "cued" and r.get("condition", "compact") == "compact"]
    out = []

    def acc(arm, types):
        sel = [r for r in rs if r["arm"] == arm and r.get("type") in types and not r.get("refused")]
        return (sum(1 for r in sel if r.get("correct")), len(sel))

    c, t = acc("Z", TYPES)
    out.append(_gate("Amnesia arm Z (no history)", "<= %s" % _pct(th["amnesia_max_accuracy"]), "%s (%d/%d)" % (_pct(c / t), c, t) if t else "0/0", None if not t else c / t <= th["amnesia_max_accuracy"]))
    c, t = acc("O", TYPES)
    out.append(_gate("Oracle arm O (no compaction)", ">= %s" % _pct(th["oracle_min_accuracy"]), "%s (%d/%d)" % (_pct(c / t), c, t) if t else "0/0", None if not t else c / t >= th["oracle_min_accuracy"]))
    for a in ("A", "B", "O"):
        c, t = acc(a, ["F"])
        out.append(_gate("Fresh control F, arm %s" % a, ">= %s" % _pct(th["fresh_min_accuracy"]), "%s (%d/%d)" % (_pct(c / t), c, t) if t else "0/0", None if not t else c / t >= th["fresh_min_accuracy"]))
    for a in ("A", "B", "O"):
        sel = [r for r in rs if r["arm"] == a and not r.get("refused")]
        ok = sum(1 for r in sel if r.get("parse_ok"))
        out.append(_gate("Parse rate, arm %s" % a, ">= %s" % _pct(th["parse_rate_min"]), "%s (%d/%d)" % (_pct(ok / len(sel)), ok, len(sel)) if sel else "0/0",
                         None if not sel else ok / len(sel) >= th["parse_rate_min"]))
    worst, worst_seed, any_forks = 0.0, None, False
    for s in seeds:
        bad, allf = g.refused_rate([r for r in rows if r.get("seed") == s and r.get("condition", "compact") == "compact"])
        if allf:
            any_forks = True
            if bad / allf >= worst:
                worst, worst_seed = bad / allf, s
    # rows of the compaction condition carry the refused flag only when a fork was flagged; a seed with no flagged fork counts as 0 refused
    out.append(_gate("Refused probe forks (worst seed)", "<= %s" % _pct(g.REFUSED_SEED_MAX), ("%s (seed %s)" % (_pct(worst), worst_seed)) if any_forks else "0 flagged (no row carries the refused flag)",
                     (worst <= g.REFUSED_SEED_MAX) if any_forks else bool(rs)))
    scored = [s for s in sessions if s.get("arm") in ("A", "B") and s.get("condition", "compact") == "compact"]
    planned = len(scored) * len(n_list)
    missed = sum(len(s.get("missed") or []) for s in scored)
    out.append(_gate("Missed checkpoints (A, B)", "<= %s" % _pct(th["missed_checkpoint_rate_max"]), "%s (%d/%d)" % (_pct(missed / planned), missed, planned) if planned else "0/0",
                     None if not planned else missed / planned <= th["missed_checkpoint_rate_max"]))
    return out


# ---- the pass rule

def verdict(rows, sessions, th_all, n_list=(1, 3, 10)):
    """The pre-registered rule. Returns a dict: verdict (PASS, FAIL or INVALID), p1, p2, gates, seeds."""
    th = th_all["proof"]
    full = dict(th_all)
    lo, hi = th["n_low"], th["n_high"]
    seeds = sorted({r["seed"] for r in rows if r.get("condition", "compact") == "compact"})
    cells = _cells(rows, lo, hi)
    gl = validity_gates(rows, sessions, full, seeds, list(n_list))
    # every seed must have A and B data at N=1 and N=10
    have = [s for s in seeds if all(cells.get((a, s, n), [0, 0])[1] for a in ("A", "B") for n in (lo, hi))]
    gl.append(_gate("Seeds with A and B data at N=%d and N=%d" % (lo, hi), "= %d" % th["seeds"], "%d of %d seeds" % (len(have), len(seeds)), len(have) == th["seeds"] and len(seeds) == th["seeds"]))
    invalid = [g_["gate"] for g_ in gl if g_["result"] != "PASS"]

    a1, a10, b10 = _pool(cells, "A", lo, seeds), _pool(cells, "A", hi, seeds), _pool(cells, "B", hi, seeds)
    p1 = {"rule": "A at N=%d minus A at N=%d >= %d points (pooled), and A at N=%d < A at N=%d in at least %d of %d seeds" % (lo, hi, th["p1_min_drop_points"], hi, lo, th["p1_min_seeds"], th["seeds"])}
    p2 = {"rule": "B minus A at N=%d >= %d points (pooled), 95%% bootstrap lower bound over seeds > 0, and B >= A at N=%d in at least %d of %d seeds" % (hi, th["p2_min_gain_points"], hi, th["p2_min_seeds"], th["seeds"])}
    if a1[1] and a10[1]:
        drop = 100 * (a1[0] / a1[1] - a10[0] / a10[1])
        sd = sum(1 for s in seeds if cells.get(("A", s, hi), [0, 0])[1] and cells.get(("A", s, lo), [0, 0])[1]
                 and cells[("A", s, hi)][0] / cells[("A", s, hi)][1] < cells[("A", s, lo)][0] / cells[("A", s, lo)][1])
        p1.update({"a_n1": _rate(*a1), "a_n10": _rate(*a10), "drop_points": drop, "seeds_with_drop": sd,
                   "ok": drop >= th["p1_min_drop_points"] and sd >= th["p1_min_seeds"]})
    else:
        p1["ok"] = None
    if a10[1] and b10[1]:
        gain = 100 * (b10[0] / b10[1] - a10[0] / a10[1])
        lower = _boot_lower(cells, seeds, hi, full)
        sb = sum(1 for s in seeds if cells.get(("A", s, hi), [0, 0])[1] and cells.get(("B", s, hi), [0, 0])[1]
                 and cells[("B", s, hi)][0] / cells[("B", s, hi)][1] >= cells[("A", s, hi)][0] / cells[("A", s, hi)][1])
        p2.update({"a_n10": _rate(*a10), "b_n10": _rate(*b10), "gain_points": gain, "ci_lower_points": None if lower is None else 100 * lower, "seeds_b_ge_a": sb,
                   "ok": gain >= th["p2_min_gain_points"] and lower is not None and lower > th["p2_ci_lower_above"] and sb >= th["p2_min_seeds"]})
    else:
        p2["ok"] = None
    for p in (p1, p2):
        p["result"] = "INVALID" if invalid or p["ok"] is None else ("PASS" if p["ok"] else "FAIL")
    if invalid or p1["ok"] is None or p2["ok"] is None:
        overall = "INVALID"
    else:
        overall = "PASS" if p1["ok"] and p2["ok"] else "FAIL"
    return {"verdict": overall, "p1": p1, "p2": p2, "gates": gl, "invalid_gates": invalid, "seeds": seeds}


# ---- examples

def expected_text(ptype, item):
    if ptype in ("R", "D", "S", "U", "F"):
        return str(item.get("answer"))
    if ptype == "X":
        return "current %s, replaced %s" % (item.get("new"), item.get("old"))
    if ptype == "E":
        return "proceed false, reason code %s" % item.get("ecode")
    if ptype == "M":
        return "order %s" % ", ".join(item.get("order") or [])
    if ptype == "C":
        return "conflict true, earlier %s" % item.get("tok1")
    return "?"


def _answer_text(r):
    p = r.get("parsed")
    if isinstance(p, dict) and r.get("parse_ok"):
        return json.dumps({k: v for k, v in p.items() if k != "id"}, sort_keys=True)
    return "(no parsed answer) " + str(r.get("raw") or "")[:120]


def _used_search(r):
    kinds = r.get("search_kinds") or []
    return bool(r.get("used_lookup") or kinds)


def examples(rows, scripts, n=10, limit=5):
    """Deterministic items at N=n. Returns (a_wrong_b_right shown (up to limit), a_wrong_b_right total, b_wrong_a_right all).
    Order: seed, item id, type. The first `limit` of the first list, all of the second. Each entry is a dict."""
    by = {}
    for r in rows:
        if r.get("phrasing", "cued") != "cued" or r.get("condition", "compact") != "compact" or r.get("type") not in TYPES or r.get("refused"):
            continue
        if r["arm"] not in ("A", "B") or r.get("n_target") != n or r.get("n_actual", n) != n:
            continue
        by[(r["seed"], r.get("item"), r["type"], r["arm"])] = r
    keys = sorted({k[:3] for k in by}, key=lambda k: (k[0], int(k[1]) if str(k[1]).lstrip("-").isdigit() else 0, k[2]))
    ab, ba = [], []
    for k in keys:
        a, b = by.get(k + ("A",)), by.get(k + ("B",))
        if not a or not b:
            continue
        items = ((scripts.get(k[0]) or {}).get("items") or {}).get(k[2]) or []
        item = next((i for i in items if str(i.get("id")) == str(k[1])), {})
        e = {"seed": k[0], "item": k[1], "type": k[2], "question": str(item.get("question") or item.get("statement") or "(not in the script)"),
             "expected": expected_text(k[2], item) if item else "(not in the script)", "a": _answer_text(a), "b": _answer_text(b), "b_search": _used_search(b)}
        if not a.get("correct") and b.get("correct"):
            ab.append(e)
        elif a.get("correct") is False and b.get("correct") is False:
            continue
        elif a.get("correct") and not b.get("correct"):
            ba.append(e)
    return ab[:limit], len(ab), ba


def _cell(x, n=170):
    x = str(x).replace("\n", " ").replace("|", "\\|")
    return x if len(x) <= n else x[:n - 3] + "..."


def _example_md(e):
    return ["- seed %s, type %s, item %s. Question: %s" % (e["seed"], e["type"], e["item"], _cell(e["question"], 220)),
            "  - expected: `%s`" % _cell(e["expected"]), "  - A answered: `%s`" % _cell(e["a"]), "  - B answered: `%s`" % _cell(e["b"]),
            "  - B used a search: %s" % ("yes" if e["b_search"] else "no")]


# ---- the report section

def section(rows, sessions, cfg, th_all, scripts, draws=None):
    """(markdown lines, dict) for the top of summary.md of a proof run."""
    if draws:
        th_all = dict(th_all, bootstrap_draws=draws)
    v = verdict(rows, sessions, th_all, cfg.get("n") or (1, 3, 10))
    th = th_all["proof"]
    md = ["## Proof verdict\n",
          "**Verdict: %s.** The run used %d seeds and is a small benchmark, not the full suite (20 seeds, 30 compactions, Opus: future work).\n" % (v["verdict"], len(v["seeds"])),
          "Rule (thresholds.json, block proof, registered %s before any proof data): types R D X S E C M pooled, cued probes, as in Table 1.\n" % th["registered"],
          "| Check | Rule | Measured | Result |", "|---|---|---|---|"]
    p1, p2 = v["p1"], v["p2"]
    if "drop_points" in p1:
        m1 = "A N=1 %s, A N=10 %s, drop %.1f pts; A lower at N=10 in %d of %d seeds" % (_pct(p1["a_n1"]), _pct(p1["a_n10"]), p1["drop_points"], p1["seeds_with_drop"], len(v["seeds"]))
    else:
        m1 = "no data"
    if "gain_points" in p2:
        lo = p2["ci_lower_points"]
        m2 = "A N=10 %s, B N=10 %s, gain %+.1f pts, bootstrap lower bound %s; B >= A in %d of %d seeds" % (
            _pct(p2["a_n10"]), _pct(p2["b_n10"]), p2["gain_points"], "n/a" if lo is None else "%+.1f pts" % lo, p2["seeds_b_ge_a"], len(v["seeds"]))
    else:
        m2 = "no data"
    md.append("| P1 loss: plain Claude Code loses information | %s | %s | %s |" % (p1["rule"], m1, p1["result"]))
    md.append("| P2 fix: Lossless Handoff keeps it | %s | %s | %s |" % (p2["rule"], m2, p2["result"]))
    for g_ in v["gates"]:
        md.append("| Gate: %s | %s | %s | %s |" % (g_["gate"], g_["threshold"], g_["measured"], g_["result"] if g_["result"] == "PASS" else "INVALID (%s)" % g_["result"]))
    if v["verdict"] == "INVALID":
        md.append("\nINVALID means a validity gate did not pass, so the run proves nothing either way. Failed gates: %s.\n" % (", ".join(v["invalid_gates"]) or "none (no data for P1 or P2)"))
    md.append("")
    shown, total, ba = examples(rows, scripts, n=th["n_high"], limit=5)
    md.append("## Side-by-side examples\n")
    md.append("Items at N=%d where the answers differ. The choice is fixed: sorted by seed, item id and type, no picking by hand.\n" % th["n_high"])
    md.append("### A wrong and B right (%d of %d shown, the first by the order above)\n" % (len(shown), total))
    for e in shown:
        md += _example_md(e)
    if not shown:
        md.append("none")
    md.append("\n### B wrong and A right (all %d, for honesty)\n" % len(ba))
    for e in ba:
        md += _example_md(e)
    if not ba:
        md.append("none")
    md.append("")
    v["examples"] = {"a_wrong_b_right_total": total, "a_wrong_b_right_shown": len(shown), "b_wrong_a_right": len(ba)}
    return md, v
