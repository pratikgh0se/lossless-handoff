# Background: why, the LCM paper and other projects

Why the project exists, the paper it builds on, and how it compares. This page is part of the [docs index](README.md).

## Why this exists

I run Claude Code with a 400k auto-compact window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000`) instead of the 1M default. With 1M, long sessions rarely compact and pay for their whole context on every turn. In a single-user estimate over 4.7 days, the shorter window gave about 19% lower API-equivalent cost [C8] (a dated one-off study, 2026-10-20, not a bill).

The catch is many more compactions. Each compaction used to lose rules, decisions and exact wording. This tool makes it safe to run a small window and compact often. I use it every day, on my own projects. It has one heavy user so far (see [Limitations](limitations.md#limitations)).

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
