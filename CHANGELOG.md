# Changelog

Lossless Handoff, formerly claude-handoff.

## [0.12.2] - 2026-11-19

Hardening: the nudge window and a format drift alarm (hooks 1.14.2). New F62 and F63 in `docs/spec.md`.

- Nudge window (F62): if `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is not a positive integer, the nudge learns the window from `preTokens` of past `compact_boundary` records (median + 33,000, rounded to 1,000; current transcript, else the newest 5 of the project). With no history it uses 200,000. Before, it assumed 400,000, so with a 200k window it could never fire. A valid value works as before. Health rows have `window` and `window_source`.
- Format drift alarm (F63): reconcile compares recorded post-compaction injects (`receipts.jsonl`) with `compact_boundary` records per session. A session with injects and no boundary sets `format_drift: true` in `reconcile.json`. `--prove` and the claims mark the affected checks `fail` (`format_drift`). `--check` prints one `format drift` line.
- Review fixes: the nudge skips manual compactions and ignores a learned window below 100,000 or above 2,100,000 (F62, ACs 298-300). It stops the scan after 1.5 s. The drift check counts boundaries from the window start (F63, AC 301). SECURITY.md lists the skill file, the LaunchAgents and the optional model download.
- The nudge tag is now `[lossless-handoff]` (it was `[claude-handoff]`).
- Text fixes: the docstring of `precompact-handoff.py`, a stale comment about subagent compaction, and `SECURITY.md` (file list, permissions, network use).

## [0.12.1] - 2026-11-18

llm_map: faster, a fixed label set, progress (hooks 1.14.1). New F61 in `docs/spec.md`, ACs 281-286.

- Defaults are now `--batch 100` and `--jobs 8`. 3,000 items no longer pass the 2-minute Bash limit.
- New `--labels "a|b|c"`: the model must answer with one label. Replies map to a label by exact match or a word-boundary prefix. Invalid replies are retried once, then fail with `error: "label: ..."`.
- Each finished batch prints `llm_map: <done> of <n> items done` to stderr.
- The skill text tells the model to use `--labels`, a 600000 ms timeout, a foreground wait and no `pgrep`.

## [0.12.0] - 2026-11-18

Label many items with a small model (hooks 1.14.0). New F60 in `docs/spec.md`, ACs 274-280.

- New `hooks/llm_map.py`: it labels each item of a large list with a small model in parallel batches. It writes one JSON line per item, retries missing items once and has a cost cap (`--max-usd`).
- Each model call runs without tools, hooks, MCP servers or a saved session, so no Lossless Handoff hook runs for it.
- New skill `skills/llm-map/SKILL.md`. Install writes it to `~/.claude/skills/llm-map/` unless a file that is not ours is there. Uninstall removes only the marked file.
- `install.sh` also copies `llm_map.py`. It changes no `settings.json` entry.

## [0.11.1] - 2026-11-18

Find what a subagent read (hooks 1.13.1). New F59 in `docs/spec.md`, ACs 268-273.

- In keyword search, a plain pattern with a space, `-` or `_` matches any run of these characters, so `ledger key` finds `ledger-key`. `--fixed` stays exact. Other patterns keep their meaning.
- A hit line from a tool call ends with ` -> result <id>` when the same chat holds the tool result. `open <id>` prints that result.
- `agents/recall.md` and the recall line tell the model to open that id.

## [0.11.0] - 2026-11-18

Subagent work in search, and rules at subagent start (hooks 1.13.0). New F57 and F58 in `docs/spec.md`, ACs 255-267.

- `search`, `open` and the meaning index include subagent transcripts by default. A subagent hit has the id `<sid8>/<agent8>:<line>` and the tag `[agent <agent8>]`. `--no-subagents` restores the old scope. `--subagents` is still accepted and does nothing. `user` still reads only main chats. Run `recall.py index` once to add subagent text to the meaning index.
- New `SubagentStart` hook (`postcompact-inject.py --subagent-start`). It gives a new subagent the active `rule` entries of the project (at most 4,000 characters) and the recall line. It gives no decisions, tasks, handoff text or notes. `HANDOFF_SUBAGENT_RULES=0` turns it off.
- `install.sh` registers the hook (also in the `--managed-hooks` file); `uninstall.sh` removes it. `SubagentStart` is a new hook event: restart open Claude Code sessions.
- Each run adds a health row with `event: "subagent_start"`. `--prove` and `--reconcile` skip these rows. The agent file `agents/recall.md` explains the `[agent ...]` tag.

## [0.10.0] - 2026-11-18

Summary nodes, a recall subagent, and a log of the excerpt cut (hooks 1.12.0). New F54, F55 and F56 in `docs/spec.md`, ACs 236-254.

- Summary nodes: one leaf per compaction in `projects/<project>-<hash>/nodes.jsonl`, built from `compact_boundary` records without a model call. The SessionStart `compact` hook adds new nodes from the saved byte offset. It adds one line that names the newest node.
- New `recall.py` commands: `map`, `describe <id>`, `nodes --backfill` and `search --node <id>`. Search hits end with `[node <id>]` or `[node live]`.
- `--prove` checks the nodes (`nodes: <b> boundaries, <n> leaves, <bad> bad`) and prints `excerpt cut: ...`.
- `install.sh` writes the recall subagent `~/.claude/agents/recall.md` (source file `agents/recall.md`). It never overwrites a file without the marker line. `uninstall.sh` removes it only with the marker. The recall line gets one more sentence when the file is installed.
- Health rows have `excerpt_chars` and `excerpt_dropped`.

## [0.9.0] - 2026-11-18

Protect the store; steer Claude Code's summary (hooks 1.11.0, release 0.9.0). New F52 and F53 in `docs/spec.md`, ACs 218-235.

- Handoff `*.md` files are kept 365 days (was 14). `HANDOFF_RETENTION_DAYS` changes it; `0` keeps them forever. The status JSON files keep the 14-day rule.
- `postcompact-inject.py --check` warns when `cleanupPeriodDays` is below 90 (managed settings first, then user settings, then 30).
- `install.sh --keep-transcripts[=DAYS]` raises `cleanupPeriodDays` (default 365). It never lowers it. An interactive install asks y/N. `uninstall.sh` never changes it and says so.
- The PreCompact hook prints guidance for Claude Code's own summary. It also prints for `/compact <text>`; Claude Code adds it to that text. `HANDOFF_COMPACT_STEER=0` turns it off. The health row has a `steer` field.
- Test env `LOSSLESS_HANDOFF_MANAGED_SETTINGS` sets the managed settings path.

## [0.8.1] - 2026-11-17

Precomputed compactions (hooks 1.10.1).

- A precomputed compaction keeps its own handoff. If the handoff is older than 15 minutes but newer than the previous compaction boundary, SessionStart injects it with a `Note: this compaction was prepared at ...` line. It no longer runs recovery or says `HANDOFF RECOVERED`. New F51 in `docs/spec.md`, ACs 212-217.

## [0.8.0] - 2026-11-17

No silent misses (machine-setup F45 to F48, hooks 1.10.0).

- `install.sh --no-nudge` does not install the context nudge (`handoff-nudge.py` and its PostToolUse entry) and removes one left by an earlier install. New F50 in `docs/spec.md`, ACs 208-211.
- CI runs the code-blind acceptance tests from the new repository [lossless-handoff-tests](https://github.com/pratikgh0se/lossless-handoff-tests) on Ubuntu and macOS, pinned by `tests.ref`. The tests moved there from bento-mods-tests with their history, and were ported to this repository's layout (`LOSSLESS_HANDOFF_REPO`, `install.sh` in a temporary HOME).
- `docs/spec.md`: the canary plist runs the `hooks/handoff-canary.sh` wrapper (AC 71 amended); the machine-setup `--launchd`, `--render-only`, `LABEL_PREFIX` and `deps.tsv` statements are withdrawn; new F49 uninstall ACs 202-207.
- README: a Tests part (the three CI layers, the code-blind rule, `tests.ref`, how to run the acceptance tests locally).
- `tests/local-proof.sh`: one local command that runs every test layer, including real Claude Code compactions, and writes a dated report in `docs/proof/`.

- **Early write and recovery (F45).** PreCompact writes `<sid>-pending.json` and a deterministic handoff before the first model call, so a hook killed at its timeout still leaves a handoff. If a compaction still has none, the SessionStart `compact` hook rebuilds one from the transcript (no model call, 6 s deadline) and says `HANDOFF RECOVERED`.
- **Install check (F45).** `postcompact-inject.py --check` runs at SessionStart `startup` and `resume` (timeout 5) and prints one `HANDOFF CHECK:` line when a hook is not wired, a hook file is missing or changed since install, the two hooks' versions differ, or reconcile found compactions without a handoff. Silent otherwise.
- **Reconcile (F45).** `precompact-handoff.py --reconcile [--days N]` counts `compact_boundary` records against `health.jsonl` and writes `reconcile.json`.
- **Subagent handoffs (F46).** A compacting subagent (hook input with `agent_id`) gets its own handoff from its own transcript, stored as `<sid>-a-<agent_id>` files; it never touches the main session's files, never merges the decisions list and never starts the indexer. Before this, subagent compactions got no handoff (0 of 28 in the author's first 30-day reconcile).
- **Headless runs (F46).** `claude -p` and the SDK load user hooks by default. `install.sh --managed-hooks` stages a managed-settings file with the handoff hooks and prints the `sudo install -m 644 ...` command (macOS `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux `/etc/claude-code/managed-settings.json`); it never runs sudo. Only `--bare` skips managed hooks. `--check` accepts hooks from user or managed settings.
- **Proof (F47).** Every injected handoff appends a receipt to `receipts.jsonl`. `precompact-handoff.py --prove [--days N]` checks receipts with plain code (`lossless`, `right_agent`, `delivered`, `decisions_safe`), appends verdicts to `proofs.jsonl` and writes `proof-report.md`.
- **Claims register (F48).** New `hooks/claims.json` (C1 to C8). `--prove` evaluates it into `claims-status.json` and `claims-report.md` (`pass`, `fail`, `stale`, `no-data`); `--claims` prints it; `--claims-lint <file.md>` exits 1 unless every `[C<n>]` marker in a document is known and `pass`. README claims now carry these markers; C7 (blind quiz) and C8 (cost) are dated one-off studies.
- `install.sh` installs `claims.json`, writes `~/.claude/hooks/.handoff-manifest.json`, adds `postcompact-inject.py --check` to `startup` and `resume`, and on macOS installs the LaunchAgent `com.lossless-handoff.handoff-reconcile` (daily 04:15, `--reconcile --prove --days 30`, log `~/.local/state/claude-handoffs/reconcile.log`; `--no-nightly` skips it). On Linux it prints the crontab line. Everything it adds is recorded in the install marker. It prints "Restart open Claude Code sessions: hooks load when a session starts."
- `uninstall.sh` removes the `--check` entries, the reconcile LaunchAgent, the manifest and `claims.json`; it keeps all state (handoffs, receipts, proofs, indexes, ledgers) and tells you if a system managed-settings file may still hold the hook entries.
- `precompact-handoff.py` and `postcompact-inject.py` 1.10.0 copied from machine-setup; `recall.py` is byte-identical to the 1.8.0 copy.
- `tests/smoke.sh` covers a clean `--check`, a changed-file `--check`, `--reconcile` and `--prove` on an empty HOME, a subagent PreCompact input, `--managed-hooks`, and the install and uninstall round trip with the new entries.
- `handoff-nudge.py` ignores subagent events (non-empty `agent_id`): no note and no meta write, so a subagent is never told to write the main `<sid>-agent.md` (spec F46 f).
- Opt-in canary: `tools/handoff-canary/` (`run.sh`, `canary.py`, `check.py`) and `hooks/handoff-canary.sh`. `install.sh --canary` installs it with the LaunchAgent `com.lossless-handoff.handoff-canary` (03:30, `--catch-up`; crontab line on Linux); not part of a default install because it makes real model calls (about $0.03-0.10 per run). Uninstall removes it and keeps its state.
- `LOSSLESS_HANDOFF_NO_LAUNCHCTL=1` now covers all three LaunchAgents (recall index, reconcile, canary) in both `install.sh` and `uninstall.sh`. Before, it covered only the canary install, so a test install into a temp HOME on macOS loaded real jobs, and a test uninstall could unload the real ones. `tests/smoke.sh` sets it.
- `install.sh --help` printed `about ./install.sh.03-0.10` for the canary cost; it now prints `$0.03-0.10`.
- `uninstall.sh` also removes `handoff-canary/__pycache__/` (written when `canary.py` imports `check`), so the canary folder is removed. Found by CI on Linux and macOS runners.
- ADRs 0001, 0007, 0008 and 0011 moved into `docs/adr/` (generic versions) and the README links repointed.
- New `docs/handoff-reliability.md` and `docs/spec.md` (the behaviour contract the code-blind tests are written from, with AC numbers); this repository is now the home of the handoff system.
- README: a "When a compaction is missed" section, subagents, headless runs and "Proof, not promises"; the old "0 hook errors" wording is replaced by the measured miss counts (188 of 192 main compactions covered in a 30-day reconcile on 2026-11-17; 4 missed: 3 from a deliberate probe run with user settings off, 1 likely a hook killed at its timeout on 2026-10-23; 0 of 28 subagent compactions before this release; live subagent test after the fix: right-agent 2/2, delivered 2/2, failed 0). Also: measured daily use, hybrid search explained, comparison.

## [0.7.0] - 2026-11-14

Automatic recall indexing (machine-setup F44.13, hooks 1.8.0).

- `recall.py index --all` indexes every Claude Code project that sits in a git repo, newest first, with a time cap (`--max-seconds`), one summary line per project.
- `recall.py session-end` is a new `SessionEnd` hook: when a chat ends it starts a detached index of that project and prints nothing; it always exits 0.
- `install.sh` registers the `SessionEnd` hook (timeout 5) and records it so `uninstall.sh` removes only what was added.
- `install.sh` offers `ollama pull bge-m3` when `ollama` is on PATH (asks y/N interactively, `--yes` says yes, a non-interactive run skips with a hint). Without Ollama it prints the download link and continues; keyword search still works. Ollama is never installed for you.
- `install.sh` installs a macOS LaunchAgent `com.lossless-handoff.recall-index` (daily 03:30, `index --all`); `--no-nightly` skips it. On Linux it prints the crontab line.
- `install.sh` starts the first `index --all` in the background after install. New flags: `--yes`, `--no-nightly`.
- `uninstall.sh` removes the `SessionEnd` entry (if install added it) and boots out and deletes the LaunchAgent. Indexes, logs and state are kept.
- `precompact-handoff.py`, `postcompact-inject.py` and `recall.py` 1.8.0 copied from machine-setup.
- `tests/smoke.sh` covers the SessionEnd hook, `index --all`, the `--yes` and non-interactive model-pull paths, and uninstall.

## [0.6.1] - 2026-11-14

Long messages in full, a cold-model timeout fix, and measured results (machine-setup F44.12).

- `recall.py open <id> --full` now returns up to 40,000 characters of a message, and `--from <N>` pages through longer ones. A cut message ends with the exact command to continue. Found by a test that saw a 14,300-character message cut at the old 12,000-character output cap.
- Embedding calls wait up to 30 s for a cold model and ask Ollama to keep it loaded (`keep_alive`), so the first search after a pause no longer falls back to keyword only.
- The new-chat context and handoff now say the decisions list and your latest words win over recall when they disagree.
- `precompact-handoff.py`, `postcompact-inject.py` and `recall.py` 1.7.1 copied from machine-setup.
- LICENSE copyright line now carries the author's name.
- README: new "Proof it works" section with measured retrieval, meaning-search and alignment results, including limits.
- `tests/smoke.sh` checks that a 50,000-character message reassembles byte-exactly from `open --full` plus `open --full --from 40000`.

## [0.6.0] - 2026-11-14

Renamed to Lossless Handoff, based on the LCM paper (arXiv:2605.04050), plus recall (machine-setup F44, F44.9, F44.10, F44.11).

- Renamed from claude-handoff to Lossless Handoff (repo `lossless-handoff`; GitHub redirects the old URL). Install paths, the state folder `~/.local/state/claude-handoffs` and hook command names are unchanged, so existing installs upgrade in place. README now credits the LCM paper by Clint Ehrlich and Theodore Blackman.
- New `recall.py`: search, open, user, handoffs and index over the transcripts of all chats in the same project. Hybrid search merges word hits with optional meaning hits from a local embedding model (Ollama `bge-m3` by default; `RECALL_EMBED_URL`, `RECALL_EMBED_MODEL`, `RECALL_EMBED_BACKEND`, `RECALL_EMBED_KEY`). Words work without any setup. Secrets are redacted before output and before embedding.
- Handoffs and new-chat context now carry one line telling the model that recall exists.
- The meaning index is refreshed at each compaction by a detached background indexer that never delays or fails the hook.
- `precompact-handoff.py` and `postcompact-inject.py` 1.7.0 copied from machine-setup. RESOLVED and SUPERSEDED lines keep their full original text (the 700-character cap no longer cuts `was:` and `| why:`).
- `precompact-handoff.py --ledger-repair <dir>` restores cut RESOLVED/SUPERSEDED lines from the newest backup that still holds the full entry.
- `--ledger-maintain` rejects a missing directory (exit 2) and creates nothing when there is no ledger.
- An active ledger line without `| why:` is injected anyway and logged to `hooks-error.jsonl`, instead of being skipped.
- `install.sh` installs `recall.py`, adds the allow rule `Bash(python3 ~/.claude/hooks/recall.py:*)` (idempotent) and prints the one-time index command; `uninstall.sh` removes `recall.py` and that rule. `tests/smoke.sh` covers recall.

## [0.5.0] - 2026-11-09

Ledger kinds, lossless injection, later reminders (machine-setup F40.x, F41, F42, F43).

- `precompact-handoff.py` and `postcompact-inject.py` 1.6.0 copied from machine-setup.
- Decision kinds: every ledger entry is tagged `[rule]`, `[decision]` or `[task]` (untagged old lines count as decisions). New `RESOLVE D<n>: <reason>` marks finished tasks and one-offs `RESOLVED` (kept in the file, no longer injected). Rules are never resolved by the model.
- Lossless injection: the ledger is injected in three parts (`--ledger --part 1/2/3`) at compaction, `startup` and `resume`. Standing rules come first and always in full, then decisions, then tasks; entries that do not fit in full become one-line index entries, so nothing is dropped.
- `--ledger-maintain <repo>`: one pass over an existing list that tags entries, resolves finished tasks and supersedes contradictions, without adding entries or editing text.
- Handoff file: seventh heading `Parallel work running` (background agents and long jobs, or "none"); a stale-note warning when the model's own handoff did not change its living sections since the last reminder.
- `handoff-nudge.py`: reminders at 75% of the window, then every 25,000 tokens (was 50,000), plus one final "Compaction is imminent" note. Steps stay 12,000 tokens below the real compaction point (window minus 33,000): 300k, 325k, 350k and a final at 355k for a 400k window. Records section hashes so a stale handoff is called out. The nudge also asks the model to list standing rules under Decisions and why.
- `install.sh` registers `--ledger --part 1/2/3` for `compact`, `startup` and `resume`, and replaces the old single `--ledger` entry on re-run. `uninstall.sh` removes all of them.
- `tests/smoke.sh` updated for three parts, no-loss check over 40 entries, and the new nudge cadence.

## [0.4.0] - 2026-10-27

Faster decisions updates (machine-setup F40).

- `precompact-handoff.py` 1.5.0 copied from machine-setup. The decisions merge now returns only the changes (new decisions and `SUPERSEDE` lines) and code applies them to the list, so the call is much shorter. Entries are still never deleted.
- Own timeout: `HANDOFF_LEDGER_TIMEOUT` (seconds, 1-300, default 100), independent of the summary timeout. The PreCompact hook timeout is now 150; `install.sh` registers 150 and raises an older 90 on re-run.
- Catch-up: the merge reads your messages oldest first, up to 40,000 characters per run, so a backlog is caught up over several compactions instead of being dropped.
- Synthetic entries (tool output, command echoes, task notifications) no longer count as your messages for freshness, the merge input or "Last user messages".
- Health lines gain `ledger_error` (`timeout`, `exit <rc>`, `parse`, `locked` or an exception name) when the update fails.

## [0.3.0] - 2026-10-25

Per-project decisions ledger (machine-setup F39 to F39.11).

- `precompact-handoff.py` 1.4.2 and `postcompact-inject.py` copied from machine-setup. At every compaction a second Sonnet call merges your decisions since the last update into `~/.local/state/claude-handoffs/projects/<project>-<hash>/decisions.md` (project = git root, else the folder). Entries are only marked `SUPERSEDED`, never deleted; code restores dropped entries, rejects invalid output, caps lines at 700 characters, keeps 20 backups and holds a lock per project.
- New injection: `postcompact-inject.py --ledger --part 1/2` after compaction (full list, newest first), and `--ledger` at session `startup` and `resume` (active decisions, about 4,000 characters).
- Handoff file layout: authority line (user's latest words > Project decisions > Agent handoff > Fresh summary > Claude Code's summary > Facts), no `## Decisions via questions` section, cap 14,000 characters, tool lines end `-> ok`, `-> error` or `-> no result`.
- New switches: `HANDOFF_LEDGER=0` disables the list entirely; `HANDOFF_LEDGER_MODEL` picks its model. Health lines gain ledger status and counts.
- `install.sh` registers the two `--ledger --part` commands in the `compact` group and new `startup` and `resume` groups (idempotent; the double-registration check covers them). `uninstall.sh` removes exactly those and keeps the ledgers, saying so.
- `tests/smoke.sh`: shim tells the merge call from the summary call; cases for ledger creation, restore on drop, all inject modes, `HANDOFF_LEDGER=0`, install/uninstall of the new groups. Git-specific check prints SKIP without git.
- Cost: two Sonnet calls per compaction, about $0.10-0.20, 20-40 s. Measured on 96 questions about earlier decisions: 13.5 -> 56 out of 100 (README).

## [0.2.0] - 2026-10-23

- `precompact-handoff.py` 1.3.0 (machine-setup F38): a fresh Sonnet summary of the conversation since the last compaction is written at every compaction (`claude -p --model sonnet`, isolated flags), placed after the model's own handoff. It replaces the Haiku fallback.
- New switches: `HANDOFF_SUMMARY=0` disables it, `HANDOFF_SUMMARY_MODEL` (default `sonnet`), `HANDOFF_SUMMARY_TIMEOUT` (seconds, default 75). `HANDOFF_HAIKU_FALLBACK=0` still disables it.
- Health lines gain `summary_model`; the section is headed `## Fresh summary at compaction (<model>; verify against facts)` and failures read `Fresh summary failed: <reason>`.
- `tests/smoke.sh`: cases with a fake `claude` shim (summary placement, disable switch, model override, timeout).
- README: blind-test quality numbers, configuration table, cost FAQ; SECURITY.md describes the per-compaction call.
- `install.sh` reports version 0.2.0; nothing else in install/uninstall changes.

## 0.1.0 - 2026-10-20

Extracted from machine-setup v1.0.0 (the Bento handoff system, spec features F35 to F37).

- PreCompact hook `precompact-handoff.py` and SessionStart hook `postcompact-inject.py` copied unchanged.
- New `handoff-nudge.py` (PostToolUse): built-in handoff nudge at 75% of the window, then every 50k tokens, with the same text and meta file as the Bento compact plugin.
- `install.sh` / `uninstall.sh`: idempotent settings merge, backups, dry-run.
- `install.sh --help` lists all flags and the version; new `--version` flag.
- `tests/smoke.sh`: install, hooks, nudge, uninstall in a temporary HOME. Runs on macOS and Linux.
- GitHub Actions CI (`.github/workflows/ci.yml`): `bash -n`, shellcheck, `py_compile` and the smoke test on ubuntu-latest and macos-latest.
- `SECURITY.md` and a bug report issue template.
- Nudge text identifies itself as coming from the user's installed claude-handoff hook (not the tool result), so models do not dismiss it as prompt injection.
- `install.sh` refuses (exit 2) when the same hooks are registered from another path, to avoid double execution; `--force` overrides.
- README FAQ: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` must be set in user settings or the shell (project settings did not override it); the window must comfortably exceed baseline context (very small windows below about 150k compact before the nudge); one very large tool result can skip the nudge, the PreCompact fallback still writes a handoff.
- README: prerequisites, one-line install, exact list of changes, FAQ, troubleshooting, CI badge.
