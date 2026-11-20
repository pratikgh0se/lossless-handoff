#!/usr/bin/env python3
"""Self-test of the lost-in-the-middle design: position tags, neutral probes, arm R, no-compaction condition, cost, report.
It uses no model, no network and no claude process. Run: python3 bench/continuity/test_litm.py
"""
import hashlib, json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost, gen, grade, litm, report, runner

FIX = os.path.join(HERE, "fixtures")
SEEDS = list(range(1000, 1020))


def sha(x):
    return hashlib.sha256(x.encode() if isinstance(x, str) else x).hexdigest()[:16]


class Regression(unittest.TestCase):
    """Old runs and old arms must score the same as before this design existed."""

    def test_old_scripts_and_probe_text_unchanged(self):
        # hashes were taken from the generator before the change (origin/main 7e826c9)
        want = {1000: ("aadb496509d85d28", "003aefe55a8b7f96"), 1001: ("74c8fbd823e5ae8b", "3f0e316c98b225d3")}
        for seed, (hs, hp) in want.items():
            s = gen.build(seed)
            self.assertEqual(sha(json.dumps(s, sort_keys=True)), hs)
            h = hashlib.sha256()
            for n in (1, 3, 10):
                for t in gen.TYPES + ["F"]:
                    it = s["fresh"][str(n)] if t == "F" else gen.select(s, t, n - 1)
                    if it:
                        h.update(gen.probe_prompt(t, s, "M", n, items=it).encode())
            self.assertEqual(h.hexdigest()[:16], hp)

    def test_stored_fixture_scores_the_same(self):
        old = report.RESULTS
        report.RESULTS = FIX
        try:
            _, text = report.build("legacy-run", 500)
        finally:
            report.RESULTS = old
        with open(os.path.join(FIX, "legacy-run", "expected-summary.md"), encoding="utf-8") as f:
            self.assertEqual(text, f.read())
        self.assertNotIn("Lost in the middle", text)      # an old run gets no new section

    def test_stored_fixture_regrades_the_same(self):
        rows = [json.loads(x) for x in open(os.path.join(FIX, "legacy-run", "results.jsonl"), encoding="utf-8")]
        self.assertGreater(len(rows), 600)
        by = {}
        for r in rows:
            by.setdefault((r["seed"], r["arm"], r["n_target"], r["type"], r["fork_sid"], r["raw"]), []).append(r)
        for (seed, arm, n, t, _f, raw), grp in by.items():
            sc = gen.build(seed)
            items = sc["fresh"][str(n)] if t == "F" else gen.select(sc, t, None if arm == "Z" else ((max(r["n_target"] for r in rows) - 1) if arm == "O" else n - 1))
            g = {x["id"]: x for x in grade.grade(t, items, raw)}
            for r in grp:
                self.assertEqual(g[r["item"]]["correct"], r["correct"])


class Position(unittest.TestCase):
    def test_every_planted_fact_has_a_tag(self):
        for seed in SEEDS[:6]:
            s = gen.build(seed)
            for t in ("R", "D", "X", "S", "E", "C", "U"):
                for it in s["items"][t]:
                    self.assertIn(gen.position_of(s, t, it), gen.POSITIONS)
            self.assertIsNone(gen.position_of(s, "M", s["items"]["M"][0]))

    def test_thirds_by_turn_index(self):
        self.assertEqual([gen.position_of_turn(i, 9) for i in range(9)], ["start"] * 3 + ["middle"] * 3 + ["end"] * 3)
        s = gen.build(1000)
        idx, total = gen.nominal_timeline(s)
        self.assertEqual(total, len(s["events"]) + 5 * gen.FILL_PER_EPOCH)
        for t in ("R", "D", "X", "S", "E", "C"):
            self.assertEqual([gen.position_of(s, t, it) for it in s["items"][t]], ["start", "start", "middle", "end"])

    def test_tags_do_not_change_the_script(self):
        s = gen.build(1000)
        before = json.dumps(s, sort_keys=True)
        for t in ("R", "S"):
            [gen.position_of(s, t, it) for it in s["items"][t]]
        self.assertEqual(json.dumps(s, sort_keys=True), before)


class NeutralProbes(unittest.TestCase):
    def test_cue_detector(self):
        for c in gen.CUE_PHRASES:
            self.assertTrue(gen.has_cue("Please " + c.upper() + " it"), c)
        self.assertTrue(gen.has_cue('Find "one two three" now'))
        self.assertFalse(gen.has_cue('Find "one two" now'))
        self.assertFalse(gen.has_cue("Write the tag."))

    def test_the_cue_list_matches_the_contract(self):
        self.assertEqual(len(gen.CUE_PHRASES), 16)
        self.assertEqual(sorted(gen.CUE_PHRASES), sorted(gen.CUE_PHRASES))
        for c in ("what did i say", "last time", "which one did", "what was the", "as we discussed"):
            self.assertIn(c, gen.CUE_PHRASES)

    def test_neutral_probes_hold_no_cue_phrase(self):
        for seed in SEEDS:
            s = gen.build(seed)
            for t in gen.NEUTRAL_TYPES:
                for n in (1, 3, 10, 30, 0):
                    items = gen.select(s, t, None if n == 0 else n - 1)
                    for marker in ("PROBE-%d-R-%d-%sn" % (seed, n, t), "PROBE-%d-A-%d-%s" % (seed, n, t)):
                        p = gen.probe_prompt(t, s, marker, n, items=items, phrasing="neutral")
                        self.assertFalse(gen.has_cue(p), (seed, t, n, p))
                        self.assertNotIn("history", p.lower())      # no pointer to the past either

    def test_neutral_probe_has_no_answer_and_grades_with_the_same_grader(self):
        s = gen.build(1003)
        make = {"R": lambda x: {"id": x["id"], "answer": x["answer"]}, "D": lambda x: {"id": x["id"], "answer": x["answer"]},
                "S": lambda x: {"id": x["id"], "answer": x["answer"]},
                "X": lambda x: {"id": x["id"], "current": x["new"], "replaced": [x["old"]]},
                "E": lambda x: {"id": x["id"], "proceed": False, "reason_code": x["ecode"]}}
        for t in gen.NEUTRAL_TYPES:
            items = s["items"][t]
            p = gen.probe_prompt(t, s, "M", items=items, phrasing="neutral")
            self.assertEqual([v for v in s["secrets"] if v in p], [])
            raw = json.dumps({"answers": [make[t](x) for x in items]})
            self.assertTrue(all(g["correct"] for g in grade.grade(t, items, raw)))
            for x in items:
                self.assertIn(x["topic"], p)

    def test_neutral_does_not_exist_for_c_and_m(self):
        s = gen.build(1000)
        for t in ("C", "M"):
            with self.assertRaises(ValueError):
                gen.probe_prompt(t, s, "M", items=s["items"][t], phrasing="neutral")
        self.assertEqual(runner.probe_plan({"phrasings": ["cued", "neutral"]}, "C"), ["cued"])
        self.assertEqual(runner.probe_plan({"phrasings": ["cued", "neutral"]}, "R"), ["cued", "neutral"])
        self.assertEqual(runner.probe_plan({}, "R"), ["cued"])

    def test_cued_probes_of_design_2_carry_a_cue_so_the_hook_can_fire(self):
        s = gen.build(1000)
        for t in gen.TYPES + ["F"]:
            items = s["fresh"]["3"] if t == "F" else s["items"][t]
            self.assertTrue(gen.has_cue(gen.probe_prompt(t, s, "M", 3, items=items, cue=True)), t)


class ArmR(unittest.TestCase):
    def test_settings_register_the_hook_only_for_arm_r(self):
        b = runner.lh_settings("/x/hooks")
        r = runner.lh_settings("/x/hooks", auto_recall=True)
        self.assertNotIn("UserPromptSubmit", b["hooks"])
        h = r["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual((h["command"], h["timeout"]), ("python3 /x/hooks/auto-recall.py", 5))
        r["hooks"].pop("UserPromptSubmit")
        self.assertEqual(r, b)          # everything else is the default install

    def test_fire_log_reading(self):
        rows = [{"session_id": "f1", "fired": True, "hits": 2}, {"session_id": "f1", "fired": True, "hits": 3},
                {"session_id": "other", "fired": True, "hits": 9}, "junk"]
        self.assertEqual(runner.recall_log_fired(rows, "f1"), (True, 3))
        self.assertEqual(runner.recall_log_fired(rows, "none"), (False, 0))
        self.assertEqual(runner.recall_log_fired([], "f1"), (False, 0))

    def test_arm_r_needs_the_hook_file(self):
        class A:
            pass
        a = A()
        for k, v in dict(model=None, window=None, n=None, arms="A,R", types=None, seeds=1, seed_file=None, lh_path=tempfile.gettempdir(),
                         files_per_turn=None, o_window=None, oracle_turns=None, o_files_per_turn=None, work_base=None, call_cap_usd=None,
                         turn_timeout_s=None, jobs=None, budget_usd=None, conditions=None, phrasings=None, nc_window=None, dry_run=True).items():
            setattr(a, k, v)
        with self.assertRaises(SystemExit):
            runner.build_cfg("slice", a)

    def test_state_folder_is_separate_for_each_session(self):
        ctx = FakeCtx()
        a = runner.Session(ctx, "s1-R", 1000, "R", {}, "compact")
        b = runner.Session(ctx, "s1-R-nc", 1000, "R", {}, "nocompact")
        self.assertNotEqual(a.statedir, b.statedir)
        self.assertEqual(a.overrides["HANDOFF_STATE_DIR"], a.statedir)


class FakeCtx:
    def __init__(self):
        self.cfg = {"run_id": "t", "work_base": tempfile.gettempdir(), "o_window": 200000, "window": 400000, "nc_window": 200000,
                    "model": "sonnet", "lh_path": "/x", "gen_version": gen.GEN_VERSION}

    def script(self, seed):
        return gen.build(seed)


class NoCompaction(unittest.TestCase):
    def test_compaction_is_off_only_in_the_no_compaction_condition(self):
        ctx = FakeCtx()
        c = runner.Session(ctx, "s1-A", 1000, "A", {}, "compact")
        n = runner.Session(ctx, "s1-A-nc", 1000, "A", {}, "nocompact")
        self.assertNotIn("DISABLE_AUTO_COMPACT", c.overrides)
        self.assertEqual(c.window, 400000)
        self.assertEqual(n.overrides["DISABLE_AUTO_COMPACT"], "1")
        self.assertEqual(n.window, 200000)
        self.assertTrue(n.proj.endswith("-nc"))
        self.assertFalse(c.proj.endswith("-nc"))
        self.assertEqual(n.env()["DISABLE_AUTO_COMPACT"], "1")

    def test_same_session_length_as_the_nominal_timeline(self):
        s = gen.build(1000)
        steps = runner.nc_plan_steps(s)
        self.assertEqual(len(steps), gen.nominal_timeline(s)[1])
        self.assertEqual(sum(1 for k, _ in steps if k == "ev"), len(s["events"]))
        self.assertEqual([i for k, i in steps if k == "fill"], list(range(5 * gen.FILL_PER_EPOCH)))
        # the plant turns come in the same order as in the compaction run
        self.assertEqual([i for k, i in steps if k == "ev"], list(range(len(s["events"]))))

    def test_session_fits_its_window(self):
        planted = runner.o_planted_tokens(SEEDS[:5])
        lines, est = runner.nc_plan(200000, planted)
        self.assertGreaterEqual(lines, 20)
        self.assertLess(est, 0.75 * 200000)
        with self.assertRaises(SystemExit):
            runner.nc_plan(40000, planted)
        for seed in SEEDS[:3]:
            s = gen.build(seed)
            name, text = gen.filler_file(seed, 0, 0, lines=lines)
            self.assertEqual(len(text.splitlines()), lines)
            self.assertFalse(any(x in text for x in s["secrets"]))

    def test_work_units(self):
        cfg = {"seeds": [1, 2], "arms": ["A", "B", "R", "Z", "O"], "conditions": ["compact", "nocompact"]}
        u = runner.work_units(cfg)
        self.assertEqual(len(u), 2 * (3 * 2 + 2))
        self.assertEqual(sorted(set(c for s, a, c in u if a in ("Z", "O"))), ["compact"])
        self.assertEqual(runner.work_units({"seeds": [1], "arms": ["A", "B"]}), [(1, "A", "compact"), (1, "B", "compact")])   # old config

    def test_old_state_keys_still_match(self):
        self.assertEqual(runner.fork_key(10, "R"), "10:R")
        self.assertEqual(runner.fork_key(10, "R", "neutral"), "10:R:neutral")


def synth_rows():
    """Synthetic rows of the new design: arms A, B, R, both conditions, both phrasings, 4 seeds."""
    import random
    rng = random.Random(5)
    p = {("A", "compact"): .2, ("A", "nocompact"): .8, ("B", "compact"): .6, ("B", "nocompact"): .8, ("R", "compact"): .75, ("R", "nocompact"): .8}
    rows = []
    for seed in (1000, 1001, 1002, 1003):
        sc = gen.build(seed)
        for (arm, cond), pr in p.items():
            ns = [10] if cond == "compact" else [0]
            for n in ns:
                for t in ("R", "D", "X", "S", "E", "C"):
                    for ph in (("cued", "neutral") if t in gen.NEUTRAL_TYPES else ("cued",)):
                        for it in gen.select(sc, t, None if cond == "nocompact" else n - 1):
                            neutral_pr = pr if not (arm == "R" and ph == "neutral") else p[("B", cond)]
                            fired = None if arm != "R" else (ph == "cued")
                            rows.append({"seed": seed, "arm": arm, "n_target": n, "n_actual": n if cond == "compact" else 0, "type": t, "item": it["id"],
                                         "fork_sid": "f%d-%s-%s-%d-%s-%s" % (seed, arm, cond, n, t, ph), "correct": rng.random() < neutral_pr, "parse_ok": True,
                                         "abstained": False, "used_lookup": False, "condition": cond, "phrasing": ph,
                                         "position": gen.position_of(sc, t, it), "auto_recall_fired": fired, "epoch": it["epoch"]})
    return rows


class ReportSection(unittest.TestCase):
    def setUp(self):
        self.th = json.load(open(os.path.join(HERE, "thresholds.json")))

    def test_thresholds_are_registered(self):
        lt = self.th["litm"]
        for k in ("h1_middle_minus_ends_max_points", "h2_compaction_minus_nocompaction_max_points", "h3_r_minus_b_cued_min_points",
                  "h4_r_minus_b_neutral_no_gain_below_points", "h5_fire_rate_cued_min", "h5_fire_rate_neutral_max"):
            self.assertIn(k, lt)

    def test_sections_and_fire_rates(self):
        rows = synth_rows()
        sess = [{"name": "s1000-A-nc", "arm": "A", "condition": "nocompact", "boundaries": 0, "missed": []}]
        md, out = litm.section(rows, sess, self.th, 300)
        text = "\n".join(md)
        for h in ("Table L0", "Table L1", "Table L2", "Table L3", "Table L4", "H1", "H2", "H3", "H4", "H5"):
            self.assertIn(h, text)
        self.assertIn("The hook fired on 100% (", text)
        self.assertIn("of cued and 0% (", text)
        self.assertIn("neutral probes (arm R)", text)
        pos = out["tables"]["position"]
        self.assertGreater(pos["A/nocompact/cued/middle"][1], 0)           # counts are in the report
        self.assertGreater(pos["A/nocompact/cued/start"][1], pos["A/nocompact/cued/middle"][1])   # 2 start items for 1 middle item
        self.assertIn("No-compaction sessions with a compaction (must be 0): **0 of 1**", text)
        res = {h["name"][:2]: h["result"] for h in out["hypotheses"]}
        self.assertEqual(res["H2"], "SUPPORTED")          # A: 20% with compaction, 80% without
        self.assertEqual(res["H5"], "SUPPORTED")
        self.assertIn(res["H4"].split(" ")[0], ("CONFIRMED", "INCONCLUSIVE", "REFUTED"))

    def test_invalid_no_compaction_session_is_flagged(self):
        md, out = litm.section(synth_rows(), [{"name": "s1-A-nc", "arm": "A", "condition": "nocompact", "boundaries": 1, "missed": [{"target": 0, "actual": 1}]}], self.th, 100)
        self.assertEqual(out["nc_invalid"], ["s1-A-nc"])
        self.assertIn("INVALID", "\n".join(md))

    def test_full_report_keeps_old_tables_and_adds_the_new_section(self):
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(FIX, "legacy-run")
            dst = os.path.join(d, "run1")
            shutil.copytree(src, dst)
            with open(os.path.join(dst, "results.jsonl"), "a", encoding="utf-8") as f:
                for r in synth_rows():
                    if not (r["arm"] == "R" or r["phrasing"] == "neutral" or r["condition"] == "nocompact"):
                        continue        # a compaction, cued row of A or B belongs to the old tables, so it would change them
                    r.update({"run": "x", "gen_version": "5"})
                    f.write(json.dumps(r, sort_keys=True) + "\n")
            old = report.RESULTS
            report.RESULTS = d
            try:
                _, text = report.build("run1", 500)
            finally:
                report.RESULTS = old
            legacy = open(os.path.join(src, "expected-summary.md"), encoding="utf-8").read()
            self.assertIn("Lost in the middle", text)
            # the old tables hold the same cells: new rows (neutral, no compaction) never enter them
            old_part = text.split("\n## Lost in the middle")[0].replace("run1", "legacy-run")
            self.assertEqual(old_part.rstrip("\n"), legacy.rstrip("\n"))      # every old table is byte for byte the same
        finally:
            shutil.rmtree(d)


class CostEstimate(unittest.TestCase):
    def test_pilot_and_full(self):
        p, _ = cost.render("PILOT", cost.PILOT)
        f, _ = cost.render("FULL", cost.FULL)
        self.assertEqual(cost.PILOT["seeds"], 2)
        self.assertEqual(cost.PILOT["arms"], ["A", "B", "R"])
        self.assertEqual(cost.FULL["seeds"], 20)
        self.assertEqual(cost.PILOT["conditions"], ["compact", "nocompact"])
        self.assertTrue(100 < p < 250, p)
        self.assertTrue(1000 < f < 3000, f)
        self.assertAlmostEqual(f / 20 / (p / 2) > 1, True)

    def test_model_matches_the_wave_numbers(self):
        self.assertAlmostEqual(2 * cost.UNIT_N10 + cost.Z_PLUS_O_PER_SEED, cost.WAVE1_PER_SEED_4ARMS)
        self.assertAlmostEqual(2 * (cost.BASE + cost.PER_COMPACTION * 30), cost.WAVE2_PER_SEED_AB_N30)
        old, _, _ = cost.estimate(1, ["A", "B"], [1, 3, 10], ["compact"], ["cued"])
        self.assertAlmostEqual(old, 2 * cost.UNIT_N10)


if __name__ == "__main__":
    unittest.main(verbosity=1)
