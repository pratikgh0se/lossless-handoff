---
name: llm-map
description: Use it to label, classify, extract or check each item of a large list (hundreds of items or more) with a small model, instead of estimating from a sample. Use it to count or aggregate over hundreds or thousands of items, and for exact per-item answers.
---
<!-- lossless-handoff:llm-map-skill -->
Use llm_map when a task needs an answer for each item of a large list. Do not read a sample and estimate a count.

Steps:
1. Write the items to a file with a short script. Write one item per line, or a `.jsonl` file with `id` and `text`. Use relative paths in the current folder (not `$TMPDIR`).
2. Run llm_map as its own Bash call (no `cd`, no `&&` chain, no redirect in the same call). Give the Bash call a timeout of 600000 ms. Wait for it to end in the foreground.
3. Read the summary line first. Then count or aggregate the `out` values of the output file with a script.

A run of a few thousand items takes about one minute. Do not poll for it with `pgrep` or `ps`.

Command:
`python3 ~/.claude/hooks/llm_map.py --in <file> --prompt <text> --out <file> [--labels "a|b|c"] [--model haiku] [--batch 100] [--jobs 8] [--max-usd 2.00] [--json-out]`

- `--labels` gives the exact set of allowed answers. Always give it for a classification. `out` is then always one of the labels.
- `--prompt` says what to answer for each item.
- `--json-out` lets `out` be any JSON value. Do not use it with `--labels`.
- `--max-usd` is a cost cap. Items that the cap leaves unsent get `error: "budget"`.
- The output file has one JSON line per item, in input order: `i`, `id` (if given), `text`, `out`, and `error` when the item failed.
- The last line on stdout is a summary: items, ok, failed, batches and cost. Progress lines go to stderr.
- The exit code is 0 when all items have `out`, 3 when some failed, and 2 on a usage error.

Example (customer reviews are already in `reviews.txt`, one per line):
```
python3 ~/.claude/hooks/llm_map.py --in reviews.txt --out labels.jsonl --labels "positive|negative|mixed" --prompt "Label the tone of each review."
python3 -c "import json,collections; print(collections.Counter(json.loads(l)['out'] for l in open('labels.jsonl')))"
```

Rule: report exact counts from the output file. Say how many items failed. Do not fill a failed item with a guess.
