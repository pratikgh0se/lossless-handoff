#!/usr/bin/env bash
# Lossless Handoff uninstaller (formerly claude-handoff): removes exactly what install.sh added.
# Usage: ./uninstall.sh [--dry-run]
# Handoff files, receipts and proofs, recall indexes and project ledgers in ~/.local/state/claude-handoffs are kept (delete that folder yourself if you want them gone).
set -euo pipefail

DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
STATE_DIR="$HOME/.local/state/claude-handoffs"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
say() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else echo "$*"; fi; }

HC_DRY="$DRY" HC_SETTINGS="$SETTINGS" HC_HOOKS="$HOOKS_DIR" HC_TS="$TS" python3 - <<'PY'
import json, os, shutil, sys
dry = os.environ["HC_DRY"] == "1"
path = os.environ["HC_SETTINGS"]
hooks_dir = os.environ["HC_HOOKS"]
ts = os.environ["HC_TS"]
marker_path = os.path.join(hooks_dir, ".claude-handoff-install.json")
tag = "[dry-run] " if dry else ""
ours = {"python3 %s/%s" % (hooks_dir, s) for s in ("precompact-handoff.py", "handoff-nudge.py")}
ours |= {"python3 %s/postcompact-inject.py --part %d" % (hooks_dir, n) for n in (1, 2, 3)}
ours |= {"python3 %s/postcompact-inject.py --ledger --part %d" % (hooks_dir, n) for n in (1, 2, 3)}
ours.add("python3 %s/postcompact-inject.py --ledger" % hooks_dir)
ours.add("python3 %s/postcompact-inject.py" % hooks_dir)  # --part-less form
ours.add("python3 %s/postcompact-inject.py --check" % hooks_dir)  # hooks 1.10.0 install check (startup, resume)
sess_end = "python3 %s/recall.py session-end" % hooks_dir
allow_rule = "Edit(~/.local/state/claude-handoffs/**)"
recall_rule = "Bash(python3 ~/.claude/hooks/recall.py:*)"

if not os.path.exists(path):
    print("%ssettings: %s not found, nothing to edit" % (tag, path)); sys.exit(0)
with open(path, encoding="utf-8") as f:
    raw = f.read()
try:
    data = json.loads(raw) if raw.strip() else {}
except ValueError as e:
    sys.exit("settings.json is not valid JSON, not touching it: %s" % e)
if not isinstance(data, dict):
    sys.exit("settings.json is not a JSON object, not touching it")
marker = {}
if os.path.exists(marker_path):
    try:
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        marker = {}
before = json.dumps(data, sort_keys=True)
removed = 0

if marker.get("session_end_added"):
    ours.add(sess_end)  # only when install added it; an entry the user had before stays

hooks = data.get("hooks")
if isinstance(hooks, dict):
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                keep = [h for h in g["hooks"] if not (isinstance(h, dict) and h.get("command") in ours)]
                removed += len(g["hooks"]) - len(keep)
                g["hooks"] = keep
        hooks[event] = [g for g in groups
                        if not (isinstance(g, dict) and isinstance(g.get("hooks"), list) and not g["hooks"])]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del data["hooks"]
print("%ssettings: removed %d hook entries" % (tag, removed))

if marker.get("allow_added"):
    perms = data.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("allow"), list) and allow_rule in perms["allow"]:
        perms["allow"].remove(allow_rule)
        print("%ssettings: removed permissions.allow %s" % (tag, allow_rule))
        if not perms["allow"]:
            del perms["allow"]
        if not perms:
            del data["permissions"]
if marker.get("recall_allow_added"):
    perms = data.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("allow"), list) and recall_rule in perms["allow"]:
        perms["allow"].remove(recall_rule)
        print("%ssettings: removed permissions.allow %s" % (tag, recall_rule))
        if not perms["allow"]:
            del perms["allow"]
        if not perms:
            del data["permissions"]
ws = marker.get("window_set")
if ws is not None:
    env = data.get("env")
    if isinstance(env, dict) and env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == ws:
        del env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
        print("%ssettings: removed env.CLAUDE_CODE_AUTO_COMPACT_WINDOW (we set it)" % tag)
        if not env:
            del data["env"]

# F52.c: never lower cleanupPeriodDays (a lower value deletes chats); only tell the user
raised = marker.get("cleanup_raised_to")
if isinstance(raised, int) and not isinstance(raised, bool):
    now_v = data.get("cleanupPeriodDays")
    if isinstance(now_v, bool) or not isinstance(now_v, int):
        now_v = raised
    print("cleanupPeriodDays was left at %d; lower it yourself if you want." % now_v)

if json.dumps(data, sort_keys=True) == before:
    print("%ssettings: no change needed" % tag)
elif not dry:
    bak = "%s.bak-%s" % (path, ts)
    shutil.copy2(path, bak)
    print("settings: backup -> %s" % bak)
    tmp = path + ".tmp-handoff"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    print("settings: written %s" % path)
PY

LABEL="com.lossless-handoff.recall-index"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ -e "$PLIST" ]; then
  say "boot out and remove LaunchAgent $LABEL"
  if [ "$DRY" = 0 ]; then
    [ -n "${LOSSLESS_HANDOFF_NO_LAUNCHCTL:-}" ] || launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    rm "$PLIST"
  fi
fi

RLABEL="com.lossless-handoff.handoff-reconcile"
RPLIST="$HOME/Library/LaunchAgents/$RLABEL.plist"
if [ -e "$RPLIST" ]; then
  say "boot out and remove LaunchAgent $RLABEL"
  if [ "$DRY" = 0 ]; then
    [ -n "${LOSSLESS_HANDOFF_NO_LAUNCHCTL:-}" ] || launchctl bootout "gui/$(id -u)/$RLABEL" >/dev/null 2>&1 || true
    rm "$RPLIST"
  fi
fi

# a managed-settings file staged by install.sh --managed-hooks lives in the state dir (kept); the system copy is yours to remove
if [ -f "$HOOKS_DIR/.claude-handoff-install.json" ] && grep -q managed_settings_file "$HOOKS_DIR/.claude-handoff-install.json"; then
  case "$(uname -s)" in
    Darwin) MS_SYS="/Library/Application Support/ClaudeCode/managed-settings.json" ;;
    *) MS_SYS="/etc/claude-code/managed-settings.json" ;;
  esac
  [ -n "${HANDOFF_MANAGED_SETTINGS_PATH:-}" ] && MS_SYS="$HANDOFF_MANAGED_SETTINGS_PATH"
  [ -n "${LOSSLESS_HANDOFF_MANAGED_SETTINGS:-}" ] && MS_SYS="$LOSSLESS_HANDOFF_MANAGED_SETTINGS"
  if [ -f "$MS_SYS" ]; then
    echo "Managed settings: $MS_SYS may still hold the handoff hook entries. This script never uses sudo; edit that file yourself (or remove it if it holds nothing else)."
  fi
fi

CLABEL="com.lossless-handoff.handoff-canary"
CPLIST="$HOME/Library/LaunchAgents/$CLABEL.plist"
if [ -e "$CPLIST" ]; then
  say "boot out and remove LaunchAgent $CLABEL"
  if [ "$DRY" = 0 ]; then
    [ -n "${LOSSLESS_HANDOFF_NO_LAUNCHCTL:-}" ] || launchctl bootout "gui/$(id -u)/$CLABEL" >/dev/null 2>&1 || true
    rm "$CPLIST"
  fi
fi
if [ -e "$HOOKS_DIR/handoff-canary" ] || [ -e "$HOOKS_DIR/handoff-canary.sh" ]; then
  say "remove the canary: $HOOKS_DIR/handoff-canary/ and handoff-canary.sh"
  if [ "$DRY" = 0 ]; then
    for f in run.sh canary.py check.py; do rm -f "$HOOKS_DIR/handoff-canary/$f"; done
    rm -rf "$HOOKS_DIR/handoff-canary/__pycache__"  # canary.py imports check, so Python can leave a bytecode cache here
    rmdir "$HOOKS_DIR/handoff-canary" 2>/dev/null || true
    rm -f "$HOOKS_DIR/handoff-canary.sh"
  fi
fi

for f in precompact-handoff.py postcompact-inject.py handoff-nudge.py recall.py claims.json .handoff-manifest.json .claude-handoff-install.json; do
  if [ -e "$HOOKS_DIR/$f" ]; then
    say "remove $HOOKS_DIR/$f"
    [ "$DRY" = 1 ] || rm "$HOOKS_DIR/$f"
  fi
done

echo "kept $STATE_DIR (handoffs and health.jsonl); delete it yourself if you do not want them"
echo "kept the recall indexes and logs under $STATE_DIR/recall/ (delete that folder yourself to drop them)"
echo "kept the project decision ledgers under $STATE_DIR/projects/ (delete that folder yourself to forget them)"
for b in "$HOOKS_DIR"/.claude-handoff-backup-*; do [ -e "$b" ] && echo "kept backup: $b"; done
echo "kept canary state under $HOME/.local/state/handoff-canary (if you used --canary)"
echo "kept proof receipts, reports and claims status under $STATE_DIR (delete the folder yourself to drop them)"
echo "Done. Restart open Claude Code sessions: hooks load when a session starts."
exit 0
