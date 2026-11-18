---
name: llm-map
description: Use it to label, classify, extract or check each item of a large list (hundreds of items or more) with a small model, instead of estimating from a sample. Use it for exact counts and per-item answers.
---
<!-- lossless-handoff:llm-map-skill -->
Use llm_map when a task needs an answer for each item of a large list. Do not read a sample and estimate a count.

Steps:
1. Write the items to a file with a short script. Write one item per line, or a `.jsonl` file with `id` and `text`.
2. Run llm_map. A small model labels each item.
3. Read the `out` values of the output file with a script. Count or aggregate them there.

Command:
`python3 ~/.claude/hooks/llm_map.py --in <file> --prompt <text> --out <file> [--model haiku] [--batch 40] [--jobs 4] [--max-usd 2.00] [--json-out]`

- `--prompt` says what to answer for each item, for example the allowed labels.
- `--json-out` lets `out` be any JSON value. Without it, `out` is a string.
- `--max-usd` is a cost cap. Items that the cap leaves unsent get `error: "budget"`.
- The output file has one JSON line per item, in input order: `i`, `id` (if given), `text`, `out`, and `error` when the item failed.
- The last line on stdout is a summary: items, ok, failed, batches and cost.
- The exit code is 0 when all items have `out`, 3 when some failed, and 2 on a usage error.

Example (the items are already in `items.txt`, one per line):
```
python3 ~/.claude/hooks/llm_map.py --in items.txt --out labels.jsonl --prompt "Label each question as one of: ABBR, ENTY, DESC, HUM, LOC, NUM. Answer with the label only."
python3 -c "import json,collections; print(collections.Counter(json.loads(l)['out'] for l in open('labels.jsonl')))"
```

Rule: report exact counts from the output file. Say how many items failed. Do not fill a failed item with a guess.
