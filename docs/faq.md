# FAQ

Questions and answers. This page is part of the [docs index](README.md).

**Does it cost tokens?** It costs about one short nudge text each time. It also costs up to about 14,000 characters injected once for each compaction, and up to about 20,000 characters of decisions. Each compaction adds two `claude -p` Sonnet calls (fresh summary and decisions update; 20-40 s in parallel, roughly $0.10-0.20 of API-equivalent usage). Set `HANDOFF_SUMMARY=0` and `HANDOFF_LEDGER=0` to avoid them.

**Will recall find old messages?** Only while Claude Code still keeps the transcript. The default is 30 days. Run `./install.sh --keep-transcripts` (365 days) or set `"cleanupPeriodDays"` yourself. The hooks keep handoff files for 365 days. A longer retention means that more chat text stays on your disk for longer.

**Which new fields does the health row have?** `excerpt_chars` is the number of characters of transcript that the handoff model received (0 if the hook made no model call). `excerpt_dropped` is the number of characters of the current segment that the hook left out because of the 120,000-character limit (0 if it cut nothing). `--prove --days N` prints `excerpt cut: <c> of <n> compactions, median dropped <m> chars`. This line is information only and never fails a proof run. It measures how often the cut happens. It does not claim any gain in answer quality.

**What does the PreCompact hook print?** It prints a short block of guidance for Claude Code's own summary (quote your words, keep exact values, list running work, end with the next step). The hook gives this guidance only. A later benchmark measures the effect on summaries. Set `HANDOFF_COMPACT_STEER=0` to turn it off. The health row has a `steer` field (`printed` or `off`).

**Does it work in the desktop app?** Yes. The desktop app runs the same hooks from `~/.claude/settings.json`.

**Does it work with my existing hooks?** Yes. The install merges into your existing `hooks` groups and never removes or reorders yours. Uninstall removes only its own commands.

**Where must I set `CLAUDE_CODE_AUTO_COMPACT_WINDOW`?** Set it in your user `~/.claude/settings.json` under `env` (the install does this), or export it in the shell that starts Claude Code. A live test showed that `env` in the `.claude/settings.json` of a project did not override it.

**What window size should I use?** Use a size that comfortably exceeds your baseline context. With a very small window (180,000 tokens or less), the final threshold (window minus 45,000) is at or below the first reminder threshold. The nudge then sends only the final nudge, at the first threshold that the context reaches, and the PreCompact hook covers the rest. The savings come from long sessions on 1M-context models. If your sessions rarely pass 300k tokens, the window never triggers and you mainly get the handoff. Nobody measured whether a shorter context also improves answer quality. See [ADR 0001](adr/0001-compact-window-400k-with-handoff.md).

**Can the nudge be skipped?** Yes. It reads the token count of the previous turn, so one very large tool result can jump past the threshold without a nudge. The PreCompact hook still writes a handoff.

**Does it send anything over the network?** The scripts open no connection to a remote host. Word search stays on your machine. Meaning search sends message text to the embedding server that you configure (by default Ollama on your own machine). The two per-compaction calls run your local `claude -p`. These calls send conversation text to Anthropic through Claude Code, as usual. See `SECURITY.md`.
