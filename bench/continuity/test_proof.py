#!/usr/bin/env python3
"""Tests of the proof test: the preset, the seed check, the pass rule (PASS, FAIL, INVALID) and the deterministic examples. No model call."""
import json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import proof, runner  # noqa: E402

TH = json.load(open(os.path.join(HERE, "thresholds.json")))
SEEDS = [2001, 2002, 2003, 2004]
TYPES = proof.TYPES
ITEMS = 10


def row(seed, arm, typ, item, n, correct, **kw):
    r = {"seed": seed, "arm": arm, "type": typ, "item": item, "n_target": n, "n_actual": n, "correct": correct, "parse_ok": True, "phrasing": "cued",
         "condition": "compact", "parsed": {"id": item, "answer": "x-%s" % ("ok" if correct else "bad")}, "raw": "{}", "used_lookup": False, "search_kinds": []}
    r.update(kw)
    return r


def make(a_by_n, b_by_n, o=1.0, z=0.0, fresh=1.0, parse=True, b_seed_bump=None):
    """Synthetic rows. a_by_n and b_by_n: {n: accuracy}. Items are spread so that every seed gets the same accuracy, unless b_seed_bump is {seed: delta}."""
    rows = []
    for s in SEEDS:
        for typ in TYPES:
            for i in range(1, ITEMS + 1):
                for arm, d in (("A", a_by_n), ("B", b_by_n)):
                    for n, acc in d.items():
                        acc = acc + ((b_seed_bump or {}).get(s, 0) if arm == "B" else 0)
                        rows.append(row(s, arm, typ, i, n, i <= round(acc * ITEMS), parse_ok=parse))
                rows.append(row(s, "O", typ, i, 0, i <= round(o * ITEMS)))
                rows.append(row(s, "Z", typ, i, 0, i <= round(z * ITEMS)))
        for arm in ("A", "B", "O"):
            rows.append(row(s, arm, "F", 1, 0, fresh >= 0.95))
    return rows


def sessions():
    return [{"arm": a, "seed": s, "condition": "compact", "missed": []} for s in SEEDS for a in ("A", "B")]


class Preset(unittest.TestCase):
    def test_kind(self):
        k = runner.KINDS["proof"]
        self.assertEqual(k["arms"], "A,B,O,Z")
        self.assertEqual(k["conditions"], "compact")
        self.assertEqual(k["n"], [1, 3, 10])
        self.assertEqual(k["seeds"], 4)
        self.assertEqual(k["seed_file"], "seeds-proof.txt")
        self.assertTrue(k["selfcheck"])
        self.assertEqual(k["phrasings"], runner.KINDS["litm4-pilot"]["phrasings"])
        self.assertEqual(k["probe_design"], 4)

    def test_run_sh_knows_proof(self):
        self.assertIn("proof", open(os.path.join(HERE, "run.sh")).read().split("case")[1].split(")")[0])

    def test_dry_run_estimate_and_budget(self):
        p = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), "run", "proof", "--dry-run"], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertIn("$19.59 per seed", p.stdout)
        self.assertIn("$16.85 per seed", p.stdout)
        import re
        total = float(re.search(r"total about \$(\d+)", p.stdout).group(1))
        self.assertTrue(160 <= total <= 170, total)
        budget = float(re.search(r"budget_usd: ([\d.]+)", p.stdout).group(1))
        self.assertAlmostEqual(budget, round(1.5 * (19.59 + 16.85 + 4.0836 + 0.5832) * 4, 1), delta=1.5)

    def test_selfcheck_failure_stops_before_any_probe(self):
        import inspect
        src = inspect.getsource(runner.execute)
        self.assertIn("proof test stopped before any probe", src)
        self.assertLess(src.index('cfg.get("selfcheck")'), src.index("ThreadPoolExecutor"))

    def test_thresholds_block(self):
        p = TH["proof"]
        self.assertEqual((p["p1_min_drop_points"], p["p1_min_seeds"], p["p2_min_gain_points"], p["p2_ci_lower_above"], p["p2_min_seeds"], p["seeds"]), (25, 3, 15, 0, 3, 4))
        self.assertEqual((p["n_low"], p["n_high"]), (1, 10))


class Seeds(unittest.TestCase):
    def test_proof_seeds_are_new_and_four(self):
        seeds = [int(x) for x in open(os.path.join(HERE, "seeds-proof.txt")).read().split()]
        self.assertEqual(len(seeds), 4)
        self.assertEqual(proof.check_seeds(seeds, HERE), [])

    def test_clash_with_a_seeds_file_and_with_a_run(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "seeds-dev.txt"), "w").write("1 2 2001\n")
        os.makedirs(os.path.join(d, "results", "r1"))
        json.dump({"seeds": [2002, 7]}, open(os.path.join(d, "results", "r1", "config.json"), "w"))
        open(os.path.join(d, "seeds-proof.txt"), "w").write("2001 2002 2003 2004\n")      # the proof file itself is skipped
        problems = proof.check_seeds(SEEDS, d)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("2001" in x and "seeds-dev.txt" in x for x in problems))
        self.assertTrue(any("2002" in x and "r1" in x for x in problems))

    def test_wrong_count_and_duplicates(self):
        d = tempfile.mkdtemp()
        self.assertTrue(proof.check_seeds([1, 2, 3], d))
        self.assertTrue(proof.check_seeds([9, 9, 8, 7], d))
        self.assertEqual(proof.check_seeds([5001, 5002, 5003, 5004], d), [])

    def test_build_cfg_refuses_used_seeds(self):
        import argparse
        ap = argparse.ArgumentParser()
        runner.parse_common(ap)
        a = ap.parse_args(["--seed-file", os.path.join(HERE, "seeds-dev.txt"), "--seeds", "4"])
        with self.assertRaises(SystemExit) as cm:
            runner.build_cfg("proof", a)
        self.assertIn("must be new", str(cm.exception))


class Rule(unittest.TestCase):
    def v(self, rows):
        return proof.verdict(rows, sessions(), TH)

    def test_pass(self):
        v = self.v(make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.8}))
        self.assertEqual(v["verdict"], "PASS", v)
        self.assertEqual((v["p1"]["result"], v["p2"]["result"]), ("PASS", "PASS"))
        self.assertAlmostEqual(v["p1"]["drop_points"], 40, delta=1)
        self.assertEqual(v["p1"]["seeds_with_drop"], 4)
        self.assertGreater(v["p2"]["ci_lower_points"], 0)

    def test_fail_p1_when_plain_does_not_lose_enough(self):
        v = self.v(make({1: 0.8, 3: 0.8, 10: 0.7}, {1: 0.8, 3: 0.8, 10: 0.9}))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual((v["p1"]["result"], v["p2"]["result"]), ("FAIL", "PASS"))

    def test_fail_p2_when_the_gain_is_small(self):
        v = self.v(make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.5}))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual((v["p1"]["result"], v["p2"]["result"]), ("PASS", "FAIL"))

    def test_fail_p2_when_b_beats_a_in_too_few_seeds(self):
        rows = make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.8}, b_seed_bump={2001: -0.6, 2002: -0.6})      # B below A in 2 seeds
        v = self.v(rows)
        self.assertEqual(v["p2"]["seeds_b_ge_a"], 2)
        self.assertEqual(v["p2"]["result"], "FAIL")
        self.assertEqual(v["verdict"], "FAIL")

    def test_invalid_when_a_gate_fails(self):
        for kw, name in ((dict(o=0.5), "Oracle"), (dict(z=0.4), "Amnesia"), (dict(fresh=0.0), "Fresh control"), (dict(parse=False), "Parse rate")):
            v = self.v(make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.8}, **kw))
            self.assertEqual(v["verdict"], "INVALID", name)
            self.assertEqual((v["p1"]["result"], v["p2"]["result"]), ("INVALID", "INVALID"))
            self.assertTrue(any(name in g for g in v["invalid_gates"]), (name, v["invalid_gates"]))

    def test_invalid_on_missed_checkpoints_and_refusals(self):
        rows = make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.8})
        ss = sessions()
        ss[0]["missed"] = [10]
        ss[1]["missed"] = [10]
        v = proof.verdict(rows, ss, TH)
        self.assertEqual(v["verdict"], "INVALID")
        self.assertTrue(any("Missed" in g for g in v["invalid_gates"]))
        for r in rows:       # refused forks: the rows of one seed, every fork refused
            if r["seed"] == 2001:
                r["refused"] = True
        v = proof.verdict(rows, sessions(), TH)
        self.assertEqual(v["verdict"], "INVALID")

    def test_invalid_with_a_missing_seed_or_no_data(self):
        rows = [r for r in make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.8}) if r["seed"] != 2004]
        v = self.v(rows)
        self.assertEqual(v["verdict"], "INVALID")
        self.assertTrue(any("Seeds with" in g for g in v["invalid_gates"]))
        self.assertEqual(proof.verdict([], [], TH)["verdict"], "INVALID")


class Examples(unittest.TestCase):
    def build(self):
        rows, scripts = [], {}
        for s in (2002, 2001):       # input order is not the output order
            scripts[s] = {"items": {t: [{"id": i, "question": "Q %s %s %s" % (s, t, i), "answer": "ans-%s-%s-%s" % (s, t, i)} for i in range(1, 5)] for t in ("R", "S")}}
            for t in ("S", "R"):
                for i in (4, 3, 2, 1):
                    a_ok = i in (1, 2)      # items 1 and 2: A right. items 3 and 4: A wrong
                    b_ok = i in (2, 3, 4)    # B right at 2, 3, 4: so A wrong B right = 3, 4; B wrong A right = 1; both right = 2
                    rows.append(row(s, "A", t, i, 10, a_ok))
                    rows.append(row(s, "B", t, i, 10, b_ok, used_lookup=(i == 3), search_kinds=["search"] if i == 3 else []))
                    rows.append(row(s, "A", t, i, 1, True))
        return rows, scripts

    def test_deterministic_order_limit_and_all_b_wrong(self):
        rows, scripts = self.build()
        shown, total, ba = proof.examples(rows, scripts, n=10, limit=5)
        self.assertEqual(total, 8)
        self.assertEqual(len(shown), 5)
        self.assertEqual([(e["seed"], e["item"], e["type"]) for e in shown], [(2001, 3, "R"), (2001, 3, "S"), (2001, 4, "R"), (2001, 4, "S"), (2002, 3, "R")])
        self.assertEqual([(e["seed"], e["item"], e["type"]) for e in ba], [(2001, 1, "R"), (2001, 1, "S"), (2002, 1, "R"), (2002, 1, "S")])
        rows2 = list(reversed(rows))
        self.assertEqual(proof.examples(rows2, scripts, n=10, limit=5), (shown, total, ba))
        e = shown[0]
        self.assertEqual((e["question"], e["expected"], e["b_search"]), ("Q 2001 R 3", "ans-2001-R-3", True))
        self.assertFalse(shown[2]["b_search"])

    def test_section_text(self):
        rows, scripts = self.build()
        md, v = proof.section(make({1: 0.8, 3: 0.6, 10: 0.4}, {1: 0.8, 3: 0.8, 10: 0.8}), sessions(), {"n": [1, 3, 10]}, TH, {})
        text = "\n".join(md)
        self.assertTrue(text.startswith("## Proof verdict"))
        self.assertIn("Verdict: PASS", text)
        self.assertIn("used 4 seeds and is a small benchmark, not the full suite", text)
        self.assertIn("## Side-by-side examples", text)
        self.assertIn("P1 loss", text)
        self.assertIn("P2 fix", text)
        self.assertLess(text.index("## Proof verdict"), text.index("## Side-by-side examples"))
        md2, _ = proof.section(rows, sessions(), {"n": [1, 3, 10]}, TH, scripts)
        self.assertIn("B wrong and A right (all 4, for honesty)", "\n".join(md2))

    def test_report_puts_the_proof_section_first(self):
        src = open(os.path.join(HERE, "report.py")).read()
        self.assertIn('cfg.get("kind") == "proof"', src)
        self.assertIn("head[:1] + [\"\"] + pmd", src)


if __name__ == "__main__":
    unittest.main()


class Recheck(unittest.TestCase):
    def test_preset(self):
        k = runner.KINDS["proof-recheck"]
        for key in ("window", "n", "conditions", "phrasings", "probe_design", "selfcheck", "model", "seeds", "seed_file"):
            self.assertEqual(k[key], runner.KINDS["proof"][key])
        self.assertEqual(k["arms"], "B")
        self.assertIn("proof-recheck", open(os.path.join(HERE, "run.sh")).read().split("case")[1].split(")")[0])

    def test_dry_run_allows_used_seeds_and_costs_about_70(self):
        import re
        p = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), "run", "proof-recheck", "--dry-run"], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertIn("kind proof-recheck", p.stdout)
        total = float(re.search(r"total about \$(\d+)", p.stdout).group(1))
        self.assertTrue(66 <= total <= 74, total)

    def test_proof_still_refuses_used_seeds_check(self):
        import inspect
        self.assertIn('kind == "proof"', inspect.getsource(runner.build_cfg))

    def _rows(self, neutral_ok, cued_ok=1.0):
        rows = []
        for s in SEEDS:
            for typ in proof.RECHECK_TYPES:
                for i in range(10):
                    for n in (1, 10):
                        rows.append(row(s, "B", typ, i, n, i < 10 * cued_ok))
                        rows.append(row(s, "B", typ, i, n, i < 10 * neutral_ok, phrasing="neutral"))
                        rows.append(row(s, "A", typ, i, n, False))      # other arms and other types are ignored
            rows.append(row(s, "B", "C", 0, 1, False))
        return rows

    def test_recheck_section_without_compare(self):
        md, out = proof.recheck_section(self._rows(0.9), {"b_recall_rules": True})
        text = "\n".join(md)
        self.assertIn("## Recheck", text)
        self.assertNotIn("PASS", text)
        self.assertNotIn("FAIL", text)
        self.assertEqual(out["by_n"][1]["neutral"], (180, 200))
        self.assertEqual(out["by_n"][10]["cued"], (200, 200))
        self.assertIn("(90%)", text)
        self.assertIn("Marker .recall-rules in arm B: yes", text)

    def test_recheck_section_compare_and_paired_difference(self):
        old = self._rows(0.8)
        md, out = proof.recheck_section(self._rows(0.9), {"compare": "RUN-OLD"}, old)
        text = "\n".join(md)
        self.assertEqual(out["by_n"][1]["neutral_compare"], (160, 200))
        self.assertIn("RUN-OLD", text)
        for s in SEEDS:
            self.assertAlmostEqual(out["per_seed"][s][-2], 10.0)      # neutral, all N
            self.assertAlmostEqual(out["per_seed"][s][-1], 0.0)       # cued, all N
        self.assertAlmostEqual(out["mean_seed_diff_neutral"], 10.0)
        self.assertIn("+10.0", text)
