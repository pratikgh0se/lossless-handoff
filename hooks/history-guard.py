#!/usr/bin/env python3
"""Stop hook (opt-in, F77): check a reply that uses a key whose value changed in this chat.

Reads the hook JSON on stdin (session_id, transcript_path, cwd, stop_hook_active, last_assistant_message). When the
context is 100,000 tokens or more and the reply holds an identifier (a "subject") that was stated in 2 to 40 text
messages of this chat, of which 2 or more are user messages, and the turn has no history check for it yet, the hook
acts by mode:
  HANDOFF_HISTORY_GUARD unset or "shadow" (default): append one row to <state>/history-guard.jsonl, print nothing.
  HANDOFF_HISTORY_GUARD=block: also print {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": ...}}
      with the full history of each subject (F73), once per user turn. Claude Code then continues the turn.
  HANDOFF_HISTORY_GUARD=0: nothing at all.
HANDOFF_HISTORY_GUARD_TOKENS (10,000-1,000,000) changes the 100,000 threshold. The log holds no reply text and no
identifier text. Internal budget 3.0 seconds. Never fails: any error means exit 0 with no output. Stdlib only.
The shared rules are in guard_common.py (beside this file).
"""
import json, os, sys, threading, time

HOOK_VERSION = "1.24.0"
HEADER = ("Lossless Handoff history check: your reply uses values that changed in this chat. The full history is below, "
          "oldest first. A later mention can be a rejected proposal, a dry run or an undo. Read every mention in order. "
          "If your reply is correct, send the same reply again. If it is not, send the corrected reply. Do not mention "
          "this check.")


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


def main():
    t0 = time.monotonic()
    raw = sys.stdin.read()  # read stdin first: an early exit would break the pipe of the caller
    mode = os.environ.get("HANDOFF_HISTORY_GUARD", "").strip().lower()
    if mode == "0":
        return
    d = json.loads(raw or "{}")
    if not isinstance(d, dict) or d.get("stop_hook_active") is True:
        return
    reply, tp, cwd = d.get("last_assistant_message"), d.get("transcript_path"), d.get("cwd")
    sid = str(d.get("session_id") or "").replace("/", "_")
    if not isinstance(reply, str) or not sid or not isinstance(tp, str) or not os.path.isfile(tp):
        return
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = os.getcwd()
    g = common()
    tk = g.context_tokens(tp)
    if tk is None or tk < g.threshold("HANDOFF_HISTORY_GUARD_TOKENS"):
        return
    anchors = list(dict.fromkeys(g.identifiers(reply)))[:g.MAX_ANCHORS]
    if not anchors:
        return
    deadline = t0 + g.BUDGET_S
    r = g.read_session(tp, deadline, None, True)
    if r is None:
        return
    msgs, turn = r
    idx, sent = g.build_index(msgs)
    st = g.read_state(sid)
    if turn["uuid"] and st.get("turn") == turn["uuid"]:
        return  # the guard blocked in this user turn already
    keep, written = g.pick_subjects(anchors, cwd, idx, sent, turn, st, tk, True)
    if not keep or time.monotonic() > deadline:
        return
    live = mode == "block"
    text = ""
    try:
        text = g.history_text(HEADER, [written[k] for k in keep], cwd, tp, __file__)
    except Exception:
        text = ""
    if live and (not text or time.monotonic() > deadline):
        return
    g.log_row("history-guard.jsonl", {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": sid[:36],
                                      "mode": "block" if live else "shadow", "fired": True, "subjects": len(keep),
                                      "anchors": len(anchors), "tokens": tk, "chars": len(text),
                                      "ms": int((time.monotonic() - t0) * 1000)})
    g.save_shown(sid, keep, idx, tk, turn["uuid"])  # shadow saves it too, so shadow predicts live
    if live:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}))
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
