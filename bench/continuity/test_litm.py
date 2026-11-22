#!/usr/bin/env python3
"""Self-test of the lost-in-the-middle design: position tags, neutral probes, arm R, no-compaction condition, cost, report.
It uses no model, no network and no claude process. Run: python3 bench/continuity/test_litm.py
"""
import hashlib, json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost, gates, gen, grade, litm, report, runner

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
        self.assertEqual(runner.recall_log_fired(rows, "f1"), (True, 3, False))
        self.assertEqual(runner.recall_log_fired(rows, "none"), (False, 0, False))
        self.assertEqual(runner.recall_log_fired([], "f1"), (False, 0, False))
        self.assertEqual(runner.recall_log_fired([{"session_id": "t", "fired": True, "hits": 0, "timed_out": True}], "t"), (True, 0, True))

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
        self.assertIn("must be 0): **0 of 1**", text)
        res = {h["name"][:2]: h["result"] for h in out["hypotheses"]}
        self.assertEqual(res["H2"], "SUPPORTED")          # A: 20% with compaction, 80% without
        self.assertEqual(res["H5"], "SUPPORTED")
        self.assertTrue(res["H4"].startswith(("no gain of 5 points", "INCONCLUSIVE", "REFUTED", "INVALID")), res["H4"])

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


def tmp_transcript(lines):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
    return path


class RoundTwoFixes(unittest.TestCase):
    """Fixes of the review of PR 29."""

    def setUp(self):
        self.th = json.load(open(os.path.join(HERE, "thresholds.json")))

    # -- 1. H1 ceiling
    def test_h1_too_easy_above_ceiling(self):
        self.assertEqual(self.th["litm"]["h1_ceiling_max_accuracy"], 0.95)
        rows = [r for r in synth_rows() if not (r["arm"] == "A" and r["condition"] == "nocompact" and r["phrasing"] == "cued")]
        for r in synth_rows():
            if r["arm"] == "A" and r["condition"] == "nocompact" and r["phrasing"] == "cued":
                r = dict(r, correct=True)       # everything right: no room for a middle effect
                rows.append(r)
        _, out = litm.section(rows, [], self.th, 100)
        h1 = [h for h in out["hypotheses"] if h["name"].startswith("H1")][0]
        self.assertTrue(h1["result"].startswith("TOO EASY: no claim"), h1)

    def test_h1_uses_the_mean_of_start_and_end_rates(self):
        class B:
            draws = [[0, 1]] * 3
            def _ci(self, v):
                return min(v), max(v)
        # seed 0 has 1 start item, seed 1 has 3 start items; the rates are pooled over seeds first, then averaged
        d = litm.mean_ends_diff(B(), ([1, 0], [1, 1]), ([1, 0], [1, 3]), ([1, 1], [1, 1]))
        self.assertAlmostEqual(d[0], 0.5 - (0.25 + 1.0) / 2)

    # -- 2. no-compaction validity
    def test_validity_rules(self):
        ok, why = runner.nc_validity(0, 0, 100000, 100000)
        self.assertTrue(ok and not why)
        self.assertFalse(runner.nc_validity(1, 0, 100000, 100000)[0])                  # compaction
        self.assertFalse(runner.nc_validity(0, 2, 100000, 100000)[0])                  # cleared marker
        ok, why = runner.nc_validity(0, 0, 60000, 100000)                              # context too small (below 75%)
        self.assertFalse(ok)
        self.assertIn("last-turn context", why[0])
        self.assertTrue(runner.nc_validity(0, 0, 75000, 100000)[0])                    # at the tolerance: valid
        self.assertFalse(runner.nc_validity(0, 0, None, 100000)[0])                    # no token data: cannot verify

    def test_cleared_marker_in_a_fixture_transcript(self):
        clean = tmp_transcript([{"type": "user", "message": {"content": "hello"}}, {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a file"}]}}])
        dirty = tmp_transcript([{"type": "user", "message": {"content": [{"type": "tool_result", "content": "[Old tool result content cleared]"}]}}])
        try:
            self.assertEqual(runner.count_cleared(clean), 0)
            self.assertEqual(runner.count_cleared(dirty), 1)
            self.assertEqual(runner.count_cleared("/no/such/file"), 0)
        finally:
            os.remove(clean)
            os.remove(dirty)

    def test_last_turn_context_from_calls(self):
        calls = [{"name": "s1-A-nc", "kind": "turn", "in": 5, "cache_read": 1000}, {"name": "s1-A-nc", "kind": "fork", "in": 9, "cache_read": 9},
                 {"name": "s1-A-nc", "kind": "turn", "in": 7, "cache_read": 90000}, {"name": "other", "kind": "turn", "in": 1, "cache_read": 1}]
        self.assertEqual(runner.last_turn_context(calls, "s1-A-nc"), 90007)
        self.assertIsNone(runner.last_turn_context(calls, "none"))
        self.assertIsNone(runner.last_turn_context([{"name": "x", "kind": "turn"}], "x"))

    def test_report_counts_invalid_no_compaction_sessions(self):
        sess = [{"name": "s1-A-nc", "arm": "A", "condition": "nocompact", "boundaries": 0, "missed": [], "nc_valid": False,
                 "nc_reasons": ["1 cleared-result marker line(s)"], "status": "invalid"},
                {"name": "s2-A-nc", "arm": "A", "condition": "nocompact", "boundaries": 0, "missed": [], "nc_valid": True, "status": "done"}]
        md, out = litm.section(synth_rows(), sess, self.th, 100)
        self.assertEqual(out["nc_invalid"], ["s1-A-nc"])
        self.assertIn("**1 of 2**", "\n".join(md))

    # -- 3. fire detection reads only the arm's own state folder
    def test_fire_log_path_is_the_arm_state_folder(self):
        ctx = FakeCtx()
        r = runner.Session(ctx, "s1-R", 1000, "R", {}, "compact")
        os.makedirs(r.statedir, exist_ok=True)
        log = os.path.join(r.statedir, runner.AUTO_RECALL_LOG)
        with open(log, "w") as f:
            f.write(json.dumps({"session_id": "fork1", "fired": True, "hits": 2, "timed_out": False}) + "\n")
        try:
            self.assertEqual(r.auto_recall_fired("fork1"), (True, 2, False))
            self.assertEqual(r.auto_recall_fired("fork2"), (False, 0, False))
            a = runner.Session(ctx, "s1-A", 1000, "A", {}, "compact")
            self.assertEqual(a.auto_recall_fired("fork1"), (None, None, None))
        finally:
            shutil.rmtree(r.statedir, ignore_errors=True)
        src = open(os.path.join(HERE, "runner.py"), encoding="utf-8").read()
        body = src[src.index("def auto_recall_fired"):src.index("def probe(")]
        self.assertNotIn("HOME", body)              # the code never builds the real state folder path

    def test_missing_log_line_stops_a_cued_probe(self):
        src = open(os.path.join(HERE, "runner.py"), encoding="utf-8").read()
        self.assertIn('phrasing == "cued" and cfg.get("probe_design", 1) >= 2 and fsid and not fired', src)
        self.assertIn("raise Fatal(\"arm %s: a cued probe left no line", src)

    # -- 4. L0 columns
    def test_l0_has_hits_and_timeout_rates_and_the_cap(self):
        rows = synth_rows()
        for r in rows:
            if r["arm"] == "R" and r["auto_recall_fired"]:
                r["auto_recall_hits"], r["auto_recall_timed_out"] = 2, False
        md, out = litm.section(rows, [], self.th, 100)
        text = "\n".join(md)
        self.assertIn("Fired with hits > 0", text)
        self.assertIn("Timed out", text)
        self.assertIn("at most 3 hits", text)
        self.assertEqual(out["tables"]["fire"]["compact/cued"][2], out["tables"]["fire"]["compact/cued"][1])

    # -- 6. H4 invalid when the hook fires on neutral probes
    def test_h4_invalid_if_neutral_fire_rate_above_limit(self):
        rows = synth_rows()
        for r in rows:
            if r["arm"] == "R" and r["phrasing"] == "neutral":
                r["auto_recall_fired"] = True
        _, out = litm.section(rows, [], self.th, 100)
        res = {h["name"][:2]: h["result"] for h in out["hypotheses"]}
        self.assertTrue(res["H4"].startswith("INVALID"), res["H4"])
        self.assertEqual(res["H5"], "NOT SUPPORTED")
        text = "\n".join(litm.section(synth_rows(), [], self.th, 100)[0])
        self.assertNotIn("CONFIRMED", text)

    # -- 7. largest N
    def test_largest_n_means_equal_to_the_maximum(self):
        self.assertTrue(litm.full_n({"n_target": 10}, [1, 3, 10]))
        self.assertFalse(litm.full_n({"n_target": 3}, [1, 3, 10]))
        self.assertFalse(litm.full_n({"n_target": 10}, [1, 3, 10, 30]))
        self.assertTrue(litm.full_n({"n_target": 30}, [1, 3, 10, 30]))
        self.assertFalse(litm.full_n({"n_target": 10}, []))

    # -- 8, 9, 10, 11 labels and presets
    def test_head_labels_and_preset(self):
        d = tempfile.mkdtemp()
        try:
            dst = os.path.join(d, "run1")
            shutil.copytree(os.path.join(FIX, "legacy-run"), dst)
            cfg = json.load(open(os.path.join(dst, "config.json")))
            cfg["probe_design"] = 2
            json.dump(cfg, open(os.path.join(dst, "config.json"), "w"))
            old = report.RESULTS
            report.RESULTS = d
            try:
                _, text = report.build("run1", 100)
            finally:
                report.RESULTS = old
            self.assertIn("Probe design: 2.", text)
            self.assertIn("not comparable with Wave 1 and Wave 2", text)
        finally:
            shutil.rmtree(d)
        md = "\n".join(litm.section(synth_rows(), [], self.th, 100)[0])
        self.assertIn("not a pure cue contrast", md)
        self.assertIn("compactions survived", md)
        self.assertIn("middle minus mean(start, end)", md)
        self.assertIn("plumbing check", md)
        self.assertIn("expected, not blind; mixes compaction with session length", md)
        k = runner.KINDS["litm-smoke"]
        self.assertEqual((k["seeds"], k["arms"], k["conditions"]), (1, "A,R", "nocompact"))
        total, _, _ = cost.estimate(1, ["A", "R"], [1, 3, 10], ["nocompact"], ["cued", "neutral"])
        self.assertTrue(10 < total < 14, total)
        self.assertIn("litm-smoke", open(os.path.join(HERE, "run.sh")).read())


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


class Design3(unittest.TestCase):
    """Probe design 3: real long context (A), non-literal probes (B), balanced positions (C), arm K and search rate (D), framing (E), cost (F)."""

    def setUp(self):
        self.th = json.load(open(os.path.join(HERE, "thresholds.json")))

    # ---- A. real long context, all user turns
    def test_plan_is_user_turns_only_with_75_turns_in_equal_thirds(self):
        for seed in SEEDS[:5]:
            s = gen.build_litm3(seed)
            plan = s["plan"]
            self.assertEqual(len(plan), 75)
            self.assertEqual([x["turn"] for x in plan], list(range(75)))
            self.assertEqual({x["kind"] for x in plan}, {"bridge", "fact", "x2", "fill"})
            self.assertEqual(s["events"], [])                       # no plant event with files: nothing is a tool result
            self.assertTrue(all("files" not in x for x in plan))
            self.assertEqual(sum(1 for x in plan if x["kind"] == "fill"), 36)
            for th in range(3):
                self.assertEqual([x["third"] for x in plan[25 * th:25 * th + 25]], [th] * 25)

    def test_plan_hits_300k_tokens_in_a_400k_window(self):
        self.assertEqual(runner.NC3_WINDOW, 400000)
        lines, est = runner.nc3_plan(400000)
        self.assertTrue(0.70 * 400000 <= est <= 0.80 * 400000, est)
        self.assertGreaterEqual(lines, 300)
        with self.assertRaises(SystemExit):
            runner.nc3_plan(60000)

    def test_filler_is_a_pasted_document_with_a_small_task(self):
        s = gen.build_litm3(1000)
        lines, _ = runner.nc3_plan(400000)
        prompt, expect = gen.filler_doc_prompt(s, 3, lines)
        self.assertIn("<<<DOC", prompt)
        doc = prompt.split("<<<DOC\n")[1].split("DOC>>>")[0]
        self.assertEqual(len(doc.splitlines()), lines)                 # the document is in the prompt, line for line
        self.assertIn("Task: reply with only the number at the start of the last line", prompt)
        self.assertTrue(expect and expect in prompt)
        self.assertFalse(any(v in prompt for v in s["secrets"]))
        self.assertFalse(gen.has_cue(prompt))

    def test_plan_turns_hold_no_cue_so_the_hook_fires_only_on_probes(self):
        for seed in SEEDS:
            for st in gen.build_litm3(seed)["plan"]:
                if "prompt" in st:
                    self.assertFalse(gen.has_cue(st["prompt"]), (seed, st["kind"]))

    def test_design_3_uses_its_own_script_only_in_the_no_compaction_condition(self):
        cfg = dict(FakeCtx().cfg, probe_design=3)
        ctx = FakeCtx()
        ctx.cfg = cfg
        ctx.script3 = lambda seed: gen.build_litm3(seed)
        nc = runner.Session(ctx, "s1-A-nc", 1000, "A", {}, "nocompact")
        cm = runner.Session(ctx, "s1-A", 1000, "A", {}, "compact")
        z = runner.Session(ctx, "s1-Z", 1000, "Z", {}, "compact")
        self.assertTrue(nc.d3)
        self.assertFalse(cm.d3)
        self.assertFalse(z.d3)
        self.assertEqual(nc.overrides["DISABLE_AUTO_COMPACT"], "1")
        cfg2 = dict(FakeCtx().cfg)          # design 2: unchanged
        ctx2 = FakeCtx()
        self.assertFalse(runner.Session(ctx2, "s1-A-nc", 1000, "A", {}, "nocompact").d3)

    def test_validity_rule_is_kept_for_design_3(self):
        src = open(os.path.join(HERE, "runner.py"), encoding="utf-8").read()
        body = src[src.index("def run_nc3_session"):src.index("def run_nc_session")]
        self.assertIn("nc_validity(", body)
        self.assertIn("count_cleared(", body)
        self.assertIn("nc_estimate_tokens", body)

    # ---- B. non-literal probes
    def test_nonliteral_probe_shares_no_content_word_with_its_plant(self):
        for seed in range(1000, 1060):
            s = gen.build_litm3(seed)
            for t, lst in s["items"].items():
                for it in lst:
                    q = gen.nonliteral_question(t, it)
                    plant = " ".join(gen.plant_sentences(t, it))
                    drop = [it.get(k) for k in ("answer", "old", "new", "ecode")]
                    self.assertEqual(gen.content_words(q, drop) & gen.content_words(plant, drop), set(), (seed, t, q, plant))

    def test_nonliteral_probe_holds_no_cue_and_no_answer(self):
        for seed in SEEDS:
            s = gen.build_litm3(seed)
            for t in gen.L3_TYPES:
                p = gen.probe_prompt(t, s, "PROBE-%d-R-0-%sn" % (seed, t), 0, items=s["items"][t], phrasing="nonliteral")
                self.assertFalse(gen.has_cue(p), (seed, t))
                self.assertEqual([v for v in s["secrets"] if v in p], [])
                for it in s["items"][t]:
                    self.assertNotIn(it["topic"], p)                    # the probe names the alias only
                    self.assertIn(it["alias"], p)

    def test_the_overlap_test_can_fail(self):
        self.assertEqual(gen.content_words("write the queue", []) & gen.content_words("Decision: the queue is x", []), {"queue"})
        self.assertEqual(gen.content_words("TAG-AAAA-1111-BBBB tag", ["TAG-AAAA-1111-BBBB"]), set())

    def test_bridge_is_in_the_same_third_as_the_fact_and_before_it(self):
        for seed in SEEDS[:5]:
            s = gen.build_litm3(seed)
            for t, lst in s["items"].items():
                for it in lst:
                    bridge = [x for x in s["plan"] if x["kind"] == "bridge" and x["third"] == it["third"]][0]
                    self.assertIn(it["alias"], bridge["prompt"])
                    self.assertIn("`%s`" % it["topic"], bridge["prompt"])
                    self.assertLess(bridge["turn"], it["turn"])
                    self.assertEqual(it["turn"] // 25, bridge["turn"] // 25)
                    self.assertNotIn(it["alias"], [x for x in s["plan"] if x["kind"] == "fact" and x["turn"] == it["turn"]][0]["prompt"])

    def test_nonliteral_grades_with_the_same_grader(self):
        s = gen.build_litm3(1005)
        make = {"R": lambda x: {"id": x["id"], "answer": x["answer"]}, "D": lambda x: {"id": x["id"], "answer": x["answer"]},
                "S": lambda x: {"id": x["id"], "answer": x["answer"]}, "X": lambda x: {"id": x["id"], "current": x["new"], "replaced": [x["old"]]},
                "E": lambda x: {"id": x["id"], "proceed": False, "reason_code": x["ecode"]}}
        for t in gen.L3_TYPES:
            items = gen.select(s, t, None)
            self.assertEqual(len(items), 6)
            raw = json.dumps({"answers": [make[t](x) for x in items]})
            self.assertTrue(all(g["correct"] for g in grade.grade(t, items, raw)))

    def test_nonliteral_only_for_r_d_x_s_e_and_only_in_design_3(self):
        s = gen.build_litm3(1000)
        for t in ("C", "M"):
            with self.assertRaises(ValueError):
                gen.nonliteral_question(t, s["items"]["R"][0])
        ph = {"phrasings": ["cued", "neutral", "nonliteral"]}
        self.assertEqual(runner.probe_plan(ph, "R"), ["cued", "neutral"])                 # compaction condition: no bridge, so no non-literal probe
        self.assertEqual(runner.probe_plan(ph, "R", d3=True), ["cued", "neutral", "nonliteral"])
        self.assertEqual(runner.probe_plan(ph, "C", d3=True), ["cued"])

    # ---- C. balanced positions
    def test_two_facts_of_each_type_in_each_third(self):
        for seed in SEEDS[:8]:
            s = gen.build_litm3(seed)
            for t in gen.L3_TYPES:
                self.assertEqual([it["position"] for it in s["items"][t]], ["start", "start", "middle", "middle", "end", "end"])

    def test_position_is_the_real_turn_order(self):
        for seed in SEEDS[:8]:
            s = gen.build_litm3(seed)
            for t, lst in s["items"].items():
                for it in lst:
                    self.assertEqual(gen.position_of_turn(it["turn"], len(s["plan"])), it["position"])
                    step = s["plan"][it["turn"]]
                    self.assertEqual((step["kind"], step["type"], step["id"]), ("fact", t, it["id"]))

    def test_power_figure(self):
        p3, mde3, _ = cost.h1_power(20, 10, 10, 10)
        p2, mde2, _ = cost.h1_power(20, 6, 12, 6)
        self.assertGreater(p3, p2)
        self.assertLess(mde3, mde2)
        self.assertTrue(0.45 < p3 < 0.70, p3)
        self.assertGreater(cost.h1_power(60, 10, 10, 10)[0], 0.8)
        self.assertTrue(any("design 3" in x and "power to find a -10 point effect" in x for x in cost.power_lines(20)))

    def test_script_of_design_2_is_not_changed_by_design_3(self):
        a = json.dumps(gen.build(1000), sort_keys=True)
        gen.build_litm3(1000)
        self.assertEqual(json.dumps(gen.build(1000), sort_keys=True), a)
        self.assertEqual(sha(a), "aadb496509d85d28")

    # ---- D. arm K and search rate
    def test_arm_k_settings(self):
        k = runner.lh_settings("/x/hooks", auto_recall=True, reanchor=True)["hooks"]
        self.assertEqual(k["UserPromptSubmit"][0]["hooks"][0]["command"], "python3 /x/hooks/auto-recall.py")
        post = k["PostToolUse"]
        self.assertEqual([h["hooks"][0]["command"] for h in post], ["python3 /x/hooks/handoff-nudge.py", "python3 /x/hooks/reanchor.py"])
        self.assertEqual((post[1]["matcher"], post[1]["hooks"][0]["timeout"]), ("*", 5))
        b = runner.lh_settings("/x/hooks")["hooks"]
        self.assertEqual(len(b["PostToolUse"]), 1)                                         # arm B is as before
        self.assertNotIn("UserPromptSubmit", b)

    def test_arm_k_hooks_copy_marker_and_mcp(self):
        d = tempfile.mkdtemp()
        try:
            lh = os.path.join(d, "lh")
            os.makedirs(os.path.join(lh, "hooks"))
            for f in runner.K_FILES + ("recall.py",):
                open(os.path.join(lh, "hooks", f), "w").write("# x\n")
            ctx = FakeCtx()
            ctx.cfg.update(lh_path=lh, work_base=d)
            k = runner.Session(ctx, "s1-K", 1000, "K", {}, "compact")
            b = runner.Session(ctx, "s1-B", 1000, "B", {}, "compact")
            kd = k.hooks_dir()
            self.assertTrue(os.path.isfile(os.path.join(kd, ".recall-rules")))             # F68 marker, in K's own copy
            self.assertTrue(os.path.isfile(os.path.join(kd, "recall-mcp.py")))
            self.assertEqual(b.hooks_dir(), os.path.join(lh, "hooks"))
            self.assertFalse(os.path.exists(os.path.join(lh, "hooks", ".recall-rules")))   # the shared checkout is untouched
            mcp = json.loads(k.mcp_config())["mcpServers"]["lossless-recall"]
            self.assertEqual(mcp["args"], [os.path.join(kd, "recall-mcp.py")])
            self.assertEqual(mcp["env"]["HANDOFF_STATE_DIR"], k.statedir)
            self.assertEqual(json.loads(b.mcp_config()), {"mcpServers": {}})
            self.assertIn("mcp__lossless-recall__search_earlier_chat", k.allowed_tools())
            self.assertNotIn("mcp__", b.allowed_tools())
            self.assertEqual(runner.work_units({"seeds": [1], "arms": ["K"], "conditions": ["compact", "nocompact"]}), [(1, "K", "compact"), (1, "K", "nocompact")])
        finally:
            shutil.rmtree(d)

    def test_arm_k_needs_its_hook_files(self):
        class A:
            pass
        a = A()
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "hooks"))
            open(os.path.join(d, "hooks", "precompact-handoff.py"), "w").close()
            open(os.path.join(d, "hooks", "auto-recall.py"), "w").close()
            for k_, v in dict(model=None, window=None, n=None, arms="A,K", types=None, seeds=1, seed_file=None, lh_path=d, files_per_turn=None,
                              o_window=None, oracle_turns=None, o_files_per_turn=None, work_base=None, call_cap_usd=None, turn_timeout_s=None,
                              jobs=None, budget_usd=None, conditions=None, phrasings=None, nc_window=None, dry_run=True).items():
                setattr(a, k_, v)
            with self.assertRaises(SystemExit) as cm:
                runner.build_cfg("litm3-smoke", a)
            self.assertIn("recall-mcp.py", str(cm.exception))
        finally:
            shutil.rmtree(d)

    def test_search_used_from_fixture_records(self):
        mark = "PROBE-1-K-0-Rn"
        user = {"type": "user", "message": {"role": "user", "content": mark + " question"}}
        bash = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python3 ~/.claude/hooks/recall.py search x"}}]}}
        mcp = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__lossless-recall__search_earlier_chat", "input": {"query": "x"}}]}}
        inj = {"type": "attachment", "attachment": {"content": "Lossless Handoff auto-recall: earlier messages that match your words."}}
        old_inj = {"type": "attachment", "attachment": {"content": "Lossless Handoff auto-recall: from the main chat"}}
        plain = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
        self.assertEqual(grade.search_used([old_inj, user, plain], mark), (False, []))      # a copy of the parent history does not count
        self.assertEqual(grade.search_used([user, bash], mark), (True, ["bash"]))
        self.assertEqual(grade.search_used([user, mcp], mark), (True, ["mcp"]))
        self.assertEqual(grade.search_used([user, inj, plain], mark), (True, ["injection"]))
        used, kinds = grade.search_used([user, inj, bash, mcp], mark)
        self.assertEqual((used, sorted(kinds)), (True, ["bash", "injection", "mcp"]))
        self.assertEqual(grade.search_used([plain], mark), (False, []))

    def test_every_probe_row_records_used_lookup_in_design_3(self):
        src = open(os.path.join(HERE, "runner.py"), encoding="utf-8").read()
        self.assertIn("grade.search_used(frecs, marker)", src)
        self.assertIn('"used_lookup": used, "search_kinds": kinds', src)

    def test_h6_h7_are_registered_and_reported(self):
        lt = self.th["litm"]
        self.assertEqual(lt["h7_k_minus_b_neutral_accuracy_min_points"], 5)
        self.assertEqual(lt["h6_k_minus_b_search_rate_min_points"], 0)
        self.assertIn("stop and remove the levers", lt["h6_note"])
        rows = d3_rows(k_search=0.9, b_search=0.1)
        md, out = litm.section(rows, [], self.th, 100)
        text = "\n".join(md)
        res = {h["name"][:2]: h["result"] for h in out["hypotheses"]}
        self.assertEqual(res["H6"], "SUPPORTED")
        self.assertEqual(res["H7"], "SUPPORTED")
        self.assertIn("H1n", " ".join(h["name"] for h in out["hypotheses"]))
        self.assertIn("Table L5", text)
        self.assertIn("search rate by arm, phrasing and position", text)
        self.assertIn("Table L2b", text)
        lo = d3_rows(k_search=0.0, b_search=0.5)
        res2 = {h["name"][:2]: h["result"] for h in litm.section(lo, [], self.th, 100)[1]["hypotheses"]}
        self.assertEqual(res2["H6"], "NOT SUPPORTED")

    # ---- E. framing
    def test_framing_null_control(self):
        md, out = litm.section(d3_rows(0.9, 0.1), [], self.th, 100)
        text = "\n".join(md)
        self.assertIn("NULL CONTROL", text)
        self.assertIn("core handoff", text)
        self.assertIn("Only arms R and K add anything", text)
        self.assertEqual(len(out["tables"]["null_control"]), 4)
        readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
        self.assertIn("B minus A there is a null control", readme)
        self.assertIn("Do not credit the core handoff", readme)
        self.assertIn("Why the filler differs between conditions", readme)

    # ---- F. cost
    def test_cost_of_the_three_runs(self):
        smoke, _, _ = cost.estimate3(1, ["A", "B", "R", "K"], [1, 3], ["compact", "nocompact"])
        nc_smoke, _, _ = cost.estimate3(1, ["A", "R"], [1, 3, 10], ["nocompact"])
        self.assertTrue(12 < nc_smoke < 22, nc_smoke)
        pilot, _, _ = cost.estimate3(2, ["A", "B", "R", "K"], [1, 3, 10], ["compact", "nocompact"])
        full, _, _ = cost.estimate3(20, ["A", "B", "R", "K", "Z", "O"], [1, 3, 10], ["compact", "nocompact"])
        self.assertTrue(50 < smoke < 90, smoke)
        self.assertTrue(180 < pilot < 300, pilot)
        self.assertTrue(1900 < full < 3000, full)
        self.assertGreater(cost.d3_nc_unit(), cost.NC_UNIT)        # a 300k session costs more than the short one of design 2
        self.assertGreater(cost.ARM_FACTOR["K"], cost.ARM_FACTOR["R"])
        txt = "\n".join(cost.ASSUMPTIONS3)
        for w in ("prices", "75 turns", "probe forks", "slack", "cache has expired", "power"):
            self.assertIn(w, txt)

    def test_dry_run_of_design_3_prints_the_estimate_and_assumptions(self):
        import subprocess
        out = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), "run", "litm3-smoke", "--arms", "A", "--dry-run"], capture_output=True, text=True).stdout
        self.assertIn("probe design 3", out)
        self.assertIn("Assumptions of the design-3 estimate", out)
        out2 = subprocess.run([sys.executable, os.path.join(HERE, "cost.py")], capture_output=True, text=True).stdout
        for w in ("SMOKE (litm3-smoke)", "PILOT (litm3-pilot)", "FULL (litm3-full)", "H1 power at 20 seeds"):
            self.assertIn(w, out2)

    def test_presets(self):
        for name, arms, conds in (("litm3-smoke", "A,B,R,K", "compact,nocompact"), ("litm3-nc-smoke", "A,R", "nocompact"), ("litm3-pilot", "A,B,R,K", "compact,nocompact"), ("litm3-full", "A,B,R,K,Z,O", "compact,nocompact")):
            k = runner.KINDS[name]
            self.assertEqual((k["arms"], k["conditions"], k["probe_design"]), (arms, conds, 3))
            self.assertEqual(k["phrasings"], "cued,neutral,nonliteral")
        self.assertEqual((runner.KINDS["litm3-pilot"]["seeds"], runner.KINDS["litm3-full"]["seeds"]), (2, 20))
        self.assertIn("litm3-smoke", open(os.path.join(HERE, "run.sh")).read())


def d3_rows(k_search, b_search):
    """Synthetic design-3 rows: arms A, B, R, K, no compaction, three phrasings, balanced positions, a used_lookup flag."""
    import random
    rng = random.Random(11)
    acc = {"A": .6, "B": .6, "R": .7, "K": .85}
    srch = {"A": 0.0, "B": b_search, "R": 0.3, "K": k_search}
    rows = []
    for seed in (1000, 1001, 1002, 1003):
        sc = gen.build_litm3(seed)
        for arm in ("A", "B", "R", "K"):
            for t in gen.L3_TYPES:
                for ph in ("cued", "neutral", "nonliteral"):
                    for it in sc["items"][t]:
                        pa = acc[arm] if ph != "nonliteral" else acc[arm] - 0.1
                        rows.append({"seed": seed, "arm": arm, "n_target": 0, "n_actual": 0, "type": t, "item": it["id"],
                                     "fork_sid": "f%d%s%s%s" % (seed, arm, t, ph), "correct": rng.random() < pa, "parse_ok": True, "abstained": False,
                                     "condition": "nocompact", "phrasing": ph, "position": it["position"], "used_lookup": rng.random() < srch[arm],
                                     "auto_recall_fired": (ph == "cued") if arm in ("R", "K") else None, "auto_recall_hits": 1, "auto_recall_timed_out": False})
    return rows


def make_pf_run(root, **flaws):
    """A fixture preflight run (arms B, R, K; one compaction session and one no-compaction session each) that passes every line.
    flaws switch one thing off or wrong. Returns (run folder, projects folder)."""
    run_id, work, projects = "pf", os.path.join(root, "work"), os.path.join(root, "projects")
    rd = os.path.join(root, "res", run_id)
    for d in (rd, work, projects):
        os.makedirs(d, exist_ok=True)
    cfg = {"run_id": run_id, "work_base": work, "nc_estimate_tokens": 72000, "kind": "preflight", "arms": ["B", "R", "K"], "seeds": [1]}
    json.dump(cfg, open(os.path.join(rd, "config.json"), "w"))
    st = {"spent": 9.0 if flaws.get("cost_high") else 1.8, "status": "done", "sessions": {}}
    sess_rows, rows, calls = [], [], []
    for arm in ("B", "R", "K"):
        for cond in ("compact", "nocompact"):
            name = "s1-%s%s" % (arm, "-nc" if cond == "nocompact" else "")
            sid = "sid-%s-%s" % (arm, cond)
            st["sessions"][name] = {"status": "done", "sid": sid, "cost": 0.1}
            stem = "lhbench-pf-s1-%s%s" % (arm, "-nc" if cond == "nocompact" else "")
            statedir = os.path.join(work, stem + "-state")
            os.makedirs(statedir, exist_ok=True)
            proj = os.path.realpath(os.path.join(work, stem))
            tdir = os.path.join(projects, runner.slug(proj))
            os.makedirs(tdir, exist_ok=True)
            lines = [{"type": "user", "message": {"content": "hello"}}]
            bounds = 0
            cost_u = cost.preflight_unit(arm, cond) * 1.0
            if cond == "compact":
                bounds = 0 if flaws.get("no_compaction") else 1
                head = "Handoff part one: goal alpha beta gamma delta"
                text = "" if flaws.get("no_inject") else head + " "
                text += "" if flaws.get("no_decisions") else "If recall and the decisions list disagree, the decisions list wins. "
                text += "- recall rule: before you state an earlier number, path, name, command or decision, search for it. " if (arm == "K" and not flaws.get("k_no_rule")) else ""
                lines += [{"subtype": "compact_boundary"}, {"type": "user", "message": {"content": text or "nothing"}}] if bounds else [{"type": "user", "message": {"content": "no boundary"}}]
                if not flaws.get("no_receipt"):
                    open(os.path.join(statedir, "receipts.jsonl"), "w").write(json.dumps({"kind": "main", "part1_head": head}) + "\n")
            else:
                lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "[Old tool result content cleared]" if flaws.get("nc_cleared") else "data"}]}})
                calls.append({"name": name, "kind": "turn", "in": 5, "cache_read": 30000 if flaws.get("nc_small") else 80000})
                if arm == "K" and not flaws.get("k_no_reanchor"):
                    open(os.path.join(statedir, "health.jsonl"), "w").write(json.dumps({"event": "reanchor", "tokens": 60000}) + "\n")
            with open(os.path.join(tdir, sid + ".jsonl"), "w") as f:
                for ln in lines:
                    f.write(json.dumps(ln) + "\n")
            sess_rows.append({"name": name, "seed": 1, "arm": arm, "condition": cond, "status": "done", "boundaries": bounds, "cost_usd": cost_u})
            for t in ("R", "X"):
                for ph in ("cued", "neutral"):
                    fsid = "fork-%s-%s-%s-%s" % (arm, cond, t, ph)
                    fired = None
                    if arm in ("R", "K"):
                        fired = (ph == "cued") and not (flaws.get("r_missing_fire") and arm == "R" and cond == "compact")
                        if arm == "R" and flaws.get("r_neutral_fire") and ph == "neutral":
                            fired = True
                        if fired:
                            open(os.path.join(statedir, "auto-recall.jsonl"), "a").write(json.dumps({"session_id": fsid, "fired": True, "hits": 1}) + "\n")
                    rows.append({"seed": 1, "arm": arm, "condition": cond, "type": t, "phrasing": ph, "fork_sid": fsid, "parse_ok": not flaws.get("bad_parse"),
                                 "correct": True, "used_lookup": None if flaws.get("no_used_lookup") else False, "auto_recall_fired": fired,
                                 "search_kinds": ["mcp"] if (arm == "K" and ph == "cued") else []})
    with open(os.path.join(rd, "results.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(rd, "sessions.jsonl"), "w") as f:
        for r in sess_rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(rd, "calls.jsonl"), "w") as f:
        for r in calls:
            f.write(json.dumps(r) + "\n")
    json.dump(st, open(os.path.join(rd, "state.json"), "w"))
    return rd, projects


def fake_mcp_server(path, tools=("search_earlier_chat", "open_message")):
    open(path, "w").write(
        "import json, sys\n"
        "for ln in sys.stdin:\n"
        "    m = json.loads(ln)\n"
        "    if m.get('id') == 1:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'serverInfo': {'name': 'lossless-recall'}}}), flush=True)\n"
        "    if m.get('id') == 2:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': 2, 'result': {'tools': [{'name': n} for n in %r]}}), flush=True)\n" % (list(tools),))


class Preflight(unittest.TestCase):
    def run_check(self, **flaws):
        import preflight
        d = tempfile.mkdtemp()
        try:
            rd, projects = make_pf_run(d, **flaws)
            return preflight.check(rd, projects, mcp=False)
        finally:
            shutil.rmtree(d)

    def status(self, res, word):
        got = [st for label, st, _ in res if word in label]
        self.assertEqual(len(got), 1, (word, got))
        return got[0]

    def test_clean_run_passes_every_line(self):
        res = self.run_check()
        self.assertEqual([x for x in res if x[1] == "FAIL"], [])
        self.assertGreaterEqual(len(res), 13)

    def test_each_line_fails_when_its_fact_is_wrong(self):
        cases = [("no_compaction", "a compaction happened"), ("no_receipt", "receipt was written"), ("no_inject", "was injected after"),
                 ("no_decisions", "decisions list or the rules"), ("nc_cleared", "no-compaction sessions"), ("nc_small", "no-compaction sessions"),
                 ("r_missing_fire", "arm R:"), ("r_neutral_fire", "arm R:"), ("k_no_reanchor", "reanchor health row"), ("k_no_rule", "recall-rule line"),
                 ("no_used_lookup", "used_lookup is set"), ("bad_parse", "parsed and graded"), ("cost_high", "total cost under"), ("cost_high", "against the cost.py estimate")]
        for flaw, word in cases:
            if flaw == "cost_high" and "estimate" in word:
                # the ratio line uses the cost of the sessions, so make the sessions expensive
                import preflight
                d = tempfile.mkdtemp()
                try:
                    rd, projects = make_pf_run(d)
                    rows = [json.loads(x) for x in open(os.path.join(rd, "sessions.jsonl"))]
                    with open(os.path.join(rd, "sessions.jsonl"), "w") as f:
                        for r in rows:
                            r["cost_usd"] *= 2
                            f.write(json.dumps(r) + "\n")
                    res = preflight.check(rd, projects, mcp=False)
                finally:
                    shutil.rmtree(d)
            else:
                res = self.run_check(**{flaw: True})
            self.assertEqual(self.status(res, word), "FAIL", (flaw, word))

    def test_cost_ratio_is_printed(self):
        res = self.run_check()
        line = [x for x in res if "against the cost.py estimate" in x[0]][0]
        self.assertIn("ratio 1.00", line[2])

    def test_mcp_probe_with_a_fake_server(self):
        import preflight
        d = tempfile.mkdtemp()
        try:
            good, bad = os.path.join(d, "good.py"), os.path.join(d, "bad.py")
            fake_mcp_server(good)
            fake_mcp_server(bad, tools=("search_earlier_chat",))
            self.assertTrue(preflight.mcp_probe(good, d, d)[0])
            self.assertFalse(preflight.mcp_probe(bad, d, d)[0])
            self.assertFalse(preflight.mcp_probe(os.path.join(d, "none.py"), d, d)[0])
        finally:
            shutil.rmtree(d)

    def test_exit_code_and_output(self):
        import preflight, io, contextlib
        d = tempfile.mkdtemp()
        try:
            rd, projects = make_pf_run(d)
            old = runner.PROJECTS
            runner.PROJECTS = projects
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = preflight.main([rd, "--no-mcp"])
            self.assertEqual(rc, 0)
            self.assertIn("RESULT: PASS", buf.getvalue())
            rd2, projects2 = make_pf_run(os.path.join(d, "two"), bad_parse=True)
            runner.PROJECTS = projects2
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = preflight.main([rd2, "--no-mcp"])
            self.assertEqual(rc, 1)
            self.assertIn("FAIL", buf.getvalue())
        finally:
            runner.PROJECTS = old
            shutil.rmtree(d)

    def test_preflight_preset_and_cost(self):
        k = runner.KINDS["preflight"]
        self.assertEqual((k["model"], k["window"], k["seeds"], k["arms"], k["types"], k["n"]), ("haiku", 30000, 1, "B,R,K", "R,X", [1]))
        self.assertEqual((k["conditions"], k["phrasings"]), ("compact,nocompact", "cued,neutral"))
        total, lines = cost.estimate_preflight()
        self.assertEqual(len(lines), 6)
        self.assertLess(total, 3.0)
        self.assertGreater(total, 1.0)
        self.assertIn("preflight", open(os.path.join(HERE, "run.sh")).read())


class AbortGates(unittest.TestCase):
    def ok_inputs(self):
        sess = [{"name": "s1-B", "seed": 1, "status": "done"}, {"name": "s1-R", "seed": 1, "status": "done"}]
        rows = [{"seed": 1, "arm": "R", "phrasing": "cued", "auto_recall_fired": True, "parse_ok": True, "type": "R", "fork_sid": "f"}] * 10
        return sess, rows

    def test_no_gate_when_all_is_well(self):
        sess, rows = self.ok_inputs()
        self.assertEqual(gates.abort_reasons(sess, rows, 10.0, [1], {"s1-B": 5.0, "s1-R": 5.0}), [])
        self.assertEqual(gates.abort_reasons(sess, rows, 10.0, [], {}), [])

    def test_invalid_session_rate_above_10_percent(self):
        sess, rows = self.ok_inputs()
        sess += [{"name": "s1-A", "seed": 1, "status": "invalid"}] + [{"name": "s1-x%d" % i, "seed": 1, "status": "done"} for i in range(7)]
        self.assertEqual(gates.abort_reasons(sess, rows, None, [1]), [])        # 1 of 10 = 10%, not above
        sess.append({"name": "s1-nc", "seed": 1, "status": "done", "nc_valid": False})
        why = gates.abort_reasons(sess, rows, None, [1])
        self.assertTrue(any("invalid-session rate" in w for w in why), why)

    def test_cost_above_1_3_times_the_estimate_so_far(self):
        sess, rows = self.ok_inputs()
        self.assertEqual(gates.abort_reasons(sess, rows, 10.0, [1], {"a": 13.0}), [])
        why = gates.abort_reasons(sess, rows, 10.0, [1], {"a": 13.5})
        self.assertTrue(any("1.3 x the estimate" in w for w in why), why)
        self.assertEqual(gates.abort_reasons(sess, rows, None, [1], {"a": 999.0}), [])    # no estimate (slice): no cost gate
        self.assertEqual(gates.abort_reasons(sess, rows, 10.0, [1, 2], {"a": 25.0}), [])  # the estimate grows with the finished seeds

    def test_arm_r_cued_probe_without_a_fire(self):
        sess, rows = self.ok_inputs()
        rows.append({"seed": 1, "arm": "K", "phrasing": "cued", "auto_recall_fired": False, "parse_ok": True, "type": "X", "fork_sid": "g"})
        self.assertTrue(any("no auto-recall fire" in w for w in gates.abort_reasons(sess, rows, None, [1])))
        rows[-1] = {"seed": 1, "arm": "R", "phrasing": "neutral", "auto_recall_fired": False, "parse_ok": True, "type": "X", "fork_sid": "g"}
        self.assertEqual(gates.abort_reasons(sess, rows, None, [1]), [])          # a neutral probe is not supposed to fire

    def test_parse_error_rate_above_2_percent(self):
        sess, rows = self.ok_inputs()
        rows = [dict(rows[0]) for _ in range(100)]
        rows[0]["parse_ok"] = rows[1]["parse_ok"] = False                          # 2 of 100: not above
        self.assertEqual(gates.abort_reasons(sess, rows, None, [1]), [])
        rows[2]["parse_ok"] = False
        self.assertTrue(any("parse error rate" in w for w in gates.abort_reasons(sess, rows, None, [1])))
        z = [{"seed": 1, "arm": "Z", "parse_ok": False}] * 50
        self.assertEqual(gates.abort_reasons(sess, [dict(rows[5]) for _ in range(100)] + z, None, [1]), [])      # arm Z may answer in prose

    def test_runner_stops_after_a_seed_and_resumes(self):
        d = tempfile.mkdtemp()
        try:
            cfg = {"run_id": "g", "kind": "full", "arms": ["B"], "seeds": [1, 2], "n": [1, 3], "conditions": ["compact"], "phrasings": ["cued"], "gates": True}
            ctx = runner.Ctx(d, cfg)
            for n_ in ("s1-B", "s2-B"):
                ctx.sess(n_)
            with open(os.path.join(d, "results.jsonl"), "w") as f:
                for i in range(50):
                    f.write(json.dumps({"seed": 1, "arm": "B", "phrasing": "cued", "parse_ok": i > 10, "type": "R", "fork_sid": "x"}) + "\n")
            with open(os.path.join(d, "sessions.jsonl"), "w") as f:
                f.write(json.dumps({"name": "s1-B", "seed": 1, "status": "done"}) + "\n")
            calls = []

            def stub(ctx_, seed, arm, cond):
                calls.append(seed)
                ctx_.state["sessions"]["s%d-%s" % (seed, arm)]["status"] = "done"
            old = runner.run_unit
            runner.run_unit = stub
            try:
                runner.run_unit_gated(ctx, 1, "B", "compact")
            finally:
                runner.run_unit = old
            self.assertTrue(ctx.stop.is_set())
            self.assertTrue(any("parse error rate" in w for w in ctx.state["gate_abort"]))
            self.assertEqual(runner.finished_seeds(ctx), [1])                        # seed 1 is finished: resume starts at seed 2
            # resume: the finished session is not run again, the gate is cleared, --ignore-gates switches the gates off
            ctx2 = runner.Ctx(d, cfg)
            self.assertEqual(ctx2.state["sessions"]["s1-B"]["status"], "done")
            self.assertEqual([u for u in runner.work_units(cfg) if u[0] == 2], [(2, "B", "compact")])
        finally:
            shutil.rmtree(d)

    def test_every_preset_has_gates_on_and_the_estimate_rule(self):
        for name, k in runner.KINDS.items():
            cfg = {"kind": name, "arms": k["arms"].split(","), "n": k["n"], "conditions": k.get("conditions", "compact").split(","),
                   "phrasings": k.get("phrasings", "cued").split(","), "probe_design": k.get("probe_design", 1)}
            est = cost.estimate_per_seed(cfg)
            if name == "slice":
                self.assertIsNone(est)
            else:
                self.assertGreater(est, 0, name)
        src = open(os.path.join(HERE, "runner.py"), encoding="utf-8").read()
        self.assertIn("ex.submit(run_unit_gated", src)
        self.assertIn("--ignore-gates", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
