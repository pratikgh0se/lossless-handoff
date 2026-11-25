# The agent file, the fresh summary call and the recall text.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

AGENT_CAP = 5000
FRESH_TOKENS = 50000
FRESH_NO_META_MS = 30 * 60 * 1000
MIN_AGENT_BYTES = 300
FRESH_GRACE_MS = 600000
STALE_USER_TURNS = 3
STALE_TOOL_CALLS = 40
HEADINGS = ["Goal", "Parallel work running", "Decisions and why", "Done", "In progress", "Dead ends", "Exact next step"]
STALE_NAMES = ["Parallel work running", "In progress", "Exact next step"]
SUMMARY_TIMEOUT_DEFAULT = 75
SUMMARY_MSG_CAP = 4000
SUMMARY_EXCERPT_CAP = 120000
SUMMARY_TEXT_CAP = 4000  # F39.6 (was 6,000)
SUMMARY_SYSTEM = ("You write handoff notes for an AI coding agent that will resume this session after its "
                  "context is compacted. Output only the note.")
SUMMARY_PROMPT = ("The conversation since the last compaction is on stdin. Write the handoff with exactly these "
                  "headings: ## Goal, ## Parallel work running, ## Decisions and why, ## Done, ## In progress, "
                  "## Dead ends, ## Exact next step. Under Parallel work running list every background agent, subagent, builder "
                  "or long-running job that is running or queued (name, task, branch or id, state), or \"none\". Describe the state at the END of the conversation, including the user's "
                  "latest request and any rules they set (what needs their approval, what not to touch). "
                  "Under Done list only work confirmed by a tool result; put anything started but unconfirmed "
                  "under In progress prefixed \"unverified:\". "
                  "Keep it under 3500 chars. Use real paths, commands and ids; no secrets.")
STANDING_HEAD = ("STANDING DECISIONS (do not contradict; if the conversation conflicts, "
                 "say so under Decisions):\n")
AUTHORITY = ("- authority: the user's latest words > Project decisions > Agent handoff > Fresh summary > "
             "Claude Code's summary > Facts. If two disagree, the newer statement from the user wins; "
             "if unsure, ask.\n")
RECALL_LINE = ("- recall: earlier chats in this project are searchable word for word: python3 ~/.claude/hooks/recall.py "
               "search \"<words or regex>\" (same full path for: open <id> [<id> ...]; user --last 20. One call takes several patterns or several ids. Do not use cd, a loop or a shell variable). Use it before acting on "
               "anything vague here (exact commands, numbers, paths, names, the user's wording) and whenever the user "
               "refers to something said earlier; quote what you find instead of guessing. "
               "If recall and the decisions list disagree, the decisions list and the user's latest words win: "
               "recall shows history, not current rules. "
               "If a hit is a tool call that shows `-> result <id>`, open that id to see what the tool returned.\n")


def recall_path():
    """F44.11(a): ~/.claude/hooks/recall.py (HOME from the environment) or recall.py next to this hook; None if neither."""
    for p in _hook_candidates("recall.py", os.path.join(os.path.expanduser("~"), ".claude", "hooks", "recall.py"),
                              os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")):
        if os.path.isfile(p):
            return p
    return None


RECALL_AGENT_MARK = "<!-- lossless-handoff:recall-agent -->"
RECALL_AGENT_SENTENCE = ("For wide lookups (many hits, long messages, or a whole node), ask the recall subagent "
                         "(Agent tool, subagent_type \"recall\") so the main context stays small; "
                         "for one exact fact, search directly.")


def recall_agent_ours():
    """F55.c: ~/.claude/agents/recall.md exists and holds our marker line. F72: with the override, only
    <HANDOFF_HOOKS_HOME>/agents/recall.md."""
    try:
        h = _hooks_home()
        ap = os.path.join(h, "agents", "recall.md") if h else os.path.join(os.path.expanduser("~"), ".claude", "agents", "recall.md")
        with open(ap, encoding="utf-8",
                  errors="replace") as f:
            return RECALL_AGENT_MARK in f.read()
    except OSError:
        return False


def recall_line():
    if not recall_path():
        return ""
    line = RECALL_LINE[:-1] + (" " + RECALL_AGENT_SENTENCE if recall_agent_ours() else "") + "\n"
    h = _hooks_home()
    return line.replace("~/.claude/hooks/recall.py", h.rstrip("/") + "/recall.py") if h else line


def summary_enabled():
    return os.environ.get("HANDOFF_SUMMARY") != "0" and os.environ.get("HANDOFF_HAIKU_FALLBACK") != "0"


def summary_model():
    m = os.environ.get("HANDOFF_SUMMARY_MODEL") or ""
    return m if re.fullmatch(r"[A-Za-z0-9._-]+", m) else "sonnet"


def summary_timeout():
    v = (os.environ.get("HANDOFF_SUMMARY_TIMEOUT") or "").strip()
    if re.fullmatch(r"\d+", v) and 1 <= int(v) <= 300:
        return int(v)
    return SUMMARY_TIMEOUT_DEFAULT


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_fresh(meta, size, mtime_ms, now_ms):
    """Agent handoff freshness. meta=None means no meta file."""
    if size is None or size < MIN_AGENT_BYTES:
        return False
    if not isinstance(meta, dict):
        return (now_ms - mtime_ms) <= FRESH_NO_META_MS
    written = _num(meta.get("agentWrittenAt"))
    if written is None:
        return False
    nudge = _num(meta.get("lastNudgeAt"))
    if nudge is not None and written >= nudge:
        return True
    ctx, wt = _num(meta.get("ctxTokens")), _num(meta.get("agentWrittenAtTokens"))
    return ctx is not None and wt is not None and (ctx - wt) <= FRESH_TOKENS


def section_hashes(text):
    """F35.1 hashes: heading (no '## ') -> first 12 hex of SHA-256 of the body, or 'absent'."""
    bodies, cur = {}, None
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln.startswith("## "):
            h = ln[3:]
            cur = [] if (ln == "## " + h and h in HEADINGS and h not in bodies) else None
            if cur is not None:
                bodies[h] = cur
            continue
        if cur is not None:
            cur.append(ln)
    return {h: (hashlib.sha256("\n".join(bodies[h]).rstrip().encode("utf-8")).hexdigest()[:12]
                if h in bodies else "absent") for h in HEADINGS}


def utc(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def read_agent(sid, P):
    """Return (body, warning, stale_sections). body None = no usable agent file (missing, unreadable,
    < 300 bytes). warning None = fresh. Never raises."""
    try:
        ap = os.path.join(HANDOFFS_DIR, f"{sid}-agent.md")
        st = os.stat(ap)
        if st.st_size < MIN_AGENT_BYTES:
            return None, None, []
        with open(ap, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        body = redact(raw).strip()
        meta = None
        try:
            with open(os.path.join(HANDOFFS_DIR, f"{sid}-agent.meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = None
        if not isinstance(meta, dict):
            meta = None
        mtime_ms = st.st_mtime * 1000
        reasons = []
        # (a) stale sections: meta says so, or the hook re-derives it
        stale = False
        stale_names = []
        if meta is not None:
            ss = meta.get("stale_sections")
            if isinstance(ss, list) and any(isinstance(x, str) for x in ss):
                stale = True
                stale_names = [x for x in ss if isinstance(x, str)]
            secs, nudge_at = meta.get("sections"), _num(meta.get("lastNudgeAt"))
            if not stale and isinstance(secs, dict) and nudge_at is not None:
                cur = section_hashes(raw)
                if all(isinstance(secs.get(h), str) and cur[h] == secs[h] for h in STALE_NAMES):
                    users = sum(1 for x in P["user_events"] if x > nudge_at)
                    tools = sum(1 for x in P["tool_events"] if x > nudge_at)
                    stale = users >= STALE_USER_TURNS or tools >= STALE_TOOL_CALLS
                    if stale:
                        stale_names = list(STALE_NAMES)
        if stale:
            na = _num(meta.get("lastNudgeAt")) if meta else None
            reasons.append(f"{', '.join(STALE_NAMES)} unchanged since {utc(na if na is not None else mtime_ms)}")
        lu = P["last_user"]
        if lu is not None and mtime_ms < lu - FRESH_GRACE_MS:
            reasons.append(f"file written {utc(mtime_ms)} is older than the last user message by more than 10 min")
        if not is_fresh(meta, st.st_size, mtime_ms, time.time() * 1000):
            reasons.append("not updated since the last nudge")
        return body, ("; ".join(reasons) if reasons else None), stale_names
    except Exception:
        return None, None, []


_EXCERPT = {"chars": 0, "dropped": 0}  # F56.a: filled by excerpt(); written to the health row


def excerpt(path, limit=SUMMARY_EXCERPT_CAP):
    """F38.2 + F39.7: genuine user/assistant messages after the last compact_boundary; tool uses as
    one-liners ending `-> ok`, `-> error: <first line>` or `-> no result`."""
    items = []
    results = {}  # tool_use_id -> suffix
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                    items = []
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                t, c = o.get("type"), msg.get("content")
                if t == "user" and not o.get("isMeta") and not o.get("isCompactSummary"):
                    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str):
                                if b.get("is_error") is True:
                                    first = (result_text(b.get("content")).strip().split("\n") or [""])[0]
                                    results[b["tool_use_id"]] = ("error: " + trunc(first, 200)).rstrip()
                                else:
                                    results[b["tool_use_id"]] = "ok"
                        continue
                    tx = text_of(c).strip()
                    if genuine(tx):
                        items.append(["USER: " + trunc(tx, SUMMARY_MSG_CAP)])
                elif t == "assistant":
                    parts = []
                    tx = text_of(c).strip()
                    if tx:
                        parts.append(trunc(tx, SUMMARY_MSG_CAP))
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                                key = (inp.get("file_path") or inp.get("command") or inp.get("pattern")
                                       or inp.get("description") or inp.get("prompt") or inp.get("subject") or "")
                                parts.append((f"[tool {b.get('name')}: {trunc(str(key), 300)}]", b.get("id")))
                    if parts:
                        items.append(["ASSISTANT: "] + parts)
    except Exception:
        return ""
    msgs = []
    for it in items:
        if len(it) == 1:
            msgs.append(it[0])
            continue
        lines = []
        for x in it[1:]:
            if isinstance(x, tuple):
                lines.append(x[0] + " -> " + results.get(x[1], "no result"))
            else:
                lines.append(x)
        msgs.append(it[0] + "\n".join(lines))
    sizes = [len(x) for x in msgs]
    total = sum(sizes) + 2 * max(len(msgs) - 1, 0)
    all_chars = total
    i = 0
    while total > limit and len(msgs) - i > 1:
        total -= sizes[i] + 2
        i += 1
    out = "\n\n".join(msgs[i:])
    sent = out[-limit:] if limit > 0 else ""
    _EXCERPT["chars"], _EXCERPT["dropped"] = len(sent), max(0, all_chars - len(sent))  # F56.a
    return sent


def run_claude(stdin_text, system, prompt, model, timeout):
    """One isolated headless `claude` call (F38.3 argv). Returns (stdout, None) or (None, reason). Never raises."""
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_AUTO_COMPACT_WINDOW"}
        env["HANDOFF_HOOK_CHILD"] = "1"
        with tempfile.TemporaryDirectory(prefix="handoff-hk-") as td:
            proc = subprocess.Popen(
                ["claude", "-p", "--model", model, "--output-format", "text", "--no-session-persistence",
                 "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                 "--disable-slash-commands", "--tools", "",
                 "--system-prompt", system, prompt],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=td, env=env, start_new_session=True)
            try:
                stdout, _ = proc.communicate(input=stdin_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except Exception:
                    proc.kill()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return None, f"timeout after {timeout}s"
            finally:
                try:
                    os.killpg(proc.pid, 9)  # stragglers holding our pipes
                except Exception:
                    pass
        if proc.returncode != 0:
            return None, f"claude exited {proc.returncode}"
        return stdout or "", None
    except FileNotFoundError:
        return None, "claude binary not found"
    except Exception as e:
        return None, type(e).__name__


def summary_handoff(tp, model, timeout, standing="(none)"):
    """Return (text, None) or (None, reason). Never raises."""
    try:
        pre = STANDING_HEAD + standing + "\n\nCONVERSATION:\n"
        ex = excerpt(tp, max(SUMMARY_EXCERPT_CAP - len(pre), 1000))
        if not ex:
            return None, "no transcript excerpt"
        out, err = run_claude(pre + ex, SUMMARY_SYSTEM, SUMMARY_PROMPT, model, timeout)
        if err == "claude binary not found":  # F56.a: nothing was sent
            _EXCERPT["chars"] = _EXCERPT["dropped"] = 0
        if err:
            return None, err
        out = out.strip()
        if len(out) < 50:
            return None, "empty or too-short output"
        return trunc(redact(out), SUMMARY_TEXT_CAP), None
    except Exception as e:
        return None, type(e).__name__
