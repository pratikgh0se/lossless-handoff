# 0001. 400k auto-compact window plus handoff, instead of the 1M window

- Status: Accepted
- Date: 2026-10-15

## Context

With the 1M default, Opus-class sessions auto-compact only near 967k tokens, so long sessions rarely compact on their own and pay for their whole context on every turn. The cost of a long context is three-fold: per-turn cache-read cost scales with context size, weekly-limit consumption grows with it, and answer quality may degrade at long context (the last point was not measured).

## Decision

Set `CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000` and pair it with a handoff. `hooks/handoff-nudge.py` nudges the working model at 75% of the window (300k), then every 25k tokens, to write its own handoff file before compaction happens. Claude Code itself compacts at about the window minus a ~33k buffer (~367k), so the nudge lands with margin. `install.sh` sets the variable unless it is already set (`--window N`, `--no-window`).

## Alternatives

- A 200k window was proposed earlier as one of a list of token savers.
- Staying on the 1M default is the implicit alternative. No explicit rejection text was recorded.

## Consequences

- Compaction happens earlier and more often, so handoff reliability becomes a hard dependency (see [0007](0007-handoff-degraded-vs-reduced.md) and [0011](0011-handoff-nudge-bounded-channel.md)).
- Context percentages shift when the window variable changes; compare token counts, not percentages.
- Windows below 40000 are ignored by the CLI.

## References

- [`docs/spec.md`](../spec.md) (nudge thresholds, section 2 and F35)
- Scope note: the original ADR also cited a token-budget spec of a private tooling repository; that part is omitted here.
