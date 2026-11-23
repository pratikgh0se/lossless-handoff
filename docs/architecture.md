# Architecture

This page shows every part of Lossless Handoff, the Claude Code event that calls each part, and the files each part reads and writes. It describes product 0.15.0 and hooks 1.17.0.

![Architecture of Lossless Handoff](architecture.svg)

## Summary

- Five Claude Code events call the hooks: `PostToolUse`, `PreCompact`, `SessionStart`, `SubagentStart` and `SessionEnd`. `SessionStart` has three matchers: `compact`, `startup` and `resume`.
- The default install has the core only: the hooks, `recall.py`, the recall subagent and the decisions list. Three parts are opt-in flags: `--window N` (the compaction window value), `--nightly` (two jobs that run outside Claude Code: the index job at 03:30 and the reconcile-and-prove job at 04:15), and `--semantic` (the local bge-m3 model for meaning search). `--save-defaults` keeps the chosen flags in `~/.config/lossless-handoff/install-defaults`. A rerun never removes an installed part.
- One redaction step masks secrets before the hooks write handoff files, the decisions list and recall output. See [Redaction](#redaction).
- Claude Code keeps the chat transcripts in `~/.claude/projects/`. Lossless Handoff only reads them. It never writes to them.
- All other files go to the state folder, `~/.local/state/claude-handoffs/` (or `$HANDOFF_STATE_DIR`). See [ADR 0008](adr/0008-handoffs-directory-under-local-state.md).
- No hook forces the model to search. Read [How Claude decides to use recall](#how-claude-decides-to-use-recall).

## Parts, events, reads and writes

| Part | File | Claude Code event | Reads | Writes |
|---|---|---|---|---|
| Nudge | `hooks/handoff-nudge.py` | `PostToolUse` (matcher `*`, timeout 5) | The tail of the transcript, `<sid>-agent.md`, `<sid>-agent.meta.json` | `<sid>-agent.meta.json`. It adds one nudge text to a tool result. The model writes `<sid>-agent.md`. |
| Handoff | `hooks/precompact-handoff.py` (entry) and `hooks/lossless_handoff/` (code) | `PreCompact` (timeout 150) | The transcript, `<sid>-agent.md`, the meta file, `decisions.md` | `<sid>-pending.json`, `<sid>-latest.md` and its timestamped copy, `health.jsonl`, `decisions.md` and its backups. It starts the indexer in the background. |
| Inject after compaction | `hooks/postcompact-inject.py --part 1/2/3` and `--ledger --part 1/2/3` | `SessionStart`, matcher `compact` (timeout 10) | `<sid>-latest.md`, `decisions.md`, `nodes.jsonl` | `receipts.jsonl`, `nodes.jsonl`, `nodes.state.json`. It prints the handoff and the decisions list as context. |
| Inject at start | `hooks/postcompact-inject.py --ledger --part 1/2/3`, and `--check` | `SessionStart`, matchers `startup` and `resume` (timeout 5 for `--check`) | `decisions.md`, the install manifest, `reconcile.json` | A once-only alert marker. It prints the decisions list, and one `HANDOFF CHECK:` line when something is wrong. |
| Re-anchor (opt-in, `--reanchor`) | `hooks/reanchor.py` | `PostToolUse` (matcher `*`, timeout 5) | The end of the transcript (token count) and `postcompact-inject.py --rules-block` (the SubagentStart rules text) | One note each 50,000 tokens of growth, only when the project has an active rule. One `reanchor` row in `health.jsonl`. |
| Claim guard (opt-in, `--claim-guard`) | `hooks/claim-guard.py` | `Stop` (timeout 5) | The last reply and the end of the transcript | One row in `claim-guard.jsonl` (no text). Prints a Stop `block` only with `HANDOFF_CLAIM_GUARD=block`. |
| History guard (opt-in, `--history-guard`) | `hooks/history-guard.py` | `Stop` (timeout 5) | The last reply and the current session transcript | Stop `additionalContext` with the F73 history of a changed key (live mode), or one log row (shadow) |
| Action guard (opt-in, `--action-guard`) | `hooks/action-guard.py` | `PostToolBatch` (timeout 5) | The new text of Edit, Write, MultiEdit, NotebookEdit and the Bash command | `additionalContext` with the F73 history (inject mode), or one log row (shadow) |
| Auto-recall (opt-in, `--auto-recall`) | `hooks/auto-recall.py` | `UserPromptSubmit` (timeout 5; the hook stops itself after 2 seconds) | The transcripts of this project and the recall index (through `recall.py`) | One row in `auto-recall.jsonl` (no prompt text). It prints at most 3 quotes, only when the prompt refers to the past. |
| Recall rule (on by default) and recall tool (opt-in, `--recall-mcp`) | The marker `.recall-rules`, and `hooks/recall-mcp.py` (an MCP server over stdio that Claude Code starts) | The inject hook adds the rule line (SessionStart and SubagentStart); the MCP server runs while Claude Code is connected | The transcripts of this project and the recall index (through `recall.py`) | `receipts.jsonl` (flag `recall_rules`) and `recall-mcp.jsonl` (no query text). The rule line and the tool name are added to the recall line. |
| Subagent rules | `hooks/postcompact-inject.py --subagent-start` | `SubagentStart` (timeout 10) | `decisions.md` | One `subagent_start` row in `health.jsonl`. It prints the active rules and the recall line to the new subagent. |
| End-of-chat index | `hooks/recall.py session-end` | `SessionEnd` (timeout 5) | The transcript of the chat | The index in `recall/` |
| Nightly index | `hooks/recall.py index --all --max-seconds 3000` | launchd, 03:30 (macOS) | All transcripts of all projects | The index in `recall/` |
| Nightly reconcile and proof | `hooks/precompact-handoff.py --reconcile --prove --days 30` | launchd, 04:15 (macOS), only with `--nightly` | The transcripts, `health.jsonl`, `receipts.jsonl`, `hooks/claims.json` | `reconcile.json`, `proofs.jsonl`, `proof-report.md`, `claims-status.json`, `claims-report.md` |
| Recall | `hooks/recall.py search`, `open`, `map`, `describe`, `user`, `handoffs` | None. The model runs it through Bash. | The index, the transcripts, `nodes.jsonl`, the handoff files | Nothing. Search is read-only. |
| Summary nodes | `hooks/recall.py nodes`, and the `compact` inject hook | `SessionStart`, matcher `compact` | `compact_boundary` records in the transcript | `projects/<project>-<hash>/nodes.jsonl` |
| Decisions list | `projects/<project>-<hash>/decisions.md` | Updated by `PreCompact`. Injected by `SessionStart` and `SubagentStart`. | n/a | n/a |
| Subagent handoffs | The same hooks, with `agent_id` set | `PreCompact` and `SessionStart` in a subagent | `<session>/subagents/agent-<agent_id>.jsonl` | `<sid>-a-<agent_id>*` files |
| Recall subagent | `agents/recall.md` (installed to `~/.claude/agents/recall.md`) | None. The model starts it with the Agent tool. | It runs `recall.py` (tools `Bash` and `Read`, model `haiku`) | Nothing |
| Canary (opt-in) | `tools/handoff-canary/` | launchd, 03:30, `--canary` install only | A temporary project | One `trigger: "canary"` row in `health.jsonl`, and `~/.local/state/handoff-canary/` |

Notes:

- The decisions update in `PreCompact` runs for a main session only. A subagent never merges the decisions list and never starts the indexer.
- The nightly jobs (opt-in with `--nightly`) are macOS LaunchAgents. On Linux, `install.sh` prints the crontab lines. A default install has no nightly reconcile, proof or format-drift alarm. Install with `--nightly`, or run `precompact-handoff.py --reconcile --prove` yourself.

## Data flow around a compaction

### Before the compaction

1. The model works. After each tool call, `handoff-nudge.py` reads the last token count from the transcript. It also picks the window: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` if it is a positive integer (`env`), else the median `preTokens` of the newest `compact_boundary` records plus 33,000, rounded to 1,000 (`learned`), else 200,000 (`default`). It stores `window` and `windowSource` in `<sid>-agent.meta.json`.
2. When the count reaches 75% of the window (300k for a 400k window), the hook adds one nudge text to the tool result. The nudge asks the model to write `<sid>-agent.md` with seven headings.
3. The hook repeats the nudge every 25,000 tokens (325k and 350k). It adds a final nudge at 355k, worded "Compaction is imminent". The real compaction point is 367k (window minus 33,000).
4. If the three living sections are byte-identical to the previous nudge, the next nudge says that the handoff is stale.

### During the compaction

1. Claude Code runs the `PreCompact` hook. The hook first prints guidance for the summary of Claude Code (quote the user's words, keep exact values, list running work, end with the next step; F53). `HANDOFF_COMPACT_STEER=0` turns this off. The hook writes `<sid>-pending.json`, then a deterministic handoff (facts and your last messages), before the first model call.
2. The hook then runs two `claude -p --model sonnet` calls in parallel: the fresh summary and the decisions update. A compaction takes about 20-40 s.
3. The hook writes `<sid>-latest.md` (14,000 characters at most) and one row in `health.jsonl`. The row holds `window` and `window_source`, copied from the nudge meta when it exists. It removes the pending file. It always exits 0.
4. The hook starts the indexer in the background.

### After the compaction

1. Claude Code runs the `SessionStart` hooks with the matcher `compact`. Each hook output is limited to 10,000 characters, so the handoff comes in three parts (`--part 1/2/3`) and the decisions list in three parts (`--ledger --part 1/2/3`).
2. Claude Code can precompute a compaction: PreCompact runs when it builds the summary, and Claude Code applies the compaction later (F51). If the handoff is newer than the previous compaction boundary, the hook injects it with the line `Note: this compaction was prepared at <time>; ...` and does not run recovery.
3. If the handoff is missing, the hook rebuilds it from the transcript with no model call. The text starts with `HANDOFF RECOVERED`.
4. The hook adds the new summary nodes to `nodes.jsonl` and writes one line to `receipts.jsonl`. Part 1 names the newest node.
5. In a new or resumed chat (`startup` and `resume`), the hooks inject the decisions list and run the install check.
6. With `--nightly` (opt-in), at 04:15, the reconcile job counts every `compact_boundary` against `health.jsonl` and proves each receipt. It also compares the recorded injects (main-session rows of `receipts.jsonl`) with the `compact_boundary` records of each session. If a session has injects and no boundary, it writes `format_drift: true`, `format_drift_sessions` and `format_drift_reason` to `reconcile.json`. `--prove` and the claims then show `fail` (reason `format_drift`) for the affected checks.
7. At the next `startup` or `resume`, `--check` prints one separate `format drift` line while the newest `reconcile.json` has `format_drift: true`.

## Redaction

`redact()` in `hooks/recall.py` is the one redaction function. The PreCompact hook (`hooks/lossless_handoff/redaction.py`) loads it from `recall.py`. It replaces each secret with `[REDACTED:<kind>]` and leaves text without a secret unchanged.

| Where the text goes | Who calls `redact()` |
|---|---|
| `<sid>-<timestamp>.md` and `<sid>-latest.md` (the deterministic part, the model handoff copy and the fresh summary) | `write_handoff_files` in `hooks/lossless_handoff/redaction.py` |
| `decisions.md` and its timestamped backups | The decisions writers in `hooks/lossless_handoff/ledger.py` |
| `recall.py` output and the meaning index | `recall.py` (search, open, user, describe, index) |

The model writes `<sid>-agent.md` itself with the Write tool. No hook sees that write, so the original file is not masked. The hooks mask every copy of it. If `recall.py` cannot load, the PreCompact hook uses a copy of the same patterns that it holds itself (the smoke test checks that both give the same output). See `SECURITY.md` for the kinds and the limits.

## PreCompact hook code layout

`hooks/precompact-handoff.py` is the entry file. It keeps `HOOK_VERSION` and the command line switch (`--reconcile`, `--prove`, `--claims`, `--claims-lint`, `--recover`, `--ledger-maintain`, `--ledger-repair`). It loads the code from the folder `hooks/lossless_handoff/`.

| File in `hooks/lossless_handoff/` | What it holds |
|---|---|
| `transcript.py` | Settings, transcript parsing, the git facts |
| `summary.py` | The agent file, the fresh summary call, the recall text |
| `ledger.py` | The decisions list: read, merge, maintain, repair |
| `handoff.py` | The handoff sections, `build`, the status |
| `runtime.py` | Atomic writes, the health line, the error log, subagent views, the indexer |
| `redaction.py` | The fallback copy of the redaction code, `redact`, `redact_tree`, `write_handoff_files` |
| `pipeline.py` | The main PreCompact pipeline, the health writer, `--recover` |
| `reconcile.py` | The format drift scan and `--reconcile` |
| `prove.py` | The receipt checks and `--prove` |
| `claims.py` | The claims register: `--claims`, `--claims-lint` |

`lossless_handoff/__init__.py` runs each file in the namespace of the entry file, by path and in a fixed order (the order of the old single file). The parts share one namespace. They are not importable alone and they are not separate Python modules. So every function is still a name of `precompact-handoff.py`, and a caller that loads the entry file with `importlib` can still call it or set a global such as `HANDOFFS_DIR`. If the folder is missing or broken, the entry file prints one line on stderr and exits 0. The hook never breaks a session. `install.sh` installs the folder and lists its files in `.handoff-manifest.json`. `uninstall.sh` removes it.

## How Claude decides to use recall

Recall is a command line tool, not an MCP server. It costs about 0 tokens until the model uses it. Four things tell the model that recall exists and what it holds.

**1. The recall line.** Part 1 of the decisions list holds this line, right after its heading. The handoff text and the `SubagentStart` block also hold it. The text below is `RECALL_LINE` in `hooks/postcompact-inject.py`:

```text
- recall: earlier chats in this project are searchable word for word: python3 ~/.claude/hooks/recall.py search "<words or regex>" (then: recall.py open <id>; recall.py user --last 20). Use it before acting on anything vague here (exact commands, numbers, paths, names, the user's wording) and whenever the user refers to something said earlier; quote what you find instead of guessing. If recall and the decisions list disagree, the decisions list and the user's latest words win: recall shows history, not current rules. If a hit is a tool call that shows `-> result <id>`, open that id to see what the tool returned.
```

**2. The recall subagent sentence.** When `~/.claude/agents/recall.md` is installed and holds the marker line, the hook adds this sentence to the recall line:

```text
For wide lookups (many hits, long messages, or a whole node), ask the recall subagent (Agent tool, subagent_type "recall") so the main context stays small; for one exact fact, search directly.
```

The recall subagent uses model `haiku` and the tools `Bash` and `Read`. It runs `map`, `search`, `describe` and `open`. It returns at most 15 short quotes with message ids and node ids.

**3. The summary node line.** After a compaction, part 1 of the handoff holds one more line, right after its recall line. The line has this form (the hook fills the fields from `nodes.jsonl`):

```text
- summary node: this chat continues after node <id> (compaction #<seq>, lines <first>-<last>). See all nodes: python3 ~/.claude/hooks/recall.py map; one node: recall.py describe <id>.
```

**4. Direct injection.** The decisions list and the standing rules are injected into the context. The model needs no search to see them. A new subagent gets the active rules (4,000 characters at most) and the recall line in the same way.

**The limit.** No hook forces a search. The model chooses whether to run recall. It can skip a search when it is wrongly sure of an answer. The injected text only makes the search easy to find and tells the model when to use it. The continuity benchmark records a lookup trace per arm for type U probes, so you can see how often the model searches.

When recall and the decisions list disagree, the decisions list and the latest words of the user win.

## Verified facts

These numbers come from the code in this repository (hooks 1.21.0), not from a benchmark run.

| Fact | Where it is in the code |
|---|---|
| The nudge starts at 75% of the window and repeats every 25,000 tokens | `AT_PERCENT`, `EVERY` in `hooks/handoff-nudge.py` |
| The window comes from the environment, else from past compactions, else it is 200,000 | `env_window`, `learn_window`, `pick_window`, `DEFAULT_WINDOW` in `hooks/handoff-nudge.py` |
| A session with injects and no `compact_boundary` record sets `format_drift` | `format_drift_scan` in `hooks/lossless_handoff/reconcile.py` |
| For a 400k window the nudges come at 300k, 325k, 350k, and a final nudge at 355k | `highest_k`, `final_due`, `limit` in `hooks/handoff-nudge.py` |
| The real compaction point is the window minus 33,000 (367k), and the final nudge is 12,000 tokens before it | `CEILING_GAP`, `HEADROOM` |
| The handoff has seven headings | `HEADINGS` in `hooks/handoff-nudge.py` and `hooks/lossless_handoff/summary.py` |
| The fresh summary uses `sonnet` unless `HANDOFF_SUMMARY_MODEL` is set | `summary_model` in `hooks/lossless_handoff/summary.py` |
