# Architecture

This page shows every part of Lossless Handoff, the Claude Code event that calls each part, and the files each part reads and writes. It describes product 0.12.1 and hooks 1.14.1.

![Architecture of Lossless Handoff](architecture.svg)

## Summary

- Five Claude Code events call the hooks: `PostToolUse`, `PreCompact`, `SessionStart`, `SubagentStart` and `SessionEnd`. `SessionStart` has three matchers: `compact`, `startup` and `resume`.
- Two nightly jobs run outside Claude Code: the index job (03:30) and the reconcile-and-prove job (04:15).
- Claude Code keeps the chat transcripts in `~/.claude/projects/`. Lossless Handoff only reads them. It never writes to them.
- All other files go to the state folder, `~/.local/state/claude-handoffs/` (or `$HANDOFF_STATE_DIR`). See [ADR 0008](adr/0008-handoffs-directory-under-local-state.md).
- No hook forces the model to search. Read [How Claude decides to use recall](#how-claude-decides-to-use-recall).

## Parts, events, reads and writes

| Part | File | Claude Code event | Reads | Writes |
|---|---|---|---|---|
| Nudge | `hooks/handoff-nudge.py` | `PostToolUse` (matcher `*`, timeout 5) | The tail of the transcript, `<sid>-agent.md`, `<sid>-agent.meta.json` | `<sid>-agent.meta.json`. It adds one note to a tool result. The model writes `<sid>-agent.md`. |
| Handoff | `hooks/precompact-handoff.py` | `PreCompact` (timeout 150) | The transcript, `<sid>-agent.md`, the meta file, `decisions.md` | `<sid>-pending.json`, `<sid>-latest.md` and its timestamped copy, `health.jsonl`, `decisions.md` and its backups. It starts the indexer in the background. |
| Inject after compaction | `hooks/postcompact-inject.py --part 1/2/3` and `--ledger --part 1/2/3` | `SessionStart`, matcher `compact` (timeout 10) | `<sid>-latest.md`, `decisions.md`, `nodes.jsonl` | `receipts.jsonl`, `nodes.jsonl`, `nodes.state.json`. It prints the handoff and the decisions list as context. |
| Inject at start | `hooks/postcompact-inject.py --ledger --part 1/2/3`, and `--check` | `SessionStart`, matchers `startup` and `resume` (timeout 5 for `--check`) | `decisions.md`, the install manifest, `reconcile.json` | A once-only alert marker. It prints the decisions list, and one `HANDOFF CHECK:` line when something is wrong. |
| Subagent rules | `hooks/postcompact-inject.py --subagent-start` | `SubagentStart` (timeout 10) | `decisions.md` | One `subagent_start` row in `health.jsonl`. It prints the active rules and the recall line to the new subagent. |
| End-of-chat index | `hooks/recall.py session-end` | `SessionEnd` (timeout 5) | The transcript of the chat | The index in `recall/` |
| Nightly index | `hooks/recall.py index --all --max-seconds 3000` | launchd, 03:30 (macOS) | All transcripts of all projects | The index in `recall/` |
| Nightly reconcile and proof | `hooks/precompact-handoff.py --reconcile --prove --days 30` | launchd, 04:15 (macOS) | The transcripts, `health.jsonl`, `receipts.jsonl`, `hooks/claims.json` | `reconcile.json`, `proofs.jsonl`, `proof-report.md`, `claims-status.json`, `claims-report.md` |
| Recall | `hooks/recall.py search`, `open`, `map`, `describe`, `user`, `handoffs` | None. The model runs it through Bash. | The index, the transcripts, `nodes.jsonl`, the handoff files | Nothing. Search is read-only. |
| Summary nodes | `hooks/recall.py nodes`, and the `compact` inject hook | `SessionStart`, matcher `compact` | `compact_boundary` records in the transcript | `projects/<project>-<hash>/nodes.jsonl` |
| Decisions list | `projects/<project>-<hash>/decisions.md` | Updated by `PreCompact`. Injected by `SessionStart` and `SubagentStart`. | n/a | n/a |
| Subagent handoffs | The same hooks, with `agent_id` set | `PreCompact` and `SessionStart` in a subagent | `<session>/subagents/agent-<agent_id>.jsonl` | `<sid>-a-<agent_id>*` files |
| Recall subagent | `agents/recall.md` (installed to `~/.claude/agents/recall.md`) | None. The model starts it with the Agent tool. | It runs `recall.py` (tools `Bash` and `Read`, model `haiku`) | Nothing |
| `llm_map` | `hooks/llm_map.py` and `skills/llm-map/SKILL.md` | None. The model runs it through Bash, as the skill describes. | The input file that you name | The output file that you name |
| Canary (opt-in) | `tools/handoff-canary/` | launchd, 03:30, `--canary` install only | A temporary project | One `trigger: "canary"` row in `health.jsonl`, and `~/.local/state/handoff-canary/` |

Notes:

- The decisions update in `PreCompact` runs for a main session only. A subagent never merges the decisions list and never starts the indexer.
- `llm_map.py` is not a hook. No Claude Code event calls it. It runs `claude -p` with no tools, no hooks and no saved session, so the handoff hooks never run for it.
- The nightly jobs are macOS LaunchAgents. On Linux, `install.sh` prints the crontab lines.

## Data flow around a compaction

### Before the compaction

1. The model works. After each tool call, `handoff-nudge.py` reads the last token count from the transcript.
2. When the count reaches 75% of the window (300k for a 400k window), the hook adds one note to the tool result. The note asks the model to write `<sid>-agent.md` with seven headings.
3. The hook repeats the note every 25,000 tokens (325k and 350k). It adds a final note at 355k, worded "Compaction is imminent". The real compaction point is 367k (window minus 33,000).
4. If the three living sections are byte-identical to the previous note, the next note says that the handoff is stale.

### During the compaction

1. Claude Code runs the `PreCompact` hook. The hook writes `<sid>-pending.json`, then a deterministic handoff (facts and your last messages), before the first model call.
2. The hook then runs two `claude -p --model sonnet` calls in parallel: the fresh summary and the decisions update. A compaction takes about 20-40 s.
3. The hook writes `<sid>-latest.md` (14,000 characters at most) and one row in `health.jsonl`. It removes the pending file. It always exits 0.
4. The hook starts the indexer in the background.

### After the compaction

1. Claude Code runs the `SessionStart` hooks with the matcher `compact`. Each hook output is limited to 10,000 characters, so the handoff comes in three parts (`--part 1/2/3`) and the decisions list in three parts (`--ledger --part 1/2/3`).
2. If the handoff is missing, the hook rebuilds it from the transcript with no model call. The text starts with `HANDOFF RECOVERED`.
3. The hook adds the new summary nodes to `nodes.jsonl` and writes one line to `receipts.jsonl`. Part 1 names the newest node.
4. In a new or resumed chat (`startup` and `resume`), the hooks inject the decisions list and run the install check.
5. At 04:15, the reconcile job counts every `compact_boundary` against `health.jsonl` and proves each receipt.

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

**3. The summary node line.** After a compaction, part 1 holds one more line, right after the recall line. The line has this form (the hook fills the fields from `nodes.jsonl`):

```text
- summary node: this chat continues after node <id> (compaction #<seq>, lines <first>-<last>). See all nodes: python3 ~/.claude/hooks/recall.py map; one node: recall.py describe <id>.
```

**4. Direct injection.** The decisions list and the standing rules are injected into the context. The model needs no search to see them. A new subagent gets the active rules (4,000 characters at most) and the recall line in the same way.

**The limit.** No hook forces a search. The model chooses whether to run recall. It can skip a search when it is wrongly sure of an answer. The injected text only makes the search easy to find and tells the model when to use it. The continuity benchmark records a lookup trace per arm, so you can see how often the model searches.

When recall and the decisions list disagree, the decisions list and the latest words of the user win.

## Verified facts

These numbers come from the code in this repository (hooks 1.14.1), not from a benchmark run.

| Fact | Where it is in the code |
|---|---|
| The nudge starts at 75% of the window and repeats every 25,000 tokens | `AT_PERCENT`, `EVERY` in `hooks/handoff-nudge.py` |
| For a 400k window the notes come at 300k, 325k, 350k, and a final note at 355k | `highest_k`, `final_due`, `limit` in `hooks/handoff-nudge.py` |
| The real compaction point is the window minus 33,000 (367k), and the final note is 12,000 tokens before it | `CEILING_GAP`, `HEADROOM` |
| The handoff has seven headings | `HEADINGS` in `hooks/handoff-nudge.py` and `hooks/precompact-handoff.py` |
| The fresh summary uses `sonnet` unless `HANDOFF_SUMMARY_MODEL` is set | `summary_model` in `hooks/precompact-handoff.py` |
