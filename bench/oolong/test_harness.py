#!/usr/bin/env python3
"""Offline tests of harness.py and run.sh. No network and no model call. Run: python3 test_harness.py
Cases: size of the settings, the E2BIG stop, the pre-flight (settings and environment), the run id in run.sh, the Read allow list,
the silent-failure guards (Bash stop rule on main and subagent transcripts, missing transcript, resume refusal, llm_map successes,
report duplicates and INVALID banner, disallowed tools, own temp folder).
Exit 0 only if every case passes."""
import json, os, shutil, stat, subprocess, sys, tempfile
import textwrap
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("oolong_report", os.path.join(HERE, "report.py"))   # not `import report`: bench/continuity has a report.py too
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

LIMIT = 64 * 1024


def make_cfg(base, run_id="20261118T160603"):
    return {"run_id": run_id, "model": "haiku", "window": 200000, "lh_path": harness.REPO, "cache": os.path.join(base, "cache"),
            "work_base": base, "call_cap_usd": 0.3, "timeout_s": 60, "budget_usd": None, "claude_version": "x", "jobs": 1}


def test_size_is_constant():
    """A run of 6 lengths x 50 tasks x 4 arms has 1200 units; a 300-task run has 7200. The settings must not depend on that."""
    base = "/private/tmp/some-long-work-base-folder-name/lhoo-work"
    cfg = make_cfg(base)
    sizes = {}
    for arm in "ABC":
        proj = os.path.join(base, harness.unit_stem(cfg["run_id"]))
        s = harness.project_settings(cfg, arm, proj=proj)
        t = harness.trusted_settings(cfg, arm, harness.env_for(cfg, arm, proj + "-state", proj))
        sizes[arm] = (len(json.dumps(s)), len(json.dumps(s["sandbox"])), len(json.dumps(t)))
        assert max(sizes[arm]) < LIMIT, (arm, sizes[arm])
    assert not harness.preflight(cfg, ["A", "B", "C", "N"])
    s = json.dumps(harness.project_settings(cfg, "A", proj=base + "/x"))
    assert "lhoo-2026" not in s, "no rule may name a task folder"
    return "bytes (project, sandbox, --settings) per arm: %s" % sizes


def test_preflight_fails_when_large():
    cfg = make_cfg("/private/tmp/x")
    old = harness.deny_rules
    harness.deny_rules = lambda c: ["Read(/%s/**)" % ("a" * 100) for _ in range(1000)] + [str(i) for i in range(2000)]
    try:
        probs = harness.preflight(cfg, ["A"])
    finally:
        harness.deny_rules = old
    assert probs and "E2BIG" in probs[0], probs


def test_read_tool_allow_list():
    assert "Read" not in harness.ALLOWED_TOOLS.split(), "a bare Read allow rule opens the folders of other units"
    assert harness.ALLOWED_TOOLS.split() == ["Write", "Edit", "Bash", "Task"]


def test_unit_stems_are_random():
    a, b = harness.unit_stem("r"), harness.unit_stem("r")
    assert a != b and len(a) >= 32 + len("lhoo-r-")


def test_e2big_detection():
    text = "Could not start /bin/sh: the command line plus environment exceed the OS exec argument limit (E2BIG)."
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]}}
    assert harness.e2big_hit([use, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "is_error": True, "content": text}]}}])
    assert harness.e2big_hit([], "x E2BIG y")
    assert harness.e2big_hit([], "they exceed the OS exec argument limit")
    assert not harness.e2big_hit([{"type": "user", "message": {"content": "all fine"}}], "ok")


def test_run_stops_on_e2big():
    """A stub `claude` prints the E2BIG text. execute must stop at the first unit and end with status failed."""
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    try:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "claude")
        body = json.dumps({"result": "Could not start /bin/sh: exceed the OS exec argument limit (E2BIG). At spawn: command line 1.2MB across 3 args",
                           "total_cost_usd": 0.01, "is_error": False, "num_turns": 1})
        with open(stub, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n" + body + "\nEOF\n")
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        old_path, old_results = os.environ["PATH"], harness.RESULTS
        os.environ["PATH"] = bindir + os.pathsep + old_path
        harness.RESULTS = os.path.join(tmp, "results")
        try:
            work = os.path.join(tmp, "work")
            os.makedirs(work)
            cfg = make_cfg(os.path.realpath(work), "20261118T000000")
            run_dir = os.path.join(tmp, "run")
            os.makedirs(run_dir)
            tasks = [{"task_id": str(100 + i), "question": "q?", "context": "c", "answer": "['x']", "answer_type": "ANSWER_TYPE.LABEL",
                      "task": "t", "length": 8192} for i in range(4)]
            recs = {(t["task_id"], "A"): harness.config_record(cfg, t, "A") for t in tasks}
            ctx = harness.Ctx(run_dir, cfg)
            code = harness.execute(ctx, tasks, ["A"], recs)
            assert code == 1 and ctx.state["status"] == "failed", (code, ctx.state["status"])
            assert "E2BIG" in ctx.state["error"]
            assert ctx.stop.is_set()
            started = [u for u, v in ctx.state["units"].items() if v.get("status") != "new"]
            assert len(started) == 1, "run must stop at the first E2BIG unit with jobs=1: %s" % started
            assert [v["status"] for v in ctx.state["units"].values()] == ["failed"]
        finally:
            os.environ["PATH"], harness.RESULTS = old_path, old_results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


STUB = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, re, sys
    mode = open(os.environ["STUB_MODE_FILE"]).read().strip()
    a = sys.argv
    sid = a[a.index("--session-id") + 1]
    d = os.path.join(os.environ["STUB_PROJECTS"], re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
    os.makedirs(d, exist_ok=True)

    def use(i, name="Bash"):
        return {"type": "assistant", "message": {"id": "m%s" % i, "content": [{"type": "tool_use", "id": "t%s" % i, "name": name, "input": {"command": "ls"}}]}}

    def res(i, text, err):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%s" % i, "is_error": err, "content": text}]}}

    text = {"type": "assistant", "message": {"id": "mz", "content": [{"type": "text", "text": "Answer: x"}]}}
    INFRA = "Could not start /bin/sh: spawn failed"
    main, sub = [text], []
    if mode == "infra3main":
        for i in range(3):
            main += [use(i), res(i, INFRA, True)]
    elif mode == "infra3sub":
        main += [use(9, "Task")]
        for i in range(3):
            sub += [use(i), res(i, INFRA, True)]
    elif mode == "infra1":
        main += [use(0), res(0, INFRA, True)]
    elif mode == "llm":
        main += [use(0), res(0, "llm_map: 5 items, 5 ok, 0 failed, 1 batches, cost $0.5000", False)]
    elif mode == "ok3":
        for i in range(3):
            main += [use(i), res(i, "fine", False)]
    elif mode == "denied3":
        for i in range(3):
            main += [use(i), res(i, "Permission to use Bash with command curl has been denied.", True)]
    elif mode == "exit1":
        for i in range(3):
            main += [use(i), res(i, "Exit code 1\\nnot found", True)]
    if mode != "missing":
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write("".join(json.dumps(x) + "\\n" for x in main))
        if sub:
            os.makedirs(os.path.join(d, sid, "subagents"), exist_ok=True)
            with open(os.path.join(d, sid, "subagents", "agent-1.jsonl"), "w") as f:
                f.write("".join(json.dumps(x) + "\\n" for x in sub))
    print(json.dumps({"result": "Answer: x", "total_cost_usd": 0.01, "is_error": False, "num_turns": 2, "permission_denials": []}))
""")


def run_stub(mode, n_tasks=4, arms=("A",), window=None, jobs=1):
    """Run execute() with a stub `claude` that writes transcripts of the given mode. Returns (code, ctx, rows, tmp-less info)."""
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    old_path, old_results, old_projects = os.environ["PATH"], harness.RESULTS, harness.PROJECTS
    try:
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "claude")
        open(stub, "w").write(STUB)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        open(os.path.join(tmp, "mode"), "w").write(mode)
        os.environ.update(STUB_MODE_FILE=os.path.join(tmp, "mode"), STUB_PROJECTS=os.path.join(tmp, "projects"))
        os.environ["PATH"] = bindir + os.pathsep + old_path
        harness.RESULTS, harness.PROJECTS = os.path.join(tmp, "results"), os.path.join(tmp, "projects")
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        cfg = make_cfg(os.path.realpath(work), "20261118T000001")
        cfg["jobs"] = jobs
        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir)
        tasks = [{"task_id": str(100 + i), "question": "q?", "context": "c", "answer": "['x']", "answer_type": "ANSWER_TYPE.LABEL",
                  "task": "t", "length": 8192} for i in range(n_tasks)]
        recs = {(t["task_id"], arm): harness.config_record(cfg, t, arm) for t in tasks for arm in arms}
        ctx = harness.Ctx(run_dir, cfg)
        if window:
            ctx.state["bash_window"] = window
        code = harness.execute(ctx, tasks, list(arms), recs)
        rows = [json.loads(x) for x in open(os.path.join(run_dir, "results.jsonl"))] if os.path.exists(os.path.join(run_dir, "results.jsonl")) else []
        tdirs = sorted(os.listdir(os.path.join(run_dir, "transcripts"))) if os.path.isdir(os.path.join(run_dir, "transcripts")) else []
        return code, ctx, rows, tdirs
    finally:
        os.environ["PATH"], harness.RESULTS, harness.PROJECTS = old_path, old_results, old_projects
        shutil.rmtree(tmp, ignore_errors=True)


def test_e2big_marker_argument_list():
    assert "Argument list too long" in harness.E2BIG_MARKERS
    assert harness.e2big_hit([], "bash: /bin/ls: Argument list too long")


def test_bash_stats_classes():
    """Exit code failures and denials are not infrastructure errors. Other Bash errors are. Per-tool error counts work."""
    def u(i, name):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": {}}]}}

    def r(i, text, err=True):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": err, "content": [{"type": "text", "text": text}]}]}}
    recs = [u("a", "Bash"), r("a", "Exit code 2\nx"), u("b", "Bash"), r("b", "Could not start /bin/sh"), u("c", "Bash"), r("c", "Permission to use Bash has been denied"),
            u("d", "Bash"), r("d", "ok", False), u("e", "Read"), r("e", "File not found"), u("f", "Bash")]
    errs, b = harness.tool_error_stats(recs)
    assert errs == {"Bash": 3, "Read": 1}, errs
    assert (b["calls"], b["started"], b["infra"], b["denied"]) == (5, 3, 1, 1), b
    assert harness.infra_unit_trip({"calls": 3, "started": 0, "infra": 3, "examples": ["e"]})
    assert not harness.infra_unit_trip({"calls": 2, "started": 0, "infra": 2})
    assert not harness.infra_unit_trip({"calls": 3, "started": 1, "infra": 2})
    assert harness.infra_unit_trip({"calls": 1, "started": 0, "infra": 1}, 1)


def test_window_rule():
    assert not harness.infra_window_trip([(3, 3, 0)] * 3 + [(1, 0, 1)])           # 1 of 10 calls
    assert harness.infra_window_trip([(5, 5, 0), (5, 3, 2), (1, 0, 1)])           # 3 of 11 is above 20 percent
    assert not harness.infra_window_trip([(2, 0, 2)])                              # under the minimum number of calls


def test_stop_rule_main_transcript():
    code, ctx, rows, _t = run_stub("infra3main")
    assert code == 1 and ctx.state["status"] == "failed", (code, ctx.state["status"])
    assert "none started" in ctx.state["error"] and ctx.stop.is_set()
    assert len(rows) == 1 and rows[0]["bash_infra"] == 3 and rows[0]["tool_errors"] == {"Bash": 3}, rows


def test_stop_rule_subagent_transcript():
    """The infrastructure errors sit only in a subagent transcript. The rule must still see them and the copy must be kept."""
    code, ctx, rows, tdirs = run_stub("infra3sub")
    assert code == 1 and "none started" in ctx.state["error"], (code, ctx.state)
    assert rows[0]["bash_calls"] == 3 and rows[0]["bash_infra"] == 3
    assert any(not t.endswith(".jsonl") for t in tdirs), "subagent transcripts must be copied into transcripts/<unit>/: %s" % tdirs


def test_canary_unit_is_strict_and_runs_alone():
    code, ctx, rows, _t = run_stub("infra1", n_tasks=6, jobs=3)
    assert code == 1 and "first unit of its arm" in ctx.state["error"], ctx.state.get("error")
    assert len(rows) == 1, "only the first unit may run before the pool: %d" % len(rows)


def test_denials_and_exit_codes_do_not_stop():
    for mode in ("denied3", "exit1", "ok3"):
        code, ctx, rows, _t = run_stub(mode, n_tasks=2)
        assert code == 0 and ctx.state["status"] == "done", (mode, code, ctx.state.get("error"))
    assert rows[0]["bash_infra"] == 0 and rows[0]["tool_errors"] == {}


def test_window_stop_in_run():
    # 9 earlier units: 9 calls, 3 infra. This unit adds 3 good calls: 3 of 12 is 25 percent.
    win = [[1, 1, 0]] * 6 + [[1, 0, 1]] * 3
    code, ctx, rows, _t = run_stub("ok3", n_tasks=3, window=[list(w) for w in win])
    assert code == 1 and "infrastructure errors" in ctx.state["error"] and "last" in ctx.state["error"], ctx.state.get("error")


def test_missing_transcript_stops():
    code, ctx, rows, _t = run_stub("missing", n_tasks=3)
    assert code == 1 and "transcript is missing or empty" in ctx.state["error"], ctx.state.get("error")


def test_resume_refuses_changed_config():
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    try:
        cfg = make_cfg(tmp)
        cfg["bench_commit"] = harness.sh(["git", "rev-parse", "--short", "HEAD"], cwd=harness.REPO)
        cfg["claude_version"] = harness.sh(["claude", "--version"])
        t = {"task_id": "1", "question": "q?", "context": "c", "answer": "x", "answer_type": "ANSWER_TYPE.LABEL", "task": "t", "length": 8192}
        recs = {("1", "A"): harness.config_record(cfg, t, "A")}
        json.dump({"%s|%s" % k: v for k, v in recs.items()}, open(os.path.join(tmp, "config_records.json"), "w"))
        assert harness.resume_problems(cfg, tmp, recs) == []
        assert any("bench_commit" in x for x in harness.resume_problems(dict(cfg, bench_commit="deadbee"), tmp, recs))
        other = {("1", "A"): dict(recs[("1", "A")], deny_rules_sha="changed")}
        probs = harness.resume_problems(cfg, tmp, other)
        assert probs and "hash differs" in probs[0] and "deny_rules_sha" in probs[0], probs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_llm_map_successes():
    def call(i):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": "python3 /x/hooks/llm_map.py --in a"}}]}}

    def out(i, t):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": t}]}}
    recs = [call("1"), out("1", "llm_map: 100 items, 0 ok, 100 failed, 1 batches, cost $0.0100"), call("2"),
            out("2", "llm_map: 100 items, 98 ok, 2 failed, 1 batches, cost $0.0200"), call("3"), out("3", "Traceback crash")]
    st = harness.llm_map_stats(recs)
    assert (st["llm_map_calls"], st["llm_map_ok_calls"], st["llm_map_summaries"]) == (3, 1, 2), st


def test_preflight_environment_size():
    cfg = make_cfg("/private/tmp/x")
    os.environ["LHOO_TEST_BIG"] = "x" * (harness.PREFLIGHT_MAX_ENV + 10)
    try:
        probs = harness.preflight(cfg, ["A"])
    finally:
        del os.environ["LHOO_TEST_BIG"]
    assert probs and "environment" in probs[0] and "E2BIG" in probs[0], probs
    assert not harness.preflight(cfg, ["A"])


def test_disallowed_and_tmpdir():
    for t in ("ScheduleWakeup", "Monitor", "CronCreate", "CronDelete", "CronList", "Workflow", "WebSearch", "WebFetch"):
        assert t in harness.DISALLOWED_BASE, t
    cfg = make_cfg("/private/tmp/x")
    env = harness.env_for(cfg, "A", "/p/u-state", "/p/u")
    assert env["CLAUDE_CODE_TMPDIR"] == "/p/u/.cctmp"
    assert "CLAUDE_CODE_TMPDIR" in harness.COMMON_ENV
    s = harness.project_settings(cfg, "A", proj="/private/tmp/x/u")
    deny = s["sandbox"]["filesystem"]["denyRead"]
    for root in harness.repo_roots():
        assert root in deny, root
    assert os.path.join(os.path.realpath("/tmp/claude-%d" % os.getuid()), "-*") in deny
    assert s["sandbox"]["filesystem"]["allowRead"] == ["/private/tmp/x/u"]
    assert os.path.realpath("/tmp/claude-%d" % os.getuid()) in s["sandbox"]["filesystem"]["denyWrite"]


def test_report_duplicates_banner_and_rescan():
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    old = report.HERE
    try:
        d = os.path.join(tmp, "results", "R1")
        os.makedirs(os.path.join(d, "transcripts", "7-A", "subagents"))
        json.dump({"model": "haiku"}, open(os.path.join(d, "config.json"), "w"))
        base = {"unit": "7-A", "task_id": "7", "arm": "A", "length": 8192, "score": 0.0, "is_error": False, "full_answer": "", "bash_calls": 0}
        with open(os.path.join(d, "results.jsonl"), "w") as f:
            f.write(json.dumps(dict(base, score=0.0)) + "\n" + json.dumps(dict(base, score=1.0)) + "\n")
        report.HERE = tmp
        _d, _c, rows = report.load("R1")
        assert len(rows) == 1 and rows[0]["score"] == 1.0, rows
        # a stored subagent transcript with 3 infrastructure errors; the main one is clean
        main = [{"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "Answer: x"}]}}]
        sub = []
        for i in range(3):
            sub += [{"type": "assistant", "message": {"id": "s%d" % i, "content": [{"type": "tool_use", "id": "t%d" % i, "name": "Bash", "input": {}}]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "is_error": True, "content": "Could not start /bin/sh"}]}}]
        open(os.path.join(d, "transcripts", "7-A.jsonl"), "w").write("".join(json.dumps(x) + "\n" for x in main))
        open(os.path.join(d, "transcripts", "7-A", "subagents", "agent-1.jsonl"), "w").write("".join(json.dumps(x) + "\n" for x in sub))
        rows = report.rescan(d, rows)
        assert rows[0]["bash_calls"] == 3 and rows[0]["bash_infra"] == 3, rows[0]
        msgs = harness.infra_scan(rows)
        assert msgs and "none started" in msgs[0], msgs
        assert not harness.infra_scan([dict(base, bash_calls=5, bash_started=5, bash_infra=0)])
    finally:
        report.HERE = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_e2big_ignores_model_output():
    """A command that prints the marker is no hit: only a Bash error that does not start with "Exit code", result text and stderr count."""
    use = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}},
                                                        {"type": "tool_use", "id": "y", "name": "Read", "input": {}}]}}

    def res(i, text, err):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": err, "content": text}]}}
    assert not harness.e2big_hit([use, res("x", "Exit code 126\nbash: ls: Argument list too long", True)])
    assert not harness.e2big_hit([use, res("x", "the text Argument list too long printed by echo", False)])
    assert not harness.e2big_hit([use, res("y", "Argument list too long", True)])      # not a Bash result
    assert harness.e2big_hit([use, res("x", "Could not start /bin/sh: Argument list too long", True)])
    assert harness.e2big_hit([use], "stderr E2BIG")


def test_scratch_deny_rules():
    cfg = make_cfg("/private/tmp/x")
    rules = harness.deny_rules(cfg)
    for w in ("TMPDIR", "mktemp", "tempfile"):
        assert "Bash(*%s*)" % w in rules, w
    assert not {"Bash(*/tmp*)", "Bash(*/tmp/*)", "Bash(*/private/tmp*)", "Bash(*/private/tmp/*)"} & set(rules), "/tmp itself must not be denied"
    assert "scratch file in the current folder" in harness.PROMPT_CORE


def test_shared_tmp_refs():
    def use(name, inp):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "i", "name": name, "input": inp}]}}
    recs = [use("Bash", {"command": "echo a > $TMPDIR/q.txt; cat /tmp/claude-501/q.txt"}),
            use("Bash", {"command": "python3 /private/tmp/claude-501/w/c.py"}),
            use("Write", {"file_path": "/tmp/claude-501/d.txt"}),
            use("Read", {"file_path": "/private/tmp/claude-501/labels.jsonl"}),
            use("Bash", {"command": "cat /private/tmp/claude-501/-Users-x-scratchpad/work/lhoo-r-abc/context.txt"}),   # session folder: no ref
            use("Bash", {"command": "cat /private/tmp/claude-501/unitbase/lhoo-r-abc/ownfile.txt"}),                    # inside exclude
            use("Grep", {"pattern": "/tmp/claude-501/z"})]
    refs = harness.shared_tmp_refs(recs, ("/private/tmp/claude-501/unitbase",))
    assert len(refs) == 5, refs
    assert harness.shared_tmp_refs(recs[4:6], ("/private/tmp/claude-501/unitbase",)) == []


def test_shared_tmp_refs_cover_reads():
    """Read tool inputs and Bash commands that only READ a shared path count, in main and in subagent records alike."""
    def use(name, inp, i="i"):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}
    main = [use("Bash", {"command": "cat /tmp/claude-501/x"}), use("Read", {"file_path": "/tmp/claude-501/y.txt"})]
    sub = [use("Bash", {"command": "grep a /private/tmp/claude-501/z && cat ${TMPDIR}/w"})]
    assert len(harness.shared_tmp_refs(main)) == 2
    assert len(harness.shared_tmp_refs(main + sub)) == 4
    assert len(harness.shared_tmp_refs(sub)) == 2


def test_accept_loose_tmp():
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    try:
        root = os.path.join(tmp, "claude-501")
        os.makedirs(root)
        open(os.path.join(root, "q.txt"), "w").write("a")
        os.makedirs(os.path.join(root, "d"))
        ok, msgs = harness.tmp_guard(root, False)
        assert not ok and "--accept-loose-tmp" in msgs[0]
        ok, msgs = harness.tmp_guard(root, False, accept=True)
        assert ok and msgs[0].startswith("WARNING")
        rec = {r["name"]: r for r in harness.loose_tmp_record(root)}
        assert len(rec["q.txt"]["sha256"]) == 64 and rec["d"]["sha256"] is None and rec["q.txt"]["bytes"] == 1 and rec["q.txt"]["mtime"]
        assert os.path.exists(os.path.join(root, "q.txt")), "nothing is moved"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_spent_includes_llm_map():
    code, ctx, rows, _t = run_stub("llm", n_tasks=2)
    assert code == 0, code
    assert abs(rows[0]["llm_map_cost_usd"] - 0.5) < 1e-9, rows[0]
    assert abs(ctx.state["spent"] - 2 * 0.51) < 1e-6, ctx.state["spent"]
    assert abs(ctx.state["max_unit_cost"] - 0.51) < 1e-6


def test_tmpcheck_quarantine():
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    try:
        root = os.path.join(tmp, "claude-501")
        os.makedirs(os.path.join(root, "-Users-x-session"))
        open(os.path.join(root, "q.txt"), "w").write("a")
        os.makedirs(os.path.join(root, "w"))
        open(os.path.join(root, "w", "items.txt"), "w").write("b")
        names = [n for n, _s, _m in harness.loose_tmp_items(root)]
        assert names == ["q.txt", "w"], names
        ok, msgs = harness.tmp_guard(root, False)
        assert not ok and "tmpcheck --quarantine-tmp" in msgs[0]
        assert harness.tmp_guard(os.path.join(tmp, "nothing"), False)[0]
        busy = open(os.path.join(root, "q.txt"))          # an open file stays
        ok, msgs = harness.tmp_guard(root, True)
        busy.close()
        assert not ok and any("stayed" in m for m in msgs), msgs
        dests = [d for d in os.listdir(tmp) if d.startswith("claude-501-quarantine-")]
        assert len(dests) == 1
        assert open(os.path.join(tmp, dests[0], "w", "items.txt")).read() == "b", "moved, not deleted"
        assert os.path.isdir(os.path.join(root, "-Users-x-session")), "session folders stay"
        assert os.path.exists(os.path.join(root, "q.txt"))
        ok, msgs = harness.tmp_guard(root, True)      # now nothing is open
        assert ok and os.path.exists(os.path.join(tmp, [d for d in os.listdir(tmp) if d.startswith("claude-501-quarantine-")][-1], "q.txt")), msgs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_report_shared_tmp_refs():
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    old = report.HERE
    try:
        d = os.path.join(tmp, "results", "R2")
        os.makedirs(os.path.join(d, "transcripts", "7-A", "subagents"))
        json.dump({"model": "haiku", "work_base": "/private/tmp/claude-501/unitbase"}, open(os.path.join(d, "config.json"), "w"))
        row = {"unit": "7-A", "task_id": "7", "arm": "A", "length": 8192, "score": 1.0, "is_error": False, "full_answer": ""}
        open(os.path.join(d, "results.jsonl"), "w").write(json.dumps(row) + "\n")
        report.HERE = tmp
        sub = [{"type": "assistant", "message": {"id": "s", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "echo x > /tmp/claude-501/c.py"}}]}}]
        main = [{"type": "assistant", "message": {"id": "m", "content": [{"type": "text", "text": "Answer: x"}]}}]
        open(os.path.join(d, "transcripts", "7-A.jsonl"), "w").write(json.dumps(main[0]) + "\n")
        open(os.path.join(d, "transcripts", "7-A", "subagents", "a.jsonl"), "w").write(json.dumps(sub[0]) + "\n")
        _d, _c, rows = report.load("R2")
        rows = report.rescan(d, rows)
        assert rows[0]["shared_tmp_ref_count"] == 1, rows[0]
    finally:
        report.HERE = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_run_sh_uses_own_run_id():
    """run.sh must report the run id that its own harness printed, not the one in results/.last (a parallel run overwrites it)."""
    tmp = tempfile.mkdtemp(prefix="lhoo-test-")
    try:
        shutil.copy(os.path.join(HERE, "run.sh"), tmp)
        os.makedirs(os.path.join(tmp, "results", "OTHER"))
        open(os.path.join(tmp, "results", ".last"), "w").write("OTHER")
        open(os.path.join(tmp, "results", "OTHER", "results.jsonl"), "w").write("{}\n")
        fake = ("import os, sys\nD = os.path.dirname(os.path.abspath(__file__))\nprint('run_id: MINE', flush=True)\nprint('other output')\n"
                "os.makedirs(os.path.join(D, 'results', 'MINE'))\nopen(os.path.join(D, 'results', 'MINE', 'results.jsonl'), 'w').write('{}\\n')\n"
                "open(os.path.join(D, 'results', '.last'), 'w').write('OTHER')\nsys.exit(RC)\n")
        open(os.path.join(tmp, "harness.py"), "w").write(fake.replace("RC", "0"))
        open(os.path.join(tmp, "report.py"), "w").write(
            "import os, sys\nopen(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reported.txt'), 'a').write(sys.argv[1] + '\\n')\n")
        r = subprocess.run(["bash", os.path.join(tmp, "run.sh"), "run", "--x"], capture_output=True, text=True)
        assert r.returncode == 0, (r.returncode, r.stderr)
        got = open(os.path.join(tmp, "reported.txt")).read().split()
        assert got == ["MINE"], got
        assert "results/MINE/summary.md" in r.stdout
        shutil.rmtree(os.path.join(tmp, "results", "MINE"))
        open(os.path.join(tmp, "harness.py"), "w").write(fake.replace("RC", "1"))
        r = subprocess.run(["bash", os.path.join(tmp, "run.sh"), "run"], capture_output=True, text=True)
        assert r.returncode == 1, r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_harness_prints_run_id_line():
    src = open(os.path.join(HERE, "harness.py"), encoding="utf-8").read()
    assert 'print("run_id: %s" % cfg["run_id"], flush=True)' in src


def main():
    bad = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            note = t()
            print("PASS %s%s" % (t.__name__, (": " + note) if note else ""))
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("FAIL %s: %r" % (t.__name__, e))
    print("%d of %d passed" % (len(tests) - bad, len(tests)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
