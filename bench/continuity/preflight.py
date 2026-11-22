#!/usr/bin/env python3
"""Checklist of the preflight run (run.sh preflight). It reads the finished run and prints PASS or FAIL for each line.
Exit 0 only if no line is FAIL. It makes no model call. (One check starts the recall MCP server of arm K for a second, to see that it answers.)

Usage: preflight.py <run-id | run folder> [--no-mcp]
"""
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost, gates, runner  # noqa: E402

COST_CAP_USD = 5.0
COST_RATIO_MAX = 1.3
REANCHOR_TOKENS = 50000
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
        r_miss = [s["name"] for s in crossed if not any(x.get("event") == "reanchor" for x in jl(os.path.join(R.statedir(s), "health.jsonl")))]
        add("arm K: a reanchor health row exists if the session crossed the interval (%d tokens)" % REANCHOR_TOKENS, not r_miss,
            "%d of %d K compaction session(s) crossed it; missing in: %s" % (len(crossed), len([s for s in k_sess if cond(s) == "compact"]), r_miss or "none"))
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
    # 8. parse and grade
    # Parse policy (one threshold, gates.PARSE_ERROR_RATE_MAX, the same as the abort gate): a reply that cannot be read counts as wrong for every arm.
    # Arm Z (no history) is excluded from the rate, as in the abort gate: it may answer with fewer entries or in prose (INFO only).
    # A row whose `correct` is not true or false is a harness error: always a FAIL.
    not_graded = [r for r in R.rows if not isinstance(r.get("correct"), bool)]
    unread = [r for r in R.rows if not r.get("parse_ok") and not isinstance(r.get("parsed"), dict)]       # no readable entry for the item
    dev = [r for r in R.rows if not r.get("parse_ok") and isinstance(r.get("parsed"), dict)]              # entry read, field rejected
    scored = [r for r in R.rows if r["arm"] != "Z"]
    unread_s = [r for r in unread if r["arm"] != "Z"]
    rate = len(unread_s) / float(len(scored)) if scored else 1.0
    kinds = sorted({(r["arm"], r.get("phrasing"), r.get("type"), r.get("condition")) for r in unread + dev})
    add("every probe answer parsed and graded (no grader error)", bool(R.rows) and not not_graded and rate <= gates.PARSE_ERROR_RATE_MAX,
        "%d probe row(s); not graded: %d; unreadable (no entry): %d, of them arm Z %d; rate outside Z %.2f%% (limit %.0f%%); format deviations: %d; classes %s" % (
            len(R.rows), len(not_graded), len(unread), len(unread) - len(unread_s), 100 * rate, 100 * gates.PARSE_ERROR_RATE_MAX, len(dev), kinds))
    # 8b. the non-literal probes must be answerable: the short oracle (arm O, no compaction) gets at least 80% on every type
    o_nl = [r for r in R.rows if r["arm"] == "O" and r.get("phrasing") == "nonliteral" and cond_row(r) == "nocompact"]
    by_t = {}
    for r in o_nl:
        c = by_t.setdefault(r["type"], [0, 0])
        c[0] += 1 if r.get("correct") is True else 0
        c[1] += 1
    low = {t: "%d/%d" % tuple(c) for t, c in sorted(by_t.items()) if c[0] < NL_ORACLE_MIN * c[1]}
    add("nonliteral oracle (arm O) accuracy at least %d%% per type" % int(NL_ORACLE_MIN * 100), bool(by_t) and not low,
        "per type: %s%s" % ({t: "%d/%d" % tuple(c) for t, c in sorted(by_t.items())}, "; below the limit: %s" % low if low else ""))
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
    add("deny-rule selfcheck (arms A and B)", bool(sc and sc.get("ok")), "not run" if not sc else ("ok" if sc.get("ok") else "; ".join(sc.get("lines", []))[:200]))
    big = R.state.get("big")
    if not big:
        add("one Sonnet turn with a 300k-token paste (size, price, call cap)", False, "not run")
    else:
        add("one Sonnet turn with a 300k-token paste: reached the size and stayed under the call cap of $%.2f" % big["call_cap_usd"],
            BIG_MIN_TOKENS <= big["tokens"] <= BIG_MAX_TOKENS and not big["over_call_cap"],
            "tokens %d, cost $%.2f (standard price $%.2f), price ratio %s%s" % (big["tokens"], big["cost_usd"], big["standard_price_usd"], big["price_ratio"],
                                                                           "; the price per token is above the standard price (long-context price)" if (big["price_ratio"] or 0) > 1.3 else ""))
    # 14. cost
    spent = R.state.get("spent", 0.0)
    add("total cost under $%.0f" % COST_CAP_USD, spent < COST_CAP_USD, "$%.2f" % spent)
    est_total, _ = cost.estimate_preflight(tuple(R.cfg["arms"]), tuple(R.cfg.get("conditions", ["compact"])))
    ratio = spent / est_total if est_total else None
    parts = []
    for arm in sorted({s["arm"] for s in R.sessions}):
        for c in ("compact", "nocompact"):
            ss = [s for s in R.sessions if s["arm"] == arm and cond(s) == c and not s.get("view_of_arm_a")]
            if ss:
                real = sum(s.get("cost_usd") or 0 for s in ss)
                if arm == "A" and c == "nocompact":
                    real += sum(s.get("cost_usd") or 0 for s in views)
                est = cost.preflight_unit(arm, c, tuple(R.cfg["arms"])) * len(ss)
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
