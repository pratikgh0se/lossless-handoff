# The hooks in detail

The three main steps, missed compactions and subagents. This page is part of the [docs index](README.md).

## How it works

[docs/architecture.md](architecture.md) has the full picture. These are the three main steps.

1. **Nudge** (`handoff-nudge.py`, PostToolUse). When context reaches 75% of the auto-compact window, and again every 25,000 tokens, the hook appends one short nudge text to a tool result. The nudge asks the working model to write its own handoff to `~/.local/state/claude-handoffs/<session_id>-agent.md`. The file has seven fixed headings: Goal, Parallel work running, Decisions and why, Done, In progress, Dead ends, Exact next step. With a 400k window the reminders come at 300k, 325k and 350k. One final nudge, worded "Compaction is imminent", comes at 355k. That is about 12,000 tokens below the real compaction point (window minus 33,000), so the model still has room to write the file. Three sections are living sections: Parallel work running, In progress and Exact next step. If they are byte-identical to the previous reminder, the next nudge says that the handoff is stale. It also asks for a rewrite. It costs no extra turn.

**Which window the nudge uses.** The hook picks the window in this order. (1) `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, if it is a positive integer (source `env`). (2) Otherwise a learned value (source `learned`). The hook reads `compactMetadata.preTokens` of the newest `compact_boundary` records (20 at most) in the current transcript. If there are none, it reads the newest 5 other transcripts of the same project folder, read-only. The window is the median of these counts plus 33,000, rounded to the nearest 1,000. (3) If there is no history, the window is 200,000 (source `default`). The thresholds use the same formulas for every source. If the final threshold is at or below the first reminder threshold, the hook sends only the final nudge. The hook stores `window` and `windowSource` in `<sid>-agent.meta.json`. The next health row copies them as `window` and `window_source`. The hook keeps a learned or default value for 6 hours or until a compaction, so the scan of a large transcript (about 0.25 s for 500 MB) does not run on every tool call.
2. **Handoff** (`precompact-handoff.py`, PreCompact). Just before compaction, the hook writes `<session_id>-latest.md` (14,000 characters at most). The file starts with an authority line. The line says whom to believe when sources disagree. The file holds:
   - the model's own handoff, if there is one (labelled stale when it is out of date);
   - a fresh summary that Sonnet writes (`claude -p --model sonnet`) of the conversation since the last compaction, labelled "verify against facts". It runs in parallel with the decisions update, so a compaction takes about 20-40 s;
   - a facts section that the hook extracts from the transcript with no model: your last 8 messages, files touched, task state, git branch and status, last Bash commands, last assistant message.

   The hook always exits 0 and never blocks compaction. If anything fails, it still writes a minimal handoff.
3. **Inject** (`postcompact-inject.py`, SessionStart with matcher `compact`). After compaction, the hook feeds the handoff back into the new context. Claude Code limits the injected text of each hook to 10,000 characters. So the hook runs three times (`--part 1/2/3`). It splits on section boundaries and always names the full file path. Three more runs (`--ledger --part 1/2/3`) inject the decisions list. A stale agent handoff is still included, after a warning with the reason.

**Who wins when sources disagree:** the user's latest words, then the project decisions, then the agent handoff, then the fresh summary, then Claude Code's own summary, then the extracted facts. If the model is not sure, it should ask.

**Works without the model's cooperation.** If the model never writes the agent file, you still get the fresh Sonnet summary plus the facts (status `reduced`). If the summary also fails or is disabled, you get the facts only. The injected text then starts with a `HANDOFF DEGRADED` warning line.

## When a compaction is missed

A hook cannot force Claude Code to run it, so a handoff can be missed. For example, the hook stopped at its timeout, hooks were switched off for a run, or the install broke. Hooks 1.10.0 treat this as the case to design for. They write early, recover after, detect every miss, check the install, and keep evidence.

| Step | What happens |
|---|---|
| **Early write** | PreCompact writes `<sid>-pending.json` first. Then it writes a deterministic handoff (facts and your last messages) before the first model call. A hook that stops at its timeout still leaves a handoff. The health line removes the pending file. |
| **HANDOFF RECOVERED** | If a compaction still has no handoff, the SessionStart `compact` hook rebuilds one from the transcript (no model call, 6 s deadline). The injected text starts with `HANDOFF RECOVERED`. |
| **`--check`** | At session `startup` and `resume`, `postcompact-inject.py --check` prints one `HANDOFF CHECK:` line in these cases: a hook is not wired, a hook file is missing or changed since install (it compares the manifest), the versions of the two hooks differ, or reconcile found compactions without a handoff. It prints a separate `format drift` line when reconcile found format drift (see Limitations). It prints nothing when all is well. |
| **Nightly reconcile and proof** | With `--nightly` (opt-in), at 04:15, `precompact-handoff.py --reconcile --prove --days 30` counts every `compact_boundary` in your transcripts against `health.jsonl`. It proves each receipt (see [Proof, not promises](evidence.md#proof-not-promises)). It also compares two counts for each session: the recorded post-compaction injects and the `compact_boundary` records. A session with injects and no boundary sets `format_drift` in `reconcile.json`.  A default install has no nightly reconcile, proof or format-drift alarm. Install with `--nightly`, or run `precompact-handoff.py --reconcile --prove` yourself. |

Claude Code can also precompute a compaction. PreCompact runs when Claude Code builds the summary, and Claude Code applies the compaction later, sometimes hours later. The messages from that time on stay word for word after the summary. So the handoff from the first moment, plus the kept messages, cover everything. Hooks 1.10.1 handle this case. If the handoff is newer than the previous compaction boundary, they inject it with the line `Note: this compaction was prepared at <time>; ...` and do not run recovery.

Restart open Claude Code sessions after you install: hooks load when a session starts. The longer write-up is [docs/handoff-reliability.md](handoff-reliability.md).

## Subagents

When a subagent compacts, Claude Code fires the hooks with `agent_id` set, but with the `session_id` and `transcript_path` of the parent. Before 1.10.0, that looked like a main-session compaction and no subagent handoff was written. Now each compacting subagent gets its own handoff. The hooks build it from its own transcript (`<session>/subagents/agent-<agent_id>.jsonl` next to the parent transcript) and store it in `<sid>-a-<agent_id>` files.

- A subagent never reads or writes the handoff of the main session [C3]. The main session never reads or writes the handoff of a subagent.
- A subagent never merges the decisions list and never starts the indexer. The nudge hook (`handoff-nudge.py`) ignores subagent events (non-empty `agent_id`), so it never nudges a subagent.
- If the subagent transcript is missing, the health row says `degraded` / `agent_transcript_missing` and the hooks write no handoff.
- Reconcile counts subagent compactions separately, and the register tracks that they are covered [C6]. Before 1.10.0 they were not: 0 of 28 in the first 30-day reconcile of the author.

**Rules at subagent start (hooks 1.13.0).** A new subagent gets no project rules, and most subagents never compact. So the installer registers a `SubagentStart` hook (`postcompact-inject.py --subagent-start`, timeout 10). The hook gives the new subagent, and only the subagent, one block of context. The block holds the active `rule` entries of the project decisions list (newest first, 4,000 characters at most, cut at an entry boundary) and the recall line. The hook never gives decisions, tasks, the handoff of the main agent or the agent notes. If there is no decisions list or no rule, the hook gives only the recall line. `HANDOFF_SUBAGENT_RULES=0` turns the hook off. Each run adds a row with `event: "subagent_start"` to `health.jsonl`. `--prove` and `--reconcile` skip these rows. The hook takes about 50 ms with 200 rules. `SubagentStart` is a new hook event: restart open Claude Code sessions after you update. The installer prints a line about this only when it adds the entry. Since hooks 1.16.0 the rules start with one preamble line: these are the user's standing rules for the main session, apply only the rules that fit your task, and your task brief comes first.
