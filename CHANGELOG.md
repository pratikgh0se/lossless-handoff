# Changelog

Lossless Handoff, formerly claude-handoff.

## [Unreleased]

### Docs

- Correct the benchmark claims after the 6-seed fair replication (run `20261128T002104-fair6-step1`, verdict INVALID): plain Claude Code scored 186 of 200 (93%) with a hint, so we claim no gain there; 117 of 160 (73%) without a hint, against 40 of 40 for Lossless Handoff on 2 seeds only. Remove the 82% and 100% badges, add the section "Fair replication, 6 seeds (2026-11-28)" to `docs/evidence.md`, and update claim C11. The earlier 82% against 100% numbers overstate the gap.

## [0.23.0] - 2026-11-26

A live canary, a shape census and a mutation gate (hooks 1.25.0). Spec: F91 in `docs/spec.md`.

### Added

- `--check` reports `proof: <k> failed checks in the last 7 days (latest: <check> at <ts>); see precompact-handoff.py --prove --days 7` when `proofs.jsonl` has a failed check in the last 7 days. It is a data problem. The once-only `proof failed:` notice stays; when both apply, the line holds both texts. `--check --verbose` adds the 3 latest failures (detail cut to 100 characters, after `redact()`) and the count of samples.
- A failed prove check writes a structure-only sample to `<state>/samples/<ts>-<check>.json` (mode 0600): record types, sorted keys, content forms, text lengths and a reason code. No text, ids, paths or values. At most 50 samples stay.
- `precompact-handoff.py --shape-census [--days 30]` counts record types, top-level keys, content block types and tool-result forms in your transcripts. It writes `shapes.json` and, on the first run, `shapes-baseline.json`, and prints `new record type:` and `new key:` lines for names that are not in the baseline. It makes no model call and prints names only. `--check --verbose` prints `info: transcript shapes: <n> new since <date>`.
- The nightly job (`--nightly`) runs the census after the prove run.
- `tools/mutation/`: about 20 seeded faults and `run.sh`, a manual or weekly gate that measures whether the tests catch faults. CI does not run it.

### Tests

- Mutation gate: replace 3 faults that changed nothing visible (M10 file mode under `umask 077`, M16 fallback redaction, M18 a limit that never binds) with visible faults of the same type. Add `test_mutation_gaps.py` to their headers. Score: 20/20. `tests.ref` points at the new code-blind tests (state modes under umask 022, redaction twice gives the same text, parts within the hard max with no lost line).

### Docs

- Add the fair re-test result to the README and `docs/evidence.md` (claim C11): 2 seeds, 10 compactions. Plain Claude Code that can search its own chat files scored 41 of 50 (82%) with a hint and 29 of 40 (72%) without. Lossless Handoff scored 50 of 50 and 40 of 40. Two seeds show a direction, not a proof. The earlier 58% against 100% run stays as a line about a blocked baseline.
- Add the F91 checks to the README: `--check` reports failed proof checks, a nightly shape census, and the mutation gate score (20 of 20, 2026-11-26). Add the section "Mutation gate (2026-11-26)" to `docs/evidence.md`.

## [0.22.0] - 2026-11-25

One grammar for each shared format (hooks 1.24.0). Spec: F90 in `docs/spec.md`.

### Added

- `hooks/lossless_handoff/grammar.py`: the one parser for a decisions list line (G1) and for a recall hit id (G2). `ledger.py`, `postcompact-inject.py`, `recall.py` and `prove.py` use it and keep no pattern of their own for these formats. The two hooks that run without the package keep a copy that a smoke check compares with the file.
- `--check` reports `decisions list: <k> malformed lines (first: line <i>)` for the project of the current folder. `--check --verbose` also shows the first 3 malformed lines, cut to 120 characters, after secrets are masked.
- A `VERSION` file in the repo root (`product=` and `hooks=`). Smoke checks read it, and fail when `install.sh`, a `HOOK_VERSION`, the README version line or the top changelog entry differs.
- Smoke negative controls: the proof check, `--check` and the claims lint each fail on a known-bad sample.

### Changed

- A line of the decisions list that is outside the known forms (for example `- D9 [date] RETIRED [date] (reason): [rule] text`) is no longer read as an active entry. It is not injected, it is not counted as active, and a merge writes it back where it was. Before, a merge dropped such a line. A legacy active line with no `| why:` stays valid, as before.
- Part 1 of the injected decisions list says: `This list is written by the hooks; do not edit decisions.md yourself.` The sentence is at the end of the existing header line of part 1, on the same line, so no new line comes before the entries.
- The text of every active line (after the tag, if any) must not start with `SUPERSEDED` or `RESOLVED`, or with 2 or more capitals followed by ` [`, `:` or ` YYYY-MM-DD`. So a superseded line with no `| because:` (for example `- D49 [2026-10-27] SUPERSEDED by D154 [2026-11-02] (reason): text`) and a task line such as `- D311 [2026-11-15] [task] DONE 2026-11-15: text` are malformed, not active. Text such as `README is x` or `OOLONG runs x` stays valid.

### Fixed

- `recall.py open <id> --full` removed trailing whitespace from message text (for example `delta \t ` came back as `delta`). It now keeps the text byte for byte after `redact()`. Search snippets are unchanged.

## [0.21.1] - 2026-11-25

End a rule with your own words (hooks 1.23.1). Spec: F89 in `docs/spec.md`.

### Changed

- The decisions list merge may now resolve a `[rule]` entry, but only when the merge model quotes (8 to 300 characters) your own words from the new conversation that end the rule. The code checks that the quote is in one of your messages (tool results and assistant text do not count; case and spacing are ignored). Without a valid quote, the rule stays active. The health row has two new counters, `ledger_rule_resolved` and `ledger_rule_resolve_rejected`. `--ledger-maintain` still never resolves a rule.

## [0.21.0] - 2026-11-25

Decisions list at scale (hooks 1.23.0). Spec: F88 in `docs/spec.md`. Restart Claude Code after the install: the installer adds a `clear` entry to `settings.json`.

### Added

- F88.a: `recall.py decisions [<pattern> ...] [--kind rule|decision|task] [--closed]` prints full lines of the decisions list. A bare `D12` names one entry. It makes no model call and writes no file.
- F88.c: a subagent gets the full text of the newest rules up to 4,000 characters, then an index line for each other rule (9,900 characters at most). The health row has `rules_index`.
- F88.d: `--check --verbose` prints an `info: decisions list` line. `--check` reports rules that use 90% of the room or more, and rules that are index lines only.
- F88.e: the installer registers the three `--ledger --part N` commands for the SessionStart matcher `clear`; `uninstall.sh` removes them; `--check` reports a missing `clear` entry as an install problem.

### Changed

- F88.b: the index note and the count line of the decisions list name `recall.py decisions`. The `[more rules: ...]` line names `recall.py decisions --kind rule`.

## [0.20.2] - 2026-11-25

Safety fixes (hooks 1.22.2). Spec: F87 in `docs/spec.md`.

### Changed

- F87.a: inside a chat (`CLAUDE_CODE_SESSION_ID` set and its transcript found), `recall.py` reads only the project of that session. `--cwd` for another project exits 2. Run recall in a terminal to read another project.
- F87.b: `redact()` masks 8 more kinds: `stripe`, `gitlab`, `huggingface`, `telegram`, Slack webhook URLs, `cookie`, `cli` (mysql `-p`, `curl -u`) and `password is <value>` prose.
- F87.c: the installer sets mode 0700 on the state folder and the folders below it; hooks create state folders with mode 0700; `--check` reports a state folder that is open to other users.
- F87.d: each receipt row has `health_ts`. A handoff that a receipt shows as already served for more than 60 seconds is handled as old: recovery first, then the `HANDOFF DEGRADED` line.

### Docs

- Qualify the 58% against 100% benchmark numbers: plain Claude Code was blocked from reading its own chat files in that test. A fair re-test is planned. Fix the product version in `docs/architecture.md`.

### Rollback

Run `git checkout v0.20.1` and `./install.sh` again. Folder modes stay 0700; no data changes.

## [0.20.1] - 2026-11-25

Faster recall on large projects and a check that gives correct advice (hooks 1.22.1). Spec: F85 and F86 in `docs/spec.md`.

### Changed

- Recall parses only the candidate lines that `rg` names and finds its own tool calls with the same `rg` pass. A keyword search on a 4,064-session project takes 9.7 s instead of 19.3 s (warm scope cache). Output is byte-identical.
- The lossless check of `--prove` searches one transcript (`--session`) with a 60 s limit per recall call (`HANDOFF_PROVE_RECALL_TIMEOUT` changes it).
- `--check` separates install problems (installer advice) from data problems (no installer advice). It tests the recall file and the recall allow rule.

### Added

- `RECALL_STATS=1` prints a counter line on stderr.
- `--check --verbose` prints one line for each check and ends with a summary. The installer prints this command as its last line.

## [0.20.0] - 2026-11-25

Batch recall calls (hooks 1.22.0). Spec: F84 in `docs/spec.md`.

### Added

- `recall.py open <id> [<id> ...]` takes 1 to 10 ids and `recall.py search <pattern> [<pattern> ...]` takes 1 to 5 patterns in one call. A loop or a shell variable is not needed, and Claude Code blocks these in headless mode.
- An exit-2 error of `recall.py` prints one `usage:` line on stderr.

### Changed

- The recall line (PreCompact handoff, `--ledger --part 1`, SessionStart, SubagentStart), the recall MCP `instructions` and `agents/recall.md` say that one call takes several patterns or ids.
- One id or one pattern gives the same output as before.
- README version line fixed (it named 0.19.1 and 1.21.1).

## [0.19.2] - 2026-11-25

Lossless check fix (hooks 1.21.2). Amendment (a) in the proof section of `docs/spec.md`.

### Fixed

- The lossless check in `--prove` now reads both recall label forms, `<session8>:<line>` and `<parent8>/<agent8>:<line>`, and opens the hit with the same label. Before, every subagent receipt failed with "recall found no hit". All 11 live failures of 2026-11-18 to 2026-11-24 were of this kind. The 26 main-chat checks passed. Old rows in `proofs.jsonl` stay as they are.
- A recall call that times out in the lossless check gives `skip` with the reason `timeout`, not `fail`.

### Changed

- Benchmark: the hooks copy of each arm moves to `<base>/lhtools-<run>-sN-<arm>/hooks`, outside the state folder, so no deny rule matches a recall command in any form (`cd`, loop, `ls; ...`). Result rows hold `bash_denials` (our rules) and `bash_native_blocks` (Claude Code itself), the report totals them per arm, and the selfcheck `recall-forms` fails only when our deny rules block a recall form. `run.sh cleanup` also deletes `lhtools-*`.
- Benchmark: new preset `proof-recheck` re-measures arm B on the proof seeds, with no PASS or FAIL. Arm B now gets the `.recall-rules` marker when the checkout has F81.

## [0.19.1] - 2026-11-24

Recall robustness (hooks 1.21.1). New F83 in `docs/spec.md`.

### Changed

- `recall.py search` run with no `--cwd` from the hooks folder uses the previous folder (`OLDPWD`) as the project.
- A search that covers 0 sessions prints a warning line with the full command.
- The recall line says "same full path ... do not cd". The rule line is new: run the recall command, copy it exactly, run a failed or denied command again, and say so if no search can run.
- The MCP sentences appear only when the `lossless-recall` server is registered in `~/.claude.json`. The rule line then ends with `If Bash is not allowed, call search_earlier_chat.`
- Docs name the allow rule `Bash(python3 ~/.claude/hooks/recall.py:*)` (F44.7) and say what it does. The installer is unchanged.

## [0.19.0] - 2026-11-23

Recall rule on by default (hooks 1.21.0). New F81 in `docs/spec.md`.

### Upgrade notes

- A default install now writes the marker file `~/.claude/hooks/.recall-rules`. The rule line follows the recall line in every session start and subagent start.
- To turn it off, run `./install.sh --no-recall-rules --save-defaults`. This saves `recall_rules=0` and removes an installed marker (the installer prints `recall-rules: removed`). `HANDOFF_RECALL_RULES=0` turns the line off at run time.
- Without `--save-defaults`, a rerun never removes an installed marker. `--recall-rules` stays valid and changes nothing.
- Restart open Claude Code sessions after the install.

### Changed

- F81: the rule line now also says: if a value is not in your context, or a file no longer holds it, search before you answer "unknown". In the proof test, Claude answered "unknown" for exact strings that a search would have found. The benefit is not measured yet.
- F64: the default install now includes the recall rule.

### Added

- Docs: the result of the proof test (2026-11-23, 4 seeds, $149.50). Plain Claude Code scored 100%, 80% and 58% after 1, 3 and 10 compactions. Lossless Handoff scored 100% each time. The README Benchmarks section, `docs/proof/benchmarks/2026-11-23-proof-test.md` and the claims register (C9) have the numbers and the limits.
- Benchmark: the proof test, `bench/continuity/run.sh proof`. It uses 4 new seeds, the arms A, B, O and Z, and N = 1, 3 and 10, for about $165. The pass rule (P1 loss, P2 fix, validity gates) is in `thresholds.json`, and the report starts with a "Proof verdict" and side-by-side examples. The full suite is future work.

### Fixed

- State files now have mode 0600 (folders 0700). Before, the handoff files and the decisions list were mode 644, so other local users could read them. The three hooks set `umask 077`, and `write_atomic` makes its temp file 0600. `install.sh` sets existing files in the state folder to 0600. Content, paths and hook behaviour are the same.
- The decisions list merge keeps one cursor for each session (`cursors` in `decisions.state.json`). Before, one cursor served the whole project. If chat X compacted first, chat Y lost its earlier decisions. An old state file works: its project cursor applies only to the session in `lastSid`. `lastEntryTs` and `lastSid` are still written. At most 200 cursors are kept.

## [0.18.0] - 2026-11-22

History guard, action guard and size trigger (hooks 1.20.0). New F77, F78 and F80 in `docs/spec.md`. All three are opt-in.

### Upgrade notes

- The default install is the same as before. Add the parts with `./install.sh --history-guard`, `--action-guard` and `--recall-size-trigger` (or save them with `--save-defaults`). `--recall-size-trigger` needs `--auto-recall`.
- A rerun never removes an installed part. The installer prints `history-guard: already installed, kept`, `action-guard: already installed, kept` or `recall-size-trigger: already installed, kept`.
- The guards start in shadow mode: they write a log line (no text) and change nothing. Set `HANDOFF_HISTORY_GUARD=block` or `HANDOFF_ACTION_GUARD=inject` to let them show the history to the model.
- Restart open Claude Code sessions after the install.

### Changes

- F77: new Stop hook `history-guard.py`. When the context is 100,000 tokens or more and the reply uses a key that changed in this chat, it gives the model the full history of that key (the F73 blocks) as Stop `additionalContext`, once per user turn.
- F80: new PostToolBatch hook `action-guard.py`. It scans the new text of Edit, Write, MultiEdit and NotebookEdit and the Bash command, and injects the same history before the next model call. It never blocks a call.
- F78: with the marker `.recall-size-trigger`, auto-recall also fires with no cue phrase when the context is 100,000 tokens or more and the prompt names a key that changed in this chat.
- The guards count the turn in progress (so a key changed in a long turn is a subject) and the history block includes it. Shadow mode saves the cooldown. The state file holds key hashes. The lookup scope is narrowed to the project folders.
- New shared file `guard_common.py` (subject, evidence, cooldown and state rules) so F77, F78 and F80 do not drift. F77 and F80 share one cooldown table.

## [0.17.0] - 2026-11-22

Key history and retrieval fixes (hooks 1.19.0). New F73 to F75 in `docs/spec.md`.

### Upgrade notes

- The default install is the same as before. Auto-recall and the recall tool stay opt-in.
- Restart open Claude Code sessions after the install.

### Changes

- F73: new `recall.py history <identifier> [--role] [--all]` and the MCP tool `key_history`. Every mention of one identifier as a whole token, oldest first, as sentence windows. Output cap 4,000 characters (middle cut).
- F74: `recall.py search` and `search_earlier_chat` leave out the tool calls and results of recall itself, auto-recall injections and duplicates (a fork copies its parent history). The MCP tool also leaves out the current prompt. Hits that match as whole tokens rank above substring hits.
- F75: auto-recall builds the history of the first 3 identifiers of the prompt (1,500 characters each, 4,000 in total). Without an identifier it uses the word search with a stop list of generic words. The health row has `mode` and `identifiers`.
- The MCP server instructions and the tool descriptions name `key_history`.

## [0.16.0] - 2026-11-22

Four opt-in search levers: a strict recall rule line, a recall MCP tool, a re-anchor hook and a claim guard (hooks 1.18.0). New F68 to F72 in `docs/spec.md`.

### Upgrade notes

- The default install is the same as before. All four levers are off. Add them with `./install.sh --recall-rules`, `--recall-mcp`, `--reanchor` and `--claim-guard` (or save them with `--save-defaults`).
- `--no-recall-rules` and `--no-recall-mcp` skip a part for one run. A rerun never removes an installed part. The installer prints `recall-rules: already installed, kept` or `recall-mcp: already installed, kept`.
- `--recall-mcp` runs `claude mcp add --scope user lossless-recall -- python3 <hooks dir>/recall-mcp.py`. If `claude` is not on the PATH, the installer prints the command and still installs the file. `./uninstall.sh` runs `claude mcp remove lossless-recall --scope user`.
- No lever forces a search.
- Restart open Claude Code sessions after the install.

### Changes

- F68: with the marker file `~/.claude/hooks/.recall-rules`, the recall line is followed by one rule line in every session start and subagent start. `HANDOFF_RECALL_RULES=0` switches it off. Inject rows in `receipts.jsonl` have `recall_rules`.
- F69: new `hooks/recall-mcp.py`, a stdio MCP server (name `lossless-recall`) with the tools `search_earlier_chat` and `open_message`. Secrets are masked, a result has at most 12,000 characters, and `recall-mcp.jsonl` holds no query text. The recall line names the tool when it is installed.
- F72: `HANDOFF_HOOKS_HOME` replaces `~/.claude/hooks` for every hook and helper when it names an existing folder. No file is read from `~/.claude/hooks` then, and no copy next to the hook is used.
- F69: a tool result is at most 12,000 characters in total, including the cut marker (it was up to 12,026).
- Review fixes (F68 to F72):
  - Re-anchor holds a lock from the anchor read to the anchor save, so parallel tool calls inject once. Its rules block has no SubagentStart preamble, and a zero-usage entry is skipped.
  - Claim guard: a `<task-notification>`, `<bash-input>`, `<bash-stdout>` or `<local-command-` entry and a compact summary are not user messages. An auto-recall injection before the last user message is no evidence.
  - Recall tool: the project comes from `CLAUDE_PROJECT_DIR` (else the working directory), a query that starts with `-` is text, and the fallback protocol version is `2025-11-25`. A rerun with the server already registered prints `recall-mcp: already installed, kept` and skips `claude mcp add`.
  - A default install no longer treats a foreign `reanchor.py` or `claim-guard.py` hook as a conflict (as on 0.15.0).
  - `HANDOFF_HOOKS_HOME` also sets the path in every printed recall command (recall line, summary node line, auto-recall footer) and the folder of the recall subagent file (`<folder>/agents/recall.md`).
  - With the `.recall-rules` marker, session start prints the recall line and the rule line also when the project has no decisions list.
  - New state files (`recall-mcp.jsonl`, `claim-guard.jsonl`, `reanchor-*.json`, `reanchor-*.lock`) have mode 0600.
- All hook versions are 1.18.0. Product version 0.16.0.
### Re-anchor and claim guard (opt-in)

- New hook `hooks/reanchor.py` (`PostToolUse`, `--reanchor`, hooks 1.18.0). Each 50,000 tokens of context growth it repeats the active rules of the project. `HANDOFF_REANCHOR=0` and `HANDOFF_REANCHOR_TOKENS` control it. New mode `postcompact-inject.py --rules-block`. New F70 in `docs/spec.md`.
- New hook `hooks/claim-guard.py` (`Stop`, `--claim-guard`, hooks 1.18.0). It logs replies that state an earlier fact without a recall search (shadow mode, default). `HANDOFF_CLAIM_GUARD=block` makes it ask for a search once per turn. New F71 in `docs/spec.md`.
- Saved keys `reanchor` and `claim_guard`. A rerun keeps an installed hook and prints `reanchor: already installed, kept` or `claim-guard: already installed, kept`. `./uninstall.sh` removes both.

## [0.15.0] - 2026-11-20

Auto-recall: an opt-in hook that searches the earlier chats when your prompt refers to the past (hooks 1.17.0). New F67 in `docs/spec.md`.

### Upgrade notes

- The default install is the same as before. Auto-recall is off. Add it with `./install.sh --auto-recall` (or save it with `--save-defaults --auto-recall`).
- `--no-auto-recall` skips it for one run. A rerun never removes an installed auto-recall. The installer prints `auto-recall: already installed, kept`.
- `./uninstall.sh` removes `auto-recall.py` and its `UserPromptSubmit` entry in `settings.json`. It keeps `auto-recall.jsonl`.
- Restart open Claude Code sessions after the install. Hooks load when a session starts.

### Changes

- New hook `hooks/auto-recall.py` (`UserPromptSubmit`, timeout 5). It acts only when the prompt does not start with `/` and refers to the past (30 phrases such as `what did we decide` and `you mentioned`, matched as whole words, or a double-quoted phrase of 3 or more words; the bare words `before`, `earlier` and `remember` are not cues). It searches the transcripts of the project with the code of `recall.py` (word, plus meaning when the index exists), and prints the top 3 hits (300 characters each, secrets masked) with the exact first and last line in F67.c.
- Time budget 2.0 seconds for the whole hook. On a timeout, no hits, fewer than 2 query words, a missing `recall.py` or any error: no output and exit 0.
- Cue change (2026-11-22): the cue list has 30 phrases, matched as whole words. The bare words `before`, `earlier`, `remember`, `did i`, `did we` and `what was the` no longer fire. The health line has the field `cue`.
- Hits: only user and assistant messages count (tool results no longer push real hits out); the current prompt is excluded by the file of the current transcript, also for a multi-line prompt; the log file has mode 0600.
- `HANDOFF_AUTO_RECALL=0` switches it off at run time.
- Each run that passes the trigger appends one line to `~/.local/state/claude-handoffs/auto-recall.jsonl` (`ts`, `session_id`, `fired`, `cue`, `hits`, `ms`, `timed_out`). The line holds no prompt text and no quotes.
- Docs: README, `docs/install.md`, `docs/configuration.md`, `docs/recall.md` (how it decides and its limits), `docs/architecture.md` and its diagram, `SECURITY.md`.
- `HOOK_VERSION` is `1.17.0`; the product version is `0.15.0`.

## [0.14.0] - 2026-11-20

llm_map moves to its own project, subagents get a rules preamble, and the evidence wording is honest (hooks 1.16.0). New F66 in `docs/spec.md`. F60 and F61 are retired there ("moved to the llm-map project"; the numbers stay reserved).

### Upgrade notes

- llm_map moved to https://github.com/pratikgh0se/llm-map. This repository no longer ships `hooks/llm_map.py` or the llm-map skill, and `install.sh` no longer installs them.
- An existing install keeps its `llm_map.py` and skill. The installer does not change or remove them, and prints `llm-map: already installed, kept (now maintained at https://github.com/pratikgh0se/llm-map; this installer no longer updates it)`. `./uninstall.sh` never removes them, because the llm-map project uses the same paths. If one exists, it prints `llm-map: kept (remove it with ./install.sh --uninstall from https://github.com/pratikgh0se/llm-map)`.
- To get llm-map (or its updates), clone the new repository and run its `./install.sh` (same install paths; it has `--dry-run` and `--uninstall`). See [docs/llm-map.md](docs/llm-map.md).
- `--llm-map` and `--no-llm-map` are still accepted. Each prints one line (`llm-map is now a separate project: https://github.com/pratikgh0se/llm-map`) and changes nothing else. A saved `llm_map=1` line is ignored and prints the same line. `--save-defaults` never writes it and removes an old one.

### Changes

- Subagent rules (F66.b): when the standing rules are injected at `SubagentStart`, the block starts with `These are the user's standing rules for the main session of this project. Apply only the rules that fit your task. Your task brief comes first.` The main-session injection does not get this line.
- Evidence wording (F66.c): the README no longer has a "Proof" heading or the `[C1]` to `[C8]` markers. It says that no scored benchmark result is published yet and that some live checks currently fail. The markers moved to `docs/evidence.md`. `--claims-lint` with no file now lints `docs/evidence.md`. It still accepts a path. `tests/local-proof.sh` lints `docs/evidence.md`.
- `--claims-lint` with no file looks in the script's own tree first, then in the current folder. A run in another project no longer lints that project's `docs/evidence.md`.
- Benchmark: OOLONG arm C is now Lossless Handoff alone (hooks and recall agent). The new option `--llm-map-path <dir>` adds llm_map from a checkout of the llm-map project, so the old setup stays reproducible. The 2026-11-18 pilot used arm C with llm_map.
- Docs: `docs/architecture.md` and `docs/faq.md` no longer give a stale version or say that the install sets the window (only `--window N` does). One term, "decisions list", in the README and the docs; `docs/decisions-list.md` says that "ledger" in code and option names means the decisions list. `SECURITY.md` and `docs/install.md` are updated.
- The parts of the PreCompact hook are called "files" in the docs. (The squash title of #27, "Split the PreCompact hook into modules", used the word "modules".)
- `HOOK_VERSION` is `1.16.0`; the product version is `0.14.0`.

### Also in this release: the PreCompact hook split (#27)

The PreCompact hook is split into files. No behaviour change: same flags, same output, same files, same exit codes.

- `hooks/precompact-handoff.py` is now a thin entry file (version, command line switch). Its code moved to ten part files in `hooks/lossless_handoff/`: transcript, summary, ledger, handoff, runtime, redaction, pipeline, reconcile, prove, claims. No file has more than 561 lines.
- The files run in the namespace of the entry file, so every old top-level name (`redact`, `parse`, `build`, `redact_tree`, `eval_claims`, `format_drift_scan` and the rest) is still importable from `precompact-handoff.py`.
- If the folder `lossless_handoff/` is missing or broken, the hook prints one line on stderr and exits 0.
- `install.sh` installs the folder and lists its files in the manifest. `uninstall.sh` removes it. `--dry-run` lists the files.
- Any script that copies `precompact-handoff.py` alone must copy the `lossless_handoff/` folder too.

## [0.13.0] - 2026-11-19

Smaller default install and one redaction function (hooks 1.15.0). New F64 and F65 in `docs/spec.md`. The install default changes, so this is a minor version.

- `./install.sh` with no flags now installs the core only. It no longer sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, installs the two nightly jobs, pulls `bge-m3`, or installs `llm_map.py` and its skill.
- New opt-in flags: `--window N`, `--nightly`, `--semantic`, `--llm-map`. `--no-window` and `--no-nightly` are still accepted and do nothing extra (the installer prints one note).
- New `--save-defaults`: saves the opt-in flags to `~/.config/lossless-handoff/install-defaults`. Later runs apply them. A command-line flag wins; `--no-semantic`, `--no-nightly`, `--no-llm-map` and `--no-window` turn one saved default off for that run. `./uninstall.sh --purge` deletes the file.
- A rerun never removes, turns off or changes an installed part. Existing 0.12.x installs keep their window value, nightly jobs and llm-map.
- `--dry-run` lists exactly what the chosen options install, and which options come from saved defaults.
- Redaction: one function masks secrets as `[REDACTED:<kind>]` in the handoff files, their copies, the decisions list and recall output. New kinds: AWS (AKIA and ASIA), GitHub (all token prefixes), Slack, Anthropic, OpenAI, Google, JWT, PEM private keys, and `key=value` secrets. The mark `[redacted]` of earlier versions is replaced.
- Limit: the model writes its own agent handoff file with the Write tool, and a hook cannot mask that write.

## [0.12.2] - 2026-11-19

Hardening: the nudge window and a format drift alarm (hooks 1.14.2). New F62 and F63 in `docs/spec.md`.

- Nudge window (F62): if `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is not a positive integer, the nudge learns the window from `preTokens` of past `compact_boundary` records (median + 33,000, rounded to 1,000; current transcript, else the newest 5 of the project). With no history it uses 200,000. Before, it assumed 400,000, so with a 200k window it could never fire. A valid value works as before. Health rows have `window` and `window_source`.
- Format drift alarm (F63): reconcile compares recorded post-compaction injects (`receipts.jsonl`) with `compact_boundary` records per session. A session with injects and no boundary sets `format_drift: true` in `reconcile.json`. `--prove` and the claims mark the affected checks `fail` (`format_drift`). `--check` prints one `format drift` line.
- Review fixes: the nudge skips manual compactions and ignores a learned window below 100,000 or above 2,100,000 (F62, ACs 298-300). It stops the scan after 1.5 s. The drift check counts boundaries from the window start (F63, AC 301). SECURITY.md lists the skill file, the LaunchAgents and the optional model download.
- The nudge tag is now `[lossless-handoff]` (it was `[claude-handoff]`).
- Text fixes: the docstring of `precompact-handoff.py`, a stale comment about subagent compaction, and `SECURITY.md` (file list, permissions, network use).

## [0.12.1] - 2026-11-18

llm_map: faster, a fixed label set, progress (hooks 1.14.1). New F61 in `docs/spec.md`, ACs 281-286.

- Defaults are now `--batch 100` and `--jobs 8`. 3,000 items no longer pass the 2-minute Bash limit.
- New `--labels "a|b|c"`: the model must answer with one label. Replies map to a label by exact match or a word-boundary prefix. Invalid replies are retried once, then fail with `error: "label: ..."`.
- Each finished batch prints `llm_map: <done> of <n> items done` to stderr.
- The skill text tells the model to use `--labels`, a 600000 ms timeout, a foreground wait and no `pgrep`.

## [0.12.0] - 2026-11-18

Label many items with a small model (hooks 1.14.0). New F60 in `docs/spec.md`, ACs 274-280.

- New `hooks/llm_map.py`: it labels each item of a large list with a small model in parallel batches. It writes one JSON line per item, retries missing items once and has a cost cap (`--max-usd`).
- Each model call runs without tools, hooks, MCP servers or a saved session, so no Lossless Handoff hook runs for it.
- New skill `skills/llm-map/SKILL.md`. Install writes it to `~/.claude/skills/llm-map/` unless a file that is not ours is there. Uninstall removes only the marked file.
- `install.sh` also copies `llm_map.py`. It changes no `settings.json` entry.

## [0.11.1] - 2026-11-18

Find what a subagent read (hooks 1.13.1). New F59 in `docs/spec.md`, ACs 268-273.

- In keyword search, a plain pattern with a space, `-` or `_` matches any run of these characters, so `ledger key` finds `ledger-key`. `--fixed` stays exact. Other patterns keep their meaning.
- A hit line from a tool call ends with ` -> result <id>` when the same chat holds the tool result. `open <id>` prints that result.
- `agents/recall.md` and the recall line tell the model to open that id.

## [0.11.0] - 2026-11-18

Subagent work in search, and rules at subagent start (hooks 1.13.0). New F57 and F58 in `docs/spec.md`, ACs 255-267.

- `search`, `open` and the meaning index include subagent transcripts by default. A subagent hit has the id `<sid8>/<agent8>:<line>` and the tag `[agent <agent8>]`. `--no-subagents` restores the old scope. `--subagents` is still accepted and does nothing. `user` still reads only main chats. Run `recall.py index` once to add subagent text to the meaning index.
- New `SubagentStart` hook (`postcompact-inject.py --subagent-start`). It gives a new subagent the active `rule` entries of the project (at most 4,000 characters) and the recall line. It gives no decisions, tasks, handoff text or notes. `HANDOFF_SUBAGENT_RULES=0` turns it off.
- `install.sh` registers the hook (also in the `--managed-hooks` file); `uninstall.sh` removes it. `SubagentStart` is a new hook event: restart open Claude Code sessions.
- Each run adds a health row with `event: "subagent_start"`. `--prove` and `--reconcile` skip these rows. The agent file `agents/recall.md` explains the `[agent ...]` tag.

## [0.10.0] - 2026-11-18

Summary nodes, a recall subagent, and a log of the excerpt cut (hooks 1.12.0). New F54, F55 and F56 in `docs/spec.md`, ACs 236-254.

- Summary nodes: one leaf per compaction in `projects/<project>-<hash>/nodes.jsonl`, built from `compact_boundary` records without a model call. The SessionStart `compact` hook adds new nodes from the saved byte offset. It adds one line that names the newest node.
- New `recall.py` commands: `map`, `describe <id>`, `nodes --backfill` and `search --node <id>`. Search hits end with `[node <id>]` or `[node live]`.
- `--prove` checks the nodes (`nodes: <b> boundaries, <n> leaves, <bad> bad`) and prints `excerpt cut: ...`.
- `install.sh` writes the recall subagent `~/.claude/agents/recall.md` (source file `agents/recall.md`). It never overwrites a file without the marker line. `uninstall.sh` removes it only with the marker. The recall line gets one more sentence when the file is installed.
- Health rows have `excerpt_chars` and `excerpt_dropped`.

## [0.9.0] - 2026-11-18

Protect the store; steer Claude Code's summary (hooks 1.11.0, release 0.9.0). New F52 and F53 in `docs/spec.md`, ACs 218-235.

- Handoff `*.md` files are kept 365 days (was 14). `HANDOFF_RETENTION_DAYS` changes it; `0` keeps them forever. The status JSON files keep the 14-day rule.
- `postcompact-inject.py --check` warns when `cleanupPeriodDays` is below 90 (managed settings first, then user settings, then 30).
- `install.sh --keep-transcripts[=DAYS]` raises `cleanupPeriodDays` (default 365). It never lowers it. An interactive install asks y/N. `uninstall.sh` never changes it and says so.
- The PreCompact hook prints guidance for Claude Code's own summary. It also prints for `/compact <text>`; Claude Code adds it to that text. `HANDOFF_COMPACT_STEER=0` turns it off. The health row has a `steer` field.
- Test env `LOSSLESS_HANDOFF_MANAGED_SETTINGS` sets the managed settings path.

## [0.8.1] - 2026-11-17

Precomputed compactions (hooks 1.10.1).

- A precomputed compaction keeps its own handoff. If the handoff is older than 15 minutes but newer than the previous compaction boundary, SessionStart injects it with a `Note: this compaction was prepared at ...` line. It no longer runs recovery or says `HANDOFF RECOVERED`. New F51 in `docs/spec.md`, ACs 212-217.

## [0.8.0] - 2026-11-17

No silent misses (machine-setup F45 to F48, hooks 1.10.0).

- `install.sh --no-nudge` does not install the context nudge (`handoff-nudge.py` and its PostToolUse entry) and removes one left by an earlier install. New F50 in `docs/spec.md`, ACs 208-211.
- CI runs the code-blind acceptance tests from the new repository [lossless-handoff-tests](https://github.com/pratikgh0se/lossless-handoff-tests) on Ubuntu and macOS, pinned by `tests.ref`. The tests moved there from bento-mods-tests with their history, and were ported to this repository's layout (`LOSSLESS_HANDOFF_REPO`, `install.sh` in a temporary HOME).
- `docs/spec.md`: the canary plist runs the `hooks/handoff-canary.sh` wrapper (AC 71 amended); the machine-setup `--launchd`, `--render-only`, `LABEL_PREFIX` and `deps.tsv` statements are withdrawn; new F49 uninstall ACs 202-207.
- README: a Tests part (the three CI layers, the code-blind rule, `tests.ref`, how to run the acceptance tests locally).
- `tests/local-proof.sh`: one local command that runs every test layer, including real Claude Code compactions, and writes a dated report in `docs/proof/`.

- **Early write and recovery (F45).** PreCompact writes `<sid>-pending.json` and a deterministic handoff before the first model call, so a hook killed at its timeout still leaves a handoff. If a compaction still has none, the SessionStart `compact` hook rebuilds one from the transcript (no model call, 6 s deadline) and says `HANDOFF RECOVERED`.
- **Install check (F45).** `postcompact-inject.py --check` runs at SessionStart `startup` and `resume` (timeout 5) and prints one `HANDOFF CHECK:` line when a hook is not wired, a hook file is missing or changed since install, the two hooks' versions differ, or reconcile found compactions without a handoff. Silent otherwise.
- **Reconcile (F45).** `precompact-handoff.py --reconcile [--days N]` counts `compact_boundary` records against `health.jsonl` and writes `reconcile.json`.
- **Subagent handoffs (F46).** A compacting subagent (hook input with `agent_id`) gets its own handoff from its own transcript, stored as `<sid>-a-<agent_id>` files; it never touches the main session's files, never merges the decisions list and never starts the indexer. Before this, subagent compactions got no handoff (0 of 28 in the author's first 30-day reconcile).
- **Headless runs (F46).** `claude -p` and the SDK load user hooks by default. `install.sh --managed-hooks` stages a managed-settings file with the handoff hooks and prints the `sudo install -m 644 ...` command (macOS `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux `/etc/claude-code/managed-settings.json`); it never runs sudo. Only `--bare` skips managed hooks. `--check` accepts hooks from user or managed settings.
- **Proof (F47).** Every injected handoff appends a receipt to `receipts.jsonl`. `precompact-handoff.py --prove [--days N]` checks receipts with plain code (`lossless`, `right_agent`, `delivered`, `decisions_safe`), appends verdicts to `proofs.jsonl` and writes `proof-report.md`.
- **Claims register (F48).** New `hooks/claims.json` (C1 to C8). `--prove` evaluates it into `claims-status.json` and `claims-report.md` (`pass`, `fail`, `stale`, `no-data`); `--claims` prints it; `--claims-lint <file.md>` exits 1 unless every `[C<n>]` marker in a document is known and `pass`. README claims now carry these markers; C7 (blind quiz) and C8 (cost) are dated one-off studies.
- `install.sh` installs `claims.json`, writes `~/.claude/hooks/.handoff-manifest.json`, adds `postcompact-inject.py --check` to `startup` and `resume`, and on macOS installs the LaunchAgent `com.lossless-handoff.handoff-reconcile` (daily 04:15, `--reconcile --prove --days 30`, log `~/.local/state/claude-handoffs/reconcile.log`; `--no-nightly` skips it). On Linux it prints the crontab line. Everything it adds is recorded in the install marker. It prints "Restart open Claude Code sessions: hooks load when a session starts."
- `uninstall.sh` removes the `--check` entries, the reconcile LaunchAgent, the manifest and `claims.json`; it keeps all state (handoffs, receipts, proofs, indexes, ledgers) and tells you if a system managed-settings file may still hold the hook entries.
- `precompact-handoff.py` and `postcompact-inject.py` 1.10.0 copied from machine-setup; `recall.py` is byte-identical to the 1.8.0 copy.
- `tests/smoke.sh` covers a clean `--check`, a changed-file `--check`, `--reconcile` and `--prove` on an empty HOME, a subagent PreCompact input, `--managed-hooks`, and the install and uninstall round trip with the new entries.
- `handoff-nudge.py` ignores subagent events (non-empty `agent_id`): no note and no meta write, so a subagent is never told to write the main `<sid>-agent.md` (spec F46 f).
- Opt-in canary: `tools/handoff-canary/` (`run.sh`, `canary.py`, `check.py`) and `hooks/handoff-canary.sh`. `install.sh --canary` installs it with the LaunchAgent `com.lossless-handoff.handoff-canary` (03:30, `--catch-up`; crontab line on Linux); not part of a default install because it makes real model calls (about $0.03-0.10 per run). Uninstall removes it and keeps its state.
- `LOSSLESS_HANDOFF_NO_LAUNCHCTL=1` now covers all three LaunchAgents (recall index, reconcile, canary) in both `install.sh` and `uninstall.sh`. Before, it covered only the canary install, so a test install into a temp HOME on macOS loaded real jobs, and a test uninstall could unload the real ones. `tests/smoke.sh` sets it.
- `install.sh --help` printed `about ./install.sh.03-0.10` for the canary cost; it now prints `$0.03-0.10`.
- `uninstall.sh` also removes `handoff-canary/__pycache__/` (written when `canary.py` imports `check`), so the canary folder is removed. Found by CI on Linux and macOS runners.
- ADRs 0001, 0007, 0008 and 0011 moved into `docs/adr/` (generic versions) and the README links repointed.
- New `docs/handoff-reliability.md` and `docs/spec.md` (the behaviour contract the code-blind tests are written from, with AC numbers); this repository is now the home of the handoff system.
- README: a "When a compaction is missed" section, subagents, headless runs and "Proof, not promises"; the old "0 hook errors" wording is replaced by the measured miss counts (188 of 192 main compactions covered in a 30-day reconcile on 2026-11-17; 4 missed: 3 from a deliberate probe run with user settings off, 1 likely a hook killed at its timeout on 2026-10-23; 0 of 28 subagent compactions before this release; live subagent test after the fix: right-agent 2/2, delivered 2/2, failed 0). Also: measured daily use, hybrid search explained, comparison.

## [0.7.0] - 2026-11-14

Automatic recall indexing (machine-setup F44.13, hooks 1.8.0).

- `recall.py index --all` indexes every Claude Code project that sits in a git repo, newest first, with a time cap (`--max-seconds`), one summary line per project.
- `recall.py session-end` is a new `SessionEnd` hook: when a chat ends it starts a detached index of that project and prints nothing; it always exits 0.
- `install.sh` registers the `SessionEnd` hook (timeout 5) and records it so `uninstall.sh` removes only what was added.
- `install.sh` offers `ollama pull bge-m3` when `ollama` is on PATH (asks y/N interactively, `--yes` says yes, a non-interactive run skips with a hint). Without Ollama it prints the download link and continues; keyword search still works. Ollama is never installed for you.
- `install.sh` installs a macOS LaunchAgent `com.lossless-handoff.recall-index` (daily 03:30, `index --all`); `--no-nightly` skips it. On Linux it prints the crontab line.
- `install.sh` starts the first `index --all` in the background after install. New flags: `--yes`, `--no-nightly`.
- `uninstall.sh` removes the `SessionEnd` entry (if install added it) and boots out and deletes the LaunchAgent. Indexes, logs and state are kept.
- `precompact-handoff.py`, `postcompact-inject.py` and `recall.py` 1.8.0 copied from machine-setup.
- `tests/smoke.sh` covers the SessionEnd hook, `index --all`, the `--yes` and non-interactive model-pull paths, and uninstall.

## [0.6.1] - 2026-11-14

Long messages in full, a cold-model timeout fix, and measured results (machine-setup F44.12).

- `recall.py open <id> --full` now returns up to 40,000 characters of a message, and `--from <N>` pages through longer ones. A cut message ends with the exact command to continue. Found by a test that saw a 14,300-character message cut at the old 12,000-character output cap.
- Embedding calls wait up to 30 s for a cold model and ask Ollama to keep it loaded (`keep_alive`), so the first search after a pause no longer falls back to keyword only.
- The new-chat context and handoff now say the decisions list and your latest words win over recall when they disagree.
- `precompact-handoff.py`, `postcompact-inject.py` and `recall.py` 1.7.1 copied from machine-setup.
- LICENSE copyright line now carries the author's name.
- README: new "Proof it works" section with measured retrieval, meaning-search and alignment results, including limits.
- `tests/smoke.sh` checks that a 50,000-character message reassembles byte-exactly from `open --full` plus `open --full --from 40000`.

## [0.6.0] - 2026-11-14

Renamed to Lossless Handoff, based on the LCM paper (arXiv:2605.04050), plus recall (machine-setup F44, F44.9, F44.10, F44.11).

- Renamed from claude-handoff to Lossless Handoff (repo `lossless-handoff`; GitHub redirects the old URL). Install paths, the state folder `~/.local/state/claude-handoffs` and hook command names are unchanged, so existing installs upgrade in place. README now credits the LCM paper by Clint Ehrlich and Theodore Blackman.
- New `recall.py`: search, open, user, handoffs and index over the transcripts of all chats in the same project. Hybrid search merges word hits with optional meaning hits from a local embedding model (Ollama `bge-m3` by default; `RECALL_EMBED_URL`, `RECALL_EMBED_MODEL`, `RECALL_EMBED_BACKEND`, `RECALL_EMBED_KEY`). Words work without any setup. Secrets are redacted before output and before embedding.
- Handoffs and new-chat context now carry one line telling the model that recall exists.
- The meaning index is refreshed at each compaction by a detached background indexer that never delays or fails the hook.
- `precompact-handoff.py` and `postcompact-inject.py` 1.7.0 copied from machine-setup. RESOLVED and SUPERSEDED lines keep their full original text (the 700-character cap no longer cuts `was:` and `| why:`).
- `precompact-handoff.py --ledger-repair <dir>` restores cut RESOLVED/SUPERSEDED lines from the newest backup that still holds the full entry.
- `--ledger-maintain` rejects a missing directory (exit 2) and creates nothing when there is no ledger.
- An active ledger line without `| why:` is injected anyway and logged to `hooks-error.jsonl`, instead of being skipped.
- `install.sh` installs `recall.py`, adds the allow rule `Bash(python3 ~/.claude/hooks/recall.py:*)` (idempotent) and prints the one-time index command; `uninstall.sh` removes `recall.py` and that rule. `tests/smoke.sh` covers recall.

## [0.5.0] - 2026-11-09

Ledger kinds, lossless injection, later reminders (machine-setup F40.x, F41, F42, F43).

- `precompact-handoff.py` and `postcompact-inject.py` 1.6.0 copied from machine-setup.
- Decision kinds: every ledger entry is tagged `[rule]`, `[decision]` or `[task]` (untagged old lines count as decisions). New `RESOLVE D<n>: <reason>` marks finished tasks and one-offs `RESOLVED` (kept in the file, no longer injected). Rules are never resolved by the model.
- Lossless injection: the ledger is injected in three parts (`--ledger --part 1/2/3`) at compaction, `startup` and `resume`. Standing rules come first and always in full, then decisions, then tasks; entries that do not fit in full become one-line index entries, so nothing is dropped.
- `--ledger-maintain <repo>`: one pass over an existing list that tags entries, resolves finished tasks and supersedes contradictions, without adding entries or editing text.
- Handoff file: seventh heading `Parallel work running` (background agents and long jobs, or "none"); a stale-note warning when the model's own handoff did not change its living sections since the last reminder.
- `handoff-nudge.py`: reminders at 75% of the window, then every 25,000 tokens (was 50,000), plus one final "Compaction is imminent" note. Steps stay 12,000 tokens below the real compaction point (window minus 33,000): 300k, 325k, 350k and a final at 355k for a 400k window. Records section hashes so a stale handoff is called out. The nudge also asks the model to list standing rules under Decisions and why.
- `install.sh` registers `--ledger --part 1/2/3` for `compact`, `startup` and `resume`, and replaces the old single `--ledger` entry on re-run. `uninstall.sh` removes all of them.
- `tests/smoke.sh` updated for three parts, no-loss check over 40 entries, and the new nudge cadence.

## [0.4.0] - 2026-10-27

Faster decisions updates (machine-setup F40).

- `precompact-handoff.py` 1.5.0 copied from machine-setup. The decisions merge now returns only the changes (new decisions and `SUPERSEDE` lines) and code applies them to the list, so the call is much shorter. Entries are still never deleted.
- Own timeout: `HANDOFF_LEDGER_TIMEOUT` (seconds, 1-300, default 100), independent of the summary timeout. The PreCompact hook timeout is now 150; `install.sh` registers 150 and raises an older 90 on re-run.
- Catch-up: the merge reads your messages oldest first, up to 40,000 characters per run, so a backlog is caught up over several compactions instead of being dropped.
- Synthetic entries (tool output, command echoes, task notifications) no longer count as your messages for freshness, the merge input or "Last user messages".
- Health lines gain `ledger_error` (`timeout`, `exit <rc>`, `parse`, `locked` or an exception name) when the update fails.

## [0.3.0] - 2026-10-25

Per-project decisions ledger (machine-setup F39 to F39.11).

- `precompact-handoff.py` 1.4.2 and `postcompact-inject.py` copied from machine-setup. At every compaction a second Sonnet call merges your decisions since the last update into `~/.local/state/claude-handoffs/projects/<project>-<hash>/decisions.md` (project = git root, else the folder). Entries are only marked `SUPERSEDED`, never deleted; code restores dropped entries, rejects invalid output, caps lines at 700 characters, keeps 20 backups and holds a lock per project.
- New injection: `postcompact-inject.py --ledger --part 1/2` after compaction (full list, newest first), and `--ledger` at session `startup` and `resume` (active decisions, about 4,000 characters).
- Handoff file layout: authority line (user's latest words > Project decisions > Agent handoff > Fresh summary > Claude Code's summary > Facts), no `## Decisions via questions` section, cap 14,000 characters, tool lines end `-> ok`, `-> error` or `-> no result`.
- New switches: `HANDOFF_LEDGER=0` disables the list entirely; `HANDOFF_LEDGER_MODEL` picks its model. Health lines gain ledger status and counts.
- `install.sh` registers the two `--ledger --part` commands in the `compact` group and new `startup` and `resume` groups (idempotent; the double-registration check covers them). `uninstall.sh` removes exactly those and keeps the ledgers, saying so.
- `tests/smoke.sh`: shim tells the merge call from the summary call; cases for ledger creation, restore on drop, all inject modes, `HANDOFF_LEDGER=0`, install/uninstall of the new groups. Git-specific check prints SKIP without git.
- Cost: two Sonnet calls per compaction, about $0.10-0.20, 20-40 s. Measured on 96 questions about earlier decisions: 13.5 -> 56 out of 100 (README).

## [0.2.0] - 2026-10-23

- `precompact-handoff.py` 1.3.0 (machine-setup F38): a fresh Sonnet summary of the conversation since the last compaction is written at every compaction (`claude -p --model sonnet`, isolated flags), placed after the model's own handoff. It replaces the Haiku fallback.
- New switches: `HANDOFF_SUMMARY=0` disables it, `HANDOFF_SUMMARY_MODEL` (default `sonnet`), `HANDOFF_SUMMARY_TIMEOUT` (seconds, default 75). `HANDOFF_HAIKU_FALLBACK=0` still disables it.
- Health lines gain `summary_model`; the section is headed `## Fresh summary at compaction (<model>; verify against facts)` and failures read `Fresh summary failed: <reason>`.
- `tests/smoke.sh`: cases with a fake `claude` shim (summary placement, disable switch, model override, timeout).
- README: blind-test quality numbers, configuration table, cost FAQ; SECURITY.md describes the per-compaction call.
- `install.sh` reports version 0.2.0; nothing else in install/uninstall changes.

## 0.1.0 - 2026-10-20

Extracted from machine-setup v1.0.0 (the Bento handoff system, spec features F35 to F37).

- PreCompact hook `precompact-handoff.py` and SessionStart hook `postcompact-inject.py` copied unchanged.
- New `handoff-nudge.py` (PostToolUse): built-in handoff nudge at 75% of the window, then every 50k tokens, with the same text and meta file as the Bento compact plugin.
- `install.sh` / `uninstall.sh`: idempotent settings merge, backups, dry-run.
- `install.sh --help` lists all flags and the version; new `--version` flag.
- `tests/smoke.sh`: install, hooks, nudge, uninstall in a temporary HOME. Runs on macOS and Linux.
- GitHub Actions CI (`.github/workflows/ci.yml`): `bash -n`, shellcheck, `py_compile` and the smoke test on ubuntu-latest and macos-latest.
- `SECURITY.md` and a bug report issue template.
- Nudge text identifies itself as coming from the user's installed claude-handoff hook (not the tool result), so models do not dismiss it as prompt injection.
- `install.sh` refuses (exit 2) when the same hooks are registered from another path, to avoid double execution; `--force` overrides.
- README FAQ: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` must be set in user settings or the shell (project settings did not override it); the window must comfortably exceed baseline context (very small windows below about 150k compact before the nudge); one very large tool result can skip the nudge, the PreCompact fallback still writes a handoff.
- README: prerequisites, one-line install, exact list of changes, FAQ, troubleshooting, CI badge.
