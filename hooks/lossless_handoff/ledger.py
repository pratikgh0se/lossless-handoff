# The decisions list (ledger): read, merge, maintain and repair.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

LEDGER_LINE_MAX = 700  # F39.11 (was 400)
LEDGER_SECTION_CAP = 4000
MERGE_INPUT_CAP = 40000  # F40.2 (was 60,000)
LEDGER_TIMEOUT_DEFAULT = 100  # F40.2
LOCK_WAIT_S = 10
BACKUPS_KEEP = 20
LEDGER_SYSTEM = "You maintain a project's decisions ledger for an AI coding agent. Output only the ledger."
LEDGER_PROMPT = ("Stdin has the CURRENT LEDGER, then NEW CONVERSATION since it was last updated. "
                 "Output only the changes, one per line, nothing else: new decisions as "
                 "\"- D<n> [YYYY-MM-DD] [rule|decision|task] <decision> | why: <reason>\" with n continuing after the highest "
                 "existing id, and replacements as \"SUPERSEDE D<old> BY D<new>: <reason>\" where D<new> is an "
                 "existing or newly added id. Output \"NO CHANGES\" if nothing qualifies. Rules: keep every existing entry and its id. "
                 "Add an entry only for a decision the user made or explicitly approved (a choice, a rule, a "
                 "constraint, a permission, a rejected option), dated with the day it was made. When the user "
                 "replaces an earlier decision, rewrite that entry as SUPERSEDED by the new id with the reason; "
                 "never delete entries. Do not record plans, progress or the assistant's own unapproved proposals. "
                 "Format of active lines: \"- D<n> [YYYY-MM-DD] [rule|decision|task] <decision> | why: <reason>\". Format of superseded "
                 "lines: \"- D<n> [YYYY-MM-DD] SUPERSEDED by D<m> [YYYY-MM-DD]: <old decision> | because: <reason>\". "
                 "Keep the ledger lean: when a new decision changes, narrows or repeats an older one, rewrite the "
                 "older entry as SUPERSEDED by the new id (because: replaced, narrowed or merged). Two active "
                 "entries must never say the same thing or contradict each other. When you merge or narrow "
                 "entries, keep every concrete value from both (names, numbers, paths, limits, schedules). "
                 # F42.2
                 "Tag each new entry [rule], [decision] or [task]. Record operating modes and the user's working "
                 "preferences as [rule] entries the moment the user states them (for example how many agents run in "
                 "parallel, priority order, who writes tests, how to ask the user questions), with the concrete "
                 "numbers. One-off approvals and status updates are [task]. When an active [task] is done or was a "
                 "one-off already acted on, output \"RESOLVE D<n>: <reason>\". When two active entries contradict "
                 "each other, SUPERSEDE the older one. For untagged existing entries you may output "
                 "\"KIND D<n> rule|decision|task\" (at most 40).")
MAINT_PROMPT = ("Stdin has a project's decisions LEDGER (one entry per line). Output ONLY maintenance lines, one per "
                "line, nothing else, no prose: \"KIND D<n> rule|decision|task\" to tag an untagged active entry; "
                "\"RESOLVE D<n>: <reason>\" for an active task or untagged entry that is finished or was a one-off "
                "approval, action or status already acted on; \"SUPERSEDE D<old> BY D<new>: <reason>\" when two "
                "active entries contradict each other or say the same thing (old and new are existing ids; the newer "
                "entry is D<new>). Kinds: rule = a standing rule, preference, priority, operating mode or process "
                "that applies until replaced (how many builders run in parallel, who writes tests, how the user wants "
                "to be asked, what to never do, naming, priority order); decision = a design, product or technical "
                "choice; task = a one-off approval, action or status that is finished once done. Tag every untagged "
                "active entry. Never add entries, never edit entry text, never RESOLVE a rule. Output \"NO CHANGES\" "
                "if nothing applies.")
ACT_RE = re.compile(r"^- D(\d+) \[(\d{4}-\d{2}-\d{2})\] (.+?) \| why: (.+)$")
RES_RE = re.compile(r"^- D(\d+) \[(\d{4}-\d{2}-\d{2})\] RESOLVED \[(\d{4}-\d{2}-\d{2})\]: (.+?) \| was: (.+)$")
TAG_RE = re.compile(r"^\[(rule|decision|task)\] ")
SUP_RE = re.compile(r"^- D(\d+) \[(\d{4}-\d{2}-\d{2})\] SUPERSEDED by D(\d+) \[(\d{4}-\d{2}-\d{2})\]: (.+?) \| because: (.+)$")


def ledger_enabled():
    return os.environ.get("HANDOFF_LEDGER") != "0"


def ledger_timeout():
    v = (os.environ.get("HANDOFF_LEDGER_TIMEOUT") or "").strip()
    if re.fullmatch(r"\d+", v) and 1 <= int(v) <= 300:
        return int(v)
    return LEDGER_TIMEOUT_DEFAULT


def ledger_model():
    m = os.environ.get("HANDOFF_LEDGER_MODEL") or ""
    return m if re.fullmatch(r"[A-Za-z0-9._-]+", m) else summary_model()


def project_root(cwd):
    """F39.1: git toplevel of cwd (5 s timeout), else cwd itself; None without a cwd."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
    except Exception:
        pass
    return cwd


def project_key(root):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(root.rstrip("/")))[:40]
    return base + "-" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]


def ledger_dir(root):
    return os.path.abspath(os.path.join(HANDOFFS_DIR, "projects", project_key(root)))


def parse_entry(line, cap=True):
    """One ledger line -> dict(id, kind, line, by, tag) or None. kind is act|sup|res; tag (F42.1) is
    rule|decision|task|None for an active entry. Over-long lines are cut with `...` (F39.2)."""
    line = (line or "").rstrip("\r\n").strip()
    m = RES_RE.match(line)
    if m:
        pre = f"- D{m.group(1)} [{m.group(2)}] RESOLVED [{m.group(3)}]: {m.group(4)} | was: "
        was = m.group(5)
        # F44.11(c): RESOLVED lines are exempt from LEDGER_LINE_MAX (never injected); `was:` is kept whole
        return None if int(m.group(1)) < 1 else {"id": int(m.group(1)), "kind": "res", "line": pre + was, "by": None, "tag": None}
    m = SUP_RE.match(line)
    if m:
        pre = f"- D{m.group(1)} [{m.group(2)}] SUPERSEDED by D{m.group(3)} [{m.group(4)}]: "
        dec, sep, why = m.group(5), " | because: ", m.group(6)
        kind, by = "sup", int(m.group(3))
    else:
        m = ACT_RE.match(line)
        if not m:
            return None
        pre = f"- D{m.group(1)} [{m.group(2)}] "
        dec, sep, why = m.group(3), " | why: ", m.group(4)
        kind, by = "act", None
    if int(m.group(1)) < 1:
        return None
    full = pre + dec + sep + why
    if cap and len(full) > LEDGER_LINE_MAX and kind == "act":  # F44.11(c): SUPERSEDED lines are exempt from the cap
        # F39.11: parsed first; cut the why/because text (ending ...), then the decision text; never dropped
        over = len(full) - LEDGER_LINE_MAX
        keep = max(len(why) - over - 3, 0)
        if keep or len(why) - over >= 3:
            why = why[:keep] + "..."
        else:
            over -= len(why) - 3
            why = "..."
            dec = dec[: max(len(dec) - over - 3, 0)] + "..."
        full = pre + dec + sep + why
    tm = TAG_RE.match(dec) if kind == "act" else None
    return {"id": int(m.group(1)), "kind": kind, "line": full, "by": by, "tag": tm.group(1) if tm else None}


def parse_ledger(text):
    out = {}
    for ln in (text or "").splitlines():
        e = parse_entry(ln, cap=False)  # F44.11(c): the cap applies to new model lines only, never to stored entries
        if e and e["id"] not in out:
            out[e["id"]] = e
    return out


def ledger_text(root, entries):
    return f"# Project decisions: {root}\n" + "".join(entries[i]["line"] + "\n" for i in sorted(entries))


def active_lines(entries):
    """F39.4: active entries, newest id first, cut by whole lines to LEDGER_SECTION_CAP chars."""
    act = [entries[i]["line"] for i in sorted(entries, reverse=True) if entries[i]["kind"] == "act"]
    if sum(len(x) + 1 for x in act) <= LEDGER_SECTION_CAP:
        return act
    room = LEDGER_SECTION_CAP - len("- (999 older active decisions in the full ledger)") - 1
    out, used = [], 0
    for x in act:
        if used + len(x) + 1 > room:
            break
        out.append(x)
        used += len(x) + 1
    out.append(f"- ({len(act) - len(out)} older active decisions in the full ledger)")
    return out


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def merge_items(path, since_ms):
    """F39.3 + F40.2 merge input: ([item text], ts string of the newest item sent or None). Items are taken
    OLDEST first up to MERGE_INPUT_CAP chars (whole items), so a backlog is caught up over several
    compactions. Never raises."""
    units, asks, last_asst = [], {}, ""  # unit: (text, ms, ts_s)
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except Exception:
        return [], None
    with fh:
        for line in fh:
            try:
                o = json.loads(line)
                if not isinstance(o, dict) or not isinstance(o.get("message"), dict):
                    continue
                msg, t = o["message"], o.get("type")
                c = msg.get("content")
                ts_s = o.get("timestamp") if isinstance(o.get("timestamp"), str) else None
                ms = ts_ms(ts_s)
                newer = since_ms is None or (ms is not None and ms > since_ms)
                if t == "assistant":
                    tx = text_of(c).strip()
                    if tx:
                        last_asst = tx
                    if isinstance(c, list):
                        for b in c:
                            if (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion"
                                    and isinstance(b.get("id"), str) and isinstance((b.get("input") or {}).get("questions"), list)):
                                asks[b["id"]] = [(q.get("header"), q.get("question")) for q in b["input"]["questions"]
                                                 if isinstance(q, dict) and isinstance(q.get("question"), str)]
                elif t == "user":
                    if o.get("isMeta") or o.get("isCompactSummary"):
                        continue
                    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        tur = o.get("toolUseResult")
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in asks:
                                qs = asks.pop(b["tool_use_id"])
                                if not newer:
                                    continue
                                for _h, q, a, note in answer_pairs(qs, b, tur):
                                    ln = f"ANSWER {ts_s or '?'}: {one_line(q, 200)} -> {one_line(a, 200)}"
                                    if isinstance(note, str) and note.strip():
                                        ln += f" (notes: {one_line(note, 200)})"
                                    units.append((ln, ms, ts_s))
                        continue
                    tx = text_of(c).strip()
                    if genuine(tx):
                        if newer:
                            u = f"USER {ts_s or '?'}: " + trunc(tx, 4000)
                            if last_asst:
                                u = "ASSISTANT: " + last_asst[-1500:] + "\n\n" + u
                            units.append((u, ms, ts_s))
                        last_asst = ""
            except Exception:
                continue
    sent, total, newest = [], 0, None
    for text, ms, ts_s in units:
        add = len(text) + (2 if sent else 0)
        if sent and total + add > MERGE_INPUT_CAP:
            break
        sent.append(text)
        total += add
        if ms is not None and (newest is None or ms >= newest[0]):
            newest = (ms, ts_s)
    if sent and total > MERGE_INPUT_CAP:
        sent = [sent[0][:MERGE_INPUT_CAP]]
    return sent, (newest[1] if newest else None)


SUPERSEDE_RE = re.compile(r"^SUPERSEDE D(\d+) BY D(\d+): (.+)$")
RESOLVE_RE = re.compile(r"^RESOLVE D(\d+): (.+)$")
KIND_RE = re.compile(r"^KIND D(\d+) (rule|decision|task)$")
KIND_LIMIT = 40  # F42.2: KIND lines applied per merge


def apply_changes(prev, out_text, allow_new=True, kind_limit=KIND_LIMIT, stats=None):
    """F40.1 + F42.2: apply the model's ADD / KIND / RESOLVE / SUPERSEDE lines to the previous entries.
    Returns (entries, n_changes); entries is a new dict, `prev` is untouched. Invalid lines are ignored.
    Never removes an entry. allow_new=False (maintenance) ignores new entries; kind_limit None = no limit.
    `stats` (dict) receives counts add/kind/resolved/superseded."""
    res = dict(prev)
    st = {"add": 0, "kind": 0, "resolved": 0, "superseded": 0}
    prev_max = max(prev) if prev else 0
    lines = [(ln or "").strip() for ln in (out_text or "").splitlines()]
    today = datetime.date.today().isoformat()
    if allow_new:
        for ln in lines:
            e = parse_entry(ln)
            if e and e["kind"] == "act" and e["id"] > prev_max and e["id"] not in res:
                res[e["id"]] = e
                st["add"] += 1
    for ln in lines:  # F42.2 KIND: tag an untagged active entry (text otherwise unchanged)
        m = KIND_RE.match(ln)
        if not m or (kind_limit is not None and st["kind"] >= kind_limit):
            continue
        i = int(m.group(1))
        am = ACT_RE.match(res[i]["line"]) if i in res and res[i]["kind"] == "act" and res[i].get("tag") is None else None
        if not am:
            continue
        e = parse_entry(f"- D{i} [{am.group(2)}] [{m.group(2)}] {am.group(3)} | why: {am.group(4)}")
        if e:
            res[i] = e
            st["kind"] += 1
    for ln in lines:  # F42.2 RESOLVE: only an active task or untagged entry; a rule is never resolved
        m = RESOLVE_RE.match(ln)
        if not m:
            continue
        i, why = int(m.group(1)), m.group(2).strip()
        if i not in res or res[i]["kind"] != "act" or res[i].get("tag") not in (None, "task") or not why:
            continue
        pm = re.match(r"^(- D\d+ \[\d{4}-\d{2}-\d{2}\]) (.+)$", res[i]["line"])
        if not pm:
            continue
        e = parse_entry(f"{pm.group(1)} RESOLVED [{today}]: {why} | was: {pm.group(2)}")
        if e:
            res[i] = e
            st["resolved"] += 1
    for ln in lines:
        m = SUPERSEDE_RE.match(ln)
        if not m:
            continue
        old, new, why = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if old == new or old not in res or new not in res or res[old]["kind"] != "act" or not why:
            continue
        am = ACT_RE.match(res[old]["line"])
        if not am:
            continue
        sup = parse_entry(f"- D{old} [{am.group(2)}] SUPERSEDED by D{new} [{today}]: {am.group(3)} | because: {why}")
        if sup:
            res[old] = sup
            st["superseded"] += 1
    if stats is not None:
        stats.update(st)
    return res, sum(st.values())


def prune_backups(ddir):
    try:
        for old in sorted(glob.glob(os.path.join(ddir, "decisions.[0-9]*T*Z.md")))[:-BACKUPS_KEEP]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass


def ledger_counts(entries):
    return (sum(1 for e in entries.values() if e["kind"] == "act"),
            sum(1 for e in entries.values() if e["kind"] == "sup"),
            sum(1 for e in entries.values() if e["kind"] == "res"))


def ledger_error_of(err):
    """Map a run_claude failure reason to the F40.4 ledger_error value."""
    err = str(err)
    if err.startswith("timeout"):
        return "timeout"
    m = re.match(r"claude exited (-?\d+)", err)
    if m:
        return "exit " + m.group(1)
    if err == "claude binary not found":
        return "FileNotFoundError"
    return err or "failed"


def _snap(led, which, entries):
    """F47.a: remember the entry ids (`D<n>`) and the superseded ones of a ledger state in `led`."""
    try:
        led["ids_" + which] = ["D%d" % i for i in sorted(entries)]
        led["sup_" + which] = ["D%d" % i for i in sorted(entries) if entries[i]["kind"] == "sup"]
    except Exception:
        pass


MAX_CURSORS = 200


def clean_cursors(raw):
    """The per-session merge cursors of the state file: {session id: ISO timestamp}. Drops bad entries."""
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def prune_cursors(cursors):
    """Keep the MAX_CURSORS newest cursors (by timestamp); drop the oldest."""
    if len(cursors) <= MAX_CURSORS:
        return cursors
    keep = sorted(cursors.items(), key=lambda kv: ts_ms(kv[1]) or 0, reverse=True)[:MAX_CURSORS]
    return dict(keep)


def ledger_merge(sid, tp, root, led, timeout):
    """Thread body: lock, merge the new conversation into the ledger as a delta, write. Fills `led`
    (status ok|unchanged|failed|locked, restored, error). `timeout` covers the lock wait and the claude
    call together (F40.2). Never raises."""
    t0 = time.monotonic()
    deadline = t0 + timeout
    lock = None
    try:
        if fcntl is None:
            led["status"], led["error"] = "failed", "NoFcntl"
            return
        ddir = ledger_dir(root)
        os.makedirs(ddir, mode=0o700, exist_ok=True)
        lock = open(os.path.join(ddir, "decisions.lock"), "a+")
        wait_until = t0 + min(LOCK_WAIT_S, timeout)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= wait_until:
                    led["status"], led["error"] = "locked", "locked"
                    return
                time.sleep(0.1)
        lpath, spath = os.path.join(ddir, "decisions.md"), os.path.join(ddir, "decisions.state.json")
        prev_text = read_text(lpath)
        prev = parse_ledger(prev_text)
        try:
            st = json.loads(read_text(spath))
            state = st if isinstance(st, dict) else {}
        except Exception:
            state = {}
        cursors = clean_cursors(state.get("cursors"))
        last_ts = cursors.get(sid)
        if last_ts is None and state.get("lastSid") == sid and isinstance(state.get("lastEntryTs"), str):
            last_ts = state["lastEntryTs"]  # old state file: the project value is valid only for its own session
        _snap(led, "before", prev)  # F47.a: evidence for the decisions_safe proof
        _snap(led, "after", prev)
        items, newest = merge_items(tp, ts_ms(last_ts) if last_ts else None)
        if not items:
            led["status"] = "unchanged"
            return
        stdin_text = ("CURRENT LEDGER:\n" + (prev_text.rstrip("\n") if prev_text.strip() else "(empty)")
                      + "\n\nNEW CONVERSATION:\n" + "\n\n".join(items))
        remaining = max(1.0, deadline - time.monotonic())
        out, err = run_claude(stdin_text, LEDGER_SYSTEM, LEDGER_PROMPT, ledger_model(), remaining)
        if err or out is None:
            led["status"], led["error"] = "failed", ledger_error_of(err)
            return
        res, changes = apply_changes(prev, out)
        if changes:
            if prev_text.strip():
                ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                write_atomic(os.path.join(ddir, f"decisions.{ts}.md"), redact(prev_text))
                prune_backups(ddir)
            write_atomic(lpath, redact(ledger_text(root, res)))
        if (newest or last_ts):
            cursors[sid] = newest or last_ts
        cursors = prune_cursors(cursors)
        write_atomic(spath, json.dumps({"v": 1, "root": root, "updatedAt": int(time.time() * 1000), "lastSid": sid,
                                        "lastEntryTs": newest or last_ts, "cursors": cursors}))
        _snap(led, "after", res)
        led["status"] = "ok" if changes else "unchanged"
        led["restored"] = 0
    except BaseException as e:
        led["status"], led["error"] = "failed", type(e).__name__
    finally:
        try:
            if lock is not None:
                lock.close()  # releases the flock
        except Exception:
            pass


MAINTAIN_TIMEOUT = 300  # F42.4


def ledger_maintain(dirarg):
    """F42.4: `--ledger-maintain <dir>`: one maintenance pass over the whole ledger of the repo containing <dir>.
    Asks the merge model ONLY for KIND / RESOLVE / SUPERSEDE lines (no new entries, no text edits), applies them under
    the ledger lock, prints one summary line. Input checks run first, before HANDOFF_LEDGER and before anything is
    created: <dir> not an existing directory -> stderr `ledger-maintain: no such directory: <dir>`, exit 2; no
    decisions.md for its project -> stdout `no ledger for <dir>`, exit 0 (no folder or lock created). Otherwise
    returns normally (exit 0)."""
    if not os.path.isdir(dirarg):
        print(f"ledger-maintain: no such directory: {dirarg}", file=sys.stderr)
        sys.exit(2)
    root = project_root(os.path.abspath(dirarg))
    ddir = ledger_dir(root)
    lpath = os.path.join(ddir, "decisions.md")
    if not os.path.exists(lpath):
        print(f"no ledger for {dirarg}")
        return
    if not ledger_enabled():
        return
    if fcntl is None:
        print("maintain failed: NoFcntl")
        return
    lock = None
    try:
        os.makedirs(ddir, mode=0o700, exist_ok=True)
        lock = open(os.path.join(ddir, "decisions.lock"), "a+")
        t0 = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - t0 >= LOCK_WAIT_S:
                    print("maintain failed: locked")
                    return
                time.sleep(0.1)
        prev_text = read_text(lpath)
        prev = parse_ledger(prev_text)
        before = ledger_counts(prev)[0]
        if not prev:
            print(f"kind=0 resolved=0 superseded=0 active_before=0 active_after=0")
            return
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        write_atomic(os.path.join(ddir, f"decisions.{ts}.md"), redact(prev_text))  # F39.6 backup first
        prune_backups(ddir)
        out, err = run_claude("LEDGER:\n" + prev_text.rstrip("\n"), LEDGER_SYSTEM, MAINT_PROMPT, ledger_model(), MAINTAIN_TIMEOUT)
        if err or out is None:
            print(f"maintain failed: {ledger_error_of(err)}")
            return
        stats = {}
        res, changes = apply_changes(prev, out, allow_new=False, kind_limit=None, stats=stats)
        if changes:
            write_atomic(lpath, redact(ledger_text(root, res)))
        print(f"kind={stats.get('kind', 0)} resolved={stats.get('resolved', 0)} superseded={stats.get('superseded', 0)} "
              f"active_before={before} active_after={ledger_counts(res)[0]}")
    finally:
        try:
            if lock is not None:
                lock.close()
        except Exception:
            pass


def ledger_repair(dirarg):
    """F44.11(d): `--ledger-repair <dir>`: restore RESOLVED/SUPERSEDED lines whose `was:` (or superseded decision)
    text was cut to end in `...`, from the newest timestamped backup decisions.<ts>.md that holds the entry as an
    active line. Takes the ledger lock, writes a backup first (only when something is repaired), prints
    `repaired <n> of <m> cut lines`. <dir> not a directory -> stderr + exit 2; no ledger -> `no ledger for <dir>`."""
    if not os.path.isdir(dirarg):
        print(f"ledger-repair: no such directory: {dirarg}", file=sys.stderr)
        sys.exit(2)
    root = project_root(os.path.abspath(dirarg))
    ddir = ledger_dir(root)
    lpath = os.path.join(ddir, "decisions.md")
    if not os.path.exists(lpath):
        print(f"no ledger for {dirarg}")
        return
    if not ledger_enabled():
        return
    if fcntl is None:
        print("repair failed: NoFcntl")
        return
    lock = None
    try:
        lock = open(os.path.join(ddir, "decisions.lock"), "a+")
        t0 = time.monotonic()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - t0 >= LOCK_WAIT_S:
                    print("repair failed: locked")
                    return
                time.sleep(0.1)
        text = read_text(lpath)
        backups = sorted(glob.glob(os.path.join(ddir, "decisions.[0-9]*T*Z.md")), reverse=True)
        cache = {}

        def original(i, cut_prefix):
            """Full `[tag] text | why: reason` of active entry i from the newest backup that has it (and agrees with the cut text)."""
            for b in backups:
                if b not in cache:
                    d = {}
                    for ln in read_text(b).splitlines():
                        m = ACT_RE.match(ln.strip())
                        if m and int(m.group(1)) not in d:
                            d[int(m.group(1))] = (m.group(3), m.group(4))
                    cache[b] = d
                if i in cache[b]:
                    dec, why = cache[b][i]
                    if (dec + " | why: " + why).startswith(cut_prefix):
                        return dec, why
            return None

        lines = text.split("\n")
        total = fixed = 0
        for n, raw in enumerate(lines):
            ln = raw.rstrip("\r").strip()
            m = RES_RE.match(ln)
            if m and m.group(5).endswith("..."):
                total += 1
                o = original(int(m.group(1)), m.group(5)[:-3])
                if o:
                    lines[n] = f"- D{m.group(1)} [{m.group(2)}] RESOLVED [{m.group(3)}]: {m.group(4)} | was: {o[0]} | why: {o[1]}"
                    fixed += 1
                continue
            m = SUP_RE.match(ln)
            if m and m.group(5).endswith("..."):
                total += 1
                o = original(int(m.group(1)), m.group(5)[:-3])
                if o:
                    lines[n] = (f"- D{m.group(1)} [{m.group(2)}] SUPERSEDED by D{m.group(3)} [{m.group(4)}]: "
                                f"{o[0]} | because: {m.group(6)}")
                    fixed += 1
        if fixed:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            write_atomic(os.path.join(ddir, f"decisions.{ts}.md"), redact(text))  # F39.6 backup first
            prune_backups(ddir)
            write_atomic(lpath, redact("\n".join(lines)))
        print(f"repaired {fixed} of {total} cut lines")
    finally:
        try:
            if lock is not None:
                lock.close()
        except Exception:
            pass
