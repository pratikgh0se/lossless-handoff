#!/usr/bin/env python3
"""Probe design 4 of the continuity benchmark: update chains with interference, for the no-compaction condition.
Spec: design 4 (Fable design pass, 2026-11-22). Designs 1 to 3 and gen.py do not change.

One item is a chain: one key, one attribute (queue or tag), a list of statements (set, change, reject, undo, scoped).
Levels L1 to L4 (L5 for the calibration escalation) share one mixed session. A chain is tight: all its statements are in one third,
so the position of a chain is that third. The probe asks one level and one phrasing at a time (a level is the "type" of a probe row).

Choices where the spec was open (the simplest reading):
  - previous = the value in force just prior to the current one: the value of the current before the last op that changed the current
    (after an undo, the undone value).
  - replaced = every value that was in force at some time and is not current (the undone value is in it).
  - family of size n = the base key plus n-1 suffixed keys. The base member has the description "... , the ordinary everyday copy".
  - the attribute (queue or tag) alternates by family, so siblings share the attribute.
  - the fields previous and replaced are explained in the head of every phrasing, so M2 and M3 exist for every phrasing.
  - undo is the last update op in half of the L3 and L4 chains (alternating), and in the middle in the other half. Rejects and the scoped
    note come after the last update op.
"""
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen        # noqa: E402
import grade      # noqa: E402

DESIGN = 4
GEN_VERSION = "litm4-1"
POSITIONS = gen.POSITIONS
LEVELS = {
    "L1": {"k": 2, "fam": 1, "rejects": 0, "undo": False, "scoped": 0, "chains": 3, "prefix": False},
    "L2": {"k": 4, "fam": 2, "rejects": 1, "undo": False, "scoped": 0, "chains": 4, "prefix": False},
    "L3": {"k": 6, "fam": 3, "rejects": 1, "undo": True, "scoped": 0, "chains": 3, "prefix": False},
    "L4": {"k": 10, "fam": 3, "rejects": 2, "undo": True, "scoped": 1, "chains": 3, "prefix": True},
    "L5": {"k": 16, "fam": 4, "rejects": 2, "undo": True, "scoped": 1, "chains": 4, "prefix": True},     # calibration escalation, once
}
D4_LEVELS = ("L1", "L2", "L3", "L4")
PLANT_TURNS_PER_THIRD = 23
FILL_TURNS_PER_THIRD = 12
STMTS_PER_TURN = 4
PHRASINGS = ("cued", "neutral", "nonliteral")
ERROR_CLASSES = ("stale", "lure", "sibling", "other_chain", "abstain", "novel", "unreadable")
FORM = '{"answers":[{"id":1,"current":"...","previous":"...","replaced":["..."]}]}'
FIELDS = ("In each answer, `current` is the value in force now, `previous` is the value that was in force just prior to it, "
          "and `replaced` lists every other value that was ever in force for it.")

# ---- lexicon. A key is a base plus an optional suffix. The description of a key needs common knowledge only and shares no content word
# and no 5-character stem with the key (checked in code: overlap()). Do not use the words of a key in any description.
BASES = [
    ("invoice-mailer", "the service that sends bills to customers"),
    ("photo-resizer", "the tool that shrinks pictures to smaller dimensions"),
    ("login-guard", "the gate that checks who may enter the application"),
    ("report-builder", "the job that assembles monthly summaries for managers"),
    ("search-indexer", "the worker that catalogs documents so lookups return quickly"),
    ("order-tracker", "the component that follows purchases until delivery"),
    ("backup-runner", "the task that copies data to safe storage each evening"),
    ("video-encoder", "the program that converts films into smaller files"),
    ("email-digest", "the roundup of messages sent to subscribers"),
    ("payment-gateway", "the bridge to the bank for card charges"),
    ("cache-warmer", "the helper that preloads popular pages into memory"),
    ("audit-logger", "the recorder of who changed what and when"),
    ("image-cropper", "the utility that trims the edges of photos"),
    ("tax-calculator", "the module that works out what is owed to the government"),
    ("chat-relay", "the forwarder that passes conversations between users"),
    ("stock-syncer", "the updater that keeps warehouse counts matching"),
    ("pdf-renderer", "the converter that draws documents as fixed pages"),
    ("sms-notifier", "the sender of short alerts to cellular phones"),
    ("token-refresher", "the renewer of expiring access passes"),
    ("data-exporter", "the extractor that hands records to partner companies"),
    ("billing-scheduler", "the planner of when customers are charged"),
    ("thumbnail-maker", "the generator of tiny previews for pictures"),
    ("health-checker", "the watcher that pings servers to see if they live"),
    ("geo-locator", "the finder of a place from coordinates"),
    ("spam-filter", "the screener that blocks unwanted messages"),
    ("session-store", "the keeper of who is currently signed in"),
    ("asset-packer", "the bundler that squeezes scripts and styles together"),
    ("retry-handler", "the helper that tries failed work again"),
    ("font-loader", "the fetcher of typefaces for the screen"),
    ("metric-collector", "the gatherer of numbers about speed and load"),
]
BASE_MEMBER = "that is the ordinary everyday copy"
MODS = [
    ("eu", "in the European region"),
    ("us", "in the North American region"),
    ("nightly", "that runs after dark"),
    ("staging", "for the practice environment"),
    ("canary", "for the small early test group"),
    ("legacy", "for the old outdated version"),
    ("beta", "for volunteers trying new features"),
    ("bulk", "for very large batches"),
    ("fast", "for speedy urgent cases"),
    ("mobile", "for handheld devices"),
    ("internal", "for company employees only"),
    ("archive", "for long-term storage copies"),
]


def words_of(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in gen.STOPWORDS}


def stem(w):
    return w[:5]


def overlap(key, desc):
    """Content words and 5-character stems that a key and a description share (empty set: zero overlap)."""
    kw, dw = words_of(key), words_of(desc)
    return (kw & dw) | ({stem(w) for w in kw} & {stem(w) for w in dw})


def describe(base_desc, mod_desc):
    return "%s %s" % (base_desc, mod_desc) if mod_desc else base_desc


# ---- statements (the five ops) ----
OPS = ("set", "change", "reject", "undo", "scoped")


def val_text(attr, v):
    return "TAG-" + v if attr == "tag" else v


def stmt_text(op, key, attr, v=None):
    t = val_text(attr, v) if v is not None else None
    noun = "repository" if attr == "tag" else "queue"
    if op == "set":
        if attr == "tag":
            return "Standing rule from now on: in the `%s` repository, every commit message must end with the tag `%s`." % (key, t)
        return "Decision: the `%s` queue is `%s`." % (key, t)
    if op == "change":
        return "Change for the `%s` %s: use `%s` instead." % (key, "tag" if attr == "tag" else "queue", t)
    if op == "reject":
        return "Proposal for the `%s` %s: `%s`. Rejected. Keep the active one." % (key, "tag" if attr == "tag" else "queue", t)
    if op == "undo":
        return "Undo the last change to the `%s` %s. The value before it is active again." % (key, "tag" if attr == "tag" else "queue")
    if op == "scoped":
        return "For the dry-run copy of `%s` only, the %s is `%s`. The main `%s` %s does not change." % (
            key, "tag" if attr == "tag" else "queue", t, key, "tag" if attr == "tag" else "queue")
    raise ValueError(op)


def interpret(ops):
    """The op interpreter. ops: list of {"op", "value"}. Returns {"current", "previous", "replaced" (sorted list), "ever" (in order)}.
    set: V is in force. change: V is in force. reject and scoped: no effect. undo: the value before the last change is in force again.
    previous = the value of the current just before the last op that changed the current. replaced = every value in force at some time, not current."""
    stack, ever, prev = [], [], None
    for o in ops:
        old = stack[-1] if stack else None
        if o["op"] == "set":
            stack = [o["value"]]
            ever.append(o["value"])
        elif o["op"] == "change":
            stack.append(o["value"])
            ever.append(o["value"])
        elif o["op"] == "undo":
            if len(stack) < 2:
                raise ValueError("undo with no change to undo")
            stack.pop()
        elif o["op"] in ("reject", "scoped"):
            continue
        else:
            raise ValueError(o["op"])
        new = stack[-1]
        if new != old:
            prev = old
    cur = stack[-1]
    return {"current": cur, "previous": prev, "replaced": sorted({v for v in ever if v != cur}), "ever": ever}


# ---- generator ----
class Tok:
    def __init__(self, r):
        self.r, self.used = r, set()

    def chars(self, n):
        return "".join(self.r.choice(gen.B32) for _ in range(n))

    def new(self, prefix=None):
        for _ in range(10000):
            v = ("%s-%s" % (prefix, self.chars(4))) if prefix else "%s-%04d-%s" % (self.chars(4), self.r.randrange(10000), self.chars(4))
            if v not in self.used:
                self.used.add(v)
                return v
        raise RuntimeError("no unique value")

    def prefix(self):
        return "%s-%04d" % (self.chars(4), self.r.randrange(10000))


def chain_ops(spec, toks, r, undo_last, prefix):
    """Update ops (set, change..., one undo), then the lures (rejects, scoped). Values are fresh tokens."""
    k = spec["k"]
    vals = [toks.new(prefix) for _ in range(k)]
    ops = [{"op": "set", "value": vals[0]}] + [{"op": "change", "value": v} for v in vals[1:]]
    if spec["undo"]:
        if undo_last:
            ops.append({"op": "undo", "value": None})
        else:
            j = r.randint(2, k - 1)                  # after the change that made vals[j - 1] active, before the next change
            ops.insert(j, {"op": "undo", "value": None})
    lures = [{"op": "reject", "value": toks.new(prefix)} for _ in range(spec["rejects"])] + \
            [{"op": "scoped", "value": toks.new(prefix)} for _ in range(spec["scoped"])]
    r.shuffle(lures)
    return ops + lures


def pick_families(r, n_families, taken_bases):
    free = [b for b in BASES if b[0] not in taken_bases]
    r.shuffle(free)
    return free[:n_families]


def build_litm4(seed, levels=D4_LEVELS):
    """Script of the no-compaction condition, design 4. 3 thirds of 23 plant turns and 12 filler turns (105 turns).
    items: {level: [chain, ...]}. Each chain: id, level, key, desc, attr, family, third, position, ops, stmts, current, previous, replaced,
    values (in force at some time, in order), lures (rejected or scoped values), undo_last, turn (of its last statement)."""
    r = random.Random("lhbench-litm4-%d" % seed)
    pr = random.Random("lhbench-litm4-prose-%d" % seed)
    toks = Tok(r)
    taken_bases, fam_count = set(), 0
    items = {lv: [] for lv in levels}
    per_third = [[], [], []]
    for lv in levels:
        spec = LEVELS[lv]
        n_fam = spec["chains"] // spec["fam"]
        idx = 0
        for th in range(3):
            fams = pick_families(r, n_fam, taken_bases)
            for base, bdesc in fams:
                taken_bases.add(base)
                fid = "%s-f%d" % (lv, fam_count)
                attr = ("queue", "tag")[fam_count % 2]
                fam_count += 1
                prefix = toks.prefix() if spec["prefix"] else None
                mods = r.sample(MODS, spec["fam"] - 1)
                members = [(base, describe(bdesc, BASE_MEMBER if spec["fam"] > 1 else ""))] + \
                          [("%s-%s" % (base, m), describe(bdesc, md)) for m, md in mods]
                for key, desc in members:
                    undo_last = bool(idx % 2 == 0)
                    ops = chain_ops(spec, toks, r, undo_last, prefix)
                    res = interpret(ops)
                    it = {"id": idx + 1, "level": lv, "key": key, "desc": desc, "attr": attr, "family": fid, "third": th, "position": POSITIONS[th],
                          "ops": ops, "stmts": [stmt_text(o["op"], key, attr, o["value"]) for o in ops],
                          "current": res["current"], "previous": res["previous"], "replaced": res["replaced"], "values": res["ever"],
                          "lures": [o["value"] for o in ops if o["op"] in ("reject", "scoped")],
                          "undo_last": bool(spec["undo"] and undo_last)}
                    idx += 1
                    items[lv].append(it)
                    per_third[th].append(it)
    plan, fill_k = [], 0
    for th in range(3):
        turns = interleave(per_third[th], r)
        for i, group in enumerate(turns):
            body = " ".join(s for _it, s in group)
            plan.append({"kind": "fact", "third": th, "prompt": "%s Note for this project: %s Reply only `ok`." % (gen.prose3(pr, 300, 500), body),
                         "keys": [it["key"] for it, _s in group]})
            for it, _s in group:
                it["_last"] = len(plan) - 1
            if ((i + 1) * FILL_TURNS_PER_THIRD) // len(turns) > (i * FILL_TURNS_PER_THIRD) // len(turns):
                plan.append({"kind": "fill", "third": th, "k": fill_k})
                fill_k += 1
    for i, step in enumerate(plan):
        step["turn"] = i
    for lv in levels:
        for it in items[lv]:
            it["turn"] = it.pop("_last")
    vals = set()
    for lv in levels:
        for it in items[lv]:
            vals.update(v for v in it["values"] + it["lures"])
    return {"design": DESIGN, "gen_version": GEN_VERSION, "seed": seed, "levels": list(levels), "items": items, "plan": plan,
            "n_facts": sum(len(v) for v in items.values()), "max_epoch": 0, "events": [], "fresh": {}, "secrets": sorted(vals)}


def interleave(chains, r):
    """Order the statements of one third into turns of STMTS_PER_TURN. The order inside a chain is kept. One key is never twice in a turn."""
    left = {id(c): list(range(len(c["stmts"]))) for c in chains}
    total = sum(len(c["stmts"]) for c in chains)
    turns = []
    while sum(len(v) for v in left.values()):
        live = [c for c in chains if left[id(c)]]
        live.sort(key=lambda c: -(len(left[id(c)]) + r.random() * 1.5))
        take = live[:STMTS_PER_TURN]
        group = []
        for c in take:
            i = left[id(c)].pop(0)
            group.append((c, c["stmts"][i]))
        r.shuffle(group)
        turns.append(group)
    assert total == sum(len(g) for g in turns)
    return turns


# ---- probes ----
def question(phrasing, it):
    a = it["attr"]
    if phrasing == "cued":
        return "What is the %s for `%s` now? Give the value before it and all replaced values." % (a, it["key"])
    if phrasing == "neutral":
        return "Write the %s setting for `%s`." % (a, it["key"])
    if phrasing == "nonliteral":
        return "Write the %s setting for %s." % (a, it["desc"])
    raise ValueError(phrasing)


def probe_prompt(level, script, marker, phrasing, items=None):
    if items is None:
        items = script["items"][level]
    head = "[%s] Do not guess. If you do not know a value, set `current` to null. %s%s\n" % (
        marker, FIELDS, gen.CUED_HEAD_TAIL if phrasing == "cued" else "")
    body = "\n".join("%d. %s" % (x["id"], question(phrasing, x)) for x in items)
    return head + body + "\nReply with JSON only, in this form: " + FORM


def strip_tag(a):
    return a[4:] if isinstance(a, str) and a.startswith("TAG-") else a


def classify(it, ans, script):
    """Class of a wrong answer a (normalised text) of chain it: stale (with lag), lure, sibling, other_chain, novel. Returns (class, lag)."""
    a = strip_tag(ans)
    if a in it["values"] and a != it["current"]:
        return "stale", len(it["values"]) - 1 - it["values"].index(a)
    if a in it["lures"]:
        return "lure", None
    for lv in script["items"].values():
        for o in lv:
            if o is it:
                continue
            if a in o["values"] or a in o["lures"]:
                return ("sibling" if o["family"] == it["family"] else "other_chain"), None
    return "novel", None


def grade_level(items, raw, script):
    """One dict for each chain: id, parse_ok, parsed, abstained, m1, m2, m3, correct (= M1), error_class, lag. No partial credit.
    A null current is an abstain: wrong, counted apart. An unreadable reply (no entry) is wrong."""
    answers = grade.find_answers(raw)
    out = []
    for it in items:
        e = grade.entry_for(answers, it["id"]) if answers is not None else None
        base = {"id": it["id"], "parse_ok": False, "parsed": None, "abstained": False, "m1": False, "m2": False, "m3": False,
                "correct": False, "error_class": "unreadable", "lag": None}
        if e is None:
            out.append(base)
            continue
        base["parsed"] = e
        if "current" in e and e["current"] is None:
            base.update(parse_ok=True, abstained=True, error_class="abstain")
            out.append(base)
            continue
        cur = grade.norm(e.get("current"))
        if cur is None:
            base["error_class"] = "unreadable"
            out.append(base)
            continue
        base["parse_ok"] = True
        cur = strip_tag(cur)
        prev = grade.norm(e.get("previous"))
        prev = strip_tag(prev) if prev is not None else None
        rep = e.get("replaced")
        reps = None
        if isinstance(rep, list) and all(isinstance(x, str) for x in rep):
            reps = sorted({strip_tag(grade.norm(x)) for x in rep})
        m1 = cur == it["current"]
        m2 = m1 and prev == it["previous"]
        m3 = m2 and reps == it["replaced"]
        base.update(m1=m1, m2=m2, m3=m3, correct=m1)
        if m1:
            base["error_class"] = None
        else:
            base["error_class"], base["lag"] = classify(it, cur, script)
        out.append(base)
    return out


# ---- offline solvers ----
RES = [
    ("set", re.compile(r"Decision: the `([^`]+)` queue is `([^`]+)`\.")),
    ("set", re.compile(r"Standing rule from now on: in the `([^`]+)` repository, every commit message must end with the tag `TAG-([^`]+)`\.")),
    ("change", re.compile(r"Change for the `([^`]+)` (?:queue|tag): use `(?:TAG-)?([^`]+)` instead\.")),
    ("reject", re.compile(r"Proposal for the `([^`]+)` (?:queue|tag): `(?:TAG-)?([^`]+)`\. Rejected\. Keep the active one\.")),
    ("undo", re.compile(r"Undo the last change to the `([^`]+)` (?:queue|tag)\. The value before it is active again\.")),
    ("scoped", re.compile(r"For the dry-run copy of `([^`]+)` only, the (?:queue|tag) is `(?:TAG-)?([^`]+)`\. The main `[^`]+` (?:queue|tag) does not change\.")),
]


def parse_turn(prompt):
    """The statements of one plant-turn prompt in text order: [(op, key, value or None)]. Reads the text only."""
    body = prompt.split("Note for this project:", 1)[1].rsplit(" Reply only", 1)[0]
    found = []
    for op, rx in RES:
        for m in rx.finditer(body):
            found.append((m.start(), op, m.group(1), m.group(2) if rx.groups >= 2 else None))
    found.sort()
    return [(op, key, v) for _p, op, key, v in found]


def solve_full(script, upto_turn=None):
    """Full-information solver: reads only the plant turns (text), applies the ops by key. Returns {key: interpret(...) result}."""
    per = {}
    for step in script["plan"]:
        if step["kind"] == "fill" or (upto_turn is not None and step["turn"] > upto_turn):
            continue
        for op, key, v in parse_turn(step["prompt"]):
            per.setdefault(key, []).append({"op": op, "value": v})
    return {k: interpret(v) for k, v in per.items()}


def last_mention_answers(script):
    """The naive solver: the answer for a key is the last value that any statement names for it (a rejected or scoped value counts)."""
    last = {}
    for step in script["plan"]:
        if step["kind"] == "fill":
            continue
        for op, key, v in parse_turn(step["prompt"]):
            if v is not None:
                last[key] = v
    return last


def last_mention_rate(script, level):
    ans = last_mention_answers(script)
    its = script["items"][level]
    return sum(1 for it in its if ans.get(it["key"]) == it["current"]) / float(len(its))


def check_script(script):
    """Code checks of a script. Returns a list of problems (empty = fine): zero overlap, unique descriptions, statements, counts."""
    bad = []
    descs = [it["desc"] for lv in script["items"].values() for it in lv]
    keys = [it["key"] for lv in script["items"].values() for it in lv]
    if len(set(descs)) != len(descs):
        bad.append("descriptions are not unique")
    if len(set(keys)) != len(keys):
        bad.append("keys are not unique")
    for lv in script["items"].values():
        for it in lv:
            ov = overlap(it["key"], it["desc"])
            if ov:
                bad.append("overlap %s: %s" % (it["key"], sorted(ov)))
    if sum(len(s["stmts"]) for lv in script["items"].values() for s in lv) != sum(len(parse_all(step)) for step in script["plan"] if step["kind"] != "fill"):
        bad.append("statement count differs from the plan")
    return bad


def parse_all(step):
    return parse_turn(step["prompt"])


# ---- gates ----
O_MIN, Z_MAX, SOLVER_MAX, CEILING, FLOOR = 0.95, 0.02, 0.50, 0.95, 0.30
BAND_LOW, BAND_HIGH = 0.55, 0.80
SEED2_LOW, SEED2_HIGH = 0.45, 0.90
METRICS = ("m1", "m2", "m3")


def rate(rows, **flt):
    sel = [r for r in rows if all(r.get(k) == v for k, v in flt.items())]
    if not sel:
        return None, 0
    return sum(1 for r in sel if r.get(flt.get("_m", "m1"))) / float(len(sel)), len(sel)


def cell(rows, arm, level, metric, phrasing=None, position=None):
    sel = [r for r in rows if r["arm"] == arm and r.get("type") == level and (phrasing is None or r.get("phrasing") == phrasing)
           and (position is None or r.get("position") == position) and r.get("condition", "nocompact") == "nocompact"]
    if not sel:
        return None, 0
    return sum(1 for r in sel if r.get(metric) is True) / float(len(sel)), len(sel)


def gate_lines(rows, scripts=(), levels=D4_LEVELS):
    """Design-4 gates. Returns [(name, status, detail)] with status PASS, FAIL, NO DATA or INFO.
    O: at least O_MIN for each level, phrasing and metric. Z: at most Z_MAX (M1). Solver: last mention wins at most SOLVER_MAX at L3 and L4.
    Ceiling and floor: arm A above CEILING is TOO EASY, below FLOOR is TOO HARD (per level and phrasing, M1)."""
    out = []
    for arm, lim, name in (("O", O_MIN, "oracle O (no filler)"),):
        bad, seen = [], 0
        for lv in levels:
            for ph in PHRASINGS:
                for m in METRICS:
                    v, n = cell(rows, arm, lv, m, ph)
                    if n:
                        seen += 1
                        if v < lim:
                            bad.append("%s %s %s %.0f%%" % (lv, ph, m.upper(), 100 * v))
        out.append(("%s: at least %d%% for each level, phrasing and metric" % (name, int(100 * lim)),
                    "NO DATA" if not seen else ("FAIL" if bad else "PASS"), "below the limit: %s" % (bad or "none") if seen else "no rows"))
    bad, seen = [], 0
    for lv in levels:
        for ph in PHRASINGS:
            v, n = cell(rows, "Z", lv, "m1", ph)
            if n:
                seen += 1
                if v > Z_MAX:
                    bad.append("%s %s %.0f%%" % (lv, ph, 100 * v))
    out.append(("amnesia Z (no history): at most %d%%" % int(100 * Z_MAX), "NO DATA" if not seen else ("FAIL" if bad else "PASS"), "above the limit: %s" % (bad or "none") if seen else "no rows"))
    if scripts:
        bad = []
        for lv in ("L3", "L4"):
            if lv in levels:
                for sc in scripts:
                    v = last_mention_rate(sc, lv)
                    if v > SOLVER_MAX:
                        bad.append("seed %s %s %.0f%%" % (sc["seed"], lv, 100 * v))
        out.append(("offline solver 'last mention wins': at most %d%% on M1 at L3 and L4" % int(100 * SOLVER_MAX), "FAIL" if bad else "PASS",
                    "above the limit: %s" % (bad or "none")))
    for lv in levels:
        for ph in PHRASINGS:
            v, n = cell(rows, "A", lv, "m1", ph)
            if n and v > CEILING:
                out.append(("arm A %s %s" % (lv, ph), "INFO", "TOO EASY: %.0f%% is above %d%%" % (100 * v, int(100 * CEILING))))
            if n and v < FLOOR:
                out.append(("arm A %s %s" % (lv, ph), "INFO", "TOO HARD: %.0f%% is below %d%%" % (100 * v, int(100 * FLOOR))))
    return out


def error_mix(rows, arm, level=None):
    """Counts of the error classes of the wrong answers of one arm (no-compaction rows)."""
    mix = {c: 0 for c in ERROR_CLASSES}
    for r in rows:
        if r["arm"] == arm and r.get("condition", "nocompact") == "nocompact" and str(r.get("type", "")).startswith("L") and (level is None or r["type"] == level):
            c = r.get("error_class")
            if c in mix:
                mix[c] += 1
    return mix


# ---- calibration: the pick rule ----
def calibration_cells(rows, levels=D4_LEVELS):
    """{(metric, level): {"A": (rate, n), "O": ..., "Z": ...}} pooled over phrasings (no-compaction rows)."""
    out = {}
    for m in METRICS:
        for lv in levels:
            out[(m, lv)] = {arm: cell(rows, arm, lv, m) for arm in ("A", "O", "Z")}
    return out


def decide(rows, levels=D4_LEVELS, l5_done=False):
    """The pick rule of the calibration. Metrics in the order M1, M2, M3; levels from L1 up. Pick the first (metric, level) where arm A
    (phrasings pooled) is 55% to 80%, O is at least 95% and Z is at most 2%.
    If A is above 80% in every cell: add L5 once; if still above 80%: Sonnet has no loss here.
    Returns {"decision": pick | add_l5 | no_loss | none, "pick", "seed2" (a cell of A between 45% and 90%), "notes"}."""
    cells = calibration_cells(rows, levels)
    notes, pick = [], None
    a_vals = [(k, c["A"]) for k, c in cells.items() if c["A"][1]]
    seed2 = any(SEED2_LOW <= v[0] <= SEED2_HIGH for _k, v in a_vals)
    for m in METRICS:
        for lv in levels:
            c = cells[(m, lv)]
            a, o, z = c["A"], c["O"], c["Z"]
            if not a[1]:
                continue
            in_band = BAND_LOW <= a[0] <= BAND_HIGH
            o_ok = o[1] > 0 and o[0] >= O_MIN
            z_ok = z[1] == 0 or z[0] <= Z_MAX
            if in_band and not o_ok:
                notes.append("%s %s: A is in the band (%.0f%%) but O is %s: the loss is interference at zero distance, not lost in the middle." % (
                    m.upper(), lv, 100 * a[0], "%.0f%%" % (100 * o[0]) if o[1] else "missing"))
            if in_band and o_ok and z_ok and pick is None:
                pick = (m, lv)
    if pick:
        return {"decision": "pick", "pick": pick, "seed2": seed2, "notes": notes}
    if a_vals and all(v[0] > BAND_HIGH for _k, v in a_vals):
        if "L5" in levels or l5_done:
            return {"decision": "no_loss", "pick": None, "seed2": seed2,
                    "notes": notes + ["Arm A is above 80% in every cell, also with L5: Sonnet has no loss here. Report it."]}
        return {"decision": "add_l5", "pick": None, "seed2": seed2, "notes": notes + ["Arm A is above 80% in every cell: add L5 (k 16, family 4) once."]}
    return {"decision": "none", "pick": None, "seed2": seed2, "notes": notes + ["No cell has A in 55% to 80% with O at least 95% and Z at most 2%."]}


def describe_decision(d):
    lines = ["decision: %s" % d["decision"]]
    if d["pick"]:
        lines.append("pick: %s %s (commit it in thresholds.json litm.design4.pick before the full run)" % (d["pick"][0].upper(), d["pick"][1]))
    lines.append("seed 2 needed (a cell of arm A is between %d%% and %d%%): %s" % (int(100 * SEED2_LOW), int(100 * SEED2_HIGH), "yes" if d["seed2"] else "no"))
    lines += ["note: " + n for n in d["notes"]]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "decide":
        rd = argv[1] if os.path.isdir(argv[1]) else os.path.join(HERE, "results", argv[1])
        rows = []
        with open(os.path.join(rd, "results.jsonl"), encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    rows.append(json.loads(ln))
        rows = [r for r in gen_dedupe(rows) if str(r.get("type", "")).startswith("L")]
        print(describe_decision(decide(rows)))
        return 0
    print("usage: design4.py decide <run-id | run folder>")
    return 2


def gen_dedupe(rows):
    import gates
    return gates.dedupe_rows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
