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
| `--arms` | from A,B,Z,O,R (R needs `hooks/auto-recall.py` in `--lh-path`) | A,B,Z,O |
| `--conditions`, `--phrasings`, `--nc-window`, `--dry-run` | see "Lost-in-the-middle design" below | compact, cued |
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

## Lost-in-the-middle design (probe design 2, 2026-11-20)

This design adds four things. It does not change the old arms, the old probe text or the old scores. `test_litm.py` checks this with hashes of old scripts and old probe text, and with a stored run (`fixtures/legacy-run/`) that must give the same report, byte for byte.

### What is new

- **Position.** Each planted fact has a `position` tag: `start`, `middle` or `end`. The tag is the third of the nominal session timeline that holds the plant turn of the fact (`gen.position_of`). The nominal timeline is, for each epoch 0 to 4: the plant turns, then 6 filler turns (50 turns in total). With the v5 plan, each of the types R, D, X, S, E and C has 4 facts: 2 at start, 1 in the middle and 1 at the end. Type M has no single plant turn, so it has no position. The tag is computed from the script. It is not stored in the script, so no old script changes.
- **Two phrasings.** `cued` refers to the past. `neutral` is a task that needs the fact with no reference to the past, for example "Write the config line for X". Neutral exists for R, D, X, S and E. C and M ask about the past by their nature, so they have a cued probe only. Both phrasings use the same JSON form and the same code grader. A neutral prompt has no cue phrase of the auto-recall hook and no pointer to the session history. `test_litm.py` checks this for 20 seeds. In a run of design 2, the cued head also says "I told you these values earlier, so remember them." Reason: the hook must be able to fire on a cued probe. All arms get the same text.
- **Arm R.** Lossless Handoff with `--auto-recall`. The runner writes the project settings of the default install plus the `UserPromptSubmit` hook `auto-recall.py` (timeout 5 seconds). It does not run `install.sh`, so the real `~/.claude` stays untouched. `--lh-path` must hold `hooks/auto-recall.py`, or the run stops at once. After each probe fork, the runner reads `auto-recall.jsonl` of the arm (`HANDOFF_STATE_DIR` is the arm's own folder; the default state folder is read too) and keeps the lines with the session id of the fork. A line means the hook ran. The result row has `auto_recall_fired` and `auto_recall_hits`. The report says "the hook fired on X% of cued and Y% of neutral probes".
- **No-compaction condition.** For arms A, B and R, a second session has the same plant turns and the same number of filler turns as the nominal timeline, with compaction off (`DISABLE_AUTO_COMPACT=1`). The filler files are small (`nc_lines` lines, one file per turn), so the whole session fits in `--nc-window` (default 200000, under 60% by the arm O size model; the run stops above 75%). The probe comes once, after the last turn, and asks all epochs. `DISABLE_AUTO_COMPACT` stops auto compaction only. The binary also clears old tool results (microcompact, marker "[Old tool result content cleared]"). So a no-compaction session is valid only if all three hold: no compaction, no cleared marker in the transcript, and a last-turn context (input plus cache read tokens) of at least 75% of the expected size (`nc_estimate_tokens`). An invalid session stops before its probes, gets status `invalid`, and the report counts it with its reasons. Arms Z and O are controls with no condition, so each runs once.

The old filler plan is not the no-compaction plan. In a compaction run the filler is large (it forces compactions), so the positions are nominal tags. In the no-compaction run the positions are the real order of the turns.

### Pre-registered hypotheses

The thresholds are in `thresholds.json` (block `litm`, version 3). The author committed them before any data of this design. A hypothesis that fails stays in the report. Do not change a threshold after a run. All tests use the seed as the unit, the 95% bootstrap interval of the old design, and cued or neutral probes of the types R, D, X, S, E and C, as stated.

| ID | Hypothesis | Pass rule |
|---|---|---|
| H1 | Lost in the middle without compaction: arm A (no compaction, cued) loses more middle facts than start and end facts. | middle rate minus the mean of the start rate and the end rate at most -10 points, and the interval upper bound below 0. Ceiling gate: if that mean is above 95%, H1 prints "TOO EASY: no claim". |
| H2 (expected, not blind; mixes compaction with session length) | Compaction loses facts that long context keeps: arm A with compaction (largest N, cued) scores below arm A with no compaction. | difference at most -15 points, and the interval upper bound below 0 |
| H3 | Auto-recall helps when the prompt refers to the past: R beats B on cued probes (compaction, largest N). | R minus B at least +5 points, and the interval lower bound above 0 |
| H4 | Auto-recall does not help without a cue: R does not beat B on neutral probes (compaction, largest N). The honest expectation is no gain, because the hook does not fire. | "no gain of 5 points or more" if the interval upper bound is below +5 points. REFUTED if R minus B is at least +5 and the lower bound is above 0. INVALID if the hook fired on more than 5% of neutral probes. Else INCONCLUSIVE. |
| H5 (plumbing check) | The hook fires on cued probes and not on neutral probes. | cued fire rate at least 90%, neutral fire rate at most 5%. A neutral rate above 5% makes H4 INVALID. |

"Largest N" means `n_target` equal to the largest N of the run (N=10 in the presets). Compaction position rows mean "compactions survived", because their positions are nominal. The cap of the hook is 3 hits, and one probe asks up to 4 facts, so one fire cannot cover every fact. Table L0 shows the rate of fires with hits above 0 and the timeout rate. Table L2 is not a pure cue contrast: the neutral head also drops "Answer from this session's history only" and adds a null rule. A design-2 run prints `Probe design: 2` in its head. Its cued tables are not comparable with Wave 1 and Wave 2.

The old primary comparisons (B minus A at N=10, N=30 and type X) and the headroom gate stay as they are. The new section does not replace them. For H1, a Sonnet session of about 100k tokens may show no lost-in-the-middle effect. A result of NOT SUPPORTED is a valid result.

Power: each of these types has one middle fact for each seed, so 20 seeds give 120 cued middle probes (6 types) and 100 neutral middle probes (5 types) for each arm and condition, at the largest N. The report prints every count and every interval. Read the interval before you read the point.

## Probe design 3 (2026-11-22): real long context, non-literal probes, balanced positions, arm K

Design 3 is new. Designs 1 and 2, and the old tests, are unchanged. Run it with `litm3-smoke`, `litm3-pilot` and `litm3-full`. The owner wants the benchmark to test two things that are different: compaction loss, and lost in the middle in a long context with no compaction.

### What design 3 changes

- **A. Real long context.** The no-compaction session aims at about 300k tokens in a 400k window (`--nc-window` default 400000; `nc3_plan` gives the size, and the run stops above 90% of the window). The session is 75 turns of user text only: plant turns, and 36 filler turns. A filler turn pastes one document (about 362 lines) and asks one small task ("reply with the number at the start of the last line"). The task is not graded. The runner counts the loose hits in `filler_task_ok`, as information only. The validity rule stays: no compaction, no cleared marker, and a last-turn context of at least 75% of the estimate.
- **Why the filler differs between conditions.** In the compaction condition the filler is file reads, because many large tool results force the compactions. In the no-compaction condition a file read is a tool result. Claude Code clears old tool results (microcompact), so the context could shrink and the session would no longer be long. User turns are never cleared. So the no-compaction filler is pasted text in user turns. The two conditions have different filler and different fact sets, so Table L3 (compaction minus no compaction) compares different sessions. Read it with that in mind.
- **B. Non-literal probes.** Phrasing `nonliteral` exists for R, D, X, S and E, in the no-compaction condition. The question shares no content word with the plant sentence of the fact (NoLiMa, ICML 2025: the drop shows mainly when the question shares no words with the fact). Each fact has an alias, for example "the amber harbor project". A bridge turn in the same third says "In my notes, the amber harbor project is the one called `<topic>`". The plant sentence names the topic. The probe names only the alias. The self-test checks that the content-word overlap between each non-literal probe and its plant sentence is 0 (stopword list in `gen.STOPWORDS`, answer values removed), and that the probe holds no F67 cue.
- **C. Balanced positions.** Design 3 plants 6 facts of each type: 2 in each third of the session (start, middle, end). Each third has 25 turns, so the position tag is the real turn order. The report prints the power of H1: at 20 seeds, with a base accuracy of 80% and a seed spread of 10 points (assumptions), design 3 finds a -10 point effect with 57% power (design 2: 45%). The smallest effect with 80% power is 13.1 points (design 2: 15.3). 80% power for -10 points needs about 35 seeds (design 2: 48). Plan for this: a null H1 at 20 seeds is not strong evidence of no effect.
- **D. Arm K and search rate.** Arm K is Lossless Handoff with auto-recall, the F68 recall rules (marker file `.recall-rules` in a private copy of the hooks folder, so arms B and R stay as they are), the F69 recall MCP server (`--mcp-config`, tools `mcp__lossless-recall__search_earlier_chat` and `open_message` allowed) and the F70 reanchor hook (PostToolUse). `hooks/auto-recall.py`, `hooks/recall-mcp.py` and `hooks/reanchor.py` must be in `--lh-path`, or the run stops at once. In design 3, `used_lookup` is true if the fork made a recall.py call, called an `mcp__lossless-recall__` tool, or got an auto-recall injection. `search_kinds` lists which. Table L5 shows the search rate by arm, phrasing and position. In the design-3 no-compaction session the main turns use no tool, so the reanchor hook (PostToolUse) cannot fire there. Only the probe forks can use tools. (In the no-compaction condition the rule line depends on a hook change and on a live check; see Round 5.)
- **H1n, H6, H7** are registered in `thresholds.json` before any data. H1n: the H1 rule on non-literal probes. H6: K minus B search rate on neutral probes, no compaction: point above 0 and interval lower bound above 0. H7: K minus B accuracy on neutral probes, no compaction: at least +5 points and interval lower bound above 0. Stop rule: if the pilot shows no higher search rate for K, stop and remove the levers.
- **E. Framing.** In the no-compaction condition the core handoff never runs. So B minus A there is a null control (Table L6): it must be about 0. Only arms R and K can help in that condition. Do not credit the core handoff with a lost-in-the-middle gain. The core handoff is tested in the compaction condition.

### Cost of design 3

`bench/continuity/run.sh estimate` prints the three runs and every assumption. The long no-compaction unit comes from a token model (assumed list prices, not a Wave number), so plan for plus or minus 40%.

### The ladder: five steps, cheapest first

Do not move to the next step until the current step passes. Each step proves something that the next step costs more to find out. Every run (every preset) also has abort gates (below).

| Step | Command | Cost (estimate) | What it proves | Pass rule |
|---|---|---|---|---|
| 1. Offline self-tests | `python3 bench/continuity/test_litm.py` and `test_grader.py` | $0 | The logic: positions, probes, grading, validity rule, gates, checklist, regression of old runs | All tests pass |
| 2. Preflight | `run.sh preflight --lh-path <checkout with the levers>` | about $3.8 to $4.9 (target: about $5 at most) | The plumbing, on Haiku with a 30000 window: a compaction happens, the handoff is written and injected, the hooks and the MCP server work, the logs have the lines the report needs, the cost matches the estimate | The checklist prints PASS on every line. `run.sh check <run-id>` prints it again. The exit code is non-zero on a FAIL |
| 3. Smoke | `run.sh litm3-smoke --lh-path ...` | about $47 (see note) | The real model, at the real size, with all arms and both conditions, N=1 and 3: the no-compaction session reaches 300k tokens and stays valid, the model searches in arm K, the cost per unit | No invalid session, controls pass, cost ratio at most 1.3 |
| 4. Pilot | `run.sh litm3-pilot --lh-path ...` | about $194 | The size of the effects and the cost per unit on 2 dev seeds; the stop rule for the levers (H6) | H6 holds (K searches more than B), or stop and remove the levers |
| 5. Full | `run.sh litm3-full --lh-path ...` | about $5,600, or about $4,400 without arm R in the compaction condition (23 seeds, N up to 30) | The claims, on 20 report seeds | Run only when the pilot is clean and the owner says go |

Note on step 3: the owner expected $12 to $25. The estimate for 1 seed, arms A, B, R and K, both conditions, N=1 and 3, is about $47. The main no-compaction session of design 3 is about $6 once for each seed, and each arm adds about $2 of forks. For about $10 use `litm3-nc-smoke` (arms A and R, no compaction only). The preflight checks all other parts first.

What the preflight checklist tests (each line prints PASS or FAIL):

Round 4 added lines to this list (see the Round 4 section): the row count of every probe fork class; arms A, Z and O; the sha256 of the hook files; no foreign instructions record; no auto-memory file; the environment allow-list; the arm K MCP check turn and the rule line in every K fork; the deny-rule selfcheck; the Sonnet 300k turn (size, price, call cap). The preflight is now probe design 3 on Haiku and costs about $3.8 to $4.9, not $3.

- a compaction happened in each compaction session (`compact_boundary` count);
- a handoff receipt is in `receipts.jsonl` of the arm, and its text is in the transcript after the compaction;
- the decisions list or the rules were injected;
- no-compaction sessions: no compaction, no cleared marker, last-turn context at least 75% of the estimate (the preflight session is short, so its estimate is about 72k tokens);
- arm R: an auto-recall log line for every cued probe, matched on the fork id, and none for a neutral probe;
- arm K: the recall MCP server starts and lists its two tools (the checklist starts it for a second, no model call); whether an MCP call happened is recorded (it is not required); a reanchor health row exists if the session crossed the 50,000 token interval; the recall-rule line is in the injected text;
- `used_lookup` is set for every probe;
- every probe answer parsed and graded;
- the total cost is under $5, and the real cost is at most 1.3 times the `cost.py` estimate (the ratio is printed for each arm and condition).

### Abort gates (every preset)

After each finished seed the run stops with a clear message if any of these is true: the invalid-session rate is above 10%; the cost of the finished seeds is above 1.3 times their estimate; any cued probe of arm R or K has no auto-recall fire; the parse error rate is above 2% (arm Z excluded). The thresholds are in `gates.py`. The `slice` preset has no cost gate, because it has no estimate. The exit code is 4. The state file keeps every finished session, so `run.sh resume <run-id>` continues from the last finished seed. Use `resume --ignore-gates` only after you fix the cause.

### How to run the pilot

```
python3 bench/continuity/test_litm.py                          # offline self-test, no model call
bench/continuity/run.sh estimate                               # print the pilot cost and the full cost, no model call
bench/continuity/run.sh litm-smoke --lh-path <checkout with auto-recall>   # 1 dev seed, arms A and R, no compaction only, about $12. Run it first.
bench/continuity/run.sh litm-pilot --dry-run --lh-path <checkout with auto-recall>   # print the plan and the cost, no model call
bench/continuity/run.sh litm-pilot --lh-path <checkout with auto-recall> --budget-usd 200   # 2 dev seeds, arms A,B,R, both conditions, both phrasings, N=1,3,10
bench/continuity/run.sh litm-full  --lh-path <checkout with auto-recall>                    # 20 report seeds, arms A,B,R,Z,O. Run only after the pilot is clean.
```

Read the pilot before the full run:

- The hook (`hooks/auto-recall.py`) writes `auto-recall.jsonl` into `$HANDOFF_STATE_DIR` (the arm's own folder) and logs the session id of the fork. The harness reads only that file, never the real `~/.local/state/claude-handoffs`. If a cued probe of arm R leaves no line, the run stops with an error.
- Table L0: neutral fire rate must be at most 5% (H5). If not, the neutral probes are not neutral.
- Compactions in the no-compaction sessions must be 0. If not, `DISABLE_AUTO_COMPACT` did not work in this Claude Code version. Stop.
- Sessions of arm R must show `auto_recall_lines` above 0 in `sessions.jsonl`.
- Compare the real cost with the estimate below. Put the real numbers in `cost.py`.

### Cost estimate

`cost.py` prices a run from the real Sonnet runs of the full evaluation: about $38 for each seed of Wave 1 (arms A, B, Z, O, N up to 10) and about $92 for each seed of Wave 2 (arms A and B, N up to 30). Assumptions that were not measured: arms Z and O cost $4 for each seed together, probe forks are 30% of a unit, a no-compaction unit costs $5. Plan for plus or minus 30%.

| Run | Seeds | Arms | Estimate |
|---|---|---|---|
| Pilot | 2 | A, B, R, both conditions, both phrasings, N=1,3,10 | about $154 |
| Full | 20 | A, B, R, Z, O, both conditions, both phrasings, N=1,3,10 | about $1,633 |

Add N=30 with `cost.py --seeds 20 --arms A,B,R --n 1,3,10,30` (a printed estimate only; the full preset uses N up to 10).

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
| `test_litm.py` | self-test of position, neutral probes, arm R, no-compaction condition, cost, report, and the old-run regression |
| `litm.py` | report section: position, phrasing, condition, fire rate, hypotheses H1 to H5 |
| `cost.py` | cost estimate of the preflight, smoke, pilot and full run, and the H1 power |
| `gates.py` | abort gates after each seed |
| `preflight.py` | PASS/FAIL checklist of the preflight run |
| `fixtures/legacy-run/` | stored old-design run and its expected report (regression fixture) |
| `thresholds.json` | pre-registered pass thresholds |
| `seeds-dev.txt`, `seeds-report.txt` | seed lists |


## Round 4: fixes from the methods audit

An independent methods audit of the benchmark (verdict: fix first) found the faults below. Each fix has a self-test.

### One main session, forked with the settings of each arm

In the design-3 no-compaction condition, one main session runs for each seed. It runs as arm A, with no Lossless Handoff hooks. After it ends, arm A and then arms B, R and K fork it at the probe point. Each fork uses the settings of its arm: its hooks (a private copy), its environment, its MCP config and its allowed tools. The arms answer on one context, so the comparison is paired, and arms B, R and K do not pay for their own 300k-token session. This saves about $18 for each seed.

What each arm tests in this condition (the main session had no hooks, so no compaction handoff and no reanchor ran in it):

- A: plain Claude Code.
- B: the startup injection only (the SessionStart `resume` matcher runs at the fork: the decisions ledger and the recall line). It tests nothing from the compaction handoff. B minus A is the null control.
- R: B plus the auto-recall hook (UserPromptSubmit).
- K: R plus the MCP tool (F69). The recall rule line (F68) is part of K only if the preflight shows `fork_rule_line` true in the K forks. The levers hooks print nothing for source `resume` when no ledger exists (re-audit), so the rule line can come from another source. In the first real preflight run it came from the MCP server instructions of `lossless-recall` (an `mcp_instructions_delta` record), not from a hook. K forks the session of arm A, so the reanchor hook (F70) cannot fire (one turn, no earlier tool call). H6 and H7 test the label that the report prints from the data.

The runner records, for each fork, whether the startup injection and the rule line are in the fork transcript (`fork_injected`, `fork_rule_line`). The report prints the label of K from the data: if `fork_rule_line` is true in every K fork, K = R + recall rule line + MCP tool; otherwise K = R + MCP tool. Whether SessionStart hooks run on a `--resume --fork-session` call is unverified: the preflight shows it (an INFO line, not a FAIL).

### Non-literal forks, fork keys and row counts

The old fork key was the same for the cued and the non-literal phrasing, so every non-literal fork was skipped (finding 1). The key and the marker now hold the phrasing name. The preflight and the gates compare the rows with the expected forks (arm x phrasing x condition x type; exact for the design-3 no-compaction condition). A missing class FAILS the preflight and trips a gate.

The non-literal probe is a two-step word bridge, not a meaning match. The question shares no content word with the plant sentence. A bridge turn links the question words to the plant words through an alias ("the amber ledger"). The model must follow two word links. It is a weaker test than meaning search. Embedding search is off in the run (`RECALL_EMBED_URL` points to a closed port), so this benchmark cannot show a meaning-search gain.

### Hook isolation (F72)

Each arm with hooks (B, R, K) gets its own copy of the hooks of `--lh-path` in its state folder. The environment variable `HANDOFF_HOOKS_HOME` points the hooks to that copy (contract F72: with it set, a hook never reads `~/.claude/hooks`). Arm K also gets the F68 marker `.recall-rules` in its copy. The sha256 of every hook file of each arm goes to `sessions.jsonl` (`hook_sha256`). The preflight checks that the files still match. A live install during a run cannot change the arms. This needs the levers branch (F72) in `--lh-path`.

### Machine state

- The work base must be outside HOME. The runner refuses a work base below HOME, because Claude Code reads the `CLAUDE.md` files of parent folders (`--setting-sources project` does not stop it). The default is the system temp folder.
- The preflight FAILS if any `instructions` record in a transcript names a file from outside the project folder of the session. The record shape of Claude Code is not documented. The check reads the keys `path`, `filePath`, `file_path`, `source` and `file`, and a record with no path counts as foreign. If the first real run shows another shape, fix `gates.instruction_paths`.
- Auto-memory is off for every arm. Source: the installed binary (claude 2.1.326) holds the environment variable name `CLAUDE_CODE_DISABLE_AUTO_MEMORY` and the settings key `autoMemoryEnabled`. The runner sets both (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the environment of every call, `autoMemoryEnabled: false` in the project settings). I found the names by a byte search in the binary; the public docs were not read. A session is invalid if its project memory folder holds any file afterwards.
- `claude` gets an allow-listed environment, not the shell environment (which holds `CLAUDE_EFFORT`, `CLAUDE_CODE_CHILD_SESSION` and more). The list is `runner.ENV_ALLOW` and is written to `config.json` (`env_allowlist`). Each session record in `sessions.jsonl` has `env_keys` (names only, never values).

### Arm K tools

- The preflight runs one fork of arm K that must make one `mcp__lossless-recall__` call (it FAILS if none).
- Claude Code may defer MCP tools and load them through `ToolSearch` (the binary holds the variable `ENABLE_TOOL_SEARCH`). Arm K therefore allows `ToolSearch`. The MCP check turn proves that the call works.

### Gates

- A failed session now counts: more than 10% failed sessions trips a gate (checked after every session).
- One session that costs more than 2 times its own estimate (and more than $0.25 above it) trips a gate.
- Every preset has a default budget of 1.5 times its estimate; the run pauses there (exit code 3). `--budget-usd` overrides it.
- `resume --ignore-gates` needs `--i-know` and prints a warning.
- The full preset runs 23 seeds, so `min_pairs` 20 survives 3 lost seeds. It includes N=30.

### Preflight covers the gaps

The preflight now has: a design-3 mini session on Haiku (75 turns, about 15 forks for each of arms A, B, R and K, all phrasings); arms A, Z and O; the deny-rule selfcheck; and one Sonnet turn with a paste of about 300k tokens (sent through stdin, because the command line is too short for it). The Sonnet turn records the real cost, checks whether the price per token is above the standard price (the price above 200k tokens) and checks that the call stays under its own cap of $2.50 (`BIG_CAP_USD`). The paste is sized from the measured tokens per character (0.46) to land at 280k to 320k tokens. First real run: 467,684 tokens, $1.80, price ratio 1.06 (no long-context premium). Estimate after calibration: about $3.2.

### Controls, margins and the pilot stop rule (registered in thresholds.json v5, before any data)

- Design-3 controls (no compaction): arm O (all plant turns, no filler) must score at least 95% on every phrasing, so the probe is answerable at zero distance. Arm Z (no history) must score at most 2%, so the answer cannot be guessed. If not, do not read that phrasing (Table L7).
- Null control: B minus A (no compaction, cued) passes if it is at most 5 points and the interval holds 0. If not, the harness has an arm effect without the core handoff.
- H7 prints "TOO EASY: no claim" if the accuracy of arm B (no compaction, neutral) is above 95%.
- H5 is true by construction (plumbing check): the hook trigger depends only on the cue words of the probe. It is not a result about the model.
- Pilot stop rule, as counts. Run the full run only if all of these hold: each of arms A, B, R and K has at least 10 probe forks (2 seeds x 5 types) in each phrasing, no-compaction condition; the parse rate is at least 98%; the K search rate minus the B search rate (neutral, no compaction) has a point estimate above 0 (two seeds give no interval claim); the null control passes; the controls pass; the invalid-session rate is at most 10%.

### Smaller fixes

- The question order inside a probe is a seeded shuffle (design 3), so that the order is not the position.
- A retry is idempotent: if the transcript already holds the prompt, the prompt is not sent again and the recorded answer is used.
- A session whose temp folder is lost restarts from turn 0 with a higher `attempt`; the report, the gates and the preflight drop the rows of the older attempts.
- Estimates (assumed prices, plus or minus 40%): preflight about $3.8 to $4.9; smoke (A, B, R, K, both conditions, N=1,3) about $40; no-compaction smoke (`litm3-nc-smoke`) about $3; pilot about $180; full (23 seeds, N=1,3,10,30) about $5,450, or about $4,200 without arm R in the compaction condition (calibrated with the first preflight run, see the header of `cost.py`).


## Round 5: fixes from the re-audit

- Retry replay: a retry replays a recorded answer only if an assistant text record follows the prompt in the transcript. The replays are counted for each session (`replays` in `sessions.jsonl`). If the prompt is recorded and no answer follows it, the retry raises an error: an error on every turn can no longer look like a good session.
- A restarted session: the report (`report.py`, `litm.py`), the gates and the preflight all drop the rows of older attempts. If the arm A main session restarts, its fork views (B, R, K) run again with the new attempt.
- Updater and version: `DISABLE_AUTOUPDATER=1` is in the environment of every call. Each session row records `claude --version`. A gate trips if the version changes inside a run.
- Instruction records: the check reads `files[].path` (the real record shape). It reports project files and foreign files apart. The preflight FAILS on a foreign file. A project file is recorded (`project_instructions`) and expected to be none.
- Live recall.py: the injected text still names `python3 ~/.claude/hooks/recall.py` until the hook change (the recall command with the `HANDOFF_HOOKS_HOME` path) lands. Until then the runner records the sha256 of the live `~/.claude/hooks/recall.py` at run start (`live_recall_sha256` in `config.json`) next to the pinned copy. The preflight prints both as INFO and marks a change.
- K cache miss: MCP tools change the tool list, so the first K fork may write about 300k tokens of cache ($1.1 to $2.3). Forks of arm K have a per-call cap of $3 (`K_FORK_CAP_USD`); other calls keep $1.50. The preflight records the cost of the first fork of each view (`first_fork_cost_usd`).
- Claim for K (conditional): if `fork_rule_line` is true in every K no-compaction fork, K = R + recall rule line + MCP tool. Otherwise K = R + MCP tool. The report prints the right label from the data. The levers hooks print nothing for source `resume` when no ledger exists, so the preflight INFO line names the source (`fork_rule_source`: `mcp_instructions` or `hook`). In the first real run all 90 K forks had the line from the MCP server instructions, so K = R + recall rule line (MCP server instructions) + MCP tool.
- Presets: `litm3-full-v3` is the default full run (23 seeds; compaction: A and B to N=30, R and K to N=1,3,10; about $3,740). `litm3-full-v5` (A and B to N=30, K at N=10 only, no R; about $3,260) is the cheaper one. `litm3-full` stays V1 as designed (about $5,450). The no-compaction part is the same in all (about $326). With R and K at N=10 only, their compaction rows are not at the largest N, so the position tables use arms A and B for the compaction condition.

### Fixes from the first real preflight run (run 20261122T110446-preflight, Haiku, $2.92)

- Six probe rows were not parsed. All six came from one reply of arm O (no compaction, non-literal, type E): the model gave the right codes but set `"proceed": null`, because "the main field" was not clear for type E. This is a model format deviation, not a grader bug. The grader is unchanged (a pinned test uses the real reply). The probe text now names the main field of type E (`gen.E_NOTE`), the same for every arm. The preflight line now separates unreadable replies (FAIL) from format deviations (listed).
- The `instructions` record in the K transcript is an MCP server record (`attachment.type` = `mcp_instructions_delta`, keys `addedNames`, `addedBlocks`, `removedNames`), not a file. `gates.instruction_paths` skips it. Only a file path outside the project folder is a FAIL. A sanitized copy is in `fixtures/preflight-real-shapes.json`.
- `runner.OVERRIDE_ENV` lists every variable the runner sets (including `DISABLE_AUTOUPDATER`). The env check allows these and `ENV_ALLOW`.
- Non-literal oracle (arm O) answered 6 of 30 correctly in the first run. Cause, from the real transcripts: the bridge and the plant were both in the context of O and A, so the generator planted them correctly. The questions were ambiguous. Types D and X said "lane" for a queue, and both arms answered null. Type S said "the exact string that was stored", and arm O gave the topic name (arm A gave the value). Fix (design 3 only): every bridge turn holds a word list (`holding line` means a queue, and so on), and the questions use those words. A self-test checks 100 seeds and all types with a simple solver. The preflight has a new line: the non-literal oracle must reach 80% for every type (it names the type that fails). Whether Haiku can do the two-step bridge is still open until the next preflight.

### Parse policy (second preflight run, 20261122T115558)

- The 3 unreadable rows were arm Z (no history), type X, cued, compaction condition. The reply was `{"answers":[{"id":1,"current":"","replaced":[]}]}`: one entry for four items. Items 2 to 4 have no entry. This is a model reply with no data, not a grader bug.
- Policy: a reply that cannot be read counts as wrong for every arm. The preflight and the abort gate use one threshold, `gates.PARSE_ERROR_RATE_MAX` (2%), and both leave arm Z out of the rate (arm Z may answer with fewer entries). A row whose `correct` is not true or false is always a FAIL.
- The big Sonnet session now ends with status `done`, so the run line no longer lists it as not finished.


## Probe design 4: update chains with interference (no-compaction condition)

Why: the Sonnet smoke (run 20261122T123857-litm3-smoke) put every arm at 100% in the no-compaction condition (a ceiling). Design 3 plants single facts in filler of a different form, so nothing competes with the answer. Design 4 adds interference to distance. Designs 1 to 3 and the legacy fixture do not change (a test pins the scripts of designs 2 and 3). The compaction condition stays as in design 3. Code: `design4.py` (generator, op interpreter, grader, solvers, gates, pick rule), `litm4.py` (report section), `test_design4.py` (67 tests).

What it is: one item is a chain (one key, the attribute `queue` or `tag`, and statements of five ops: set, change, reject, undo, scoped). Levels L1 to L4 (k active values 2, 4, 6, 10; family size 1, 2, 3, 3; lures) are in one mixed session of 105 turns (3 thirds of 23 plant turns and 12 filler turns, 4 statements per turn, 39 chains, 276 statements). A chain is tight: all its statements are in one third, and the position is that third. Probes: one fork per level and phrasing (12 for each arm). Non-literal probes describe the key by common knowledge (no glossary turn). Code checks: zero content word and zero 5-character stem in common with the key, and unique descriptions.

Grading: M1 `current` exact; M2 M1 and `previous`; M3 M2 and `replaced` as an exact set. Abstain (null `current`) is wrong and counted apart. Error classes of a wrong `current`: stale (with lag), lure, sibling, other chain, abstain, novel (and unreadable).

Gates: O at least 95% for each level, phrasing and metric; Z at most 2%; the offline "last mention wins" solver at most 50% on M1 at L3 and L4, applied to the mean over the seeds of the run for each level, not for each seed (and a full-information solver must reach 100%); arm A above 95% is TOO EASY, below 30% is TOO HARD. Hypotheses H8 to H11 are registered in `thresholds.json` (litm.design4) before any data. H1 (the three phrasings pooled) uses the primary cell; H1c (cued), H1s (neutral) and H1n (non-literal) are secondary. H11 is a paired bootstrap interval: HARM if the lower bound is above 0. H6, H7 and the null control are kept.

Calibration: `run.sh litm4-calibrate` (arms A, O, Z, Sonnet, no compaction, dev seed 1; about $6.1 for one seed (A $5.0, O $0.9, Z $0.25), about $12 for two). `run.sh decide <run-id>` prints the pick rule: metrics M1, M2, M3, levels from L1 up, the first (metric, level) with arm A (phrasings pooled) in 55% to 80%, arm A in 45% to 90% for each phrasing, O at least 95%, and Z with rows and at most 2% (no Z rows is NO DATA, not a pass). M2 and M3 are allowed only if the alt rate is below 10%. The pick prints the abstain rate and the alt rate. If arm A is above 80% everywhere it says to add L5 and prints the command `bench/continuity/run.sh litm4-calibrate-l5`: L5 is its own run (levels L1 to L5, one session of 165 turns) and it replaces all cells of the L1 to L4 run; if still above 80%, Sonnet has no loss here. It also says if seed 2 is needed (a cell of A at 45% to 90%): then run `run.sh litm4-calibrate --seed-file bench/continuity/seeds-dev-2.txt`. Commit the pick in `thresholds.json` (litm.design4.pick) before the full run. Record the thinking setting.

Presets: `litm4-smoke`, `litm4-pilot`, `litm4-full-v3` (compaction as in design 3, no compaction is design 4). `run.sh preflight4` is the preflight with a design-4 mini session on Haiku; it adds lines for sound scripts, the solver gate, the oracle (80% on Haiku) and amnesia.

Estimates (plus or minus 40%; real smoke figures in `cost.py`: main session $3.055 for 75 turns, 15 A forks $0.671): calibration about $12 for 2 seeds (about $18 if the main session costs $8; the L5 run about $9), smoke about $41, pilot about $182 (dev seeds 3 and 4 in `seeds-pilot.txt`, not the calibration seeds 1 and 2), full V3 about $3,790. The compaction part of these figures is the design-3 model, which was above the real smoke cost.

Where the spec was open (the simplest reading): `previous` = the value in force just prior to the current one (after an undo, the undone value); `replaced` = every value that was in force and is not current; a family of size n is the base key plus n-1 suffixed keys, and the base member is described as "that is the ordinary everyday copy"; the attribute of a family is drawn by the seed and balanced over the thirds; `previous` and `replaced` are explained in the head of every phrasing, so M2 and M3 exist for all phrasings; undo is the last update op in about half of the L3 and L4 chains (drawn by the seed, balanced over the thirds), and a reject or scoped note is at a random place after the first set; the second calibration seed is a separate run with `seeds-dev-2.txt`; the nc target is 75% of the window (the 95% upper bound is registered as `nc_max_fraction`; the run fails above 90% as in design 3). Not done: the full report was not run end to end on a design-4 run folder (no run exists); the section is tested on synthetic rows.

### Fixes from the audit of PR #35 (the generator changed: `GEN_VERSION` litm4-2)

1. The attribute (`queue` or `tag`) of a family is drawn by the seed and balanced over the thirds at every level (`balanced_bits`). Before, L3 and L4 had a fixed attribute for each position in every seed.
2. The undo pattern (undo last or undo in the middle) is drawn by the seed the same way. Before, the start and end thirds had 2 undo-last chains and the middle third had 1, in every seed.
3. `interleave` gives each chain a random start in its third. A chain that must run in every remaining turn is forced; the rest is random. The position tag is the third of the turn of the last update (the deciding statement), checked in a test. Distribution of the deciding statements over 60 seeds (2,340 chains, 105 turns): in tenths of the session 5.9%, 5.9%, 13.5%, 10.7%, 6.7%, 8.2%, 17.1%, 5.7%, 7.6%, 18.7%; inside its third: early 16.5%, middle 25.4%, late 58.1%. Every tenth holds deciding statements, but they still lean late inside a third, because a chain of 14 statements needs 14 of the 23 turns. Before, every chain ended at 79% to 100% of its third.
4. The head of every probe states the rules in plain words: `previous` is the value that `current` took the place of; if the last update was an undo, it is the value that the undo took out; `replaced` lists every value that was in force at some time and is not `current`, and `previous` is one of them; a rejected proposal and a dry-run value were never in force. M2 and M3 are graded by these rules. For an undo-last chain the other reading of `previous` (the value before `current`) is also accepted for M2 and M3 and marked `alt_ok` in the row. The pick may use M2 or M3 only if the alt rate (the share of correct answers on undo-last chains that needed the other reading) is below 10%.
5. Size: the filler is sized from the measured rate. The smoke aimed at 299,860 tokens and reached 393,352 (362 lines in 36 filler turns), so a filler line is 26.6 tokens (the planted prose counts 0.46 tokens per character). `runner.nc3_estimate` reproduces the smoke within 1%. Design 4 now plans 250 lines and an estimate of 299,089 tokens (L5: 230 lines, 299,397); the target is 300k plus or minus 10%, and the run stops above 90% of the window. This also fixes design 3 (its lines drop from 362 to 264).
6. L5: the option `--levels L1,L2,L3,L4,L5` and the preset `litm4-calibrate-l5` run L5 as its own session of 165 turns; it replaces all cells. `decide` prints the command.
7. The pilot uses the seeds in `seeds-pilot.txt` (3 and 4), which the calibration did not use (1 and 2). The pick needs arm A in 45% to 90% in the picked cell for each phrasing, and prints the abstain rate. No Z rows is NO DATA.
8. H1 pools the three phrasings; the rows of each phrasing stay as secondary (H1c, H1s, H1n). H11 is a paired bootstrap interval (same draws, seed as the unit): HARM if the lower bound is above 0. The solver gate could never fail (a reject was always last). Design change: a reject or scoped note now goes to a random place after the first set, so the naive solver is right when a chain ends with an update (and not with an undo or a lure). Over 200 seeds it scores 11% to 56% at L3 and 0% to 56% at L4. The gate limit is 60%; a design without undo scores above 70% on average, and the gate flags such scripts (tested).
9. The lag of a stale value is the number of updates (set, change, undo) after the value was last put in force. An undone value has lag 1 (the undo itself), not 0.
10. K's gain (H6, H7) is an upper bound. Every statement has one fixed sentence form with the key in backticks, so a search by the key finds the statements almost like an oracle. Real notes do not look like this. The report says so under Table D4-4.
11. Cost: arm O is estimated at $0.90 (before $0.25). The per-session gate allows up to the larger of 2 x the estimate and the estimate plus $0.25, so a $0.90 session passes.

### Decisions after the re-audit (`GEN_VERSION` litm4-3)

1. Solver gate: the limit is 0.5 (code and `thresholds.json` agree; a test reads both). It applies to the mean over the seeds of the run, for each of L3 and L4. A single seed can reach 56% (5 of 9 chains without undo-last), so a per-seed limit of 50% failed legitimate seeds. Over 100 seeds the mean is L3 23%, L4 19% (L1 100%, L2 39%). A design without lures and without undo has mean 100% and fails (tested).
2. Late lean: a chain may put 2 statements in one turn (a burst; the same key is then twice in a turn), and every chain is early (its last update is by plant turn 11 of 23, the first half of its third) or late (from plant turn 12). The flag is drawn by the seed and balanced over the thirds. Last update in the first half of its third, over 100 seeds: L1 51%, L2 50%, L3 49%, L4 40% (target 35% to 65%). Mean place of the last update inside a third (0 = start, 1 = end), third 1 / 2 / 3: L1 0.44 / 0.43 / 0.44, L2 0.53 / 0.54 / 0.54, L3 0.63 / 0.65 / 0.62, L4 0.70 / 0.70 / 0.71, so the place is the same in the three thirds. If a third cannot be scheduled, the early chain with the most updates becomes late (this is why L4 is 40%, not 50%). Deciding statements by tenth of the session: 5.8, 11.6, 11.7, 7.1, 12.7, 6.5, 12.4, 7.3, 11.5, 13.2 percent. Table D4-5 of the report prints the first-half share for each level; if the primary level has under 10% there, the report says that H1 does not test the first part of a third.
3. Lures: in half of the L2 to L4 chains all lures (rejects and scoped notes) come after the last update (`lure_late`), balanced by the seed over the thirds (share 40% to 60% by third, by attribute and by level, tested); the other half keeps random places after the first set.
4. H11 OK reads "OK: no harm shown (N seeds)".
5. A reject stated just ahead of an undo: the head says that a rejected proposal and a dry-run value were never in force, also when one is stated just ahead of an undo, and that an undo takes out the last value that was in force. A test uses that shape (set, change, reject, undo): current is the first value, previous and replaced are the changed value.

### Neutral lexicon (`GEN_VERSION` litm4-4)

The preflight4 run 20261122T142851 stopped: a probe fork of session s1-A-nc (the 4th fork, the L2 cued probe; the main session had finished all 105 turns) returned `API Error: Haiku 5.5 can't help with this ... Details: [bio]`, a safety filter refusal (a false positive). The fork had written about 41,000 output tokens in 205 s when it stopped, and the retry did the same. The reply text is not kept, so the exact trigger is not proved. The design-4 text that is bio, lab or chemistry adjacent: the shared filler words of `gen.py` `cobalt`, `orchid`, `thistle` and `willow` (about 450 uses of each in the 300k-token context; also `walnut` and `meadow`, and the same words in the plant-turn prose), and in the lexicon `health-checker` (`live`), `cellular` phones and the suffix `canary`. Design 4 now has its own filler and prose words (`design4.FILLER_WORDS`: cobalt, orchid, thistle, willow, walnut, meadow become anchor, ribbon, button, ladder, pencil, window) and its own filler functions (`design4.filler_doc_prompt`; `gen.py` is unchanged, so designs 2 and 3 are pinned). Lexicon: `uptime-monitor` ("respond"), "pocket phones", suffix `pilot`. A test checks a deny list over the lexicon and over the plant turns, probes and filler of 100 seeds. A refusal like this is a session failure of class `safety_refusal` (`gates.failure_class`): it is in sessions.jsonl (`failure_class`) and in the failed-session gate message. The abort gate is unchanged.

### Probe output cap and failed calls (PR #36)

- Probe forks (and the arm Z probe) run with `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000`; the turns of a main session do not. The name is real in claude 2.1.326: it is in the installed binary (`/opt/homebrew/Caskroom/claude-code@latest/2.1.326/claude`) as a settings variable, and its value is read by `esr()`, which checks it against the model limit and returns the effective cap. It is in `runner.OVERRIDE_ENV` (the preflight env check allows it). Whether the cap stops a runaway reply is shown by the next preflight: the lines "probe fork output at most 8000 tokens" (FAIL above) and "median probe output tokens" (INFO).
- Every probe row has `output_tokens`.
- A failed call keeps `failed/<session>-<kind>-<attempt>-<time>.json` (reply redacted and cut at 4,000 characters, usage, class) and a copy of the transcript in the run folder.
- A `safety_refusal` is not retried (`runner.SafetyRefusal`); the session has `safety_refusals` in sessions.jsonl and the gate message counts them.


### Smaller probes and the runaway class (`GEN_VERSION` litm4-5, 2026-11-22)

Why: in preflight4 runs 1 and 2, the probe fork for L2 with 12 items thought for 8,000 tokens. Claude Code continued 4 times and then failed with "exceeded the 8000 output token maximum" (32,000 output tokens in total). The cap worked, but the probe was too large.

What changed:

- A probe fork asks at most 4 chains (`design4.MAX_ITEMS_PER_PROBE`). `design4.chunks` splits each level into chunks. The split is the same for every arm and phrasing, and it is set by the seed. Each chunk holds each third of the context within one chain of the others, so the positions stay balanced.
- Fork counts for each arm and seed: L1 3, L2 3, L3 3, L4 3 chunks, times 3 phrasings, so 36 forks (before: 12). With L5, 45 forks (before: 15). The fork key of a chunk ends with `:c<n>`. Result rows have `chunk` and `runaway`.
- A probe fork that hits the output cap is a probe outcome, not a session failure. All its items are wrong, with error class `runaway`. The runaway class is counted apart from abstain and unreadable. It does not count toward the failed-session gate. A main turn that hits the cap is still a failure.
- `gates.failure_class` maps "exceeded the N output token maximum" to `runaway`.
- Report: Table D4-6 gives the runaway forks for each arm, level and phrasing. Preflight: FAIL if the runaway rate is above 5% of the probe forks of an arm, and an INFO line with the counts for each level. Abort gate: a runaway rate above 10% of the probe forks of a seed.
- The 8,000 cap stays. Thinking is not limited.
- Estimates (offline, `python3 cost.py`): calibration about $16 for 2 seeds ($8.04 per seed), L5 run about $11, smoke about $46, pilot about $192, full V3 about $3,917.

### Refused probes as an outcome (`GEN_VERSION` litm4-6, 2026-11-22)

Why: in preflight4 run 3, the safety classifier stopped the hidden thinking of Haiku in one probe fork (non-literal, 4 items, plain neutral text with no bio words), and the retry in Claude Code was refused too. This happened in all 3 runs, at about 1 of 16 forks of arm A. It is a false positive on text that we cannot see. The real runs use Sonnet, and the calibration measures the Sonnet rate.

What changed:

- A `safety_refusal` in a probe fork or a Z call is a probe outcome, error class `refused`. It is not retried and it is not a session failure. The failed/ evidence is kept as before, and `safety_refusals` in the session summary still counts it. A main-session turn that is refused stays a failure.
- Result rows have `refused`. The items of a refused fork are left out of the accuracy denominator in every main table (D4-1 to D4-5) and in the pick rule.
- Report: Table D4-7 gives the refused forks (and items) for each arm, level and phrasing. Table D4-4s repeats every hypothesis with the refused items counted as wrong. The report says when the two readings disagree for a hypothesis.
- Pick rule output: the refused forks and items in the picked cell and in all cells. A cell with more than 5% refused forks cannot be picked, and a note says why.
- Gates: preflight FAIL if more than 10% of the probe forks of an arm are refused, plus an INFO line with the counts for each level. Paid-run abort: more than 5% of the probe forks in a seed are refused.
