#!/usr/bin/env python3
"""Seeded script generator for the continuity benchmark (see SPEC.md).

The same seed and the same GEN_VERSION always give the same script.
The only source of random is random.Random with a string seed.

Usage: gen.py --seed S [--out FILE]
"""
import argparse, json, random, re, sys

GEN_VERSION = "5"
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


def stream_version(version):
    """Version text in the RNG seeds. Gen v4 and newer share the v4 streams, so v5 plants the same items,
    answers and probes as v4 for a seed. Only filler_lines differs. Older versions keep their own streams."""
    try:
        return "4" if int(version) >= 4 else str(version)
    except ValueError:
        return str(version)


class Gen:
    def __init__(self, seed, version=GEN_VERSION):
        self.r = random.Random("lhbench-%s-%d" % (stream_version(version), seed))
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
    pr = random.Random("lhbench-prose-%s-%d" % (stream_version(version), seed))
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


FILLER_LINES_V4 = 330           # gen v4 and older: every filler file has this many lines
FILLER_LINES_RANGE = (280, 380)  # gen v5: lines of a file, from the seed and the file name. Mean 330, as in v4.


def filler_lines(seed, name, version=GEN_VERSION):
    """Line count of a filler file. v4: fixed. v5: random in FILLER_LINES_RANGE, from the seed and the name only.
    The count is in no file name and in no header, so a model has to read the file to know it."""
    try:
        old = int(version) < 5
    except ValueError:
        old = False
    if old:
        return FILLER_LINES_V4
    return random.Random("%s:%s" % (seed, name)).randint(*FILLER_LINES_RANGE)


def filler_file(seed, t, j, lines=None, version=GEN_VERSION):
    """File j of filler turn t. About 20 KB, about 6k tokens on average. Random words and noise tokens."""
    r = random.Random("lhbench-filler-%s-%d-%d-%d" % (stream_version(version), seed, t, j))
    name = "pad-%03d-%d.txt" % (t, j)
    if lines is None:
        lines = filler_lines(seed, name, version)
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
    r = random.Random("lhbench-fprose-%s-%d-%d" % (stream_version(script["gen_version"]), script["seed"], t))
    pre = prose(r) + " " + pre      # long, realistic user text (600 to 1200 characters), so the user-message list of a summary is long too
    return (pre + "Read %s completely with the Read tool. Then reply only with the number of lines of each file, like `name=count`." % files)


def probe_prompt(ptype, script, marker, ckpt=None, items=None, phrasing="cued", cue=False):
    """Neutral probe text. It holds the questions and a JSON form. It never holds an answer.
    items: the chosen items (oldest first). Default: all items of the type."""
    if phrasing == "neutral":
        return neutral_probe_prompt(ptype, script, marker, items)
    if phrasing == "nonliteral":
        return nonliteral_probe_prompt(ptype, script, marker, items)
    if items is None:
        items = script["fresh"][str(ckpt)] if ptype == "F" else script["items"][ptype]
    head = "[%s] Answer from this session's history only. Do not guess.%s\n" % (marker, CUED_HEAD_TAIL if cue else "")
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


# Type E has two fields. In the first preflight run (2026-11-22) the oracle arm answered 6 of 6 E items with the right code and "proceed": null,
# because "the main field" was not clear. The note names the main field. It is the same for every arm.
E_NOTE = " For error codes the main field is reason_code. Always set proceed to true or false."


# ---- position and phrasing (probe design 2, added with the lost-in-the-middle design; see README.md)
# These helpers read a script. They never change a script, so every old seed gives the same script and the same old probe text.
FILL_PER_EPOCH = 6                       # nominal filler turns after the plant turns of each epoch
POSITIONS = ("start", "middle", "end")
PHRASINGS = ("cued", "neutral", "nonliteral")   # nonliteral: probe design 3 only, no-compaction condition
NEUTRAL_TYPES = ("R", "D", "X", "S", "E")  # C and M ask about the past by their nature, so they have a cued probe only
PLANT_KIND = {"R": "stmt", "D": "stmt", "X": "stmt", "C": "stmt", "S": "S", "E": "E", "U": "U"}
# Cue phrases of the auto-recall hook (contract F67.b). A neutral probe must hold none of them.
CUE_PHRASES = ["what did i say", "what did we decide", "we decided", "i told you", "i said", "you said", "earlier",
               "before", "last time", "previously", "remember", "as we discussed", "did we", "did i", "which one did",
               "what was the"]
CUED_HEAD_TAIL = " I told you these values earlier, so remember them."   # design 2 only: it makes a cued probe fire the hook
QUOTED_PHRASE_RE = re.compile(r'"[^"\n]*"')


def nominal_timeline(script, fill=FILL_PER_EPOCH):
    """The nominal session timeline: for each epoch 0..max_epoch, its plant turns, then `fill` filler turns.
    Returns ({event seq: turn index}, total turns). The no-compaction condition runs exactly this timeline.
    A compaction run follows the same order of plant turns. Its filler length differs, so its tags stay nominal."""
    idx, t = {}, 0
    for e in range(script["max_epoch"] + 1):
        for ev in script["events"]:
            if ev["epoch"] == e:
                idx[ev["seq"]] = t
                t += 1
        t += fill
    return idx, t


def position_of_turn(i, total):
    """start, middle or end: the third of the timeline that turn index i is in."""
    return POSITIONS[min(2, (3 * i) // total)]


def position_of(script, ptype, item, fill=FILL_PER_EPOCH):
    """Position tag of a planted fact (by the turn index of its plant turn). None for M and F (no single plant turn)."""
    kind = PLANT_KIND.get(ptype)
    if kind is None or "epoch" not in item:
        return None
    idx, total = nominal_timeline(script, fill)
    for ev in script["events"]:
        if ev["epoch"] == item["epoch"] and ev["kind"] == kind:
            return position_of_turn(idx[ev["seq"]], total)
    return None


def neutral_question(ptype, item):
    """A task that needs the fact. No reference to the past, no cue phrase."""
    if ptype not in NEUTRAL_TYPES:
        raise ValueError("no neutral phrasing for probe type " + str(ptype))
    t = item["topic"]
    if ptype == "R":
        return "Write the tag that ends every commit message in the `%s` repository." % t
    if ptype == "D":
        return "Write the queue name to use for `%s`." % t
    if ptype == "X":
        return "Write the queue to use for `%s` now. Also list every other queue value for `%s` that is no longer valid." % (t, t)
    if ptype == "S":
        return "Write the exact %s for `%s`, as it must appear in the config." % (item["kind"], t)
    if ptype == "E":
        return ("I plan to use the `%s` approach for `%s`. Give the go or no-go in proceed. In reason_code, copy the exact "
                "error code text that applies to this approach, character for character." % (item["name"], t))
    raise ValueError("no neutral phrasing for probe type " + str(ptype))


def neutral_probe_prompt(ptype, script, marker, items=None):
    """Neutral probe text: the same JSON form and the same grader as the cued probe, a different question."""
    if items is None:
        items = script["items"][ptype]
    head = "[%s] Do not guess. If you do not know a value, set the main field to null.%s\n" % (marker, E_NOTE if ptype == "E" else "")
    body = "\n".join("%d. %s" % (x["id"], neutral_question(ptype, x)) for x in items)
    form = {"X": '{"answers":[{"id":1,"current":"...","replaced":["..."]}]}',
            "E": '{"answers":[{"id":1,"proceed":true,"reason_code":"..."}]}'}.get(ptype, '{"answers":[{"id":1,"answer":"..."}]}')
    return head + body + "\nReply with JSON only, in this form: " + form


def has_cue(text):
    """True if the text would fire the auto-recall hook: a cue phrase, or a double-quoted phrase of 3 or more words."""
    low = text.lower()
    if any(c in low for c in CUE_PHRASES):
        return True
    return any(len(q[1:-1].split()) >= 3 for q in QUOTED_PHRASE_RE.findall(text))


# ---- probe design 3 (2026-11-22): real long context, balanced positions, non-literal probes
# build_litm3 makes a separate script for the no-compaction condition. It has its own RNG stream, so nothing old changes.
# Everything is user-stated (no tool output, because Claude Code clears old tool results). Filler is pasted documents in user turns.
L3_TYPES = ("R", "D", "X", "S", "E")
L3_PER_POS = 2                 # facts of each type in each third: 2 start, 2 middle, 2 end
L3_FILL_PER_THIRD = 12         # filler turns in each third
L3_FACTS_PER_THIRD = len(L3_TYPES) * L3_PER_POS
ADJ = ("amber brisk calm dusty eager frosty gentle hollow ivory jolly keen lunar mellow nimble opal plain quiet rustic "
       "silver tidy upbeat vivid woolly young zesty azure bold crisp dim fuzzy glossy humble inky kindly leafy misty noble").split()
NOUN = ("harbor meadow lantern orchard pewter saddle timber velvet walnut zephyr canyon glacier island jungle kettle ledge "
        "marsh nursery oasis pasture quarry ridge summit tundra valley wharf anchor beacon cabin dune fjord grove hamlet isle "
        "knoll lagoon mesa").split()
SENT3 = [s_.replace(" before you touch anything else", " first").replace(" before you guess", " first") for s_ in SENT]    # no cue phrase: the hook must not fire on plant or filler turns
assert not any(c in " ".join(SENT3).lower() for c in CUE_PHRASES), "SENT3 holds a cue phrase"
STOPWORDS = set(("a an the for of to in on at is are was were be been it its this that these those and or but each with from by as "
                 "i you we me my your our us he she they them their there here if so than then do does did not no can will would "
                 "should could may might have has had also too only just into over under up down out off about").split())


def content_words(text, drop=()):
    """Lowercase words (letters and digits) of a text without stopwords, and without the words of the values in `drop`."""
    gone = set()
    for v in drop:
        gone.update(re.findall(r"[a-z0-9]+", str(v).lower()))
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and w not in gone}


# Word list of design 3 (told in each bridge turn). First preflight run: the old questions said "lane" for a queue and "the exact string stored"
# for a settings value, and arms A and O answered null or gave the topic name. The bridge now explains each word, so the question is not ambiguous.
L3_S_WORD = {"deploy command": "ship step", "output path": "landing folder", "export env line": "shell switch"}
L3_WORD_LIST = ('Word list for this project: `holding line` means a queue. `ship step` means a deploy command. `landing folder` means an output path. '
                '`shell switch` means an export env line.')


def nonliteral_question(ptype, item):
    """A task that needs the fact but shares no content word with the plant sentence. It names the topic by an alias that only a bridge turn explains."""
    a = item["alias"]
    if ptype == "R":
        return "For %s, give the closing marker that each change record needs to carry." % a
    if ptype == "D":
        return "For %s, which holding line was picked? Write its name." % a
    if ptype == "X":
        return "For %s, write the holding line that is active now, and list every holding line that was set aside." % a
    if ptype == "S":
        return "For %s, write the %s, character for character." % (a, L3_S_WORD[item["kind"]])
    if ptype == "E":
        return ("For %s, a teammate wants to run the same idea again. Give the go or no-go in proceed. In reason_code, copy the exact "
                "fault identifier it hit, character for character." % a)
    raise ValueError("no non-literal phrasing for probe type " + str(ptype))


def nonliteral_probe_prompt(ptype, script, marker, items=None):
    """Same head, JSON form and grader as the neutral probe. Only the question differs."""
    if items is None:
        items = script["items"][ptype]
    head = "[%s] Do not guess. If you do not know a value, set the main field to null.%s\n" % (marker, E_NOTE if ptype == "E" else "")
    body = "\n".join("%d. %s" % (x["id"], nonliteral_question(ptype, x)) for x in items)
    form = {"X": '{"answers":[{"id":1,"current":"...","replaced":["..."]}]}',
            "E": '{"answers":[{"id":1,"proceed":true,"reason_code":"..."}]}'}.get(ptype, '{"answers":[{"id":1,"answer":"..."}]}')
    return head + body + "\nReply with JSON only, in this form: " + form


def plant_sentences(ptype, item):
    """The sentences that plant a design-3 fact (a non-literal probe shares no content word with these)."""
    return [item["plant"]] + ([item["plant2"]] if item.get("plant2") else [])


def build_litm3(seed):
    """Script of the no-compaction condition, probe design 3. Facts of types R D X S E, 6 each: 2 in each third of the
    session. Each fact has an alias, and a bridge turn in the same third tells which topic the alias names.
    plan: the turns in order. Each third has 25 turns, so the thirds of the real turn order are the thirds of the plan."""
    g = Gen(seed, "litm3")
    g.r = random.Random("lhbench-litm3-%d" % seed)
    pr = random.Random("lhbench-litm3-prose-%d" % seed)
    taken = set()
    adj, noun = ADJ[:], NOUN[:]
    g.r.shuffle(adj)
    g.r.shuffle(noun)
    n_facts = len(L3_TYPES) * 3 * L3_PER_POS
    items = {t: [] for t in L3_TYPES}
    third_facts = [[], [], []]
    kinds = [("deploy command", g.cmd), ("output path", g.path), ("export env line", g.envline)]
    k = 0
    for t in L3_TYPES:
        for j in range(3 * L3_PER_POS):
            pos = POSITIONS[j // L3_PER_POS]
            topic = g.topics(1, taken)[0]
            alias = "the %s %s" % (adj[k], noun[k])
            k += 1
            it = {"id": j + 1, "topic": topic, "alias": alias, "position": pos, "third": j // L3_PER_POS}
            if t == "R":
                tok = g.tok()
                it.update(answer=tok, plant="Standing rule from now on: in the `%s` repository, every commit message must end with the tag `TAG-%s`." % (topic, tok),
                          question="What tag must every commit message in the `%s` repository end with?" % topic)
            elif t == "D":
                tok = g.tok()
                it.update(answer=tok, plant="Decision: the `%s` queue is `%s`." % (topic, tok), question="Which queue did we choose for `%s`?" % topic)
            elif t == "X":
                old, new = g.tok(), g.tok()
                it.update(old=old, new=new, plant="Decision: the `%s` queue is `%s`." % (topic, old),
                          plant2="Change for the `%s` queue: use `%s` instead." % (topic, new),
                          question="What is the current queue for `%s`? Also list every earlier queue that we replaced." % topic)
            elif t == "S":
                kind, make = kinds[j % 3]
                val = make()
                it.update(kind=kind, answer=val, plant="Settings note: the %s for `%s` is `%s`." % (kind, topic, val),
                          question="Give the exact %s for `%s`, character for character." % (kind, topic))
            else:
                name = g.r.choice(APPROACHES) + "-" + g.chars(3).lower()
                ecode = g.ecode()
                it.update(name=name, ecode=ecode, plant="Dead end: the `%s` approach for `%s` broke with `%s`." % (name, topic, ecode),
                          question="I plan to use the `%s` approach for `%s`. Should we proceed? In reason_code, copy the exact error code text that this approach failed with, character for character." % (name, topic))
            items[t].append(it)
            third_facts[it["third"]].append((t, it))
    plan, fill_k = [], 0
    for th in range(3):
        facts = third_facts[th][:]
        g.r.shuffle(facts)
        bridge = " ".join("In my notes, %s is the one called `%s`." % (it["alias"], it["topic"]) for _t, it in sorted(facts, key=lambda x: x[1]["alias"]))
        plan.append({"kind": "bridge", "third": th, "prompt": "%s Naming notes for this project, for reference: %s %s Reply only `ok`." % (prose3(pr, 300, 500), bridge, L3_WORD_LIST)})
        for _t, it in facts:
            plan.append({"kind": "fact", "third": th, "type": _t, "id": it["id"],
                         "prompt": "%s Note for this project: %s Reply only `ok`." % (prose3(pr, 300, 500), it["plant"])})
            if it.get("plant2"):
                plan.append({"kind": "x2", "third": th, "type": _t, "id": it["id"],
                             "prompt": "%s Note for this project: %s Reply only `ok`." % (prose3(pr, 300, 500), it["plant2"])})
            plan.append({"kind": "fill", "third": th, "k": fill_k})
            fill_k += 1
        while fill_k < (th + 1) * L3_FILL_PER_THIRD:
            plan.append({"kind": "fill", "third": th, "k": fill_k})
            fill_k += 1
    for i, step in enumerate(plan):
        step["turn"] = i
        if step["kind"] == "fact":
            for it in items[step["type"]]:
                if it["id"] == step["id"]:
                    it["turn"] = i
    script = {"design": 3, "gen_version": "litm3-1", "seed": seed, "items": items, "plan": plan, "n_facts": n_facts,
              "max_epoch": 0, "events": [], "fresh": {}}
    vals = set()
    for t, lst in items.items():
        for it in lst:
            for kk in ("answer", "old", "new", "ecode"):
                if kk in it:
                    vals.add(it[kk])
    script["secrets"] = sorted(vals)
    return script


def prose3(r, lo=600, hi=1200):
    """Like prose(), with sentences that hold no cue phrase."""
    out = ""
    while len(out) < lo:
        s_ = r.choice(SENT3) % {"w": r.choice(WORDS), "w2": r.choice(WORDS)}
        if out and len(out) + len(s_) + 1 > hi:
            break
        out += (" " if out else "") + s_
    return out


def shuffle3(items, seed, ptype, phrasing):
    """Design 3: the order of the questions inside one probe is a seeded shuffle, so that the order in the probe is not the position
    in the session (the old order was oldest first, which is the position). The same order for every arm."""
    r = random.Random("lhbench-order3-%d-%s-%s" % (seed, ptype, phrasing))
    out = list(items)
    r.shuffle(out)
    return out


def filler_doc_prompt(script, k, lines):
    """Filler turn of design 3: a pasted document and one small task. Returns (prompt, expected first token of the task)."""
    name, text = filler_file(script["seed"], 1000 + k, 0, lines=lines)
    r = random.Random("lhbench-litm3-fill-%d-%d" % (script["seed"], k))
    expect = text.splitlines()[-1].split()[0]
    prompt = ("%s Here is a document to skim. It is pasted below between the markers.\n<<<DOC\n%sDOC>>>\n"
              "Task: reply with only the number at the start of the last line of the document." % (prose3(r, 300, 500), text))
    return prompt, expect


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
