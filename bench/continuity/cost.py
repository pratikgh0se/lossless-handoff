#!/usr/bin/env python3
"""Cost estimate of the continuity benchmark. It uses no model and no network.

The numbers come from the real Sonnet runs of the full evaluation (Wave 1 and Wave 2, run on 2026-11-18),
not from list prices. Each constant says where it comes from. A pilot gives better numbers: copy them here.

Usage: cost.py [--seeds N] [--arms A,B,R] [--n 1,3,10] [--conditions compact,nocompact] [--phrasings cued,neutral]
       cost.py            (no option: print the pilot and the full run of the lost-in-the-middle design)
"""
import argparse

# Sonnet, window 400000, probe design 1 (cued only), measured.
WAVE1_PER_SEED_4ARMS = 38.0     # USD for one seed, arms A+B+Z+O, N up to 10 (Wave 1: about $38 per seed)
WAVE2_PER_SEED_AB_N30 = 92.0    # USD for one seed, arms A+B, N up to 30 (Wave 2: about $92 per seed)
# Assumed (not measured): arms Z and O together cost about $4 of the Wave 1 figure. They have one short session each.
Z_PLUS_O_PER_SEED = 4.0
Z_UNIT, O_UNIT = 0.5, 3.5
# Derived: a unit of arm A or B is linear in the largest N: base + per_compaction * N_max.
UNIT_N10 = (WAVE1_PER_SEED_4ARMS - Z_PLUS_O_PER_SEED) / 2      # 17.0
UNIT_N30 = WAVE2_PER_SEED_AB_N30 / 2                           # 46.0
PER_COMPACTION = (UNIT_N30 - UNIT_N10) / 20.0                  # 1.45
BASE = UNIT_N10 - PER_COMPACTION * 10                          # 2.5
# Assumed (not measured): probe forks are 30% of a unit. Probe design 2 asks more forks (neutral phrasing).
FORK_SHARE = 0.30
# Assumed (not measured): a no-compaction unit is one 50-turn session of about 100k tokens, with forks at its end.
# Cached input of Sonnet makes it much cheaper than a unit with compactions. Check it with the pilot.
NC_UNIT = 5.0
# Probe forks of one checkpoint: R D X S E U C M F = 9 cued. Design 2 adds neutral forks for R D X S E.
CUED_FORKS, NEUTRAL_FORKS = 9, 5
PILOT = {"seeds": 2, "arms": ["A", "B", "R"], "n": [1, 3, 10], "conditions": ["compact", "nocompact"], "phrasings": ["cued", "neutral"]}
SMOKE = {"seeds": 1, "arms": ["A", "R"], "n": [1, 3, 10], "conditions": ["nocompact"], "phrasings": ["cued", "neutral"]}
FULL = {"seeds": 20, "arms": ["A", "B", "R", "Z", "O"], "n": [1, 3, 10], "conditions": ["compact", "nocompact"], "phrasings": ["cued", "neutral"]}


def fork_multiplier(phrasings):
    extra = NEUTRAL_FORKS if "neutral" in phrasings else 0
    return 1 + FORK_SHARE * (extra / CUED_FORKS)


def estimate(seeds, arms, n, conditions, phrasings):
    """Returns (total USD, lines of the breakdown). Arm R costs like arm B plus the same fork multiplier."""
    mult = fork_multiplier(phrasings)
    per_seed, lines = 0.0, []
    for a in arms:
        if a in ("A", "B", "R"):
            if "compact" in conditions:
                u = (BASE + PER_COMPACTION * max(n)) * mult
                per_seed += u
                lines.append("arm %s, with compactions (N up to %d): $%.2f per seed" % (a, max(n), u))
            if "nocompact" in conditions:
                u = NC_UNIT * mult
                per_seed += u
                lines.append("arm %s, no compaction: $%.2f per seed" % (a, u))
        elif a == "Z":
            u = Z_UNIT * mult
            per_seed += u
            lines.append("arm Z (no history, one run for all conditions): $%.2f per seed" % u)
        elif a == "O":
            u = O_UNIT * mult
            per_seed += u
            lines.append("arm O (oracle, no compaction, short session, one run for all conditions): $%.2f per seed" % u)
    return per_seed * seeds, lines, per_seed


def render(name, cfg):
    total, lines, per_seed = estimate(cfg["seeds"], cfg["arms"], cfg["n"], cfg["conditions"], cfg["phrasings"])
    out = ["%s: %d seeds, arms %s, N=%s, conditions %s, phrasings %s" % (
        name, cfg["seeds"], ",".join(cfg["arms"]), ",".join(map(str, cfg["n"])), ",".join(cfg["conditions"]), ",".join(cfg["phrasings"]))]
    out += ["  " + x for x in lines]
    out.append("  per seed $%.2f; total about $%.0f (an estimate from Wave 1 and Wave 2, not measured for this design; plan for plus or minus 30%%)" % (per_seed, total))
    return total, "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int)
    ap.add_argument("--arms")
    ap.add_argument("--n")
    ap.add_argument("--conditions")
    ap.add_argument("--phrasings")
    a = ap.parse_args(argv)
    if not any([a.seeds, a.arms, a.n, a.conditions, a.phrasings]):
        for name, cfg in (("SMOKE (litm-smoke)", SMOKE), ("PILOT", PILOT), ("FULL", FULL)):
            print(render(name, cfg)[1])
        return 0
    cfg = dict(PILOT)
    if a.seeds:
        cfg["seeds"] = a.seeds
    if a.arms:
        cfg["arms"] = a.arms.split(",")
    if a.n:
        cfg["n"] = [int(x) for x in a.n.split(",")]
    if a.conditions:
        cfg["conditions"] = a.conditions.split(",")
    if a.phrasings:
        cfg["phrasings"] = a.phrasings.split(",")
    print(render("CUSTOM", cfg)[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
