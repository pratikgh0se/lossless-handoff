#!/usr/bin/env bash
# Smoke test: install, run the three hooks on synthetic data, uninstall. Uses a temporary HOME.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME/.claude"
export HANDOFF_HAIKU_FALLBACK=0
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
"$ROOT/install.sh" --dry-run >"$T/dry-nightly.out"
grep -Eq "handoff-reconcile|--reconcile --prove --days 30" "$T/dry-nightly.out" || fail "dry-run does not mention the nightly reconcile job"
[ ! -e "$HOME/Library/LaunchAgents" ] || fail "dry-run wrote a LaunchAgent"
[ ! -e "$H/claims.json" ] || fail "dry-run installed files"
ok "dry-run changes nothing"

"$ROOT/install.sh" --no-nightly >"$T/install1.out"
cp "$S" "$T/after1.json"
"$ROOT/install.sh" --no-nightly >"$T/install2.out"
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
assert m["version"] == "1.10.1" and m["installed_at"], m
assert sorted(m["files"]) == ["claims.json", "handoff-nudge.py", "postcompact-inject.py", "precompact-handoff.py", "recall.py"], m
for n, want in m["files"].items():
    assert hashlib.sha256(open(h + "/" + n, "rb").read()).hexdigest() == want, n
mk = json.load(open(h + "/.claude-handoff-install.json"))
assert mk["check_added"] is True and mk["manifest_written"] is True, mk
PY
ok "claims.json installed, manifest written (hashes match), marker records them"
grep -q "building in the background" "$T/install1.out" || fail "install did not say the first index is building"
grep -q "https://ollama.com/download" "$T/install1.out" || fail "install did not print the Ollama hint"
grep -q "ollama.com/download" "$T/install1.out" && grep -q "skipped (--no-nightly)" "$T/install1.out" || fail "install did not report skipping the nightly job"
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
PATH="$T/fakebin:$PATH" "$ROOT/install.sh" --no-nightly --yes >/dev/null
grep -qx "pull bge-m3" "$T/ollama.calls" || fail "--yes did not run ollama pull bge-m3"
PATH="$T/fakebin:$PATH" "$ROOT/install.sh" --no-nightly >"$T/nontty.out" </dev/null
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
[ ! -s "$T/pre.out" ] && [ ! -s "$T/pre.err" ] || fail "precompact printed output"
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
nudge() { printf '{"session_id":"nsess","transcript_path":"%s","tool_name":"Bash","tool_input":{}}' "$TR" | python3 "$H/handoff-nudge.py"; }
mk 310000; o1="$(nudge)"
mk 320000; o2="$(nudge)"
mk 355000; o3="$(nudge)"
[ -n "$o1" ] && [ -z "$o2" ] && [ -n "$o3" ] || fail "nudge pattern wrong (got: ${#o1} ${#o2} ${#o3} chars)"
grep -q "nsess-agent.md" <<<"$o1" || fail "nudge lacks agent path"
grep -q "claude-handoff PostToolUse hook that the user installed" <<<"$o1" || fail "nudge does not identify its source"
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
printf '%s' "$subin" | HANDOFF_LEDGER=0 python3 "$H/precompact-handoff.py" >"$T/sub.out" 2>"$T/sub.err" || fail "subagent precompact exited non-zero"
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
HANDOFF_MANAGED_SETTINGS_PATH="$T/ms-sys.json" "$ROOT/install.sh" --managed-hooks >"$T/mh.out" || fail "--managed-hooks failed"
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
  export HOME="$T/home-nn"; mkdir -p "$HOME/.claude"
  "$ROOT/install.sh" --no-nightly --no-nudge >/dev/null
  [ ! -e "$HOME/.claude/hooks/handoff-nudge.py" ] || fail "--no-nudge copied the nudge file"
  ! grep -q handoff-nudge "$HOME/.claude/settings.json" "$HOME/.claude/hooks/.handoff-manifest.json" || fail "--no-nudge left a nudge entry"
  [ -z "$(echo '{}' | python3 "$HOME/.claude/hooks/postcompact-inject.py" --check)" ] || fail "--check not silent after --no-nudge"
  export HOME="$T/home-nn2"; mkdir -p "$HOME/.claude"
  "$ROOT/install.sh" --no-nightly >/dev/null
  grep -q handoff-nudge "$HOME/.claude/settings.json" || fail "plain install has no nudge entry"
  "$ROOT/install.sh" --no-nightly --no-nudge >/dev/null
  [ ! -e "$HOME/.claude/hooks/handoff-nudge.py" ] && ! grep -q handoff-nudge "$HOME/.claude/settings.json" || fail "--no-nudge did not remove the nudge"
  [ -z "$(echo '{}' | python3 "$HOME/.claude/hooks/postcompact-inject.py" --check)" ] || fail "--check not silent after nudge removal"
)
ok "--no-nudge: no nudge file or entry, --check silent, an earlier nudge is removed"

"$ROOT/uninstall.sh" >"$T/un.out"
python3 - "$S" "$T/original.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8")); b = json.load(open(sys.argv[2], encoding="utf-8"))
assert a == b, (a, b)
PY
python3 -c "import json,sys; assert 'SessionEnd' not in json.load(open(sys.argv[1])).get('hooks', {})" "$S" || fail "SessionEnd hook left behind"
for f in precompact-handoff.py postcompact-inject.py handoff-nudge.py recall.py claims.json .handoff-manifest.json .claude-handoff-install.json; do [ ! -e "$H/$f" ] || fail "$f left behind"; done
[ ! -e "$HOME/Library/LaunchAgents/com.lossless-handoff.handoff-reconcile.plist" ] || fail "reconcile LaunchAgent left behind"
[ ! -e "$HOME/Library/LaunchAgents/com.lossless-handoff.handoff-canary.plist" ] && [ ! -e "$H/handoff-canary" ] && [ ! -e "$H/handoff-canary.sh" ] || fail "canary left behind"
[ -d "$T/canary-state" ] || fail "canary state should be kept"
[ -d "$ST" ] || fail "state dir should be kept"
[ -s "$ST/health.jsonl" ] && [ -s "$ST/subsess-a-ag7-latest.md" ] || fail "uninstall deleted state data"
"$ROOT/uninstall.sh" >/dev/null
ok "uninstall removes recall.py and its allow rule, restores settings; second uninstall is a no-op"
echo "ALL PASSED"
