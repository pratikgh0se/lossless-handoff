#!/usr/bin/env python3
"""Stop hook (opt-in, F71): flag a reply that states an earlier fact without a recall search.

Reads the hook JSON on stdin (session_id, transcript_path, stop_hook_active, last_assistant_message) and the end of the
transcript. If the reply holds a claim cue ("as we decided", "you said", "last time" ...) and, since the last user
message, there is no recall evidence (a Bash call with recall.py, an mcp__lossless-recall__ tool call, or an
auto-recall injection), the hook acts by mode:
  HANDOFF_CLAIM_GUARD unset or "shadow" (default): append one row to <state>/claim-guard.jsonl, print nothing.
  HANDOFF_CLAIM_GUARD=block: also print {"decision": "block", "reason": ...}, once per user turn.
  HANDOFF_CLAIM_GUARD=0: nothing at all.
The log has no reply text and no prompt text. Never fails: any error means exit 0 with no output.
"""
import json, os, re, sys, time

HOOK_VERSION = "1.20.0"
HOME = os.path.expanduser("~")
STATE_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")
CUES = ["as we decided", "we decided", "as you said", "you said", "we agreed", "as agreed", "as discussed",
        "you told me", "you mentioned", "as mentioned earlier", "earlier you", "last time"]
CUE_RES = [(c, re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, c.split())) + r"(?!\w)", re.I)) for c in CUES]
REASON = "You stated an earlier fact without searching. Search for it with recall, then correct or confirm it."
AR_HEADER = "Lossless Handoff auto-recall:"
TAILS = (1 << 20, 8 << 20, 64 << 20)
NOT_USER_PREFIXES = ("<task-notification>", "<bash-input>", "<bash-stdout>", "<local-command-")


def find_cue(text):
    for c, rx in CUE_RES:
        if rx.search(text):
            return c
    return None


def is_user_turn(e):
    """A real user message: type user, not meta, not a tool result."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain") or e.get("isCompactSummary"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return not c.lstrip().startswith(NOT_USER_PREFIXES)
    return isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def strings(o, depth=0):
    if depth > 6:
        return
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v, depth + 1)


def has_evidence(e):
    if e.get("type") == "assistant":
        c = (e.get("message") or {}).get("content")
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name")
            if isinstance(name, str) and name.startswith("mcp__lossless-recall__"):
                return True
            cmd = (b.get("input") or {}).get("command") if isinstance(b.get("input"), dict) else None
            if name == "Bash" and isinstance(cmd, str) and "recall.py" in cmd:
                return True
        return False
    return any(s.lstrip().startswith(AR_HEADER) for s in strings(e))


def scan(path):
    """-> (uuid of the last user message, evidence since it) or None when the transcript has no user message."""
    size = os.path.getsize(path)
    for n in TAILS:
        with open(path, "rb") as f:
            f.seek(max(0, size - n))
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        if size > n and lines:
            lines = lines[1:]  # the first line may be cut
        ents = []
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if isinstance(e, dict):
                ents.append(e)
        idx = next((i for i in range(len(ents) - 1, -1, -1) if is_user_turn(ents[i])), None)
        if idx is None:
            if size <= n:
                return None
            continue
        uuid = ents[idx].get("uuid")
        ev = any(has_evidence(e) for e in ents[idx:])  # only entries after the last user message
        return (uuid if isinstance(uuid, str) else ""), ev
    return None


def main():
    raw = sys.stdin.read()  # read stdin first: an early exit would break the pipe of the caller
    mode = os.environ.get("HANDOFF_CLAIM_GUARD", "").strip().lower()
    if mode == "0":
        return
    d = json.loads(raw or "{}")
    if not isinstance(d, dict) or d.get("stop_hook_active") is True:
        return
    msg, tp = d.get("last_assistant_message"), d.get("transcript_path")
    sid = str(d.get("session_id") or "").replace("/", "_")
    if not isinstance(msg, str) or not sid or not isinstance(tp, str):
        return
    cue = find_cue(msg)
    if not cue:
        return
    r = scan(tp)
    if r is None or r[1]:
        return
    turn = r[0]
    live = mode == "block"
    mp = os.path.join(STATE_DIR, "claim-guard-%s.json" % sid)
    if live:
        try:
            with open(mp, encoding="utf-8") as f:
                if json.load(f).get("turn") == turn and turn:
                    return  # already blocked once in this user turn
        except Exception:
            pass
    os.makedirs(STATE_DIR, exist_ok=True)
    with os.fdopen(os.open(os.path.join(STATE_DIR, "claim-guard.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session_id": sid[:36],
                            "would_block": True, "cue": cue}) + "\n")
    if live:
        with os.fdopen(os.open(mp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump({"turn": turn}, f)
        sys.stdout.write(json.dumps({"decision": "block", "reason": REASON}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
