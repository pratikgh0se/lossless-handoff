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
            self.assertEqual([r["correct"] for r in one], [True] + [False] * (len(items) - 1))
            self.assertEqual([r["parse_ok"] for r in one], [True] + [False] * (len(items) - 1))

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
        self.assertEqual([x["correct"] for x in r], [True] + [False] * (len(items) - 1))


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


class OrderType(Base):
    def test_M(self):
        it = self.I["M"][0]
        o = it["order"]
        g = lambda v: grade.grade("M", [it], js([{"id": 1, "order": v}]))[0]
        self.assertTrue(g(o)["correct"])
        self.assertTrue(g(["`%s`" % x for x in o])["correct"])        # one set of backticks
        self.assertFalse(g([o[1], o[0], o[2]])["correct"])              # wrong order
        self.assertFalse(g(list(reversed(o)))["correct"])
        self.assertFalse(g(o[:2])["correct"])                           # missing id
        self.assertFalse(g(o + ["extra"])["correct"])                   # extra id
        self.assertFalse(g([o[0], o[1], "nope"])["correct"])
        self.assertTrue(g(o[:2])["parse_ok"])                           # parsed, but wrong
        self.assertFalse(g("not a list")["parse_ok"])
        self.assertFalse(g([1, 2, 3])["parse_ok"])

    def test_M_plants(self):
        s = self.s
        it = s["items"]["M"][0]
        self.assertEqual(len(set(it["order"])), 3)
        self.assertEqual(len(it["plants"]), 3)
        ev = s["events"]
        self.assertTrue(any(it["plants"][0] in e["prompt"] and e["epoch"] == 0 for e in ev))      # first in epoch 0
        self.assertTrue(any(it["plants"][2] in e["prompt"] and e["epoch"] == 2 for e in ev))     # last in epoch 2
        text = " ".join(e["prompt"] for e in ev if e["epoch"] == 0)
        self.assertNotIn(it["plants"][1], text)                               # others come in later epochs
        self.assertNotIn(it["order"][2], text)
        self.assertEqual(gen.GEN_VERSION, "4")


class V4Design(Base):
    def test_load_decoys_and_epochs(self):
        s = self.s
        for ty in ("R", "D", "X", "S", "E", "C"):
            its = s["items"][ty]
            self.assertEqual(len(its), 4, ty)
            self.assertEqual([x["epoch"] for x in its], gen.ITEM_EPOCHS)
            self.assertTrue(all(len(x["decoys"]) == 2 for x in its), ty)
        # decoys look alike: user-stated decoys share the noun of the target topic
        for x in s["items"]["D"]:
            noun = x["topic"].split("-")[0]
            self.assertTrue(all("`%s-" % noun in d for d in x["decoys"]))

    def test_select_oldest_first_and_age(self):
        s = self.s
        a = gen.select(s, "D", 2)
        self.assertEqual([x["epoch"] for x in a], [0, 1, 2])
        self.assertEqual([x["epoch"] for x in gen.select(s, "D", 0)], [0])
        self.assertEqual(len(gen.select(s, "D", 9)), 4)
        self.assertEqual(gen.select(s, "M", 1), [])          # M needs epoch 2
        self.assertEqual(len(gen.select(s, "M", 2)), 1)
        # probe text lists only the chosen items, oldest first, and holds no answer
        p = gen.probe_prompt("D", s, "PROBE-x", 3, items=a)
        self.assertEqual(p.count("\n1. ") + p.count("\n2. ") + p.count("\n3. "), 3)
        self.assertNotIn(s["items"]["D"][3]["topic"], p)

    def test_user_text_is_long(self):
        s = self.s
        for e in s["events"]:
            if e["kind"] in ("stmt",):
                self.assertGreaterEqual(len(e["prompt"]), 600)
        for t in range(12):
            self.assertTrue(600 <= len(gen.filler_prompt(s, t, ["x"])) <= 1500)

    def test_events_sorted_and_epochs(self):
        ev = self.s["events"]
        self.assertEqual([e["epoch"] for e in ev], sorted(e["epoch"] for e in ev))
        self.assertEqual({e["epoch"] for e in ev}, {0, 1, 2, 4})


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
        raw = " ".join("%d) %s" % (i + 1, x["answer"]) for i, x in enumerate(items))
        r = grade.grade("D", items, raw)
        self.assertTrue(all(a["correct"] and not a["parse_ok"] for a in r))      # right, but parse_ok is false
        r = grade.grade("D", items, "only one: " + items[0]["answer"])
        self.assertFalse(any(a["correct"] for a in r))
        r = grade.grade("X", self.I["X"], "%s %s" % (self.I["X"][0]["new"], self.I["X"][0]["old"]))
        self.assertFalse(any(a["correct"] for a in r))                           # no fallback for X

    def test_id_as_text(self):
        x = self.I["D"][0]
        self.assertTrue(grade.grade("D", [x], js([{"id": "1", "answer": x["answer"]}]))[0]["correct"])


class Abstain(Base):
    """An explicit null in the main field is an abstention: parsed, not correct, flagged."""
    FIELD = {"R": "answer", "D": "answer", "U": "answer", "S": "answer", "X": "current",
             "E": "reason_code", "M": "order", "C": "earlier"}

    def test_null_for_every_type(self):
        for t, f in self.FIELD.items():
            items = self.I[t]
            r = grade.grade(t, items, js([{"id": x["id"], f: None} for x in items]))
            self.assertTrue(all(g["parse_ok"] and not g["correct"] and g["abstained"] for g in r), t)
        items = self.s["fresh"]["3"]
        r = grade.grade("F", items, js([{"id": x["id"], "answer": None} for x in items]))
        self.assertTrue(all(g["parse_ok"] and not g["correct"] and g["abstained"] for g in r))

    def test_null_with_other_fields(self):
        e = self.I["E"][0]
        for ent in ({"id": e["id"], "proceed": False, "reason_code": None}, {"id": e["id"], "proceed": None, "reason_code": None}):
            g = grade.grade("E", [e], js([ent]))[0]
            self.assertTrue(g["parse_ok"] and g["abstained"] and not g["correct"])
        c = self.I["C"][0]
        g = grade.grade("C", [c], js([{"id": c["id"], "conflict": True, "earlier": None}]))[0]
        self.assertTrue(g["parse_ok"] and g["abstained"] and not g["correct"])

    def test_missing_or_wrong_type_is_not_abstention(self):
        for t, f in self.FIELD.items():
            it = self.I[t][0]
            for ent in ({"id": it["id"]}, {"id": it["id"], f: 5}):
                g = grade.grade(t, [it], js([ent]))[0]
                self.assertFalse(g["parse_ok"] or g["abstained"] or g["correct"], (t, ent))
        g = grade.grade("R", self.I["R"][:1], "I do not know")[0]
        self.assertFalse(g["parse_ok"] or g["abstained"])

    def test_normal_answers_are_not_abstentions(self):
        items = self.I["R"]
        r = grade.grade("R", items, js([{"id": x["id"], "answer": x["answer"]} for x in items]))
        self.assertTrue(all(g["correct"] and not g["abstained"] for g in r))

    def test_mixed_reply(self):
        a, b = self.I["R"][:2]
        r = grade.grade("R", [a, b], js([{"id": a["id"], "answer": a["answer"]}, {"id": b["id"], "answer": None}]))
        self.assertEqual([(g["parse_ok"], g["correct"], g["abstained"]) for g in r], [(True, True, False), (True, False, True)])


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
                vals += it.get("order") or []
                for d in it.get("decoys") or []:
                    vals += gen.TOKEN_ANY.findall(d) or [d]
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

    def test_dead_end_probe_asks_for_exact_code(self):
        s = gen.build(29)
        self.assertIn("exact error code", gen.probe_prompt("E", s, "PROBE-29"))

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
        text = " ".join(e["prompt"] for e in s["events"])
        for t in ("R", "D", "C"):                          # user-stated types: the value is in the user text
            for it in s["items"][t]:
                self.assertIn(it["answer"] if "answer" in it else it["tok1"], text)
        for t in ("S", "E", "U"):                          # tool-output types: the value is never in the user text
            for it in s["items"][t]:
                self.assertNotIn(it["answer"] if "answer" in it else it["ecode"], text)
                for dcy in it.get("decoys") or []:
                    self.assertNotIn(dcy, text)
        files = {}
        for e in s["events"]:
            files.update(e["files"])
        for it in s["items"]["S"]:
            self.assertTrue(any(it["answer"] in c for c in files.values()))
        for it in s["items"]["E"]:
            self.assertTrue(any(it["ecode"] in c for c in files.values()))


class SLabel(Base):
    def grade_s(self, answer):
        it = self.I["S"][0]
        r = grade.grade("S", [it], js([{"id": it["id"], "answer": answer}]))[0]
        return it, r["correct"]

    def test_accept_label_forms(self):
        it = self.I["S"][0]
        v = it["answer"]
        for a in (v, "config: deploy command for x = " + v, "deploy: " + v, "label = " + v, "`" + v + "`", "a: b = " + v, "a = b: " + v):
            self.assertTrue(self.grade_s(a)[1], a)

    def test_reject_label_forms(self):
        it = self.I["S"][0]
        v = it["answer"]
        for a in ("config: x = " + v + "z", "config: x = other-value", "config: x = " + v + " and more", "see " + v + " in the file",
                  "x = " + v + ": extra", v + "z", "x=" + v, "x:" + v):
            self.assertFalse(self.grade_s(a)[1], a)

    def test_other_types_stay_exact(self):
        it = self.I["R"][0]
        for a in ("rule = " + it["answer"], "rule: " + it["answer"]):
            r = grade.grade("R", [it], js([{"id": it["id"], "answer": a}]))[0]
            self.assertFalse(r["correct"], a)


class Diagnostics(Base):
    def test_lookup_calls_cut_and_order(self):
        recs = [{"type": "user", "message": {"content": "hello PROBE-1-A-1-U"}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {"subagent_type": "recall", "prompt": "x" * 500}},
                                                               {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}]
        before = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"p": 1}}]}}]
        got = grade.lookup_calls(before + recs, "PROBE-1-A-1-U")
        self.assertEqual([c["tool"] for c in got], ["Agent", "Bash"])
        self.assertEqual(len(got[0]["input"]), 300)
        self.assertEqual(grade.lookup_calls(before, "PROBE-1-A-1-U"), [])

    def test_retention_counts_and_collapse(self):
        ans = grade.planted_answers(self.s)
        self.assertEqual({t for t, _, _ in ans}, set("RDSXCU"))
        vals = [v for _, _, v in ans]

        def summ(vs):
            return {"type": "user", "isCompactSummary": True, "message": {"content": [{"type": "text", "text": "notes " + " ".join(vs)}]}}
        recs = [{"type": "user", "message": {"content": "plain"}}, summ(vals[:10]), summ(vals[:3])]
        r = grade.retention(recs, self.s)
        self.assertEqual((r["found"], r["total"], r["last"], r["summaries"]), ([10, 3], len(vals), 3, 2))
        self.assertTrue(r["collapse"])
        r2 = grade.retention([summ(vals[:10]), summ(vals[:5])], self.s)
        self.assertFalse(r2["collapse"])
        self.assertIsNone(grade.retention([], self.s)["last"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
