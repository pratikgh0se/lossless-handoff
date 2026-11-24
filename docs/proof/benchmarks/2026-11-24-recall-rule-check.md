# Recall rule check: two small re-measures of 2026-11-24

This page reports two small re-measures of arm B (Lossless Handoff) after the recall rule. Neither one is a new proof. Neither one has a verdict. The first one is invalid for the rule. The second one is a small sample.

The raw files (`results.jsonl`, `summary.md`) stay on the machine that ran the checks (`bench/continuity/results/` is not committed). The base for comparison is the proof test: [2026-11-23-proof-test.md](2026-11-23-proof-test.md).

| Item | Recheck 0.19.0 | Check 0.19.1 |
|---|---|---|
| Run id | 20261124T094639-proof-recheck | 20261124T211329-proof-recheck |
| Date | 2026-11-24 | 2026-11-24 |
| Version tested | strict recall rule (0.19.0, on by default) | fix 0.19.1 |
| Seeds | 2001, 2002, 2003, 2004 | 2001, 2002 |
| Compactions (N) | 1, 3, 10 | 1, 3 |
| Cost | $70.57 | $13.38 |
| Status | invalid for the rule: Claude Code blocked searches | small sample, no verdict |
| Model, window, Claude Code | Sonnet, 400000 tokens, 2.1.326 | Sonnet, 400000 tokens, 2.1.326 |
| Command | `runner.py run proof-recheck --compare 20261123T190206-proof` | `runner.py run proof-recheck --seeds 2 --n 1,3 --compare 20261123T190206-proof` |

Both runs use arm B only and the types R D X S E. The proof test (run 20261123T190206-proof) is the base.

## Recheck 0.19.0 (4 seeds, invalid for the rule)

Items correct, by phrasing. Cued questions hold a hint. Neutral questions hold no hint.

| N | B cued | B cued (proof test) | B neutral | B neutral (proof test) |
|---|---|---|---|---|
| 1 | 20/20 (100%) | 20/20 (100%) | 20/20 (100%) | 20/20 (100%) |
| 3 | 54/60 (90%) | 60/60 (100%) | 58/60 (97%) | 56/60 (93%) |
| 10 | 72/80 (90%) | 80/80 (100%) | 76/80 (95%) | 76/80 (95%) |

Cued questions fell from 100% to 90% at 3 and at 10 compactions.

Root cause, found in review:

- `recall.py` run from its own folder searched 0 sessions. The output looked like "nothing found".
- Claude Code blocks `cd` outside the project in headless mode, so the agent could not move to the right folder.
- The rule had no retry step. The agent took a failed command as "not found".

So the run did not test the rule. It tested a rule that the agent could not follow.

## Check 0.19.1 (2 seeds, 1 and 3 compactions)

The fix: `recall.py` works from any folder and warns when it searches 0 sessions. The rule now says to copy the exact command and that a failed command is not "not found".

| N | B cued | B cued (proof test) | B neutral | B neutral (proof test) |
|---|---|---|---|---|
| 1 | 10/10 (100%) | 20/20 (100%) | 10/10 (100%) | 20/20 (100%) |
| 3 | 30/30 (100%) | 60/60 (100%) | 30/30 (100%) | 56/60 (93%) |

The proof test column holds 4 seeds. The check holds 2 seeds.

Same seeds only (2001 and 2002), 3 compactions, items correct:

| Version | Hint, 3 compactions | No hint, 3 compactions |
|---|---|---|
| Before the rule (proof test) | 30/30 | 26/30 |
| Strict rule 0.19.0 | 28/30 | 30/30 |
| Fix 0.19.1 | 30/30 | 30/30 |

In the check, the 54 probe forks had 1 Bash denial from our own deny rules and 3 blocks by Claude Code itself. The agent recovered in the end.

## Limits

- 2 seeds only (2001 and 2002). Probes at 1 and 3 compactions only. There is no data at 10 compactions for 0.19.1.
- The count of correct items is small (30 per cell). A difference of 2 items is within noise.
- There is no verdict and no pass rule for these checks. They show a direction. They do not prove that the fix works.
- The 0.19.0 recheck is invalid for the rule, because blocked searches stopped the agent from following it.
- Same model (Sonnet), same window, one Claude Code version (2.1.326). It is our own synthetic benchmark.
