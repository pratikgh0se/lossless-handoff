# Documentation index

This folder holds the detail pages of Lossless Handoff. The short overview is in the [README](../README.md).

## Use it

- [Install and uninstall](install.md): all flags, the saved defaults file, upgrade from 0.12.x, what install changes, headless runs, verify, uninstall.
- [Configuration and state files](configuration.md): every environment variable and every file that the hooks write.
- [Recall](recall.md): word search, meaning search and every `recall.py` command.
- [The project decisions list](decisions-list.md): what goes in, how it is kept and how it is injected.
- [llm_map](llm-map.md): label many items with a small model.
- [FAQ](faq.md) and [Troubleshooting](troubleshooting.md).

## How it works

- [Architecture](architecture.md): parts, events, files, data flow and how Claude decides to use recall.
- [The hooks in detail](hooks-in-detail.md): nudge, handoff, inject, missed compactions and subagents.
- [Reliability notes](handoff-reliability.md): misses, subagents, headless runs, proof and state files.
- [Behaviour contract](spec.md): the contract that the hooks and their tests come from.
- Decisions: [ADR 0001](adr/0001-compact-window-400k-with-handoff.md) (the 400k window), [ADR 0007](adr/0007-handoff-degraded-vs-reduced.md) (degraded vs reduced), [ADR 0008](adr/0008-handoffs-directory-under-local-state.md) (where handoffs live), [ADR 0011](adr/0011-handoff-nudge-bounded-channel.md) (the nudge).

## Evidence and limits

- [Evidence and benchmarks](evidence.md): measurements, proof checks, the claims register and the two benchmarks.
- Benchmarks: [bench/continuity/README.md](../bench/continuity/README.md) and [bench/oolong/README.md](../bench/oolong/README.md).
- [Limitations](limitations.md): the full list.
- [Background](background.md): why the project exists, the LCM paper and other projects.
- [Tests and contributing](testing.md): the three test layers and the code-blind rule.
- Past proof reports: [proof/](proof/).

## Other files

- [CHANGELOG](../CHANGELOG.md) and [SECURITY](../SECURITY.md).
