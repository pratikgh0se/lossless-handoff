#!/usr/bin/env python3
"""Self-test of probe design 4 (update chains with interference). No model, no network, no claude process.
Run: python3 bench/continuity/test_design4.py"""
import time, hashlib, json, os, re, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cost, design4 as d4, gates, gen, grade, litm4, runner   # noqa: E402
import test_litm                                                  # noqa: E402

SEEDS = list(range(1000, 1100))        # 100 seeds
_CACHE = {}


def script(seed):
    if seed not in _CACHE:
        _CACHE[seed] = d4.build_litm4(seed)
    return _CACHE[seed]


def ops(*pairs):
    return [{"op": o, "value": v} for o, v in pairs]


class Interpreter(unittest.TestCase):
    def test_each_op(self):
        r = d4.interpret(ops(("set", "A"), ("change", "B"), ("change", "C")))
        self.assertEqual((r["current"], r["previous"], r["replaced"]), ("C", "B", ["A", "B"]))
        r = d4.interpret(ops(("set", "A"), ("change", "B"), ("reject", "X"), ("scoped", "Y")))
        self.assertEqual((r["current"], r["previous"], r["replaced"]), ("B", "A", ["A"]))       # reject and scoped change nothing
        r = d4.interpret(ops(("set", "A"), ("change", "B"), ("change", "C"), ("undo", None)))
        self.assertEqual((r["current"], r["previous"], r["replaced"]), ("B", "C", ["A", "C"]))  # the undone value is replaced and is the previous
        r = d4.interpret(ops(("set", "A"), ("change", "B"), ("undo", None), ("change", "C")))
        self.assertEqual((r["current"], r["previous"], r["replaced"]), ("C", "A", ["A", "B"]))  # undo in the middle
        r = d4.interpret(ops(("set", "A"), ("change", "B"), ("change", "C"), ("undo", None), ("undo", None)))
        self.assertEqual(r["current"], "A")                                                      # undo twice walks back

    def test_undo_without_change_is_an_error(self):
        with self.assertRaises(ValueError):
            d4.interpret(ops(("set", "A"), ("undo", None)))
        with self.assertRaises(ValueError):
            d4.interpret(ops(("set", "A"), ("bogus", "B")))

    def test_statement_text_roundtrip_for_both_attributes(self):
        for attr in ("queue", "tag"):
            for op, v in (("set", "AAAA-1111-BBBB"), ("change", "CCCC-2222-DDDD"), ("reject", "EEEE-3333-FFFF"), ("undo", None), ("scoped", "GGGG-4444-HHHH")):
                t = d4.stmt_text(op, "invoice-mailer", attr, v)
                got = d4.parse_turn("x Note for this project: %s Reply only `ok`." % t)
                self.assertEqual(got, [(op, "invoice-mailer", v)], (attr, op))
        self.assertIn("TAG-AAAA-1111-BBBB", d4.stmt_text("set", "k", "tag", "AAAA-1111-BBBB"))


class Structure(unittest.TestCase):
    def test_counts_for_100_seeds(self):
        for sd in SEEDS:
            sc = script(sd)
            plan = sc["plan"]
            self.assertEqual(len(plan), 105, sd)
            self.assertEqual(sum(1 for s in plan if s["kind"] == "fill"), 36)
            self.assertEqual(sc["n_facts"], 39)
            self.assertEqual(sum(len(i["stmts"]) for lv in sc["items"].values() for i in lv), 276)
            for th in range(3):
                facts = [s for s in plan if s["kind"] == "fact" and s["third"] == th]
                self.assertEqual(len(facts), 23)
                self.assertEqual(sum(1 for s in plan if s["kind"] == "fill" and s["third"] == th), 12)
                self.assertTrue(all(len(d4.parse_turn(s["prompt"])) == 4 for s in facts), (sd, th))
            for lv, spec in d4.LEVELS.items():
                if lv in sc["items"]:
                    self.assertEqual(len(sc["items"][lv]), 3 * spec["chains"])
                    for th in range(3):
                        self.assertEqual(sum(1 for i in sc["items"][lv] if i["third"] == th), spec["chains"])
            o_plan = [s for s in plan if s["kind"] != "fill"]
            self.assertEqual(len(o_plan), 69)        # arm O: plant turns only

    def test_chains_are_tight_ordered_and_one_key_per_turn(self):
        for sd in SEEDS[:30]:
            sc = script(sd)
            where = {}
            for step in sc["plan"]:
                if step["kind"] == "fill":
                    continue
                keys = [k for _o, k, _v in d4.parse_turn(step["prompt"])]
                self.assertLessEqual(max(keys.count(k) for k in keys), 2, (sd, "a key more than twice in a turn"))      # a burst has 2
                for k in sorted(set(keys)):
                    where.setdefault(k, []).append(step)
            for lv in sc["items"].values():
                for it in lv:
                    steps = where[it["key"]]
                    self.assertEqual({s["third"] for s in steps}, {it["third"]})                      # tight: one third
                    self.assertEqual(it["position"], d4.POSITIONS[it["third"]])
                    got = [(o, v) for s in steps for o, k, v in d4.parse_turn(s["prompt"]) if k == it["key"]]
                    self.assertEqual(got, [(o["op"], o["value"]) for o in it["ops"]], (sd, it["key"]))   # order inside the chain is kept
                    self.assertEqual(it["turn"], steps[-1]["turn"])

    def test_level_rules(self):
        for sd in SEEDS:
            sc = script(sd)
            fam = {}
            for lv, spec in d4.LEVELS.items():
                for it in sc["items"].get(lv, []):
                    fam.setdefault(it["family"], []).append(it)
                    kinds = [o["op"] for o in it["ops"]]
                    self.assertEqual(len(it["values"]) - 0, spec["k"], (lv, it["key"]))                  # k values were in force
                    self.assertEqual(kinds.count("reject"), spec["rejects"])
                    self.assertEqual(kinds.count("scoped"), spec["scoped"])
                    self.assertEqual(kinds.count("undo"), 1 if spec["undo"] else 0)
                    last_update = max(i for i, o in enumerate(kinds) if o in ("set", "change", "undo"))
                    self.assertEqual(kinds[0], "set")                                                    # a lure never comes before the first set
                    self.assertEqual(sum(1 for o in kinds if o in ("reject", "scoped")), spec["rejects"] + spec["scoped"])
                    if spec["undo"]:
                        self.assertEqual(kinds[last_update] == "undo", it["undo_last"])
                    self.assertNotIn(it["current"], it["lures"])
                    if spec["prefix"]:
                        allv = it["values"] + it["lures"]
                        self.assertEqual({v[:9] for v in allv}, {allv[0][:9]})
            for fid, members in fam.items():
                lv = members[0]["level"]
                self.assertEqual(len(members), d4.LEVELS[lv]["fam"])
                self.assertEqual(len({m["third"] for m in members}), 1)                                   # a family sits in one third
                if d4.LEVELS[lv]["prefix"]:                                                               # look-alike values: first 9 characters shared
                    self.assertEqual(len({v[:9] for m in members for v in m["values"] + m["lures"]}), 1)
            allvals = [v for lv in sc["items"].values() for it in lv for v in it["values"] + it["lures"]]
            self.assertEqual(len(allvals), len(set(allvals)))
        for sd in SEEDS[:40]:       # undo is last in half of the L3 and L4 chains
            for lv in ("L3", "L4"):
                n = sum(1 for i in script(sd)["items"][lv] if i["undo_last"])
                self.assertIn(n, (4, 5), (sd, lv, n))

    def test_l5_exists_for_the_escalation(self):
        sc = d4.build_litm4(7, levels=d4.D4_LEVELS + ("L5",))
        self.assertEqual(len(sc["items"]["L5"]), 12)
        self.assertEqual(len(sc["items"]["L5"][0]["values"]), 16)
        self.assertEqual(sc["items"]["L5"][0]["family"].count("L5"), 1)
        self.assertEqual(d4.check_script(sc), [])

    def test_interference_attribute_is_half_queue_half_tag(self):
        sc = script(5)
        attrs = [i["attr"] for lv in sc["items"].values() for i in lv]
        self.assertTrue(10 <= attrs.count("tag") <= 29)
        self.assertEqual(set(attrs), {"queue", "tag"})


class FullInformationSolver(unittest.TestCase):
    def test_solver_reaches_100_percent_from_the_plant_text_only(self):
        for sd in SEEDS:
            sc = script(sd)
            full = d4.solve_full(sc)
            for lv in sc["items"].values():
                for it in lv:
                    r = full[it["key"]]
                    self.assertEqual((r["current"], r["previous"], r["replaced"]), (it["current"], it["previous"], it["replaced"]), (sd, it["key"]))

    def test_perfect_reply_scores_m3_for_every_level(self):
        sc = script(3)
        for lv in d4.D4_LEVELS:
            items = sc["items"][lv]
            reply = json.dumps({"answers": [{"id": i["id"], "current": d4.val_text(i["attr"], i["current"]), "previous": i["previous"], "replaced": i["replaced"]} for i in items]})
            g = d4.grade_level(items, "```json\n%s\n```" % reply, sc)
            self.assertTrue(all(x["m1"] and x["m2"] and x["m3"] and x["correct"] and x["error_class"] is None for x in g), lv)

    def test_naive_last_mention_solver_stays_low_at_l3_and_l4(self):
        for sd in SEEDS:
            sc = script(sd)
            self.assertLessEqual(d4.last_mention_rate(sc, "L3"), d4.SOLVER_MAX, sd)
            self.assertLessEqual(d4.last_mention_rate(sc, "L4"), d4.SOLVER_MAX, sd)
            self.assertEqual(d4.last_mention_rate(sc, "L1"), 1.0)         # no lures at L1: the naive solver is right there


class Lures(unittest.TestCase):
    def setUp(self):
        self.sc = script(11)
        self.it = next(i for i in self.sc["items"]["L4"] if i["undo_last"])
        self.items = [self.it]

    def grade_current(self, cur, **kw):
        e = {"id": self.it["id"], "current": cur, "previous": kw.get("previous", self.it["previous"]), "replaced": kw.get("replaced", self.it["replaced"])}
        return d4.grade_level(self.items, json.dumps({"answers": [e]}), self.sc)[0]

    def test_right_answer_and_metrics(self):
        g = self.grade_current(self.it["current"])
        self.assertEqual((g["m1"], g["m2"], g["m3"], g["error_class"]), (True, True, True, None))
        g = self.grade_current(self.it["current"], previous="WRONG")
        self.assertEqual((g["m1"], g["m2"], g["m3"]), (True, False, False))
        g = self.grade_current(self.it["current"], replaced=self.it["replaced"][:-1])
        self.assertEqual((g["m1"], g["m2"], g["m3"]), (True, True, False))
        g = self.grade_current(self.it["current"], replaced=list(reversed(self.it["replaced"])) + self.it["replaced"][:1])    # set equality: order and duplicates do not matter
        self.assertTrue(g["m3"])
        g = self.grade_current("TAG-" + self.it["current"])           # TAG- prefix is accepted
        self.assertTrue(g["m1"])
        g = self.grade_current("`%s`" % self.it["current"])
        self.assertTrue(g["m1"])

    def test_stale_with_lag(self):
        v = self.it["values"]
        g = self.grade_current(v[0])
        self.assertEqual(g["error_class"], "stale")
        self.assertEqual(g["lag"], sum(1 for o in self.it["ops"] if o["op"] in ("set", "change", "undo")) - 1)    # updates after it was put in force
        self.assertFalse(g["m1"])
        g = self.grade_current(self.it["replaced"][-1])
        self.assertEqual(g["error_class"], "stale")

    def test_lure(self):
        g = self.grade_current(self.it["lures"][0])
        self.assertEqual(g["error_class"], "lure")

    def test_sibling_and_other_chain(self):
        sib = next(o for o in self.sc["items"]["L4"] if o is not self.it and o["family"] == self.it["family"])
        self.assertEqual(self.grade_current(sib["current"])["error_class"], "sibling")
        other = next(o for o in self.sc["items"]["L1"])
        self.assertEqual(self.grade_current(other["current"])["error_class"], "other_chain")

    def test_abstain_novel_unreadable(self):
        g = self.grade_current(None)
        self.assertEqual((g["abstained"], g["error_class"], g["m1"], g["parse_ok"]), (True, "abstain", False, True))
        self.assertEqual(self.grade_current("ZZZZ-0000-ZZZZ")["error_class"], "novel")
        g = d4.grade_level(self.items, "I do not know", self.sc)[0]
        self.assertEqual((g["error_class"], g["m1"], g["parse_ok"], g["abstained"]), ("unreadable", False, False, False))
        g = d4.grade_level(self.items, json.dumps({"answers": [{"id": 999, "current": "x"}]}), self.sc)[0]
        self.assertEqual(g["error_class"], "unreadable")

    def test_every_class_is_possible_in_a_real_script_for_100_seeds(self):
        seen = set()
        for sd in SEEDS:
            sc = script(sd)
            it = sc["items"]["L4"][0]
            seen.add(d4.classify(it, it["lures"][0], sc)[0])
            seen.add(d4.classify(it, it["replaced"][0], sc)[0])
            seen.add(d4.classify(it, "ZZZZ-1-ZZZZ", sc)[0])
        self.assertEqual(seen, {"lure", "stale", "novel"})


class NonLiteral(unittest.TestCase):
    def test_zero_overlap_and_unique_descriptions_for_100_seeds(self):
        for sd in SEEDS:
            self.assertEqual(d4.check_script(script(sd)), [], sd)

    def test_every_lexicon_combination_has_zero_overlap(self):
        for base, bdesc in d4.BASES:
            for mod, mdesc in [("", d4.BASE_MEMBER)] + d4.MODS:
                key = base + ("-" + mod if mod and mod != "" and (mod, mdesc) in d4.MODS else "")
                desc = d4.describe(bdesc, mdesc)
                self.assertEqual(d4.overlap(key, desc), set(), (key, desc))
        self.assertEqual(len({b for b, _ in d4.BASES}), len(d4.BASES))
        self.assertGreaterEqual(len(d4.BASES), 21)

    def test_overlap_rule_catches_words_and_stems(self):
        self.assertEqual(d4.overlap("invoice-mailer-eu", "the service that sends bills to customers in the European region"), set())
        self.assertTrue(d4.overlap("backup-runner-nightly", "the task that runs each night"))     # stem night
        self.assertTrue(d4.overlap("invoice-mailer", "the invoices of a shop"))                   # stem invoi
        self.assertTrue(d4.overlap("photo-resizer", "a photo tool"))                              # same word

    def test_probe_text_uses_the_description_only(self):
        sc = script(2)
        for lv in d4.D4_LEVELS:
            p = d4.probe_prompt(lv, sc, "M-1", "nonliteral")
            for it in sc["items"][lv]:
                self.assertNotIn(it["key"], p)
                self.assertIn(it["desc"], p)
                self.assertIn("%s setting" % it["attr"], p)             # the attribute word stays
            self.assertNotIn("glossary", p.lower())
            self.assertFalse(gen.has_cue(p), lv)
            self.assertFalse(gen.has_cue(d4.probe_prompt(lv, sc, "M-1", "neutral")))
            cued = d4.probe_prompt(lv, sc, "M-1", "cued")
            self.assertTrue(gen.has_cue(cued))
            self.assertIn("Give the value before it and all replaced values.", cued)
            self.assertIn(gen.CUED_HEAD_TAIL, cued)
            self.assertIn('"current":"...","previous":"...","replaced":["..."]', cued)

    def test_no_glossary_turn_in_the_plan(self):
        for step in script(4)["plan"]:
            self.assertNotIn("Word list", step.get("prompt", ""))
            self.assertNotIn("is the one called", step.get("prompt", ""))


class Gates(unittest.TestCase):
    def rows(self, **over):
        rows = []
        for arm in ("A", "O", "Z"):
            for lv in d4.D4_LEVELS:
                for ph in d4.PHRASINGS:
                    for pos in ("start", "middle", "end"):
                        for i in range(4):
                            ok = {"A": i < 2, "O": True, "Z": False}[arm]
                            r = {"arm": arm, "type": lv, "phrasing": ph, "position": pos, "condition": "nocompact", "m1": ok, "m2": ok, "m3": ok, "seed": 1}
                            r.update(over.get((arm, lv, ph), {}))
                            rows.append(r)
        return rows

    def status(self, lines, word):
        got = [s for n, s, _d in lines if word in n]
        self.assertEqual(len(got), 1, (word, got))
        return got[0]

    def test_clean_gates_pass(self):
        lines = d4.gate_lines(self.rows(), [script(1)])
        self.assertEqual(self.status(lines, "oracle O"), "PASS")
        self.assertEqual(self.status(lines, "amnesia Z"), "PASS")
        self.assertEqual(self.status(lines, "last mention wins"), "PASS")

    def test_oracle_below_limit_fails_and_names_the_cell(self):
        bad = {("O", "L3", "neutral"): {"m3": False}}
        rows = [dict(r, m3=False) if (r["arm"], r["type"], r["phrasing"]) == ("O", "L3", "neutral") and r["position"] == "start" else r for r in self.rows()]
        lines = d4.gate_lines(rows, [])
        self.assertEqual(self.status(lines, "oracle O"), "FAIL")
        self.assertIn("L3 neutral M3", [d for n, s, d in lines if "oracle O" in n][0])
        _ = bad

    def test_amnesia_above_two_percent_fails(self):
        rows = self.rows()
        rows[[i for i, r in enumerate(rows) if r["arm"] == "Z"][0]]["m1"] = True       # 1 of 144 per level and phrasing is below 2%? one cell has 12 rows
        lines = d4.gate_lines(rows, [])
        self.assertEqual(self.status(lines, "amnesia Z"), "FAIL")

    def test_solver_gate_fails_for_a_weak_script(self):
        sc = json.loads(json.dumps(script(1)))
        for it in sc["items"]["L3"]:          # make the last mention the right one: the lures are too weak
            it["current"] = d4.last_mention_answers(sc)[it["key"]]
        self.assertEqual(self.status(d4.gate_lines([], [sc]), "last mention wins"), "FAIL")

    def test_ceiling_and_floor(self):
        rows = [dict(r, m1=True) if r["arm"] == "A" and r["type"] == "L1" and r["phrasing"] == "cued" else r for r in self.rows()]
        rows = [dict(r, m1=False) if r["arm"] == "A" and r["type"] == "L2" and r["phrasing"] == "cued" else r for r in rows]
        lines = d4.gate_lines(rows, [])
        self.assertIn("TOO EASY", " ".join(d for n, s, d in lines if "L1 cued" in n))
        self.assertIn("TOO HARD", " ".join(d for n, s, d in lines if "L2 cued" in n))

    def test_error_mix(self):
        rows = [{"arm": "A", "type": "L3", "condition": "nocompact", "error_class": c} for c in ("stale", "stale", "lure", "abstain", None)]
        mix = d4.error_mix(rows, "A")
        self.assertEqual((mix["stale"], mix["lure"], mix["abstain"], mix["sibling"]), (2, 1, 1, 0))


class PickRule(unittest.TestCase):
    def rows(self, a, o=1.0, z=0.0, metric="m1", levels=d4.D4_LEVELS, n=20):
        """a, o, z: dict level -> rate, or a number for all levels. Rows carry m1, m2, m3 with the given rate for the metric."""
        rows = []
        for arm, spec in (("A", a), ("O", o), ("Z", z)):
            for lv in levels:
                rate = spec[lv] if isinstance(spec, dict) else spec
                for i in range(n):
                    ok = i < round(rate * n)
                    r = {"arm": arm, "type": lv, "phrasing": "cued", "condition": "nocompact", "m1": ok, "m2": ok, "m3": ok}
                    rows.append(r)
        return rows

    def test_first_cell_in_band_in_metric_then_level_order(self):
        d = d4.decide(self.rows({"L1": 0.95, "L2": 0.9, "L3": 0.7, "L4": 0.6}))
        self.assertEqual((d["decision"], d["pick"]), ("pick", ("m1", "L3")))

    def test_band_edges(self):
        self.assertEqual(d4.decide(self.rows({"L1": 0.55, "L2": 0.9, "L3": 0.9, "L4": 0.9}))["pick"], ("m1", "L1"))
        self.assertEqual(d4.decide(self.rows({"L1": 0.80, "L2": 0.9, "L3": 0.9, "L4": 0.9}))["pick"], ("m1", "L1"))
        self.assertEqual(d4.decide(self.rows({"L1": 0.85, "L2": 0.9, "L3": 0.9, "L4": 0.9}))["decision"], "add_l5")

    def test_o_below_limit_is_not_a_pick_and_is_reported(self):
        d = d4.decide(self.rows({"L1": 0.9, "L2": 0.9, "L3": 0.7, "L4": 0.9}, o={"L1": 1, "L2": 1, "L3": 0.8, "L4": 1}))
        self.assertEqual(d["decision"], "none")
        self.assertTrue(any("interference at zero distance" in n for n in d["notes"]))

    def test_z_above_limit_blocks_a_pick(self):
        d = d4.decide(self.rows({"L1": 0.9, "L2": 0.9, "L3": 0.7, "L4": 0.9}, z=0.1))
        self.assertEqual(d["decision"], "none")

    def test_above_80_everywhere_adds_l5_once_then_stops(self):
        d = d4.decide(self.rows(0.95))
        self.assertEqual(d["decision"], "add_l5")
        d = d4.decide(self.rows(0.95, levels=d4.D4_LEVELS + ("L5",)), levels=d4.D4_LEVELS + ("L5",))
        self.assertEqual(d["decision"], "no_loss")
        self.assertTrue(any("no loss" in n for n in d["notes"]))
        self.assertEqual(d4.decide(self.rows(0.95), l5_done=True)["decision"], "no_loss")

    def test_below_band_everywhere_picks_nothing(self):
        d = d4.decide(self.rows(0.2))
        self.assertEqual(d["decision"], "none")

    def test_second_seed_rule(self):
        self.assertTrue(d4.decide(self.rows({"L1": 1.0, "L2": 0.95, "L3": 0.5, "L4": 0.1}))["seed2"])
        self.assertFalse(d4.decide(self.rows({"L1": 1.0, "L2": 0.95, "L3": 1.0, "L4": 0.2}))["seed2"])
        self.assertIn("seed 2 needed", d4.describe_decision(d4.decide(self.rows(0.7))))

    def test_m2_is_used_when_m1_has_no_pick(self):
        rows2 = self.rows(0.95)               # M1 of A is above 80% everywhere, so M1 has no pick
        for i, r in enumerate([r for r in rows2 if r["arm"] == "A" and r["type"] == "L2"]):
            r["m2"] = i < 14                  # M2 of A at L2 is 70%
        d = d4.decide(rows2)
        self.assertEqual(d["pick"], ("m2", "L2"))


class Plumbing(unittest.TestCase):
    def test_presets_and_types(self):
        for k in ("litm4-calibrate", "litm4-smoke", "litm4-pilot", "litm4-full-v3", "preflight4"):
            self.assertEqual(runner.KINDS[k]["probe_design"], 4, k)
        c = runner.KINDS["litm4-calibrate"]
        self.assertEqual((c["arms"], c["model"], c["conditions"]), ("A,O,Z", "sonnet", "nocompact"))
        self.assertEqual(runner.KINDS["litm4-full-v3"]["n_by_arm"], {"R": [1, 3, 10], "K": [1, 3, 10]})
        self.assertEqual(runner.nc_types({"probe_design": 4}), ["L1", "L2", "L3", "L4"])
        self.assertEqual(runner.nc_types({"probe_design": 3}), list(gen.L3_TYPES))
        cfg = {"probe_design": 4, "phrasings": ["cued", "neutral", "nonliteral"], "arms": ["A", "O", "Z"], "seeds": [1], "conditions": ["nocompact"], "types": ["R"]}
        self.assertEqual(runner.probe_plan(cfg, "L3", d3=True), ["cued", "neutral", "nonliteral"])
        ex = runner.expected_forks(cfg)["exact"]
        self.assertEqual(len(ex), 3 * 4 * 3)
        self.assertIn((1, "A", "nocompact", "nonliteral", "L4", 0), ex)
        self.assertEqual(open(os.path.join(HERE, "seeds-dev-2.txt")).read().split(), ["2"])

    def test_nc3_plan_for_design_4_fits_the_window(self):
        lines, est = runner.nc3_plan(400000, 4)
        self.assertGreater(lines, 100)
        self.assertLess(est, 0.9 * 400000)
        self.assertGreater(est, 0.7 * 400000)

    def test_designs_2_and_3_are_unchanged(self):
        want3 = {1: "a5b6f32d32a9f93c", 2: "866536eece173cde", 3: "97e36c8a98f771ae"}
        want2 = {1: "29f6dbc177de8cda", 2: "0e78f90b6eda46bf", 3: "d6e02369f78063b9"}
        for sd in (1, 2, 3):
            self.assertEqual(hashlib.sha256(json.dumps(gen.build_litm3(sd), sort_keys=True).encode()).hexdigest()[:16], want3[sd])
            self.assertEqual(hashlib.sha256(json.dumps(gen.build(sd), sort_keys=True).encode()).hexdigest()[:16], want2[sd])

    def test_thresholds_register_h8_to_h11_and_an_empty_pick(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        d = th["litm"]["design4"]
        self.assertEqual((d["h8_dose_min_points"], d["h9_interference_min_share"], d["h10_length_min_points"]), (15, 0.60, 15))
        self.assertIn("lower bound of the interval is above 0", d["h11_note"])
        self.assertEqual(d["pick"]["metric"], None)
        self.assertEqual(d["gates"]["o_min_accuracy"], 0.95)
        self.assertEqual((d["chains_per_seed"], d["statements_per_seed"]), (39, 276))
        self.assertIn("H1 and its secondary forms use the primary cell", d["kept"])
        self.assertEqual(litm4.pick_cell(th), (("m1", "L3"), False))
        th["litm"]["design4"]["pick"] = {"metric": "M2", "level": "L2"}
        self.assertEqual(litm4.pick_cell(th), (("m2", "L2"), True))

    def test_calibration_cost_uses_the_real_smoke_figures(self):
        self.assertEqual(cost.SMOKE_REAL["main_nc_75_turns"], 3.055)
        self.assertAlmostEqual(cost.SMOKE_REAL["forks_15"]["A"], 0.671)
        self.assertAlmostEqual(cost.d4_nc_main(), 4.28, delta=0.05)
        tot, _l, per = cost.estimate3(2, ["A", "O", "Z"], [1], ["nocompact"], design=4)
        self.assertTrue(13 < tot < 19, tot)
        self.assertTrue(7.0 < per < 9.0, per)
        self.assertEqual(cost.D4_CONTROL_USD["O"], 1.40)       # arm O: $0.9 plus the extra forks
        smoke = cost.estimate3(1, ["A", "B", "R", "K"], [1, 3], ["compact", "nocompact"], design=4)[0]
        smoke3 = cost.estimate3(1, ["A", "B", "R", "K"], [1, 3], ["compact", "nocompact"], design=3)[0]
        self.assertGreater(smoke, smoke3)
        self.assertEqual(cost.estimate_per_seed({"kind": "litm4-calibrate", "arms": ["A", "O", "Z"], "n": [1], "conditions": ["nocompact"],
                                                 "phrasings": ["cued"], "probe_design": 4}), per)


class ReportAndPreflight(unittest.TestCase):
    def synth_rows(self):
        rows, n = [], 0
        for arm in ("A", "B", "R", "K", "O", "Z"):
            for lv in d4.D4_LEVELS:
                for ph in d4.PHRASINGS:
                    for pos in ("start", "middle", "end"):
                        for i in range(3):
                            n += 1
                            ok = arm == "O" or (arm != "Z" and (i + n) % 3 != 0)
                            cls = None if ok else ("stale", "lure", "sibling", "abstain")[n % 4]
                            rows.append({"seed": 1, "arm": arm, "type": lv, "phrasing": ph, "position": pos, "condition": "nocompact", "n_target": 0,
                                         "m1": ok, "m2": ok, "m3": ok, "correct": ok, "error_class": cls, "lag": 2 if cls == "stale" else None,
                                         "used_lookup": arm == "K" and ph == "neutral", "abstained": cls == "abstain"})
        return rows

    def test_section4_has_every_table_and_hypothesis(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        md, out = litm4.section4(self.synth_rows(), {}, th, 200)
        text = "\n".join(md)
        for word in ("Table D4-1 (A)", "Table D4-1 (K)", "Table D4-2", "Table D4-3", "Table D4-4", "provisional", "H1 lost in the middle", "H1n", "H8 dose", "H9 interference",
                     "H10 length", "H6 search rate", "H7 accuracy", "H11 harm guard", "mean stale lag"):
            self.assertIn(word, text, word)
        self.assertEqual(len([h for h in out["hypotheses"]]), 10)
        th["litm"]["design4"]["pick"] = {"metric": "M1", "level": "L2"}
        md2, out2 = litm4.section4(self.synth_rows(), {}, th, 200)
        self.assertIn("Primary cell: **M1 L2**.", "\n".join(md2))
        self.assertTrue(out2["committed"])

    def test_h11_flags_a_harm(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        rows = [r for r in self.synth_rows() if r["arm"] in ("B", "R", "A")]
        for r in rows:
            if r["arm"] == "R" and r["phrasing"] == "cued":
                r.update(m1=False, correct=False, error_class="stale")
            if r["arm"] == "B" and r["phrasing"] == "cued":
                r.update(m1=True, correct=True, error_class=None)
        md, out = litm4.section4(rows, {}, th, 200)
        h11 = [h for h in out["hypotheses"] if h["name"].startswith("H11")][0]
        self.assertTrue(h11["result"].startswith("HARM"))

    def test_report_skips_design3_rows_of_design_4(self):
        r = {"type": "L2", "condition": "nocompact"}
        self.assertTrue(litm4.is_d4(r))
        self.assertFalse(litm4.is_d4({"type": "R", "condition": "nocompact"}))
        self.assertFalse(litm4.is_d4({"type": "L2", "condition": "compact"}))

    def run_check(self, **flaws):
        import preflight
        d = tempfile.mkdtemp()
        try:
            rd, projects = test_litm.make_pf_run(d, d4=True, **flaws)
            return preflight.check(rd, projects, mcp=False)
        finally:
            shutil.rmtree(d)

    def test_preflight_with_design_4_passes_its_lines(self):
        res = self.run_check()
        bad = [x for x in res if x[1] == "FAIL"]
        self.assertEqual(bad, [], bad)
        names = [x[0] for x in res]
        for word in ("scripts are sound", "solver gate", "oracle (arm O) M1", "amnesia (arm Z)"):
            self.assertTrue(any(word in n for n in names), word)

    def test_preflight_design_4_fails_when_the_oracle_or_amnesia_is_wrong(self):
        res = self.run_check(d4_o_low=True)
        # a low oracle at L3 is INFO in the Haiku preflight (Haiku capability): the gate of L1 and L2 stays PASS and the INFO line shows L3
        self.assertEqual([x[1] for x in res if "oracle (arm O) M1" in x[0]], ["PASS", "INFO"])
        self.assertIn("L3", [x[2] for x in res if "oracle (arm O) M1" in x[0]][1])
        res = self.run_check(d4_z_high=True)
        self.assertEqual([x[1] for x in res if "amnesia (arm Z)" in x[0]], ["FAIL"])


class PreflightHaikuScope(ReportAndPreflight):
    """The Haiku preflight gates L1 and L2 only; L3 and L4 are INFO. Stop continuation is INFO when there is no G arm."""

    def test_nonliteral_oracle_low_at_l3_is_info_not_fail(self):
        res = self.run_check(d4_o_low=True)
        gate = [x for x in res if x[0].startswith("nonliteral oracle (arm O) accuracy at least")]
        self.assertEqual([x[1] for x in gate], ["PASS"])
        self.assertNotIn("L3", gate[0][2])
        info = [x for x in res if x[0].startswith("nonliteral oracle (arm O) accuracy at L3 and L4")]
        self.assertEqual([x[1] for x in info], ["INFO"])
        self.assertIn("L3", info[0][2])

    def test_stop_continuation_is_info_when_the_preset_has_no_g_arm(self):
        res = self.run_check()
        line = [x for x in res if x[0].startswith("Stop continuation")]
        self.assertEqual([x[1] for x in line], ["INFO"])
        self.assertIn("not run: no G arm in this preset", line[0][2])

    def test_output_limit_line_counts_l1_l2_forks_only(self):
        res = self.run_check()
        names = [x[0] for x in res]
        self.assertTrue(any(n.startswith("largest single request of a probe fork at most 10000 tokens") for n in names))
        # these rows have no per-request field: the sum line is INFO and nothing compares a sum with the per-request cap
        self.assertTrue(any(n.startswith("probe fork output: no per-request data") for n in names))

    def test_gate_uses_the_largest_single_request(self):
        import grade
        recs = [{"type": "user", "message": {"content": "PROBE-q go"}},
                {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 5000}, "content": []}},
                {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 7000}, "content": []}},      # the same request, streamed twice
                {"type": "assistant", "message": {"id": "m2", "usage": {"output_tokens": 6000}, "content": []}}]
        self.assertEqual(grade.request_stats(recs, "PROBE-q"), (2, 7000))        # a fork sum of 13000 with no request above 7000
        self.assertEqual(grade.request_stats([{"type": "user", "message": {"content": "x"}}], "PROBE-q"), (0, None))


class PreflightRun5Fixes(unittest.TestCase):
    def rows(self):
        out = []
        for lv, n_rw, n_rf in (("L1", 0, 0), ("L2", 0, 0), ("L3", 6, 4), ("L4", 6, 4)):
            for i in range(6):
                out.append({"seed": 1, "arm": "A", "type": lv, "phrasing": "cued", "condition": "nocompact", "chunk": i, "fork_sid": "f%s%d" % (lv, i),
                            "runaway": i < n_rw, "refused": (n_rw <= i < n_rw + n_rf), "parse_ok": not (i < n_rw), "m1": True})
        return out

    def test_abort_gate_counts_l1_l2_only_for_a_haiku_preflight(self):
        import gates
        rows = self.rows()
        sess = [{"name": "s1-A", "seed": 1, "arm": "A", "status": "done", "condition": "nocompact"}]
        why_all = gates.abort_reasons(sess, rows, None, {1})
        self.assertTrue([w for w in why_all if "runaway rate" in w])
        why_h = gates.abort_reasons(sess, rows, None, {1}, haiku_l12=True)
        self.assertFalse([w for w in why_h if "runaway rate" in w or "refused rate" in w or "parse error" in w], why_h)

    def test_runner_passes_the_haiku_scope_only_for_preflight4(self):
        import inspect
        src = inspect.getsource(runner)
        self.assertIn('haiku_l12=str(ctx.cfg.get("kind", "")).startswith("preflight") and ctx.cfg.get("probe_design", 1) >= 4', src)

    def test_runaway_and_refused_lines_are_info_with_counts(self):
        import preflight
        d = tempfile.mkdtemp()
        try:
            rd, projects = test_litm.make_pf_run(d, d4=True)
            res = preflight.check(rd, projects, mcp=False)
            for start in ("runaway forks of each arm at L1 and L2", "refused forks of each arm"):
                line = [x for x in res if x[0].startswith(start)]
                self.assertEqual([x[1] for x in line], ["INFO"], start)
                self.assertIn("counts:", line[0][2])
        finally:
            shutil.rmtree(d)

    def test_reanchor_line_source_accepts_an_anchor_without_a_row(self):
        import inspect, preflight
        src = inspect.getsource(preflight.check)
        self.assertIn("has_anchor", src)
        self.assertIn("reanchor-", src)


class ForkCacheSharing(unittest.TestCase):
    """The run 20261123T033514 cost bug: forks that miss the prompt cache write the whole history."""

    def calls(self, read, write, n=6):
        return [{"kind": "fork", "name": "s1-B-nc", "ts": "2026-11-23T04:0%d:00Z" % i, "cache_read": read, "cache_write": write, "delta_usd": 1.1 if write > 100000 else 0.04}
                for i in range(n)]

    def test_fraction_is_small_for_sharing_forks_and_large_for_missing_forks(self):
        import gates
        self.assertLess(gates.cache_write_fraction(self.calls(298154, 385))[0], gates.CACHE_WRITE_MAX)
        self.assertGreater(gates.cache_write_fraction(self.calls(19805, 278700))[0], gates.CACHE_WRITE_MAX)
        # the first fork of a parent may write everything: it does not count
        mixed = [dict(self.calls(0, 298540, 1)[0])] + self.calls(298540, 300, 4)
        self.assertLess(gates.cache_write_fraction(mixed)[0], gates.CACHE_WRITE_MAX)

    def test_runner_guard_stops_after_the_minimum_number_of_forks(self):
        import gates
        s = runner.Session.__new__(runner.Session)

        class Ctx:
            pass
        s.ctx = Ctx()
        s.ctx.stop = __import__("threading").Event()
        stopped = None
        for i in range(8):
            try:
                s.cache_guard("fb0cc723-aaaa", {"cache_read_input_tokens": 19805, "cache_creation_input_tokens": 278700})
            except runner.Fatal as e:
                stopped = (i + 1, str(e))
                break
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped[0], gates.CACHE_MIN_FORKS)
        self.assertIn("do not share the prompt cache", stopped[1])
        self.assertTrue(s.ctx.stop.is_set())
        # sharing forks never stop the run
        s2 = runner.Session.__new__(runner.Session)
        s2.ctx = Ctx()
        s2.ctx.stop = __import__("threading").Event()
        for i in range(20):
            s2.cache_guard("p2", {"cache_read_input_tokens": 298154, "cache_creation_input_tokens": 385})

    def test_preflight_line_fails_for_missing_forks(self):
        import preflight
        d = tempfile.mkdtemp()
        try:
            rd, projects = test_litm.make_pf_run(d, d4=True)
            with open(os.path.join(rd, "calls.jsonl"), "a") as f:
                for c in self.calls(19805, 278700):
                    f.write(json.dumps(c) + "\n")
            res = preflight.check(rd, projects, mcp=False)
            line = [x for x in res if x[0].startswith("no-compaction forks share the prompt cache")]
            self.assertEqual([x[1] for x in line], ["FAIL"])
            self.assertIn("s1-B-nc", line[0][2])
        finally:
            shutil.rmtree(d)

    def test_fork_estimate_for_the_cost_check(self):
        e = cost.d4_nc_fork_estimate("B")
        self.assertTrue(0.01 < e < 0.2, e)


class ColdParentRule(unittest.TestCase):
    """Resume rule (fake clock): a no-compaction parent that is idle for more than 4 minutes runs again before its forks."""
    T0 = 1795400000        # any fixed second

    def iso(self, t):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    def calls(self, last):
        return [{"name": "s1-A-nc", "kind": "turn", "sid": "P1", "ts": self.iso(self.T0)},
                {"name": "s1-A-nc", "kind": "fork", "fork_of": "P1", "sid": "F1", "ts": self.iso(last)}]

    def test_age_and_cold_with_a_fake_clock(self):
        c = self.calls(self.T0 + 60)
        self.assertEqual(runner.parent_age_s(c, "P1", now=self.T0 + 100), 40)         # the fork counts as a use of the parent
        self.assertFalse(runner.parent_is_cold(c, "P1", now=self.T0 + 60 + runner.CACHE_TTL_S))
        self.assertTrue(runner.parent_is_cold(c, "P1", now=self.T0 + 60 + runner.CACHE_TTL_S + 1))
        self.assertFalse(runner.parent_is_cold([], "P1", now=self.T0))

    def make(self, status_b="running", done=False):
        d = tempfile.mkdtemp()

        class Ctx:
            pass
        ctx = Ctx()
        ctx.dir = d
        ctx.lock = __import__("threading").RLock()
        ctx.saved = 0
        ctx.save = lambda: None
        cfg = {"arms": ["A", "B"], "probe_design": 4, "levels": ["L1", "L2"], "phrasings": ["cued"]}
        ctx.cfg = cfg
        ctx.state = {"sessions": {"s1-B-nc": {"status": status_b}}}
        keys = [runner.fork_key(0, t, ph) for t in runner.nc_types(cfg) for ph in runner.probe_plan(cfg, t, d3=True)]
        ss = runner.Session.__new__(runner.Session)
        ss.arm, ss.seed, ss.name, ss.ctx = "A", 1, "s1-A-nc", ctx
        ss.st = {"nc_done": 5, "sid": "P1", "done_forks": keys if done else keys[:1], "status": "running", "restarts": 0, "next_turn": 5}
        ss.proj = d
        ss.transcript = lambda sid: os.path.join(d, sid + ".jsonl")
        rows = [{"seed": 1, "arm": a, "condition": "nocompact", "item": 1, "attempt": 0} for a in ("A", "B")] + [{"seed": 1, "arm": "O", "condition": "nocompact", "item": 1}]
        with open(os.path.join(d, "results.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        return ctx, ss, d

    def run_rule(self, last, **kw):
        ctx, ss, d = self.make(**kw)
        with open(os.path.join(d, "calls.jsonl"), "w") as f:
            f.writelines(json.dumps(c) + "\n" for c in self.calls(last))
        try:
            r = runner.rewarm_cold_parent(ctx, ss, list(range(5)), now=self.T0 + 600)
            return r, ctx, ss, d
        finally:
            pass

    def test_a_cold_parent_with_forks_left_runs_again_and_supersedes_the_fork_rows(self):
        r, ctx, ss, d = self.run_rule(self.T0 + 60)             # last use 540 s ago
        try:
            self.assertTrue(r)
            self.assertNotEqual(ss.st["sid"], "P1")
            self.assertEqual((ss.st["nc_done"], ss.st["restarts"], ss.st["done_forks"], ss.st["first"]), (0, 1, [], True))
            left = [json.loads(l) for l in open(os.path.join(d, "results.jsonl"))]
            self.assertEqual([x["arm"] for x in left], ["O"])                  # arm O is a separate session: kept
            sup = [json.loads(l) for f in os.listdir(os.path.join(d, "superseded")) for l in open(os.path.join(d, "superseded", f))]
            self.assertEqual(sorted(x["arm"] for x in sup), ["A", "B"])
        finally:
            shutil.rmtree(d)

    def test_a_warm_parent_a_finished_parent_and_an_unfinished_main_are_left_alone(self):
        r, _c, ss, d = self.run_rule(self.T0 + 500)             # last use 100 s ago: warm
        shutil.rmtree(d)
        self.assertFalse(r)
        r, _c, ss, d = self.run_rule(self.T0 + 60, status_b="done", done=True)       # nothing left to fork
        shutil.rmtree(d)
        self.assertFalse(r)
        ctx, ss, d = self.make()
        ss.st["nc_done"] = 2                                                     # the main session is not finished
        with open(os.path.join(d, "calls.jsonl"), "w") as f:
            f.write("")
        try:
            self.assertFalse(runner.rewarm_cold_parent(ctx, ss, list(range(5)), now=self.T0 + 9999))
        finally:
            shutil.rmtree(d)

    def test_call_refuses_a_cold_nc_fork_in_a_running_run(self):
        import inspect
        src = inspect.getsource(runner.Session.call)
        self.assertIn("age > CACHE_TTL_S", src)
        self.assertIn('self.cond == "nocompact"', src)


class AuditFixes(unittest.TestCase):
    """Fixes of the audit of PR #35 (findings 1 to 9). Offline, from the script generator and the code only."""
    SEEDS = range(1000, 1200)

    def test_1_attribute_is_balanced_over_the_thirds_at_every_level(self):
        import collections
        cnt = collections.defaultdict(collections.Counter)
        for sd in self.SEEDS:
            for lv, its in script(sd)["items"].items():
                for it in its:
                    cnt[(lv, it["third"])][it["attr"]] += 1
        for k, c in cnt.items():
            share = c["queue"] / float(c["queue"] + c["tag"])
            self.assertTrue(0.40 <= share <= 0.60, (k, dict(c)))        # no fixed attribute for a position
            self.assertTrue(c["queue"] and c["tag"], k)
        a = d4.balanced_bits(__import__("random").Random(5), 3, 1)     # one family in each third: at most one group differs from the others
        self.assertEqual(len(a), 3)

    def test_2_undo_pattern_is_balanced_over_the_thirds(self):
        import collections
        cnt = collections.defaultdict(collections.Counter)
        per_seed = set()
        for sd in self.SEEDS:
            sc = script(sd)
            for lv in ("L3", "L4"):
                per_seed.add(tuple(sum(1 for it in sc["items"][lv] if it["third"] == th and it["undo_last"]) for th in range(3)))
                for it in sc["items"][lv]:
                    cnt[(lv, it["third"])][it["undo_last"]] += 1
        for k, c in cnt.items():
            share = c[True] / float(c[True] + c[False])
            self.assertTrue(0.40 <= share <= 0.60, (k, dict(c)))
        self.assertGreater(len(per_seed), 1)             # the pattern differs between seeds
        self.assertNotEqual(per_seed, {(2, 1, 2)})

    def test_3_deciding_statements_cover_the_session_and_position_is_the_last_update(self):
        import collections
        tenth = collections.Counter()
        n = 0
        for sd in range(1000, 1060):
            sc = script(sd)
            T = len(sc["plan"])
            for its in sc["items"].values():
                for it in its:
                    n += 1
                    ut = it["update_turn"]
                    self.assertEqual(it["position"], d4.POSITIONS[min(2, ut * 3 // T)])
                    self.assertEqual(it["position"], d4.POSITIONS[it["third"]])
                    last_upd = max(i for i, o in enumerate(it["ops"]) if o["op"] in ("set", "change", "undo"))
                    self.assertIn(it["stmts"][last_upd], sc["plan"][ut]["prompt"])      # the turn of the last update holds that statement
                    self.assertLessEqual(ut, it["turn"])
                    tenth[min(9, ut * 10 // T)] += 1
        self.assertEqual(sorted(tenth), list(range(10)))
        for i in range(10):
            self.assertGreater(tenth[i] / float(n), 0.03, (i, dict(tenth)))     # every tenth of the session holds deciding statements
        self.assertLess(tenth[9] / float(n), 0.30)                              # no cluster at the end

    def test_4_head_states_previous_and_replaced_in_plain_words(self):
        sc = script(3)
        for ph in d4.PHRASINGS:
            head = d4.probe_prompt("L3", sc, "PROBE-X", ph).split("\n")[0]
            for w in ("`previous` is the value that `current` took the place of", "if the last update was an undo, `previous` is the value that the undo took out",
                      "`replaced` lists every value that was in force at some time and is not `current`", "`previous` is one of them"):
                self.assertIn(w, head, (ph, w))

    def test_4_grading_by_the_stated_rules_and_the_alt_reading(self):
        sc = script(11)
        it = next(i for i in sc["items"]["L4"] if i["undo_last"])
        self.assertIsNotNone(it["previous_alt"])
        self.assertNotEqual(it["previous"], it["previous_alt"])
        self.assertIn(it["previous"], it["replaced"])                  # replaced includes previous
        self.assertIn(it["previous_alt"], it["replaced"])

        def g(prev, rep):
            e = {"id": it["id"], "current": it["current"], "previous": prev, "replaced": rep}
            return d4.grade_level([it], json.dumps({"answers": [e]}), sc)[0]
        r = g(it["previous"], it["replaced"])
        self.assertEqual((r["m1"], r["m2"], r["m3"], r["alt_ok"]), (True, True, True, False))
        r = g(it["previous_alt"], it["replaced"])                       # the other reading of an undo: accepted, marked
        self.assertEqual((r["m2"], r["m3"], r["alt_ok"]), (True, True, True))
        r = g("WRONG", it["replaced"])
        self.assertEqual((r["m1"], r["m2"], r["alt_ok"]), (True, False, False))
        r = g(it["previous"], [x for x in it["replaced"] if x != it["previous"]])        # a reader who leaves out `previous` fails M3
        self.assertEqual((r["m2"], r["m3"]), (True, False))
        plain = next(i for i in sc["items"]["L1"])
        e = {"id": plain["id"], "current": plain["current"], "previous": "WRONG", "replaced": plain["replaced"]}
        self.assertFalse(d4.grade_level([plain], json.dumps({"answers": [e]}), sc)[0]["alt_ok"])      # no alt reading without an undo-last chain

    def test_4_interpreter_after_an_undo(self):
        r = d4.interpret(ops(("set", "a"), ("change", "b"), ("change", "c"), ("undo", None)))
        self.assertEqual((r["current"], r["previous"], r["previous_alt"], r["replaced"]), ("b", "c", "a", ["a", "c"]))
        r = d4.interpret(ops(("set", "a"), ("change", "b"), ("undo", None), ("change", "c")))
        self.assertEqual((r["current"], r["previous"], r["previous_alt"], r["replaced"]), ("c", "a", None, ["a", "b"]))

    def test_4_pick_with_m2_or_m3_needs_a_low_alt_rate(self):
        rows = PickRule.rows(PickRule(), {"L1": 0.9, "L2": 0.9, "L3": 0.7, "L4": 0.9})
        for r in rows:
            r["undo_last"] = True
            r["alt_ok"] = False
        self.assertEqual(d4.decide(rows)["pick"], ("m1", "L3"))
        # M1 is out of the band, M2 is in the band at L3: the pick is M2 only if the alt rate is below 10%
        for r in rows:
            if r["arm"] == "A" and r["type"] == "L3":
                r["m1"] = r["m1"] or False
        rows2 = []
        for r in rows:
            r = dict(r)
            if r["arm"] == "A" and r["type"] == "L3":
                r["m1"], r["m2"], r["m3"] = True, False, False       # M1 100%
                r["alt_ok"] = False
            rows2.append(r)
        k = 0
        for r in rows2:
            if r["arm"] == "A" and r["type"] == "L3":
                r["m2"] = k < 14                                       # M2 70% (n 20)
                k += 1
        self.assertEqual(d4.decide(rows2)["pick"], ("m2", "L3"))
        for r in rows2:
            if r["arm"] == "A" and r["type"] == "L3" and r["m2"]:
                r["alt_ok"] = True                                     # every M2 hit needed the alt reading
        d = d4.decide(rows2)
        self.assertNotEqual(d["pick"], ("m2", "L3"))
        self.assertTrue(any("alt rate" in n for n in d["notes"]), d["notes"])

    def test_5_size_model_uses_the_measured_rate(self):
        # the smoke: 362 lines in a design-3 session reached 393,352 tokens; the model agrees within 1%
        est = runner.nc3_estimate(gen.build_litm3(1), 362)
        self.assertAlmostEqual(est / 393352.0, 1.0, delta=0.01)
        for lv in (None, ["L1", "L2", "L3", "L4", "L5"]):
            lines, est4 = runner.nc3_plan(400000, 4, lv)
            self.assertTrue(270000 <= est4 <= 330000, est4)            # 300k plus or minus 10%
            self.assertLess(est4, 0.90 * 400000)
        with self.assertRaises(SystemExit):
            runner.nc3_plan(60000, 4)

    def test_5_gate_fails_above_90_percent_of_the_window(self):
        old = runner.NC3_TARGET_FRACTION
        runner.NC3_TARGET_FRACTION = 0.95
        try:
            with self.assertRaises(SystemExit):
                runner.nc3_plan(400000, 4)
        finally:
            runner.NC3_TARGET_FRACTION = old

    def test_6_levels_option_and_l5_session(self):
        self.assertEqual(runner.nc_types({"probe_design": 4}), list(d4.D4_LEVELS))
        self.assertEqual(runner.nc_types({"probe_design": 4, "levels": ["L1", "L2", "L3", "L4", "L5"]}), ["L1", "L2", "L3", "L4", "L5"])
        sc = d4.build_litm4(1, ("L1", "L2", "L3", "L4", "L5"))
        self.assertEqual(len(sc["plan"]), 165)
        self.assertEqual(sorted(sc["items"]), ["L1", "L2", "L3", "L4", "L5"])
        self.assertEqual(d4.check_script(sc), [])
        k = runner.KINDS["litm4-calibrate-l5"]
        self.assertEqual(k["levels"], "L1,L2,L3,L4,L5")
        self.assertEqual(k["conditions"], "nocompact")
        self.assertIn("litm4-calibrate-l5", open(os.path.join(HERE, "run.sh")).read())
        rows = PickRule.rows(PickRule(), {"L1": 0.9, "L2": 0.9, "L3": 0.9, "L4": 0.9})
        d = d4.decide(rows)
        self.assertEqual(d["decision"], "add_l5")
        self.assertIn("bench/continuity/run.sh litm4-calibrate-l5", d4.describe_decision(d))
        l5 = PickRule.rows(PickRule(), {"L1": 0.9, "L2": 0.9, "L3": 0.9, "L4": 0.9, "L5": 0.9}, levels=d4.D4_LEVELS + ("L5",))
        self.assertEqual(d4.decide(l5, d4.D4_LEVELS + ("L5",))["decision"], "no_loss")
        self.assertGreater(cost.estimate3(1, ["A"], [1], ["nocompact"], design=4, levels=["L1", "L5"])[2], cost.estimate3(1, ["A"], [1], ["nocompact"], design=4)[2])

    def test_7_pilot_seeds_are_new_dev_seeds(self):
        def seeds(name):
            return [int(x) for x in open(os.path.join(HERE, name)).read().split()]
        cal = set(seeds("seeds-dev.txt")[:runner.KINDS["litm4-calibrate"]["seeds"]]) | set(seeds("seeds-dev-2.txt"))
        pilot = seeds(runner.KINDS["litm4-pilot"]["seed_file"])[:runner.KINDS["litm4-pilot"]["seeds"]]
        self.assertEqual(runner.KINDS["litm4-pilot"]["seed_file"], "seeds-pilot.txt")
        self.assertEqual(len(pilot), 2)
        self.assertFalse(cal & set(pilot))
        self.assertFalse(set(pilot) & set(seeds("seeds-report.txt")))

    def test_7_pick_needs_each_phrasing_in_45_to_90_and_z_rows(self):
        rows = []
        for arm in ("A", "O", "Z"):
            for lv in d4.D4_LEVELS:
                for ph, rate in zip(d4.PHRASINGS, (0.95, 0.55, 0.40)):
                    for i in range(20):
                        ok = {"A": i < round(rate * 20), "O": True, "Z": False}[arm]
                        rows.append({"arm": arm, "type": lv, "phrasing": ph, "condition": "nocompact", "m1": ok, "m2": ok, "m3": ok, "abstained": False})
        d = d4.decide(rows)                                            # pooled A is 63%: in the band, but cued is 95% and nonliteral is 40%
        self.assertNotEqual(d["decision"], "pick")
        self.assertTrue(any("outside 45% to 90%" in n for n in d["notes"]), d["notes"])
        for r in rows:
            if r["arm"] == "A":
                r["m1"] = r["m2"] = r["m3"] = (r["phrasing"] != "nonliteral") or r["m1"]
        ok_rows = []
        for r in rows:
            ok_rows.append(r)
        # a clean case: all phrasings at 60%, with some abstains: the pick prints the abstain rate
        clean = []
        for arm in ("A", "O", "Z"):
            for ph in d4.PHRASINGS:
                for i in range(20):
                    ok = {"A": i < 12, "O": True, "Z": False}[arm]
                    clean.append({"arm": arm, "type": "L2", "phrasing": ph, "condition": "nocompact", "m1": ok, "m2": ok, "m3": ok, "abstained": arm == "A" and i == 19})
        d = d4.decide(clean, ("L2",))
        self.assertEqual(d["pick"], ("m1", "L2"))
        self.assertAlmostEqual(d["abstain"]["A"][0], 1 / 20.0)
        self.assertIn("abstain rate in the picked cell: A 5.0%", d4.describe_decision(d))
        no_z = [r for r in clean if r["arm"] != "Z"]
        d = d4.decide(no_z, ("L2",))
        self.assertNotEqual(d["decision"], "pick")                      # Z with no rows is NO DATA, not a pass
        self.assertTrue(any("NO DATA" in n for n in d["notes"]), d["notes"])
        lines = d4.gate_lines(no_z, [], ("L2",))
        self.assertEqual([st for n, st, _d in lines if "amnesia Z" in n], ["NO DATA"])

    def test_8_h1_is_pooled_and_phrasings_are_secondary_and_h11_is_paired(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        rows = []
        for sd in (1, 2, 3):
            for arm in ("A", "B", "R"):
                for ph in d4.PHRASINGS:
                    for pos in ("start", "middle", "end"):
                        for i in range(3):
                            ok = (i < 2) if pos != "middle" else False        # start and end 67%, middle 0%
                            if arm == "R" and ph == "cued":
                                ok = False                                      # R: all cued answers wrong (stale)
                            rows.append({"seed": sd, "arm": arm, "type": "L3", "phrasing": ph, "position": pos, "condition": "nocompact", "m1": ok, "m2": ok, "m3": ok,
                                         "correct": ok, "error_class": None if ok else "stale", "lag": 1, "abstained": False})
        md, out = litm4.section4(rows, {}, th, 200)
        names = [h["name"] for h in out["hypotheses"]]
        self.assertTrue(names[0].startswith("H1 lost in the middle, primary") and "3 phrasings pooled" in names[0], names[0])
        for w in ("H1c secondary", "H1s secondary", "H1n secondary"):
            self.assertTrue(any(n.startswith(w) for n in names), w)
        self.assertEqual(out["hypotheses"][0]["result"], "SUPPORTED")
        # H11: R has more stale errors than B in every seed: the paired interval lower bound is above 0
        h11 = [h for h in out["hypotheses"] if h["name"].startswith("H11")][0]
        self.assertTrue(h11["result"].startswith("HARM"), h11)
        # equal errors: no harm
        rows2 = [dict(r, error_class="stale", m1=False) if r["arm"] == "R" else dict(r) for r in rows]
        for r in rows2:
            if r["arm"] == "B":
                r["error_class"] = "stale"                                      # B has the same errors as R on the cued probes
        md2, out2 = litm4.section4(rows2, {}, th, 200)
        h11b = [h for h in out2["hypotheses"] if h["name"].startswith("H11")][0]
        self.assertTrue(h11b["result"].startswith("OK"), h11b)
        self.assertIn("upper bound", "\n".join(md) + "upper bound")
        self.assertIn("K's gain (H6, H7) is an upper bound", "\n".join(md))

    def test_8_solver_gate_can_fail(self):
        scs = [script(sd) for sd in range(1000, 1060)]
        for lv in ("L3", "L4"):
            mean = sum(d4.last_mention_rate(sc, lv) for sc in scs) / len(scs)
            self.assertGreater(mean, 0.0)
            self.assertLessEqual(mean, 0.5, lv)                          # the mean over the seeds of a run, per level
        self.assertEqual([st for n, st, _d in d4.gate_lines([], scs, ("L3", "L4")) if "last mention wins" in n], ["PASS"])
        old = {lv: dict(d4.LEVELS[lv]) for lv in ("L3", "L4")}
        try:
            for lv in ("L3", "L4"):
                d4.LEVELS[lv].update(undo=False, rejects=0, scoped=0)       # a design with no undo and no lure: the naive solver is always right
            bad = [d4.build_litm4(sd) for sd in range(1000, 1010)]
            self.assertEqual(d4.last_mention_rate(bad[0], "L3"), 1.0)
            self.assertEqual([st for n, st, _d in d4.gate_lines([], bad, ("L3", "L4")) if "last mention wins" in n], ["FAIL"])
        finally:
            for lv, v in old.items():
                d4.LEVELS[lv].update(v)

    def test_8_solver_limit_agrees_in_code_and_thresholds(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))["litm"]["design4"]["gates"]
        self.assertEqual(th["solver_max_accuracy_l3_l4"], d4.SOLVER_MAX)
        self.assertEqual(d4.SOLVER_MAX, 0.5)
        self.assertEqual((th["o_min_accuracy"], th["z_max_accuracy"]), (d4.O_MIN, d4.Z_MAX))
        self.assertIn("mean over the seeds", th["note"])
        # one seed above the limit does not fail the gate when the mean is below it
        sc = script(1000)
        hi = dict(sc, seed=-1)
        hi["items"] = {lv: [dict(it) for it in its] for lv, its in sc["items"].items()}
        orig = d4.last_mention_rate
        vals = iter([0.56, 0.30, 0.56, 0.30])
        d4.last_mention_rate = lambda s_, lv: next(vals)
        try:
            self.assertEqual([st for n, st, _d in d4.gate_lines([], [sc, hi], ("L3", "L4")) if "last mention wins" in n], ["PASS"])
        finally:
            d4.last_mention_rate = orig

    def test_v3_late_lean_the_last_update_falls_anywhere_in_its_third(self):
        scs = [script(sd) for sd in range(1000, 1100)]
        pt = d4.place_table(scs)
        for lv in d4.D4_LEVELS:
            self.assertTrue(0.35 <= pt[lv]["first_half"] <= 0.65, (lv, pt[lv]))
            m = pt[lv]["mean_by_third"]
            self.assertLess(max(m) - min(m), 0.06, (lv, m))                # the place inside a third is the same in the three thirds
        bursts = sum(1 for sc in scs for st in sc["plan"] if st["kind"] == "fact" and len(set(st["keys"])) < len(st["keys"]))
        self.assertGreater(bursts, 0)                                      # a chain puts 2 statements in one turn when it needs to
        for sc in scs[:20]:
            self.assertEqual(len([st for st in sc["plan"] if st["kind"] == "fact"]), 69)
            self.assertEqual(len(sc["plan"]), 105)

    def test_v3_half_of_the_chains_with_lures_have_them_last(self):
        import collections
        cnt = collections.defaultdict(collections.Counter)
        for sd in range(1000, 1200):
            sc = script(sd)
            for lv in ("L2", "L3", "L4"):
                for it in sc["items"][lv]:
                    kinds = [o["op"] for o in it["ops"]]
                    last_update = max(i for i, o in enumerate(kinds) if o in ("set", "change", "undo"))
                    if it["lure_late"]:
                        self.assertTrue(all(o in ("reject", "scoped") for o in kinds[last_update + 1:]))
                        self.assertEqual(len(kinds) - last_update - 1, len(it["lures"]))
                    cnt[("third", it["third"])][it["lure_late"]] += 1
                    cnt[("attr", it["attr"])][it["lure_late"]] += 1
                    cnt[(lv, "all")][it["lure_late"]] += 1
        for k, c in cnt.items():
            share = c[True] / float(c[True] + c[False])
            self.assertTrue(0.40 <= share <= 0.60, (k, dict(c)))
        for it in script(1000)["items"]["L1"]:
            self.assertFalse(it["lure_late"])                             # L1 has no lure

    def test_v3_naive_solver_mean_per_level(self):
        scs = [script(sd) for sd in range(1000, 1100)]
        mean = {lv: sum(d4.last_mention_rate(sc, lv) for sc in scs) / len(scs) for lv in d4.D4_LEVELS}
        self.assertLessEqual(mean["L3"], 0.5)
        self.assertLessEqual(mean["L4"], 0.5)
        self.assertEqual(mean["L1"], 1.0)

    def test_v3_reject_right_ahead_of_an_undo_is_unambiguous(self):
        head = d4.probe_prompt("L3", script(3), "PROBE-X", "neutral").split("\n")[0]
        self.assertIn("never in force", head)
        self.assertIn("just ahead of an undo", head)
        self.assertFalse(gen.has_cue(head))
        o = ops(("set", "A1"), ("change", "B2"), ("reject", "R3"), ("undo", None))
        r = d4.interpret(o)
        self.assertEqual((r["current"], r["previous"], r["replaced"]), ("A1", "B2", ["B2"]))     # the reject is not in force, the undo takes out B2
        text = " ".join(d4.stmt_text(x["op"], "k-x", "queue", x["value"]) for x in o)
        self.assertEqual([(a, b, c) for a, b, c in d4.parse_turn("p Note for this project: %s Reply only `ok`." % text)], [("set", "k-x", "A1"), ("change", "k-x", "B2"), ("reject", "k-x", "R3"), ("undo", "k-x", None)])
        sc = script(11)
        it = dict(next(i for i in sc["items"]["L3"]), current="A1", previous="B2", previous_alt=None, replaced=["B2"], values=["A1", "B2"], lures=["R3"], undo_last=True)
        e = {"id": it["id"], "current": "A1", "previous": "B2", "replaced": ["B2"]}
        g = d4.grade_level([it], json.dumps({"answers": [e]}), sc)[0]
        self.assertEqual((g["m1"], g["m2"], g["m3"]), (True, True, True))
        e = {"id": it["id"], "current": "A1", "previous": "R3", "replaced": ["B2", "R3"]}      # a reader who counted the reject as in force
        g = d4.grade_level([it], json.dumps({"answers": [e]}), sc)[0]
        self.assertEqual((g["m1"], g["m2"], g["m3"]), (True, False, False))

    def test_v3_report_h11_wording_and_place_table(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        rows = []
        for sd in (1, 2, 3):
            for arm in ("A", "B", "R"):
                for ph in d4.PHRASINGS:
                    for pos in ("start", "middle", "end"):
                        for i in range(3):
                            ok = i < 2
                            rows.append({"seed": sd, "arm": arm, "type": "L3", "phrasing": ph, "position": pos, "condition": "nocompact", "m1": ok, "m2": ok, "m3": ok,
                                         "correct": ok, "error_class": None if ok else "stale", "lag": 1, "abstained": False})
        md, out = litm4.section4(rows, {}, th, 200)
        h11 = [h for h in out["hypotheses"] if h["name"].startswith("H11")][0]
        self.assertEqual(h11["result"], "OK: no harm shown (3 seeds)")
        text = "\n".join(md)
        self.assertIn("Table D4-5", text)
        self.assertNotIn("H1 does not test", text)
        self.assertEqual(sorted(out["place"]), list(d4.D4_LEVELS))
        orig = d4.place_table
        d4.place_table = lambda scripts, levels=d4.D4_LEVELS: {"L3": {"first_half": 0.0, "mean_by_third": [0.9, 0.9, 0.9], "n": 9}}
        try:
            md2, _o = litm4.section4(rows, {}, th, 200)
        finally:
            d4.place_table = orig
        self.assertIn("H1 does not test the first part of a third at L3", "\n".join(md2))

    def test_9_lag_of_an_undone_value_is_defined(self):
        r = d4.interpret(ops(("set", "a"), ("change", "b"), ("change", "c"), ("undo", None)))
        self.assertEqual(r["lags"], {"a": 3, "c": 1})                    # the undone value c: one update (the undo) after it
        sc = script(11)
        it = next(i for i in sc["items"]["L4"] if i["undo_last"])
        undone = it["values"][-1]
        self.assertNotEqual(undone, it["current"])
        e = {"id": it["id"], "current": undone, "previous": "x", "replaced": []}
        g = d4.grade_level([it], json.dumps({"answers": [e]}), sc)[0]
        self.assertEqual((g["error_class"], g["lag"]), ("stale", 1))
        self.assertGreater(it["lags"][it["values"][0]], g["lag"])         # an older value has a longer lag


DENY = ("sample specimen culture strain assay vial pathogen virus viral dose dosage toxin toxic bacteria bacterial germ cell cells cellular dna rna gene genes genome "
        "protein enzyme peptide plasmid antibody antigen vaccine disease infect infection outbreak epidemic pandemic lab laboratory reagent compound chemical "
        "chemistry acid poison venom spore fungus fungal mold organism tissue blood serum patient clinical medical medicine drug pharma sequence sequencing "
        "mutation mutant clone cloning incubate ferment biology biotech molecule isolate contagious cobalt orchid thistle willow walnut meadow canary health live").split()


class NeutralLexicon(unittest.TestCase):
    """The run 20261122T142851-preflight4 stopped with a safety filter refusal (`[bio]`). No bio, lab, medical or chemistry word may be in the text."""

    def hits(self, text):
        import re
        low = text.lower()
        return sorted({w for w in DENY if re.search(r"\b" + re.escape(w) + r"\b", low)})

    def test_lexicon_has_no_deny_word(self):
        texts = [b + " " + d for b, d in d4.BASES] + [m + " " + d for m, d in d4.MODS] + [d4.BASE_MEMBER, d4.FIELDS, " ".join(d4.FILLER_WORDS), " ".join(d4.gen.SENT3)]
        for t in texts:
            self.assertEqual(self.hits(t), [], t)
        for old in ("cobalt", "orchid", "thistle", "willow", "walnut", "meadow"):
            self.assertNotIn(old, d4.FILLER_WORDS)
        self.assertEqual(len(d4.FILLER_WORDS), len(d4.gen.WORDS))

    def test_scripts_probes_and_filler_of_100_seeds_have_no_deny_word(self):
        for sd in range(1, 101):
            sc = d4.build_litm4(sd)
            for st in sc["plan"]:
                if st["kind"] == "fact":
                    self.assertEqual(self.hits(st["prompt"]), [], (sd, st["turn"]))
            for lv in sc["items"]:
                for ph in d4.PHRASINGS:
                    self.assertEqual(self.hits(d4.probe_prompt(lv, sc, "PROBE-%d-A-0-%s" % (sd, lv), ph)), [], (sd, lv, ph))
            if sd <= 20:
                for k in (0, 17, 35):
                    pr, _e = runner.filler_prompt(sc, k, 250)
                    self.assertEqual(self.hits(pr), [], (sd, k))

    def test_design_3_filler_is_unchanged(self):
        sc3 = gen.build_litm3(1)
        self.assertEqual(runner.filler_prompt(sc3, 3, 100), gen.filler_doc_prompt(sc3, 3, 100))
        self.assertIn("cobalt", gen.filler_doc_prompt(sc3, 3, 300)[0])          # gen.py keeps its words: designs 2 and 3 are pinned
        sc4 = d4.build_litm4(1)
        self.assertNotEqual(runner.filler_prompt(sc4, 3, 100), gen.filler_doc_prompt(sc4, 3, 100))
        self.assertEqual(runner.filler_prompt(sc4, 3, 100)[1].count(" "), 0)     # the expected token is a line number

    def test_zero_overlap_and_unique_checks_still_hold(self):
        for sd in range(1, 101):
            self.assertEqual(d4.check_script(d4.build_litm4(sd)), [], sd)
        self.assertEqual(d4.GEN_VERSION, "litm4-6")

    def test_a_safety_refusal_is_its_own_failure_class(self):
        msg = ("is_error: API Error: Haiku 5.5 can't help with this. Start a new session to continue.\n\nLearn more: https://www.anthropic.com/legal/aup\n\n"
               "Details: `[bio]`\n\nRequest ID: req_x")
        self.assertEqual(gates.failure_class(msg), "safety_refusal")
        self.assertEqual(gates.failure_class("is_error: API Error: 529 overloaded"), "other")
        self.assertEqual(gates.failure_class("timeout after 900 s"), "other")
        self.assertEqual(gates.failure_class(None), "other")
        why = gates.session_reasons({"a": "done", "b": "failed"}, {}, {"b": "safety_refusal"})
        self.assertTrue(any("failed-session rate" in w and "safety_refusal" in w and "b" in w for w in why), why)
        why = gates.session_reasons({"a": "done", "b": "failed"}, {}, {"b": "other"})
        self.assertTrue(why and not any("safety_refusal" in w for w in why), why)
        self.assertTrue(gates.session_reasons({"a": "done", "b": "failed"}, {}))           # the abort gate is as before

    def test_runner_records_the_class_in_sessions_jsonl(self):
        import types
        rows, st = [], {"status": "pending", "boundaries": 0, "turns": 5}

        class FakeSession:
            def __init__(self, ctx, name, seed, arm, st_, cond):
                self.st, self.name, self.seed, self.arm = st_, name, seed, arm

            def prepare(self):
                raise runner.CallError("is_error: API Error: Haiku 5.5 can't help with this. Details: `[bio]`")
        ctx = types.SimpleNamespace(sess=lambda n: st, save=lambda: None, append=lambda f, r: rows.append((f, r)), cfg={})
        keep = (runner.Session, runner.claude_version, runner.session_summary, runner.write_retention)
        runner.Session, runner.claude_version = FakeSession, lambda: "x"
        runner.session_summary = lambda c, ss: {"name": ss.name, "status": ss.st["status"], "reason": ss.st.get("reason"), "failure_class": ss.st.get("failure_class")}
        runner.write_retention = lambda c, ss: None
        try:
            runner.run_unit(ctx, 1, "A", "nocompact")
        finally:
            runner.Session, runner.claude_version, runner.session_summary, runner.write_retention = keep
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["failure_class"], "safety_refusal")
        self.assertEqual(rows[0][0], "sessions.jsonl")
        self.assertEqual(rows[0][1]["failure_class"], "safety_refusal")
        import inspect
        self.assertIn('"failure_class": st.get("failure_class")', inspect.getsource(keep[2]))      # the real session summary carries the field


class ProbeCapAndFailures(unittest.TestCase):
    """Output cap of probe forks, kept evidence of failed calls, and no retry of a safety refusal."""
    REFUSAL = "API Error: Haiku 5.5 can't help with this. Start a new session to continue. Details: `[bio]`"

    def session(self, d):
        ctx, cfg = test_litm.r4_cfg(d)
        ss = runner.Session(ctx, "s1-A-nc", 1, "A", ctx.sess("s1-A-nc"), "nocompact")
        ss.proj = os.path.join(d, "proj")
        os.makedirs(os.path.join(runner.PROJECTS, runner.slug(ss.proj)), exist_ok=True)
        return ctx, ss

    def run_call(self, replies, kind="fork", fork_of="parent1"):
        d = tempfile.mkdtemp()
        old = (runner.PROJECTS, runner.subprocess.Popen)
        runner.PROJECTS = os.path.join(d, "projects")
        seen = []

        class P:
            def __init__(self, cmd, **k):
                seen.append(k.get("env"))

            def communicate(self, input=None, timeout=None):
                n = len(seen) - 1
                return json.dumps(replies[min(n, len(replies) - 1)]), ""
            returncode = 0
        runner.subprocess.Popen = P
        try:
            ctx, ss = self.session(d)
            err = None
            try:
                ss.call("PROBE prompt", kind, sid=None, fork_of=fork_of if kind == "fork" else None, first=(kind != "fork"))
            except runner.CallError as e:
                err = e
            return d, ctx, ss, seen, err
        finally:
            runner.PROJECTS, runner.subprocess.Popen = old

    def run_call_d4(self, replies, kind="fork", fork_of="parent1"):
        d = tempfile.mkdtemp()
        old = (runner.PROJECTS, runner.subprocess.Popen)
        runner.PROJECTS = os.path.join(d, "projects")
        seen = []

        class P:
            def __init__(self, cmd, **k):
                seen.append(k.get("env"))

            def communicate(self, input=None, timeout=None):
                return json.dumps(replies[min(len(seen) - 1, len(replies) - 1)]), ""
            returncode = 0
        runner.subprocess.Popen = P
        try:
            ctx, ss = self.session(d)
            ss.d4 = True
            err = None
            try:
                o = ss.call("PROBE prompt", kind, sid=None, fork_of=fork_of if kind == "fork" else None, first=(kind != "fork"))
                self.last = o
            except runner.CallError as e:
                err = e
            return d, ctx, ss, seen, err
        finally:
            runner.PROJECTS, runner.subprocess.Popen = old

    def test_cap_for_probe_forks_only(self):
        ok = {"result": "{}", "session_id": "f1", "total_cost_usd": 0.01, "usage": {"output_tokens": 50}, "is_error": False}
        d, ctx, ss, seen, err = self.run_call([ok], "fork")
        try:
            self.assertIsNone(err)
            self.assertEqual(seen[0].get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), "8000")
        finally:
            shutil.rmtree(d)
        d, ctx, ss, seen, err = self.run_call([ok], "turn")
        try:
            self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", seen[0])       # not for the turns of a main session
        finally:
            shutil.rmtree(d)
        self.assertEqual(runner.PROBE_MAX_OUTPUT_TOKENS, 8000)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", runner.OVERRIDE_ENV)       # the preflight env check allows it
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", runner.ENV_ALLOW)         # and it does not come from the user's environment

    def test_safety_refusal_is_not_retried_and_the_evidence_is_kept(self):
        key = "sk-ant-api03-" + "A" * 40
        bad = {"result": self.REFUSAL + " " + key + " " + "x" * 9000, "session_id": "f2", "total_cost_usd": 0.1, "usage": {"output_tokens": 41000}, "is_error": True}
        d, ctx, ss, seen, err = self.run_call([bad], "fork")
        try:
            self.assertIsInstance(err, runner.SafetyRefusal)
            self.assertEqual(len(seen), 1)                                   # one call, no retry
            self.assertEqual(ss.st["safety_refusals"], 1)
            self.assertEqual(gates.failure_class(str(err)), "safety_refusal")
            fd = os.path.join(ctx.dir, "failed")
            js = [f for f in os.listdir(fd) if f.endswith(".json")]
            self.assertEqual(len(js), 1)
            rec = json.load(open(os.path.join(fd, js[0])))
            self.assertEqual(rec["failure_class"], "safety_refusal")
            self.assertEqual(rec["output_tokens"], 41000)
            self.assertLessEqual(len(rec["reply"]), 4000)
            self.assertEqual(rec["reply_chars"], len(bad["result"]))
            self.assertNotIn(key, rec["reply"])                              # redacted
            self.assertIn("can't help with this", rec["reply"])
        finally:
            shutil.rmtree(d)

    def test_other_errors_are_retried_and_kept_once(self):
        bad = {"result": "API Error: 529 overloaded", "session_id": "f3", "total_cost_usd": 0.0, "usage": {}, "is_error": True}
        d, ctx, ss, seen, err = self.run_call([bad], "fork")
        try:
            self.assertIsInstance(err, runner.CallError)
            self.assertNotIsInstance(err, runner.SafetyRefusal)
            self.assertEqual(len(seen), 2)                                   # the normal retry: 2 attempts
            self.assertEqual(len([f for f in os.listdir(os.path.join(ctx.dir, "failed")) if f.endswith(".json")]), 1)
            self.assertEqual(ss.st.get("safety_refusals", 0), 0)
        finally:
            shutil.rmtree(d)

    def test_failed_transcript_is_copied(self):
        d = tempfile.mkdtemp()
        old = runner.PROJECTS
        runner.PROJECTS = os.path.join(d, "projects")
        try:
            ctx, ss = self.session(d)
            with open(ss.transcript("fsid"), "w") as f:
                f.write(json.dumps({"type": "user", "message": {"content": "q"}}) + "\n")
            ss.save_failed({"result": "boom", "session_id": "fsid"}, "", "", "fork", "p", 2)
            names = os.listdir(os.path.join(ctx.dir, "failed"))
            self.assertEqual(sorted(x.rsplit(".", 1)[1] for x in names), ["json", "jsonl"])
        finally:
            runner.PROJECTS = old
            shutil.rmtree(d)

    def test_rows_have_output_tokens_and_preflight_has_the_lines(self):
        import inspect
        self.assertIn('"output_tokens": u.get("output_tokens")', inspect.getsource(runner.Session.probe))
        d = tempfile.mkdtemp()
        try:
            import preflight
            rd, projects = test_litm.make_pf_run(d, d4=True)
            res = preflight.check(rd, projects, mcp=False)
            names = [r[0] for r in res]
            self.assertTrue(any(n.startswith("largest single request of a probe fork at most 10000 tokens") for n in names), names)
            self.assertIn("median probe output tokens", names)
            old = runner.PROBE_MAX_OUTPUT_TOKENS
            runner.PROBE_MAX_OUTPUT_TOKENS = 1
            try:
                res2 = preflight.check(rd, projects, mcp=False)
            finally:
                runner.PROBE_MAX_OUTPUT_TOKENS = old
            st = [r[1] for r in res2 if r[0].startswith("largest single request of a probe fork")][0]
            self.assertIn(st, ("FAIL", "INFO"))        # INFO while the rows have no max_request_output; FAIL when a request is above the limit
        finally:
            shutil.rmtree(d)


class SmallProbes(unittest.TestCase):
    """Probe forks of at most 4 chains, and the runaway class (a probe that hit the output cap is an outcome, not a session failure)."""

    def test_chunks_have_at_most_4_items_and_cover_every_chain_once(self):
        for sd in range(1, 41):
            sc = script(sd)
            for lv, its in sc["items"].items():
                ch = d4.chunks(its, sd, lv)
                self.assertTrue(all(0 < len(c) <= 4 for c in ch), (sd, lv, [len(c) for c in ch]))
                self.assertEqual(sorted(i["id"] for c in ch for i in c), sorted(i["id"] for i in its))
                self.assertEqual(ch, d4.chunks(its, sd, lv))                         # deterministic by seed
                for th in range(3):                                                  # positions balanced: each chunk holds each third within 1 chain
                    cnt = [sum(1 for i in c if i["third"] == th) for c in ch]
                    self.assertLessEqual(max(cnt) - min(cnt), 1, (sd, lv, th, cnt))
        self.assertNotEqual(d4.chunks(script(1)["items"]["L2"], 1, "L2"), d4.chunks(script(2)["items"]["L2"], 2, "L2"))

    def test_fork_counts(self):
        self.assertEqual([len(d4.chunks(script(1)["items"][lv], 1, lv)) for lv in d4.D4_LEVELS], [3, 3, 3, 3])
        self.assertEqual(d4.n_probe_forks(), 36)                                     # was 12
        self.assertEqual(d4.n_probe_forks(d4.D4_LEVELS + ("L5",)), 45)
        self.assertEqual(cost.D4_FORKS, 36)
        self.assertAlmostEqual(cost.D4_A_FORKS, cost.SMOKE_REAL["forks_15"]["A"] / 15.0 * 36)

    def make_session(self, d, replies):
        ctx, cfg = test_litm.r4_cfg(d)
        cfg.update(probe_design=4, levels=list(d4.D4_LEVELS), phrasings=["cued", "neutral", "nonliteral"])
        ss = runner.Session(ctx, "s1-A-nc", 1, "A", ctx.sess("s1-A-nc"), "nocompact")
        ss.script, ss.d4, ss.d3 = d4.build_litm4(1), True, True
        prompts = []

        def call(prompt, kind, sid=None, fork_of=None, first=False, model=None, cap=None, action_dir=None):
            prompts.append(prompt)
            r = replies(len(prompts) - 1, prompt)
            return dict(r, _delta=0.01, usage={"output_tokens": 10})
        ss.call = call
        return ctx, ss, prompts

    def test_probe_runs_one_fork_per_chunk_and_marks_them_done(self):
        d = tempfile.mkdtemp()
        try:
            ctx, ss, prompts = self.make_session(d, lambda n, p: {"result": "{}", "session_id": None})
            ss.probe("L2", 0, fork_of="p1", phrasing="cued")
            self.assertEqual(len(prompts), 3)
            self.assertEqual(len({re.search(r"PROBE-1-A-0-L2c\d", p).group(0) for p in prompts}), 3)      # a marker for each chunk
            self.assertTrue(all(p.count("\n") >= 3 and len(re.findall(r"^\d+\. ", p, re.M)) <= 4 for p in prompts))
            self.assertEqual(ss.st["done_forks"], ["0:L2:c0", "0:L2:c1", "0:L2:c2", "0:L2"])
            ss.probe("L2", 0, fork_of="p1", phrasing="cued")           # resume: nothing is run twice for the finished chunks
            self.assertEqual(len(prompts), 3)
            rows = [json.loads(l) for l in open(os.path.join(ctx.dir, "results.jsonl"))]
            self.assertEqual(len(rows), 12)
            self.assertEqual(sorted({r["chunk"] for r in rows}), [0, 1, 2])
            self.assertTrue(all(r["runaway"] is False for r in rows))
        finally:
            shutil.rmtree(d)

    def test_a_runaway_fork_is_an_outcome_with_every_item_wrong(self):
        d = tempfile.mkdtemp()
        try:
            ctx, ss, prompts = self.make_session(d, lambda n, p: {"result": "API Error: exceeded the 8000 output token maximum", "session_id": None, "_runaway": n == 1})
            ss.probe("L2", 0, fork_of="p1", phrasing="cued")
            rows = [json.loads(l) for l in open(os.path.join(ctx.dir, "results.jsonl"))]
            rw = [r for r in rows if r["runaway"]]
            self.assertEqual({r["chunk"] for r in rw}, {1})
            self.assertEqual(len(rw), 4)
            self.assertTrue(all(r["error_class"] == "runaway" and r["m1"] is False and r["correct"] is False and r["abstained"] is False for r in rw))
            self.assertEqual(ss.st.get("status"), ss.st.get("status"))
            self.assertNotEqual(ss.st.get("status"), "failed")
            self.assertEqual(ss.st["done_forks"][-1], "0:L2")
        finally:
            shutil.rmtree(d)

    def test_call_returns_a_runaway_probe_and_raises_for_a_main_turn(self):
        bad = {"result": "API Error: Claude's response exceeded the 8000 output token maximum. To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.",
               "session_id": "f9", "total_cost_usd": 0.2, "usage": {"output_tokens": 32000}, "is_error": True}
        d, ctx, ss, seen, err = ProbeCapAndFailures().run_call_d4([bad], "fork")
        try:
            self.assertIsNone(err)
            self.assertEqual(len(seen), 1)                                           # no retry
            self.assertEqual(len([f for f in os.listdir(os.path.join(ctx.dir, "failed")) if f.endswith(".json")]), 1)       # the evidence is kept
        finally:
            shutil.rmtree(d)
        d, ctx, ss, seen, err = ProbeCapAndFailures().run_call_d4([bad], "turn")
        try:
            self.assertIsInstance(err, runner.CallError)                              # a main turn that runs away is a failure
        finally:
            shutil.rmtree(d)

    def test_failure_class_maps_the_cap_text_to_runaway(self):
        self.assertEqual(gates.failure_class("is_error: API Error: Claude's response exceeded the 8000 output token maximum."), "runaway")
        self.assertEqual(gates.failure_class("exceeded the 32000 output token maximum"), "runaway")
        self.assertEqual(gates.failure_class("is_error: API Error: Haiku 5.5 can't help with this."), "safety_refusal")
        self.assertEqual(gates.failure_class("timeout"), "other")
        self.assertEqual(gates.failure_class("is_error: API Error: Sonnet 5.5's safeguards flagged this message (https://www.anthropic.com/legal/aup). This sometimes happens with safe, normal conversations. Claude Code can't respond to this message with Sonnet 5.5. Details: `[reasoning_extraction]`"), "safety_refusal")
        for model in ("Opus 5.5", "Fable 5.2"):
            self.assertEqual(gates.failure_class("is_error: API Error: %s's safeguards flagged this message (https://www.anthropic.com/legal/aup)." % model), "safety_refusal")
        self.assertEqual(gates.failure_class("is_error: API Error: a new wording we have not seen. Learn more: https://www.anthropic.com/legal/aup"), "safety_refusal")
        self.assertEqual(gates.failure_class("is_error: API Error: Overloaded"), "other")
        self.assertIn("runaway", d4.ERROR_CLASSES)

    def rows(self, seed, n, bad, arm="A"):
        out = []
        for i in range(n):
            out.append({"seed": seed, "arm": arm, "condition": "nocompact", "type": "L2", "phrasing": "cued", "chunk": i, "fork_sid": "f%d-%d" % (seed, i),
                        "item": 1, "runaway": i < bad})
        return out

    def test_runaway_rate_and_the_abort_gate(self):
        self.assertEqual(gates.runaway_rate(self.rows(1, 36, 3)), (3, 36))
        r = self.rows(1, 36, 3) + [dict(x, item=2) for x in self.rows(1, 36, 3)]     # two items of the same fork count once
        self.assertEqual(gates.runaway_rate(r), (3, 36))
        ok = gates.abort_reasons([], self.rows(1, 36, 3), None, [1])                 # 8.3%
        self.assertFalse([w for w in ok if "runaway" in w], ok)
        bad = gates.abort_reasons([], self.rows(1, 36, 4), None, [1])                # 11%
        self.assertTrue([w for w in bad if "runaway rate 11%" in w and "seed 1" in w], bad)
        self.assertFalse([w for w in gates.abort_reasons([], self.rows(2, 36, 9), None, [1]) if "runaway" in w])      # a seed that is not finished is not judged

    def test_runaway_is_not_a_failed_session(self):
        self.assertFalse(gates.session_reasons({"a": "done", "b": "done"}, {}))
        self.assertEqual(gates.failure_class("exceeded the 8000 output token maximum"), "runaway")

    def test_preflight_lines_and_report_table(self):
        import preflight
        d = tempfile.mkdtemp()
        try:
            rd, projects = test_litm.make_pf_run(d, d4=True)
            names = [r[0] for r in preflight.check(rd, projects, mcp=False)]
            self.assertTrue(any(n.startswith("runaway forks of each arm at L1 and L2") for n in names), names)
            self.assertIn("runaway forks per level (counts)", names)
        finally:
            shutil.rmtree(d)
        self.assertEqual(preflight.RUNAWAY_ARM_MAX, 0.05)
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        rows = ReportAndPreflight().synth_rows()
        for i, r in enumerate(rows):
            r["chunk"], r["fork_sid"], r["runaway"] = i % 3, "f%d" % (i // 3), (r["arm"] == "K" and r["type"] == "L2" and i % 3 == 0)
        md, out = litm4.section4(rows, {}, th, 100)
        self.assertIn("Table D4-6", "\n".join(md))
        self.assertTrue(out["runaway"]["K|L2"][0] > 0)
        self.assertEqual(out["runaway"]["A|L2"][0], 0)


class RefusedProbes(unittest.TestCase):
    """A probe fork (or a Z call) that a safety filter refuses is an outcome with class refused, not a session failure."""
    REFUSAL = {"result": "API Error: Haiku 5.5 can't help with this [bio]", "session_id": "f7", "total_cost_usd": 0.1, "usage": {"output_tokens": 4512}, "is_error": True}

    def test_a_refused_fork_is_an_outcome_left_out_of_the_accuracy(self):
        d = tempfile.mkdtemp()
        try:
            ctx, ss, prompts = SmallProbes().make_session(d, lambda n, p: {"result": "x", "session_id": None, "_refused": n == 2})
            ss.probe("L2", 0, fork_of="p1", phrasing="neutral")
            rows = [json.loads(l) for l in open(os.path.join(ctx.dir, "results.jsonl"))]
            rf = [r for r in rows if r["refused"]]
            self.assertEqual({r["chunk"] for r in rf}, {2})
            self.assertEqual(len(rf), 4)
            self.assertTrue(all(r["error_class"] == "refused" and r["m1"] is False and r["runaway"] is False and r["abstained"] is False for r in rf))
            self.assertNotEqual(ss.st.get("status"), "failed")
            self.assertEqual(ss.st["done_forks"][-1], "0:L2:neutral")
        finally:
            shutil.rmtree(d)

    def test_call_returns_a_refused_probe_and_raises_for_a_main_turn(self):
        for kind in ("fork", "z"):
            d, ctx, ss, seen, err = ProbeCapAndFailures().run_call_d4([self.REFUSAL], kind)
            try:
                self.assertIsNone(err, kind)
                self.assertEqual(len(seen), 1)                                       # no retry
                self.assertEqual(len([f for f in os.listdir(os.path.join(ctx.dir, "failed")) if f.endswith(".json")]), 1)   # the evidence is kept
                self.assertEqual(ss.st.get("safety_refusals"), 1)
            finally:
                shutil.rmtree(d)
        d, ctx, ss, seen, err = ProbeCapAndFailures().run_call_d4([self.REFUSAL], "turn")
        try:
            self.assertIsInstance(err, runner.SafetyRefusal)                          # a refused main-session turn stays a failure
        finally:
            shutil.rmtree(d)

    def rows(self, seed, n, bad, arm="A", level="L2", ph="cued"):
        return [{"seed": seed, "arm": arm, "condition": "nocompact", "type": level, "phrasing": ph, "chunk": i, "fork_sid": "f%d-%d" % (seed, i), "item": 1,
                 "runaway": False, "refused": i < bad, "m1": False, "m2": False, "m3": False} for i in range(n)]

    def test_refused_rate_and_the_abort_gate(self):
        self.assertEqual(gates.refused_rate(self.rows(1, 36, 3)), (3, 36))
        self.assertEqual(gates.runaway_rate(self.rows(1, 36, 3)), (0, 36))
        self.assertFalse([w for w in gates.abort_reasons([], self.rows(1, 36, 1), None, [1]) if "refused" in w])         # 2.8%
        self.assertTrue([w for w in gates.abort_reasons([], self.rows(1, 36, 2), None, [1]) if "refused rate 6%" in w and "seed 1" in w])   # 5.6%
        self.assertFalse([w for w in gates.abort_reasons([], self.rows(2, 36, 9), None, [1]) if "refused" in w])

    def test_refused_is_not_a_failed_session(self):
        self.assertEqual(gates.failure_class("is_error: API Error: Haiku 5.5 can't help with this."), "safety_refusal")
        self.assertFalse(gates.session_reasons({"a": "done", "b": "done"}, {}, {}))
        self.assertIn("refused", d4.ERROR_CLASSES)

    def test_pick_rule_leaves_refused_items_out_and_blocks_a_cell_above_5_percent(self):
        pr = PickRule()
        base = pr.rows({"L1": 0.9, "L2": 0.9, "L3": 0.7, "L4": 0.9})
        for i, r in enumerate(base):
            r.update(seed=1, chunk=i, fork_sid="x", runaway=False, refused=False)        # one fork for each row
        self.assertEqual(d4.decide(base)["pick"], ("m1", "L3"))
        # 20 refused A items at L3 are left out of the denominator: A stays at 70%, the cell is still in the band
        ref = [dict(r, refused=True, m1=False, m2=False, m3=False, error_class="refused", chunk=100 + i, fork_sid="r%d" % i) for i, r in enumerate(
            [r for r in base if r["arm"] == "A" and r["type"] == "L3"][:1])]
        d = d4.decide(base + ref)
        self.assertEqual(d["pick"], ("m1", "L3"))          # 1 refused fork of 21 (4.8%): allowed
        self.assertEqual(d["refused"]["A"][:2], (1, 21))
        ref2 = [dict(ref[0], chunk=200 + i, fork_sid="q%d" % i) for i in range(3)]
        d = d4.decide(base + ref + ref2)                   # 4 refused forks of 24 (17%)
        self.assertNotEqual(d["pick"], ("m1", "L3"))
        self.assertTrue([n for n in d["notes"] if "refused by a safety filter" in n and "L3" in n], d["notes"])
        self.assertIn("refused probe forks in all cells", d4.describe_decision(d))

    def test_report_tables_and_the_sensitivity_reading(self):
        th = json.load(open(os.path.join(HERE, "thresholds.json")))
        rows = ReportAndPreflight().synth_rows()
        for i, r in enumerate(rows):
            r["chunk"], r["fork_sid"], r["runaway"], r["refused"] = i % 3, "f%d" % (i // 3), False, False
        md, out = litm4.section4(rows, {}, th, 100)
        self.assertNotIn("sensitivity", out)                                          # nothing refused: no sensitivity table
        self.assertIn("Table D4-7", "\n".join(md))
        rows2 = [dict(r) for r in rows]
        for r in rows2:
            if r["arm"] == "A" and r["type"] == "L2" and r["chunk"] == 0:
                r.update(refused=True, error_class="refused", m1=False, m2=False, m3=False)
        md2, out2 = litm4.section4(rows2, {}, th, 100)
        text = "\n".join(md2)
        self.assertIn("Table D4-4s", text)
        self.assertTrue(out2["refused"]["A|L2"][0] > 0)
        self.assertEqual(out2["refused"]["K|L2"][0], 0)
        self.assertEqual(len(out2["sensitivity"]), len(out2["hypotheses"]))
        self.assertTrue("The two readings" in text)
        self.assertIsInstance(out2["readings_disagree"], list)

    def test_preflight_lines(self):
        import preflight
        d = tempfile.mkdtemp()
        try:
            rd, projects = test_litm.make_pf_run(d, d4=True)
            names = [r[0] for r in preflight.check(rd, projects, mcp=False)]
            self.assertTrue(any(n.startswith("refused forks of each arm") for n in names), names)
            self.assertIn("refused forks per level (counts)", names)
        finally:
            shutil.rmtree(d)
        self.assertEqual(preflight.REFUSED_ARM_MAX, 0.10)
        self.assertEqual(gates.REFUSED_SEED_MAX, 0.05)
        self.assertEqual(d4.REFUSED_CELL_MAX, 0.05)


class PreflightEstimates4(unittest.TestCase):
    """The preflight4 estimates against the real Haiku figures of run 20261122T162409-preflight4 (calls.jsonl, USD, 1 seed)."""
    ARMS = ("A", "B", "R", "K", "Z", "O")
    CFG = {"kind": "preflight4", "arms": list(ARMS), "conditions": ["compact", "nocompact"], "probe_design": 4, "seeds": [1], "n": [1], "phrasings": ["cued", "neutral", "nonliteral"]}
    REAL = {("A", "compact"): 0.0296, ("B", "compact"): 0.0316, ("R", "compact"): 0.0316, ("K", "compact"): 0.0334, ("Z", "compact"): 0.0044,
            ("O", "compact"): 0.0954, ("O", "nocompact"): 0.3457, ("Z", "nocompact"): 0.0466}

    def within2(self, est, real, what):
        self.assertTrue(real / 2.0 <= est <= real * 2.0, "%s: estimate %.4f, real %.4f" % (what, est, real))

    def test_every_session_estimate_is_within_a_factor_of_2_of_the_real_figure(self):
        for (arm, cond), real in self.REAL.items():
            self.within2(cost.preflight_unit(arm, cond, self.ARMS, 4), real, "%s %s" % (arm, cond))
            self.within2(cost.unit_estimate(self.CFG, arm, cond), real, "unit_estimate %s %s" % (arm, cond))
        turns, forks, scale = cost.pf4_nc_unit_parts(self.ARMS)
        self.within2(turns, 0.0694, "A-nc turns")
        self.within2(forks, 0.4203, "A-nc forks of arm A")
        self.within2(cost.preflight_unit("A", "nocompact", self.ARMS, 4), 0.4897 + 0.4203 * 3.25, "A-nc unit with the forks of B, R, K")

    def test_the_gate_that_stopped_the_run_passes(self):
        # s1-O-nc cost $0.3457 against 2.0 x its estimate and the floor (gates.session_reasons)
        e = cost.unit_estimate(self.CFG, "O", "nocompact")
        self.assertEqual(gates.session_reasons({"s1-O-nc": "done"}, {"s1-O-nc": (0.3457, e)}), [])
        self.assertEqual(gates.session_reasons({"s1-Z-nc": "done"}, {"s1-Z-nc": (0.0466, cost.unit_estimate(self.CFG, "Z", "nocompact"))}), [])
        self.assertTrue(cost.unit_estimate(self.CFG, "O", "nocompact") > 0.2)         # was $0.02

    def test_design_3_figures_are_unchanged_and_forks_of_b_r_k_have_no_unit(self):
        self.assertEqual(cost.preflight_unit("O", "nocompact", self.ARMS), cost.PF_O3)
        self.assertEqual(cost.preflight_unit("A", "nocompact", self.ARMS), cost.preflight_unit("A", "nocompact", self.ARMS, 3))
        for arm in ("B", "R", "K"):
            self.assertEqual(cost.preflight_unit(arm, "nocompact", self.ARMS, 4), 0.0)
        t4, _ = cost.estimate_preflight(self.ARMS, ("compact", "nocompact"), 4)
        self.assertEqual(cost.estimate_per_seed(self.CFG), t4)
        self.assertTrue(3.5 < t4 < 5.0, t4)

    def test_resume_takes_the_current_estimate_for_a_default_budget(self):
        import types
        d = tempfile.mkdtemp()
        old = (runner.RESULTS, runner.Ctx, runner.execute)
        try:
            os.makedirs(os.path.join(d, "r1"))
            json.dump(dict(self.CFG, budget_usd=4.82, budget_default=True, jobs=2, gates=True), open(os.path.join(d, "r1", "config.json"), "w"))
            seen = {}

            class C:
                def __init__(self, run_dir, cfg):
                    seen["cfg"] = cfg
                    self.state = {"sessions": {}}

                def save(self):
                    pass
            runner.RESULTS, runner.Ctx, runner.execute = d, C, lambda ctx: 0
            runner.cmd_resume(types.SimpleNamespace(run_id="r1", budget_usd=None, jobs=None))
            self.assertAlmostEqual(seen["cfg"]["budget_usd"], round(1.5 * cost.estimate_per_seed(self.CFG), 2))
            self.assertNotEqual(seen["cfg"]["budget_usd"], 4.82)
            self.assertEqual(json.load(open(os.path.join(d, "r1", "config.json")))["budget_usd"], seen["cfg"]["budget_usd"])
            runner.cmd_resume(types.SimpleNamespace(run_id="r1", budget_usd=9.0, jobs=None))          # an explicit budget wins and stays
            self.assertEqual(seen["cfg"]["budget_usd"], 9.0)
            runner.cmd_resume(types.SimpleNamespace(run_id="r1", budget_usd=None, jobs=None))
            self.assertEqual(seen["cfg"]["budget_usd"], 9.0)
        finally:
            runner.RESULTS, runner.Ctx, runner.execute = old
            shutil.rmtree(d)


class ActionProbes(unittest.TestCase):
    """F79: action probes, arms G and P, controls, free-form cell, trigger gate without the hook."""

    def setUp(self):
        self.script = d4.build_litm4(3)
        self.items = self.script["items"]["L2"][:3]

    def test_action_prompt_has_task_no_question_and_both_naming_forms(self):
        p = d4.action_prompt("L2", self.script, "PROBE-x", self.items, "probe-out/x")
        self.assertIn("probe-out/x/services.conf", p)
        self.assertNotIn("?", p)
        self.assertIn("`%s`" % self.items[0]["key"], p)          # odd number: named by key
        self.assertIn(self.items[1]["desc"], p)                  # even number: non-literal description
        self.assertNotIn("`%s`" % self.items[1]["key"], p)

    def test_action_grade_reads_file_lines_with_m1_only(self):
        txt = "\n".join("%d %s=%s" % (i + 1, it["attr"], it["current"] if i == 0 else "WRONG-1") for i, it in enumerate(self.items))
        g = d4.grade_action(self.items, txt, self.script)
        self.assertTrue(g[0]["m1"])
        self.assertFalse(g[1]["m1"])
        self.assertIsNone(g[0]["m2"])
        g = d4.grade_action(self.items, "", self.script)
        self.assertEqual(g[0]["error_class"], "unreadable")

    def test_arms_g_and_p_settings(self):
        self.assertIn("G", runner.FORK_CAPABLE)
        self.assertIn("P", runner.FORK_CAPABLE)
        self.assertEqual(runner.GUARD_ENV, ("HANDOFF_HISTORY_GUARD", "HANDOFF_ACTION_GUARD"))
        self.assertIn("HANDOFF_HISTORY_GUARD", runner.OVERRIDE_ENV)
        self.assertIn("HANDOFF_ACTION_GUARD", runner.OVERRIDE_ENV)

    def test_guard_kind_is_found_in_a_hook_record(self):
        recs = [{"type": "user", "message": {"content": "PROBE-m go"}}, {"type": "user", "message": {"content": "Lossless Handoff history check: look at the history of x."}}]
        self.assertIn("guard", grade.search_used(recs, "PROBE-m")[1])

    def test_controls_state_each_fact_one_time(self):
        c = d4.add_controls(self.script, 3)
        ct = c["items"]["CT"]
        self.assertEqual(len(ct), 4)
        for it in ct:
            self.assertEqual(len(it["stmts"]), 1)
            n = sum(t.get("prompt", "").count(it["stmts"][0]) for t in c["plan"])
            self.assertEqual(n, 1)

    def test_free_form_has_no_backticks_and_30_percent_assistant_text(self):
        f = d4.free_form(self.script, 3)
        facts = [t for t in f["plan"] if t["kind"] == "fact"]
        for it in [i for lv in f["items"].values() for i in lv]:
            self.assertNotIn("`", "".join(it["stmts"]))
        n_ast = sum(len(t.get("assistant_text", [])) for t in facts)
        total = sum(len(i["stmts"]) for lv in f["items"].values() for i in lv)
        self.assertAlmostEqual(n_ast / float(total), 0.30, delta=0.02)

    def test_cost_counts_four_phrasings_and_g_p_factors(self):
        three = cost.estimate3(1, ["A", "O", "Z"], [1], ["nocompact"], ["cued", "neutral", "nonliteral"], design=4)[0]
        four = cost.estimate3(1, ["A", "O", "Z"], [1], ["nocompact"], ["cued", "neutral", "nonliteral", "action"], design=4)[0]
        self.assertGreater(four, three)
        self.assertGreater(cost.ARM_FACTOR["G"], cost.ARM_FACTOR["K"])
        self.assertGreater(cost.ARM_FACTOR["P"], cost.ARM_FACTOR["K"])

    def test_thresholds_register_h12_to_h17_before_data(self):
        d = json.load(open(os.path.join(HERE, "thresholds.json")))["litm"]["design4"]
        for k in ("h12_min_share", "h13_min_points", "h14_cued_min_points", "h15_control_fire_max", "h15_replay_fire_max", "h16_fire_ratio_min", "h17_min_points"):
            self.assertIn(k, d)
        self.assertEqual(d["h13_primary"], "action")

    def test_trigger_gate_and_replay_report_not_available_without_the_hook(self):
        import retrieval_gate as rg
        with tempfile.TemporaryDirectory() as hd:
            _r, line, ok = rg.trigger_gate([3], "L2", hd)
            self.assertIsNone(ok)
            self.assertIn("NOT AVAILABLE", line)
            self.assertIsNone(rg.replay(hd, hd))

    def test_oracle_replies_have_four_kinds(self):
        import retrieval_gate as rg
        its = self.script["items"]["L4"]
        kinds = set()
        for it in its:
            kinds |= set(rg.oracle_replies(it, its, self.script))
        self.assertEqual(kinds, set(rg.REPLY_KINDS))


class OracleCheckAndHaikuScope(unittest.TestCase):
    def rows(self, lv, arm="O", n=8, wrong=0, runaway=0):
        out = []
        for i in range(n):
            out.append({"arm": arm, "type": lv, "phrasing": "cued", "condition": "nocompact", "seed": 1, "chunk": i, "fork_sid": "f%s%d" % (lv, i),
                        "m1": i >= wrong, "runaway": i < runaway, "refused": False, "parse_ok": True})
        return out

    def test_preset_has_a_cost_estimate(self):
        est = cost.estimate_per_seed({"kind": "litm4-oracle-check", "arms": ["O"], "n": [1], "conditions": ["nocompact"],
                                      "phrasings": ["cued", "neutral", "nonliteral", "action"], "probe_design": 4})
        self.assertTrue(0 < est < 4, est)

    def test_parse_gate_leaves_out_runaway_rows(self):
        import gates
        rows = self.rows("L1", arm="A", n=10)
        for r in rows[:5]:
            r.update({"runaway": True, "parse_ok": False})
        sessions = []
        why = gates.abort_reasons(sessions, rows, None, {1})
        self.assertFalse([w for w in (why or []) if "parse error" in w])


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.script = d4.build_litm4(3)
        self.items = self.script["items"]["L2"][:1]

    def test_action_line_forms(self):
        v = self.items[0]["current"]
        for form in ("1 queue=%s", "1. queue=%s", "1) queue=%s", "1: queue=%s", "1 svc queue=%s", "1. svc-a queue=%s"):
            g = d4.grade_action(self.items, form % v, self.script)
            self.assertTrue(g[0]["m1"], form)
        for bad in ("queue=%s", "one queue=%s"):
            g = d4.grade_action(self.items, bad % v, self.script)
            self.assertEqual(g[0]["error_class"], "unreadable", bad)

    def test_oracle_check_per_phrasing_refused_and_tamper(self):
        def rows(lv, ph, **kw):
            return [dict({"arm": "O", "type": lv, "phrasing": ph, "condition": "nocompact", "seed": 1, "chunk": i, "fork_sid": "f%s%s%d" % (lv, ph, i),
                          "m1": True, "runaway": False, "refused": False, "parse_ok": True}, **kw) for i in range(6)]
        full = sum((rows(lv, ph) for lv in d4.D4_LEVELS for ph in d4.ALL_PHRASINGS), [])
        self.assertTrue(d4.oracle_check(full)[0])
        missing = [r for r in full if not (r["type"] == "L2" and r["phrasing"] == "action")]
        ok, lines = d4.oracle_check(missing)
        self.assertFalse(ok)
        self.assertTrue(any("L2 action: NO DATA" in l for l in lines))
        ref = [dict(r, refused=True) if (r["type"] == "L1" and r["phrasing"] == "cued" and r["chunk"] < 2) else r for r in full]
        self.assertFalse(d4.oracle_check(ref)[0])
        tam = [dict(r, tamper=["x"]) if r is full[0] else r for r in full]
        self.assertFalse(d4.oracle_check(tam)[0])

    def test_edit_rule_has_the_double_slash_and_no_write_rule(self):
        s = runner.Session.__new__(runner.Session)
        s.arm = "O"
        tools = s.allowed_tools("Users/me/proj/probe-out/m1")
        self.assertIn("Edit(//Users/me/proj/probe-out/m1/**)", tools)
        self.assertNotIn("Write(", tools)
        self.assertNotIn("Bash(python3:*)", tools)


if __name__ == "__main__":
    unittest.main(verbosity=1)
