# OOLONG benchmark harness (Phase 4, P4-1)

## Purpose

This harness answers one question. Does plain Claude Code still lose on large aggregation tasks?
The answer decides if an `llm_map` tool is worth building. The rule is in `decision.json`.
The harness does not test the handoff hooks in a useful way below the compaction window. See "Arm C".

## What it runs

- Data: OOLONG-synth, data set `trec_coarse`, lengths 32K and 128K (the data set's own windows).
- `trec_coarse` is a validation task in this data set, not a test task. `fetch.py` reads the validation split.
- Pins: `dataset.lock.json` holds the Hugging Face revision and the OOLONG repo commit.
- Each task is one question on one context window. The context sits in `context.txt` in a new temp folder.
- Each task runs as one `claude -p` call with the real login. A temp HOME cannot log in.
- Project settings only (`--setting-sources project`). No MCP servers. No slash commands.

## Arms

| Arm | What it is |
|---|---|
| A | Plain Claude Code. The Workflow tool is disallowed. |
| B | Plain Claude Code. The task prompt adds one line: "You may use a workflow or parallel subagents if it helps." |
| C | Lossless Handoff hooks from `--lh-path`. Same task prompt as A. |
| N | No-context control. Same as A, but `context.txt` is not in the folder. |

Same in all arms: model, Claude Code version, allowed tools, permission mode, cost cap per call, time limit, window setting.
The harness writes a config record per task and arm (`config_records.json`).
`check_fairness` compares the records. If any setting other than the intended one differs, the run stops before the first call.

Arm C: at lengths under the compaction window, no compaction occurs. Then C equals A apart from the session-start text.
The report prints the compaction count of arm C. With a count of 0, C tests only that text.

Arm B: the report prints the workflow use rate. At 0 percent, B equals A in practice.
Arm A: the report flags any workflow call (it must be 0).

## Sandbox and contamination guards

- WebSearch and WebFetch are disallowed. Bash commands with curl, wget, pip, huggingface, git clone, urllib, requests are denied by permission rules.
- Claude Code sandbox is on for Bash: no network, `allowUnsandboxedCommands` false. Bash can read only its own folder (no sibling folders, no gold, no results, no transcripts).
- The Read tool is not covered by the sandbox. So each unit has deny rules for the other task folders of the run, the bench folder and the cache folder.
- The gold answers stay in the cache and in `results/`. They never enter a task folder. The harness deletes each task folder when the unit ends.
- Proof: `run.sh selfcheck` runs one real `curl` from inside a task session with no curl deny rule. The sandbox must stop it. It stores `evidence/sandbox-<time>.json` with the tool list of each arm.
- Control N: each question runs once with no context file. A task that is right without context leaves the cleaned score (also by chance: a label question has a 1 in 6 chance).
- Transcript search: the text the model wrote and its tool inputs (not tool results, which print context lines) are searched for the data set name, "oolong", "ground truth", Hugging Face, download commands and URLs in commands. The report lists the hits per task.
- A task with a control hit or any transcript hit leaves the cleaned score in all arms (so the pairs stay). The report prints raw and cleaned scores and the counts removed.

## Scoring

`scorer.py` is a port of the OOLONG rule: `src/eval/eval_helpers.py` (`synth_attempt_answer_parse`, `synth_process_response`), commit `0bb7eabe839218fee7fe8d007f41cfc2fd3ae24c`.
Exact match for labels, comparisons and user ids. `0.75 ** abs(gold - answer)` for numbers. Date equality for dates.
No model grader.
The prompt asks for the final answer on the last line with no period. The OOLONG parser takes the text after the last colon of the whole reply.

Self-test: `run.sh selftest` (24 fixed cases for all answer types). With `--data <cache>` it also checks every cached row (perfect answer scores 1, wrong answer scores under 1).

## Commands

```
./run.sh fetch --lens 32768,131072 --cache DIR         # pip install pyarrow first. Network. Harness side only.
./run.sh selftest [--data DIR]
./run.sh selfcheck --arms A,B,C                          # sandbox proof, a few cents
./run.sh run --model haiku --lengths 32K --tasks 2 --arms A,B --budget-usd 1.3
./run.sh resume <run-id> [--budget-usd X]
./run.sh report <run-id>
./run.sh cleanup [--all] [--dry-run]                    # only folders with the exact prefix lhoo-
```

Flags of `run`: `--model` (pilot haiku, slice sonnet, full opus), `--lengths`, `--tasks`, `--arms`, `--window`, `--lh-path`, `--cache`, `--work-base`,
`--call-cap-usd`, `--timeout-s`, `--jobs` (default 2), `--budget-usd` (pause with exit 3; resume later), `--answer-types`, `--seed`.
The run refuses to start if `decision.json` is not committed.
Set `OOLONG_CACHE` and `LHOO_WORK` to keep the gold and the task folders outside the repo.

Exit codes: 0 finished, 1 a unit failed or the fairness check failed, 2 usage error, 3 paused.

## Report

`results/<run-id>/summary.md`. Per length and arm: mean score (raw and cleaned) with a 95 percent bootstrap CI, paired difference B-A and C-A,
cost per task, wall time, turns, workflow calls, compactions. It applies the decision rule only with 10 or more paired clean tasks at 128K.

## Known limits

- 30 paired tasks see only a gap of 0.15 or more. The pilot (2 tasks) proves the pipeline. It measures no gap.
- `--max-budget-usd` is checked between turns. A call with workflow subagents can pass the cap.
- In `claude -p`, a workflow runs in the background. The main session can end before the workflow returns. Check the arm B transcripts before you trust arm B.
- The Claude Code version (2.1.326) differs from the LCM paper (v2.1.4).
- The Read tool of an earlier run's leftover folder is not blocked (only the sandboxed Bash is). Run `cleanup` between runs.
