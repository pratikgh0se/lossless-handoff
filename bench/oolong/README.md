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
| A | Plain Claude Code. The Workflow tool is disallowed. The subagent tool (named Task in this version) stays allowed. |
| B | Plain Claude Code. The task prompt adds one line: "You may split the work across parallel subagents (Agent tool) if it helps; wait for all their results before your final answer." |
| C | Lossless Handoff from `--lh-path` (alias `--lh`): the hooks, the recall agent and the `llm_map` tool with its skill. Same task prompt as A. |
| N | No-context control. Same as A, but `context.txt` is not in the folder. |

Same in all arms: model, Claude Code version, allowed tools, permission mode, cost cap per call, time limit, window setting.
The harness writes a config record per task and arm (`config_records.json`).
`check_fairness` compares the records. If any setting other than the intended one differs, the run stops before the first call.

Arm C and llm_map: the harness copies `skills/llm-map/SKILL.md` of the checkout into the task project (`.claude/skills/llm-map/SKILL.md`).
It replaces each `~/.claude/hooks/llm_map.py` in the file with the absolute path `<lh>/hooks/llm_map.py`. The session loads user skills never (`--setting-sources project`).
`--disable-slash-commands` also turns off skills. So arm C runs without that flag. A and B keep it. The config record names this difference in `llm_map_skill`.
If the checkout has no `skills/llm-map/SKILL.md` or no `hooks/llm_map.py`, arm C stops before the first call with a clear message.

Arm C: at lengths under the compaction window, no compaction occurs. Then C equals A apart from the session-start text.
The report prints the compaction count of arm C. With a count of 0, C tests only that text.

Arm B: the report prints the subagent use rate. At 0 percent, B equals A in practice.
The report counts Agent tool calls (the Task tool in this version) for every arm, and flags any Workflow call (it must be 0 in all arms).

Why the Workflow tool is not tested: the Workflow tool is disallowed in all arms. Under `claude -p` a workflow runs in the background.
The main session can end before the workflow returns. In the first pilot run, arm B called Workflow on task 15000201. The session ended with
"waiting for the per-question labels" and the answer was wrong. That call also cost $0.49 against a $0.40 cap. So a headless run cannot test workflows.
Arm B tests parallel subagents instead.

## Sandbox and contamination guards

- WebSearch and WebFetch are disallowed. Bash commands with curl, wget, pip, huggingface, git clone, urllib, requests are denied by permission rules.
- Arm C only: `llm_map` must reach the Anthropic API from Bash, and its child `claude -p` needs the real login. So arm C has one sandbox exception:
  `sandbox.excludedCommands = ["python3 <lh>/hooks/llm_map.py *"]`. Claude Code reads this list only from a trusted settings tier, not from the project file.
  So the harness sets it in the `--settings` JSON of the arm C call. `allowUnsandboxedCommands` stays false. Other commands, also in the same command line, stay in the sandbox.
  WebFetch and WebSearch stay off, and the curl and wget deny rules stay. No general network is open. The config record holds the list in `llm_map_sandbox_exception`.
  Claude Code exempts a call only when every command in it matches. So `llm_map` must be a Bash call of its own: a chain (`&&`, `;`, a pipe), a `cd`, a redirect or a command substitution keeps the call in the sandbox, and then it fails on the network.
  Known limit: the excluded command runs outside the sandbox, so its `--in` file is not limited by the sandbox read rules. The Bash deny rules for the gold and bench paths still apply to the command text.
- Claude Code sandbox is on for Bash: no network, `allowUnsandboxedCommands` false. Bash can read only its own folder (no sibling folders, no gold, no results, no transcripts).
- The Read tool is not covered by the sandbox. So each unit has deny rules for the other task folders of the run, the bench folder and the cache folder.
- The gold answers stay in the cache and in `results/`. They never enter a task folder. The harness deletes each task folder when the unit ends.
- Proof: `run.sh selfcheck` runs one real `curl` from inside a task session with no curl deny rule. The sandbox must stop it. It stores `evidence/sandbox-<time>.json` with the tool list of each arm.
  With arm C it also checks that the session lists the `llm-map` skill, that one `llm_map` run of 3 items succeeds from inside the sandbox, and that a curl to example.com in the same session still fails.
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

Flags of `run`: `--model` (pilot haiku, slice sonnet, full opus), `--lengths`, `--tasks`, `--arms`, `--window`, `--lh-path` (or `--lh`), `--cache`, `--work-base`,
`--call-cap-usd`, `--timeout-s`, `--jobs` (default 2), `--budget-usd` (pause with exit 3; resume later), `--answer-types`, `--seed`.
The run refuses to start if `decision.json` is not committed.
Set `OOLONG_CACHE` and `LHOO_WORK` to keep the gold and the task folders outside the repo.

Exit codes: 0 finished, 1 a unit failed or the fairness check failed, 2 usage error, 3 paused.

## Report

`results/<run-id>/summary.md`. Per length and arm: mean score (raw and cleaned) with a 95 percent bootstrap CI, paired difference B-A and C-A,
cost per task, wall time, turns, workflow calls, llm_map calls, compactions.

How cost is counted: the main session's `total_cost_usd` does not include the child `claude -p` calls of `llm_map`.
The harness counts `llm_map_calls` (Bash tool calls whose command holds `llm_map.py`) and reads `llm_map_cost_usd` from the summary line `llm_map: ... cost $x` in the tool results.
Cost per task in the report is the main cost plus `llm_map_cost_usd`. The column "llm_map calls (tasks with 1+)" gives the call count and the number of tasks that used it.
If a call has no readable summary line (for example a crash), the report says that the cost is a lower bound.
The budget guard (`--budget-usd`) still counts only the main session cost. Leave margin for arm C. It applies the decision rule only with 10 or more paired clean tasks at 128K.

## Known limits

- 30 paired tasks see only a gap of 0.15 or more. The pilot (2 tasks) proves the pipeline. It measures no gap.
- Arm C runs without `--disable-slash-commands`, so it also lists the 19 built-in skills of Claude Code 2.1.326 (see `skills` in the evidence file). A and B list none. This is a small confound that cannot be removed without dropping the llm_map skill.
- `--max-budget-usd` is checked between turns. One call can pass its cap (a $0.40 cap once spent $0.49).
- Budget guard: with `--budget-usd X`, no new unit starts when the total reaches X minus the largest unit cost seen so far. Calls that already run can still overshoot, so keep a margin when you set X.
- The Claude Code version (2.1.326) differs from the LCM paper (v2.1.4).
- The Read tool of an earlier run's leftover folder is not blocked (only the sandboxed Bash is). Run `cleanup` between runs.

## Evidence files

- `evidence/sandbox-20261118T122555.json` is the current sandbox proof (Workflow disallowed in all arms, subagent tool present).
- `evidence/sandbox-20261118T121859.json` is older. It was made when arm B still allowed the Workflow tool.
- `evidence/pilot-20261118-summary.md` is the pilot report (haiku, 32K, 2 tasks) from before the arm B change.
