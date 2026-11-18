#!/usr/bin/env python3
"""Seeded script generator for the continuity benchmark (see SPEC.md).

The same seed and the same GEN_VERSION always give the same script.
The only source of random is random.Random with a string seed.

Usage: gen.py --seed S [--out FILE]
"""
import argparse, json, random, re, sys

GEN_VERSION = "4"
K = 3                                   # instances of each probe type for each seed
TYPES = ["R", "D", "X", "S", "E", "U", "C", "M"]
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


ITEM_EPOCHS = [0, 1, 2, 4]              # epoch (compactions so far) in which item j of a type is planted
KI = len(ITEM_EPOCHS)                    # items of each scored type for each seed (v4: 4)
DECOYS = 2                               # look-alike facts for sibling components, planted with each item
SENT = ["Please keep the %(w)s changes small and review the %(w2)s notes before you touch anything else.",
        "Last week the %(w)s team asked for a short status note, so keep your replies brief and exact.",
        "We are tidying the %(w)s and %(w2)s parts of the project, one step at a time and with no rush.",
        "Do not change files that I did not name. If a step is unclear, ask me before you guess.",
        "The %(w)s review is on Friday, and the %(w2)s checklist must be ready for it by then.",
        "I will send more details when the %(w)s work starts, so treat this as background for now.",
        "Our habit is to write small commits, run the local checks, and keep the %(w)s folder clean.",
        "A new colleague will read this history later, so please stay precise about names and values.",
        "The %(w)s migration is slow, and the %(w2)s step depends on it, so we go in order.",
        "Keep a calm pace. Quality of the %(w)s output matters more than speed this month."]


def prose(r, lo=600, hi=1200):
    """Realistic task text of lo to hi characters. It holds no digits, so it never looks like a fact."""
    out = ""
    while len(out) < lo:
        s = r.choice(SENT) % {"w": r.choice(WORDS), "w2": r.choice(WORDS)}
        if out and len(out) + len(s) + 1 > hi:
            break
        out += (" " if out else "") + s
    return out


def logfile(g, facts, n=40):
    """About n lines of normal log text with the fact lines hidden among them."""
    lines = ["[%02d:%02d:%02d] %s %s step %d ok" % (g.r.randrange(24), g.r.randrange(60), g.r.randrange(60),
                                                   g.r.choice(WORDS), g.r.choice(WORDS), g.r.randrange(100)) for _ in range(n)]
    for f in facts:
        lines.insert(g.r.randrange(len(lines) + 1), f)
    return "\n".join(lines) + "\n"


def sibs(g, tp, taken, n):
    """n look-alike topics: same noun, other number."""
    out = []
    while len(out) < n:
        t = "%s-%04d" % (tp.split("-")[0], g.r.randrange(10000))
        if t not in taken:
            taken.add(t)
            out.append(t)
    return out


def build(seed, version=GEN_VERSION):
    g = Gen(seed, version)
    pr = random.Random("lhbench-prose-%s-%d" % (version, seed))
    taken = set()
    items = {t: [] for t in TYPES}
    events = []                                  # plant events: {"epoch", "seq", "kind", "prompt", "files", "delete_after"}
    stmts = {e: [] for e in ITEM_EPOCHS}         # user-stated statements (targets and decoys) for each epoch
    repl = {e: [] for e in ITEM_EPOCHS}

    def ev(epoch, kind, prompt, files=None, delete_after=None):
        events.append({"epoch": epoch, "seq": len(events), "kind": kind, "prompt": prompt,
                       "files": files or {}, "delete_after": delete_after or []})

    for j, e in enumerate(ITEM_EPOCHS):
        # R: standing rule (user-stated), with decoy rules for sibling repositories
        tp = g.topics(1, taken)[0]
        tok = g.tok()
        stm = ["Standing rule from now on: in the `%s` repository, every commit message must end with the tag `TAG-%s`." % (tp, tok)]
        for s in sibs(g, tp, taken, DECOYS):
            stm.append("Standing rule from now on: in the `%s` repository, every commit message must end with the tag `TAG-%s`." % (s, g.tok()))
        items["R"].append({"id": j + 1, "epoch": e, "topic": tp, "answer": tok, "decoys": stm[1:],
                           "question": "What tag must every commit message in the `%s` repository end with?" % tp})
        stmts[e] += stm
        # D: decision (user-stated), with decoy decisions for sibling queues
        tp = g.topics(1, taken)[0]
        tok = g.tok()
        stm = ["Decision: the `%s` queue is `%s`." % (tp, tok)] + ["Decision: the `%s` queue is `%s`." % (s, g.tok()) for s in sibs(g, tp, taken, DECOYS)]
        items["D"].append({"id": j + 1, "epoch": e, "topic": tp, "answer": tok, "decoys": stm[1:],
                           "question": "Which queue did we choose for `%s`?" % tp})
        stmts[e] += stm
        # X: decision, later replaced (user-stated); decoys are sibling decisions
        tp = g.topics(1, taken)[0]
        old, new = g.tok(), g.tok()
        stm = ["Decision: the `%s` queue is `%s`." % (tp, old)] + ["Decision: the `%s` queue is `%s`." % (s, g.tok()) for s in sibs(g, tp, taken, DECOYS)]
        items["X"].append({"id": j + 1, "epoch": e, "topic": tp, "old": old, "new": new, "decoys": stm[1:],
                           "question": "What is the current queue for `%s`? Also list every earlier queue that we replaced." % tp})
        stmts[e] += stm
        repl[e].append("Change for the `%s` queue: use `%s` instead." % (tp, new))
        # C: decision that the probe contradicts (user-stated), with decoys
        tp = g.topics(1, taken)[0]
        tok1, tok2 = g.tok(), g.tok()
        stm = ["Decision: the `%s` queue is `%s`." % (tp, tok1)] + ["Decision: the `%s` queue is `%s`." % (s, g.tok()) for s in sibs(g, tp, taken, DECOYS)]
        items["C"].append({"id": j + 1, "epoch": e, "topic": tp, "tok1": tok1, "tok2": tok2, "decoys": stm[1:],
                           "statement": "Let's use `%s` for the `%s` queue." % (tok2, tp)})
        stmts[e] += stm
    for e in ITEM_EPOCHS:
        lines = stmts[e][:]
        g.r.shuffle(lines)
        ev(e, "stmt", "%s Remember these for later work in this project: %s Reply only `ok`." % (prose(pr), " ".join(lines)))
        # S: exact string. The value is only in a file that the agent must read. The user never states it.
        kinds = [("deploy command", g.cmd), ("output path", g.path), ("export env line", g.envline)]
        j = ITEM_EPOCHS.index(e)
        kind, make = kinds[j % len(kinds)]
        tp = g.topics(1, taken)[0]
        val = make()
        facts = ["config: %s for %s = %s" % (kind, tp, val)] + ["config: %s for %s = %s" % (kind, s, make()) for s in sibs(g, tp, taken, DECOYS)]
        fname = "cfg/%s.txt" % tp
        items["S"].append({"id": j + 1, "epoch": e, "topic": tp, "answer": val, "kind": kind, "decoys": [x.split(" = ")[1] for x in facts[1:]],
                           "question": "Give the exact %s for `%s`, character for character." % (kind, tp)})
        ev(e, "S", "%s Open the file `%s` with the Read tool. Then tell me only whether it has a %s entry for `%s`. Reply yes or no." % (prose(pr, 300, 500), fname, kind, tp),
           files={fname: logfile(g, facts)}, delete_after=[fname])
        # E: dead end. The error code is only in a log that the agent must read. The user never states it.
        tp = g.topics(1, taken)[0]
        name = g.r.choice(APPROACHES) + "-" + g.chars(3).lower()
        ecode = g.ecode()
        facts = ["FAILED approach `%s` for `%s` with code %s" % (name, tp, ecode)]
        for s in sibs(g, tp, taken, DECOYS):
            facts.append("FAILED approach `%s` for `%s` with code %s" % (g.r.choice(APPROACHES) + "-" + g.chars(3).lower(), s, g.ecode()))
        fname = "logs/try-%s.txt" % name
        items["E"].append({"id": j + 1, "epoch": e, "topic": tp, "name": name, "ecode": ecode, "decoys": [x.rsplit(" ", 1)[1] for x in facts[1:]],
                           "question": "I plan to use the `%s` approach for `%s`. Should we proceed? In reason_code, copy the exact error code text that this approach failed with, character for character." % (name, tp)})
        ev(e, "E", "%s Read the file `%s` with the Read tool and tell me if the `%s` approach for `%s` passed. If it failed, we drop that approach for good. Reply passed or failed." % (prose(pr, 300, 500), fname, name, tp),
           files={fname: logfile(g, facts)}, delete_after=[fname])
        # X replace: three turns after the old value
        if repl[e]:
            ev(e, "X2", "%s Remember this too: %s Reply only `ok`." % (prose(pr, 300, 500), " ".join(repl[e])))
    # U: fact that only a subagent reads (reported apart, epoch 0)
    items_u = []
    for i in range(K):
        tok, fname = g.tok(), "led-%d.txt" % (i + 1)
        lines = ["%s %s %s" % (g.r.choice(WORDS), g.r.choice(WORDS), g.r.choice(WORDS)) for _ in range(11)]
        lines.insert(g.r.randrange(len(lines) + 1), "ledger-key: %s" % tok)
        items["U"].append({"id": i + 1, "epoch": 0, "file": fname, "answer": tok, "content": "\n".join(lines) + "\n",
                           "question": "What is the ledger key in the file `%s`?" % fname})
    ev(0, "U", "Do these %d jobs one after the other. For each job, use the Task tool to start one subagent. "
       "Tell the subagent to read one file in the current folder and reply with only the number of lines in that file. "
       "The files are %s. After each subagent replies, note its answer. At the end, reply only `ok`." % (K, ", ".join("`led-%d.txt`" % (i + 1) for i in range(K))),
       files={x["file"]: x["content"] for x in items["U"]}, delete_after=[x["file"] for x in items["U"]])
    # M: order of three decisions in epochs 0, 1 and 2. Random ids, so an arm with no history cannot guess them.
    mids = [g.tok() for _ in range(3)]
    mt = g.topics(3, taken)
    plants = ["Ordered decision %s: the `%s` queue is `%s`." % (mids[i], mt[i], g.tok()) for i in range(3)]
    items["M"] = [{"id": 1, "epoch": 2, "order": mids, "topics": mt, "plants": plants,
                   "question": "Earlier in this session I gave three decisions that I labelled `Ordered decision <id>`. List their three ids, earliest first."}]
    for i in range(3):
        ev(i, "M", "%s Remember this for later work in this project. %s Reply only `ok`." % (prose(pr, 300, 500), plants[i]))
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
    filler_noise = ["Decision: the `%s` queue is `%s`." % (g.topics(1, taken)[0], g.tok()) for _ in range(40)]
    events.sort(key=lambda x: (x["epoch"], x["seq"]))
    script = {"gen_version": version, "seed": seed, "k": KI, "items": items, "fresh": fresh, "events": events,
              "filler_noise": filler_noise, "noise_every": 6, "max_epoch": max(ITEM_EPOCHS)}
    script["secrets"] = sorted(secrets(script))
    return script


def select(script, ptype, epoch_max):
    """Items of a type planted in epochs <= epoch_max, oldest first. epoch_max None means all."""
    its = script["items"][ptype]
    its = [x for x in its if epoch_max is None or x.get("epoch", 0) <= epoch_max]
    return sorted(its, key=lambda x: (x.get("epoch", 0), x["id"]))


def secrets(script):
    """Every answer value and every decoy value. A filler file must never hold one of them."""
    s = set()
    for t, lst in script["items"].items():
        for it in lst:
            for k in ("answer", "old", "new", "ecode", "tok1", "tok2"):
                if k in it:
                    s.add(it[k])
            s.update(it.get("order") or [])
            for d in it.get("decoys") or []:
                s.update(TOKEN_ANY.findall(d) or [d])
    for lst in script["fresh"].values():
        for it in lst:
            s.add(it["answer"])
    return s


TOKEN_ANY = re.compile(r"[A-HJ-NP-Z2-9]{4}-\d{4}-[A-HJ-NP-Z2-9]{4}")


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
    r = random.Random("lhbench-fprose-%s-%d-%d" % (script["gen_version"], script["seed"], t))
    pre = prose(r) + " " + pre      # long, realistic user text (600 to 1200 characters), so the user-message list of a summary is long too
    return (pre + "Read %s completely with the Read tool. Then reply only with the number of lines of each file, like `name=count`." % files)


def probe_prompt(ptype, script, marker, ckpt=None, items=None):
    """Neutral probe text. It holds the questions and a JSON form. It never holds an answer.
    items: the chosen items (oldest first). Default: all items of the type."""
    if items is None:
        items = script["fresh"][str(ckpt)] if ptype == "F" else script["items"][ptype]
    head = "[%s] Answer from this session's history only. Do not guess.\n" % marker
    if ptype == "C":
        body = "\n".join("%d. %s" % (x["id"], x["statement"]) for x in items)
        head += ("For each statement below, say if it conflicts with an earlier decision in this session. "
                 "If it conflicts, give the earlier value.\n")
        form = '{"answers":[{"id":1,"conflict":true,"earlier":"..."},{"id":2,"conflict":false,"earlier":""}]}'
    elif ptype == "M":
        body = "\n".join("%d. %s" % (x["id"], x["question"]) for x in items)
        form = '{"answers":[{"id":1,"order":["...","...","..."]}]}'
    else:
        body = "\n".join("%d. %s" % (x["id"], x["question"]) for x in items)
        form = {"X": '{"answers":[{"id":1,"current":"...","replaced":["..."]}]}',
                "E": '{"answers":[{"id":1,"proceed":true,"reason_code":"<exact error code text>"}]}'}.get(ptype, '{"answers":[{"id":1,"answer":"..."}]}')
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
