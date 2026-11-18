#!/usr/bin/env python3
"""Seeded script generator for the continuity benchmark (see SPEC.md).

The same seed and the same GEN_VERSION always give the same script.
The only source of random is random.Random with a string seed.

Usage: gen.py --seed S [--out FILE]
"""
import argparse, json, random, re, sys

GEN_VERSION = "1"
K = 3                                   # instances of each probe type for each seed
TYPES = ["R", "D", "X", "S", "E", "U", "C"]
CHECKPOINTS = [1, 3, 10, 30]
B32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # base32 without 0, O, 1, I
TOKEN_RE = re.compile(r"\b[A-HJ-NP-Z2-9]{4}-\d{4}-[A-HJ-NP-Z2-9]{4}\b")
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
         "quebec romeo sierra tango uniform victor whiskey xray yankee zulu ember quartz willow cobalt "
         "marble thistle harbor lantern meadow orchid pewter saddle timber velvet walnut zephyr").split()
NOUNS = ("export import audit billing cache sync report ingest backup search notify archive render "
         "upload digest mirror ledger").split()
VERBS = ("fx-sync lh-push zq-deploy rk-ship nv-roll").split()
APPROACHES = ("shard-merge delta-replay lazy-flush ring-rebuild tiered-spill blind-retry fan-in-batch "
              "snapshot-swap").split()
CONS, VOW = "bcdfghjklmnprstvz", "aeiou"


class Gen:
    def __init__(self, seed, version=GEN_VERSION):
        self.r = random.Random("lhbench-%s-%d" % (version, seed))
        self.used = set()

    def _uniq(self, make):
        for _ in range(1000):
            v = make()
            if v not in self.used:
                self.used.add(v)
                return v
        raise RuntimeError("cannot make a unique value")

    def chars(self, n):
        return "".join(self.r.choice(B32) for _ in range(n))

    def tok(self):          # 4 chars + 4 digits + 4 chars: about 53 bits
        return self._uniq(lambda: "%s-%04d-%s" % (self.chars(4), self.r.randrange(10000), self.chars(4)))

    def path(self):         # about 43 bits
        return self._uniq(lambda: "/srv/%s/lane-%04d/out" % (self.chars(6).lower(), self.r.randrange(10000)))

    def cmd(self):          # about 43 bits
        return self._uniq(lambda: "%s --lane %04d --tag %s" % (self.r.choice(VERBS), self.r.randrange(10000), self.chars(6)))

    def envline(self):      # about 43 bits
        return self._uniq(lambda: "LH_SYNC_KEY=%s%04d" % (self.chars(6), self.r.randrange(10000)))

    def ecode(self):        # about 43 bits
        return self._uniq(lambda: "ERR-%04d-%s" % (self.r.randrange(10000), self.chars(6)))

    def topics(self, n, taken):
        """Topic labels like export-3391. They are not answers. They stay unique across the script."""
        out = []
        while len(out) < n:
            t = "%s-%04d" % (self.r.choice(NOUNS), self.r.randrange(10000))
            if t not in taken:
                taken.add(t)
                out.append(t)
        return out

    def codename(self):
        c = lambda: self.r.choice(CONS) + self.r.choice(VOW)
        return self._uniq(lambda: c() + c() + c() + "-%04d" % self.r.randrange(10000))


def build(seed, version=GEN_VERSION):
    g = Gen(seed, version)
    taken = set()
    items = {t: [] for t in TYPES}
    # R: standing rule
    for i, tp in enumerate(g.topics(K, taken)):
        tok = g.tok()
        items["R"].append({"id": i + 1, "topic": tp, "answer": tok,
                           "plant": "Standing rule from now on: in the `%s` repository, every commit message must end with the tag `TAG-%s`." % (tp, tok),
                           "question": "What tag must every commit message in the `%s` repository end with?" % tp})
    # D: decision
    for i, tp in enumerate(g.topics(K, taken)):
        tok = g.tok()
        items["D"].append({"id": i + 1, "topic": tp, "answer": tok,
                           "plant": "Decision: the `%s` queue is `%s`." % (tp, tok),
                           "question": "Which queue did we choose for `%s`?" % tp})
    # X: decision, later replaced
    for i, tp in enumerate(g.topics(K, taken)):
        old, new = g.tok(), g.tok()
        items["X"].append({"id": i + 1, "topic": tp, "old": old, "new": new,
                           "plant": "Decision: the `%s` queue is `%s`." % (tp, old),
                           "replace": "Change for the `%s` queue: use `%s` instead." % (tp, new),
                           "question": "What is the current queue for `%s`? Also list every earlier queue that we replaced." % tp})
    # S: exact string
    kinds = [("deploy command", g.cmd, "The deploy command for `%s` is `%s`."),
             ("output path", g.path, "The output path for `%s` is `%s`."),
             ("export env line", g.envline, "The export env line for `%s` is `%s`.")]
    for i, tp in enumerate(g.topics(K, taken)):
        kind, make, fmt = kinds[i % len(kinds)]
        val = make()
        items["S"].append({"id": i + 1, "topic": tp, "answer": val, "kind": kind, "plant": fmt % (tp, val),
                           "question": "Give the exact %s for `%s`, character for character." % (kind, tp)})
    # E: dead end
    for i, tp in enumerate(g.topics(K, taken)):
        name, ecode = APPROACHES[(g.r.randrange(len(APPROACHES)) + i) % len(APPROACHES)] + "-" + g.chars(3).lower(), g.ecode()
        items["E"].append({"id": i + 1, "topic": tp, "name": name, "ecode": ecode,
                           "plant": "Dead end: we tried the `%s` approach for `%s`. It failed with error code `%s`. Do not try it again." % (name, tp, ecode),
                           "question": "I plan to use the `%s` approach for `%s`. Should we proceed?" % (name, tp)})
    # U: fact that only a subagent reads
    for i in range(K):
        tok, fname = g.tok(), "led-%d.txt" % (i + 1)
        lines = ["%s %s %s" % (g.r.choice(WORDS), g.r.choice(WORDS), g.r.choice(WORDS)) for _ in range(11)]
        lines.insert(g.r.randrange(len(lines) + 1), "ledger-key: %s" % tok)
        items["U"].append({"id": i + 1, "file": fname, "answer": tok, "content": "\n".join(lines) + "\n",
                           "question": "What is the ledger key in the file `%s`?" % fname})
    # C: user contradiction
    for i, tp in enumerate(g.topics(K, taken)):
        tok1, tok2 = g.tok(), g.tok()
        items["C"].append({"id": i + 1, "topic": tp, "tok1": tok1, "tok2": tok2,
                           "plant": "Decision: the `%s` queue is `%s`." % (tp, tok1),
                           "statement": "Let's use `%s` for the `%s` queue." % (tok2, tp)})
    # F: fresh control, one set for each checkpoint (planted right before the probe)
    fresh = {}
    for n in CHECKPOINTS + [0]:
        fi = []
        for i, tp in enumerate(g.topics(K, taken)):
            tok = g.tok()
            fi.append({"id": i + 1, "topic": tp, "answer": tok,
                       "plant": "Scratch label `%s` is `%s`." % (tp, tok),
                       "question": "What is the value of scratch label `%s`?" % tp})
        fresh[str(n)] = fi
    # noise: near duplicates of D and S topics (same noun, other number, other value)
    noise_plant = []
    for it in items["D"] + items["S"]:
        tp = "%s-%04d" % (it["topic"].split("-")[0], g.r.randrange(10000))
        while tp in taken:
            tp = "%s-%04d" % (it["topic"].split("-")[0], g.r.randrange(10000))
        taken.add(tp)
        noise_plant.append("Decision: the `%s` queue is `%s`." % (tp, g.tok()))
    filler_noise = ["Decision: the `%s` queue is `%s`." % (g.topics(1, taken)[0], g.tok()) for _ in range(40)]
    # plant turns (epoch 0)
    def remember(lines):
        return "Remember this for later work in this project. " + " ".join(lines) + " Reply only `ok`."
    plant_turns = [
        {"kind": "R", "prompt": remember([x["plant"] for x in items["R"]])},
        {"kind": "D", "prompt": remember([x["plant"] for x in items["D"]])},
        {"kind": "S", "prompt": remember([x["plant"] for x in items["S"]])},
        {"kind": "X1", "prompt": remember([x["plant"] for x in items["X"]])},
        {"kind": "E", "prompt": remember([x["plant"] for x in items["E"]])},
        {"kind": "noise", "prompt": remember(noise_plant)},
        {"kind": "U", "files": {x["file"]: x["content"] for x in items["U"]}, "delete_after": [x["file"] for x in items["U"]],
         "prompt": ("Do these %d jobs one after the other. For each job, use the Task tool to start one subagent. "
                    "Tell the subagent to read one file in the current folder and reply with only the number of lines in that file. "
                    "The files are %s. After each subagent replies, note its answer. At the end, reply only `ok`." %
                    (K, ", ".join("`%s`" % x["file"] for x in items["U"])))},
        {"kind": "X2", "prompt": remember([x["replace"] for x in items["X"]])},
        {"kind": "C", "prompt": remember([x["plant"] for x in items["C"]])},
    ]
    script = {"gen_version": version, "seed": seed, "k": K, "items": items, "fresh": fresh,
              "plant_turns": plant_turns, "filler_noise": filler_noise, "noise_every": 6}
    script["secrets"] = sorted(secrets(script))
    return script


def secrets(script):
    """Every answer value. A filler file must never hold one of them."""
    s = set()
    for t, lst in script["items"].items():
        for it in lst:
            for k in ("answer", "old", "new", "ecode", "tok1", "tok2"):
                if k in it:
                    s.add(it[k])
    for lst in script["fresh"].values():
        for it in lst:
            s.add(it["answer"])
    return s


def filler_file(seed, t, j, lines=330, version=GEN_VERSION):
    """File j of filler turn t. About 20 KB, about 6k tokens. Random words and noise tokens."""
    r = random.Random("lhbench-filler-%s-%d-%d-%d" % (version, seed, t, j))
    name = "pad-%03d-%d.txt" % (t, j)
    out = []
    for ln in range(1, lines + 1):
        row = "%d.%04d " % (j + 1, ln) + " ".join(r.choice(WORDS) for _ in range(8))
        if r.random() < 0.03:       # noise item: same form as an answer, never an answer
            row += " ref-%s-%04d-%s" % ("".join(r.choice(B32) for _ in range(4)), r.randrange(10000), "".join(r.choice(B32) for _ in range(4)))
        out.append(row)
    return name, "\n".join(out) + "\n"


def filler_prompt(script, t, names):
    """Prompt of filler turn t (0-based). Every noise_every turns it adds one noise decision."""
    pre = ""
    ne = script["noise_every"]
    if t > 0 and t % ne == 0:
        pre = "Note for later: " + script["filler_noise"][(t // ne - 1) % len(script["filler_noise"])] + " "
    files = " and ".join("`%s`" % n for n in names)
    return (pre + "Read %s completely with the Read tool. Then reply only with the number of lines of each file, like `name=count`." % files)


def probe_prompt(ptype, script, marker, ckpt=None):
    """Neutral probe text. It holds the questions and a JSON form. It never holds an answer."""
    items = script["fresh"][str(ckpt)] if ptype == "F" else script["items"][ptype]
    head = "[%s] Answer from this session's history only. Do not guess.\n" % marker
    if ptype == "C":
        body = "\n".join("%d. %s" % (x["id"], x["statement"]) for x in items)
        head += ("For each statement below, say if it conflicts with an earlier decision in this session. "
                 "If it conflicts, give the earlier value.\n")
        form = '{"answers":[{"id":1,"conflict":true,"earlier":"..."},{"id":2,"conflict":false,"earlier":""}]}'
    else:
        body = "\n".join("%d. %s" % (x["id"], x["question"]) for x in items)
        form = {"X": '{"answers":[{"id":1,"current":"...","replaced":["..."]}]}',
                "E": '{"answers":[{"id":1,"proceed":true,"reason_code":"..."}]}'}.get(ptype, '{"answers":[{"id":1,"answer":"..."}]}')
    return head + body + "\nReply with JSON only, in this form: " + form


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--version", default=GEN_VERSION)
    ap.add_argument("--out")
    a = ap.parse_args()
    s = build(a.seed, a.version)
    txt = json.dumps(s, indent=1, sort_keys=True)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt + "\n")
    else:
        sys.stdout.write(txt + "\n")


if __name__ == "__main__":
    main()
