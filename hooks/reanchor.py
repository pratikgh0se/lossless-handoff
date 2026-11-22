#!/usr/bin/env python3
"""PostToolUse hook (opt-in, F70): repeat the project's standing rules near the end of a long context.

Reads the hook JSON on stdin and the TAIL of the transcript only. The context token count is the usage of the last
assistant entry (input + cache read + cache creation), the same rule as handoff-nudge.py. When the count is 50,000 or
more above the last anchor, it prints one PostToolUse additionalContext note (a first line, the same rules block that
the SubagentStart hook uses, and the recall line) and stores the new anchor. A count below the anchor means a
compaction happened: the anchor is reset to the current count. State: <state>/reanchor-<session>.json.
HANDOFF_REANCHOR=0 switches it off. HANDOFF_REANCHOR_TOKENS (10,000-500,000) changes the 50,000 interval.
A subagent event (non-empty agent_id) is ignored. Never fails: any error means exit 0 with no output.
"""
import json, os, subprocess, sys, time

HOOK_VERSION = "1.18.0"
HOME = os.path.expanduser("~")
STATE_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")
DEFAULT_INTERVAL = 50000
FIRST_LINE = ("Lossless Handoff re-anchor: the project's active rules, repeated so they stay near the end of your "
              "context.")
TAILS = (65536, 400000)  # read the end of the transcript; widen once if no usage line is in it


def interval():
    s = os.environ.get("HANDOFF_REANCHOR_TOKENS", "").strip()
    if s.isdigit() and 10000 <= int(s) <= 500000:
        return int(s)
    return DEFAULT_INTERVAL


def context_tokens(path):
    """Same rule as handoff-nudge.py: usage of the last assistant entry that has one."""
    size = os.path.getsize(path)
    for n in TAILS:
        with open(path, "rb") as f:
            f.seek(max(0, size - n))
            data = f.read().decode("utf-8", "replace")
        for line in reversed(data.splitlines()):
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("type") != "assistant":
                continue
            u = (e.get("message") or {}).get("usage")
            if not isinstance(u, dict):
                continue
            t = 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                v = u.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    t += int(v)
            return t
        if size <= n:
            break
    return None


def rules_text(cwd):
    """The rules block + recall line, from postcompact-inject.py --rules-block (the SubagentStart builder). '' if none."""
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "postcompact-inject.py"),
              os.path.join(HOME, ".claude", "hooks", "postcompact-inject.py")):
        if os.path.isfile(p):
            r = subprocess.run([sys.executable, p, "--rules-block"], input=json.dumps({"cwd": cwd}),
                               capture_output=True, text=True, timeout=3)
            return r.stdout.strip() if r.returncode == 0 else ""
    return ""


def save_anchor(path, anchor):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "anchor": anchor}, f)
    os.replace(tmp, path)


def main():
    if os.environ.get("HANDOFF_REANCHOR") == "0":
        return
    d = json.loads(sys.stdin.read() or "{}")
    if not isinstance(d, dict):
        return
    aid = d.get("agent_id")
    if isinstance(aid, str) and aid.strip():
        return
    sid = str(d.get("session_id") or "").replace("/", "_")
    tp, cwd = d.get("transcript_path"), d.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = os.getcwd()
    if not sid or not isinstance(tp, str):
        return
    t = context_tokens(tp)
    if t is None:
        return
    sp = os.path.join(STATE_DIR, "reanchor-%s.json" % sid)
    try:
        with open(sp, encoding="utf-8") as f:
            a = json.load(f).get("anchor")
        anchor = a if isinstance(a, int) and not isinstance(a, bool) and a >= 0 else 0
    except Exception:
        anchor = 0
    if t < anchor:  # a compaction happened
        save_anchor(sp, t)
        return
    if t - anchor < interval():
        return
    save_anchor(sp, t)  # also when there is no rule, so the rules builder runs once per interval
    body = rules_text(cwd)
    if not body:
        return
    text = FIRST_LINE + "\n" + body
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
    sys.stdout.flush()
    try:
        with open(os.path.join(STATE_DIR, "health.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": sid[:36],
                                "event": "reanchor", "tokens": t, "chars": len(text)}) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
