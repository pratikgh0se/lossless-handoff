# OOLONG run 20261118T121906

- Model: haiku. Claude Code 2.1.326 (Claude Code). Window setting: 200000. Bench commit 931971d, decision.json commit 931971d, Lossless Handoff commit 931971d.
- Data: trec_coarse, split validation (trec_coarse is a validation task), Hugging Face revision 81d2f8d9349f6ff329d695f9be494051e9f05cb9, OOLONG repo commit 0bb7eabe839218fee7fe8d007f41cfc2fd3ae24c.
- Scorer: bench/oolong/scorer.py, a port of src/eval/eval_helpers.py of the OOLONG repo (commit above). Code only, no model grader.
- Config records: one per unit in config_records.json. The harness checked before the run that only the intended setting differs.
- The Claude Code version differs from the one in the LCM paper (v2.1.4), so these numbers are not a copy of the paper's run.

## Contamination guards

- Tasks: 2. Removed from the cleaned score: 0 (control right without context: 0; transcript hit: 0; both: 0).
- A control hit by chance also removes the task (a label question has about 1 in 6 chance). The rule is the same for all arms.

| Task | Length | Control (no context) score | Control parsed | Gold |
|---|---|---|---|---|
| 15000238 | 32K | 0.00 | question. | 100 |
| 15000201 | 32K | 0.00 | answer` format. | entity |

Transcript hits (search of the text the model wrote, and its tool inputs; patterns: data set name, oolong, ground truth, huggingface, download command, urls in commands):

- none

## Scores

| Length | Arm | n raw | Raw mean [95% CI] | n clean | Clean mean [95% CI] | Cost/task | Wall s/task | Turns/task | Workflow calls (tasks with 1+) | Subagent calls | Compactions | Errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 32K | A | 2 | 0.55 [0.10, 1.00] | 2 | 0.55 [0.10, 1.00] | $0.027 | 114 | 6.0 | 0 (0) | 0 | 0 | 0 |
| 32K | B | 2 | 0.28 [0.00, 0.56] | 2 | 0.28 [0.00, 0.56] | $0.013 | 38 | 5.0 | 0 (0) | 0 | 0 | 0 |

## Paired difference (same tasks, score of the arm minus score of A)

| Length | Pair | n raw | Raw diff [95% CI] | n clean | Clean diff [95% CI] |
|---|---|---|---|---|---|
| 32K | B-A | 2 | -0.27 [-1.00, 0.46] | 2 | -0.27 [-1.00, 0.46] |

## Notes

- Arm B workflow use rate: 0 of 2 tasks (0%). At 0% arm B equals arm A in practice.
- Wall time and cost are per task, one `claude -p` call. Cost is `total_cost_usd` (API-equivalent).
- Sample size: 30 paired tasks see only a gap of 0.15 or more (paired SD about 0.3, 95%% half-width about 0.11). A smaller gap is not a result.

## Decision rule (decision.json)

- Rule not applied: 0 paired clean tasks at 128K (10 or more needed). This run does not decide P4-1.
