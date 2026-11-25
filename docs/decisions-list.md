# The project decisions list

How the decisions list works. This page is part of the [docs index](README.md).

## The project decisions list

A handoff belongs to one session, but decisions belong to the project. The hooks keep one plain-text list for each project (a git repo, or the folder where you started if there is no repo). The path is `~/.local/state/claude-handoffs/projects/<project>-<hash>/decisions.md`. In code, option and file names (`HANDOFF_LEDGER`, `--ledger`, `ledger.py`), the word "ledger" means the decisions list. The two are the same thing. Each decision has one line: an id (`D12`), the date, a kind, what was decided, and why.

- **Kinds.** `[rule]` (a standing rule or working preference), `[decision]` (a choice) or `[task]` (a follow-up that you asked for). Older untagged lines count as decisions.
- **What goes in.** Only things that you decided or approved explicitly: a choice, a rule, a constraint, a permission, a rejected option. Plans, progress and unapproved ideas of the assistant do not go in.
- **Kept up to date.** At every compaction, a second Sonnet call reads the conversation since the last update and the current list. It returns the updated list. When a task is finished, the hook marks the line `RESOLVED` and no longer injects it. The model never resolves a rule on its own judgement (F89): it may resolve a rule only when it quotes your own words from the new conversation that end it, and the code checks the quote.
- **Never deleted.** When you change your mind, the hook marks the old decision `SUPERSEDED by D<n>` with the reason. The decision stays in the file. Before each update, the hook saves the previous list as `decisions.<timestamp>.md` (it keeps the newest 20).
- **Code checks the model.** Sonnet proposes, and plain code decides. The code puts back an entry that the model drops. It never revives a superseded entry. Existing ids never change. The code shortens over-long lines and does not drop them. It throws away a result that has no valid line. A project lock stops two compactions in the same repo from overwriting each other.
- **Injected after compaction and in new chats.** There are three parts: rules first (always in full), then decisions, then tasks, newest first. What does not fit in 3 x 10,000 characters becomes one-line index entries, so nothing is silently dropped. The hooks inject the same parts at `startup` and `resume`, so a fresh session does not ask again what you already settled.
- **Tidy an old list.** `python3 ~/.claude/hooks/precompact-handoff.py --ledger-maintain <repo>` runs one maintenance pass. It tags untagged entries, resolves finished tasks and supersedes contradictions. It adds no entries and edits no text.

## Hand edits and line forms

The hooks write the decisions list. Do not edit it by hand or by script. To change a rule or decision, say it in the chat and run `/compact`. The injected part 1 says the same: `This list is written by the hooks; do not edit decisions.md yourself.`

If you must edit by hand, use only these forms (G1 in the spec, F90.a), then run `python3 ~/.claude/hooks/postcompact-inject.py --check`. A date is `YYYY-MM-DD` and an id `D<n>` is a number of 1 or more.

- `- D<n> [<date>] [<tag>] <text> | why: <why>`, where `<tag>` is `rule`, `decision` or `task`.
- `- D<n> [<date>] <text> | why: <why>` (older lists, no tag). The text does not start with a word in capitals followed by ` [` or `:`. So `RETIRED [..]` and `DONE:` are not valid text.
- `- D<n> [<date>] [<tag>] <text>` or `- D<n> [<date>] <text>` (older lists, no `| why:` part). The hooks still inject it and write one line in `hooks-error.jsonl`.
- `- D<n> [<date>] SUPERSEDED by D<m> [<date>]: <old text> | because: <reason>`.
- `- D<n> [<date>] RESOLVED [<date>]: <reason> | was: <old text>`.
- A header line (starts with `#`), a blank line, or a comment line (starts with `<!--`).

Any other line is a malformed line. The hooks never inject it and never count it as active. A merge writes it back where it was, so it is never deleted. `postcompact-inject.py --check` reports `decisions list: <k> malformed lines (first: line <i>)` for the project of the current folder. `--check --verbose` shows the first 3 malformed lines, cut to 120 characters, with secrets masked. This is a data problem, not an install problem.

One parser reads these forms: `hooks/lossless_handoff/grammar.py`. A recall hit id has two forms with the same parser: `<sid8>:<line>` for a main transcript and `<sid8>/<agent8>:<line>` for a subagent transcript.

Set `HANDOFF_LEDGER=0` to turn it off. Turning it off or uninstalling never deletes existing lists.
