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


class Reprobe(unittest.TestCase):
    """FR.7: the answer fallback of probe(), the reprobe rows in the summary, and the clear error of a missing main session. No model call."""

    def cfg(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        cfg = runner.build_cfg("fair-step1", ap.parse_args(["--dry-run"]))
        cfg["run_id"] = "fx-reprobe"
        return cfg

    def session(self, root):
        rd = os.path.join(root, "run")
        os.makedirs(rd, exist_ok=True)
        ctx = runner.Ctx(rd, self.cfg())
        ss = runner.Session(ctx, "s3002-Af", 3002, "Af", ctx.sess("s3002-Af"))
        ss.st.update({"boundaries": 10, "sid": "main-sid"})
        ss.proj = os.path.join(root, "proj")
        os.makedirs(ss.proj, exist_ok=True)
        return ctx, ss

    def run_probe(self, root, result, msgs):
        """probe() of type E with a stubbed claude call. msgs: assistant texts of the fork transcript. Returns the rows written."""
        ctx, ss = self.session(root)
        tp = os.path.join(root, "fork.jsonl")
        ss.transcript = lambda sid: tp
        marker = "PROBE-3002-Af-10-E"
        recs = [{"type": "user", "message": {"role": "user", "content": "old history, no marker"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": '{"answers": [{"id": "OLD"}]}'}]}},
                {"type": "user", "message": {"role": "user", "content": "question " + marker}}]
        recs += [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}} for t in msgs]
        open(tp, "w").write("\n".join(json.dumps(r) for r in recs) + "\n")
        ss.call = lambda *a, **k: {"result": result, "session_id": "fork1", "usage": {"input_tokens": 1, "output_tokens": 2}, "_delta": 0.5, "permission_denials": []}
        ss.probe("E", 10, fork_of="main-sid", phrasing="cued")
        self.assertFalse(os.path.exists(tp), "the fork transcript is deleted after grading, as before")
        return runner.read_jsonl(os.path.join(ctx.dir, "results.jsonl"))

    def answer_for(self, root, ok=True):
        import gen
        _c, ss = self.session(root)
        items = gen.select(ss.script, "E", runner.need_epoch(ss.ctx.cfg, "Af", 10))
        return json.dumps({"answers": [{"id": it["id"], "proceed": False, "reason_code": it["ecode"] if ok else "X"} for it in items]}), len(items)

    def test_fallback_takes_the_last_parsable_assistant_message(self):
        import tempfile
        root = tempfile.mkdtemp()
        good, n = self.answer_for(root)
        late = "The background grep I started earlier has finished. I already have the answers."
        rows = self.run_probe(root, late, ['{"answers": [{"id": "x"}] first, broken', good, "thinking again", late])
        self.assertEqual(len(rows), n)
        self.assertTrue(all(r["raw_source"] == "transcript" and r["parse_ok"] and r["correct"] for r in rows))
        self.assertEqual(rows[0]["raw"], good)
        self.assertEqual(rows[0]["raw_result"], late)
        self.assertNotIn("reprobe", rows[0])

    def test_two_parsable_messages_take_the_last(self):
        import tempfile
        root = tempfile.mkdtemp()
        good, _n = self.answer_for(root)
        bad, _n = self.answer_for(root, ok=False)
        rows = self.run_probe(root, "done", [bad, good, "ok done"])
        self.assertTrue(all(r["raw_source"] == "transcript" and r["correct"] for r in rows))

    def test_no_parsable_message_keeps_the_result(self):
        import tempfile
        rows = self.run_probe(tempfile.mkdtemp(), "no json here", ["still no json"])
        self.assertTrue(all(r["raw_source"] == "result" and not r["parse_ok"] and r["raw"] == "no json here" and "raw_result" not in r for r in rows))

    def test_a_row_that_parses_today_is_unchanged(self):
        import tempfile
        root = tempfile.mkdtemp()
        bad, _n = self.answer_for(root, ok=False)
        better, _n = self.answer_for(root, ok=True)
        rows = self.run_probe(root, bad, [better, "late turn"])
        self.assertTrue(all(r["raw_source"] == "result" and r["raw"] == bad and r["parse_ok"] and not r["correct"] for r in rows))

    def make_run(self, root, rows, replaced=()):
        rd = os.path.join(root, "fx-reprobe")
        os.makedirs(rd)
        json.dump(self.cfg(), open(os.path.join(rd, "config.json"), "w"))
        json.dump({"spent": 1.0, "status": "done", "sessions": {"s3002-Af": {"status": "done", "sid": "main-sid"}}}, open(os.path.join(rd, "state.json"), "w"))
        open(os.path.join(rd, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
        open(os.path.join(rd, "sessions.jsonl"), "w").write("")
        open(os.path.join(rd, "calls.jsonl"), "w").write("")
        if replaced:
            open(os.path.join(rd, "reprobed.jsonl"), "w").write("\n".join(json.dumps(r) for r in replaced) + "\n")
        return rd

    def test_summary_uses_reprobe_rows_and_prints_the_line(self):
        import tempfile
        import gates
        import report
        root = tempfile.mkdtemp()
        rows = make(af=0.5, b=0.9, with_b=False)
        old = [r for r in rows if r["arm"] == "Af" and r["seed"] == 3002 and r["type"] == "E" and r["phrasing"] == "cued"][:4]
        for r in old:
            r.update(parse_ok=False, correct=False, raw="late turn")
        new = [dict(r, parse_ok=True, correct=True, reprobe=True, raw_source="transcript", fork_sid="new-%d" % i) for i, r in enumerate(old)]
        marks = [{"seed": r["seed"], "arm": r["arm"], "condition": "compact", "phrasing": "cued", "type": "E", "n_target": 10, "item": r["item"], "fork_sid": r["fork_sid"]} for r in old]
        rd = self.make_run(root, rows + new, marks)
        eff = gates.load_rows(rd)
        self.assertEqual(len(eff), len(rows))
        self.assertEqual(len([r for r in eff if r.get("reprobe")]), 4)
        self.assertEqual(len(open(os.path.join(rd, "results.jsonl")).read().splitlines()), len(rows) + 4, "no old row is deleted")
        self.assertEqual(gates.reprobe_line(rd), "reprobed: 4 probe rows (harness parse failure); old result 0/4, new result 4/4")
        old_root = report.RESULTS
        report.RESULTS = root
        try:
            _out, text = report.build("fx-reprobe")
        finally:
            report.RESULTS = old_root
        self.assertIn("reprobed: 4 probe rows (harness parse failure); old result 0/4, new result 4/4", text)
        rd2 = os.path.join(tempfile.mkdtemp(), "fx")      # no reprobe: no line, rows unchanged
        os.makedirs(rd2)
        open(os.path.join(rd2, "results.jsonl"), "w").write(json.dumps(rows[0]) + "\n")
        self.assertIsNone(gates.reprobe_line(rd2))
        self.assertEqual(gates.load_rows(rd2), [rows[0]])

    def test_missing_main_session_is_a_clear_error(self):
        import argparse, contextlib, io, tempfile
        root = tempfile.mkdtemp()
        rows = [row(3002, "Af", "E", i, False, "cued", 10, parse_ok=False) for i in range(1, 5)]
        rd = self.make_run(root, rows)
        old = runner.RESULTS
        runner.RESULTS = root
        try:
            a = argparse.Namespace(run_id="fx-reprobe", seed=3002, arm="Af", type="E", phrasing="cued", n=None, dry_run=True)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = runner.cmd_reprobe(a)
            self.assertEqual(rc, 1)
            self.assertIn("main session transcript is gone", err.getvalue())
            self.assertIn("Nothing was run", err.getvalue())
            err2 = io.StringIO()
            with contextlib.redirect_stderr(err2):
                self.assertEqual(runner.cmd_reprobe(argparse.Namespace(run_id="fx-reprobe", seed=3002, arm="Z", type="E", phrasing="cued", n=None, dry_run=True)), 1)
            self.assertIn("arm Z", err2.getvalue())
            self.assertFalse(os.path.exists(os.path.join(rd, "reprobed.jsonl")))
        finally:
            runner.RESULTS = old


# ---- FR.8 (SPEC.md): the 6-seed replication. No model call, no cost.

SEEDS6 = [4001, 4002, 4003, 4004, 4005, 4006]
PER_TYPE = {"R": 4, "D": 4, "X": 4, "S": 4, "E": 4, "C": 4, "M": 1}      # 25 items per seed and phrasing (the real item counts of gen.build)
HOOKS = {"precompact-handoff.py": "aa11", "recall.py": "bb22"}


def row6(seed, arm, typ, item, correct, phrasing="cued", **kw):
    r = row(seed, arm, typ, item, correct, phrasing, 10 if arm in ("Af", "B") else 0, claude_version="2.1.0", **kw)
    r["n_target"] = 10
    return r


def make6(af=(15,) * 6, b=(22,) * 6, neutral=None, told=None, with_b=True, o=1.0, z=0.0, lookup=None):
    """Rows for 6 seeds x 3 phrasings x 25 items. af, b: correct items of 25 for each seed (cued). neutral and told: (af, b) tuples that replace the cued numbers.
    lookup: {phrasing: used_lookup of Af}."""
    per_ph = {"cued": (af, b), "neutral": neutral or (af, b), "told": told or (af, b)}
    rows = []
    for si, s in enumerate(SEEDS6):
        for ph, (a_list, b_list) in per_ph.items():
            for arm, k in (("Af", a_list[si]), ("B", b_list[si]), ("O", round(o * 25)), ("Z", round(z * 25))):
                if arm == "B" and not with_b:
                    continue
                n = 0
                for t, cnt in PER_TYPE.items():
                    for i in range(1, cnt + 1):
                        n += 1
                        extra = {"used_lookup": (lookup or {}).get(ph, True)} if arm == "Af" else {}
                        rows.append(row6(s, arm, t, i, n <= k, ph, **extra))
    return rows


def sessions6(with_b=True, version="2.1.0", hooks=None):
    out = []
    for s in SEEDS6:
        out.append({"name": "s%d-Af" % s, "arm": "Af", "seed": s, "condition": "compact", "boundaries": 10, "claude_version": version, "hook_sha256": None,
                    "af_reads": {"own_ok": 4, "own_denied": 0, "other": [], "calls": 40}})
        if with_b:
            out.append({"name": "s%d-B" % s, "arm": "B", "seed": s, "condition": "compact", "boundaries": 10, "claude_version": version, "hook_sha256": dict(hooks or HOOKS), "hook_receipts": 3})
    return out


META = [{"live_recall_sha256": "cc33", "settings_hash_changed": False}, {"live_recall_sha256": "cc33", "settings_hash_changed": False}]


class Fair6Seeds(unittest.TestCase):
    def test_seed_file_and_check(self):
        seeds = [int(x) for x in open(os.path.join(HERE, "seeds-fair6.txt")).read().split()]
        self.assertEqual(seeds, SEEDS6)
        self.assertEqual(proof.check_seeds(seeds, HERE, want=6, skip=("seeds-fair6.txt",), skip_kinds=runner.FAIR_KINDS), [])
        self.assertEqual(fair.seeds6(TH), SEEDS6)

    def test_seeds_are_new_and_used_seeds_are_caught(self):
        import tempfile
        used = proof.used_seeds(HERE, skip=("seeds-fair6.txt",), skip_kinds=runner.FAIR_KINDS)
        self.assertFalse([s for s in SEEDS6 if s in used], used)      # no other seeds file and no run folder holds them
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "results", "r1"))
        json.dump({"seeds": SEEDS6, "kind": "fair6-step1"}, open(os.path.join(d, "results", "r1", "config.json"), "w"))
        self.assertEqual(proof.check_seeds(SEEDS6, d, want=6, skip_kinds=runner.FAIR_KINDS), [])      # step 1 and step 2 share them
        self.assertEqual(len(proof.check_seeds(SEEDS6, d, want=6)), 6)
        json.dump({"seeds": [4003], "kind": "full"}, open(os.path.join(d, "results", "r1", "config.json"), "w"))
        self.assertEqual(len(proof.check_seeds(SEEDS6, d, want=6, skip_kinds=runner.FAIR_KINDS)), 1)      # a run of another kind makes a seed used
        self.assertTrue(proof.check_seeds(SEEDS6[:5], d, want=6))      # the wrong count

    def test_build_cfg_checks_the_seeds(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        cfg = runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run"]))
        self.assertEqual(cfg["seeds"], SEEDS6)
        self.assertEqual(runner.build_cfg("fair6-step2", ap.parse_args(["--dry-run"]))["seeds"], SEEDS6)
        self.assertEqual(runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run", "--seeds", "3"]))["seeds"], SEEDS6[:3])
        orig = runner.proof.check_seeds
        try:
            runner.proof.check_seeds = lambda *a, **k: ["seed 4001 was used before (x)"]      # a used seed stops the preset, as for the fair presets
            with self.assertRaises(SystemExit) as cm:
                runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run"]))
            self.assertIn("seeds must be new", str(cm.exception))
        finally:
            runner.proof.check_seeds = orig


class Fair6Presets(unittest.TestCase):
    def test_kinds(self):
        s1, s2 = runner.KINDS["fair6-step1"], runner.KINDS["fair6-step2"]
        o1, o2 = runner.KINDS["fair-step1"], runner.KINDS["fair-step2"]
        self.assertEqual((s1["arms"], s2["arms"]), ("Af,O,Z", "B"))
        for k in (s1, s2):
            self.assertEqual((k["model"], k["window"], k["n"], k["seeds"], k["seed_file"], k["conditions"], k["phrasings"], k["probe_design"], k["jobs"]),
                             ("sonnet", 400000, [10], 6, "seeds-fair6.txt", "compact", "cued,neutral,told", 4, 3))
            self.assertEqual(k["types"], "R,D,X,S,E,C,M")
            self.assertTrue(k["fair"] and k["search_used"] and k["selfcheck"])
        self.assertEqual((s1["budget_usd"], s2["budget_usd"]), (130.0, 110.0))
        self.assertEqual((s1["cost_basis"], s2["cost_basis"]), ({"Af": 18.20, "O": 0.83, "Z": 0.19}, {"B": 15.95}))
        for old in (o1, o2):      # the old presets do not change
            self.assertEqual(old["phrasings"], "cued,neutral")
            self.assertEqual(old["seeds"], 2)
            self.assertNotIn("jobs", old)
        self.assertEqual(runner.KINDS["fair-preflight"]["phrasings"], "cued,neutral,told")      # FR.8.2: the preflight runs told too
        for k in ("fair6-step1", "fair6-step2"):
            self.assertIn(k, runner.FAIR_KINDS)

    def test_thresholds_block_is_the_registered_text(self):
        spec = open(os.path.join(HERE, "SPEC.md"), encoding="utf-8").read()
        block = spec.split("## FR.8 ")[1].split("```json\n")[1].split("\n```")[0]
        self.assertEqual(json.loads("{" + block + "}")["fair6"], TH["fair6"])
        self.assertEqual(TH["version"], 8)
        self.assertIn("v8 adds the fair6 block, committed before any fair6 data", TH["note"])
        self.assertEqual((TH["fair"]["min_seeds_b_ge_af"], TH["fair"]["seeds"], TH["fair"]["registered"]), (2, 2, "2026-11-26"))      # the fair block does not change

    def test_run_sh(self):
        sh = open(os.path.join(HERE, "run.sh")).read()
        head = sh.split("case")[1].split(")")[0]
        for k in ("fair6-step1", "fair6-step2"):
            self.assertIn(k, head)
            self.assertIn("run.sh " + k, sh)      # usage text
        self.assertIn('"$cmd" = fair6-step1', sh)      # the stop check runs after step 1
        self.assertNotIn('"$cmd" = fair6-step2 ]; } && [ "$dry"', sh)      # and never after step 2
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(HERE, "run.sh")]).returncode, 0)

    def dry(self, kind, *extra):
        p = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), "run", kind, "--dry-run", *extra], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        return p.stdout

    def test_dry_run_step1(self):
        out = self.dry("fair6-step1")
        self.assertIn("arms Af,O,Z", out)
        self.assertIn("phrasings ['cued', 'neutral', 'told']", out)
        self.assertIn("budget_usd: 130.0", out)
        self.assertIn("6 seeds", out)
        total = float(re.search(r"total about \$(\d+)", out).group(1))
        self.assertLessEqual(total, 130)
        self.assertAlmostEqual(total, 115, delta=1)      # 6 x (18.20 + 0.83 + 0.19)
        self.assertIn("ordering", out)

    def test_dry_run_step2(self):
        out = self.dry("fair6-step2", "--compare", "20261127T000000-fair6-step1")
        self.assertIn("budget_usd: 110.0", out)
        self.assertIn("arms B", out)
        total = float(re.search(r"total about \$(\d+)", out).group(1))
        self.assertLessEqual(total, 110)
        self.assertAlmostEqual(total, 96, delta=1)      # 6 x 15.95

    def test_dry_run_step2_without_compare_prints_the_estimate_but_a_real_run_needs_it(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        with self.assertRaises(SystemExit) as cm:
            runner.build_cfg("fair6-step2", ap.parse_args([]))
        self.assertIn("--compare", str(cm.exception))

    def test_default_jobs_is_3_and_the_old_default_is_kept(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        self.assertEqual(runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run"]))["jobs"], 3)
        self.assertEqual(runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run", "--jobs", "1"]))["jobs"], 1)
        self.assertEqual(runner.build_cfg("fair-step1", ap.parse_args(["--dry-run"]))["jobs"], 2)
        cfg = runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run"]))
        self.assertEqual((cfg["budget_usd"], cfg["n"], cfg["arms"], cfg["phrasings"]), (130.0, [10], ["Af", "O", "Z"], ["cued", "neutral", "told"]))
        self.assertTrue(cfg["fair"])


class Fair6Prompts(unittest.TestCase):
    """FR.8.6: told = the cued head plus one sentence. The cued and neutral prompts stay byte-identical to before (hashes made with the code of main, before this change)."""
    GOLD = {4001: "15190928bcd3da2724a702ebe6ebf63864d595f37700c1425481255d986c26f2", 4002: "68183488206f4cf5c4022fa62ae56f427c9dfe9edab449a9dc714be1f6ce9be9"}

    def prompts(self, seed, phrasing, cue=True):
        import gen
        sc = gen.build(seed)
        out = {}
        for t in "RDXSECM":
            if phrasing == "neutral" and t not in gen.NEUTRAL_TYPES:
                continue
            items = gen.select(sc, t, None)
            out[t] = gen.probe_prompt(t, sc, "PROBE-%d-Af-10-%s%s" % (seed, t, {"neutral": "n", "told": "t"}.get(phrasing, "")), 10, items=items, phrasing=phrasing, cue=cue)
        return out

    def test_cued_and_neutral_are_byte_identical_to_before(self):
        import hashlib
        for seed, want in self.GOLD.items():
            h = hashlib.sha256()
            for t in "RDXSECM":
                for ph in ("cued", "neutral"):
                    p = self.prompts(seed, ph).get(t)
                    if p is None:
                        continue
                    marker = "PROBE-%d-Af-10-%s%s" % (seed, t, "n" if ph == "neutral" else "")
                    self.assertTrue(p.startswith("[%s]" % marker))
                    h.update(p.encode() + b"\x00")
            self.assertEqual(h.hexdigest(), want, seed)

    def test_cued_and_neutral_literal_text(self):
        import gen
        sc = {"items": {"R": [{"id": 1, "question": "What is the tag?"}]}}
        self.assertEqual(gen.probe_prompt("R", sc, "M1", 10, items=sc["items"]["R"], phrasing="cued", cue=True),
                         "[M1] Answer from this session's history only. Do not guess. I told you these values earlier, so remember them.\n"
                         '1. What is the tag?\nReply with JSON only, in this form: {"answers":[{"id":1,"answer":"..."}]}')
        self.assertEqual(gen.probe_prompt("R", sc, "M1", 10, items=sc["items"]["R"], phrasing="cued", cue=False),
                         "[M1] Answer from this session's history only. Do not guess.\n"
                         '1. What is the tag?\nReply with JSON only, in this form: {"answers":[{"id":1,"answer":"..."}]}')

    def test_told_is_the_cued_head_plus_the_sentence(self):
        import gen
        sentence = " If a value is not in your context, search the transcript files of this session before you answer."
        self.assertEqual(gen.TOLD_SENTENCE, sentence)
        for seed in (4001, 4002):
            cued, told = self.prompts(seed, "cued"), self.prompts(seed, "told")
            self.assertEqual(set(cued), set("RDXSECM"))
            self.assertEqual(set(told), set("RDXSECM"))      # every type, as cued: the same items for all four arms
            for t in cued:
                c = cued[t].replace("-%s]" % t, "-%st]" % t, 1)      # only the marker differs: the told marker ends in t
                self.assertEqual(told[t].count(sentence), 1)
                self.assertEqual(told[t].replace(sentence, "", 1), c, t)
                head = told[t].split("\n")[0]
                self.assertTrue(head.endswith("remember them." + sentence), head)      # the sentence ends the cued head

    def test_told_has_no_neutral_phrasing_for_c_and_m_and_uses_all_25_items(self):
        import gen
        sc = gen.build(4001)
        n = sum(len(gen.select(sc, t, None)) for t in "RDXSECM")
        self.assertEqual(n, 25)

    def test_probe_plan_and_order(self):
        cfg = {"phrasings": ["cued", "neutral", "told"]}
        for t in "RDXSECM":
            self.assertIn("told", runner.probe_plan(cfg, t))
            self.assertEqual(runner.probe_plan(cfg, t)[0], "cued")
        self.assertEqual(runner.probe_plan(cfg, "C"), ["cued", "told"])
        self.assertEqual(runner.probe_plan(cfg, "R"), ["cued", "neutral", "told"])
        order = runner.probe_order(cfg, list("RDXSECM"))
        phs = [p for _t, p in order]
        self.assertEqual(phs.index("told"), len([p for p in phs if p != "told"]))      # told forks come after every cued and neutral fork
        self.assertEqual(phs[phs.index("told"):], ["told"] * 7)
        self.assertEqual([x for x in order if x[1] != "told"][:3], [("R", "cued"), ("R", "neutral"), ("D", "cued")])
        old = {"phrasings": ["cued", "neutral"]}
        self.assertEqual(runner.probe_order(old, list("RDXSECM")), [(t, ph) for t in "RDXSECM" for ph in runner.probe_plan(old, t)])      # no told: the old order
        self.assertEqual(runner.probe_order({}, ["R", "C"]), [("R", "cued"), ("C", "cued")])

    def test_told_has_its_own_fork_key_and_marker(self):
        keys = {runner.fork_key(10, "R", ph) for ph in ("cued", "neutral", "told")}
        self.assertEqual(len(keys), 3)
        self.assertEqual(runner.fork_key(10, "R", "cued"), "10:R")      # the old key does not change
        self.assertEqual(runner.fork_key(10, "R", "neutral"), "10:R:neutral")
        self.assertIn('"told": "t"', open(os.path.join(HERE, "runner.py")).read())

    def test_told_runs_through_probe(self):
        import tempfile
        t = Reprobe("test_fallback_takes_the_last_parsable_assistant_message")
        root = tempfile.mkdtemp()
        ctx, ss = t.session(root)
        seen = {}
        ss.transcript = lambda sid: os.path.join(root, "none.jsonl")
        ss.call = lambda prompt, *a, **k: (seen.update(prompt=prompt) or {"result": "{}", "session_id": "f1", "usage": {}, "_delta": 0.1, "permission_denials": []})
        ss.probe("R", 10, fork_of="main-sid", phrasing="told")
        self.assertIn("search the transcript files of this session", seen["prompt"])
        self.assertTrue(seen["prompt"].startswith("[PROBE-3002-Af-10-Rt]"))
        rows = runner.read_jsonl(os.path.join(ctx.dir, "results.jsonl"))
        self.assertTrue(rows and all(r["phrasing"] == "told" for r in rows))
        self.assertIn("10:R:told", ss.st["done_forks"])


class Fair6Rule(unittest.TestCase):
    def v(self, rows, sess=None, meta=META, old_rows=None):
        return fair.verdict6(rows, sess if sess is not None else sessions6(), TH, meta=meta, old_rows=old_rows)

    def test_sign_flip_p(self):
        from fractions import Fraction as F
        self.assertEqual(fair.sign_flip_p([F(1, 25)] * 6), F(1, 64))      # 6 of 6: the smallest p
        self.assertEqual(fair.sign_flip_p([F(1)] * 6), F(1, 64))
        self.assertEqual(fair.sign_flip_p([F(7, 25)] * 5 + [F(-1, 25)]), F(2, 64))      # 5 of 6
        self.assertEqual(fair.sign_flip_p([F(0)] * 6), F(1))      # all ties: every pattern counts
        self.assertEqual(fair.sign_flip_p([F(-1, 25)] * 6), F(1))      # all negative: p = 1
        self.assertEqual(fair.sign_flip_p([F(1)] * 8), F(1, 256))      # the pooled 8 seeds: 256 patterns
        self.assertIsNone(fair.sign_flip_p([]))

    def test_pass(self):
        v = self.v(make6(af=(15,) * 6, b=(22,) * 6))
        self.assertEqual(v["verdict"], "PASS", v["gates"])
        p = v["primary"]
        self.assertEqual((p["af"], p["b"], p["k"]), ((90, 150), (132, 150), 6))
        self.assertAlmostEqual(p["gain_points"], 28.0, places=3)
        from fractions import Fraction as F
        self.assertEqual(p["p"], F(1, 64))      # 6 of 6
        self.assertIsNone(p["note"])

    def test_pass_with_5_of_6_seeds(self):
        v = self.v(make6(af=(15,) * 6, b=(22, 22, 22, 22, 22, 14)))
        from fractions import Fraction as F
        self.assertEqual((v["primary"]["k"], v["primary"]["p"]), (5, F(2, 64)))
        self.assertGreaterEqual(v["primary"]["gain_points"], 15)
        self.assertEqual(v["verdict"], "PASS")

    def test_4_of_6_seeds_is_not_a_pass(self):
        v = self.v(make6(af=(15,) * 6, b=(24, 24, 24, 24, 12, 12)))      # G = 20 points, k = 4: not PASS, not FAIL
        self.assertEqual((v["primary"]["k"], v["verdict"]), (4, NO_CLEAR_6))

    def test_fail_when_the_gain_is_under_5_points(self):
        v = self.v(make6(af=(20,) * 6, b=(21,) * 6))      # G = 4.0
        self.assertEqual(v["verdict"], "FAIL")
        self.assertAlmostEqual(v["primary"]["gain_points"], 4.0, places=3)

    def test_fail_when_b_is_ahead_in_3_seeds_or_fewer(self):
        v = self.v(make6(af=(15,) * 6, b=(24, 24, 24, 10, 10, 10)))      # G = 8 points, k = 3
        self.assertEqual((v["primary"]["k"], v["verdict"]), (3, "FAIL"))
        v = self.v(make6(af=(10,) * 6, b=(25, 25, 25, 9, 9, 9)))      # G is 30 points, but k = 3: FAIL
        self.assertGreaterEqual(v["primary"]["gain_points"], 15)
        self.assertEqual(v["verdict"], "FAIL")

    def test_no_clear_difference_and_the_extra_line(self):
        v = self.v(make6(af=(15,) * 6, b=(18,) * 6))      # G = 12, k = 6, p = 1/64
        self.assertEqual(v["verdict"], NO_CLEAR_6)
        self.assertEqual(v["primary"]["note"], "gain of 12.0 points, below the registered 15")
        md, _ = fair.section6(make6(af=(15,) * 6, b=(18,) * 6), sessions6(), CFG, TH, meta=META)
        text = "\n".join(md)
        self.assertIn("Primary: no clear difference.", text)
        self.assertIn("gain of 12.0 points, below the registered 15", text)

    def test_no_extra_line_when_p_is_above_0_05(self):
        v = self.v(make6(af=(15,) * 6, b=(20, 20, 20, 14, 14, 20)))      # k = 4, G = 11.3, p > 0.05
        self.assertEqual(v["verdict"], NO_CLEAR_6)
        self.assertGreater(float(v["primary"]["p"]), 0.05)
        self.assertIsNone(v["primary"]["note"])

    def test_just_above_and_below_15_points(self):
        v = self.v(make6(af=(16,) * 6, b=(20, 20, 20, 20, 20, 19)))      # (119 - 96) / 150 = 15.33 points
        self.assertAlmostEqual(v["primary"]["gain_points"], 15.333333, places=5)
        self.assertEqual(v["verdict"], "PASS")
        v = self.v(make6(af=(16,) * 6, b=(19, 19, 19, 19, 19, 19)))      # (114 - 96) / 150 = 12 points
        self.assertEqual(v["verdict"], NO_CLEAR_6)

    def test_exactly_15_points_passes(self):
        th = fair.th6(TH)
        rows = []
        for s in SEEDS6:      # 20 questions each for the test: Af 12 of 20, B 15 of 20 = exactly 15 points
            for arm, k in (("Af", 12), ("B", 15)):
                rows += [row6(s, arm, "R", i, i <= k, "cued") for i in range(1, 21)]
        r = fair.rule6(rows, th, "cued", SEEDS6)
        self.assertEqual(r["gain_points"], 15.0)
        self.assertEqual(r["cls"], "PASS")

    def test_secondary_results_never_change_the_verdict(self):
        rows = make6(af=(15,) * 6, b=(22,) * 6, neutral=((24,) * 6, (10,) * 6), told=((24,) * 6, (10,) * 6))      # neutral and told say B is far behind
        v = self.v(rows)
        self.assertEqual(v["verdict"], "PASS")
        self.assertEqual(v["secondary"]["neutral"]["cls"], "FAIL")
        self.assertEqual(v["secondary"]["told"]["cls"], "FAIL")
        self.assertEqual(v["secondary"]["told"]["af"], (144, 150))

    def test_pooled8_is_secondary_and_has_256_patterns(self):
        old = []
        for s in (3001, 3002):
            for arm, k in (("Af", 15), ("B", 22)):
                n = 0
                for t, cnt in PER_TYPE.items():
                    for i in range(1, cnt + 1):
                        n += 1
                        old.append(row6(s, arm, t, i, n <= k, "cued"))
        v = self.v(make6(), old_rows=old)
        from fractions import Fraction as F
        self.assertEqual(v["pooled8"]["seeds"], [3001, 3002] + SEEDS6)
        self.assertEqual((v["pooled8"]["af"], v["pooled8"]["p"]), ((120, 200), F(1, 256)))
        self.assertEqual(v["primary"]["af"], (90, 150))      # the primary rule uses the 6 new seeds only
        md, _ = fair.section6(make6(), sessions6(), CFG, TH, meta=META, old_rows=old)
        self.assertIn("not confirmatory: 2 of these seeds set the plan; other LH commit", "\n".join(md))
        self.assertIsNone(self.v(make6())["pooled8"])

    def test_bootstrap_is_fixed_and_not_a_gate(self):
        v1, v2 = self.v(make6()), self.v(make6())
        self.assertEqual(v1["bootstrap"], v2["bootstrap"])      # the same rng seed, the same interval
        lo, hi = v1["bootstrap"]
        self.assertAlmostEqual(lo, 28.0, places=3)      # every seed has the same difference
        self.assertAlmostEqual(hi, 28.0, places=3)
        b = fair.bootstrap6(v1["primary"], dict(fair.th6(TH), bootstrap_rng_seed=1))
        self.assertIsNotNone(b)
        self.assertFalse(TH["fair6"]["bootstrap_is_gate"])
        v = self.v(make6(af=(15,) * 6, b=(24, 24, 24, 24, 14, 14)))
        self.assertLess(v["bootstrap"][0], v["bootstrap"][1])
        self.assertEqual(v["bootstrap"], fair.bootstrap6(v["primary"], fair.th6(TH)))
        text = "\n".join(fair.section6(make6(af=(15,) * 6, b=(24, 24, 24, 24, 14, 14)), sessions6(), CFG, TH, meta=META)[0])
        self.assertIn("6 seeds: this interval is too narrow; description only", text)
        self.assertIn("10000 draws, rng seed 20261127", text)

    def test_step1_alone_is_incomplete(self):
        v = self.v(make6(with_b=False), sessions6(with_b=False))
        self.assertEqual(v["verdict"], "INCOMPLETE")
        self.assertEqual(v["primary"]["af"], (90, 150))
        self.assertIsNone(v["bootstrap"])


NO_CLEAR_6 = "NO CLEAR DIFFERENCE"


class Fair6Stop(unittest.TestCase):
    def test_stop_at_128_and_not_at_127(self):
        s127 = make6(af=(22, 21, 21, 21, 21, 21), with_b=False)      # 127 of 150: 23 errors
        s128 = make6(af=(22, 22, 21, 21, 21, 21), with_b=False)      # 128 of 150: 22 errors
        stop, x, y = fair.stop_rule6(s127, TH)
        self.assertEqual((stop, x, y), (False, 127, 150))
        stop, x, y = fair.stop_rule6(s128, TH)
        self.assertEqual((stop, x, y), (True, 128, 150))
        self.assertEqual(fair.STOP6_FMT % (x, y), "STOP: plain Claude Code with search scores 128/150; the primary rule cannot pass")

    def test_stop_uses_the_hinted_probes_only(self):
        rows = make6(af=(5,) * 6, neutral=((25,) * 6, (25,) * 6), told=((25,) * 6, (25,) * 6), with_b=False)
        self.assertFalse(fair.stop_rule6(rows, TH)[0])

    def test_stop_line_is_in_the_summary_of_step1(self):
        md, v = fair.section6(make6(af=(22, 22, 21, 21, 21, 21), with_b=False), sessions6(with_b=False), CFG, TH, meta=META[:1])
        text = "\n".join(md)
        self.assertIn("STOP: plain Claude Code with search scores 128/150; the primary rule cannot pass", text)
        self.assertIn("INCOMPLETE", text)

    def test_stop_check_command_reads_the_fair6_run(self):
        import io, contextlib, tempfile
        root = tempfile.mkdtemp()
        rd = os.path.join(root, "fx")
        os.makedirs(rd)
        json.dump({"kind": "fair6-step1", "seeds": SEEDS6}, open(os.path.join(rd, "config.json"), "w"))
        open(os.path.join(rd, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in make6(af=(22, 22, 21, 21, 21, 21), with_b=False)) + "\n")
        open(os.path.join(rd, "sessions.jsonl"), "w").write("")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fair.main(["stop-check", rd])
        self.assertEqual(rc, 0)
        self.assertIn("STOP: plain Claude Code with search scores 128/150; the primary rule cannot pass", buf.getvalue())
        open(os.path.join(rd, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in make6(af=(22, 21, 21, 21, 21, 21), with_b=False)) + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fair.main(["stop-check", rd])
        self.assertNotIn("STOP", buf.getvalue())
        self.assertIn("127/150", buf.getvalue())


class Fair6Gates(unittest.TestCase):
    def v(self, rows=None, sess=None, meta=META):
        return fair.verdict6(rows if rows is not None else make6(), sess if sess is not None else sessions6(), TH, meta=meta)

    def invalid(self, v, name):
        self.assertEqual(v["verdict"], "INVALID", v["gates"])
        self.assertTrue(any(name in g for g in v["invalid_gates"]), (name, v["invalid_gates"]))

    def test_all_gates_pass_on_clean_data(self):
        v = self.v()
        self.assertEqual(v["invalid_gates"], [])
        self.assertEqual(v["verdict"], "PASS")
        names = " | ".join(g["gate"] for g in v["gates"])
        for n in ("Oracle", "Amnesia", "Parse rate", "real compactions", "Denied reads", "another arm", "Every seed has", "One claude_version", "One hook sha256", "No live install"):
            self.assertIn(n, names)

    def test_the_fr4_gates_still_apply(self):
        self.invalid(self.v(make6(o=0.9)), "Oracle")
        self.invalid(self.v(make6(z=0.12)), "Amnesia")
        sess = sessions6()
        sess[0] = dict(sess[0], boundaries=9)
        self.invalid(self.v(sess=sess), "real compactions")
        sess = sessions6()
        sess[0] = dict(sess[0], af_reads={"own_ok": 0, "own_denied": 1, "other": [], "calls": 3})
        self.invalid(self.v(sess=sess), "Denied reads")
        sess = sessions6()
        sess[0] = dict(sess[0], af_reads={"own_ok": 1, "own_denied": 0, "other": ["Bash cat /x/lhbench-r-s4001-B/a.jsonl"], "calls": 3})
        self.invalid(self.v(sess=sess), "another arm")
        rows = make6()
        for r in [x for x in rows if x["arm"] == "Af" and x["seed"] == 4001][:10]:
            r["parse_ok"] = False
        self.invalid(self.v(rows), "Parse rate, arm Af")

    def test_a_seed_without_arm_o_rows(self):
        rows = [r for r in make6() if not (r["arm"] == "O" and r["seed"] == 4003)]
        self.invalid(self.v(rows), "Every seed has")

    def test_a_seed_without_z_af_or_b_rows(self):
        for arm in ("Z", "Af", "B"):
            rows = [r for r in make6() if not (r["arm"] == arm and r["seed"] == 4006)]
            self.invalid(self.v(rows), "Every seed has")

    def test_o_and_z_must_run_on_all_6_seeds(self):
        rows = [r for r in make6() if not (r["arm"] in ("O", "Z") and r["seed"] in (4005, 4006))]
        v = self.v(rows)
        self.invalid(v, "Every seed has")
        self.assertIn("s4005-O", [g for g in v["gates"] if g["gate"].startswith("Every seed")][0]["measured"])

    def test_two_claude_versions(self):
        sess = sessions6()
        sess[3] = dict(sess[3], claude_version="2.1.1")
        self.invalid(self.v(sess=sess), "claude_version")
        rows = make6()
        rows[-1] = dict(rows[-1], claude_version="2.0.9")
        self.invalid(self.v(rows), "claude_version")

    def test_no_claude_version_is_no_data_not_a_pass(self):
        rows = [dict(r, claude_version=None) for r in make6()]
        sess = [dict(s, claude_version=None) for s in sessions6()]
        v = self.v(rows, sess)
        self.assertEqual(v["verdict"], "INVALID")
        self.assertEqual([g["result"] for g in v["gates"] if g["gate"].startswith("One claude_version")], ["NO DATA"])

    def test_two_hook_hashes_in_arm_b(self):
        sess = sessions6()
        i = [k for k, s in enumerate(sess) if s["arm"] == "B"][2]
        sess[i] = dict(sess[i], hook_sha256={"precompact-handoff.py": "aa11", "recall.py": "CHANGED"})
        self.invalid(self.v(sess=sess), "hook sha256")

    def test_missing_hook_hash_in_arm_b(self):
        sess = sessions6()
        i = [k for k, s in enumerate(sess) if s["arm"] == "B"][0]
        sess[i] = dict(sess[i], hook_sha256=None)
        self.invalid(self.v(sess=sess), "hook sha256")

    def test_live_install_between_the_steps(self):
        self.invalid(self.v(meta=[{"live_recall_sha256": "cc33", "settings_hash_changed": False}, {"live_recall_sha256": "dd44", "settings_hash_changed": False}]), "No live install")
        self.invalid(self.v(meta=[{"live_recall_sha256": "cc33", "settings_hash_changed": False}, {"live_recall_sha256": "cc33", "settings_hash_changed": True}]), "No live install")
        self.invalid(self.v(meta=None), "No live install")
        self.invalid(self.v(meta=[{"live_recall_sha256": None, "settings_hash_changed": False}]), "No live install")

    def test_a_row_that_does_not_parse_counts_as_wrong(self):
        rows = make6()
        r = [x for x in rows if x["arm"] == "B" and x["phrasing"] == "cued" and x["correct"]][0]
        r.update(parse_ok=False, correct=False)      # the grader sets correct False for a row that does not parse, also after the FR.7 fallback
        b = fair.rule6(rows, fair.th6(TH), "cued", SEEDS6)["b"]
        self.assertEqual(b, (131, 150))      # still 150 questions: the row counts as wrong, it is not dropped

    def test_reprobe_only_a_harness_parse_failure_one_time(self):
        import argparse, tempfile
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        cfg = runner.build_cfg("fair6-step1", ap.parse_args(["--dry-run"]))
        cfg["run_id"] = "fx-r6"
        root = tempfile.mkdtemp()

        def plan(rows):
            open(os.path.join(root, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
            return runner.reprobe_plan(root, cfg, {"sessions": {"s4001-Af": {"sid": "m1"}}}, 4001, "Af", "E", "cued")

        fails = [row6(4001, "Af", "E", i, False, "cued", parse_ok=False) for i in range(1, 5)]
        self.assertEqual(plan(fails)[2], 10)      # a harness parse failure: allowed
        wrong = [row6(4001, "Af", "E", i, False, "cued", parse_ok=True) for i in range(1, 5)]
        with self.assertRaises(runner.Fatal) as cm:
            plan(wrong)
        self.assertIn("wrong answer", str(cm.exception))
        again = [dict(r, reprobe=True) for r in fails]
        with self.assertRaises(runner.Fatal) as cm:
            plan(again)
        self.assertIn("one time", str(cm.exception))
        old = runner.build_cfg("fair-step1", ap.parse_args(["--dry-run"]))      # the old presets keep the old rule
        old["run_id"] = "fx-old"
        open(os.path.join(root, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in wrong) + "\n")
        self.assertEqual(runner.reprobe_plan(root, old, {"sessions": {"s4001-Af": {"sid": "m1"}}}, 4001, "Af", "E", "cued")[2], 10)


class Fair6Report(unittest.TestCase):
    def text(self, rows, sess=None, **kw):
        md, _v = fair.section6(rows, sess if sess is not None else sessions6(), CFG, TH, meta=kw.pop("meta", META), **kw)
        return "\n".join(md)

    def test_fr5_wording_with_6_new_seeds(self):
        t = self.text(make6(af=(15,) * 6, b=(22,) * 6))
        self.assertIn("Small test: 6 new seeds, 150 hinted questions, Sonnet, 10 compactions. Plain Claude Code that can search its own transcripts: 90 of 150. "
                      "Lossless Handoff: 132 of 150.", t)
        self.assertIn("B minus Af: +28.0 points. Seeds with B >= Af: 6 of 6. Exact one-sided sign-flip p: 1/64 = 0.0156 (64 patterns).", t)
        self.assertIn("Verdict (primary rule, 6 new seeds, hinted probes, N=10): PASS.", t)

    def test_never_says_validated_or_proof(self):
        for rows in (make6(), make6(af=(20,) * 6, b=(21,) * 6), make6(af=(15,) * 6, b=(18,) * 6), make6(o=0.5), make6(with_b=False), make6(af=(24,) * 6, b=(24,) * 6)):
            t = self.text(rows).lower()
            self.assertNotIn("validated", t)
            self.assertNotIn("proof", t)

    def test_told_line_and_the_no_instruction_note(self):
        t = self.text(make6(af=(15,) * 6, b=(22,) * 6, told=((13,) * 6, (24,) * 6)))
        self.assertIn("When the prompt tells plain Claude Code to search: 78 of 150. Lossless Handoff: 144 of 150.", t)
        self.assertNotIn("The gain is that no instruction is necessary.", t)
        t = self.text(make6(af=(15,) * 6, b=(22,) * 6, told=((23,) * 6, (24,) * 6)))      # told Af 138 of 150 = 92%
        self.assertIn("When the prompt tells plain Claude Code to search: 138 of 150. Lossless Handoff: 144 of 150.", t)
        self.assertIn("The gain is that no instruction is necessary.", t)
        t = self.text(make6(af=(15,) * 6, b=(22,) * 6, told=((22,) * 6, (24,) * 6)))      # 132 of 150 = 88%: no note
        self.assertNotIn("The gain is that no instruction is necessary.", t)
        edge = self.text(make6(af=(15,) * 6, b=(22,) * 6, told=((23, 22, 22, 22, 23, 23), (24,) * 6)))      # 135 of 150 = 90% exactly: the note
        self.assertIn("The gain is that no instruction is necessary.", edge)

    def test_search_rate_and_accuracy_with_and_without_a_lookup(self):
        rows = make6(lookup={"cued": True, "neutral": False, "told": True})
        v = fair.verdict6(rows, sessions6(), TH, meta=META)
        s = v["search"]
        self.assertEqual((s["cued"]["searched"], s["cued"]["forks"]), (150, 150))      # every Af fork of the fixture searched
        self.assertEqual(s["neutral"]["searched"], 0)
        self.assertEqual(s["neutral"]["with"], (0, 0))
        self.assertEqual(s["neutral"]["without"][1], 150)
        self.assertEqual(s["cued"]["with"], (90, 150))
        # a mixed fork set: half of the Af rows of cued searched, the half that searched are right
        rows = make6(af=(15,) * 6)
        for r in rows:
            if r["arm"] == "Af" and r["phrasing"] == "cued":
                r["used_lookup"] = bool(r["correct"])
        s = fair.verdict6(rows, sessions6(), TH, meta=META)["search"]["cued"]
        self.assertEqual((s["with"], s["without"]), ((90, 90), (0, 60)))
        t = self.text(rows)
        self.assertIn("Af search rate", t)
        self.assertIn("Forks that searched", t)

    def test_types_apart_are_counts_only(self):
        v = fair.verdict6(make6(), sessions6(), TH, meta=META)
        self.assertEqual(set(v["types"]), {"S and E", "R D X C M"})
        self.assertEqual(v["types"]["S and E"]["told"]["Af"][1], 48)      # 2 types x 4 items x 6 seeds
        self.assertEqual(v["types"]["R D X C M"]["cued"]["B"][1], 102)      # (4+4+4+4+1) x 6 seeds
        self.assertEqual(set(v["types"]["S and E"]), {"cued", "neutral", "told"})

    def test_report_build_for_step1_then_step2(self):
        import tempfile
        import argparse
        import report
        root = tempfile.mkdtemp()
        old = report.RESULTS
        report.RESULTS = root
        try:
            all_rows, all_sess = make6(), sessions6()

            def make_run(kind, rows, sess, compare=None):
                ap = argparse.ArgumentParser()
                runner.parse_common(ap)
                cfg = runner.build_cfg(kind, ap.parse_args(["--dry-run"] + (["--compare", compare] if compare else [])))
                cfg["run_id"] = "fx-" + kind
                cfg["live_recall_sha256"] = "cc33"
                rd = os.path.join(root, cfg["run_id"])
                os.makedirs(rd)
                json.dump(cfg, open(os.path.join(rd, "config.json"), "w"))
                json.dump({"spent": 1.0, "status": "done", "sessions": {}}, open(os.path.join(rd, "state.json"), "w"))
                open(os.path.join(rd, "results.jsonl"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
                open(os.path.join(rd, "sessions.jsonl"), "w").write("\n".join(json.dumps(dict({"turns": 50, "cost_usd": 1.0, "missed": [], "status": "done"}, **s)) for s in sess) + "\n")
                open(os.path.join(rd, "calls.jsonl"), "w").write("")
                return cfg["run_id"]

            step1 = make_run("fair6-step1", [r for r in all_rows if r["arm"] != "B"], [s for s in all_sess if s["arm"] != "B"])
            out, text = report.build(step1)
            self.assertIn("## Fair replication (6 seeds)", text)
            self.assertIn("INCOMPLETE", text)
            json.dumps(out, default=str)
            step2 = make_run("fair6-step2", [dict(r, run="r2") for r in all_rows if r["arm"] == "B"], [s for s in all_sess if s["arm"] == "B"], compare=step1)
            out2, text2 = report.build(step2)
            self.assertIn("Verdict (primary rule, 6 new seeds, hinted probes, N=10): PASS", text2)
            self.assertIn("Lossless Handoff: 132 of 150", text2)
            self.assertNotIn("validated", text2.lower())
            self.assertEqual(out2["fair6"]["verdict"], "PASS")
        finally:
            report.RESULTS = old


def inspect_src(mod):
    import inspect
    return inspect.getsource(mod)


if __name__ == "__main__":
    unittest.main()
