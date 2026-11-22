#!/usr/bin/env bash
# Lossless Handoff installer (formerly claude-handoff): copies the hooks and merges them into ~/.claude/settings.json.
set -euo pipefail

VERSION="0.17.0"
usage() {
  cat <<EOF
Lossless Handoff $VERSION (formerly claude-handoff)
Default install (F64): the core only. It copies the hooks (nudge, PreCompact handoff, post-compaction inject,
SessionStart check, SubagentStart rules, SessionEnd index) and the recall command into ~/.claude/hooks,
merges them into ~/.claude/settings.json, installs the recall subagent (~/.claude/agents/recall.md, unless a
file of that name that is not ours is there), keeps the decisions list, and starts a first recall index.
Hooks 1.19.0 keep the chat store: handoff files live 365 days, and --keep-transcripts raises Claude Code's transcript retention.
Hooks 1.19.0 also mask secrets in every file they write (see SECURITY.md).
The default does NOT set CLAUDE_CODE_AUTO_COMPACT_WINDOW, pull a model, or install nightly jobs.
Add them with the opt-in flags below. A rerun never removes, turns off or changes a part that is already installed
(window value, meaning search, nightly jobs): a --no-* flag only skips adding it. Use ./uninstall.sh to remove parts.

Usage: ./install.sh [--dry-run] [--force] [--window N] [--nightly] [--semantic [--yes]] [--auto-recall] [--recall-rules] [--recall-mcp] [--reanchor] [--claim-guard] [--save-defaults] [--no-nudge] [--canary] [--managed-hooks] [--keep-transcripts[=DAYS]] [--version] [-h|--help]

  --dry-run     list exactly what the chosen options install (and which come from saved defaults), change nothing
  --window N    opt-in: set CLAUDE_CODE_AUTO_COMPACT_WINDOW to N tokens (for example 400000);
                left alone if already set
  --nightly     opt-in: install the nightly jobs (macOS LaunchAgents: recall index 03:30,
                reconcile and proof 04:15); on Linux the crontab lines are printed, not installed
  --semantic    opt-in: meaning search on this machine only (nothing is sent to a remote service). If ollama is
                on PATH, offer "ollama pull bge-m3" (an interactive run asks y/N; a non-interactive run skips the
                pull unless --yes is given). If ollama is missing, print how to get it and continue keyword-only
  --auto-recall opt-in (F67): install the UserPromptSubmit hook auto-recall.py. When your prompt refers to the past
                (for example "what did we decide earlier"), it searches this project's earlier chats and adds
                the top 3 exact quotes (secrets masked) to the context. It takes at most 2 seconds, never blocks
                a prompt, and writes only ~/.local/state/claude-handoffs/auto-recall.jsonl (no prompt text).
                Set HANDOFF_AUTO_RECALL=0 to switch it off at run time. Off by default
  --recall-rules
                opt-in (F68): add one strict rule line after the recall line in every session start and subagent start:
                search before you state an earlier number, path, name, command or decision. It writes the marker file
                ~/.claude/hooks/.recall-rules. Set HANDOFF_RECALL_RULES=0 to switch it off at run time. Off by default
  --recall-mcp  opt-in (F69): install hooks/recall-mcp.py, a local MCP server (stdio) with the tools search_earlier_chat and
                open_message, and register it with: claude mcp add --scope user lossless-recall -- python3 <hooks dir>/recall-mcp.py
                If the claude command is not on PATH, the command is printed and the file is still installed. Off by default
  --reanchor    opt-in (F70): install the PostToolUse hook reanchor.py. Each time the context grows by 50,000
                tokens, it repeats the project's active rules (the same text as the subagent rules) near the
                end of the context. It prints nothing when the project has no active rule. Set HANDOFF_REANCHOR=0
                to switch it off at run time, HANDOFF_REANCHOR_TOKENS (10000-500000) to change the interval.
                Off by default
  --claim-guard opt-in (F71): install the Stop hook claim-guard.py. When a reply states an earlier fact
                ("as we decided", "you said") and the turn has no recall search, it writes one line to
                ~/.local/state/claude-handoffs/claim-guard.jsonl (no text) and changes nothing else (shadow mode).
                Set HANDOFF_CLAIM_GUARD=block to make it ask for a search once per turn, 0 to switch it off.
                Off by default
  --yes         with --semantic: answer yes to the model pull without asking
  --llm-map, --no-llm-map
                no longer used. llm-map is now a separate project (https://github.com/pratikgh0se/llm-map).
                The flags are accepted and print one line; they change nothing else
  --save-defaults
                save the opt-in flags given in this run (--semantic, --nightly, --auto-recall, --recall-rules, --recall-mcp, --reanchor, --claim-guard, --window N) to
                ~/.config/lossless-handoff/install-defaults (key=value lines: semantic=1, nightly=1,
                auto_recall=1, recall_rules=1, recall_mcp=1, reanchor=1, claim_guard=1, window=N). Every later ./install.sh run reads that file and applies those
                options as if you gave them. A flag on the command line wins over the file.
                With --save-defaults, a --no-* flag removes that saved default. Delete the file, or run
                ./uninstall.sh --purge, to drop all saved defaults
  --no-semantic, --no-nightly, --no-auto-recall, --no-recall-rules, --no-recall-mcp, --no-reanchor, --no-claim-guard, --no-window
                turn one opt-in off for this run, also when a saved default turns it on. They only skip
                adding the part; they never uninstall one that is there
  --no-nudge    do not install the context nudge (handoff-nudge.py). Use it if another tool already
                nudges you before compaction. It also removes a nudge file and entry from an earlier install
  --canary      also install the opt-in nightly canary (a real headless Claude Code run past a
                compaction; spends model money, about \$0.03-0.10 per run): copies it to
                ~/.claude/hooks/handoff-canary/ and adds a LaunchAgent (03:30) on macOS or prints a
                crontab line on Linux. Off by default
  --managed-hooks
                standalone mode: write a managed-settings file with the handoff hook
                entries (so headless runs keep them) and PRINT the sudo command that
                installs it; nothing else is changed and sudo is never run
  --keep-transcripts[=DAYS]
                set cleanupPeriodDays to DAYS (default 365, 90 or more) in ~/.claude/settings.json,
                only when the value is missing or lower. It never lowers a value and never
                overrides the managed settings file. Without this flag, an interactive run asks
                y/N when Claude Code would delete transcripts after less than 90 days
  --force       install even if the same hooks are already registered from another
                path (they would then run twice); default is to stop with exit 2
  --version     print the version and exit
  -h, --help    show this help

Requires bash and python3 (macOS or Linux). Ollama is optional: without it, keyword
search still works (https://ollama.com/download). Ollama itself is never installed.
EOF
}

DRY=0; FORCE=0; WINDOW=400000; SETWINDOW=0; YES=0; NIGHTLY=0; SEMANTIC=0; NOTE_LM=0; OLDFLAG=0; SAVE=0; C_WIN=""; C_NIGHT=""; C_SEM=""; C_AR=""; C_RR=""; C_RM=""; AUTORECALL=0; RECALLRULES=0; RECALLMCP=0; C_RA=""; REANCHOR=0; C_CG=""; CLAIMGUARD=0; MANAGED=0; CANARY=0; NONUDGE=0; KEEP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --window) shift; WINDOW="${1:-}"; case "$WINDOW" in ''|*[!0-9]*) echo "--window needs a number" >&2; exit 2 ;; esac
      if [ "${#WINDOW}" -gt 12 ] || [ "$((10#$WINDOW))" -le 0 ]; then echo "--window needs a number above 0" >&2; exit 2; fi
      SETWINDOW=1; C_WIN="$WINDOW" ;;
    --no-window) SETWINDOW=0; C_WIN=off; OLDFLAG=1 ;;
    --force) FORCE=1 ;;
    --yes|-y) YES=1 ;;
    --no-nightly) C_NIGHT=0; OLDFLAG=1 ;;
    --nightly) C_NIGHT=1 ;;
    --semantic) C_SEM=1 ;;
    --no-semantic) C_SEM=0 ;;
    --auto-recall) C_AR=1 ;;
    --no-auto-recall) C_AR=0 ;;
    --recall-rules) C_RR=1 ;;
    --no-recall-rules) C_RR=0 ;;
    --recall-mcp) C_RM=1 ;;
    --no-recall-mcp) C_RM=0 ;;
    --reanchor) C_RA=1 ;;
    --no-reanchor) C_RA=0 ;;
    --claim-guard) C_CG=1 ;;
    --no-claim-guard) C_CG=0 ;;
    --llm-map|--no-llm-map) NOTE_LM=1 ;;
    --save-defaults) SAVE=1 ;;
    --no-nudge) NONUDGE=1 ;;
    --managed-hooks) MANAGED=1 ;;
    --canary) CANARY=1 ;;
    --keep-transcripts) KEEP=365 ;;
    --keep-transcripts=*)
      KEEP="${1#--keep-transcripts=}"
      case "$KEEP" in ''|*[!0-9]*) echo "--keep-transcripts needs a whole number of 90 or more" >&2; exit 2 ;; esac
      if [ "$KEEP" -lt 90 ]; then echo "--keep-transcripts needs a whole number of 90 or more" >&2; exit 2; fi ;;
    --version) echo "Lossless Handoff $VERSION"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
say() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else echo "$*"; fi; }
# saved defaults: read key=value lines (never sourced); a command-line flag wins over the file
DEFAULTS_FILE="$HOME/.config/lossless-handoff/install-defaults"
D_WIN=""; D_NIGHT=""; D_SEM=""; D_AR=""; D_RR=""; D_RM=""; D_RA=""; D_CG=""
if [ -f "$DEFAULTS_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      semantic=1) D_SEM=1 ;;
      nightly=1) D_NIGHT=1 ;;
      auto_recall=1) D_AR=1 ;;
      recall_rules=1) D_RR=1 ;;
      recall_mcp=1) D_RM=1 ;;
      reanchor=1) D_RA=1 ;;
      claim_guard=1) D_CG=1 ;;
      llm_map=1) NOTE_LM=1 ;;
      window=*) v="${line#window=}"; case "$v" in ''|*[!0-9]*) ;; *) if [ "${#v}" -le 12 ] && [ "$((10#$v))" -gt 0 ]; then D_WIN="$v"; fi ;; esac ;;
    esac
  done < "$DEFAULTS_FILE"
fi
FROM_SAVED=""
pick() { # $1 name, $2 cli value, $3 saved value -> sets P (the effective value)
  if [ -n "$2" ]; then P="$2"; elif [ -n "$3" ]; then P="$3"; FROM_SAVED="$FROM_SAVED $1"; else P=""; fi
}
pick window "$C_WIN" "$D_WIN"; case "$P" in ''|off) SETWINDOW=0 ;; *) SETWINDOW=1; WINDOW="$P" ;; esac
pick nightly "$C_NIGHT" "$D_NIGHT"; [ "$P" = 1 ] && NIGHTLY=1 || NIGHTLY=0
pick semantic "$C_SEM" "$D_SEM"; [ "$P" = 1 ] && SEMANTIC=1 || SEMANTIC=0
pick auto_recall "$C_AR" "$D_AR"; [ "$P" = 1 ] && AUTORECALL=1 || AUTORECALL=0
pick recall_rules "$C_RR" "$D_RR"; [ "$P" = 1 ] && RECALLRULES=1 || RECALLRULES=0
pick recall_mcp "$C_RM" "$D_RM"; [ "$P" = 1 ] && RECALLMCP=1 || RECALLMCP=0
pick reanchor "$C_RA" "$D_RA"; [ "$P" = 1 ] && REANCHOR=1 || REANCHOR=0
pick claim_guard "$C_CG" "$D_CG"; [ "$P" = 1 ] && CLAIMGUARD=1 || CLAIMGUARD=0
if [ "$NOTE_LM" = 1 ]; then echo "llm-map is now a separate project: https://github.com/pratikgh0se/llm-map"; fi
if [ -n "$FROM_SAVED" ]; then say "options from saved defaults ($DEFAULTS_FILE):$FROM_SAVED"; fi
if [ "$SAVE" = 1 ]; then
  if [ "$DRY" = 1 ]; then say "would save the opt-in flags of this run to $DEFAULTS_FILE"; else
    HC_DF="$DEFAULTS_FILE" HC_CWIN="$C_WIN" HC_CN="$C_NIGHT" HC_CS="$C_SEM" HC_CA="$C_AR" HC_CRR="$C_RR" HC_CRM="$C_RM" HC_CRA="$C_RA" HC_CCG="$C_CG" python3 - <<'PY'
import os
p = os.environ["HC_DF"]
cur = {}
try:
    for ln in open(p, encoding="utf-8").read().splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            cur[k.strip()] = v.strip()
except OSError:
    pass
def setk(k, v, on):
    if v == "":
        return
    if v == on:
        cur[k] = "1" if k != "window" else v
    else:
        cur.pop(k, None)
setk("semantic", os.environ["HC_CS"], "1")
setk("nightly", os.environ["HC_CN"], "1")
setk("auto_recall", os.environ["HC_CA"], "1")
setk("recall_rules", os.environ["HC_CRR"], "1")
setk("recall_mcp", os.environ["HC_CRM"], "1")
setk("reanchor", os.environ["HC_CRA"], "1")
setk("claim_guard", os.environ["HC_CCG"], "1")
cur.pop("llm_map", None)
w = os.environ["HC_CWIN"]
if w == "off":
    cur.pop("window", None)
elif w:
    cur["window"] = w
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p, "w", encoding="utf-8").write("".join("%s=%s\n" % (k, cur[k]) for k in sorted(cur)))
PY
    echo "saved defaults: $DEFAULTS_FILE"
  fi
fi
if [ "$OLDFLAG" = 1 ] && [ -z "$D_WIN$D_NIGHT" ]; then
  echo "note: --no-window and --no-nightly are no longer needed. The window and the nightly jobs are now off by default (use --window N or --nightly to add them)."
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
STATE_DIR="$HOME/.local/state/claude-handoffs"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOOKS_DIR/.claude-handoff-backup-$TS"
LABEL="com.lossless-handoff.recall-index"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RLOG_DIR="$STATE_DIR/recall"
RLABEL="com.lossless-handoff.handoff-reconcile"
RPLIST="$HOME/Library/LaunchAgents/$RLABEL.plist"
MARKER="$HOOKS_DIR/.claude-handoff-install.json"
MANIFEST="$HOOKS_DIR/.handoff-manifest.json"
FILES="precompact-handoff.py postcompact-inject.py handoff-nudge.py recall.py claims.json"
# --no-nudge: the nudge file is not part of the install, so the manifest does not list it
if [ "$NONUDGE" = 1 ]; then FILES="precompact-handoff.py postcompact-inject.py recall.py claims.json"; fi
# F67: auto-recall.py is opt-in. A copy that is already installed is kept up to date and stays in the manifest.
AR_THERE=0; [ -e "$HOOKS_DIR/auto-recall.py" ] && AR_THERE=1
if [ "$AUTORECALL" = 1 ] || [ "$AR_THERE" = 1 ]; then FILES="$FILES auto-recall.py"; fi
# F69: recall-mcp.py is opt-in, the same way. F68: the marker file .recall-rules is the whole F68 install.
RM_THERE=0; [ -e "$HOOKS_DIR/recall-mcp.py" ] && RM_THERE=1
if [ "$RECALLMCP" = 1 ] || [ "$RM_THERE" = 1 ]; then FILES="$FILES recall-mcp.py"; fi
RR_THERE=0; [ -e "$HOOKS_DIR/.recall-rules" ] && RR_THERE=1
# F70, F71: reanchor.py and claim-guard.py are opt-in in the same way.
RA_THERE=0; [ -e "$HOOKS_DIR/reanchor.py" ] && RA_THERE=1
if [ "$REANCHOR" = 1 ] || [ "$RA_THERE" = 1 ]; then FILES="$FILES reanchor.py"; fi
CG_THERE=0; [ -e "$HOOKS_DIR/claim-guard.py" ] && CG_THERE=1
if [ "$CLAIMGUARD" = 1 ] || [ "$CG_THERE" = 1 ]; then FILES="$FILES claim-guard.py"; fi
# The PreCompact hook keeps its code in the folder hooks/lossless_handoff/. It is part of the install and of the manifest.
PKG_FILES="$(cd "$SRC/hooks" 2>/dev/null && ls lossless_handoff/*.py 2>/dev/null | tr '\n' ' ')"
if [ -z "$PKG_FILES" ]; then echo "error: hooks/lossless_handoff/ is missing from this copy; get a full copy of the repo" >&2; exit 1; fi
FILES="$FILES $PKG_FILES"

say() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else echo "$*"; fi; }

# set one key in the install marker (JSON), keeping the keys already there
marker_set() { # $1 key, $2 JSON value
  HC_MARKER="$MARKER" HC_HOOKS="$HOOKS_DIR" python3 - "$1" "$2" <<'PY'
import json, os, sys
p = os.environ["HC_MARKER"]
m = {}
try:
    m = json.load(open(p, encoding="utf-8"))
    if not isinstance(m, dict):
        m = {}
except Exception:
    m = {}
m[sys.argv[1]] = json.loads(sys.argv[2])
os.makedirs(os.environ["HC_HOOKS"], exist_ok=True)
json.dump(m, open(p, "w", encoding="utf-8"))
PY
}

# --managed-hooks: standalone. Stage a managed-settings file and print the sudo command; never run sudo.
if [ "$MANAGED" = 1 ]; then
  MS_OUT="$STATE_DIR/managed-settings.json"
  case "$(uname -s)" in
    Darwin) MS_SYS="/Library/Application Support/ClaudeCode/managed-settings.json" ;;
    *) MS_SYS="/etc/claude-code/managed-settings.json" ;;
  esac
  [ -n "${HANDOFF_MANAGED_SETTINGS_PATH:-}" ] && MS_SYS="$HANDOFF_MANAGED_SETTINGS_PATH"
  [ -n "${LOSSLESS_HANDOFF_MANAGED_SETTINGS:-}" ] && MS_SYS="$LOSSLESS_HANDOFF_MANAGED_SETTINGS"
  if [ "$DRY" = 1 ]; then
    say "write $MS_OUT (the handoff hook entries, merged with $MS_SYS if it exists; that file is only read)"
  else
    mkdir -p "$STATE_DIR"
    HC_HOOKS="$HOOKS_DIR" python3 - "$MS_SYS" "$MS_OUT" <<'PY' || { echo "managed-settings file not written" >&2; exit 1; }
import json, os, sys
sys_path, out_path = sys.argv[1:3]
h = os.environ["HC_HOOKS"]
cur = {}
if os.path.isfile(sys_path):
    try:
        cur = json.load(open(sys_path, encoding="utf-8"))
        if not isinstance(cur, dict):
            raise ValueError("not an object")
    except Exception as e:
        sys.stderr.write("cannot merge into %s: %s\n" % (sys_path, e)); sys.exit(1)
pi = "python3 %s/postcompact-inject.py" % h
led = [{"type": "command", "command": "%s --ledger --part %d" % (pi, n), "timeout": 10} for n in (1, 2, 3)]
chk = {"type": "command", "command": pi + " --check", "timeout": 5}
want = {
    "PreCompact": [{"hooks": [{"type": "command", "command": "python3 %s/precompact-handoff.py" % h, "timeout": 150}]}],
    "SessionStart": [
        {"matcher": "compact", "hooks": [{"type": "command", "command": "%s --part %d" % (pi, n), "timeout": 10} for n in (1, 2, 3)] + led},
        {"matcher": "startup", "hooks": led + [chk]},
        {"matcher": "resume", "hooks": led + [chk]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 %s/recall.py session-end" % h, "timeout": 5}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": pi + " --subagent-start", "timeout": 10}]}],
}
hooks = cur.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.stderr.write("existing 'hooks' is not an object\n"); sys.exit(1)
for ev, groups in want.items():
    have = hooks.setdefault(ev, [])
    for g in groups:
        if g not in have:
            have.append(g)
tmp = out_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cur, f, indent=2)
    f.write("\n")
os.replace(tmp, out_path)
PY
    echo "wrote $MS_OUT"
    marker_set managed_settings_file "\"$MS_OUT\""
  fi
  echo "To make the handoff hooks load in every run, run this yourself (needs admin rights; this script never runs sudo):"
  echo "sudo install -m 644 \"$MS_OUT\" \"$MS_SYS\""
  echo "Only claude --bare skips managed hooks. Install the hook files first with ./install.sh if you have not."
  echo "Restart open Claude Code sessions: hooks load when a session starts."
  exit 0
fi

# F52: Claude Code deletes transcripts after cleanupPeriodDays; recall needs them. Find the effective value first.
CLEANUP_LINE_FMT='HANDOFF CHECK: Claude Code deletes chat transcripts after %s days (cleanupPeriodDays), so recall loses older messages. Set "cleanupPeriodDays": 365 in ~/.claude/settings.json, or run ./install.sh --keep-transcripts.'
MS_FOR_CLEANUP="${LOSSLESS_HANDOFF_MANAGED_SETTINGS:-${HANDOFF_MANAGED_SETTINGS_PATH:-}}"
if [ -z "$MS_FOR_CLEANUP" ]; then
  case "$(uname -s)" in
    Darwin) MS_FOR_CLEANUP="/Library/Application Support/ClaudeCode/managed-settings.json" ;;
    *) MS_FOR_CLEANUP="/etc/claude-code/managed-settings.json" ;;
  esac
fi
EFF_LINE="$(HC_SETTINGS="$SETTINGS" HC_MS="$MS_FOR_CLEANUP" python3 - <<'PY'
import json, os
def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
ms, us = load(os.environ["HC_MS"]), load(os.environ["HC_SETTINGS"])
src, raw = "default", 30
if "cleanupPeriodDays" in ms:
    src, raw = "managed", ms["cleanupPeriodDays"]
elif "cleanupPeriodDays" in us:
    src, raw = "user", us["cleanupPeriodDays"]
if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
    raw = 30
print(raw, src)
PY
)"
EFF_DAYS="${EFF_LINE% *}"; EFF_SRC="${EFF_LINE#* }"
CLEANUP_WARN=0
if [ "$EFF_SRC" = managed ]; then
  if [ -n "$KEEP" ]; then
    echo "settings: the managed settings file sets cleanupPeriodDays to $EFF_DAYS; --keep-transcripts does not override it"
    KEEP=""
  fi
  if [ "$EFF_DAYS" -lt 90 ]; then CLEANUP_WARN=1; fi
elif [ -z "$KEEP" ] && [ "$EFF_DAYS" -lt 90 ]; then
  if [ "$DRY" = 0 ] && [ -t 0 ] && [ -t 1 ]; then
    printf "Claude Code deletes chat transcripts after %s days. Keep them for 365 days so recall can find older messages? [y/N] " "$EFF_DAYS"
    read -r ans || ans=""
    case "$ans" in y|Y) KEEP=365 ;; *) CLEANUP_WARN=1 ;; esac
  else
    CLEANUP_WARN=1
  fi
fi
# shellcheck disable=SC2059
if [ "$CLEANUP_WARN" = 1 ]; then printf "warning: $CLEANUP_LINE_FMT\n" "$EFF_DAYS"; fi

# 3. settings merge
HC_AR="$AUTORECALL" HC_RA="$REANCHOR" HC_CG="$CLAIMGUARD" HC_KEEP="$KEEP" HC_NONUDGE="$NONUDGE" HC_DRY="$DRY" HC_FORCE="$FORCE" HC_WINDOW="$WINDOW" HC_SETWINDOW="$SETWINDOW" HC_SETTINGS="$SETTINGS" \
HC_HOOKS="$HOOKS_DIR" HC_TS="$TS" python3 - <<'PY'
import json, os, shutil, sys
dry = os.environ["HC_DRY"] == "1"
path = os.environ["HC_SETTINGS"]
hooks_dir = os.environ["HC_HOOKS"]
ts = os.environ["HC_TS"]
pre = "python3 %s/precompact-handoff.py" % hooks_dir
nudge = "python3 %s/handoff-nudge.py" % hooks_dir
auto_recall = "python3 %s/auto-recall.py" % hooks_dir  # F67
reanchor_cmd = "python3 %s/reanchor.py" % hooks_dir  # F70
claim_cmd = "python3 %s/claim-guard.py" % hooks_dir  # F71
post = ["python3 %s/postcompact-inject.py --part %d" % (hooks_dir, n) for n in (1, 2, 3)]
post_led = ["python3 %s/postcompact-inject.py --ledger --part %d" % (hooks_dir, n) for n in (1, 2, 3)]
start_led = "python3 %s/postcompact-inject.py --ledger" % hooks_dir  # pre-0.5.0 startup/resume form, migrated away
sess_end = "python3 %s/recall.py session-end" % hooks_dir
check_cmd = "python3 %s/postcompact-inject.py --check" % hooks_dir
subagent_cmd = "python3 %s/postcompact-inject.py --subagent-start" % hooks_dir  # F58.c
allow_rule = "Edit(~/.local/state/claude-handoffs/**)"
recall_rule = "Bash(python3 ~/.claude/hooks/recall.py:*)"
marker_path = os.path.join(hooks_dir, ".claude-handoff-install.json")
tag = "[dry-run] " if dry else ""

existed = os.path.exists(path)
if existed:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        sys.exit("settings.json is not valid JSON, not touching it: %s" % e)
    if not isinstance(data, dict):
        sys.exit("settings.json is not a JSON object, not touching it")
else:
    data = {}
before = json.dumps(data, sort_keys=True)
changes = []

hooks = data.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.exit("settings.json: hooks is not an object")

# conflict check: the same hook scripts registered from a different path would run twice
own_cmds = set([pre, nudge, auto_recall, reanchor_cmd, claim_cmd, start_led, check_cmd, subagent_cmd] + post + post_led)
names = ["precompact-handoff.py", "postcompact-inject.py", "handoff-nudge.py", "auto-recall.py"]
# F70, F71: these two names count as ours only when the flag is on or the file is installed (a default install is as on main)
if os.environ["HC_RA"] == "1" or os.path.exists(os.path.join(hooks_dir, "reanchor.py")):
    names.append("reanchor.py")
if os.environ["HC_CG"] == "1" or os.path.exists(os.path.join(hooks_dir, "claim-guard.py")):
    names.append("claim-guard.py")
conflicts = []
for ev, groups_ in hooks.items():
    if not isinstance(groups_, list):
        continue
    for g in groups_:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            continue
        for h in g["hooks"]:
            c = h.get("command") if isinstance(h, dict) else None
            if isinstance(c, str) and any(n in c for n in names) and c not in own_cmds:
                conflicts.append((ev, c))
if conflicts and os.environ["HC_FORCE"] != "1":
    sys.stderr.write("Lossless Handoff hooks are already registered from another path; installing would run every hook twice:\n")
    for ev, c in conflicts:
        sys.stderr.write("  %s: %s\n" % (ev, c))
    sys.stderr.write("Already installed from another path (not %s): remove those entries from %s, or rerun with --force.\n" % (hooks_dir, path))
    sys.exit(2)
if conflicts:
    print("%sWARNING: --force given; %d conflicting hook entr%s left in place, hooks will run twice" % (tag, len(conflicts), "y" if len(conflicts) == 1 else "ies"))

def has_cmd(groups, cmd):
    return any(isinstance(g, dict) and any(isinstance(h, dict) and h.get("command") == cmd
               for h in g.get("hooks", [])) for g in groups)

groups = hooks.setdefault("PreCompact", [])
if has_cmd(groups, pre):
    bumped = False
    for g in groups:
        for h in (g.get("hooks", []) if isinstance(g, dict) else []):
            if isinstance(h, dict) and h.get("command") == pre and isinstance(h.get("timeout"), (int, float)) and h["timeout"] < 150:
                h["timeout"] = 150
                bumped = True
    changes.append("PreCompact hook already present" + (" (timeout raised to 150)" if bumped else ""))
else:
    groups.append({"hooks": [{"type": "command", "command": pre, "timeout": 150}]})
    changes.append("add PreCompact hook (timeout 150)")

if os.environ["HC_NONUDGE"] == "1":
    # remove only the entry whose command is exactly ours; drop a group only if it ends up empty
    ptu = hooks.get("PostToolUse")
    if isinstance(ptu, list) and has_cmd(ptu, nudge):
        kept = []
        for g in ptu:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                g["hooks"] = [h for h in g["hooks"] if not (isinstance(h, dict) and h.get("command") == nudge)]
                if not g["hooks"]:
                    continue
            kept.append(g)
        if kept:
            hooks["PostToolUse"] = kept
        else:
            del hooks["PostToolUse"]
        changes.append("remove PostToolUse nudge hook (--no-nudge)")
    else:
        changes.append("PostToolUse nudge hook not installed (--no-nudge)")
elif has_cmd(hooks.setdefault("PostToolUse", []), nudge):
    changes.append("PostToolUse nudge hook already present")
else:
    groups = hooks["PostToolUse"]
    groups.append({"matcher": "*", "hooks": [{"type": "command", "command": nudge, "timeout": 5}]})
    changes.append("add PostToolUse nudge hook (matcher *, timeout 5)")

# F67: the opt-in UserPromptSubmit hook. Never removed here (a rerun keeps what is installed).
if os.environ["HC_AR"] == "1":
    ups = hooks.setdefault("UserPromptSubmit", [])
    if has_cmd(ups, auto_recall):
        changes.append("UserPromptSubmit auto-recall hook already present")
    else:
        ups.append({"hooks": [{"type": "command", "command": auto_recall, "timeout": 5}]})
        changes.append("add UserPromptSubmit auto-recall hook (timeout 5)")

# F70: the opt-in PostToolUse re-anchor hook. F71: the opt-in Stop claim guard. Never removed here.
if os.environ["HC_RA"] == "1":
    ptu = hooks.setdefault("PostToolUse", [])
    if has_cmd(ptu, reanchor_cmd):
        changes.append("PostToolUse reanchor hook already present")
    else:
        ptu.append({"matcher": "*", "hooks": [{"type": "command", "command": reanchor_cmd, "timeout": 5}]})
        changes.append("add PostToolUse reanchor hook (matcher *, timeout 5)")
if os.environ["HC_CG"] == "1":
    stp = hooks.setdefault("Stop", [])
    if has_cmd(stp, claim_cmd):
        changes.append("Stop claim-guard hook already present")
    else:
        stp.append({"hooks": [{"type": "command", "command": claim_cmd, "timeout": 5}]})
        changes.append("add Stop claim-guard hook (timeout 5)")

groups = hooks.setdefault("SessionStart", [])
grp = next((g for g in groups if isinstance(g, dict) and g.get("matcher") == "compact"
            and isinstance(g.get("hooks"), list)), None)
if grp is None:
    grp = {"matcher": "compact", "hooks": []}
    groups.append(grp)
for n, cmd in enumerate(post, 1):
    if any(isinstance(h, dict) and h.get("command") == cmd for h in grp["hooks"]):
        changes.append("SessionStart compact --part %d already present" % n)
    else:
        grp["hooks"].append({"type": "command", "command": cmd, "timeout": 10})
        changes.append("add SessionStart compact --part %d (timeout 10)" % n)

for n, cmd in enumerate(post_led, 1):
    if any(isinstance(h, dict) and h.get("command") == cmd for h in grp["hooks"]):
        changes.append("SessionStart compact --ledger --part %d already present" % n)
    else:
        grp["hooks"].append({"type": "command", "command": cmd, "timeout": 10})
        changes.append("add SessionStart compact --ledger --part %d (timeout 10)" % n)

check_added = False
for matcher in ("startup", "resume"):
    g2 = next((g for g in groups if isinstance(g, dict) and g.get("matcher") == matcher
               and isinstance(g.get("hooks"), list)), None)
    if g2 is None:
        g2 = {"matcher": matcher, "hooks": []}
        groups.append(g2)
    if any(isinstance(h, dict) and h.get("command") == start_led for h in g2["hooks"]):
        g2["hooks"] = [h for h in g2["hooks"] if not (isinstance(h, dict) and h.get("command") == start_led)]
        changes.append("SessionStart %s: replace the old single --ledger command with --ledger --part 1/2/3" % matcher)
    for n, cmd in enumerate(post_led, 1):
        if any(isinstance(h, dict) and h.get("command") == cmd for h in g2["hooks"]):
            changes.append("SessionStart %s --ledger --part %d already present" % (matcher, n))
        else:
            g2["hooks"].append({"type": "command", "command": cmd, "timeout": 10})
            changes.append("add SessionStart %s --ledger --part %d (timeout 10)" % (matcher, n))
    if any(isinstance(h, dict) and h.get("command") == check_cmd for h in g2["hooks"]):
        changes.append("SessionStart %s --check already present" % matcher)
    else:
        g2["hooks"].append({"type": "command", "command": check_cmd, "timeout": 5})
        check_added = True
        changes.append("add SessionStart %s --check (timeout 5)" % matcher)

groups = hooks.setdefault("SessionEnd", [])
session_end_added = False
if has_cmd(groups, sess_end):
    changes.append("SessionEnd recall hook already present")
else:
    groups.append({"hooks": [{"type": "command", "command": sess_end, "timeout": 5}]})
    session_end_added = True
    changes.append("add SessionEnd recall hook (timeout 5)")

groups = hooks.setdefault("SubagentStart", [])
subagent_added = False
if has_cmd(groups, subagent_cmd):
    changes.append("SubagentStart rules hook already present")
else:
    groups.append({"hooks": [{"type": "command", "command": subagent_cmd, "timeout": 10}]})
    subagent_added = True
    changes.append("add SubagentStart rules hook (timeout 10)")

# permission so the model can write its handoff without a prompt
perms = data.setdefault("permissions", {})
if not isinstance(perms, dict):
    sys.exit("settings.json: permissions is not an object")
allow = perms.setdefault("allow", [])
allow_added = False
if allow_rule in allow:
    changes.append("permissions.allow already has %s" % allow_rule)
else:
    allow.append(allow_rule)
    allow_added = True
    changes.append("add permissions.allow %s" % allow_rule)

recall_added = False
if recall_rule in allow:
    changes.append("permissions.allow already has %s" % recall_rule)
else:
    allow.append(recall_rule)
    recall_added = True
    changes.append("add permissions.allow %s" % recall_rule)

cleanup_raised = None
keep = os.environ.get("HC_KEEP", "")
if keep:  # F52.c: raise cleanupPeriodDays when it is missing or lower; never lower it
    keep_n = int(keep)
    cur_v = data.get("cleanupPeriodDays")
    if isinstance(cur_v, bool) or not isinstance(cur_v, int) or cur_v < 0 or cur_v < keep_n:
        data["cleanupPeriodDays"] = keep_n
        cleanup_raised = keep_n
        changes.append("set cleanupPeriodDays = %d (--keep-transcripts; Claude Code keeps chat transcripts longer)" % keep_n)
    else:
        changes.append("cleanupPeriodDays already %d, left alone" % cur_v)

window_set = None
if os.environ["HC_SETWINDOW"] == "1":
    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        sys.exit("settings.json: env is not an object")
    key = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    if key in env:
        changes.append("window already installed, kept: %s is %r (a value that is set is never changed; --window %s ignored)" % (key, env[key], os.environ["HC_WINDOW"]))
    else:
        env[key] = os.environ["HC_WINDOW"]
        window_set = os.environ["HC_WINDOW"]
        changes.append("set env.%s = %s" % (key, window_set))
else:
    cur_env = data.get("env") if isinstance(data.get("env"), dict) else {}
    if "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in cur_env:
        changes.append("window already installed, kept: CLAUDE_CODE_AUTO_COMPACT_WINDOW is %r" % cur_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"])
    else:
        changes.append("window not set (opt-in: --window N)")

for c in changes:
    print("%ssettings: %s" % (tag, c))
if subagent_added:
    print("%sNew hook event (SubagentStart): restart open Claude Code sessions to load it." % tag)

changed = json.dumps(data, sort_keys=True) != before
if not changed:
    print("%ssettings: no change needed" % tag)
if dry or not changed:
    sys.exit(0)

if existed:
    bak = "%s.bak-%s" % (path, ts)
    shutil.copy2(path, bak)
    print("settings: backup -> %s" % bak)
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp-handoff"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, path)
print("settings: written %s" % path)

# remember what we added so uninstall removes exactly that
marker = {}
if os.path.exists(marker_path):
    try:
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        marker = {}
if window_set is not None:
    marker["window_set"] = window_set
if allow_added:
    marker["allow_added"] = True
if recall_added:
    marker["recall_allow_added"] = True
if session_end_added:
    marker["session_end_added"] = True
if check_added:
    marker["check_added"] = True
if cleanup_raised is not None:
    marker["cleanup_raised_to"] = cleanup_raised
marker.setdefault("session_end_added", False)
marker.setdefault("check_added", False)
marker.setdefault("recall_allow_added", False)
marker.setdefault("window_set", None)
marker.setdefault("allow_added", False)
os.makedirs(hooks_dir, exist_ok=True)
with open(marker_path, "w", encoding="utf-8") as f:
    json.dump(marker, f)
PY

# hook files and state dir (after the settings merge, so a conflict exit 2 changes nothing)
for f in $FILES; do
  target="$HOOKS_DIR/$f"
  if [ -f "$target" ] && cmp -s "$SRC/hooks/$f" "$target"; then
    say "hook unchanged: $target"; continue
  fi
  if [ -e "$target" ]; then say "back up existing $target -> $BACKUP/$f"; fi
  say "install hook: $target"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$HOOKS_DIR" "$(dirname "$target")"
    if [ -e "$target" ]; then mkdir -p "$(dirname "$BACKUP/$f")"; cp -p "$target" "$BACKUP/$f"; fi
    cp "$SRC/hooks/$f" "$target"
    case "$f" in *.json|lossless_handoff/*) chmod 644 "$target" ;; *) chmod 755 "$target" ;; esac
  fi
done

# F55.a: the recall subagent (user-level). It is ours only when the file holds the marker line.
AGENT_SRC="$SRC/agents/recall.md"
AGENT_DST="$HOME/.claude/agents/recall.md"
AGENT_MARK="<!-- lossless-handoff:recall-agent -->"
if [ ! -f "$AGENT_SRC" ]; then
  say "recall subagent: skipped (agents/recall.md is missing from this copy)"
elif [ -e "$AGENT_DST" ] && ! grep -qF "$AGENT_MARK" "$AGENT_DST" 2>/dev/null; then
  # shellcheck disable=SC2088  # print the literal ~ path for the user
  echo "~/.claude/agents/recall.md exists and is not ours; left unchanged."
elif [ -f "$AGENT_DST" ] && cmp -s "$AGENT_SRC" "$AGENT_DST"; then
  say "recall subagent unchanged: $AGENT_DST"
else
  say "install recall subagent: $AGENT_DST"
  if [ "$DRY" = 0 ]; then mkdir -p "$HOME/.claude/agents"; cp "$AGENT_SRC" "$AGENT_DST"; chmod 644 "$AGENT_DST"; fi
fi

# F66.a: llm-map is a separate project. An existing copy is never changed or removed (F64).
LM_DST="$HOOKS_DIR/llm_map.py"; SK_DST="$HOME/.claude/skills/llm-map/SKILL.md"
if [ -e "$LM_DST" ] || [ -e "$SK_DST" ]; then
  say "llm-map: already installed, kept (now maintained at https://github.com/pratikgh0se/llm-map; this installer no longer updates it)"
fi

# F67: auto-recall is opt-in. An installed copy is never removed by a rerun.
if [ "$AUTORECALL" = 0 ] && [ "$AR_THERE" = 1 ]; then
  say "auto-recall: already installed, kept"
fi

# F68: strict recall rules are opt-in. The marker file is the install. An installed marker is never removed by a rerun.
if [ "$RECALLRULES" = 1 ] && [ "$RR_THERE" = 0 ]; then
  say "install recall rules marker: $HOOKS_DIR/.recall-rules"
  if [ "$DRY" = 0 ]; then mkdir -p "$HOOKS_DIR"; : > "$HOOKS_DIR/.recall-rules"; chmod 644 "$HOOKS_DIR/.recall-rules"; fi
elif [ "$RR_THERE" = 1 ]; then
  say "recall-rules: already installed, kept"
fi

# F69: recall-mcp is opt-in. Register it through the claude command; never edit ~/.claude.json directly.
MCP_CMD="claude mcp add --scope user lossless-recall -- python3 $HOOKS_DIR/recall-mcp.py"
if [ "$RECALLMCP" = 1 ]; then
  if ! command -v claude >/dev/null 2>&1; then
    say "recall-mcp: claude CLI not found; register it later with: $MCP_CMD"
  elif [ "$DRY" = 1 ]; then
    say "would run: $MCP_CMD"
  elif MCP_GET="$(claude mcp get lossless-recall 2>/dev/null)" && grep -q "lossless-recall" <<<"$MCP_GET"; then
    say "recall-mcp: already installed, kept"
  elif claude mcp add --scope user lossless-recall -- python3 "$HOOKS_DIR/recall-mcp.py" >/dev/null 2>&1; then
    say "recall-mcp: registered with: $MCP_CMD"
  else
    say "recall-mcp: claude mcp add did not succeed (it may be registered already); to register it by hand: $MCP_CMD"
  fi
elif [ "$RM_THERE" = 1 ]; then
  say "recall-mcp: already installed, kept"
fi

# F70, F71: opt-in hooks are never removed by a rerun.
if [ "$REANCHOR" = 0 ] && [ "$RA_THERE" = 1 ]; then
  say "reanchor: already installed, kept"
fi
if [ "$CLAIMGUARD" = 0 ] && [ "$CG_THERE" = 1 ]; then
  say "claim-guard: already installed, kept"
fi

# --no-nudge: remove a nudge file left by an earlier install
if [ "$NONUDGE" = 1 ]; then
  if [ -e "$HOOKS_DIR/handoff-nudge.py" ]; then
    say "remove hook: $HOOKS_DIR/handoff-nudge.py (--no-nudge)"
    [ "$DRY" = 1 ] || rm -f "$HOOKS_DIR/handoff-nudge.py"
  else
    say "nudge hook: skipped (--no-nudge)"
  fi
fi

# manifest: a hash per installed hook file; the session-start `postcompact-inject.py --check` compares it
if [ "$DRY" = 1 ]; then say "write manifest: $MANIFEST"; else
  HC_HOOKS="$HOOKS_DIR" HC_FILES="$FILES" python3 - <<'PY' || echo "warning: could not write the handoff manifest" >&2
import datetime, hashlib, json, os, re
h = os.environ["HC_HOOKS"]
files = {}
for n in sorted(os.environ["HC_FILES"].split()):
    p = os.path.join(h, n)
    if os.path.isfile(p):
        files[n] = hashlib.sha256(open(p, "rb").read()).hexdigest()
mp = os.path.join(h, ".handoff-manifest.json")
try:
    if json.load(open(mp)).get("files") == files:
        raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    pass
ver = ""
try:
    ver = re.search(r'(?m)^HOOK_VERSION = "([^"]*)"', open(os.path.join(h, "precompact-handoff.py")).read()).group(1)
except Exception:
    pass
tmp = mp + ".tmp"
json.dump({"version": ver, "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": files}, open(tmp, "w"), indent=1)
os.replace(tmp, mp)
PY
  marker_set manifest_written true
  say "manifest: $MANIFEST"
fi

# 2. state dir
if [ -d "$STATE_DIR" ]; then say "state dir exists: $STATE_DIR"; else
  say "create state dir: $STATE_DIR"; [ "$DRY" = 1 ] || mkdir -p "$STATE_DIR"; fi

# 3. semantic search model (opt-in: --semantic)
if [ "$SEMANTIC" = 1 ]; then
echo
if command -v ollama >/dev/null 2>&1; then
  PULL=0
  if [ "$YES" = 1 ]; then PULL=1
  elif [ "$DRY" = 1 ]; then say "would ask whether to run: ollama pull bge-m3"
  elif [ -t 0 ] && [ -t 1 ]; then
    printf "Pull the bge-m3 model for search by meaning (about 1.2 GB)? [y/N] "
    read -r ans || ans=""
    case "$ans" in y|Y|yes|YES) PULL=1 ;; esac
  else
    echo "Meaning search: skipped the model pull (not interactive). Run: ollama pull bge-m3   (or rerun install with --yes)"
  fi
  if [ "$PULL" = 1 ]; then
    if [ "$DRY" = 1 ]; then say "run: ollama pull bge-m3"
    else ollama pull bge-m3 || echo "warning: ollama pull bge-m3 failed; keyword search still works, retry later" >&2; fi
  fi
else
  echo "Meaning search is off: ollama is not on PATH. To enable it, install Ollama (https://ollama.com/download), then run ./install.sh --semantic. Recall works in keyword-only mode until then."
fi
else
  say "meaning search: not selected (opt-in: --semantic)"
fi

# 4. nightly index job
INDEX_CMD="python3 $HOOKS_DIR/recall.py index --all --max-seconds 3000"
if [ "$NIGHTLY" != 1 ]; then
  if [ -e "$PLIST" ] || [ -e "$RPLIST" ]; then
    say "nightly jobs: already installed, kept (--nightly would rewrite them)"
  else
    say "nightly jobs: not selected (opt-in: --nightly)"
  fi
elif [ "$(uname)" = "Darwin" ]; then
  say "install LaunchAgent $LABEL (daily 03:30): $PLIST"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$HOME/Library/LaunchAgents" "$RLOG_DIR"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$HOOKS_DIR/recall.py</string>
    <string>index</string>
    <string>--all</string>
    <string>--max-seconds</string>
    <string>3000</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$RLOG_DIR/index-all.log</string>
  <key>StandardErrorPath</key>
  <string>$RLOG_DIR/index-all.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLISTEOF
    if [ -z "${LOSSLESS_HANDOFF_NO_LAUNCHCTL:-}" ]; then
      launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST" \
        || echo "warning: could not load the LaunchAgent; run: launchctl bootstrap gui/$(id -u) $PLIST" >&2
    fi
  fi
else
  echo "Nightly index job: add this line with 'crontab -e' (not edited for you):"
  echo "  30 3 * * * $INDEX_CMD >> $RLOG_DIR/index-all.log 2>&1"
fi

# 4b. nightly reconcile and proof job
RECON_CMD="python3 $HOOKS_DIR/precompact-handoff.py --reconcile --prove --days 30"
if [ "$NIGHTLY" != 1 ]; then
  :
elif [ "$(uname)" = "Darwin" ]; then
  say "install LaunchAgent $RLABEL (daily 04:15): $RPLIST"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR"
    cat > "$RPLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$RLABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$HOOKS_DIR/precompact-handoff.py</string>
    <string>--reconcile</string>
    <string>--prove</string>
    <string>--days</string>
    <string>30</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>4</integer>
    <key>Minute</key>
    <integer>15</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$STATE_DIR/reconcile.log</string>
  <key>StandardErrorPath</key>
  <string>$STATE_DIR/reconcile.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLISTEOF
    marker_set reconcile_agent_added true
    if [ -z "${LOSSLESS_HANDOFF_NO_LAUNCHCTL:-}" ]; then
      launchctl bootout "gui/$(id -u)/$RLABEL" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$(id -u)" "$RPLIST" \
        || echo "warning: could not load the LaunchAgent; run: launchctl bootstrap gui/$(id -u) $RPLIST" >&2
    fi
  fi
else
  echo "Nightly reconcile and proof job: add this line with 'crontab -e' (not edited for you):"
  echo "  15 4 * * * $RECON_CMD >> $STATE_DIR/reconcile.log 2>&1"
fi

# 4c. opt-in canary (--canary only)
if [ "$CANARY" = 1 ]; then
  CLABEL="com.lossless-handoff.handoff-canary"
  CPLIST="$HOME/Library/LaunchAgents/$CLABEL.plist"
  CDIR="$HOME/.local/state/handoff-canary"
  say "install the canary: $HOOKS_DIR/handoff-canary/ and $HOOKS_DIR/handoff-canary.sh (spends model money: about \$0.03-0.10 per run)"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$HOOKS_DIR/handoff-canary"
    cp "$SRC/tools/handoff-canary/run.sh" "$SRC/tools/handoff-canary/canary.py" "$SRC/tools/handoff-canary/check.py" "$HOOKS_DIR/handoff-canary/"
    chmod 755 "$HOOKS_DIR/handoff-canary/run.sh" "$HOOKS_DIR/handoff-canary/canary.py" "$HOOKS_DIR/handoff-canary/check.py"
    cp "$SRC/hooks/handoff-canary.sh" "$HOOKS_DIR/handoff-canary.sh"; chmod 755 "$HOOKS_DIR/handoff-canary.sh"
    marker_set canary_added true
  fi
  if [ "$(uname)" = "Darwin" ]; then
    say "install LaunchAgent $CLABEL (daily 03:30): $CPLIST"
    if [ "$DRY" = 0 ]; then
      mkdir -p "$HOME/Library/LaunchAgents" "$CDIR/logs"
      cat > "$CPLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$CLABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>$HOOKS_DIR/handoff-canary.sh --catch-up</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$CDIR/logs/launchd.out</string>
  <key>StandardErrorPath</key>
  <string>$CDIR/logs/launchd.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLISTEOF
      if [ -z "${LOSSLESS_HANDOFF_NO_LAUNCHCTL:-}" ]; then
        launchctl bootout "gui/$(id -u)/$CLABEL" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$(id -u)" "$CPLIST" \
          || echo "warning: could not load the LaunchAgent; run: launchctl bootstrap gui/$(id -u) $CPLIST" >&2
      fi
    fi
  else
    echo "Nightly canary: add this line with 'crontab -e' (not edited for you):"
    echo "  30 3 * * * $HOOKS_DIR/handoff-canary.sh --catch-up >> $CDIR/cron.log 2>&1"
  fi
fi

# 5. first index, detached
if [ "$DRY" = 1 ]; then
  say "start in the background: $INDEX_CMD (log: $RLOG_DIR/index-all.log)"
else
  mkdir -p "$RLOG_DIR"
  nohup python3 "$HOOKS_DIR/recall.py" index --all >> "$RLOG_DIR/index-all.log" 2>&1 < /dev/null &
fi

echo
if [ "$DRY" = 1 ]; then echo "Dry run only: nothing was changed."; else
  echo "Installed."
  echo "Restart open Claude Code sessions: hooks load when a session starts."
  echo
  echo "Recall: the first index of your earlier chats is building in the background (log: $RLOG_DIR/index-all.log)."
  echo "Chats are indexed automatically when they end. Search from a project folder:  python3 ~/.claude/hooks/recall.py search \"words\""
  echo
  echo "Verify after the first compaction: tail -n 3 $STATE_DIR/health.jsonl"
  echo "Opt-in extras (not installed by default): --window N, --nightly, --semantic (add --save-defaults to keep them). Rerun with the flags you want."
  echo "Headless runs (claude -p, SDK) load these hooks too, except with --bare. To keep them under --setting-sources without user, run ./install.sh --managed-hooks."
fi
