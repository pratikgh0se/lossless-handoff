# 0007. Handoff states: DEGRADED is loud, REDUCED is logged only

- Status: Accepted
- Date: 2026-10-16

## Context

Every handoff failure path (hook crash, stale agent file, summary timeout, missing latest file) used to end in a silent exit. A compaction whose handoff is missing or stale must never be silent, but alerting on every harmless fallback would train users to ignore alerts.

## Decision

The PreCompact hook computes a status. `degraded` means a section failed, a section is stale, or there is no fresh agent file and the fresh-summary fallback also failed: the injected text starts with a `HANDOFF DEGRADED` warning line to the model. `reduced` means the handoff was usable but partial (no fresh agent file, summary fallback worked, no section errors): it is written to the health log with status `reduced` and nothing else. Health lines go to `~/.local/state/claude-handoffs/health.jsonl`, and the opt-in nightly canary exercises the whole chain.

## Alternatives

An earlier ruling that an absent agent file is always degraded was replaced by this split.

## Consequences

- Users see only true degradation, not every fallback.
- Since hooks 1.10.0 a missed compaction is also caught by early write, recovery (`HANDOFF RECOVERED`), `--check` and the nightly reconcile; see [handoff-reliability](../handoff-reliability.md).

## References

- [`docs/spec.md`](../spec.md) section F36 (F36.A to F36.G)
- [`tools/handoff-canary/README.md`](../../tools/handoff-canary/README.md)
- Scope note: the original ADR was written for a larger tooling setup (toast, band chip, weekly report); only the handoff-relevant part is kept.
