#!/usr/bin/env python3
"""The fair re-test (run.sh fair-preflight, fair-step1, fair-step2). SPEC.md section 16, thresholds.json block "fair".

Arm Af is plain Claude Code (no Lossless Handoff hooks) that may read, grep and glob its OWN transcripts. Arm B is Lossless Handoff.
This module holds the logic that makes no model call:
  - scan_reads: what the tool calls of an Af session did with transcripts (own, other arm, state folder);
  - verdict: the pre-registered rule (PASS, FAIL, INVALID or INCOMPLETE) with the INVALID gates;
  - stop_rule: after step 1, if Af scores 90% or more of the hinted N=10 items, step 2 is not needed;
  - section: the markdown that report.py puts at the top of summary.md (counts, no bootstrap interval with 2 seeds);
  - preflight_check: the PASS or FAIL checklist of run.sh fair-preflight.

Usage: fair.py preflight-check <run-id> | stop-check <run-id>
       fair.py new8 <fair6-step1 dir> <fair6-step2 dir> <fair6x-step1 dir> <fair6x-step2 dir>   (FR.8.10 secondary result, $0)
"""
import json, math, os, random, re, sys
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
TYPES = ["R", "D", "X", "S", "E", "C", "M"]
SE_TYPES = ("S", "E")
OTHER_TYPES = ("R", "D", "X", "C", "M")
ARMS_ALL = "A B O Z R K G P".split()
# A path of another arm's project folder (lhbench-<run>-s<seed>-<arm>, also the -nc form) or of a Lossless Handoff state folder (lhbench-...-state).
# The arm letter must end the word, so that "-Af" (the own arm) never matches the arm "A".
OTHER_ARM_RE = re.compile(r"lhbench-[^\s'\"/]*-state|lhbench-[^\s'\"/]*-s\d+-(?:%s)(?![A-Za-z0-9])" % "|".join(ARMS_ALL))
DENY_MARKERS = ("has been denied", "blocked by a deny rule", "Permission to use", "deny rule")
STOP_FMT = "STOP: plain Claude Code with search scores %d/%d; step 2 not needed"


def _jl(path):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def own_stem(run_id, seed):
    return "lhbench-%s-s%d-Af" % (run_id, seed)


def _result_text(b):
    t = b.get("content")
    if isinstance(t, list):
        t = " ".join(x.get("text", "") for x in t if isinstance(x, dict))
    return str(t or "")


def scan_reads(records, marker=None):
    """What the tool calls of one Af transcript did. Only records from the probe message (the one with `marker`) on count, because a fork copies the history.
    Returns {"own_ok": n, "own_denied": n, "other": [input text, ...], "calls": n}.
    - other: a tool call whose input names a project folder of another arm or a state folder (OTHER_ARM_RE) and that SUCCEEDED (the gate).
    - af_blocked_attempts: the same kind of call whose result is an error or a denial. Counted, not a gate.
    - own_ok / own_denied: a tool call whose input names a transcript (".claude/projects" or ".jsonl") and not another arm. denied: the result is an error or a denial."""
    seen = marker is None
    uses, results = {}, {}
    order = []
    for r in records:
        if not isinstance(r, dict):
            continue
        m = r.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if not seen:
            txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
            if marker in txt and r.get("type") == "user":
                seen = True
            continue
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id")] = (b.get("name"), json.dumps(b.get("input"), ensure_ascii=False))
                order.append(b.get("id"))
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = (bool(b.get("is_error")), _result_text(b))
    out = {"own_ok": 0, "own_denied": 0, "other": [], "af_blocked_attempts": 0, "calls": len(order)}
    for i in order:
        name, s = uses[i]
        if OTHER_ARM_RE.search(s):
            err, txt = results.get(i, (False, ""))
            if err or any(mk in txt for mk in DENY_MARKERS):
                out["af_blocked_attempts"] += 1       # blocked: cannot contaminate the answers, so no gate (counted only)
            else:
                out["other"].append("%s %s" % (name, s[:200]))      # SUCCEEDED: the gate
            continue
        if ".claude/projects" in s or ".jsonl" in s:
            err, txt = results.get(i, (False, ""))
            if any(mk in txt for mk in DENY_MARKERS) or (err and "No such file" not in txt):
                out["own_denied"] += 1
            elif i in results:
                out["own_ok"] += 1
    return out


# ---- data

def _acc(rows, arm, phrasing, types=TYPES, n=None, seed=None):
    c = t = 0
    for r in rows:
        if r.get("arm") != arm or r.get("phrasing", "cued") != phrasing or r.get("condition", "compact") != "compact" or r.get("type") not in types or r.get("refused"):
            continue
        if n is not None and r.get("n_target") != n:
            continue
        if seed is not None and r.get("seed") != seed:
            continue
        t += 1
        c += 1 if r.get("correct") else 0
    return c, t


def _pct(x):
    return "%.0f%%" % (100 * x)


def _frac(c, t):
    return "%d of %d (%s)" % (c, t, _pct(c / t)) if t else "no data"


def _gate(name, thr, measured, ok):
    return {"gate": name, "threshold": thr, "measured": measured, "result": "NO DATA" if ok is None else ("PASS" if ok else "FAIL")}


def _arms_present(rows):
    return {r.get("arm") for r in rows}


def read_totals(rows, sessions):
    """(own_denied, other list) over the Af probe forks (once for each fork) and the Af main sessions."""
    forks, own_denied, other = {}, 0, []
    for r in rows:
        if r.get("arm") == "Af" and r.get("af_reads") is not None:
            forks[(r.get("run"), r.get("fork_sid") or id(r))] = r["af_reads"]
    for k, v in forks.items():
        own_denied += v.get("own_denied", 0)
        other += v.get("other", [])
    for s in sessions:
        if s.get("arm") == "Af" and s.get("af_reads"):
            own_denied += s["af_reads"].get("own_denied", 0)
            other += s["af_reads"].get("other", [])
    return own_denied, other


def blocked_total(rows, sessions):
    """Blocked attempts on another arm folder or a state folder (not a gate), over the Af forks (once each) and Af main sessions."""
    forks = {(r.get("run"), r.get("fork_sid") or id(r)): r["af_reads"] for r in rows if r.get("arm") == "Af" and r.get("af_reads") is not None}
    return sum(v.get("af_blocked_attempts", 0) for v in forks.values()) + sum((s.get("af_reads") or {}).get("af_blocked_attempts", 0) for s in sessions if s.get("arm") == "Af")


def validity_gates(rows, sessions, th):
    """The INVALID gates of FR.4. A gate for an arm with no rows is left out (step 1 has no arm B yet), except the gates of arm Af."""
    present = _arms_present(rows)
    out = []
    hinted = th["primary_phrasing"]
    c, t = _acc(rows, "O", hinted)
    out.append(_gate("Oracle arm O (no compaction)", ">= %s" % _pct(th["oracle_min_accuracy"]), "%s (%d/%d)" % (_pct(c / t), c, t) if t else "0/0", None if not t else c / t >= th["oracle_min_accuracy"]))
    c, t = _acc(rows, "Z", hinted)
    out.append(_gate("Amnesia arm Z (no history)", "<= %s" % _pct(th["amnesia_max_accuracy"]), "%s (%d/%d)" % (_pct(c / t), c, t) if t else "0/0", None if not t else c / t <= th["amnesia_max_accuracy"]))
    for a in ("Af", "B", "O"):
        if a not in present and a != "Af":
            continue
        sel = [r for r in rows if r.get("arm") == a and r.get("condition", "compact") == "compact" and not r.get("refused")]
        ok = sum(1 for r in sel if r.get("parse_ok"))
        out.append(_gate("Parse rate, arm %s" % a, ">= %s" % _pct(th["parse_rate_min"]), "%s (%d/%d)" % (_pct(ok / len(sel)), ok, len(sel)) if sel else "0/0",
                         None if not sel else ok / len(sel) >= th["parse_rate_min"]))
    need = th["compactions_per_main_session"]
    for a in ("Af", "B"):
        if a not in present and a != "Af":
            continue
        ss = [s for s in sessions if s.get("arm") == a and s.get("condition", "compact") == "compact"]
        bad = [s.get("name") for s in ss if (s.get("boundaries") or 0) < need]
        out.append(_gate("%d real compactions in every main session, arm %s" % (need, a), ">= %d" % need, "%d of %d sessions short%s" % (len(bad), len(ss), (": " + ", ".join(map(str, bad))) if bad else ""),
                         None if not ss else not bad))
    den, other = read_totals(rows, sessions)
    has_reads = any(r.get("arm") == "Af" and r.get("af_reads") is not None for r in rows)
    out.append(_gate("Denied reads of the own transcript of Af", "= %d" % th["denied_own_reads_max"], "%d" % den, (den <= th["denied_own_reads_max"]) if has_reads else None))
    out.append(_gate("Af tool calls that SUCCEEDED on a folder of another arm or a state folder", "= %d" % th["other_arm_reads_max"], "%d%s" % (len(other), (" (e.g. %s)" % other[0][:80]) if other else ""),
                     (len(other) <= th["other_arm_reads_max"]) if has_reads else None))
    return out


def stop_rule(rows, th_all):
    """(stop, x, y): Af at N=10 on the hinted probes. stop is True when x/y >= stop_af_min_rate."""
    th = th_all["fair"]
    x, y = _acc(rows, "Af", th["primary_phrasing"], TYPES, th["n"])
    return (y > 0 and x / y >= th["stop_af_min_rate"]), x, y


def _rule(rows, th, phrasing):
    """The pooled rule for one phrasing. Returns a dict."""
    n = th["n"]
    seeds = sorted({r["seed"] for r in rows if r.get("arm") in ("Af", "B") and r.get("condition", "compact") == "compact"})
    a, b = _acc(rows, "Af", phrasing, TYPES, n), _acc(rows, "B", phrasing, TYPES, n)
    out = {"phrasing": phrasing, "af": a, "b": b, "per_seed": {}, "seeds": seeds}
    sb = 0
    for s in seeds:
        sa, sbb = _acc(rows, "Af", phrasing, TYPES, n, s), _acc(rows, "B", phrasing, TYPES, n, s)
        out["per_seed"][s] = {"af": sa, "b": sbb}
        if sa[1] and sbb[1] and sbb[0] / sbb[1] >= sa[0] / sa[1]:
            sb += 1
    out["seeds_b_ge_af"] = sb
    if a[1] and b[1]:
        out["gain_points"] = round(100 * (b[0] / b[1] - a[0] / a[1]), 6)      # rounded: exactly 15.0 points must pass
        out["ok"] = out["gain_points"] >= th["min_gain_points"] and sb >= th["min_seeds_b_ge_af"]
        out["clear"] = out["gain_points"] >= th["min_gain_points"] and sb == len(seeds) and len(seeds) >= th["seeds"]
    else:
        out["gain_points"], out["ok"], out["clear"] = None, None, False
    return out


def verdict(rows, sessions, th_all):
    """Returns {"verdict": PASS|FAIL|INVALID|INCOMPLETE, "primary", "secondary", "gates", "invalid_gates", "stop", "stop_counts", "types"}."""
    th = th_all["fair"]
    gl = validity_gates(rows, sessions, th)
    invalid = [g["gate"] for g in gl if g["result"] != "PASS"]
    prim, sec = _rule(rows, th, th["primary_phrasing"]), _rule(rows, th, th["secondary_phrasing"])
    stop, x, y = stop_rule(rows, th_all)
    types = {}
    for label, ts in (("S and E", SE_TYPES), ("R D X C M", OTHER_TYPES)):
        types[label] = {ph: {arm: _acc(rows, arm, ph, ts, th["n"]) for arm in ("Af", "B")} for ph in (th["primary_phrasing"], th["secondary_phrasing"])}
    if any(g["result"] == "FAIL" for g in gl):
        overall = "INVALID"
    elif prim["ok"] is None:
        overall = "INCOMPLETE"       # arm Af or arm B has no data yet (step 1 alone)
    elif invalid:
        overall = "INVALID"          # a gate has no data (for example no arm O or Z rows)
    else:
        overall = "PASS" if prim["ok"] else "FAIL"
    return {"verdict": overall, "primary": prim, "secondary": sec, "gates": gl, "invalid_gates": invalid, "stop": stop, "stop_counts": [x, y], "types": types}


def _gap_words(rule, th):
    if rule["ok"] is None:
        return "no data"
    if rule["clear"]:
        return "B is ahead by %.0f points in every seed" % rule["gain_points"]
    return "no clear difference"


def section(rows, sessions, cfg, th_all):
    """(markdown lines, dict). Counts, not intervals. The wording of FR.5."""
    th = th_all["fair"]
    v = verdict(rows, sessions, th_all)
    p, s = v["primary"], v["secondary"]
    seeds = p["seeds"]
    y = p["af"][1]
    lines = ["## Fair re-test\n"]
    if v["stop"]:
        lines.append("**" + STOP_FMT % tuple(v["stop_counts"]) + "**\n")
    lines.append("**Verdict (primary rule, hinted probes, N=%d): %s.**\n" % (th["n"], v["verdict"]))
    if p["ok"] is None:
        lines.append("Small test: %d seeds, %d hinted questions, %s, %d compactions. Plain Claude Code that can search its own transcripts: %s. Lossless Handoff: %s. "
                     "Two seeds show a direction, not a proof.\n" % (len(seeds), y, cfg.get("model", "sonnet").capitalize(), th["n"], ("%d of %d" % p["af"]) if y else "no data",
                                                                    ("%d of %d" % p["b"]) if p["b"][1] else "not run yet"))
    else:
        lines.append("Small test: %d seeds, %d hinted questions, %s, %d compactions. Plain Claude Code that can search its own transcripts: %d of %d. Lossless Handoff: %d of %d. "
                     "Two seeds show a direction, not a proof.\n" % (len(seeds), y, cfg.get("model", "sonnet").capitalize(), th["n"], p["af"][0], y, p["b"][0], p["b"][1]))
        lines.append("Primary: %s. Secondary (neutral probes): %s.\n" % (_gap_words(p, th), _gap_words(s, th)))
    lines += ["| Probes | Af (can search) | B (Lossless Handoff) | B minus Af | Seeds with B >= Af |", "|---|---|---|---|---|"]
    for name, r in (("hinted (cued)", p), ("neutral", s)):
        lines.append("| %s | %s | %s | %s | %s |" % (name, _frac(*r["af"]), _frac(*r["b"]), "n/a" if r["gain_points"] is None else "%+.1f points" % r["gain_points"],
                                                   "%d of %d" % (r["seeds_b_ge_af"], len(r["seeds"])) if r["seeds"] else "n/a"))
    lines += ["", "Per seed (counts, hinted ; neutral):\n", "| Seed | Af | B |", "|---|---|---|"]
    for sd in p["seeds"]:
        lines.append("| %s | %s ; %s | %s ; %s |" % (sd, _frac(*p["per_seed"][sd]["af"]), _frac(*s["per_seed"][sd]["af"]), _frac(*p["per_seed"][sd]["b"]), _frac(*s["per_seed"][sd]["b"])))
    lines += ["", "Types apart (S and E: the fact file is deleted, so the fact lives in the transcript only):\n", "| Types | Probes | Af | B |", "|---|---|---|---|"]
    for label, d in v["types"].items():
        for ph, arms in d.items():
            lines.append("| %s | %s | %s | %s |" % (label, ph, _frac(*arms["Af"]), _frac(*arms["B"])))
    lines += ["", "Validity gates (any result but PASS makes the verdict INVALID):\n", "| Gate | Threshold | Measured | Result |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % (g["gate"], g["threshold"], g["measured"], g["result"]) for g in v["gates"]]
    lines.append("")
    lines.append("af_blocked_attempts (Af tool calls on a folder of another arm or a state folder that a deny rule blocked; not a gate): %d.\n" % blocked_total(rows, sessions))
    lines.append("Isolation method: ordering. All Af sessions and probe forks finish before any O, Z or B session of the same seeds starts (SPEC.md section 16). "
                 "No bootstrap interval is given: two seeds are too few.\n")
    return lines, v


# ---- FR.8: the 6-seed confirmatory replication (SPEC.md FR.8, thresholds.json block "fair6"). The fair code above does not change.

STOP6_FMT = "STOP: plain Claude Code with search scores %d/%d; the primary rule cannot pass"
TOLD_LINE = "When the prompt tells plain Claude Code to search: %d of %d. Lossless Handoff: %d of %d."
TOLD_NOTE = "The gain is that no instruction is necessary."
BOOT_LABEL = "6 seeds: this interval is too narrow; description only"
POOLED_LABEL = "not confirmatory: 2 of these seeds set the plan; other LH commit"
OLD_SEEDS = (3001, 3002)
NO_CLEAR = "NO CLEAR DIFFERENCE"
PHRASINGS6 = ("cued", "neutral", "told")


def th6(th_all):
    """The fair6 block with the keys of the fair block that it does not repeat (the FR.4 gates read denied_own_reads_max and other_arm_reads_max)."""
    return dict(th_all["fair"], **th_all["fair6"])


def seeds6(th_all, here=HERE):
    """The 6 new seeds, from the seed file of the fair6 block."""
    with open(os.path.join(here, th_all["fair6"]["seed_file"])) as f:
        return [int(x) for x in f.read().split()]


def sign_flip_p(diffs):
    """Exact one-sided sign-flip test on the mean of the per-seed differences: the share of all 2^n sign patterns whose mean is >= the observed mean.
    No random numbers. diffs are Fractions, so a tie is exact. Returns a Fraction (the smallest value is 1/2^n)."""
    n = len(diffs)
    if not n:
        return None
    obs = sum(diffs)
    hit = 0
    for mask in range(1 << n):
        if sum((d if (mask >> i) & 1 else -d) for i, d in enumerate(diffs)) >= obs:
            hit += 1
    return Fraction(hit, 1 << n)


def rule6(rows, th, phrasing, seeds):
    """The FR.8.3 statistics for one phrasing on the given seeds: pooled counts, G (points), k, exact p, and the class PASS, FAIL or NO CLEAR DIFFERENCE.
    Returns ok None (class None) when a seed has no Af or no B rows."""
    n = th["n"]
    per = {s: {"af": _acc(rows, "Af", phrasing, TYPES, n, s), "b": _acc(rows, "B", phrasing, TYPES, n, s)} for s in seeds}
    af = (sum(v["af"][0] for v in per.values()), sum(v["af"][1] for v in per.values()))
    b = (sum(v["b"][0] for v in per.values()), sum(v["b"][1] for v in per.values()))
    out = {"phrasing": phrasing, "af": af, "b": b, "per_seed": per, "seeds": list(seeds), "gain_points": None, "k": None, "p": None, "cls": None, "note": None}
    if not seeds or any(not v["af"][1] or not v["b"][1] for v in per.values()):
        return out
    diffs = [Fraction(v["b"][0], v["b"][1]) - Fraction(v["af"][0], v["af"][1]) for v in per.values()]
    out["gain_points"] = round(100 * (b[0] / b[1] - af[0] / af[1]), 6)      # rounded: exactly 15.0 points must pass
    out["k"] = sum(1 for d in diffs if d >= 0)
    out["p"] = sign_flip_p(diffs)
    g, k, p = out["gain_points"], out["k"], float(out["p"])
    if g >= th["min_gain_points"] and p <= th["p_max"] and k >= th["min_seeds_b_ge_af"]:
        out["cls"] = "PASS"
    elif g < th["fail_below_points"] or k <= th["fail_max_seeds_b_ge_af"]:
        out["cls"] = "FAIL"
    else:
        out["cls"] = NO_CLEAR
        if p <= th["p_max"]:
            out["note"] = "gain of %.1f points, below the registered %d" % (g, th["min_gain_points"])
    return out


def bootstrap6(rule, th):
    """95% percentile bootstrap interval of G over seeds (description only, never a gate). 10000 draws, fixed rng seed. None without data."""
    per, seeds = rule["per_seed"], rule["seeds"]
    if rule["gain_points"] is None:
        return None
    rng = random.Random(th["bootstrap_rng_seed"])
    gains = []
    for _ in range(th["bootstrap_draws"]):
        pick = [seeds[rng.randrange(len(seeds))] for _ in seeds]
        ca, ta = sum(per[s]["af"][0] for s in pick), sum(per[s]["af"][1] for s in pick)
        cb, tb = sum(per[s]["b"][0] for s in pick), sum(per[s]["b"][1] for s in pick)
        if ta and tb:
            gains.append(100 * (cb / tb - ca / ta))
    gains.sort()
    if not gains:
        return None
    return gains[int(math.floor(0.025 * len(gains)))], gains[int(math.ceil(0.975 * len(gains))) - 1]


def stop_rule6(rows, th_all):
    """(stop, x, y): Af on the hinted N=10 probes. FR.8.7: with 150 questions and x >= 128 (fewer than 23 errors), G cannot reach 15 points."""
    th = th6(th_all)
    x, y = _acc(rows, "Af", th["primary_phrasing"], TYPES, th["n"])
    return (y == 150 and x >= th["stop_af_min_correct_of_150"]), x, y


def search_rates(rows, th):
    """Af search rate per phrasing: the forks with used_lookup of all forks. And Af accuracy (rows) with and without a lookup."""
    out = {}
    for ph in PHRASINGS6:
        forks, cw, cwo = {}, [0, 0], [0, 0]
        for r in rows:
            if r.get("arm") != "Af" or r.get("phrasing", "cued") != ph or r.get("condition", "compact") != "compact" or r.get("type") not in TYPES or r.get("refused") or r.get("n_target") != th["n"]:
                continue
            forks.setdefault((r.get("run"), r.get("fork_sid") or id(r)), []).append(r)
            c = cw if r.get("used_lookup") else cwo
            c[0] += 1 if r.get("correct") else 0
            c[1] += 1
        out[ph] = {"forks": len(forks), "searched": sum(1 for rs in forks.values() if any(x.get("used_lookup") for x in rs)), "with": tuple(cw), "without": tuple(cwo)}
    return out


def validity_gates6(rows, sessions, th, seeds, meta=None):
    """The FR.4 gates (validity_gates) and the FR.8.5 gates: every seed has Af, B, O and Z rows; one claude_version; one hook sha256 in all B sessions;
    no live install (the live recall.py hash is the same in every run folder and the settings file did not change). A gate without data is NO DATA, not PASS."""
    out = validity_gates(rows, sessions, th)
    present = _arms_present(rows)
    missing = ["s%d-%s" % (s, a) for s in seeds for a in ("Af", "B", "O", "Z") if not any(r["seed"] == s and r.get("arm") == a and r.get("condition", "compact") == "compact" for r in rows)]
    if "B" not in present:
        out.append(_gate("Every seed has Af, B, O and Z rows", "0 missing", "arm B has no rows yet (step 1 alone)", None))
    else:
        out.append(_gate("Every seed has Af, B, O and Z rows", "0 missing", "%d missing%s" % (len(missing), (": " + ", ".join(missing[:6])) if missing else ""), not missing))
    vers = {r.get("claude_version") for r in rows if r.get("claude_version")} | {s.get("claude_version") for s in sessions if s.get("claude_version")}
    out.append(_gate("One claude_version in all sessions of both steps", "= 1", "%d (%s)" % (len(vers), ", ".join(sorted(map(str, vers)))[:80]) if vers else "no version recorded", (len(vers) == 1) if vers else None))
    bs = [s for s in sessions if s.get("arm") == "B"]
    hashes = {json.dumps(s.get("hook_sha256"), sort_keys=True) for s in bs if s.get("hook_sha256")}
    nohash = [s.get("name") for s in bs if not s.get("hook_sha256")]
    out.append(_gate("One hook sha256 in all B sessions", "= 1", "%d distinct%s" % (len(hashes), (", no hash in: " + ", ".join(map(str, nohash))) if nohash else "") if bs else "no B session yet",
                     (len(hashes) == 1 and not nohash) if bs else None))
    if meta:
        shas = {m.get("live_recall_sha256") for m in meta}
        changed = [m for m in meta if m.get("settings_hash_changed")]
        out.append(_gate("No live install between the registration commit and the end of step 2", "no change", "%d live recall.py hash(es), %d run(s) with a changed settings file" % (len(shas), len(changed)),
                         len(shas) == 1 and None not in shas and not changed))
    else:
        out.append(_gate("No live install between the registration commit and the end of step 2", "no change", "no run metadata", None))
    return out


def verdict6(rows, sessions, th_all, seeds=None, meta=None, old_rows=None):
    """Returns {"verdict": PASS|FAIL|NO CLEAR DIFFERENCE|INVALID|INCOMPLETE, "primary", "secondary" (neutral, told), "pooled8", "bootstrap", "gates", "invalid_gates",
    "stop", "stop_counts", "types", "search"}. Only the primary rule on the 6 new seeds sets the verdict. Everything else is secondary."""
    th = th6(th_all)
    seeds = list(seeds or seeds6(th_all))
    new = [r for r in rows if r.get("seed") in seeds]
    sess = [s for s in sessions if s.get("seed") in seeds or s.get("seed") is None]
    gl = validity_gates6(new, sess, th, seeds, meta)
    invalid = [g["gate"] for g in gl if g["result"] != "PASS"]
    prim = rule6(new, th, th["primary_phrasing"], seeds)
    sec = {ph: rule6(new, th, ph, seeds) for ph in th["secondary_phrasings"].split()}
    pooled = None
    if old_rows:
        both = list(new) + [r for r in old_rows if r.get("seed") in OLD_SEEDS]
        pooled = rule6(both, th, th["primary_phrasing"], list(OLD_SEEDS) + seeds)
    stop, x, y = stop_rule6(new, th_all)
    types = {}
    for label, ts in (("S and E", SE_TYPES), ("R D X C M", OTHER_TYPES)):
        types[label] = {ph: {arm: _acc(new, arm, ph, ts, th["n"]) for arm in ("Af", "B")} for ph in PHRASINGS6}
    if any(g["result"] == "FAIL" for g in gl):
        overall = "INVALID"
    elif prim["cls"] is None:
        overall = "INCOMPLETE"
    elif invalid:
        overall = "INVALID"
    else:
        overall = prim["cls"]
    return {"verdict": overall, "primary": prim, "secondary": sec, "pooled8": pooled, "bootstrap": bootstrap6(prim, th), "gates": gl, "invalid_gates": invalid,
            "stop": stop, "stop_counts": [x, y], "types": types, "search": search_rates(new, th)}


def _p_text(r):
    if r["p"] is None:
        return "n/a"
    return "%d/%d = %.4f" % (r["p"].numerator, r["p"].denominator, float(r["p"]))


def _row6(name, r):
    return "| %s | %s | %s | %s | %s | %s |" % (name, _frac(*r["af"]), _frac(*r["b"]) if r["b"][1] else "not run yet", "n/a" if r["gain_points"] is None else "%+.1f points" % r["gain_points"],
                                                "n/a" if r["k"] is None else "%d of %d" % (r["k"], len(r["seeds"])), _p_text(r))


def section6(rows, sessions, cfg, th_all, seeds=None, meta=None, old_rows=None):
    """(markdown lines, dict) for the fair6 runs. Counts, G, k and p. FR.5 wording with the new seed count. It never says that anything is validated or proven."""
    th = th6(th_all)
    v = verdict6(rows, sessions, th_all, seeds, meta, old_rows)
    p, told = v["primary"], v["secondary"]["told"]
    ns = len(p["seeds"])
    y = p["af"][1]
    lines = ["## Fair replication (%d seeds)\n" % ns]
    if v["stop"]:
        lines.append("**" + STOP6_FMT % tuple(v["stop_counts"]) + "**\n")
    lines.append("**Verdict (primary rule, %d new seeds, hinted probes, N=%d): %s.**\n" % (ns, th["n"], v["verdict"]))
    lines.append("Small test: %d new seeds, %d hinted questions, %s, %d compactions. Plain Claude Code that can search its own transcripts: %s. Lossless Handoff: %s.\n" % (
        ns, y, cfg.get("model", "sonnet").capitalize(), th["n"], ("%d of %d" % p["af"]) if y else "no data", ("%d of %d" % p["b"]) if p["b"][1] else "not run yet"))
    if p["cls"] is not None:
        lines.append("B minus Af: %+.1f points. Seeds with B >= Af: %d of %d. Exact one-sided sign-flip p: %s (%d patterns).\n" % (p["gain_points"], p["k"], ns, _p_text(p), 2 ** ns))
        if p["note"]:
            lines.append(p["note"] + "\n")
        lines.append("Primary: %s.\n" % ("no clear difference" if p["cls"] == NO_CLEAR else p["cls"]))
    if v["bootstrap"]:
        lines.append("95%% percentile bootstrap interval of B minus Af over seeds (%d draws, rng seed %d): %.1f to %.1f points. %s.\n" % (th["bootstrap_draws"], th["bootstrap_rng_seed"], v["bootstrap"][0], v["bootstrap"][1], BOOT_LABEL))
    if told["af"][1]:
        lines.append(TOLD_LINE % (told["af"][0], told["af"][1], told["b"][0], told["b"][1]) + "\n" if told["b"][1] else
                     "When the prompt tells plain Claude Code to search: %d of %d. Lossless Handoff: not run yet.\n" % told["af"])
        if told["af"][0] / told["af"][1] >= th["told_af_note_min_rate"]:
            lines.append(TOLD_NOTE + "\n")
    lines += ["Secondary results (they never change the verdict):\n", "| Probes | Af (can search) | B (Lossless Handoff) | B minus Af | Seeds with B >= Af | p |", "|---|---|---|---|---|---|"]
    for name, r in (("hinted (cued), primary", p), ("neutral", v["secondary"]["neutral"]), ("told", told)):
        lines.append(_row6(name, r))
    if v["pooled8"] is not None:
        lines.append(_row6("hinted (cued), pooled %d seeds (%s)" % (len(v["pooled8"]["seeds"]), POOLED_LABEL), v["pooled8"]))
    lines += ["", "Per seed (counts, hinted ; neutral ; told):\n", "| Seed | Af | B |", "|---|---|---|"]
    for sd in p["seeds"]:
        cells = [[_frac(*v["secondary"][ph]["per_seed"][sd][arm]) if ph != "cued" else _frac(*p["per_seed"][sd][arm]) for ph in PHRASINGS6] for arm in ("af", "b")]
        lines.append("| %s | %s | %s |" % (sd, " ; ".join(cells[0]), " ; ".join(cells[1])))
    lines += ["", "Types apart (S and E: the fact file is deleted, so the fact lives in the transcript only; counts only):\n", "| Types | Probes | Af | B |", "|---|---|---|---|"]
    for label, d in v["types"].items():
        for ph, arms in d.items():
            lines.append("| %s | %s | %s | %s |" % (label, ph, _frac(*arms["Af"]), _frac(*arms["B"])))
    lines += ["", "Af search rate (forks with used_lookup, of all Af forks) and Af accuracy with and without a lookup:\n", "| Probes | Forks that searched | Af correct with a lookup | Af correct without a lookup |", "|---|---|---|---|"]
    for ph, d in v["search"].items():
        lines.append("| %s | %s | %s | %s |" % (ph, "%d of %d" % (d["searched"], d["forks"]) if d["forks"] else "no data", _frac(*d["with"]), _frac(*d["without"])))
    lines += ["", "Validity gates (any result but PASS makes the verdict INVALID):\n", "| Gate | Threshold | Measured | Result |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % (g["gate"], g["threshold"], g["measured"], g["result"]) for g in v["gates"]]
    lines.append("")
    lines.append("af_blocked_attempts (Af tool calls on a folder of another arm or a state folder that a deny rule blocked; not a gate): %d.\n" % blocked_total(rows, sessions))
    lines.append("Isolation method: ordering. All Af sessions and probe forks finish before any O, Z or B session of the same seeds starts (SPEC.md section 16).\n")
    return lines, v


# ---- FR.8.10: 2 extra seeds (4007 4008), a side result. It never changes the verdict. No stop rule, no 6-seed verdict section.

FAIR6X_KINDS = ("fair6x-step1", "fair6x-step2")
X8_LABEL = "secondary: 2 seeds added after registration; does not change the verdict"
X8_TITLE = "8 new seeds (4001-4008)"


def seeds6x(here=HERE):
    """The 2 extra seeds, from seeds-fair6x.txt (FR.8.10)."""
    with open(os.path.join(here, "seeds-fair6x.txt")) as f:
        return [int(x) for x in f.read().split()]


def section6x(rows, sessions, cfg, th_all, meta=None):
    """(markdown lines, dict) for the fair6x runs: per-arm counts per phrasing and the FR.8.5 gates for the 2 extra seeds. No stop rule, no verdict."""
    th = th6(th_all)
    seeds = seeds6x()
    new = [r for r in rows if r.get("seed") in seeds]
    sess = [s for s in sessions if s.get("seed") in seeds or s.get("seed") is None]
    gl = validity_gates6(new, sess, th, seeds, meta)
    invalid = [g["gate"] for g in gl if g["result"] != "PASS"]
    rules = {ph: rule6(new, th, ph, seeds) for ph in PHRASINGS6}
    lines = ["## Extra seeds (%d seeds, FR.8.10)\n" % len(seeds), "**%s.** No stop rule and no verdict apply to these seeds.\n" % X8_LABEL,
             "| Probes | Af (can search) | B (Lossless Handoff) | B minus Af |", "|---|---|---|---|"]
    for ph in PHRASINGS6:
        r = rules[ph]
        lines.append("| %s | %s | %s | %s |" % (ph, _frac(*r["af"]), _frac(*r["b"]) if r["b"][1] else "not run yet", "n/a" if r["gain_points"] is None else "%+.1f points" % r["gain_points"]))
    lines += ["", "Per seed (counts, cued ; neutral ; told):\n", "| Seed | Af | B |", "|---|---|---|"]
    for sd in seeds:
        cells = [[_frac(*rules[ph]["per_seed"][sd][arm]) for ph in PHRASINGS6] for arm in ("af", "b")]
        lines.append("| %s | %s | %s |" % (sd, " ; ".join(cells[0]), " ; ".join(cells[1])))
    lines += ["", "Validity gates for these %d seeds (any result but PASS makes this secondary result INVALID):\n" % len(seeds), "| Gate | Threshold | Measured | Result |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % (g["gate"], g["threshold"], g["measured"], g["result"]) for g in gl]
    lines.append("")
    return lines, {"seeds": seeds, "rules": rules, "gates": gl, "invalid_gates": invalid, "invalid": bool(invalid)}


def new8(rows, sessions_x, th_all, meta_x=None, rows_x=None):
    """The FR.8.10 secondary result. rows: the rows of all 8 seeds (4001-4008). rows_x, sessions_x, meta_x: rows, sessions and run metadata of the 2 extra seeds
    for the FR.8.5 gates. Returns (lines, dict). Cued, neutral and told: pooled Af and B counts, G, k and the exact sign-flip p over 256 patterns."""
    th = th6(th_all)
    xs = seeds6x()
    seeds = list(seeds6(th_all)) + xs
    gl = validity_gates6([r for r in (rows_x if rows_x is not None else rows) if r.get("seed") in xs], sessions_x, th, xs, meta_x)
    invalid = [g["gate"] for g in gl if g["result"] != "PASS"]
    rules = {ph: rule6(rows, th, ph, seeds) for ph in PHRASINGS6}
    lines = ["%s: %s" % (X8_TITLE, X8_LABEL)]
    if invalid:
        lines.append("INVALID: the FR.8.5 gates failed or have no data for the 2 extra seeds: " + "; ".join(invalid))
    lines.append("%d seeds, %d sign patterns" % (len(seeds), 2 ** len(seeds)))
    for ph in PHRASINGS6:
        r = rules[ph]
        if r["cls"] is None:
            lines.append("%-8s no data for every seed (Af %s, B %s)" % (ph, _frac(*r["af"]), _frac(*r["b"])))
            continue
        lines.append("%-8s Af %s  B %s  G %+.1f points  k %d of %d  p %s" % (ph, _frac(*r["af"]), _frac(*r["b"]), r["gain_points"], r["k"], len(seeds), _p_text(r)))
    lines.append("FR.8.5 gates for the 2 extra seeds (%s):" % ", ".join(map(str, xs)))
    lines += ["  %s: %s (%s)" % (g["result"], g["gate"], g["measured"]) for g in gl]
    return lines, {"rules": rules, "gates": gl, "invalid": bool(invalid), "invalid_gates": invalid, "label": X8_LABEL}


def old_fair_rows(results_dir):
    """Rows of the two seeds of the first fair re-test (3001 3002), for the pooled secondary result: the newest fair-step1 and fair-step2 run folders of these seeds. [] if none."""
    import glob
    sys.path.insert(0, HERE)
    import gates
    rows = []
    for kind in ("fair-step1", "fair-step2"):
        best = None
        for f in sorted(glob.glob(os.path.join(results_dir, "*", "config.json"))):
            try:
                c = json.load(open(f, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if c.get("kind") == kind and set(c.get("seeds") or []) <= set(OLD_SEEDS) and os.path.isfile(os.path.join(os.path.dirname(f), "results.jsonl")):
                best = os.path.dirname(f)
        if best:
            rows += gates.load_rows(best)
    return rows


# ---- the fair-preflight checklist

def preflight_check(rows, sessions, cfg):
    """[(label, PASS|FAIL, detail)]: real compactions, B hooks that fire, an Af read of its own transcript, used_lookup recorded."""
    out = []
    comp = [s for s in sessions if s.get("arm") in ("Af", "B") and s.get("condition", "compact") == "compact"]
    want = max(cfg.get("n") or [1])
    bad = [s.get("name") for s in comp if (s.get("boundaries") or 0) < want]
    out.append(("real compactions in each Af and B session (at least %d)" % want, "PASS" if comp and not bad else "FAIL", "%d session(s); too few: %s" % (len(comp), bad or "none")))
    bs = [s for s in comp if s.get("arm") == "B"]
    nb = [s.get("name") for s in bs if not (s.get("hook_receipts") or 0)]
    out.append(("B hooks fire (a hook receipt in health.jsonl of each B session)", "PASS" if bs and not nb else "FAIL", "no receipt in: %s" % (nb or "none")))
    af = [r for r in rows if r.get("arm") == "Af"]
    ok = sum((r.get("af_reads") or {}).get("own_ok", 0) for r in {r.get("fork_sid"): r for r in af}.values())
    ok += sum((s.get("af_reads") or {}).get("own_ok", 0) for s in sessions if s.get("arm") == "Af")
    den = sum((r.get("af_reads") or {}).get("own_denied", 0) for r in {r.get("fork_sid"): r for r in af}.values())
    out.append(("at least 1 successful Af read of its own transcript, and none denied", "PASS" if ok >= 1 and den == 0 else "FAIL", "%d successful, %d denied" % (ok, den)))
    allrows = [r for r in rows if r.get("arm") in ("Af", "B")]
    have = [r for r in allrows if "used_lookup" in r]
    used = sum(1 for r in have if r.get("used_lookup"))
    out.append(("used_lookup recorded in every Af and B row", "PASS" if allrows and len(have) == len(allrows) else "FAIL", "%d of %d rows have it; %d true" % (len(have), len(allrows), used)))
    return out


def _load(run_id):
    rd = run_id if os.path.isdir(run_id) else os.path.join(HERE, "results", run_id)
    cfg = json.load(open(os.path.join(rd, "config.json"), encoding="utf-8"))
    sys.path.insert(0, HERE)
    import gates
    rows = gates.load_rows(rd)      # without restarted rows and without rows that a reprobe replaced (FR.7)
    return rd, cfg, rows, _jl(os.path.join(rd, "sessions.jsonl"))


def _run_meta(rd):
    try:
        c = json.load(open(os.path.join(rd, "config.json"), encoding="utf-8"))
    except (OSError, ValueError):
        c = {}
    try:
        s = json.load(open(os.path.join(rd, "state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    return {"live_recall_sha256": c.get("live_recall_sha256"), "settings_hash_changed": bool(s.get("settings_hash_changed"))}


def main_new8(dirs):
    """fair.py new8 <fair6-step1> <fair6-step2> <fair6x-step1> <fair6x-step2>: prints the FR.8.10 secondary result. $0, no model call."""
    rows_all, rows_x, sess_x, meta_x = [], [], [], []
    for i, d in enumerate(dirs):
        rd, _cfg, rows, sess = _load(d)
        rows_all += rows
        if i >= 2:
            rows_x += rows
            sess_x += sess
            meta_x.append(_run_meta(rd))
    th = json.load(open(os.path.join(HERE, "thresholds.json"), encoding="utf-8"))
    lines, _out = new8(rows_all, sess_x, th, meta_x, rows_x)
    print("\n".join(lines))
    return 0


def main(argv):
    if argv[:1] == ["new8"]:
        if len(argv) != 5:
            print(__doc__, file=sys.stderr)
            return 2
        return main_new8(argv[1:])
    if len(argv) != 2 or argv[0] not in ("preflight-check", "stop-check"):
        print(__doc__, file=sys.stderr)
        return 2
    rd, cfg, rows, sess = _load(argv[1])
    if argv[0] == "preflight-check":
        res = preflight_check(rows, sess, cfg)
        for label, st, detail in res:
            print("%s  %s (%s)" % (st, label, detail))
        return 0 if all(st == "PASS" for _l, st, _d in res) else 1
    th = json.load(open(os.path.join(HERE, "thresholds.json"), encoding="utf-8"))
    import gates
    if cfg.get("kind") in FAIR6X_KINDS:      # FR.8.10: no stop rule for the 2 extra seeds
        print("no stop rule for %s (FR.8.10: the extra seeds are a side result)" % cfg.get("kind"))
        return 0
    if str(cfg.get("kind", "")).startswith("fair6"):      # FR.8.7: Af >= 128 of 150 hinted: the primary rule cannot pass. Step 2 does not start by itself
        stop, x, y = stop_rule6(rows, th)
        if gates.reprobe_line(rd):
            print(gates.reprobe_line(rd))
        print(STOP6_FMT % (x, y) if stop else "go on: plain Claude Code with search scores %d/%d (stop at %d of 150 or more; the owner decides about step 2)" % (x, y, th["fair6"]["stop_af_min_correct_of_150"]))
        return 0
    stop, x, y = stop_rule(rows, th)
    if gates.reprobe_line(rd):
        print(gates.reprobe_line(rd))
    print(STOP_FMT % (x, y) if stop else "go on: plain Claude Code with search scores %d/%d (stop rule at %s)" % (x, y, _pct(th["fair"]["stop_af_min_rate"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
