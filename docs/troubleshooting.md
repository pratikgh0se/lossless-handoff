# Troubleshooting

What to check when something does not work. This page is part of the [docs index](README.md).

- Logs and health are in `~/.local/state/claude-handoffs/` (or `$HANDOFF_STATE_DIR`). Start with `tail -n 5 ~/.local/state/claude-handoffs/health.jsonl`. It has one line for each compaction with `status` `ok`, `reduced` or `degraded`.
- `<sid>-degraded.json` and `<sid>-last-health.json` in the same folder say why a compaction was degraded.
- A `HANDOFF CHECK:` line at session start names what is wrong (hook not wired, file missing or changed, version mismatch, compactions without a handoff). Run `./install.sh` again, then restart open sessions. The `proof failed` and `claims` lines point at `proof-report.md` and `claims-report.md` in the state folder.
- A `HANDOFF RECOVERED` line at the top of the injected text means that PreCompact did not finish. The hook rebuilt the handoff from the transcript with no model call. Check `health.jsonl` and `hooks-error.jsonl`.
- If there is no health line after `/compact`, restart Claude Code (hooks load at startup). Then check that `~/.claude/settings.json` has the `PreCompact` entry and that `~/.claude/hooks/precompact-handoff.py` exists.
- If there is no nudge: it fires only above 75% of the window (300k for 400k) and it needs a usage entry in the transcript. Check `CLAUDE_CODE_AUTO_COMPACT_WINDOW`.
- If search says `keyword only`, Ollama is not running or `bge-m3` is not pulled. Start Ollama and run `ollama pull bge-m3`.
- If settings or hooks are in a bad state: install and uninstall leave timestamped backups (`settings.json.bak-*`, `~/.claude/hooks/.claude-handoff-backup-*`).
- To check the install, run `python3 ~/.claude/hooks/postcompact-inject.py --check --verbose`. It prints one line for each check and a last line.
- If the last line names an install problem (hook not wired, file missing, file differs, version mismatch, `recall.py` missing, or the allow rule `Bash(python3 ~/.claude/hooks/recall.py:*)` missing): run the installer again, then restart Claude Code.
- If the last line names a data problem (compactions without a handoff, a failed proof, or failing or stale claims): do not run the installer. For missed compactions, run `python3 ~/.claude/hooks/precompact-handoff.py --reconcile`. For a failed proof, read `proof-report.md`. For claims, read `claims-report.md`.
- For any other problem, run `bash tests/smoke.sh`, then open an issue with the bug report template.
