# Continuity benchmark

This benchmark measures one claim. After many compactions, the agent still holds the rules, decisions, exact strings, dead ends and subagent facts from early in the work.
`SPEC.md` has the full design. This file tells you how to run the benchmark.

## What it measures

- A seeded generator plants items with random answers at the start of a long scripted session (epoch 0).
- Filler turns (file reads) force compactions. Code counts the `compact_boundary` records in the main transcript.
- After N = 1, 3, 10 and 30 compactions, the runner forks the session and asks probes. Code grades the answers. No model grades anything.
- Probe types: R rule, D decision, X decision that was replaced, S exact string, E dead end, C user contradiction, M order of three decisions made in different epochs. F is a fresh control (a gate, not a score). U (subagent-only fact) has its own row in the report, "needs subagent search (Phase 3)". U is not in the pooled score or the gates, because no arm can answer it until subagent transcripts are searchable.
- Arms: A plain Claude Code (no hooks). B Lossless Handoff. Z no history (amnesia control). O no compaction (oracle control).
- Output: one JSONL line for each probe item, and a table by arm, type and N with seed-level bootstrap intervals. The B minus A difference is paired by seed.

## Commands

```
bench/continuity/run.sh slice   [options]    # haiku, window 30000, dev seeds, N=1,3,10
bench/continuity/run.sh full    [options]    # sonnet, window 400000, report seeds, N=1,3,10,30
bench/continuity/run.sh resume  <run-id> [--budget-usd X] [--jobs N]
bench/continuity/run.sh report  <run-id> [--publish] [--draws N]
bench/continuity/run.sh aa      <run-id-1> <run-id-2> [--arm A|B]   # A/A noise floor
bench/continuity/run.sh selfcheck                           # live check of the transcript deny rules (a few cents)
bench/continuity/run.sh cleanup [--all] [--dry-run] [--work-base DIR]
python3 bench/continuity/test_grader.py      # grader self-test, no model, no cost
```

Options of `slice` and `full` (see `python3 bench/continuity/runner.py run slice --help`):

| Option | Meaning | Default |
|---|---|---|
| `--model` | model alias | slice haiku, full sonnet |
| `--window` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | slice 30000, full 400000 |
| `--n` | compaction counts | slice 1,3,10; full 1,3,10,30 |
| `--arms` | from A,B,Z,O | A,B,Z,O |
| `--types` | from R,D,X,S,E,U,C,F | all |
| `--seeds` | how many seeds to take from the list | slice 5, full 20 |
| `--seed-file` | seed list | `seeds-dev.txt` (slice), `seeds-report.txt` (full) |
| `--lh-path` | pinned checkout for arm B | this repo |
| `--jobs` | sessions in parallel | 2 |
| `--budget-usd` | pause cleanly when the total reaches this value | none |
| `--files-per-turn` | filler files per turn | from the window (2 at 30000) |
| `--o-window` | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` of arm O | 200000 |
| `--oracle-turns` | filler turns of arm O before its probes | 3 |
| `--o-files-per-turn` | filler files per turn of arm O | from `--o-window`, not from `--window` (3 at 200000) |

Arm O never compacts, so its size must fit its own window. The runner sets its filler size from `--o-window`, so that the estimate stays under 60% of that window. The size does not change with `--window`. Before the run, a pre-flight estimate (overhead, planted content, probe, filler) must stay under 75% of `--o-window`. If it does not, the run stops at once with "arm O would be too large". `config.json` records `o_files_per_turn`, `oracle_turns` and `o_estimate_tokens`.

A run with `--budget-usd X` stops new calls when the running total reaches X. Calls that already run finish, so the total can pass X by a few calls.
The state file keeps the session id and the next turn of each session. `resume` continues from there and repeats no finished work.
The pause exit code is 3. Exit code 1 means that a session failed or a control gate failed.

Smoke example (1 seed, arms A and B, N=1, 2 types):

```
bench/continuity/run.sh slice --seeds 1 --arms A,B --n 1 --types R,X
bench/continuity/run.sh cleanup
```

## Generator v5 change (2026-11-18)

- Each filler file has its own line count, 280 to 380 (mean 330). The count comes from the seed and the file name. The count is in no file name and no header. The model must read the file to know it.
- Why: on Opus, after some compactions, the model answered the filler question from memory with no Read call. The context stopped growing and the session became invalid ("no compaction in 10 filler turns").
- Guard: if a filler turn has no Read call on a `pad-*` file, the runner sends one retry turn ("Use the Read tool on each file; do not answer from memory.") before the quiet counter counts it. The rule is the same for every arm.
- `sessions.jsonl` has `filler_no_read_turns`, `filler_retries` and `filler_retry_no_read` for each session. `summary.md` shows the totals for each arm.
- Old runs (gen v4 and older) keep 330 lines. `run.sh regrade` and `report` still work on them (the new fields show 0).
- The facts are the same as v4. The RNG streams of v4 and v5 share one seed text (`gen.stream_version`). For a seed, v5 plants the same items, answers, probes and prompts as v4. Only the filler line counts differ. A test checks seeds 1000-1004 and N 1, 3, 10, 30. So the N curve 1, 3, 10 (v4) to 30 (v5) compares the same facts.

## Generator v4 in short

- S (exact string) and E (dead-end code) facts live in tool output only (a file that the agent reads). R, D, X, C are user-stated. Each item has 2 look-alike decoys.
- There are 4 items for each scored type, planted in epochs 0, 1, 2 and 4. The probe at N asks about items of epochs up to N-1, oldest first. The JSONL has `epoch` and `age`.
- Plant turns and filler turns have long user text (600 to 1,200 characters).
- The report has a headroom table (oracle minus arm A at N, 15 points minimum, from `thresholds.json`) and an accuracy-by-age table. It prints "BENCHMARK TOO EASY at this N: no claim either way" when the headroom is too small.
- Diagnostics: the report shows the seed spread (per-seed accuracy and min-max range for each arm). It shows the planted answers found in the last compaction summary (found/total, with a `SUMMARY COLLAPSE` flag). It shows a U lookup trace (forks that called recall, first 3 recall commands). The summary count is in `retention.jsonl`, and the fork tool calls are in the `lookup_calls` field of each row.
- S grader: an S answer is right if it equals the value, or ends with the exact value after the last ` = ` or `: `. All other types stay exact.
- The arm Z parse rate is information only. `SPEC.md` section 14 gives the reasons.
- A reply with an explicit `null` in the main field is an abstention. It counts as parsed and wrong. The report shows the abstain rate (INFO). `run.sh regrade <run-id>` grades stored replies again with no model call. If `retention.jsonl` is missing and the live transcripts exist in `~/.claude/projects`, it also builds the file from them. If the transcripts do not exist, the report prints "no data".

## Seeds and thresholds

- `seeds-dev.txt`: seeds for work on the harness. Never use them in a report.
- `seeds-report.txt`: seeds 1000 to 1019. Do not run them while the harness or the generator changes.
- `thresholds.json`: pass thresholds. They come from the verification plan. The author committed them before any run. Do not change them after a run.
- A generator change needs a new `GEN_VERSION` in `gen.py`. Do not mix versions in one table.

## Isolation

- The run uses the real HOME. A temp HOME gives "Not logged in".
- Each session has a fresh temp project folder `<temp>/lhbench-<run-id>-s<seed>-<arm>`. Claude Code reads project settings only (`--setting-sources project`). Settings from `--settings` set the window.
- The project settings of arm A have no hooks.
- The project settings of arm B register the Lossless Handoff hooks of the default install, from the pinned checkout (`--lh-path`). `HANDOFF_STATE_DIR` points to a temp folder, so the real handoff state stays untouched.
- The run does not touch the real `~/.claude` settings and hooks, `~/.local/state/claude-handoffs` or launchd. The runner hashes `~/.claude/settings.json` before and after the run. It warns if the hash changed (another chat can change it).
- The transcripts go to `~/.claude/projects/<temp-folder-slug>/`. The slug is the folder path with each non-alphanumeric character replaced by `-`. It starts with `-private-...-lhbench-`.
- `cleanup` deletes only `<temp>/lhbench-*` and `~/.claude/projects/<slug of temp>-lhbench-*`. The prefix is exact. The folders of a paused run stay, so that it can resume. Use `--all` to delete those too. Use `--dry-run` to see the list first.
- Transcript lookup is blocked in every arm. Each project has permission deny rules: `Read`, `Grep` and `Glob` of `~/.claude/projects/**` and of the bench state folders, and `Bash` commands whose text holds `.claude/projects`, `lhbench` or `-state`. The agent cannot read any transcript, its own or another run's, with a direct tool call. Run `run.sh selfcheck` before a scored run. It asks the agent to read a decoy file in its own transcript folder. It passes when the Read, Grep and Bash calls are denied and the secret is not in the reply.
- The deny rules match tool input text. They do not block a `python3` call that builds the path in code. The runner logs such a call as `used_lookup` when the input names `.claude/projects`, `.jsonl` or `recall.py`. The report shows accuracy with and without lookup. Read it before you make a claim.
- `recall.py` (arm B) reads the files inside Python, so the Bash deny does not block it. This is intended: it is the product feature. Its scope is the git root of the cwd. Each project folder is its own git root (`git init`), so recall cannot search other bench folders. The selfcheck confirms that recall does not find a secret in another bench folder.
- After each probe, the runner deletes the fork transcript, so a later recall cannot find an earlier probe answer.
- The tools of the agent: `Read`, `Grep`, `Task`, and `Bash` limited to `grep`, `cat`, `ls`, `head`, `tail`, `python3`. The agent can search its own transcript. The runner logs this as `used_lookup`.
- The runner deletes the U files (`led-*.txt`) after the subagent turn, so the key lives in the transcript only.

## Cost and time

- `claude -p` reports `total_cost_usd`. It is cumulative for a session, and a fork reports the total of its parent plus its own. The runner keeps deltas. `calls.jsonl` has the raw value and the delta of each call.
- The smoke run (haiku, 1 seed, arms A and B, N=1, types R and X) used 22 calls, cost $0.146 and took 2 minutes 7 seconds. It made 1 compaction in each arm. Plant turns cost most (the subagent turn is the largest).
- The estimates in `SPEC.md` section 7 are not measured. Run a slice first and read the real cost for each compaction.
- The report has a cost table: turns for each compaction, main-turn cost for each compaction and cost for each fork, for each arm. Use a pilot at the same model and window to predict the cost of a full run.
- Memory: each session is one short-lived `claude -p` process at a time. `--jobs 2` is the default for a laptop with many chats open.

## A/A noise floor

Run the same arm on the same seeds twice, with two run ids. Then run `run.sh aa <run-id-1> <run-id-2>`. It prints run 2 minus run 1 with the paired 95% CI for all N and for each N. The check passes when the CI holds 0. The width is the noise floor. Print it in each report. Example: run `run.sh slice --arms A --seeds 5` two times.

## Where results go

- Each run writes `results/<run-id>/` (gitignored): `config.json`, `state.json`, `results.jsonl`, `sessions.jsonl`, `retention.jsonl`, `calls.jsonl`, `scripts/`, `summary.md`, `summary.json`.
- To publish, run `run.sh report <run-id> --publish`. It writes `docs/proof/<date>-continuity-<sha>.md` and the matching `.jsonl`.
- The tokens are random and synthetic. The raw JSONL holds no chat content, so you can commit it. Transcripts and work folders never go in git.
- A README line can cite a result only with date, commit, model, window, Claude Code version and seed count.
- The benchmark stores and shows a result with no gain in the same way as a gain.
- Do not tune the hooks on report seeds. The author of the hooks does not choose the report seeds.

## Files

| File | Job |
|---|---|
| `run.sh` | the one command |
| `gen.py` | seeded generator, filler files, probe prompts |
| `runner.py` | runs sessions, forks, budget, resume, cleanup |
| `grade.py` | code grader |
| `report.py` | tables, bootstrap intervals, gates, verdicts |
| `test_grader.py` | grader and generator self-test |
| `thresholds.json` | pre-registered pass thresholds |
| `seeds-dev.txt`, `seeds-report.txt` | seed lists |
