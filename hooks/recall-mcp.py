#!/usr/bin/env python3
"""Recall MCP server (F69, opt-in): search earlier chats of this project as an MCP tool.

MCP over stdio: newline-delimited JSON-RPC 2.0 (initialize, notifications/initialized, ping, tools/list, tools/call).
It loads recall.py by path and calls its search and open code; it holds no search logic of its own. Every text goes
through redact() from recall.py. It only reads transcripts on this machine and talks only over stdin/stdout.
It never crashes on bad input: a bad JSON line gets a parse error (-32700) and the loop goes on.
Health: one row per tool call in recall-mcp.jsonl (ts, tool, hits, ms; no query text).
"""
import contextlib, json, os, sys, time

HOOK_VERSION = "1.18.0"
SERVER_NAME = "lossless-recall"
HOME = os.path.expanduser("~")


def hooks_home():
    """F72: HANDOFF_HOOKS_HOME when it names an existing folder, else None (then ~/.claude/hooks as before)."""
    v = (os.environ.get("HANDOFF_HOOKS_HOME") or "").strip()
    return v if v and os.path.isdir(v) else None


def hook_candidates(name, first, second):
    """F72: with the override, only that folder (no fallback, no mixing of two trees); else (first, second) as before."""
    h = hooks_home()
    return [os.path.join(h, name)] if h else [first, second]

STATE_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")
OUT_MAX = 12000
CUT_LINE = "[cut at 12000 characters]"
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"

RULE = ("- recall rule: before you state an earlier number, path, name, command or decision, search for it. "
        "Do not answer from memory. If the search finds nothing, say so.")
INSTRUCTIONS = (RULE + "\n"
                "Use search_earlier_chat to find exact earlier messages of this project (word for word, secrets masked). "
                "Use open_message with a hit id to read the full message.")

ALWAYS = {"anthropic/alwaysLoad": True}
TOOLS = [
    {"name": "search_earlier_chat",
     "description": ("Search the earlier chats of this project word for word (and by meaning if the meaning index exists). "
                     "Returns one line per hit: <id> <time> <role> [tag]: <snippet>. Use it before you state an earlier "
                     "number, path, name, command or decision."),
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "Words or a regex to search for."},
                                    "role": {"type": "string", "enum": ["user", "assistant", "any"], "default": "any"},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
                     "required": ["query"]},
     "_meta": ALWAYS},
    {"name": "open_message",
     "description": "Open one earlier message, with its neighbours, by the id of a search hit (for example 1a2b3c4d:57).",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string", "description": "A hit id: <sid8>:<line>."},
                                    "full": {"type": "boolean", "default": False}},
                     "required": ["id"]},
     "_meta": ALWAYS},
]

_mod = [None]


def load_recall():
    if _mod[0] is not None:
        return _mod[0]
    for p in hook_candidates("recall.py", os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py"),
                             os.path.join(HOME, ".claude", "hooks", "recall.py")):
        if os.path.isfile(p):
            import importlib.util
            spec = importlib.util.spec_from_file_location("lh_recall", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for n in ("redact", "cmd_search", "cmd_open", "build_parser", "project_root"):
                if not hasattr(mod, n):
                    raise RuntimeError("recall.py is too old or a stub")
            _mod[0] = mod
            return mod
    raise RuntimeError("recall.py not found")


def health(tool, hits, t0):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "recall-mcp.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": tool, "hits": int(hits),
                                "ms": int((time.monotonic() - t0) * 1000)}) + "\n")
    except BaseException:
        pass


def cap(text):
    """F69: at most OUT_MAX characters in total; a cut result ends with CUT_LINE (the marker counts in the total)."""
    if len(text) <= OUT_MAX:
        return text
    return text[:OUT_MAX - len(CUT_LINE) - 1] + "\n" + CUT_LINE


class ToolError(Exception):
    pass


def one_line(s):
    return " ".join(str(s).split())[:300] or "error"


def tool_search(args):
    q = args.get("query")
    if not isinstance(q, str) or not q.strip():
        raise ToolError("query must be a non-empty string")
    role = args.get("role", "any")
    if role not in ("user", "assistant", "any"):
        raise ToolError("role must be user, assistant or any")
    limit = args.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ToolError("limit must be an integer from 1 to 10")
    mod = load_recall()
    a = mod.build_parser().parse_args(["search", q, "--role", role, "--limit", str(limit)])
    a.subagents = True
    root = mod.project_root(os.getcwd())
    with contextlib.redirect_stdout(sys.stderr):
        lines = mod.cmd_search(a, root)
    hits = [x for x in lines[1:-1] if x]  # drop the mode line and the count line
    if not hits:
        return "no matches", 0
    return mod.redact("\n".join(hits)), len(hits)


def tool_open(args):
    mid = args.get("id")
    if not isinstance(mid, str) or not mid.strip():
        raise ToolError("id must be a non-empty string")
    full = args.get("full", False)
    if not isinstance(full, bool):
        raise ToolError("full must be true or false")
    mod = load_recall()
    a = mod.build_parser().parse_args(["open", mid.strip()] + (["--full"] if full else []))
    root = mod.project_root(os.getcwd())
    with contextlib.redirect_stdout(sys.stderr):
        lines = mod.cmd_open(a, root)
    text = "\n".join(lines)
    return mod.redact(text), sum(1 for x in lines if x.startswith("--- "))


def call_tool(params):
    """-> result dict, or raises KeyError for an unknown tool name."""
    name = params.get("name")
    args = params.get("arguments")
    fn = {"search_earlier_chat": tool_search, "open_message": tool_open}.get(name)
    if fn is None:
        raise KeyError(name)
    t0 = time.monotonic()
    hits = 0
    try:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError("arguments must be an object")
        text, hits = fn(args)
        res = {"content": [{"type": "text", "text": cap(text)}]}
    except ToolError as e:
        res = {"content": [{"type": "text", "text": one_line(e)}], "isError": True}
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        res = {"content": [{"type": "text", "text": one_line(e)}], "isError": True}
    health(name, hits, t0)
    return res


def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def err(rid, code, msg):
    reply({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}})


def handle(msg):
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
            return  # a response from the client: ignore
        err(msg.get("id") if isinstance(msg, dict) and isinstance(msg.get("id"), (str, int)) else None, -32600, "invalid request")
        return
    method = msg["method"]
    has_id = "id" in msg
    rid = msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if not has_id:
        return  # a notification (notifications/initialized and others): no reply
    if method == "initialize":
        want = params.get("protocolVersion")
        reply({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": want if want in SUPPORTED else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": HOOK_VERSION},
            "instructions": INSTRUCTIONS}})
    elif method == "ping":
        reply({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "tools/list":
        reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        try:
            reply({"jsonrpc": "2.0", "id": rid, "result": call_tool(params)})
        except KeyError:
            err(rid, -32602, "unknown tool")
    else:
        err(rid, -32601, "method not found")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            err(None, -32700, "parse error")
            continue
        try:
            handle(msg)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                return
            try:
                err(msg.get("id") if isinstance(msg, dict) and isinstance(msg.get("id"), (str, int)) else None, -32603, "internal error")
            except BaseException:
                pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
