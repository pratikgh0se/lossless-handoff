#!/usr/bin/env python3
"""Offline retrieval gate (F76), $0, no model call.

    run.sh retrieval-gate [--level L] [--seeds FILE] [--hooks-dir DIR] [--keep]

It writes each generated design-4 session as a synthetic Claude Code transcript in a temporary HOME (never the real ~/.claude): the main
session file, and a fork file that copies the parent history with the same uuids (a real fork does this: checked on a real fork file, see
README) and ends with the probe prompt as the current prompt. Then it runs the retrieval tools of the hooks folder on it:

  - `recall.py history <key>` (CLI) and the MCP tool `key_history`   (not available on a hooks folder without them: reported "not available")
  - the MCP tool `search_earlier_chat` with the key, limit 5 and limit 10
  - the hook auto-recall.py on each cued, neutral and non-literal probe text

and reports for each level: full-chain coverage (every statement of the chain present, in time order), current-value coverage, the lure share
of the injected hits, the lure-only blocks of auto-recall and the sibling leaks (a look-alike key such as `api-v2` for `api`).

Gate before any paid R or K run: `key_history` full-chain coverage of at least 95% at the picked level, and R injects no lure-only block on
cued probes at that level (all of its injected blocks hold the current value).
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys, tempfile, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design4  # noqa: E402

REPO_HOOKS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "hooks")
COVERAGE_MIN = 0.95
TOOLS = ("history_cli", "key_history", "search5", "search10")
LURE_OPS = ("reject", "scoped")
BOUNDARY = r"(?<![A-Za-z0-9_./-])%s(?![A-Za-z0-9_./-])"

# ---- the real Claude Code jsonl shape: the field layout of a fork transcript of a bench run (claude 2.1.326, failed/*.jsonl of preflight4 run
# 20261122T151831: user, assistant, mode and session-owner records).
USER_FIELDS = ("parentUuid", "isSidechain", "promptId", "type", "message", "uuid", "timestamp", "permissionMode", "promptSource", "turnOrigin",
               "turnPosition", "userType", "entrypoint", "cwd", "sessionId", "version", "gitBranch")
ASSISTANT_FIELDS = ("parentUuid", "isSidechain", "message", "apiBlockIndex", "requestId", "requestRef", "requestedModel", "promptCacheRequested", "type",
                    "uuid", "timestamp", "advisorModel", "effort", "perTurnEffort", "madeBy", "userType", "entrypoint", "cwd", "sessionId", "version", "gitBranch")
VERSION = "2.1.326"


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


class Writer:
    """Records of one synthetic session file (one session id)."""

    def __init__(self, sid, cwd, t0):
        self.sid, self.cwd, self.t, self.parent, self.n = sid, cwd, t0, None, 0
        self.records = [{"type": "mode", "mode": "normal", "sessionId": sid},
                        {"type": "session-owner", "accountUuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "acct")), "organizationUuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "org")),
                         "sessionId": sid}]

    def tick(self):
        self.t += datetime.timedelta(seconds=7)
        return iso(self.t)

    def user(self, text, uid):
        self.n += 1
        r = {"parentUuid": self.parent, "isSidechain": False, "promptId": str(uuid.uuid5(uuid.NAMESPACE_DNS, "p" + uid)), "type": "user",
             "message": {"role": "user", "content": text}, "uuid": uid, "timestamp": self.tick(), "permissionMode": "acceptEdits", "promptSource": "sdk",
             "turnOrigin": "sdk", "turnPosition": {"promptIndex": self.n - 1, "turnIndex": 1}, "userType": "external", "entrypoint": "sdk-cli",
             "cwd": self.cwd, "sessionId": self.sid, "version": VERSION, "gitBranch": "main"}
        self.records.append(r)
        self.parent = uid

    def assistant(self, text, uid):
        r = {"parentUuid": self.parent, "isSidechain": False,
             "message": {"model": "claude-haiku-5-5", "id": "msg_" + uid[:22], "type": "message", "role": "assistant", "content": [{"type": "text", "text": text}],
                         "container": None, "stop_reason": "end_turn", "stop_sequence": None, "stop_details": None,
                         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 6},
                         "input_transformations": [], "diagnostics": None, "context_management": None},
             "apiBlockIndex": 0, "requestId": "req_" + uid[:22], "requestRef": str(uuid.uuid5(uuid.NAMESPACE_DNS, "r" + uid)), "requestedModel": "claude-haiku-5-5",
             "promptCacheRequested": "1h", "type": "assistant", "uuid": uid, "timestamp": self.tick(), "advisorModel": "claude-opus-5-5", "effort": "medium",
             "perTurnEffort": "medium", "madeBy": "model", "userType": "external", "entrypoint": "sdk-cli", "cwd": self.cwd, "sessionId": self.sid,
             "version": VERSION, "gitBranch": "main"}
        self.records.append(r)
        self.parent = uid


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def build_session(script, home, seed):
    """Write the main session file of a design-4 script under home. Returns (project dir, projects dir of the slug, main writer)."""
    proj = os.path.realpath(os.path.join(home, "work", "s%d" % seed))
    os.makedirs(proj, exist_ok=True)
    pdir = os.path.join(home, ".claude", "projects", slug(proj))
    os.makedirs(pdir, exist_ok=True)
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "main-%d" % seed))
    w = Writer(sid, proj, datetime.datetime(2026, 11, 22, 8, 0, 0))
    for i, turn in enumerate(script["plan"]):
        if turn["kind"] == "fact":
            text = turn["prompt"]
        else:
            try:
                text = design4.filler_doc_prompt(script, turn)
            except Exception:
                text = "Filler document %d. Keep it for later; no action is needed." % i
        w.user(text, str(uuid.uuid5(uuid.NAMESPACE_DNS, "u-%d-%d" % (seed, i))))
        w.assistant("Noted.", str(uuid.uuid5(uuid.NAMESPACE_DNS, "a-%d-%d" % (seed, i))))
    write_jsonl(os.path.join(pdir, sid + ".jsonl"), w.records)
    return proj, pdir, w


def write_fork(pdir, main, probe_text, tag):
    """A fork file: the parent history copied with the same uuids and message bodies (the session id is the fork's), then the probe as the current prompt."""
    fsid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "fork-" + tag))
    recs = []
    for r in main.records:
        r = dict(r)
        r["sessionId"] = fsid
        recs.append(r)
    w = Writer(fsid, main.cwd, main.t)
    w.records, w.parent, w.n = recs, main.parent, main.n
    w.user(probe_text, str(uuid.uuid5(uuid.NAMESPACE_DNS, "probe-" + tag)))
    path = os.path.join(pdir, fsid + ".jsonl")
    write_jsonl(path, w.records)
    return fsid, path


def collapse(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ---- scoring (pure functions of the tool output)
def item_values(item):
    ops = item.get("ops") or []
    lure = {o["value"] for o in ops if o.get("op") in LURE_OPS and o.get("value")} - {item.get("current")}
    return lure


def stmt_pos(item, text):
    """Positions in text of each statement of the chain (None if absent). A statement is present when its text, or its value for an op with a value, is in the text."""
    t = collapse(text)
    out = []
    for i, st in enumerate(item.get("stmts") or []):
        p = t.find(collapse(st))
        if p < 0:
            ops = item.get("ops") or []
            v = ops[i].get("value") if i < len(ops) else None
            p = t.find(v) if v else -1
        out.append(p if p >= 0 else None)
    return out


def score_chain(item, text):
    """{full: all statements present in time order, any_order: all present, current: the current value is present}."""
    pos = stmt_pos(item, text)
    present = all(p is not None for p in pos) and bool(pos)
    ordered = present and all(pos[i] <= pos[i + 1] for i in range(len(pos) - 1))
    return {"full": ordered, "any_order": present, "current": bool(item.get("current")) and item["current"] in (text or "")}


def sibling_leaks(key, all_keys, text):
    """Lines of the output that mention a look-alike key (a key that extends this key with `-...`, or that this key extends)."""
    n = 0
    for ln in (text or "").splitlines():
        for k in all_keys:
            if k != key and (k.startswith(key + "-") or key.startswith(k + "-")) and re.search(BOUNDARY % re.escape(k), ln, re.I):
                n += 1
                break
    return n


def score_injection(items, text, all_keys=()):
    """Auto-recall output for one probe: per item whether the injected lines of its key hold the current value (lure-only if lines exist but not the value),
    and the lure share of the injected hit lines."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    hit_lines = [ln for ln in lines if any(re.search(BOUNDARY % re.escape(it["key"]), ln, re.I) for it in items)]
    lure_vals = set()
    for it in items:
        lure_vals |= item_values(it)
    lure_hits = [ln for ln in hit_lines if any(v in ln for v in lure_vals)]
    lure_only = 0
    with_current = 0
    for it in items:
        mine = [ln for ln in hit_lines if re.search(BOUNDARY % re.escape(it["key"]), ln, re.I)]
        if not mine:
            continue
        if any(it["current"] in ln for ln in mine):
            with_current += 1
        else:
            lure_only += 1
    return {"fired": bool(lines), "hits": len(hit_lines), "lure_hits": len(lure_hits), "lure_only_blocks": lure_only, "blocks_with_current": with_current}


def gate(rep, level):
    """The gate line of F76. rep: the report of run_gate."""
    r = rep["levels"].get(level)
    if not r:
        return "RETRIEVAL GATE at %s: no data" % level, None
    kh = r["tools"].get("key_history")
    r_ok = r["injection"]["cued"]["lure_only_blocks"] == 0
    r_txt = "R lure-only blocks on cued probes: %d of %d injected blocks (need 0): %s" % (
        r["injection"]["cued"]["lure_only_blocks"], r["injection"]["cued"]["lure_only_blocks"] + r["injection"]["cued"]["blocks_with_current"], "PASS" if r_ok else "FAIL")
    if kh is None:
        return "RETRIEVAL GATE at %s: NOT AVAILABLE (this hooks folder has no key_history); %s" % (level, r_txt), None
    cov = kh["full"] / float(kh["n"]) if kh["n"] else 0.0
    k_ok = cov >= COVERAGE_MIN
    ok = k_ok and r_ok
    return "RETRIEVAL GATE at %s: key_history full-chain coverage %.0f%% (%d/%d), need at least %d%%: %s; %s; GATE %s" % (
        level, 100 * cov, kh["full"], kh["n"], int(100 * COVERAGE_MIN), "PASS" if k_ok else "FAIL", r_txt, "PASS" if ok else "FAIL"), ok


# ---- running the tools
def child_env(home, state, hooks, proj=None):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    env.update({"HOME": home, "HANDOFF_STATE_DIR": state, "HANDOFF_HOOKS_HOME": hooks, "HANDOFF_AUTO_RECALL": "1"})
    if proj:
        env["CLAUDE_PROJECT_DIR"] = proj
    return env


def run_history_cli(hooks, env, proj, key):
    p = subprocess.run([sys.executable, os.path.join(hooks, "recall.py"), "history", key, "--cwd", proj], capture_output=True, text=True, timeout=120, env=env, cwd=proj)
    if p.returncode != 0 and ("invalid choice" in p.stderr or "usage:" in p.stderr):
        return None
    return p.stdout


def mcp_calls(hooks, env, proj, calls):
    """calls: list of (tool, args). One server process for all calls. Returns the list of texts (None when the tool does not exist) and the tool names."""
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "gate", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    for i, (tool, args) in enumerate(calls):
        reqs.append({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    p = subprocess.run([sys.executable, os.path.join(hooks, "recall-mcp.py")], input="\n".join(json.dumps(r) for r in reqs) + "\n", capture_output=True, text=True,
                       timeout=600, env=env, cwd=proj)
    rep = {}
    for ln in p.stdout.splitlines():
        try:
            m = json.loads(ln)
        except Exception:
            continue
        rep[m.get("id")] = m
    names = [t["name"] for t in ((rep.get(2) or {}).get("result") or {}).get("tools", [])]
    out = []
    for i, (tool, _a) in enumerate(calls):
        m = rep.get(10 + i) or {}
        if tool not in names or "result" not in m:
            out.append(None)
            continue
        c = m["result"].get("content") or []
        out.append(c[0].get("text", "") if c else "")
    return out, names


def run_auto_recall(hooks, env, proj, prompt, fsid, fpath):
    d = {"prompt": prompt, "transcript_path": fpath, "cwd": proj, "session_id": fsid, "hook_event_name": "UserPromptSubmit"}
    p = subprocess.run([sys.executable, os.path.join(hooks, "auto-recall.py")], input=json.dumps(d), capture_output=True, text=True, timeout=60, env=env, cwd=proj)
    out = p.stdout.strip()
    try:        # a hook may answer with the JSON form of additionalContext
        j = json.loads(out)
        out = ((j.get("hookSpecificOutput") or {}).get("additionalContext")) or out
    except Exception:
        pass
    return out


def new_tally():
    return {"n": 0, "full": 0, "any_order": 0, "current": 0, "leak_lines": 0, "leak_chains": 0}


def add_tally(t, sc, leaks):
    t["n"] += 1
    t["full"] += 1 if sc["full"] else 0
    t["any_order"] += 1 if sc["any_order"] else 0
    t["current"] += 1 if sc["current"] else 0
    t["leak_lines"] += leaks
    t["leak_chains"] += 1 if leaks else 0


def run_gate(seeds, levels, hooks, keep=False):
    tmp = tempfile.mkdtemp(prefix="lhgate-")
    rep = {"hooks_dir": hooks, "seeds": list(seeds), "levels": {}, "tmp": tmp}
    try:
        state = os.path.join(tmp, "state")
        os.makedirs(state)
        for seed in seeds:
            home = os.path.join(tmp, "home%d" % seed)
            script = design4.build_litm4(seed)
            proj, pdir, main = build_session(script, home, seed)
            env = child_env(home, state, hooks, proj)
            all_keys = sorted({it["key"] for lv in script["items"].values() for it in lv})
            for lv in levels:
                items = script["items"][lv]
                R = rep["levels"].setdefault(lv, {"tools": {}, "injection": {ph: {"probes": 0, "fired": 0, "hits": 0, "lure_hits": 0, "lure_only_blocks": 0, "blocks_with_current": 0}
                                                                          for ph in design4.PHRASINGS}})
                calls = []
                for it in items:
                    calls += [("key_history", {"identifier": it["key"]}), ("search_earlier_chat", {"query": it["key"], "limit": 5}),
                              ("search_earlier_chat", {"query": it["key"], "limit": 10})]
                texts, _names = mcp_calls(hooks, env, proj, calls)
                for j, it in enumerate(items):
                    outs = {"history_cli": run_history_cli(hooks, env, proj, it["key"]), "key_history": texts[3 * j], "search5": texts[3 * j + 1], "search10": texts[3 * j + 2]}
                    for tool, text in outs.items():
                        if text is None:
                            R["tools"].setdefault(tool, None)
                            continue
                        t = R["tools"].get(tool) or new_tally()
                        add_tally(t, score_chain(it, text), sibling_leaks(it["key"], all_keys, text))
                        R["tools"][tool] = t
                for ci, chunk in enumerate(design4.chunks(items, seed, lv)):
                    for ph in design4.PHRASINGS:
                        tag = "%d-%s-%s-%d" % (seed, lv, ph, ci)
                        prompt = design4.probe_prompt(lv, script, "PROBE-%d-A-0-%sc%d" % (seed, lv, ci), ph, items=chunk)
                        fsid, fpath = write_fork(pdir, main, prompt, tag)
                        text = run_auto_recall(hooks, env, proj, prompt, fsid, fpath)
                        sc = score_injection(chunk, text, all_keys)
                        a = R["injection"][ph]
                        a["probes"] += 1
                        a["fired"] += 1 if sc["fired"] else 0
                        for k in ("hits", "lure_hits", "lure_only_blocks", "blocks_with_current"):
                            a[k] += sc[k]
                        os.remove(fpath)
        return rep
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def render(rep, level):
    out = ["RETRIEVAL GATE (offline, $0, no model call). hooks dir: %s; seeds: %s; synthetic transcripts in a temporary HOME" % (rep["hooks_dir"], ",".join(map(str, rep["seeds"]))), ""]
    names = {"history_cli": "recall.py history (CLI)", "key_history": "key_history (MCP)", "search5": "search_earlier_chat limit 5", "search10": "search_earlier_chat limit 10"}
    out += ["| Level | Tool | chains | full chain, in time order | full chain, any order | current value | sibling leak lines (chains) |", "|---|---|---|---|---|---|---|"]
    for lv, R in sorted(rep["levels"].items()):
        for tool in TOOLS:
            t = R["tools"].get(tool)
            if t is None:
                out.append("| %s | %s | not available | | | | |" % (lv, names[tool]))
            else:
                pc = lambda x: "%.0f%% (%d/%d)" % (100.0 * x / t["n"], x, t["n"]) if t["n"] else "no data"
                out.append("| %s | %s | %d | %s | %s | %s | %d (%d) |" % (lv, names[tool], t["n"], pc(t["full"]), pc(t["any_order"]), pc(t["current"]), t["leak_lines"], t["leak_chains"]))
    out += ["", "| Level | Phrasing | probes | auto-recall fired | injected hits | lure share of injected hits | lure-only blocks | blocks with the current value |", "|---|---|---|---|---|---|---|---|"]
    for lv, R in sorted(rep["levels"].items()):
        for ph in design4.PHRASINGS:
            a = R["injection"][ph]
            out.append("| %s | %s | %d | %d | %d | %s | %d | %d |" % (lv, ph, a["probes"], a["fired"], a["hits"], "%.0f%% (%d/%d)" % (100.0 * a["lure_hits"] / a["hits"], a["lure_hits"], a["hits"]) if a["hits"] else "no hits",
                                                              a["lure_only_blocks"], a["blocks_with_current"]))
    line, ok = gate(rep, level)
    out += ["", line]
    return "\n".join(out), ok


def picked_level():
    try:
        import litm4
        th = json.load(open(os.path.join(HERE, "thresholds.json"), encoding="utf-8"))
        (_m, level), _c = litm4.pick_cell(th)
        if level in design4.LEVELS:
            return level
    except Exception:
        pass
    return "L2"


def read_seeds(path):
    p = path or os.path.join(HERE, "seeds-pilot.txt")
    try:
        txt = open(p, encoding="utf-8").read()
    except OSError:
        return [1]
    seeds = [int(x) for x in re.findall(r"\d+", re.sub(r"#.*", "", txt))]
    return seeds or [1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", default=None, help="one level (L1 to L4); the gate is judged at this level (default: the picked level of thresholds.json)")
    ap.add_argument("--seeds", default=None, help="file with seeds (default seeds-pilot.txt)")
    ap.add_argument("--hooks-dir", default=REPO_HOOKS)
    ap.add_argument("--keep", action="store_true", help="keep the temporary HOME (its path is printed)")
    a = ap.parse_args(argv)
    hooks = os.path.abspath(a.hooks_dir)
    if not os.path.isfile(os.path.join(hooks, "recall.py")):
        print("no recall.py in %s" % hooks, file=sys.stderr)
        return 2
    level = a.level or picked_level()
    levels = [level] if a.level else list(design4.D4_LEVELS)
    if level not in levels:
        levels.append(level)
    rep = run_gate(read_seeds(a.seeds), levels, hooks, a.keep)
    text, ok = render(rep, level)
    print(text)
    if a.keep:
        print("kept: %s" % rep["tmp"])
    return 1 if ok is False else 0


if __name__ == "__main__":
    sys.exit(main())
