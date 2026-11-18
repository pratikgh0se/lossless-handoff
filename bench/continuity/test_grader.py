#!/usr/bin/env python3
"""Self-test of the grader with synthetic answers. It uses no model and no network.
Run: python3 bench/continuity/test_grader.py   (exit 0 when every check passes)
"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen, grade


def js(answers):
    return "Here you go:\n```json\n" + json.dumps({"answers": answers}) + "\n```"


class Base(unittest.TestCase):
    def setUp(self):
        self.s = gen.build(7)
        self.I = self.s["items"]

    def run_all(self, ptype, make_right, items=None):
        items = items or self.I[ptype]
        right = grade.grade(ptype, items, js([make_right(x) for x in items]))
        self.assertTrue(all(r["correct"] and r["parse_ok"] for r in right), right)
        return items


class SimpleTypes(Base):
    def test_R_D_S_U_right_and_wrong(self):
        for t in ("R", "D", "S", "U"):
            items = self.I[t]
            self.run_all(t, lambda x: {"id": x["id"], "answer": x["answer"]})
            wrong = grade.grade(t, items, js([{"id": x["id"], "answer": x["answer"] + "x"} for x in items]))
            self.assertTrue(all(r["parse_ok"] and not r["correct"] for r in wrong), t)
            one = grade.grade(t, items, js([{"id": 1, "answer": items[0]["answer"]}]))
            self.assertEqual([r["correct"] for r in one], [True, False, False])
            self.assertEqual([r["parse_ok"] for r in one], [True, False, False])

    def test_F(self):
        items = self.s["fresh"]["3"]
        self.run_all("F", lambda x: {"id": x["id"], "answer": x["answer"]}, items)
        bad = grade.grade("F", items, js([{"id": x["id"], "answer": "nope"} for x in items]))
        self.assertFalse(any(r["correct"] for r in bad))

    def test_R_accepts_tag_prefix_only(self):
        x = self.I["R"][0]
        self.assertTrue(grade.grade("R", [x], js([{"id": 1, "answer": "TAG-" + x["answer"]}]))[0]["correct"])
        self.assertFalse(grade.grade("R", [x], js([{"id": 1, "answer": "TAG-" + x["answer"][:-1]}]))[0]["correct"])

    def test_exact_string_rules(self):
        x = self.I["S"][0]
        a = x["answer"]
        ok = lambda v: grade.grade("S", [x], js([{"id": 1, "answer": v}]))[0]["correct"]
        self.assertTrue(ok("  " + a + "  "))        # trim
        self.assertTrue(ok("`" + a + "`"))          # one set of backticks
        self.assertTrue(ok('"' + a + '"'))          # one set of quotes
        self.assertFalse(ok("``" + a + "``"))       # two sets: no
        self.assertFalse(ok(a.lower() if a != a.lower() else a.upper()))   # case counts
        self.assertFalse(ok(a + " --extra"))
        self.assertFalse(ok(["list"]))              # wrong JSON type
        self.assertFalse(ok(None))

    def test_any_token_noise_fails(self):
        # a lazy answer that picks a token of the right form from another item must fail
        items = self.I["D"]
        r = grade.grade("D", items, js([{"id": x["id"], "answer": items[0]["answer"]} for x in items]))
        self.assertEqual([x["correct"] for x in r], [True, False, False])


class ReplacedType(Base):
    def mk(self, x, cur=None, rep=None):
        return {"id": x["id"], "current": x["new"] if cur is None else cur, "replaced": [x["old"]] if rep is None else rep}

    def test_X(self):
        items = self.I["X"]
        self.run_all("X", self.mk)
        g = lambda **kw: grade.grade("X", items[:1], js([self.mk(items[0], **kw)]))[0]
        x = items[0]
        self.assertFalse(g(cur=x["old"])["correct"])                 # old value as current
        self.assertFalse(g(rep=[])["correct"])                       # old value not listed
        self.assertFalse(g(rep=["nope"])["correct"])
        self.assertFalse(g(cur="nope")["correct"])
        self.assertTrue(g(rep=[x["old"], "extra"])["correct"])       # extra items are allowed
        self.assertTrue(g(rep=x["old"])["correct"])                  # one string counts as a list of one
        self.assertFalse(g(cur=x["old"], rep=[x["old"]])["correct"])
        bad = grade.grade("X", items[:1], js([{"id": 1, "current": x["new"]}]))[0]
        self.assertFalse(bad["parse_ok"])
        self.assertFalse(bad["correct"])


class DeadEnd(Base):
    def test_E(self):
        items = self.I["E"]
        self.run_all("E", lambda x: {"id": x["id"], "proceed": False, "reason_code": x["ecode"]})
        x = items[0]
        g = lambda **kw: grade.grade("E", items[:1], js([dict({"id": 1, "proceed": False, "reason_code": x["ecode"]}, **kw)]))[0]
        self.assertTrue(g(proceed="false")["correct"])          # bool as text is read
        self.assertFalse(g(proceed=True)["correct"])            # says proceed
        self.assertFalse(g(reason_code="ERR-0000-AAAAAA")["correct"])
        self.assertFalse(g(reason_code="")["correct"])
        self.assertFalse(g(proceed="maybe")["parse_ok"])


class ConflictFlag(Base):
    def test_C_flag_and_earlier(self):
        items = self.I["C"]
        self.run_all("C", lambda x: {"id": x["id"], "conflict": True, "earlier": x["tok1"]})
        x = items[0]
        g = lambda **kw: grade.grade("C", items[:1], js([dict({"id": 1, "conflict": True, "earlier": x["tok1"]}, **kw)]))[0]
        self.assertFalse(g(conflict=False)["correct"])              # no flag
        self.assertFalse(g(earlier="")["correct"])                  # flag but no earlier value
        self.assertFalse(g(earlier=x["tok2"])["correct"])           # flag with the new value
        self.assertFalse(g(earlier="nope")["correct"])
        self.assertTrue(g(conflict="true")["correct"])
        self.assertTrue(g(earlier="`" + x["tok1"] + "`")["correct"])
        self.assertFalse(g(conflict=None)["parse_ok"])


class ParseHandling(Base):
    def test_prose_around_json_and_first_object(self):
        x = self.I["D"][0]
        raw = 'Sure. {"answers":[{"id":1,"answer":"%s"}]} and then {"answers":[{"id":1,"answer":"zzz"}]}' % x["answer"]
        self.assertTrue(grade.grade("D", [x], raw)[0]["correct"])

    def test_no_json_is_wrong_and_flagged(self):
        for t in gen.TYPES:
            r = grade.grade(t, self.I[t], "I do not know.")
            self.assertTrue(all(not a["correct"] and not a["parse_ok"] for a in r), t)
        self.assertFalse(grade.grade("D", self.I["D"], None)[0]["parse_ok"])

    def test_fallback_regex(self):
        items = self.I["D"]
        raw = "1) %s 2) %s 3) %s" % tuple(x["answer"] for x in items)
        r = grade.grade("D", items, raw)
        self.assertTrue(all(a["correct"] and not a["parse_ok"] for a in r))      # right, but parse_ok is false
        r = grade.grade("D", items, "only one: " + items[0]["answer"])
        self.assertFalse(any(a["correct"] for a in r))
        r = grade.grade("X", self.I["X"], "%s %s" % (self.I["X"][0]["new"], self.I["X"][0]["old"]))
        self.assertFalse(any(a["correct"] for a in r))                           # no fallback for X

    def test_id_as_text(self):
        x = self.I["D"][0]
        self.assertTrue(grade.grade("D", [x], js([{"id": "1", "answer": x["answer"]}]))[0]["correct"])


class LookupFlag(unittest.TestCase):
    def rec(self, role, content):
        return {"type": role, "message": {"role": role, "content": content}}

    def test_lookup(self):
        m = "PROBE-1-R-3"
        hist = [self.rec("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "cat a.jsonl"}}])]
        probe = self.rec("user", [{"type": "text", "text": "[%s] question" % m}])
        use = self.rec("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "grep -r X ~/.claude/projects/foo"}}])
        plain = self.rec("assistant", [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/pad.txt"}}])
        self.assertTrue(grade.lookup_used(hist + [probe, use], m))
        self.assertFalse(grade.lookup_used(hist + [probe, plain], m))     # history before the probe does not count
        self.assertFalse(grade.lookup_used(hist, m))


class GeneratorChecks(unittest.TestCase):
    def test_repeatable_and_distinct(self):
        self.assertEqual(gen.build(5), gen.build(5))
        self.assertNotEqual(gen.build(5), gen.build(6))
        self.assertNotEqual(gen.build(5, "1"), gen.build(5, "2"))

    def test_answers_unique_and_strong(self):
        s = gen.build(11)
        vals = []
        for t, lst in s["items"].items():
            for it in lst:
                vals += [it[k] for k in ("answer", "old", "new", "ecode", "tok1", "tok2") if k in it]
        for lst in s["fresh"].values():
            vals += [it["answer"] for it in lst]
        self.assertEqual(len(vals), len(set(vals)))
        self.assertEqual(set(vals), set(s["secrets"]))
        self.assertTrue(all(len(v) >= 12 for v in vals))

    def test_probe_prompt_has_no_answer(self):
        s = gen.build(13)
        for t in gen.TYPES + ["F"]:
            p = gen.probe_prompt(t, s, "PROBE-13", 3)
            ans = [v for v in s["secrets"] if v in p]
            if t == "C":
                self.assertEqual(sorted(ans), sorted(it["tok2"] for it in s["items"]["C"]))   # only the new value is shown
            else:
                self.assertEqual(ans, [], t)
            for it in s["items"]["S"]:
                self.assertNotIn(it["answer"], p)

    def test_filler_holds_no_answer(self):
        s = gen.build(17)
        for t in range(40):
            for j in range(2):
                _, txt = gen.filler_file(17, t, j)
                self.assertFalse([v for v in s["secrets"] if v in txt])

    def test_u_file_holds_key(self):
        s = gen.build(19)
        for it in s["items"]["U"]:
            self.assertIn("ledger-key: " + it["answer"], it["content"])

    def test_plant_turns_hold_answers_in_user_text_only(self):
        s = gen.build(23)
        text = " ".join(t["prompt"] for t in s["plant_turns"])
        for t in ("R", "D", "S", "C"):
            for it in s["items"][t]:
                self.assertIn(it["answer"] if "answer" in it else it["tok1"], text)
        for it in s["items"]["U"]:
            self.assertNotIn(it["answer"], text)          # the key is in a file only


if __name__ == "__main__":
    unittest.main(verbosity=1)
