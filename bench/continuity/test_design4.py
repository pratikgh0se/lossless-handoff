#!/usr/bin/env python3
"""Self-test of probe design 4 (update chains with interference). No model, no network, no claude process.
Run: python3 bench/continuity/test_design4.py"""
import hashlib, json, os, re, shutil, sys, tempfile, unittest

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
                self.assertEqual(len(keys), len(set(keys)), (sd, "one key twice in a turn"))
                for k in keys:
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
                    self.assertTrue(all(o in ("reject", "scoped") for o in kinds[last_update + 1:]))     # lures come after the last update
                    self.assertEqual(len(kinds[last_update + 1:]), spec["rejects"] + spec["scoped"])
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
            self.assertLessEqual(d4.last_mention_rate(sc, "L3"), 0.5, sd)
            self.assertLessEqual(d4.last_mention_rate(sc, "L4"), 0.5, sd)
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
        self.assertEqual(g["lag"], len(v) - 1 - 0)
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
        self.assertEqual((d["h8_dose_min_points"], d["h9_interference_min_share"], d["h10_length_min_points"], d["h11_harm_max_points"]), (15, 0.60, 15, 5))
        self.assertEqual(d["pick"]["metric"], None)
        self.assertEqual(d["gates"]["o_min_accuracy"], 0.95)
        self.assertEqual((d["chains_per_seed"], d["statements_per_seed"]), (39, 276))
        self.assertIn("H1 and H1n use the primary cell", d["kept"])
        self.assertEqual(litm4.pick_cell(th), (("m1", "L3"), False))
        th["litm"]["design4"]["pick"] = {"metric": "M2", "level": "L2"}
        self.assertEqual(litm4.pick_cell(th), (("m2", "L2"), True))

    def test_calibration_cost_uses_the_real_smoke_figures(self):
        self.assertEqual(cost.SMOKE_REAL["main_nc_75_turns"], 3.055)
        self.assertAlmostEqual(cost.SMOKE_REAL["forks_15"]["A"], 0.671)
        self.assertAlmostEqual(cost.d4_nc_main(), 4.28, delta=0.05)
        tot, _l, per = cost.estimate3(2, ["A", "O", "Z"], [1], ["nocompact"], design=4)
        self.assertTrue(9 < tot < 14, tot)
        self.assertTrue(5.0 < per < 6.0, per)
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
        self.assertEqual(len([h for h in out["hypotheses"]]), 8)
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
        self.assertEqual([x[1] for x in res if "oracle (arm O) M1" in x[0]], ["FAIL"])
        self.assertIn("L3", [x[2] for x in res if "oracle (arm O) M1" in x[0]][0])
        res = self.run_check(d4_z_high=True)
        self.assertEqual([x[1] for x in res if "amnesia (arm Z)" in x[0]], ["FAIL"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
