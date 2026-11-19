# llm_map

Label many items with a small model. This page is part of the [docs index](README.md).

## llm_map: label many items with a small model

`llm_map.py` sends each item of a large list through a small model (Haiku by default) and writes one exact answer for each item. The main model then counts or aggregates the answers with a script, so it does not guess a count from a sample.

When to use it: a task needs a label, class, extracted value or check for each of hundreds of items or more. In OOLONG pilot runs on the author's machine (2026-11-18, results not published), plain Claude Code filtered with a regex, read a sample and estimated the count.

```
python3 ~/.claude/hooks/llm_map.py --in reviews.txt --prompt "Label each review as positive, negative or mixed." --out labels.jsonl [--labels "positive|negative|mixed"] [--model haiku] [--batch 100] [--jobs 8] [--max-usd 2.00] [--json-out]
```

- `--in` is a text file with one item on each line, or a `.jsonl` file with `text` and an optional `id`.
- `--out` gets one JSON line for each item, in input order, with `i`, `text`, `out` and (if it failed) `error`.
- `--labels "a|b|c"` sets the allowed answers (2 or more, split on `|`; not with `--json-out`). `out` is always a canonical label. A reply maps to a label in these cases: it equals a label (trim, case fold), or it is a prefix of exactly one label, or exactly one label is a prefix of it. The prefix must end at a word end of the longer text (`numeric` matches `numeric value`; `h` does not match `human being`). Other replies are invalid. The tool retries them once. Then they get `out` null and `error: "label: <reply>"`.
- The defaults are 100 items in a batch and 8 batches at the same time, so 3,000 items take about one minute. After each batch, stderr gets `llm_map: <done> of <n> items done`.
- The tool retries a missing, invalid or unparsable answer once. The last stdout line is `llm_map: <n> items, <ok> ok, <failed> failed, <b> batches, cost $<x>`. Exit code 3 means that some items failed.
- Cost note: each batch is a real model call and costs money. Haiku batches of 100 items cost a fraction of a cent each. `--max-usd` (default 2.00) stops new batches when the cap would pass. The unsent items get `error: "budget"`.
- Each call runs `claude -p` with no tools, no hooks, no MCP servers and no saved session, in a new temp folder. Lossless Handoff hooks never run for these calls, so they write no handoffs or health rows.
- The installer also writes the skill `~/.claude/skills/llm-map/SKILL.md`. Its description tells Claude Code to use llm_map for tasks that need an answer for each item of a large list. Its body gives the command and the rule to report exact counts and the number of failed items. If a skill file is not ours (it has no marker line), install leaves it unchanged. Uninstall removes only the marked file.
