#!/usr/bin/env bash
# Lossless Handoff installer (formerly claude-handoff): copies the hooks and merges them into ~/.claude/settings.json.
set -euo pipefail

VERSION="0.8.0"
usage() {
  cat <<EOF
Lossless Handoff $VERSION (formerly claude-handoff)
Copies the hooks (three handoff hooks plus the recall command) into ~/.claude/hooks and merges them into ~/.claude/settings.json.
Also sets up automatic recall indexing: an end-of-chat hook, an optional bge-m3 model pull, a nightly job (macOS) and a first index.
Hooks 1.10.0 add: an install check at session start, a nightly reconcile and proof job (macOS), and an install manifest.

Usage: ./install.sh [--dry-run] [--force] [--window N] [--no-window] [--yes] [--no-nightly] [--no-nudge] [--canary] [--managed-hooks] [--version] [-h|--help]

  --dry-run     show what would change, change nothing
  --window N    set CLAUDE_CODE_AUTO_COMPACT_WINDOW to N tokens (default 400000;
                left alone if already set)
  --no-window   do not touch CLAUDE_CODE_AUTO_COMPACT_WINDOW
  --yes         answer yes to the bge-m3 model pull (needs ollama on PATH). Without
                it, an interactive run asks y/N and a non-interactive run skips it
  --no-nightly  do not install the nightly jobs (macOS LaunchAgents: recall index 03:30,
                reconcile and proof 04:15); on Linux the crontab lines are not printed
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
  --force       install even if the same hooks are already registered from another
                path (they would then run twice); default is to stop with exit 2
  --version     print the version and exit
  -h, --help    show this help

Requires bash and python3 (macOS or Linux). Ollama is optional: without it, keyword
search still works (https://ollama.com/download). Ollama itself is never installed.
EOF
}

DRY=0; FORCE=0; WINDOW=400000; SETWINDOW=1; YES=0; NONIGHTLY=0; MANAGED=0; CANARY=0; NONUDGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --window) shift; WINDOW="${1:-}"; case "$WINDOW" in ''|*[!0-9]*) echo "--window needs a number" >&2; exit 2 ;; esac ;;
    --no-window) SETWINDOW=0 ;;
    --force) FORCE=1 ;;
    --yes|-y) YES=1 ;;
    --no-nightly) NONIGHTLY=1 ;;
    --no-nudge) NONUDGE=1 ;;
    --managed-hooks) MANAGED=1 ;;
    --canary) CANARY=1 ;;
    --version) echo "Lossless Handoff $VERSION"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

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

# 3. settings merge
HC_NONUDGE="$NONUDGE" HC_DRY="$DRY" HC_FORCE="$FORCE" HC_WINDOW="$WINDOW" HC_SETWINDOW="$SETWINDOW" HC_SETTINGS="$SETTINGS" \
HC_HOOKS="$HOOKS_DIR" HC_TS="$TS" python3 - <<'PY'
import json, os, shutil, sys
dry = os.environ["HC_DRY"] == "1"
path = os.environ["HC_SETTINGS"]
hooks_dir = os.environ["HC_HOOKS"]
ts = os.environ["HC_TS"]
pre = "python3 %s/precompact-handoff.py" % hooks_dir
nudge = "python3 %s/handoff-nudge.py" % hooks_dir
post = ["python3 %s/postcompact-inject.py --part %d" % (hooks_dir, n) for n in (1, 2, 3)]
post_led = ["python3 %s/postcompact-inject.py --ledger --part %d" % (hooks_dir, n) for n in (1, 2, 3)]
start_led = "python3 %s/postcompact-inject.py --ledger" % hooks_dir  # pre-0.5.0 startup/resume form, migrated away
sess_end = "python3 %s/recall.py session-end" % hooks_dir
check_cmd = "python3 %s/postcompact-inject.py --check" % hooks_dir
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
own_cmds = set([pre, nudge, start_led, check_cmd] + post + post_led)
names = ("precompact-handoff.py", "postcompact-inject.py", "handoff-nudge.py")
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

window_set = None
if os.environ["HC_SETWINDOW"] == "1":
    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        sys.exit("settings.json: env is not an object")
    key = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    if key in env:
        changes.append("%s already set to %r, left alone" % (key, env[key]))
    else:
        env[key] = os.environ["HC_WINDOW"]
        window_set = os.environ["HC_WINDOW"]
        changes.append("set env.%s = %s" % (key, window_set))
else:
    changes.append("window left alone (--no-window)")

for c in changes:
    print("%ssettings: %s" % (tag, c))

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
    mkdir -p "$HOOKS_DIR"
    if [ -e "$target" ]; then mkdir -p "$BACKUP"; cp -p "$target" "$BACKUP/$f"; fi
    cp "$SRC/hooks/$f" "$target"
    case "$f" in *.json) chmod 644 "$target" ;; *) chmod 755 "$target" ;; esac
  fi
done

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

# 3. semantic search model (optional)
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
  echo "Meaning search is optional. To enable it, install Ollama (https://ollama.com/download), then run: ollama pull bge-m3"
  echo "Keyword search works without it."
fi

# 4. nightly index job
INDEX_CMD="python3 $HOOKS_DIR/recall.py index --all --max-seconds 3000"
if [ "$NONIGHTLY" = 1 ]; then
  say "nightly index job: skipped (--no-nightly)"
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
if [ "$NONIGHTLY" = 1 ]; then
  say "nightly reconcile job: skipped (--no-nightly)"
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
  echo "Headless runs (claude -p, SDK) load these hooks too, except with --bare. To keep them under --setting-sources without user, run ./install.sh --managed-hooks."
fi
