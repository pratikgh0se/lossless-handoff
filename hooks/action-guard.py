#!/usr/bin/env python3
"""PostToolBatch hook (opt-in, F80): check a tool call that uses a name whose value changed in this chat.

Reads the hook JSON on stdin (session_id, transcript_path, cwd, tool_calls). It scans the new text of Edit, Write,
MultiEdit and NotebookEdit calls (not old_string, not file_path) and the command of Bash calls. When the context is
100,000 tokens or more, and a name in them is a subject (stated in 2 to 40 text messages, 2 or more from the user), with
no history evidence in this turn and no cooldown, the hook acts by mode:
  HANDOFF_ACTION_GUARD unset or "shadow" (default): append one row to <state>/action-guard.jsonl, print nothing.
  HANDOFF_ACTION_GUARD=inject: also print {"hookSpecificOutput": {"hookEventName": "PostToolBatch",
      "additionalContext": ...}} with the full history of each subject (F73). It never denies or blocks a call.
  HANDOFF_ACTION_GUARD=0: nothing at all.
HANDOFF_ACTION_GUARD_TOKENS (10,000-1,000,000) changes the 100,000 threshold. The log holds no input text and no
identifier text. Internal budget 3.0 seconds. Never fails: any error means exit 0 with no output. Stdlib only.
The shared rules and the cooldown table are in guard_common.py (beside this file), shared with history-guard.py.
"""
import json, os, sys, threading, time

HOOK_VERSION = "1.20.0"
HEADER = ("Lossless Handoff history check: your last tool call used names whose values changed in this chat. The full "
          "history is below, oldest first. A later mention can be a rejected proposal, a dry run or an undo. If the call "
          "used an old, rejected or undone value, fix it now. If it is correct, go on. Do not mention this check.")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")


def common():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "guard_common.py")
    if not os.path.isfile(p):
        p = os.path.join(os.path.dirname(os.path.realpath(__file__)), "guard_common.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("lh_guard_common", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def input_texts(name, inp):
    """The texts of one call that can hold a value: new text and content, the Bash command. Not old_string, not file_path."""
    if not isinstance(inp, dict):
        return []
    out = []
    if name == "Bash":
        out.append(inp.get("command"))
    elif name == "Edit":
        out.append(inp.get("new_string"))
    elif name == "Write":
        out.append(inp.get("content"))
    elif name == "MultiEdit":
        for e in inp.get("edits") if isinstance(inp.get("edits"), list) else []:
            if isinstance(e, dict):
                out.append(e.get("new_string"))
    elif name == "NotebookEdit":
        out.append(inp.get("new_source"))
    return [x for x in out if isinstance(x, str)]


def main():
    t0 = time.monotonic()
    mode = os.environ.get("HANDOFF_ACTION_GUARD", "").strip().lower()
    if mode == "0":
        return
    d = json.loads(sys.stdin.read() or "{}")
    if not isinstance(d, dict):
        return
    tp, cwd = d.get("transcript_path"), d.get("cwd")
    sid = str(d.get("session_id") or "").replace("/", "_")
    calls = d.get("tool_calls")
    if not sid or not isinstance(tp, str) or not os.path.isfile(tp) or not isinstance(calls, list):
        return
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = os.getcwd()
    g = common()
    anchors, tool_of = [], {}
    for c in calls:
        if not isinstance(c, dict) or c.get("tool_name") not in EDIT_TOOLS:
            continue
        for text in input_texts(c.get("tool_name"), c.get("tool_input")):
            for a in g.identifiers(text):
                if a not in tool_of:
                    tool_of[a] = c.get("tool_name")
                    anchors.append(a)
    anchors = anchors[:g.MAX_ANCHORS]
    if not anchors:
        return
    tk = g.context_tokens(tp)
    if tk is None or tk < g.threshold("HANDOFF_ACTION_GUARD_TOKENS"):
        return
    deadline = t0 + g.BUDGET_S
    r = g.read_session(tp, deadline)
    if r is None:
        return
    msgs, turn = r
    idx, sent = g.build_index(msgs)
    st = g.read_state(sid)
    keep, written = g.pick_subjects(anchors, cwd, idx, sent, turn, st, tk, False)
    if not keep or time.monotonic() > deadline:
        return
    live = mode == "inject"
    text = ""
    try:
        text = g.history_text(HEADER, [written[k] for k in keep], cwd, tp, __file__)
    except Exception:
        text = ""
    if live and (not text or time.monotonic() > deadline):
        return
    tool = next((tool_of[a] for a in anchors if a in tool_of), "")
    g.log_row("action-guard.jsonl", {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": sid[:36],
                                     "mode": "inject" if live else "shadow", "fired": True, "subjects": len(keep),
                                     "anchors": len(anchors), "tokens": tk, "chars": len(text),
                                     "ms": int((time.monotonic() - t0) * 1000), "tool": tool})
    if live:
        g.save_shown(sid, keep, idx, tk)
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolBatch", "additionalContext": text}}))
        sys.stdout.flush()


def watchdog():
    os._exit(0)  # the internal budget is over: no output


if __name__ == "__main__":
    try:
        timer = threading.Timer(3.2, watchdog)
        timer.daemon = True
        timer.start()
        main()
    except BaseException:
        pass
    sys.exit(0)
