# Continuity benchmark: design spec

Status: built (Phase 1 harness), 2026-11-18. The user approved the design and made the decisions in section 12.
Code: this folder. How to run it: `README.md`.

## 0. TL;DR

- **Claim to measure:** an agent stays aligned across many compactions in long work.
- **Method:** a seeded generator plants items with random answers in the first part of a long scripted session. Filler work forces compactions. At N = 1, 3, 10 and 30 compactions the runner asks probes in a fork of the session. Code grades the answers.
- **Arms:** A plain Claude Code. B Lossless Handoff. Z no history. O no compaction. Later arms test new features.
- **Output:** one JSONL line per probe item. A table of accuracy by arm, type and N, with seed-level bootstrap intervals. B minus A is paired by seed.
- **Command:** `bench/continuity/run.sh slice` or `full`.

```
 seed --> gen.py --> script (plant turns + filler + probe prompts)
                          |
         +----------------+----------------+
         v                                 v
  Arm A: temp project folder,       Arm B: temp project folder,
  no hooks                          Lossless Handoff hooks (pinned checkout)
         |  main session, forced window    |
         |  (epoch 0: plant; then filler)  |
         +-- count compact_boundary == N? -+
                  | yes
                  v
   fork the session (--resume SID --fork-session), one fork for each probe type
                  |
                  v
   grade.py (code only) --> results.jsonl --> report.py --> docs/proof/
```

## 1. Goal and non-goals

Goal: after N compactions, does the agent still hold the rules, decisions, exact strings, dead ends and subagent facts from early in the work?

Non-goals:

- It does not replace OOLONG. OOLONG has no history to carry, so it cannot test the handoff.
- It does not test the hooks for safety. The acceptance tests and `tests/local-proof.sh` do that.
- It never uses a model as grader.

## 2. Arms

All arms use the same Claude Code version, model, window, tools, permission mode and per-call budget cap. `config.json` of each run pins these values.

| Arm | Name | What is different |
|---|---|---|
| A | plain | Project settings have no hooks. |
| B | Lossless Handoff | Project settings register the hooks of the default install (PreCompact, SessionStart compact/startup/resume, SessionEnd, PostToolUse nudge) from a pinned checkout (`--lh-path`). `HANDOFF_STATE_DIR` is a temp folder. `HANDOFF_SUMMARY_MODEL` is the run model. |
| Z | no history | A new session for each probe type. It gets the probe only. It sets the guess rate. |
| O | no compaction | The same script with a large window (200000). It stops after a few filler turns (default 6), so no compaction occurs. It sets the ceiling for each probe. |

Rules:

- Arms A and B get the same script for a seed.
- Direct reads of transcripts are denied in every arm (permission deny rules, section 8). Arm B keeps `recall.py`, scoped to the run's own folder. The runner logs `used_lookup` for each probe. The report shows the lookup rate and the accuracy with and without lookup, for each arm.
- Arms run in the same batch, in seed order, so a change in the model or the service hits all arms the same way.
- Later arms (one new feature on or off each time) use the same seeds and the same install with one switch.

## 3. Generated sessions

### 3.1 Generator

`gen.py --seed S` writes the script. The same seed and `GEN_VERSION` always give the same script (`random.Random` with a string seed, no other source of random). The model output is not seeded, so use many seeds, not repeats.

- Each answer is a random token. Forms: `KQ7Z-4821-MRDV` (base32 without `0 O 1 I`), a path `/srv/zk9m2q/lane-3171/out`, a command `fx-sync --lane 3171 --tag PWQ4K2`, an env line, an error code `ERR-4471-XQ7K3M`. Each answer has 40 bits or more. A model cannot guess it.
- Topics are labels like `export-3391`. Real tool names are never used (a model guesses those from habit).
- Noise items of the same form (near duplicates with another number and value) are planted in a noise turn and every 6th filler turn.
- No answer token occurs in a filler file. The runner checks every file before it writes it and stops if it finds one.
- `test_grader.py` tests that the same seed gives the same script, that answers are unique, and that probe prompts hold no answer.

### 3.2 Probe types

K = 3 instances of each type for each seed. One fork asks the 3 instances of one type as numbered items. Probe text is neutral: it holds the question and a JSON form. It never holds an answer.

| Code | Type | Planted (epoch 0) | Right answer (code check) |
|---|---|---|---|
| R | standing rule | "every commit message in repo X ends with `TAG-<tok>`" | `answer` equals `<tok>` (or `TAG-<tok>`) |
| D | decision | "the X queue is `<tok>`" | `answer` equals `<tok>` |
| X | decision, later replaced | decision `<old>`, 3 turns later "use `<new>` instead" | `current` equals `<new>`, `replaced` holds `<old>`, `current` is not `<old>` |
| S | exact string | a deploy command, an output path or an env line | `answer` equals the string after trim (case kept), or ends with the exact value after the last ` = ` or `: ` (a label form; a longer text after the label is wrong) |
| E | dead end | "we tried approach P. It failed with code `<ecode>`. Do not try it again." | `proceed` is false and `reason_code` equals `<ecode>` |
| U | subagent-only fact | a subagent reads a file with `ledger-key: <tok>` and replies with the line count only. The runner deletes the file. | `answer` equals `<tok>` |
| C | user contradiction | a decision as in D. The probe states a different value. | `conflict` is true and `earlier` equals the planted value |
| M | order of decisions | three "Ordered decision <id>" statements in three epochs: the first in epoch 0, the second after compaction 1, the third after compaction 2 (planted by the runner, so each lands in its own compaction segment) | `order` equals the three ids, earliest first, exact order, no missing or extra id |
| F | fresh control | planted right before each probe set (a gate, not a score) | `answer` equals the value |

Notes:

- Type U is outside the score and the gates. The key lives in a subagent transcript only. The main agent sees the line count only (checked: the key is in no main transcript). So no arm can answer U today, not even arm O. Probe replies say "not in this session's history". The report shows U in its own row, "U needs subagent search (Phase 3)", and leaves it out of the pooled score, the oracle gate, the amnesia gate and the gain verdicts. Phase 3 adds subagent-transcript search. Then U gets a gate again.
- Type E probe text asks for the exact error code text. Without that sentence the model gave a label such as `dead_end_do_not_retry` instead of the code (slice 1: E scored 0/6 in every arm, even arm O). The report counts main-agent reads of the U files and subagent files, to show that the plant went through a subagent.
- Type M is one item (a permutation of 3). The ids are random tokens, so an arm with no history cannot guess them. A and B probe M at N=3 or more only, because three epochs need two compactions before the third decision. Arm O plants all three on consecutive turns. M is for the Phase 2 summary-node feature. Its decisions are planted after epoch 0, so M does not follow the "all items in epoch 0" rule of the other types.
- Type C needs the flag and the earlier value. A flag with no earlier value is wrong.

### 3.3 Timeline

- **Epoch 0** (before compaction 1): 9 plant turns (R, D, S, X old, E, noise, U, X replace, C). Each plant turn holds the K items of one type. All planted items are in epoch 0, so the item age at checkpoint N is N compactions.
- **Filler:** each turn reads `files-per-turn` fresh files (about 20 KB, about 6k tokens each) and replies with line counts. Every file is new, because Claude Code may skip a repeated read of an unchanged file. Default: 2 files at a 30000 window. About 12 files at 400000.
- **Fresh control F:** right after the N-th compaction, one plant turn sets 3 scratch labels in the main session. The probe asks for them. F accuracy must be 95% or more in every arm.

## 4. Forcing, counting and probing

### 4.1 Forcing

`CLAUDE_CODE_AUTO_COMPACT_WINDOW` goes through `--settings '{"env":{...}}'`, because user settings beat the process env and a temp HOME does not work. Slices use 30000. The full eval uses 400000.

### 4.2 Counting

- N = the number of `compact_boundary` records in the main transcript. The runner counts them after every turn.
- If one turn adds two boundaries, that checkpoint is missed. The report shows the missed rate. Reduce `--files-per-turn` if it is above 5%.
- If 10 filler turns in a row add no boundary, the session is marked invalid.
- The runner also records `precomputed` boundaries (`compactMetadata.precomputed`) and subagent compactions (counted apart, not part of N).

### 4.3 Probing without a leak: `--fork-session`

- `claude -p --resume <SID> --fork-session` works in `-p` mode (Claude Code 2.1.326). It makes a new session id with the history up to now. The parent session file does not change.
- The runner forks once for each probe type. A probe never sees another probe.
- Leak check: every probe prompt has a marker `PROBE-<seed>-<arm>-<N>-<type>`. After the forks, the runner counts the marker in the main transcript. The count must be 0 (gate "Probe leaks").
- The planted items are in user turns only. They never go in a file in the project folder (the U files are deleted).
- A fork can run SessionStart hooks in arm B. A real user does the same on resume. The size of this effect is not measured in Phase 1 (open item, section 11).

## 5. Grading and output

### 5.1 Grader (`grade.py`)

- Take the first JSON object in the text that holds an `answers` list. If none, use a fixed fallback regex for the bare-token types (R, D, U, F) and set `parse_ok` false.
- Compare the text after trim. Case counts. Allow one set of surrounding backticks or quotes. Booleans may be `true`/`false` text.
- No partial credit. Each item is right or wrong.
- The grader never calls a model. `test_grader.py` covers every type, right and wrong answers, and the conflict-flag check.

### 5.2 Result file

`results/<run-id>/results.jsonl`: one line for each probe item with `seed`, `arm`, `n_target`, `n_actual`, `precomputed_boundaries`, `type`, `item`, `fork_sid`, `raw`, `parsed`, `parse_ok`, `correct`, `used_lookup`, `lookup_calls`, `tokens`, `fork_cost_usd`, `claude_version`, `model`, `window`, `commit`, `turn_index`.
Other files: `config.json` (all settings, versions, commits), `state.json` (running total and session progress), `sessions.jsonl` (one line for each finished session), `calls.jsonl` (every call with cost), `retention.jsonl` (one line for each A, B or O session: summary retention), `scripts/`.
`lookup_calls` is the list of tool calls of the fork after the probe message (`tool`, and `input` cut to 300 characters). The runner keeps it because it deletes the fork transcript after grading.
`retention.jsonl` has `found` (planted answers in each `isCompactSummary` record of the main transcript, in order), `last`, `total`, `by_type_last` and `collapse`. The runner writes it when a session ends, before cleanup. The planted answers are the single values of types R, D, S, X (new value), C (earlier token) and U.

### 5.3 Report (`report.py`)

- Table 1: accuracy by arm and N, all scored types pooled. Table 2: by type, one block for each N. Table 3: gates. Then the gain verdicts.
- Unit of resampling: the seed. 10,000 draws, fixed RNG seed, 95% percentile interval. B minus A is paired: both arms use the same drawn seeds, and a seed that lacks data in one arm is dropped from both.
- Every table has a label: window size, model, Claude Code version, seed count, generator version, hooks commit.
- Gain verdict words: GAIN (point at least 5 points, CI lower bound above 0, at least 20 pairs), NO GAIN PROVEN, SMALL SAMPLE (fewer than 20 pairs: no claim), NO DATA.
- A cell with 0 items is "no data", never a pass.
- Seed spread: for each arm and N, the pooled rate, the min-max over seeds and each seed. A pooled number alone can hide a seed that fails for a random reason.
- Planted answers in the last summary: for each arm and seed, found/total in the last compaction summary, and the found count of each summary in order. The line `SUMMARY COLLAPSE` appears when the last summary keeps fewer than half of the answers that the best earlier summary kept. Read it before a claim: a collapse is a random summary failure, not an effect of the arm. It prints "no data" when `retention.jsonl` is missing or empty.
- Type U lookup trace (information only): for each arm, how many U forks made any tool call and how many called recall, and the first 3 recall commands. It prints "no data" for rows with no `lookup_calls`.
- `run.sh regrade` applies the S label rule to stored replies. If `retention.jsonl` is missing, it counts from the live transcripts in `~/.claude/projects` when they exist, and prints "no data" when they do not.

## 6. Pre-registered thresholds (`thresholds.json`)

| Gate | Pass |
|---|---|
| Amnesia (arm Z, all types) | 2% or less |
| Oracle (arm O, types R D X S E C M) | 95% or more |

Arm O size rule: arm O has its own filler size. Files per turn come from `--o-window` (default 200000), not from the main window. Default 3 filler turns. The estimated context (25k overhead, planted content, 12k probe, 6.5k tokens per filler file) stays under 60% of the O window. A pre-flight check stops the run above 75%. Reason: at a 400k main window, 12 files per turn made arm O compact once, and its probes did not run (run 20261118T152042-full).
| Fresh control F, each arm | 95% or more |
| Parse rate, each arm | 98% or more |
| Missed checkpoints (A, B) | 5% or less |
| Invalid pairs | 20% or less |
| Probe leaks | 0 |
| Items planted before compaction 1 | 0 compactions at the end of the plant turns |
| Gain B minus A | 5 points or more, 95% CI lower bound above 0, 20 pairs or more |

Three primary comparisons: B minus A at N=10, at N=30, and type X at N=10. The file is committed before any run and does not change after a run.

## 7. Seeds

`seeds-dev.txt` (1 to 5) is for work on the harness. `seeds-report.txt` (1000 to 1019, rule `1000 + i`) is for reports. Do not tune the hooks on report seeds. A generator change gives a new `GEN_VERSION`. Do not mix versions in one table.

## 8. Isolation

- Real HOME. A temp HOME gives "Not logged in" (decision 2).
- A fresh temp project folder for each session, named `lhbench-<run-id>-s<seed>-<arm>`. Project-level settings only (`--setting-sources project`, no MCP, no slash commands).
- Arm B hooks come from the pinned checkout. `HANDOFF_STATE_DIR` is a temp folder. `RECALL_EMBED_URL` points to a dead port, so no embedding call goes out.
- The transcripts land in `~/.claude/projects/<temp-folder-slug>/`.
- Deny rules in every project: `Read`, `Grep`, `Glob` of `~/.claude/projects/**` and of `lhbench-*-state` folders; `Bash` text with `.claude/projects`, `lhbench` or `-state`. Each project folder is a git root, so `recall.py` (arm B) searches only that folder. Probe fork transcripts are deleted after grading. `run.sh selfcheck` proves the deny rules live.
- `run.sh cleanup` deletes only `<temp>/lhbench-*` and `~/.claude/projects/<slug>-lhbench-*` (exact prefix). It keeps folders of paused runs, unless `--all`.
- The runner never touches the real `~/.claude` settings or hooks, the real handoff state or launchd. It hashes `~/.claude/settings.json` before and after, and warns if it changed.

## 9. Budget, pause and resume

- `claude -p` reports `total_cost_usd`. It is cumulative for a session. A fork reports its parent's total plus its own. The runner keeps a running total of deltas in `state.json`.
- `--budget-usd X`: before each call, the runner stops if the total is X or more. The run pauses cleanly (exit 3). The user plans to pause at 80% of the weekly plan limit. A later pilot will set X from it.
- `run.sh resume <run-id> [--budget-usd X]` continues each unfinished session from its saved turn. A finished session and a finished probe are not repeated. If a project folder is gone, that session starts again from turn 0.
- `--jobs N` (default 2) sets how many sessions run in parallel.

## 10. Cost

- Measured, smoke run (Haiku, 30000 window, 1 seed, arms A and B, N=1, types R and X): $0.146 for 22 calls, 2 min 7 s, 1 compaction in each arm. Pause and resume test: $0.064 to finish after a pause at $0.0435.
- Not measured yet: the cost of 10 or 30 compactions, and of Sonnet and Opus runs. Run a slice first.
- Old estimates (list prices, not verified): full run on Sonnet about $170 (10 seeds) to $360 (20 seeds). Opus about $305 to $640. Check the prices again before a run. `calls.jsonl` has the real numbers.
- Models: slices use Haiku. The full eval uses Sonnet for 20 seeds plus a few Opus seeds (`--model`).

## 11. Risks and open items

- A 30000 window with Haiku is a stress test, not a 400000 window with Sonnet. A small real-window check (2 seeds, N=1 and N=3) must agree before a public claim.
- Claude Code changes behaviour between versions (for example precomputed compactions). The version is in every table label.
- Arm B might gain only because the handoff quotes the planted turns word for word. A second kind of plant (long, wandering instruction) is planned for generator version 2.
- Fork effect: not measured. A later check can ask the same probes in the live session (in a copy of the project folder) for 2 seeds and compare.
- The deny rules match tool input text. A `python3` call that builds a transcript path in code is not blocked. The runner logs lookups. Review `used_lookup` before a claim.
- A/A noise floor: run arm A twice on the same seeds, then `run.sh aa <id1> <id2>`.
- Cost estimates in older drafts were off by large factors. Use measured costs.

## 12. Decisions by the user (binding)

1. The design in the draft is approved: seeded random-token items planted at epoch 0; 7 probe types (R, D, X, S, E, U medium difficulty, C); probes after N = 1, 3, 10, 30 through forked sessions; code grading; arms A, B, Z, O; JSONL per probe; table by arm x type x N with seed-level bootstrap intervals (paired B minus A).
2. Isolation: real HOME, fresh temp project folder, project settings only. No temp HOME.
3. Window: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=30000` for slices, 400000 for the full eval, through `--settings`.
4. Models: Haiku for slices. Sonnet for the full eval (20 seeds) plus a few Opus seeds. The model is a CLI flag.
5. Budget: running total in a state file; `--budget-usd`; `--resume`.
6. Report seeds come from a committed seed list. Dev seeds are separate.
7. Parallelism: `--jobs N`, default 2.

## 13. Recall agent in arm B

If `<lh-path>/agents/recall.md` exists, the runner copies it into the project folder of arm B as `.claude/agents/recall.md`. Arm A never gets it. Test result (Claude Code 2.1.326): a user-level agent in `~/.claude/agents` is NOT visible to `claude -p --setting-sources project`. The session listed only claude, Explore, general-purpose, Plan, statusline-setup and web-fetch. So a project copy is the only way to give arm B this agent.

## 14. v4 changes and why (GEN_VERSION "4")

**Why.** Slice 2 (run 20261118T101712-slice, generator v3, $1.36): all gates passed, but arm A (plain Claude Code) scored 100% at N=1, N=3 and N=10. With no headroom, the benchmark cannot show a gain or a loss. Cause: the Claude Code summary keeps a list of all user messages. Every planted fact was in a short user message, and there were few items. Real long sessions lose facts that sit in tool output, among many look-alike details, over many segments.

**Changes.**

1. **Source realism.** Types S (exact string) and E (dead-end error code) now live in tool output only. The agent must read a file (`cfg/<topic>.txt` or `logs/try-<name>.txt`, 40 lines of normal text with the fact hidden). The user message asks for an action ("tell me if it passed") and never states the value. The runner deletes the file after the turn, so the value is in the transcript only. Types R, D, X and C stay user-stated. U is unchanged (subagent only, reported apart). M is unchanged in meaning (epochs 0, 1, 2).
2. **Load and decoys.** 4 items for each scored type (R, D, X, S, E, C) for each seed. Each item has 2 decoys: look-alike facts for sibling components in the same epoch (same noun, other number, same format, other value). A vague summary gives the wrong one. Probes ask for one component. Decoy values are in `secrets`, so filler never holds them.
3. **Spread and age.** Items are planted in epochs 0, 1, 2 and 4 (`ITEM_EPOCHS`). The runner plants an epoch-e event after compaction e. At checkpoint N the probe asks about the items of epochs up to N-1, oldest first. Each JSONL line has `epoch` and `age` (compactions since planting). User filler turns and plant turns are long (600 to 1,200 characters of realistic task text with no digits), so the user-message list of a summary is long too. Arm O plants all events at once and has no compaction.
4. **Headroom gate (pre-registered in `thresholds.json` before any v4 run, commit 5bd4cc5).** Discriminative at N means oracle accuracy minus arm A accuracy at N is 15 points or more (scored types). If not, the gain verdict at that N prints "BENCHMARK TOO EASY at this N: no claim either way". The report also shows accuracy by item age.
5. **Parse gate.** Arm Z (no history) may answer in prose. A Z parse failure counts as wrong. The Z parse rate is INFO, not a gate.
6. Plan: `events` (sorted by epoch) replace `plant_turns` in the script.

Slice 2 numbers for v3 must not be mixed with v4 numbers in one table.

## 15. v5 changes and why (GEN_VERSION "5", 2026-11-18)

**Why.** In run 20261118T190206-full (Opus, sessions s1001-A and s1001-B), the model answered some filler turns with `name=330` and no Read call. Every file had 330 lines, so the answer was always right. The context stopped growing (about 150k of a 400k window). The session was marked invalid: "no compaction in 10 filler turns".

**Changes.**

1. **Line count per file.** `gen.filler_lines(seed, name, version)` gives each filler file a count of 280 to 380 lines, from `random.Random("<seed>:<name>")`. The mean is 330, so the size of a file (about 20 KB, about 6k tokens) and `files_per_turn` stay valid on average. The count is in no name and no header. For gen v4 and older the count stays 330, so old scripts and regrade do not change.
2. **No-read guard.** `runner.run_filler_turn` counts Read calls on `pad-*` files in the main transcript. If a filler turn has none, the runner sends one retry turn: "Use the Read tool on each file; do not answer from memory." The retry happens before the quiet counter counts the turn. Same rule for every arm. A retry that also has no Read call is recorded in `filler_retry_no_read` and not repeated.
3. **Records.** Each session in `sessions.jsonl` has `filler_no_read_turns`, `filler_retries` and `filler_retry_no_read`. The report has a table with the totals per arm.

4. **Same facts as v4.** `gen.stream_version` maps every version 4 or newer to the v4 seed text. For one seed, v5 and v4 scripts are equal except the `gen_version` field. The probes and filler prompts are equal too. Only the filler line counts differ. A test proves it for seeds 1000-1004 and N 1, 3, 10, 30.

**Limit.** The guard sends extra turns, so the turn count and cost of a v5 run can be a little higher than v4. Arm O has fewer files, so its size estimate can differ a little from the real size (the 60% margin covers this).

## 16. Fair re-test presets (arm Af), 2026-11-26

Why: the proof run (results/20261123T190206-proof) blocked arm A from reading `~/.claude/projects/**`. Plain Claude Code could not search its own transcripts, so the README calls it a "blocked baseline". The owner approved a staged 2-seed fair re-test: max about $88. Fable's design is below.

**FR.1 Arm Af (A-fair).** Plain Claude Code, as arm A (no Lossless Handoff hooks), with these changes:
- Af may read, grep and glob its OWN transcripts.
- Remove the `~/.claude/projects` deny rules for Read, Grep, Glob and Bash for Af.
- Trap: arm A also has `Bash(*lhbench*)`, and Af's own project folder name contains `lhbench-<run>-s<seed>-Af`. For Af, replace that rule with denies that cover only the Lossless Handoff state paths (`*lhbench-*-state*`) and the transcripts of other arms.
- Keep the gen.py `delete_after` deletion of the S/E fact files. The fact must exist only in the transcript.

**FR.2 Isolation from other arms.** Af must not read a transcript of another arm of the same seed: arm O has every fact with no compaction, and arm B holds them too.
- Preferred: run Af with its own Claude config folder (for example `CLAUDE_CONFIG_DIR`, if the runner and the installed Claude Code support it), so that its projects folder holds only Af transcripts.
- If that is not possible: run all Af sessions and probe forks of a run to the end before any O, Z or B session of the same seeds starts. Run `run.sh cleanup --all` first.
- In both cases, add the post-run gate of FR.4. Document which method you used.

**FR.3 Presets and seeds.**
- New seed file `seeds-fair.txt` with 2 seeds that no earlier run used (3001 3002).
- Sonnet, 400k window, 10 compactions, probes at N=10 only, hinted and neutral probes.
- `run.sh fair-preflight`: Haiku, 1 seed, a short session, arms Af and B, cap $3. It must show:
  - real compactions;
  - B hooks that fire;
  - at least 1 successful Af read of its own transcript;
  - `used_lookup` recorded.
  It prints PASS or FAIL for each of these items.
- `run.sh fair-step1`: arms Af, O and Z on seeds-fair.txt, cap $55.
- `run.sh fair-step2`: arm B (current installed version) on the same seeds, cap $45, with `--compare <step1 run id>`.
- `--dry-run` prints the estimate for each preset and spends nothing.

**FR.4 Pre-registered rule.** Add a `fair` block to thresholds.json, committed before any fair data exists, with the rule below.
- Primary rule: B minus Af, hinted probes, N=10, pooled over 2 seeds, is 15 points or more, and B >= Af in 2 of 2 seeds.
- Secondary rule: the same, on neutral probes.
- Report the S/E types apart from R D X C M.
- No bootstrap interval with 2 seeds: report counts.
- INVALID gates:
  - O >= 95% and Z <= 2%;
  - parse rate >= 98%;
  - 10 real compactions per main session;
  - zero denied reads of Af's own transcript;
  - zero Af reads of a path under another arm's project folder or a Lossless Handoff state folder that SUCCEEDED (the tool result is not an error and not a deny). Read these from the Af tool_use inputs and results in the transcripts. A blocked attempt cannot contaminate the answers: count it in the field `af_blocked_attempts`, print it in summary.md, and do not use it as a gate.
- Stop rule, checked after fair-step1: if Af gets 90% or more of the hinted N=10 items, the primary rule cannot pass. Print `STOP: plain Claude Code with search scores <x>/<y>; step 2 not needed`, and write the summary.

**FR.5 Report.** `summary.md` for the fair runs gives counts in this form: "Small test: 2 seeds, <y> hinted questions, Sonnet, 10 compactions. Plain Claude Code that can search its own transcripts: <x> of <y>. Lossless Handoff: <z> of <y>. Two seeds show a direction, not a proof." It never uses the word "validated". When the gap is under 15 points or mixed across seeds, it says "no clear difference".

**FR.6 Tests ($0).**
- Unit tests in bench/continuity (the style of the existing test_proof.py, and the Bash deny emulation if one exists):
  - Af may read its own transcript path;
  - Af may not read another arm's transcript path or a state path;
  - the fair verdict and the stop rule on fixture result rows;
  - the INVALID gates.
- `run.sh fair-step1 --dry-run` and `run.sh fair-step2 --dry-run` work.
- The selfcheck (runner.py `selfcheck`) gets a fair branch that expects an own-transcript read to SUCCEED for Af.

**FR.7 Harness parse failure: answer fallback and reprobe.** Decided 2026-11-26 before any arm B data of the fair re-test.
Found live in fair-step1: in 4 Af probe rows (seed 3002, type E, cued), the fork started a background Bash task. The final `claude -p` result was a late turn ("The background grep I started earlier has finished ..."). The real JSON answer was in an earlier assistant message of the fork. The rows got `parse_ok` False because of the harness, not because of the model.
- **Answer fallback (every arm).** If `result` does not parse for grading, the runner takes the LAST assistant text message of the fork transcript (after the probe prompt) that parses, and grades it. The transcript is read before it is deleted. The row has `raw_source`: `result`, or `transcript` when the fallback was used (`raw_result` then holds the original result). A row that parses today is graded as before. Refused and runaway handling does not change.
- **Reprobe.** `run.sh reprobe <run_id> --seed S --arm X --type T --phrasing P [--dry-run]` re-runs ONLY that probe fork, from the kept main session at the same checkpoint, with the same items, prompt, settings and deny rules (for Af, also the FR.4 read scan). It works for a run that a gate stopped. The new rows go to results.jsonl with `reprobe: true`. The keys of the replaced rows go to `reprobed.jsonl` in the run folder. No old row is deleted. The summary, the verdict, the stop rule and the gates use the reprobe rows in place of the old rows, and the summary prints: `reprobed: <n> probe rows (harness parse failure); old result <a>/<b>, new result <c>/<d>`.
- **Limits.** The command fails with a clear message if the main session transcript is gone (temp folder deleted), if the main session no longer has the compaction count of the probe, for arm Z (no main session), and for the design 4 no-compaction condition (chunked forks). `--dry-run` prints the estimate and starts no model call.
- **Tests ($0).** The fallback takes the last parsable assistant message and sets `raw_source`; a row that parses today is unchanged; the summary uses the reprobe rows and prints the line; a missing main session gives a clear error.

### Implementation notes (this branch)

- **FR.2 method: ordering.** The runner passes `CLAUDE_CONFIG_DIR` through only if the parent shell sets it. Every path in the runner (`PROJECTS`, transcript lookup, cleanup, selfcheck) is fixed to `~/.claude/projects`. A separate config folder also has its own login state. Nobody can confirm that it works without a paid run. So the runner uses the ordering method: in a run with arm Af, all Af sessions and their probe forks finish before any other arm starts. If an Af session is not done, the other arms do not start. Run `run.sh cleanup --all` before the run. Fair-step2 (arm B) starts after fair-step1 has finished.
- **Scored types.** The rule pools R D X S E C M (as the proof test). The report also gives S and E apart from R D X C M. The types U and F are not planted in the fair presets.
- **Hinted probes** are the `cued` phrasing. **Neutral probes** are the `neutral` phrasing.
- **Reads gate.** Fork transcripts are deleted after each probe. So the runner scans the tool calls of every Af probe fork (and the main session) before the delete. It stores the result in the field `af_reads` of each Af row and of the Af session summary. A tool call that names another arm's project folder or a state folder and that SUCCEEDED makes the run INVALID. A blocked call (error or deny) is only counted in `af_blocked_attempts`.

## FR.8 Fair replication (6 seeds), 2026-11-27

Why: the fair re-test (FR.1-FR.7, seeds 3001 3002) shows a direction only. The owner approved preparing a 6-seed confirmatory run (about $220, cap $243) before the weekly reset. A check of the 2-seed data found that every hinted Af error came from a fork that did not search. Each Af fork that searched was right. So FR.8 also measures `told` probes.

**FR.8.1 Seeds.** New file `seeds-fair6.txt`: 4001 4002 4003 4004 4005 4006. No seeds-*.txt and no results/*/config.json holds them. `proof.check_seeds` (or the equivalent check) must accept them.

**FR.8.2 Presets.** Copy fair-step1 and fair-step2. Add both to FAIR_KINDS (or the equivalent) and to the run.sh case list and its usage text. Run the stop check after fair6-step1.
- `fair6-step1`: arms Af,O,Z; seeds from seeds-fair6.txt (6); phrasings cued,neutral,told; budget_usd 130; cost_basis Af 18.20, O 0.83, Z 0.19.
- `fair6-step2 --compare <step1 id>`: arm B; the same seeds and phrasings; budget_usd 110; cost_basis B 15.95.
- Default `--jobs 3`.
- Keep everything else from the fair presets: Sonnet, 400k, N=10, types R,D,X,S,E,C,M, probe design 4, selfcheck, isolation by ordering, FR.7 fallback.
- `fair-preflight` also runs the `told` phrasing.
- `--dry-run` works for both new presets and prints the estimate.

**FR.8.3 Primary rule (confirmatory, the 6 new seeds only).** Hinted (cued) probes, N=10.
- d_s = rate(B) − rate(Af) of seed s. G = pooled B − pooled Af, in points. k = the number of seeds with d_s >= 0.
- p = an exact one-sided sign-flip test on the mean of d_s: take all 64 sign patterns, and count the share with mean >= the observed mean. Use no random numbers. The smallest p is 1/64.
- PASS: G >= 15 and p <= 0.05 and k >= 5.
- FAIL: G < 5 or k <= 3.
- Else: "no clear difference". If p <= 0.05, add the line "gain of <G> points, below the registered 15".
- Also print a 95% percentile bootstrap interval over seeds: 10000 draws, rng seed 20261127. It is not a gate. Label it "6 seeds: this interval is too narrow; description only".

**FR.8.4 Secondary results (these never change the verdict).**
- The same rule on neutral probes.
- The pooled 8 seeds (3001, 3002 and the 6 new seeds), with the same statistics (256 patterns). Label: "not confirmatory: 2 of these seeds set the plan; other LH commit".
- The S and E types apart from R D X C M: counts only.
- Told probes (FR.8.6): counts, G and p.
- Af search rate: the forks with used_lookup, of all forks, per phrasing. Also Af accuracy with and without a lookup.

**FR.8.5 INVALID gates.** All FR.4 gates, on 6 seeds, and also:
- every seed has Af, B, O and Z rows (O and Z run on all 6 seeds);
- one `claude_version` in all sessions of both steps;
- one hook sha256 in all B sessions;
- no live install between the registration commit and the end of step 2.
- A row that does not parse after the FR.7 fallback counts as wrong. Reprobe only a fork with a harness parse failure, one time, with the same rule for each arm. Never reprobe a wrong answer.

**FR.8.6 Told probes.**
- New phrasing `told`: the cued head plus the sentence " If a value is not in your context, search the transcript files of this session before you answer."
- It uses the same 25 items per seed as cued, for all four arms.
- Told forks start after the cued and neutral forks.
- The cued and neutral prompts must stay byte-identical to before (add a test).
- Report line: "When the prompt tells plain Claude Code to search: <x> of 150. Lossless Handoff: <z> of 150."
- If told Af is 90% or more, the summary says: "The gain is that no instruction is necessary."

**FR.8.7 Stop rule after step 1.** If hinted Af has fewer than 23 errors of 150 (Af >= 128), G cannot reach 15. Then print `STOP: plain Claude Code with search scores <x>/150; the primary rule cannot pass`. Do not start step 2 automatically; the owner decides.

**FR.8.8 Report.** Use the FR.5 wording with "6 new seeds, 150 hinted questions". Print the counts, G, k and p. Never write "validated" or "proof".

**FR.8.9 Tests ($0).** Verdict fixtures for:
- PASS, FAIL, and no clear difference;
- the p of 6 of 6 (1/64);
- a 5-of-6 case;
- the stop rule at 127 and at 128;
- each new INVALID gate;
- the seed check;
- both dry runs;
- byte-identical cued and neutral prompts.

**thresholds.json.** Set version 8, and add "v8 adds the fair6 block, committed before any fair6 data" to the note. Add this block (the `fair` block does not change):
```json
"fair6": {"registered": "2026-11-27", "seed_file": "seeds-fair6.txt", "seeds": 6, "n": 10, "types": "R D X S E C M",
  "primary_phrasing": "cued", "secondary_phrasings": "neutral told", "min_gain_points": 15, "fail_below_points": 5,
  "test": "exact one-sided sign-flip on the mean of the per-seed differences, 64 patterns", "p_max": 0.05,
  "min_seeds_b_ge_af": 5, "fail_max_seeds_b_ge_af": 3, "bootstrap_draws": 10000, "bootstrap_rng_seed": 20261127, "bootstrap_is_gate": false,
  "stop_af_min_correct_of_150": 128, "told_af_note_min_rate": 0.9, "pooled8": "secondary only, seeds 3001 3002 added",
  "oracle_min_accuracy": 0.95, "amnesia_max_accuracy": 0.02, "parse_rate_min": 0.98, "compactions_per_main_session": 10,
  "same_claude_version": true, "same_hook_sha256_arm_b": true, "reprobe": "harness parse failure only, one time, each arm",
  "note": "Primary uses the 6 new seeds only. Do not change after a run."}
```


**FR.8.10 Extra seeds (side result), registered 2026-11-28 while fair6-step1 ran and before any fair6 result was read.**
- The owner added 2 more new seeds: 4007 4008 (file `seeds-fair6x.txt`). No earlier run used them.
- They run with the same settings as FR.8.2 (arms Af, O, Z in step 1, then B in step 2; phrasings cued, neutral, told; N=10), after the 6-seed runs.
- The FR.8.3 verdict does not change. It uses the 6 seeds 4001-4006 only.
- Secondary result "8 new seeds" (4001-4008): the FR.8.3 statistics (pooled G, k, exact one-sided sign-flip p over 256 patterns) on cued, neutral and told. Label: "secondary: 2 seeds added after registration; does not change the verdict". It never changes the verdict.
- The FR.8.5 gates apply to the 2 extra seeds too. If a gate fails there, only the secondary result is INVALID.
