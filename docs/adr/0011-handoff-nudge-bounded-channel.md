# 0011. Handoff nudge through a bounded model-facing channel

- Status: Accepted
- Date: 2026-10-15

## Context

Only the agent that did the work holds the full context. A deterministic fact dump taken at compaction time is not enough on its own.

## Decision

`hooks/handoff-nudge.py`, a PostToolUse hook, asks the working model to write an agent handoff file when context reaches 75% of the auto-compact window, and again every 25k tokens, plus one final note worded as imminent. The note is delivered as one `additionalContext` entry on the next tool result, so it costs no extra turn. The file has seven fixed headings: Goal, Parallel work running, Decisions and why, Done, In progress, Dead ends, Exact next step. The hook writes only a small meta file (counts and timestamps, no text). The PreCompact hook uses the agent file when it is fresh. Staleness detection covers the three volatile headings. A subagent event (non-empty `agent_id`) is ignored, so a subagent is never told to write the main session's file.

(The original decision implemented this as a mod of a separate plugin with six headings and a 50k step; this repository ships the hook form.)

## Alternatives

Not recorded.

## Consequences

- It adds a one-shot model-visible note (about 130 tokens per delivery, zero always loaded).
- The model may ignore the note; nothing blocks or compacts for it.
- A stale or missing agent file degrades the handoff ([0007](0007-handoff-degraded-vs-reduced.md)).

## References

- [`docs/spec.md`](../spec.md) F35, section 4 and F46 (f)
