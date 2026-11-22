#!/usr/bin/env python3
"""recall.py (F44): search this project's earlier Claude Code chats word for word.

Read-only over ~/.claude/projects/*/<sid>.jsonl, except its own cache <state>/recall-index.json.
Subcommands: search, open, user, handoffs, index, map, describe, nodes. Python 3 stdlib only; uses `rg` as a prefilter when on
PATH (same output as the pure-Python scan).
"""
import argparse
import array
import datetime
import fcntl
import glob
import hashlib
import json
import math
import operator
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
STATE_DIR = (os.environ.get("HANDOFF_STATE_DIR") or "").strip() or os.path.join(HOME, ".local", "state", "claude-handoffs")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
INDEX_FILE = os.path.join(STATE_DIR, "recall-index.json")

OUT_CAP = 12000
CHUNK_MAX, MIN_CHUNK, BATCH, MEANING_TOP = 1500, 20, 32, 50
QUERY_TIMEOUT, INDEX_TIMEOUT = 30, 60  # F44.12(b): cold model load needs > 5 s
CUT_MARK = "[output cut at 12,000 chars; narrow the search]"
OPEN_FULL_CAP = 45000  # F44.12(a): total stdout cap for `open --full`
MSG_CAP_FULL = 40000

REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
WRAPPER_PREFIXES = ("<task-notification", "[SYSTEM NOTIFICATION", "<command-name", "<local-command",
                    "<command-message", "<command-args", "Caveat:", "<artifact-content-authored-by-others",
                    "<bash-input>", "<bash-stdout>", "<bash-stderr>", "This session is being continued")

# F65: one shared redaction function. Each pattern is (kind, regex, group); the matched group is
# replaced by [REDACTED:<kind>]. Order matters for ties: the first kind at one start position wins.
SECRET_RES = [
    ("aws", re.compile(r"(?<![A-Za-z0-9_-])(?:AKIA|ASIA)[0-9A-Z]{16}"), 0),
    ("github", re.compile(r"(?<![A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9]{20,}"), 0),
    ("github", re.compile(r"(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{20,}"), 0),
    ("slack", re.compile(r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("anthropic", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{16,}"), 0),
    ("openai", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}"), 0),
    ("google", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"), 0),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), 0),
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----(?:[^-]|-(?!----))*-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----"), 0),
    # URL credentials: scheme://user:PASSWORD@host (the password goes)
    ("url", re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@]{1,100}:([^\s/@]{3,200})@"), 1),
    # Authorization: Bearer VALUE
    ("secret", re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+([A-Za-z0-9._~+/=-]{8,})"), 1),
    # key=value or key: value; the key name ends with a secret word; the key stays, the value goes.
    # No prefix scan before the key word, so the match time is linear. _value_ok() filters the value.
    ("secret", re.compile(r"(?i)(?:password|passwd|passphrase|secret(?:[_-]?key)?|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)"
                          r"[\"']?\s*[:=][ \t]*(?:bearer[ \t]+)?(\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|[^\s\"'`,;]+)"), 1),
]
MARK = "[redacted]"  # legacy mark of earlier versions; readers still treat it as redacted


def _recall_cmd():
    """F72: the printed path of this script: <HANDOFF_HOOKS_HOME>/recall.py when that names a folder."""
    h = (os.environ.get("HANDOFF_HOOKS_HOME") or "").strip()
    return h.rstrip("/") + "/recall.py" if h and os.path.isdir(h) else "~/.claude/hooks/recall.py"


def mark_for(kind):
    return "[REDACTED:%s]" % kind


def _value_ok(v):
    """True when a key=value value looks like a secret: 8 or more characters, not only letters, not only
    digits, not a call or an index (get_secret(name), os.environ[...]) and not a variable reference."""
    if len(v) < 8 or v.isdigit() or re.fullmatch(r"[A-Za-z_.]+", v):
        return False
    if re.match(r"[A-Za-z_][\w.]*[(\[]", v) or re.fullmatch(r"\$\{?\w+\}?|<[A-Za-z_ -]*>|\{\{.*\}\}", v):
        return False
    return True


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def fmt_ts(ts):
    """ISO timestamp (UTC, as stored) -> 'YYYY-MM-DD HH:MM'."""
    if isinstance(ts, str) and len(ts) >= 16 and ts[10] in "T ":
        return ts[:10] + " " + ts[11:16]
    return "????-??-?? ??:??"


def project_root(cwd):
    """F39.1: git toplevel of cwd (5 s timeout), else cwd itself."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
    except Exception:
        pass
    return cwd


def inside(path, root_real):
    p = os.path.realpath(path)
    return p == root_real or p.startswith(root_real.rstrip(os.sep) + os.sep)


def secret_spans(text, kv=True):
    """-> merged [[start, end, kind], ...] of the secrets in text (end-exclusive)."""
    spans = []
    for kind, rx, grp in SECRET_RES:
        if not kv and rx is SECRET_RES[-1][1]:
            continue  # kv=False: leave out the key=value form (used on text whose lines were already joined)
        for m in rx.finditer(text):
            if not grp:
                spans.append((m.start(), m.end(), kind))
                continue
            s, e = m.start(grp), m.end(grp)
            if text.startswith(("[REDACTED", MARK), s):
                continue  # already masked: keep the function idempotent
            if kind == "secret":
                if text[s] in "\"'" and e - s >= 2 and text[e - 1] == text[s]:
                    s, e = s + 1, e - 1  # a quoted value: mask what is inside the quotes
                if not _value_ok(text[s:e]):
                    continue
            spans.append((s, e, kind))
    spans.sort(key=lambda x: (x[0], -x[1]))
    merged = []
    for s, e, k in spans:
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, k])
    return merged


def redact(text, kv=True):
    """F65: mask secrets as [REDACTED:<kind>]. Text without secrets is returned unchanged."""
    if not text:
        return text
    spans = secret_spans(text, kv)
    if not spans:
        return text
    out, pos = [], 0
    for s, e, k in spans:
        out.append(text[pos:s])
        out.append(mark_for(k))
        pos = e
    out.append(text[pos:])
    return "".join(out)


def redacted_snippet(text, s, e, ctx=120):
    """Redact the whole text first (matching ran on the raw text), then cut the window around the
    match mapped into the redacted text, so a secret split by the window edge never leaks."""
    spans = secret_spans(text)
    if not spans:
        red, ns, ne = text, s, e
    else:
        def pos(i, end_side):
            delta = 0
            for a, b, k in spans:
                if b <= i:
                    delta += len(mark_for(k)) - (b - a)
                elif a < i < b:
                    return a + delta + (len(mark_for(k)) if end_side else 0)
                else:
                    break
            return i + delta
        red = redact(text)
        ns, ne = pos(s, False), pos(e, True)
    snip = red[max(0, ns - ctx):ne + ctx]
    return re.sub(r"\r?\n", " / ", snip)


# ---------------------------------------------------------------- transcripts

def strip_reminders(t):
    return REMINDER_RE.sub("", t).strip()


def typed_ok(t):
    t = t.strip()
    if not t:
        return False
    return not t.startswith(WRAPPER_PREFIXES)


def result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def segments(rec):
    """F44.3: record -> list of (role, text); role in user|assistant|tool."""
    typ = rec.get("type")
    if typ not in ("user", "assistant"):
        return []
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    out = []
    if typ == "user":
        if rec.get("isMeta") or rec.get("isCompactSummary"):
            return []
        typed, tools = [], []
        if isinstance(c, str):
            typed.append(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    typed.append(b["text"])
                elif b.get("type") == "tool_result":
                    tools.append(result_text(b.get("content")))
        t = strip_reminders("\n".join(typed))
        if typed_ok(t):
            out.append(("user", t))
        for x in tools:
            x = strip_reminders(x)
            if x:
                out.append(("tool", x))
        return out
    # assistant
    texts, tools = [], []
    if isinstance(c, str):
        texts.append(c)
    elif isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                texts.append(b["text"])
            elif b.get("type") == "tool_use":
                inp = b.get("input")
                try:
                    s = json.dumps(inp, ensure_ascii=False) if not isinstance(inp, str) else inp
                except Exception:
                    s = str(inp)
                tools.append("%s %s" % (b.get("name") or "tool", s))
    t = strip_reminders("\n".join(texts))
    if t:
        out.append(("assistant", t))
    for x in tools:
        out.append(("tool", x))
    return out


def parse_line(line):
    try:
        d = json.loads(line)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


class Transcript:
    def __init__(self, path, sub):
        self.path = path
        self.sub = sub
        stem = os.path.basename(path)[:-len(".jsonl")]
        if sub and stem.startswith("agent-"):
            stem = stem[len("agent-"):]
        self.sid = stem
        self.sid8 = stem[:8]
        # F57.b: a subagent transcript has the id form <sid8>/<agent8>; self.sid stays the agent id (index key)
        self.agent = stem if sub else None
        self.agent8 = stem[:8] if sub else None
        self.parent = os.path.basename(os.path.dirname(os.path.dirname(path))) if sub else None
        self.label = ("%s/%s" % (self.parent[:8], stem[:8])) if sub else stem[:8]
        try:
            self.mtime = os.stat(path).st_mtime
        except OSError:
            self.mtime = 0

    def records(self, want=None):
        """Yield (lineno, rec) for parseable records; `want`: optional set of line numbers."""
        try:
            f = open(self.path, encoding="utf-8", errors="replace")
        except OSError:
            return
        with f:
            for n, line in enumerate(f, 1):
                if want is not None and n not in want:
                    continue
                rec = parse_line(line)
                if rec is not None:
                    yield n, rec

    def visible(self, rec, include_sub):
        if rec.get("isSidechain") and not (include_sub or self.sub):
            return False
        return True


# ---------------------------------------------------------------- scoping + cache

def load_index():
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_index(idx):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = INDEX_FILE + ".%d.tmp" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        os.replace(tmp, INDEX_FILE)
    except Exception:
        pass


def first_cwd(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n > 3000:
                    break
                if '"cwd"' not in line:
                    continue
                rec = parse_line(line)
                if rec and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                    return rec["cwd"]
    except OSError:
        pass
    return ""


def in_scope_transcripts(root, include_sub):
    root_real = os.path.realpath(root)
    idx = load_index()
    dirty = False
    files = [(p, False) for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))]
    if include_sub:
        files += [(p, True) for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*", "subagents", "*.jsonl"))]
    res = []
    for p, sub in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        ent = idx.get(p)
        if not (isinstance(ent, dict) and ent.get("mtime") == st.st_mtime and ent.get("size") == st.st_size):
            ent = {"mtime": st.st_mtime, "size": st.st_size, "cwd": first_cwd(p)}
            idx[p] = ent
            dirty = True
        cwd = ent.get("cwd")
        if cwd and inside(cwd, root_real):
            res.append(Transcript(p, sub))
    if dirty:
        for k in [k for k in idx if not os.path.exists(k)]:
            del idx[k]
        save_index(idx)
    return res


def select_session(ts, sess):
    if not sess or sess == "all":
        return ts
    return [t for t in ts if t.sid.startswith(sess) or t.sid8.startswith(sess[:8])
            or (t.parent and t.parent.startswith(sess))]


# ---------------------------------------------------------------- search

SAFE_RE = re.compile(r"^[A-Za-z0-9_ @:/=,|()?*+.#%&'<>!~;\[\]-]+$")
SAFE_FIXED_RE = re.compile(r"^[\x20\x21\x23-\x5b\x5d-\x7e]+$")  # printable ASCII except " and \
DOT_REPL = r"(?:\\u[0-9a-fA-F]{4}(?:\\u[0-9a-fA-F]{4})?|\\.|.)"


def rg_candidates(files, pattern, fixed):
    """Return {path: set(lineno)} of lines rg thinks may match, or None to use the full scan."""
    rg = shutil.which("rg")
    if not rg or not files:
        return None
    if fixed:
        if not SAFE_FIXED_RE.match(pattern):
            return None
        args = ["-F", "-e", pattern]
    else:
        if not SAFE_RE.match(pattern):
            return None
        args = ["-e", pattern.replace(".", DOT_REPL)]
    hits = {}
    paths = [t.path for t in files]
    for i in range(0, len(paths), 400):
        chunk = paths[i:i + 400]
        try:
            r = subprocess.run([rg, "-i", "-n", "--no-heading", "--with-filename", "--no-messages",
                                "--max-columns", "0"] + args + ["--"] + chunk,
                               capture_output=True, timeout=300)
        except Exception:
            return None
        if r.returncode > 1:
            return None
        # stdout lines: path:lineno:content (content may be huge; we only need the head)
        for raw in r.stdout.split(b"\n"):
            if not raw:
                continue
            m = re.match(rb"(.+?\.jsonl):(\d+):", raw)
            if m:
                hits.setdefault(m.group(1).decode("utf-8", "replace"), set()).add(int(m.group(2)))
    return hits


# ---------------------------------------------------------------- exclusions, whole tokens, key history (F73, F74)

AR_HEADER = "Lossless Handoff auto-recall:"
CURRENT_CUTOFF = None  # F74.c: {realpath: line}; set by recall-mcp.py and auto-recall.py, never by the CLI
HISTORY_CAP = 4000
_TOK_RE = re.compile(r"[A-Za-z0-9_./-]")


def boundary_ok(text, s, e):
    """F73: the characters next to text[s:e] are not token characters [A-Za-z0-9_./-]. A final `.` that is not followed
    by a token character (a sentence end) is allowed."""
    if s > 0 and _TOK_RE.match(text[s - 1]):
        return False
    if e < len(text):
        c = text[e]
        if c == "." and (e + 1 >= len(text) or not _TOK_RE.match(text[e + 1])):
            return True
        if _TOK_RE.match(c):
            return False
    return True


def ident_rx(ident):
    return re.compile(re.escape(ident), re.I)


def whole_matches(rx, text, limit=50):
    """-> (first match or None, first whole-token match or None)."""
    first = None
    for i, m in enumerate(rx.finditer(text)):
        if first is None:
            first = m
        if boundary_ok(text, m.start(), m.end()):
            return first, m
        if i >= limit:
            break
    return first, None


def _is_recall_use(b):
    if not isinstance(b, dict) or b.get("type") != "tool_use":
        return False
    name = b.get("name")
    if isinstance(name, str) and name.startswith("mcp__lossless-recall__"):
        return True
    inp = b.get("input")
    cmd = inp.get("command") if isinstance(inp, dict) else None
    return name == "Bash" and isinstance(cmd, str) and "recall.py" in cmd


def recall_tool_ids(path):
    """Ids of the tool calls of recall itself in one transcript (F73.a)."""
    ids = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "recall.py" not in line and "lossless-recall" not in line:
                    continue
                rec = parse_line(line)
                c = ((rec or {}).get("message") or {}).get("content") if isinstance(rec, dict) else None
                if isinstance(c, list):
                    for b in c:
                        if _is_recall_use(b) and isinstance(b.get("id"), str):
                            ids.add(b["id"])
    except OSError:
        pass
    return ids


def strip_recall_blocks(rec, ids):
    msg = rec.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    if not ids or not isinstance(c, list):
        return rec
    keep = [b for b in c if not (isinstance(b, dict) and ((b.get("type") == "tool_use" and b.get("id") in ids)
                                                          or (b.get("type") == "tool_result" and b.get("tool_use_id") in ids)))]
    if len(keep) == len(c):
        return rec
    r = dict(rec)
    m = dict(msg)
    m["content"] = keep
    r["message"] = m
    return r


def _norm(t):
    return re.sub(r"\s+", " ", t or "").strip().lower()


class Clean:
    """F73/F74 exclusions: (a) recall's own tool calls and results, (b) auto-recall injections, (c) the current prompt and
    everything after it (`cutoff`), (d) duplicates (same uuid, then same text and timestamp)."""

    def __init__(self, cutoff=None):
        self.cutoff = cutoff or {}
        self.ids = {}
        self.cut = {}
        self.seen_u, self.seen_t = set(), set()

    def segments(self, t, n, rec):
        cut = self.cut.get(t.path)
        if cut is None:
            cut = self.cut[t.path] = self.cutoff.get(os.path.realpath(t.path))
        if cut is not None and n >= cut:
            return []
        if t.path not in self.ids:
            self.ids[t.path] = recall_tool_ids(t.path)
        out = []
        for role, text in segments(strip_recall_blocks(rec, self.ids[t.path])):
            if text.lstrip().startswith(AR_HEADER):
                continue
            uid = rec.get("uuid")
            if isinstance(uid, str) and uid:
                k = (uid, role, text)
                if k in self.seen_u:
                    continue
                self.seen_u.add(k)
            k = (rec.get("timestamp") or "", role, _norm(text))
            if k in self.seen_t:
                continue
            self.seen_t.add(k)
            out.append((role, text))
        return out


def current_cutoff(ts, transcript_path=None, prompt=None):
    """F73.c: {realpath: line} of the last user prompt of the current session. Without a path: the newest main transcript
    in scope. With a prompt text: only when the last user message is that prompt. Else {}."""
    try:
        path = transcript_path
        if not path:
            mains = [t for t in ts if not t.sub]
            if not mains:
                return {}
            path = max(mains, key=lambda t: t.mtime).path
        if not os.path.isfile(path):
            return {}
        last = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if '"user"' not in line:
                    continue
                rec = parse_line(line)
                if rec is None or rec.get("type") != "user":
                    continue
                for role, text in segments(rec):
                    if role == "user":
                        last = (n, text)
        if last is None:
            return {}
        if prompt is not None:
            p, x = _norm(prompt), _norm(last[1])
            if not p or not x or not (x in p or p[:80] in x):
                return {}
        return {os.path.realpath(path): last[0]}
    except Exception:
        return {}


SENT_RE = re.compile(r"(?<=[.?!])[ \t]+|\n+")


def history_entries(ts, ident, role="any", cutoff=None):
    """F73: -> list of entries (dicts with ts, path, line, label, role, text), oldest first."""
    rx = ident_rx(ident)
    cand = rg_candidates(ts, ident, True)
    clean = Clean(cutoff)
    out = []
    for t in ts:
        want = None
        if cand is not None:
            want = cand.get(t.path)
            if not want:
                continue
        for n, rec in t.records(want):
            if not t.visible(rec, True):
                continue
            for r, text in clean.segments(t, n, rec):
                if role != "any" and r != role:
                    continue
                sents = [x for x in SENT_RE.split(text) if x.strip()]
                idx = [i for i, x in enumerate(sents) if any(boundary_ok(x, m.start(), m.end()) for m in rx.finditer(x))]
                wins = []
                for i in idx:
                    a, b = max(0, i - 1), min(len(sents) - 1, i + 1)
                    if wins and a <= wins[-1][1]:
                        wins[-1][1] = b
                    else:
                        wins.append([a, b])
                for a, b in wins:
                    out.append({"ts": rec.get("timestamp") or "", "path": t.path, "line": n, "label": t.label, "role": r,
                                "text": " ".join(sents[a:b + 1])})
    out.sort(key=lambda e: e["ts"])  # stable: ties keep the file order
    return out


def _cut(s, n):
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def history_lines(ident, entries, cap=HISTORY_CAP):
    """F73: the output lines. `cap` None: no cap. Over the cap: line 1, the 2 oldest entries, an omission line, and as
    many of the newest entries as fit."""
    n = len(entries)
    if n == 0:
        return ["history of %s: no mentions" % ident]
    l1 = ("history of %s: %d mention(s), oldest first. A later mention can be a rejected proposal, a dry run or an undo; "
          "read all of them." % (ident, n))
    rows = ["- %s:%d %s %s: %s" % (e["label"], e["line"], fmt_ts(e["ts"]), e["role"], redact(_norm_keep(e["text"])))
            for e in entries]
    if cap is None or len("\n".join([l1] + rows)) <= cap:
        return [l1] + rows
    for lim in (600, 300, 150, 80, 40):
        rr = [_row_cut(entries[i], rows[i], lim) for i in range(n)]
        if len("\n".join([l1] + rr)) <= cap:
            return [l1] + rr
        if n < 3:
            continue
        for m in range(n - 2 - 1, -1, -1):
            k = n - 2 - m
            om = "[%d mention(s) omitted in the middle; run: python3 %s history %s --all]" % (k, _recall_cmd(), ident)
            lines = [l1] + rr[:2] + [om] + (rr[n - m:] if m else [])
            if len("\n".join(lines)) <= cap:
                return lines
    return ["\n".join([l1] + rows)[:cap]]


def _norm_keep(t):
    return re.sub(r"\s+", " ", t or "").strip()


def _row_cut(e, row, lim):
    head = row[:len(row) - len(redact(_norm_keep(e["text"])))]
    return head + _cut(redact(_norm_keep(e["text"])), lim)


def history_for(ts, ident, role="any", cutoff=None, cap=HISTORY_CAP):
    """-> (lines, number of mentions)."""
    ents = history_entries(ts, ident, role, cutoff)
    return history_lines(ident, ents, cap), len(ents)


def cmd_history(a, root):
    ident = (a.identifier or "").strip()
    if not ident:
        raise UsageError("identifier must not be empty")
    ts = in_scope_transcripts(root, True)
    lines, _n = history_for(ts, ident, a.role, CURRENT_CUTOFF, None if a.all else HISTORY_CAP)
    return lines


def keyword_hits(a, ts, rx, pattern, fixed):
    """F44.1 keyword hits, newest first, one per (message line, role). Each: dict."""
    cand = rg_candidates(ts, pattern, fixed)
    clean = Clean(CURRENT_CUTOFF)
    hits = []
    for t in ts:
        want = None
        if cand is not None:
            want = cand.get(t.path)
            if not want:
                continue
        for n, rec in t.records(want):
            if not t.visible(rec, a.subagents):
                continue
            for role, text in clean.segments(t, n, rec):
                if a.role != "any" and role != a.role:
                    continue
                first, whole = whole_matches(rx, text)
                if first is None:
                    continue
                m = whole or first
                hits.append({"ts": rec.get("timestamp") or "", "path": t.path, "line": n, "sid8": t.label,
                             "agent8": t.agent8, "sid": t.sid, "role": role, "tag": "word", "whole": whole is not None,
                             "snippet": redacted_snippet(text, m.start(), m.end())})
    hits.sort(key=lambda h: (h["ts"], h["path"], h["line"]), reverse=True)
    seen, uniq = set(), []
    for h in hits:
        k = (h["path"], h["line"], h["role"])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return uniq


LOOSE_PLAIN_RE = re.compile(r"^[A-Za-z0-9 _.-]+$")


def loose_pattern(pattern):
    """F59.a: a plain pattern with a space, - or _ gets loose separators. Else the pattern is unchanged."""
    if LOOSE_PLAIN_RE.match(pattern) and re.search(r"[ _-]", pattern):
        return re.sub(r"[ _-]+", "[ _-]+", pattern)
    return pattern


def tool_use_blocks(rec):
    """F59.b: (tool_use id, hit text) of each tool_use block of an assistant record. Same text form as `segments`."""
    msg = rec.get("message")
    c = msg.get("content") if isinstance(msg, dict) else None
    out = []
    if rec.get("type") == "assistant" and isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                inp = b.get("input")
                try:
                    s = json.dumps(inp, ensure_ascii=False) if not isinstance(inp, str) else inp
                except Exception:
                    s = str(inp)
                out.append((b["id"], "%s %s" % (b.get("name") or "tool", s)))
    return out


def add_result_pointers(shown, ts, rx):
    """F59.b: set h["result"] on a tool hit from a tool_use block when a later line holds its tool_result.
    Reads only the transcripts of the shown tool hits. Never raises."""
    try:
        by_path = {t.path: t for t in ts}
        groups = {}
        for h in shown:
            if h.get("role") == "tool" and h["path"] in by_path:
                groups.setdefault(h["path"], []).append(h)
        for path, hs in groups.items():
            t = by_path[path]
            want = {}  # tool_use id -> hit
            lines = {h["line"] for h in hs}
            for n, rec in t.records(lines):
                blocks = tool_use_blocks(rec)
                pick = [i for i, x in blocks if rx.search(x)] or [i for i, _x in blocks[:1]]
                for h in hs:
                    if h["line"] == n:
                        for i in pick:
                            want.setdefault(i, h)
            if not want:
                continue
            first = min(h["line"] for h in want.values())
            try:
                f = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                for n, raw in enumerate(f, 1):
                    if n <= first or "tool_use_id" not in raw:
                        continue
                    rec = parse_line(raw)
                    msg = rec.get("message") if rec else None
                    c = msg.get("content") if isinstance(msg, dict) else None
                    if rec and rec.get("type") == "user" and isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                h = want.get(b.get("tool_use_id"))
                                if h is not None and n > h["line"] and "result" not in h:
                                    h["result"] = "%s:%d" % (t.label, n)
                    if all("result" in h for h in want.values()):
                        break
    except Exception:
        pass


def fmt_hit(h):
    s = "%s:%d %s %s [%s]%s: %s" % (h["sid8"], h["line"], fmt_ts(h["ts"]), h["role"], h["tag"],
                                    (" [agent %s]" % h["agent8"]) if h.get("agent8") else "", h["snippet"])
    s += (" -> result %s" % h["result"]) if h.get("result") else ""
    return s + (" [node %s]" % h["node"] if h.get("node") else "")


def tag_nodes(shown, root):
    """F54.c: ` [node <id>]` / ` [node live]` on each hit. Syncs only the transcripts that have hits. Never raises."""
    try:
        ldir = ledger_dir_for(root)
        for p in {h["path"] for h in shown}:
            try:
                sync_transcript(ldir, p, 3.0)
            except Exception:
                pass
        idx = leaf_index(read_nodes(ldir))
        for h in shown:
            h["node"] = node_tag(idx, h["path"], h["line"])
    except Exception:
        pass


def cmd_search(a, root):
    limit = max(1, min(a.limit, 100))
    if a.keyword and a.meaning:
        raise UsageError("--keyword and --meaning are exclusive")
    fixed = a.fixed
    pattern = a.pattern
    if not fixed:
        pattern = loose_pattern(pattern)  # F59.a
    try:
        rx = re.compile(re.escape(pattern) if fixed else pattern, re.I)
    except re.error as e:
        if a.keyword:
            raise UsageError("bad pattern: %s" % e)
        fixed = True  # F44.9: not a valid regex -> fixed string for the keyword part
        rx = re.compile(re.escape(pattern), re.I)
    node = None
    if a.node:  # F54.c: only hits inside one leaf's line range
        node = find_node(root, a.node)
        if node.get("agent"):
            a.subagents = True  # a node of a subagent transcript is always searchable
    ts = select_session(in_scope_transcripts(root, a.subagents), a.session)
    if node:
        ts = [t for t in ts if os.path.realpath(t.path) == os.path.realpath(node["transcript"])]

    mode_line, mhits, off_reason = None, [], None
    if a.keyword:
        off_reason = "--keyword"
    elif a.role not in ("any", "user", "assistant"):
        off_reason = "role %s is not indexed" % a.role
    else:
        mhits, mode_line, off_reason = meaning_hits(a, root, ts)
    if off_reason is not None:
        mode_line = "mode: keyword only (meaning search off: %s)" % off_reason

    if a.meaning and not off_reason:
        kw = []
    else:
        kw = keyword_hits(a, ts, rx, pattern, fixed)
        if not a.keyword and not off_reason:
            kw = kw[:200]

    if off_reason is not None:
        merged = kw
    else:
        merged = rrf_merge(kw, mhits)
    if node:
        merged = [h for h in merged if os.path.realpath(h["path"]) == os.path.realpath(node["transcript"])
                  and node["first_line"] <= h["line"] <= node["last_line"]]
    if CURRENT_CUTOFF:  # F74.c: meaning hits inside the current prompt or after it
        merged = [h for h in merged if os.path.realpath(h["path"]) not in CURRENT_CUTOFF
                  or h["line"] < CURRENT_CUTOFF[os.path.realpath(h["path"])]]
    merged = [h for h in merged if h.get("whole")] + [h for h in merged if not h.get("whole")]  # F74: whole tokens first
    shown = merged[:limit]
    fill_meaning_snippets(shown, ts)
    add_result_pointers(shown, ts, rx)
    tag_nodes(shown, root)
    lines = [mode_line]
    lines += [fmt_hit(h) for h in shown]
    lines.append("%d hits shown of %d (%d sessions searched)" % (len(shown), len(merged), len(ts)))
    return lines


# ---------------------------------------------------------------- meaning search (F44.9)

def embed_cfg():
    return {
        "url": (os.environ.get("RECALL_EMBED_URL") or "http://localhost:11434").rstrip("/"),
        "model": os.environ.get("RECALL_EMBED_MODEL") or "bge-m3",
        "backend": (os.environ.get("RECALL_EMBED_BACKEND") or "ollama").lower(),
        "key": os.environ.get("RECALL_EMBED_KEY") or "",
    }


def embed(texts, timeout):
    """-> list of unit-normalised array('f'). Raises RuntimeError with a short reason."""
    cfg = embed_cfg()
    if cfg["backend"] == "openai":
        url = cfg["url"] + "/v1/embeddings"
    else:
        url = cfg["url"] + "/api/embed"
    headers = {"Content-Type": "application/json"}
    if cfg["backend"] == "openai" and cfg["key"]:
        headers["Authorization"] = "Bearer " + cfg["key"]
    payload = {"model": cfg["model"], "input": texts}
    if cfg["backend"] != "openai":
        payload["keep_alive"] = "30m"  # F44.12(b): keep the model loaded between calls
    body = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("embedding backend HTTP %s" % e.code)
    except Exception as e:
        reason = getattr(e, "reason", None) or e
        raise RuntimeError("embedding backend unreachable (%s)" % str(reason)[:120])
    try:
        if cfg["backend"] == "openai":
            vecs = [x["embedding"] for x in d["data"]]
        else:
            vecs = d["embeddings"]
        if len(vecs) != len(texts):
            raise ValueError("count")
        out = []
        for v in vecs:
            arr = array.array("f", v)
            norm = math.sqrt(sum(x * x for x in arr)) or 1.0
            out.append(array.array("f", (x / norm for x in arr)))
        return out
    except Exception:
        raise RuntimeError("embedding backend returned an unexpected reply")


def project_key(root):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(root.rstrip("/")))[:40]
    return base + "-" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]


def index_paths(root):
    d = os.path.join(STATE_DIR, "recall")
    k = project_key(root)
    return d, os.path.join(d, k + ".sqlite"), os.path.join(d, k + ".lock")


def open_db(path, create):
    db = sqlite3.connect(path, timeout=30)
    if create:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);"
            "CREATE TABLE IF NOT EXISTS files(sid TEXT PRIMARY KEY, path TEXT, off INTEGER, lines INTEGER, msgs INTEGER);"
            "CREATE TABLE IF NOT EXISTS chunks(sid TEXT, line INTEGER, chunk INTEGER, ts TEXT, role TEXT, vec BLOB,"
            " PRIMARY KEY(sid, line, chunk));")
    return db


def chunk_text(text, size=CHUNK_MAX):
    """Split on paragraph, then sentence boundaries, into pieces of at most `size` chars."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    pieces = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        for sent in re.split(r"(?<=[.!?])\s+", para):
            while len(sent) > size:
                pieces.append(sent[:size])
                sent = sent[size:]
            if sent.strip():
                pieces.append(sent.strip())
    out, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 2 + len(p) > size:
            out.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        out.append(cur)
    return out


def message_chunks(rec):
    """-> list of (role, chunk_text) for the indexed units of one record (user/assistant text only)."""
    out = []
    for role, text in segments(rec):
        if role not in ("user", "assistant"):
            continue
        for c in chunk_text(redact(text)):
            if len(c) >= MIN_CHUNK:
                out.append((role, c))
    return out


def meaning_hits(a, root, ts):
    """-> (hits, mode_line, off_reason). hits: list of dicts ranked best first (top 50 chunks, one per message)."""
    cfg = embed_cfg()
    _d, dbp, _l = index_paths(root)
    if not os.path.exists(dbp):
        return [], None, "no meaning index yet (run: recall.py index)"
    try:
        db = open_db(dbp, False)
        meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
        if meta.get("model") != cfg["model"] or not meta.get("dim"):
            return [], None, "index was built with model %s, not %s" % (meta.get("model"), cfg["model"])
        dim = int(meta["dim"])
        i = db.execute("SELECT COUNT(DISTINCT sid || ':' || line) FROM chunks").fetchone()[0]
        if not i:
            return [], None, "meaning index is empty (run: recall.py index)"
        known, off_sum, size_sum = 0, 0, 0
        frows = {r[0]: r for r in db.execute("SELECT sid, path, off, lines, msgs FROM files")}
    except sqlite3.Error as e:
        return [], None, "meaning index unreadable (%s)" % str(e)[:80]
    try:
        q = embed([a.pattern], QUERY_TIMEOUT)[0]
    except RuntimeError as e:
        return [], None, str(e)
    if len(q) != dim:
        return [], None, "query vector size %d differs from index size %d" % (len(q), dim)

    by_sid = {t.sid: t for t in ts}
    # coverage: indexed vs known messages, plus an estimate for bytes not yet scanned
    known = sum(r[4] or 0 for sid, r in frows.items() if sid in by_sid)
    done_bytes = sum(r[2] or 0 for sid, r in frows.items() if sid in by_sid)
    todo_bytes = 0
    for sid, t in by_sid.items():
        try:
            size = os.stat(t.path).st_size
        except OSError:
            continue
        todo_bytes += max(0, size - ((frows.get(sid) or (0, 0, 0))[2] or 0))
    est = int(known * todo_bytes / done_bytes) if done_bytes and todo_bytes else 0
    n = max(known + est, i)

    sql = "SELECT sid, line, chunk, ts, role, vec FROM chunks"
    params = []
    if a.role in ("user", "assistant"):
        sql += " WHERE role = ?"
        params.append(a.role)
    dot = getattr(math, "sumprod", None)
    scored = []
    cur = db.execute(sql, params)
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for sid, line, chunk, ts_, role, blob in rows:
            if sid not in by_sid:
                continue
            v = array.array("f")
            v.frombytes(blob)
            if len(v) != dim:
                continue
            s = dot(q, v) if dot else sum(map(operator.mul, q, v))
            scored.append((s, sid, line, chunk, ts_, role))
    db.close()
    scored.sort(key=lambda x: x[0], reverse=True)
    hits, seen = [], set()
    for s, sid, line, chunk, ts_, role in scored[:MEANING_TOP]:
        k = (sid, line, role)
        if k in seen:
            continue
        seen.add(k)
        t = by_sid[sid]
        hits.append({"ts": ts_ or "", "path": t.path, "line": line, "sid8": t.label, "agent8": t.agent8, "sid": sid, "role": role,
                     "tag": "meaning", "chunk": chunk, "snippet": None})
    mode = "mode: hybrid (meaning index covers %d of %d messages)" % (i, n)
    return hits, mode, None


def rrf_merge(kw, mh, k=60):
    """Reciprocal-rank fusion, equal weights; a message found both ways counts once ([both])."""
    scores, items = {}, {}
    for rank, h in enumerate(kw):
        key = (h["path"], h["line"], h["role"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        items[key] = dict(h)
    for rank, h in enumerate(mh):
        key = (h["path"], h["line"], h["role"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        if key in items:
            items[key]["tag"] = "both"
        else:
            items[key] = dict(h)
    order = sorted(items, key=lambda x: (scores[x], items[x]["ts"], x[0], x[1]), reverse=True)
    return [items[x] for x in order]


def fill_meaning_snippets(shown, ts):
    """Meaning-only hits carry no text yet: re-read the transcript line and take the matched chunk."""
    need = {}
    for h in shown:
        if h.get("snippet") is None:
            need.setdefault(h["path"], {})[h["line"]] = h
    for t in ts:
        m = need.get(t.path)
        if not m:
            continue
        for n, rec in t.records(set(m)):
            h = m[n]
            chunks = [c for r, c in message_chunks(rec) if r == h["role"]]
            c = chunks[h["chunk"]] if h.get("chunk", 0) < len(chunks) else (chunks[0] if chunks else "")
            h["snippet"] = re.sub(r"\r?\n", " / ", c[:240])
    for h in shown:
        if h.get("snippet") is None:
            h["snippet"] = "(message no longer readable)"


# ---------------------------------------------------------------- index (F44.9)

def cmd_index(a, root):
    t0 = time.time()
    deadline = t0 + a.max_seconds if a.max_seconds else None
    d, dbp, lockp = index_paths(root)
    os.makedirs(d, exist_ok=True)
    lockf = open(lockp, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return ["index busy"]
    cfg = embed_cfg()
    db = open_db(dbp, True)
    meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
    if meta.get("model") != cfg["model"] and meta:
        db.executescript("DELETE FROM chunks; DELETE FROM files; DELETE FROM meta;")
        db.commit()
        meta = {}
    dim = int(meta["dim"]) if meta.get("dim") else None
    ts = in_scope_transcripts(root, True)  # F57.c: main chats and subagent transcripts
    ts.sort(key=lambda t: t.mtime, reverse=True)
    frows = {r[0]: r for r in db.execute("SELECT sid, path, off, lines, msgs FROM files")}
    total, skipped = 0, None
    avg_bytes = 4000.0
    stopped = False
    remaining = 0

    def rest_estimate(from_idx):
        b = 0
        for t in ts[from_idx:]:
            try:
                size = os.stat(t.path).st_size
            except OSError:
                continue
            b += max(0, size - ((frows.get(t.sid) or (0, 0, 0))[2] or 0))
        return int(b / avg_bytes)

    for ti, t in enumerate(ts):
        try:
            size = os.stat(t.path).st_size
        except OSError:
            continue
        row = frows.get(t.sid)
        off, lines, msgs = (row[2] or 0, row[3] or 0, row[4] or 0) if row else (0, 0, 0)
        if size < off:  # truncated or replaced: start over for this transcript
            db.execute("DELETE FROM chunks WHERE sid = ?", (t.sid,))
            off, lines, msgs = 0, 0, 0
        if size == off:
            continue
        # scan new complete lines into items
        items = []  # (line, ts, role, chunk_i, text, end_off)
        pos, n = off, lines
        with open(t.path, "rb") as f:
            f.seek(off)
            for raw in f:
                end = pos + len(raw)
                n += 1
                if not raw.endswith(b"\n"):
                    rec = parse_line(raw.decode("utf-8", "replace"))
                    if rec is None:  # incomplete last line: leave for the next run
                        n -= 1
                        break
                pos = end
                if (b'"user"' not in raw and b'"assistant"' not in raw):
                    continue
                rec = parse_line(raw.decode("utf-8", "replace"))
                if not rec or (rec.get("isSidechain") and not t.sub):
                    continue
                cs = message_chunks(rec)
                if not cs:
                    continue
                msgs_seen = {}
                for role, c in cs:
                    ci = msgs_seen.get(role, 0)
                    msgs_seen[role] = ci + 1
                    items.append((n, rec.get("timestamp") or "", role, len(items), c, pos))
        new_end_line, new_end_pos = n, pos
        # chunk index must be per (line, role)
        per, fixed_items = {}, []
        for (ln, tsx, role, _gi, c, endp) in items:
            ci = per.get((ln, role), 0)
            per[(ln, role)] = ci + 1
            fixed_items.append((ln, tsx, role, ci, c, endp))
        items = fixed_items
        new_msgs = len({(it[0], it[2]) for it in items})
        if not items:
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (t.sid, t.path, new_end_pos, new_end_line, msgs))
            db.commit()
            frows[t.sid] = (t.sid, t.path, new_end_pos, new_end_line, msgs)
            continue
        pending = len(items)
        i = 0
        while i < len(items):
            if deadline and time.time() >= deadline:
                stopped = True
                break
            batch = items[i:i + BATCH]
            try:
                vecs = embed([b[4] for b in batch], INDEX_TIMEOUT)
            except RuntimeError as e:
                skipped = str(e)
                break
            if dim is None:
                dim = len(vecs[0])
                db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (cfg["model"],))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(dim),))
            elif len(vecs[0]) != dim:  # model dimension changed: rebuild
                db.executescript("DELETE FROM chunks; DELETE FROM files; DELETE FROM meta;")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (cfg["model"],))
                dim = len(vecs[0])
                db.execute("INSERT OR REPLACE INTO meta VALUES ('dim', ?)", (str(dim),))
                frows.clear()
                skipped = "embedding size changed; index reset, run again"
                break
            db.executemany("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)",
                           [(t.sid, b[0], b[3], b[1], b[2], v.tobytes()) for b, v in zip(batch, vecs)])
            i += len(batch)
            total += len(batch)
            # advance the offset to the end of the last line whose chunks are all stored
            done_line = items[i - 1][0] if i >= len(items) or items[i][0] != items[i - 1][0] else None
            if i >= len(items):
                row_off, row_lines = new_end_pos, new_end_line
            elif done_line is not None:
                row_off, row_lines = items[i - 1][5], done_line
            else:
                row_off, row_lines = None, None
            if row_off is not None:
                share = new_msgs * i // len(items) if i < len(items) else new_msgs
                db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                           (t.sid, t.path, row_off, row_lines, msgs + share))
                frows[t.sid] = (t.sid, t.path, row_off, row_lines, msgs + share)
            db.commit()
            if (total // BATCH) % 10 == 0 and i < len(items):
                print("indexed %d chunks, %d remaining" % (total, pending - i + rest_estimate(ti + 1)), flush=True)
        if skipped or stopped:
            remaining = pending - i + rest_estimate(ti + 1)
            break
        avg_bytes = max(500.0, (size - off) / max(1, pending))
    db.commit()
    db.close()
    out = []
    if skipped:
        out.append("meaning index skipped: %s" % skipped)
    out.append("indexed %d chunks, %d remaining" % (total, remaining))
    out.append("%.1f chunks/s over %.1f s" % (total / max(0.001, time.time() - t0), time.time() - t0)) if total else None
    return [x for x in out if x]


# ---------------------------------------------------------------- index --all / session-end (F44.13)

def git_toplevel(path):
    """Git toplevel of path, or None when path is not inside a git repo."""
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
    except Exception:
        pass
    return None


def discover_projects():
    """[(toplevel, newest_mtime)] for every Claude Code project folder whose cwd still exists and is in a git repo,
    deduplicated, newest activity first."""
    found = {}
    for pd in glob.glob(os.path.join(PROJECTS_DIR, "*")):
        files = []
        for f in glob.glob(os.path.join(pd, "*.jsonl")):
            try:
                files.append((os.stat(f).st_mtime, f))
            except OSError:
                pass
        if not files:
            continue
        mt, newest = max(files)
        cwd = None
        try:
            with open(newest, "rb") as fh:
                for raw in fh:
                    rec = parse_line(raw.decode("utf-8", "replace"))
                    if isinstance(rec, dict) and isinstance(rec.get("cwd"), str) and rec["cwd"].strip():
                        cwd = rec["cwd"]
                        break
        except OSError:
            continue
        if not cwd or not os.path.isdir(cwd):
            continue
        top = git_toplevel(cwd)
        if not top:
            continue
        found[top] = max(found.get(top, 0), mt)
    return sorted(found.items(), key=lambda kv: kv[1], reverse=True)


def cmd_index_all(a):
    t0 = time.time()
    cap = a.max_seconds if a.max_seconds else 3000
    projects = discover_projects()
    n, k = len(projects), 0
    out, skipped = [], None
    for top, _mt in projects:
        left = cap - (time.time() - t0)
        if left <= 0:
            out.append("stopped: time cap, %d of %d projects done" % (k, n))
            break
        sub = argparse.Namespace(max_seconds=left, cwd=top, cmd="index", all=False)
        try:
            lines = cmd_index(sub, top)
        except Exception as e:
            out.append("%s: index error: %s" % (top, e))
            continue
        sk = [x for x in lines if x.startswith("meaning index skipped:")]
        if sk:
            skipped = sk[0]
            break
        out.append("%s: %s" % (top, "; ".join(lines)))  # F44.13(a): one line per project
        if lines != ["index busy"]:
            k += 1
    if skipped:
        out.append(skipped)
    out.append("indexed %d of %d projects" % (k, n))
    return out


def log_hook_error(exc):
    """One JSON line in <state>/hooks-error.jsonl. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "hooks-error.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "hook": "recall.py session-end", "version": "1.8.0", "sid": None,
                                "error": "%s: %s" % (type(exc).__name__, exc)}) + "\n")
    except Exception:
        pass


def cmd_session_end():
    """SessionEnd hook: start a detached `index --cwd <cwd>`; print nothing; always exit 0."""
    try:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        d = json.loads(raw) if raw.strip() else None
        cwd = d.get("cwd") if isinstance(d, dict) else None
        if not isinstance(cwd, str) or not cwd.strip() or not os.path.isdir(cwd):
            return 0
        rdir = os.path.join(STATE_DIR, "recall")
        os.makedirs(rdir, exist_ok=True)
        log = open(os.path.join(rdir, "index.log"), "ab")
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "index", "--cwd", cwd, "--max-seconds", "600"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
        finally:
            log.close()
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            log_hook_error(e)
    return 0


# ---------------------------------------------------------------- summary nodes (F54)

NODES_FILE, NODES_STATE, NODES_LOCK = "nodes.jsonl", "nodes.state.json", "nodes.lock"
BOUNDARY_MARK = b"compact_boundary"
SCAN_STEP = 8 << 20


class NotFound(Exception):
    pass


def ledger_dir_for(root):
    """The project ledger folder (the folder that holds decisions.md), same key as F39.1."""
    return os.path.join(STATE_DIR, "projects", project_key(root))


def transcript_names(path):
    """-> (session id, agent id or None) from a transcript path."""
    stem = os.path.basename(path)
    stem = stem[:-len(".jsonl")] if stem.endswith(".jsonl") else stem
    parent = os.path.dirname(path)
    if os.path.basename(parent) == "subagents":
        sid = os.path.basename(os.path.dirname(parent))
        return sid, (stem[len("agent-"):] if stem.startswith("agent-") else stem)
    return stem, None


def node_id(session, uuid):
    return "n" + hashlib.sha1(("%s:%s" % (session, uuid)).encode("utf-8")).hexdigest()[:10]


def is_boundary_rec(o):
    return isinstance(o, dict) and (o.get("subtype") == "compact_boundary" or o.get("type") == "compact_boundary")


def read_nodes(ldir):
    """All valid leaf nodes in nodes.jsonl (a malformed line is skipped)."""
    out, seen = [], set()
    try:
        with open(os.path.join(ldir, NODES_FILE), encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if (isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("first_line"), int)
                        and isinstance(o.get("last_line"), int) and isinstance(o.get("transcript"), str)
                        and o["id"] not in seen):
                    seen.add(o["id"])
                    out.append(o)
    except OSError:
        pass
    return out


def scan_boundaries(path, start_off=0, start_line=1, upto=None):
    """Boundary records at or after byte start_off (a line start whose number is start_line).
    -> (list of (lineno, record, line_start_offset), end_off, end_line, tail) where end_off is the offset after the last
    complete line, end_line the number of the next line, and tail = (lineno, record) for an unterminated final line
    that parses as a boundary (else None). Counts newlines at C speed; parses only lines holding the marker."""
    found, carry_off, line_no, tail = [], start_off, start_line, None
    with open(path, "rb") as f:
        f.seek(start_off)
        buf, buf_off = b"", start_off
        while True:
            chunk = f.read(SCAN_STEP)
            if not chunk and not buf:
                break
            data = buf + chunk
            last_nl = data.rfind(b"\n")
            if not chunk:  # end of file: whatever is left has no trailing newline
                if data.strip():
                    if BOUNDARY_MARK in data:
                        try:
                            o = json.loads(data)
                        except Exception:
                            o = None
                        if is_boundary_rec(o):
                            tail = (line_no, o)
                break
            if last_nl < 0:
                buf = data
                continue
            body = data[:last_nl + 1]
            pos = 0
            while True:
                i = body.find(BOUNDARY_MARK, pos)
                if i < 0:
                    break
                ls = body.rfind(b"\n", 0, i) + 1
                le = body.find(b"\n", i)
                ln = line_no + body.count(b"\n", 0, ls)
                try:
                    o = json.loads(body[ls:le])
                except Exception:
                    o = None
                if is_boundary_rec(o):
                    found.append((ln, o, buf_off + ls))
                pos = le + 1
            line_no += body.count(b"\n")
            buf_off += len(body)
            buf = data[last_nl + 1:]
    return found, buf_off, line_no, tail


def head_sig(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read(4096)).hexdigest()
    except OSError:
        return ""


def handoff_for(session, agent, btime):
    """Path of the timestamped handoff dump written for this compaction (newest at or before the boundary), else None."""
    key = session if not agent else "%s-a-%s" % (session, agent)
    best = None
    try:
        for p in glob.glob(os.path.join(STATE_DIR, glob.escape(key) + "-????????T??????Z.md")):
            m = re.search(r"-(\d{8}T\d{6}Z)\.md$", p)
            if not m:
                continue
            try:
                t = datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
                b = datetime.datetime.fromisoformat(str(btime).replace("Z", "+00:00"))
                if b.tzinfo is None:
                    b = b.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if t <= b + datetime.timedelta(seconds=5) and (best is None or t > best[0]):
                best = (t, p)
    except Exception:
        pass
    return best[1] if best else None


def open_nodes_lock(ldir, wait):
    os.makedirs(ldir, exist_ok=True)
    lf = open(os.path.join(ldir, NODES_LOCK), "a+")
    end = time.time() + wait
    while True:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lf
        except OSError:
            if time.time() >= end:
                lf.close()
                return None
            time.sleep(0.05)


def sync_transcript(ldir, tpath, wait=10.0):
    """F54.b: append a leaf node for each new compact_boundary of one transcript, reading from the saved byte offset.
    Returns the number of nodes added (0 when nothing is new or the lock is busy). Raises only on a real error."""
    rpath = os.path.realpath(tpath)
    if not os.path.isfile(rpath):
        return 0
    lf = open_nodes_lock(ldir, wait)
    if lf is None:
        return 0
    try:
        sp = os.path.join(ldir, NODES_STATE)
        try:
            with open(sp, encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        size = os.path.getsize(rpath)
        sig = head_sig(rpath)
        ent = state.get(rpath)
        fresh = {"offset": 0, "line": 1, "seq": 0, "prev_line": 0, "prev_id": None, "sig": sig}
        if not isinstance(ent, dict) or ent.get("offset", 0) > size or ent.get("sig") != sig:
            ent = dict(fresh)  # new, shorter or replaced transcript: scan from the start; add only missing ids
        if ent["offset"] == size:
            if state.get(rpath) != ent:
                state[rpath] = ent
                write_state(sp, state)
            return 0
        known = {n["id"] for n in read_nodes(ldir)}
        found, end_off, end_line, tail = scan_boundaries(rpath, ent["offset"], ent["line"])
        session, agent = transcript_names(rpath)
        seq, prev_line, prev_id = ent["seq"], ent["prev_line"], ent["prev_id"]
        new_lines, commit = [], dict(ent)
        items = [(ln, o) for ln, o, _ in found]
        committed_n = len(items)
        if tail:
            items.append(tail)
        for k, (ln, o) in enumerate(items):
            seq += 1
            uuid = o.get("uuid") if isinstance(o.get("uuid"), str) and o.get("uuid") else "line-%d" % ln
            nid = node_id(session, uuid)
            cm = o.get("compactMetadata") if isinstance(o.get("compactMetadata"), dict) else {}
            if nid not in known:
                known.add(nid)
                pt = cm.get("preTokens")
                new_lines.append(json.dumps({
                    "id": nid, "kind": "leaf", "session": session, "agent": agent, "transcript": os.path.abspath(tpath),
                    "seq": seq, "boundary_uuid": uuid, "time": o.get("timestamp") if isinstance(o.get("timestamp"), str) else None,
                    "trigger": cm.get("trigger") if isinstance(cm.get("trigger"), str) else None,
                    "pre_tokens": pt if isinstance(pt, int) and not isinstance(pt, bool) else None,
                    "first_line": prev_line + 1, "last_line": ln - 1, "prev": prev_id,
                    "handoff": handoff_for(session, agent, o.get("timestamp")),
                }, ensure_ascii=False))
            prev_line, prev_id = ln, nid
            if k == committed_n - 1:
                commit.update(seq=seq, prev_line=prev_line, prev_id=prev_id)
        if new_lines:
            with open(os.path.join(ldir, NODES_FILE), "a", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
        commit.update(offset=end_off, line=end_line, sig=sig)
        state[rpath] = commit
        write_state(sp, state)
        return len(new_lines)
    finally:
        lf.close()


def write_state(sp, state):
    tmp = "%s.%d.tmp" % (sp, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, sp)


def project_transcripts(root):
    return in_scope_transcripts(root, True)


def sync_project(root):
    """Sync every transcript of the project (main chats and subagents). -> nodes added."""
    ldir = ledger_dir_for(root)
    added = 0
    for t in project_transcripts(root):
        try:
            added += sync_transcript(ldir, t.path)
        except Exception:
            continue
    return added


def leaf_index(nodes):
    """realpath(transcript) -> leaves sorted by first_line."""
    idx = {}
    for n in nodes:
        idx.setdefault(os.path.realpath(n["transcript"]), []).append(n)
    for v in idx.values():
        v.sort(key=lambda n: n["first_line"])
    return idx


def node_tag(idx, path, line):
    leaves = idx.get(os.path.realpath(path))
    if not leaves:
        return None
    for n in leaves:
        if n["first_line"] <= line <= n["last_line"]:
            return n["id"]
    if line > leaves[-1]["last_line"]:
        return "live"
    return None


def summary_of(node):
    """Full Claude Code summary text (the isCompactSummary record after the boundary), or ''."""
    path = node.get("transcript")
    b = node["last_line"] + 1
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if n <= b:
                    continue
                if n > b + 60:
                    break
                if '"isCompactSummary"' not in line:
                    if "compact_boundary" in line:
                        rec = parse_line(line)
                        if is_boundary_rec(rec):
                            break
                    continue
                rec = parse_line(line)
                if not rec or not rec.get("isCompactSummary"):
                    continue
                msg = rec.get("message")
                c = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
    except OSError:
        pass
    return ""


HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s+|\*\*)?(?:\d+\.\s+)?(.*?)\**:?\**\s*$")
NEXT_HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s+\S.*|\**\d+\.\s+[A-Z][A-Za-z /&'()-]{2,50}:\**\s*)$")


def gist_of(summary):
    if not summary.strip():
        return "(no summary)"
    lines = summary.splitlines()
    for i, ln in enumerate(lines):
        if "current work" in ln.lower() and len(ln) < 120 and (NEXT_HEAD_RE.match(ln) or ln.strip().startswith(("#", "*"))):
            body = []
            for x in lines[i + 1:]:
                if NEXT_HEAD_RE.match(x) and body:
                    break
                if NEXT_HEAD_RE.match(x):
                    break
                body.append(x)
            text = " ".join(" ".join(body).split())
            if not text:
                text = " ".join(ln.split(":", 1)[1].split()) if ":" in ln else ""
            if text:
                return redact(text)[:160]
    rest = " ".join(" ".join(lines[1:]).split()) or " ".join(lines[0].split())
    return redact(rest)[:160] or "(no summary)"


def cmd_nodes(a, root):
    if not a.backfill:
        raise UsageError("nodes needs --backfill")
    added = sync_project(root)
    return ["nodes: %d added, %d total" % (added, len(read_nodes(ledger_dir_for(root))))]


def cmd_map(a, root):
    sync_project(root)
    nodes = read_nodes(ledger_dir_for(root))
    if a.session and a.session != "all":
        nodes = [n for n in nodes if str(n.get("session", "")).startswith(a.session) or str(n.get("session", ""))[:8].startswith(a.session[:8])]
    if a.limit < 1:
        raise UsageError("--limit must be >= 1")
    nodes.sort(key=lambda n: (str(n.get("time") or ""), n["id"]))
    nodes = nodes[-a.limit:]
    if not nodes:
        return ["no summary nodes yet"]
    first_time = {}
    for n in nodes:
        first_time.setdefault(n["session"], str(n.get("time") or ""))
    nodes.sort(key=lambda n: (first_time[n["session"]], n["session"], str(n.get("time") or ""), n.get("seq", 0)))
    # one pass per transcript: read the lines after each boundary for the gist
    by_path = {}
    for n in nodes:
        by_path.setdefault(n["transcript"], []).append(n)
    gists = {}
    for p, ns in by_path.items():
        for n in ns:
            gists[n["id"]] = gist_of(summary_of(n))
    out = []
    for n in nodes:
        who = n["session"][:8] + ("/" + n["agent"][:8] if n.get("agent") else "")
        pt = n.get("pre_tokens")
        out.append("%s %s %s #%s lines %d-%d %s tokens: %s" % (
            n["id"], fmt_ts(n.get("time")), who, n.get("seq"), n["first_line"], n["last_line"],
            pt if pt is not None else "?", gists[n["id"]]))
    out.append("%d nodes in %d sessions" % (len(nodes), len({n["session"] for n in nodes})))
    return out


def find_node(root, nid):
    sync_project(root)
    for n in read_nodes(ledger_dir_for(root)):
        if n["id"] == nid:
            return n
    raise NotFound("no node %s" % nid)


def cmd_describe(a, root):
    n = find_node(root, a.id)
    pt = n.get("pre_tokens")
    out = ["id: %s" % n["id"], "session: %s" % n["session"], "agent: %s" % (n.get("agent") or "none"),
           "seq: %s" % n.get("seq"), "time: %s" % (n.get("time") or "unknown"),
           "trigger: %s" % (n.get("trigger") or "unknown"),
           "lines: %d-%d" % (n["first_line"], n["last_line"]),
           "pre_tokens: %s" % (pt if pt is not None else "unknown"),
           "prev: %s" % (n.get("prev") or "none"), "handoff: %s" % (n.get("handoff") or "none"),
           "open: python3 %s open %s:%d" % (_recall_cmd(), n["session"][:8], n["first_line"]), ""]
    out.append(redact(summary_of(n)) or "(no summary found in the transcript)")
    return out


# ---------------------------------------------------------------- open / user

def cap_body(text, full, mid=None, start=0):
    """Body text capped at 4,000 chars (40,000 with full), starting at char `start`. A cut body ends with a marker
    naming the next offset (F44.12(a))."""
    cap = MSG_CAP_FULL if full else 4000
    seg = text[start:start + cap]
    end = start + len(seg)
    if end >= len(text):
        return seg
    return seg + "\n[cut: %d more chars; continue with: recall.py open %s --full --from %d]" % (len(text) - end, mid or "<id>", end)


def rec_message(rec):
    segs = segments(rec)
    if not segs:
        return None
    role = segs[0][0]
    parts = []
    for i, (r, t) in enumerate(segs):
        parts.append(t if i == 0 or r == role and r != "tool" else "[%s] %s" % (r, t))
    return role, "\n".join(parts)


def heading(t, n, rec, role):
    return "--- %s:%d %s %s" % (t.label, n, fmt_ts(rec.get("timestamp")), role)


def cmd_open(a, root):
    m = re.match(r"^([A-Za-z0-9_-]+)(?:/([A-Za-z0-9_-]+))?:(\d+)$", a.id)
    if not m:
        raise UsageError("id must look like <sid8>:<line> or <sid8>/<agent8>:<line>")
    sid, agent, line = m.group(1), m.group(2), int(m.group(3))
    if a.around < 0:
        raise UsageError("--around must be >= 0")
    around = min(a.around, 10)
    ts = in_scope_transcripts(root, True)
    # prefer main transcripts over subagent ones for the same prefix
    if agent:  # F57.b
        match = [t for t in ts if t.sub and t.parent.startswith(sid) and t.agent.startswith(agent)]
    else:  # also the older id form <agent8>:<line> of a subagent hit
        match = [t for t in ts if t.sid.startswith(sid)]
        mains = [t for t in match if not t.sub]
        match = mains or match
    if len(match) != 1:
        raise UsageError("unknown or ambiguous session id %s" % sid)
    t = match[0]
    msgs = []  # (lineno, rec, role, text)
    for n, rec in t.records():
        mm = rec_message(rec) if t.visible(rec, True) else None
        if mm:
            msgs.append((n, rec, mm[0], mm[1]))
    pos = next((i for i, x in enumerate(msgs) if x[0] == line), None)
    if pos is None:
        raise UsageError("no message at %s:%d" % (sid, line))
    out = []
    if a.from_ is not None:
        if a.from_ < 0:
            raise UsageError("--from must be >= 0")
        n, rec, role, text = msgs[pos]
        text = redact(text)
        mid = "%s:%d" % (t.label, n)
        seg_end = min(len(text), a.from_ + MSG_CAP_FULL)
        out.append("%s [chars %d-%d of %d]" % (heading(t, n, rec, role), min(a.from_, len(text)), seg_end, len(text)))
        out.append(cap_body(text, True, mid, a.from_))
        return out
    for n, rec, role, text in msgs[max(0, pos - around):pos + around + 1]:
        out.append(heading(t, n, rec, role))
        out.append(cap_body(redact(text), a.full, "%s:%d" % (t.label, n)))
    return out


def cmd_user(a, root):
    n_last = a.last
    if n_last < 1:
        raise UsageError("--last must be >= 1")
    n_last = min(n_last, 200)
    ts = select_session(in_scope_transcripts(root, False), a.session)
    ts.sort(key=lambda t: t.mtime, reverse=True)
    found = []  # (timestamp, path, line, transcript, rec, text)
    for t in ts:
        if len(found) >= n_last:
            kth = sorted((f[0] for f in found), reverse=True)[n_last - 1]
            iso = datetime.datetime.fromtimestamp(t.mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            if kth and iso < kth[:19]:  # nothing in this or later (older) files can beat the kth newest
                break
        try:
            f = open(t.path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for n, line in enumerate(f, 1):
                if '"user"' not in line:
                    continue
                rec = parse_line(line)
                if not rec or rec.get("type") != "user" or not t.visible(rec, False):
                    continue
                for role, text in segments(rec):
                    if role == "user":
                        found.append((rec.get("timestamp") or "", t.path, n, t, rec, text))
    found.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out = []
    for ts_, _p, n, t, rec, text in found[:n_last]:
        out.append(heading(t, n, rec, "user"))
        out.append(cap_body(redact(text), False))
    return out


# ---------------------------------------------------------------- handoffs

def cmd_handoffs(a, root):
    root_real = os.path.realpath(root)
    rows = []
    for p in glob.glob(os.path.join(STATE_DIR, "*.md")):
        base = os.path.basename(p)
        if not (base.endswith("-latest.md") or base.endswith("-agent.md")
                or re.search(r"-\d{8}T\d{6}Z\.md$", base)):
            continue
        cwd = None
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 40:
                        break
                    if line.startswith("- cwd:"):
                        cwd = line[len("- cwd:"):].strip()
                        break
            st = os.stat(p)
        except OSError:
            continue
        if cwd and inside(cwd, root_real):
            rows.append((st.st_mtime, p, st.st_size))
    rows.sort(reverse=True)
    return ["%s %s %d" % (datetime.datetime.fromtimestamp(m, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"), p, s)
            for m, p, s in rows]


# ---------------------------------------------------------------- main

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def build_parser():
    ap = Parser(prog="recall.py", description="Search this project's earlier chats word for word.")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    def common(p):
        p.add_argument("--cwd", default=None)

    p = sub.add_parser("search")
    p.add_argument("pattern")
    p.add_argument("--fixed", action="store_true")
    p.add_argument("--role", choices=["user", "assistant", "tool", "any"], default="any")
    p.add_argument("--session", default="all")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--subagents", action="store_true")  # F57.a: accepted, changes nothing
    p.add_argument("--no-subagents", dest="no_subagents", action="store_true")
    p.add_argument("--keyword", action="store_true")
    p.add_argument("--meaning", action="store_true")
    p.add_argument("--node", default=None)
    common(p)
    p = sub.add_parser("history")
    p.add_argument("identifier")
    p.add_argument("--role", choices=["user", "assistant", "any"], default="any")
    p.add_argument("--all", action="store_true")
    common(p)
    p = sub.add_parser("open")
    p.add_argument("id")
    p.add_argument("--around", type=int, default=2)
    p.add_argument("--full", action="store_true")
    p.add_argument("--from", dest="from_", type=int, default=None)
    common(p)
    p = sub.add_parser("user")
    p.add_argument("--last", type=int, default=20)
    p.add_argument("--session", default="all")
    common(p)
    p = sub.add_parser("index")
    p.add_argument("--max-seconds", type=float, default=0)
    p.add_argument("--all", action="store_true")
    common(p)
    p = sub.add_parser("map")
    p.add_argument("--session", default="all")
    p.add_argument("--limit", type=int, default=200)
    common(p)
    p = sub.add_parser("describe")
    p.add_argument("id")
    common(p)
    p = sub.add_parser("nodes")
    p.add_argument("--backfill", action="store_true")
    common(p)
    p = sub.add_parser("session-end")
    p = sub.add_parser("handoffs")
    common(p)
    return ap


def emit(lines, cap=OUT_CAP):
    text = "\n".join(lines)
    if len(text) > cap:
        text = text[:cap] + "\n" + CUT_MARK.replace("12,000", "{:,}".format(cap))
    sys.stdout.write(text + ("\n" if text else ""))


def main(argv):
    try:
        a = build_parser().parse_args(argv)
        if a.cmd == "search":
            a.subagents = not a.no_subagents  # F57.a: subagent transcripts are in scope by default
        if a.cmd == "session-end":
            return cmd_session_end()
        if a.cmd == "index" and a.all:
            emit(cmd_index_all(a))
            return 0
        cwd = os.path.expanduser(a.cwd) if a.cwd else os.getcwd()
        root = project_root(cwd)
        fn = {"search": cmd_search, "open": cmd_open, "user": cmd_user, "index": cmd_index, "handoffs": cmd_handoffs,
              "map": cmd_map, "describe": cmd_describe, "nodes": cmd_nodes, "history": cmd_history}[a.cmd]
        big = a.cmd == "open" and (a.full or a.from_ is not None)
        emit(fn(a, root), 10 ** 9 if a.cmd == "history" and a.all else OPEN_FULL_CAP if big else OUT_CAP)
        return 0
    except UsageError as e:
        sys.stderr.write("recall.py: %s\n" % e)
        return 2
    except NotFound as e:  # F54.c: `no node <id>`, exit 1
        sys.stdout.write("%s\n" % e)
        sys.stderr.write("%s\n" % e)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
