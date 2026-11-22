#!/usr/bin/env python3
"""Grade the stored raw replies of a finished run again with the current grader. No model call.
Usage: regrade.py <run-id | path of the run folder>
It keeps the old file as results.jsonl.pre-regrade (only the first time), rewrites results.jsonl,
and builds the report again with report.py.
"""
import collections, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen, grade


def need_epoch(cfg, arm, target):
    """Same rule as runner.need_epoch: items of epochs up to this value are planted before the probe."""
    return (max(cfg["n"]) - 1) if arm == "O" else target - 1


def compute_retention(rd, cfg, script):
    """Build retention.jsonl from live transcripts when the run has none. Returns the row count."""
    out = os.path.join(rd, "retention.jsonl")
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return -1
    import runner
    try:
        state = json.load(open(os.path.join(rd, "state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    base = runner.work_base(cfg.get("work_base"))
    rows = []
    for name, st in sorted(state.get("sessions", {}).items()):
        m = re.match(r"s(\d+)-([A-Z])$", name)
        if not m or m.group(2) == "Z" or not st.get("sid"):
            continue
        seed, arm = int(m.group(1)), m.group(2)
        proj = os.path.realpath(os.path.join(base, "%s%s-s%d-%s" % (runner.PREFIX, cfg["run_id"], seed, arm)))
        tp = os.path.join(runner.PROJECTS, runner.slug(proj), st["sid"] + ".jsonl")
        if not os.path.isfile(tp):
            continue
        ret = grade.retention(runner.read_jsonl(tp), script(seed))
        ret.update({"run": cfg["run_id"], "seed": seed, "arm": arm})
        rows.append(ret)
    if rows:
        with open(out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    return len(rows)


def main():
    if len(sys.argv) != 2:
        print("usage: regrade.py <run-id | run folder>", file=sys.stderr)
        return 2
    rd = sys.argv[1]
    if not os.path.isdir(rd):
        rd = os.path.join(HERE, "results", rd)
    if not os.path.isdir(rd):
        print("no such run:", sys.argv[1], file=sys.stderr)
        return 2
    path = os.path.join(rd, "results.jsonl")
    cfg = json.load(open(os.path.join(rd, "config.json"), encoding="utf-8"))
    rows = []
    for ln in open(path, encoding="utf-8"):
        if ln.strip():
            rows.append(json.loads(ln))
    if not rows:
        print("no result rows in", path, file=sys.stderr)
        return 2
    if any("raw" not in r for r in rows):
        print("stop: the results do not store the raw reply, so they cannot be graded again", file=sys.stderr)
        return 2
    scripts = {}

    def script(seed):
        if seed not in scripts:
            sp = os.path.join(rd, "scripts", "%d.json" % seed)
            if os.path.isfile(sp):
                scripts[seed] = json.load(open(sp, encoding="utf-8"))
            else:
                scripts[seed] = gen.build(seed, cfg["gen_version"])
        return scripts[seed]

    groups = collections.OrderedDict()      # one group = one probe reply
    for r in rows:
        groups.setdefault((r["seed"], r["arm"], r["n_target"], r["type"], r["fork_sid"], r["raw"], r.get("condition", "compact")), []).append(r)
    changed = acc_before = acc_after = 0
    for (seed, arm, n, ptype, _fsid, raw, cond), grp in groups.items():
        s = script(seed)
        if ptype == "F":
            items = s["fresh"][str(n)]
        else:
            items = gen.select(s, ptype, None if (arm == "Z" or cond == "nocompact") else need_epoch(cfg, arm, n))
        by_id = {g["id"]: g for g in grade.grade(ptype, items, raw)}
        for r in grp:
            g = by_id[r["item"]]
            acc_before += 1 if r["correct"] else 0
            acc_after += 1 if g["correct"] else 0
            if (r["parse_ok"], r["correct"], r.get("abstained", False)) != (g["parse_ok"], g["correct"], g["abstained"]):
                changed += 1
            r["parsed"], r["parse_ok"], r["correct"], r["abstained"] = g["parsed"], g["parse_ok"], g["correct"], g["abstained"]
    nret = compute_retention(rd, cfg, script)
    if nret == 0:
        print("retention: no data (no transcripts of this run found)", file=sys.stderr)
    elif nret > 0:
        print("retention: %d sessions counted from live transcripts" % nret, file=sys.stderr)
    bak = path + ".pre-regrade"
    if not os.path.exists(bak):
        shutil.copyfile(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, path)
    print("regrade: %d rows, %d changed, correct %d -> %d" % (len(rows), changed, acc_before, acc_after), file=sys.stderr)
    return subprocess.call([sys.executable, os.path.join(HERE, "report.py"), os.path.realpath(rd)], stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
