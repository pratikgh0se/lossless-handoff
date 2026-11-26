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
"""
import json, os, re, sys

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


def main(argv):
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
    stop, x, y = stop_rule(rows, th)
    import gates
    if gates.reprobe_line(rd):
        print(gates.reprobe_line(rd))
    print(STOP_FMT % (x, y) if stop else "go on: plain Claude Code with search scores %d/%d (stop rule at %s)" % (x, y, _pct(th["fair"]["stop_af_min_rate"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
