# Lossless Handoff

**Claude Code hooks that make compaction safe: a handoff before every compaction, a decisions list per project, and exact-words search over every earlier chat.**

*Formerly claude-handoff.*

[![CI](https://github.com/pratikgh0se/lossless-handoff/actions/workflows/ci.yml/badge.svg)](https://github.com/pratikgh0se/lossless-handoff/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) [![Release](https://img.shields.io/github/v/release/pratikgh0se/lossless-handoff)](https://github.com/pratikgh0se/lossless-handoff/releases/latest)

![How a handoff flows through a compaction](docs/handoff-timeline.svg)

## TL;DR

- **Without it:** every compaction turns your session into a generic summary. The exact next step, the dead ends, your standing rules and your exact wording are gone, and the model re-does work or asks again.
- **With it:** before each compaction a structured handoff is written and fed back in. Each project keeps a decisions list that survives compactions, and `recall` finds the exact words of earlier chats (keyword plus local semantic search). Python 3 standard library only.
- **Proof:** 217 compactions with a handoff over 32 days on the author's machine, 0 hook errors logged, 497 of 497 sampled messages retrieved byte-identical. Details and caveats below.

## Why this exists

I run Claude Code with a 400k auto-compact window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000`) instead of the 1M default. With 1M, long sessions rarely compact and pay for their whole context on every turn. In a single-user estimate over 4.7 days, the shorter window gave about 19% lower API-equivalent cost.

The catch is many more compactions, and each one used to lose rules, decisions and exact wording. This tool is what makes running small and compacting often safe. I use it every day, on my own projects. It has one heavy user so far (see [Limitations](#limitations)).

## Install

```bash
git clone https://github.com/pratikgh0se/lossless-handoff.git
cd lossless-handoff
./install.sh --dry-run   # show the plan, change nothing
./install.sh
```

Then restart Claude Code. Prerequisites: `python3` and `bash` on macOS or Linux (Windows is not supported), and a Claude Code version that supports `PreCompact`, `SessionStart` and `PostToolUse` hooks with `additionalContext`. Optional: the `claude` binary on `PATH` (used for the fresh summary) and [Ollama](https://ollama.com/download) (meaning search).

The installer sets up, in one go:

- the hooks and `recall.py`, copied to `~/.claude/hooks/`;
- the entries in `~/.claude/settings.json`, merged next to yours, with a timestamped backup;
- the end-of-chat index (a `SessionEnd` hook);
- on macOS, a nightly index job (03:30);
- a first index of all your projects, started in the background;
- optionally, `ollama pull bge-m3` for meaning search, if `ollama` is on your PATH.

Options: `--window N`, `--no-window`, `--yes` (pull bge-m3 without asking), `--no-nightly`, `--dry-run`, `--force`, `--version`, `--help`. Install is idempotent. Full details are in [What install changes](#what-install-changes).

## Quick start

1. Work as usual. When Claude Code compacts, the handoff and decisions list are injected automatically.
2. After a compaction, ask "what exactly did I say about X?" and Claude runs recall and quotes you.
3. Try it yourself: `python3 ~/.claude/hooks/recall.py user --last 5`

## In daily use (measured)

| What | Number |
|---|---|
| Compactions with a handoff | 217 over 32 days, across 23 sessions; the busiest session had 96 compactions |
| Handoff status | 83% ok, 15% reduced, 2% degraded; 0 hook errors logged |
| Nightly self-test | passed 31 of 34 runs; the last 7 nights all passed |
| Decisions lists | 12 projects, 495 active entries (84 standing rules), 111 finished |
| Search index | 48,282 chunks from about 30.6k messages across 1,242 sessions |
| Hook time | median 17 s (90th percentile 29 s); it always exits 0 and never blocks compaction |

Numbers are from the author's own machine, one heavy user; collected 2026-11-16.

## How word search and meaning search work together

`recall.py search` runs two searches and merges them.

- **Word search** (keyword or regex) finds exact things: commands, paths, numbers, error strings and names. It is instant and needs no setup. It uses `rg` if installed, else plain Python.
- **Meaning search** (local bge-m3 through Ollama) finds what was said in different words.
- **Fusion.** Results are merged with reciprocal-rank fusion. Each hit is tagged `[word]`, `[meaning]` or `[both]`, so you can see why it matched.

**Example.** Searching "ask the user questions in pop-ups" surfaced an earlier message, "ask me things one by one in pop ups", which shares almost no words with the query. Illustrative output in the real format (ids made up):

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

In 4 of 26 searches the best hit came only from meaning search, which keyword search alone would have missed.

Behaviour worth knowing:

- **It degrades gracefully.** With Ollama off the first line says `mode: keyword only` and search still works. In the measured sample, 10 of 28 classifiable searches ran keyword-only, at least one because the embedding service was down.
- **It is incremental.** The index is refreshed at each compaction, at chat end and nightly. None of it delays Claude Code.
- **The decisions list wins.** If recall and the decisions list disagree, or recall disagrees with your latest words, the decisions list and your latest words win. Recall can surface an older statement.

Command reference is in [Recall commands](#recall-commands).

## Proof it works

Measured on the author's machine on 2026-11-13/14, on the author's own projects. These are measurements, not promises. All tests are AI-written and AI-graded, on one user's projects.

| Test | Result |
|---|---|
| Lossless retrieval: 497 random messages from before a compaction | 497 of 497 found and returned byte-identical |
| Meaning search, easy paraphrase test, 118 queries | 98% in the top 10, 89% first |
| Blind quiz after compaction, 33 questions | 41/66 with the v0.4 injection, 50/66 with the full decisions list, 65/66 with the decisions list plus recall |
| Handoff quality, 18 real compactions (score out of 100) | Claude Code's summary alone 62; plus the model's own note 68; plus a fresh Sonnet summary 75-76; given the real recent chat 85 |
| Decisions list, 96 questions about older decisions (out of 100) | 13.5 with the 0.2.0 handoff, 56 with the decisions list |
| Continuity, first 13 real compactions | 13 of 13 had a fresh model-written note, no degraded cases |

**Lossless retrieval.** 497 randomly sampled messages (297 of yours and 200 of Claude's; fixed seed) from before a compaction, across 4 projects with up to 196 compactions each, were found by `recall.py search` and returned byte-identical by `recall.py open --full`. Mean search time 0.7 s, worst 3.1 s on a loaded machine. Limits: a sample, not every message. Messages containing secrets are redacted by design. Subagent transcripts are excluded. Messages over 40,000 characters need `--from` paging; that was added in 0.6.1 after the test found one 14,300-character message cut at the old 12,000-character output cap.

**Blind quiz.** 33 questions over 3 projects: 24 about old decisions and 9 about exact details found only in the chats. Sonnet answered from what a new chat receives, and a separate blind grader scored each answer 0 to 2. The one miss with recall was recall surfacing an older statement over a current rule, which led to the "decisions list wins" rule in 0.6.1. Limits: one run, questions written by a model.

**Handoff quality.** A fresh Claude was quizzed on what it needed to carry on (latest request, next step, decisions, dead ends, rules, files). Version 0.2.0 added the fresh Sonnet summary. Treat it as a strong hint, not proof.

**Decisions list.** By age: 1 compaction 21 to 66, 2 compactions 10 to 56, 3 or more 8 to 44. Recent memory was unchanged (62.5).

## Built on the LCM paper

Lossless Handoff is based on, and inspired by, **"LCM: Lossless Context Management"** by Clint Ehrlich and Theodore Blackman ([arXiv:2605.04050](https://arxiv.org/abs/2605.04050)). The paper's idea: when a context window fills up, keep every message verbatim in a store and give the model tools to look things up again, so compaction summarises without destroying anything.

This is an independent project. It is not written by, affiliated with or endorsed by the paper's authors, and the paper's benchmark results are theirs: nothing here repeats them as ours. The only numbers in this README are our own, from the author's machine.

| From the paper | What we took | What we did differently, and why |
|---|---|---|
| Immutable store of every message | Claude Code already keeps each message verbatim in `~/.claude/projects/<slug>/<session>.jsonl`; we use those files as the store and never write to them. | No new database for messages. Nothing to migrate, nothing to keep in sync. |
| Search and expand tools over the store | `recall.py search` finds hits, `recall.py open` expands a hit with the messages around it. | A plain command line tool, not an MCP server: it costs about 0 tokens until used. One line in the injected context tells the model it exists. |
| Summaries of older context | A fresh summary at each compaction, plus the model's own handoff. | No summary tree. Hooks cannot change how Claude Code builds its context, so we add a handoff and a decisions list next to Claude Code's own summary instead of replacing it. |
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

Other projects solve parts of this well. Stars were fetched on 2026-11-16 and move quickly. Ticks come from each project's README, not from reading the code or running it. `?` means the README does not say; "partial" means indirect.

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
- you use agents other than Claude Code, or want summaries built live during the session: volt;
- you want rules with types, approval and drift checks: look at the projects that focus on that. This tool's decisions list is append-style, without contradiction checks beyond marking an entry superseded.

**What this does differently:**

- Verbatim transcripts stay the source of truth, read as plain files, with no database server.
- Regex search and semantic search are fused over the raw transcripts, not over distilled notes.
- A structured handoff and a reasoned decisions list (what was decided, and why) in one tool.

## How it works

1. **Nudge** (`handoff-nudge.py`, PostToolUse). When context reaches 75% of the auto-compact window, and again every 25,000 tokens, one short note is appended to a tool result asking the working model to write its own handoff to `~/.local/state/claude-handoffs/<session_id>-agent.md` with seven fixed headings: Goal, Parallel work running, Decisions and why, Done, In progress, Dead ends, Exact next step. With a 400k window the reminders come at 300k, 325k and 350k, plus one final note worded "Compaction is imminent" at 355k, about 12,000 tokens below the real compaction point (window minus 33,000), so the model still has room to write the file. If the three living sections (Parallel work running, In progress, Exact next step) are byte-identical to the previous reminder, the next note says the handoff is stale and asks for a rewrite. It costs no extra turn.
2. **Handoff** (`precompact-handoff.py`, PreCompact). Just before compaction it writes `<session_id>-latest.md` (capped at 14,000 characters). It starts with an authority line that says whom to believe when sources disagree. Contents:
   - the model's own handoff, if there is one (labelled stale when out of date);
   - a fresh summary written by Sonnet (`claude -p --model sonnet`) of the conversation since the last compaction, labelled "verify against facts". It runs in parallel with the decisions update, so a compaction takes about 20-40 s;
   - a facts section extracted from the transcript without any model: your last 8 messages, files touched, task state, git branch and status, last Bash commands, last assistant message.

   It always exits 0 and never blocks compaction. If anything fails, a minimal handoff is still written.
3. **Inject** (`postcompact-inject.py`, SessionStart with matcher `compact`). After compaction it feeds the handoff back into the new context. Claude Code caps each hook's injected text at 10,000 characters, so it runs three times (`--part 1/2/3`), splitting on section boundaries and always naming the full file path. Three more runs (`--ledger --part 1/2/3`) inject the decisions list. A stale agent handoff is still included, preceded by a warning with the reason.

**Who wins when sources disagree:** the user's latest words, then the project decisions, then the agent handoff, then the fresh summary, then Claude Code's own summary, then the extracted facts. If unsure, the model should ask.

**Works without the model's cooperation.** If the model never writes the agent file, you still get the fresh Sonnet summary plus the facts (status `reduced`). If the summary also fails or is disabled, you get the facts only, and the injected text starts with a `HANDOFF DEGRADED` warning line.

## The project decisions list

A handoff is per session, but decisions belong to the project. One plain-text list is kept per project (a git repo, or the folder you started in when there is no repo) at `~/.local/state/claude-handoffs/projects/<project>-<hash>/decisions.md`. One line per decision: an id (`D12`), the date, a kind, what was decided, and why.

- **Kinds.** `[rule]` (a standing rule or working preference), `[decision]` (a choice) or `[task]` (a follow-up you asked for). Older untagged lines count as decisions.
- **What goes in.** Only things you decided or explicitly approved: a choice, a rule, a constraint, a permission, a rejected option. Not plans, not progress, not the assistant's own unapproved ideas.
- **Kept up to date.** At every compaction a second Sonnet call reads the conversation since the last update and the current list, and returns the updated list. When a task is finished the line is marked `RESOLVED` and is no longer injected. Rules are never resolved by the model.
- **Never deleted.** When you change your mind, the old decision is marked `SUPERSEDED by D<n>` with the reason and stays in the file. Before each update the previous list is saved as `decisions.<timestamp>.md` (the newest 20 are kept).
- **Code checks the model.** Sonnet proposes, plain code decides: an entry the model drops is put back, a superseded entry is never revived, existing ids never change, over-long lines are shortened rather than dropped, and a result with no valid line is thrown away. A project lock keeps two compactions in the same repo from overwriting each other.
- **Injected after compaction and in new chats.** Three parts, rules first and always in full, then decisions, then tasks, newest first. What does not fit in 3 x 10,000 characters becomes one-line index entries, so nothing is silently dropped. The same parts are injected at `startup` and `resume`, so a fresh session does not re-ask what you already settled.
- **Tidying an old list.** `python3 ~/.claude/hooks/precompact-handoff.py --ledger-maintain <repo>` runs one maintenance pass: it tags untagged entries, resolves finished tasks and supersedes contradictions. It adds no entries and edits no text.

Turn it off with `HANDOFF_LEDGER=0`. Existing lists are never deleted by turning it off or by uninstalling.

## Recall commands

`recall.py` searches every earlier chat in the same project (all sessions whose folder is inside the project's git root), read-only, over the transcripts Claude Code already keeps. A summary keeps decisions but loses exact commands, numbers, paths and your own wording; this gets them back.

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

Every command accepts `--cwd DIR` (default: the current folder). `search` also takes `--session SID8|all`, `--limit N` and `--subagents`. `--keyword` is words only, `--meaning` is the embedding index only. Output is capped at 12,000 characters, and obvious secrets (API keys, tokens, `password=...`) are replaced by `[redacted]` before printing or embedding. `open <id> --full` returns up to 40,000 characters; a longer message ends with a line giving the exact `--from <N>` to continue with.

**Meaning search setup.** It needs a local embedding model: [Ollama](https://ollama.com/download) with `ollama pull bge-m3`. If `ollama` is on your PATH the installer offers to pull it (`--yes` says yes; a non-interactive run skips it and prints the command). If Ollama is not installed, the installer prints a hint and carries on. The installer never installs Ollama itself. Backend settings are `RECALL_EMBED_*` in [Configuration](#configuration).

## What install changes

- Copies `precompact-handoff.py`, `postcompact-inject.py`, `handoff-nudge.py` and `recall.py` to `~/.claude/hooks/` (a differing existing file is first saved to `~/.claude/hooks/.claude-handoff-backup-<timestamp>/`).
- Merges into `~/.claude/settings.json` (backup `settings.json.bak-<timestamp>`):
  - a PreCompact hook (timeout 150) and a PostToolUse hook (matcher `*`, timeout 5);
  - a SessionStart `compact` group with the three inject commands and the three `--ledger --part 1/2/3` commands (timeout 10), and `startup` and `resume` groups with the three `--ledger` commands;
  - a `SessionEnd` hook running `recall.py session-end` (timeout 5);
  - `permissions.allow` entries `Edit(~/.local/state/claude-handoffs/**)` (so the model can write its handoff without a prompt) and `Bash(python3 ~/.claude/hooks/recall.py:*)` (so it can run recall without a prompt);
  - `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` = `"400000"` unless already set (`--window N` picks another value, `--no-window` leaves it alone).
- Creates `~/.local/state/claude-handoffs/`.
- Optionally runs `ollama pull bge-m3`.
- On macOS, unless `--no-nightly`: writes the LaunchAgent `~/Library/LaunchAgents/com.lossless-handoff.recall-index.plist` (daily 03:30, runs `recall.py index --all --max-seconds 3000`, log `~/.local/state/claude-handoffs/recall/index-all.log`) and loads it. On Linux it prints the equivalent crontab line and does not edit your crontab.
- Starts `recall.py index --all` in the background.

Your other hooks and settings are left alone: new entries are appended next to existing ones. If the same hook scripts are already registered from another path, install prints those commands and exits 2, because every hook would run twice; remove them, or pass `--force`. Install fails with a clear message if `python3` is missing or `settings.json` is not valid JSON.

## What gets written where

| Path | Content |
|---|---|
| `~/.local/state/claude-handoffs/<sid>-latest.md` | The handoff that is injected after compaction. `<sid>-<timestamp>.md` is a copy. |
| `<sid>-agent.md` | The model's own handoff (written by the model). |
| `<sid>-agent.meta.json` | Nudge state and timestamps (numbers only, no text). |
| `recall-index.json`, `recall/` | Recall caches: which transcripts belong to which project, and the meaning index (`<project>.sqlite`, `index.log`). Safe to delete. |
| `health.jsonl` | One JSON line per compaction: time, trigger, agent file fresh/stale/absent, fallback result, ledger result (`ok`, `unchanged`, `failed`, `locked`, `disabled`) and counts, duration, size, status `ok` / `reduced` / `degraded`. No message text or paths. Rotates at 5,000 lines. |
| `<sid>-last-health.json`, `<sid>-degraded.json` | Per-session status and degraded reasons. |
| `projects/<project>-<hash>/decisions.md` | The project's decisions list (kept by uninstall). Beside it: `decisions.<timestamp>.md`, `decisions.state.json`, `decisions.lock`. |

Files older than 14 days are cleaned up automatically. The directory is outside `~/.claude/` because Claude Code refuses model writes under that path ([ADR 0008](https://github.com/pratikgh0se/machine-setup/blob/main/docs/adr/0008-handoffs-directory-under-local-state.md)).

## Verify

```
claude -p --model haiku "say hi" --output-format json   # note the session_id
claude -p --model haiku --resume <session_id> "/compact"
tail -n 3 ~/.local/state/claude-handoffs/health.jsonl
```

A new line with `"trigger": "manual"` means the PreCompact hook fired. In a normal session, run `/compact` and look at the same file. `status` is `ok` (model handoff used), `reduced` (fresh summary used instead) or `degraded` (facts only). You can also run `bash tests/smoke.sh`; it uses a temporary HOME and does not touch your real settings.

## Configuration

| Variable | Effect |
|---|---|
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | Window size in tokens. Nudge thresholds are 75% of it, then every 25,000, then a final note (window minus 45,000). Default 400000 if unset. |
| `HANDOFF_SUMMARY=0` | Disable the fresh summary at compaction (no model call; facts only). |
| `HANDOFF_SUMMARY_MODEL` | Model for the fresh summary. Default `sonnet`. |
| `HANDOFF_SUMMARY_TIMEOUT` | Seconds to wait for the summary (1-300). Default 75. |
| `HANDOFF_LEDGER=0` | Disable the decisions list: no merge call, nothing written, nothing injected. |
| `HANDOFF_LEDGER_MODEL` | Model for the decisions update. Default: the summary model. |
| `HANDOFF_LEDGER_TIMEOUT` | Seconds to wait for the decisions update (1-300). Default 100. The PreCompact hook timeout is 150. |
| `HANDOFF_HAIKU_FALLBACK=0` | Legacy switch from 0.1.x; still honoured and also disables the fresh summary. |
| `RECALL_EMBED_URL`, `RECALL_EMBED_MODEL`, `RECALL_EMBED_BACKEND`, `RECALL_EMBED_KEY` | Meaning search backend. Defaults: Ollama at `http://localhost:11434`, model `bge-m3`. Backend `openai` works with any `/v1/embeddings` server; the key is an optional Bearer token. |
| `HANDOFF_STATE_DIR` | Use another directory instead of `~/.local/state/claude-handoffs`. |
| `HANDOFF_HOOK_CHILD` | Set by the hook for its own summary call so it never recurses. Do not set it yourself. |

## FAQ

**Does it cost tokens?** About one short note per nudge, plus up to about 14,000 characters injected once per compaction and up to about 20,000 characters of decisions. Each compaction adds two `claude -p` Sonnet calls (fresh summary and decisions update; 20-40 s in parallel, roughly $0.10-0.20 of API-equivalent usage). Set `HANDOFF_SUMMARY=0` and `HANDOFF_LEDGER=0` to avoid them.

**Does it work in the desktop app?** Yes. The desktop app runs the same hooks from `~/.claude/settings.json`.

**Does it work with my existing hooks?** Yes. Install merges into your existing `hooks` groups and never removes or reorders yours; uninstall removes only its own commands.

**Where must `CLAUDE_CODE_AUTO_COMPACT_WINDOW` be set?** In your user `~/.claude/settings.json` under `env` (install does this) or exported in the shell that starts Claude Code. A live test showed `env` in a project's `.claude/settings.json` did not override it.

**What window size should I use?** One that comfortably exceeds your baseline context. Very small windows (below roughly 150k) compact before the 75% nudge is reached; the PreCompact hook then covers it. Savings come from long sessions on 1M-context models; if your sessions rarely pass 300k tokens the window never triggers and you mainly get the handoff. Whether a shorter context also improves answer quality was not measured. See [ADR 0001](https://github.com/pratikgh0se/machine-setup/blob/main/docs/adr/0001-compact-window-400k-with-handoff.md).

**Can the nudge be skipped?** Yes. It reads the previous turn's token count, so one very large tool result can jump past the threshold without a nudge. The PreCompact hook still writes a handoff.

**Does it send anything over the network?** The scripts do not, except meaning search, which sends message text to the embedding server you configure (by default Ollama on your own machine). The two per-compaction calls run your local `claude -p`. See `SECURITY.md`.

## Troubleshooting

- Logs and health live in `~/.local/state/claude-handoffs/` (or `$HANDOFF_STATE_DIR`). Start with `tail -n 5 ~/.local/state/claude-handoffs/health.jsonl`: one line per compaction with `status` `ok`, `reduced` or `degraded`.
- `<sid>-degraded.json` and `<sid>-last-health.json` in the same folder say why a compaction was degraded.
- No health line after `/compact`: restart Claude Code (hooks load at startup), then check `~/.claude/settings.json` has the `PreCompact` entry and that `~/.claude/hooks/precompact-handoff.py` exists.
- No nudge: it only fires above 75% of the window (300k for 400k) and needs a usage entry in the transcript; check `CLAUDE_CODE_AUTO_COMPACT_WINDOW`.
- Search says `keyword only`: Ollama is not running or `bge-m3` is not pulled. Start Ollama and run `ollama pull bge-m3`.
- Settings or hooks in a bad state: install and uninstall leave timestamped backups (`settings.json.bak-*`, `~/.claude/hooks/.claude-handoff-backup-*`).
- Something else: run `bash tests/smoke.sh`, then open an issue using the bug report template.

## Limitations

- **One heavy user so far.** All numbers come from the author's machine. Whether it helps other workflows is untested.
- **Handoffs can be cut.** 20% of handoffs (43 of 217) hit the 14,000-character size cap and were truncated. 15% were `reduced` and 2% `degraded`, and the nightly self-test failed 2 of 34 runs.
- **Recall is new.** It has existed since 2026-11-12, and its use so far is mostly the author's, much of it while building and testing it. The meaning half is a minority of wins (4 of 26 searches had a meaning-only top hit), and 143 meaning hits count lines shown, not proof they were useful.
- **Platform.** The nightly index job is a macOS LaunchAgent; on Linux you add the printed cron line yourself. Windows is not supported. The nightly self-test mentioned above is part of the author's own setup and is not included in this repo.
- **AI-graded evals.** The quizzes and benchmarks were written and graded by models, on one user's projects. The 19% cost estimate is a single 4.7-day, one-user figure against a modelled counterfactual, not a bill.
- The nudge only works if the model obeys it. If it does not, you still get the fresh summary, which is lossier than the model's own note.
- The fresh summary and the decisions update each run `claude -p --model sonnet` and use some of your quota (75 s and 100 s timeouts by default).
- The decisions list needs `fcntl` (macOS and Linux) and only records what you said in chat; a decision you never wrote or approved is not captured.
- The nudge reads context size from the transcript's last usage entry, so it lags by one assistant message.
- The order in which the three injected parts appear in context is not guaranteed; each part names its number.

## Uninstall

```
./uninstall.sh
```

Removes exactly the hook entries install added (matched by command path; other hooks in those groups stay), the allow rules and the window value if install added them, the macOS LaunchAgent, and the hook files including `recall.py`, after backing up settings. Handoff files, indexes and the project decisions lists under `~/.local/state/claude-handoffs/projects/` stay; delete that folder yourself if you want them gone. Backups are kept.

## More

- Behaviour contract for these hooks: `specs/handoff-hooks-spec.md` in [machine-setup](https://github.com/pratikgh0se/machine-setup).
- Decisions: [ADR 0007](https://github.com/pratikgh0se/machine-setup/blob/main/docs/adr/0007-handoff-degraded-vs-reduced.md) (degraded vs reduced), [ADR 0011](https://github.com/pratikgh0se/machine-setup/blob/main/docs/adr/0011-handoff-nudge-bounded-channel.md) (the nudge).

MIT license, copyright Pratik Ghose. See `LICENSE`. Security notes: `SECURITY.md`. Changes: `CHANGELOG.md`.
