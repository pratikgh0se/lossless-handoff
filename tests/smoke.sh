#!/usr/bin/env bash
# Smoke test: install, run the three hooks on synthetic data, uninstall. Uses a temporary HOME.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME/.claude"
export HANDOFF_HAIKU_FALLBACK=0
export LOSSLESS_HANDOFF_MANAGED_SETTINGS="$T/no-managed.json"   # never read the real managed settings file
export LOSSLESS_HANDOFF_NO_LAUNCHCTL=1   # never load or unload jobs in the real launchd from a temp HOME
export RECALL_EMBED_URL="http://127.0.0.1:9"   # closed port: recall and the detached indexer stay keyword-only
# no ollama on PATH: drop any PATH entry that holds one, so the install never pulls a model
NP=""; IFS=: read -ra _pd <<<"$PATH"
for d in "${_pd[@]}"; do [ -x "$d/ollama" ] || NP="${NP:+$NP:}$d"; done
export PATH="$NP"
unset CLAUDE_CODE_AUTO_COMPACT_WINDOW HANDOFF_STATE_DIR HANDOFF_HOOK_CHILD || true
H="$HOME/.claude/hooks"; ST="$HOME/.local/state/claude-handoffs"; S="$HOME/.claude/settings.json"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }

cat > "$S" <<'JSON'
{
  "model": "opus",
  "cleanupPeriodDays": 365,
  "note": "café ☃",
  "env": {"FOO": "1"},
  "permissions": {"allow": ["Bash(ls:*)"]},
  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}],
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo hello"}]}]}
}
JSON
cp "$S" "$T/original.json"

"$ROOT/install.sh" --no-nightly --dry-run >/dev/null
cmp -s "$S" "$T/original.json" || fail "dry-run changed settings"
"$ROOT/install.sh" --nightly --dry-run >"$T/dry-nightly.out"
grep -Eq "handoff-reconcile|--reconcile --prove --days 30" "$T/dry-nightly.out" || fail "dry-run does not mention the nightly reconcile job"
grep -q "install hook: .*/lossless_handoff/ledger.py" "$T/dry-nightly.out" || fail "dry-run does not list the lossless_handoff package files"
[ ! -e "$HOME/Library/LaunchAgents" ] || fail "dry-run wrote a LaunchAgent"
[ ! -e "$H/claims.json" ] || fail "dry-run installed files"
ok "dry-run changes nothing"

"$ROOT/install.sh" --no-nightly --window 400000 >"$T/install1.out"
cp "$S" "$T/after1.json"
"$ROOT/install.sh" --no-nightly --window 400000 >"$T/install2.out"
cmp -s "$S" "$T/after1.json" || fail "second install changed settings"
ls "$S".bak-* >/dev/null 2>&1 || fail "no settings backup"
ok "install is idempotent"

python3 - "$S" "$H" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8")); h = sys.argv[2]
assert d["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000" and d["env"]["FOO"] == "1"
assert d["note"] == "café ☃"
assert d["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo hi"
assert [x["command"] for x in d["hooks"]["PreCompact"][0]["hooks"]] == ["python3 %s/precompact-handoff.py" % h]
ss = {g["matcher"]: [x["command"] for x in g["hooks"]] for g in d["hooks"]["SessionStart"]}
assert sorted(ss) == ["compact", "resume", "startup"], ss
pi = "python3 %s/postcompact-inject.py" % h
assert ss["compact"] == [pi + " --part 1", pi + " --part 2", pi + " --part 3", pi + " --ledger --part 1", pi + " --ledger --part 2", pi + " --ledger --part 3"], ss
led3 = [pi + " --ledger --part %d" % n for n in (1, 2, 3)]
chk = pi + " --check"
assert ss["startup"] == ["echo hello"] + led3 + [chk] and ss["resume"] == led3 + [chk], ss
assert all(x["timeout"] == 10 for g in d["hooks"]["SessionStart"] for x in g["hooks"] if "inject" in x["command"] and "--check" not in x["command"])
assert all(x["timeout"] == 5 for g in d["hooks"]["SessionStart"] for x in g["hooks"] if x["command"].endswith("--check"))
assert ss["compact"].count(chk) == 0
assert d["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"] == 5
assert "Edit(~/.local/state/claude-handoffs/**)" in d["permissions"]["allow"]
assert d["permissions"]["allow"].count("Bash(python3 ~/.claude/hooks/recall.py:*)") == 1
assert "Bash(ls:*)" in d["permissions"]["allow"]
PY
[ -f "$H/recall.py" ] || fail "recall.py not installed"
[ -f "$H/claims.json" ] || fail "claims.json not installed"
python3 - "$H" <<'PY'
import hashlib, json, sys
h = sys.argv[1]
m = json.load(open(h + "/.handoff-manifest.json"))
assert m["version"] == "1.21.0" and m["installed_at"], m
pkg = ["lossless_handoff/%s.py" % n for n in "__init__ transcript summary ledger handoff runtime redaction pipeline reconcile prove claims".split()]
assert sorted(m["files"]) == sorted(["claims.json", "handoff-nudge.py", "postcompact-inject.py", "precompact-handoff.py", "recall.py"] + pkg), m
for n, want in m["files"].items():
    assert hashlib.sha256(open(h + "/" + n, "rb").read()).hexdigest() == want, n
mk = json.load(open(h + "/.claude-handoff-install.json"))
assert mk["check_added"] is True and mk["manifest_written"] is True, mk
PY
ok "claims.json installed, manifest written (hashes match), marker records them"
grep -q "building in the background" "$T/install1.out" || fail "install did not say the first index is building"
grep -q -- "--semantic" "$T/install1.out" || fail "install did not print the opt-in hint"
grep -q "nightly jobs: not selected" "$T/install1.out" || fail "install did not report skipping the nightly job"
python3 - "$S" "$H" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8")); h = sys.argv[2]
se = d["hooks"]["SessionEnd"]
assert [(x["command"], x["timeout"]) for g in se for x in g["hooks"]] == [("python3 %s/recall.py session-end" % h, 5)], se
m = json.load(open(h + "/.claude-handoff-install.json"))
assert m["session_end_added"] is True, m
PY
[ ! -e "$HOME/Library/LaunchAgents/com.lossless-handoff.recall-index.plist" ] || fail "--no-nightly still wrote a LaunchAgent"
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -e "$ST/recall/index-all.log" ] && break; sleep 1; done
[ -e "$ST/recall/index-all.log" ] || fail "first index did not start (no index-all.log)"
ok "SessionEnd hook registered and recorded, ollama hint, no nightly job, first index started"
# --check: a clean install prints nothing; a hook file changed since install is reported
o="$(python3 "$H/postcompact-inject.py" --check 2>&1)" || fail "--check exited non-zero"
[ -z "$o" ] || fail "--check printed output on a clean install: $o"
cp "$H/claims.json" "$T/claims.keep"; printf ' ' >> "$H/claims.json"
o="$(python3 "$H/postcompact-inject.py" --check 2>&1)" || fail "--check exited non-zero on a changed file"
grep -q "^HANDOFF CHECK: .*claims.json differs from the installed manifest" <<<"$o" || fail "--check missed a changed file: $o"
cp "$T/claims.keep" "$H/claims.json"
[ -z "$(python3 "$H/postcompact-inject.py" --check 2>&1)" ] || fail "--check still reports after restoring the file"
ok "--check: silent on a clean install, reports a changed hook file"
# reconcile and prove on an empty HOME: run and exit 0
EH="$T/emptyhome"; mkdir -p "$EH"
HOME="$EH" python3 "$H/precompact-handoff.py" --reconcile >"$T/rec.out" 2>&1 || fail "--reconcile exited non-zero on an empty HOME: $(cat "$T/rec.out")"
HOME="$EH" python3 "$H/precompact-handoff.py" --prove >"$T/prove.out" 2>&1 || fail "--prove exited non-zero on an empty HOME: $(cat "$T/prove.out")"
HOME="$EH" python3 "$H/precompact-handoff.py" --reconcile --prove --days 30 >/dev/null 2>&1 || fail "--reconcile --prove --days 30 exited non-zero"
HOME="$EH" python3 "$H/precompact-handoff.py" --claims >/dev/null 2>&1 || fail "--claims exited non-zero"
ok "--reconcile and --prove run and exit 0 on an empty HOME"
# SessionEnd hook: empty input, silent, exit 0
o="$(printf '{}' | python3 "$H/recall.py" session-end 2>&1)" || fail "session-end exited non-zero"
[ -z "$o" ] || fail "session-end printed output: $o"
o="$(python3 "$H/recall.py" index --all 2>&1)" || fail "index --all exited non-zero"
grep -q "indexed" <<<"$o" || fail "index --all: expected 'indexed': $o"
ok "recall.py session-end is silent with {}, index --all exits 0"
# --yes with a fake ollama pulls bge-m3
mkdir -p "$T/fakebin"; printf '#!/bin/sh\necho "$@" >> "%s/ollama.calls"\n' "$T" > "$T/fakebin/ollama"; chmod +x "$T/fakebin/ollama"
PATH="$T/fakebin:$PATH" "$ROOT/install.sh" --no-nightly --semantic --yes >/dev/null
grep -qx "pull bge-m3" "$T/ollama.calls" || fail "--yes did not run ollama pull bge-m3"
PATH="$T/fakebin:$PATH" "$ROOT/install.sh" --no-nightly --semantic >"$T/nontty.out" </dev/null
[ "$(wc -l < "$T/ollama.calls")" -eq 1 ] || fail "non-interactive run without --yes pulled the model"
grep -q "ollama pull bge-m3" "$T/nontty.out" || fail "non-interactive run did not print the pull hint"
ok "ollama: --yes pulls bge-m3, non-interactive skips with a hint"
ok "settings content (compact, startup and resume ledger commands)"

# PreCompact hook on a tiny transcript
TR="$T/t.jsonl"
python3 - "$TR" <<'PY'
import json, sys
rows = [
 {"type": "user", "message": {"content": "Please refactor the parser"}},
 {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."},
   {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/x.py"}}],
   "usage": {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
]
open(sys.argv[1], "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
printf '{"session_id":"smoke1","cwd":"%s","transcript_path":"%s","trigger":"manual"}' "$T" "$TR" \
  | HANDOFF_LEDGER=0 python3 "$H/precompact-handoff.py" >"$T/pre.out" 2>"$T/pre.err"
[ "$(head -1 "$T/pre.out")" = "Lossless Handoff guidance for this summary:" ] && [ ! -s "$T/pre.err" ] || fail "precompact: expected the F53 block on stdout and nothing on stderr"
[ -s "$ST/smoke1-latest.md" ] || fail "no latest handoff"
grep -q "Please refactor the parser" "$ST/smoke1-latest.md" || fail "user message missing from handoff"
[ "$(wc -l < "$ST/health.jsonl")" -eq 1 ] || fail "expected one health line"
python3 -c "import json,sys; json.loads(open(sys.argv[1]).readline())" "$ST/health.jsonl" || fail "bad health line"
grep -q "^- authority: the user's latest words > Project decisions > Agent handoff" "$ST/smoke1-latest.md" || fail "authority line missing"
! grep -q "^## Decisions via questions" "$ST/smoke1-latest.md" || fail "old '## Decisions via questions' section present"
[ "$(wc -c < "$ST/smoke1-latest.md")" -le 14000 ] || fail "handoff over the 14,000 char cap"
ok "PreCompact wrote handoff and health line (authority line, cap 14,000)"

# F38: fresh summary via a fake `claude` shim on PATH
SH="$T/shim"; mkdir -p "$SH"
cat > "$SH/claude" <<'SHIM'
#!/usr/bin/env bash
# Tells the ledger merge call from the summary call by its --system-prompt.
sys=""; prev=""
for a in "$@"; do [ "$prev" = "--system-prompt" ] && sys="$a"; prev="$a"; done
case "$sys" in
  *"decisions ledger"*)
    printf '%s\n' "$@" > "$SHIM_LOG.merge"
    cat > "$SHIM_LOG.merge.stdin"
    [ -n "${MERGE_SLEEP:-}" ] && sleep "$MERGE_SLEEP"
    [ -n "${SHIM_MERGE_OUT:-}" ] && cat "$SHIM_MERGE_OUT"
    exit 0 ;;
esac
printf '%s\n' "$@" > "$SHIM_LOG"
cat > /dev/null
[ -n "${SHIM_SLEEP:-}" ] && sleep "$SHIM_SLEEP"
printf 'SHIM-SUMMARY %.0s' 1 2 3 4 5 6
exit 0
SHIM
chmod +x "$SH/claude"
run_pre() { # $1 = session id; rest = env assignments
  local sid="$1"; shift
  printf '{"session_id":"%s","cwd":"%s","transcript_path":"%s","trigger":"manual"}' "$sid" "$T" "$TR" \
    | env -u HANDOFF_HAIKU_FALLBACK PATH="$SH:$PATH" SHIM_LOG="$T/shim.log" "$@" python3 "$H/precompact-handoff.py" >/dev/null 2>&1
}
python3 -c "print('## Goal\nrefactor\n' + 'x' * 400)" > "$ST/sumsess-agent.md"
rm -f "$T/shim.log"
run_pre sumsess
F="$ST/sumsess-latest.md"
grep -q "^## Fresh summary at compaction (sonnet; verify against facts)" "$F" || fail "summary heading missing"
grep -q "SHIM-SUMMARY" "$F" || fail "summary text missing"
a="$(grep -n '^## Agent handoff' "$F" | head -1 | cut -d: -f1)"; b="$(grep -n '^## Fresh summary' "$F" | head -1 | cut -d: -f1)"
[ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ] || fail "summary not after agent section"
grep -qx -- "--model" "$T/shim.log" && grep -qx "sonnet" "$T/shim.log" || fail "shim not called with --model sonnet"
tail -n 1 "$ST/health.jsonl" | grep -q '"summary_model": *"sonnet"' || fail "health lacks summary_model sonnet"
! grep -q "Haiku fallback" "$F" || fail "old Haiku wording emitted"
ok "F38 summary present after agent section (sonnet)"

rm -f "$T/shim.log"
run_pre sumsess HANDOFF_SUMMARY=0
[ ! -e "$T/shim.log" ] || fail "HANDOFF_SUMMARY=0 still ran claude"
! grep -q "^## Fresh summary" "$F" || fail "HANDOFF_SUMMARY=0 left a summary section"
tail -n 1 "$ST/health.jsonl" | grep -q '"fallback": *"disabled"' || fail "health fallback not disabled"
ok "HANDOFF_SUMMARY=0 disables it"

run_pre sumsess HANDOFF_SUMMARY_MODEL=haiku
grep -q "^## Fresh summary at compaction (haiku; verify against facts)" "$F" || fail "haiku heading missing"
grep -qx "haiku" "$T/shim.log" || fail "shim not called with haiku"
ok "HANDOFF_SUMMARY_MODEL=haiku is used"

s0=$SECONDS
run_pre sumsess HANDOFF_SUMMARY_TIMEOUT=2 SHIM_SLEEP=10
[ $((SECONDS - s0)) -le 7 ] || fail "timeout took too long"
grep -q "^Fresh summary failed: timeout after 2s" "$F" || fail "timeout line missing"
ok "HANDOFF_SUMMARY_TIMEOUT=2 gives 'timeout after 2s' quickly"
rm -f "$ST"/sumsess-*

# F39: per-project decisions ledger
# $PR is a plain folder (no git needed); the ledger key comes from the folder when it is not a repo.
PR="$T/proj"; mkdir -p "$PR"
TR2="$T/t2.jsonl"
python3 - "$TR2" <<'PY'
import json, sys
rows = [{"type": "user", "timestamp": "2026-10-20T10:00:00.000Z", "message": {"content": "Decision: use tabs for indentation, spaces are rejected"}},
        {"type": "assistant", "timestamp": "2026-10-20T10:00:05.000Z", "message": {"content": [{"type": "text", "text": "Understood."}]}}]
open(sys.argv[1], "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
M1="$T/merge1.txt"; M2="$T/merge2.txt"
cat > "$M1" <<'TXT'
# Project decisions
- D1 [2026-10-20] Use tabs for indentation | why: team style
- D2 [2026-10-20] Never push to main without review | why: release rule
TXT
cat > "$M2" <<'TXT'
- D3 [2026-10-21] Push to main only after CI passes | why: new release rule
SUPERSEDE D2 BY D3: replaced
prose that is not a change line
SUPERSEDE D9 BY D3: unknown old id is ignored
TXT
run_led() {
  local sid="$1" cwd="$2" tr="$3" mo="$4"; shift 4
  printf '{"session_id":"%s","cwd":"%s","transcript_path":"%s","trigger":"auto"}' "$sid" "$cwd" "$tr" \
    | env -u HANDOFF_HAIKU_FALLBACK PATH="$SH:$PATH" SHIM_LOG="$T/shim.log" SHIM_MERGE_OUT="$mo" "$@" python3 "$H/precompact-handoff.py" >/dev/null 2>&1
}
ledger_file() { ls "$ST"/projects/*/decisions.md 2>/dev/null | head -1; }
run_led led1 "$PR" "$TR2" "$M1"
LF="$(ledger_file)"
[ -n "$LF" ] || fail "ledger not created under projects/<key>/decisions.md"
case "$LF" in "$ST"/projects/proj-*/decisions.md) ;; *) fail "unexpected ledger path $LF" ;; esac
grep -q "^- D1 \[2026-10-20\] Use tabs" "$LF" && grep -q "^- D2 " "$LF" || fail "ledger lacks D1/D2"
grep -q "decisions ledger" "$T/shim.log.merge" || fail "merge call lacks the ledger system prompt"
grep -q "Decision: use tabs" "$T/shim.log.merge.stdin" || fail "merge stdin lacks the conversation"
tail -n 1 "$ST/health.jsonl" | grep -q '"ledger": *"ok"' || fail "health lacks ledger ok"
[ -s "$T/shim.log" ] && grep -q "SHIM-SUMMARY" "$ST/led1-latest.md" || fail "summary call did not run alongside the merge"
ok "ledger created for a project folder; merge and summary calls told apart"

# second compaction (F40 delta): model outputs only D3 and a SUPERSEDE of D2
python3 - "$TR2" <<'PY'
import json, sys
r = {"type": "user", "timestamp": "2026-10-21T09:00:00.000Z", "message": {"content": "Decision: push to main only after CI passes"}}
open(sys.argv[1], "a").write(json.dumps(r) + "\n")
PY
run_led led1 "$PR" "$TR2" "$M2"
grep -q "^- D1 \[2026-10-20\] Use tabs" "$LF" || fail "D1 lost after a delta merge"
grep -q "^- D2 \[2026-10-20\] SUPERSEDED by D3" "$LF" || fail "D2 not marked superseded"
grep -q "^- D3 " "$LF" || fail "D3 not added"
tail -n 1 "$ST/health.jsonl" | grep -q '"ledger_restored": *0' || fail "health lacks ledger_restored 0"
tail -n 1 "$ST/health.jsonl" | grep -q '"ledger_error": *""' || fail "health lacks empty ledger_error"
! grep -q "D9" "$LF" || fail "invalid SUPERSEDE line was applied"
ls "$(dirname "$LF")"/decisions.[0-9]*T*Z.md >/dev/null 2>&1 || fail "no backup of the previous ledger"
ok "delta merge applies valid changes, ignores invalid ones, deletes nothing"

# State files are private: mode 0600 (handoff, health log, decisions list, state file, backup)
mode_of() { python3 -c "import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])" "$1"; }
for f in "$ST/smoke1-latest.md" "$ST/health.jsonl" "$LF" "$(dirname "$LF")/decisions.state.json" \
  "$(ls "$(dirname "$LF")"/decisions.[0-9]*T*Z.md | head -1)"; do
  [ "$(mode_of "$f")" = "600" ] || fail "state file not mode 600: $f ($(mode_of "$f"))"
done
ok "handoff, health log, decisions list, state file and backup have mode 600"

# F39.3: one merge cursor for each session (parallel chats in one project)
PX="$T/curproj"; mkdir -p "$PX"
cat > "$T/mk_tr.py" <<'PY'
import json, sys
f, a = sys.argv[1], sys.argv[2:]
rows = [{"type": "user", "timestamp": a[i], "message": {"content": a[i + 1]}} for i in range(0, len(a), 2)]
open(f, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
cat > "$T/cursors.py" <<'PY'
import json, sys
mode, path = sys.argv[1], sys.argv[2]
if mode == "get":
    print(json.load(open(path)).get("cursors", {}).get(sys.argv[3], ""))
elif mode == "last":
    d = json.load(open(path))
    assert d["lastSid"] == "curB" and d["lastEntryTs"] == "2026-10-20T15:00:00.000Z", d
elif mode == "old":  # old format: no cursors; argv[3] = lastSid
    json.dump({"v": 1, "root": "x", "updatedAt": 1, "lastSid": sys.argv[3], "lastEntryTs": "2026-10-20T14:00:00.000Z"}, open(path, "w"))
elif mode == "many":
    c = {"old%03d" % i: "2026-01-01T00:%02d:%02d.000Z" % (i // 60, i % 60) for i in range(250)}
    json.dump({"v": 1, "root": "x", "updatedAt": 1, "lastSid": "old000", "lastEntryTs": c["old000"], "cursors": c}, open(path, "w"))
elif mode == "check":
    c = json.load(open(path))["cursors"]
    assert len(c) == 200, len(c)
    assert "curD" in c and "old249" in c and "old000" not in c and "old050" not in c and "old051" in c, sorted(c)[:3]
PY
mk_tr() { python3 "$T/mk_tr.py" "$@"; }
mk_tr "$T/cA.jsonl" 2026-10-20T14:00:00.000Z "Decision: alpha rule from chat A"
mk_tr "$T/cB.jsonl" 2026-10-20T10:00:00.000Z "Decision: bravo early rule from chat B" 2026-10-20T15:00:00.000Z "Decision: bravo late rule"
printf '%s\n' "# Project decisions" "- D1 [2026-10-20] Alpha rule | why: a" > "$T/mx1.txt"
printf '%s\n' "- D2 [2026-10-20] Bravo early rule | why: b" > "$T/mx2.txt"
run_led curA "$PX" "$T/cA.jsonl" "$T/mx1.txt"
run_led curB "$PX" "$T/cB.jsonl" "$T/mx2.txt"
grep -q "bravo early rule" "$T/shim.log.merge.stdin" || fail "session B early message lost after session A merged first"
CS="$(ls "$ST"/projects/curproj-*/decisions.state.json | head -1)"
[ "$(python3 "$T/cursors.py" get "$CS" curA)" = "2026-10-20T14:00:00.000Z" ] || fail "cursor of session A not kept"
[ "$(python3 "$T/cursors.py" get "$CS" curB)" = "2026-10-20T15:00:00.000Z" ] || fail "cursor of session B wrong"
python3 "$T/cursors.py" last "$CS" || fail "lastSid/lastEntryTs not written"
ok "each session has its own merge cursor; other cursors are kept"

# old state file (no cursors) for another session must not hide this session's history
python3 "$T/cursors.py" old "$CS" curOther
printf '%s\n' "- D3 [2026-10-20] Bravo late rule | why: c" > "$T/mx3.txt"
run_led curC "$PX" "$T/cB.jsonl" "$T/mx3.txt"
grep -q "bravo early rule" "$T/shim.log.merge.stdin" || fail "old state: other session's project cursor hid the history"
# old state file whose lastSid is this session: the old value still applies
python3 "$T/cursors.py" old "$CS" curC
run_led curC "$PX" "$T/cB.jsonl" "$T/mx3.txt"
! grep -q "bravo early rule" "$T/shim.log.merge.stdin" && grep -q "bravo late rule" "$T/shim.log.merge.stdin" || fail "old state with matching lastSid: cursor not used"
ok "old state file migrates (project cursor used only for its own session)"

# prune: at most 200 cursors, oldest dropped
python3 "$T/cursors.py" many "$CS"
mk_tr "$T/cD.jsonl" 2026-10-20T16:00:00.000Z "Decision: delta rule from chat D"
printf '%s\n' "- D4 [2026-10-20] Delta rule | why: d" > "$T/mx4.txt"
run_led curD "$PX" "$T/cD.jsonl" "$T/mx4.txt"
python3 "$T/cursors.py" check "$CS" || fail "cursor prune limit wrong"
ok "cursors are pruned to 200, oldest first"

# F40.2: own ledger timeout, health ledger_error
PT="$T/toproj"; mkdir -p "$PT"
s0=$SECONDS
run_led led4 "$PT" "$TR2" "$M1" HANDOFF_LEDGER_TIMEOUT=2 MERGE_SLEEP=10
[ $((SECONDS - s0)) -le 12 ] || fail "ledger timeout took too long"
tail -n 1 "$ST/health.jsonl" | grep -q '"ledger": *"failed"' && tail -n 1 "$ST/health.jsonl" | grep -q '"ledger_error": *"timeout"' || fail "health lacks ledger failed/timeout"
ok "HANDOFF_LEDGER_TIMEOUT=2 gives ledger_error timeout"

# inject: compact parts and startup/resume
inj() { printf '{"cwd":"%s"}' "$PR" | python3 "$H/postcompact-inject.py" --ledger; }
inj_ctx() { python3 -c "import json,sys; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])"; }
o="$(inj | inj_ctx)"
grep -q "D1 " <<<"$o" && grep -q "D3 " <<<"$o" || fail "plain --ledger lacks entries"
! grep -q "D2 " <<<"$o" || fail "plain --ledger shows a superseded entry"
o="$(printf '{"cwd":"%s"}' "$PR" | python3 "$H/postcompact-inject.py" --ledger --part 1 | inj_ctx)"
grep -q "part 1 of the ledger" <<<"$o" && grep -q "D3 " <<<"$o" || fail "--ledger --part 1 lacks entries"
[ -z "$(printf '{"cwd":"%s"}' "$PR" | python3 "$H/postcompact-inject.py" --ledger --part 2)" ] || fail "--ledger --part 2 should be empty for a short ledger"
# a big ledger spills into part 2 and plain mode is cut to 4,000 chars
PB="$T/bigproj"; mkdir -p "$PB"
printf '{"session_id":"big","cwd":"%s","transcript_path":"%s","trigger":"auto"}' "$PB" "$TR2" \
  | env PATH="$SH:$PATH" SHIM_LOG="$T/shim.log" SHIM_MERGE_OUT="$M1" python3 "$H/precompact-handoff.py" >/dev/null 2>&1
BF="$(ls -d "$ST"/projects/bigproj-* | head -1)/decisions.md"; [ -f "$BF" ] || fail "big project ledger missing"
python3 - "$BF" <<'PY'
import sys
with open(sys.argv[1], "w") as f:
    f.write("# Project decisions\n")
    for i in range(1, 41):
        f.write("- D%d [2026-10-20] %s | why: %s\n" % (i, "d" * 300, "w" * 250))
PY
p1="$(printf '{"cwd":"%s"}' "$PB" | python3 "$H/postcompact-inject.py" --ledger --part 1 | inj_ctx)"
p2="$(printf '{"cwd":"%s"}' "$PB" | python3 "$H/postcompact-inject.py" --ledger --part 2 | inj_ctx)"
p3="$(printf '{"cwd":"%s"}' "$PB" | python3 "$H/postcompact-inject.py" --ledger --part 3 | inj_ctx)"
[ "${#p1}" -lt 10000 ] && [ "${#p2}" -lt 10000 ] && [ "${#p3}" -lt 10000 ] || fail "ledger part over 10,000 chars"
grep -q "D40 " <<<"$p1" && grep -q "part 2 of the ledger" <<<"$p2" || fail "part 2 content wrong"
# nothing is dropped: every one of the 40 entries appears in parts 1-3 (in full or as an index line)
allp="$p1 $p2 $p3"
for i in $(seq 1 40); do grep -q "D$i " <<<"$allp" || fail "entry D$i missing from the 3 parts"; done
ok "--ledger --part 1/2/3 and plain --ledger print the entries, nothing dropped"

# git: the ledger is keyed on the repo root, so a subfolder reaches the same file
if command -v git >/dev/null 2>&1; then
  GR="$T/gitrepo"; mkdir -p "$GR/sub"; git -C "$GR" init -q
  run_led led2 "$GR/sub" "$TR2" "$M1"
  ls -d "$ST"/projects/gitrepo-* >/dev/null 2>&1 || fail "git repo ledger not keyed on the repo root"
  [ "$(ls -d "$ST"/projects/gitrepo-* | wc -l)" -eq 1 ] || fail "more than one ledger for one repo"
  o="$(printf '{"cwd":"%s"}' "$GR" | python3 "$H/postcompact-inject.py" --ledger | inj_ctx)"
  grep -q "D1 " <<<"$o" || fail "repo root and subfolder do not share the ledger"
  ok "git repo: ledger keyed on the repo root"
else
  echo "SKIP: git not installed, git-specific ledger check skipped"
fi

# HANDOFF_LEDGER=0 turns everything off
PO="$T/offproj"; mkdir -p "$PO"
run_led led3 "$PO" "$TR2" "$M1" HANDOFF_LEDGER=0
ls -d "$ST"/projects/offproj-* >/dev/null 2>&1 && fail "HANDOFF_LEDGER=0 still wrote a ledger"
tail -n 1 "$ST/health.jsonl" | grep -q '"ledger": *"disabled"' || fail "health ledger not disabled"
for args in "--ledger" "--ledger --part 1" "--ledger --part 2" "--ledger --part 3"; do
  # shellcheck disable=SC2086
  [ -z "$(printf '{"cwd":"%s"}' "$PR" | HANDOFF_LEDGER=0 python3 "$H/postcompact-inject.py" $args)" ] || fail "HANDOFF_LEDGER=0: inject $args printed output"
done
ok "HANDOFF_LEDGER=0 disables the ledger (no file, no injection)"

# Inject
printf '{"session_id":"smoke1"}' | python3 "$H/postcompact-inject.py" --part 1 >"$T/inj.out"
python3 - "$T/inj.out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))["hookSpecificOutput"]
assert d["hookEventName"] == "SessionStart" and "Please refactor the parser" in d["additionalContext"]
PY
ok "inject --part 1 JSON"

# Inject: a precomputed compaction (F51) keeps its own handoff; a handoff older than the last boundary is recovered
PC="$T/precomp"; mkdir -p "$PC/st"
python3 - "$PC" <<'PY'
import datetime, json, sys
pc = sys.argv[1]
now = datetime.datetime.now(datetime.timezone.utc)
z = lambda m: (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
def mk(name, boundary_min, h_min):
    open(f"{pc}/st/{name}-latest.md", "w").write("## Agent handoff\nPRECOMP-MARK\n")
    json.dump({"ts": z(h_min), "trigger": "auto"}, open(f"{pc}/st/{name}-last-health.json", "w"))
    with open(f"{pc}/{name}.jsonl", "w") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": z(boundary_min)}) + "\n")
mk("p212", 180, 100)
mk("p213", 30, 100)
PY
for n in p212 p213; do
  printf '{"session_id":"%s","transcript_path":"%s/%s.jsonl"}' "$n" "$PC" "$n" \
    | HANDOFF_STATE_DIR="$PC/st" python3 "$H/postcompact-inject.py" --part 1 >"$PC/$n.out"
done
grep -q 'Note: this compaction was prepared at' "$PC/p212.out" && grep -q PRECOMP-MARK "$PC/p212.out" \
  && ! grep -q 'HANDOFF RECOVERED' "$PC/p212.out" || fail "precomputed compaction: expected the Note line and no recovery"
grep -q 'HANDOFF RECOVERED' "$PC/p213.out" || fail "handoff older than the last boundary: expected recovery"
ok "inject: precomputed compaction gets the Note line; an old handoff is recovered"

# Nudge: 310k nudges once, 320k not, 355k once more
mk() { python3 - "$TR" "$1" <<'PY'
import json, sys
row = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}],
       "usage": {"input_tokens": 10, "cache_read_input_tokens": int(sys.argv[2]) - 10, "cache_creation_input_tokens": 0}}}
open(sys.argv[1], "a").write(json.dumps(row) + "\n")
PY
}
nudge() { printf '{"session_id":"nsess","transcript_path":"%s","tool_name":"Bash","tool_input":{}}' "$TR" | CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000 python3 "$H/handoff-nudge.py"; }
mk 310000; o1="$(nudge)"
mk 320000; o2="$(nudge)"
mk 355000; o3="$(nudge)"
[ -n "$o1" ] && [ -z "$o2" ] && [ -n "$o3" ] || fail "nudge pattern wrong (got: ${#o1} ${#o2} ${#o3} chars)"
grep -q "nsess-agent.md" <<<"$o1" || fail "nudge lacks agent path"
grep -q "lossless-handoff PostToolUse hook that the user installed" <<<"$o1" || fail "nudge does not identify its source"
python3 -c "import json,sys; t=json.load(sys.stdin)['hookSpecificOutput']['additionalContext']; assert len(t)<1300, len(t)" <<<"$o1" || fail "nudge too long"
echo "$o1" | python3 -c "import json,sys; assert json.load(sys.stdin)['hookSpecificOutput']['hookEventName']=='PostToolUse'"
grep -q "Parallel work running" <<<"$o1" && ! grep -q "imminent" <<<"$o1" || fail "first nudge: seventh heading missing or wrongly imminent"
grep -q "Compaction is imminent" <<<"$o3" || fail "final nudge at 355k lacks the imminent wording"
ok "nudge fires at 310k, not at 320k, again (final) at 355k"

# Agent write is recorded in meta
printf '{"session_id":"nsess","transcript_path":"%s","tool_name":"Write","tool_input":{"file_path":"%s"}}' "$TR" "$ST/nsess-agent.md" | python3 "$H/handoff-nudge.py" >/dev/null
python3 -c "import json,sys; m=json.load(open(sys.argv[1])); assert m['agentWrittenAt'] and m['nudgeCount']==2" "$ST/nsess-agent.meta.json"
ok "agent write recorded in meta"

# F46: a subagent compaction (agent_id set) writes its own files and leaves the main ones alone
SD="$T/subs"; mkdir -p "$SD/subsess/subagents"
python3 - "$SD" <<'PY'
import json, sys
d = sys.argv[1]
def w(path, text):
    rows = [{"type": "user", "message": {"content": text}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Noted."}]}}]
    open(path, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
w(d + "/subsess.jsonl", "MAINMARK please fix the importer")
w(d + "/subsess/subagents/agent-ag7.jsonl", "SUBMARK summarise the schema")
PY
mainin="$(printf '{"session_id":"subsess","cwd":"%s","transcript_path":"%s","trigger":"auto"}' "$T" "$SD/subsess.jsonl")"
subin="$(printf '{"session_id":"subsess","agent_id":"ag7","agent_type":"general-purpose","cwd":"%s","transcript_path":"%s","trigger":"auto"}' "$T" "$SD/subsess.jsonl")"
printf '%s' "$mainin" | HANDOFF_LEDGER=0 python3 "$H/precompact-handoff.py" >/dev/null 2>&1 || fail "main precompact failed"
[ -s "$ST/subsess-latest.md" ] || fail "main handoff not written"
grep -q "MAINMARK" "$ST/subsess-latest.md" || fail "main handoff lacks its own message"
( cd "$ST" && cksum subsess-latest.md; ls subsess-* | sort ) > "$T/main.before"
printf '%s' "$subin" | HANDOFF_COMPACT_STEER=0 HANDOFF_LEDGER=0 python3 "$H/precompact-handoff.py" >"$T/sub.out" 2>"$T/sub.err" || fail "subagent precompact exited non-zero"
[ ! -s "$T/sub.out" ] && [ ! -s "$T/sub.err" ] || fail "subagent precompact printed output"
[ -s "$ST/subsess-a-ag7-latest.md" ] || fail "no subagent handoff subsess-a-ag7-latest.md (have: $(ls "$ST" | tr '\n' ' '))"
grep -q "SUBMARK" "$ST/subsess-a-ag7-latest.md" || fail "subagent handoff lacks the subagent's own message"
! grep -q "MAINMARK" "$ST/subsess-a-ag7-latest.md" || fail "subagent handoff holds main-session text"
( cd "$ST" && cksum subsess-latest.md; printf "%s\n" subsess-* | grep -v -- "-a-ag7" | sort ) > "$T/main.after"
cmp -s "$T/main.before" "$T/main.after" || fail "a subagent event changed the main session's files"
tail -n 1 "$ST/health.jsonl" | grep -q '"agent_id": *"ag7"' || fail "health row lacks the agent_id"
ok "subagent PreCompact writes <sid>-a-<agent_id> files, main files untouched"

# --managed-hooks: stages a file, prints the sudo command, runs no sudo, changes no settings
cp "$S" "$T/before-managed.json"
printf '{"allowedMcpServers": []}' > "$T/ms-sys.json"; cp "$T/ms-sys.json" "$T/ms-sys.orig"
LOSSLESS_HANDOFF_MANAGED_SETTINGS="$T/ms-sys.json" "$ROOT/install.sh" --managed-hooks >"$T/mh.out" || fail "--managed-hooks failed"
grep -q "^sudo install -m 644 .*managed-settings.json\" \"$T/ms-sys.json\"" "$T/mh.out" || fail "--managed-hooks did not print the sudo install command: $(cat "$T/mh.out")"
grep -q "Restart open Claude Code sessions: hooks load when a session starts." "$T/mh.out" || fail "--managed-hooks lacks the restart line"
cmp -s "$T/ms-sys.json" "$T/ms-sys.orig" || fail "--managed-hooks wrote the system file"
cmp -s "$S" "$T/before-managed.json" || fail "--managed-hooks changed user settings"
python3 - "$ST/managed-settings.json" "$H" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); h = sys.argv[2]
assert d["allowedMcpServers"] == []
assert [g["hooks"][0]["command"] for g in d["hooks"]["PreCompact"]] == ["python3 %s/precompact-handoff.py" % h]
ss = {g["matcher"]: [x["command"] for x in g["hooks"]] for g in d["hooks"]["SessionStart"]}
assert sorted(ss) == ["compact", "resume", "startup"] and ss["startup"][-1].endswith("postcompact-inject.py --check"), ss
assert d["hooks"]["SessionEnd"][0]["hooks"][0]["command"].endswith("recall.py session-end")
assert d["hooks"]["SubagentStart"][0]["hooks"][0]["command"].endswith("postcompact-inject.py --subagent-start"), d["hooks"]
PY
ok "--managed-hooks stages managed settings, prints the sudo command, touches nothing else"

# nudge ignores a subagent event: no output, no meta write
mk 399000
rm -f "$ST/asub-agent.meta.json"
o="$(printf '{"session_id":"asub","agent_id":"ag9","transcript_path":"%s","tool_name":"Bash","tool_input":{}}' "$TR" | python3 "$H/handoff-nudge.py")"
[ -z "$o" ] && [ ! -e "$ST/asub-agent.meta.json" ] || fail "nudge acted on a subagent event"
o="$(printf '{"session_id":"asub","transcript_path":"%s","tool_name":"Bash","tool_input":{}}' "$TR" | python3 "$H/handoff-nudge.py")"
[ -n "$o" ] && [ -e "$ST/asub-agent.meta.json" ] || fail "nudge no longer fires for the main session"
ok "nudge ignores subagent events (agent_id), still nudges the main session"

# opt-in canary: not in a default install; --canary installs it; run.sh skips without claude; check.py unit
[ ! -e "$H/handoff-canary.sh" ] && [ ! -e "$H/handoff-canary" ] || fail "default install put the canary in place"
"$ROOT/install.sh" --no-nightly --canary --dry-run >"$T/canary-dry.out"
grep -q "handoff-canary" "$T/canary-dry.out" && [ ! -e "$H/handoff-canary.sh" ] || fail "--canary --dry-run output or side effect wrong"
LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 "$ROOT/install.sh" --no-nightly --canary >"$T/canary.out" || fail "--canary install failed"
[ -x "$H/handoff-canary.sh" ] && [ -x "$H/handoff-canary/run.sh" ] && [ -f "$H/handoff-canary/check.py" ] || fail "canary files not installed"
python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['canary_added'] is True" "$H/.claude-handoff-install.json" || fail "marker lacks canary_added"
if [ "$(uname)" = "Darwin" ]; then
  CP="$HOME/Library/LaunchAgents/com.lossless-handoff.handoff-canary.plist"
  [ -f "$CP" ] && grep -q "handoff-canary.sh --catch-up" "$CP" && grep -q "<integer>30</integer>" "$CP" || fail "canary LaunchAgent plist wrong"
else
  grep -q "30 3 \* \* \* .*handoff-canary.sh --catch-up" "$T/canary.out" || fail "canary crontab line missing"
fi
NC="$T/nocl"; mkdir -p "$NC"; ln -s "$(command -v python3)" "$NC/python3"
if PATH="$NC:/usr/bin:/bin" command -v claude >/dev/null 2>&1; then
  echo "SKIP: a claude binary is in /usr/bin or /bin, canary skip-run check skipped"
else
  HANDOFF_CANARY_STATE_DIR="$T/canary-state" HANDOFF_HEALTH_FILE="$T/canary-health.jsonl" PATH="$NC:/usr/bin:/bin" /bin/bash "$H/handoff-canary.sh" >"$T/canary-run.out" 2>&1 || fail "canary without claude did not exit 0: $(cat "$T/canary-run.out")"
  grep -q '"skip"' "$T/canary-state/last-run.json" || fail "canary without claude did not record skip"
fi
mkdir -p "$T/canary-state"
mkdir -p "$T/case"; rc=0; python3 "$H/handoff-canary/check.py" A "$T/case" >"$T/check.out" || rc=$?
[ "$rc" -eq 1 ] && grep -q '"result": "fail"' "$T/check.out" && grep -q '"reason": "c1"' "$T/check.out" || fail "check.py on an empty case: $(cat "$T/check.out")"
rc=0; python3 "$H/handoff-canary/check.py" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 2 ] || fail "check.py usage exit should be 2"
ok "canary: opt-in only, --canary installs it, runs to skip without claude, check.py fails an empty case (c1)"

# Conflict: same hooks registered from another path -> exit 2, settings untouched; --force installs
H2="$T/home2"; mkdir -p "$H2/.claude"
cat > "$H2/.claude/settings.json" <<'JSON'
{"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "python3 /other/place/precompact-handoff.py"}]}]}}
JSON
cp "$H2/.claude/settings.json" "$T/conflict-orig.json"
rc=0; HOME="$H2" "$ROOT/install.sh" --no-nightly >"$T/c.out" 2>"$T/c.err" || rc=$?
[ "$rc" -eq 2 ] || fail "conflict: expected exit 2, got $rc"
grep -q "/other/place/precompact-handoff.py" "$T/c.err" || fail "conflict: command not printed"
grep -q -- "--force" "$T/c.err" || fail "conflict: message lacks --force"
cmp -s "$H2/.claude/settings.json" "$T/conflict-orig.json" || fail "conflict: settings changed"
[ ! -e "$H2/.claude/hooks/handoff-nudge.py" ] || fail "conflict: hook files copied"
ok "conflicting registration: exit 2, nothing changed"
HOME="$H2" "$ROOT/install.sh" --no-nightly --force >/dev/null || fail "--force install failed"
[ -f "$H2/.claude/hooks/handoff-nudge.py" ] || fail "--force did not install"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert len(d['hooks']['PreCompact'])==2" "$H2/.claude/settings.json" || fail "--force settings wrong"
ok "--force installs alongside the other registration"
rc=0; HOME="$T/home" "$ROOT/install.sh" --no-nightly >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ] || fail "own re-install reported a conflict (exit $rc)"
ok "re-install of own paths is not a conflict"

# Recall: keyword search on a tiny fixture project (embedding server is a closed port)
RP="$T/rproj"; mkdir -p "$RP" "$HOME/.claude/projects/rproj"
python3 - "$HOME/.claude/projects/rproj/abcd1234-0000-0000-0000-000000000000.jsonl" "$RP" <<'PY'
import json, sys
cwd = sys.argv[2]
rows = [
 {"type": "user", "cwd": cwd, "timestamp": "2026-11-01T10:00:00Z", "message": {"role": "user", "content": "please use the flag --zebra-mode for the export job"}},
 {"type": "assistant", "cwd": cwd, "timestamp": "2026-11-01T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done, the export job now runs with --zebra-mode."}]}},
 {"type": "user", "cwd": cwd, "timestamp": "2026-11-01T10:01:00Z", "message": {"role": "user", "content": "LONGMSG " + "".join("%05d|" % i for i in range(8333))[:49984] + "ENDLONG!"}},
]
open(sys.argv[1], "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
o="$(cd "$RP" && python3 "$H/recall.py" search "zebra-mode" 2>&1)" || fail "recall search exited non-zero"
echo "$o" | head -1 | grep -q "keyword only" || fail "recall: first line should say keyword only: $o"
grep -q "abcd1234:1 " <<<"$o" && grep -q "zebra-mode" <<<"$o" || fail "recall: hit missing: $o"
grep -q "hits shown of 2" <<<"$o" || fail "recall: count line wrong: $o"
o="$(cd "$RP" && python3 "$H/recall.py" open abcd1234:1 2>&1)" && grep -q "zebra-mode" <<<"$o" || fail "recall open failed"
o="$(cd "$RP" && python3 "$H/recall.py" user --last 5 2>&1)" && grep -q "zebra-mode" <<<"$o" || fail "recall user failed"
o="$(cd "$RP" && python3 "$H/recall.py" index 2>&1)" || fail "recall index should exit 0 with the server down"
grep -q "meaning index skipped" <<<"$o" || fail "recall index: expected skip message: $o"
ok "recall: search, open, user and index (keyword only, server down)"
python3 - "$H/recall.py" "$RP" <<'PY'
import re, subprocess, sys
rp, cwd = sys.argv[1], sys.argv[2]
want = "LONGMSG " + "".join("%05d|" % i for i in range(8333))[:49984] + "ENDLONG!"
assert len(want) == 50000
def body(*a):
    o = subprocess.check_output(["python3", rp, "open", "abcd1234:3", "--around", "0", "--full", "--cwd", cwd, *a], text=True)
    o = o.split("\n", 1)[1]
    return re.sub(r"\n?\[cut: \d+ more chars[^\]]*\]\s*$", "", o).rstrip("\n")
a = body(); b = body("--from", "40000")
assert len(a) == 40000, len(a)
assert a + b == want, (len(a), len(b))
PY
ok "recall: a 50,000-char message reassembles byte-exactly from open --full + --from 40000"

# --no-nudge (F50): no nudge file or entry, --check is silent, and a later --no-nudge run removes an earlier nudge
(
  export HOME="$T/home-nn"; mkdir -p "$HOME/.claude"; echo '{"cleanupPeriodDays": 365}' > "$HOME/.claude/settings.json"
  "$ROOT/install.sh" --no-nightly --no-nudge >/dev/null
  [ ! -e "$HOME/.claude/hooks/handoff-nudge.py" ] || fail "--no-nudge copied the nudge file"
  ! grep -q handoff-nudge "$HOME/.claude/settings.json" "$HOME/.claude/hooks/.handoff-manifest.json" || fail "--no-nudge left a nudge entry"
  [ -z "$(echo '{}' | python3 "$HOME/.claude/hooks/postcompact-inject.py" --check)" ] || fail "--check not silent after --no-nudge"
  export HOME="$T/home-nn2"; mkdir -p "$HOME/.claude"; echo '{"cleanupPeriodDays": 365}' > "$HOME/.claude/settings.json"
  "$ROOT/install.sh" --no-nightly >/dev/null
  grep -q handoff-nudge "$HOME/.claude/settings.json" || fail "plain install has no nudge entry"
  "$ROOT/install.sh" --no-nightly --no-nudge >/dev/null
  [ ! -e "$HOME/.claude/hooks/handoff-nudge.py" ] && ! grep -q handoff-nudge "$HOME/.claude/settings.json" || fail "--no-nudge did not remove the nudge"
  [ -z "$(echo '{}' | python3 "$HOME/.claude/hooks/postcompact-inject.py" --check)" ] || fail "--check not silent after nudge removal"
)
ok "--no-nudge: no nudge file or entry, --check silent, an earlier nudge is removed"

# F52: protect the store. --check line, --keep-transcripts, handoff retention.
F52LINE_FMT='HANDOFF CHECK: Claude Code deletes chat transcripts after %s days (cleanupPeriodDays), so recall loses older messages. Set "cleanupPeriodDays": 365 in ~/.claude/settings.json, or run ./install.sh --keep-transcripts.'
(
  export HOME="$T/home-f52"; mkdir -p "$HOME/.claude"
  PC="$ROOT/hooks/postcompact-inject.py"
  pcheck() { python3 "$PC" --check 2>&1 | grep "^HANDOFF CHECK: Claude Code deletes" || true; }   # other lines: the bare HOME has no hooks
  want30="$(printf "$F52LINE_FMT" 30)"
  echo '{"cleanupPeriodDays": 30}' > "$HOME/.claude/settings.json"
  python3 "$PC" --check >/dev/null 2>&1 || fail "F52 --check exit non-zero"
  o="$(pcheck)"
  [ "$o" = "$want30" ] || fail "F52.b line wrong: $o"
  rm "$HOME/.claude/settings.json"
  [ "$(pcheck)" = "$want30" ] || fail "F52: no settings file should give 30"
  for v in 90 365; do
    echo "{\"cleanupPeriodDays\": $v}" > "$HOME/.claude/settings.json"
    [ -z "$(pcheck)" ] || fail "F52: $v should print no line"
  done
  for v in '"abc"' -5 1.5 true; do
    echo "{\"cleanupPeriodDays\": $v}" > "$HOME/.claude/settings.json"
    [ "$(pcheck)" = "$want30" ] || fail "F52: value $v should count as 30"
  done
  echo 'not json' > "$HOME/.claude/settings.json"
  o="$(python3 "$PC" --check 2>&1)"; grep -qF "$want30" <<<"$o" && ! grep -q Traceback <<<"$o" || fail "F52: invalid settings: $o"
  # managed settings file wins over the user value
  echo '{"cleanupPeriodDays": 365}' > "$HOME/.claude/settings.json"; echo '{"cleanupPeriodDays": 7}' > "$T/ms-f52.json"
  [ "$(LOSSLESS_HANDOFF_MANAGED_SETTINGS="$T/ms-f52.json" pcheck)" = "$(printf "$F52LINE_FMT" 7)" ] || fail "F52: managed value not used"
  # installer: --keep-transcripts
  echo '{"cleanupPeriodDays": 30, "model": "opus"}' > "$HOME/.claude/settings.json"
  "$ROOT/install.sh" --no-nightly --keep-transcripts >/dev/null
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['cleanupPeriodDays']==365 and d['model']=='opus', d" "$HOME/.claude/settings.json" || fail "F52: --keep-transcripts did not set 365"
  cp "$HOME/.claude/settings.json" "$T/f52.after"
  "$ROOT/install.sh" --no-nightly --keep-transcripts >/dev/null
  cmp -s "$HOME/.claude/settings.json" "$T/f52.after" || fail "F52: second --keep-transcripts run changed settings"
  "$ROOT/uninstall.sh" >"$T/f52.un"
  grep -q "cleanupPeriodDays was left at 365" "$T/f52.un" || fail "F52: uninstall line missing: $(cat "$T/f52.un")"
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['cleanupPeriodDays']==365" "$HOME/.claude/settings.json" || fail "F52: uninstall changed the value"
  echo '{"cleanupPeriodDays": 400}' > "$HOME/.claude/settings.json"
  "$ROOT/install.sh" --no-nightly --keep-transcripts=120 >/dev/null
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['cleanupPeriodDays']==400" "$HOME/.claude/settings.json" || fail "F52: a higher value was lowered"
  echo '{"cleanupPeriodDays": 30}' > "$HOME/.claude/settings.json"; cp "$HOME/.claude/settings.json" "$T/f52.orig"
  for a in --keep-transcripts=10 --keep-transcripts=abc --keep-transcripts=; do
    rc=0; "$ROOT/install.sh" --no-nightly "$a" >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 2 ] && cmp -s "$HOME/.claude/settings.json" "$T/f52.orig" || fail "F52: $a should exit 2 and change nothing (rc $rc)"
  done
  o="$("$ROOT/install.sh" --no-nightly --keep-transcripts --dry-run)"
  cmp -s "$HOME/.claude/settings.json" "$T/f52.orig" || fail "F52: --dry-run changed settings"
  grep -q "cleanupPeriodDays = 365" <<<"$o" || fail "F52: --dry-run does not name the change: $o"
  o="$("$ROOT/install.sh" --no-nightly </dev/null)"
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['cleanupPeriodDays']==30" "$HOME/.claude/settings.json" || fail "F52: non-interactive install changed the value"
  grep -qF "$want30" <<<"$o" || fail "F52: non-interactive install lacks the F52.b text"
  # managed value: --keep-transcripts does not override
  o="$(LOSSLESS_HANDOFF_MANAGED_SETTINGS="$T/ms-f52.json" "$ROOT/install.sh" --no-nightly --keep-transcripts)"
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['cleanupPeriodDays']==30" "$HOME/.claude/settings.json" || fail "F52: managed file was overridden"
  grep -q "managed settings file sets cleanupPeriodDays" <<<"$o" || fail "F52: no managed note"
  # handoff retention
  export HANDOFF_STATE_DIR="$T/f52state"; mkdir -p "$HANDOFF_STATE_DIR"
  mkold() { touch -t "$(python3 -c "import time,sys; print(time.strftime('%Y%m%d%H%M', time.localtime(time.time()-int(sys.argv[1])*86400)))" "$2")" "$HANDOFF_STATE_DIR/$1"; }
  seed() { rm -f "$HANDOFF_STATE_DIR"/old-* ; for f in old-20.md old-400.md old-20-pending.json old-20-degraded.json old-20-last-health.json; do echo x > "$HANDOFF_STATE_DIR/$f"; done
    mkold old-20.md 20; mkold old-400.md 400; mkold old-20-pending.json 20; mkold old-20-degraded.json 20; mkold old-20-last-health.json 20; }
  pin='{"session_id":"f52sess","cwd":"/tmp","transcript_path":"/nonexistent","trigger":"manual"}'
  seed; printf '%s' "$pin" | HANDOFF_COMPACT_STEER=0 HANDOFF_LEDGER=0 python3 "$ROOT/hooks/precompact-handoff.py" >/dev/null
  [ -e "$HANDOFF_STATE_DIR/old-20.md" ] && [ ! -e "$HANDOFF_STATE_DIR/old-400.md" ] || fail "F52.d: default retention wrong"
  [ ! -e "$HANDOFF_STATE_DIR/old-20-pending.json" ] && [ ! -e "$HANDOFF_STATE_DIR/old-20-degraded.json" ] && [ ! -e "$HANDOFF_STATE_DIR/old-20-last-health.json" ] || fail "F52.d: json files must keep the 14-day rule"
  seed; printf '%s' "$pin" | HANDOFF_RETENTION_DAYS=0 HANDOFF_COMPACT_STEER=0 HANDOFF_LEDGER=0 python3 "$ROOT/hooks/precompact-handoff.py" >/dev/null
  [ -e "$HANDOFF_STATE_DIR/old-20.md" ] && [ -e "$HANDOFF_STATE_DIR/old-400.md" ] && [ ! -e "$HANDOFF_STATE_DIR/old-20-pending.json" ] || fail "F52.d: HANDOFF_RETENTION_DAYS=0 wrong"
  seed; printf '%s' "$pin" | HANDOFF_RETENTION_DAYS=abc HANDOFF_COMPACT_STEER=0 HANDOFF_LEDGER=0 python3 "$ROOT/hooks/precompact-handoff.py" >/dev/null
  [ -e "$HANDOFF_STATE_DIR/old-20.md" ] && [ ! -e "$HANDOFF_STATE_DIR/old-400.md" ] || fail "F52.d: invalid value should count as 365"
)
ok "F52: --check line, --keep-transcripts, uninstall note, managed value, handoff retention"

# F53: the PreCompact stdout block
(
  export HOME="$T/home-f53"; mkdir -p "$HOME/.claude"; export HANDOFF_STATE_DIR="$T/f53state"; export HANDOFF_LEDGER=0
  PRE="$ROOT/hooks/precompact-handoff.py"
  steer="$(sed -n '/^STEER_TEXT = """/,/^"""$/p' "$PRE" | sed '1s/^STEER_TEXT = """//;$d')"
  [ "$(head -1 <<<"$steer")" = "Lossless Handoff guidance for this summary:" ] && [ "$(wc -l <<<"$steer")" -eq 6 ] || fail "F53: cannot read the block from the hook"
  run() { printf '%s' "$1" | env "${@:2}" python3 "$PRE" 2>"$T/f53.err"; }
  o="$(run '{"trigger":"auto","session_id":"f53a","cwd":"/tmp","transcript_path":"/nonexistent"}' X=1)"
  [ "$o" = "$steer" ] || fail "F53: auto output differs from the block"
  [ ! -s "$T/f53.err" ] || fail "F53: stderr not empty"
  [ -s "$HANDOFF_STATE_DIR/f53a-latest.md" ] || fail "F53: handoff not written"
  o="$(run '{"trigger":"manual","custom_instructions":"keep the API notes","session_id":"f53b","cwd":"/tmp","transcript_path":"/nonexistent"}' X=1)"
  [ "$o" = "$steer" ] && [ -s "$HANDOFF_STATE_DIR/f53b-latest.md" ] || fail "F53: custom_instructions must still print the block and write a handoff"
  o="$(run '{"trigger":"manual","custom_instructions":"","session_id":"f53c","cwd":"/tmp","transcript_path":"/nonexistent"}' X=1)"
  [ "$o" = "$steer" ] || fail "F53: empty custom_instructions should print the block"
  o="$(run '{"trigger":"auto","session_id":"f53d","cwd":"/tmp","transcript_path":"/nonexistent"}' HANDOFF_COMPACT_STEER=0)"
  [ -z "$o" ] || fail "F53: HANDOFF_COMPACT_STEER=0 should print nothing"
  o="$(run '{"trigger":"auto","session_id":"f53e","agent_id":"agx","cwd":"/tmp","transcript_path":"/nonexistent"}' X=1)"
  [ "$o" = "$steer" ] || fail "F53: subagent event should print the block"
  for m in "--check" "--reconcile --days 1" "--prove --days 1"; do
    # shellcheck disable=SC2086
    o="$(python3 "$PRE" $m </dev/null 2>&1)"; ! grep -q "Lossless Handoff guidance" <<<"$o" || fail "F53: block printed in mode $m"
  done
  o="$(python3 "$ROOT/hooks/postcompact-inject.py" --check 2>&1 </dev/null)"; ! grep -q "Lossless Handoff guidance" <<<"$o" || fail "F53: block in --check"
  for pair in f53a:printed f53b:printed f53d:off; do
    grep '"sid": *"'"${pair%%:*}"'"' "$HANDOFF_STATE_DIR/health.jsonl" | tail -1 | grep -q '"steer": *"'"${pair##*:}"'"' || fail "F53.e: ${pair%%:*} should have steer ${pair##*:}"
  done
)
ok "F53: block on auto, subagent and any custom_instructions; silent for STEER=0 and CLI modes"

# F54: summary nodes (map, describe, search --node, SessionStart line, prove check)
(
  export HOME="$T/home-f54"; mkdir -p "$HOME/.claude"; export HANDOFF_LEDGER=0
  STF="$HOME/.local/state/claude-handoffs"; mkdir -p "$STF"
  POST="$ROOT/hooks/postcompact-inject.py"; PRE="$ROOT/hooks/precompact-handoff.py"; RC="$ROOT/hooks/recall.py"
  python3 - "$HOME" <<'PY'
import json, os, subprocess, sys
home = sys.argv[1]
proj = os.path.join(home, "proj"); os.makedirs(proj)
subprocess.run(["git", "-C", proj, "init", "-q"], check=True)
proj = os.path.realpath(proj)
sid = "11111111-2222-3333-4444-555555555555"
pdir = os.path.join(home, ".claude", "projects", proj.replace("/", "-")); os.makedirs(pdir)
L = []
def add(o): L.append(json.dumps(o))
def msg(t, text, ts, role=None):
    c = text if t == "user" else [{"type": "text", "text": text}]
    add({"type": t, "uuid": "m%d" % len(L), "timestamp": ts, "cwd": proj, "message": {"role": t, "content": c}})
for k in (1, 2, 3):
    ts = "2026-11-1%dT10:00:00.000Z" % k
    msg("user", "question %d about SHARED%d" % (k, k), ts)
    msg("assistant", "answer %d with COMMON token" % k, ts)
    add({"type": "system", "subtype": "compact_boundary", "uuid": "b%d" % k, "timestamp": ts, "cwd": proj,
         "compactMetadata": {"trigger": "auto", "preTokens": 1000 * k}})
    add({"type": "user", "uuid": "s%d" % k, "isCompactSummary": True, "timestamp": ts, "cwd": proj,
         "message": {"role": "user", "content": "This session is being continued.\n\nSummary:\n1. Primary Request:\n   x\n"
                     "7. Current Work:\n   Work on SUMTOKEN%d thing.\n8. Optional Next Step:\n   later\n" % k}})
msg("user", "after the last boundary COMMON live", "2026-11-20T10:00:00.000Z")
open(os.path.join(pdir, sid + ".jsonl"), "w").write("\n".join(L) + "\n")
open(os.path.join(home, "ids"), "w").write("%s\n%s\n%s\n" % (sid, proj, os.path.join(pdir, sid + ".jsonl")))
PY
  SID="$(sed -n 1p "$HOME/ids")"; PROJ="$(sed -n 2p "$HOME/ids")"; TP="$(sed -n 3p "$HOME/ids")"
  printf '# Handoff (pre-compaction)\n- session: %s\n- cwd: %s\n- authority: x\n- recall: old line\n## Goal\nhello\n' "$SID" "$PROJ" > "$STF/$SID-latest.md"
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"trigger\":\"auto\",\"degraded\":false,\"status\":\"ok\"}" > "$STF/$SID-last-health.json"
  hin() { printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s","hook_event_name":"SessionStart","source":"%s"%s}' "$SID" "$TP" "$PROJ" "$1" "${2:-}"; }
  ctx() { python3 -c "import sys,json; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])"; }
  NJ() { cat "$STF"/projects/*/nodes.jsonl; }
  out="$(hin compact | python3 "$POST" --part 1 | ctx)"
  [ "$(NJ | wc -l | tr -d ' ')" = 3 ] || fail "F54: expected 3 leaves"
  python3 - "$SID" "$STF" <<'PY'
import glob, hashlib, json, sys
sid, stf = sys.argv[1], sys.argv[2]
n = [json.loads(x) for x in open(glob.glob(stf + "/projects/*/nodes.jsonl")[0])]
assert [x["seq"] for x in n] == [1, 2, 3], n
assert [(x["first_line"], x["last_line"]) for x in n] == [(1, 2), (4, 6), (8, 10)], n
assert n[0]["prev"] is None and n[1]["prev"] == n[0]["id"] and n[2]["prev"] == n[1]["id"]
assert all(x["id"] == "n" + hashlib.sha1((sid + ":" + x["boundary_uuid"]).encode()).hexdigest()[:10] for x in n)
assert [x["pre_tokens"] for x in n] == [1000, 2000, 3000] and n[0]["agent"] is None and n[0]["kind"] == "leaf"
PY
  ID3="$(NJ | python3 -c "import sys,json; print(json.loads(sys.stdin.readlines()[2])['id'])")"
  ID2="$(NJ | python3 -c "import sys,json; print(json.loads(sys.stdin.readlines()[1])['id'])")"
  grep -qF "summary node: this chat continues after node $ID3 (compaction #3, lines 8-10)." <<<"$out" || fail "F54.d: node line missing: $out"
  [ "$(grep -n 'summary node:' <<<"$out" | cut -d: -f1)" = "$(( $(grep -n -- '- recall:' <<<"$out" | cut -d: -f1) + 1 ))" ] || fail "F54.d: node line not right after the recall line"
  for s in startup resume; do
    o="$(hin $s | python3 "$POST" --part 1 | ctx)"; ! grep -q "summary node:" <<<"$o" || fail "F54.d: $s output has the node line"
  done
  hin compact | python3 "$POST" --part 1 >/dev/null
  [ "$(NJ | wc -l | tr -d ' ')" = 3 ] || fail "F54: a second run added nodes"
  [ "$(cd "$PROJ" && python3 "$RC" nodes --backfill)" = "nodes: 0 added, 3 total" ] || fail "F54: backfill line"
  s="$(cd "$PROJ" && python3 "$RC" search COMMON --keyword)"
  grep -qF "answer 2 with COMMON token [node $ID2]" <<<"$s" && grep -qF "answer 3 with COMMON token [node $ID3]" <<<"$s" && grep -qF "live [node live]" <<<"$s" || fail "F54.c: hit tags: $s"
  NJ | head -3 > "$T/f54.first3"
  printf '%s\n' '{"type":"system","subtype":"compact_boundary","uuid":"b4","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'","compactMetadata":{"trigger":"manual","preTokens":4000}}' >> "$TP"
  hin compact | python3 "$POST" --part 1 >/dev/null
  [ "$(NJ | wc -l | tr -d ' ')" = 4 ] && [ "$(NJ | head -3)" = "$(cat "$T/f54.first3")" ] || fail "F54: 4th boundary / append only"
  python3 - "$TP" <<'PY'
import json, sys
L = open(sys.argv[1]).read().splitlines()
L = [x for x in L if '"b4"' not in x]
open(sys.argv[1], "w").write("\n".join(L) + "\n")
PY
  # (the transcript got shorter: a rescan adds only missing ids, so the 4th leaf stays and nothing is added)
  hin compact | python3 "$POST" --part 1 >/dev/null
  [ "$(NJ | wc -l | tr -d ' ')" = 4 ] || fail "F54: rescan after a shorter transcript added nodes"
  printf '%s\n' '{"type":"system","subtype":"compact_boundary","uuid":"b4","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'","compactMetadata":{"trigger":"manual","preTokens":4000}}' >> "$TP"
  hin compact | python3 "$POST" --part 1 >/dev/null
  [ "$(NJ | wc -l | tr -d ' ')" = 4 ] || fail "F54: re-adding the same boundary made a duplicate"
  # subagent leaf
  python3 - "$(dirname "$TP")/$SID/subagents" "$PROJ" <<'PY'
import json, os, sys
sub, proj = sys.argv[1], sys.argv[2]
os.makedirs(sub)
open(os.path.join(sub, "agent-ag1.jsonl"), "w").write("\n".join([
    json.dumps({"type": "user", "uuid": "x1", "timestamp": "2026-11-15T10:00:00.000Z", "cwd": proj, "isSidechain": True, "message": {"role": "user", "content": "sub work"}}),
    json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": "xb1", "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "cwd": proj, "compactMetadata": {"trigger": "auto", "preTokens": 50}})]) + "\n")
PY
  SUBTP="$(dirname "$TP")/$SID/subagents/agent-ag1.jsonl"
  printf '{"session_id":"%s","agent_id":"ag1","transcript_path":"%s","agent_transcript_path":"%s","cwd":"%s","source":"compact"}' "$SID" "$TP" "$SUBTP" "$PROJ" | python3 "$POST" --part 1 >/dev/null
  NJ | python3 -c "import sys,json; r=[json.loads(x) for x in sys.stdin]; s=[x for x in r if x['agent']=='ag1']; assert len(s)==1 and s[0]['transcript'].endswith('subagents/agent-ag1.jsonl'), r"  || fail "F54: subagent leaf"
  # map, describe, search
  m="$(cd "$PROJ" && python3 "$RC" map)"
  grep -qE "^$ID2 2026-11-12 10:00 11111111 #2 lines 4-6 2000 tokens: Work on SUMTOKEN2 thing" <<<"$m" || fail "F54.c: map line: $m"
  grep -q "^5 nodes in 1 sessions$" <<<"$m" && grep -qE "^n[0-9a-f]{10} .* 11111111/ag1 #1 lines 1-1 50 tokens: " <<<"$m" || fail "F54.c: map count line: $m"
  [ "$(grep -c SUMTOKEN <<<"$m")" = 3 ] || fail "F54.c: map gists"
  d="$(cd "$PROJ" && python3 "$RC" describe "$ID2")"
  grep -q "^seq: 2$" <<<"$d" && grep -q "^lines: 4-6$" <<<"$d" && grep -q "^open: python3 ~/.claude/hooks/recall.py open 11111111:4$" <<<"$d" || fail "F54.c: describe fields: $d"
  grep -q SUMTOKEN2 <<<"$d" && ! grep -q SUMTOKEN3 <<<"$d" || fail "F54.c: describe summary text"
  rc=0; o="$(cd "$PROJ" && python3 "$RC" describe nzzzzzzzzzz 2>&1)" || rc=$?
  [ "$rc" = 1 ] && grep -q "no node nzzzzzzzzzz" <<<"$o" || fail "F54.c: unknown node (rc $rc)"
  s="$(cd "$PROJ" && python3 "$RC" search COMMON --keyword --node "$ID2")"
  grep -q "answer 2 with COMMON" <<<"$s" && ! grep -q "answer 3 with COMMON" <<<"$s" || fail "F54.c: --node filter: $s"
  # no boundary: no node line; a malformed line does not break map
  printf '%s\n' 'not json at all' >> "$STF"/projects/*/nodes.jsonl
  [ "$(cd "$PROJ" && python3 "$RC" map | grep -c SUMTOKEN)" = 3 ] || fail "F54: malformed nodes line broke map"
  # prove: nodes check
  p="$(python3 "$PRE" --prove --days 1)"
  grep -qE "^nodes: 5 boundaries, 5 leaves, 0 bad$" <<<"$p" || fail "F54.e: prove nodes line: $p"
  python3 - "$STF" <<'PY'
import glob, json, sys
f = glob.glob(sys.argv[1] + "/projects/*/nodes.jsonl")[0]
rows = [x for x in open(f).read().splitlines()]
out = []
for x in rows:
    try:
        o = json.loads(x)
    except Exception:
        out.append(x); continue
    if o.get("seq") == 2 and not o.get("agent"):
        o["first_line"] = 2
    out.append(json.dumps(o))
open(f, "w").write("\n".join(out) + "\n")
PY
  p="$(python3 "$PRE" --prove --days 1)"
  grep -qE "^nodes: [0-9]+ boundaries, [0-9]+ leaves, [1-9][0-9]* bad$" <<<"$p" || fail "F54.e: overlap not found: $p"
  grep -qE "failed [1-9]" <<<"$p" || fail "F54.e: prove did not count the bad nodes as failed: $p"
  # an empty project
  mkdir -p "$HOME/empty"; (cd "$HOME/empty" && git init -q && o="$(python3 "$RC" map)" && [ "$o" = "no summary nodes yet" ]) || fail "F54.c: empty map"
)
ok "F54: leaves, ids, append only, SessionStart line, subagent, map, describe, search --node and tags, prove check"

# F55: the recall subagent file
(
  export HOME="$T/home-f55"; mkdir -p "$HOME/.claude"
  AG="$HOME/.claude/agents/recall.md"
  [ -f "$ROOT/agents/recall.md" ] || fail "F55: agents/recall.md missing in the repo"
  "$ROOT/install.sh" --no-nightly --dry-run >/dev/null; [ ! -e "$AG" ] || fail "F55: dry-run wrote the agent"
  "$ROOT/install.sh" --no-nightly >/dev/null
  grep -qF "<!-- lossless-handoff:recall-agent -->" "$AG" && grep -q "^name: recall$" "$AG" && grep -q "^model: haiku$" "$AG" \
    && grep -q "^tools: Bash, Read$" "$AG" && grep -q "recall.py" "$AG" || fail "F55.a: agent file content"
  # recall line sentence (SessionStart, with a handoff that has a recall line)
  STF="$HOME/.local/state/claude-handoffs"; mkdir -p "$STF"
  printf '# Handoff\n- authority: x\n- recall: old line\n## Goal\nhi\n' > "$STF/s55-latest.md"
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"trigger\":\"auto\",\"degraded\":false,\"status\":\"ok\"}" > "$STF/s55-last-health.json"
  run55() { printf '{"session_id":"s55","cwd":"/tmp","source":"compact"}' | python3 "$ROOT/hooks/postcompact-inject.py" --part 1; }
  grep -q "ask the recall subagent" <<<"$(run55)" || fail "F55.c: sentence missing with the agent file"
  # not ours: left unchanged; uninstall keeps it
  printf 'mine\n' > "$AG"; cp "$AG" "$T/f55.mine"
  o="$("$ROOT/install.sh" --no-nightly)"
  # shellcheck disable=SC2088  # the literal ~ is the expected output text
  cmp -s "$AG" "$T/f55.mine" && grep -qF "~/.claude/agents/recall.md exists and is not ours; left unchanged." <<<"$o" || fail "F55.a: foreign file touched"
  ! grep -q "ask the recall subagent" <<<"$(run55)" || fail "F55.c: sentence present without our agent file"
  "$ROOT/uninstall.sh" >/dev/null; [ -f "$AG" ] || fail "F55.b: uninstall removed a foreign file"
  # ours with old content: replaced; uninstall removes it
  printf '<!-- lossless-handoff:recall-agent -->\nold\n' > "$AG"
  "$ROOT/install.sh" --no-nightly >/dev/null; grep -q "^name: recall$" "$AG" || fail "F55.a: marked old file not replaced"
  "$ROOT/uninstall.sh" >/dev/null; [ ! -e "$AG" ] || fail "F55.b: uninstall left our agent file"
)

# F66: llm_map moved out (flags, saved line, existing copy kept, uninstall) and the subagent rules preamble
(
  set -e
  export HOME="$T/f66home"; mkdir -p "$HOME/.claude/hooks"; F="$HOME/.claude/hooks"; DF="$HOME/.config/lossless-handoff/install-defaults"
  SK="$HOME/.claude/skills/llm-map/SKILL.md"
  LMLINE="llm-map is now a separate project: https://github.com/pratikgh0se/llm-map"
  KEPT="llm-map: already installed, kept (now maintained at https://github.com/pratikgh0se/llm-map; this installer no longer updates it)"
  [ ! -e "$ROOT/hooks/llm_map.py" ] && [ ! -e "$ROOT/skills/llm-map" ] || fail "F66.a: llm_map files still in the repo"
  # the flags print one line and change nothing else; the exit code is the same as without them
  for fl in --llm-map --no-llm-map; do
    o="$("$ROOT/install.sh" --dry-run $fl)"; [ "$(grep -cxF "$LMLINE" <<<"$o")" = 1 ] || fail "F66.a: $fl line: $o"
    [ "$(grep -c "llm-map is now" <<<"$o")" = 1 ] || fail "F66.a: $fl printed the line more than once"
  done
  o="$("$ROOT/install.sh" --dry-run)"; ! grep -q "llm-map" <<<"$o" || fail "F66.a: default dry-run mentions llm-map"
  "$ROOT/install.sh" --llm-map >"$T/f66-1.out"
  grep -qxF "$LMLINE" "$T/f66-1.out" && [ ! -e "$F/llm_map.py" ] && [ ! -e "$SK" ] || fail "F66.a: --llm-map installed something"
  [ -f "$F/recall.py" ] && [ -f "$F/lossless_handoff/ledger.py" ] || fail "F66.a: core missing"
  ! grep -q "llm_map" "$F/.handoff-manifest.json" || fail "F66.a: manifest lists llm_map"
  # exit code of a bad flag is unchanged
  r=0; "$ROOT/install.sh" --llm-map --window 0 >/dev/null 2>&1 || r=$?; [ "$r" = 2 ] || fail "F66.a: exit code changed with --llm-map ($r)"
  # a saved llm_map=1 is ignored with the same line; --save-defaults removes it and never writes it
  mkdir -p "$(dirname "$DF")"; printf 'llm_map=1\nsemantic=0\n' > "$DF"
  o="$("$ROOT/install.sh" --dry-run)"; grep -qxF "$LMLINE" <<<"$o" || fail "F66.a: saved llm_map=1 not noted"
  ! grep -q "llm_map.py" <<<"$o" || fail "F66.a: saved llm_map=1 was applied"
  "$ROOT/install.sh" --save-defaults --window 300000 >/dev/null
  ! grep -q "llm_map" "$DF" && grep -qx "window=300000" "$DF" || fail "F66.a: --save-defaults kept llm_map: $(cat "$DF")"
  "$ROOT/install.sh" --save-defaults --llm-map --window 300000 >/dev/null
  ! grep -q "llm_map" "$DF" || fail "F66.a: --save-defaults --llm-map wrote llm_map"
  rm -f "$DF"
  # an existing copy (older install) is kept: not changed, not removed, one kept line
  printf 'OLD LLM MAP\n' > "$F/llm_map.py"; mkdir -p "$(dirname "$SK")"; printf 'OLD SKILL\n' > "$SK"
  o="$("$ROOT/install.sh" --dry-run)"; grep -qxF "[dry-run] $KEPT" <<<"$o" || fail "F66.a: no kept line (dry-run): $o"
  o="$("$ROOT/install.sh")"; [ "$(grep -cxF "$KEPT" <<<"$o")" = 1 ] || fail "F66.a: no kept line: $o"
  [ "$(cat "$F/llm_map.py")" = "OLD LLM MAP" ] && [ "$(cat "$SK")" = "OLD SKILL" ] || fail "F66.a: install changed an existing llm-map"
  "$ROOT/install.sh" --llm-map >/dev/null
  [ "$(cat "$F/llm_map.py")" = "OLD LLM MAP" ] && [ "$(cat "$SK")" = "OLD SKILL" ] || fail "F66.a: --llm-map changed an existing llm-map"
  # uninstall never removes llm_map.py or the llm-map skill (listed in the manifest or marked or not); one kept line
  "$ROOT/install.sh" >/dev/null
  python3 - "$F/.handoff-manifest.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); d["files"]["llm_map.py"] = "x"; json.dump(d, open(sys.argv[1], "w"))
PY
  printf '<!-- lossless-handoff:llm-map-skill -->\nOLD\n' > "$SK"
  UL="llm-map: kept (remove it with ./install.sh --uninstall from https://github.com/pratikgh0se/llm-map)"
  o="$("$ROOT/uninstall.sh")"
  [ "$(grep -cxF "$UL" <<<"$o")" = 1 ] || fail "F66.a: uninstall kept line: $o"
  [ "$(cat "$F/llm_map.py")" = "OLD LLM MAP" ] && [ -f "$SK" ] || fail "F66.a: uninstall removed llm_map.py or the skill"
  [ ! -e "$F/precompact-handoff.py" ] || fail "F66.a: uninstall did not finish its other work"
  rm "$F/llm_map.py" "$SK"; rmdir "$(dirname "$SK")"; o="$("$ROOT/uninstall.sh")"; ! grep -q "llm-map: kept" <<<"$o" || fail "F66.a: kept line without llm-map files"
  # F66.d versions
  [ "$("$ROOT/install.sh" --version)" = "Lossless Handoff 0.19.0" ] || fail "F66.d: product version"
  [ "$(grep -c '^HOOK_VERSION = "1.21.0"$' "$ROOT/hooks/precompact-handoff.py" "$ROOT/hooks/postcompact-inject.py" | grep -c ':1$')" = 2 ] || fail "F66.d: HOOK_VERSION"
  # F66.c: README holds no claim markers; evidence page does; claims-lint default path
  ! grep -qE '\[C[1-9]\]' "$ROOT/README.md" && ! grep -qiE '^#+ .*proof|\*\*proof' "$ROOT/README.md" || fail "F66.c: README holds Proof or claim markers"
  grep -qE '\[C1\]' "$ROOT/docs/evidence.md" || fail "F66.c: docs/evidence.md has no markers"
  grep -qi "small benchmark" "$ROOT/README.md" && grep -q "docs/proof/benchmarks/2026-11-23-proof-test.md" "$ROOT/README.md" || fail "F66.c: README does not state the limits of its one scored result and link its report"
  # a clean state has no live evidence, so lint may exit 1 (no-data); the file must be found and every marker known
  r=0; ( cd "$ROOT" && HANDOFF_STATE_DIR="$T/f66-state" python3 hooks/precompact-handoff.py --claims-lint ) >"$T/f66-lint.out" 2>&1 || r=$?
  [ "$r" -le 1 ] && ! grep -qE "no docs/evidence.md|cannot read|unknown claim" "$T/f66-lint.out" || fail "F66.c: --claims-lint default path: $(cat "$T/f66-lint.out")"
  ( cd "$T" && HANDOFF_STATE_DIR="$T/f66-state" python3 "$ROOT/hooks/precompact-handoff.py" --claims-lint ) >"$T/f66-lint2.out" 2>&1 || true
  cmp -s "$T/f66-lint.out" "$T/f66-lint2.out" || fail "F66.c: --claims-lint default path differs outside the repo folder"
  # another project's docs/evidence.md must not be linted by default (script tree first)
  mkdir -p "$T/f66-other/docs"; printf 'bogus marker [C99]\n' > "$T/f66-other/docs/evidence.md"
  ( cd "$T/f66-other" && HANDOFF_STATE_DIR="$T/f66-state" python3 "$ROOT/hooks/precompact-handoff.py" --claims-lint ) >"$T/f66-lint4.out" 2>&1 || true
  cmp -s "$T/f66-lint.out" "$T/f66-lint4.out" && ! grep -q "C99" "$T/f66-lint4.out" || fail "F66.c: --claims-lint in another project linted that project's docs/evidence.md"
  for n in 1 2 3 4 5 6 7 8; do grep -qF "[C$n]" "$ROOT/docs/evidence.md" || fail "F66.c: evidence.md lacks [C$n]"; done
  HANDOFF_STATE_DIR="$T/f66-state" python3 "$ROOT/hooks/precompact-handoff.py" --claims-lint "$ROOT/docs/evidence.md" >"$T/f66-lint3.out" 2>&1 || true
  cmp -s "$T/f66-lint.out" "$T/f66-lint3.out" || fail "F66.c: explicit path gives another result"
)
ok "F66: llm-map flags, saved line, existing copy kept, uninstall, versions, evidence wording"
ok "F55: agent file install, foreign file kept, replace when marked, uninstall, recall-line sentence"

# F56: excerpt cut in health rows and prove
(
  export HOME="$T/home-f56"; mkdir -p "$HOME/.claude"; export HANDOFF_STATE_DIR="$T/f56state"; export HANDOFF_LEDGER=0
  unset HANDOFF_HAIKU_FALLBACK
  mkdir -p "$T/f56bin"; printf '#!/bin/sh\ncat >/dev/null\necho "## Goal"; echo "fake summary for the smoke test, long enough to pass the length check of fifty chars"\n' > "$T/f56bin/claude"; chmod +x "$T/f56bin/claude"
  python3 - "$T" <<'PY'
import json, sys
t = sys.argv[1]
def w(name, n, size):
    L = []
    for i in range(n):
        L.append(json.dumps({"type": "user", "timestamp": "2026-11-18T10:00:%02dZ" % (i % 60), "message": {"role": "user", "content": "message %d " % i + "x" * size}}))
        L.append(json.dumps({"type": "assistant", "timestamp": "2026-11-18T10:00:%02dZ" % (i % 60), "message": {"role": "assistant", "content": [{"type": "text", "text": "reply %d " % i + "y" * size}]}}))
    open(t + "/" + name, "w").write("\n".join(L) + "\n")
w("f56-small.jsonl", 3, 100)
w("f56-big.jsonl", 60, 3500)
PY
  for pair in small:f56a big:f56b; do
    printf '{"trigger":"auto","session_id":"%s","cwd":"/tmp","transcript_path":"%s/f56-%s.jsonl"}' "${pair##*:}" "$T" "${pair%%:*}" | PATH="$T/f56bin:$PATH" HANDOFF_COMPACT_STEER=0 python3 "$ROOT/hooks/precompact-handoff.py" >/dev/null
  done
  python3 - "$HANDOFF_STATE_DIR/health.jsonl" <<'PY'
import json, sys
rows = {json.loads(x)["sid"]: json.loads(x) for x in open(sys.argv[1]) if x.strip()}
a, b = rows["f56a"], rows["f56b"]
assert a["excerpt_chars"] > 0 and a["excerpt_dropped"] == 0, a
assert b["excerpt_dropped"] > 0 and 0 < b["excerpt_chars"] <= 120000, b
assert a["hook_version"] == "1.21.0"
PY
  p="$(python3 "$ROOT/hooks/precompact-handoff.py" --prove --days 1; echo "rc=$?")"
  grep -qE "^excerpt cut: 1 of 2 compactions, median dropped [0-9]+ chars$" <<<"$p" || fail "F56.b: prove line: $p"
  grep -q "rc=0" <<<"$p" || fail "F56.b: prove exit code"
)
ok "F56: excerpt_chars and excerpt_dropped in health rows; prove prints the cut line"

# F57: subagent transcripts in search (default scope, ids, --no-subagents, user, open, meaning index)
(
  export HOME="$T/home-f57"; mkdir -p "$HOME/.claude"; export HANDOFF_LEDGER=0
  RC="$ROOT/hooks/recall.py"
  python3 - "$HOME" <<'PY'
import json, os, subprocess, sys
home = sys.argv[1]
proj = os.path.join(home, "proj"); os.makedirs(proj)
subprocess.run(["git", "-C", proj, "init", "-q"], check=True)
proj = os.path.realpath(proj)
sid = "aaaaaaaa-1111-2222-3333-444444444444"
aid = "ag12345678abcdef"
pdir = os.path.join(home, ".claude", "projects", proj.replace("/", "-")); os.makedirs(os.path.join(pdir, sid, "subagents"))
def w(path, rows):
    open(path, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
def m(t, text, cwd, side=False, n=0):
    c = text if t == "user" else [{"type": "text", "text": text}]
    r = {"type": t, "uuid": "u%d" % n, "timestamp": "2026-11-18T10:00:%02d.000Z" % n, "cwd": cwd, "message": {"role": t, "content": c}}
    if side:
        r["isSidechain"] = True; r["agentId"] = aid
    return r
w(os.path.join(pdir, sid + ".jsonl"), [m("user", "typed MAINTYPED words here", proj, n=1), m("assistant", "main reply about plans", proj, n=2)])
w(os.path.join(pdir, sid, "subagents", "agent-%s.jsonl" % aid),
  [m("user", "SUBPROMPT from the main agent", proj, True, 3), m("assistant", "the schema has a Quokkasub table", proj, True, 4)])
other = "/r/proj-private"
odir = os.path.join(home, ".claude", "projects", "-r-proj-private"); os.makedirs(os.path.join(odir, "bbbbbbbb-1", "subagents"))
w(os.path.join(odir, "bbbbbbbb-1.jsonl"), [m("user", "private main", other, n=1)])
w(os.path.join(odir, "bbbbbbbb-1", "subagents", "agent-agprivate01.jsonl"), [m("assistant", "Quokkasub private", other, True, 2)])
open(os.path.join(home, "ids"), "w").write("%s\n" % proj)
PY
  PROJ="$(sed -n 1p "$HOME/ids")"
  R() { (cd "$PROJ" && python3 "$RC" "$@"); }
  out="$(R search Quokkasub --keyword)"
  grep -qE "^aaaaaaaa/ag123456:2 .*\[word\] \[agent ag123456\]: " <<<"$out" || fail "F57: subagent hit id/tag: $out"
  [ "$(grep -c Quokkasub <<<"$out")" = 1 ] || fail "F57: out-of-scope session leaked: $out"
  out2="$(R search Quokkasub --keyword --subagents)"
  [ "$out" = "$out2" ] || fail "F57: --subagents changes the output"
  out3="$(R search Quokkasub --keyword --no-subagents)"
  grep -q "^0 hits shown of 0" <<<"$out3" || fail "F57: --no-subagents: $out3"
  o="$(R open aaaaaaaa/ag123456:2 --around 0)"
  grep -q "^--- aaaaaaaa/ag123456:2 " <<<"$o" && grep -q "Quokkasub table" <<<"$o" || fail "F57: open: $o"
  o="$(R open ag123456:2 --around 0)"
  grep -q "Quokkasub table" <<<"$o" || fail "F57: the older id form does not open: $o"
  u="$(R user --last 50)"
  grep -q MAINTYPED <<<"$u" || fail "F57: user lacks the typed message: $u"
  ! grep -q SUBPROMPT <<<"$u" || fail "F57: user holds subagent text: $u"
  # meaning index, with a fake embedding backend on a free local port
  python3 - "$T/f57-embed.port" <<'PY' &
import hashlib, http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        vs = []
        for t in d["input"]:
            v = [0.0] * 16
            for w in t.lower().split():
                v[hashlib.md5(w.encode()).digest()[0] % 16] += 1.0
            vs.append(v)
        b = json.dumps({"embeddings": vs}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
s = http.server.HTTPServer(("127.0.0.1", 0), H)
open(sys.argv[1], "w").write(str(s.server_address[1]))
s.serve_forever()
PY
  EPID=$!; disown "$EPID"
  for _i in $(seq 1 240); do [ -s "$T/f57-embed.port" ] && break; sleep 0.25; done
  [ -s "$T/f57-embed.port" ] || fail "F57: the fake embedding server did not start in 60 s"
  F57PORT="$(cat "$T/f57-embed.port")"; export RECALL_EMBED_URL="http://127.0.0.1:$F57PORT"
  i1="$(R index)"
  m="$(R search Quokkasub --meaning)"
  i2="$(R index)"
  kill "$EPID" 2>/dev/null || true
  grep -qE "^aaaaaaaa/ag123456:2 .*\[meaning\] \[agent ag123456\]: " <<<"$m" || fail "F57: meaning hit: $i1 / $m"
  grep -q "^indexed 0 chunks" <<<"$i2" || fail "F57: a second index added rows: $i2"
)
ok "F57: subagent hits by default, ids and tags, --no-subagents, open, user, meaning index"

# F59: loose separators and the result pointer (ACs 268, 270, 271; 269 and 272 as extra checks)
(
  export HOME="$T/home-f59"; mkdir -p "$HOME/.claude"; export HANDOFF_LEDGER=0
  RC="$ROOT/hooks/recall.py"
  python3 - "$HOME" <<'PY'
import json, os, subprocess, sys
home = sys.argv[1]
proj = os.path.join(home, "proj"); os.makedirs(proj)
subprocess.run(["git", "-C", proj, "init", "-q"], check=True)
proj = os.path.realpath(proj)
sid = "cccccccc-1111-2222-3333-444444444444"
aid = "ag98765432abcdef"
pdir = os.path.join(home, ".claude", "projects", proj.replace("/", "-")); os.makedirs(os.path.join(pdir, sid, "subagents"))
def w(path, rows):
    open(path, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
def rec(t, content, n):
    return {"type": t, "uuid": "u%d" % n, "timestamp": "2026-11-18T10:00:%02d.000Z" % n, "cwd": proj,
            "isSidechain": True, "agentId": aid, "message": {"role": t, "content": content}}
w(os.path.join(pdir, sid + ".jsonl"), [{"type": "user", "uuid": "m1", "timestamp": "2026-11-18T10:00:01.000Z", "cwd": proj,
                                         "message": {"role": "user", "content": "typed F59MAIN words"}}])
w(os.path.join(pdir, sid, "subagents", "agent-%s.jsonl" % aid), [
    rec("user", "SUBPROMPT read the file", 2),
    rec("assistant", [{"type": "tool_use", "id": "toolu_led1", "name": "Read", "input": {"file_path": "led-1.txt"}}], 3),
    rec("user", [{"type": "tool_result", "tool_use_id": "toolu_led1", "content": "ledger-key: AH8X-2548-MJ8D"}], 4)])
open(os.path.join(home, "ids"), "w").write("%s\n" % proj)
PY
  PROJ="$(sed -n 1p "$HOME/ids")"
  R() { (cd "$PROJ" && python3 "$RC" "$@"); }
  out="$(R search "ledger key" --keyword)"
  grep -qE "^cccccccc/ag987654:3 .*AH8X-2548-MJ8D" <<<"$out" || fail "F59: AC 268, loose separators: $out"
  out="$(R search "ledger key" --keyword --fixed)"
  ! grep -q "AH8X-2548-MJ8D" <<<"$out" || fail "F59: --fixed must stay exact: $out"
  out="$(R search "led-1.txt" --keyword)"
  grep -qE "^cccccccc/ag987654:2 .* -> result cccccccc/ag987654:3$" <<<"$out" || fail "F59: AC 270, result pointer: $out"
  o="$(R open cccccccc/ag987654:3 --around 0)"
  grep -q "AH8X-2548-MJ8D" <<<"$o" || fail "F59: AC 271, open of the result id: $o"
  grep -q "AH8X-2548-MJ8D" <<<"$(R search 'ledger.key' --keyword)" || fail "F59: regex ledger.key"
  grep -q "AH8X-2548-MJ8D" <<<"$(R search 'zzz|ledger-key' --keyword)" || fail "F59: regex with |"
  grep -q -- "-> result" "$ROOT/agents/recall.md" && grep -q -- "-> result" "$ROOT/hooks/postcompact-inject.py" \
    && grep -q -- "-> result" "$ROOT"/hooks/lossless_handoff/*.py || fail "F59: AC 273, text"
)
ok "F59: loose separators find ledger-key; a tool call hit points to its result"

# F58: rules at subagent start
(
  export HOME="$T/home-f58"; mkdir -p "$HOME/.claude/hooks"
  unset RECALL_EMBED_URL || true
  export HANDOFF_STATE_DIR="$HOME/.local/state/claude-handoffs"; STF="$HANDOFF_STATE_DIR"; mkdir -p "$STF"
  POST="$ROOT/hooks/postcompact-inject.py"; PRE="$ROOT/hooks/precompact-handoff.py"
  PROJ="$HOME/proj"; mkdir -p "$PROJ"; git -C "$PROJ" init -q; PROJ="$(cd "$PROJ" && pwd -P)"
  python3 - "$PROJ" "$STF" <<'PY'
import hashlib, os, re, sys
proj, stf = sys.argv[1:3]
key = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(proj))[:40] + "-" + hashlib.sha1(proj.encode()).hexdigest()[:8]
d = os.path.join(stf, "projects", key); os.makedirs(d)
open(os.path.join(stf, "key"), "w").write(key)
L = ["- D1 [2026-11-01] [rule] RULEONE never mention traces | why: user said so",
     "- D2 [2026-11-02] [decision] DECISIONTEXT use sqlite | why: simple",
     "- D3 [2026-11-03] [task] TASKTEXT write docs | why: open",
     "- D4 [2026-11-04] [rule] RULETWO run tests first | why: user said so"]
open(os.path.join(d, "decisions.md"), "w").write("# Decisions\n\n" + "\n".join(L) + "\n")
PY
  KEY="$(cat "$STF/key")"; LED="$STF/projects/$KEY/decisions.md"
  sin() { printf '{"session_id":"s58","cwd":"%s","agent_id":"agx","agent_type":"general-purpose","hook_event_name":"SubagentStart"}' "${1:-$PROJ}"; }
  cx() { python3 -c "import sys,json; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])"; }
  SUBP="$HOME/.claude/hooks"; cp "$POST" "$ROOT/hooks/recall.py" "$SUBP/"
  # AC 261: valid JSON, header, rules newest first, recall line, no decision, no task
  o="$(sin | python3 "$POST" --subagent-start)"
  python3 -c "import sys,json; d=json.loads(sys.argv[1])['hookSpecificOutput']; assert d['hookEventName']=='SubagentStart'" "$o" || fail "F58: output is not valid hook JSON: $o"
  c="$(cx <<<"$o")"
  [ "$(head -n 1 <<<"$c")" = "These are the user's standing rules for the main session of this project. Apply only the rules that fit your task. Your task brief comes first." ] || fail "F66.b: preamble: $c"
  [ "$(sed -n 2p <<<"$c")" = "Project rules for this repo (standing; from the Lossless Handoff decisions list; the user's latest words win):" ] || fail "F58: header: $c"
  [ "$(sed -n 3p <<<"$c" | cut -c1-5)" = "- D4 " ] && [ "$(sed -n 4p <<<"$c" | cut -c1-5)" = "- D1 " ] || fail "F58: rules not newest first: $c"
  grep -q "^- recall: " <<<"$c" || fail "F58: no recall line: $c"
  ! grep -q DECISIONTEXT <<<"$c" && ! grep -q TASKTEXT <<<"$c" || fail "F58: decision or task text in the output: $c"
  # AC 267: health row
  python3 - "$STF/health.jsonl" <<'PY'
import json, sys
rows = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
r = [x for x in rows if x.get("event") == "subagent_start"][-1]
assert r["rules"] == 2 and r["chars"] > 0 and r["hook_version"] == "1.21.0" and r["agent_type"] == "general-purpose", r
PY
  # AC 262: no handoff text, no agent notes
  printf '# Handoff\n- cwd: %s\nLATESTTOKEN\n' "$PROJ" > "$STF/s58-latest.md"; printf 'AGENTTOKEN\n' > "$STF/s58-agent.md"
  o="$(sin | python3 "$POST" --subagent-start)"
  ! grep -q LATESTTOKEN <<<"$o" && ! grep -q AGENTTOKEN <<<"$o" || fail "F58: handoff text reached the subagent"
  # health rows change no count in --prove, --reconcile and --ledger
  nl() { wc -l < "$STF/health.jsonl" | tr -d ' '; }
  pb="$(python3 "$PRE" --prove --days 1)"; rb="$(python3 "$PRE" --reconcile --days 1)"; lb="$(printf '{"cwd":"%s"}' "$PROJ" | python3 "$POST" --ledger --part 1)"
  for _i in 1 2 3; do sin | python3 "$POST" --subagent-start >/dev/null; done
  [ "$(python3 "$PRE" --prove --days 1)" = "$pb" ] || fail "F58: --prove changed with subagent rows"
  [ "$(python3 "$PRE" --reconcile --days 1)" = "$rb" ] || fail "F58: --reconcile changed with subagent rows"
  [ "$(printf '{"cwd":"%s"}' "$PROJ" | python3 "$POST" --ledger --part 1)" = "$lb" ] || fail "F58: --ledger changed with subagent rows"
  grep -q subagent_start "$STF/health.jsonl" || fail "F58: no subagent rows to prove it"
  # AC 263: 60 long rules
  python3 - "$LED" <<'PY'
import sys
L = ["- D%d [2026-11-05] [rule] LONGRULE%d %s | why: x" % (i, i, "word " * 30) for i in range(10, 70)]
open(sys.argv[1], "a").write("\n".join(L) + "\n")
PY
  c="$(sin | python3 "$POST" --subagent-start | cx)"
  [ "${#c}" -le 4001 ] || fail "F58: context is ${#c} chars"
  grep -q "^\[more rules: " <<<"$c" || fail "F58: no more-rules line"
  python3 - "$c" <<'PY'
import sys
c = sys.argv[1].rstrip("\n")
assert len(c) <= 4000, len(c)
lines = c.split("\n")
rl = [x for x in lines if x.startswith("- D")]
assert rl and all(x.endswith("| why: x") for x in rl), rl[-1]  # every rule line is whole
PY
  # AC 264: no ledger -> only the recall line; no recall.py either -> nothing
  mkdir -p "$T/f58-empty"; (cd "$T/f58-empty" && git init -q)
  c="$(sin "$T/f58-empty" | python3 "$POST" --subagent-start | cx)"
  grep -q "^- recall: " <<<"$c" && [ "$(wc -l <<<"$c" | tr -d ' ')" = 1 ] || fail "F58: no-ledger output: $c"
  mkdir -p "$T/f58-bare" "$T/f58-nohome"; cp "$POST" "$T/f58-bare/"
  o="$(sin "$T/f58-empty" | HOME="$T/f58-nohome" python3 "$T/f58-bare/postcompact-inject.py" --subagent-start)"; rc=$?
  [ -z "$o" ] && [ "$rc" = 0 ] || fail "F58: expected nothing without recall.py: $o"
  # AC 265: switch off, garbage and empty stdin
  [ -z "$(sin | HANDOFF_SUBAGENT_RULES=0 python3 "$POST" --subagent-start)" ] || fail "F58: HANDOFF_SUBAGENT_RULES=0 printed output"
  for bad in "not json" "" "[]" '{"agent_id":"x"}'; do
    o="$(printf '%s' "$bad" | python3 "$POST" --subagent-start)"; rc=$?
    [ -z "$o" ] && [ "$rc" = 0 ] || fail "F58: bad stdin '$bad': $o rc=$rc"
  done
  python3 "$POST" --subagent-start </dev/null >/dev/null || fail "F58: exit code with empty stdin"
)
ok "F58: SubagentStart output, rule cut, empty cases, switch, health rows, no change to prove/reconcile/ledger"

# F58.c: install and uninstall of the SubagentStart hook
(
  export HOME="$T/home-f58i"; mkdir -p "$HOME/.claude"
  SS="$HOME/.claude/settings.json"; echo '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"echo hi"}]}]}}' > "$SS"
  "$ROOT/install.sh" --no-nightly >"$T/f58i-1.out" 2>&1 || fail "F58: install failed: $(tail -3 "$T/f58i-1.out")"
  "$ROOT/install.sh" --no-nightly >"$T/f58i-2.out" 2>&1 || fail "F58: second install failed"
  [ "$(grep -c "^New hook event (SubagentStart): restart open Claude Code sessions to load it\.$" "$T/f58i-1.out")" = 1 ] || fail "F58: restart line missing on the first install"
  ! grep -q "New hook event (SubagentStart)" "$T/f58i-2.out" || fail "F58: restart line on the second install"
  python3 - "$SS" "$HOME/.claude/hooks" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
g = d["hooks"]["SubagentStart"]
cmds = [h["command"] for x in g for h in x["hooks"]]
assert cmds == ["python3 %s/postcompact-inject.py --subagent-start" % sys.argv[2]], cmds
assert g[0]["hooks"][0]["timeout"] == 10
PY
  "$ROOT/uninstall.sh" >/dev/null 2>&1 || fail "F58: uninstall failed"
  python3 -c "import json,sys; h=json.load(open(sys.argv[1])).get('hooks',{}); assert 'SubagentStart' not in h and 'PreToolUse' in h, h" "$SS" || fail "F58: uninstall left the hook or lost another"
)
ok "F58: install adds one SubagentStart entry and the restart line once; uninstall removes it"

# F62: where the nudge window comes from (env, learned, default 200,000) and F63: format drift alarm
(
  set -euo pipefail
  W="$T/f62"; mkdir -p "$W"
  python3 - "$W" <<'PY'
import json, os, sys
w = sys.argv[1]
def usage(n):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": n - 10, "cache_creation_input_tokens": 0}}}
def boundary(pre):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-01T10:00:00.000Z",
            "compactMetadata": {"trigger": "auto", "preTokens": pre}}
def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("".join(json.dumps(r) + "\n" for r in rows))
# 160,000 tokens: nudges with a 200k window (first at 150k), not with 400k (first at 300k)
write(w + "/none/cur.jsonl", [usage(160000)])
write(w + "/cur/cur.jsonl", [boundary(367000), boundary(167000), boundary(267000), usage(160000)])  # median 267k + 33k = 300k
write(w + "/sib/cur.jsonl", [usage(160000)])
write(w + "/sib/old.jsonl", [boundary(167000), boundary(167000), boundary(167000)])  # 167k + 33k = 200k
PY
  nrun() { # $1 transcript, $2 sid, $3 state dir; the caller sets or unsets the window variable
    printf '{"session_id":"%s","transcript_path":"%s","tool_name":"Bash","tool_input":{}}' "$2" "$1" | HANDOFF_STATE_DIR="$3" python3 "$ROOT/hooks/handoff-nudge.py"
  }
  meta() { python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(m['window'], m['windowSource'])" "$1/$2-agent.meta.json"; }
  i=0
  for v in "" "abc" "0" "-5" "1.5"; do
    i=$((i+1)); d="$W/s-bad$i"
    o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW="$v" nrun "$W/none/cur.jsonl" a "$d")"
    [ -n "$o" ] && [ "$(meta "$d" a)" = "200000 default" ] || fail "F62: value '$v' did not give the 200000 default"
  done
  d="$W/s-unset"
  o="$(env -u CLAUDE_CODE_AUTO_COMPACT_WINDOW HANDOFF_STATE_DIR="$d" python3 "$ROOT/hooks/handoff-nudge.py" < <(printf '{"session_id":"a","transcript_path":"%s","tool_name":"Bash","tool_input":{}}' "$W/none/cur.jsonl"))"
  [ -n "$o" ] && [ "$(meta "$d" a)" = "200000 default" ] || fail "F62: unset variable did not give the 200000 default"
  d="$W/s-env"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000 nrun "$W/none/cur.jsonl" a "$d")"
  [ -z "$o" ] && [ "$(meta "$d" a)" = "400000 env" ] || fail "F62: a valid env value must win and keep the 400k behaviour"
  d="$W/s-cur"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/cur/cur.jsonl" a "$d")"
  [ -z "$o" ] && [ "$(meta "$d" a)" = "300000 learned" ] || fail "F62: window not learned from the current transcript (got $(meta "$d" a))"
  d="$W/s-sib"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/sib/cur.jsonl" a "$d")"
  [ -n "$o" ] && [ "$(meta "$d" a)" = "200000 learned" ] || fail "F62: window not learned from a sibling transcript (got $(meta "$d" a))"
  # tiny window: the final nudge is at or below the first threshold: one final nudge, none after
  d="$W/s-tiny"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW=40000 nrun "$W/none/cur.jsonl" a "$d")"
  [ -n "$o" ] && grep -q "Compaction is imminent" <<<"$o" || fail "F62: tiny window must send the final nudge"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW=40000 nrun "$W/none/cur.jsonl" a "$d")"
  [ -z "$o" ] || fail "F62: tiny window nudged twice"
  # health row copies window and window_source from the nudge meta
  d="$W/s-health"; mkdir -p "$d"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/sib/cur.jsonl" hsess "$d" >/dev/null
  printf '{"session_id":"hsess","transcript_path":"%s","trigger":"auto","cwd":"%s"}' "$W/sib/cur.jsonl" "$W" | HANDOFF_STATE_DIR="$d" python3 "$ROOT/hooks/precompact-handoff.py" >/dev/null
  python3 - "$d/health.jsonl" <<'PY'
import json, sys
r = [json.loads(x) for x in open(sys.argv[1]) if x.strip()][-1]
assert r["window"] == 200000 and r["window_source"] == "learned", r
PY

  # review fixes: manual boundaries are skipped, an absurd window is not used, cache and re-learn
  python3 - "$W" <<'PY'
import json, os, sys
w = sys.argv[1]
def usage(n):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": n - 10, "cache_creation_input_tokens": 0}}}
def boundary(pre, trig="auto"):
    cm = {"preTokens": pre}
    if trig:
        cm["trigger"] = trig
    return {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-01T10:00:00.000Z", "compactMetadata": cm}
def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("".join(json.dumps(r) + "\n" for r in rows))
write(w + "/manual/cur.jsonl", [boundary(30000, "manual"), usage(25000)])
write(w + "/huge/cur.jsonl", [boundary(10 ** 15), usage(160000)])
write(w + "/notrig/cur.jsonl", [boundary(267000, None), usage(160000)])
write(w + "/mixed/cur.jsonl", [boundary(30000, "manual"), boundary(267000), usage(160000)])
write(w + "/relearn/cur.jsonl", [usage(160000)])
write(w + "/cache/cur.jsonl", [usage(160000)])
write(w + "/cache/old.jsonl", [boundary(167000)])
PY
  d="$W/s-manual"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/manual/cur.jsonl" a "$d")"
  [ "$(meta "$d" a)" = "200000 default" ] || fail "F62: a manual boundary must not be learned (got $(meta "$d" a))"
  d="$W/s-huge"
  o="$(CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/huge/cur.jsonl" a "$d")"
  [ "$(meta "$d" a)" = "200000 default" ] || fail "F62: an absurd learned window must give the default (got $(meta "$d" a))"
  d="$W/s-notrig"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/notrig/cur.jsonl" a "$d" >/dev/null
  [ "$(meta "$d" a)" = "300000 learned" ] || fail "F62: a boundary with no trigger must be kept (got $(meta "$d" a))"
  d="$W/s-mixed"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/mixed/cur.jsonl" a "$d" >/dev/null
  [ "$(meta "$d" a)" = "300000 learned" ] || fail "F62: manual must be skipped and auto kept (got $(meta "$d" a))"
  # cache: the learned value stays after the history is gone; it is learned again after 6 hours
  d="$W/s-cache"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/cache/cur.jsonl" a "$d" >/dev/null
  [ "$(meta "$d" a)" = "200000 learned" ] || fail "F62: cache setup (got $(meta "$d" a))"
  : > "$W/cache/old.jsonl"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/cache/cur.jsonl" a "$d" >/dev/null || true
  [ "$(meta "$d" a)" = "200000 learned" ] || fail "F62: the learned value must stay in the 6-hour cache (got $(meta "$d" a))"
  python3 -c "import json,sys,time; p=sys.argv[1]; m=json.load(open(p)); m['windowAt']=time.time()-7*3600; json.dump(m,open(p,'w'))" "$d/a-agent.meta.json"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/cache/cur.jsonl" a "$d" >/dev/null || true
  [ "$(meta "$d" a)" = "200000 default" ] || fail "F62: after 6 hours the window must be learned again (got $(meta "$d" a))"
  # re-learn after a compaction: the context shrinks, so the cache is dropped
  d="$W/s-relearn"
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/relearn/cur.jsonl" a "$d" >/dev/null
  [ "$(meta "$d" a)" = "200000 default" ] || fail "F62: re-learn setup (got $(meta "$d" a))"
  python3 - "$W/relearn/cur.jsonl" <<'PY'
import json, sys
b = {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-01T10:00:00.000Z", "compactMetadata": {"trigger": "auto", "preTokens": 267000}}
u = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}], "usage": {"input_tokens": 10, "cache_read_input_tokens": 19990, "cache_creation_input_tokens": 0}}}
open(sys.argv[1], "a").write(json.dumps(b) + "\n" + json.dumps(u) + "\n")
PY
  CLAUDE_CODE_AUTO_COMPACT_WINDOW='' nrun "$W/relearn/cur.jsonl" a "$d" >/dev/null || true
  [ "$(meta "$d" a)" = "300000 learned" ] || fail "F62: the window must be learned again after a compaction (got $(meta "$d" a))"
  ok "F62: env wins; unset, empty or invalid gives learned or the 200000 default; tiny window sends one final nudge; health row has window and window_source"

  # F63
  R="$T/f63"; mkdir -p "$R"
  python3 - "$R" <<'PY'
import datetime, json, os, sys
r = sys.argv[1]
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
b = {"type": "system", "subtype": "compact_boundary", "timestamp": now, "compactMetadata": {"trigger": "auto", "preTokens": 300000}}
u = {"type": "user", "timestamp": now, "message": {"role": "user", "content": "hi"}}
def mk(case, sid, rows, receipts):
    home = os.path.join(r, case, "home"); st = os.path.join(r, case, "state")
    pd = os.path.join(home, ".claude", "projects", "proj"); os.makedirs(pd); os.makedirs(st)
    tp = os.path.join(pd, sid + ".jsonl")
    open(tp, "w").write("".join(json.dumps(x) + "\n" for x in rows))
    open(os.path.join(st, "receipts.jsonl"), "w").write("".join(json.dumps(
        {"ts": now, "sid": sid[:8], "agent_id": None, "key": sid, "transcript": tp, "boundary_ts": now, "kind": "normal",
         "handoff_sha256": "x", "part1_sha256": "x", "part1_head": "x"}) + "\n" for _ in range(receipts)))
mk("ok", "aaaaaaaa-1", [u, b, u], 1)
mk("drift", "bbbbbbbb-2", [u, u], 2)
mk("none", "cccccccc-3", [u, u], 0)
mk("real", "eeeeeeee-5", [u, u], 1)  # the real drift case: receipt row, transcript without boundary
PY
  rec() { HOME="$R/$1/home" HANDOFF_STATE_DIR="$R/$1/state" python3 "$ROOT/hooks/precompact-handoff.py" --reconcile >/dev/null; }
  chk() { HOME="$R/$1/home" HANDOFF_STATE_DIR="$R/$1/state" python3 "$ROOT/hooks/postcompact-inject.py" --check; }
  rec ok; rec drift; rec none
  python3 - "$R" <<'PY'
import json, sys
r = sys.argv[1]
j = lambda c: json.load(open("%s/%s/state/reconcile.json" % (r, c)))
assert j("ok")["format_drift"] is False and j("ok")["format_drift_sessions"] == []
assert j("none")["format_drift"] is False
d = j("drift")
assert d["format_drift"] is True and d["format_drift_sessions"] == [{"sid": "bbbbbbbb", "injects": 2, "boundaries": 0}], d
assert isinstance(d["format_drift_reason"], str) and "\n" not in d["format_drift_reason"]
PY
  [ -z "$(chk ok | grep -i 'format drift' || true)" ] && [ -z "$(chk none | grep -i 'format drift' || true)" ] || fail "F63: --check warned without drift"
  [ "$(chk drift | grep -ci 'format drift')" = "1" ] || fail "F63: --check must print exactly one format drift line"
  # claims: a drifted session fails the claims, never passes
  mkdir -p "$R/drift/hooks"
  python3 - "$R/drift/hooks" <<'PY'
import json, sys
json.dump({"claims": [{"id": "C1", "text": "t", "source": "reconcile:coverage", "window_days": 30, "refresh_days": 30, "min_rate": 0.0}]},
          open(sys.argv[1] + "/claims.json", "w"))
PY
  cp -R "$ROOT/hooks/precompact-handoff.py" "$ROOT/hooks/lossless_handoff" "$R/drift/hooks/"
  c="$(HOME="$R/drift/home" HANDOFF_STATE_DIR="$R/drift/state" python3 "$R/drift/hooks/precompact-handoff.py" --claims)"
  grep -q "| fail (format_drift) |" <<<"$c" || fail "F63: claims did not fail with format_drift: $c"
  # --prove and a proof:* claim: a drift session never passes
  python3 - "$R/drift" <<'PY'
import datetime, json, sys
d = sys.argv[1]
rc = [json.loads(x) for x in open(d + "/state/receipts.jsonl")][0]
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
ok = {"lossless": "pass", "right_agent": "pass", "delivered": "pass", "decisions_safe": "pass"}
open(d + "/state/proofs.jsonl", "w").write(json.dumps({"ts": now, "receipt_ts": rc["ts"], "sid": rc["sid"], "agent_id": None,
    "boundary_ts": None, "checks": ok, "details": {}}) + "\n")
json.dump({"claims": [{"id": "C1", "text": "t", "source": "proof:delivered", "window_days": 30, "refresh_days": 30, "min_rate": 0.0}]},
          open(d + "/hooks/claims.json", "w"))
PY
  pv="$(HOME="$R/drift/home" HANDOFF_STATE_DIR="$R/drift/state" python3 "$R/drift/hooks/precompact-handoff.py" --prove)"
  grep -Eq "delivered 0/[1-9]" <<<"$pv" || fail "F63: --prove must not count a drift session as pass: $pv"
  grep -q "format_drift" "$R/drift/state/proof-report.md" || fail "F63: proof report lacks the format_drift reason"
  c="$(HOME="$R/drift/home" HANDOFF_STATE_DIR="$R/drift/state" python3 "$R/drift/hooks/precompact-handoff.py" --claims)"
  grep -q "| fail (format_drift) |" <<<"$c" || fail "F63: a proof:* claim did not fail with format_drift: $c"
  # the real drift case, end to end: --reconcile --prove writes fail rows, then --claims shows fail (format_drift)
  mkdir -p "$R/real/hooks"
  python3 - "$R/real/hooks" <<'PY'
import json, sys
json.dump({"claims": [{"id": "C1", "text": "t", "source": "proof:delivered", "window_days": 30, "refresh_days": 30, "min_rate": 0.0}]},
          open(sys.argv[1] + "/claims.json", "w"))
PY
  cp -R "$ROOT/hooks/precompact-handoff.py" "$ROOT/hooks/lossless_handoff" "$R/real/hooks/"
  HOME="$R/real/home" HANDOFF_STATE_DIR="$R/real/state" python3 "$R/real/hooks/precompact-handoff.py" --reconcile --prove >/dev/null
  python3 - "$R/real/state/proofs.jsonl" <<'PY'
import json, sys
rows = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
assert rows, rows
for r in rows:
    assert set(r["checks"].values()) == {"fail"} and len(r["checks"]) == 4, r
    assert all("format_drift" in d for d in r["details"].values()), r
PY
  c="$(HOME="$R/real/home" HANDOFF_STATE_DIR="$R/real/state" python3 "$R/real/hooks/precompact-handoff.py" --claims)"
  grep -q "| fail (format_drift) |" <<<"$c" || fail "F63: real drift case: proof claim not fail (format_drift): $c"
  ok "F63: boundaries present, no inject rows, and drift cases give the right reconcile.json; --check prints one warning line only on drift; claims fail with format_drift"
)


# F64: minimal default install, opt-in flags, saved defaults, re-install keeps existing parts
(
  set -e
  export HOME="$T/f64home"; mkdir -p "$HOME/.claude"; F="$HOME/.claude/hooks"; FS="$HOME/.claude/settings.json"; DF="$HOME/.config/lossless-handoff/install-defaults"
  hp="$("$ROOT/install.sh" --help)"
  for fl in --window --nightly --semantic --save-defaults --no-semantic install-defaults; do grep -q -- "$fl" <<<"$hp" || fail "F64: --help lacks $fl"; done
  o="$("$ROOT/install.sh" --dry-run)"
  grep -q "LaunchAgent" <<<"$o" && fail "F64: default dry-run lists a nightly job"
  grep -q "llm_map.py\|llm-map\|bge-m3" <<<"$o" && fail "F64: default dry-run lists an opt-in part"
  grep -q "settings: window not set" <<<"$o" || fail "F64: default dry-run does not say the window is off"
  [ ! -e "$HOME/.claude/hooks" ] || fail "F64: dry-run wrote files"
  "$ROOT/install.sh" >"$T/f64-1.out"
  [ -f "$F/recall.py" ] && [ -f "$F/precompact-handoff.py" ] && [ -f "$F/postcompact-inject.py" ] && [ -f "$F/handoff-nudge.py" ] && [ -f "$HOME/.claude/agents/recall.md" ] || fail "F64: core missing"
  [ ! -e "$HOME/Library/LaunchAgents" ] && [ ! -e "$DF" ] || fail "F64: default installed an opt-in part"
  python3 - "$FS" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in d.get("env", {}), d
for ev in ("PreCompact", "PostToolUse", "SessionStart", "SessionEnd", "SubagentStart"):
    assert ev in d["hooks"], ev
PY
  # opt-in flags
  "$ROOT/install.sh" --window 250000 >/dev/null
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW']=='250000'" "$FS" || fail "F64: --window"
  "$ROOT/install.sh" --nightly >/dev/null
  [ "$(uname)" != Darwin ] || { [ -f "$HOME/Library/LaunchAgents/com.lossless-handoff.recall-index.plist" ] && [ -f "$HOME/Library/LaunchAgents/com.lossless-handoff.handoff-reconcile.plist" ] || fail "F64: --nightly"; }
  # re-install without flags keeps window and nightly jobs; --no-* flags do not uninstall
  "$ROOT/install.sh" --window 111111 >/dev/null
  "$ROOT/install.sh" --no-nightly --no-window >/dev/null
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW']=='250000'" "$FS" || fail "F64: re-install changed the window"
  [ "$(uname)" != Darwin ] || [ -f "$HOME/Library/LaunchAgents/com.lossless-handoff.recall-index.plist" ] || fail "F64: re-install removed nightly jobs"
  # --window 0 is rejected; an upgrade run says "already installed, kept"
  r=0; "$ROOT/install.sh" --window 0 --dry-run >/dev/null 2>&1 || r=$?; [ "$r" = 2 ] || fail "F64: --window 0 was not rejected"
  o="$("$ROOT/install.sh" --dry-run)"
  grep -q "window already installed, kept" <<<"$o" || fail "F64: upgrade run does not say already installed, kept"
  [ "$(uname)" != Darwin ] || grep -q "nightly jobs: already installed, kept" <<<"$o" || fail "F64: upgrade run: nightly message"
  o="$("$ROOT/install.sh" --window 300000 --dry-run)"
  grep -q "window already installed, kept.*250000" <<<"$o" || fail "F64: --window N on an existing value: no kept line"
  "$ROOT/install.sh" --window 300000 >/dev/null
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW']=='250000'" "$FS" || fail "F64: --window N changed an existing value"
  # a saved window=0 (or any bad line) is ignored with exit 0; only the --window 0 flag exits 2
  mkdir -p "$(dirname "$DF")"; printf 'window=0\nbogus line\nsemantic=maybe\n' > "$DF"
  r=0; o="$("$ROOT/install.sh" --dry-run 2>&1)" || r=$?; [ "$r" = 0 ] || fail "F64: a bad saved defaults line made install fail"
  grep -q "saved defaults" <<<"$o" && fail "F64: a bad saved line was applied"
  rm -f "$DF"
  # old flags: accepted, one note
  o="$("$ROOT/install.sh" --no-window --no-nightly --dry-run)"
  [ "$(grep -c "no longer needed" <<<"$o")" = 1 ] || fail "F64: old flags note"
  "$ROOT/uninstall.sh" >/dev/null
  [ ! -e "$HOME/Library/LaunchAgents/com.lossless-handoff.recall-index.plist" ] || fail "F64: uninstall left opt-in parts"
  # --semantic with a stub ollama (the stub only records the call; no real pull)
  mkdir -p "$T/f64bin"; printf '#!/bin/sh\necho "$@" >> "%s/f64-ollama.calls"\n' "$T" > "$T/f64bin/ollama"; chmod +x "$T/f64bin/ollama"
  PATH="$T/f64bin:$PATH" "$ROOT/install.sh" >/dev/null
  [ ! -e "$T/f64-ollama.calls" ] || fail "F64: default pulled a model"
  o="$(PATH="$T/f64bin:$PATH" "$ROOT/install.sh" --semantic --dry-run)"
  grep -q "would ask whether to run: ollama pull bge-m3" <<<"$o" || fail "F64: --semantic does not offer the pull"
  o="$("$ROOT/install.sh" --semantic </dev/null)"
  grep -q "keyword-only" <<<"$o" && grep -q "ollama.com/download" <<<"$o" || fail "F64: --semantic without ollama: no note"
  PATH="$T/f64bin:$PATH" "$ROOT/install.sh" --semantic --yes >/dev/null
  grep -qx "pull bge-m3" "$T/f64-ollama.calls" || fail "F64: --semantic --yes did not pull"
  # saved defaults
  "$ROOT/install.sh" --dry-run --save-defaults --semantic --window 300000 >/dev/null
  [ ! -e "$DF" ] || fail "F64: dry-run saved defaults"
  "$ROOT/install.sh" --save-defaults --semantic --window 300000 >/dev/null
  grep -qx "semantic=1" "$DF" && grep -qx "window=300000" "$DF" && ! grep -q nightly "$DF" || fail "F64: saved defaults file: $(cat "$DF")"
  "$ROOT/uninstall.sh" >/dev/null; [ -f "$DF" ] || fail "F64: uninstall removed the defaults file"
  rm -f "$T/f64-ollama.calls"
  o="$(PATH="$T/f64bin:$PATH" "$ROOT/install.sh" --dry-run)"
  grep -q "options from saved defaults.*semantic" <<<"$o" && grep -q "would ask whether to run: ollama pull bge-m3" <<<"$o" || fail "F64: saved defaults not applied in dry-run"
  o="$(PATH="$T/f64bin:$PATH" "$ROOT/install.sh" --no-semantic --dry-run)"
  grep -q "bge-m3" <<<"$o" && fail "F64: --no-semantic did not override the saved default"
  PATH="$T/f64bin:$PATH" "$ROOT/install.sh" --yes >/dev/null
  grep -qx "pull bge-m3" "$T/f64-ollama.calls" || fail "F64: saved semantic default did not pull"
  python3 -c "import json,sys; assert json.load(open(sys.argv[1]))['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW']=='300000'" "$FS" || fail "F64: saved window not applied"
  "$ROOT/uninstall.sh" --purge >/dev/null; [ ! -e "$DF" ] || fail "F64: --purge kept the defaults file"
)
ok "F64: minimal default install, opt-in flags, saved defaults, re-install keeps parts"


# F65: one redaction function; handoff files, decisions list and recall output mask secrets
(
  set -e
  python3 - "$ROOT" "$T" <<'PY'
import importlib.util, os, sys
root, t = sys.argv[1:3]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
rc = load("rc", root + "/hooks/recall.py")
samples = [
    ("aws", "AKIA" + "ABCDEFGHIJKLMNOP"), ("aws", "ASIA" + "ABCDEFGHIJKLMNOP"),
    ("github", "ghp_" + "a" * 36), ("github", "gho_" + "b" * 36), ("github", "ghu_" + "c" * 36),
    ("github", "ghs_" + "d" * 36), ("github", "ghr_" + "e" * 36), ("github", "github_pat_" + "f" * 40),
    ("slack", "xoxb-" + "1234567890-abcdef"), ("slack", "xoxs-" + "1234567890-abcdef"),
    ("anthropic", "sk-ant-" + "api03-abcdefghijklmnop"), ("openai", "sk-" + "proj1234567890abcdefgh"),
    ("google", "AIza" + "S" * 35),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"),
    ("pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\ndef\n-----END RSA PRIVATE KEY-----"),
]
for kind, v in samples:
    out = rc.redact("before %s after" % v)
    assert v not in out and "[REDACTED:%s]" % kind in out and out.startswith("before ") and out.endswith(" after"), (kind, out)
for line, secret in [("password=hunter2xx", "hunter2xx"), ("API_KEY: abcd1234", "abcd1234"), ('{"client_secret": "s3cr3tvalue"}', "s3cr3tvalue"),
                     ("access_key = zzzz9999", "zzzz9999"), ("my_token=tok123456", "tok123456"), ("passwd: pw998877x", "pw998877x"), ("apikey=k1k2k3k4", "k1k2k3k4")]:
    out = rc.redact(line)
    assert secret not in out and "[REDACTED:" in out, (line, out)
assert rc.redact("password=hunter2xx").startswith("password=")
# review fixes: no damage to ordinary text, and the misses are masked
keep = ["tests/task-runner-integration-suite.py", "disk-usage-report-2026-11-19.txt", "risk-assessment-for-the-installer",
        "pytest tests/test_flask-application-factory.py", "skill mask-secrets-in-every-file-they-write",
        "token: 160000 of 400000", "if not token: return None", "api_key: str = Field(...)", "Set `HANDOFF_SECRET=0` to stop",
        "secret = get_secret(name)", "token = os.environ['GITHUB_TOKEN']", "password=args.password, user=args.user",
        "token=${VAR_NAME_X}", "token=<placeholder>", "token={{tmplname}}", "password=$VARIABLE_1", "Secret:\n/Users/x/docs/file-name-1.md",
        "max_token = 4096", "Tokens: 12 in, 40 out", "commit 5b5c7de4a1f0c3e2b9d8a7f6e5d4c3b2a1f0e9d8", "token="]
for k in keep:
    assert rc.redact(k) == k, (k, rc.redact(k))
miss = [("token: Bearer " + "aB3dE5gH7jK9mN1p", "aB3dE5gH7jK9mN1p"), ("Authorization: Bearer " + "aB3dE5gH7jK9mN1p", "aB3dE5gH7jK9mN1p"),
        ('password: "correct horse battery"', "horse"), ("SECRET_KEY=" + "aB3dE5gH7jK9mN1p", "aB3dE5gH7jK9mN1p"),
        ("private_key=" + "aB3dE5gH7jK9mN1p", "aB3dE5gH7jK9mN1p"), ("password=$uperS3cret!!", "$uperS3cret!!"), ("token=<abc12345XYZ>", "abc12345XYZ"),
        ("postgres://admin:" + "s3cr3tPass99" + "@db.example.com/x", "s3cr3tPass99"),
        ("key=sk-" + "abcdefghijklmnop1234", "abcdefghijklmnop"), ('"sk-proj-' + 'abcdefghijklmnop1234"', "abcdefghijklmnop")]
for t_, sec in miss:
    o = rc.redact(t_)
    assert sec not in o and "[REDACTED:" in o, (t_, o)
assert rc.redact("postgres://admin:s3cr3tPass99@db/x").startswith("postgres://admin:") and rc.redact("postgres://admin:s3cr3tPass99@db/x").endswith("@db/x")
import time
pem_in = "-----BEGIN PRIVATE KEY-----\nabc\n"
for pem_t in ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\\nMIIabc\\n-----END PRIVATE KEY-----",
              "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n-----END OPENSSH PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nxyz\n-----END RSA PRIVATE KEY-----"):
    assert rc.redact("a " + pem_t + " b") == "a [REDACTED:pem] b", pem_t
assert rc.redact("x -----BEGIN PRIVATE KEY-----\nk1\n-----END PRIVATE KEY----- MID -----BEGIN PRIVATE KEY-----\nk2\n-----END PRIVATE KEY----- y") == "x [REDACTED:pem] MID [REDACTED:pem] y"
for big in (pem_in * 35000, "a." * 20000, ("Qm9vdHN0cmFw-" * 16000)[:200000], "The hook writes the token file. " * 3000):
    t0 = time.time(); rc.redact(big); assert time.time() - t0 < 1.0, ("redact is too slow", len(big), time.time() - t0)
plain = "Plain text: we chose sqlite. tokens: 5000 and a path /tmp/x.py, the AKIA prefix idea.\nline two é"
assert rc.redact(plain) == plain
once = rc.redact("token=abc12345 ghp_" + "a" * 30)
assert rc.redact(once) == once
pc = load("pc", root + "/hooks/precompact-handoff.py")
d = os.path.join(t, "f65"); os.makedirs(d)
pc.HANDOFFS_DIR = d
body = "# Handoff\nkey ghp_" + "a" * 36 + "\npassword=hunter2xx\n" + samples[-1][1] + "\n"
pc.write_handoff_files("sid1", os.path.join(d, "sid1-x.md"), body)
for f in ("sid1-x.md", "sid1-latest.md"):
    got = open(os.path.join(d, f)).read()
    assert "ghp_" + "a" * 36 not in got and "hunter2xx" not in got and "MIIabc" not in got and "[REDACTED:github]" in got and "# Handoff" in got, got
pc.write_handoff_files("sid2", os.path.join(d, "sid2-x.md"), plain)
assert open(os.path.join(d, "sid2-latest.md")).read() == plain
led = pc.redact(pc.ledger_text("/r", {1: {"line": "- D1 [rule] use token=abcd1234 | why: x"}}))
assert "abcd1234" not in led and "[REDACTED:secret]" in led, led
snip = rc.redacted_snippet("deploy with key ghp_" + "a" * 36 + " now", 16, 30)
assert "ghp_" + "a" * 36 not in snip and "[REDACTED:github]" in snip, snip
fbtext = "\n".join("x %s y" % v for _, v in samples) + "\npassword=hunter2xx\n" + plain
assert pc._fb_redact(fbtext) == rc.redact(fbtext) != fbtext
assert pc._fb_redact(plain) == plain
for k in keep:
    assert pc._fb_redact(k) == k, k
for t_, sec in miss:
    assert pc._fb_redact(t_) == rc.redact(t_), t_
print("F65 ok")
PY
)
ok "F65: every secret kind masked in handoff files, decisions list and recall output; plain text unchanged"

# The PreCompact code folder (hooks/lossless_handoff/) is missing or broken: the hook still exits 0 and breaks no session
(
  set -e
  PK="$T/pkgmiss"; mkdir -p "$PK/home" "$PK/state"
  inp='{"session_id":"pkg1","transcript_path":"/nonexistent.jsonl","cwd":"/tmp","trigger":"auto"}'
  for case in missing broken empty; do
    mkdir -p "$PK/$case/hooks"; cp "$ROOT/hooks/precompact-handoff.py" "$ROOT/hooks/claims.json" "$PK/$case/hooks/"
    case "$case" in
      broken) mkdir "$PK/$case/hooks/lossless_handoff"; cp "$ROOT"/hooks/lossless_handoff/*.py "$PK/$case/hooks/lossless_handoff/"; printf 'def (:\n' > "$PK/$case/hooks/lossless_handoff/ledger.py" ;;
      empty) mkdir "$PK/$case/hooks/lossless_handoff" ;;
    esac
    for args in "" "--reconcile" "--prove" "--claims" "--recover"; do
      rc=0; printf '%s' "$inp" | HOME="$PK/home" HANDOFF_STATE_DIR="$PK/state" python3 "$PK/$case/hooks/precompact-handoff.py" $args >"$PK/o" 2>"$PK/e" || rc=$?
      [ "$rc" = 0 ] || fail "package $case: precompact-handoff.py $args exited $rc"
      grep -q "lossless_handoff folder is missing or broken" "$PK/e" || fail "package $case: no message on stderr for '$args': $(cat "$PK/e")"
    done
  done
)
ok "package folder missing, empty or broken: the hook exits 0 with one stderr line for every mode"

# Two copies of the entry file loaded in one process each run their own parts; sys.path stays as it was
(
  set -e
  TC="$T/twocopies"; mkdir -p "$TC/home"
  for c in h1 h2; do mkdir -p "$TC/$c"; cp -R "$ROOT/hooks/precompact-handoff.py" "$ROOT/hooks/lossless_handoff" "$ROOT/hooks/claims.json" "$TC/$c/"; printf "\nCOPY_MARK = '%s'\n" "$c" >> "$TC/$c/lossless_handoff/transcript.py"; done
  HOME="$TC/home" HANDOFF_STATE_DIR="$TC/state" python3 - "$TC" <<'PY'
import importlib.util, io, contextlib, os, sys
tc = sys.argv[1]
sys.argv = ["x", "--claims"]
p0 = list(sys.path)
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        try: spec.loader.exec_module(m)
        except SystemExit: pass
    return m
a = load("pc1", tc + "/h1/precompact-handoff.py"); b = load("pc2", tc + "/h2/precompact-handoff.py")
assert (a.COPY_MARK, b.COPY_MARK) == ("h1", "h2"), (a.COPY_MARK, b.COPY_MARK)
assert a.trunc.__code__.co_filename == tc + "/h1/lossless_handoff/transcript.py", a.trunc.__code__.co_filename
assert b.trunc.__code__.co_filename == tc + "/h2/lossless_handoff/transcript.py", b.trunc.__code__.co_filename
assert sys.path == p0 and "lossless_handoff" not in sys.modules, "the loader changed sys.path or sys.modules"
print("two copies ok")
PY
)
ok "two copies of the entry file in one process use their own parts; sys.path and sys.modules are unchanged"

# The parts share one namespace: every name is defined, none is defined twice, no top-level code uses a later name
(
  set -e
  python3 - "$ROOT" <<'PY'
import ast, builtins, sys
root = sys.argv[1] + "/hooks/"
ns = {}; exec(open(root + "lossless_handoff/__init__.py").read(), ns)
files = [("entry-head", root + "precompact-handoff.py")] + [(p, root + "lossless_handoff/%s.py" % p) for p in ns["PARTS"]]
trees = [(n, ast.parse(open(f, encoding="utf-8").read())) for n, f in files]
# the entry file has its head, the loader try, and the tail (dispatch). The loader sits between head and the parts.
head, tail = [], []
entry = trees[0][1]
li = next(i for i, n in enumerate(entry.body) if isinstance(n, ast.Try) and "load_into" in ast.dump(n))
order = [("entry-head", entry.body[:li])] + [(n, t.body) for n, t in trees[1:]] + [("entry-tail", entry.body[li + 1:])]
def target_names(t):
    return [x.id for x in ast.walk(t) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)]
defined, where = {}, {}
for idx, (pn, body) in enumerate(order):
    for n in body:
        names = [n.name] if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else []
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            names = [x for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) for x in target_names(t)]
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in n.names]
        for nm in names:
            if isinstance(n, (ast.Assign, ast.FunctionDef, ast.ClassDef)):
                assert nm not in where, "top-level name defined twice: %s (%s and %s)" % (nm, where[nm][1], pn)
            where.setdefault(nm, (idx, pn))
# every name loaded anywhere must be bound somewhere (top level, local, argument, import, loop, except ...) or a builtin
bound = set(where) | set(dir(builtins)) | {"__file__", "__name__", "_LH", "_f", "_INIT", "_PKG", "_e"}
for _, t in trees:
    for x in ast.walk(t):
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)): bound.add(x.id)
        elif isinstance(x, ast.arg): bound.add(x.arg)
        elif isinstance(x, ast.ExceptHandler) and x.name: bound.add(x.name)
        elif isinstance(x, (ast.FunctionDef, ast.ClassDef)): bound.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            for a in x.names: bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(x, (ast.Global, ast.Nonlocal)): bound.update(x.names)
for pn, t in trees:
    for x in ast.walk(t):
        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
            assert x.id in bound, "name not defined in any part, the entry or builtins: %s (%s line %d)" % (x.id, pn, x.lineno)
# top-level code (not function bodies) must not use a name that a later part defines
def top_loads(n):
    stack = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(x.decorator_list if hasattr(x, "decorator_list") else [])
            stack.extend(x.args.defaults + [d for d in x.args.kw_defaults if d])
            continue
        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load): yield x
        stack.extend(ast.iter_child_nodes(x))
for idx, (pn, body) in enumerate(order):
    for n in body:
        for x in top_loads(n):
            if x.id in where and where[x.id][0] > idx:
                raise AssertionError("top-level code in %s uses %s, defined later in %s" % (pn, x.id, where[x.id][1]))
print("namespace check ok:", len(where), "top-level names in", len(order), "parts")
PY
)
ok "the parts share one namespace: every name is defined, none twice, no top-level use of a later name"

# F65: a missing or stub recall.py never costs a handoff; the built-in copy masks the secrets
(
  set -e
  SB="$T/f65stub"; mkdir -p "$SB"
  for case in nofile exit0 noredact; do
    D="$SB/$case"; mkdir -p "$D/hooks" "$D/home" "$D/state"
    cp -R "$ROOT/hooks/precompact-handoff.py" "$ROOT/hooks/lossless_handoff" "$ROOT/hooks/claims.json" "$D/hooks/"
    case "$case" in
      exit0) printf 'import sys; sys.exit(0)\n' > "$D/hooks/recall.py" ;;
      noredact) printf 'X = 1\n' > "$D/hooks/recall.py" ;;
    esac
    python3 - "$D/tr.jsonl" <<'PY'
import json, sys
rows = [{"type": "user", "timestamp": "2026-11-19T10:00:00Z", "message": {"role": "user", "content": "deploy now with key ghp_" + "a" * 36 + " and password=hunter2xx please"}},
        {"type": "assistant", "timestamp": "2026-11-19T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}]
open(sys.argv[1], "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
    printf '{"session_id":"stubsid","transcript_path":"%s","trigger":"auto","cwd":"%s"}' "$D/tr.jsonl" "$D" \
      | HOME="$D/home" HANDOFF_STATE_DIR="$D/state" HANDOFF_SUMMARY=0 HANDOFF_HAIKU_FALLBACK=0 HANDOFF_LEDGER=0 python3 "$D/hooks/precompact-handoff.py" >/dev/null 2>&1 || true
    [ -s "$D/state/stubsid-latest.md" ] || fail "F65: no handoff with recall.py case $case"
    ls "$D"/state/stubsid-2*.md >/dev/null 2>&1 || fail "F65: no dated handoff with recall.py case $case"
    ! grep -q "ghp_aaaa\|hunter2xx" "$D"/state/stubsid-*.md || fail "F65: secret in a handoff with recall.py case $case"
    grep -q "REDACTED" "$D/state/stubsid-latest.md" || fail "F65: nothing masked with recall.py case $case"
    # a key=value secret in every section is masked (the final pass leaves key=value to the per-section masking)
    D3="$D/q"; mkdir -p "$D3/state" "$D3/repo"
    git -C "$D3/repo" init -q 2>/dev/null; : > "$D3/repo/password=Hunter2xx99zz.txt"
    printf '# Agent handoff\n## Decisions\nkeep password=Hunter2xx99zz in mind\n%s\n' "$(python3 -c "print('x' * 400)")" > "$D3/state/qsid-agent.md"
    python3 - "$D3/tr.jsonl" <<'PY'
import json, sys
S = "password=Hunter2xx99zz"
def tu(i, name, inp):
    return {"type": "assistant", "timestamp": "2026-11-19T10:00:0%dZ" % i, "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": name, "input": inp}]}}
rows = [{"type": "user", "timestamp": "2026-11-19T10:00:00Z", "message": {"role": "user", "content": "please use " + S + " here"}},
        tu(1, "TaskCreate", {"subject": "rotate " + S, "description": "d"}),
        tu(2, "TodoWrite", {"todos": [{"content": "todo " + S, "status": "pending", "activeForm": "x"}]}),
        tu(3, "Bash", {"command": "echo " + S}),
        tu(4, "Write", {"file_path": "/tmp/proj/" + S + ".txt", "content": "x"}),
        {"type": "assistant", "timestamp": "2026-11-19T10:00:09Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "done with " + S}]}}]
open(sys.argv[1], "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
    printf '{"session_id":"qsid","transcript_path":"%s","trigger":"auto","cwd":"%s"}' "$D3/tr.jsonl" "$D3/repo" \
      | HOME="$D/home" HANDOFF_STATE_DIR="$D3/state" HANDOFF_SUMMARY=0 HANDOFF_HAIKU_FALLBACK=0 HANDOFF_LEDGER=0 python3 "$D/hooks/precompact-handoff.py" >/dev/null 2>&1 || true
    [ -s "$D3/state/qsid-latest.md" ] || fail "F65: no handoff for the all-sections case ($case)"
    ! grep -rq "Hunter2xx99zz" "$D3/state"/qsid-2*.md "$D3/state/qsid-latest.md" || fail "F65: a section wrote password=Hunter2xx99zz unmasked (case $case): $(grep -rn Hunter2xx99zz "$D3/state"/qsid-latest.md | cut -c1-120)"
    for h in "## Last assistant message" "## Task state" "## Last Bash commands" "## Last user messages" "## Open tasks" "## Files touched" "## Git"; do
      grep -q "^$h" "$D3/state/qsid-latest.md" || fail "F65: section '$h' missing, so the all-sections case proves nothing (case $case)"
    done
    grep -c "REDACTED:secret" "$D3/state/qsid-latest.md" | grep -qv '^0$' || fail "F65: nothing masked in the all-sections case ($case)"
    # a key word, a newline and a path: the handoff keeps the path (the text is masked before the lines are joined)
    D2="$D/p"; mkdir -p "$D2/state"
    python3 - "$D2/tr.jsonl" <<'PY'
import json, sys
rows = [{"type": "user", "timestamp": "2026-11-19T10:00:00Z", "message": {"role": "user", "content": "Secret:\n/etc/ssl/private/server-key.pem is the file"}},
        {"type": "assistant", "timestamp": "2026-11-19T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}]
open(sys.argv[1], "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
    printf '{"session_id":"pathsid","transcript_path":"%s","trigger":"auto","cwd":"%s"}' "$D2/tr.jsonl" "$D" \
      | HOME="$D/home" HANDOFF_STATE_DIR="$D2/state" HANDOFF_SUMMARY=0 HANDOFF_HAIKU_FALLBACK=0 HANDOFF_LEDGER=0 python3 "$D/hooks/precompact-handoff.py" >/dev/null 2>&1 || true
    grep -qF -- "- Secret: /etc/ssl/private/server-key.pem is the file" "$D2/state/pathsid-latest.md" || fail "F65: path after a key word was masked in the handoff (case $case): $(grep -F 'Secret' "$D2/state/pathsid-latest.md" || echo none)"
  done
)
ok "F65: no recall.py, a stub that exits at import, and a stub without redact(): the handoff is still written and masked"

# F67: auto-recall hook (opt-in): trigger, search, output, redaction, off switch, time budget, health log
(
  set -e
  D="$T/f67"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W"
  export HANDOFF_STATE_DIR="$D/state"; HK="$ROOT/hooks/auto-recall.py"; AL="$HANDOFF_STATE_DIR/auto-recall.jsonl"
  python3 - "$HOME" "$W" <<'PY'
import json, os, re, sys
home, w = sys.argv[1:3]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real))
os.makedirs(pd)
def rec(t, ts, text, n):
    return {"type": t, "timestamp": ts, "cwd": real, "sessionId": "f67sess", "uuid": "u%d" % n,
            "message": {"role": t, "content": text if t == "user" else [{"type": "text", "text": text}]}}
rows = [rec("user", "2026-11-19T10:00:00Z", "Our staging database rotation policy is every ninety days for all credentials", 1),
        rec("assistant", "2026-11-19T10:00:05Z", "Noted: the staging database rotation policy is every ninety days.", 2),
        rec("user", "2026-11-19T10:01:00Z", "The deploy pipeline token is ghp_abcdefghijklmnopqrstuvwxyz0123456789 for the staging pipeline", 3),
        rec("user", "2026-11-19T10:02:00Z", "We picked the purple elephant migration strategy for the warehouse", 4)]
open(os.path.join(pd, "f67sess.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  ar() { python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"cwd":sys.argv[2],"session_id":"f67cur","transcript_path":sys.argv[3]}))' "$1" "$W" "${AR_TP:-}" | python3 "$HK"; }
  HDR="Lossless Handoff auto-recall: earlier messages that match your words. Verify them before you rely on them."
  FTR="Open one in full: python3 ~/.claude/hooks/recall.py open <id>"
  # cue: output has the exact header, one line per hit, the exact footer
  o="$(ar "What did we decide earlier about the staging database rotation policy?")"
  [ "$(head -n 1 <<<"$o")" = "$HDR" ] && [ "$(tail -n 1 <<<"$o")" = "$FTR" ] || fail "F67.c: header or footer wrong: $o"
  [ "$(grep -c '^- ' <<<"$o")" -ge 1 ] && [ "$(grep -c '^- ' <<<"$o")" -le 3 ] || fail "F67.c: hit lines: $o"
  grep -Eq '^- f67sess?[^ ]*:[0-9]+ 2026-11-19 10:0[0-9] (user|assistant): ' <<<"$o" || fail "F67.c: hit line format: $o"
  grep -q "ninety days" <<<"$o" || fail "F67.c: hit text missing: $o"
  [ "$(awk 'length($0) > 400' <<<"$o" | wc -l | tr -d ' ')" = 0 ] || fail "F67.c: a hit line is too long"
  # F67.b (amended): one case per cue class fires, and the health line holds the matched cue
  for cue in "what did you say" "did we decide" "did we agree" "did i say" "did i tell you" "we agreed" "you mentioned" "i mentioned" \
             "as discussed" "earlier you" "earlier we" "said before" "like before" "same as before" "do you remember" "remember when" "remember what"; do
    o="$(ar "$cue the staging database rotation policy")"
    grep -q "ninety days" <<<"$o" || fail "F67.b: cue '$cue' did not fire: $o"
    [ "$(tail -n 1 "$AL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["cue"])')" = "$cue" ] || fail "F67.d: health cue is not '$cue'"
  done
  o="$(ar "WHAT DID I SAY about the staging database rotation policy")"; grep -q "ninety days" <<<"$o" || fail "F67.b: cue is case sensitive"
  [ "$(tail -n 1 "$AL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["cue"])')" = "what did i say" ] || fail "F67.d: health cue lower case"
  # whole words: a cue inside another word does not fire
  [ -z "$(ar "the staging database rotation policy was mentioned, as discussedly")" ] || fail "F67.b: a cue inside a word fired"
  # the old bare cues no longer fire
  for q in "run the tests before you commit" "remember to rotate the staging database credentials" "did we ship the staging database rotation policy" \
           "did I rotate the staging database credentials" "what was the staging database rotation policy" "earlier staging database rotation policy"; do
    [ -z "$(ar "$q")" ] || fail "F67.b: a bare cue fired: $q"
  done
  # no cue: no output
  [ -z "$(ar "Please rotate the staging database credentials now")" ] || fail "F67.b: output without a cue"
  # slash command: no output even with a cue
  [ -z "$(ar "/review what did we decide earlier about the staging database rotation policy")" ] || fail "F67.b: slash prompt fired"
  # quoted phrase of 3 or more words fires without a cue phrase; 2 words does not
  o="$(ar 'Find "purple elephant migration" in the notes')"
  grep -q "purple elephant migration" <<<"$o" || fail "F67.b/c: quoted phrase: $o"
  [ "$(tail -n 1 "$AL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["cue"])')" = "quote" ] || fail "F67.d: health cue for a quote"
  [ -z "$(ar 'Find "purple elephant" in the notes')" ] || fail "F67.b: a 2-word quote fired"
  # fewer than 2 words of 4+ characters after the cues are removed: no output
  [ -z "$(ar "what did I say on the zebra")" ] || fail "F67.c: fewer than 2 words fired"
  # no hits: no output
  [ -z "$(ar "what did we decide earlier about quantum unicorn telemetry")" ] || fail "F67.c: no hits gave output"
  # the current prompt is not a hit
  python3 - "$HOME" <<'PY'
import glob, json, os, sys
p = glob.glob(os.path.join(sys.argv[1], ".claude", "projects", "*", "f67sess.jsonl"))[0]
r = {"type": "user", "timestamp": "2026-11-20T09:00:00Z", "cwd": os.path.realpath(os.path.dirname(p) and json.loads(open(p).readline())["cwd"]), "sessionId": "f67sess", "uuid": "u9",
     "message": {"role": "user", "content": "did we decide on the tangerine hovercraft mechanism"}}
open(p, "a").write(json.dumps(r) + "\n")
PY
  TPF="$(ls "$HOME"/.claude/projects/*/f67sess.jsonl)"
  [ -z "$(AR_TP="$TPF" ar "did we decide on the tangerine hovercraft mechanism")" ] || fail "F67.c: the current prompt was a hit"
  # a multi-line current prompt is excluded too (recall writes a newline as " / ")
  python3 - "$TPF" <<'PY'
import json, sys
p = sys.argv[1]
cwd = json.loads(open(p).readline())["cwd"]
r = {"type": "user", "timestamp": "2026-11-20T09:05:00Z", "cwd": cwd, "sessionId": "f67sess", "uuid": "u10",
     "message": {"role": "user", "content": "did we decide on the walrus\nsubmarine telemetry plan"}}
open(p, "a").write(json.dumps(r) + "\n")
PY
  ML="$(printf 'did we decide on the walrus\nsubmarine telemetry plan')"
  [ -z "$(AR_TP="$TPF" ar "$ML")" ] || fail "F67.c: a multi-line current prompt was a hit"
  # without the path of the current transcript, nothing is excluded (that message is then a normal hit)
  grep -q "walrus" <<<"$(ar "$ML")" || fail "F67.c: a hit in another transcript file was dropped"
  # F67.c: tool results never push out real hits (2 old messages, 250 newer tool results)
  python3 - "$TPF" <<'PY'
import json, sys
p = sys.argv[1]
cwd = json.loads(open(p).readline())["cwd"]
rows = []
for i in range(2):
    rows.append({"type": "user", "timestamp": "2026-11-01T10:0%d:00Z" % i, "cwd": cwd, "sessionId": "f67sess", "uuid": "o%d" % i,
                 "message": {"role": "user", "content": "zirconium capacitor calibration notes number %d" % i}})
for i in range(250):
    rows.append({"type": "user", "timestamp": "2026-11-21T10:00:00Z", "cwd": cwd, "sessionId": "f67sess", "uuid": "t%d" % i,
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x%d" % i,
                                                          "content": "zirconium capacitor calibration output %d" % i}]}})
open(p, "a").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  o="$(ar "what did we decide about zirconium capacitor calibration")"
  grep -q "calibration notes number" <<<"$o" && ! grep -q "calibration output" <<<"$o" || fail "F67.c: tool results pushed out real hits: $o"
  # redaction: the secret never reaches the output
  o="$(ar "what did we decide earlier about the deploy pipeline token staging")"
  grep -q "REDACTED" <<<"$o" && ! grep -q "ghp_abcdefghijkl" <<<"$o" || fail "F67.c: secret not masked: $o"
  # off switch
  [ -z "$(HANDOFF_AUTO_RECALL=0 bash -c "$(declare -f ar); W='$W' HK='$HK' ar 'What did we decide earlier about the staging database rotation policy?'")" ] || fail "F67.a: HANDOFF_AUTO_RECALL=0 printed output"
  # a bad input never fails
  r=0; echo "not json" | python3 "$HK" >/dev/null || r=$?; [ "$r" = 0 ] || fail "F67.c: bad stdin gave exit $r"
  r=0; echo '{"prompt":"what did we decide earlier about staging","cwd":"/no/such/dir"}' | python3 "$HK" >"$D/bad.out" || r=$?; [ "$r" = 0 ] && [ ! -s "$D/bad.out" ] || fail "F67.c: bad cwd"
  # health log: one line per run that passed the trigger; no prompt text, no snippets
  [ -s "$AL" ] || fail "F67.d: no health log"
  python3 - "$AL" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
assert all(json.loads(l)["cue"] for l in raw.splitlines()), "cue missing"
rows = [json.loads(l) for l in raw.splitlines()]
assert rows and all(set(r) == {"ts", "session_id", "fired", "cue", "mode", "identifiers", "hits", "ms", "timed_out"} for r in rows), rows
assert all(r["fired"] is True and isinstance(r["hits"], int) and isinstance(r["ms"], int) and r["timed_out"] is False for r in rows), rows
assert any(r["hits"] >= 1 for r in rows) and any(r["hits"] == 0 for r in rows), rows
for bad in ("ninety", "staging", "ghp_", "tangerine", "purple"):
    assert bad not in raw, bad
PY
  n0="$(wc -l < "$AL")"; ar "Please rotate the staging database credentials now" >/dev/null; ar "/x what did we decide earlier" >/dev/null
  [ "$(wc -l < "$AL")" = "$n0" ] || fail "F67.d: a run that did not pass the trigger was logged"
  # an existing log with loose rights is tightened to 600 by the next run
  chmod 644 "$AL"; ar "what did we decide earlier about the staging database rotation policy" >/dev/null
  [ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$AL")" = 600 ] || fail "F67.d: an existing log kept loose rights"
  # a missing recall.py: no output, exit 0
  mkdir -p "$D/solo"; cp "$HK" "$D/solo/auto-recall.py"
  r=0; o="$(python3 -c 'import json; print(json.dumps({"prompt":"what did we decide earlier about the staging database rotation policy","cwd":"'"$W"'"}))' | HOME="$D/nohome" python3 "$D/solo/auto-recall.py")" || r=$?
  [ "$r" = 0 ] && [ -z "$o" ] || fail "F67.c: missing recall.py gave output or failure"
  # a stub recall.py without the search functions: no output, exit 0
  mkdir -p "$D/stubhome/.claude/hooks"; printf 'def redact(t, kv=True):\n    return t\n' > "$D/stubhome/.claude/hooks/recall.py"
  r=0; o="$(python3 -c 'import json; print(json.dumps({"prompt":"what did we decide earlier about the staging database rotation policy","cwd":"'"$W"'"}))' | HOME="$D/stubhome" python3 "$HK")" || r=$?
  [ "$r" = 0 ] && [ -z "$o" ] || fail "F67.c: stub recall.py gave output or failure"
  # timeout path: a search that never ends gives no output, exit 0, in about 2 seconds, and a timed_out row
  mkdir -p "$D/slowhome/.claude/hooks"
  cat > "$D/slowhome/.claude/hooks/recall.py" <<'PY'
import time
PROJECTS_DIR = "/nonexistent"
def redact(t, kv=True): return t
def project_root(c): return c
def in_scope_transcripts(r, s): return [object()]
def keyword_hits(*a): time.sleep(30)
def meaning_hits(*a): return [], None, "off"
def rrf_merge(a, b): return a
def fill_meaning_snippets(*a): pass
def fmt_ts(t): return t
def index_paths(r): return ("", "/nonexistent", "")
PY
  t0=$(date +%s); nrows="$(wc -l < "$AL")"
  r=0; o="$(python3 -c 'import json; print(json.dumps({"prompt":"what did we decide earlier about the staging database rotation policy","cwd":"'"$W"'"}))' | HOME="$D/slowhome" python3 "$HK")" || r=$?
  t1=$(date +%s)
  [ "$r" = 0 ] && [ -z "$o" ] && [ $((t1 - t0)) -le 4 ] || fail "F67.c: timeout path: exit $r, output '$o', $((t1 - t0)) s"
  [ "$(wc -l < "$AL" | tr -d " ")" = "$(( $(echo "$nrows" | tr -d " ") + 1 ))" ] || fail "F67.d: the timeout run wrote $(wc -l < "$AL") rows after $nrows"
  [ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$AL")" = 600 ] || fail "F67.d: the log is not mode 600"
  tail -n 1 "$AL" | python3 -c 'import json,sys; r=json.loads(sys.stdin.read()); assert r["timed_out"] is True and r["hits"] == 0 and 1500 <= r["ms"] <= 2500, r' || fail "F67.d: timeout row"
  ok "F67: cue, no cue, slash, quote, short query, no hits, current prompt, redaction, off switch, stub, timeout, health log"
)
# F67.a/e: install, uninstall, dry-run, saved defaults, upgrade message, versions
(
  set -e
  export HOME="$T/f67inst"; mkdir -p "$HOME/.claude"; F="$HOME/.claude/hooks"; FS="$HOME/.claude/settings.json"; DF="$HOME/.config/lossless-handoff/install-defaults"
  CMD="python3 $F/auto-recall.py"
  echo '{"model": "opus"}' > "$FS"
  grep -q -- "--auto-recall" <<<"$("$ROOT/install.sh" --help)" || fail "F67.a: --help lacks --auto-recall"
  [ "$(grep -c '^HOOK_VERSION = "1.21.0"$' "$ROOT/hooks/auto-recall.py")" = 1 ] || fail "F67.e: auto-recall HOOK_VERSION"
  # default install: not installed, not registered
  "$ROOT/install.sh" >/dev/null
  [ ! -e "$F/auto-recall.py" ] && ! grep -q "auto-recall" "$FS" && ! grep -q "auto-recall" "$F/.handoff-manifest.json" || fail "F67.a: default install holds auto-recall"
  ! grep -q "auto-recall" <<<"$("$ROOT/install.sh" --dry-run)" || fail "F67.a: default dry-run mentions auto-recall"
  # dry-run with the flag lists it and changes nothing
  cp "$FS" "$T/f67-before.json"
  o="$("$ROOT/install.sh" --auto-recall --dry-run)"
  grep -q "install hook: .*/auto-recall.py" <<<"$o" && grep -q "add UserPromptSubmit auto-recall hook" <<<"$o" || fail "F67.a: dry-run does not list auto-recall: $o"
  [ ! -e "$F/auto-recall.py" ] && cmp -s "$FS" "$T/f67-before.json" || fail "F67.a: dry-run changed something"
  # install with the flag
  "$ROOT/install.sh" --auto-recall >"$T/f67-i.out"
  [ -x "$F/auto-recall.py" ] && grep -q '"auto-recall.py"' "$F/.handoff-manifest.json" || fail "F67.a: file or manifest entry missing"
  ls "$FS".bak-* >/dev/null 2>&1 && [ -n "$(grep -L auto-recall "$FS".bak-*)" ] || fail "F67.a: no settings backup from before auto-recall"
  python3 - "$FS" "$CMD" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); cmd = sys.argv[2]
g = d["hooks"]["UserPromptSubmit"]
assert [h for x in g for h in x["hooks"] if h["command"] == cmd] == [{"type": "command", "command": cmd, "timeout": 5}], g
assert d["model"] == "opus"
PY
  # a rerun without the flag keeps it and says so; a rerun with --no-auto-recall also keeps it
  cp "$FS" "$T/f67-s1.json"
  o="$("$ROOT/install.sh")"; grep -qx "auto-recall: already installed, kept" <<<"$o" || fail "F67.a: upgrade message: $o"
  o="$("$ROOT/install.sh" --no-auto-recall)"; grep -qx "auto-recall: already installed, kept" <<<"$o" || fail "F67.a: --no-auto-recall upgrade message: $o"
  [ -x "$F/auto-recall.py" ] && grep -q "$CMD" "$FS" || fail "F67.a: a rerun removed auto-recall"
  "$ROOT/install.sh" --auto-recall >/dev/null
  [ "$(grep -c "$CMD" "$FS")" = 1 ] || fail "F67.a: a second --auto-recall registered it twice"
  # uninstall: dry-run lists, real run removes the file and the entry, other settings stay
  o="$("$ROOT/uninstall.sh" --dry-run)"; grep -q "auto-recall.py" <<<"$o" || fail "F67.a: uninstall dry-run does not list auto-recall"
  [ -e "$F/auto-recall.py" ] || fail "F67.a: uninstall dry-run removed the file"
  "$ROOT/uninstall.sh" >/dev/null
  [ ! -e "$F/auto-recall.py" ] && ! grep -q "auto-recall" "$FS" || fail "F67.a: uninstall left auto-recall"
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d == {'model': 'opus'}, d" "$FS" || fail "F67.a: uninstall did not restore settings"
  # saved defaults
  "$ROOT/install.sh" --save-defaults --auto-recall >/dev/null
  grep -qx "auto_recall=1" "$DF" && [ -x "$F/auto-recall.py" ] || fail "F67.a: --save-defaults --auto-recall"
  "$ROOT/uninstall.sh" >/dev/null
  o="$("$ROOT/install.sh" --dry-run)"; grep -q "saved defaults.*auto_recall" <<<"$o" && grep -q "auto-recall.py" <<<"$o" || fail "F67.a: saved default not applied: $o"
  "$ROOT/install.sh" >/dev/null; [ -x "$F/auto-recall.py" ] && grep -q "$CMD" "$FS" || fail "F67.a: saved default did not install it"
  "$ROOT/uninstall.sh" >/dev/null
  "$ROOT/install.sh" --no-auto-recall >/dev/null
  [ ! -e "$F/auto-recall.py" ] && ! grep -q "auto-recall" "$FS" && grep -qx "auto_recall=1" "$DF" || fail "F67.a: --no-auto-recall did not skip it, or touched the saved default"
  "$ROOT/install.sh" --save-defaults --no-auto-recall >/dev/null
  ! grep -q "auto_recall" "$DF" && [ ! -e "$F/auto-recall.py" ] || fail "F67.a: --save-defaults --no-auto-recall"
  ok "F67: install, default off, dry-run, backup, upgrade message, uninstall, saved defaults"
)

# F68, F81: strict recall rules, on by default (marker file, one extra line after the recall line, run-time off switch, health flag)
(
  set -e
  export HOME="$T/f68"; mkdir -p "$HOME/.claude"; F="$HOME/.claude/hooks"; DF="$HOME/.config/lossless-handoff/install-defaults"
  export HANDOFF_STATE_DIR="$HOME/.local/state/claude-handoffs"; STF="$HANDOFF_STATE_DIR"; mkdir -p "$STF"
  POST="$ROOT/hooks/postcompact-inject.py"
  RULE='- recall rule: before you state an earlier number, path, name, command or decision, search for it. Do not answer from memory. If a value is not in your context, or a file no longer holds it, search before you answer "unknown". If the search finds nothing, say so.'
  PROJ="$HOME/proj"; mkdir -p "$PROJ"; git -C "$PROJ" init -q; PROJ="$(cd "$PROJ" && pwd -P)"
  python3 - "$PROJ" "$STF" <<'PY'
import hashlib, os, re, sys
proj, stf = sys.argv[1:3]
key = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(proj))[:40] + "-" + hashlib.sha1(proj.encode()).hexdigest()[:8]
d = os.path.join(stf, "projects", key); os.makedirs(d)
open(os.path.join(d, "decisions.md"), "w").write("# Decisions\n\n- D1 [2026-11-01] [rule] RULEONE be brief | why: user said so\n- D2 [2026-11-02] [decision] DEC use sqlite | why: simple\n")
PY
  printf '# Handoff\n- authority: x\n- recall: old line\n## Goal\nhi\n' > "$STF/s68-latest.md"
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"trigger\":\"auto\",\"degraded\":false,\"status\":\"ok\"}" > "$STF/s68-last-health.json"
  run_start() { printf '{"session_id":"s68","cwd":"%s","source":"compact"}' "$PROJ" | python3 "$POST" --part 1; }
  run_led() { printf '{"cwd":"%s"}' "$PROJ" | python3 "$POST" --ledger --part 1; }
  run_sub() { printf '{"session_id":"s68","cwd":"%s","agent_id":"agx","agent_type":"general-purpose"}' "$PROJ" | python3 "$POST" --subagent-start; }
  ctx() { python3 -c "import sys,json; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])"; }
  # F81: a default install writes the marker, and the dry-run lists it
  o="$("$ROOT/install.sh" --dry-run)"; grep -q "recall rules marker" <<<"$o" && [ ! -e "$F/.recall-rules" ] || fail "F81: default dry-run: $o"
  "$ROOT/install.sh" >/dev/null
  [ -e "$F/.recall-rules" ] || fail "F81: default install did not write the marker"
  # baseline outputs: the off switch gives the output without the rule line
  S0="$(HANDOFF_RECALL_RULES=0 run_start | ctx)"; L0="$(HANDOFF_RECALL_RULES=0 run_led | ctx)"; B0="$(HANDOFF_RECALL_RULES=0 run_sub | ctx)"
  ! grep -qF "recall rule:" <<<"$S0$L0$B0" || fail "F68: rule line with HANDOFF_RECALL_RULES=0"
  grep -q "^- recall: " <<<"$S0" && grep -q "^- recall: " <<<"$L0" && grep -q "^- recall: " <<<"$B0" || fail "F68: a baseline output has no recall line"
  for w in start led sub; do
    if [ "$w" = start ]; then o="$(run_start | ctx)"; b="$S0"; fi
    if [ "$w" = led ]; then o="$(run_led | ctx)"; b="$L0"; fi
    if [ "$w" = sub ]; then o="$(run_sub | ctx)"; b="$B0"; fi
    [ "$(grep -cxF -- "$RULE" <<<"$o")" = 1 ] || fail "F68: $w: the rule line is missing or doubled: $o"
    n="$(grep -n '^- recall: ' <<<"$o" | cut -d: -f1)"
    [ "$(sed -n "$((n + 1))p" <<<"$o")" = "$RULE" ] || fail "F68: $w: the rule line is not directly after the recall line"
    [ "$(grep -vxF -- "$RULE" <<<"$o")" = "$b" ] || fail "F68: $w: output differs from the baseline in more than the rule line"
  done
  # no marker: no rule line
  rm -f "$F/.recall-rules"
  S1="$(run_start | ctx)"; ! grep -qF "recall rule:" <<<"$S1" || fail "F68: rule line without the marker"
  "$ROOT/install.sh" --recall-rules >/dev/null
  [ -e "$F/.recall-rules" ] || fail "F68: --recall-rules did not write the marker"
  # the receipt flag
  run_start >/dev/null; HANDOFF_RECALL_RULES=0 run_start >/dev/null
  python3 - "$STF/receipts.jsonl" <<'PY'
import json, sys
rows = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
assert rows[-2]["recall_rules"] is True and rows[-1]["recall_rules"] is False, rows[-2:]
PY
  # a rerun keeps it; --no-recall-rules alone keeps it
  o="$("$ROOT/install.sh")"; grep -qx "recall-rules: already installed, kept" <<<"$o" || fail "F68: kept message: $o"
  o="$("$ROOT/install.sh" --no-recall-rules)"; grep -qx "recall-rules: already installed, kept" <<<"$o" && [ -e "$F/.recall-rules" ] || fail "F68: --no-recall-rules removed it"
  # uninstall: dry-run lists, real run removes
  o="$("$ROOT/uninstall.sh" --dry-run)"; grep -q "\.recall-rules" <<<"$o" && [ -e "$F/.recall-rules" ] || fail "F68: uninstall dry-run"
  "$ROOT/uninstall.sh" >/dev/null; [ ! -e "$F/.recall-rules" ] || fail "F68: uninstall left the marker"
  # saved defaults (F81)
  "$ROOT/install.sh" --save-defaults --recall-rules >/dev/null
  grep -qx "recall_rules=1" "$DF" && [ -e "$F/.recall-rules" ] || fail "F68: --save-defaults --recall-rules"
  "$ROOT/uninstall.sh" >/dev/null
  "$ROOT/install.sh" --no-recall-rules >/dev/null
  [ ! -e "$F/.recall-rules" ] && grep -qx "recall_rules=1" "$DF" || fail "F81: --no-recall-rules did not skip it, or touched the saved default"
  "$ROOT/install.sh" >/dev/null; [ -e "$F/.recall-rules" ] || fail "F68: saved default did not install it"
  o="$("$ROOT/install.sh" --save-defaults --no-recall-rules)"
  grep -qx "recall-rules: removed" <<<"$o" && [ ! -e "$F/.recall-rules" ] && grep -qx "recall_rules=0" "$DF" || fail "F81: --save-defaults --no-recall-rules: $o"
  "$ROOT/install.sh" >/dev/null; [ ! -e "$F/.recall-rules" ] || fail "F81: saved recall_rules=0 did not stop the marker"
  o="$("$ROOT/install.sh" --dry-run)"; ! grep -q "recall rules marker" <<<"$o" || fail "F81: dry-run with recall_rules=0 lists the marker"
  "$ROOT/install.sh" --recall-rules >/dev/null; [ -e "$F/.recall-rules" ] || fail "F81: --recall-rules beats recall_rules=0"
  grep -q -- "--recall-rules" <<<"$("$ROOT/install.sh" --help)" && grep -q "on by default" <<<"$("$ROOT/install.sh" --help)" || fail "F81: --help"
  "$ROOT/uninstall.sh" >/dev/null
  # F81.b: the MCP instructions hold the same rule line as the inject hook
  python3 - "$ROOT/hooks" <<'PY'
import importlib.util, sys
def load(n, f):
    sp = importlib.util.spec_from_file_location(n, sys.argv[1] + "/" + f); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
a = load("pci", "postcompact-inject.py").RECALL_RULE_LINE
m = load("rmcp", "recall-mcp.py")
assert m.RULE == a and m.INSTRUCTIONS.startswith(a) and len(m.INSTRUCTIONS) < 4096, (m.RULE, a)
PY
  ok "F68: marker, rule line after the recall line (start, ledger, subagent), off switch, receipt flag, install, saved defaults, uninstall"
)

# F69: recall MCP tool (install, registration through the claude command, protocol, tools, limits, health)
(
  set -e
  D="$T/f69"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W" "$D/fakebin"
  F="$HOME/.claude/hooks"; DF="$HOME/.config/lossless-handoff/install-defaults"; MC="python3 $F/recall-mcp.py"
  export HANDOFF_STATE_DIR="$D/state"
  cat > "$D/fakebin/claude" <<'SH'
#!/bin/sh
echo "$*" >> "$FAKE_CLAUDE_LOG"
exit 0
SH
  chmod +x "$D/fakebin/claude"; export FAKE_CLAUDE_LOG="$D/claude.args"; : > "$FAKE_CLAUDE_LOG"
  # a PATH without any claude command (the real one may sit next to python3)
  NC=""; IFS=: read -ra _pd <<<"$PATH"
  for d in "${_pd[@]}"; do [ -x "$d/claude" ] || NC="${NC:+$NC:}$d"; done
  command -v python3 >/dev/null || true
  if ! PATH="$NC" command -v python3 >/dev/null 2>&1; then mkdir -p "$D/pybin"; ln -sf "$(command -v python3)" "$D/pybin/python3"; NC="$NC:$D/pybin"; fi
  ! PATH="$NC" command -v claude >/dev/null 2>&1 || fail "F69: test setup: claude is still on PATH"
  # default install: no file, no manifest entry, no claude call
  PATH="$D/fakebin:$PATH" "$ROOT/install.sh" >/dev/null
  [ ! -e "$F/recall-mcp.py" ] && ! grep -q "recall-mcp" "$F/.handoff-manifest.json" && [ ! -s "$FAKE_CLAUDE_LOG" ] || fail "F69: default install holds recall-mcp or called claude"
  ! grep -q "recall-mcp" <<<"$(PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --dry-run)" || fail "F69: default dry-run mentions recall-mcp"
  grep -q -- "--recall-mcp" <<<"$("$ROOT/install.sh" --help)" || fail "F69: --help lacks --recall-mcp"
  [ "$(grep -c '^HOOK_VERSION = "1.21.0"$' "$ROOT/hooks/recall-mcp.py")" = 1 ] || fail "F69: HOOK_VERSION"
  # dry-run lists the file and the command; runs nothing
  o="$(PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --recall-mcp --dry-run)"
  grep -q "install hook: .*/recall-mcp.py" <<<"$o" && grep -qF "claude mcp add --scope user lossless-recall -- $MC" <<<"$o" || fail "F69: dry-run: $o"
  [ ! -e "$F/recall-mcp.py" ] && [ ! -s "$FAKE_CLAUDE_LOG" ] || fail "F69: dry-run changed something"
  # install with the flag: the file, the manifest, the exact claude call, no direct edit of ~/.claude.json
  PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --recall-mcp >/dev/null
  [ -x "$F/recall-mcp.py" ] && grep -q '"recall-mcp.py"' "$F/.handoff-manifest.json" || fail "F69: file or manifest entry missing"
  grep -qxF "mcp add --scope user lossless-recall -- python3 $F/recall-mcp.py" "$FAKE_CLAUDE_LOG" || fail "F69: claude args: $(cat "$FAKE_CLAUDE_LOG")"
  [ ! -e "$HOME/.claude.json" ] || fail "F69: the installer wrote ~/.claude.json"
  # kept message
  o="$(PATH="$D/fakebin:$PATH" "$ROOT/install.sh")"; grep -qx "recall-mcp: already installed, kept" <<<"$o" || fail "F69: kept message: $o"
  o="$(PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --no-recall-mcp)"; grep -qx "recall-mcp: already installed, kept" <<<"$o" && [ -e "$F/recall-mcp.py" ] || fail "F69: --no-recall-mcp removed it"
  # the recall line names the tool, only when installed
  printf '# Handoff\n- authority: x\n- recall: old line\n## Goal\nhi\n' > "$D/h.md"
  mkdir -p "$HANDOFF_STATE_DIR"; cp "$D/h.md" "$HANDOFF_STATE_DIR/s69-latest.md"
  echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"trigger\":\"auto\",\"degraded\":false,\"status\":\"ok\"}" > "$HANDOFF_STATE_DIR/s69-last-health.json"
  o="$(printf '{"session_id":"s69","cwd":"/tmp","source":"compact"}' | python3 "$ROOT/hooks/postcompact-inject.py" --part 1)"
  python3 - "$o" <<'PY'
import json, sys
c = json.loads(sys.argv[1])["hookSpecificOutput"]["additionalContext"]
l = [x for x in c.split("\n") if x.startswith("- recall:")][0]
assert l.endswith(" Or call the search_earlier_chat tool."), l
PY
  # uninstall: no claude on PATH still exits 0; with claude it runs the remove command
  : > "$FAKE_CLAUDE_LOG"
  o="$(PATH="$D/fakebin:$PATH" "$ROOT/uninstall.sh" --dry-run)"; grep -q "recall-mcp.py" <<<"$o" && [ ! -s "$FAKE_CLAUDE_LOG" ] || fail "F69: uninstall dry-run"
  PATH="$D/fakebin:$PATH" "$ROOT/uninstall.sh" >/dev/null
  [ ! -e "$F/recall-mcp.py" ] || fail "F69: uninstall left the file"
  [ "$(cat "$FAKE_CLAUDE_LOG")" = "mcp remove lossless-recall --scope user" ] || fail "F69: uninstall claude args: $(cat "$FAKE_CLAUDE_LOG")"
  o="$(printf '{"session_id":"s69","cwd":"/tmp","source":"compact"}' | python3 "$ROOT/hooks/postcompact-inject.py" --part 1)"
  ! grep -q "search_earlier_chat" <<<"$o" || fail "F69: tool sentence without recall-mcp.py installed"
  # no claude on PATH: the install prints the register command, installs the file, exits 0
  r=0; o="$(PATH="$NC" "$ROOT/install.sh" --recall-mcp)" || r=$?
  [ "$r" = 0 ] && [ -x "$F/recall-mcp.py" ] || fail "F69: install without claude: exit $r"
  grep -qxF "recall-mcp: claude CLI not found; register it later with: claude mcp add --scope user lossless-recall -- $MC" <<<"$o" || fail "F69: no-claude message: $o"
  r=0; PATH="$NC" "$ROOT/uninstall.sh" >/dev/null || r=$?
  [ "$r" = 0 ] && [ ! -e "$F/recall-mcp.py" ] || fail "F69: uninstall without claude: exit $r"
  # saved defaults
  PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --save-defaults --recall-mcp >/dev/null
  grep -qx "recall_mcp=1" "$DF" && [ -x "$F/recall-mcp.py" ] || fail "F69: --save-defaults --recall-mcp"
  PATH="$D/fakebin:$PATH" "$ROOT/uninstall.sh" >/dev/null
  o="$(PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --dry-run)"; grep -q "saved defaults.*recall_mcp" <<<"$o" && grep -q "recall-mcp.py" <<<"$o" || fail "F69: saved default not applied: $o"
  PATH="$D/fakebin:$PATH" "$ROOT/install.sh" >/dev/null; [ -x "$F/recall-mcp.py" ] || fail "F69: saved default did not install it"
  PATH="$D/fakebin:$PATH" "$ROOT/uninstall.sh" >/dev/null
  PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --no-recall-mcp >/dev/null
  [ ! -e "$F/recall-mcp.py" ] && grep -qx "recall_mcp=1" "$DF" || fail "F69: --no-recall-mcp did not skip it, or touched the saved default"
  PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --save-defaults --no-recall-mcp >/dev/null
  ! grep -q "recall_mcp" "$DF" && [ ! -e "$F/recall-mcp.py" ] || fail "F69: --save-defaults --no-recall-mcp"
  ok "F69: install, registration command, no-claude message, kept message, recall line sentence, uninstall, saved defaults"
)
(
  set -e
  D="$T/f69p"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W"
  export HANDOFF_STATE_DIR="$D/state"; MS="$ROOT/hooks/recall-mcp.py"
  python3 - "$HOME" "$W" <<'PY'
import json, os, re, sys
home, w = sys.argv[1:3]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real))
os.makedirs(pd)
def rec(t, ts, text, n):
    return {"type": t, "timestamp": ts, "cwd": real, "sessionId": "f69sess", "uuid": "u%d" % n,
            "message": {"role": t, "content": text if t == "user" else [{"type": "text", "text": text}]}}
rows = [rec("user", "2026-11-19T10:00:00Z", "Our staging database rotation policy is every ninety days for all credentials", 1),
        rec("assistant", "2026-11-19T10:00:05Z", "Noted: the staging database rotation policy is every ninety days.", 2),
        rec("user", "2026-11-19T10:01:00Z", "The deploy pipeline token is ghp_abcdefghijklmnopqrstuvwxyz0123456789 for the staging pipeline", 3),
        rec("user", "2026-11-19T10:02:00Z", "LONGMARK " + ("lorem ipsum dolor " * 3000), 4)]
open(os.path.join(pd, "f69sess.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  cat > "$D/drive.py" <<'PY'
import json, os, subprocess, sys
ms, cwd = sys.argv[1:3]
def run(lines):
    p = subprocess.run([sys.executable, ms], input="".join(l + "\n" for l in lines), capture_output=True, text=True, cwd=cwd, timeout=60)
    assert p.returncode == 0, p.stderr
    return [json.loads(x) for x in p.stdout.splitlines()], p.stdout
def req(i, m, p=None):
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": m, **({"params": p} if p is not None else {})})
def call(i, name, args):
    return req(i, "tools/call", {"name": name, "arguments": args})
txt = lambda r: r["result"]["content"][0]["text"]
out, raw = run([
    req(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}),
    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    req(2, "ping"), req(3, "tools/list"),
    call(4, "search_earlier_chat", {"query": "staging database rotation"}),
    call(5, "search_earlier_chat", {"query": "staging", "role": "assistant", "limit": 1}),
    call(6, "search_earlier_chat", {"query": "quantumunicorntelemetry"}),
    call(7, "search_earlier_chat", {"query": "pipeline token"}),
    call(8, "search_earlier_chat", {"query": ""}),
    call(9, "search_earlier_chat", {"query": "x", "limit": 11}),
    call(10, "search_earlier_chat", {"query": "x", "role": "tool"}),
    call(11, "open_message", {"id": "f69sess:1"}),
    call(12, "open_message", {"id": "zzzzzzzz:1"}),
    call(13, "open_message", {"id": "f69sess:4", "full": True}),
    call(14, "open_message", {}),
    call(15, "nosuchtool", {}),
    req(16, "nosuch/method"),
    "this is not json",
    req(17, "ping"),
    call(18, "open_message", {"id": "f69sess:3"}),
])
by = {o.get("id"): o for o in out}
assert [o.get("id") for o in out].count(2) == 1 and 99 not in by
init = by[1]["result"]
assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"] == {"name": "lossless-recall", "version": "1.21.0"}, init
ins = init["instructions"]
assert len(ins) < 4096 and "recall rule: before you state an earlier number, path, name, command or decision, search for it. Do not answer from memory. If a value is not in your context, or a file no longer holds it, search before you answer \"unknown\". If the search finds nothing, say so." in ins, ins
assert by[2]["result"] == {}
tools = {t["name"]: t for t in by[3]["result"]["tools"]}
assert set(tools) == {"search_earlier_chat", "key_history", "open_message"}
assert all(t["_meta"] == {"anthropic/alwaysLoad": True} for t in tools.values())
assert tools["search_earlier_chat"]["inputSchema"]["required"] == ["query"] and tools["open_message"]["inputSchema"]["required"] == ["id"]
t = txt(by[4]); assert "isError" not in by[4]["result"] or by[4]["result"]["isError"] is False
ls = t.splitlines(); assert 1 <= len(ls) <= 5 and all(x.startswith("f69sess") for x in ls) and "ninety days" in t, t
assert len(txt(by[5]).splitlines()) == 1 and " assistant " in txt(by[5]), txt(by[5])
assert txt(by[6]) == "no matches", txt(by[6])
assert "ghp_abcdefghijklmnopqrstuvwxyz" not in txt(by[7]) and "[REDACTED:github]" in txt(by[7]), txt(by[7])
for i in (8, 9, 10, 12, 14):
    r = by[i]["result"]; assert r["isError"] is True and "\n" not in r["content"][0]["text"] and r["content"][0]["text"], (i, r)
assert "ninety days" in txt(by[11]) and txt(by[11]).startswith("--- "), txt(by[11])
assert len(txt(by[13])) <= 12000 and txt(by[13]).endswith("[cut at 12000 characters]"), (len(txt(by[13])), txt(by[13])[-60:])
assert by[15].get("error", {}).get("code") == -32602 or by[15]["result"]["isError"] is True
assert by[16]["error"]["code"] == -32601
pe = [o for o in out if o.get("id") is None and "error" in o]
assert len(pe) == 1 and pe[0]["error"]["code"] == -32700, pe
assert 17 in by and "ghp_abcdefghijklmnopqrstuvwxyz" not in txt(by[18]) and "[REDACTED:github]" in txt(by[18]), by.get(18)
# an unsupported client version gets the default; a supported older one is echoed
o2, _ = run([req(1, "initialize", {"protocolVersion": "1999-01-01"}), req(2, "initialize", {"protocolVersion": "2025-03-26"})])
assert o2[0]["result"]["protocolVersion"] == "2025-11-25" and o2[1]["result"]["protocolVersion"] == "2025-03-26", o2
PY
  python3 "$D/drive.py" "$MS" "$W" || fail "F69: scripted MCP session"
  python3 - "$HANDOFF_STATE_DIR/recall-mcp.jsonl" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
rows = [json.loads(x) for x in raw.splitlines() if x.strip()]
assert len(rows) >= 10 and all(set(r) == {"ts", "tool", "hits", "ms"} and r["tool"] in ("search_earlier_chat", "open_message") for r in rows), rows[:3]
assert all(isinstance(r["hits"], int) and isinstance(r["ms"], int) for r in rows)
for bad in ("staging", "ninety", "ghp_", "quantum", "pipeline"):
    assert bad not in raw, bad
PY
  # the server keeps running after bad input: a closed stdin ends it with exit 0
  r=0; python3 "$MS" </dev/null || r=$?; [ "$r" = 0 ] || fail "F69: exit $r on empty stdin"
  ok "F69: MCP protocol, both tools, limits, redaction, errors, parse error, version echo, health log without query text"
)
# F70: re-anchor hook (opt-in): interval, anchor state, compaction reset, no rules, off switch, env interval, health row, errors
(
  set -e
  D="$T/f70"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W"
  export HANDOFF_STATE_DIR="$D/state"; HK="$ROOT/hooks/reanchor.py"; unset HANDOFF_REANCHOR HANDOFF_REANCHOR_TOKENS
  # a ledger with two rules for the project of $W (same key rule as the SubagentStart hook)
  KEY="$(python3 -c 'import hashlib,os,re,sys; r=sys.argv[1]; print(re.sub(r"[^A-Za-z0-9._-]","_",os.path.basename(r))[:40]+"-"+hashlib.sha1(r.encode()).hexdigest()[:8])' "$W")"
  mkdir -p "$HANDOFF_STATE_DIR/projects/$KEY"
  cat > "$HANDOFF_STATE_DIR/projects/$KEY/decisions.md" <<'MD'
- D2 [2026-11-01] [rule] Always run the linter before a commit | why: the user asked
- D1 [2026-11-01] [rule] Never push to main | why: the user asked
MD
  python3 - "$D" <<'PY'
import json, sys
d = sys.argv[1]
def tr(name, tokens):
    rows = [{"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
             "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 5, "output_tokens": 3}}}]
    open("%s/%s.jsonl" % (d, name), "w").write("".join(json.dumps(r) + "\n" for r in rows))
tr("t30k", 29985); tr("t60k", 59985); tr("t120k", 119985); tr("t20k", 19985)
PY
  ra() { python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"transcript_path":sys.argv[2],"cwd":sys.argv[3],"hook_event_name":"PostToolUse","tool_name":"Bash"}))' "$1" "$D/$2.jsonl" "$W" | python3 "$HK"; }
  [ "$(grep -c '^HOOK_VERSION = "1.21.0"$' "$HK")" = 1 ] || fail "F70: HOOK_VERSION"
  # below the interval: nothing, no state file
  [ -z "$(ra s1 t30k)" ] && [ ! -e "$HANDOFF_STATE_DIR/reanchor-s1.json" ] || fail "F70: output below 50,000"
  # at 60,000: exactly one JSON object, exact shape and first line, the rules block, the recall line is not required to exist here
  o="$(ra s1 t60k)"
  python3 - "$o" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
assert list(d) == ["hookSpecificOutput"] and d["hookSpecificOutput"]["hookEventName"] == "PostToolUse", d
t = d["hookSpecificOutput"]["additionalContext"]
assert t.split("\n")[0] == "Lossless Handoff re-anchor: the project's active rules, repeated so they stay near the end of your context.", t
assert "Never push to main" in t and "Always run the linter" in t, t
assert len(t) <= 4200, len(t)
PY
  # same count again: nothing (the anchor moved)
  [ -z "$(ra s1 t60k)" ] || fail "F70: second output at the same count"
  [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["anchor"])' "$HANDOFF_STATE_DIR/reanchor-s1.json")" = 60000 ] || fail "F70: anchor not stored"
  # 120,000 is 60,000 above the anchor: output again
  [ -n "$(ra s1 t120k)" ] || fail "F70: no output 60,000 above the anchor"
  # compaction: a count below the anchor resets the anchor and prints nothing; then 20,000 is not enough, nothing
  [ -z "$(ra s1 t20k)" ] || fail "F70: output on a compaction"
  [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["anchor"])' "$HANDOFF_STATE_DIR/reanchor-s1.json")" = 20000 ] || fail "F70: anchor not reset"
  # the interval env: 10,000 gives output at 30,000 from anchor 20,000? (10,000 above) and 600,000 is not valid (falls back to 50,000)
  [ -n "$(HANDOFF_REANCHOR_TOKENS=10000 ra s2 t30k)" ] || fail "F70: HANDOFF_REANCHOR_TOKENS=10000"
  [ -z "$(HANDOFF_REANCHOR_TOKENS=600000 ra s3 t30k)" ] && [ -z "$(HANDOFF_REANCHOR_TOKENS=abc ra s3 t30k)" ] || fail "F70: an invalid interval was used"
  [ -n "$(HANDOFF_REANCHOR_TOKENS=600000 ra s4 t60k)" ] || fail "F70: an invalid interval did not fall back to 50,000"
  # off switch
  [ -z "$(HANDOFF_REANCHOR=0 ra s5 t60k)" ] && [ ! -e "$HANDOFF_STATE_DIR/reanchor-s5.json" ] || fail "F70: HANDOFF_REANCHOR=0"
  # no active rules: no output
  mkdir -p "$D/empty"
  [ -z "$(python3 -c 'import json,sys; print(json.dumps({"session_id":"s6","transcript_path":sys.argv[1],"cwd":sys.argv[2]}))' "$D/t60k.jsonl" "$D/empty" | python3 "$HK")" ] || fail "F70: output with no rules"
  # a subagent event is ignored
  [ -z "$(python3 -c 'import json,sys; print(json.dumps({"session_id":"s7","agent_id":"a1","transcript_path":sys.argv[1],"cwd":sys.argv[2]}))' "$D/t60k.jsonl" "$W" | python3 "$HK")" ] || fail "F70: a subagent event printed"
  # errors: bad stdin, missing transcript, no usage line: exit 0 and no output
  r=0; o="$(echo "not json" | python3 "$HK")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F70: bad stdin"
  r=0; o="$(python3 -c 'import json; print(json.dumps({"session_id":"s8","transcript_path":"/no/such","cwd":"/"}))' | python3 "$HK")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F70: missing transcript"
  # health: one row per injection, exact fields
  python3 - "$HANDOFF_STATE_DIR/health.jsonl" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if '"reanchor"' in l]
assert len(rows) == 4, rows
assert all(set(r) == {"ts", "session_id", "event", "tokens", "chars"} and r["event"] == "reanchor" for r in rows), rows
assert all(isinstance(r["tokens"], int) and isinstance(r["chars"], int) and r["chars"] > 0 for r in rows), rows
assert "Never push" not in open(sys.argv[1]).read()
PY
  ok "F70: interval, anchor, compaction reset, env interval, off switch, no rules, subagent, errors, health row"
)
# F71: claim guard (opt-in Stop hook): cues, evidence, shadow, block, once per turn, loop guard, off switch
(
  set -e
  D="$T/f71"; mkdir -p "$D"; export HOME="$D/home"; mkdir -p "$HOME/.claude"
  export HANDOFF_STATE_DIR="$D/state"; HK="$ROOT/hooks/claim-guard.py"; LOG="$HANDOFF_STATE_DIR/claim-guard.jsonl"; unset HANDOFF_CLAIM_GUARD
  [ "$(grep -c '^HOOK_VERSION = "1.21.0"$' "$HK")" = 1 ] || fail "F71: HOOK_VERSION"
  python3 - "$D" <<'PY'
import json, sys
d = sys.argv[1]
def user(u, text): return {"type": "user", "uuid": u, "message": {"role": "user", "content": text}}
def tool(name, inp): return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": name, "input": inp}]}}
def res(): return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}
def txt(t): return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}
def w(name, rows): open("%s/%s.jsonl" % (d, name), "w").write("".join(json.dumps(r) + "\n" for r in rows))
w("none", [user("u1", "what port?"), tool("Read", {"file_path": "/x"}), res()])
w("bash", [user("u1", "what port?"), tool("Bash", {"command": "python3 ~/.claude/hooks/recall.py search port"}), res()])
w("mcp", [user("u1", "what port?"), tool("mcp__lossless-recall__search_earlier_chat", {"query": "port"}), res()])
w("auto", [user("u1", "what port?"), {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": ["Lossless Handoff auto-recall: earlier messages that match your words. Verify them before you rely on them."]}}])
w("old", [user("u0", "earlier"), tool("Bash", {"command": "python3 recall.py search x"}), res(), user("u1", "what port?"), tool("Read", {"file_path": "/x"}), res()])
w("othertool", [user("u1", "what port?"), tool("Bash", {"command": "ls recall"}), res()])
PY
  cg() { python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"transcript_path":sys.argv[2],"stop_hook_active":sys.argv[4]=="1","last_assistant_message":sys.argv[3]}))' "$1" "$D/$2.jsonl" "$3" "${4:-0}" | python3 "$HK"; }
  # shadow (default): one row, no output
  o="$(cg a1 none "As we decided, the port is 8080.")"
  [ -z "$o" ] || fail "F71: shadow mode printed: $o"
  python3 - "$LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
assert len(rows) == 1 and set(rows[0]) == {"ts", "session_id", "would_block", "cue"} and rows[0]["would_block"] is True and rows[0]["cue"] == "as we decided" and rows[0]["session_id"] == "a1", rows
PY
  ! grep -q "8080" "$LOG" || fail "F71: reply text in the log"
  # every cue fires (case-insensitive); a part of a word does not
  n0="$(wc -l < "$LOG")"
  for c in "as we decided" "we decided" "as you said" "you said" "we agreed" "as agreed" "as discussed" "you told me" "you mentioned" "as mentioned earlier" "earlier you" "last time"; do
    cg a2 none "Right. $(echo "$c" | tr a-z A-Z) it was fine." >/dev/null
  done
  [ "$(( $(wc -l < "$LOG") - n0 ))" = 12 ] || fail "F71: not every cue fired"
  n0="$(wc -l < "$LOG")"
  cg a3 none "The forecast is fine. Nothing was decided. We decidedly agree." >/dev/null
  cg a3 none "No cue in this reply at all." >/dev/null
  [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F71: a non-cue reply was logged"
  # evidence: bash recall.py, mcp tool, auto-recall injection: no row; evidence from an older turn does not count; a Bash call without recall.py does not count
  for t in bash mcp auto; do cg a4 "$t" "As you said, the port is 8080." >/dev/null; done
  [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F71: evidence did not stop the row"
  cg a5 old "As you said, the port is 8080." >/dev/null; cg a5 othertool "As you said, it is 8080." >/dev/null
  [ "$(( $(wc -l < "$LOG") ))" = "$((n0 + 2))" ] || fail "F71: old or unrelated evidence counted"
  # stop_hook_active: nothing
  n0="$(wc -l < "$LOG")"; o="$(HANDOFF_CLAIM_GUARD=block cg a6 none "As you said." 1)"
  [ -z "$o" ] && [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F71: stop_hook_active was not honoured"
  # block mode: the documented Stop output, once per user turn
  o="$(HANDOFF_CLAIM_GUARD=block cg a7 none "As you said, it is 8080.")"
  [ "$o" = '{"decision": "block", "reason": "You stated an earlier fact without searching. Search for it with recall, then correct or confirm it."}' ] || fail "F71: block output: $o"
  o="$(HANDOFF_CLAIM_GUARD=block cg a7 none "As you said, it is 8080.")"; [ -z "$o" ] || fail "F71: second block in one turn"
  o="$(HANDOFF_CLAIM_GUARD=block cg a8 none "As you said, it is 8080.")"; [ -n "$o" ] || fail "F71: a different session did not block"
  o="$(HANDOFF_CLAIM_GUARD=block cg a7 bash "As you said, it is 8080.")"; [ -z "$o" ] || fail "F71: block with evidence"
  # off switch: nothing and no row
  n0="$(wc -l < "$LOG")"; o="$(HANDOFF_CLAIM_GUARD=0 cg a9 none "As you said.")"
  [ -z "$o" ] && [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F71: HANDOFF_CLAIM_GUARD=0"
  # errors: exit 0, no output
  r=0; o="$(echo "not json" | python3 "$HK")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F71: bad stdin"
  r=0; o="$(python3 -c 'import json; print(json.dumps({"session_id":"b","transcript_path":"/no/such","last_assistant_message":"As you said."}))' | HANDOFF_CLAIM_GUARD=block python3 "$HK")" || r=$?
  [ "$r" = 0 ] && [ -z "$o" ] || fail "F71: missing transcript"
  ok "F71: cues, evidence, shadow, block form, once per turn, stop_hook_active, off switch, errors"
)
# F70/F71: install, default off, dry-run, hook entries, kept message, uninstall, saved defaults
(
  set -e
  export HOME="$T/f7071inst"; mkdir -p "$HOME/.claude"; F="$HOME/.claude/hooks"; FS="$HOME/.claude/settings.json"; DF="$HOME/.config/lossless-handoff/install-defaults"
  echo '{"model": "opus"}' > "$FS"
  for fl in reanchor claim-guard; do grep -q -- "--$fl" <<<"$("$ROOT/install.sh" --help)" || fail "F70/F71: --help lacks --$fl"; done
  "$ROOT/install.sh" >/dev/null
  [ ! -e "$F/reanchor.py" ] && [ ! -e "$F/claim-guard.py" ] && ! grep -Eq "reanchor|claim-guard" "$FS" "$F/.handoff-manifest.json" || fail "F70/F71: default install holds them"
  ! grep -Eq "reanchor|claim-guard" <<<"$("$ROOT/install.sh" --dry-run)" || fail "F70/F71: default dry-run mentions them"
  cp "$FS" "$T/f7071-before.json"
  o="$("$ROOT/install.sh" --reanchor --claim-guard --dry-run)"
  grep -q "install hook: .*/reanchor.py" <<<"$o" && grep -q "install hook: .*/claim-guard.py" <<<"$o" && grep -q "add PostToolUse reanchor hook" <<<"$o" && grep -q "add Stop claim-guard hook" <<<"$o" || fail "F70/F71: dry-run: $o"
  [ ! -e "$F/reanchor.py" ] && cmp -s "$FS" "$T/f7071-before.json" || fail "F70/F71: dry-run changed something"
  "$ROOT/install.sh" --reanchor --claim-guard >/dev/null
  [ -x "$F/reanchor.py" ] && [ -x "$F/claim-guard.py" ] && grep -q '"reanchor.py"' "$F/.handoff-manifest.json" && grep -q '"claim-guard.py"' "$F/.handoff-manifest.json" || fail "F70/F71: file or manifest"
  python3 - "$FS" "$F" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); f = sys.argv[2]
ra = "python3 %s/reanchor.py" % f; cg = "python3 %s/claim-guard.py" % f
g = [x for x in d["hooks"]["PostToolUse"] if any(h["command"] == ra for h in x["hooks"])]
assert len(g) == 1 and g[0]["matcher"] == "*" and [h for h in g[0]["hooks"] if h["command"] == ra] == [{"type": "command", "command": ra, "timeout": 5}], g
s = [x for x in d["hooks"]["Stop"] if any(h["command"] == cg for h in x["hooks"])]
assert len(s) == 1 and [h for h in s[0]["hooks"] if h["command"] == cg] == [{"type": "command", "command": cg, "timeout": 5}], s
assert d["model"] == "opus"
PY
  o="$("$ROOT/install.sh")"
  grep -qx "reanchor: already installed, kept" <<<"$o" && grep -qx "claim-guard: already installed, kept" <<<"$o" || fail "F70/F71: kept message: $o"
  o="$("$ROOT/install.sh" --no-reanchor --no-claim-guard)"
  grep -qx "reanchor: already installed, kept" <<<"$o" && [ -x "$F/reanchor.py" ] && grep -q "reanchor.py" "$FS" || fail "F70/F71: --no-* removed it"
  "$ROOT/install.sh" --reanchor --claim-guard >/dev/null
  [ "$(grep -c "reanchor.py" "$FS")" = 1 ] && [ "$(grep -c "claim-guard.py" "$FS")" = 1 ] || fail "F70/F71: registered twice"
  o="$("$ROOT/uninstall.sh" --dry-run)"; grep -q "reanchor.py" <<<"$o" && grep -q "claim-guard.py" <<<"$o" || fail "F70/F71: uninstall dry-run"
  [ -e "$F/reanchor.py" ] || fail "F70/F71: uninstall dry-run removed a file"
  "$ROOT/uninstall.sh" >/dev/null
  [ ! -e "$F/reanchor.py" ] && [ ! -e "$F/claim-guard.py" ] && ! grep -Eq "reanchor|claim-guard" "$FS" || fail "F70/F71: uninstall left something"
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d == {'model': 'opus'}, d" "$FS" || fail "F70/F71: settings not restored"
  # saved defaults
  "$ROOT/install.sh" --save-defaults --reanchor --claim-guard >/dev/null
  grep -qx "reanchor=1" "$DF" && grep -qx "claim_guard=1" "$DF" && [ -x "$F/reanchor.py" ] && [ -x "$F/claim-guard.py" ] || fail "F70/F71: --save-defaults"
  "$ROOT/uninstall.sh" >/dev/null
  o="$("$ROOT/install.sh" --dry-run)"; grep -q "saved defaults.*reanchor" <<<"$o" && grep -q "claim-guard.py" <<<"$o" || fail "F70/F71: saved default not applied: $o"
  "$ROOT/install.sh" --no-reanchor --no-claim-guard >/dev/null
  [ ! -e "$F/reanchor.py" ] && [ ! -e "$F/claim-guard.py" ] && grep -qx "reanchor=1" "$DF" || fail "F70/F71: --no-* did not skip, or touched the default"
  "$ROOT/install.sh" --save-defaults --no-reanchor --no-claim-guard >/dev/null
  ! grep -Eq "reanchor|claim_guard" "$DF" || fail "F70/F71: --save-defaults --no-* kept the lines"
  ok "F70/F71: install, default off, dry-run, entries, kept message, uninstall, saved defaults"
)

# F72: HANDOFF_HOOKS_HOME replaces ~/.claude/hooks, with no fallback and no mixing of two trees.
# HOME holds decoy files with other behaviour; a hook that reads one writes a line to $DECOY_LOG.
(
  set -e
  D="$T/f72"; mkdir -p "$D"; export HOME="$D/home"; REAL="$HOME/.claude/hooks"; PRIV="$D/priv"; W="$D/work"
  mkdir -p "$REAL" "$PRIV" "$W" "$D/state"; export HANDOFF_STATE_DIR="$D/state"; export DECOY_LOG="$D/decoy.log"; : > "$DECOY_LOG"
  unset HANDOFF_HOOKS_HOME HANDOFF_RECALL_RULES
  for f in recall.py postcompact-inject.py precompact-handoff.py reanchor.py; do
    printf 'import os\nopen(os.environ["DECOY_LOG"], "a").write("%s\\n")\nprint("DECOYOUT")\n' "$f" > "$REAL/$f"
  done
  : > "$REAL/.recall-rules"; : > "$REAL/recall-mcp.py"
  decoy_clean() { [ ! -s "$DECOY_LOG" ] || fail "F72: $1 read a decoy file: $(cat "$DECOY_LOG")"; }
  POST="$ROOT/hooks/postcompact-inject.py"
  sub() { printf '{"session_id":"s72","cwd":"%s","agent_id":"agx","agent_type":"general-purpose"}' "$W" | python3 "$POST" --subagent-start; }
  ctx() { python3 -c "import sys,json; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])"; }
  # postcompact-inject: a private folder without recall.py gives no recall line, although HOME has one (no fallback)
  o="$(HANDOFF_HOOKS_HOME="$PRIV" sub)"; [ -z "$o" ] || fail "F72: postcompact-inject used a file outside HANDOFF_HOOKS_HOME: $o"
  decoy_clean postcompact-inject
  # with recall.py in the private folder: the recall line, and the HOME marker and HOME recall-mcp.py are not seen
  cp "$ROOT/hooks/recall.py" "$PRIV/recall.py"
  o="$(HANDOFF_HOOKS_HOME="$PRIV" sub | ctx)"
  grep -q "^- recall: " <<<"$o" && ! grep -q "recall rule:" <<<"$o" && ! grep -q "search_earlier_chat" <<<"$o" || fail "F72: postcompact-inject saw the HOME marker or HOME recall-mcp.py: $o"
  # the private folder holds the marker and recall-mcp.py: both are seen
  : > "$PRIV/.recall-rules"; : > "$PRIV/recall-mcp.py"
  o="$(HANDOFF_HOOKS_HOME="$PRIV" sub | ctx)"
  grep -q "^- recall rule: " <<<"$o" && grep -q "search_earlier_chat" <<<"$o" || fail "F72: postcompact-inject ignored the private marker: $o"
  # HOME marker alone (override unset): seen, as before. Override not a folder: as if unset.
  o="$(sub | ctx)"; grep -q "^- recall rule: " <<<"$o" || fail "F72: unset override changed the behaviour: $o"
  o="$(HANDOFF_HOOKS_HOME="$D/nonexistent" sub | ctx)"; grep -q "^- recall rule: " <<<"$o" || fail "F72: a missing folder was not ignored: $o"
  o="$(HANDOFF_HOOKS_HOME="" sub | ctx)"; grep -q "^- recall rule: " <<<"$o" || fail "F72: an empty value was not ignored: $o"
  rm -f "$PRIV/.recall-rules" "$PRIV/recall-mcp.py" "$PRIV/recall.py"
  # auto-recall: the hook file sits in the repo next to a real recall.py, and HOME holds a decoy; neither is used
  PR="what did we decide earlier about the staging database rotation policy"
  o="$(printf '{"prompt":"%s","cwd":"%s","session_id":"s72"}' "$PR" "$W" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/auto-recall.py")"
  [ -z "$o" ] || fail "F72: auto-recall printed with an empty private folder: $o"
  decoy_clean auto-recall
  printf '{"prompt":"%s","cwd":"%s","session_id":"s72"}' "$PR" "$W" | python3 "$ROOT/hooks/auto-recall.py" >/dev/null || true
  grep -q "^recall.py$" "$DECOY_LOG" || fail "F72: control: without the override auto-recall should load the HOME copy"
  : > "$DECOY_LOG"
  # recall-mcp: a tool call with an empty private folder finds no recall.py (no HOME copy, no copy next to the file)
  o="$(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_earlier_chat","arguments":{"query":"staging"}}}' | (cd "$W" && HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/recall-mcp.py"))"
  grep -q '"isError": true' <<<"$o" && grep -q "recall.py not found" <<<"$o" || fail "F72: recall-mcp used a recall.py outside HANDOFF_HOOKS_HOME: $o"
  decoy_clean recall-mcp
  # reanchor: it needs postcompact-inject.py from the private folder; none there means no output
  python3 - "$D" <<'PY'
import json, sys
d = sys.argv[1]
rows = [{"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 59985, "cache_creation_input_tokens": 5, "output_tokens": 3}}}]
open(d + "/t60k.jsonl", "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  o="$(printf '{"session_id":"s72r","transcript_path":"%s","cwd":"%s","hook_event_name":"PostToolUse","tool_name":"Bash"}' "$D/t60k.jsonl" "$W" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/reanchor.py")"
  [ -z "$o" ] || fail "F72: reanchor printed with an empty private folder: $o"
  decoy_clean reanchor
  # claim-guard reads no file from the hooks folder: the same output with the override and with decoys
  o="$(printf '{"session_id":"s72","transcript_path":"%s","stop_hook_active":false,"last_assistant_message":"Done."}' "$D/t60k.jsonl" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/claim-guard.py")"
  [ -z "$o" ] || fail "F72: claim-guard output: $o"
  decoy_clean claim-guard
  # precompact-handoff (and its parts): the recall line of the handoff comes only from the private folder
  python3 - "$D" "$W" <<'PY'
import json, sys
d = sys.argv[1]
rows = [{"type": "user", "message": {"content": "PRIVMARK please fix the importer"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Noted."}]}}]
open(d + "/pre72.jsonl", "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
PY
  pre() { printf '{"session_id":"%s","cwd":"%s","transcript_path":"%s","trigger":"auto"}' "$1" "$W" "$D/pre72.jsonl" | HANDOFF_HOOKS_HOME="$PRIV" HANDOFF_LEDGER=0 python3 "$ROOT/hooks/precompact-handoff.py" >/dev/null 2>&1 || true; }
  pre p72a
  grep -q "PRIVMARK" "$D/state/p72a-latest.md" && ! grep -q "^- recall: " "$D/state/p72a-latest.md" || fail "F72: precompact-handoff used a recall.py outside HANDOFF_HOOKS_HOME"
  cp "$ROOT/hooks/recall.py" "$PRIV/recall.py"; pre p72b
  grep -q "^- recall: " "$D/state/p72b-latest.md" || fail "F72: precompact-handoff ignored the private recall.py"
  decoy_clean precompact-handoff
  ok "F72: no hook or part reads ~/.claude/hooks when HANDOFF_HOOKS_HOME is a folder; unset, empty or missing value: as before"
)

# PR #32 review fixes: parallel re-anchor, notification turns, foreign hook on a default install, override path in printed commands,
# agents file under the override, F68 without a ledger, a query that starts with "-", the kept line, file modes
(
  set -e
  D="$T/rv"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W" "$D/fakebin"
  export HANDOFF_STATE_DIR="$D/state"; mkdir -p "$HANDOFF_STATE_DIR"; unset HANDOFF_HOOKS_HOME HANDOFF_RECALL_RULES HANDOFF_REANCHOR HANDOFF_REANCHOR_TOKENS HANDOFF_CLAIM_GUARD
  mode_of() { python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }
  # a ledger with one rule for $W, and a transcript past 50,000 tokens
  KEY="$(python3 -c 'import hashlib,os,re,sys; r=os.path.realpath(sys.argv[1]); print(re.sub(r"[^A-Za-z0-9._-]","_",os.path.basename(r))[:40]+"-"+hashlib.sha1(r.encode()).hexdigest()[:8])' "$W")"
  git -C "$W" init -q
  KEY="$(python3 -c 'import hashlib,os,re,sys; r=os.path.realpath(sys.argv[1]); print(re.sub(r"[^A-Za-z0-9._-]","_",os.path.basename(r))[:40]+"-"+hashlib.sha1(r.encode()).hexdigest()[:8])' "$W")"
  mkdir -p "$HANDOFF_STATE_DIR/projects/$KEY"
  echo "- D1 [2026-11-01] [rule] Never push to main | why: the user asked" > "$HANDOFF_STATE_DIR/projects/$KEY/decisions.md"
  python3 - "$D" <<'PY'
import json, sys
d = sys.argv[1]
rows = [{"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 59985, "cache_creation_input_tokens": 5, "output_tokens": 3}}},
        {"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": "x"}],
         "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}}}]
open(d + "/t60k.jsonl", "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  # 8 parallel re-anchor runs inject once; the block has no SubagentStart preamble; the state files are 0600
  for i in 1 2 3 4 5 6 7 8; do
    printf '{"session_id":"par","transcript_path":"%s","cwd":"%s","hook_event_name":"PostToolUse","tool_name":"Bash"}' "$D/t60k.jsonl" "$W" | python3 "$ROOT/hooks/reanchor.py" > "$D/par$i.out" &
  done
  wait
  n=0; for i in 1 2 3 4 5 6 7 8; do [ -s "$D/par$i.out" ] && n=$((n + 1)); done
  [ "$n" = 1 ] || fail "F70: $n of 8 parallel runs injected"
  cat "$D"/par*.out | grep -q "Never push to main" || fail "F70: the rule is missing from the injection"
  ! cat "$D"/par*.out | grep -q "These are the user's standing rules for the main session" || fail "F70: the SubagentStart preamble is in the re-anchor text"
  [ "$(mode_of "$HANDOFF_STATE_DIR/reanchor-par.json")" = 600 ] && [ "$(mode_of "$HANDOFF_STATE_DIR/reanchor-par.lock")" = 600 ] || fail "F70: state file mode"
  [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["anchor"])' "$HANDOFF_STATE_DIR/reanchor-par.json")" = 60000 ] || fail "F70: a zero-usage entry set the anchor"
  # F71: a task-notification entry and a compact summary are not user messages
  python3 - "$D" <<'PY'
import json, sys
d = sys.argv[1]
rows = [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": "please check the config"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python3 ~/.claude/hooks/recall.py search config"}}]}},
        {"type": "user", "uuid": "u2", "message": {"role": "user", "content": "<task-notification>done</task-notification>"}},
        {"type": "user", "uuid": "u3", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued from a summary"}}]
open(d + "/cg.jsonl", "w").write("".join(json.dumps(r) + "\n" for r in rows))
rows2 = rows[:1] + [{"type": "user", "uuid": "u4", "message": {"role": "user", "content": "and what else"}}]
open(d + "/cg2.jsonl", "w").write("".join(json.dumps(r) + "\n" for r in rows2))
PY
  cg() { printf '{"session_id":"cg","transcript_path":"%s","stop_hook_active":false,"last_assistant_message":"As we decided, use sqlite."}' "$1" | python3 "$ROOT/hooks/claim-guard.py"; }
  cg "$D/cg.jsonl" >/dev/null; [ ! -e "$HANDOFF_STATE_DIR/claim-guard.jsonl" ] || fail "F71: a notification or compact-summary entry reset the user turn"
  cg "$D/cg2.jsonl" >/dev/null; [ -s "$HANDOFF_STATE_DIR/claim-guard.jsonl" ] || fail "F71: control: a cue with no evidence was not logged"
  [ "$(mode_of "$HANDOFF_STATE_DIR/claim-guard.jsonl")" = 600 ] || fail "F71: claim-guard.jsonl mode"
  # F71: no lookback before the last user message (an injection before it is not evidence)
  python3 - "$D" <<'PY'
import json, sys
d = sys.argv[1]
rows = [{"type": "attachment", "attachment": {"content": "Lossless Handoff auto-recall: earlier messages"}},
        {"type": "user", "uuid": "u5", "message": {"role": "user", "content": "what did we decide"}}]
open(d + "/cg3.jsonl", "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  n0="$(wc -l < "$HANDOFF_STATE_DIR/claim-guard.jsonl" | tr -d " ")"; cg "$D/cg3.jsonl" >/dev/null
  [ "$(wc -l < "$HANDOFF_STATE_DIR/claim-guard.jsonl" | tr -d " ")" = $((n0 + 1)) ] || fail "F71: an injection before the last user message counted as evidence"
  # default install: a foreign reanchor.py or claim-guard.py hook keeps exit 0, as on main
  mkdir -p "$HOME/.claude"
  cat > "$HOME/.claude/settings.json" <<'JSON'
{"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 /opt/other/reanchor.py"}]}],
           "Stop": [{"hooks": [{"type": "command", "command": "python3 /opt/other/claim-guard.py"}]}]}}
JSON
  r=0; "$ROOT/install.sh" >/dev/null 2>&1 || r=$?
  [ "$r" = 0 ] || fail "F70/F71: a default install exited $r because of a foreign reanchor.py or claim-guard.py hook"
  grep -q "/opt/other/reanchor.py" "$HOME/.claude/settings.json" || fail "F70: the foreign hook was removed"
  "$ROOT/uninstall.sh" >/dev/null 2>&1 || true
  # F72: printed recall commands, the footer and the agents file use the override folder
  PRIV="$D/priv"; mkdir -p "$PRIV/agents"; cp "$ROOT/hooks/recall.py" "$PRIV/recall.py"
  o="$(printf '{"session_id":"x","cwd":"%s","agent_id":"a","agent_type":"g"}' "$W" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/postcompact-inject.py" --subagent-start)"
  python3 - "$o" "$PRIV" <<'PY'
import json, sys
c = json.loads(sys.argv[1])["hookSpecificOutput"]["additionalContext"]; p = sys.argv[2]
assert "python3 %s/recall.py search" % p in c and "~/.claude/hooks/recall.py" not in c, c
assert "ask the recall subagent" not in c, c
PY
  mkdir -p "$HOME/.claude/agents"; printf '<!-- lossless-handoff:recall-agent -->\n' > "$HOME/.claude/agents/recall.md"
  o="$(printf '{"session_id":"x","cwd":"%s","agent_id":"a","agent_type":"g"}' "$W" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/postcompact-inject.py" --subagent-start)"
  ! grep -q "ask the recall subagent" <<<"$o" || fail "F72: the HOME agents file was used under the override"
  printf '<!-- lossless-handoff:recall-agent -->\n' > "$PRIV/agents/recall.md"
  o="$(printf '{"session_id":"x","cwd":"%s","agent_id":"a","agent_type":"g"}' "$W" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/postcompact-inject.py" --subagent-start)"
  grep -q "ask the recall subagent" <<<"$o" || fail "F72: the override agents file was not used"
  # F68: with the marker and no ledger, startup prints the recall line and the rule line; without it, nothing
  W2="$D/work2"; mkdir -p "$W2"; git -C "$W2" init -q; : > "$PRIV/.recall-rules"
  o="$(printf '{"cwd":"%s"}' "$W2" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/postcompact-inject.py" --ledger --part 1)"
  python3 - "$o" "$PRIV" <<'PY'
import json, sys
c = json.loads(sys.argv[1])["hookSpecificOutput"]["additionalContext"].split("\n")
assert c[0].startswith("- recall: ") and "%s/recall.py" % sys.argv[2] in c[0], c
assert c[1].startswith("- recall rule: ") and len(c) == 2, c
PY
  rm -f "$PRIV/.recall-rules"
  [ -z "$(printf '{"cwd":"%s"}' "$W2" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/postcompact-inject.py" --ledger --part 1)" ] || fail "F68: output without the marker and without a ledger"
  # F72: the auto-recall footer
  python3 - "$HOME" "$W" <<'PY'
import json, os, re, sys
home, w = sys.argv[1:3]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real)); os.makedirs(pd)
rows = [{"type": "user", "timestamp": "2026-11-19T10:00:00Z", "cwd": real, "sessionId": "rvsess", "uuid": "u1",
         "message": {"role": "user", "content": "Our staging database rotation policy is every ninety days for all credentials"}},
        {"type": "user", "timestamp": "2026-11-19T10:05:00Z", "cwd": real, "sessionId": "rvsess", "uuid": "u2",
         "message": {"role": "user", "content": "A later prompt about something else"}}]
open(os.path.join(pd, "rvsess.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  o="$(printf '{"prompt":"what did we decide earlier about the staging database rotation policy","cwd":"%s","session_id":"rvcur"}' "$W" | HANDOFF_HOOKS_HOME="$PRIV" python3 "$ROOT/hooks/auto-recall.py")"
  [ "$(tail -n 1 <<<"$o")" = "Open one in full: python3 $PRIV/recall.py open <id>" ] || fail "F72: auto-recall footer: $o"
  # F69: a query that starts with "-" is text; the project comes from CLAUDE_PROJECT_DIR; the fallback protocol
  o="$(printf '%s\n%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_earlier_chat","arguments":{"query":"-x"}}}' '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}' | (cd "$D" && CLAUDE_PROJECT_DIR="$W" python3 "$ROOT/hooks/recall-mcp.py"))"
  python3 - "$o" <<'PY'
import json, sys
rows = [json.loads(x) for x in sys.argv[1].splitlines()]
assert rows[0]["result"].get("isError") in (None, False) and rows[0]["result"]["content"][0]["text"] == "no matches", rows[0]
assert rows[1]["result"]["protocolVersion"] == "2025-11-25", rows[1]
PY
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_earlier_chat","arguments":{"query":"staging"}}}' | (cd "$D" && CLAUDE_PROJECT_DIR="$W" python3 "$ROOT/hooks/recall-mcp.py") | grep -q "ninety days" || fail "F69: CLAUDE_PROJECT_DIR was not used"
  [ "$(mode_of "$HANDOFF_STATE_DIR/recall-mcp.jsonl")" = 600 ] || fail "F69: recall-mcp.jsonl mode"
  # F69: the server is registered already: the kept line, and no add call
  cat > "$D/fakebin/claude" <<'SH'
#!/bin/sh
echo "$*" >> "$FAKE_CLAUDE_LOG"
if [ "$1 $2" = "mcp get" ]; then echo "lossless-recall:"; echo "  Status: Connected"; fi
exit 0
SH
  chmod +x "$D/fakebin/claude"; export FAKE_CLAUDE_LOG="$D/claude.args"; : > "$FAKE_CLAUDE_LOG"
  o="$(PATH="$D/fakebin:$PATH" "$ROOT/install.sh" --recall-mcp)"
  grep -qx "recall-mcp: already installed, kept" <<<"$o" && ! grep -q "mcp add" "$FAKE_CLAUDE_LOG" || fail "F69: registered server: $o / $(cat "$FAKE_CLAUDE_LOG")"
  PATH="$D/fakebin:$PATH" "$ROOT/uninstall.sh" >/dev/null 2>&1 || true
  ok "PR #32 review fixes: parallel re-anchor, notification turns, no lookback, foreign hook, override paths, agents file, no-ledger rule line, -x query, kept line, file modes"
)

# F73 key history, F74 search fixes, F75 auto-recall identifiers
(
  set -e
  D="$T/f73"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W"
  export HANDOFF_STATE_DIR="$D/state"; unset HANDOFF_HOOKS_HOME HANDOFF_AUTO_RECALL
  git -C "$W" init -q; RC="$ROOT/hooks/recall.py"
  python3 - "$HOME" "$W" <<'PY'
import json, os, re, sys
home, w = sys.argv[1:3]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real)); os.makedirs(pd)
def rec(t, ts, text, sess, uuid):
    return {"type": t, "timestamp": ts, "cwd": real, "sessionId": sess, "uuid": uuid,
            "message": {"role": t, "content": text if t == "user" else [{"type": "text", "text": text}]}}
rows = [rec("user", "2026-11-19T10:00:00Z", "Set the invoice-mailer batch size to 50. Nothing else changes. Thanks.", "aaaaaaaa", "U1"),
        rec("assistant", "2026-11-19T10:01:00Z", "Done: invoice-mailer batch size is now 50.", "aaaaaaaa", "U2"),
        rec("user", "2026-11-19T10:02:00Z", "Also invoice-mailer-eu uses 10, invoice-mailer.v2 uses 5 and xinvoice-mailer uses 1.", "aaaaaaaa", "U3"),
        rec("user", "2026-11-19T10:03:00Z", "Change `invoice-mailer` batch size to 75, (invoice-mailer), invoice-mailer, ok.", "aaaaaaaa", "U4"),
        {"type": "assistant", "timestamp": "2026-11-19T10:04:00Z", "uuid": "U5", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tc1", "name": "Bash", "input": {"command": "python3 recall.py history invoice-mailer"}},
            {"type": "tool_use", "id": "tc2", "name": "mcp__lossless-recall__key_history", "input": {"identifier": "invoice-mailer"}}]}},
        {"type": "user", "timestamp": "2026-11-19T10:04:01Z", "uuid": "U6", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tc1", "content": "RECALLRESULT invoice-mailer stale"},
            {"type": "tool_result", "tool_use_id": "tc2", "content": "MCPRESULT invoice-mailer stale"}]}},
        rec("user", "2026-11-19T10:04:30Z", "Lossless Handoff auto-recall: earlier messages INJECTED invoice-mailer", "aaaaaaaa", "U8"),
        rec("user", "2026-11-19T10:05:00Z", "Rollback invoice-mailer to 50 (dry run only).", "aaaaaaaa", "U7")]
open(os.path.join(pd, "aaaaaaaa.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
fork = [dict(r) for r in rows[:4]] + [rec("user", "2026-11-19T11:00:00Z", "Final: invoice-mailer batch size is 100.", "bbbbbbbb", "U9")]
open(os.path.join(pd, "bbbbbbbb.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in fork))
PY
  cd "$W"
  o="$(python3 "$RC" history invoice-mailer)"
  [ "$(head -n 1 <<<"$o")" = "history of invoice-mailer: 5 mention(s), oldest first. A later mention can be a rejected proposal, a dry run or an undo; read all of them." ] || fail "F73: line 1: $o"
  [ "$(grep -c '^- ' <<<"$o")" = 5 ] || fail "F73: entry count: $o"
  grep -q "^- aaaaaaaa:1 2026-11-19 10:00 user: Set the invoice-mailer batch size to 50. Nothing else changes.$" <<<"$o" && ! grep -q "Thanks" <<<"$o" || fail "F73: entry 1 (sentence window): $o"
  ! grep -q "invoice-mailer-eu\|invoice-mailer.v2\|xinvoice" <<<"$o" || fail "F73: a look-alike key matched: $o"
  ! grep -q "RECALLRESULT\|MCPRESULT\|INJECTED\|recall.py history" <<<"$o" || fail "F73: an excluded text is in the history: $o"
  grep -q '`invoice-mailer`' <<<"$o" && grep -q "bbbbbbbb:5 2026-11-19 11:00 user: Final" <<<"$o" || fail "F73: backtick entry or fork entry missing: $o"
  [ "$(grep -c "Change" <<<"$o")" = 1 ] || fail "F73: the fork copy of the parent history was not removed"
  [ "$(grep '^- ' <<<"$o" | sed -n '5p' | cut -c1-29)" = "- bbbbbbbb:5 2026-11-19 11:00" ] || fail "F73: not oldest first"
  [ "$(python3 "$RC" history nothing-here)" = "history of nothing-here: no mentions" ] || fail "F73: no-match line"
  [ "$(python3 "$RC" history INVOICE-MAILER --role assistant | grep -c '^- ')" = 1 ] || fail "F73: --role or case"
  [ "$(python3 "$RC" history invoice-mailer.v2 | grep -c '^- ')" = 1 ] || fail "F73: a dotted identifier"
  # the cap: 4,000 characters; 2 oldest + newest + an omission line; --all has no cap
  python3 - "$HOME" "$W" <<'PY'
import json, os, re, sys
home, w = sys.argv[1:3]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real))
rows = [{"type": "user", "timestamp": "2026-11-20T10:%02d:00Z" % i, "cwd": real, "sessionId": "cccccccc", "uuid": "C%d" % i,
         "message": {"role": "user", "content": "capkey step %d is %s" % (i, "word " * 40)}} for i in range(40)]
open(os.path.join(pd, "cccccccc.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
PY
  o="$(python3 "$RC" history capkey)"
  [ "${#o}" -le 4000 ] || fail "F73: cap: ${#o}"
  python3 - "$o" "$RC" <<'PY'
import sys
L = sys.argv[1].split("\n")
assert L[0].startswith("history of capkey: 40 mention(s)"), L[0]
om = [i for i, x in enumerate(L) if x.startswith("[") and "omitted in the middle; run: python3 " in x and x.endswith("history capkey --all]")]
assert len(om) == 1 and om[0] == 3, L[:5]
assert "step 0 " in L[1] and "step 1 " in L[2] and "step 39 " in L[-1], (L[1], L[2], L[-1])
k = int(L[3].split("]")[0].split()[0][1:]); assert k == 40 - 2 - (len(L) - 4), (k, len(L))
PY
  [ "$(python3 "$RC" history capkey --all | grep -c '^- ')" = 40 ] || fail "F73: --all"
  # F74: whole tokens rank above substrings (inside a group, the old order), exclusions apply to search
  o="$(python3 "$RC" search "invoice-mailer" --limit 10 --keyword)"
  ! grep -q "RECALLRESULT\|MCPRESULT\|INJECTED" <<<"$o" || fail "F74: an excluded text is in the search: $o"
  [ "$(grep -c "Change" <<<"$o")" = 1 ] || fail "F74: duplicate from the fork"
  python3 - "$o" <<'PY'
import sys
L = [x for x in sys.argv[1].split("\n") if " [word]: " in x]
whole = [i for i, x in enumerate(L) if "Also invoice-mailer-eu" not in x]
sub = [i for i, x in enumerate(L) if "Also invoice-mailer-eu" in x]
assert whole and sub and max(whole) < min(sub), L
PY
  touch -t 203001010000 "$HOME"/.claude/projects/*/bbbbbbbb.jsonl
  # MCP: key_history, descriptions, instructions, the current prompt is left out
  cat > "$D/mcp.py" <<'PY'
import json, os, subprocess, sys
ms, cwd = sys.argv[1:3]
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "key_history", "arguments": {"identifier": "invoice-mailer"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "key_history", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "key_history", "arguments": {"identifier": "capkey"}}}]
p = subprocess.run([sys.executable, ms], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, cwd=cwd, timeout=60,
               env=dict(os.environ, CLAUDE_CODE_SESSION_ID="bbbbbbbb"))
by = {o["id"]: o for o in map(json.loads, p.stdout.splitlines())}
assert "For the current value of a setting, key or decision, call key_history and read every mention: the newest mention can be a rejected proposal." in by[1]["result"]["instructions"]
t = {x["name"]: x for x in by[2]["result"]["tools"]}
assert set(t) == {"search_earlier_chat", "key_history", "open_message"}
assert t["key_history"]["_meta"] == {"anthropic/alwaysLoad": True} and t["key_history"]["inputSchema"]["required"] == ["identifier"]
assert t["key_history"]["description"] == "Every mention of one identifier (a key, file, service, setting or name) in this project's chats, oldest first. Use it before you state the current value of anything that can change."
assert "Hits are newest first. A newer hit can be a rejected proposal or a dry run. For the history of one identifier, use key_history." in t["search_earlier_chat"]["description"]
x = by[3]["result"]["content"][0]["text"]
assert x.startswith("history of invoice-mailer: 4 mention(s)") and "Final: invoice-mailer batch size is 100" not in x, x  # the newest user prompt of the newest session is the current prompt
assert by[4]["result"]["isError"] is True
assert len(by[5]["result"]["content"][0]["text"]) <= 4000
PY
  python3 "$D/mcp.py" "$ROOT/hooks/recall-mcp.py" "$W" || fail "F73: MCP key_history"
  grep -q '"tool": "key_history"' "$HANDOFF_STATE_DIR/recall-mcp.jsonl" || fail "F73: key_history health row"
  # F75: identifiers
  ar() { python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"cwd":sys.argv[2],"session_id":"f75","transcript_path":""}))' "$1" "$W" | python3 "$ROOT/hooks/auto-recall.py"; }
  o="$(ar "what did we decide earlier about invoice-mailer and nothing.else/at-all")"
  [ "$(head -n 1 <<<"$o")" = "Lossless Handoff auto-recall: the history of the identifiers in your prompt, oldest first. A later mention can be rejected, a dry run or undone. This can be partial; search for the rest." ] || fail "F75: header: $o"
  [ "$(tail -n 1 <<<"$o")" = "Full history: python3 ~/.claude/hooks/recall.py history invoice-mailer --all" ] || fail "F75: last line: $o"
  grep -q "^history of invoice-mailer: 5 mention" <<<"$o" && ! grep -q "nothing.else" <<<"$o" || fail "F75: block: $o"
  [ "${#o}" -le 4200 ] || fail "F75: output too long"
  o="$(ar "what did we decide earlier about https://example.com/a-b and the invoice batch rejected values")"
  [ "$(head -n 1 <<<"$o")" = "Lossless Handoff auto-recall: earlier messages that match your words. Verify them before you rely on them." ] || fail "F75: no identifier must use the word search: $o"
  python3 - "$HANDOFF_STATE_DIR/auto-recall.jsonl" <<'PY'
import json, sys
rows = [json.loads(x) for x in open(sys.argv[1]) if x.strip()]
assert rows[0]["mode"] == "history" and rows[0]["identifiers"] == 2 and rows[0]["hits"] == 5, rows
assert rows[1]["mode"] == "search" and rows[1]["identifiers"] == 0, rows
PY
  python3 - <<PY
import importlib.util
spec = importlib.util.spec_from_file_location("ar", "$ROOT/hooks/auto-recall.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert m.identifiers("fix \`MyKey\` \`current\` \`foo\` in a/b.c, see x_y and e.g. 2026-11-19 or https://h.io/p-q") == ["a/b.c", "x_y", "MyKey"], m.identifiers("fix \`MyKey\` \`current\` \`foo\` in a/b.c, see x_y and e.g. 2026-11-19 or https://h.io/p-q")
assert m.identifiers("plain words only") == []
assert m.query_words("what did we decide earlier about previous rejected proposal values staging database") == ["database", "staging"] or set(m.query_words("what did we decide earlier about previous rejected proposal values staging database")) == {"staging", "database"}
PY
  ok "F73-F75: history output, token boundary, exclusions, fork dedupe, cap, search ranking, key_history tool, auto-recall identifiers and health"
)

# F77, F78, F80: history guard (Stop), size trigger (auto-recall), action guard (PostToolBatch)
(
  set -e
  mode_of() { python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }
  D="$T/f77"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; W2="$D/work2"; mkdir -p "$HOME/.claude" "$W" "$W2"
  export HANDOFF_STATE_DIR="$D/state"; unset HANDOFF_HOOKS_HOME HANDOFF_HISTORY_GUARD HANDOFF_ACTION_GUARD HANDOFF_AUTO_RECALL HANDOFF_AUTO_RECALL_SIZE HANDOFF_HISTORY_GUARD_TOKENS HANDOFF_ACTION_GUARD_TOKENS HANDOFF_AUTO_RECALL_TOKENS
  git -C "$W" init -q; : > "$W2/queue-limit"
  HG="$ROOT/hooks/history-guard.py"; AG="$ROOT/hooks/action-guard.py"; AR="$ROOT/hooks/auto-recall.py"
  for f in history-guard.py action-guard.py auto-recall.py guard_common.py; do
    [ "$(grep -c '^HOOK_VERSION = "1.21.0"$' "$ROOT/hooks/$f")" = 1 ] || fail "F77: HOOK_VERSION of $f"
  done
  python3 - "$HOME" "$W" "$D" <<'PY'
import json, os, re, sys
home, w, d = sys.argv[1:4]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real)); os.makedirs(pd)
def user(ts, text, u):
    return {"type": "user", "timestamp": ts, "cwd": real, "sessionId": "ssss0001", "uuid": u, "message": {"role": "user", "content": text}}
def asst(ts, blocks, tokens, u):
    return {"type": "assistant", "timestamp": ts, "cwd": real, "sessionId": "ssss0001", "uuid": u,
            "message": {"role": "assistant", "content": blocks, "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 10, "cache_creation_input_tokens": 0, "output_tokens": 5}}}
def text(t): return [{"type": "text", "text": t}]
def base(tokens):
    return [user("2026-11-22T10:00:00Z", "Set queue-limit to 50 for the mailer. The deploy region is eu-west-1.", "u1"),
            asst("2026-11-22T10:01:00Z", text("Done, queue-limit is 50."), tokens, "a1"),
            user("2026-11-22T10:02:00Z", "Change queue-limit to 75. Also queue-limit-eu stays at 10.", "u2"),
            asst("2026-11-22T10:03:00Z", text("Now queue-limit is 75."), tokens, "a2"),
            user("2026-11-22T10:04:00Z", "Undo that, queue-limit is back to 50.", "u3"),
            asst("2026-11-22T10:05:00Z", text("Undone."), tokens, "a3"),
            user("2026-11-22T10:06:00Z", "Please write the config file now.", "ucur")]
def w(name, rows):
    open(os.path.join(pd, name + ".jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
    os.symlink(os.path.join(pd, name + ".jsonl"), os.path.join(d, name + ".jsonl"))
big = 150000
w("main", base(big) + [asst("2026-11-22T10:07:00Z", text("Wrote the file."), big, "a4")])
w("small", base(50000) + [asst("2026-11-22T10:07:00Z", text("Wrote the file."), 50000, "a4")])
ev = base(big) + [asst("2026-11-22T10:07:00Z", [{"type": "tool_use", "id": "tc1", "name": "mcp__lossless-recall__key_history", "input": {"identifier": "queue-limit"}}], big, "a4"),
                  {"type": "user", "timestamp": "2026-11-22T10:07:01Z", "uuid": "r1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc1", "content": "history"}]}},
                  asst("2026-11-22T10:07:02Z", text("Wrote the file."), big, "a5")]
w("evidence", ev)
w("bashev", base(big) + [asst("2026-11-22T10:07:00Z", [{"type": "tool_use", "id": "tc1", "name": "Bash", "input": {"command": "python3 ~/.claude/hooks/recall.py history queue-limit"}}], big, "a4")])
w("injected", base(big) + [{"type": "attachment", "attachment": {"type": "hook_additional_context", "content": ["Lossless Handoff auto-recall: history of queue-limit: 4 mention(s), oldest first."]}},
                           asst("2026-11-22T10:07:02Z", text("Wrote the file."), big, "a5")])
# a chat where a key is stated one time only, and the guard tokens at 100,000
w("once", [user("2026-11-22T10:00:00Z", "The deploy region is eu-west-1.", "u1"), asst("2026-11-22T10:01:00Z", text("Noted."), big, "a1"),
           user("2026-11-22T10:02:00Z", "Please write the config file now.", "ucur"), asst("2026-11-22T10:03:00Z", text("Wrote it."), big, "a2")])
PY
  hg() { python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"transcript_path":sys.argv[2],"cwd":sys.argv[4],"stop_hook_active":sys.argv[5]=="1","last_assistant_message":sys.argv[3]}))' "$1" "$D/$2.jsonl" "$3" "${4:-$W}" "${5:-0}" | python3 "$HG"; }
  LOG="$HANDOFF_STATE_DIR/history-guard.jsonl"
  # shadow (default): one row, no output; the row has no text
  o="$(hg s1 main "I set the limit: queue-limit is 75.")"; [ -z "$o" ] || fail "F77: shadow mode printed: $o"
  python3 - "$LOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
assert len(rows) == 1 and set(rows[0]) == {"ts", "session_id", "mode", "fired", "subjects", "anchors", "tokens", "chars", "ms"}, rows
r = rows[0]; assert r["mode"] == "shadow" and r["fired"] is True and r["subjects"] == 1 and r["anchors"] == 1 and r["tokens"] == 150000 and r["session_id"] == "s1", r
PY
  ! grep -q "queue-limit\|limit is" "$LOG" || fail "F77: text in the log"   # not a bare "75": ts and ms values can hold it
  [ "$(mode_of "$LOG")" = 600 ] || fail "F77: log mode"
  # a fact stated one time, a sibling, a key never seen, a small context: no row
  n0="$(wc -l < "$LOG")"
  hg s2 once "The region is eu-west-1." >/dev/null
  hg s2 main "The europe value is queue-limit-eu and nothing else." >/dev/null
  hg s2 main "We could use queue-limit-v2 or other-name here." >/dev/null
  hg s2 small "I set queue-limit to 75." >/dev/null
  hg s2 main "No identifier in this reply at all." >/dev/null
  [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F77: a row for a once-fact, a sibling, an unknown key or a small context"
  # an existing path below cwd is not an anchor
  hg s2 main "I set queue-limit to 75." "$W2" >/dev/null; [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F77: an existing path was an anchor"
  # evidence in this turn: a key_history call, a recall.py history call, an injected block
  for t in evidence bashev injected; do hg s2 "$t" "I set queue-limit to 75." >/dev/null; done
  [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F77: evidence did not stop the row"
  # off switch and loop guard
  o="$(HANDOFF_HISTORY_GUARD=0 hg s3 main "I set queue-limit to 75.")"; [ -z "$o" ] && [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F77: switch 0"
  o="$(HANDOFF_HISTORY_GUARD=block hg s3 main "I set queue-limit to 75." "$W" 1)"; [ -z "$o" ] && [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F77: stop_hook_active"
  # the threshold env
  o="$(HANDOFF_HISTORY_GUARD_TOKENS=200000 hg s3 main "I set queue-limit to 75.")"; [ "$(wc -l < "$LOG")" = "$n0" ] || fail "F77: threshold env (high)"
  HANDOFF_HISTORY_GUARD_TOKENS=40000 hg s3 small "I set queue-limit to 75." >/dev/null; [ "$(( $(wc -l < "$LOG") ))" = "$((n0 + 1))" ] || fail "F77: threshold env (low)"
  # block mode: the documented Stop form, line 1 exact, the full history, once per user turn
  o="$(HANDOFF_HISTORY_GUARD=block hg s4 main "I set queue-limit to 75.")"
  python3 - "$o" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
assert list(d) == ["hookSpecificOutput"] and d["hookSpecificOutput"]["hookEventName"] == "Stop", d
t = d["hookSpecificOutput"]["additionalContext"]
l1, rest = t.split("\n", 1)
assert l1 == "Lossless Handoff history check: your reply uses values that changed in this chat. The full history is below, oldest first. A later mention can be a rejected proposal, a dry run or an undo. Read every mention in order. If your reply is correct, send the same reply again. If it is not, send the corrected reply. Do not mention this check.", l1
assert "history of queue-limit:" in rest and "50" in rest and "75" in rest and "Please write the config" not in rest and "queue-limit-eu" not in rest.replace("queue-limit-eu stays", ""), rest
assert len(t) <= 6000
PY
  o="$(HANDOFF_HISTORY_GUARD=block hg s4 main "I set queue-limit to 75.")"; [ -z "$o" ] || fail "F77: a second stop in the same turn fired"
  [ "$(mode_of "$HANDOFF_STATE_DIR/history-guard-s4.json")" = 600 ] || fail "F77: state file mode"
  o="$(HANDOFF_HISTORY_GUARD=block hg s5 main "I set queue-limit to 75.")"; [ -n "$o" ] || fail "F77: a different session did not fire"
  # errors: no output, exit 0
  r=0; o="$(echo "not json" | python3 "$HG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F77: bad stdin"
  r=0; o="$(echo '{"session_id":"x","transcript_path":"/no/such","last_assistant_message":"queue-limit"}' | HANDOFF_HISTORY_GUARD=block python3 "$HG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F77: missing transcript"
  r=0; o="$(echo '{"session_id":"x","transcript_path":"'"$D"'/main.jsonl","last_assistant_message":5}' | HANDOFF_HISTORY_GUARD=block python3 "$HG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F77: bad reply type"
  r=0; o="$(HANDOFF_STATE_DIR=/dev/null/x HANDOFF_HISTORY_GUARD=block hg s6 main "I set queue-limit to 75.")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F77: unwritable state dir"
  r=0; o="$(HANDOFF_HOOKS_HOME="$D/nohooks" HANDOFF_HISTORY_GUARD=block hg s7 main "I set queue-limit to 75.")" || r=$?; [ "$r" = 0 ] || fail "F77: bad hooks home"
  ok "F77: shadow row, no text, once-fact, sibling, small context, path, evidence, switch, loop guard, block form, once per turn, errors"

  # F80: action guard
  ag() { python3 -c 'import json,sys; print(json.dumps({"session_id":sys.argv[1],"transcript_path":sys.argv[2],"cwd":sys.argv[3],"hook_event_name":"PostToolBatch","tool_calls":json.loads(sys.argv[4])}))' "$1" "$D/$2.jsonl" "${4:-$W}" "$3" | python3 "$AG"; }
  ALOG="$HANDOFF_STATE_DIR/action-guard.jsonl"
  EDIT='[{"tool_name":"Edit","tool_input":{"file_path":"/x/app.conf","old_string":"queue-limit=75","new_string":"queue-limit=75\nmailer=on"}}]'
  BASH='[{"tool_name":"Bash","tool_input":{"command":"echo queue-limit=75 >> app.conf"}}]'
  OLDONLY='[{"tool_name":"Edit","tool_input":{"file_path":"/x/queue-limit.conf","old_string":"queue-limit=75","new_string":"mailer=on"}}]'
  READ='[{"tool_name":"Read","tool_input":{"file_path":"/x/queue-limit.conf"}}]'
  o="$(ag a1 main "$EDIT")"; [ -z "$o" ] || fail "F80: shadow printed: $o"
  python3 - "$ALOG" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
assert len(rows) == 1 and set(rows[0]) == {"ts", "session_id", "mode", "fired", "subjects", "anchors", "tokens", "chars", "ms", "tool"}, rows
assert rows[0]["mode"] == "shadow" and rows[0]["tool"] == "Edit" and rows[0]["subjects"] == 1 and rows[0]["tokens"] == 150000, rows
PY
  ! grep -q "queue-limit\|mailer" "$ALOG" || fail "F80: text in the log"
  [ "$(mode_of "$ALOG")" = 600 ] || fail "F80: log mode"
  # inject mode: an Edit with a changed key fires, the documented PostToolBatch form, line 1 exact, no deny
  o="$(HANDOFF_ACTION_GUARD=inject ag a2 main "$EDIT")"
  python3 - "$o" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
assert list(d) == ["hookSpecificOutput"] and d["hookSpecificOutput"]["hookEventName"] == "PostToolBatch", d
t = d["hookSpecificOutput"]["additionalContext"]
assert t.split("\n", 1)[0] == "Lossless Handoff history check: your last tool call used names whose values changed in this chat. The full history is below, oldest first. A later mention can be a rejected proposal, a dry run or an undo. If the call used an old, rejected or undone value, fix it now. If it is correct, go on. Do not mention this check.", t[:200]
assert "history of queue-limit:" in t and len(t) <= 6000
PY
  # a Bash command naming a changed key fires
  o="$(HANDOFF_ACTION_GUARD=inject ag a3 main "$BASH")"; [ -n "$o" ] && grep -q "history of queue-limit:" <<<"$o" || fail "F80: Bash did not fire: $o"
  # only old_string, a Read call, an existing path, a small context, the switch: nothing
  n0="$(wc -l < "$ALOG")"
  for c in "$OLDONLY" "$READ"; do o="$(HANDOFF_ACTION_GUARD=inject ag a4 main "$c")"; [ -z "$o" ] || fail "F80: fired on old_string or Read"; done
  o="$(HANDOFF_ACTION_GUARD=inject ag a4 main "$EDIT" "$W2")"; [ -z "$o" ] || fail "F80: an existing path fired"
  o="$(HANDOFF_ACTION_GUARD=inject ag a4 small "$EDIT")"; [ -z "$o" ] || fail "F80: a small context fired"
  o="$(HANDOFF_ACTION_GUARD=inject ag a4 once "$EDIT")"; [ -z "$o" ] || fail "F80: a once-fact fired"
  for t in evidence bashev injected; do o="$(HANDOFF_ACTION_GUARD=inject ag a4 "$t" "$EDIT")"; [ -z "$o" ] || fail "F80: evidence ($t) did not stop it"; done
  o="$(HANDOFF_ACTION_GUARD=0 ag a4 main "$EDIT")"; [ -z "$o" ] || fail "F80: switch 0"
  [ "$(wc -l < "$ALOG")" = "$n0" ] || fail "F80: a row was written for a case without a fire"
  # the shared cooldown: a key shown by F77 in this change does not fire again in F80 (and the other way round)
  o="$(HANDOFF_HISTORY_GUARD=block hg c1 main "I set queue-limit to 75.")"; [ -n "$o" ] || fail "F80: setup (F77 fire)"
  o="$(HANDOFF_ACTION_GUARD=inject ag c1 main "$EDIT")"; [ -z "$o" ] || fail "F80: fired again after F77"
  o="$(HANDOFF_ACTION_GUARD=inject ag c2 main "$EDIT")"; [ -n "$o" ] || fail "F80: setup (F80 fire)"
  o="$(HANDOFF_ACTION_GUARD=inject ag c2 main "$EDIT")"; [ -z "$o" ] || fail "F80: cooldown (same change)"
  o="$(HANDOFF_HISTORY_GUARD=block hg c2 main "I set queue-limit to 75.")"; [ -z "$o" ] || fail "F77: fired again after F80"
  # errors: no output, exit 0
  r=0; o="$(echo "not json" | python3 "$AG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F80: bad stdin"
  r=0; o="$(echo '{"session_id":"x","transcript_path":"/no/such","tool_calls":[]}' | HANDOFF_ACTION_GUARD=inject python3 "$AG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F80: missing transcript"
  r=0; o="$(echo '{"session_id":"x","transcript_path":"'"$D"'/main.jsonl","tool_calls":"nope"}' | HANDOFF_ACTION_GUARD=inject python3 "$AG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F80: bad tool_calls"
  r=0; o="$(echo '{"session_id":"x","transcript_path":"'"$D"'/main.jsonl","tool_calls":[5,{"tool_name":"Edit","tool_input":7}]}' | HANDOFF_ACTION_GUARD=inject python3 "$AG")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F80: bad call shapes"
  r=0; o="$(HANDOFF_STATE_DIR=/dev/null/x HANDOFF_ACTION_GUARD=inject ag c3 main "$EDIT")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F80: unwritable state dir"
  ok "F80: shadow row, inject form, Bash, old_string, path, small context, evidence, switch, shared cooldown with F77, errors"

  # review fixes: the current turn counts (long autonomous turn), shadow saves the cooldown, a tool result excludes an anchor,
  # stdin is read before the mode check, the state file holds no key text
  python3 - "$HOME" "$W" "$D" <<'PY'
import json, os, re, sys
home, w, d = sys.argv[1:4]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real))
def user(ts, text, u): return {"type": "user", "timestamp": ts, "cwd": real, "sessionId": "ssss0002", "uuid": u, "message": {"role": "user", "content": text}}
def asst(ts, blocks, u):
    return {"type": "assistant", "timestamp": ts, "cwd": real, "sessionId": "ssss0002", "uuid": u,
            "message": {"role": "assistant", "content": blocks, "usage": {"input_tokens": 10, "cache_read_input_tokens": 149990, "cache_creation_input_tokens": 0, "output_tokens": 5}}}
def text(t): return [{"type": "text", "text": t}]
def w2(name, rows):
    open(os.path.join(pd, name + ".jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
    os.symlink(os.path.join(pd, name + ".jsonl"), os.path.join(d, name + ".jsonl"))
# a user states retry-count one time, then a long turn: the prompt names it, the model changes it inside the turn
w2("turn", [user("2026-11-22T10:00:00Z", "Use retry-count 3 for the worker.", "t1"), asst("2026-11-22T10:01:00Z", text("Done."), "t2"),
            user("2026-11-22T10:02:00Z", "Work alone now. Keep retry-count as it is.", "tcur"),
            asst("2026-11-22T10:03:00Z", text("I will change retry-count to 5 for the test run."), "t3"),
            asst("2026-11-22T10:04:00Z", [{"type": "tool_use", "id": "x1", "name": "Edit", "input": {"file_path": "/x/w.conf", "old_string": "a", "new_string": "retry-count=5"}}], "t4")])
w2("turn2", [user("2026-11-22T10:00:00Z", "Use retry-count 3 for the worker.", "t1"), asst("2026-11-22T10:01:00Z", text("Done."), "t2"),
             user("2026-11-22T10:02:00Z", "Work alone now. Keep retry-count as it is.", "tcur"),
             asst("2026-11-22T10:03:00Z", text("I will change retry-count to 5 for the test run."), "t3"),
             asst("2026-11-22T10:04:00Z", text("Test run done."), "t4")])
w2("toolres", [user("2026-11-22T10:00:00Z", "Set queue-limit to 50.", "t1"), asst("2026-11-22T10:01:00Z", text("Done."), "t2"),
               user("2026-11-22T10:02:00Z", "Now queue-limit is 75, ok?", "t3"), asst("2026-11-22T10:03:00Z", text("Yes."), "t4"),
               user("2026-11-22T10:04:00Z", "Check the file.", "tcur"),
               asst("2026-11-22T10:05:00Z", [{"type": "tool_use", "id": "x1", "name": "Read", "input": {"file_path": "/x/w.conf"}}], "t5"),
               {"type": "user", "timestamp": "2026-11-22T10:05:01Z", "uuid": "r1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x1", "content": "queue-limit=75"}]}},
               asst("2026-11-22T10:06:00Z", text("The file is fine."), "t6")])
PY
  RET='[{"tool_name":"Edit","tool_input":{"file_path":"/x/w.conf","old_string":"a","new_string":"retry-count=3"}}]'
  o="$(HANDOFF_ACTION_GUARD=inject ag d1 turn "$RET")"; grep -q "history of retry-count:" <<<"$o" || fail "F80: a key changed in the current turn did not fire: $o"
  grep -q "retry-count to 5" <<<"$o" || fail "F80: the history block lacks the current turn: $o"
  o="$(HANDOFF_HISTORY_GUARD=block hg d2 turn2 "retry-count is now 5.")"; grep -q "history of retry-count:" <<<"$o" || fail "F77: current turn not counted: $o"
  # shadow saves the cooldown: the second shadow run for the same change logs no row
  n0="$(( $(wc -l < "$ALOG") ))"; ag d3 turn "$RET" >/dev/null; ag d3 turn "$RET" >/dev/null
  [ "$(( $(wc -l < "$ALOG") ))" = "$((n0 + 1))" ] || fail "F80: shadow did not save the cooldown"
  n0="$(( $(wc -l < "$LOG") ))"; hg d4 turn2 "retry-count is 5." >/dev/null; hg d4 turn2 "retry-count is 5." >/dev/null
  [ "$(( $(wc -l < "$LOG") ))" = "$((n0 + 1))" ] || fail "F77: shadow did not save the cooldown"
  # the state file holds a hash, not the key text
  ! grep -q "retry-count" "$HANDOFF_STATE_DIR/history-guard-d3.json" && [ "$(mode_of "$HANDOFF_STATE_DIR/history-guard-d3.json")" = 600 ] || fail "F77: key text in the state file"
  # a tool result of the turn excludes the anchor
  n0="$(( $(wc -l < "$LOG") ))"; hg d5 toolres "The queue-limit is 75." >/dev/null
  [ "$(( $(wc -l < "$LOG") ))" = "$n0" ] || fail "F77: a tool result did not exclude the anchor"
  # stdin is read before the mode check: a large input, switch 0, no broken pipe (pipefail is on)
  python3 -c 'import json; print(json.dumps({"session_id":"p","transcript_path":"/x","last_assistant_message":"x"*400000,"tool_calls":[]}))' > "$D/big.json"
  for i in 1 2 3 4 5 6 7 8; do
    HANDOFF_HISTORY_GUARD=0 python3 -c 'import sys; sys.stdout.write(open(sys.argv[1]).read())' "$D/big.json" | HANDOFF_HISTORY_GUARD=0 python3 "$HG"
    python3 -c 'import sys; sys.stdout.write(open(sys.argv[1]).read())' "$D/big.json" | HANDOFF_ACTION_GUARD=0 python3 "$AG"
    python3 -c 'import sys; sys.stdout.write(open(sys.argv[1]).read())' "$D/big.json" | HANDOFF_CLAIM_GUARD=0 python3 "$ROOT/hooks/claim-guard.py"
  done
  ok "review fixes: current turn counts, shadow cooldown, tool result excludes anchor, hashed state key, stdin read first"

  # backticked identifiers (Zeta, MaxVal) fire; a tool result of either content shape (string, list) excludes the anchor;
  # the guard fallback identifier rule equals auto-recall.identifiers on a corpus
  python3 - "$HOME" "$W" "$D" <<'PY'
import json, os, re, sys
home, w, d = sys.argv[1:4]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real))
def user(ts, text, u): return {"type": "user", "timestamp": ts, "cwd": real, "sessionId": "ssss0003", "uuid": u, "message": {"role": "user", "content": text}}
def asst(ts, blocks, u):
    return {"type": "assistant", "timestamp": ts, "cwd": real, "sessionId": "ssss0003", "uuid": u,
            "message": {"role": "assistant", "content": blocks, "usage": {"input_tokens": 10, "cache_read_input_tokens": 149990, "cache_creation_input_tokens": 0, "output_tokens": 5}}}
def text(t): return [{"type": "text", "text": t}]
def w3(name, rows):
    open(os.path.join(pd, name + ".jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
    os.symlink(os.path.join(pd, name + ".jsonl"), os.path.join(d, name + ".jsonl"))
head = [user("2026-11-22T10:00:00Z", "Set `Zeta` to 5 and `MaxVal` to 1.", "t1"), asst("2026-11-22T10:01:00Z", text("Done."), "t2"),
        user("2026-11-22T10:02:00Z", "Change `Zeta` to 7 and `MaxVal` to 2.", "t3"), asst("2026-11-22T10:03:00Z", text("Done."), "t4"),
        user("2026-11-22T10:04:00Z", "Check it.", "tcur")]
call = asst("2026-11-22T10:05:00Z", [{"type": "tool_use", "id": "x1", "name": "Read", "input": {"file_path": "/x/w.conf"}}], "t5")
def res(content): return {"type": "user", "timestamp": "2026-11-22T10:05:01Z", "uuid": "r1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x1", "content": content}]}}
fin = asst("2026-11-22T10:06:00Z", text("Fine."), "t6")
w3("zeta", head + [fin])
w3("trstr", head + [call, res("Zeta=7"), fin])
w3("trlist", head + [call, res([{"type": "text", "text": "Zeta=7"}]), fin])
PY
  o="$(HANDOFF_HISTORY_GUARD=block hg e1 zeta 'The `Zeta` is 10.')"; grep -q "history of Zeta:" <<<"$o" || fail "F77: a backticked Zeta did not fire: $o"
  o="$(HANDOFF_HISTORY_GUARD=block hg e2 zeta 'The `MaxVal` is 10.')"; grep -q "history of MaxVal:" <<<"$o" || fail "F77: a backticked MaxVal did not fire: $o"
  for t in trstr trlist; do o="$(HANDOFF_HISTORY_GUARD=block hg e3 "$t" 'The `Zeta` is 10.')"; [ -z "$o" ] || fail "F77: tool result ($t) did not exclude the anchor: $o"; done
  python3 - "$ROOT/hooks" <<'PY'
import importlib.util, sys
def load(n, p):
    s = importlib.util.spec_from_file_location(n, p); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
g = load("gc", sys.argv[1] + "/guard_common.py"); a = load("ar", sys.argv[1] + "/auto-recall.py")
corpus = ["The `Zeta` is 10.", "`MaxVal` and `dry` and `x1`", "set queue-limit and dry-run via a/b.c and_x https://a-b.c/d `my.key`", "as you said `value`",
          "e.g. and/or i.e. foo.bar, `Foo`, `a-b`, `eu-west-1` `v2`", "plain words only here", "`two words` `snake_case` Camel.Case x-y-z 1.2.3"]
for t in corpus:
    assert g.identifiers(t) == a.identifiers(t) == g._identifiers_fallback(t), (t, g.identifiers(t), a.identifiers(t), g._identifiers_fallback(t))
PY
  ok "review fixes 2: backticked identifiers, tool result shapes, one identifier rule"

  # Amendment A: the anchor is an identifier; its mentions are counted with the F73 token match (plain words count)
  python3 - "$HOME" "$W" "$D" <<'PY'
import json, os, re, sys
home, w, d = sys.argv[1:4]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real))
def rec(t, ts, content, u=None):
    r = {"type": t, "timestamp": ts, "cwd": real, "sessionId": "ssss0004", "parentUuid": None, "isSidechain": False, "userType": "external",
         "message": {"role": t, "content": content}}
    if u: r["uuid"] = u
    return r
rows = []
for name in ("Quark", "Vexel"):
    rows += [rec("user", "2026-03-01T10:00:00.000Z", "%s step 0 is the value 0" % name, "%s-0" % name),
             rec("assistant", "2026-03-01T10:01:00.000Z", [{"type": "text", "text": "%s step 1 is the value 10" % name}], "%s-1" % name),
             rec("user", "2026-03-01T10:02:00.000Z", "%s step 2 is the value 20" % name, "%s-2" % name)]
rows += [dict(rec("assistant", "2026-03-01T13:00:00Z", [{"type": "text", "text": "working"}]), message={"role": "assistant", "content": [{"type": "text", "text": "working"}], "usage": {"input_tokens": 150000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}}),
         rec("user", "2026-03-02T12:00:00.000Z", "please go on with the task", "cur-p")]
open(os.path.join(pd, "plain.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
os.symlink(os.path.join(pd, "plain.jsonl"), os.path.join(d, "plain.jsonl"))
PY
  for k in Quark Vexel; do
    o="$(HANDOFF_HISTORY_GUARD=block hg f1$k plain "The \`$k\` is 10.")"
    python3 - "$o" "$k" <<'PY'
import json, sys
t = json.loads(sys.argv[1])["hookSpecificOutput"]["additionalContext"]; k = sys.argv[2]
assert "history of %s: 3 mention(s)" % k in t, t
PY
  done
  o="$(HANDOFF_HISTORY_GUARD=block hg f2 plain "The \`VEXEL\` is 10.")"; grep -q "history of VEXEL: 3 mention(s)" <<<"$o" || fail "F77: case-insensitive count: $(cut -c1-300 <<<"$o")"
  o="$(HANDOFF_HISTORY_GUARD=block hg f3 plain "Quark is 10.")"; [ -z "$o" ] || fail "F77: a bare capital word fired"
  o="$(HANDOFF_ACTION_GUARD=inject ag f4 plain '[{"tool_name":"Bash","tool_input":{"command":"echo `Quark` 10"}}]')"; grep -q "history of Quark:" <<<"$o" || fail "F80: plain-word mentions not counted"
  ok "Amendment A: anchor is an identifier, mentions counted by the F73 token match (F77, F80)"

  # F78: size trigger in auto-recall
  mkdir -p "$HOME/.claude/hooks"
  ar() { python3 -c 'import json,sys; print(json.dumps({"session_id":"z1","transcript_path":sys.argv[1],"cwd":sys.argv[3],"prompt":sys.argv[2]}))' "$D/${3:-main}.jsonl" "$1" "${2:-$W}" | python3 "$AR"; }
  RLOG="$HANDOFF_STATE_DIR/auto-recall.jsonl"; P="Now write the file with the queue-limit value."
  [ -z "$(ar "$P")" ] || fail "F78: no marker, but output"
  : > "$HOME/.claude/hooks/.recall-size-trigger"
  o="$(ar "$P")"
  grep -q "^Lossless Handoff" <<<"$o" && grep -q "queue-limit" <<<"$o" && ! grep -q "queue-limit-eu" <<<"$(head -n 1 <<<"$o")" || fail "F78: no history output: $o"
  python3 - "$RLOG" <<'PY'
import json, sys
r = [json.loads(l) for l in open(sys.argv[1])][-1]
assert r["cue"] == "size" and r["mode"] == "history" and r["identifiers"] == 1 and r["fired"] is True, r
PY
  n0="$(wc -l < "$RLOG")"
  [ -z "$(HANDOFF_AUTO_RECALL_SIZE=0 ar "$P")" ] || fail "F78: switch 0"
  [ -z "$(ar "$P" "$W" small)" ] || fail "F78: small context"
  [ -z "$(HANDOFF_AUTO_RECALL_TOKENS=900000 ar "$P")" ] || fail "F78: token env"
  [ -z "$(ar "/compact queue-limit")" ] || fail "F78: slash prompt"
  [ -z "$(ar "Use the region eu-west-1 now.")" ] || fail "F78: a fact stated one time"
  [ -z "$(ar "$P" "$W2")" ] || fail "F78: an existing path"
  [ -z "$(ar "Write the file with no key at all.")" ] || fail "F78: no identifier"
  [ "$(wc -l < "$RLOG")" = "$n0" ] || fail "F78: a row for a no-fire case"
  o="$(ar "As we decided, queue-limit is 75.")"; [ "$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read().splitlines()[-1])["cue"] != "size")' "$RLOG")" = True ] || fail "F78: a cue prompt did not keep its cue"
  r=0; o="$(echo "not json" | python3 "$AR")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F78: bad stdin"
  r=0; o="$(python3 -c 'import json; print(json.dumps({"prompt":"queue-limit now","transcript_path":"/no/such","cwd":"/tmp"}))' | python3 "$AR")" || r=$?; [ "$r" = 0 ] && [ -z "$o" ] || fail "F78: missing transcript"
  ok "F78: marker gate, history output, cue size, switch, token env, slash, once-fact, path, no identifier, cue kept, errors"
)
# F77, F78, F80: install, default off, dry-run, entries, kept message, skip message, uninstall, saved defaults
(
  set -e
  export HOME="$T/f77inst"; mkdir -p "$HOME/.claude"; F="$HOME/.claude/hooks"; FS="$HOME/.claude/settings.json"; DF="$HOME/.config/lossless-handoff/install-defaults"
  echo '{"model": "opus"}' > "$FS"
  for fl in history-guard action-guard recall-size-trigger; do grep -q -- "--$fl" <<<"$("$ROOT/install.sh" --help)" || fail "F77: --help lacks --$fl"; done
  "$ROOT/install.sh" >/dev/null
  [ ! -e "$F/history-guard.py" ] && [ ! -e "$F/action-guard.py" ] && [ ! -e "$F/guard_common.py" ] && [ ! -e "$F/.recall-size-trigger" ] && ! grep -Eq "history-guard|action-guard|guard_common|recall-size" "$FS" "$F/.handoff-manifest.json" || fail "F77: the default install holds them"
  ! grep -Eq "history-guard|action-guard|recall-size" <<<"$("$ROOT/install.sh" --dry-run)" || fail "F77: default dry-run mentions them"
  # F78 without auto-recall: the skip message, exit 0, no marker
  r=0; o="$("$ROOT/install.sh" --recall-size-trigger)" || r=$?
  [ "$r" = 0 ] && grep -qx "recall-size-trigger: needs --auto-recall; skipped" <<<"$o" && [ ! -e "$F/.recall-size-trigger" ] || fail "F78: skip: $o"
  cp "$FS" "$T/f77-before.json"
  o="$("$ROOT/install.sh" --auto-recall --history-guard --action-guard --recall-size-trigger --dry-run)"
  grep -q "install hook: .*/history-guard.py" <<<"$o" && grep -q "install hook: .*/action-guard.py" <<<"$o" && grep -q "install hook: .*/guard_common.py" <<<"$o" && grep -q "add Stop history-guard hook" <<<"$o" && grep -q "add PostToolBatch action-guard hook" <<<"$o" && grep -q ".recall-size-trigger" <<<"$o" || fail "F77: dry-run: $o"
  [ ! -e "$F/history-guard.py" ] && cmp -s "$FS" "$T/f77-before.json" || fail "F77: dry-run changed something"
  "$ROOT/install.sh" --auto-recall --history-guard --action-guard --recall-size-trigger >/dev/null
  [ -x "$F/history-guard.py" ] && [ -x "$F/action-guard.py" ] && [ -e "$F/guard_common.py" ] && [ -e "$F/.recall-size-trigger" ] && grep -q '"history-guard.py"' "$F/.handoff-manifest.json" && grep -q '"action-guard.py"' "$F/.handoff-manifest.json" && grep -q '"guard_common.py"' "$F/.handoff-manifest.json" || fail "F77: files or manifest"
  python3 - "$FS" "$F" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); f = sys.argv[2]
hg = "python3 %s/history-guard.py" % f; ag = "python3 %s/action-guard.py" % f
s = [x for x in d["hooks"]["Stop"] if any(h["command"] == hg for h in x["hooks"])]
assert len(s) == 1 and [h for h in s[0]["hooks"] if h["command"] == hg] == [{"type": "command", "command": hg, "timeout": 5}], s
b = [x for x in d["hooks"]["PostToolBatch"] if any(h["command"] == ag for h in x["hooks"])]
assert len(b) == 1 and [h for h in b[0]["hooks"] if h["command"] == ag] == [{"type": "command", "command": ag, "timeout": 5}], b
assert d["model"] == "opus"
PY
  o="$("$ROOT/install.sh")"
  grep -qx "history-guard: already installed, kept" <<<"$o" && grep -qx "action-guard: already installed, kept" <<<"$o" && grep -qx "recall-size-trigger: already installed, kept" <<<"$o" || fail "F77: kept messages: $o"
  o="$("$ROOT/install.sh" --no-history-guard --no-action-guard --no-recall-size-trigger)"
  [ -x "$F/history-guard.py" ] && [ -e "$F/.recall-size-trigger" ] && grep -q "history-guard.py" "$FS" || fail "F77: --no-* removed something"
  "$ROOT/install.sh" --history-guard --action-guard >/dev/null
  [ "$(grep -c "history-guard.py" "$FS")" = 1 ] && [ "$(grep -c "action-guard.py" "$FS")" = 1 ] || fail "F77: registered twice"
  o="$("$ROOT/uninstall.sh" --dry-run)"; grep -q "history-guard.py" <<<"$o" && grep -q "action-guard.py" <<<"$o" && grep -q "recall-size-trigger" <<<"$o" || fail "F77: uninstall dry-run"
  [ -e "$F/history-guard.py" ] || fail "F77: uninstall dry-run removed a file"
  "$ROOT/uninstall.sh" >/dev/null
  [ ! -e "$F/history-guard.py" ] && [ ! -e "$F/action-guard.py" ] && [ ! -e "$F/guard_common.py" ] && [ ! -e "$F/.recall-size-trigger" ] && ! grep -Eq "history-guard|action-guard" "$FS" || fail "F77: uninstall left something"
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d == {'model': 'opus'}, d" "$FS" || fail "F77: settings not restored"
  # saved defaults
  "$ROOT/install.sh" --save-defaults --auto-recall --history-guard --action-guard --recall-size-trigger >/dev/null
  grep -qx "history_guard=1" "$DF" && grep -qx "action_guard=1" "$DF" && grep -qx "recall_size_trigger=1" "$DF" && [ -x "$F/history-guard.py" ] && [ -e "$F/.recall-size-trigger" ] || fail "F77: --save-defaults"
  "$ROOT/uninstall.sh" >/dev/null
  o="$("$ROOT/install.sh" --dry-run)"; grep -q "saved defaults.*history" <<<"$o" && grep -q "history-guard.py" <<<"$o" || fail "F77: saved default not applied: $o"
  "$ROOT/install.sh" --no-history-guard --no-action-guard --no-recall-size-trigger --no-auto-recall >/dev/null
  [ ! -e "$F/history-guard.py" ] && [ ! -e "$F/action-guard.py" ] && [ ! -e "$F/.recall-size-trigger" ] && grep -qx "history_guard=1" "$DF" || fail "F77: --no-* did not skip, or touched the default"
  "$ROOT/install.sh" --save-defaults --no-history-guard --no-action-guard --no-recall-size-trigger --no-auto-recall >/dev/null
  ! grep -Eq "history_guard|action_guard|recall_size_trigger" "$DF" || fail "F77: --save-defaults --no-* kept the lines"
  ok "F77, F78, F80: install, default off, skip message, dry-run, entries, kept messages, uninstall, saved defaults"
)

# F73-F75 amendments (PR #41 review): slots, current session by id, redaction before the split, whole-token ranking
(
  set -e
  D="$T/f75b"; mkdir -p "$D"; export HOME="$D/home"; W="$D/work"; mkdir -p "$HOME/.claude" "$W"
  export HANDOFF_STATE_DIR="$D/state" HANDOFF_LEDGER=0; unset HANDOFF_HOOKS_HOME HANDOFF_AUTO_RECALL CLAUDE_CODE_SESSION_ID
  git -C "$W" init -q; RC="$ROOT/hooks/recall.py"
  python3 - "$HOME" "$W" <<'PY'
import json, os, re, sys
home, w = sys.argv[1:3]
real = os.path.realpath(w)
pd = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", real)); os.makedirs(pd)
def rec(t, ts, text, sess, uuid):
    return {"type": t, "timestamp": ts, "cwd": real, "sessionId": sess, "uuid": uuid,
            "message": {"role": t, "content": text if t == "user" else [{"type": "text", "text": text}]}}
def put(name, rows):
    open(os.path.join(pd, name + ".jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in rows))
# probe-like keys, a PEM block, two sessions with one key, and 250 substring hits
put("probe0001", [rec("user", "2026-11-19T09:00:00Z", "Set key-alpha-one to 1. Set key-beta-two to 2. Set key-gamma-three to 3.", "probe0001", "P1"),
                  rec("assistant", "2026-11-19T09:01:00Z", "Done. key-alpha-one is 1, key-beta-two is 2, key-gamma-three is 3.", "probe0001", "P2"),
                  rec("user", "2026-11-19T09:02:00Z", "The pem-holder-x block follows. -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\nSECRETPEMBODY99\n-----END PRIVATE KEY-----\nThat is all.", "probe0001", "P3")])
put("oldsess01", [rec("user", "2026-11-20T10:00:00Z", "OLDPROMPT zebra-key is 7 now.", "oldsess01", "O1"),
                  rec("assistant", "2026-11-20T10:01:00Z", "OLDREPLY zebra-key acknowledged.", "oldsess01", "O2")])
put("newsess01", [rec("user", "2026-11-20T11:00:00Z", "NEWPROMPT zebra-key is 9 final.", "newsess01", "N1")])
rows = [rec("user", "2026-11-01T00:00:00Z", "zz-key set to 5 and kept.", "ranksess1", "R0")]
for i in range(250):
    rows.append(rec("user", "2026-11-10T00:%02d:%02dZ" % (i // 60, i % 60), "see zz-key-eu-%d here" % i, "ranksess1", "R%d" % (i + 1)))
put("ranksess1", rows)
fill = "Some long unrelated filler words go here to make the neighbour sentence much longer than the match itself."
ch = []
for i in range(14):
    if i == 10:
        v = "chain-key-q is now DECIDEDVAL."
    elif i > 10:
        v = "Rejected: chain-key-q set to LURE%d." % i
    else:
        v = "chain-key-q set to OLD%d." % i
    ch.append(rec("user", "2026-11-05T00:%02d:00Z" % i, fill + " " + v + " " + fill, "chainsess1", "C%d" % i))
put("chainsess1", ch)
PY
  # 1. probe-like prompt: `current`, `previous`, `replaced` and 3 keys with no mention take no slot
  ar() { python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"cwd":sys.argv[2],"session_id":"f75b","transcript_path":""}))' "$1" "$W" | python3 "$ROOT/hooks/auto-recall.py"; }
  o="$(ar 'what did we decide earlier about `current` `previous` `replaced` ghost-aa-1 ghost-bb-2 ghost-cc-3 key-alpha-one key-beta-two key-gamma-three')"
  for k in key-alpha-one key-beta-two key-gamma-three; do grep -q "^history of $k: " <<<"$o" || fail "F75: real key $k got no slot: $o"; done
  ! grep -q "history of current\|history of previous\|history of replaced\|ghost-" <<<"$o" || fail "F75: a lure took a slot: $o"
  # 2. MCP: two main transcripts, the session id names the older one: only its prompt is hidden
  cat > "$D/mcp.py" <<'PY'
import json, os, subprocess, sys
ms, cwd = sys.argv[1:3]
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "key_history", "arguments": {"identifier": "zebra-key"}}}]
p = subprocess.run([sys.executable, ms], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, cwd=cwd,
                   timeout=60, env=dict(os.environ, CLAUDE_CODE_SESSION_ID="oldsess01"))
x = {o["id"]: o for o in map(json.loads, p.stdout.splitlines())}[2]["result"]["content"][0]["text"]
assert "OLDPROMPT" not in x and "OLDREPLY" in x and "NEWPROMPT" in x, x
PY
  python3 "$D/mcp.py" "$ROOT/hooks/recall-mcp.py" "$W" || fail "F73: current session by id"
  # 3. a PEM block in a mention is redacted before the sentence split
  cd "$W"
  o="$(python3 "$RC" history pem-holder-x)"
  grep -q "REDACTED" <<<"$o" && ! grep -q "SECRETPEMBODY99\|MIIEvQIBADANBgkq" <<<"$o" || fail "F73: PEM block not redacted: $o"
  # 4. whole tokens rank above substrings before the cut to 200 (meaning search on, so the cut applies)
  python3 - "$ROOT/hooks" "$W" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("recall", sys.argv[1] + "/recall.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.meaning_hits = lambda a, root, ts: ([], "mode: test", None)
a = m.build_parser().parse_args(["search", "--role", "user", "--limit", "5", "--", "zz-key"])
a.subagents = True
lines = m.cmd_search(a, m.project_root(sys.argv[2]))
hits = [x for x in lines if " [word]: " in x]
assert hits and "zz-key set to 5" in hits[0], lines[:4]
PY
  # 5. cap amendment: 14 mentions, the newest 3 rejected, under the 1,500 cap: every mention and the deciding one stay
  python3 - "$ROOT/hooks" "$W" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("recall", sys.argv[1] + "/recall.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ts = m.in_scope_transcripts(m.project_root(sys.argv[2]), True)
lines, n = m.history_for(ts, "chain-key-q", "any", None, 1500)
x = "\n".join(lines)
assert n == 14 and len(x) <= 1500, (n, len(x))
assert sum(1 for l in lines if l.startswith("- ")) == 14 and "omitted" not in x and "DECIDEDVAL" in x and "LURE13" in x, x
PY
  # 6. auto-recall amendment 2: the `dry-run` order, and a 14-mention chain under 2,000 characters keeps every mention
  python3 - "$ROOT/hooks" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ar", sys.argv[1] + "/auto-recall.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
got = m.identifiers("keep dry-run and key_alpha-1 and `Zed` and `real-key` and a/b.c")
assert got == ["real-key", "key_alpha-1", "a/b.c", "dry-run", "Zed"], got
assert (m.PER_IDENT_CAP, m.TOTAL_CAP) == (2000, 4000)
PY
  o="$(ar 'what did we decide earlier about dry-run and chain-key-q')"
  [ "$(grep -c '^- .*chain-key-q' <<<"$o")" = 14 ] && grep -q DECIDEDVAL <<<"$o" && ! grep -q omitted <<<"$o" || fail "F75: 14-mention chain not whole in auto-recall: $o"
  ok "F73-F75 amendments: real keys get the slots, current session by id, PEM redaction, whole-token ranking before the cut"
)

"$ROOT/uninstall.sh" >"$T/un.out"
python3 - "$S" "$T/original.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8")); b = json.load(open(sys.argv[2], encoding="utf-8"))
assert a == b, (a, b)
PY
python3 -c "import json,sys; assert 'SessionEnd' not in json.load(open(sys.argv[1])).get('hooks', {})" "$S" || fail "SessionEnd hook left behind"
for f in precompact-handoff.py postcompact-inject.py handoff-nudge.py recall.py claims.json .handoff-manifest.json .claude-handoff-install.json lossless_handoff; do [ ! -e "$H/$f" ] || fail "$f left behind"; done
[ ! -e "$HOME/Library/LaunchAgents/com.lossless-handoff.handoff-reconcile.plist" ] || fail "reconcile LaunchAgent left behind"
[ ! -e "$HOME/Library/LaunchAgents/com.lossless-handoff.handoff-canary.plist" ] && [ ! -e "$H/handoff-canary" ] && [ ! -e "$H/handoff-canary.sh" ] || fail "canary left behind"
[ -d "$T/canary-state" ] || fail "canary state should be kept"
[ -d "$ST" ] || fail "state dir should be kept"
[ -s "$ST/health.jsonl" ] && [ -s "$ST/subsess-a-ag7-latest.md" ] || fail "uninstall deleted state data"
"$ROOT/uninstall.sh" >/dev/null
ok "uninstall removes recall.py and its allow rule, restores settings; second uninstall is a no-op"
echo "ALL PASSED"
