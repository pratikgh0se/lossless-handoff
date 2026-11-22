# Configuration and state files

Every setting and every file that the hooks write. This page is part of the [docs index](README.md).

## Configuration

| Variable | Effect |
|---|---|
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | Window size in tokens. Nudge thresholds are 75% of it, then every 25,000, then a final nudge (window minus 45,000). If it is unset, empty or not a positive integer, the nudge learns the window from past compactions, or uses 200000 (see "Which window the nudge uses"). The installer sets it only with `--window N`. |
| `HANDOFF_SUMMARY=0` | Disable the fresh summary at compaction (no model call; facts only). |
| `HANDOFF_SUMMARY_MODEL` | Model for the fresh summary. Default `sonnet`. |
| `HANDOFF_SUMMARY_TIMEOUT` | Seconds to wait for the summary (1-300). Default 75. |
| `HANDOFF_LEDGER=0` | Disable the decisions list: no merge call, nothing written, nothing injected. |
| `HANDOFF_LEDGER_MODEL` | Model for the decisions update. Default: the summary model. |
| `HANDOFF_LEDGER_TIMEOUT` | Seconds to wait for the decisions update (1-300). Default 100. The PreCompact hook timeout is 150. |
| `HANDOFF_HAIKU_FALLBACK=0` | Legacy switch from 0.1.x; still honoured and also disables the fresh summary. |
| `RECALL_EMBED_URL`, `RECALL_EMBED_MODEL`, `RECALL_EMBED_BACKEND`, `RECALL_EMBED_KEY` | Meaning search backend. Defaults: Ollama at `http://localhost:11434`, model `bge-m3`. Backend `openai` works with any `/v1/embeddings` server; the key is an optional Bearer token. |
| `HANDOFF_RETENTION_DAYS` | Days to keep handoff `*.md` files. Default 365. `0` keeps them forever. A value that is not a whole number counts as 365. The `*-degraded.json`, `*-last-health.json` and `*-pending.json` files always use 14 days. |
| `HANDOFF_AUTO_RECALL=0` | Switch off auto-recall at run time (see [Auto-recall](recall.md#auto-recall)). The hook then prints nothing and exits 0. It has no effect when auto-recall is not installed. |
| `HANDOFF_RECALL_RULES=0` | Switch off the strict recall rule line at run time (see [Strict recall rule](recall.md#strict-recall-rule-and-the-recall-tool)). The output is then the same as without `--recall-rules`. It has no effect when the rules are not installed. |
| `HANDOFF_SUBAGENT_RULES=0` | Do not give project rules to a new subagent. The `SubagentStart` hook then prints nothing. |
| `HANDOFF_COMPACT_STEER=0` | Do not print the guidance for Claude Code's own compaction summary. The hook prints it by default. The guidance is added to any text that you type after `/compact`. A later benchmark measures the effect on summary quality. |
| `LOSSLESS_HANDOFF_MANAGED_SETTINGS` | Test only. Path of the managed settings file that `install.sh`, `uninstall.sh` and `postcompact-inject.py --check` read instead of `/Library/Application Support/ClaudeCode/managed-settings.json`. The older name `HANDOFF_MANAGED_SETTINGS_PATH` still works. The tools never create the real file. |
| `HANDOFF_STATE_DIR` | Use another directory instead of `~/.local/state/claude-handoffs`. |
| `LOSSLESS_HANDOFF_NO_LAUNCHCTL=1` | `install.sh` and `uninstall.sh` write and remove the LaunchAgent files but never call `launchctl`. Use it for any install into a temporary HOME on macOS. `launchctl` acts on your real login session, so without it a test install loads, and a test uninstall unloads, jobs with the real labels. |
| `HANDOFF_HOOK_CHILD` | The hook sets it for its own summary call, so it never recurses. Do not set it yourself. |

## What gets written where

| Path | Content |
|---|---|
| `~/.local/state/claude-handoffs/<sid>-latest.md` | The handoff that the hooks inject after compaction. `<sid>-<timestamp>.md` is a copy. |
| `<sid>-agent.md` | The model's own handoff (written by the model). |
| `<sid>-agent.meta.json` | Nudge state and timestamps (numbers only, no text). |
| `recall-index.json`, `recall/` | Recall caches: which transcripts belong to which project, and the meaning index (`<project>.sqlite`, `index.log`). Safe to delete. |
| `auto-recall.jsonl` | Only with `--auto-recall`. One JSON line for each prompt that refers to the past: `ts`, `session_id`, `fired`, `cue` (the matched phrase from a fixed list, or `quote`), `hits`, `ms`, `timed_out`. It holds no prompt text and no quotes. |
| `recall-mcp.jsonl` | Only with `--recall-mcp`. One JSON line for each tool call: `ts`, `tool`, `hits`, `ms`. It holds no query text and no quotes. |
| `health.jsonl` | One JSON line for each compaction: time, trigger, agent file fresh/stale/absent, fallback result, decisions update result (`ok`, `unchanged`, `failed`, `locked`, `disabled`) and counts, duration, size, status `ok` / `reduced` / `degraded`. No message text or paths. It rotates at 5,000 lines. |
| `<sid>-last-health.json`, `<sid>-degraded.json` | Per-session status and degraded reasons. |
| `<sid>-pending.json` | Marker while PreCompact runs. The health line removes it. |
| `<sid>-a-<agent_id>*` | The same files for a compacting subagent (handoff, latest, pending, note). Health rows for subagents carry `agent_id` and `agent_type`. |
| `reconcile.json`, `reconcile.log` | Last reconcile result (with the once-only notice marker for `--check`) and the output of the nightly job. |
| `receipts.jsonl`, `proofs.jsonl`, `proof-report.md`, `proof-notice.json` | Proof: one receipt for each injected handoff, one verdict for each receipt, the report, and the once-only alert marker. |
| `claims-status.json`, `claims-report.md`, `claims-notice.json` | Claims register evaluation and its once-only alert marker. |
| `managed-settings.json` | Only after `install.sh --managed-hooks`: the file that you copy to the system location. |
| `~/.claude/hooks/.handoff-manifest.json` | Installed hook file hashes, including the files in `lossless_handoff/` (not in the state folder). |
| `projects/<project>-<hash>/nodes.jsonl`, `nodes.state.json`, `nodes.lock` | Summary nodes: one line for each compaction (ids, session, line range, times, token count, paths). It holds no message text. |
| `~/.claude/agents/recall.md` | The recall subagent (user level; not in the state folder). Source file in this repository: `agents/recall.md`. |
| `projects/<project>-<hash>/decisions.md` | The decisions list of the project (uninstall keeps it). Next to it: `decisions.<timestamp>.md`, `decisions.state.json`, `decisions.lock`. |

The hooks keep handoff files (`*.md`) for 365 days. Set `HANDOFF_RETENTION_DAYS` to change this, or to `0` to keep them forever. The hooks remove the small status files (`*-degraded.json`, `*-last-health.json`, `*-pending.json`) after 14 days. The hooks never clean the `projects/` folder. A longer retention means that more of your chat text stays on disk for longer. Claude Code also deletes its own chat transcripts after `cleanupPeriodDays` days (default 30), and recall can find only messages that are still there. Run `./install.sh --keep-transcripts` to set it to 365 (it never lowers a value and never overrides a managed settings file). The session-start check warns if the value is below 90. The directory is outside `~/.claude/` because Claude Code refuses model writes under that path ([ADR 0008](adr/0008-handoffs-directory-under-local-state.md)).
