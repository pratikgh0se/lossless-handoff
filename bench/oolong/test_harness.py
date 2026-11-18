#!/usr/bin/env python3
"""Offline tests of harness.py and run.sh. No network and no model call. Run: python3 test_harness.py
Cases: size of the settings, the E2BIG stop, the pre-flight, the run id in run.sh, the Read allow list.
Exit 0 only if every case passes."""
import json, os, shutil, stat, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness  # noqa: E402

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
        t = harness.trusted_settings(cfg, arm, harness.env_for(cfg, arm, proj + "-state"))
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
    assert harness.e2big_hit([{"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}])
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
