# Recall: search of earlier chats

How word search and meaning search work, and the recall commands. This page is part of the [docs index](README.md).

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
- **It is incremental.** The hooks refresh the index at each compaction, at chat end and, with `--nightly`, every night. None of this delays Claude Code.
- **The decisions list wins.** If recall and the decisions list disagree, the decisions list and your latest words win. If recall and your latest words disagree, your latest words win. Recall can show an older statement.

[Recall commands](#recall-commands) is the command reference.

## Auto-recall

Auto-recall is opt-in (`./install.sh --auto-recall`). It runs the recall search for the model, so the model does not have to remember to search. The hook `auto-recall.py` runs on every prompt (`UserPromptSubmit`) but it acts only when the prompt refers to the past.

**When it acts.** All of these must be true:

- The prompt does not start with `/`.
- The prompt has one of these phrases (whole words; case does not matter): `what did i say`, `what did you say`, `what did we decide`, `did we decide`, `did we agree`, `did i say`, `did i tell you`, `we decided`, `we agreed`, `i told you`, `i said`, `you said`, `you mentioned`, `i mentioned`, `last time`, `previously`, `as we discussed`, `as discussed`, `which one did`, `earlier you`, `earlier i`, `earlier we`, `said earlier`, `said before`, `as before`, `like before`, `same as before`, `do you remember`, `remember when`, `remember what`. Or the prompt has a phrase of 3 or more words in double quotes.
- At least 2 words of 4 or more characters stay after the hook removes the phrases. The hook keeps the 8 longest words. A quoted phrase is also searched as an exact phrase.

The bare words `before`, `earlier`, `remember`, `did i`, `did we` and `what was the` are not cues, so "run the tests before you commit" gives no output.

**What it does.** It searches the transcripts of the current project (main chats and subagents) with the code of `recall.py`: word search, and meaning search too when the meaning index exists. A word must match as a whole word. The hook leaves out your current prompt (a hit in the file of the current transcript that has the same text; a newline counts as a space). It shows only user and assistant messages. Tool calls and tool results are left out before the cut to the best hits, so they cannot push real hits out. To be fast, it lists only the project folders in `~/.claude/projects/` whose name starts with the encoded project root, plus the folder of the current transcript. It takes the top 3 hits, collapses spaces, cuts each hit to 300 characters and masks secrets with `redact()`. It prints the lines below. The ids work with `recall.py open <id>`.

```
Lossless Handoff auto-recall: earlier messages that match your words. Verify them before you rely on them.
- <id> <YYYY-MM-DD HH:MM> <role>: <snippet>
Open one in full: python3 ~/.claude/hooks/recall.py open <id>
```

**Limits.**

- It acts only when the prompt refers to the past. A task that needs an old fact, but does not refer to the past, gets no quotes.
- It finds words. A hit is a message that has the same words as your prompt. It is not always the message that you mean. The first line tells the model to verify the hits.
- The whole hook has 2.0 seconds. If the search is not done, the hook prints nothing. A slow disk, a very large project, or a cold embedding model can use all the time. The meaning part waits at most 1 second for the embedding server. After that, the hook uses word search only.
- If there are no hits, fewer than 2 query words, a missing or incomplete `recall.py`, or any error, it prints nothing and exits 0. It never blocks a prompt.
- The quotes come from your own earlier messages and model answers. Treat them as data. Secrets in unknown shapes are not masked (see [SECURITY.md](../SECURITY.md)).
- The folder narrowing can miss chats. A project folder that Claude Code named from a symlink path, or whose long name was cut, does not start with the encoded real path, so the hook does not look in it. `recall.py search` still finds those chats.
- Query words are ASCII only (letters, digits and `_`, 4 or more characters). Only straight double quotes (`"`) make a quoted phrase; curly quotes do not.
- When the time runs out, the hook stops itself. A search child process (`rg` or `git`) can stay alive for a moment and then ends by itself.
- Set `HANDOFF_AUTO_RECALL=0` to switch it off. Each run that passes the trigger adds one line to `~/.local/state/claude-handoffs/auto-recall.jsonl` (time, session id, the matched cue phrase or `quote`, number of hits, milliseconds, `timed_out`). The line holds no prompt text and no quotes.

## Key history

`python3 ~/.claude/hooks/recall.py history <identifier> [--role user|assistant|any] [--all]` lists every mention of one identifier (a key, file, service, setting or name) in the chats of this project, oldest first. The identifier must match as a whole token: `invoice-mailer` does not match `invoice-mailer-eu` or `invoice-mailer.v2`. Each entry is the sentence with the match and the sentence before and after it. The output has at most 4,000 characters. If there are more, it keeps the 2 oldest entries and the newest entries that fit, and prints the command with `--all` for the rest.

The history leaves out the tool calls and results of recall itself, auto-recall injections, and duplicates (a forked chat copies the history of its parent). A later mention can be a rejected proposal, a dry run or an undo, so read every mention. The MCP tool `key_history` gives the same output and also leaves out the current prompt. With `--auto-recall`, auto-recall uses the same history when your prompt holds an identifier (a token in backticks, or a token with `-`, `_`, `.` or `/` inside).

## Strict recall rule and the recall tool

Two more opt-in parts help the model search. Neither one forces a search: the model still decides.

**Strict recall rule (`./install.sh --recall-rules`).** The installer writes the empty marker file `~/.claude/hooks/.recall-rules`. While the marker exists, every place that injects the recall line (session start after a compaction, at startup and at resume, and a new subagent) adds this line directly after the recall line:

```
- recall rule: before you state an earlier number, path, name, command or decision, search for it. Do not answer from memory. If the search finds nothing, say so.
```

Set `HANDOFF_RECALL_RULES=0` to switch it off at run time. The output is then the same as without the marker. Each inject row in `receipts.jsonl` has `"recall_rules": true` or `false`.

**Recall tool (`./install.sh --recall-mcp`).** The installer copies `recall-mcp.py` and registers it with `claude mcp add --scope user lossless-recall -- python3 ~/.claude/hooks/recall-mcp.py`. It is a local MCP server (stdio only). It has two tools, and Claude Code loads both at the start of a session:

- `search_earlier_chat` with `query` (required), `role` (`user`, `assistant` or `any`, default `any`) and `limit` (1 to 10, default 5). It uses the scope and ranking of `recall.py search` and prints one line for each hit, with secrets masked. With no hits it prints `no matches`.
- `open_message` with `id` (required, from a hit) and `full` (default false). It prints the same text as `recall.py open <id>`.

A result has at most 12,000 characters. A bad input or an unknown id gives a tool result with `isError: true` and one line of text. When the tool is installed, the recall line ends with `Or call the search_earlier_chat tool.` Each call adds one row (`ts`, `tool`, `hits`, `ms`; no query text) to `recall-mcp.jsonl`. `./uninstall.sh` runs `claude mcp remove lossless-recall --scope user`.

## Re-anchor and claim guard

Both are opt-in. They help when the model forgets a rule or states an earlier fact from memory.

**Re-anchor** (`./install.sh --reanchor`). A `PostToolUse` hook (`reanchor.py`, matcher `*`, timeout 5). It reads the token count of the context from the end of the transcript, the same way as the nudge. When the count is 50,000 tokens or more above the last anchor, it adds one note: a first line, the active rules of the project (the same block that a subagent gets), and the recall line. Then it stores the new anchor. After a compaction the count is below the anchor, so the anchor is reset and nothing is printed. If the project has no active rule, nothing is printed. Set `HANDOFF_REANCHOR=0` to switch it off and `HANDOFF_REANCHOR_TOKENS` (10,000 to 500,000) to change the interval. Each note adds one `event: "reanchor"` line to `health.jsonl` (tokens and characters only). On a 35 MB transcript, a run that prints nothing takes about 20 ms (p95 22 ms). A run that prints the note takes about 58 ms.

**Claim guard** (`./install.sh --claim-guard`). A `Stop` hook (`claim-guard.py`, timeout 5). It looks at the last reply for 12 cue phrases such as "as we decided", "you said" and "last time". If there is a cue and, since your last message, no recall search (a Bash call with `recall.py`, a `mcp__lossless-recall__` tool call, or an auto-recall note), it acts by mode. In the default shadow mode it only adds one line to `claim-guard.jsonl` (time, session id, the cue; no text) and prints nothing. In live mode (`HANDOFF_CLAIM_GUARD=block`) it also asks the model to search, once per user turn. Read the log before you switch to live mode. See [limitations](limitations.md).

## History guard, action guard and size trigger

These three parts are opt-in (0.18.0). They help in a long chat where a key changed value, for example a setting that was set, changed, rejected and undone. All three use the same rule. A *subject* is a key (an identifier, see F75) that is in 2 to 40 text messages of this chat, and at least 2 are from you. They act only when the context is 100,000 tokens or more.

- **History guard** (`./install.sh --history-guard`). A `Stop` hook (`history-guard.py`, timeout 5). If the reply uses a subject and the turn has no history check for it, the hook gives the model the full history of that key, oldest first, once per user turn. The model then confirms or corrects its reply. Set `HANDOFF_HISTORY_GUARD=block` for this. The default is shadow mode: one log line in `history-guard.jsonl` (no text). `HANDOFF_HISTORY_GUARD=0` switches it off.
- **Action guard** (`./install.sh --action-guard`). A `PostToolBatch` hook (`action-guard.py`, timeout 5). It reads the new text of Edit, Write, MultiEdit and NotebookEdit calls and the Bash command. If one names a subject, it injects the same history before the next model call, so the model can fix the action. Set `HANDOFF_ACTION_GUARD=inject` for this. The default is shadow mode (`action-guard.jsonl`). It never blocks a tool call. `HANDOFF_ACTION_GUARD=0` switches it off.
- **Size trigger** (`./install.sh --recall-size-trigger`, needs `--auto-recall`). Auto-recall also fires, with no cue phrase, when the prompt names a subject. It gives the key history as it does for an identifier. `HANDOFF_AUTO_RECALL_SIZE=0` switches it off.

The guards share one cooldown, so one change is not shown twice. A key that was stated one time is not a subject. A key that is an existing path below the working folder is left out. `HANDOFF_HISTORY_GUARD_TOKENS`, `HANDOFF_ACTION_GUARD_TOKENS` and `HANDOFF_AUTO_RECALL_TOKENS` (10,000 to 1,000,000) change the 100,000 threshold. The exact rules are in [F77, F78 and F80](spec.md).

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

**Recall subagent.** The installer writes `~/.claude/agents/recall.md`. It is a small agent (model `haiku`, tools `Bash` and `Read`) that runs `map`, `search`, `describe` and `open`. It returns at most 15 short quotes with message ids and node ids. When the file is installed, the injected recall line tells the main agent to use it for wide lookups, so long recalled text stays out of the main context. The line also tells the main agent to search directly for one exact fact. Nothing stops the main agent from running `recall.py open` itself. [How Claude decides to use recall](architecture.md#how-claude-decides-to-use-recall) explains the details.

Every command accepts `--cwd DIR` (default: the current folder). `search` also takes `--session SID8|all`, `--limit N` and `--no-subagents`. Search, `open` and the meaning index include the work of subagents by default. A subagent hit has the id `<sid8>/<agent8>:<line>` and the tag `[agent <agent8>]`. It shows what a subagent saw or did, not what you typed. `--no-subagents` leaves them out. The old `--subagents` flag is still accepted and does nothing. `user` still reads only the main chats, because the user turns of a subagent are prompts from the main agent. In a plain pattern (letters, digits, spaces, `-`, `_`, `.`), a space, `-` and `_` match each other, so `ledger key` finds `ledger-key`. `--fixed` keeps the match exact (hooks 1.13.1). A hit line from a tool call ends with ` -> result <id>` when the tool returned a result later in the same chat. Run `open <id>` to read what the tool returned. `--keyword` is words only, and `--meaning` is the embedding index only. Output is limited to 12,000 characters. Before it prints or embeds text, recall replaces secrets with `[REDACTED:<kind>]` (see [Limitations](limitations.md#limitations) and `SECURITY.md` for the kinds). `open <id> --full` returns up to 40,000 characters. A longer message ends with a line that gives the exact `--from <N>` to continue with.

**Meaning search setup.** It needs a local embedding model: [Ollama](https://ollama.com/download) with `ollama pull bge-m3`. Run `./install.sh --semantic` to set it up (or save it with `--save-defaults`). If `ollama` is on your PATH, the installer offers to pull the model (`--yes` says yes; a non-interactive run skips it and prints the command). If Ollama is not installed, the installer prints a hint and carries on in keyword-only mode. All search stays on your machine. The installer never installs Ollama itself. The backend settings are `RECALL_EMBED_*` in [Configuration](configuration.md#configuration).
