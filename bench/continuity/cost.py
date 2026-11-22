#!/usr/bin/env python3
"""Cost estimate of the continuity benchmark. It uses no model and no network.

The numbers come from the real Sonnet runs of the full evaluation (Wave 1 and Wave 2, run on 2026-11-18),
not from list prices. Each constant says where it comes from. A pilot gives better numbers: copy them here.

Usage: cost.py [--seeds N] [--arms A,B,R] [--n 1,3,10] [--conditions compact,nocompact] [--phrasings cued,neutral]
       cost.py            (no option: print the pilot and the full run of the lost-in-the-middle design)
"""
import argparse, math

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
SMOKE3 = {"seeds": 1, "arms": ["A", "B", "R", "K"], "n": [1, 3], "conditions": ["compact", "nocompact"], "phrasings": ["cued", "neutral", "nonliteral"]}
NC_SMOKE3 = {"seeds": 1, "arms": ["A", "R"], "n": [1, 3, 10], "conditions": ["nocompact"], "phrasings": ["cued", "neutral", "nonliteral"]}
PILOT3 = {"seeds": 2, "arms": ["A", "B", "R", "K"], "n": [1, 3, 10], "conditions": ["compact", "nocompact"], "phrasings": ["cued", "neutral", "nonliteral"]}
FULL3 = {"seeds": 23, "arms": ["A", "B", "R", "K", "Z", "O"], "n": [1, 3, 10, 30], "conditions": ["compact", "nocompact"], "phrasings": ["cued", "neutral", "nonliteral"]}
FULL3_V3 = dict(FULL3, n_by_arm={"R": [1, 3, 10], "K": [1, 3, 10]})      # litm3-full-v3: the default full run
FULL3_V5 = dict(FULL3, arms=["A", "B", "K", "Z", "O"], n_by_arm={"K": [10]})    # litm3-full-v5
FULL3_NO_R = dict(FULL3, skip=(("R", "compact"),))      # variant: arm R is not run in the compaction condition (arm K contains R)
FULL = {"seeds": 20, "arms": ["A", "B", "R", "Z", "O"], "n": [1, 3, 10], "conditions": ["compact", "nocompact"], "phrasings": ["cued", "neutral"]}


# ---- probe design 3 (2026-11-22). A no-compaction session of about 300k tokens in a 400k window, all user turns.
# Token model, not a Wave number: no Wave run had a session this long without compaction. Every price and count below is an assumption.
# Prices are assumed list prices of a Sonnet-class model, USD per million tokens. Check them before a real run.
PRICE_CACHE_READ, PRICE_CACHE_WRITE, PRICE_OUTPUT = 0.30, 3.75, 15.0
D3_TURNS = 75                    # 3 bridge turns, 30 fact turns, 6 replacement turns, 36 filler turns (gen.build_litm3)
D3_CONTEXT_START, D3_CONTEXT_END = 25000, 300000     # tokens in the first and in the last turn; growth is linear
D3_OUT_PER_TURN = 150            # reply tokens of a main turn ("ok" or one number)
D3_FORKS = 15                    # probe forks: 5 types (R D X S E) x 3 phrasings (cued, neutral, nonliteral)
D3_FORK_OUT = 800                # reply tokens of a probe fork (JSON of 6 answers)
D3_SLACK = 1.2                   # retries, hook time, reads of tool calls: 20% on top
# Arm factors on a design-3 unit. Assumed: R adds hook output of 3 snippets per cued prompt. K adds MCP calls, recall rules and reanchor text.
ARM_FACTOR = {"A": 1.0, "B": 1.0, "R": 1.05, "K": 1.2}
# Design 3 compaction condition: the design-2 compaction unit, with the K factor above.


def d3_nc_main():
    """USD of the main no-compaction session of design 3 (run once for each seed, as arm A, no forks), from the token model above."""
    avg_ctx = (D3_CONTEXT_START + D3_CONTEXT_END) / 2
    read = D3_TURNS * avg_ctx * PRICE_CACHE_READ / 1e6
    write = D3_CONTEXT_END * PRICE_CACHE_WRITE / 1e6
    out = D3_TURNS * D3_OUT_PER_TURN * PRICE_OUTPUT / 1e6
    return (read + write + out) * D3_SLACK


def d3_nc_forks():
    """USD of the probe forks of ONE arm on the finished no-compaction session (15 forks, each reads the full context from cache)."""
    return D3_FORKS * (D3_CONTEXT_END * PRICE_CACHE_READ / 1e6 + D3_FORK_OUT * PRICE_OUTPUT / 1e6) * D3_SLACK


def d3_nc_unit():
    """USD of one design-3 no-compaction session of arm A with its own forks."""
    return d3_nc_main() + d3_nc_forks()


D3_CONTROL_USD = {"Z": 0.15, "O": 0.30}      # no-compaction controls of design 3: Z = amnesia forks of an empty session, O = short oracle (about 35 small turns)


def estimate3(seeds, arms, n, conditions, phrasings=("cued", "neutral", "nonliteral"), skip=(), n_by_arm=None):
    """Design 3. Compaction units: the design-2 model (cued and neutral phrasings), with the arm factor. No-compaction condition: ONE main session
    for each seed (arm A) plus the forks of each arm A, B, R, K on it (token model). skip: (arm, condition) pairs that are not run."""
    mult = fork_multiplier(["cued", "neutral"])
    per_seed, lines = 0.0, []
    skip = set(skip)
    for a in arms:
        if a in ARM_FACTOR:
            f = ARM_FACTOR[a]
            if "compact" in conditions and (a, "compact") not in skip:
                nn = (n_by_arm or {}).get(a, n)
                u = (BASE + PER_COMPACTION * max(nn)) * mult * (1.2 if a == "K" else 1.0)
                per_seed += u
                lines.append("arm %s, with compactions (N up to %d): $%.2f per seed" % (a, max(nn), u))
            if "nocompact" in conditions and (a, "nocompact") not in skip:
                u = d3_nc_forks() * f + (d3_nc_main() if a == "A" else 0.0)
                per_seed += u
                lines.append("arm %s, no compaction, design 3 (about 300k tokens): $%.2f per seed (%s)" % (
                    a, u, "main session once, plus its forks" if a == "A" else "forks of the session of arm A only"))
        elif a == "Z":
            if "compact" in conditions:
                per_seed += Z_UNIT * mult
                lines.append("arm Z (no history, one run): $%.2f per seed" % (Z_UNIT * mult))
            if "nocompact" in conditions:
                per_seed += D3_CONTROL_USD["Z"]
                lines.append("arm Z, design 3 amnesia forks: $%.2f per seed" % D3_CONTROL_USD["Z"])
        elif a == "O":
            if "compact" in conditions:
                per_seed += O_UNIT * mult
                lines.append("arm O (oracle, short session, one run): $%.2f per seed" % (O_UNIT * mult))
            if "nocompact" in conditions:
                per_seed += D3_CONTROL_USD["O"]
                lines.append("arm O, design 3 short oracle: $%.2f per seed" % D3_CONTROL_USD["O"])
    return per_seed * seeds, lines, per_seed


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def h1_power(seeds, n_mid, n_start, n_end, effect=-0.10, base_p=0.80, seed_sd=0.10):
    """Power of H1 (CI upper bound below 0 for middle minus the mean of start and end) and the smallest effect found with 80% power.
    n_*: items per seed in that position. Assumed: base accuracy 80% at start and end, a seed-level spread of 10 points, a normal interval.
    The per-seed correlation of the three positions is ignored, which makes the figure a little low (careful)."""
    def var(n, p):
        return p * (1 - p) / (n * seeds) + seed_sd ** 2 / seeds
    se = math.sqrt(var(n_mid, base_p + effect) + (var(n_start, base_p) + var(n_end, base_p)) / 4)
    power = norm_cdf(abs(effect) / se - 1.96)
    mde = (1.96 + 0.8416) * se
    return power, mde, se


def power_lines(seeds=20):
    out = []
    for name, (nm, ns_, ne) in (("design 2 (types R D X S E C, 2:1:1 per type)", (6, 12, 6)), ("design 3 (types R D X S E, 2:2:2 per type)", (10, 10, 10))):
        pw, mde, se = h1_power(seeds, nm, ns_, ne)
        need = next((k for k in range(2, 400) if h1_power(k, nm, ns_, ne)[0] >= 0.8), None)
        out.append("  %s: items per seed middle/start/end = %d/%d/%d; power to find a -10 point effect %.0f%%; smallest effect at 80%% power %.1f points (SE %.1f points); seeds for 80%% power at -10 points: %s" % (
            name, nm, ns_, ne, 100 * pw, 100 * mde, 100 * se, need))
    return out


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


# ---- preflight (Haiku, 1 dev seed, arms A B R K Z O, both conditions, design-3 mini session, plus one Sonnet turn with a 300k paste). Target: about $5.
# Assumed (not measured) Haiku-class prices, USD per million tokens. The compaction unit comes from the measured smoke run of the first
# harness test (Haiku, 30000 window: $0.146 for 2 arms, N=1, 2 types, so about $0.073 a unit) with a 1.4 factor for the neutral forks and hooks.
HAIKU_CACHE_READ, HAIKU_CACHE_WRITE, HAIKU_OUTPUT = 0.10, 1.25, 5.0
PF_COMPACT_UNIT = 0.073 * 1.4
PF_Z_COMPACT = 0.03
PF_ARM_FACTOR = {"A": 1.0, "B": 1.0, "R": 1.05, "K": 1.2, "O": 1.0}
PF_NC_TURNS, PF_NC_CTX0, PF_NC_CTX1, PF_FORKS, PF_SLACK = 75, 8000, 90000, 15, 1.2    # design-3 mini session: 75 turns, context 8k to 90k, 15 forks per arm
PF_O3, PF_Z3, PF_SELFCHECK, PF_MCP = 0.12, 0.08, 0.15, 0.02
PF_BIG_TOKENS = 300000
PF_BIG_LOW = PF_BIG_TOKENS * PRICE_CACHE_WRITE / 1e6 * 1.05      # one cold-cache Sonnet turn with a 300k paste, standard price
PF_BIG_HIGH = 2 * PF_BIG_LOW                                   # if the price above 200k tokens is double (assumed worst case)


def pf_nc_main():
    read = PF_NC_TURNS * (PF_NC_CTX0 + PF_NC_CTX1) / 2 * HAIKU_CACHE_READ / 1e6
    write = PF_NC_CTX1 * HAIKU_CACHE_WRITE / 1e6
    out = PF_NC_TURNS * D3_OUT_PER_TURN * HAIKU_OUTPUT / 1e6
    return (read + write + out) * PF_SLACK


def pf_nc_forks():
    return PF_FORKS * (PF_NC_CTX1 * HAIKU_CACHE_READ / 1e6 + 800 * HAIKU_OUTPUT / 1e6) * PF_SLACK


def preflight_unit(arm, cond, arms=("A", "B", "R", "K", "Z", "O")):
    """USD of one preflight unit. The no-compaction unit of arm A includes the forks of arms A, B, R and K (and the MCP check turn of K)."""
    f = PF_ARM_FACTOR.get(arm, 1.0)
    if arm == "Z":
        return PF_Z_COMPACT if cond == "compact" else PF_Z3
    if cond == "compact":
        return PF_COMPACT_UNIT * f
    if arm == "O":
        return PF_O3
    if arm == "A":
        return pf_nc_main() + pf_nc_forks() * sum(PF_ARM_FACTOR[x] for x in ("A", "B", "R", "K") if x in arms) + (PF_MCP if "K" in arms else 0)
    return 0.0


def estimate_preflight(arms=("A", "B", "R", "K", "Z", "O"), conditions=("compact", "nocompact")):
    """(total USD with the high price of the big turn, lines). Lines show the parts."""
    lines, total = [], 0.0
    for a in arms:
        for c in conditions:
            if c == "nocompact" and a in ("B", "R", "K"):
                continue            # forks inside the unit of arm A
            u = preflight_unit(a, c, arms)
            total += u
            lines.append("arm %s, %s: $%.2f" % (a, {"compact": "with a compaction (N=1, types R,X)", "nocompact": "design-3 mini session"
                                                    + (" and the forks of arms A,B,R,K" if a == "A" else "")}[c], u))
    sc, big_lo, big_hi = PF_SELFCHECK, PF_BIG_LOW, PF_BIG_HIGH
    lines.append("deny-rule selfcheck (arms A and B, Haiku): $%.2f" % sc)
    lines.append("one Sonnet turn with a 300k-token paste: $%.2f at the standard price, $%.2f if the price above 200k tokens is double" % (big_lo, big_hi))
    return total + sc + big_hi, lines


def estimate_per_seed(cfg):
    """USD estimate of one seed of a run config, for the cost gate of the run. None if there is no estimate (the slice preset)."""
    kind = cfg.get("kind", "")
    arms, conds = cfg["arms"], cfg.get("conditions", ["compact"])
    if kind == "preflight":
        return estimate_preflight(arms, conds)[0]
    if kind == "slice":
        return None
    if cfg.get("probe_design", 1) >= 3:
        return estimate3(1, arms, cfg["n"], conds, cfg.get("phrasings", ["cued"]), n_by_arm=cfg.get("n_by_arm"))[2]
    return estimate(1, arms, cfg["n"], conds, cfg.get("phrasings", ["cued"]))[2]


def unit_estimate(cfg, arm, cond):
    """USD estimate of one work unit (seed, arm, condition), for the per-session cost gate. None if there is none."""
    kind = cfg.get("kind", "")
    if kind == "slice":
        return None
    arms = cfg["arms"]
    if kind == "preflight":
        return preflight_unit(arm, cond, arms)
    if cfg.get("probe_design", 1) >= 3:
        if arm == "A" and cond == "nocompact":        # the unit holds the forks of arms B, R and K
            return estimate3(1, [x for x in arms if x in ("A", "B", "R", "K")], cfg["n"], ["nocompact"], cfg.get("phrasings", ["cued"]))[2]
        return estimate3(1, [arm], cfg["n"], [cond], cfg.get("phrasings", ["cued"]), n_by_arm=cfg.get("n_by_arm"))[2]
    return estimate(1, [arm], cfg["n"], [cond], cfg.get("phrasings", ["cued"]))[2]


def render3(name, cfg):
    total, lines, per_seed = estimate3(cfg["seeds"], cfg["arms"], cfg["n"], cfg["conditions"], cfg["phrasings"], skip=cfg.get("skip", ()), n_by_arm=cfg.get("n_by_arm"))
    out = ["%s: %d seeds, arms %s, N=%s, conditions %s, phrasings %s" % (
        name, cfg["seeds"], ",".join(cfg["arms"]), ",".join(map(str, cfg["n"])), ",".join(cfg["conditions"]), ",".join(cfg["phrasings"]))]
    out += ["  " + x for x in lines]
    out.append("  per seed $%.2f; total about $%.0f (an estimate, plan for plus or minus 40%%)" % (per_seed, total))
    return total, "\n".join(out)


ASSUMPTIONS3 = [
    "Assumptions of the design-3 estimate (none measured):",
    "  - prices (USD per million tokens): cache read %.2f, cache write %.2f, output %.2f (assumed list prices of a Sonnet-class model; check before a real run)" % (PRICE_CACHE_READ, PRICE_CACHE_WRITE, PRICE_OUTPUT),
    "  - a no-compaction session has %d turns, the context grows linearly from %d to %d tokens, a main reply is %d tokens" % (D3_TURNS, D3_CONTEXT_START, D3_CONTEXT_END, D3_OUT_PER_TURN),
    "  - %d probe forks at the end, each reads the full 300k context from cache and writes %d tokens" % (D3_FORKS, D3_FORK_OUT),
    "  - %.0f%% slack for retries and hook time; arm factors on a no-compaction unit: %s" % (100 * (D3_SLACK - 1), ", ".join("%s x%.2f" % kv for kv in ARM_FACTOR.items())),
    "  - a unit with compactions is the design-2 model (Wave 1 and Wave 2 numbers, probe forks 30% of a unit), arm K x1.2",
    "  - if the cache has expired between turns (more than 5 minutes), the cache-read cost becomes a cache-write cost: the cost can be 5 times higher for those turns",
    "  - preflight: assumed Haiku-class prices (cache read %.2f, write %.2f, output %.2f); the design-3 mini session has %d turns with context %d to %d tokens and %d forks for each of arms A, B, R, K; %.0f%% slack; a compaction unit is $%.3f (measured smoke run x1.4); the Sonnet 300k turn is a cold-cache write at %.2f USD per million tokens (x2 as the worst case if the price above 200k tokens doubles)" % (
        HAIKU_CACHE_READ, HAIKU_CACHE_WRITE, HAIKU_OUTPUT, PF_NC_TURNS, PF_NC_CTX0, PF_NC_CTX1, PF_FORKS, 100 * (PF_SLACK - 1), PF_COMPACT_UNIT, PRICE_CACHE_WRITE),
    "  - design-3 no-compaction condition: the main session runs ONCE for each seed (arm A), then arms A, B, R, K fork it (15 forks each); controls Z %.2f and O %.2f USD per seed" % (D3_CONTROL_USD["Z"], D3_CONTROL_USD["O"]),
    "  - power: base accuracy 80% at start and end, seed-level spread 10 points, normal interval, seeds are the resampling unit",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int)
    ap.add_argument("--arms")
    ap.add_argument("--n")
    ap.add_argument("--conditions")
    ap.add_argument("--phrasings")
    a = ap.parse_args(argv)
    if not any([a.seeds, a.arms, a.n, a.conditions, a.phrasings]):
        print("PROBE DESIGN 3 (litm3-smoke, litm3-pilot, litm3-full)")
        for name, cfg in (("SMOKE (litm3-smoke)", SMOKE3), ("NC SMOKE (litm3-nc-smoke)", NC_SMOKE3), ("PILOT (litm3-pilot)", PILOT3), ("FULL (litm3-full, 23 seeds, N up to 30)", FULL3), ("FULL VARIANT without arm R in the compaction condition", FULL3_NO_R),
                          ("FULL V3 (litm3-full-v3, the default full run: R and K to N=10)", FULL3_V3), ("FULL V5 (litm3-full-v5: no R, K at N=10 only)", FULL3_V5)):
            print(render3(name, cfg)[1])
        pf_total, pf_lines = estimate_preflight()
        print("PREFLIGHT (preflight): Haiku, 1 dev seed, arms A,B,R,K,Z,O, types R,X, N=1, both conditions, cued, neutral and nonliteral, plus selfcheck and one Sonnet 300k turn")
        print("\n".join("  " + x for x in pf_lines))
        print("  total about $%.2f with the high price of the big turn (target: about $5 at most; the Haiku prices and the 75-turn mini session are assumptions, see below)" % pf_total)
        print("\nH1 power at 20 seeds (arm A, no compaction, cued):")
        print("\n".join(power_lines(20)))
        print("\n".join(ASSUMPTIONS3))
        print("\nPROBE DESIGN 2 (kept for reference)")
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
