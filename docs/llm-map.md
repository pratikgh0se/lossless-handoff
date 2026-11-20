# llm-map has moved

llm_map is now a separate project. This page is part of the [docs index](README.md).

**New home:** https://github.com/pratikgh0se/llm-map

## What changed

`llm_map.py` labels, classifies or extracts a value for each item of a large list with a small model. Until release 0.13.x it was part of Lossless Handoff. Since release 0.14.0 it is a project of its own. It has no link to the handoff hooks, so it has its own life cycle and its own releases.

This repository no longer ships `hooks/llm_map.py` or the llm-map skill. `install.sh` no longer installs them.

## Upgrade notes

- **You have llm-map from an older install.** Nothing breaks. The installer keeps `~/.claude/hooks/llm_map.py` and `~/.claude/skills/llm-map/SKILL.md`. It prints one line that says where they are maintained now. It does not update them. `./uninstall.sh` never removes them: the llm-map project uses the same paths. It prints `llm-map: kept (remove it with ./install.sh --uninstall from https://github.com/pratikgh0se/llm-map)`.
- **You want llm-map now.** Clone https://github.com/pratikgh0se/llm-map and run its `./install.sh` (use `--dry-run` first). It uses the same paths as before: `~/.claude/hooks/llm_map.py` and `~/.claude/skills/llm-map/SKILL.md`. It has its own `--uninstall`.
- **You use `--llm-map` or `--no-llm-map`.** Both flags are still accepted. Each prints `llm-map is now a separate project: https://github.com/pratikgh0se/llm-map` and does nothing else. The exit code does not change.
- **You have `llm_map=1` in `~/.config/lossless-handoff/install-defaults`.** The installer ignores the line and prints the same message. `--save-defaults` removes it from the file.
- **You run the OOLONG benchmark.** Arm C is now Lossless Handoff alone. To add llm_map, use `--llm-map-path <dir>` with a checkout of https://github.com/pratikgh0se/llm-map. See [bench/oolong/README.md](../bench/oolong/README.md).
