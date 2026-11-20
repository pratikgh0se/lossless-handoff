# Evidence and benchmarks

Measurements, proof checks and the two benchmarks. This page is part of the [docs index](README.md).

## In daily use (measured)

| What | Number |
|---|---|
| Compactions with a handoff | 217 over 32 days, across 23 sessions; the busiest session had 96 compactions |
| Handoff status | 83% ok, 15% reduced, 2% degraded |
| Nightly self-test | 34 runs: 31 passed, 2 failed and 1 was skipped; the last 7 nights all passed |
| Decisions lists | 12 projects, 495 active entries (84 standing rules), 111 finished |
| Search index | 48,282 chunks from about 30.6k messages across 1,242 sessions |
| Hook time | median 17 s (90th percentile 29 s); it always exits 0 and never blocks compaction |

The numbers are from the author's own machine, one heavy user. The author collected them on 2026-11-16.

## Benchmarks

Two benchmarks are in this repository. Both are code-graded: no model grades an answer.

**Continuity** ([bench/continuity/README.md](../bench/continuity/README.md)) measures one claim. After many compactions, the agent still holds the rules, decisions, exact strings, dead ends and subagent facts from early in the work.

- A seeded generator plants facts with random answers at the start of a long scripted session.
- Filler turns force compactions. Code counts the `compact_boundary` records.
- After 1, 3, 10 and 30 compactions, the runner forks the session and asks probes.
- Probe types: R rule, D decision, X decision that was replaced, S exact string, E dead end, C user contradiction, M order of three decisions made at different times. F is a fresh control. U is a fact that only a subagent saw.
- Arms: A plain Claude Code (no hooks), B Lossless Handoff, Z no history (amnesia control), O no compaction (oracle control).
- Code grades the answers. The report records a lookup trace per arm for type U probes.

**OOLONG** ([bench/oolong/README.md](../bench/oolong/README.md)) tests two things on large aggregation tasks: the `llm_map` tool (now the separate [llm-map](https://github.com/pratikgh0se/llm-map) project), and that the hooks do no harm. In this harness the context is a file. The report counts compactions of the main session only, and subagents can compact. If the count is 0, the hooks add only their session-start text to the main session. The test shows if plain Claude Code still loses on counting tasks, and if `llm_map` helps. Arm C is Lossless Handoff alone. The option `--llm-map-path` adds llm_map as an extra. The pilot of 2026-11-18 used arm C with llm_map.

One OOLONG run is published ([summary](../bench/oolong/evidence/run-20261118T221353-summary.md)). The harness flagged it for shared-temp use (2.2% of units); on every cleaned task set, Lossless Handoff with llm_map tied plain Claude Code. In that run the main session never compacted, so it does not test the handoff. No scored benchmark result for the handoff's main claim is published yet. The 2-task pilot files in `bench/oolong/evidence/` are harness evidence only. The handoff results come after the full evaluation: continuity at 30 compactions, and LoCoMo and OOLONG with forced compactions. LoCoMo is a published benchmark for memory over long conversations.

## Local proof

One command runs every test layer on your machine and writes a dated report:

```
tests/local-proof.sh
```

The report goes to `docs/proof/<date>-<sha>.md`. It starts with a one-line verdict and a table with one row for each layer. [docs/proof/](proof/) holds past reports. Cite them in release PRs.

| Layer | What it does |
|---|---|
| 1 Static | `bash -n`, `shellcheck`, `py_compile` |
| 2 Smoke | `tests/smoke.sh` |
| 3 Acceptance | the separate acceptance suite, repeated (`--repeat N`, default 3), with flaky tests rerun alone |
| 4 Fresh clone | clone the pushed commit, install, `--check`, uninstall twice, all in a temporary HOME |
| 5 Real end to end | 3 real headless Claude Code runs where a main agent and a subagent each compact; checked with `--prove` |
| 6 Real stress | 3 sessions at the same time, a hook killed at its timeout, a missing PreCompact hook |
| 7 Claims | `--claims-lint` (this page), for information only |

Layers 5 and 6 use real model calls. They cost about $3 in total (measured $2.96). The run stops at $4. Use `--no-real` for a free run, `--real-only` for only layers 5 and 6, and `--tests-dir DIR` for an existing tests checkout. The run never changes your real `~/.claude`, your real state folder or your launchd jobs. The report shows a check of them before and after.

## One-off measurements

The author measured these results on 2026-11-13/14, on the author's own projects. They are measurements, not promises. Models wrote and graded all tests. The tests use one user's projects.

| Test | Result |
|---|---|
| Lossless retrieval: 497 random messages from before a compaction [C1] | 497 of 497 found and returned byte-identical. The live C1 rate on 2026-11-18 is 10 of 11. |
| Meaning search, easy paraphrase test, 118 queries | 98% in the top 10, 89% first |
| Blind quiz after compaction, 33 questions | 41/66 with the v0.4 injection, 50/66 with the full decisions list, 65/66 with the decisions list plus recall |
| Handoff quality, 18 real compactions (score out of 100) | Claude Code's summary alone 62; plus the model's own handoff 68; plus a fresh Sonnet summary 75-76; given the real recent chat 85 |
| Decisions list, 96 questions about older decisions (out of 100) | 13.5 with the 0.2.0 handoff, 56 with the decisions list |
| Continuity, first 13 real compactions | 13 of 13 had a fresh model-written handoff, no degraded cases |

**Lossless retrieval.** The test sampled 497 messages at random (297 of yours and 200 of Claude's; fixed seed) from before a compaction, across 4 projects with up to 196 compactions each. `recall.py search` found them and `recall.py open --full` returned them byte-identical. Mean search time was 0.7 s, and the worst time was 3.1 s on a loaded machine. Limits: it is a sample, not every message. Messages that contain secrets are redacted by design. Subagent transcripts are excluded. A message over 40,000 characters needs `--from` paging. The author added that in 0.6.1 after the test found one 14,300-character message cut at the old 12,000-character output cap.

**Blind quiz.** There were 33 questions over 3 projects: 24 about old decisions and 9 about exact details found only in the chats. Sonnet answered from what a new chat receives. A separate blind grader scored each answer 0 to 2. The one miss with recall was recall that showed an older statement over a current rule. That miss led to the "decisions list wins" rule in 0.6.1. Limits: one run, and a model wrote the questions.

**Handoff quality.** A fresh Claude took a quiz on what it needed to carry on (latest request, next step, decisions, dead ends, rules, files). Version 0.2.0 added the fresh Sonnet summary. Treat the result as a strong hint, not proof.

**Decisions list.** By age: 1 compaction 21 to 66, 2 compactions 10 to 56, 3 or more 8 to 44. Recent memory did not change (62.5).

## Live checks (the proof run)

The tables above are one-off measurements. From 1.10.0, the install also keeps its own evidence. Plain code checks it (no model, no cost). These checks test the mechanism: a handoff exists, it is delivered and old messages come back. They do not measure answer quality.

**Receipts.** Every handoff that `postcompact-inject.py` injects appends a line to `~/.local/state/claude-handoffs/receipts.jsonl`. The line holds the transcript, the boundary time, hashes and the first 300 characters. Receipts start with the first compaction after install, so earlier compactions have none.

**Four checks for each receipt** (`precompact-handoff.py --prove`, verdicts in `proofs.jsonl`):

| Check | Passes when |
|---|---|
| `lossless` | 5 sampled messages from the transcript come back word for word through recall's own commands [C1] |
| `right_agent` | every quoted user line in the handoff occurs in the receipt's own transcript, so a subagent never gets the main session's context [C3] |
| `delivered` | the transcript after the boundary holds the injected text [C4] |
| `decisions_safe` | no decision id present before a decisions update is missing after it, and no superseded id came back [C5] |

**Nightly report.** With `--nightly`, the 04:15 job runs `--reconcile --prove --days 30`. It writes `proof-report.md` (summary line first, then failed checks and skip counts) and `reconcile.json`. When a run has failures, `--check` shows `proof failed: <n> checks` once.

**Claims register.** `hooks/claims.json` lists each public claim (`C1` to `C8`) with its evidence source, window, refresh time and minimum rate. Each `--prove` run evaluates it into `claims-status.json` and `claims-report.md`:

| Status | Meaning |
|---|---|
| `pass` | rate at or above the minimum, evidence present and fresh |
| `fail` | rate below the minimum |
| `stale` | evidence older than its refresh time |
| `no-data` | no evidence in the window (neither failing nor stale) |

`precompact-handoff.py --claims` prints the table. The claims on this page carry the markers `[C1]` to `[C8]`, which match the register (table at the end of this page). C1 to C6 are live: the hooks compute them from your own transcripts and receipts. C7 (blind quiz) and C8 (cost) are dated one-off studies with a recorded date and a refresh time. They are not live rates. With no file, the command lints `docs/evidence.md` of the repository tree. Before you publish a document that cites claims, run:

```
python3 hooks/precompact-handoff.py --claims-lint   # from a clone: lints docs/evidence.md
python3 ~/.claude/hooks/precompact-handoff.py --claims-lint path/to/file.md
```

The command exits 1 unless every marker is known and `pass`. An unknown, `fail`, `stale` or `no-data` marker blocks publishing. On a machine with no receipts yet, C1 to C6 are `no-data`, so lint is for the author's own machine. On the author's machine (nightly run of 2026-11-18), lint reports C1 (10/11), C2 (196/200) and C6 (9/86) as `fail`. C1: one lossless check failed for a subagent receipt of 2026-11-18 (recall found no hit for one sampled message). C2: the 30-day window holds 4 misses (3 from a deliberate probe run, 1 on 2026-10-23). C6: of 77 misses, 26 are subagent compactions from before hooks 1.10.0, 2 are from the probe run, and 49 are from OOLONG benchmark units of 2026-11-18. Benchmark units use their own settings and state folder, so the installed hooks record no handoff for them. C2 and C6 stay `fail` until these dates leave the window (about 2026-12-18, if no new benchmark run adds misses). The author does not exclude these misses, because hiding real data would make the register worthless.

**What the author's own data says.** In a 30-day reconcile on the author's machine (2026-11-17), 188 of 192 main compactions had a handoff [C2]. Of the 4 misses, 3 came from a deliberate probe run with user settings switched off. The other one was a hook that most likely stopped at its timeout on 2026-10-23. For subagents, 0 of 28 compactions had a handoff before this release. That gap is the bug that 1.10.0 fixes. A live subagent test after the fix gave right-agent 2/2 [C3], delivered 2/2 [C4], failed 0. A dated one-off blind quiz (2026-11-14, same sessions with and without the handoff) scored 98% with the handoff against 62% with Claude Code's compaction summary alone [C7]. This is one author, one machine, and small counts.

**Three counts.** The numbers 217, 188 of 192 and 196 of 200 are three different counts. 217 is all compactions with a handoff over 32 days (2026-11-16). 188 of 192 is the reconcile of 2026-11-17 and 196 of 200 is the nightly run of 2026-11-18, both over 30 days for main compactions. The two rates have the same 4 misses; only the windows and the totals differ.

## The claims register

Each claim has one marker. The README holds no markers. This page holds all of them. Status and numbers are in the `--claims` table.

| Marker | Claim | Kind |
|---|---|---|
| [C1] | Sampled messages from before a compaction are still retrievable word for word | live rate |
| [C2] | Every interactive compaction gets a handoff | live rate |
| [C3] | A subagent gets only its own context, never the main session's | live rate |
| [C4] | The handoff reaches the model after compaction | live rate |
| [C5] | Recorded project decisions are never lost by a decisions-list update | live rate |
| [C6] | Subagent compactions are covered too | live rate |
| [C7] | A blind quiz after compaction scored 98% (65 of 66) with the decisions list plus recall, against 62% (41 of 66) with the v0.4 injection | dated one-off (2026-11-14) |
| [C8] | About 19% lower cost at a 400k window than at 1M | dated one-off (2026-10-20) |

These results come from one machine and one heavy user. A quiz or a cost study is a hint, not a promise.
