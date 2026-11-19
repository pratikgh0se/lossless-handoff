# The handoff sections, the build function and the status.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

AGENT_HEAD = "## Agent handoff (written by the working agent)\n"
SUMMARY_HEAD_FMT = "## Fresh summary at compaction ({}; verify against facts)\n"


class Rep(str):
    """A fail-safe replacement line: never dropped by cap trimming."""


def guard(name, info, fn, default=""):
    """Run one section builder; on any Exception return a replacement line and record the name."""
    try:
        if name in FAULTS:
            raise RuntimeError("injected")
        return fn()
    except Exception as e:
        info["errors"].append(name)
        return Rep(f"(section {name} unavailable: {type(e).__name__})")


def _f_users(P, cwd):
    return ("## Last user messages (oldest first)\n" +
            "\n".join("- " + trunc(redact(u), 600).replace("\n", " ") for u in P["users"][-8:]) + "\n") if P["users"] else ""


def _f_files(P, cwd):
    return "## Files touched (last 30)\n" + "\n".join(f"- {f}" for f in P["files"][-30:]) + "\n" if P["files"] else ""


def _f_tasks(P, cwd):
    tasks = P["tasks"]
    return "## Task state\n" + "\n".join(
        f"- [{v['status']}] {trunc(v['subject'], 150)}" for v in tasks.values()) + "\n" if tasks else ""


def _f_open(P, cwd):
    op = [f"- [{v['status']}] {one_line(v['subject'], 150)}" for v in P["tasks"].values() if v["status"] != "completed"]
    if isinstance(P["todos"], list):
        for x in P["todos"]:
            st = x.get("status")
            if isinstance(st, str) and st != "completed":
                op.append(f"- [{st}] {one_line(x.get('content'), 150)}")
    if not op:
        return ""
    return "## Open tasks\n" + "\n".join(op[:20] + ([f"- ... +{len(op) - 20} more"] if len(op) > 20 else [])) + "\n"


def _f_bg(P, cwd):
    bg = [f"- {one_line(r['desc'], 100)} (model: {r['model'] if isinstance(r['model'], str) and r['model'] else 'default'})"
          for r in P["bg"] if not r["done"]][:10]
    return "## Background agents\n" + "\n".join(bg) + "\n" if bg else ""


def _f_git(P, cwd):
    g = git(cwd)
    return f"## Git\n- branch: {redact(g[0])}\n```\n{trunc(redact(g[1]), 1500)}\n```\n" if g else ""


def _f_cmds(P, cwd):
    return "## Last Bash commands\n" + "\n".join(
        "- `" + trunc(redact(c), 200).replace("\n", " ") + "`" for c in P["cmds"][-5:]) + "\n" if P["cmds"] else ""


def _f_asst(P, cwd):
    return "## Last assistant message\n" + trunc(P["asst"], 1500) + "\n" if P["asst"] else ""


# internal key -> (section name, builder), in the F35.D display order
FACTS = [("users", "user_messages", _f_users), ("files", "files", _f_files),
         ("tasks", "tasks", _f_tasks), ("open", "open_tasks", _f_open), ("bg", "background_agents", _f_bg),
         ("git", "git", _f_git), ("cmds", "bash", _f_cmds), ("asst", "assistant", _f_asst)]
# F37.2: the user's own words (users) go last
DROP_ORDER = ["files", "cmds", "git", "tasks", "asst", "bg", "open", "users"]  # F39.6: no more `qa`


RECOVER_CAP = 9300  # F45.b: a recovered handoff must fit inline in one SessionStart part


def build(d, info, mode=None):
    """mode None = full run; "quick" = F45.a deterministic early handoff (no model call, no ledger merge);
    "recover" = F45.b rebuild after the fact (records before the last compact_boundary, no model call, no ledger)."""
    cap = RECOVER_CAP if mode == "recover" else CAP
    nomodel = mode in ("quick", "recover")
    sid = d.get("session_id", "unknown")
    cwd = d.get("cwd", "")
    tp = d.get("transcript_path", "")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    header = (f"# Handoff (pre-compaction)\n- session: {sid}\n- cwd: {cwd}\n- trigger: {'recovered' if mode == 'recover' else d.get('trigger')}\n"
              f"- time: {now}\n- transcript: {tp}\n"
              + (f"- agent: {d.get('agent_id')} ({d.get('agent_type') or 'unknown type'}), a subagent of this session\n"
                 if agent_id_of(d) else "") + f"{AUTHORITY}{recall_line()}")
    footer = f"\nFull transcript: {tp}. Read it for exact details before guessing.\n"
    P = redact_tree(parse(tp, before_last_boundary=(mode == "recover")))  # F65: every section reads masked text
    if "toplevel" in FAULTS:
        raise RuntimeError("injected")
    enabled = summary_enabled() and not nomodel
    model = summary_model() if enabled else ""
    timeout = summary_timeout()
    l_timeout = ledger_timeout()
    info["summary_model"] = model

    body, warn, stale_names = read_agent(str(sid).replace("/", "_"), P)
    info["agent_file"] = "absent" if body is None else ("fresh" if warn is None else "stale")
    info["stale_sections"] = stale_names if (body is not None and warn is not None) else []

    wtxt = guard("warning", info, lambda: f"Agent handoff may be stale: {warn}\n" if (body and warn) else "")
    atxt = guard("agent", info, lambda: AGENT_HEAD + trunc(body, AGENT_CAP) + "\n\n" if body else "")
    if isinstance(atxt, Rep):
        atxt = Rep(atxt + "\n\n")
    if isinstance(wtxt, Rep):
        wtxt = Rep(wtxt + "\n")
    pre = wtxt + atxt

    # F39: ledger root, previous active lines (for the summary), merge thread
    led = {"status": "disabled", "restored": 0}
    root = None
    if ledger_enabled() and not nomodel and not info.get("no_ledger"):  # F46.b: only the main session merges the ledger
        root = project_root(cwd)
        led["status"] = "none" if root is None else "pending"
    prev_active = []
    if root is not None:
        try:
            prev_active = active_lines(parse_ledger(read_text(os.path.join(ledger_dir(root), "decisions.md"))))
        except Exception:
            prev_active = []
    standing = "\n".join(prev_active) if prev_active else "(none)"
    mthread = None
    if root is not None:
        mthread = threading.Thread(target=ledger_merge, args=(str(sid).replace("/", "_"), tp, root, led, l_timeout),
                                   daemon=True)
        mthread.start()
        m_start = time.monotonic()

    htext, hline = None, ""

    def run_summary():
        nonlocal htext, hline
        if not enabled:
            info["fallback"] = "disabled"
            return ""
        text, err = summary_handoff(tp, model, timeout, standing)
        if text:
            info["fallback"] = "ok"
            htext = text
        else:
            info["fallback"] = "timeout" if str(err).startswith("timeout after") else "failed"
            hline = f"Fresh summary failed: {err}\n\n"
        return ""

    info["fallback"] = "disabled" if not enabled else "failed"
    f_out = guard("fallback", info, run_summary)
    if isinstance(f_out, Rep):
        htext, hline = None, f_out + "\n\n"
        if "fallback" not in info["errors"]:
            info["errors"].append("fallback")

    if mthread is not None:
        mthread.join(max(0.0, m_start + l_timeout + 6 - time.monotonic()))
        if led["status"] == "pending":
            led["status"], led["error"] = "failed", "timeout"
    info["ledger"] = led["status"]
    info["ledger_restored"] = led["restored"]
    if "ids_before" in led:  # F47.a: only when the ledger was actually read for a merge
        info["ledger_ids"] = {"ledger_ids_before": led["ids_before"], "ledger_ids_after": led.get("ids_after", led["ids_before"]),
                              "superseded_before": led.get("sup_before", []), "superseded_after": led.get("sup_after", [])}
    info["ledger_error"] = led.get("error", "") if led["status"] in ("failed", "locked") else ""
    lblock = ""
    if root is not None:
        def _ledger_section():
            ents = parse_ledger(read_text(os.path.join(ledger_dir(root), "decisions.md")))
            info["ledger_active"], info["ledger_superseded"], info["ledger_resolved"] = ledger_counts(ents)
            info["ledger_chars"] = sum(len(e["line"]) for e in ents.values() if e["kind"] == "act")
            return ""  # F39.10: the ledger is injected by postcompact-inject.py --ledger --part 1|2, not in latest.md
        lblock = guard("decisions", info, _ledger_section)
        if isinstance(lblock, Rep):
            lblock = Rep(lblock + "\n\n")

    secs, protected = {}, set()
    for key, name, fn in FACTS:
        out = guard(name, info, lambda fn=fn: fn(P, cwd))
        if isinstance(out, Rep):
            secs[key] = Rep(out + "\n")
            protected.add(key)
        elif out:
            secs[key] = out

    attempted = bool(htext) or bool(hline)

    def total(hblock):
        lead = pre + hblock + hline
        if lead or attempted:
            lead += "## Facts (auto-extracted)\n"
        return len(header) + len(lblock) + len(lead) + len(footer) + sum(len(v) + 1 for v in secs.values()), lead

    # F38.5: facts (F37.2 order, all but the user's words) first, then shorten the summary, then the user's words
    hblock = SUMMARY_HEAD_FMT.format(model) + htext + "\n\n" if htext else ""
    n, lead = total(hblock)
    for k in DROP_ORDER:
        if n <= cap or k == "users":
            continue
        if k in secs and k not in protected:
            del secs[k]
            info["truncated"] = True
            n, lead = total(hblock)
    if n > cap and htext:
        info["truncated"] = True
        head = SUMMARY_HEAD_FMT.format(model)
        room = len(htext) - (n - cap)
        hblock = head + trunc(htext, room) + "\n\n" if room >= 200 else ""
        n, lead = total(hblock)
    if n > cap and "users" in secs and "users" not in protected:
        del secs["users"]
        info["truncated"] = True
        n, lead = total(hblock)
    order = [k for k, _, _ in FACTS]
    info["errors"] = [e for e in SECTION_NAMES if e in info["errors"]] + [e for e in info["errors"] if e not in SECTION_NAMES]
    return header + "\n" + lblock + lead + "\n".join(secs[k] for k in order if k in secs) + footer


def minimal_handoff(d, sid, exc_name):
    """Top-level failure output: string formatting plus one guarded git call. Never raises."""
    try:
        tp = d.get("transcript_path", "")
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        out = (f"# Handoff (minimal: hook failed with {exc_name})\n- session: {sid}\n- cwd: {d.get('cwd')}\n"
               f"- trigger: {d.get('trigger')}\n- time: {now}\n- transcript: {tp}\n")
        try:
            cwd = d.get("cwd")
            if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
                r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                                   capture_output=True, text=True, timeout=5)
                b = (r.stdout or "").strip()
                if r.returncode == 0 and b:
                    out += f"- git branch: {b}\n"
        except BaseException:
            pass
        return out + f"Full transcript: {tp}. Read it for exact details before guessing.\n"
    except BaseException:
        return f"# Handoff unavailable (hook failed)\n- session: {sid}\n"


def compute_status(info):
    af, ff, errs, ss = info["agent_file"], info["fallback"], info["errors"], info["stale_sections"]
    if errs or ss or (af != "fresh" and ff in ("failed", "timeout", "disabled")):
        reasons = []
        if af == "absent":
            reasons.append("agent_absent")
        if af == "stale" or ss:
            reasons.append("agent_stale")
        if ff == "failed":
            reasons.append("fallback_failed")
        if ff == "timeout":
            reasons.append("fallback_timeout")
        if errs:
            reasons.append("section_errors")
        return "degraded", reasons
    if af != "fresh" and ff == "ok":
        return "reduced", ["agent_absent" if af == "absent" else "agent_stale"]
    return "ok", []
