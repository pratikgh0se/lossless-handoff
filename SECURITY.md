# Security

## What the scripts touch

`install.sh` and `uninstall.sh` read and write only these paths:

- `~/.claude/hooks/`: the hook files (`precompact-handoff.py`, `postcompact-inject.py`, `handoff-nudge.py`, `recall.py`) and `claims.json`; `llm_map.py` only with `--llm-map`. Also a backup folder `.claude-handoff-backup-<timestamp>/` for any file they replace, `.handoff-manifest.json` (file hashes for `--check`) and `.claude-handoff-install.json` (records what install added so uninstall removes exactly that).
- `~/.claude/settings.json`: merged JSON (hooks, two `permissions.allow` entries, and `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` only with `--window N`), with a `settings.json.bak-<timestamp>` backup before each write. Invalid JSON is never overwritten.
- `~/.local/state/claude-handoffs/`: created by install, written by the hooks, kept by uninstall. Project decision lists live in `projects/<project>-<hash>/` inside it (`decisions.md`, timestamped backups, `decisions.state.json`, `decisions.lock`); uninstall keeps them too.

- `~/.local/state/claude-handoffs/projects/<project>-<hash>/nodes.jsonl` (with `nodes.state.json` and `nodes.lock`): summary nodes. Each line holds ids, the transcript path, line numbers, times, a token count and a handoff path. It holds no message text. The text of a summary is read from the transcript when you ask for it.
- `~/.claude/skills/llm-map/SKILL.md`: the `llm_map` skill file. Install writes it only when it is missing or holds our marker line. Uninstall removes it only when it holds the marker.
- `~/Library/LaunchAgents/com.lossless-handoff.recall-index.plist` and `com.lossless-handoff.handoff-reconcile.plist` (macOS, unless you pass `--no-nightly`), and `com.lossless-handoff.handoff-canary.plist` (only with `--canary`). Uninstall unloads and removes them.
- `~/.claude/agents/recall.md`: the recall subagent file. Install writes it only when it is missing or holds our marker line. Uninstall removes it only when it holds the marker.

- `~/.config/lossless-handoff/install-defaults`: written only with `./install.sh --save-defaults`. It holds simple `key=value` lines for the opt-in flags. Uninstall keeps it; `./uninstall.sh --purge` deletes it.
- `~/Library/LaunchAgents/com.lossless-handoff.*.plist`: written only with `--nightly` (and `--canary` for the canary job).

Since 0.13.0 the default install is the core only. It sets no window value, installs no nightly job, pulls no model and installs no `llm_map` or skill. Each one needs its own flag.

## Redaction (since hooks 1.15.0)

One function, `redact()` in `hooks/recall.py`, masks secrets. Each secret becomes `[REDACTED:<kind>]`. Text without a secret stays the same, byte for byte. The hooks call the function before they write:

- the PreCompact handoff files (the deterministic part and the fresh summary) and their copies (`<sid>-latest.md` and the dated file);
- the project decisions list (`decisions.md`) and its timestamped backups;
- recall output, and the text that recall puts in its index.

The kinds are: `aws` (AKIA or ASIA and 16 characters), `github` (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), `slack` (`xox[abprs]-`), `anthropic` (`sk-ant-`), `openai` (`sk-`), `google` (`AIza` and 35 characters), `jwt` (three base64url parts that start with `eyJ`), `pem` (a private key block from BEGIN to END), `url` (the password in `scheme://user:password@host`) and `secret`. The `secret` kind has two forms. One is the value after `Authorization: Bearer`. The other is the value of `key=value` or `key: value` when the key name ends with `password`, `passwd`, `passphrase`, `secret`, `secret_key`, `token`, `api_key`, `apikey`, `access_key` or `private_key`; a value after `Bearer` is masked, the key stays, and a quoted value is masked inside its quotes.

A prefix such as `sk-` or `ghp_` counts only at the start of a word. A letter, digit, `_` or `-` before it stops the match, so a path such as `tests/task-runner-suite.py` stays as it is. A `key=value` value is masked only when it has 8 or more characters, is not only letters, is not only digits, and is not a call, an index or a variable reference (`get_secret(name)`, `os.environ[...]`, `$VAR`). So `token: 160000 of 400000` and `if not token: return None` stay as they are. The match time is linear in the size of the text.

Limits:

- The function knows these shapes only. A secret in any other shape (for example `sk_live_` keys, npm tokens and Slack webhook URLs), or a secret in prose without a key name, is not masked. A PEM block with no END line is not masked. A short password (under 8 characters), a password of only letters or only digits (`password: 12345678`), and a value with dots such as `secret=abc.def.ghi` that looks like an identifier are not masked. A prose word after a key word can be masked, for example `token: src/auth/token-store-v2.ts`. The value must start on the same line as the key.
- The model writes its own agent handoff file (`<sid>-agent.md`) with the Write tool. A hook cannot mask that write. The hooks mask every copy that they make of it, so the original file is the only place that can hold the secret.
- Claude Code keeps its own transcripts. This project does not change them. Recall masks what it shows from them.
- Files that earlier versions wrote are not rewritten. The next write of a decisions list masks it.
- If `recall.py` is not next to the PreCompact hook, the hook uses a copy of the same patterns that is kept inside `precompact-handoff.py`. The smoke test checks that both copies give the same output.
- Do not put secrets in chat and expect them to be removed everywhere.

Since 0.11.0 the meaning index also holds the text of subagent transcripts (user and assistant messages, redacted like other text), and `recall.py search` and `open` read them by default. A subagent transcript can hold file contents, command output and notes that never appeared in the main chat, so more of your work text is searchable. Use `--no-subagents` to leave it out of a search. The index is in `~/.local/state/claude-handoffs/recall/`; delete it to remove the text. The `SubagentStart` hook (`postcompact-inject.py --subagent-start`) reads the project decisions list and prints the active `rule` entries to the new subagent. It prints no other ledger entry and no handoff text, makes no network call, and adds one row per run to `health.jsonl`. `HANDOFF_SUBAGENT_RULES=0` turns it off.

The hooks read the session transcript Claude Code passes them and write handoff files and a health log into the state directory. The permissions added are `Edit(~/.local/state/claude-handoffs/**)`, so the model can write its handoff file there, and `Bash(python3 ~/.claude/hooks/recall.py:*)`, so the model can run recall without a prompt.

## Network

The hooks and scripts open no connection to a remote host. Two things use a network path. First, `recall.py` sends text to an embedding server for the meaning index and meaning search. The default is local Ollama at `http://localhost:11434` (model `bge-m3`). Change it with `RECALL_EMBED_URL`; if you point it at a remote server, the text that recall indexes goes there. If the server is down, recall uses keyword search only. At install, if `ollama` is on your PATH, the installer can run `ollama pull bge-m3`, which downloads the embedding model from the Ollama registry. It asks first, and `--yes` answers yes. Second, there are model calls. The fresh summary runs your local `claude -p --model sonnet` once per compaction; that is Claude Code talking to Anthropic as usual, not this project. `llm_map.py` runs `claude -p` in the same way, but only when you run it. Each call uses isolated flags (no settings, no MCP servers, no slash commands, no tools) and runs in a temporary directory with the conversation excerpt on stdin. Disable it with `HANDOFF_SUMMARY=0` (the legacy `HANDOFF_HAIKU_FALLBACK=0` also works).

Since 0.3.0 there is a second call per compaction, the decisions merge, with the same isolated flags (`claude -p --model sonnet` by default, or `HANDOFF_LEDGER_MODEL`). It sends the current decisions list and your messages, your answers to questions, and the assistant's reply before each message, since the last update (at most about 40,000 characters, oldest first; a larger backlog is caught up over several compactions). The reply is validated in code before anything is written. Disable it, and the session-start injection of the list, with `HANDOFF_LEDGER=0`.

Handoff files contain excerpts of your conversation (your recent messages, file names, git status, recent commands). The decision lists contain your project decisions in plain text, including anything you decided in chat, so do not put secrets in chat expecting them to be dropped. Keep `~/.local/state/claude-handoffs/` private and do not commit or share it. Handoff `*.md` files are removed after 365 days (change it with `HANDOFF_RETENTION_DAYS`; `0` keeps them forever). The `*-degraded.json`, `*-last-health.json` and `*-pending.json` files are removed after 14 days. A longer retention means more of your chat text stays on disk for longer. Claude Code deletes its own transcripts after `cleanupPeriodDays` days (default 30); recall can only search what is still there. `./install.sh --keep-transcripts` raises the value to 365 (it never lowers it), and the session-start check warns if it is below 90.

## Reporting a vulnerability

Open a GitHub issue on this repository. If the report includes sensitive details, describe the impact in the issue without the details and ask for a private channel.
