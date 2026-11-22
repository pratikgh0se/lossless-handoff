# Handoff reliability: misses, subagents, headless runs, proof

This page describes handoff hooks 1.10.0 (F45 to F48). Sections at the end add hooks 1.11 to 1.14. The contract is [`docs/spec.md`](spec.md). A hook cannot force Claude Code to run it. So the design has several layers of defence: write early, recover after, detect every miss, check the install, and keep evidence. For the parts and the files, read [architecture.md](architecture.md).

## When a compaction is missed

| Step | What happens | Where |
|---|---|---|
| Write early | PreCompact writes `<sid>-pending.json` first. Then it writes a deterministic handoff (facts and the last messages of the user) before the first model call. The health line removes the pending file. A hook that stops at its timeout still leaves a handoff. | `precompact-handoff.py` |
| Recover | If the compaction still has no handoff, the SessionStart `compact` hook rebuilds one from the transcript (no model call, 6 s deadline). It prints `HANDOFF RECOVERED` on the first line. | `precompact-handoff.py --recover`, called by `postcompact-inject.py` |
| Install check | At SessionStart `startup` and `resume`, one `HANDOFF CHECK:` line appears in these cases: a hook is not wired, a hook file is missing or changed since install, the versions of the two hooks differ, or reconcile found compactions without a handoff. A separate `format drift` line appears when reconcile found format drift. The hook is silent when all is well. | `postcompact-inject.py --check` |
| Nightly reconcile and proof | With `--nightly` (opt-in), `--reconcile --prove --days 30` runs at 04:15. It counts every `compact_boundary` in your transcripts against `health.jsonl` and proves each receipt. | LaunchAgent `com.lossless-handoff.handoff-reconcile` (`install.sh --nightly` writes it; on Linux it prints a crontab line)  A default install has no nightly reconcile, proof or format-drift alarm. Install with `--nightly`, or run `precompact-handoff.py --reconcile --prove` yourself. |

`--check` also reads the manifest `~/.claude/hooks/.handoff-manifest.json` that `install.sh` writes (a hash for each installed hook file, including the files in `lossless_handoff/`). Restart open Claude Code sessions after you install: hooks load when a session starts.

Real result, first 30-day reconcile on 2026-11-17: 188 of 192 main compactions were covered. Of the 4 that were missed, 3 came from a deliberate sandbox probe run with user settings turned off, and 1 was on 2026-10-23. For subagents, 0 of 28 compactions were covered. All of those are from before this change.

## Subagents

When a subagent compacts, Claude Code fires the hooks with `agent_id` set, but with the `session_id` and `transcript_path` of the parent. The hooks now treat that as its own compaction.

- Each compacting subagent gets its own handoff. The hooks build it from its own transcript (`<session>/subagents/agent-<agent_id>.jsonl` next to the parent transcript). The files have the key `<sid>-a-<agent_id>`.
- A subagent event never reads or writes the handoff files of the main session. The main session never reads or writes the handoff files of a subagent.
- A subagent run never merges the decisions list and never starts the indexer. The hooks still inject the decisions list rules into the subagent, as for the main agent.
- If the subagent transcript is missing, the health row says `degraded` / `agent_transcript_missing` and the hooks write no handoff.
- The nudge hook (`handoff-nudge.py`) ignores subagent events (non-empty `agent_id`), so it never nudges a subagent.

## Headless runs

`claude -p` and the Agent SDK load user hooks by default. They skip them only in these cases:

- `--bare`
- `disableAllHooks`
- `--setting-sources` without `user`
- SDK `settingSources: []`

Optional: `./install.sh --managed-hooks`. It is a standalone mode (it does nothing else). It writes `~/.local/state/claude-handoffs/managed-settings.json` (the existing system file, if there is one, merged with the handoff hook entries). It prints a `sudo install ...` command for you to run. The script never runs sudo. With managed hooks installed, only `--bare` can skip them. `--check` accepts hooks from user or managed settings. Reconcile counts headless sessions separately. It reports any compaction that still got no handoff.

## Proof

Every handoff that `postcompact-inject.py` injects appends a receipt. `precompact-handoff.py --prove` checks receipts with plain code, with no model and no cost.

| File (in `~/.local/state/claude-handoffs/`) | Written by | Holds |
|---|---|---|
| `receipts.jsonl` | `postcompact-inject.py` | One line for each injected handoff: transcript, boundary time, hashes, first 300 chars |
| `proofs.jsonl` | `--prove` | One verdict for each receipt (the command skips receipts that it already proved) |
| `proof-report.md` | `--prove` | Summary line first, then failed checks and skip counts |

| Check | Passes when |
|---|---|
| `lossless` | 5 sampled messages from the transcript come back word for word through the own commands of recall (skipped if recall is absent or the transcript is short) |
| `right_agent` | every quoted user line in the handoff occurs in the own transcript of the receipt |
| `delivered` | the transcript after the boundary holds the injected text |
| `decisions_safe` | no id of the decisions list that was present before a merge is missing after it, and no superseded id came back |

Coverage is not a check for each receipt. It is a number from reconcile. Receipts start with the first compaction after install, so earlier compactions have none.

| Command | Does |
|---|---|
| `precompact-handoff.py --reconcile [--days N]` | Counts compactions against handoffs; writes `reconcile.json` |
| `precompact-handoff.py --prove [--days N]` | Proves receipts, writes `proof-report.md`, evaluates the claims register |
| `precompact-handoff.py --claims` | Prints the claims table |
| `precompact-handoff.py --claims-lint <file.md>` | Checks `[C<n>]` markers in a document |
| `postcompact-inject.py --check` | Install check; also shows `proof failed` and `claims` alerts once |

## Claims register

`hooks/claims.json` lists every public claim with its evidence source, window, refresh time and minimum rate. Each `--prove` run writes `claims-status.json` and `claims-report.md`.

| Status | Meaning |
|---|---|
| `pass` | rate at or above the minimum, evidence present and fresh |
| `fail` | rate below the minimum |
| `stale` | evidence older than its refresh time |
| `no-data` | no evidence in the window (neither failing nor stale) |

Before you publish a document that cites claims, run `--claims-lint <file.md>`. It fails on an unknown, failing or stale `[C<n>]`. The register holds only ids, texts, counts, rates and dates.

## Hooks 1.11 to 1.14

These versions add parts to the system. [architecture.md](architecture.md) shows how the parts fit together. The summary below lists what each version adds.

| Hooks | What it adds | Where to read more |
|---|---|---|
| 1.11.0 | The hooks protect the chat store (handoff files live 365 days; `--keep-transcripts` raises the retention of Claude Code transcripts). The PreCompact hook prints guidance for the summary of Claude Code (`HANDOFF_COMPACT_STEER`). | [What gets written where](configuration.md#what-gets-written-where) |
| 1.12.0 | Summary nodes: one node for each compaction in `nodes.jsonl`, and the commands `recall.py map`, `describe` and `nodes`. The recall subagent (`agents/recall.md`, model `haiku`). A log of the excerpt cut in the health row. | [Recall commands](recall.md#recall-commands) |
| 1.13.0 | The `SubagentStart` hook. A new subagent gets the active rules of the decisions list and the recall line. Each run adds a `subagent_start` row to `health.jsonl`. | [Subagents](hooks-in-detail.md#subagents) |
| 1.13.1 | Search finds the work of subagents by default (`--no-subagents` leaves it out). In a plain pattern, a space, `-` and `_` match each other. A hit from a tool call shows `-> result <id>`. | [Recall commands](recall.md#recall-commands) |
| 1.14.0 and 1.14.1 | `llm_map.py` and its skill (moved to the separate llm-map project in 0.14.0). | [llm-map](llm-map.md) |
| 1.14.2 | The nudge learns its window from past compactions (else 200,000) when `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is not set, and health rows hold `window` and `window_source`. Reconcile sets `format_drift` when a session has injects and no `compact_boundary` record. `--prove`, the claims and `--check` show it. | [Limitations](limitations.md) |
| 1.15.0 | One redaction function masks secrets as `[REDACTED:<kind>]` in handoff files, the decisions list and recall output. The default install is the core only (release 0.13.0). | [Install](install.md#install), SECURITY.md |
| 1.19.0 | `recall.py history`, the MCP tool `key_history`, whole-token ranking and exclusions in search, and auto-recall with the history of identifiers. Release 0.17.0. | [Key history](recall.md#key-history) |
| 1.18.0 | The opt-in strict recall rule line (`--recall-rules`) and the opt-in recall MCP tool `recall-mcp.py` (`--recall-mcp`). Release 0.16.0. | [Strict recall rule and the recall tool](recall.md#strict-recall-rule-and-the-recall-tool) |
| 1.17.0 | The opt-in `auto-recall.py` hook (`UserPromptSubmit`, `./install.sh --auto-recall`): when the prompt refers to the past, it adds the top 3 recall hits to the context. Release 0.15.0. | [Auto-recall](recall.md#auto-recall) |
| 1.16.0 | A subagent gets a preamble line before the rules: apply only the rules that fit the task, the task brief comes first. llm_map is no longer part of this repository (release 0.14.0). | [Subagents](hooks-in-detail.md#subagents) |

## State files

All files are in `~/.local/state/claude-handoffs/` unless the table says another place.

| File | Purpose |
|---|---|
| `<sid>-pending.json` | Marker while PreCompact runs; the health line removes it |
| `<sid>-a-<agent_id>*` | Per-subagent twins of the handoff, latest, pending and agent handoff files |
| `health.jsonl` | One row for each compaction (subagent rows carry `agent_id`, `agent_type`; merge rows carry ids of the decisions list; `window` and `window_source` come from the nudge, F62) |
| `reconcile.json` | Last reconcile result, plus the notice marker for `--check`. It holds `format_drift`, `format_drift_sessions` and `format_drift_reason` (F63). |
| `receipts.jsonl`, `proofs.jsonl`, `proof-report.md`, `proof-notice.json` | Proof (above) |
| `claims-status.json`, `claims-report.md` | Claims evaluation |
| `claims-notice.json` | Once-only tracker for the claims alert in `--check` (like `proof-notice.json`) |
| `reconcile.log` | Output of the nightly job |
| `projects/<project>-<hash>/nodes.jsonl` | Summary nodes: one line for each compaction |
| `~/.claude/hooks/.handoff-manifest.json` | Installed hook file hashes (including `lossless_handoff/`) |
