# Atomic writes, the health line, the error log, subagent views and the indexer.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

def write_atomic(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    # Mode 0600: the files hold redacted chat content. The temp file is private, so os.replace also fixes an old file.
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def append_health(line):
    with open(HEALTH_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        with open(HEALTH_FILE, "rb") as f:
            lines = f.readlines()
        if len(lines) > HEALTH_MAX_LINES:
            # F58.d: SubagentStart rows go first, so they never push compaction rows out
            keep = [x for x in lines if b'"event": "subagent_start"' not in x and b'"event":"subagent_start"' not in x]
            if len(keep) > HEALTH_MAX_LINES // 2:
                lines = keep
            tmp = HEALTH_FILE + f".tmp{os.getpid()}"
            with open(tmp, "wb") as f:
                f.writelines(lines[-HEALTH_MAX_LINES:])
            os.replace(tmp, HEALTH_FILE)
    except Exception:
        pass


def log_error(sid, exc):
    """F42.6: one JSON line {ts, hook, version, sid, error, trace} in <state>/hooks-error.jsonl (rotated to .1 above
    1,000,000 bytes). Never raises."""
    try:
        import traceback
        path = os.path.join(HANDOFFS_DIR, "hooks-error.jsonl")
        os.makedirs(HANDOFFS_DIR, mode=0o700, exist_ok=True)
        try:
            if os.path.getsize(path) > 1000000:
                os.replace(path, path + ".1")
        except OSError:
            pass
        tr = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tr = "".join(tr).rstrip("\n").splitlines()[-20:]
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "hook": "precompact", "version": HOOK_VERSION, "sid": str(sid)[:36],
                                "error": f"{type(exc).__name__}: {exc}"[:300], "trace": tr}) + "\n")
    except BaseException:
        pass


def agent_id_of(d):
    """F46.a: a non-empty string `agent_id` marks a subagent event."""
    aid = d.get("agent_id")
    return aid.strip() if isinstance(aid, str) and aid.strip() else None


def agent_view(d):
    """F46.a: for a subagent event return (view, key, found): `view` is a copy of the hook input whose session_id is the
    per-agent key `<sid>-a-<agent_id>` and whose transcript_path is the subagent's own transcript
    `<dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl`; `found` says whether that file exists.
    Returns None for a main-agent event."""
    aid = agent_id_of(d)
    if not aid:
        return None
    psid = str(d.get("session_id") or "unknown")
    key = f"{psid.replace('/', '_')}-a-{aid.replace('/', '_')}"
    tp = d.get("transcript_path")
    if isinstance(tp, str) and "/subagents/" in tp:
        sub = tp
    elif isinstance(tp, str) and tp:
        sub = os.path.join(os.path.dirname(tp), psid, "subagents", f"agent-{aid}.jsonl")
    else:
        sub = ""
    v = dict(d)
    v["session_id"] = key
    v["_parent_sid"] = psid.replace("/", "_")  # health rows carry the PARENT's sid, never the key
    v["transcript_path"] = sub
    return v, key, bool(sub) and os.path.isfile(sub)


def is_subagent(d):
    """F42.5. A subagent compaction is recognised by a non-empty `agent_id` in the input, or a transcript_path under
    /subagents/. Claude Code fires the hooks for a compacting subagent with `agent_id` set, but with the parent's
    session_id and transcript_path (since 1.10.0 this hook builds the subagent handoff from the subagent's own
    transcript). The /subagents/ test covers a transcript_path that already points at the subagent."""
    aid = d.get("agent_id")
    tp = d.get("transcript_path")
    return (isinstance(aid, str) and bool(aid.strip())) or (isinstance(tp, str) and "/subagents/" in tp)


def start_indexer(d, sid):
    """F44.9/F44.11(b): start `recall.py index --cwd <cwd> --max-seconds 600` fully detached after the handoff is
    written. Never waited on; any error goes to hooks-error.jsonl."""
    try:
        rp = recall_path()
        cwd = d.get("cwd")
        if not rp or not isinstance(cwd, str) or not cwd.strip():
            return
        rdir = os.path.join(HANDOFFS_DIR, "recall")
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        log = open(os.path.join(rdir, "index.log"), "ab")
        try:
            subprocess.Popen([sys.executable, rp, "index", "--cwd", cwd, "--max-seconds", "600"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
        finally:
            log.close()
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            log_error(sid, e)


def write_pending(d, sid):
    """F45.a: <sid>-pending.json {ts, trigger, hook_version}, atomically; write_health deletes it."""
    trig = d.get("trigger")
    write_atomic(os.path.join(HANDOFFS_DIR, f"{sid}-pending.json"), json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trigger": trig if trig in ("auto", "manual") else "unknown", "hook_version": HOOK_VERSION}))
