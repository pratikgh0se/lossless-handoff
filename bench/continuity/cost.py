#!/usr/bin/env python3
"""Cost estimate of the continuity benchmark. It uses no model and no network.

The numbers come from the real Sonnet runs of the full evaluation (Wave 1 and Wave 2, run on 2026-11-18),
not from list prices. Each constant says where it comes from. A pilot gives better numbers: copy them here.

Usage: cost.py [--seeds N] [--arms A,B,R] [--n 1,3,10] [--conditions compact,nocompact] [--phrasings cued,neutral]
       cost.py            (no option: print the pilot and the full run of the lost-in-the-middle design)
"""
import argparse, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design4  # noqa: E402

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

# ---- CALIBRATION from the first real preflight run (20261122T110446-preflight, Haiku, 1 seed, 30000 window, $2.92 in total).
# Each ratio is real cost divided by the estimate of this file before the run. Real costs come from calls.jsonl (delta_usd) of that run.
#   no-compaction main session (75 turns):   real $0.0921, estimate $0.6435  -> 0.143  (real replies were about 12 tokens, not 150)
#   forks of arm A (15 forks):               real $0.1092, estimate $0.2340  -> 0.467  (B: $0.1104, R: $0.1151, so B x1.01, R x1.05 of A)
#   forks of arm K (16 forks, 85 turns):     real $0.4279 = 3.92 x the forks of arm A (MCP search turns and one cache miss of 91k tokens)
#   compaction unit (A, B, R, K):            real $0.033, $0.031, $0.033, $0.036 against $0.102 to $0.123 -> about 0.31
#   Z compaction 0.15, Z design 3 0.26, O design 3 (turns plus forks, $0.024) 0.20
#   big Sonnet turn: 467,684 tokens cost $1.7979; the standard-price estimate is $1.6894, so the price ratio is 1.06 (no long-context premium seen)
# The ratios come from Haiku at a 90k context and one seed. They are applied to the Sonnet 300k model as a first calibration: keep the +-40% band.
CAL_NC_MAIN, CAL_NC_FORK, K_FORK_VS_A = 0.143, 0.467, 3.92
CAL_PF_COMPACT, CAL_PF_Z_COMPACT, CAL_PF_Z_NC, CAL_PF_O_NC = 0.31, 0.15, 0.26, 0.20
BIG_PRICE_RATIO = 1.06       # real cost / standard price of the big Sonnet turn (assumed list prices above)
ARM_FACTOR["K"] = K_FORK_VS_A     # measured: replaces the assumed 1.2 (affects the forks of arm K in the no-compaction condition only)
# F79 arms G (K + F77 + F80) and P (K + F78): a fork that fires costs about 2 x a plain K fork (contract 0.18); assumed share of fired forks 60% for G and 30% for P.
ARM_FACTOR["G"], ARM_FACTOR["P"] = K_FORK_VS_A * 1.6, K_FORK_VS_A * 1.3
# Design 3 compaction condition: the design-2 compaction unit, with the K factor above.


def d3_nc_main():
    """USD of the main no-compaction session of design 3 (run once for each seed, as arm A, no forks), from the token model above."""
    avg_ctx = (D3_CONTEXT_START + D3_CONTEXT_END) / 2
    read = D3_TURNS * avg_ctx * PRICE_CACHE_READ / 1e6
    write = D3_CONTEXT_END * PRICE_CACHE_WRITE / 1e6
    out = D3_TURNS * D3_OUT_PER_TURN * PRICE_OUTPUT / 1e6
    return (read + write + out) * D3_SLACK * CAL_NC_MAIN


def d3_nc_forks():
    """USD of the probe forks of ONE arm on the finished no-compaction session (15 forks, each reads the full context from cache)."""
    return D3_FORKS * (D3_CONTEXT_END * PRICE_CACHE_READ / 1e6 + D3_FORK_OUT * PRICE_OUTPUT / 1e6) * D3_SLACK * CAL_NC_FORK


def d3_nc_unit():
    """USD of one design-3 no-compaction session of arm A with its own forks."""
    return d3_nc_main() + d3_nc_forks()


D3_CONTROL_USD = {"Z": round(0.15 * CAL_PF_Z_NC, 3), "O": round(0.30 * CAL_PF_O_NC, 3)}      # no-compaction controls of design 3: Z = amnesia forks of an empty session, O = short oracle (about 35 small turns)


# ---- design 4 (2026-11-22). Real figures of the Sonnet smoke run 20261122T123857-litm3-smoke (calls.jsonl, design 3, 75 turns, 15 forks):
#   main no-compaction session of arm A: $3.055; its 15 forks: $0.671 (A), $0.671 (B), $0.683 (R), $1.405 (K, 45 turns).
#   compaction units (N up to 3, with their forks): A $6.56, B $6.63, R $6.49, K $6.73. Total of the run: $32.90.
# Design 4: 105 turns (x1.4 of the main session), 36 forks per arm (3 phrasings x 12 chunks of at most 4 chains). The compaction condition is as in design 3.
SMOKE_REAL = {"main_nc_75_turns": 3.055, "forks_15": {"A": 0.671, "B": 0.671, "R": 0.683, "K": 1.405}, "total": 32.90}
# Probe forks hold at most 4 chains each (design4.MAX_ITEMS_PER_PROBE): 36 forks for each arm (L1-L4, 3 phrasings), 45 with L5. Before: 12 and 15.
D4_TURNS = 105
D4_FORKS = design4.n_probe_forks()
D4_MAIN = SMOKE_REAL["main_nc_75_turns"] * D4_TURNS / 75.0            # about $4.28 (the design pass says about $4.30)
D4_A_FORK_UNIT = SMOKE_REAL["forks_15"]["A"] / 15.0                    # real: $0.0447 for one fork (the smoke: 15 forks cost $0.671)
D4_A_FORKS = D4_A_FORK_UNIT * D4_FORKS                                 # about $1.61 for 36 forks (was $0.70 for 12)
D4_FORK_FACTOR = {a: v / SMOKE_REAL["forks_15"]["A"] for a, v in SMOKE_REAL["forks_15"].items()}    # real: B 1.00, R 1.02, K 2.09
def d4_nc_fork_estimate(arm):
    """USD estimate of one design-4 no-compaction probe fork of an arm (Sonnet prices, 300k context read from the cache)."""
    return D4_A_FORKS / float(D4_FORKS) * D4_FORK_FACTOR.get(arm, 1.0)


D4_FORK_FACTOR["G"], D4_FORK_FACTOR["P"] = D4_FORK_FACTOR["K"] * 1.6, D4_FORK_FACTOR["K"] * 1.3       # assumed (F79), not measured
# Arm O at about $0.9 (audit of PR #35: its short session holds the 69 plant turns, 3 phrasings of 4 levels of forks); the per-session cost gate
# (gates.session_reasons: above 2 x the estimate and above the estimate plus $0.25) then allows up to $1.80, so a $0.9 session does not trip it.
D4_CONTROL_USD = {"Z": 0.75, "O": 1.40}      # Z and O forks are small (short context), 3 times as many: Z 0.25 x 3; O 0.9 plus 24 forks at about $0.02
D4_TURNS_L5, D4_FORKS_L5 = 165, design4.n_probe_forks(design4.D4_LEVELS + ("L5",))                                      # the L5 escalation: 4 levels plus L5, one session of 165 turns


def d4_scale(levels=None):
    """(main session USD, factor of the fork cost) for the levels of a design-4 run: L1-L4 is 105 turns and 36 forks, with L5 it is 165 turns and 45 forks."""
    if levels and "L5" in levels:
        return SMOKE_REAL["main_nc_75_turns"] * D4_TURNS_L5 / 75.0, D4_FORKS_L5 / float(D4_FORKS)
    return D4_MAIN, 1.0


def ACTION_SCALE(phrasings):
    """Design 4 estimates are for the 3 phrasings (36 forks for each arm); the action probes add a fourth phrasing: 4/3 of the forks."""
    return 4.0 / 3.0 if "action" in (phrasings or ()) else 1.0


def d4_nc_main():
    return D4_MAIN


def d4_nc_forks(arm="A"):
    return D4_A_FORKS * D4_FORK_FACTOR.get(arm, 1.0)


def estimate3(seeds, arms, n, conditions, phrasings=("cued", "neutral", "nonliteral"), skip=(), n_by_arm=None, design=3, levels=None):
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
                u = (BASE + PER_COMPACTION * max(nn)) * mult * (1.2 if a in ("K", "G", "P") else 1.0)      # compaction units: Wave numbers, not calibrated
                per_seed += u
                lines.append("arm %s, with compactions (N up to %d): $%.2f per seed" % (a, max(nn), u))
            if "nocompact" in conditions and (a, "nocompact") not in skip:
                if design >= 4:
                    main4, fk4 = d4_scale(levels)
                    u = d4_nc_forks(a) * fk4 * ACTION_SCALE(phrasings) + (main4 if a == "A" else 0.0)      # 4 phrasings (action) = 4/3 of the forks
                else:
                    u = d3_nc_forks() * f + (d3_nc_main() if a == "A" else 0.0)
                per_seed += u
                lines.append("arm %s, no compaction, design 3 (about 300k tokens): $%.2f per seed (%s)" % (
                    a, u, "main session once, plus its forks" if a == "A" else "forks of the session of arm A only"))
        elif a == "Z":
            if "compact" in conditions:
                per_seed += Z_UNIT * mult
                lines.append("arm Z (no history, one run): $%.2f per seed" % (Z_UNIT * mult))
            if "nocompact" in conditions:
                zc = (D4_CONTROL_USD if design >= 4 else D3_CONTROL_USD)["Z"] * (ACTION_SCALE(phrasings) if design >= 4 else 1.0)
                per_seed += zc
                lines.append("arm Z, design %d amnesia forks: $%.2f per seed" % (design, zc))
        elif a == "O":
            if "compact" in conditions:
                per_seed += O_UNIT * mult
                lines.append("arm O (oracle, short session, one run): $%.2f per seed" % (O_UNIT * mult))
            if "nocompact" in conditions:
                oc = (D4_CONTROL_USD if design >= 4 else D3_CONTROL_USD)["O"] * (ACTION_SCALE(phrasings) if design >= 4 else 1.0)
                per_seed += oc
                lines.append("arm O, design %d short oracle: $%.2f per seed" % (design, oc))
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
PF_COMPACT_UNIT = 0.073 * 1.4 * CAL_PF_COMPACT      # calibrated with the first preflight run
PF_Z_COMPACT = 0.03 * CAL_PF_Z_COMPACT
PF_ARM_FACTOR = {"A": 1.0, "B": 1.0, "R": 1.05, "K": 1.2, "O": 1.0, "G": 1.9, "P": 1.5}      # G and P: assumed (F79), a fork that fires costs about 2 x a K fork
PF_NC_TURNS, PF_NC_CTX0, PF_NC_CTX1, PF_FORKS, PF_SLACK = 75, 8000, 90000, 15, 1.2    # design-3 mini session: 75 turns, context 8k to 90k, 15 forks per arm
PF_O3, PF_Z3, PF_SELFCHECK, PF_MCP = 0.12 * CAL_PF_O_NC, 0.08 * CAL_PF_Z_NC, 0.15, 0.02
PF_BIG_TOKENS = 300000
PF_BIG_LOW = PF_BIG_TOKENS * PRICE_CACHE_WRITE / 1e6 * 1.05      # one cold-cache Sonnet turn with a 300k paste, standard price
PF_BIG_HIGH = PF_BIG_TOKENS * PRICE_CACHE_WRITE / 1e6 * BIG_PRICE_RATIO     # measured price ratio 1.06 (no long-context premium); the cap of the turn is $2.50


# ---- preflight4 (design 4, Haiku): figures of the real run 20261122T162409-preflight4 (calls.jsonl, USD, 1 seed). A-nc: 105 turns $0.0694 and 36 forks of arm A
# $0.4203; O-nc: 69 turns $0.0297 and 36 forks $0.3160 ($0.3457); Z-nc: 36 calls $0.0466; O with a compaction: $0.0954; the big turn $0.0365 (it was a small paste then).
# The design-3 figures were 5 to 20 times too low for O-nc and Z-nc, and the per-session gate stopped the run on O-nc.
PF4_SLACK = 1.15
PF4_A_TURNS, PF4_A_FORK_ALL, PF4_O_NC, PF4_Z_NC, PF4_O_COMPACT = 0.0694, 0.4203, 0.3457, 0.0466, 0.0954
PF4_FORKS = 36               # probe forks of one arm in a design-4 no-compaction session (4 levels x 3 phrasings x 3 chunks)


def pf4_nc_unit_parts(arms=("A", "B", "R", "K", "Z", "O")):
    """(turns USD of the main session of arm A, USD of the forks of arm A, scale of the forks of the arms that fork it) for design 4."""
    scale = sum(PF_ARM_FACTOR[x] for x in ("A", "B", "R", "K", "G", "P") if x in arms)
    return PF4_A_TURNS * PF4_SLACK, PF4_A_FORK_ALL * PF4_SLACK, scale


def pf_nc_main():
    read = PF_NC_TURNS * (PF_NC_CTX0 + PF_NC_CTX1) / 2 * HAIKU_CACHE_READ / 1e6
    write = PF_NC_CTX1 * HAIKU_CACHE_WRITE / 1e6
    out = PF_NC_TURNS * D3_OUT_PER_TURN * HAIKU_OUTPUT / 1e6
    return (read + write + out) * PF_SLACK


def pf_nc_forks():
    return PF_FORKS * (PF_NC_CTX1 * HAIKU_CACHE_READ / 1e6 + 800 * HAIKU_OUTPUT / 1e6) * PF_SLACK


def preflight_unit(arm, cond, arms=("A", "B", "R", "K", "Z", "O"), design=3):
    """USD of one preflight unit. The no-compaction unit of arm A includes the forks of arms A, B, R and K (and the MCP check turn of K).
    design 4: the figures of the real preflight4 run (cheap Haiku units with 36 forks each)."""
    f = PF_ARM_FACTOR.get(arm, 1.0)
    if design >= 4:
        if cond == "nocompact":
            if arm == "Z":
                return PF4_Z_NC * PF4_SLACK
            if arm == "O":
                return PF4_O_NC * PF4_SLACK
            if arm == "A":
                turns, forks, scale = pf4_nc_unit_parts(arms)
                return turns + forks * scale + (PF_MCP if "K" in arms else 0)
            return 0.0
        if arm == "O":
            return PF4_O_COMPACT * PF4_SLACK
    if arm == "Z":
        return PF_Z_COMPACT if cond == "compact" else PF_Z3
    if cond == "compact":
        return PF_COMPACT_UNIT * f
    if arm == "O":
        return PF_O3
    if arm == "A":
        return pf_nc_main() + pf_nc_forks() * sum(PF_ARM_FACTOR[x] for x in ("A", "B", "R", "K", "G", "P") if x in arms) + (PF_MCP if "K" in arms else 0)
    return 0.0


def estimate_preflight(arms=("A", "B", "R", "K", "Z", "O"), conditions=("compact", "nocompact"), design=3):
    """(total USD with the high price of the big turn, lines). Lines show the parts."""
    lines, total = [], 0.0
    for a in arms:
        for c in conditions:
            if c == "nocompact" and a in ("B", "R", "K", "G", "P"):
                continue            # forks inside the unit of arm A
            u = preflight_unit(a, c, arms, design)
            total += u
            lines.append("arm %s, %s: $%.2f" % (a, {"compact": "with a compaction (N=1, types R,X)", "nocompact": "design-%d mini session" % (4 if design >= 4 else 3)
                                                    + (" and the forks of arms A,B,R,K" if a == "A" else "")}[c], u))
    sc, big_lo, big_hi = PF_SELFCHECK, PF_BIG_LOW, PF_BIG_HIGH
    lines.append("deny-rule selfcheck (arms A and B, Haiku): $%.2f" % sc)
    lines.append("one Sonnet turn with a 300k-token paste: $%.2f at the standard price, $%.2f at the measured price ratio %.2f (cap $2.50)" % (big_lo, big_hi, BIG_PRICE_RATIO))
    return total + sc + big_hi, lines


def estimate_per_seed(cfg):
    """USD estimate of one seed of a run config, for the cost gate of the run. None if there is no estimate (the slice preset)."""
    kind = cfg.get("kind", "")
    arms, conds = cfg["arms"], cfg.get("conditions", ["compact"])
    if kind.startswith("preflight"):
        return estimate_preflight(arms, conds, 4 if cfg.get("probe_design", 1) >= 4 else 3)[0]
    if kind == "slice":
        return None
    if cfg.get("probe_design", 1) >= 3:
        return estimate3(1, arms, cfg["n"], conds, cfg.get("phrasings", ["cued"]), n_by_arm=cfg.get("n_by_arm"), design=cfg["probe_design"], levels=cfg.get("levels"))[2]
    return estimate(1, arms, cfg["n"], conds, cfg.get("phrasings", ["cued"]))[2]


def unit_estimate(cfg, arm, cond):
    """USD estimate of one work unit (seed, arm, condition), for the per-session cost gate. None if there is none."""
    kind = cfg.get("kind", "")
    if kind == "slice":
        return None
    arms = cfg["arms"]
    if kind.startswith("preflight"):
        return preflight_unit(arm, cond, arms, 4 if cfg.get("probe_design", 1) >= 4 else 3)
    if cfg.get("probe_design", 1) >= 3:
        dz = cfg["probe_design"]
        if arm == "A" and cond == "nocompact":        # the unit holds the forks of arms B, R and K
            return estimate3(1, [x for x in arms if x in ("A", "B", "R", "K", "G", "P")], cfg["n"], ["nocompact"], cfg.get("phrasings", ["cued"]), design=dz, levels=cfg.get("levels"))[2]
        return estimate3(1, [arm], cfg["n"], [cond], cfg.get("phrasings", ["cued"]), n_by_arm=cfg.get("n_by_arm"), design=dz, levels=cfg.get("levels"))[2]
    return estimate(1, [arm], cfg["n"], [cond], cfg.get("phrasings", ["cued"]))[2]


CALIBRATE4 = {"seeds": 2, "arms": ["A", "O", "Z"], "n": [1], "conditions": ["nocompact"], "phrasings": ["cued", "neutral", "nonliteral", "action"], "design": 4}      # F79: action probes for A, O and Z
ORACLE_CHECK4 = {"seeds": 1, "arms": ["O"], "n": [1], "conditions": ["nocompact"], "phrasings": ["cued", "neutral", "nonliteral", "action"], "design": 4}      # litm4-oracle-check
CALIBRATE4_L5 = dict(CALIBRATE4, seeds=1, levels=["L1", "L2", "L3", "L4", "L5"])
SMOKE4 = dict(SMOKE3, design=4)
PILOT4 = dict(PILOT3, design=4)
FULL4_V3 = dict(FULL3_V3, design=4)


def render3(name, cfg):
    total, lines, per_seed = estimate3(cfg["seeds"], cfg["arms"], cfg["n"], cfg["conditions"], cfg["phrasings"], skip=cfg.get("skip", ()), n_by_arm=cfg.get("n_by_arm"),
                                       design=cfg.get("design", 3), levels=cfg.get("levels"))
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
    "  - preflight: assumed Haiku-class prices (cache read %.2f, write %.2f, output %.2f); the design-3 mini session has %d turns with context %d to %d tokens and %d forks for each of arms A, B, R, K; %.0f%% slack; a compaction unit is $%.3f (measured smoke run x1.4); the Sonnet 300k turn is a cold-cache write at %.2f USD per million tokens (measured price ratio %.2f; calibration in the header of this file)" % (
        HAIKU_CACHE_READ, HAIKU_CACHE_WRITE, HAIKU_OUTPUT, PF_NC_TURNS, PF_NC_CTX0, PF_NC_CTX1, PF_FORKS, 100 * (PF_SLACK - 1), PF_COMPACT_UNIT, PRICE_CACHE_WRITE, BIG_PRICE_RATIO),
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
        print("\nPROBE DESIGN 4 (litm4-calibrate, litm4-smoke, litm4-pilot, litm4-full-v3): no-compaction condition with update chains; compaction condition as in design 3")
        print("  real figures of the Sonnet smoke: main $%.2f for 75 turns, 15 A forks $%.2f; design 4: main $%.2f (105 turns), A forks $%.2f (%d forks), K forks x%.2f" % (
            SMOKE_REAL["main_nc_75_turns"], SMOKE_REAL["forks_15"]["A"], D4_MAIN, D4_A_FORKS, D4_FORKS, D4_FORK_FACTOR["K"]))
        for name, cfg in (("ORACLE CHECK (litm4-oracle-check: arm O only, 1 seed, all levels, 4 phrasings; run it before the calibration)", ORACLE_CHECK4),
                          ("CALIBRATION (litm4-calibrate: arms A,O,Z; the second seed only if a cell of A is at 45-90%)", CALIBRATE4), ("L5 ESCALATION (litm4-calibrate-l5, only if decide says add_l5: one seed, 165 turns, replaces all cells)", CALIBRATE4_L5),
                          ("SMOKE (litm4-smoke)", SMOKE4), ("PILOT (litm4-pilot, dev seeds 3 and 4)", PILOT4), ("FULL V3 (litm4-full-v3)", FULL4_V3)):
            print(render3(name, cfg)[1])
        print("  calibration if the main session costs $8 (twice the figure): about $%.0f for 2 seeds (plus or minus 40%%)" % (2 * (8.0 + D4_A_FORKS + D4_CONTROL_USD["O"] + D4_CONTROL_USD["Z"])))
        print()
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
