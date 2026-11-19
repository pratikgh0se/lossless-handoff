# Install and uninstall

How to install, what the install changes, how to verify it and how to remove it. This page is part of the [docs index](README.md).

## Install

```bash
git clone https://github.com/pratikgh0se/lossless-handoff.git
cd lossless-handoff
./install.sh --dry-run   # show the plan, change nothing
./install.sh
```

Then restart open Claude Code sessions. Hooks load when a session starts. Prerequisites:

- `python3` and `bash` on macOS or Linux (Windows is not supported).
- A Claude Code version that supports `PreCompact`, `SessionStart` and `PostToolUse` hooks with `additionalContext`.
- Optional: the `claude` binary on `PATH` (the hooks use it for the fresh summary).
- Optional: [Ollama](https://ollama.com/download) (for meaning search).

The default install is small. It installs the core only:

- It copies the hooks and `recall.py` to `~/.claude/hooks/`.
- It installs the recall subagent (`~/.claude/agents/recall.md`). It keeps a file of that name that is not ours.
- It merges the entries into `~/.claude/settings.json`, next to your entries, with a timestamped backup.
- It adds the end-of-chat index (a `SessionEnd` hook).
- It adds a `SubagentStart` hook that gives project rules to a new subagent.
- It adds an install check that runs at session start and prints one line only when something is wrong (`postcompact-inject.py --check`). The check compares the hooks with an install manifest (`~/.claude/hooks/.handoff-manifest.json`).
- It keeps the project decisions list.
- It starts a first index of all your projects in the background.

The default install does not do these things. Each one is an opt-in flag:

| Flag | What it adds |
|---|---|
| `--window N` | Sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to N tokens (for example 400000), only when no value is set. |
| `--nightly` | Two nightly jobs: the index (03:30) and the reconcile-and-prove run (04:15). They are macOS LaunchAgents. On Linux the installer prints the cron lines. |
| `--semantic` | Meaning search on your machine only. If `ollama` is on your PATH, the installer offers `ollama pull bge-m3` (`--yes` pulls without a question). If `ollama` is missing, it says how to get it and recall stays keyword-only. |
| `--llm-map` | `llm_map.py` and the llm-map skill (`~/.claude/skills/llm-map/SKILL.md`). |

Run `./install.sh --window 400000 --nightly --semantic --llm-map` to get everything. Use `--dry-run` first: it lists exactly what the chosen options install.

**Saved defaults.** Add `--save-defaults` to save the opt-in flags of that run to `~/.config/lossless-handoff/install-defaults`. The file holds simple lines: `semantic=1`, `nightly=1`, `llm_map=1`, `window=N`. Every later `./install.sh` run reads the file and applies these options as if you gave them. A bad line in the file (for example `window=0`) is ignored and the install goes on with exit code 0. Only the flag `--window 0` is an error (exit code 2). A flag on the command line wins over the file. `--no-semantic`, `--no-nightly`, `--no-llm-map` and `--no-window` turn one saved default off for that run. With `--save-defaults`, a `--no-*` flag removes that saved default. `--dry-run` shows which options come from the file. `./uninstall.sh` keeps the file. Run `./uninstall.sh --purge`, or delete the file, to drop it.

**Re-install is safe.** A rerun never removes, turns off or changes a part that is already installed: the window value, the nightly jobs, meaning search and the llm-map install stay. A `--no-*` flag only skips adding a part. It does not uninstall one. Use `./uninstall.sh` to remove parts.

**Upgrade from 0.12.x.** Earlier versions installed the window, the nightly jobs and llm-map by default. A rerun of the new installer keeps all of them, and prints "already installed, kept" for the window, the nightly jobs and llm-map. `--window N` never changes a value that is already set: the installer prints "window already installed, kept: CLAUDE_CODE_AUTO_COMPACT_WINDOW is <old value>". To change it, edit `~/.claude/settings.json`. It does not update an old `llm_map.py` or skill without `--llm-map`; add the flag to update them. A new machine gets the core only. The old flags `--no-window` and `--no-nightly` are still accepted. The installer prints one note that the default is now off.

Other options: `--no-nudge` (skip the context nudge, and remove one from an earlier install), `--managed-hooks` (see [Headless runs](#headless-runs)), `--canary` (opt-in nightly end-to-end test, see below), `--keep-transcripts[=DAYS]` (set `cleanupPeriodDays` to DAYS, default 365, with 90 as the lowest value; only when the value is missing or lower; see [What gets written where](configuration.md#what-gets-written-where)), `--force`, `--version`, `--help`. Use `--no-nudge` if another tool already nudges you before compaction. The install is idempotent. [What install changes](#what-install-changes) gives the full details.

## What install changes

- It copies `precompact-handoff.py`, `postcompact-inject.py`, `handoff-nudge.py`, `recall.py` and `claims.json` to `~/.claude/hooks/`. It first saves a differing existing file to `~/.claude/hooks/.claude-handoff-backup-<timestamp>/`.
- It copies `agents/recall.md` from this repository to `~/.claude/agents/recall.md` (the recall subagent). If a file of that name does not hold the marker line `<!-- lossless-handoff:recall-agent -->`, the installer keeps the file and says so. Uninstall removes the file only when it holds the marker. The repository file is the single source: a benchmark or a project can copy the same file into its own `.claude/agents/` folder.
- It writes `~/.claude/hooks/.handoff-manifest.json`: version, time and a SHA-256 for each installed file. `--check` compares them.
- With `--llm-map`: it copies `hooks/llm_map.py` to `~/.claude/hooks/` and `skills/llm-map/SKILL.md` to `~/.claude/skills/llm-map/SKILL.md`. If a file of that name does not hold the marker line `<!-- lossless-handoff:llm-map-skill -->`, the installer keeps the file and says so.
- It records everything that it added in `~/.claude/hooks/.claude-handoff-install.json`, so uninstall removes exactly that.
- It merges into `~/.claude/settings.json` (backup `settings.json.bak-<timestamp>`):
  - a PreCompact hook (timeout 150) and a PostToolUse hook (matcher `*`, timeout 5);
  - a SessionStart `compact` group with the three inject commands and the three `--ledger --part 1/2/3` commands (timeout 10), and `startup` and `resume` groups with the three `--ledger` commands plus `postcompact-inject.py --check` (timeout 5);
  - a `SessionEnd` hook that runs `recall.py session-end` (timeout 5);
  - a `SubagentStart` hook that runs `postcompact-inject.py --subagent-start` (timeout 10);
  - `permissions.allow` entries `Edit(~/.local/state/claude-handoffs/**)` (so the model can write its handoff without a prompt) and `Bash(python3 ~/.claude/hooks/recall.py:*)` (so it can run recall without a prompt);
  - only with `--window N`: `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` = `"N"`, unless it is already set (an existing value is never changed).
- It creates `~/.local/state/claude-handoffs/`.
- With `--semantic`, and only if `ollama` is on your PATH: it offers `ollama pull bge-m3`.
- With `--save-defaults`: it writes `~/.config/lossless-handoff/install-defaults`.
- On macOS, with `--nightly`: it writes the LaunchAgent `~/Library/LaunchAgents/com.lossless-handoff.recall-index.plist` (daily 03:30, runs `recall.py index --all --max-seconds 3000`, log `~/.local/state/claude-handoffs/recall/index-all.log`) and loads it. On Linux it prints the equivalent crontab line and does not edit your crontab.
- On macOS, with `--nightly`: it writes and loads the LaunchAgent `~/Library/LaunchAgents/com.lossless-handoff.handoff-reconcile.plist` (daily 04:15, runs `precompact-handoff.py --reconcile --prove --days 30`, log `~/.local/state/claude-handoffs/reconcile.log`). On Linux it prints that crontab line too (`15 4 * * * python3 ~/.claude/hooks/precompact-handoff.py --reconcile --prove --days 30 >> ~/.local/state/claude-handoffs/reconcile.log 2>&1`, with your real path).
- With `--managed-hooks` (standalone): it stages a managed-settings file in the state folder and prints the `sudo install -m 644 ...` command. It changes nothing else and never runs sudo.
- It starts `recall.py index --all` in the background.
- With `--canary` (opt-in, off by default because it makes real model calls, about $0.03-0.10 per run): it copies `tools/handoff-canary/` to `~/.claude/hooks/handoff-canary/` and `handoff-canary.sh` to `~/.claude/hooks/`. On macOS it writes the LaunchAgent `com.lossless-handoff.handoff-canary` (daily 03:30, `--catch-up`). On Linux it prints the crontab line. The canary drives two headless Claude Code sessions past a compaction and checks that the handoff arrives. See [`tools/handoff-canary/README.md`](../tools/handoff-canary/README.md).

Your other hooks and settings stay as they are: the install appends new entries next to existing ones. If the same hook scripts are already registered from another path, the install prints those commands and exits 2, because every hook would run twice. Remove them, or pass `--force`. The install fails with a clear message if `python3` is missing or `settings.json` is not valid JSON.

## Headless runs

`claude -p` and the Agent SDK load your user hooks by default. They skip them only with `--bare`, `disableAllHooks`, `--setting-sources` without `user`, or SDK `settingSources: []`.

To keep the handoff hooks in every run except `--bare`, use managed settings:

```
./install.sh --managed-hooks
```

This is a standalone mode that changes nothing else. It writes `~/.local/state/claude-handoffs/managed-settings.json`. If a system file exists, the script reads it and merges it in, and never writes to it. The script prints the command to install the file. Run the command yourself, because it needs admin rights. The script never runs sudo:

```
# macOS
sudo install -m 644 ~/.local/state/claude-handoffs/managed-settings.json "/Library/Application Support/ClaudeCode/managed-settings.json"
# Linux: /etc/claude-code/managed-settings.json
```

With managed hooks installed, only `--bare` skips them. `--check` accepts hooks from user or managed settings. Reconcile reports any compaction that still got no handoff, headless or not.

## Verify

```
claude -p --model haiku "say hi" --output-format json   # note the session_id
claude -p --model haiku --resume <session_id> "/compact"
tail -n 3 ~/.local/state/claude-handoffs/health.jsonl
```

A new line with `"trigger": "manual"` means that the PreCompact hook fired. In a normal session, run `/compact` and look at the same file. `status` is `ok` (the hook used the model handoff), `reduced` (the hook used the fresh summary instead) or `degraded` (facts only). You can also run `bash tests/smoke.sh`. It uses a temporary HOME and does not touch your real settings.

To test a real compaction without a change to your own install:

1. Install into a temporary home with `HOME=<tmp> LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 ./install.sh`, so no LaunchAgent is written or loaded.
2. Keep your real HOME for the run, because `claude` reads its login from there.
3. Run `claude -p --setting-sources local --settings <tmp>/.claude/settings.json ...` from a fresh folder, with `HANDOFF_STATE_DIR=<tmp>/.local/state/claude-handoffs`. This switches your own hooks off and keeps all handoff files in the temporary folder.
4. To force a compaction quickly, set `CLAUDE_CODE_AUTO_COMPACT_WINDOW=30000` both in the process environment and in the `env` of the settings file that you pass with `--settings`. If you run with your own user settings and they set this variable (the installer sets 400000), that value wins over the process environment. In that case, pass `--settings '{"env":{"CLAUDE_CODE_AUTO_COMPACT_WINDOW":"30000"}}'`. Keep each file that the agents read under about 20k tokens (for example 900 lines). The Read tool refuses larger files, and then the context does not grow.
5. Check with `HANDOFF_STATE_DIR=... python3 <tmp>/.claude/hooks/precompact-handoff.py --prove --days 1`.

Use a folder name that contains `handoff-canary`, so that the nightly check on your real install does not count these compactions as missed.

## Uninstall

```
./uninstall.sh
```

The script removes exactly the hook entries that install added (it matches them by command path; other hooks in those groups stay). It also removes these items, after it backs up the settings: the allow rules and the window value (if install added them), the macOS LaunchAgents (index and reconcile), the `--check` entries, and the hook files (including `recall.py`, `llm_map.py`, `claims.json` and the manifest). It also removes the recall subagent (`~/.claude/agents/recall.md`) and the llm-map skill, each only when the file holds our marker line. Add `--purge` to also delete the saved install defaults (`~/.config/lossless-handoff/install-defaults`); without it they stay. It never touches a system managed-settings file (that needs sudo). Uninstall tells you if one is present. Handoff files, receipts, proofs, indexes and the project decisions lists under `~/.local/state/claude-handoffs/` stay. Delete that folder yourself if you want them gone. The backups stay.
