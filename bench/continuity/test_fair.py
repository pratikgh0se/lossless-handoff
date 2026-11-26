#!/usr/bin/env python3
"""Tests of the fair re-test (SPEC.md section 16): the presets, the deny rules of arm Af, the reads scan, the pre-registered rule, the INVALID gates, the
stop rule, the wording of the summary and the fair branch of the selfcheck. No model call, no cost."""
import fnmatch, json, os, re, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fair, proof, runner  # noqa: E402

TH = json.load(open(os.path.join(HERE, "thresholds.json")))
SEEDS = [3001, 3002]
ITEMS = 10
BASE = "/private/tmp/lhb"
RUN = "20261126T101010-fair-step1"
PROJ = os.path.join(runner.HOME, ".claude", "projects")


def proj_dir(arm, seed=3001, suffix=""):
    """The transcript folder of a bench session: the project path with dashes."""
    return os.path.join(PROJ, "-private-tmp-lhb-lhbench-%s-s%d-%s%s" % (RUN, seed, arm, suffix))


def read_denied(tool, path, rules):
    """Emulation of a Read, Grep or Glob deny rule: Tool(//abs/**) or Tool(~/...). * stays inside one path part, ** crosses folders. Not Claude Code itself."""
    for r in rules:
        if not r.startswith(tool + "(") or not r.endswith(")"):
            continue
        pat = r[len(tool) + 1:-1]
        pat = os.path.expanduser(pat) if pat.startswith("~") else pat.replace("//", "/", 1)
        rx = "^" + re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
        if re.match(rx, path):
            return True
    return False


class Presets(unittest.TestCase):
    def test_kinds(self):
        s1, s2, pf = runner.KINDS["fair-step1"], runner.KINDS["fair-step2"], runner.KINDS["fair-preflight"]
        self.assertEqual((s1["arms"], s2["arms"], pf["arms"]), ("Af,O,Z", "B", "Af,B"))
        for k in (s1, s2):
            self.assertEqual((k["model"], k["window"], k["n"], k["seeds"], k["seed_file"], k["conditions"], k["phrasings"], k["probe_design"]),
                             ("sonnet", 400000, [10], 2, "seeds-fair.txt", "compact", "cued,neutral", 4))
            self.assertEqual(k["types"], "R,D,X,S,E,C,M")
            self.assertTrue(k["fair"] and k["search_used"] and k["selfcheck"])
        self.assertEqual((s1["budget_usd"], s2["budget_usd"], pf["budget_usd"]), (55.0, 45.0, 3.0))
        self.assertEqual((pf["model"], pf["seeds"]), ("haiku", 1))
        self.assertIn("Af", runner.ALL_ARMS)
        self.assertNotIn("Af", runner.LH_ARMS)      # plain Claude Code: no hooks, no hooks copy

    def test_seed_file(self):
        seeds = [int(x) for x in open(os.path.join(HERE, "seeds-fair.txt")).read().split()]
        self.assertEqual(seeds, [3001, 3002])
        self.assertEqual(proof.check_seeds(seeds, HERE, want=2, skip=("seeds-fair.txt",), skip_kinds=runner.FAIR_KINDS), [])

    def test_step1_and_step2_may_share_seeds(self):
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "results", "r1"))
        json.dump({"seeds": [3001, 3002], "kind": "fair-step1"}, open(os.path.join(d, "results", "r1", "config.json"), "w"))
        self.assertEqual(proof.check_seeds([3001, 3002], d, want=2, skip_kinds=runner.FAIR_KINDS), [])
        self.assertEqual(len(proof.check_seeds([3001, 3002], d, want=2)), 2)      # any other kind of run makes them used

    def test_thresholds_block(self):
        f = TH["fair"]
        self.assertEqual((f["min_gain_points"], f["min_seeds_b_ge_af"], f["seeds"], f["n"]), (15, 2, 2, 10))
        self.assertEqual((f["oracle_min_accuracy"], f["amnesia_max_accuracy"], f["parse_rate_min"], f["compactions_per_main_session"], f["stop_af_min_rate"]), (0.95, 0.02, 0.98, 10, 0.9))
        self.assertEqual((f["primary_phrasing"], f["secondary_phrasing"]), ("cued", "neutral"))
        self.assertNotIn("bootstrap", " ".join(k for k in f))      # no bootstrap interval with 2 seeds
        self.assertIn("proof", TH)      # the other blocks are still there

    def test_run_sh(self):
        sh = open(os.path.join(HERE, "run.sh")).read()
        head = sh.split("case")[1].split(")")[0]
        for k in ("fair-preflight", "fair-step1", "fair-step2"):
            self.assertIn(k, head)
        self.assertIn("fair.py", sh)
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(HERE, "run.sh")]).returncode, 0)

    def dry(self, kind):
        p = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), "run", kind, "--dry-run"], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        return p.stdout

    def test_dry_run_step1(self):
        out = self.dry("fair-step1")
        self.assertIn("arms Af,O,Z", out)
        self.assertIn("budget_usd: 55.0", out)
        total = float(re.search(r"total about \$(\d+)", out).group(1))
        self.assertLessEqual(total, 55)
        self.assertIn("ordering", out)

    def test_dry_run_step2(self):
        out = self.dry("fair-step2")
        self.assertIn("budget_usd: 45.0", out)
        self.assertLessEqual(float(re.search(r"total about \$(\d+)", out).group(1)), 45)

    def test_dry_run_preflight(self):
        out = self.dry("fair-preflight")
        self.assertLessEqual(float(re.search(r"total about \$([\d.]+)", out).group(1)), 3.0)

    def test_step2_needs_compare(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        with self.assertRaises(SystemExit) as cm:
            runner.build_cfg("fair-step2", ap.parse_args([]))
        self.assertIn("--compare", str(cm.exception))

    def test_cfg_flags(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        cfg = runner.build_cfg("fair-step1", ap.parse_args(["--dry-run"]))
        self.assertTrue(cfg["fair"])
        self.assertEqual((cfg["budget_usd"], cfg["n"], cfg["arms"]), (55.0, [10], ["Af", "O", "Z"]))


class Ordering(unittest.TestCase):
    def test_af_units_come_first(self):
        cfg = {"fair": True}
        units = [(s, a, "compact") for s in SEEDS for a in ("Af", "O", "Z")]
        ph = runner.unit_phases(cfg, units)
        self.assertEqual(len(ph), 2)
        self.assertTrue(all(u[1] == "Af" for u in ph[0]) and len(ph[0]) == 2)
        self.assertTrue(all(u[1] != "Af" for u in ph[1]) and len(ph[1]) == 4)

    def test_other_runs_stay_one_group(self):
        units = [(1, "A", "compact"), (1, "B", "compact")]
        self.assertEqual(runner.unit_phases({}, units), [units])
        self.assertEqual(runner.unit_phases({"fair": True}, units), [units])      # step 2: no Af in the run

    def test_execute_checks_af_before_other_arms(self):
        import inspect
        src = inspect.getsource(runner.execute)
        self.assertIn("unit_phases", src)
        self.assertIn("the other arms do not start", src)


class DenyRules(unittest.TestCase):
    RULES = runner.deny_rules(BASE, "Af", seeds=SEEDS)
    WORK_OWN = os.path.join(BASE, "lhbench-%s-s3001-Af" % RUN)

    def test_af_may_read_its_own_transcript(self):
        own = os.path.join(proj_dir("Af"), "abc.jsonl")
        for tool in ("Read", "Grep", "Glob"):
            self.assertFalse(read_denied(tool, own, self.RULES), tool)
        self.assertFalse(read_denied("Read", self.WORK_OWN + "/notes.txt", self.RULES))      # its own work folder
        for cmd in ("grep -r SECRET %s" % proj_dir("Af"), "cat %s/abc.jsonl" % proj_dir("Af"), "grep -B2 -A 5 token %s/abc.jsonl" % proj_dir("Af"),
                    "ls ~/.claude/projects/", "grep -l needle %s/*.jsonl | head" % proj_dir("Af"), "cd %s && ls" % self.WORK_OWN, "cat %s/f1.txt" % self.WORK_OWN):
            self.assertFalse(runner.bash_denied(cmd, self.RULES), cmd)

    def test_arm_a_still_cannot(self):
        a = runner.deny_rules(BASE, "A")
        self.assertTrue(read_denied("Read", os.path.join(proj_dir("A"), "abc.jsonl"), a))
        self.assertTrue(runner.bash_denied("cat %s/abc.jsonl" % proj_dir("Af"), a))      # the trap: Bash(*lhbench*) would also deny the own folder of Af

    def test_af_may_not_read_another_arm(self):
        for arm in ("A", "B", "O", "Z", "R", "K", "G", "P"):
            for suffix in ("", "-nc"):
                p = os.path.join(proj_dir(arm, suffix=suffix), "abc.jsonl")
                for tool in ("Read", "Grep", "Glob"):
                    self.assertTrue(read_denied(tool, p, self.RULES), (tool, arm, suffix))
                self.assertTrue(runner.bash_denied("cat %s" % p, self.RULES), (arm, suffix))
                self.assertTrue(runner.bash_denied("grep -r x %s" % os.path.dirname(p), self.RULES), (arm, suffix))
                work = os.path.join(BASE, "lhbench-%s-s3001-%s%s" % (RUN, arm, suffix))
                self.assertTrue(read_denied("Read", work + "/facts.log", self.RULES), (arm, suffix))
                self.assertTrue(runner.bash_denied("cat %s/facts.log" % work, self.RULES), (arm, suffix))

    def test_af_may_not_read_a_state_path(self):
        st = os.path.join(BASE, "lhbench-%s-s3001-B-state" % RUN)
        for tool in ("Read", "Grep", "Glob"):
            self.assertTrue(read_denied(tool, st + "/receipts.jsonl", self.RULES), tool)
        self.assertTrue(runner.bash_denied("cat %s/receipts.jsonl" % st, self.RULES))
        self.assertTrue(runner.bash_denied("cd %s && cat health.jsonl" % st, self.RULES))
        self.assertTrue(runner.bash_denied("python3 %s/hooks/recall.py search x" % st, self.RULES))
        self.assertTrue(runner.bash_denied("cat %s/recall.py" % os.path.join(BASE, "lhtools-%s-s3001-B/hooks" % RUN), self.RULES))

    def test_other_seed_of_another_arm_is_denied_too(self):
        self.assertTrue(runner.bash_denied("cat %s/abc.jsonl" % proj_dir("O", seed=3002), self.RULES))

    def test_shared_rules_unchanged(self):
        self.assertEqual(runner.deny_rules(BASE, "Af", shared=True), runner.deny_rules(BASE, "A", shared=True))

    def test_old_arms_unchanged(self):
        # arm B and arm A keep the transcript denies: no Af change leaks into them
        for arm in ("A", "B", "Z", "O"):
            r = runner.deny_rules(BASE, arm)
            self.assertIn("Read(~/.claude/projects/**)", r)
            self.assertIn("Bash(*.claude/projects*)", r)

    def test_af_rules_have_no_broad_transcript_rule(self):
        for r in self.RULES:
            self.assertNotIn("~/.claude/projects/**", r)
            self.assertNotIn("Bash(*lhbench*)", r)
            self.assertNotIn("projects*)", r)


class Scan(unittest.TestCase):
    OWN = "lhbench-%s-s3001-Af" % RUN

    def rec(self, marker=None, calls=()):
        recs = []
        if marker:
            recs.append({"type": "user", "message": {"role": "user", "content": "probe %s" % marker}})
        for i, (name, inp, result, err) in enumerate(calls):
            recs.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": name, "input": inp}]}})
            recs.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": result, "is_error": err}]}})
        return recs

    def test_own_read_ok(self):
        r = fair.scan_reads(self.rec("PROBE-1", [("Grep", {"pattern": "x", "path": proj_dir("Af")}, "match", False)]), "PROBE-1")
        self.assertEqual((r["own_ok"], r["own_denied"], r["other"]), (1, 0, []))

    def test_own_read_denied(self):
        r = fair.scan_reads(self.rec("PROBE-1", [("Read", {"file_path": proj_dir("Af") + "/a.jsonl"}, "Permission to use Read has been denied.", True)]), "PROBE-1")
        self.assertEqual((r["own_ok"], r["own_denied"]), (0, 1))

    def test_other_arm_and_state_paths(self):
        calls = [("Bash", {"command": "cat %s/a.jsonl" % proj_dir("B")}, "denied", True),
                 ("Bash", {"command": "ls /tmp/lhbench-%s-s3001-A-state" % RUN}, "x", False),
                 ("Read", {"file_path": proj_dir("A", suffix="-nc") + "/a.jsonl"}, "x", False),
                 ("Bash", {"command": "grep -A 5 x %s/a.jsonl" % proj_dir("Af")}, "x", False)]
        r = fair.scan_reads(self.rec("PROBE-1", calls), "PROBE-1")
        self.assertEqual(len(r["other"]), 2)                    # the two calls that succeeded: the gate
        self.assertEqual(r["af_blocked_attempts"], 1)           # the denied call: counted, not a gate
        self.assertEqual(r["own_ok"], 1)       # the "-Af" folder is never taken for arm A

    def test_blocked_by_deny_text_without_error_flag(self):
        r = fair.scan_reads(self.rec("PROBE-1", [("Read", {"file_path": proj_dir("O") + "/a.jsonl"}, "Permission to use Read has been denied.", False)]), "PROBE-1")
        self.assertEqual((r["other"], r["af_blocked_attempts"]), ([], 1))

    def test_only_records_after_the_marker_count(self):
        recs = self.rec(None, [("Bash", {"command": "cat %s/a.jsonl" % proj_dir("B")}, "x", False)]) + self.rec("PROBE-9", [])
        self.assertEqual(fair.scan_reads(recs, "PROBE-9")["other"], [])
        self.assertEqual(len(fair.scan_reads(recs)["other"]), 1)       # no marker: the whole transcript (main session)


# ---- fixture rows

def row(seed, arm, typ, item, correct, phrasing="cued", n=10, **kw):
    r = {"run": "r1", "seed": seed, "arm": arm, "type": typ, "item": item, "n_target": n, "n_actual": n, "correct": correct, "parse_ok": True, "phrasing": phrasing,
         "condition": "compact", "refused": False, "used_lookup": arm in ("Af", "B"), "fork_sid": "f-%s-%d-%s-%s-%d" % (arm, seed, phrasing, typ, item)}
    if arm == "Af":
        r["af_reads"] = {"own_ok": 1, "own_denied": 0, "other": [], "calls": 3}
    r.update(kw)
    return r


def make(af=0.5, b=0.9, o=1.0, z=0.0, af_seed=None, b_seed=None, with_b=True, with_af=True, **kw):
    """Rows for 2 seeds x 7 types x 10 items x 2 phrasings. af_seed and b_seed: {seed: accuracy} that override af and b."""
    rows = []
    for s in SEEDS:
        for ph in ("cued", "neutral"):
            for t in proof.TYPES:
                for i in range(1, ITEMS + 1):
                    for arm, acc in (("Af", (af_seed or {}).get(s, af)), ("B", (b_seed or {}).get(s, b)), ("O", o), ("Z", z)):
                        if (arm == "B" and not with_b) or (arm == "Af" and not with_af):
                            continue
                        n = 10 if arm in ("Af", "B") else 0
                        rows.append(row(s, arm, t, i, i <= round(acc * ITEMS), ph, n, **kw))
    return rows


def sessions(boundaries=10, with_b=True):
    arms = ["Af", "B"] if with_b else ["Af"]
    return [{"name": "s%d-%s" % (s, a), "arm": a, "seed": s, "condition": "compact", "boundaries": boundaries, "hook_receipts": 3 if a == "B" else None,
             "af_reads": {"own_ok": 4, "own_denied": 0, "other": [], "calls": 40} if a == "Af" else None} for s in SEEDS for a in arms]


CFG = {"model": "sonnet", "n": [10]}


class Verdict(unittest.TestCase):
    def v(self, rows, sess=None):
        return fair.verdict(rows, sess if sess is not None else sessions(), TH)

    def test_pass(self):
        v = self.v(make(af=0.5, b=0.9))
        self.assertEqual(v["verdict"], "PASS", v["gates"])
        self.assertEqual(v["primary"]["af"], (70, 140))
        self.assertEqual(v["primary"]["b"], (126, 140))
        self.assertAlmostEqual(v["primary"]["gain_points"], 40, delta=0.1)
        self.assertEqual(v["primary"]["seeds_b_ge_af"], 2)
        self.assertEqual(v["secondary"]["gain_points"], v["primary"]["gain_points"])

    def test_fail_when_the_gain_is_under_15_points(self):
        v = self.v(make(af=0.8, b=0.9))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertAlmostEqual(v["primary"]["gain_points"], 10, delta=0.1)

    def boundary_rows(self, shift):
        """Af 84 of 140 (60%). B 105 of 140 (75%): exactly 15 points. shift -1 gives 104 (14.3 points)."""
        rows = [r for r in make(af=0.6, b=0.0) if r["arm"] != "B"]
        for s in SEEDS:
            for ti, t in enumerate(proof.TYPES):
                k = 8 if (ti + (s == 3002)) % 2 == 0 else 7
                if s == 3002 and ti == 1:
                    k += shift
                rows += [row(s, "B", t, i, i <= k, ph) for ph in ("cued", "neutral") for i in range(1, 11)]
        return rows

    def test_boundary_exactly_15_points_passes(self):
        v = self.v(self.boundary_rows(0))
        self.assertEqual((v["primary"]["af"], v["primary"]["b"]), ((84, 140), (105, 140)))
        self.assertEqual(v["primary"]["gain_points"], 15.0)
        self.assertEqual(v["verdict"], "PASS")

    def test_just_under_15_points_fails(self):
        v = self.v(self.boundary_rows(-1))
        self.assertEqual(v["primary"]["b"], (104, 140))
        self.assertEqual(v["verdict"], "FAIL")

    def test_fail_when_b_is_behind_af_in_one_seed(self):
        v = self.v(make(af=0.4, b=0.9, b_seed={3002: 0.3}))      # pooled gain is 25 points, but B < Af in seed 3002
        self.assertEqual(v["primary"]["seeds_b_ge_af"], 1)
        self.assertGreaterEqual(v["primary"]["gain_points"], 15)
        self.assertEqual(v["verdict"], "FAIL")

    def test_secondary_rule_is_reported_on_neutral_probes(self):
        rows = [r for r in make(af=0.4, b=0.9) if r["phrasing"] == "cued"] + [r for r in make(af=0.8, b=0.85) if r["phrasing"] == "neutral"]
        v = self.v(rows)
        self.assertEqual(v["verdict"], "PASS")                  # the primary rule decides
        self.assertTrue(v["primary"]["ok"])
        self.assertFalse(v["secondary"]["ok"])

    def test_types_apart(self):
        v = self.v(make())
        self.assertEqual(set(v["types"]), {"S and E", "R D X C M"})
        self.assertEqual(v["types"]["S and E"]["cued"]["Af"][1], 40)      # 2 types x 10 items x 2 seeds
        self.assertEqual(v["types"]["R D X C M"]["cued"]["B"][1], 100)

    def test_step1_alone_is_incomplete(self):
        v = self.v(make(with_b=False), sessions(with_b=False))
        self.assertEqual(v["verdict"], "INCOMPLETE")
        self.assertEqual(v["primary"]["af"][1], 140)


class Gates(unittest.TestCase):
    def v(self, rows, sess=None):
        return fair.verdict(rows, sess if sess is not None else sessions(), TH)

    def invalid(self, v, name):
        self.assertEqual(v["verdict"], "INVALID", v["gates"])
        self.assertTrue(any(name in g for g in v["invalid_gates"]), (name, v["invalid_gates"]))

    def test_oracle_too_low(self):
        self.invalid(self.v(make(o=0.9)), "Oracle")

    def test_amnesia_too_high(self):
        self.invalid(self.v(make(z=0.1)), "Amnesia")

    def test_parse_rate_too_low(self):
        rows = make()
        for r in rows[:20]:
            if r["arm"] == "Af":
                r["parse_ok"] = False
        rows = [dict(r, parse_ok=False) if (r["arm"] == "Af" and r["seed"] == 3001 and r["item"] <= 2) else r for r in rows]      # 28 of 280 Af rows
        self.invalid(self.v(rows), "Parse rate, arm Af")

    def test_not_ten_compactions(self):
        sess = sessions()
        sess[0] = dict(sess[0], boundaries=9)
        self.invalid(self.v(make(), sess), "real compactions")

    def test_denied_read_of_the_own_transcript(self):
        rows = make()
        rows[0] = dict(rows[0], arm="Af", af_reads={"own_ok": 0, "own_denied": 1, "other": [], "calls": 2}, fork_sid="deny-1")
        self.invalid(self.v(rows), "Denied reads")

    def test_denied_read_in_the_main_session(self):
        sess = sessions()
        sess[0] = dict(sess[0], af_reads={"own_ok": 0, "own_denied": 2, "other": [], "calls": 5})
        self.invalid(self.v(make(), sess), "Denied reads")

    def test_read_of_another_arm_folder(self):
        rows = make()
        rows[0] = dict(rows[0], arm="Af", af_reads={"own_ok": 1, "own_denied": 0, "other": ["Bash cat /x/lhbench-r-s3001-B/a.jsonl"], "calls": 2}, fork_sid="other-1")
        self.invalid(self.v(rows), "another arm")

    def test_read_of_a_state_folder_in_the_main_session(self):
        sess = sessions()
        sess[0] = dict(sess[0], af_reads={"own_ok": 1, "own_denied": 0, "other": ["Read /x/lhbench-r-s3001-B-state/receipts.jsonl"], "calls": 5})
        self.invalid(self.v(make(), sess), "another arm")

    def test_blocked_attempts_are_counted_but_not_a_gate(self):
        rows = make()
        rows[0] = dict(rows[0], arm="Af", af_reads={"own_ok": 1, "own_denied": 0, "other": [], "af_blocked_attempts": 2, "calls": 2}, fork_sid="blocked-1")
        sess = sessions()
        sess[0] = dict(sess[0], af_reads={"own_ok": 1, "own_denied": 0, "other": [], "af_blocked_attempts": 3, "calls": 5})
        v = self.v(rows, sess)
        self.assertEqual(v["verdict"], "PASS", v["gates"])
        self.assertEqual(fair.blocked_total(rows, sess), 5)
        md, _v = fair.section(rows, sess, CFG, TH)
        self.assertIn("af_blocked_attempts", "\n".join(md))
        self.assertIn("not a gate): 5.", "\n".join(md))

    def test_a_missing_control_is_invalid_not_a_pass(self):
        rows = [r for r in make() if r["arm"] != "O"]
        self.assertEqual(self.v(rows)["verdict"], "INVALID")

    def test_all_gates_pass_on_clean_data(self):
        v = self.v(make())
        self.assertEqual(v["invalid_gates"], [])
        self.assertTrue(all(g["result"] == "PASS" for g in v["gates"]))


class Stop(unittest.TestCase):
    def test_stop_at_90_percent(self):
        stop, x, y = fair.stop_rule(make(af=0.9), TH)
        self.assertEqual((stop, x, y), (True, 126, 140))
        self.assertEqual(fair.STOP_FMT % (x, y), "STOP: plain Claude Code with search scores 126/140; step 2 not needed")

    def test_go_on_below_90_percent(self):
        stop, x, y = fair.stop_rule(make(af=0.8), TH)
        self.assertFalse(stop)
        self.assertEqual((x, y), (112, 140))

    def test_stop_uses_hinted_probes_at_n10_only(self):
        rows = [dict(r, correct=True) for r in make(af=0.1) if r["phrasing"] == "neutral"] + [r for r in make(af=0.1) if r["phrasing"] == "cued"]
        self.assertFalse(fair.stop_rule(rows, TH)[0])

    def test_summary_of_step1_has_the_stop_line(self):
        md, v = fair.section(make(af=0.95, with_b=False), sessions(with_b=False), CFG, TH)
        text = "\n".join(md)
        self.assertIn("STOP: plain Claude Code with search scores 140/140; step 2 not needed", text)
        self.assertIn("INCOMPLETE", text)


class Wording(unittest.TestCase):
    def text(self, rows):
        md, _v = fair.section(rows, sessions(), CFG, TH)
        return "\n".join(md)

    def test_counts_sentence(self):
        t = self.text(make(af=0.5, b=0.9))
        self.assertIn("Small test: 2 seeds, 140 hinted questions, Sonnet, 10 compactions. Plain Claude Code that can search its own transcripts: 70 of 140. "
                      "Lossless Handoff: 126 of 140. Two seeds show a direction, not a proof.", t)
        self.assertNotIn("no clear difference", t)

    def test_never_says_validated(self):
        for rows in (make(af=0.5, b=0.9), make(af=0.8, b=0.85), make(o=0.5), make(with_b=False)):
            self.assertNotIn("validated", self.text(rows).lower())

    def test_no_clear_difference_when_gap_is_small(self):
        self.assertIn("no clear difference", self.text(make(af=0.8, b=0.9)))

    def test_no_clear_difference_when_mixed_across_seeds(self):
        self.assertIn("no clear difference", self.text(make(af=0.4, b=0.9, b_seed={3002: 0.3})))

    def test_no_bootstrap_interval(self):
        t = self.text(make())
        self.assertNotIn("confidence interval", t.lower())
        self.assertIn("No bootstrap interval", t)

    def test_isolation_method_is_named(self):
        self.assertIn("Isolation method: ordering", self.text(make()))


class SelfcheckFair(unittest.TestCase):
    OWN, OTHER = "/p/-lhbench-r-s1-Af", "/p/-lhbench-r-s1-O"

    def test_own_read_succeeds_and_other_is_denied(self):
        res = "1) OWNTOK-1 2) OWNTOK-1 3) OWNTOK-1 4) DENIED 5) DENIED"
        den = [{"tool_name": "Read", "tool_input": {"file_path": self.OTHER + "/decoy.jsonl"}}]
        self.assertTrue(runner.fair_selfcheck_verdict(res, den, "OWNTOK-1", "OTHERARMTOK-2", self.OWN, self.OTHER))

    def test_own_read_denied_fails(self):
        den = [{"tool_name": "Read", "tool_input": {"file_path": self.OWN + "/decoy.jsonl"}}, {"tool_name": "Read", "tool_input": {"file_path": self.OTHER + "/decoy.jsonl"}}]
        self.assertFalse(runner.fair_selfcheck_verdict("DENIED OWNTOK-1", den, "OWNTOK-1", "OTHERARMTOK-2", self.OWN, self.OTHER))

    def test_own_token_missing_fails(self):
        den = [{"tool_name": "Read", "tool_input": {"file_path": self.OTHER + "/decoy.jsonl"}}]
        self.assertFalse(runner.fair_selfcheck_verdict("nothing", den, "OWNTOK-1", "OTHERARMTOK-2", self.OWN, self.OTHER))

    def test_other_token_leak_fails(self):
        den = [{"tool_name": "Read", "tool_input": {"file_path": self.OTHER + "/decoy.jsonl"}}]
        self.assertFalse(runner.fair_selfcheck_verdict("OWNTOK-1 OTHERARMTOK-2", den, "OWNTOK-1", "OTHERARMTOK-2", self.OWN, self.OTHER))

    def test_no_denial_of_the_other_folder_fails(self):
        self.assertFalse(runner.fair_selfcheck_verdict("OWNTOK-1", [], "OWNTOK-1", "OTHERARMTOK-2", self.OWN, self.OTHER))

    def test_wiring(self):
        import inspect
        self.assertIn("fair_selfcheck(ctx, cfg, say)", inspect.getsource(runner.selfcheck_core))
        self.assertIn("fair=bool(cfg.get(\"fair\"))", inspect.getsource(runner.execute))
        self.assertIn("--fair", inspect.getsource(runner.main))


class Preflight(unittest.TestCase):
    def rows(self, lookup=True, own_ok=1, own_denied=0):
        r = [row(3001, "Af", "R", i, True, af_reads={"own_ok": own_ok, "own_denied": own_denied, "other": [], "calls": 2}, used_lookup=lookup) for i in range(1, 3)]
        return r + [row(3001, "B", "R", i, True, used_lookup=lookup) for i in range(1, 3)]

    def run_(self, rows, sess):
        return {l: s for l, s, _d in fair.preflight_check(rows, sess, {"n": [2]})}

    def sess(self, boundaries=2, receipts=3):
        return [{"name": "s1-Af", "arm": "Af", "condition": "compact", "boundaries": boundaries}, {"name": "s1-B", "arm": "B", "condition": "compact", "boundaries": boundaries, "hook_receipts": receipts}]

    def test_all_pass(self):
        self.assertTrue(all(s == "PASS" for s in self.run_(self.rows(), self.sess()).values()))
        self.assertEqual(len(self.run_(self.rows(), self.sess())), 4)

    def test_each_item_can_fail(self):
        self.assertIn("FAIL", self.run_(self.rows(), self.sess(boundaries=1)).values())
        r = self.run_(self.rows(), self.sess(receipts=0))
        self.assertEqual([s for l, s in r.items() if l.startswith("B hooks")], ["FAIL"])
        r = self.run_(self.rows(own_ok=0), self.sess())
        self.assertEqual([s for l, s in r.items() if "Af read" in l], ["FAIL"])
        r = self.run_(self.rows(own_denied=1), self.sess())
        self.assertEqual([s for l, s in r.items() if "Af read" in l], ["FAIL"])
        rows = self.rows()
        del rows[0]["used_lookup"]
        r = self.run_(rows, self.sess())
        self.assertEqual([s for l, s in r.items() if "used_lookup" in l], ["FAIL"])


class Wiring(unittest.TestCase):
    def test_rows_and_sessions_carry_af_reads(self):
        import inspect
        self.assertIn("af_reads = fair.scan_reads(frecs, marker)", inspect.getsource(runner.Session.probe) if hasattr(runner.Session, "probe") else open(os.path.join(HERE, "runner.py")).read())
        self.assertIn('"af_reads": af_reads', open(os.path.join(HERE, "runner.py")).read())
        self.assertIn('out["af_reads"] = fair.scan_reads(read_jsonl(main))', inspect.getsource(runner.session_summary))

    def test_delete_after_is_kept_for_af(self):
        import gen
        self.assertIn("delete_after", inspect_src(gen))
        self.assertIn('ev.get("delete_after")', inspect_src(runner))      # run_event deletes the S/E fact files for every arm, Af included

    def test_report_has_the_fair_section(self):
        self.assertIn("fair.section", open(os.path.join(HERE, "report.py")).read())


class ReportSmoke(unittest.TestCase):
    """report.build on a fixture run folder in a temp dir: the fair section comes first, step 2 merges the rows of step 1 (--compare)."""

    def make_run(self, root, kind, rows, sess, compare=None):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        cfg = runner.build_cfg(kind, ap.parse_args(["--dry-run"] + (["--compare", compare] if compare else [])))
        cfg["run_id"] = "fx-" + kind
        rd = os.path.join(root, cfg["run_id"])
        os.makedirs(rd)
        json.dump(cfg, open(os.path.join(rd, "config.json"), "w"))
        json.dump({"spent": 1.0, "status": "done", "sessions": {}}, open(os.path.join(rd, "state.json"), "w"))
        open(os.path.join(rd, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
        open(os.path.join(rd, "sessions.jsonl"), "w").write("\n".join(json.dumps(dict({"turns": 50, "cost_usd": 1.0, "missed": [], "status": "done"}, **s)) for s in sess) + "\n")
        open(os.path.join(rd, "calls.jsonl"), "w").write("")
        return cfg["run_id"]

    def test_step1_then_step2(self):
        import tempfile
        import report
        root = tempfile.mkdtemp()
        old = report.RESULTS
        report.RESULTS = root
        try:
            all_rows, all_sess = make(af=0.5, b=0.9), sessions()
            step1 = self.make_run(root, "fair-step1", [r for r in all_rows if r["arm"] != "B"], [s for s in all_sess if s["arm"] != "B"])
            out, text = report.build(step1)
            self.assertIn("## Fair re-test", text)
            self.assertIn("INCOMPLETE", text)
            self.assertLess(text.index("## Fair re-test"), text.index("# Continuity benchmark") + 400)
            json.dumps(out, default=str)
            step2 = self.make_run(root, "fair-step2", [dict(r, run="r2") for r in all_rows if r["arm"] == "B"], [s for s in all_sess if s["arm"] == "B"], compare=step1)
            out2, text2 = report.build(step2)
            self.assertIn("Verdict (primary rule, hinted probes, N=10): PASS", text2)
            self.assertIn("Lossless Handoff: 126 of 140", text2)
            self.assertNotIn("validated", text2.lower())
        finally:
            report.RESULTS = old


def inspect_src(mod):
    import inspect
    return inspect.getsource(mod)


if __name__ == "__main__":
    unittest.main()
