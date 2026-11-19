# Format drift scan and --reconcile.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

def _iso_ms(ms):
    return datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_epoch(v):
    try:
        return datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return None


def _scan_transcript(path, start_ms, now):
    """(boundaries in window, latest boundary ms, first entrypoint) for one transcript; never raises."""
    n, last, ent = 0, None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                has_b = "compact_boundary" in line
                has_e = ent is None and '"entrypoint"' in line
                if not (has_b or has_e):
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                if ent is None and isinstance(o.get("entrypoint"), str):
                    ent = o["entrypoint"]
                if has_b and is_boundary(o):
                    t = ts_ms(o.get("timestamp"))
                    if t is not None and t >= start_ms and t <= now * 1000.0 + 60000:
                        n += 1
                        last = t if last is None or t > last else last
    except Exception:
        pass
    return n, last, ent


def _count_boundaries_all(path, start_ms=None):
    """compact_boundary records in one transcript with a timestamp at or after start_ms (a record with no readable
    timestamp always counts; start_ms None counts all). None if unreadable."""
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "compact_boundary" not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if is_boundary(o):
                    t = ts_ms(o.get("timestamp"))
                    if start_ms is None or t is None or t >= start_ms:
                        n += 1
    except Exception:
        return None
    return n


def format_drift_scan(start_ms, canary_sids, base):
    """F63: sessions with >= 1 recorded post-compaction inject event (a main-session row in receipts.jsonl, written by
    postcompact-inject.py when it emits handoff part 1) in the window and 0
    compact_boundary records since the window start in the session's transcript. Returns a list of {sid, injects, boundaries}. A session whose transcript is gone is skipped."""
    inj = {}
    for r in _read_jsonl(os.path.join(HANDOFFS_DIR, "receipts.jsonl")):
        e = _iso_epoch(r.get("ts"))
        if e is None or e * 1000.0 < start_ms or r.get("agent_id"):
            continue
        sid8 = str(r.get("sid") or "")[:8]
        if not sid8 or sid8 in canary_sids:
            continue
        tp = r.get("transcript") if isinstance(r.get("transcript"), str) else ""
        d = inj.setdefault(sid8, [0, ""])
        d[0] += 1
        if tp and os.path.isfile(tp):
            d[1] = tp
    out = []
    for sid8, (n, tp) in sorted(inj.items()):
        if not tp:
            g = [x for x in glob.glob(os.path.join(base, "*", sid8 + "*.jsonl")) if "handoff-canary" not in x]
            tp = g[0] if g else ""
        if not tp:
            continue
        b = _count_boundaries_all(tp, start_ms)
        if b == 0:
            out.append({"sid": sid8, "injects": n, "boundaries": 0})
    return out


def drift_sids():
    """Session ids that the newest reconcile.json lists in format_drift_sessions (empty when no drift)."""
    try:
        rj = json.load(open(os.path.join(HANDOFFS_DIR, "reconcile.json"), encoding="utf-8"))
        if isinstance(rj, dict) and rj.get("format_drift") is True:
            return {str(x.get("sid")) for x in rj.get("format_drift_sessions") or [] if isinstance(x, dict)}
    except Exception:
        pass
    return set()


def reconcile(days=7):
    """F45.c/F46.d: count compact_boundary records in main transcripts (and, per agent, in
    <project>/<session>/subagents/agent-*.jsonl) against health.jsonl rows; print the summary line, the subagent line and
    one line per missed session; write <HANDOFFS_DIR>/reconcile.json. Never raises; always exit 0."""
    now = time.time()
    start_ms = (now - days * 86400) * 1000.0
    rows = {}  # sid8 -> [(epoch, trigger, agent_id or "")]
    canary_sids = set()
    try:
        with open(HEALTH_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("event") == "subagent_start":
                    continue  # F58.d: SubagentStart rows are not compactions
                if isinstance(r, dict) and r.get("canary_child"):
                    canary_sids.add(str(r.get("sid") or ""))  # F45.c: canary sessions are excluded from every count
                if not isinstance(r, dict) or r.get("canary_child") or r.get("trigger") == "canary":
                    continue
                e = _iso_epoch(r.get("ts"))
                if e is None:
                    continue
                aid = r.get("agent_id")
                rows.setdefault(str(r.get("sid") or ""), []).append(
                    (e, r.get("trigger"), aid if isinstance(aid, str) else ""))
    except Exception:
        pass
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")

    def counts(sid8, aid, n):
        rs = [(e, tr) for e, tr, a in rows.get(sid8, []) if a == aid and e * 1000.0 >= start_ms - 86400000]
        h = sum(1 for _, tr in rs if tr in ("auto", "manual"))
        rc = sum(1 for _, tr in rs if tr == "recovered")
        return h, rc, max(0, n - h - rc)
    sessions, subs = [], []
    for path in sorted(glob.glob(os.path.join(base, "*", "*.jsonl"))):
        try:
            if os.path.getmtime(path) * 1000.0 < start_ms:
                continue
            if "handoff-canary" in os.path.basename(os.path.dirname(path)):
                continue
            n, last, ent = _scan_transcript(path, start_ms, now)
            if n == 0:
                continue
            sid8 = os.path.basename(path)[:-len(".jsonl")][:8]
            if sid8 in canary_sids:
                continue
            h, rc, m = counts(sid8, "", n)
            sessions.append({"sid": sid8, "project": os.path.basename(os.path.dirname(path)),
                             "headless": bool(ent and ent.startswith("sdk")), "boundaries": n, "handoffs": h,
                             "recovered": rc, "missed": m, "last_boundary": _iso_ms(last)})
        except Exception:
            continue
    for path in sorted(glob.glob(os.path.join(base, "*", "*", "subagents", "agent-*.jsonl"))):
        try:
            if os.path.getmtime(path) * 1000.0 < start_ms:
                continue
            proj = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
            if "handoff-canary" in proj:
                continue
            n, last, ent = _scan_transcript(path, start_ms, now)
            if n == 0:
                continue
            sid8 = os.path.basename(os.path.dirname(os.path.dirname(path)))[:8]
            if sid8 in canary_sids:
                continue
            aid = os.path.basename(path)[len("agent-"):-len(".jsonl")]
            h, rc, m = counts(sid8, aid, n)
            subs.append({"sid": sid8, "agent_id": aid, "project": proj, "boundaries": n, "handoffs": h,
                         "recovered": rc, "missed": m, "last_boundary": _iso_ms(last)})
        except Exception:
            continue
    killed = 0
    for pf in glob.glob(os.path.join(HANDOFFS_DIR, "*-pending.json")):
        try:
            stem = os.path.basename(pf)[:-len("-pending.json")]
            aid = stem.split("-a-", 1)[1] if "-a-" in stem else ""
            sid8 = (stem.split("-a-", 1)[0] if aid else stem)[:8]
            if sid8 in canary_sids:
                continue
            pts = None
            try:
                with open(pf, encoding="utf-8") as f:
                    pts = _iso_epoch((json.load(f) or {}).get("ts"))
            except Exception:
                pass
            if pts is None:
                pts = os.path.getmtime(pf)
            if now - pts > 200 and pts * 1000.0 >= start_ms and \
                    not any(e >= pts for e, _, a in rows.get(sid8, []) if a == aid):
                killed += 1
        except Exception:
            continue
    try:
        drift = format_drift_scan(start_ms, canary_sids, base)
    except Exception as e:
        log_error("unknown", e)
        drift = []
    tot = {k: sum(s[k] for s in sessions) for k in ("boundaries", "handoffs", "recovered", "missed")}
    mi = sum(s["missed"] for s in sessions if not s["headless"])
    mh = sum(s["missed"] for s in sessions if s["headless"])
    sub_n, sub_m = sum(s["boundaries"] for s in subs), sum(s["missed"] for s in subs)
    _out(f"compactions {tot['boundaries']} · handoffs {tot['handoffs']} · recovered {tot['recovered']} · "
         f"missed {tot['missed']} (interactive {mi}, headless {mh}) · killed {killed}")
    _out(f"subagent compactions {sub_n} · missed {sub_m}")
    if drift:
        _out("format drift: " + ", ".join(f"{x['sid']} (inject {x['injects']}, boundary {x['boundaries']})" for x in drift)
             + " - the transcript has no compact_boundary record; Claude Code may have changed its transcript format")
    for s in sorted([s for s in sessions if s["missed"] and not s["headless"]], key=lambda s: s["last_boundary"], reverse=True):
        _out(f"missed {s['missed']} {s['sid']} {s['project']} {s['last_boundary']}")
    for s in sorted([s for s in subs if s["missed"]], key=lambda s: s["last_boundary"], reverse=True):
        _out(f"missed {s['missed']} {s['sid']} agent {s['agent_id']} {s['project']} {s['last_boundary']}")
    try:
        os.makedirs(HANDOFFS_DIR, exist_ok=True)
        rp = os.path.join(HANDOFFS_DIR, "reconcile.json")
        out = {"ts": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "days": days, "compactions": tot["boundaries"], "handoffs": tot["handoffs"],
               "recovered": tot["recovered"], "missed": tot["missed"], "missed_interactive": mi,
               "missed_headless": mh, "killed": killed, "subagent_compactions": sub_n, "subagent_missed": sub_m,
               "sessions": sessions, "subagent_sessions": subs,
               "format_drift": bool(drift), "format_drift_sessions": drift}
        if drift:
            out["format_drift_reason"] = ("post-compaction inject events recorded but no compact_boundary record in the "
                                          "transcript: the transcript format may have changed")
        try:
            with open(rp, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("noticed_ts"):
                out["noticed_ts"] = old["noticed_ts"]
        except Exception:
            pass
        write_atomic(rp, json.dumps(out))
    except Exception as e:
        log_error("unknown", e)
