#!/usr/bin/env python3
"""Checklist of the preflight run (run.sh preflight). It reads the finished run and prints PASS or FAIL for each line.
Exit 0 only if no line is FAIL. It makes no model call. (One check starts the recall MCP server of arm K for a second, to see that it answers.)

Usage: preflight.py <run-id | run folder> [--no-mcp]
"""
import json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost, runner  # noqa: E402

COST_CAP_USD = 3.0
COST_RATIO_MAX = 1.3
REANCHOR_TOKENS = 50000
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
        self.rows = jl(os.path.join(rd, "results.jsonl"))
        last = {}
        for s in jl(os.path.join(rd, "sessions.jsonl")):
            last[s["name"]] = s
        self.sessions = [last[k] for k in sorted(last)]
        self.calls = jl(os.path.join(rd, "calls.jsonl"))
        self.projects = projects or runner.PROJECTS

    def stem(self, s):
        return "%s%s-s%d-%s%s" % (runner.PREFIX, self.cfg["run_id"], s["seed"], s["arm"], "-nc" if s.get("condition") == "nocompact" else "")

    def statedir(self, s):
        return os.path.join(self.cfg["work_base"], self.stem(s) + "-state")

    def transcript(self, s):
        sid = self.state["sessions"].get(s["name"], {}).get("sid")
        proj = os.path.realpath(os.path.join(self.cfg["work_base"], self.stem(s)))
        return os.path.join(self.projects, runner.slug(proj), "%s.jsonl" % sid) if sid else None


def check(rd, projects=None, mcp=True):
    """Returns a list of (label, status, detail). status: PASS, FAIL or INFO."""
    R = Run(rd, projects)
    res = []

    def add(label, ok, detail=""):
        res.append((label, "INFO" if ok is None else ("PASS" if ok else "FAIL"), detail))

    comp = [s for s in R.sessions if s.get("condition", "compact") == "compact" and s["arm"] in runner.LH_ARMS]
    nc = [s for s in R.sessions if s.get("condition") == "nocompact"]
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
    # 4. no-compaction sessions
    exp = R.cfg.get("nc_estimate_tokens")
    n_bad = []
    for s in nc:
        tp = R.transcript(s)
        ok, why = runner.nc_validity(s.get("boundaries") or 0, runner.count_cleared(tp) if tp else 0, runner.last_turn_context(R.calls, s["name"]), exp)
        if not ok or s.get("status") == "invalid":
            n_bad.append("%s (%s)" % (s["name"], "; ".join(why) or s.get("reason")))
    add("no-compaction sessions: no compaction, no cleared marker, context at least %d%% of the estimate (%s tokens)" % (100 * runner.NC_CONTEXT_TOLERANCE, exp),
        bool(nc) and not n_bad, "%d session(s); invalid: %s" % (len(nc), n_bad or "none"))
    # 5. arm R: a fire for every cued probe, matched on the fork id, and none for a neutral probe
    r_rows = [r for r in R.rows if r["arm"] == "R"]
    miss, extra = set(), set()
    for s in [x for x in R.sessions if x["arm"] == "R"]:
        ids = {x.get("session_id") for x in jl(os.path.join(R.statedir(s), "auto-recall.jsonl")) if x.get("fired")}
        for r in r_rows:
            if r["seed"] != s["seed"] or (r.get("condition", "compact") != s.get("condition", "compact")):
                continue
            key = (r["seed"], r.get("condition", "compact"), r["type"], r.get("phrasing", "cued"))
            if r.get("phrasing", "cued") == "cued" and (not r.get("auto_recall_fired") or r.get("fork_sid") not in ids):
                miss.add(key)
            if r.get("phrasing", "cued") == "neutral" and (r.get("auto_recall_fired") or r.get("fork_sid") in ids):
                extra.add(key)
    add("arm R: an auto-recall log line for every cued probe (matched on the fork id) and none for a neutral probe", bool(r_rows) and not miss and not extra,
        "%d probe row(s); cued without a line: %d; neutral with a line: %d" % (len(r_rows), len(miss), len(extra)))
    # 6. arm K
    k_sess = [s for s in R.sessions if s["arm"] == "K"]
    if k_sess:
        s0 = k_sess[0]
        hooks = os.path.join(R.statedir(s0), "hooks", "recall-mcp.py")
        ok, detail = (mcp_probe(hooks, R.statedir(s0), os.path.join(R.cfg["work_base"], R.stem(s0))) if mcp else (None, "skipped (--no-mcp)"))
        add("arm K: the recall MCP server starts and lists search_earlier_chat and open_message", ok, detail)
        calls = [r for r in R.rows if r["arm"] == "K" and "mcp" in (r.get("search_kinds") or [])]
        add("arm K: an mcp__lossless-recall__ call happened in a probe (recorded; a call is not required)", None, "%d probe row(s) with an MCP call" % len(calls))
        crossed = [s for s in k_sess if (runner.last_turn_context(R.calls, s["name"]) or 0) >= REANCHOR_TOKENS]
        r_miss = [s["name"] for s in crossed if not any(x.get("event") == "reanchor" for x in jl(os.path.join(R.statedir(s), "health.jsonl")))]
        add("arm K: a reanchor health row exists if the session crossed the interval (%d tokens)" % REANCHOR_TOKENS, not r_miss,
            "%d of %d K session(s) crossed it; missing in: %s" % (len(crossed), len(k_sess), r_miss or "none"))
        rule_bad = []
        for s in [x for x in k_sess if x.get("condition", "compact") == "compact"]:
            txt = text_after_first_boundary(R.transcript(s)) if R.transcript(s) else None
            if txt is None or RECALL_RULE not in txt:
                rule_bad.append(s["name"])
        add("arm K: the recall-rule line is in the injected text", bool([x for x in k_sess if x.get("condition", "compact") == "compact"]) and not rule_bad, "missing in: %s" % (rule_bad or "none"))
    # 7. used_lookup on every probe
    no_ul = [r for r in R.rows if not isinstance(r.get("used_lookup"), bool)]
    add("used_lookup is set for every probe (every arm)", bool(R.rows) and not no_ul, "%d probe row(s); without it: %d" % (len(R.rows), len(no_ul)))
    # 8. parse and grade
    bad_p = [r for r in R.rows if not r.get("parse_ok") or not isinstance(r.get("correct"), bool)]
    add("every probe answer parsed and graded (no grader error)", bool(R.rows) and not bad_p, "%d probe row(s); not parsed or not graded: %d" % (len(R.rows), len(bad_p)))
    # 9. cost
    spent = R.state.get("spent", 0.0)
    add("total cost under $%.0f" % COST_CAP_USD, spent < COST_CAP_USD, "$%.2f" % spent)
    est_total, ratios = 0.0, []
    for arm in sorted({s["arm"] for s in R.sessions}):
        for cond in ("compact", "nocompact"):
            ss = [s for s in R.sessions if s["arm"] == arm and s.get("condition", "compact") == cond]
            if ss:
                real, est = sum(s.get("cost_usd") or 0 for s in ss), cost.preflight_unit(arm, cond) * len(ss)
                est_total += est
                ratios.append("%s %s real $%.2f / estimate $%.2f = %.2f" % (arm, cond, real, est, real / est if est else 0))
    real_total = sum(s.get("cost_usd") or 0 for s in R.sessions)
    ratio = real_total / est_total if est_total else None
    add("real cost against the cost.py estimate (ratio at most %.1f)" % COST_RATIO_MAX, ratio is not None and ratio <= COST_RATIO_MAX,
        "ratio %s; %s" % ("n/a" if ratio is None else "%.2f" % ratio, "; ".join(ratios)))
    return res


def mcp_probe(server, statedir, cwd, timeout=8):
    """Start the MCP server for a moment. initialize, then tools/list. Returns (ok, detail)."""
    if not os.path.isfile(server):
        return False, "no server file %s" % server
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "preflight", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    env = dict(os.environ, HANDOFF_STATE_DIR=statedir, RECALL_EMBED_URL="http://127.0.0.1:9")
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
