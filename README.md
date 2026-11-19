# Lossless Handoff

**Claude Code hooks that make compaction safe: a handoff before every compaction, a decisions list per project, and exact-words search over every earlier chat.**

*Formerly claude-handoff.*

This documentation uses ASD-STE100 Simplified Technical English.

Current versions: product 0.13.0 (`install.sh`), hooks 1.15.0 (`HOOK_VERSION` in `hooks/precompact-handoff.py` and `hooks/postcompact-inject.py`).

[![CI](https://github.com/pratikgh0se/lossless-handoff/actions/workflows/ci.yml/badge.svg)](https://github.com/pratikgh0se/lossless-handoff/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![Release](https://img.shields.io/github/v/release/pratikgh0se/lossless-handoff)](https://github.com/pratikgh0se/lossless-handoff/releases/latest)

![How a handoff flows through a compaction](docs/handoff-timeline.svg)

For the parts, the events that call them and the files they use, read [docs/architecture.md](docs/architecture.md). To learn how Claude decides to use recall, read [How Claude decides to use recall](docs/architecture.md#how-claude-decides-to-use-recall).

## TL;DR

- **Without it:** every compaction turns your session into a generic summary. The exact next step, the dead ends, your standing rules and your exact wording are gone. The model does the work again or asks again.
- **With it:** before each compaction, the hooks write a structured handoff and feed it back in. Each project keeps a decisions list that survives compactions. `recall` finds the exact words of earlier chats (keyword search plus local semantic search). Python 3 standard library only.
- **No silent misses:** a hook cannot force Claude Code to run it. So three steps catch a missed compaction: the hooks write a handoff early, rebuild it after the fact if it is still missing, and count the misses every night. Subagents get their own handoff. See [When a compaction is missed](#when-a-compaction-is-missed).
- **Proof:** in a 30-day reconcile on the author's machine (2026-11-17), 188 of 192 main compactions had a handoff [C2]. In a one-off test (2026-11-13/14), 497 of 497 sampled messages came back byte-identical [C1]. The live C1 rate on 2026-11-18 is 10 of 11. [Proof, not promises](#proof-not-promises) lists the 4 misses and the subagent gap.

## Why this exists

I run Claude Code with a 400k auto-compact window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000`) instead of the 1M default. With 1M, long sessions rarely compact and pay for their whole context on every turn. In a single-user estimate over 4.7 days, the shorter window gave about 19% lower API-equivalent cost [C8] (a dated one-off study, 2026-10-20, not a bill).

The catch is many more compactions. Each compaction used to lose rules, decisions and exact wording. This tool makes it safe to run a small window and compact often. I use it every day, on my own projects. It has one heavy user so far (see [Limitations](#limitations)).

## Install

```bash
git clone https://github.com/pratikgh0se/lossless-handoff.git
cd lossless-handoff
./install.sh --dry-run   # show the plan, change nothing
./install.sh
```

Then restart open Claude Code sessions. Hooks load when a session starts. Prerequisites:

- `python3` and `bash` on macOS or Linux (Windows is not supported).
- A Claude Code version that supports `PreCompact`, `SessionStart` and `PostToolUse` hooks with `additionalContext`.
- Optional: the `claude` binary on `PATH` (the hooks use it for the fresh summary).
- Optional: [Ollama](https://ollama.com/download) (for meaning search).

The default install is small. It installs the core only:

- It copies the hooks and `recall.py` to `~/.claude/hooks/`.
- It installs the recall subagent (`~/.claude/agents/recall.md`). It keeps a file of that name that is not ours.
- It merges the entries into `~/.claude/settings.json`, next to your entries, with a timestamped backup.
- It adds the end-of-chat index (a `SessionEnd` hook).
- It adds a `SubagentStart` hook that gives project rules to a new subagent.
- It adds an install check that runs at session start and prints one line only when something is wrong (`postcompact-inject.py --check`). The check compares the hooks with an install manifest (`~/.claude/hooks/.handoff-manifest.json`).
- It keeps the project decisions list.
- It starts a first index of all your projects in the background.

The default install does not do these things. Each one is an opt-in flag:

| Flag | What it adds |
|---|---|
| `--window N` | Sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to N tokens (for example 400000), only when no value is set. |
| `--nightly` | Two nightly jobs: the index (03:30) and the reconcile-and-prove run (04:15). They are macOS LaunchAgents. On Linux the installer prints the cron lines. |
| `--semantic` | Meaning search on your machine only. If `ollama` is on your PATH, the installer offers `ollama pull bge-m3` (`--yes` pulls without a question). If `ollama` is missing, it says how to get it and recall stays keyword-only. |
| `--llm-map` | `llm_map.py` and the llm-map skill (`~/.claude/skills/llm-map/SKILL.md`). |

Run `./install.sh --window 400000 --nightly --semantic --llm-map` to get everything. Use `--dry-run` first: it lists exactly what the chosen options install.

**Saved defaults.** Add `--save-defaults` to save the opt-in flags of that run to `~/.config/lossless-handoff/install-defaults`. The file holds simple lines: `semantic=1`, `nightly=1`, `llm_map=1`, `window=N`. Every later `./install.sh` run reads the file and applies these options as if you gave them. A flag on the command line wins over the file. `--no-semantic`, `--no-nightly`, `--no-llm-map` and `--no-window` turn one saved default off for that run. With `--save-defaults`, a `--no-*` flag removes that saved default. `--dry-run` shows which options come from the file. `./uninstall.sh` keeps the file. Run `./uninstall.sh --purge`, or delete the file, to drop it.

**Re-install is safe.** A rerun never removes, turns off or changes a part that is already installed: the window value, the nightly jobs, meaning search and the llm-map install stay. A `--no-*` flag only skips adding a part. It does not uninstall one. Use `./uninstall.sh` to remove parts.

**Upgrade from 0.12.x.** Earlier versions installed the window, the nightly jobs and llm-map by default. A rerun of the new installer keeps all of them. A new machine gets the core only. The old flags `--no-window` and `--no-nightly` are still accepted. The installer prints one note that the default is now off.

Other options: `--no-nudge` (skip the context nudge, and remove one from an earlier install), `--managed-hooks` (see [Headless runs](#headless-runs)), `--canary` (opt-in nightly end-to-end test, see below), `--keep-transcripts[=DAYS]` (set `cleanupPeriodDays` to DAYS, default 365, with 90 as the lowest value; only when the value is missing or lower; see [What gets written where](#what-gets-written-where)), `--force`, `--version`, `--help`. Use `--no-nudge` if another tool already nudges you before compaction. The install is idempotent. [What install changes](#what-install-changes) gives the full details.

## Quick start

1. Work as usual. When Claude Code compacts, the hooks inject the handoff and the decisions list automatically.
2. After a compaction, ask "what exactly did I say about X?". Claude runs recall and quotes you.
3. Try it yourself: `python3 ~/.claude/hooks/recall.py user --last 5`

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

**Continuity** ([bench/continuity/README.md](bench/continuity/README.md)) measures one claim. After many compactions, the agent still holds the rules, decisions, exact strings, dead ends and subagent facts from early in the work.

- A seeded generator plants facts with random answers at the start of a long scripted session.
- Filler turns force compactions. Code counts the `compact_boundary` records.
- After 1, 3, 10 and 30 compactions, the runner forks the session and asks probes.
- Probe types: R rule, D decision, X decision that was replaced, S exact string, E dead end, C user contradiction, M order of three decisions made at different times. F is a fresh control. U is a fact that only a subagent saw.
- Arms: A plain Claude Code (no hooks), B Lossless Handoff, Z no history (amnesia control), O no compaction (oracle control).
- Code grades the answers. The report records a lookup trace per arm for type U probes.

**OOLONG** ([bench/oolong/README.md](bench/oolong/README.md)) tests two things on large aggregation tasks: the `llm_map` tool, and that the hooks do no harm. In this harness the context is a file. The report counts compactions of the main session only, and subagents can compact. If the count is 0, the hooks add only their session-start text to the main session. The test shows if plain Claude Code still loses on counting tasks, and if `llm_map` helps.

No scored results are published yet, except one 2-task pilot summary in `bench/oolong/evidence/`. That file is harness evidence only. Results come after the full evaluation: continuity at 30 compactions, and LoCoMo and OOLONG with forced compactions. LoCoMo is a published benchmark for memory over long conversations.

## How word search and meaning search work together

`recall.py search` runs two searches and merges the results.

- **Word search** (keyword or regex) finds exact things: commands, paths, numbers, error strings and names. It is instant and needs no setup. It uses `rg` if you installed it, and plain Python if not.
- **Meaning search** (local bge-m3 through Ollama) finds what you said in different words.
- **Fusion.** Reciprocal-rank fusion merges the results. Each hit has the tag `[word]`, `[meaning]` or `[both]`, so you can see why it matched.

**Example.** A search for "ask the user questions in pop-ups" found an earlier message, "ask me things one by one in pop ups". The two texts share almost no words. This is illustrative output in the real format (the ids are made up):

```
mode: hybrid
a1b2c3d4:57 2026-11-13T09:12 user [meaning]: ask me things one by one in pop ups
e5f6a7b8:12 2026-11-14T16:40 assistant [word]: I will ask the user questions before changing the config
```

**Real numbers** (author's machine, collected 2026-11-16; 26 searches that had hits, 336 hits):

| Tag | Share of hits |
|---|---|
| `[word]` | 51% |
| `[meaning]` | 43% |
| `[both]` | 6% |

In 4 of 26 searches, the best hit came only from meaning search. Keyword search alone would have missed it.

Behaviour to know:

- **It degrades gracefully.** If Ollama is off, the first line says `mode: keyword only` and search still works. In the measured sample, 10 of 28 classifiable searches ran keyword-only. At least one of them ran so because the embedding service was down.
- **It is incremental.** The hooks refresh the index at each compaction, at chat end and every night. None of this delays Claude Code.
- **The decisions list wins.** If recall and the decisions list disagree, the decisions list and your latest words win. If recall and your latest words disagree, your latest words win. Recall can show an older statement.

[Recall commands](#recall-commands) is the command reference.

## Local proof

One command runs every test layer on your machine and writes a dated report:

```
tests/local-proof.sh
```

The report goes to `docs/proof/<date>-<sha>.md`. It starts with a one-line verdict and a table with one row for each layer. [docs/proof/](docs/proof/) holds past reports. Cite them in release PRs.

| Layer | What it does |
|---|---|
| 1 Static | `bash -n`, `shellcheck`, `py_compile` |
| 2 Smoke | `tests/smoke.sh` |
| 3 Acceptance | the separate acceptance suite, repeated (`--repeat N`, default 3), with flaky tests rerun alone |
| 4 Fresh clone | clone the pushed commit, install, `--check`, uninstall twice, all in a temporary HOME |
| 5 Real end to end | 3 real headless Claude Code runs where a main agent and a subagent each compact; checked with `--prove` |
| 6 Real stress | 3 sessions at the same time, a hook killed at its timeout, a missing PreCompact hook |
| 7 Claims | `--claims-lint README.md`, for information only |

Layers 5 and 6 use real model calls. They cost about $3 in total (measured $2.96). The run stops at $4. Use `--no-real` for a free run, `--real-only` for only layers 5 and 6, and `--tests-dir DIR` for an existing tests checkout. The run never changes your real `~/.claude`, your real state folder or your launchd jobs. The report shows a check of them before and after.

## Proof it works

The author measured these results on 2026-11-13/14, on the author's own projects. They are measurements, not promises. Models wrote and graded all tests. The tests use one user's projects.

| Test | Result |
|---|---|
| Lossless retrieval: 497 random messages from before a compaction [C1] | 497 of 497 found and returned byte-identical |
| Meaning search, easy paraphrase test, 118 queries | 98% in the top 10, 89% first |
| Blind quiz after compaction, 33 questions | 41/66 with the v0.4 injection, 50/66 with the full decisions list, 65/66 with the decisions list plus recall |
| Handoff quality, 18 real compactions (score out of 100) | Claude Code's summary alone 62; plus the model's own handoff 68; plus a fresh Sonnet summary 75-76; given the real recent chat 85 |
| Decisions list, 96 questions about older decisions (out of 100) | 13.5 with the 0.2.0 handoff, 56 with the decisions list |
| Continuity, first 13 real compactions | 13 of 13 had a fresh model-written handoff, no degraded cases |

**Lossless retrieval.** The test sampled 497 messages at random (297 of yours and 200 of Claude's; fixed seed) from before a compaction, across 4 projects with up to 196 compactions each. `recall.py search` found them and `recall.py open --full` returned them byte-identical. Mean search time was 0.7 s, and the worst time was 3.1 s on a loaded machine. Limits: it is a sample, not every message. Messages that contain secrets are redacted by design. Subagent transcripts are excluded. A message over 40,000 characters needs `--from` paging. The author added that in 0.6.1 after the test found one 14,300-character message cut at the old 12,000-character output cap.

**Blind quiz.** There were 33 questions over 3 projects: 24 about old decisions and 9 about exact details found only in the chats. Sonnet answered from what a new chat receives. A separate blind grader scored each answer 0 to 2. The one miss with recall was recall that showed an older statement over a current rule. That miss led to the "decisions list wins" rule in 0.6.1. Limits: one run, and a model wrote the questions.

**Handoff quality.** A fresh Claude took a quiz on what it needed to carry on (latest request, next step, decisions, dead ends, rules, files). Version 0.2.0 added the fresh Sonnet summary. Treat the result as a strong hint, not proof.

**Decisions list.** By age: 1 compaction 21 to 66, 2 compactions 10 to 56, 3 or more 8 to 44. Recent memory did not change (62.5).

## Proof, not promises

The tables above are one-off measurements. From 1.10.0, the install also keeps its own evidence. Plain code checks it (no model, no cost). The install publishes only what that evidence supports.

**Receipts.** Every handoff that `postcompact-inject.py` injects appends a line to `~/.local/state/claude-handoffs/receipts.jsonl`. The line holds the transcript, the boundary time, hashes and the first 300 characters. Receipts start with the first compaction after install, so earlier compactions have none.

**Four checks for each receipt** (`precompact-handoff.py --prove`, verdicts in `proofs.jsonl`):

| Check | Passes when |
|---|---|
| `lossless` | 5 sampled messages from the transcript come back word for word through recall's own commands [C1] |
| `right_agent` | every quoted user line in the handoff occurs in the receipt's own transcript, so a subagent never gets the main session's context [C3] |
| `delivered` | the transcript after the boundary holds the injected text [C4] |
| `decisions_safe` | no decision id present before a decisions update is missing after it, and no superseded id came back [C5] |

**Nightly report.** The 04:15 job runs `--reconcile --prove --days 30`. It writes `proof-report.md` (summary line first, then failed checks and skip counts) and `reconcile.json`. When a run has failures, `--check` shows `proof failed: <n> checks` once.

**Claims register.** `hooks/claims.json` lists each public claim (`C1` to `C8`) with its evidence source, window, refresh time and minimum rate. Each `--prove` run evaluates it into `claims-status.json` and `claims-report.md`:

| Status | Meaning |
|---|---|
| `pass` | rate at or above the minimum, evidence present and fresh |
| `fail` | rate below the minimum |
| `stale` | evidence older than its refresh time |
| `no-data` | no evidence in the window (neither failing nor stale) |

`precompact-handoff.py --claims` prints the table. The claims in this README carry the markers `[C1]` to `[C8]`, which match the register. C1 to C6 are live: the hooks compute them from your own transcripts and receipts. C7 (blind quiz) and C8 (cost) are dated one-off studies with a recorded date and a refresh time. They are not live rates. Before you publish a document that cites claims, run:

```
python3 ~/.claude/hooks/precompact-handoff.py --claims-lint README.md
```

The command exits 1 unless every marker is known and `pass`. An unknown, `fail`, `stale` or `no-data` marker blocks publishing. On a machine with no receipts yet, C1 to C6 are `no-data`, so lint is for the author's own machine. On the author's machine (nightly run of 2026-11-18), lint reports C1 (10/11), C2 (196/200) and C6 (9/86) as `fail`. C1: one lossless check failed for a subagent receipt of 2026-11-18 (recall found no hit for one sampled message). C2: the 30-day window holds 4 misses (3 from a deliberate probe run, 1 on 2026-10-23). C6: of 77 misses, 26 are subagent compactions from before hooks 1.10.0, 2 are from the probe run, and 49 are from OOLONG benchmark units of 2026-11-18. Benchmark units use their own settings and state folder, so the installed hooks record no handoff for them. C2 and C6 stay `fail` until these dates leave the window (about 2026-12-18, if no new benchmark run adds misses). The author does not exclude these misses, because hiding real data would make the register worthless.

**What the author's own data says.** In a 30-day reconcile on the author's machine (2026-11-17), 188 of 192 main compactions had a handoff [C2]. Of the 4 misses, 3 came from a deliberate probe run with user settings switched off. The other one was a hook that most likely stopped at its timeout on 2026-10-23. For subagents, 0 of 28 compactions had a handoff before this release. That gap is the bug that 1.10.0 fixes. A live subagent test after the fix gave right-agent 2/2 [C3], delivered 2/2 [C4], failed 0. A dated one-off blind quiz (2026-11-14, same sessions with and without the handoff) scored 98% with the handoff against 62% with Claude Code's compaction summary alone [C7]. This is one author, one machine, and small counts.

## Built on the LCM paper

Lossless Handoff is based on, and inspired by, **"LCM: Lossless Context Management"** by Clint Ehrlich and Theodore Blackman ([arXiv:2605.04050](https://arxiv.org/abs/2605.04050)). The idea of the paper: when a context window fills up, keep every message verbatim in a store and give the model tools to look things up again. Then a compaction summarises and destroys nothing.

This is an independent project. The authors of the paper did not write it, and they are not affiliated with it or endorsing it. The benchmark results of the paper are theirs: nothing here repeats them as ours. The only numbers in this README are our own, from the author's machine.

| From the paper | What we took | What we did differently, and why |
|---|---|---|
| Immutable store of every message | Claude Code already keeps each message verbatim in `~/.claude/projects/<slug>/<session>.jsonl`. We use those files as the store and never write to them. | No new database for messages. Nothing to migrate, nothing to keep in sync. |
| Search and expand tools over the store | `recall.py search` finds hits, `recall.py open` expands a hit with the messages around it. | A plain command line tool, not an MCP server: it costs about 0 tokens until used. One line in the injected context tells the model that it exists. |
| Summaries of older context | A fresh summary at each compaction, plus the model's own handoff. | No summary tree. Hooks cannot change how Claude Code builds its context. So we add a handoff and a decisions list next to Claude Code's own summary, and do not replace it. |
| (not in the paper) | | A per-project decisions list that survives many compactions, and optional search by meaning with a local embedding model, merged with word search. |

```bibtex
@misc{ehrlich2026lcm,
  title         = {LCM: Lossless Context Management},
  author        = {Ehrlich, Clint and Blackman, Theodore},
  year          = {2026},
  eprint        = {2605.04050},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  url           = {https://arxiv.org/abs/2605.04050}
}
```

## How it compares (as of 2026-11-16)

Other projects solve parts of this well. The author fetched the stars on 2026-11-16, and they change quickly. The ticks come from the README of each project. The author did not read the code or run the projects. `?` means the README does not say. "partial" means indirect.

| Project | Stars | Approach | Compaction handoff | Decisions list | Verbatim transcript search | Semantic search | No server needed |
|---|---|---|---|---|---|---|---|
| [lossless-claude/lcm](https://github.com/lossless-claude/lcm) | 60 | Every message in SQLite, old context compacted into a summary DAG | ✓ | ✓ | ✓ | ? | no (daemon) |
| [Martian-Engineering/volt](https://github.com/Martian-Engineering/volt) | 302 | Reference LCM coding agent (OpenCode fork), immutable store, expandable summaries | no (replaces compaction) | ? | ✓ | ? | no (Postgres) |
| [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | 117,507 | Hooks capture tool calls, a second agent writes compressed observations | partial | partial | no (compressed) | ✓ | no (local worker) |
| [ccf/agentcairn](https://github.com/ccf/agentcairn) | 77 | Markdown vault plus disposable DuckDB index, BM25 and vectors fused | partial | ✓ | no (distilled notes) | ✓ | ✓ |
| [BetaBots-LLC/callimachus](https://github.com/BetaBots-LLC/callimachus) | 49 | Local index of 13 coding-agent tools, FTS5 plus vectors fused | partial | ✓ | ✓ | ✓ | ✓ |
| [grooverLab/fable](https://github.com/grooverLab/fable) | 19 | Byte-for-byte verbatim session archive, stdlib only | no | no | ✓ | no | ✓ |
| **Lossless Handoff** | new | Verbatim transcripts as source of truth, structured handoff, decisions list, word plus meaning search | ✓ | ✓ | ✓ | ✓ (optional, local) | ✓ |

**Pick something else if:**

- you want cross-tool memory at large scale across several agents: [funes](https://github.com/huggingface/funes) (594 stars) or callimachus;
- you want automatic observation capture of every tool call: claude-mem;
- you use agents other than Claude Code, or you want summaries that are built live during the session: volt;
- you want rules with types, approval and drift checks: look at the projects that focus on that. The decisions list of this tool is append-style. It has no contradiction checks, except that it marks an entry superseded.

**What this tool does differently:**

- Verbatim transcripts stay the source of truth. The tool reads them as plain files, with no database server.
- Regex search and semantic search are fused over the raw transcripts, not over distilled notes.
- One tool gives a structured handoff and a reasoned decisions list (what was decided, and why).

## How it works

[docs/architecture.md](docs/architecture.md) has the full picture. These are the three main steps.

1. **Nudge** (`handoff-nudge.py`, PostToolUse). When context reaches 75% of the auto-compact window, and again every 25,000 tokens, the hook appends one short nudge text to a tool result. The nudge asks the working model to write its own handoff to `~/.local/state/claude-handoffs/<session_id>-agent.md`. The file has seven fixed headings: Goal, Parallel work running, Decisions and why, Done, In progress, Dead ends, Exact next step. With a 400k window the reminders come at 300k, 325k and 350k. One final nudge, worded "Compaction is imminent", comes at 355k. That is about 12,000 tokens below the real compaction point (window minus 33,000), so the model still has room to write the file. Three sections are living sections: Parallel work running, In progress and Exact next step. If they are byte-identical to the previous reminder, the next nudge says that the handoff is stale. It also asks for a rewrite. It costs no extra turn.

**Which window the nudge uses.** The hook picks the window in this order. (1) `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, if it is a positive integer (source `env`). (2) Otherwise a learned value (source `learned`). The hook reads `compactMetadata.preTokens` of the newest `compact_boundary` records (20 at most) in the current transcript. If there are none, it reads the newest 5 other transcripts of the same project folder, read-only. The window is the median of these counts plus 33,000, rounded to the nearest 1,000. (3) If there is no history, the window is 200,000 (source `default`). The thresholds use the same formulas for every source. If the final threshold is at or below the first reminder threshold, the hook sends only the final nudge. The hook stores `window` and `windowSource` in `<sid>-agent.meta.json`. The next health row copies them as `window` and `window_source`. The hook keeps a learned or default value for 6 hours or until a compaction, so the scan of a large transcript (about 0.25 s for 500 MB) does not run on every tool call.
2. **Handoff** (`precompact-handoff.py`, PreCompact). Just before compaction, the hook writes `<session_id>-latest.md` (14,000 characters at most). The file starts with an authority line. The line says whom to believe when sources disagree. The file holds:
   - the model's own handoff, if there is one (labelled stale when it is out of date);
   - a fresh summary that Sonnet writes (`claude -p --model sonnet`) of the conversation since the last compaction, labelled "verify against facts". It runs in parallel with the decisions update, so a compaction takes about 20-40 s;
   - a facts section that the hook extracts from the transcript with no model: your last 8 messages, files touched, task state, git branch and status, last Bash commands, last assistant message.

   The hook always exits 0 and never blocks compaction. If anything fails, it still writes a minimal handoff.
3. **Inject** (`postcompact-inject.py`, SessionStart with matcher `compact`). After compaction, the hook feeds the handoff back into the new context. Claude Code limits the injected text of each hook to 10,000 characters. So the hook runs three times (`--part 1/2/3`). It splits on section boundaries and always names the full file path. Three more runs (`--ledger --part 1/2/3`) inject the decisions list. A stale agent handoff is still included, after a warning with the reason.

**Who wins when sources disagree:** the user's latest words, then the project decisions, then the agent handoff, then the fresh summary, then Claude Code's own summary, then the extracted facts. If the model is not sure, it should ask.

**Works without the model's cooperation.** If the model never writes the agent file, you still get the fresh Sonnet summary plus the facts (status `reduced`). If the summary also fails or is disabled, you get the facts only. The injected text then starts with a `HANDOFF DEGRADED` warning line.

## When a compaction is missed

A hook cannot force Claude Code to run it, so a handoff can be missed. For example, the hook stopped at its timeout, hooks were switched off for a run, or the install broke. Hooks 1.10.0 treat this as the case to design for. They write early, recover after, detect every miss, check the install, and keep evidence.

| Step | What happens |
|---|---|
| **Early write** | PreCompact writes `<sid>-pending.json` first. Then it writes a deterministic handoff (facts and your last messages) before the first model call. A hook that stops at its timeout still leaves a handoff. The health line removes the pending file. |
| **HANDOFF RECOVERED** | If a compaction still has no handoff, the SessionStart `compact` hook rebuilds one from the transcript (no model call, 6 s deadline). The injected text starts with `HANDOFF RECOVERED`. |
| **`--check`** | At session `startup` and `resume`, `postcompact-inject.py --check` prints one `HANDOFF CHECK:` line in these cases: a hook is not wired, a hook file is missing or changed since install (it compares the manifest), the versions of the two hooks differ, or reconcile found compactions without a handoff. It prints a separate `format drift` line when reconcile found format drift (see Limitations). It prints nothing when all is well. |
| **Nightly reconcile and proof** | At 04:15, `precompact-handoff.py --reconcile --prove --days 30` counts every `compact_boundary` in your transcripts against `health.jsonl`. It proves each receipt (see [Proof, not promises](#proof-not-promises)). It also compares two counts for each session: the recorded post-compaction injects and the `compact_boundary` records. A session with injects and no boundary sets `format_drift` in `reconcile.json`. |

Claude Code can also precompute a compaction. PreCompact runs when Claude Code builds the summary, and Claude Code applies the compaction later, sometimes hours later. The messages from that time on stay word for word after the summary. So the handoff from the first moment, plus the kept messages, cover everything. Hooks 1.10.1 handle this case. If the handoff is newer than the previous compaction boundary, they inject it with the line `Note: this compaction was prepared at <time>; ...` and do not run recovery.

Restart open Claude Code sessions after you install: hooks load when a session starts. The longer write-up is [docs/handoff-reliability.md](docs/handoff-reliability.md).

## Subagents

When a subagent compacts, Claude Code fires the hooks with `agent_id` set, but with the `session_id` and `transcript_path` of the parent. Before 1.10.0, that looked like a main-session compaction and no subagent handoff was written. Now each compacting subagent gets its own handoff. The hooks build it from its own transcript (`<session>/subagents/agent-<agent_id>.jsonl` next to the parent transcript) and store it in `<sid>-a-<agent_id>` files.

- A subagent never reads or writes the handoff of the main session [C3]. The main session never reads or writes the handoff of a subagent.
- A subagent never merges the decisions list and never starts the indexer. The nudge hook (`handoff-nudge.py`) ignores subagent events (non-empty `agent_id`), so it never nudges a subagent.
- If the subagent transcript is missing, the health row says `degraded` / `agent_transcript_missing` and the hooks write no handoff.
- Reconcile counts subagent compactions separately, and the register tracks that they are covered [C6]. Before 1.10.0 they were not: 0 of 28 in the first 30-day reconcile of the author.

**Rules at subagent start (hooks 1.13.0).** A new subagent gets no project rules, and most subagents never compact. So the installer registers a `SubagentStart` hook (`postcompact-inject.py --subagent-start`, timeout 10). The hook gives the new subagent, and only the subagent, one block of context. The block holds the active `rule` entries of the project decisions list (newest first, 4,000 characters at most, cut at an entry boundary) and the recall line. The hook never gives decisions, tasks, the handoff of the main agent or the agent notes. If there is no decisions list or no rule, the hook gives only the recall line. `HANDOFF_SUBAGENT_RULES=0` turns the hook off. Each run adds a row with `event: "subagent_start"` to `health.jsonl`. `--prove` and `--reconcile` skip these rows. The hook takes about 50 ms with 200 rules. `SubagentStart` is a new hook event: restart open Claude Code sessions after you update. The installer prints a line about this only when it adds the entry.

## Headless runs

`claude -p` and the Agent SDK load your user hooks by default. They skip them only with `--bare`, `disableAllHooks`, `--setting-sources` without `user`, or SDK `settingSources: []`.

To keep the handoff hooks in every run except `--bare`, use managed settings:

```
./install.sh --managed-hooks
```

This is a standalone mode that changes nothing else. It writes `~/.local/state/claude-handoffs/managed-settings.json`. If a system file exists, the script reads it and merges it in, and never writes to it. The script prints the command to install the file. Run the command yourself, because it needs admin rights. The script never runs sudo:

```
# macOS
sudo install -m 644 ~/.local/state/claude-handoffs/managed-settings.json "/Library/Application Support/ClaudeCode/managed-settings.json"
# Linux: /etc/claude-code/managed-settings.json
```

With managed hooks installed, only `--bare` skips them. `--check` accepts hooks from user or managed settings. Reconcile reports any compaction that still got no handoff, headless or not.

## The project decisions list

A handoff belongs to one session, but decisions belong to the project. The hooks keep one plain-text list for each project (a git repo, or the folder where you started if there is no repo). The path is `~/.local/state/claude-handoffs/projects/<project>-<hash>/decisions.md`. The code and some option names (`HANDOFF_LEDGER`, `--ledger`) call the decisions list the "ledger". Each decision has one line: an id (`D12`), the date, a kind, what was decided, and why.

- **Kinds.** `[rule]` (a standing rule or working preference), `[decision]` (a choice) or `[task]` (a follow-up that you asked for). Older untagged lines count as decisions.
- **What goes in.** Only things that you decided or approved explicitly: a choice, a rule, a constraint, a permission, a rejected option. Plans, progress and unapproved ideas of the assistant do not go in.
- **Kept up to date.** At every compaction, a second Sonnet call reads the conversation since the last update and the current list. It returns the updated list. When a task is finished, the hook marks the line `RESOLVED` and no longer injects it. The model never resolves rules.
- **Never deleted.** When you change your mind, the hook marks the old decision `SUPERSEDED by D<n>` with the reason. The decision stays in the file. Before each update, the hook saves the previous list as `decisions.<timestamp>.md` (it keeps the newest 20).
- **Code checks the model.** Sonnet proposes, and plain code decides. The code puts back an entry that the model drops. It never revives a superseded entry. Existing ids never change. The code shortens over-long lines and does not drop them. It throws away a result that has no valid line. A project lock stops two compactions in the same repo from overwriting each other.
- **Injected after compaction and in new chats.** There are three parts: rules first (always in full), then decisions, then tasks, newest first. What does not fit in 3 x 10,000 characters becomes one-line index entries, so nothing is silently dropped. The hooks inject the same parts at `startup` and `resume`, so a fresh session does not ask again what you already settled.
- **Tidy an old list.** `python3 ~/.claude/hooks/precompact-handoff.py --ledger-maintain <repo>` runs one maintenance pass. It tags untagged entries, resolves finished tasks and supersedes contradictions. It adds no entries and edits no text.

Set `HANDOFF_LEDGER=0` to turn it off. Turning it off or uninstalling never deletes existing lists.

## llm_map: label many items with a small model

`llm_map.py` sends each item of a large list through a small model (Haiku by default) and writes one exact answer for each item. The main model then counts or aggregates the answers with a script, so it does not guess a count from a sample.

When to use it: a task needs a label, class, extracted value or check for each of hundreds of items or more. In OOLONG pilot runs on the author's machine (2026-11-18, results not published), plain Claude Code filtered with a regex, read a sample and estimated the count.

```
python3 ~/.claude/hooks/llm_map.py --in reviews.txt --prompt "Label each review as positive, negative or mixed." --out labels.jsonl [--labels "positive|negative|mixed"] [--model haiku] [--batch 100] [--jobs 8] [--max-usd 2.00] [--json-out]
```

- `--in` is a text file with one item on each line, or a `.jsonl` file with `text` and an optional `id`.
- `--out` gets one JSON line for each item, in input order, with `i`, `text`, `out` and (if it failed) `error`.
- `--labels "a|b|c"` sets the allowed answers (2 or more, split on `|`; not with `--json-out`). `out` is always a canonical label. A reply maps to a label in these cases: it equals a label (trim, case fold), or it is a prefix of exactly one label, or exactly one label is a prefix of it. The prefix must end at a word end of the longer text (`numeric` matches `numeric value`; `h` does not match `human being`). Other replies are invalid. The tool retries them once. Then they get `out` null and `error: "label: <reply>"`.
- The defaults are 100 items in a batch and 8 batches at the same time, so 3,000 items take about one minute. After each batch, stderr gets `llm_map: <done> of <n> items done`.
- The tool retries a missing, invalid or unparsable answer once. The last stdout line is `llm_map: <n> items, <ok> ok, <failed> failed, <b> batches, cost $<x>`. Exit code 3 means that some items failed.
- Cost note: each batch is a real model call and costs money. Haiku batches of 100 items cost a fraction of a cent each. `--max-usd` (default 2.00) stops new batches when the cap would pass. The unsent items get `error: "budget"`.
- Each call runs `claude -p` with no tools, no hooks, no MCP servers and no saved session, in a new temp folder. Lossless Handoff hooks never run for these calls, so they write no handoffs or health rows.
- The installer also writes the skill `~/.claude/skills/llm-map/SKILL.md`. Its description tells Claude Code to use llm_map for tasks that need an answer for each item of a large list. Its body gives the command and the rule to report exact counts and the number of failed items. If a skill file is not ours (it has no marker line), install leaves it unchanged. Uninstall removes only the marked file.

## Recall commands

`recall.py` searches every earlier chat in the same project (all sessions whose folder is inside the git root of the project). It is read-only and works over the transcripts that Claude Code already keeps. A summary keeps decisions but loses exact commands, numbers, paths and your own wording. Recall gets them back.

```
python3 ~/.claude/hooks/recall.py search "zebra-mode"          # words, regex or meaning (hybrid)
python3 ~/.claude/hooks/recall.py search "why we dropped the cache" --meaning
python3 ~/.claude/hooks/recall.py search "deploy.sh" --keyword --fixed --role user
python3 ~/.claude/hooks/recall.py open abcd1234:57             # a hit plus 2 messages each side (--around K, --full)
python3 ~/.claude/hooks/recall.py open abcd1234:57 --full      # the whole message, up to 40,000 chars
python3 ~/.claude/hooks/recall.py open abcd1234:57 --full --from 40000   # next page of a longer message
python3 ~/.claude/hooks/recall.py user --last 20               # your own typed messages, newest first
python3 ~/.claude/hooks/recall.py handoffs                     # this project's handoff files
python3 ~/.claude/hooks/recall.py index                        # build or refresh the index for this project
python3 ~/.claude/hooks/recall.py index --all                  # every project that has a git repo
```

**Summary nodes.** Each compaction in a transcript gets one leaf node. A node has an id (`n` and 10 hex characters), the session, the transcript line range that it covers, the time and the token count before the compaction. The hooks read the nodes from the `compact_boundary` records of Claude Code. The hooks make no model call for them. The list is in `projects/<project>-<hash>/nodes.jsonl`. The SessionStart `compact` hook adds new nodes from the saved byte offset, so it reads only new bytes.

```
python3 ~/.claude/hooks/recall.py map                    # one line per node, oldest first: id, time, session, line range, gist
python3 ~/.claude/hooks/recall.py describe <node id>     # the node's fields and the full Claude Code summary of that compaction
python3 ~/.claude/hooks/recall.py search "word" --node <node id>   # only hits inside that node's line range
python3 ~/.claude/hooks/recall.py nodes --backfill       # add nodes for all transcripts of the project at once
```

Each `search` hit ends with `[node <id>]`, or `[node live]` when it is after the last compaction. After a compaction, the injected context has one line that names the newest node. `--prove` checks that every `compact_boundary` has one node and that the line ranges leave no gap or overlap.

**Recall subagent.** The installer writes `~/.claude/agents/recall.md`. It is a small agent (model `haiku`, tools `Bash` and `Read`) that runs `map`, `search`, `describe` and `open`. It returns at most 15 short quotes with message ids and node ids. When the file is installed, the injected recall line tells the main agent to use it for wide lookups, so long recalled text stays out of the main context. The line also tells the main agent to search directly for one exact fact. Nothing stops the main agent from running `recall.py open` itself. [How Claude decides to use recall](docs/architecture.md#how-claude-decides-to-use-recall) explains the details.

Every command accepts `--cwd DIR` (default: the current folder). `search` also takes `--session SID8|all`, `--limit N` and `--no-subagents`. Search, `open` and the meaning index include the work of subagents by default. A subagent hit has the id `<sid8>/<agent8>:<line>` and the tag `[agent <agent8>]`. It shows what a subagent saw or did, not what you typed. `--no-subagents` leaves them out. The old `--subagents` flag is still accepted and does nothing. `user` still reads only the main chats, because the user turns of a subagent are prompts from the main agent. In a plain pattern (letters, digits, spaces, `-`, `_`, `.`), a space, `-` and `_` match each other, so `ledger key` finds `ledger-key`. `--fixed` keeps the match exact (hooks 1.13.1). A hit line from a tool call ends with ` -> result <id>` when the tool returned a result later in the same chat. Run `open <id>` to read what the tool returned. `--keyword` is words only, and `--meaning` is the embedding index only. Output is limited to 12,000 characters. Before it prints or embeds text, recall replaces secrets with `[REDACTED:<kind>]` (see [Limitations](#limitations) and `SECURITY.md` for the kinds). `open <id> --full` returns up to 40,000 characters. A longer message ends with a line that gives the exact `--from <N>` to continue with.

**Meaning search setup.** It needs a local embedding model: [Ollama](https://ollama.com/download) with `ollama pull bge-m3`. Run `./install.sh --semantic` to set it up (or save it with `--save-defaults`). If `ollama` is on your PATH, the installer offers to pull the model (`--yes` says yes; a non-interactive run skips it and prints the command). If Ollama is not installed, the installer prints a hint and carries on in keyword-only mode. All search stays on your machine. The installer never installs Ollama itself. The backend settings are `RECALL_EMBED_*` in [Configuration](#configuration).

## What install changes

- It copies `precompact-handoff.py`, `postcompact-inject.py`, `handoff-nudge.py`, `recall.py` and `claims.json` to `~/.claude/hooks/`. It first saves a differing existing file to `~/.claude/hooks/.claude-handoff-backup-<timestamp>/`.
- It copies `agents/recall.md` from this repository to `~/.claude/agents/recall.md` (the recall subagent). If a file of that name does not hold the marker line `<!-- lossless-handoff:recall-agent -->`, the installer keeps the file and says so. Uninstall removes the file only when it holds the marker. The repository file is the single source: a benchmark or a project can copy the same file into its own `.claude/agents/` folder.
- It writes `~/.claude/hooks/.handoff-manifest.json`: version, time and a SHA-256 for each installed file. `--check` compares them.
- With `--llm-map`: it copies `hooks/llm_map.py` to `~/.claude/hooks/` and `skills/llm-map/SKILL.md` to `~/.claude/skills/llm-map/SKILL.md`. If a file of that name does not hold the marker line `<!-- lossless-handoff:llm-map-skill -->`, the installer keeps the file and says so.
- It records everything that it added in `~/.claude/hooks/.claude-handoff-install.json`, so uninstall removes exactly that.
- It merges into `~/.claude/settings.json` (backup `settings.json.bak-<timestamp>`):
  - a PreCompact hook (timeout 150) and a PostToolUse hook (matcher `*`, timeout 5);
  - a SessionStart `compact` group with the three inject commands and the three `--ledger --part 1/2/3` commands (timeout 10), and `startup` and `resume` groups with the three `--ledger` commands plus `postcompact-inject.py --check` (timeout 5);
  - a `SessionEnd` hook that runs `recall.py session-end` (timeout 5);
  - a `SubagentStart` hook that runs `postcompact-inject.py --subagent-start` (timeout 10);
  - `permissions.allow` entries `Edit(~/.local/state/claude-handoffs/**)` (so the model can write its handoff without a prompt) and `Bash(python3 ~/.claude/hooks/recall.py:*)` (so it can run recall without a prompt);
  - only with `--window N`: `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` = `"N"`, unless it is already set (an existing value is never changed).
- It creates `~/.local/state/claude-handoffs/`.
- With `--semantic`, and only if `ollama` is on your PATH: it offers `ollama pull bge-m3`.
- With `--save-defaults`: it writes `~/.config/lossless-handoff/install-defaults`.
- On macOS, with `--nightly`: it writes the LaunchAgent `~/Library/LaunchAgents/com.lossless-handoff.recall-index.plist` (daily 03:30, runs `recall.py index --all --max-seconds 3000`, log `~/.local/state/claude-handoffs/recall/index-all.log`) and loads it. On Linux it prints the equivalent crontab line and does not edit your crontab.
- On macOS, with `--nightly`: it writes and loads the LaunchAgent `~/Library/LaunchAgents/com.lossless-handoff.handoff-reconcile.plist` (daily 04:15, runs `precompact-handoff.py --reconcile --prove --days 30`, log `~/.local/state/claude-handoffs/reconcile.log`). On Linux it prints that crontab line too (`15 4 * * * python3 ~/.claude/hooks/precompact-handoff.py --reconcile --prove --days 30 >> ~/.local/state/claude-handoffs/reconcile.log 2>&1`, with your real path).
- With `--managed-hooks` (standalone): it stages a managed-settings file in the state folder and prints the `sudo install -m 644 ...` command. It changes nothing else and never runs sudo.
- It starts `recall.py index --all` in the background.
- With `--canary` (opt-in, off by default because it makes real model calls, about $0.03-0.10 per run): it copies `tools/handoff-canary/` to `~/.claude/hooks/handoff-canary/` and `handoff-canary.sh` to `~/.claude/hooks/`. On macOS it writes the LaunchAgent `com.lossless-handoff.handoff-canary` (daily 03:30, `--catch-up`). On Linux it prints the crontab line. The canary drives two headless Claude Code sessions past a compaction and checks that the handoff arrives. See [`tools/handoff-canary/README.md`](tools/handoff-canary/README.md).

Your other hooks and settings stay as they are: the install appends new entries next to existing ones. If the same hook scripts are already registered from another path, the install prints those commands and exits 2, because every hook would run twice. Remove them, or pass `--force`. The install fails with a clear message if `python3` is missing or `settings.json` is not valid JSON.

## What gets written where

| Path | Content |
|---|---|
| `~/.local/state/claude-handoffs/<sid>-latest.md` | The handoff that the hooks inject after compaction. `<sid>-<timestamp>.md` is a copy. |
| `<sid>-agent.md` | The model's own handoff (written by the model). |
| `<sid>-agent.meta.json` | Nudge state and timestamps (numbers only, no text). |
| `recall-index.json`, `recall/` | Recall caches: which transcripts belong to which project, and the meaning index (`<project>.sqlite`, `index.log`). Safe to delete. |
| `health.jsonl` | One JSON line for each compaction: time, trigger, agent file fresh/stale/absent, fallback result, decisions update result (`ok`, `unchanged`, `failed`, `locked`, `disabled`) and counts, duration, size, status `ok` / `reduced` / `degraded`. No message text or paths. It rotates at 5,000 lines. |
| `<sid>-last-health.json`, `<sid>-degraded.json` | Per-session status and degraded reasons. |
| `<sid>-pending.json` | Marker while PreCompact runs. The health line removes it. |
| `<sid>-a-<agent_id>*` | The same files for a compacting subagent (handoff, latest, pending, note). Health rows for subagents carry `agent_id` and `agent_type`. |
| `reconcile.json`, `reconcile.log` | Last reconcile result (with the once-only notice marker for `--check`) and the output of the nightly job. |
| `receipts.jsonl`, `proofs.jsonl`, `proof-report.md`, `proof-notice.json` | Proof: one receipt for each injected handoff, one verdict for each receipt, the report, and the once-only alert marker. |
| `claims-status.json`, `claims-report.md`, `claims-notice.json` | Claims register evaluation and its once-only alert marker. |
| `managed-settings.json` | Only after `install.sh --managed-hooks`: the file that you copy to the system location. |
| `~/.claude/hooks/.handoff-manifest.json` | Installed hook file hashes (not in the state folder). |
| `projects/<project>-<hash>/nodes.jsonl`, `nodes.state.json`, `nodes.lock` | Summary nodes: one line for each compaction (ids, session, line range, times, token count, paths). It holds no message text. |
| `~/.claude/agents/recall.md` | The recall subagent (user level; not in the state folder). Source file in this repository: `agents/recall.md`. |
| `projects/<project>-<hash>/decisions.md` | The decisions list of the project (uninstall keeps it). Next to it: `decisions.<timestamp>.md`, `decisions.state.json`, `decisions.lock`. |

The hooks keep handoff files (`*.md`) for 365 days. Set `HANDOFF_RETENTION_DAYS` to change this, or to `0` to keep them forever. The hooks remove the small status files (`*-degraded.json`, `*-last-health.json`, `*-pending.json`) after 14 days. The hooks never clean the `projects/` folder. A longer retention means that more of your chat text stays on disk for longer. Claude Code also deletes its own chat transcripts after `cleanupPeriodDays` days (default 30), and recall can find only messages that are still there. Run `./install.sh --keep-transcripts` to set it to 365 (it never lowers a value and never overrides a managed settings file). The session-start check warns if the value is below 90. The directory is outside `~/.claude/` because Claude Code refuses model writes under that path ([ADR 0008](docs/adr/0008-handoffs-directory-under-local-state.md)).

## Verify

```
claude -p --model haiku "say hi" --output-format json   # note the session_id
claude -p --model haiku --resume <session_id> "/compact"
tail -n 3 ~/.local/state/claude-handoffs/health.jsonl
```

A new line with `"trigger": "manual"` means that the PreCompact hook fired. In a normal session, run `/compact` and look at the same file. `status` is `ok` (the hook used the model handoff), `reduced` (the hook used the fresh summary instead) or `degraded` (facts only). You can also run `bash tests/smoke.sh`. It uses a temporary HOME and does not touch your real settings.

To test a real compaction without a change to your own install:

1. Install into a temporary home with `HOME=<tmp> LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 ./install.sh --no-nightly`, so no LaunchAgent is written or loaded.
2. Keep your real HOME for the run, because `claude` reads its login from there.
3. Run `claude -p --setting-sources local --settings <tmp>/.claude/settings.json ...` from a fresh folder, with `HANDOFF_STATE_DIR=<tmp>/.local/state/claude-handoffs`. This switches your own hooks off and keeps all handoff files in the temporary folder.
4. To force a compaction quickly, set `CLAUDE_CODE_AUTO_COMPACT_WINDOW=30000` both in the process environment and in the `env` of the settings file that you pass with `--settings`. If you run with your own user settings and they set this variable (the installer sets 400000), that value wins over the process environment. In that case, pass `--settings '{"env":{"CLAUDE_CODE_AUTO_COMPACT_WINDOW":"30000"}}'`. Keep each file that the agents read under about 20k tokens (for example 900 lines). The Read tool refuses larger files, and then the context does not grow.
5. Check with `HANDOFF_STATE_DIR=... python3 <tmp>/.claude/hooks/precompact-handoff.py --prove --days 1`.

Use a folder name that contains `handoff-canary`, so that the nightly check on your real install does not count these compactions as missed.

## Configuration

| Variable | Effect |
|---|---|
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | Window size in tokens. Nudge thresholds are 75% of it, then every 25,000, then a final nudge (window minus 45,000). If it is unset, empty or not a positive integer, the nudge learns the window from past compactions, or uses 200000 (see "Which window the nudge uses"). The installer sets 400000. |
| `HANDOFF_SUMMARY=0` | Disable the fresh summary at compaction (no model call; facts only). |
| `HANDOFF_SUMMARY_MODEL` | Model for the fresh summary. Default `sonnet`. |
| `HANDOFF_SUMMARY_TIMEOUT` | Seconds to wait for the summary (1-300). Default 75. |
| `HANDOFF_LEDGER=0` | Disable the decisions list: no merge call, nothing written, nothing injected. |
| `HANDOFF_LEDGER_MODEL` | Model for the decisions update. Default: the summary model. |
| `HANDOFF_LEDGER_TIMEOUT` | Seconds to wait for the decisions update (1-300). Default 100. The PreCompact hook timeout is 150. |
| `HANDOFF_HAIKU_FALLBACK=0` | Legacy switch from 0.1.x; still honoured and also disables the fresh summary. |
| `RECALL_EMBED_URL`, `RECALL_EMBED_MODEL`, `RECALL_EMBED_BACKEND`, `RECALL_EMBED_KEY` | Meaning search backend. Defaults: Ollama at `http://localhost:11434`, model `bge-m3`. Backend `openai` works with any `/v1/embeddings` server; the key is an optional Bearer token. |
| `HANDOFF_RETENTION_DAYS` | Days to keep handoff `*.md` files. Default 365. `0` keeps them forever. A value that is not a whole number counts as 365. The `*-degraded.json`, `*-last-health.json` and `*-pending.json` files always use 14 days. |
| `HANDOFF_SUBAGENT_RULES=0` | Do not give project rules to a new subagent. The `SubagentStart` hook then prints nothing. |
| `HANDOFF_COMPACT_STEER=0` | Do not print the guidance for Claude Code's own compaction summary. The hook prints it by default. The guidance is added to any text that you type after `/compact`. A later benchmark measures the effect on summary quality. |
| `LOSSLESS_HANDOFF_MANAGED_SETTINGS` | Test only. Path of the managed settings file that `install.sh`, `uninstall.sh` and `postcompact-inject.py --check` read instead of `/Library/Application Support/ClaudeCode/managed-settings.json`. The older name `HANDOFF_MANAGED_SETTINGS_PATH` still works. The tools never create the real file. |
| `HANDOFF_STATE_DIR` | Use another directory instead of `~/.local/state/claude-handoffs`. |
| `LOSSLESS_HANDOFF_NO_LAUNCHCTL=1` | `install.sh` and `uninstall.sh` write and remove the LaunchAgent files but never call `launchctl`. Use it for any install into a temporary HOME on macOS. `launchctl` acts on your real login session, so without it a test install loads, and a test uninstall unloads, jobs with the real labels. |
| `HANDOFF_HOOK_CHILD` | The hook sets it for its own summary call, so it never recurses. Do not set it yourself. |

## FAQ

**Does it cost tokens?** It costs about one short nudge text each time. It also costs up to about 14,000 characters injected once for each compaction, and up to about 20,000 characters of decisions. Each compaction adds two `claude -p` Sonnet calls (fresh summary and decisions update; 20-40 s in parallel, roughly $0.10-0.20 of API-equivalent usage). Set `HANDOFF_SUMMARY=0` and `HANDOFF_LEDGER=0` to avoid them.

**Will recall find old messages?** Only while Claude Code still keeps the transcript. The default is 30 days. Run `./install.sh --keep-transcripts` (365 days) or set `"cleanupPeriodDays"` yourself. The hooks keep handoff files for 365 days. A longer retention means that more chat text stays on your disk for longer.

**Which new fields does the health row have?** `excerpt_chars` is the number of characters of transcript that the handoff model received (0 if the hook made no model call). `excerpt_dropped` is the number of characters of the current segment that the hook left out because of the 120,000-character limit (0 if it cut nothing). `--prove --days N` prints `excerpt cut: <c> of <n> compactions, median dropped <m> chars`. This line is information only and never fails a proof run. It measures how often the cut happens. It does not claim any gain in answer quality.

**What does the PreCompact hook print?** It prints a short block of guidance for Claude Code's own summary (quote your words, keep exact values, list running work, end with the next step). The hook gives this guidance only. A later benchmark measures the effect on summaries. Set `HANDOFF_COMPACT_STEER=0` to turn it off. The health row has a `steer` field (`printed` or `off`).

**Does it work in the desktop app?** Yes. The desktop app runs the same hooks from `~/.claude/settings.json`.

**Does it work with my existing hooks?** Yes. The install merges into your existing `hooks` groups and never removes or reorders yours. Uninstall removes only its own commands.

**Where must I set `CLAUDE_CODE_AUTO_COMPACT_WINDOW`?** Set it in your user `~/.claude/settings.json` under `env` (the install does this), or export it in the shell that starts Claude Code. A live test showed that `env` in the `.claude/settings.json` of a project did not override it.

**What window size should I use?** Use a size that comfortably exceeds your baseline context. With a very small window (180,000 tokens or less), the final threshold (window minus 45,000) is at or below the first reminder threshold. The nudge then sends only the final nudge, at the first threshold that the context reaches, and the PreCompact hook covers the rest. The savings come from long sessions on 1M-context models. If your sessions rarely pass 300k tokens, the window never triggers and you mainly get the handoff. Nobody measured whether a shorter context also improves answer quality. See [ADR 0001](docs/adr/0001-compact-window-400k-with-handoff.md).

**Can the nudge be skipped?** Yes. It reads the token count of the previous turn, so one very large tool result can jump past the threshold without a nudge. The PreCompact hook still writes a handoff.

**Does it send anything over the network?** The scripts do not, except meaning search. Meaning search sends message text to the embedding server that you configure (by default Ollama on your own machine). The two per-compaction calls run your local `claude -p`. See `SECURITY.md`.

## Troubleshooting

- Logs and health are in `~/.local/state/claude-handoffs/` (or `$HANDOFF_STATE_DIR`). Start with `tail -n 5 ~/.local/state/claude-handoffs/health.jsonl`. It has one line for each compaction with `status` `ok`, `reduced` or `degraded`.
- `<sid>-degraded.json` and `<sid>-last-health.json` in the same folder say why a compaction was degraded.
- A `HANDOFF CHECK:` line at session start names what is wrong (hook not wired, file missing or changed, version mismatch, compactions without a handoff). Run `./install.sh` again, then restart open sessions. The `proof failed` and `claims` lines point at `proof-report.md` and `claims-report.md` in the state folder.
- A `HANDOFF RECOVERED` line at the top of the injected text means that PreCompact did not finish. The hook rebuilt the handoff from the transcript with no model call. Check `health.jsonl` and `hooks-error.jsonl`.
- If there is no health line after `/compact`, restart Claude Code (hooks load at startup). Then check that `~/.claude/settings.json` has the `PreCompact` entry and that `~/.claude/hooks/precompact-handoff.py` exists.
- If there is no nudge: it fires only above 75% of the window (300k for 400k) and it needs a usage entry in the transcript. Check `CLAUDE_CODE_AUTO_COMPACT_WINDOW`.
- If search says `keyword only`, Ollama is not running or `bge-m3` is not pulled. Start Ollama and run `ollama pull bge-m3`.
- If settings or hooks are in a bad state: install and uninstall leave timestamped backups (`settings.json.bak-*`, `~/.claude/hooks/.claude-handoff-backup-*`).
- For any other problem, run `bash tests/smoke.sh`, then open an issue with the bug report template.

## Tests

Three layers run on every push and pull request (GitHub Actions, Ubuntu and macOS):

1. **Static checks:** `bash -n`, shellcheck and `py_compile`.
2. **Smoke test:** `bash tests/smoke.sh`. It does install, the hooks, recall, the canary and uninstall in a temporary HOME.
3. **Acceptance tests:** the code-blind suite in [lossless-handoff-tests](https://github.com/pratikgh0se/lossless-handoff-tests), at the commit named in `tests.ref`.

**The code-blind rule.** A writer who never reads the hook code writes the acceptance tests only from [`docs/spec.md`](docs/spec.md). The people and agents who change the hooks never read the tests. They see only the test names and the assertion output. So a behaviour change starts in `docs/spec.md`. The tests follow the spec, and the hooks follow the tests.

**When a pull request changes behaviour:**

1. Amend `docs/spec.md` (a new or changed AC number).
2. Update the tests in lossless-handoff-tests from the spec.
3. Set `tests.ref` in this repository to the new tests commit, in the same pull request.

Run the acceptance tests locally:

```bash
git clone https://github.com/pratikgh0se/lossless-handoff-tests ../lossless-handoff-tests
git -C ../lossless-handoff-tests checkout "$(cat tests.ref)"
HANDOFF_HOOKS_DIR="$PWD/hooks" LOSSLESS_HANDOFF_REPO="$PWD" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 \
  python3 -W ignore -m unittest discover -s ../lossless-handoff-tests
```

The tests use a temporary HOME and never call `launchctl`, so your own install stays as it is.

## Limitations

- **One heavy user so far.** All numbers come from the author's machine. Nobody has tested whether it helps other workflows.
- **Handoffs can be cut.** 20% of handoffs (43 of 217) hit the 14,000-character size cap and were truncated. 15% were `reduced` and 2% `degraded`, and the nightly self-test failed 2 of 34 runs (31 passed and 1 was skipped; see "In daily use").
- **Recall is new.** It has existed since 2026-11-12. The author did most of its use so far, much of it while the author built and tested it. The meaning half is a minority of wins (4 of 26 searches had a meaning-only top hit). The 143 meaning hits count lines shown, not proof that they were useful.
- **Misses still happen.** 4 of 192 main compactions in the 30-day reconcile had no handoff (see [Proof, not promises](#proof-not-promises)). The early write, recovery and `--check` exist to make the misses fewer and to show them. They do not promise zero. Receipts exist only from the first compaction after install, so proof rates start small. C7 and C8 are dated one-off studies, not live rates.
- **Redaction is pattern-based.** The hooks mask known secret shapes (AWS, GitHub, Slack, Anthropic and OpenAI style keys, Google API keys, JWTs, PEM private keys, and values of keys that end with `password`, `passwd`, `secret`, `token`, `api_key`, `apikey` or `access_key`) before they write handoff files, the decisions list and recall output. A secret in another shape is not masked. A secret in prose with no key name is not masked. The model writes its own agent handoff file (`<sid>-agent.md`) with the Write tool, and a hook cannot mask that write. The hooks mask every copy that they make of it, but the original file can still hold a secret. The transcripts of Claude Code are not changed. See `SECURITY.md`.
- **Platform.** The nightly jobs (index and reconcile-and-prove, opt-in with `--nightly`) are macOS LaunchAgents. On Linux you add the printed cron lines yourself. Windows is not supported. The nightly self-test mentioned above is the canary. It ships in `tools/handoff-canary/` and is opt-in (`./install.sh --canary`, about $0.03-0.10 per run).
- **AI-graded evals.** Models wrote and graded the quizzes in this README, on one user's projects. Both benchmarks are code-graded. The 19% cost estimate is a single 4.7-day, one-user figure against a modelled counterfactual, not a bill.
- The nudge works only if the model obeys it. If the model does not obey, you still get the fresh summary, which is lossier than the handoff of the model.
- The fresh summary and the decisions update each run `claude -p --model sonnet` and use some of your quota (75 s and 100 s timeouts by default).
- The decisions list needs `fcntl` (macOS and Linux). It records only what you said in chat. It does not capture a decision that you never wrote or approved.
- The nudge reads the context size from the last usage entry of the transcript, so it lags by one assistant message.
- **Window source.** If `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is not set, the nudge learns the window from past compactions, or uses 200,000 when there is no history. The nudge ignores a manual `/compact` (a boundary with trigger `manual`), because it happens at any context size. A learned window below 100,000 or above 2,100,000 is not used, and 200,000 applies. If the history comes from more than one model, the median can give a window between the models. The nudge keeps a learned value for up to 6 hours, so after a `/model` change the old window can stay for that time. Set the variable if you know your window.
- **Drift right after a compaction.** If reconcile runs in the 2 seconds after a compaction, it can show format drift until the next run.
- **Format drift alarm.** If Claude Code changes its transcript format, reconcile could count 0 compactions and 0 misses. So reconcile compares the post-compaction injects that the hooks recorded (main-session rows of `receipts.jsonl`) with the `compact_boundary` records of the same session. A session with 1 or more injects and 0 boundary records sets `format_drift: true` in `reconcile.json`. The affected `--prove` checks and claims then show `fail` with the reason `format_drift`, and `--check` prints one `format drift` line. A session whose transcript is deleted is skipped, so a deleted transcript does not raise the alarm.
- The order in which the three injected parts appear in context is not guaranteed. Each part names its number.
- No hook forces the model to run a recall search. The model chooses, so it can skip a search when it is wrongly sure. See [How Claude decides to use recall](docs/architecture.md#how-claude-decides-to-use-recall).

## Uninstall

```
./uninstall.sh
```

The script removes exactly the hook entries that install added (it matches them by command path; other hooks in those groups stay). It also removes these items, after it backs up the settings: the allow rules and the window value (if install added them), the macOS LaunchAgents (index and reconcile), the `--check` entries, and the hook files (including `recall.py`, `llm_map.py`, `claims.json` and the manifest). It also removes the recall subagent (`~/.claude/agents/recall.md`) and the llm-map skill, each only when the file holds our marker line. Add `--purge` to also delete the saved install defaults (`~/.config/lossless-handoff/install-defaults`); without it they stay. It never touches a system managed-settings file (that needs sudo). Uninstall tells you if one is present. Handoff files, receipts, proofs, indexes and the project decisions lists under `~/.local/state/claude-handoffs/` stay. Delete that folder yourself if you want them gone. The backups stay.

## More

- Architecture (parts, events, files, data flow, how Claude decides to use recall): [docs/architecture.md](docs/architecture.md).
- Reliability notes (misses, subagents, headless runs, proof, state files): [docs/handoff-reliability.md](docs/handoff-reliability.md).
- Behaviour contract that these hooks and their tests come from: [docs/spec.md](docs/spec.md).
- Benchmarks: [bench/continuity/README.md](bench/continuity/README.md) and [bench/oolong/README.md](bench/oolong/README.md).
- Decisions: [ADR 0007](docs/adr/0007-handoff-degraded-vs-reduced.md) (degraded vs reduced), [ADR 0008](docs/adr/0008-handoffs-directory-under-local-state.md) (where handoffs live), [ADR 0011](docs/adr/0011-handoff-nudge-bounded-channel.md) (the nudge).

MIT license, copyright Pratik Ghose. See `LICENSE`. Security notes: `SECURITY.md`. Changes: `CHANGELOG.md`.
