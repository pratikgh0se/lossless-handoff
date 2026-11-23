#!/usr/bin/env python3
"""Checklist of the preflight run (run.sh preflight). It reads the finished run and prints PASS or FAIL for each line.
Exit 0 only if no line is FAIL. It makes no model call. (One check starts the recall MCP server of arm K for a second, to see that it answers.)

Usage: preflight.py <run-id | run folder> [--no-mcp]
"""
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost, design4, gates, runner  # noqa: E402

COST_CAP_USD = 5.0
COST_RATIO_MAX = 1.3
RUNAWAY_ARM_MAX = 0.05
REFUSED_ARM_MAX = 0.10
REANCHOR_TOKENS = 50000
D4_INFO_LEVELS = ("L3", "L4")      # the Haiku preflight cannot reason through these chains: INFO only
D4_INFO_NOTE = "Haiku capability; the calibration on Sonnet measures them"
OUT_LIMIT_FACTOR = 1.25            # the probe output line limit is this factor x the cap; a fork above the cap is already a runaway
NL_ORACLE_MIN = 0.80      # non-literal probes: the oracle must answer at least this share of every type
BIG_MIN_TOKENS = 250000
BIG_MAX_TOKENS = 350000     # the paste is sized for 280k to 320k tokens; above this the turn costs more than planned
DECISION_MARKERS = ("decisions list", "Project decisions for this repo", "Project rules for this repo")
RECALL_RULE = "recall rule: before you state an earlier number"


def jl(path):
    return runner.read_jsonl(path)


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


def text_after_first_boundary(path):
    """All text of the transcript lines after the first compact_boundary record, whitespace normalised. None if no boundary."""
    out, seen = [], False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not seen:
                    seen = '"compact_boundary"' in line
                    continue
                try:
                    out.append(" ".join(strings(json.loads(line))))
                except ValueError:
                    pass
    except OSError:
        return None
    return re.sub(r"\s+", " ", " ".join(out)) if seen else None


class Run:
    def __init__(self, rd, projects=None):
        self.rd = rd
        self.cfg = json.load(open(os.path.join(rd, "config.json"), encoding="utf-8"))
        self.state = json.load(open(os.path.join(rd, "state.json"), encoding="utf-8"))
        self.rows = gates.dedupe_rows(jl(os.path.join(rd, "results.jsonl")))
        last = {}
        for s in jl(os.path.join(rd, "sessions.jsonl")):
            last[s["name"]] = s
        self.sessions = [last[k] for k in sorted(last)]
        self.calls = jl(os.path.join(rd, "calls.jsonl"))
        self.projects = projects or runner.PROJECTS

    def stem(self, s, arm=None):
        return "%s%s-s%d-%s%s" % (runner.PREFIX, self.cfg["run_id"], s["seed"], arm or s["arm"], "-nc" if s.get("condition") == "nocompact" else "")

    def statedir(self, s):
        return os.path.join(self.cfg["work_base"], self.stem(s) + "-state")

    def transcript(self, s):
        """Main transcript of a session. Arms B, R and K in the design-3 no-compaction condition fork the session of arm A: its folder."""
        sid = self.state["sessions"].get(s["name"], {}).get("sid")
        proj = os.path.realpath(os.path.join(self.cfg["work_base"], self.stem(s, "A" if s.get("view_of_arm_a") else None)))
        return os.path.join(self.projects, runner.slug(proj), "%s.jsonl" % sid) if sid else None


def allowed_env_names(cfg):
    return set(cfg.get("env_allowlist", runner.ENV_ALLOW)) | set(runner.OVERRIDE_ENV)      # one list: every override the runner sets


def check(rd, projects=None, mcp=True):
    """Returns a list of (label, status, detail). status: PASS, FAIL or INFO."""
    R = Run(rd, projects)
    res = []

    def add(label, ok, detail=""):
        res.append((label, "INFO" if ok is None else ("PASS" if ok else "FAIL"), detail))

    cond = lambda s: s.get("condition", "compact")  # noqa: E731
    comp = [s for s in R.sessions if cond(s) == "compact" and s["arm"] in runner.LH_ARMS]
    nc_main = [s for s in R.sessions if cond(s) == "nocompact" and s["arm"] in ("A", "O")]
    views = [s for s in R.sessions if s.get("view_of_arm_a")]
    # 1. a compaction in each compaction session
    bad = [s["name"] for s in comp if (s.get("boundaries") or 0) < 1]
    add("a compaction happened in each compaction session", bool(comp) and not bad, "%d session(s); without a compaction: %s" % (len(comp), bad or "none"))
    # 2, 3. receipt written, handoff injected after the compaction, decisions or rules injected
    r_bad, i_bad, d_bad = [], [], []
    for s in comp:
        rec = jl(os.path.join(R.statedir(s), "receipts.jsonl"))
        txt = text_after_first_boundary(R.transcript(s)) if R.transcript(s) else None
        if not rec:
            r_bad.append(s["name"])
        heads = [re.sub(r"\s+", " ", x.get("part1_head") or "").strip()[:40] for x in rec if x.get("part1_head")]
        if txt is None or not heads or not any(h and h in txt for h in heads):
            i_bad.append(s["name"])
        if txt is None or not any(m in txt for m in DECISION_MARKERS):
            d_bad.append(s["name"])
    add("a handoff receipt was written (receipts.jsonl of the arm)", bool(comp) and not r_bad, "missing in: %s" % (r_bad or "none"))
    add("the handoff text of the receipt was injected after the compaction", bool(comp) and not i_bad, "not found in the transcript of: %s" % (i_bad or "none"))
    add("the decisions list or the rules were injected", bool(comp) and not d_bad, "no marker in: %s" % (d_bad or "none"))
    # 4. no-compaction main sessions (arm A, and the short oracle of arm O)
    exp = R.cfg.get("nc_estimate_tokens")
    n_bad = []
    for s in nc_main:
        tp = R.transcript(s)
        ok, why = runner.nc_validity(s.get("boundaries") or 0, runner.count_cleared(tp) if tp else 0, runner.last_turn_context(R.calls, s["name"]),
                                     exp if s["arm"] == "A" else None)
        if not ok or s.get("status") == "invalid":
            n_bad.append("%s (%s)" % (s["name"], "; ".join(why) or s.get("reason")))
    add("no-compaction sessions: no compaction, no cleared marker, context at least %d%% of the estimate (%s tokens; arm O is short, no size rule)" % (100 * runner.NC_CONTEXT_TOLERANCE, exp),
        bool(nc_main) and not n_bad, "%d session(s); invalid: %s" % (len(nc_main), n_bad or "none"))
    # 5. arm R: a fire for every cued probe, matched on the fork id, and none for another probe
    r_rows = [r for r in R.rows if r["arm"] == "R"]
    miss, extra = set(), set()
    for s in [x for x in R.sessions if x["arm"] == "R"]:
        ids = {x.get("session_id") for x in jl(os.path.join(R.statedir(s), "auto-recall.jsonl")) if x.get("fired")}
        for r in r_rows:
            if r["seed"] != s["seed"] or (r.get("condition", "compact") != cond(s)):
                continue
            key = (r["seed"], cond(s), r["type"], r.get("phrasing", "cued"))
            if r.get("phrasing", "cued") == "cued" and (not r.get("auto_recall_fired") or r.get("fork_sid") not in ids):
                miss.add(key)
            if r.get("phrasing", "cued") != "cued" and (r.get("auto_recall_fired") or r.get("fork_sid") in ids):
                extra.add(key)
    add("arm R: an auto-recall log line for every cued probe (matched on the fork id) and none for a neutral or non-literal probe", bool(r_rows) and not miss and not extra,
        "%d probe row(s); cued without a line: %d; neutral or non-literal with a line: %d" % (len(r_rows), len(miss), len(extra)))
    # 6. arm K
    k_sess = [s for s in R.sessions if s["arm"] == "K"]
    if k_sess:
        s0 = k_sess[0]
        hooks = os.path.join(R.statedir(s0), "hooks", "recall-mcp.py")
        ok, detail = (mcp_probe(hooks, R.statedir(s0), os.path.join(R.cfg["work_base"], R.stem(s0))) if mcp else (None, "skipped (--no-mcp)"))
        add("arm K: the recall MCP server starts and lists search_earlier_chat and open_message", ok, detail)
        mc = [s.get("mcp_check") for s in k_sess if s.get("mcp_check")]
        add("arm K: the MCP check turn made an mcp__lossless-recall__ call (a fork of the no-compaction session; ToolSearch is allowed)", bool(mc) and all(m.get("called") for m in mc),
            "%d check turn(s); calls: %s" % (len(mc), [bool(m.get("called")) for m in mc]))
        crossed = [s for s in k_sess if cond(s) == "compact" and (runner.last_turn_context(R.calls, s["name"]) or 0) >= REANCHOR_TOKENS]
        # The hook saves its anchor (reanchor-<sid>.json) at every decision, also when it has no rules yet (reanchor.py decide(): "also when there is no rule"),
        # and it writes a health row only when it injects. The first crossing of the interval can come before the decisions ledger has rules, and a compaction
        # then resets the anchor, so a session that ends 53k tokens long can have an anchor and no row. FAIL only when the hook left neither a row nor an anchor.
        def has_row(s):
            return any(x.get("event") == "reanchor" for x in jl(os.path.join(R.statedir(s), "health.jsonl")))

        def has_anchor(s):
            sd = R.statedir(s)
            return os.path.isdir(sd) and any(n.startswith("reanchor-") and n.endswith(".json") for n in os.listdir(sd))
        r_miss = [s["name"] for s in crossed if not has_row(s) and not has_anchor(s)]
        r_anchor_only = [s["name"] for s in crossed if not has_row(s) and has_anchor(s)]
        add("arm K: the reanchor hook ran (a health row or a saved anchor) if the session crossed the interval (%d tokens)" % REANCHOR_TOKENS, not r_miss,
            "%d of %d K compaction session(s) crossed it; no row and no anchor in: %s; anchor but no row (no rules at the crossing, or no second interval): %s" % (
                len(crossed), len([s for s in k_sess if cond(s) == "compact"]), r_miss or "none", r_anchor_only or "none"))
        rule_bad = []
        for s in [x for x in k_sess if cond(x) == "compact"]:
            txt = text_after_first_boundary(R.transcript(s)) if R.transcript(s) else None
            if txt is None or RECALL_RULE not in txt:
                rule_bad.append(s["name"])
        add("arm K: the recall-rule line is in the injected text (compaction session)", bool([x for x in k_sess if cond(x) == "compact"]) and not rule_bad, "missing in: %s" % (rule_bad or "none"))
        k_rows = [r for r in R.rows if r["arm"] == "K" and cond_row(r) == "nocompact"]
        k_bad = [r for r in k_rows if not r.get("fork_rule_line")]
        srcs = sorted({x for r in k_rows for x in (r.get("fork_rule_source") or [])})
        via = {"mcp_instructions": "MCP server instructions, not a hook", "hook": "a hook"}
        where = " and ".join(via.get(x, x) for x in srcs) or "not recorded in this run"
        add("arm K: the recall-rule line is in the transcript of every no-compaction fork (conditional, decides the label of K; not a FAIL)", None,
            "%d fork row(s); without the line: %d; source: %s; claim: %s" % (len(k_rows), len(k_bad), where,
                "K = R + recall rule line (from %s) + MCP tool" % where if (k_rows and not k_bad) else "K = R + MCP tool"))
        kf = [(s["name"], s.get("first_fork_cost_usd")) for s in k_sess if s.get("view_of_arm_a")]
        add("arm K: cost of the first fork (a cache miss writes about 300k tokens; per-call cap of K forks $%.2f)" % runner.K_FORK_CAP_USD, None,
            "first K fork $%s; other arms: %s" % (kf[0][1] if kf else "n/a", [(s["name"], s.get("first_fork_cost_usd")) for s in views if s["arm"] != "K"]))
    v_rows = [r for r in R.rows if r["arm"] in ("B", "R") and cond_row(r) == "nocompact"]
    add("arms B and R: startup injection seen in the no-compaction forks (recorded, not required)", None,
        "%d fork row(s); with an injection marker: %d" % (len(v_rows), len([r for r in v_rows if r.get("fork_injected")])))
    # 7. used_lookup on every probe
    no_ul = [r for r in R.rows if not isinstance(r.get("used_lookup"), bool)]
    add("used_lookup is set for every probe (every arm)", bool(R.rows) and not no_ul, "%d probe row(s); without it: %d" % (len(R.rows), len(no_ul)))
    # 7b. output tokens of the probe forks (a probe answers in short JSON; a runaway reply wastes money and can set off a safety filter)
    forks = {}
    for r in R.rows:
        n_out = r.get("output_tokens")
        if n_out is None and isinstance(r.get("tokens"), dict):
            n_out = r["tokens"].get("out")
        if isinstance(n_out, (int, float)):
            forks[r.get("fork_sid") or id(r)] = (n_out, r.get("type"))
    info_forks = {k: v[0] for k, v in forks.items() if v[1] in D4_INFO_LEVELS}          # L3 and L4: Haiku capability, INFO only
    out_lim = int(OUT_LIMIT_FACTOR * runner.PROBE_MAX_OUTPUT_TOKENS)
    # The cap CLAUDE_CODE_MAX_OUTPUT_TOKENS is per REQUEST. The row field output_tokens is the SUM over the requests of a fork, so a fork that uses tools can pass
    # the cap with no request near it. The line gates the largest single request (max_request_output) when the rows have it; older rows have no such field.
    max_req = {}
    for r in R.rows:
        if isinstance(r.get("max_request_output"), (int, float)):
            max_req[r.get("fork_sid") or id(r)] = (r["max_request_output"], r.get("type"))
    if max_req:
        outs = sorted(v[0] for k, v in max_req.items() if v[1] not in D4_INFO_LEVELS)
        info_forks = {k: v[0] for k, v in max_req.items() if v[1] in D4_INFO_LEVELS}
    else:
        outs, info_forks = [], {}
    over = sorted(v for v in outs if v > out_lim)
    over_info = sorted(v for v in info_forks.values() if v > out_lim)
    if not max_req:
        sums = sorted(v[0] for v in forks.values())
        add("probe fork output: no per-request data in these rows (INFO: the old field output_tokens is a sum over the requests of a fork and is not compared with the per-request cap)",
            None, "%d fork(s)%s; later runs record max_request_output and requests" % (len(sums), (", largest sum %d" % sums[-1]) if sums else ""))
    add("largest single request of a probe fork at most %d tokens (%s x the per-request cap %s)" % (out_lim, OUT_LIMIT_FACTOR, runner.PROBE_MAX_OUTPUT_ENV), None if not outs else not over,
        "no per-request data" if not outs else "%d fork(s), above the limit: %s" % (len(outs), over[:5] or "none"))
    if info_forks:
        add("probe fork output above %d tokens at L3 and L4 (INFO: %s)" % (out_lim, D4_INFO_NOTE), None, "%d fork(s) at L3 or L4, above the limit: %s" % (len(info_forks), over_info[:8] or "none"))
    # runaway forks: a probe that hit the output cap is an outcome (class runaway), at most 5% of the forks of each arm
    rw_arm = {}
    g_rows_ = [r for r in R.rows if r.get("type") not in D4_INFO_LEVELS]        # L3 and L4 are INFO in the Haiku preflight
    for a_ in sorted({r.get("arm") for r in R.rows}):
        rw_arm[a_] = gates.runaway_rate([r for r in g_rows_ if r.get("arm") == a_])
    rw_bad = {a_: v for a_, v in rw_arm.items() if v[1] and v[0] / float(v[1]) > RUNAWAY_ARM_MAX}
    add("runaway forks of each arm at L1 and L2 (INFO: Haiku limits are not what the preflight tests; the Sonnet oracle check gates runaway at %d%% per level and phrasing)" % int(100 * RUNAWAY_ARM_MAX), None,
        "counts: %s; above %d%%: %s" % (", ".join("%s %d/%d" % ((a_,) + v) for a_, v in sorted(rw_arm.items())), int(100 * RUNAWAY_ARM_MAX), sorted(rw_bad) or "none"))
    lv_rw = {}
    for r in R.rows:
        if str(r.get("type", "")).startswith("L"):
            lv_rw.setdefault(r["type"], []).append(r)
    add("runaway forks at L3 and L4 (INFO: %s)" % D4_INFO_NOTE, None, ", ".join("%s %d/%d" % ((lv,) + gates.runaway_rate(v)) for lv, v in sorted(lv_rw.items()) if lv in D4_INFO_LEVELS) or "no L3 or L4 rows")
    add("runaway forks per level (counts)", None, ", ".join("%s %d/%d" % ((lv,) + gates.runaway_rate(v)) for lv, v in sorted(lv_rw.items())) or "no design-4 rows")
    # refused forks: a safety filter refused a probe fork (class refused, an outcome), at most 10% of the forks of each arm
    rf_arm = {a_: gates.refused_rate([r for r in R.rows if r.get("arm") == a_]) for a_ in sorted({r.get("arm") for r in R.rows})}
    rf_bad = {a_: v for a_, v in rf_arm.items() if v[1] and v[0] / float(v[1]) > REFUSED_ARM_MAX}
    add("refused forks of each arm (INFO: Haiku [bio] false positives are not what the preflight tests; the Sonnet oracle check gates refused at %d%% per level and phrasing)" % int(100 * REFUSED_ARM_MAX), None,
        "counts: %s; above %d%%: %s" % (", ".join("%s %d/%d" % ((a_,) + v) for a_, v in sorted(rf_arm.items())), int(100 * REFUSED_ARM_MAX), sorted(rf_bad) or "none"))
    lv_rf = {}
    for r in R.rows:
        if str(r.get("type", "")).startswith("L"):
            lv_rf.setdefault(r["type"], []).append(r)
    add("refused forks per level (counts)", None, ", ".join("%s %d/%d" % ((lv,) + gates.refused_rate(v)) for lv, v in sorted(lv_rf.items())) or "no design-4 rows")
    # F79: a G fork shows the history guard (a Stop continuation or a tool-call check); the graded answer is the final reply of `claude -p`, which is the second reply
    g_rows = [r for r in R.rows if r.get("arm") == "G"]
    if not g_rows and "G" not in R.cfg["arms"]:
        add("Stop continuation (arm G)", None, "not run: no G arm in this preset")
    if g_rows:
        forks = {gates.fork_id(r) for r in g_rows}
        fired = {gates.fork_id(r) for r in g_rows if r.get("guard_fired")}
        add("Stop continuation: at least one G fork shows the history check in its transcript (the graded answer is the second reply)", bool(fired),
            "%d of %d G fork(s) show it" % (len(fired), len(forks)))
    act = [r for r in R.rows if r.get("phrasing") == "action"]
    if act:
        bad_ = sorted({gates.fork_id(r) for r in act if r.get("tamper")}, key=str)
        nofile = sorted({gates.fork_id(r) for r in act if r.get("action_file_found") is False}, key=str)
        add("action forks wrote only below probe-out (no other file of the project folder changed)", not bad_, "%d fork(s) changed other files" % len(bad_))
        add("action forks wrote their file (INFO: a missing file is graded unreadable)", None, "%d of %d action fork(s) wrote no file" % (len(nofile), len({gates.fork_id(r) for r in act})))
    add("median probe output tokens", None, "no data" if not outs else "median %d, max %d (%d fork(s))" % (outs[len(outs) // 2], outs[-1], len(outs)))
    # 8. parse and grade
    # Parse policy (one threshold, gates.PARSE_ERROR_RATE_MAX, the same as the abort gate): a reply that cannot be read counts as wrong for every arm.
    # Arm Z (no history) is excluded from the rate, as in the abort gate: it may answer with fewer entries or in prose (INFO only).
    # A row whose `correct` is not true or false is a harness error: always a FAIL.
    not_graded = [r for r in R.rows if not isinstance(r.get("correct"), bool)]
    unread = [r for r in R.rows if not r.get("parse_ok") and not isinstance(r.get("parsed"), dict) and not r.get("runaway") and not r.get("refused")]       # no readable entry for the item (runaway and refused rows are counted apart)
    dev = [r for r in R.rows if not r.get("parse_ok") and isinstance(r.get("parsed"), dict)]              # entry read, field rejected
    scored = [r for r in R.rows if r["arm"] != "Z" and not r.get("runaway") and not r.get("refused") and r.get("type") not in D4_INFO_LEVELS]
    unread_s = [r for r in unread if r["arm"] != "Z" and r.get("type") not in D4_INFO_LEVELS]
    rate = len(unread_s) / float(len(scored)) if scored else 1.0
    kinds = sorted({(r["arm"], r.get("phrasing"), r.get("type"), r.get("condition")) for r in unread + dev})
    add("every probe answer parsed and graded (no grader error)", bool(R.rows) and not not_graded and rate <= gates.PARSE_ERROR_RATE_MAX,
        "%d probe row(s); not graded: %d; unreadable (no entry): %d, of them arm Z %d; rate outside Z %.2f%% (limit %.0f%%); format deviations: %d; classes %s" % (
            len(R.rows), len(not_graded), len(unread), len(unread) - len(unread_s), 100 * rate, 100 * gates.PARSE_ERROR_RATE_MAX, len(dev), kinds))
    # 8b. the non-literal probes must be answerable: the short oracle (arm O, no compaction) gets at least 80% on every type
    o_nl = [r for r in R.rows if r["arm"] == "O" and r.get("phrasing") == "nonliteral" and cond_row(r) == "nocompact" and r.get("type") not in D4_INFO_LEVELS]
    o_nl_info = {}
    for r in R.rows:
        if r["arm"] == "O" and r.get("phrasing") == "nonliteral" and cond_row(r) == "nocompact" and r.get("type") in D4_INFO_LEVELS:
            c_ = o_nl_info.setdefault(r["type"], [0, 0])
            c_[0] += 1 if r.get("correct") is True else 0
            c_[1] += 1
    by_t = {}
    for r in o_nl:
        c = by_t.setdefault(r["type"], [0, 0])
        c[0] += 1 if r.get("correct") is True else 0
        c[1] += 1
    low = {t: "%d/%d" % tuple(c) for t, c in sorted(by_t.items()) if c[0] < NL_ORACLE_MIN * c[1]}
    if o_nl_info:
        add("nonliteral oracle (arm O) accuracy at L3 and L4 (INFO: %s)" % D4_INFO_NOTE, None, ", ".join("%s %d/%d" % ((t,) + tuple(c)) for t, c in sorted(o_nl_info.items())))
    add("nonliteral oracle (arm O) accuracy at least %d%% per type" % int(NL_ORACLE_MIN * 100), bool(by_t) and not low,
        "per type: %s%s" % ({t: "%d/%d" % tuple(c) for t, c in sorted(by_t.items())}, "; below the limit: %s" % low if low else ""))
    # 8c. design 4 (update chains): the scripts are sound, a full-information solver reaches 100%, the naive solver stays low, and the controls work
    if R.cfg.get("probe_design", 1) >= 4:
        probs, full_bad, naive_bad = [], [], []
        for sd in R.cfg["seeds"]:
            sc = design4.build_litm4(sd)
            probs += design4.check_script(sc)
            full = design4.solve_full(sc)
            full_bad += [it["key"] for lv in sc["items"].values() for it in lv
                         if (full[it["key"]]["current"], full[it["key"]]["previous"], full[it["key"]]["replaced"]) != (it["current"], it["previous"], it["replaced"])]
            naive_bad += ["seed %s %s %.0f%%" % (sd, lv, 100 * design4.last_mention_rate(sc, lv)) for lv in ("L3", "L4") if design4.last_mention_rate(sc, lv) > design4.SOLVER_MAX]
        add("design 4: scripts are sound (zero word and stem overlap, unique descriptions and keys)", not probs, "problems: %s" % (probs[:3] or "none"))
        add("design 4 solver gate: a full-information solver reaches 100%% on M3, and 'last mention wins' scores at most %d%% on M1 at L3 and L4" % int(100 * design4.SOLVER_MAX),
            not full_bad and not naive_bad, "full solver misses: %s; naive solver above the limit: %s" % (full_bad[:3] or "none", naive_bad or "none"))
        o_low = {}
        o_info = []
        for lv in design4.D4_LEVELS:
            if lv in D4_INFO_LEVELS:
                for ph in design4.ALL_PHRASINGS:
                    v_, n_ = design4.cell(R.rows, "O", lv, "m1", ph)
                    if n_:
                        o_info.append("%s %s %.0f%%" % (lv, ph, 100 * v_))
                continue
            for ph in design4.ALL_PHRASINGS:
                v_, n_ = design4.cell([r for r in R.rows], "O", lv, "m1", ph)
                if n_ and v_ < NL_ORACLE_MIN:
                    o_low["%s %s" % (lv, ph)] = "%.0f%%" % (100 * v_)
        add("design 4: oracle (arm O) M1 at least %d%% for each level and phrasing (Haiku, plumbing level; the gate of the real runs is 95%%)" % int(NL_ORACLE_MIN * 100),
            any(r["arm"] == "O" for r in R.rows) and not o_low, "below the limit: %s" % (o_low or "none"))
        add("design 4: oracle (arm O) M1 at L3 and L4 (INFO: %s)" % D4_INFO_NOTE, None, ", ".join(o_info) or "no L3 or L4 rows")
        z_hi = {}
        for lv in design4.D4_LEVELS:
            v_, n_ = design4.cell(R.rows, "Z", lv, "m1")
            if n_ and v_ > design4.Z_MAX:
                z_hi[lv] = "%.0f%%" % (100 * v_)
        add("design 4: amnesia (arm Z) M1 at most %d%%" % int(100 * design4.Z_MAX), not z_hi, "above the limit: %s" % (z_hi or "none"))
    # 9. row counts: no probe class is missing (for example non-literal)
    miss_f = gates.missing_forks(runner.expected_forks(R.cfg), R.rows, R.cfg["seeds"])
    add("row count: every expected probe fork (arm x phrasing x condition x type) has rows", not miss_f, "none missing" if not miss_f else gates.describe_missing(miss_f))
    # 10. arms A, Z, O ran
    arms_bad = [a for a in ("A", "Z", "O") if a in R.cfg["arms"] and not [r for r in R.rows if r["arm"] == a]]
    add("arms A, Z and O produced rows", not arms_bad and any(a in R.cfg["arms"] for a in ("A", "Z", "O")), "no rows for: %s" % (arms_bad or "none"))
    # 11. hook files pinned: sha256 recorded and still equal
    h_bad = []
    for s in [x for x in R.sessions if x["arm"] in runner.LH_ARMS]:
        rec = s.get("hook_sha256")
        d = os.path.join(R.statedir(s), "hooks")
        now = {}
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                p = os.path.join(root, f)
                now[os.path.relpath(p, d)] = runner.sha256_file(p)
        if not rec or rec != now:
            h_bad.append(s["name"])
        if s["arm"] == "K" and rec is not None and ".recall-rules" not in rec:
            h_bad.append(s["name"] + " (no .recall-rules)")
    add("hooks of each arm with hooks: sha256 of every hook file recorded in sessions.jsonl and unchanged (private copy, HANDOFF_HOOKS_HOME)",
        bool([x for x in R.sessions if x["arm"] in runner.LH_ARMS]) and not h_bad, "wrong or missing: %s" % (h_bad or "none"))
    live = runner.sha256_file(os.path.join(runner.HOME, ".claude", "hooks", "recall.py"))
    add("live ~/.claude/hooks/recall.py (the injected text still names it; recorded at run start)", None,
        "sha256 at start %s; now %s%s; pinned copy %s" % ((R.cfg.get("live_recall_sha256") or "none")[:12], (live or "none")[:12],
                                                          " (CHANGED during the run)" if R.cfg.get("live_recall_sha256") != live else "", (R.cfg.get("pinned_recall_sha256") or "none")[:12]))
    # 12. machine state
    fi = []
    for s in R.sessions:
        t = R.transcript(s)
        if t and os.path.exists(t):      # read the transcript again: a run made before the record shape was known stored wrong values
            f = gates.foreign_instructions(jl(t), os.path.join(R.cfg["work_base"], R.stem(s, "A" if s.get("view_of_arm_a") else None)))
        else:
            f = s.get("foreign_instructions")
        if f:
            fi.append((s["name"], f))
    add("no instructions record loaded from outside the project folder of the session", not fi and any("foreign_instructions" in s for s in R.sessions), "found: %s" % (fi or "none"))
    mf = [(s["name"], len(s.get("memory_files") or [])) for s in R.sessions if s.get("memory_files")]
    add("auto-memory: no file in the memory folder of any session", not mf and any("memory_files" in s for s in R.sessions), "files in: %s" % (mf or "none"))
    ok_env = allowed_env_names(R.cfg)
    e_bad = sorted({k for s in R.sessions for k in (s.get("env_keys") or []) if k not in ok_env})
    add("environment of `claude`: only allow-listed names (recorded as env_keys)", not e_bad and any("env_keys" in s for s in R.sessions), "names outside the list: %s" % (e_bad or "none"))
    # 13. deny-rule selfcheck and the big Sonnet turn
    sc = R.state.get("selfcheck")
    add("deny-rule selfcheck (arms A and B, and the printed recall command of B and R in a fork)", bool(sc and sc.get("ok")), "not run" if not sc else ("ok" if sc.get("ok") else "; ".join(sc.get("lines", []))[:200]))
    big = R.state.get("big")
    if not big:
        add("one Sonnet turn with a 300k-token paste (size, price, call cap)", False, "not run")
    else:
        add("one Sonnet turn with a 300k-token paste: reached the size and stayed under the call cap of $%.2f" % big["call_cap_usd"],
            BIG_MIN_TOKENS <= big["tokens"] <= BIG_MAX_TOKENS and not big["over_call_cap"],
            "tokens %d, cost $%.2f (standard price $%.2f), price ratio %s%s" % (big["tokens"], big["cost_usd"], big["standard_price_usd"], big["price_ratio"],
                                                                           "; the price per token is above the standard price (long-context price)" if (big["price_ratio"] or 0) > 1.3 else ""))
    # 13b. forks share the prompt cache: the forks of one no-compaction session write about their probe, not the whole history
    by_s = {}
    for c in R.calls:
        if c.get("kind") == "fork" and str(c.get("name", "")).endswith("-nc"):
            by_s.setdefault(c["name"], []).append(c)
    bad_c, ratio_txt = [], []
    for nm, cs in sorted(by_s.items()):
        frac, n_ = gates.cache_write_fraction(sorted(cs, key=lambda x: x.get("ts", "")))
        if n_ >= 2:
            ratio_txt.append("%s %.2f (%d forks)" % (nm, frac, n_ + 1))
            if frac > gates.CACHE_WRITE_MAX:
                bad_c.append(nm)
    add("no-compaction forks share the prompt cache: cache write at most %d%% of cache read, on average per session" % int(100 * gates.CACHE_WRITE_MAX),
        None if not ratio_txt else not bad_c, "write/read: %s; above the limit: %s" % (", ".join(ratio_txt) or "no session with 3 forks", bad_c or "none"))
    if R.cfg.get("model") == "sonnet":
        bad_cost = []
        for nm, cs in sorted(by_s.items()):
            arm = nm.split("-")[1] if nm.count("-") >= 2 else None
            est = cost.d4_nc_fork_estimate(arm)
            avg = sum(c.get("delta_usd") or 0 for c in cs) / float(len(cs))
            if avg > gates.FORK_COST_RATIO_MAX * est:
                bad_cost.append("%s $%.3f vs estimate $%.3f" % (nm, avg, est))
        add("no-compaction forks cost at most %.0f x the cost.py estimate of a fork (average per session)" % gates.FORK_COST_RATIO_MAX, None if not by_s else not bad_cost,
            "above the limit: %s" % (bad_cost or "none"))
    # 14. cost
    spent = R.state.get("spent", 0.0)
    add("total cost under $%.0f" % COST_CAP_USD, spent < COST_CAP_USD, "$%.2f" % spent)
    est_total, _ = cost.estimate_preflight(tuple(R.cfg["arms"]), tuple(R.cfg.get("conditions", ["compact"])), 4 if R.cfg.get("probe_design", 1) >= 4 else 3)
    ratio = spent / est_total if est_total else None
    parts = []
    for arm in sorted({s["arm"] for s in R.sessions}):
        for c in ("compact", "nocompact"):
            ss = [s for s in R.sessions if s["arm"] == arm and cond(s) == c and not s.get("view_of_arm_a")]
            if ss:
                real = sum(s.get("cost_usd") or 0 for s in ss)
                if arm == "A" and c == "nocompact":
                    real += sum(s.get("cost_usd") or 0 for s in views)
                est = cost.preflight_unit(arm, c, tuple(R.cfg["arms"]), 4 if R.cfg.get("probe_design", 1) >= 4 else 3) * len(ss)
                parts.append("%s %s real $%.2f / estimate $%.2f = %.2f" % (arm, c, real, est, real / est if est else 0))
    add("real cost against the cost.py estimate (ratio at most %.1f; the estimate takes the high price of the big turn)" % COST_RATIO_MAX, ratio is not None and ratio <= COST_RATIO_MAX,
        "ratio %s; %s" % ("n/a" if ratio is None else "%.2f" % ratio, "; ".join(parts)))
    return res


def cond_row(r):
    return r.get("condition", "compact")


def mcp_probe(server, statedir, cwd, timeout=8):
    """Start the MCP server for a moment. initialize, then tools/list. Returns (ok, detail)."""
    if not os.path.isfile(server):
        return False, "no server file %s" % server
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "preflight", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    env = dict(os.environ, HANDOFF_STATE_DIR=statedir, RECALL_EMBED_URL="http://127.0.0.1:9", HANDOFF_HOOKS_HOME=os.path.dirname(server))
    try:
        p = subprocess.run([sys.executable, server], input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=cwd if os.path.isdir(cwd) else None)
    except Exception as e:
        return False, "server did not answer: %s" % e
    names = set()
    for ln in p.stdout.splitlines():
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if m.get("id") == 2:
            names = {t.get("name") for t in (m.get("result") or {}).get("tools", [])}
    ok = {"search_earlier_chat", "open_message"} <= names
    return ok, "tools: %s" % (sorted(names) or "none")


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print("usage: preflight.py <run-id | run folder> [--no-mcp]", file=sys.stderr)
        return 2
    rd = args[0] if os.path.isdir(args[0]) else os.path.join(HERE, "results", args[0])
    if not os.path.isfile(os.path.join(rd, "config.json")):
        print("no such run:", args[0], file=sys.stderr)
        return 2
    res = check(rd, mcp="--no-mcp" not in argv)
    print("PREFLIGHT CHECKLIST: %s" % os.path.basename(rd.rstrip("/")))
    for label, st, detail in res:
        print("%-5s %s  [%s]" % (st, label, detail))
    fails = [x for x in res if x[1] == "FAIL"]
    print("RESULT: %s (%d FAIL of %d lines). %s" % ("FAIL" if fails else "PASS", len(fails), len(res),
          "Do not go to the next step of the ladder." if fails else "You can go to the next step of the ladder (smoke)."))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
