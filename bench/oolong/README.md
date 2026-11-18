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
- The Read tool is not covered by the sandbox. Each unit has deny rules for the bench folder, the cache folder and `~/.claude/projects`. The other task folders have no rule of their own (see "E2BIG and constant-size rules").
- The gold answers stay in the cache and in `results/`. They never enter a task folder. The harness deletes each task folder when the unit ends.
- Proof: `run.sh selfcheck` makes a sibling folder with a token. The Read tool and Bash must both fail to read it. It also runs one real `curl` from inside a task session with no curl deny rule. The sandbox must stop it. It stores `evidence/sandbox-<time>.json` with the tool list of each arm.
  With arm C it also checks that the session lists the `llm-map` skill, that one `llm_map` run of 3 items succeeds from inside the sandbox, and that a curl to example.com in the same session still fails.
- Control N: each question runs once with no context file. A task that is right without context leaves the cleaned score (also by chance: a label question has a 1 in 6 chance).
- Transcript search: the text the model wrote and its tool inputs (not tool results, which print context lines) are searched for `trec_coarse`, "oolong", "ground truth", Hugging Face, download commands and URLs in commands. The report lists the hits per task. The plain word "TREC" is a recognition note, not a hit (see "Harness rules of 2026-11-18").
- A task with a control hit or any transcript hit leaves the cleaned score in all arms (so the pairs stay). The report prints raw and cleaned scores and the counts removed, under the new rule and the old rule.

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
python3 test_harness.py                                  # offline tests of harness.py and run.sh (no model call)
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
cost per task, wall time, turns, workflow calls, llm_map calls, compactions, timed-out rows, and the clean columns under the new and the old hit rule.

How cost is counted: the main session's `total_cost_usd` does not include the child `claude -p` calls of `llm_map`.
The harness counts `llm_map_calls` (Bash tool calls whose command holds `llm_map.py`) and reads `llm_map_cost_usd` from the summary line `llm_map: ... cost $x` in the tool results.
Cost per task in the report is the main cost plus `llm_map_cost_usd`. The column "llm_map calls (tasks with 1+)" gives the call count and the number of tasks that used it.
If a call has no readable summary line (for example a crash), the report says that the cost is a lower bound.
The budget guard (`--budget-usd`) still counts only the main session cost. Leave margin for arm C. It applies the decision rule only with 10 or more paired clean tasks at 128K.

## Harness rules of 2026-11-18

These four changes were made on 2026-11-18, after the decision run (20261118T132316) and the arm C slice (20261118T142600), and before the full eval.
Each change applies to all arms in the same way. The config record holds them (`harness_rules`, `env_bash_timeout_ms`, `timeout_s`).
`check_fairness` compares them across arms like any other setting.

1. Timeout. The unit time limit is now 1200 s (it was 900 s). Reason: in run 20261118T142600, unit 17000213-C wrote its final answer at 07:35:39,
   but a background task kept `claude -p` alive to the 900 s limit, so a correct answer scored 0 as an ERROR.
   At a timeout the harness still kills the whole process group. Then it reads the last assistant text of the session transcript.
   If the last line of that text is a labelled final answer (`Answer:`, `Label:`, `User:` or `Date:`), the harness scores it and sets `timed_out: true` (subtype `timeout_answered`).
   Any other timeout stays an ERROR with score 0 (also `timed_out: true`). The report shows a count of timed-out rows per arm, and how many of them were scored.
   A killed session has no result event, so its cost is the last `cost-state` record of the transcript, or 0 if none exists. That cost is a lower bound.
2. Turns, final text and cost. The turn count is the number of assistant messages (distinct message ids) over the whole session, not `num_turns` of the last result.
   A background job can wake the session for one more turn (unit 17000220-C showed "turns 1"). The final answer is the last assistant text of the whole session.
   Cost: the `total_cost_usd` of the result is the session total, not the cost of the last turn. For 17000220-C the result cost ($0.1040844) equals `totalCostUSD` in the transcript `cost-state` record,
   although a wake-up turn came after the first answer. So the harness does not sum several results. It takes the one result cost.
   Each row also keeps `num_turns_result` (the old figure) and `result_text` (the old text).
3. Bash time limit. Every arm's `claude -p` process gets `BASH_DEFAULT_TIMEOUT_MS=600000` and `BASH_MAX_TIMEOUT_MS=600000` (also in the `--settings` env).
   Claude Code 2.1.326 reads both names (found as strings in the installed binary).
   Reason: the default Bash limit is 120 s, and a long `llm_map` call needs more.
4. Contamination rule. The old pattern `\btrec(_coarse)?\b` also matched the plain word "TREC".
   Models write that word when they recognise the label set, also inside their own `llm_map --prompt` text. That is recognition, not a lookup of the data.
   New rule: hits are `trec_coarse`, "oolong", "ground truth", Hugging Face names, download commands, and URLs in commands. The plain word "TREC" is a recognition note.
   Recognition notes are counted and listed in the report. They do not remove a task from the cleaned score. The no-context control rule is unchanged.
   The report shows both columns: the clean score under the new rule and the clean score under the old rule, and the decision rule under both. Nothing is hidden.
   The report derives all of this again from the stored transcripts, so older runs can be shown under the new rules (a re-rendered report scores a timed-out unit from its transcript and counts turns again; cost stays as stored).

## E2BIG and constant-size rules (2026-11-18)

Run 20261118T160603 (6 lengths, 50 tasks, 4 arms, 1200 units) lost every Bash call in all 825 recorded units.
The text was: "Could not start /bin/sh: the command line plus environment exceed the OS exec argument limit (E2BIG). At spawn: command line 1.2MB across 3 args".
Cause: the settings file of each unit had a Read, Grep and Glob deny rule for every sibling task folder. Claude Code also copies Read deny rules into the sandbox profile.
So the size grew with the number of units (1.2 MB for 1200 units). Runs with 12 and 100 tasks stayed under the limit.

Fix. The size of the settings and of the sandbox profile does not depend on the number of units.
- No rule names a sibling folder. The unit settings hold only the same base rules for every unit (about 2.8 KB for A and B, 6.7 KB for C).
- Bash: the sandbox has `denyRead` for the work base, the bench folder, the cache and `~/.claude/projects`, and `allowRead` for the own folder. This is a fixed-size rule.
- Read tool: a deny rule cannot work here. Claude Code applies deny before allow, so `deny Read(<work base>/**)` would also block the own folder. So the harness does not allow a bare `Read`:
  `--allowedTools` is `Write Edit Bash Task`. The Read tool then opens only files in the unit folder. For any other path `claude -p` has nobody to ask, and the call is refused.
  Tested on Claude Code 2.1.326: with a bare `Read` allow rule a sibling file was readable; without it the call was refused and `permission_denials` listed it.
  This tool list is the same in all arms. Glob and Grep do not exist as tools in this version.
- Each unit folder has a random name (`lhoo-<run id>-<128 bit random>`), so a unit cannot guess a sibling path. The prefix `lhoo-<run id>-` still keeps `cleanup` and resume protection working.
- `run.sh selfcheck` proves it: a sibling folder holds a token. The Read call must be refused (it must show in `permission_denials`), and Bash must fail to read it.

Guards.
- Pre-flight: before the first unit, the harness builds the settings of the largest unit (longest folder path, every arm). If the project settings, the sandbox profile or the `--settings` argument is over 64 KB, the run does not start and the message names the arm and the size.
- Stop: if a transcript or a result holds "E2BIG" or "exceed the OS exec argument limit", the run stops at once. The unit is `failed`, the run status is `failed`, and `state.json` holds the message in `error`. No later unit starts.
- Run id: `harness.py run` prints a line `run_id: <id>`. `run.sh` reads that line for its report step (it does not use `results/.last`, which parallel runs overwrite). `run.sh resume <id>` uses the id you gave.
- Tests: `python3 test_harness.py` (size for a 1200-unit run under 64 KB, pre-flight failure, E2BIG stop, `run.sh` run id, Read allow list).

## Known limits

- 30 paired tasks see only a gap of 0.15 or more. The pilot (2 tasks) proves the pipeline. It measures no gap.
- Arm C runs without `--disable-slash-commands`, so it also lists the 19 built-in skills of Claude Code 2.1.326 (see `skills` in the evidence file). A and B list none. This is a small confound that cannot be removed without dropping the llm_map skill.
- `--max-budget-usd` is checked between turns. One call can pass its cap (a $0.40 cap once spent $0.49).
- Budget guard: with `--budget-usd X`, no new unit starts when the total reaches X minus the largest unit cost seen so far. Calls that already run can still overshoot, so keep a margin when you set X.
- The Claude Code version (2.1.326) differs from the LCM paper (v2.1.4).
- Folder names are random. A leftover folder of an earlier run is not blocked for the Read tool by a rule, but the Read tool needs the exact path, and only a path inside the unit folder is allowed. Run `cleanup` between runs.

## Evidence files

- `evidence/sandbox-20261118T122555.json` is the current sandbox proof (Workflow disallowed in all arms, subagent tool present).
- `evidence/sandbox-20261118T121859.json` is older. It was made when arm B still allowed the Workflow tool.
- `evidence/pilot-20261118-summary.md` is the pilot report (haiku, 32K, 2 tasks) from before the arm B change.
