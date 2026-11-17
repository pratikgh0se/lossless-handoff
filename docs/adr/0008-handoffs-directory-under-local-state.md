# 0008. Handoffs live in `~/.local/state/claude-handoffs`

- Status: Accepted
- Date: 2026-10-15

## Context

A live probe showed that Claude Code treats every path under its own config directory (`~/.claude/`) as a sensitive file. The model's writes there were denied even with an explicit allow rule.

## Decision

Handoff files are written to `~/.local/state/claude-handoffs/` (override: `HANDOFF_STATE_DIR`). A user-level allow rule for that path (`Edit(~/.local/state/claude-handoffs/**)`) makes it writable; `install.sh` adds it and records it so uninstall removes it.

## Alternatives

- Keeping the files under the config directory with an allow rule: denied by the harness.
- A repo-local `handoff/` directory: superseded for the agent handoff.

## Consequences

- The directory sits outside the config tree, so install creates it.
- Hooks, the nudge, the canary and the health log all share this one location.
- Uninstall keeps the directory and its data.

## References

- [`docs/spec.md`](../spec.md) section 2 (`HANDOFFS_DIR`)
