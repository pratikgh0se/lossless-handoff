#!/usr/bin/env python3
"""Self-tests of retrieval_gate.py and of the split Table L5 (F76). No model call."""
import glob, json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import design4, litm, retrieval_gate as rg  # noqa: E402

REAL_FORKS = sorted(glob.glob(os.path.join(HERE, "results", "20261122T151831-preflight4", "failed", "*.jsonl")))


def item(**kw):
    base = {"id": "1", "key": "api", "current": "CUR1-0000-AAAA",
            "stmts": ["Decision: the `api` queue is `OLD1-0000-AAAA`.", "Proposal for the `api` queue: `BAD1-0000-AAAA`. Rejected.", "Change for the `api` queue: use `CUR1-0000-AAAA` instead."],
            "ops": [{"op": "set", "value": "OLD1-0000-AAAA"}, {"op": "reject", "value": "BAD1-0000-AAAA"}, {"op": "change", "value": "CUR1-0000-AAAA"}]}
    base.update(kw)
    return base


class Shape(unittest.TestCase):
    def test_records_have_the_real_field_layout(self):
        sc = design4.build_litm4(1)
        home = tempfile.mkdtemp()
        try:
            proj, pdir, main = rg.build_session(sc, home, 1)
            users = [r for r in main.records if r["type"] == "user"]
            asst = [r for r in main.records if r["type"] == "assistant"]
            self.assertEqual(set(users[0]), set(rg.USER_FIELDS))
            self.assertEqual(set(asst[0]), set(rg.ASSISTANT_FIELDS))
            self.assertEqual([r["type"] for r in main.records[:2]], ["mode", "session-owner"])
            self.assertEqual(len(users), len(sc["plan"]))
            self.assertTrue(pdir.startswith(os.path.join(home, ".claude", "projects")))
            self.assertEqual(os.path.basename(pdir), rg.slug(proj))
            if REAL_FORKS:      # compare with a real fork transcript when the run folder is on this machine
                real = [json.loads(l) for l in open(REAL_FORKS[0], encoding="utf-8") if l.strip()]
                ru = next(r for r in real if r["type"] == "user")
                ra = next(r for r in real if r["type"] == "assistant")
                self.assertEqual(set(ru), set(users[0]))
                self.assertEqual(set(ra), set(asst[0]))
                self.assertEqual(set(ra["message"]), set(asst[0]["message"]))
        finally:
            shutil.rmtree(home)

    def test_a_fork_file_copies_the_parent_history(self):
        sc = design4.build_litm4(1)
        home = tempfile.mkdtemp()
        try:
            proj, pdir, main = rg.build_session(sc, home, 1)
            fsid, fpath = rg.write_fork(pdir, main, "PROBE text", "t")
            fr = [json.loads(l) for l in open(fpath, encoding="utf-8")]
            mu = {r["uuid"]: r for r in main.records if r.get("uuid")}
            fu = {r["uuid"]: r for r in fr if r.get("uuid")}
            self.assertTrue(set(mu) < set(fu))                                      # the same uuids, plus the probe
            self.assertEqual(len(fu) - len(mu), 1)
            self.assertTrue(all(fu[u]["message"] == mu[u]["message"] and fu[u]["sessionId"] == fsid != mu[u]["sessionId"] for u in mu))
            self.assertEqual(fr[-1]["message"]["content"], "PROBE text")
            self.assertEqual(len(open(os.path.join(pdir, main.sid + ".jsonl")).readlines()), len(main.records))      # the parent file is unchanged
        finally:
            shutil.rmtree(home)


class Scoring(unittest.TestCase):
    def test_chain_coverage_needs_every_statement_in_time_order(self):
        it = item()
        full = "\n".join("- x %s" % s for s in it["stmts"])
        self.assertEqual(rg.score_chain(it, full), {"full": True, "any_order": True, "current": True})
        rev = "\n".join("- x %s" % s for s in reversed(it["stmts"]))
        self.assertEqual(rg.score_chain(it, rev), {"full": False, "any_order": True, "current": True})
        part = "- x " + it["stmts"][2]
        self.assertEqual(rg.score_chain(it, part), {"full": False, "any_order": False, "current": True})
        self.assertEqual(rg.score_chain(it, "no mentions"), {"full": False, "any_order": False, "current": False})

    def test_injection_lure_only_blocks_and_lure_share(self):
        it = item()
        lure = "- 1:2 Proposal for the `api` queue: `BAD1-0000-AAAA`. Rejected."
        cur = "- 1:3 Change for the `api` queue: use `CUR1-0000-AAAA` instead."
        s = rg.score_injection([it], "Lossless Handoff auto-recall: x\n" + lure + "\n" + lure)
        self.assertEqual((s["lure_only_blocks"], s["blocks_with_current"], s["hits"], s["lure_hits"]), (1, 0, 2, 2))
        s = rg.score_injection([it], "header\n" + lure + "\n" + cur)
        self.assertEqual((s["lure_only_blocks"], s["blocks_with_current"], s["lure_hits"]), (0, 1, 1))
        s = rg.score_injection([it], "")
        self.assertEqual((s["fired"], s["hits"]), (False, 0))

    def test_sibling_leaks_count_look_alike_keys_only(self):
        text = "- a `api-v2` queue is X\n- b `api` queue\n- c `other` queue\n- d `xapi` queue"
        self.assertEqual(rg.sibling_leaks("api", ["api", "api-v2", "other", "xapi"], text), 1)

    def rep(self, kh, cued_lure):
        return {"levels": {"L2": {"tools": {"key_history": kh}, "injection": {"cued": {"lure_only_blocks": cued_lure, "blocks_with_current": 5}}}}}

    def test_gate_line(self):
        line, ok = rg.gate(self.rep(None, 3), "L2")
        self.assertIsNone(ok)
        self.assertIn("NOT AVAILABLE", line)
        self.assertIn("3 of 8 injected blocks (need 0): FAIL", line)
        line, ok = rg.gate(self.rep({"n": 20, "full": 19}, 0), "L2")
        self.assertTrue(ok)
        self.assertIn("key_history full-chain coverage 95% (19/20), need at least 95%: PASS", line)
        self.assertTrue(line.endswith("GATE PASS"))
        line, ok = rg.gate(self.rep({"n": 20, "full": 18}, 0), "L2")
        self.assertFalse(ok)
        self.assertTrue(line.endswith("GATE FAIL"))
        line, ok = rg.gate(self.rep({"n": 20, "full": 20}, 1), "L2")
        self.assertFalse(ok)
        self.assertIn("R lure-only blocks on cued probes: 1 of 6", line)


class EndToEnd(unittest.TestCase):
    def test_runs_the_hooks_of_the_repo_in_a_temporary_home(self):
        real_home = os.path.expanduser("~")
        before = set(glob.glob(os.path.join(real_home, ".claude", "projects", "*lhgate*")))
        rep = rg.run_gate([3], ["L1"], rg.REPO_HOOKS)
        R = rep["levels"]["L1"]
        self.assertFalse(os.path.exists(rep["tmp"]))                                  # the temporary home is removed
        self.assertEqual(set(glob.glob(os.path.join(real_home, ".claude", "projects", "*lhgate*"))), before)
        self.assertEqual(R["tools"]["search5"]["n"], 9)
        self.assertEqual(R["tools"]["search10"]["n"], 9)
        self.assertTrue(R["tools"]["search10"]["current"] >= R["tools"]["search5"]["current"])
        self.assertEqual(R["tools"]["search5"]["full"], 0)                            # one window per message, newest first: no chain in time order
        self.assertEqual(R["injection"]["cued"]["probes"], 3)
        self.assertEqual(R["injection"]["cued"]["fired"], 3)
        self.assertEqual(R["injection"]["neutral"]["fired"], 0)                      # the cue words are the trigger
        text, ok = rg.render(rep, "L1")
        self.assertIn("RETRIEVAL GATE at L1", text)
        if R["tools"].get("key_history") is None:
            self.assertIsNone(ok)
            self.assertIn("not available", text)

    def test_a_hooks_folder_without_recall_is_an_error(self):
        d = tempfile.mkdtemp()
        try:
            self.assertEqual(rg.main(["--hooks-dir", d]), 2)
        finally:
            shutil.rmtree(d)


class SplitSearchRate(unittest.TestCase):
    def test_three_readings(self):
        inj = {"used_lookup": True, "search_kinds": ["injection"]}
        mcp = {"used_lookup": True, "search_kinds": ["mcp"]}
        bash = {"used_lookup": True, "search_kinds": ["injection", "bash"]}
        none = {"used_lookup": False, "search_kinds": []}
        old = {"used_lookup": True}
        self.assertEqual([litm.injected_val(r) for r in (inj, mcp, bash, none, old)], [True, False, True, False, False])
        self.assertEqual([litm.model_search_val(r) for r in (inj, mcp, bash, none, old)], [False, True, True, False, True])     # an old row: `either`
        self.assertEqual([litm.either_val(r) for r in (inj, mcp, bash, none, old)], [True, True, True, False, True])

    def test_h6_counts_model_search_not_injection(self):
        import litm4
        rows = []
        for arm, kinds in (("K", ["injection"]), ("B", [])):
            for i in range(10):
                rows.append({"seed": 1, "arm": arm, "type": "L2", "phrasing": "neutral", "condition": "nocompact", "used_lookup": bool(kinds), "search_kinds": kinds, "m1": True})
        V = litm4.Vec(rows)
        ck, tk = V.vecf(lambda r: r["arm"] == "K", litm.model_search_val)
        cb, tb = V.vecf(lambda r: r["arm"] == "B", litm.model_search_val)
        self.assertEqual((ck, tk, cb, tb), ([0], [10], [0], [10]))                     # injection alone is not a model search (the old reading gave 10/10)


if __name__ == "__main__":
    unittest.main(verbosity=1)
