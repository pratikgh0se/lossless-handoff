# Receipt checks and --prove.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

# ---------------------------------------------------------------- F47 proof receipts, F48 claims register

def _read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    rows.append(o)
    except Exception:
        pass
    return rows


def _norm_ws(s):
    return " ".join(str(s).split())


def _norm_id(x):
    x = str(x).strip()
    return "D" + x if x.isdigit() else x


def _user_text(rec):
    """Plain typed text of a user record (no tool results), or None."""
    msg = rec.get("message")
    if not isinstance(msg, dict) or rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return None


def _plain_messages(path):
    """[(lineno, ts_ms, role, text)] of single-segment user/assistant messages, read independently of recall."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if '"user"' not in line and '"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                t = o.get("type")
                if t == "user":
                    tx = _user_text(o)
                elif t == "assistant":
                    c = (o.get("message") or {}).get("content") if isinstance(o.get("message"), dict) else None
                    if isinstance(c, str):
                        tx = c
                    elif isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c):
                        tx = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    else:
                        tx = None
                else:
                    continue
                if tx is None:
                    continue
                out.append((n, ts_ms(o.get("timestamp")), t, tx))
    except Exception:
        pass
    return out


def _first_cwd(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"cwd"' in line:
                    try:
                        c = json.loads(line).get("cwd")
                    except Exception:
                        continue
                    if isinstance(c, str) and c:
                        return c
    except Exception:
        pass
    return None


def _recall_run(args, timeout=30):
    import subprocess
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.stdout or ""


def check_lossless(rc):
    rp = recall_path()
    if not rp:
        return "skip", "recall.py absent"
    tp = rc.get("transcript")
    if not isinstance(tp, str) or not os.path.isfile(tp):
        return "skip", "transcript gone"
    bms = ts_ms(rc.get("boundary_ts"))
    if bms is None:
        return "skip", "no boundary"
    pool = [m for m in _plain_messages(tp)
            if m[1] is not None and m[1] < bms and len(m[3]) >= 30 and m[3] == m[3].strip()
            and "<system-reminder" not in m[3] and not m[3].startswith("<") and "[redacted]" not in m[3] and "[REDACTED:" not in m[3]]
    if len(pool) < 5:
        return "skip", "fewer than 5 messages"
    import random
    import subprocess
    rnd = random.Random(hashlib.sha256((str(rc.get("ts")) + str(rc.get("key"))).encode("utf-8")).hexdigest())
    order = list(range(len(pool)))
    rnd.shuffle(order)
    sub = bool(rc.get("agent_id"))
    stem = os.path.basename(tp)[:-len(".jsonl")]
    if sub and stem.startswith("agent-"):
        stem = stem[len("agent-"):]
    sid8 = stem[:8]
    cwd = _first_cwd(tp) or os.getcwd()
    passes = 0
    for i in order:
        n, _t, role, text = pool[i]
        phrase = text[:60].split("\n")[0]
        if len(phrase.strip()) < 10:
            continue
        try:
            sargs = [rp, "search", phrase, "--fixed", "--keyword", "--limit", "100", "--cwd", cwd] + (["--subagents"] if sub else [])
            hits = {}
            for ln in _recall_run(sargs).splitlines():
                # F57.b labels: <session8>:<line> (main) or <parent8>/<agent8>:<line> (subagent)
                m = re.match(r"^(?:([A-Za-z0-9_-]+)/)?([A-Za-z0-9_-]+):(\d+) \d{4}-", ln)
                if m and m.group(2) == sid8 and bool(m.group(1)) == sub:
                    hits[int(m.group(3))] = (m.group(1) + "/" if m.group(1) else "") + m.group(2)
            if not hits:
                return "fail", f"line {n}: recall found no hit for the message"
            line = n if n in hits else next(iter(hits))
            out = _recall_run([rp, "open", f"{hits[line]}:{line}", "--full", "--around", "0", "--cwd", cwd])
            lines = out.split("\n")
            if not lines or not lines[0].startswith("--- "):
                return "fail", f"line {n}: recall open printed no message"
            body = "\n".join(lines[1:])
            body = body[:-1] if body.endswith("\n") else body
            if body != text:
                if "[redacted]" in body or "[REDACTED:" in body:
                    continue  # recall hides secrets; that message cannot be compared byte for byte
                return "fail", f"line {n}: recall printed different text ({len(body)} vs {len(text)} chars)"
            passes += 1
            if passes >= 5:
                return "pass", "5 of 5 identical"
        except subprocess.TimeoutExpired:
            return "skip", "timeout"
        except Exception as e:
            return "fail", f"line {n}: recall error {type(e).__name__}"
    return "skip", "fewer than 5 messages could be compared"


def _handoff_file_for(rc):
    key, want = rc.get("key"), rc.get("handoff_sha256")
    if not key or not want:
        return None
    for p in glob.glob(os.path.join(HANDOFFS_DIR, f"{key}-*.md")):
        b = os.path.basename(p)
        if not (b.endswith("-latest.md") or re.search(r"-\d{8}T\d{6}Z(-prev)?\.md$", b)):
            continue
        try:
            with open(p, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == want:
                    return p
        except Exception:
            continue
    return None


def check_right_agent(rc):
    hp = _handoff_file_for(rc)
    if not hp:
        return "skip", "handoff file no longer exists"
    tp = rc.get("transcript")
    if not isinstance(tp, str) or not os.path.isfile(tp):
        return "skip", "transcript gone"
    try:
        text = open(hp, encoding="utf-8", errors="replace").read()
    except Exception:
        return "skip", "handoff file unreadable"
    m = re.search(r"(?ms)^## Last user messages[^\n]*\n(.*?)(?=^## |\Z)", text)
    if not m:
        return "skip", "no user messages section"
    quoted = [ln[2:].strip() for ln in m.group(1).splitlines() if ln.startswith("- ")]
    if not quoted:
        return "skip", "no user messages section"
    users = []
    for _n, _t, role, tx in _plain_messages(tp):
        if role == "user":
            users.append(_norm_ws(tx))
    for q in quoted:
        nq = _norm_ws(q)
        if nq.endswith("..."):
            nq = nq[:-3].rstrip()
        if not nq:
            continue
        if not any(nq in u for u in users):
            return "fail", "not in own transcript: " + nq[:80]
    return "pass", f"{len(quoted)} lines found"


def _strings(o, out, depth=0):
    if depth > 8:
        return
    if isinstance(o, str):
        out.append(o)
        if o.startswith("{"):
            try:
                _strings(json.loads(o), out, depth + 1)
            except Exception:
                pass
    elif isinstance(o, dict):
        for v in o.values():
            _strings(v, out, depth + 1)
    elif isinstance(o, list):
        for v in o:
            _strings(v, out, depth + 1)


def check_delivered(rc):
    tp, bts, head = rc.get("transcript"), rc.get("boundary_ts"), rc.get("part1_head")
    if not isinstance(tp, str) or not os.path.isfile(tp):
        return "skip", "transcript gone"
    if not bts or not isinstance(head, str) or not head:
        return "skip", "no boundary"
    want = _norm_ws(head)
    seen_boundary, after, found = False, 0, False
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return "skip", "transcript unreadable"
    last = -1
    for i, line in enumerate(lines):
        if "compact_boundary" in line:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if is_boundary(o) and o.get("timestamp") == bts:
                last = i
    if last < 0:
        return "skip", "boundary not in transcript"
    for line in lines[last + 1:]:
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not isinstance(o, dict):
            continue
        after += 1
        if o.get("type") == "attachment" or "attachment" in o:
            ss = []
            _strings(o.get("attachment"), ss)
            if any(want in _norm_ws(s) for s in ss):
                found = True
                break
    if found:
        return "pass", "found after the boundary"
    if after == 0:
        return "skip", "no record after the boundary yet"
    return "fail", "no hook context after the boundary contains the part-1 head"


def check_decisions_safe(rc, health):
    sid, aid, rts = rc.get("sid"), rc.get("agent_id") or None, _iso_epoch(rc.get("ts"))
    best = None
    for r in health:
        if "ledger_ids_before" not in r or r.get("sid") != sid or (r.get("agent_id") or None) != aid:
            continue
        e = _iso_epoch(r.get("ts"))
        if e is None or (rts is not None and not (rts - 900 <= e <= rts)):
            continue
        if best is None or e >= best[0]:
            best = (e, r)
    if best is None:
        return "skip", "no ledger merge"
    r = best[1]
    before = [_norm_id(x) for x in r.get("ledger_ids_before") or []]
    after = {_norm_id(x) for x in r.get("ledger_ids_after") or []}
    sup_b = [_norm_id(x) for x in r.get("superseded_before") or []]
    sup_a = {_norm_id(x) for x in r.get("superseded_after") or []}
    lost = [x for x in before if x not in after]
    if lost:
        return "fail", "lost " + ", ".join(lost)
    back = [x for x in sup_b if x in after and x not in sup_a]
    if back:
        return "fail", ", ".join(back) + " active again"
    return "pass", f"{len(before)} ids kept"


CHECKS = ["lossless", "right_agent", "delivered", "decisions_safe"]


def _rkey(r):
    return (str(r.get("ts")), str(r.get("sid")), str(r.get("agent_id") or ""))


def resolve_boundary(rc):
    """F47 ruling: the receipt's boundary_ts is only a hint (SessionStart runs before Claude Code flushes the current
    compact_boundary). The boundary is the latest compact_boundary in the receipt's transcript whose timestamp is at
    most the receipt ts plus 10 s; None when there is none."""
    tp = rc.get("transcript")
    limit = (_iso_epoch(rc.get("ts")) or 0) * 1000.0 + 10000
    best, best_ms = None, None
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "compact_boundary" not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not is_boundary(o):
                    continue
                t = ts_ms(o.get("timestamp"))
                if t is not None and t <= limit and (best_ms is None or t >= best_ms):
                    best, best_ms = o.get("timestamp"), t
    except Exception:
        return None
    return best


def _load_recall():
    """Import recall.py (next to this hook, else ~/.claude/hooks); None if it is not there."""
    for q in _hook_candidates("recall.py", os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py"),
                              os.path.join(os.path.expanduser("~"), ".claude", "hooks", "recall.py")):
        if os.path.isfile(q):
            import importlib.util
            spec = importlib.util.spec_from_file_location("lh_recall", q)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def nodes_check(receipts, start):  # receipts are not used: the scope is every transcript with a boundary in the window
    """F54.e: after a sync, every compact_boundary has exactly one leaf; per transcript the leaf ranges start at line 1,
    do not overlap and leave no gap. -> (the printed line, number of bad)."""
    b = n = bad = 0
    try:
        rm = _load_recall()
        if rm is None:
            return "nodes: 0 boundaries, 0 leaves, 0 bad", 0
        targets = {}  # realpath(transcript) -> ledger dir
        pd = os.path.join(os.path.expanduser("~"), ".claude", "projects")
        cands = glob.glob(os.path.join(pd, "*", "*.jsonl")) + glob.glob(os.path.join(pd, "*", "*", "subagents", "*.jsonl"))
        for tp in cands:
            try:
                if os.path.getmtime(tp) < start:
                    continue  # no record in the window can be in a file older than the window
                found, _eo, _el, tail = rm.scan_boundaries(tp)
                recent = False
                for _ln, o in [(x[0], x[1]) for x in found] + ([tail] if tail else []):
                    try:
                        te = datetime.datetime.fromisoformat(str(o.get("timestamp")).replace("Z", "+00:00")).timestamp()
                    except Exception:
                        te = 0
                    if te >= start:
                        recent = True
                        break
                if not recent:
                    continue
                cwd = rm.first_cwd(tp)
                if cwd:
                    targets[os.path.realpath(tp)] = rm.ledger_dir_for(rm.project_root(cwd))
            except Exception as e:
                log_error("unknown", e)
        for tp, ldir in sorted(targets.items()):
            try:
                rm.sync_transcript(ldir, tp, 5.0)
            except Exception as e:
                log_error("unknown", e)
            found, _eo, _el, tail = rm.scan_boundaries(tp)
            bl = [(ln, o) for ln, o, _ in found] + ([tail] if tail else [])
            session, agent = rm.transcript_names(tp)
            ids = [rm.node_id(session, o.get("uuid") if isinstance(o.get("uuid"), str) and o.get("uuid") else f"line-{ln}")
                   for ln, o in bl]
            leaves = [x for x in rm.read_nodes(ldir) if os.path.realpath(x["transcript"]) == tp]
            b += len(bl)
            n += len(leaves)
            byid = {}
            for x in leaves:
                byid.setdefault(x["id"], []).append(x)
            for i, (ln, _o) in enumerate(bl):
                if len(byid.get(ids[i], [])) != 1:
                    bad += 1
            ordered = sorted(leaves, key=lambda x: x["first_line"])
            expect = 1
            for x in ordered:
                if x["first_line"] != expect or x["last_line"] < x["first_line"] - 1:
                    bad += 1
                expect = x["last_line"] + 2
            if bl and ordered and ordered[-1]["last_line"] + 1 != bl[-1][0]:
                bad += 1
    except Exception as e:
        log_error("unknown", e)
    return f"nodes: {b} boundaries, {n} leaves, {bad} bad", bad


def excerpt_cut_line(health, start):
    """F56.b: information only, from health rows that have the excerpt fields."""
    rows = [r for r in health if isinstance(r, dict) and "excerpt_dropped" in r and (_iso_epoch(r.get("ts")) or 0) >= start
            and isinstance(r.get("excerpt_dropped"), int)]
    cut = sorted(r["excerpt_dropped"] for r in rows if r["excerpt_dropped"] > 0)
    if cut:
        m = len(cut) // 2
        med = cut[m] if len(cut) % 2 else (cut[m - 1] + cut[m]) // 2
    else:
        med = 0
    return f"excerpt cut: {len(cut)} of {len(rows)} compactions, median dropped {med} chars"


def prove(days=7):
    """F47.b: verdicts for receipts in the window, appended to proofs.jsonl; prints the summary line; writes
    proof-report.md and (F48) claims-status.json / claims-report.md. Never raises."""
    now = time.time()
    start = now - days * 86400
    run_ts = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipts = [r for r in _read_jsonl(os.path.join(HANDOFFS_DIR, "receipts.jsonl"))
                if (_iso_epoch(r.get("ts")) or 0) >= start]
    pfile = os.path.join(HANDOFFS_DIR, "proofs.jsonl")
    proofs = _read_jsonl(pfile)
    have = {(str(p.get("receipt_ts")), str(p.get("sid")), str(p.get("agent_id") or "")): p for p in proofs}
    health = [r for r in _read_jsonl(HEALTH_FILE) if r.get("event") != "subagent_start"]  # F58.d
    new_rows = []
    dsids = drift_sids()  # F63: a session with format drift never passes
    for rc in receipts:
        k = _rkey(rc)
        if k in have:
            continue
        checks, details = {}, {}
        rc = dict(rc, boundary_ts=resolve_boundary(rc))
        for name, fn in (("lossless", check_lossless), ("right_agent", check_right_agent),
                         ("delivered", check_delivered), ("decisions_safe", lambda r: check_decisions_safe(r, health))):
            try:
                v, why = fn(rc)
            except Exception as e:
                v, why = "skip", f"check error {type(e).__name__}"
                log_error(rc.get("key") or "unknown", e)
            checks[name], details[name] = v, why[:200]
        if str(rc.get("sid"))[:8] in dsids and not rc.get("agent_id"):
            for name in CHECKS:  # F63.c: the file records the failure, not a skip
                checks[name], details[name] = "fail", "format_drift"
        row = {"ts": run_ts, "receipt_ts": rc.get("ts"), "sid": rc.get("sid"), "agent_id": rc.get("agent_id"),
               "boundary_ts": rc.get("boundary_ts"), "checks": checks, "details": details}
        new_rows.append(row)
        have[k] = row
    if new_rows:
        try:
            os.makedirs(HANDOFFS_DIR, exist_ok=True)
            with open(pfile, "a", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in new_rows))
        except Exception as e:
            log_error("unknown", e)
    tally = {c: [0, 0] for c in CHECKS}  # pass, fail
    fails, skips = [], {}
    for rc in receipts:
        p = have.get(_rkey(rc))
        if not p:
            continue
        drifted = str(rc.get("sid"))[:8] in dsids and not rc.get("agent_id")
        for c in CHECKS:
            v = (p.get("checks") or {}).get(c)
            why = str((p.get("details") or {}).get(c) or "")
            if drifted:
                v, why = "fail", "format_drift"
            if v == "pass":
                tally[c][0] += 1
            elif v == "fail":
                tally[c][1] += 1
                fails.append(f"failed {c} {str(p.get('sid'))[:8]} {p.get('agent_id') or '-'}: {why}")
            elif v == "skip":
                skips[(c, why)] = skips.get((c, why), 0) + 1

    def pq(c):
        return f"{tally[c][0]}/{tally[c][0] + tally[c][1]}"
    line = (f"proof {days}d: receipts {len(receipts)} · lossless {pq('lossless')} · right-agent {pq('right_agent')} "
            f"· delivered {pq('delivered')} · decisions-safe {pq('decisions_safe')} · failed {len(fails)}")
    nodes_line, nbad = nodes_check(receipts, start)
    if nbad > 0:
        fails.append(f"failed nodes: {nbad} bad (see {nodes_line})")
        line = re.sub(r"failed \d+$", f"failed {len(fails)}", line)
    _before = list(_OUTLOG)  # reconcile's lines when run together
    _out(line)
    _out(nodes_line)
    try:
        _out(excerpt_cut_line(health, start))
    except Exception as e:
        log_error("unknown", e)
    try:
        rep_lines = [line, ""] + _before + ([""] if _before else [])
        rep_lines += fails + ([""] if fails else [])
        rep_lines += [f"skipped {c}: {why or 'n/a'} x{n}" for (c, why), n in sorted(skips.items())]
        write_atomic(os.path.join(HANDOFFS_DIR, "proof-report.md"), "\n".join(rep_lines) + "\n")
    except Exception as e:
        log_error("unknown", e)
    try:
        st = eval_claims(now)
        if st is not None:
            write_atomic(os.path.join(HANDOFFS_DIR, "claims-status.json"),
                         json.dumps({"ts": run_ts, "claims": [{k: c[k] for k in ("id", "value", "rate", "as_of", "status")} for c in st]}))
            write_atomic(os.path.join(HANDOFFS_DIR, "claims-report.md"), claims_table(st))
    except Exception as e:
        log_error("unknown", e)
