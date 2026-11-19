# The main PreCompact pipeline, the health writer and --recover.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

def steer_summary(d):
    """F53: Claude Code uses PreCompact stdout as instructions for its own summary. Print the guidance once,
    flushed, before any model call. Claude Code adds this to any /compact text, so print it for every event (F53.b). Print nothing if HANDOFF_COMPACT_STEER=0."""
    try:
        if any(a.startswith("--") for a in sys.argv[1:]):  # a CLI mode, not a PreCompact event
            return
        if (os.environ.get("HANDOFF_COMPACT_STEER") or "").strip() == "0":
            _STEER[0] = "off"
            return
        sys.stdout.buffer.write(STEER_TEXT.encode("utf-8"))
        sys.stdout.buffer.flush()
        _STEER[0] = "printed"
    except Exception:
        pass


def main():
    try:
        d = json.load(sys.stdin)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    steer_summary(d)
    sid_raw = d.get("session_id")
    sid = str(sid_raw or "unknown").replace("/", "_")
    av = agent_view(d)
    if av is not None:  # F46.b: a subagent compaction runs the full pipeline over its OWN transcript, keyed by <sid>-a-<id>
        d, sid, found = av
        if not found:
            try:
                os.makedirs(HANDOFFS_DIR, exist_ok=True)
            except Exception:
                pass
            sub = {"agent_file": "absent", "stale_sections": [], "fallback": "skipped",
                   "errors": ["agent_transcript_missing"], "truncated": False, "ledger": "skipped"}
            write_health(d, sid, sub, "", status_override="degraded", reasons_override=["agent_transcript_missing"])
            return
    elif is_subagent(d):  # F42.5 (kept for inputs that mark a subagent only by a /subagents/ transcript path): health line only; no dump, no latest, no ledger merge, no sidecar, no cleanup
        try:
            os.makedirs(HANDOFFS_DIR, exist_ok=True)
        except Exception:
            pass
        sub = {"agent_file": "absent", "stale_sections": [], "fallback": "skipped", "errors": [], "truncated": False,
               "ledger": "skipped"}
        write_health(d, sid, sub, "", status_override="subagent", sidecar=False)
        return
    info = {"agent_file": "absent", "stale_sections": [], "fallback": "skipped", "errors": [], "truncated": False,
            "ledger": "none" if ledger_enabled() else "disabled", "ledger_active": 0, "ledger_superseded": 0,
            "ledger_resolved": 0, "ledger_restored": 0, "ledger_chars": 0, "ledger_error": ""}
    if av is not None:
        info["no_ledger"] = True
        info["ledger"] = "skipped"
    body = ""
    try:
        try:
            os.makedirs(HANDOFFS_DIR, exist_ok=True)
        except Exception:
            pass
        try:  # F45.a: pending marker first, then the deterministic handoff, both before any model call
            write_pending(d, sid)
        except BaseException as e:
            if not isinstance(e, KeyboardInterrupt):
                log_error(sid, e)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = os.path.join(HANDOFFS_DIR, f"{sid}-{ts}.md")
        try:
            early = build(d, {"agent_file": "absent", "stale_sections": [], "fallback": "skipped", "errors": [],
                              "truncated": False, "ledger": "skipped"}, mode="quick")
            write_handoff_files(sid, p, early)
        except Exception:
            pass  # the main build below logs a failing build once (exactly one hooks-error line per failed run)
        try:
            body = build(d, info)
        except BaseException as e:
            info["errors"].append("toplevel")
            info["hard_error"] = True  # F42.6: health status "error"
            log_error(sid, e)
            body = minimal_handoff(d, sid, type(e).__name__)
        try:
            write_handoff_files(sid, p, body)
        except Exception:
            body = ""
    finally:
        try:
            write_health(d, sid, info, body)
        except BaseException:
            pass
    if body and av is None:
        start_indexer(d, sid)
    try:
        keep_days = 365  # F52.d: handoff .md files live 365 days by default; 0 keeps them forever
        raw = (os.environ.get("HANDOFF_RETENTION_DAYS") or "").strip()
        if re.fullmatch(r"[0-9]+", raw):
            keep_days = int(raw)
        now_t = time.time()
        for pat, days in (("*.md", keep_days), ("*-degraded.json", 14), ("*-last-health.json", 14),
                          ("*-pending.json", 14)):
            if days == 0:
                continue
            cutoff = now_t - days * 86400
            for old in glob.glob(os.path.join(HANDOFFS_DIR, pat)):
                try:
                    if os.path.isfile(old) and os.path.getmtime(old) < cutoff:
                        os.remove(old)
                except Exception:
                    pass
    except Exception:
        pass


def write_health(d, sid, info, body, status_override=None, sidecar=True, reasons_override=None):
    status, reasons = compute_status(info)
    if reasons_override is not None:
        reasons = list(reasons_override)
    if status_override:
        status = status_override
    elif info.get("hard_error"):
        status = "error"  # F42.6; the sidecar stays degraded so the inject hook still flags it
    trig = d.get("trigger")
    line = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sid": str(d.get("_parent_sid") or sid)[:8] if d.get("session_id") else "unknown",
        "trigger": trig if trig in ("auto", "manual", "recovered") else "unknown",
        "agent_file": info["agent_file"],
        "stale_sections": info["stale_sections"],
        "fallback": info["fallback"],
        "summary_model": info.get("summary_model", ""),
        "ledger": info.get("ledger", "none"),
        "ledger_active": info.get("ledger_active", 0),
        "ledger_superseded": info.get("ledger_superseded", 0),
        "ledger_resolved": info.get("ledger_resolved", 0),
        "ledger_restored": info.get("ledger_restored", 0),
        "ledger_chars": info.get("ledger_chars", 0),
        "ledger_error": info.get("ledger_error", ""),
        "duration_ms": int((time.monotonic() - _T0) * 1000),
        "output_chars": len(body),
        "truncated": bool(info["truncated"]),
        "inline_cut": len(INJECT_PREFIX) + len(body.strip()) >= 10000,  # F37.4 prediction
        "errors": info["errors"],
        "status": status,
        "hook_version": HOOK_VERSION,
        "excerpt_chars": _EXCERPT["chars"],
        "excerpt_dropped": _EXCERPT["dropped"],
        "canary_child": bool(os.environ.get("HANDOFF_CANARY")),
    }
    if _STEER[0]:
        line["steer"] = _STEER[0]
    try:  # F62: the window and its source that the nudge hook chose (absent when no nudge meta exists)
        with open(os.path.join(HANDOFFS_DIR, f"{sid}-agent.meta.json"), encoding="utf-8") as _mf:
            _m = json.load(_mf)
        if isinstance(_m, dict) and isinstance(_m.get("window"), int) and _m.get("windowSource") in ("env", "learned", "default"):
            line["window"] = _m["window"]
            line["window_source"] = _m["windowSource"]
    except Exception:
        pass
    if isinstance(info.get("ledger_ids"), dict):
        line.update(info["ledger_ids"])
    if agent_id_of(d):  # F46.b
        line["agent_id"] = agent_id_of(d)
        line["agent_type"] = d.get("agent_type") if isinstance(d.get("agent_type"), str) else ""
    if reasons_override is not None:
        line["reasons"] = list(reasons_override)
    try:  # F45.a: the run finished (or failed visibly): the pending marker has done its job
        if status_override == "subagent":
            raise OSError("a subagent run never owns the parent's marker")
        os.unlink(os.path.join(HANDOFFS_DIR, f"{sid}-pending.json"))
    except Exception:
        pass
    try:
        side = dict(line)
        side["reasons"] = reasons
        side["degraded"] = status in ("degraded", "error")
        if not sidecar:
            raise OSError("no sidecar")
        write_atomic(os.path.join(HANDOFFS_DIR, f"{sid}-last-health.json"), json.dumps(side))
    except Exception:
        pass
    try:
        if not d.get("session_id") and not (os.environ.get("HANDOFF_STATE_DIR") or "").strip() and sys.stdin.isatty():
            return  # F42.6: a manual run with no session id never touches the live log
        append_health(json.dumps(line))
    except Exception:
        pass


_OUTLOG = []


def _out(text):
    _OUTLOG.append(text)
    sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))
    sys.stdout.buffer.flush()


def recover():
    """F45.b: `--recover` reads the SessionStart JSON on stdin, rebuilds the handoff from the transcript records before
    the last compact_boundary (no model call, no ledger merge), writes the handoff and latest files (an older latest
    is kept as an archive copy) and appends a health line with trigger and status `recovered`. Always exits 0; prints
    one JSON line {"ok": true} or {"ok": false, "error": ...}. It does not log: the caller logs once."""
    try:
        try:
            d = json.load(sys.stdin)
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        sid = str(d.get("session_id") or "unknown").replace("/", "_")
        av = agent_view(d)
        if av is not None:  # F46.c: recovery for a subagent reads its own transcript and writes <key> files
            d, sid, _found = av
        tp = d.get("transcript_path")
        if not isinstance(tp, str) or not os.path.isfile(tp):
            _out(json.dumps({"ok": False, "error": f"transcript not found: {str(tp)[:200]}"}))
            return
        d = dict(d)
        d["trigger"] = "recovered"
        if av is not None:
            d["agent_id"], d["agent_type"] = av[0].get("agent_id"), av[0].get("agent_type")
        os.makedirs(HANDOFFS_DIR, exist_ok=True)
        info = {"agent_file": "absent", "stale_sections": [], "fallback": "skipped", "errors": [], "truncated": False,
                "ledger": "skipped"}
        body = build(d, info, mode="recover")
        latest = os.path.join(HANDOFFS_DIR, f"{sid}-latest.md")
        try:  # keep what was there as an archive copy (named by its own mtime) before latest is replaced
            if os.path.isfile(latest):
                mt = datetime.datetime.fromtimestamp(os.path.getmtime(latest), datetime.timezone.utc)
                arch = os.path.join(HANDOFFS_DIR, f"{sid}-{mt.strftime('%Y%m%dT%H%M%SZ')}-prev.md")
                if not os.path.exists(arch):
                    shutil.copyfile(latest, arch)
        except Exception:
            pass
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        write_handoff_files(sid, os.path.join(HANDOFFS_DIR, f"{sid}-{ts}.md"), body)
        write_health(d, sid, info, body, status_override="recovered")
        _out(json.dumps({"ok": True}))
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        _out(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}))
