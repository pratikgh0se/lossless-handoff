# Continuity proof test: result of 2026-11-23

This page is the report of one benchmark run. It is the file `summary.md` of the run, without changes except for the two path placeholders below and one added section at the end ("Added: kind of fact at 10 compactions"). The raw `results.jsonl` of the run stays on the machine that ran it (`bench/continuity/results/` is not committed).

| Item | Value |
|---|---|
| Run id | 20261123T190206-proof |
| Date | 2026-11-23 |
| Claude Code | 2.1.326 |
| Model | Sonnet |
| Context window | 400000 tokens |
| Lossless Handoff | commit 59471d8 |
| Seeds | 2001, 2002, 2003, 2004 (new seeds, never used before) |
| Cost | $149.50 |
| Reproduce | `bench/continuity/run.sh proof` (add `--dry-run` to only print the estimate) |

Placeholders: `<repo>` is the folder of the repository on the machine that ran the test. `<tmp>` is the temporary folder of that machine.

---

# Continuity benchmark: run 20261123T190206-proof


## Proof verdict

**Verdict: PASS.** The run used 4 seeds and is a small benchmark, not the full suite (20 seeds, 30 compactions, Opus: future work).

Rule (thresholds.json, block proof, registered 2026-11-23 before any proof data): types R D X S E C M pooled, cued probes, as in Table 1.

| Check | Rule | Measured | Result |
|---|---|---|---|
| P1 loss: plain Claude Code loses information | A at N=1 minus A at N=10 >= 25 points (pooled), and A at N=10 < A at N=1 in at least 3 of 4 seeds | A N=1 100%, A N=10 58%, drop 42.0 pts; A lower at N=10 in 4 of 4 seeds | PASS |
| P2 fix: Lossless Handoff keeps it | B minus A at N=10 >= 15 points (pooled), 95% bootstrap lower bound over seeds > 0, and B >= A at N=10 in at least 3 of 4 seeds | A N=10 58%, B N=10 100%, gain +42.0 pts, bootstrap lower bound +27.0 pts; B >= A in 4 of 4 seeds | PASS |
| Gate: Amnesia arm Z (no history) | <= 2% | 0% (0/100) | PASS |
| Gate: Oracle arm O (no compaction) | >= 95% | 100% (100/100) | PASS |
| Gate: Fresh control F, arm A | >= 95% | 100% (36/36) | PASS |
| Gate: Fresh control F, arm B | >= 95% | 100% (36/36) | PASS |
| Gate: Fresh control F, arm O | >= 95% | 100% (12/12) | PASS |
| Gate: Parse rate, arm A | >= 98% | 100% (271/272) | PASS |
| Gate: Parse rate, arm B | >= 98% | 100% (272/272) | PASS |
| Gate: Parse rate, arm O | >= 98% | 100% (124/124) | PASS |
| Gate: Refused probe forks (worst seed) | <= 5% | 0 flagged (no row carries the refused flag) | PASS |
| Gate: Missed checkpoints (A, B) | <= 5% | 0% (0/24) | PASS |
| Gate: Seeds with A and B data at N=1 and N=10 | = 4 | 4 of 4 seeds | PASS |

## Side-by-side examples

Items at N=10 where the answers differ. The choice is fixed: sorted by seed, item id and type, no picking by hand.

### A wrong and B right (5 of 42 shown, the first by the order above)

- seed 2001, type E, item 1. Question: I plan to use the `blind-retry-hay` approach for `import-8081`. Should we proceed? In reason_code, copy the exact error code text that this approach failed with, character for character.
  - expected: `proceed false, reason code ERR-6162-D8DMJC`
  - A answered: `{"proceed": false, "reason_code": "UNKNOWN"}`
  - B answered: `{"proceed": false, "reason_code": "ERR-6162-D8DMJC"}`
  - B used a search: yes
- seed 2001, type M, item 1. Question: Earlier in this session I gave three decisions that I labelled `Ordered decision <id>`. List their three ids, earliest first.
  - expected: `order NSKK-7211-TXNE, L3N9-0550-F4DU, 3K49-3054-TNDJ`
  - A answered: `{"order": []}`
  - B answered: `{"order": ["NSKK-7211-TXNE", "L3N9-0550-F4DU", "3K49-3054-TNDJ"]}`
  - B used a search: no
- seed 2001, type S, item 1. Question: Give the exact deploy command for `mirror-3481`, character for character.
  - expected: `rk-ship --lane 5912 --tag JBENXM`
  - A answered: `{"answer": "UNKNOWN"}`
  - B answered: `{"answer": "rk-ship --lane 5912 --tag JBENXM"}`
  - B used a search: yes
- seed 2001, type X, item 1. Question: What is the current queue for `export-2230`? Also list every earlier queue that we replaced.
  - expected: `current ZUWZ-5027-76V5, replaced KU9F-8053-26RK`
  - A answered: `{"current": "ZUWZ-5027-76V5", "replaced": []}`
  - B answered: `{"current": "ZUWZ-5027-76V5", "replaced": ["KU9F-8053-26RK"]}`
  - B used a search: no
- seed 2001, type E, item 2. Question: I plan to use the `shard-merge-vzd` approach for `search-4653`. Should we proceed? In reason_code, copy the exact error code text that this approach failed with, character for character.
  - expected: `proceed false, reason code ERR-5765-5X6TEN`
  - A answered: `{"proceed": false, "reason_code": "UNKNOWN"}`
  - B answered: `{"proceed": false, "reason_code": "ERR-5765-5X6TEN"}`
  - B used a search: yes

### B wrong and A right (all 0, for honesty)

none


**Controls: PASS. Cost: $149.5019. Status: done.** real window 400000, model sonnet, Claude Code 2.1.326 (Claude Code), 4 seeds, gen v5, LH 59471d8

Command: `<repo>/bench/continuity/runner.py run proof --budget-usd 190`. Seeds: 2001,2002,2003,2004. Types: R,D,X,S,E,U,C,M,F. Arms: A,B,O,Z.

Probe design: 4. The cued head of design 2 has an added cue sentence, so its cued tables are not comparable with Wave 1 and Wave 2 (probe design 1).

### Table 1: accuracy by arm and compaction count (types R D X S E C M pooled; U is in its own row in Table 2)

Label: real window 400000, model sonnet, Claude Code 2.1.326 (Claude Code), 4 seeds, gen v5, LH 59471d8. Exploratory unless the pair count passes the threshold. Interval: 95% bootstrap over seeds.

| Arm | N=1 | N=3 | N=10 |
|---|---|---|---|
| A plain | 100% [100%, 100%] (24/24) | 80% [74%, 93%] (61/76) | 58% [49%, 73%] (58/100) |
| B Lossless Handoff | 100% [100%, 100%] (24/24) | 100% [100%, 100%] (76/76) | 100% [100%, 100%] (100/100) |
| B minus A | +0 pts [+0, +0] (4 seeds) | +20 pts [+7, +26] (4 seeds) | +42 pts [+27, +51] (4 seeds) |

### Table 2: accuracy by probe type (cell = rate [interval] (correct/total))

**N = 1**

| Type | A plain | B Lossless Handoff | B minus A |
|---|---|---|---|
| R | 100% [100%, 100%] (4/4) | 100% [100%, 100%] (4/4) | +0 pts [+0, +0] (4 seeds) |
| D | 100% [100%, 100%] (4/4) | 100% [100%, 100%] (4/4) | +0 pts [+0, +0] (4 seeds) |
| X | 100% [100%, 100%] (4/4) | 100% [100%, 100%] (4/4) | +0 pts [+0, +0] (4 seeds) |
| S | 100% [100%, 100%] (4/4) | 100% [100%, 100%] (4/4) | +0 pts [+0, +0] (4 seeds) |
| E | 100% [100%, 100%] (4/4) | 100% [100%, 100%] (4/4) | +0 pts [+0, +0] (4 seeds) |
| U needs subagent search (Phase 3) | 0% [0%, 0%] (0/12) | 67% [25%, 100%] (8/12) | +67 pts [+25, +100] (4 seeds) |
| C | 100% [100%, 100%] (4/4) | 100% [100%, 100%] (4/4) | +0 pts [+0, +0] (4 seeds) |
| M | no data | no data | no data |

**N = 3**

| Type | A plain | B Lossless Handoff | B minus A |
|---|---|---|---|
| R | 100% [100%, 100%] (12/12) | 100% [100%, 100%] (12/12) | +0 pts [+0, +0] (4 seeds) |
| D | 100% [100%, 100%] (12/12) | 100% [100%, 100%] (12/12) | +0 pts [+0, +0] (4 seeds) |
| X | 92% [75%, 100%] (11/12) | 100% [100%, 100%] (12/12) | +8 pts [+0, +25] (4 seeds) |
| S | 50% [33%, 83%] (6/12) | 100% [100%, 100%] (12/12) | +50 pts [+17, +67] (4 seeds) |
| E | 50% [33%, 83%] (6/12) | 100% [100%, 100%] (12/12) | +50 pts [+17, +67] (4 seeds) |
| U needs subagent search (Phase 3) | 0% [0%, 0%] (0/12) | 92% [75%, 100%] (11/12) | +92 pts [+75, +100] (4 seeds) |
| C | 100% [100%, 100%] (12/12) | 100% [100%, 100%] (12/12) | +0 pts [+0, +0] (4 seeds) |
| M | 50% [0%, 100%] (2/4) | 100% [100%, 100%] (4/4) | +50 pts [+0, +100] (4 seeds) |

**N = 10**

| Type | A plain | B Lossless Handoff | B minus A |
|---|---|---|---|
| R | 100% [100%, 100%] (16/16) | 100% [100%, 100%] (16/16) | +0 pts [+0, +0] (4 seeds) |
| D | 100% [100%, 100%] (16/16) | 100% [100%, 100%] (16/16) | +0 pts [+0, +0] (4 seeds) |
| X | 38% [6%, 81%] (6/16) | 100% [100%, 100%] (16/16) | +62 pts [+19, +94] (4 seeds) |
| S | 12% [0%, 38%] (2/16) | 100% [100%, 100%] (16/16) | +88 pts [+62, +100] (4 seeds) |
| E | 12% [0%, 38%] (2/16) | 100% [100%, 100%] (16/16) | +88 pts [+62, +100] (4 seeds) |
| U needs subagent search (Phase 3) | 0% [0%, 0%] (0/12) | 67% [25%, 100%] (8/12) | +67 pts [+25, +100] (4 seeds) |
| C | 100% [100%, 100%] (16/16) | 100% [100%, 100%] (16/16) | +0 pts [+0, +0] (4 seeds) |
| M | 0% [0%, 0%] (0/4) | 100% [100%, 100%] (4/4) | +100 pts [+100, +100] (4 seeds) |

### Table 3: gates (the benchmark must prove itself first)

| Gate | Threshold | Measured | Result |
|---|---|---|---|
| Amnesia arm Z (no history) | <= 2% | 0% (0/100) | PASS |
| Oracle arm O (no compaction) | >= 95% | 100% (100/100) | PASS |
| Fresh control F, arm A | >= 95% | 100% (36/36) | PASS |
| Fresh control F, arm B | >= 95% | 100% (36/36) | PASS |
| Fresh control F, arm O | >= 95% | 100% (12/12) | PASS |
| Parse rate, arm A | >= 98% | 100% (271/272) | PASS |
| Parse rate, arm B | >= 98% | 100% (272/272) | PASS |
| Parse rate, arm O | >= 98% | 100% (124/124) | PASS |
| Parse rate, arm Z (info) | none | 100% (124/124) | INFO |
| Abstain rate, arm A (info) | none | 1% (2/272) | INFO |
| Abstain rate, arm B (info) | none | 0% (0/272) | INFO |
| Abstain rate, arm O (info) | none | 0% (0/124) | INFO |
| Abstain rate, arm Z (info) | none | 0% (0/124) | INFO |
| Missed checkpoints (A, B) | <= 5% | 0% (0/24) | PASS |
| Invalid pairs (seed x N) | <= 20% | 0% (0/12) | PASS |
| Items planted before compaction 1 | 0 compactions at plant end | 12 of 12 sessions with 0 | PASS |
| Probe leaks into the main timeline | 0 | 0 | PASS |
| Arm B hook receipts vs compactions (info) | receipts >= compactions | 40 vs 40 | PASS |
| Type U planted by a subagent (info) | main reads of led-* files = 0 | reads 0, subagent files 36 | PASS |
| Lookup rate, arm A (info) | none | 24% (66/272) | INFO |
| Lookup rate, arm B (info) | none | 31% (83/272) | INFO |

### Headroom (is the benchmark discriminative at this N?)

Rule (thresholds.json): oracle minus arm A at N must be at least 15 points, else no claim either way.

| N | Oracle O | Arm A | O minus A | Result |
|---|---|---|---|---|
| 1 | 100% | 100% | +0 pts | BENCHMARK TOO EASY |
| 3 | 100% | 80% | +20 pts | DISCRIMINATIVE |
| 10 | 100% | 58% | +42 pts | DISCRIMINATIVE |

### Accuracy by item age (compactions since the item was planted; types R D X S E C M)

| Arm | age 1 | age 2 | age 3 | age 6 | age 8 | age 9 | age 10 |
|---|---|---|---|---|---|---|---|
| A plain | 96% (50/52) | 75% (18/24) | 71% (17/24) | 71% (17/24) | 54% (15/28) | 54% (13/24) | 54% (13/24) |
| B Lossless Handoff | 100% (52/52) | 100% (24/24) | 100% (24/24) | 100% (24/24) | 100% (28/28) | 100% (24/24) | 100% (24/24) |

### Gain verdict (pre-registered in thresholds.json)

| Comparison | B minus A | Pairs | Verdict |
|---|---|---|---|
| B minus A at N=1, all types | +0 pts [+0, +0] (4 seeds) | 4 | BENCHMARK TOO EASY at this N: no claim either way |
| B minus A at N=3, all types | +20 pts [+7, +26] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |
| B minus A at N=10, all types | +42 pts [+27, +51] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |
| B minus A at N=30, all types | no data | 0 | NO DATA |
| B minus A at N=10, type X | +62 pts [+19, +94] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |

A result with no gain is stored and shown in the same way as a gain.

### Accuracy with and without lookup (items where the probe used a transcript search or recall.py)

| Arm | no lookup | with lookup |
|---|---|---|
| A plain | 96% (134/140) | 15% (9/60) |
| B Lossless Handoff | 100% (150/150) | 100% (50/50) |
| O no compaction | 100% (100/100) | no data |

### Seed spread (types R D X S E C M pooled; pooled rate, min-max over seeds, each seed)

| Arm | N | Pooled | Min-max over seeds | Per seed |
|---|---|---|---|---|
| A plain | 1 | 100% | 100%-100% | s2001 100%, s2002 100%, s2003 100%, s2004 100% |
| A plain | 3 | 80% | 74%-100% | s2001 100%, s2002 74%, s2003 74%, s2004 74% |
| A plain | 10 | 58% | 48%-80% | s2001 52%, s2002 48%, s2003 80%, s2004 52% |
| B Lossless Handoff | 1 | 100% | 100%-100% | s2001 100%, s2002 100%, s2003 100%, s2004 100% |
| B Lossless Handoff | 3 | 100% | 100%-100% | s2001 100%, s2002 100%, s2003 100%, s2004 100% |
| B Lossless Handoff | 10 | 100% | 100%-100% | s2001 100%, s2002 100%, s2003 100%, s2004 100% |

### Planted answers in the last summary (found/total; types R D S X C U; answer values only)

| Seed | A plain | B Lossless Handoff |
|---|---|---|
| 2001 | 16/23 (10 summaries) | 16/23 (10 summaries) |
| 2002 | 16/23 (10 summaries) | 4/23 (10 summaries) |
| 2003 | 18/23 (10 summaries) | 4/23 (10 summaries) |
| 2004 | 16/23 (10 summaries) | 16/23 (10 summaries) |

Found in each summary, in order:

- arm A seed 2001: 5, 10, 15, 15, 17, 16, 16, 16, 16, 16 of 23
- arm A seed 2002: 5, 9, 13, 13, 17, 17, 17, 16, 16, 16 of 23
- arm A seed 2003: 5, 9, 13, 13, 18, 18, 18, 18, 18, 18 of 23
- arm A seed 2004: 5, 9, 13, 13, 18, 16, 16, 16, 16, 16 of 23
- arm B seed 2001: 5, 9, 13, 13, 17, 16, 16, 16, 16, 16 of 23
- arm B seed 2002: 5, 9, 13, 12, 6, 4, 4, 4, 4, 4 of 23
- arm B seed 2003: 5, 10, 5, 1, 5, 5, 4, 4, 4, 4 of 23
- arm B seed 2004: 5, 10, 13, 13, 17, 16, 16, 16, 16, 16 of 23

SUMMARY COLLAPSE: arm B seed 2002: the last summary keeps 4 of 23 answers, the best earlier one kept 13.
SUMMARY COLLAPSE: arm B seed 2003: the last summary keeps 4 of 23 answers, the best earlier one kept 10.

### Type U lookup trace (information only: tool calls of the fork after the probe)

| Arm | U forks | Forks with any tool call | Forks that called recall |
|---|---|---|---|
| A plain | 12 | 12/12 | 0/12 |
| B Lossless Handoff | 12 | 12/12 | 11/12 |
| O no compaction | 4 | 4/4 | 0/4 |

First recall commands, arm A: none
First recall commands, arm B: 
- `Bash` {"command": "python3 <tmp>/lhbench-20261123T190206-proof-s2001-B-state/hooks/recall.py search \"ledger key\""}
- `Bash` {"command": "python3 <tmp>/lhbench-20261123T190206-proof-s2001-B-state/hooks/recall.py search \"led-[123]\\.txt|ledger key\""}
- `Bash` {"command": "cd <tmp>/lhbench-20261123T190206-proof-s2001-B-state/hooks; for i in 21aae83e/a951450e:15 21aae83e/a4c11584:15 21aae83e/a1f8aab4:15; do python3 recall.py open $i | grep -i -E \"key|ledger\" | head -5; echo ---; done"}
First recall commands, arm O: none

### Cost per compaction (to predict the cost of a full run)

| Arm | Sessions | Main turns | Compactions | Turns per compaction | Main-turn cost per compaction | Cost per fork | Total cost |
|---|---|---|---|---|---|---|---|
| A plain | 4 | 216 | 40 | 5.4 | $1.8064 | $0.0248 | $76.3269 |
| B Lossless Handoff | 4 | 195 | 40 | 4.9 | $1.5746 | $0.0346 | $68.6559 |
| O no compaction | 4 | 96 | 0 | n/a | n/a | $0.0157 | $3.5785 |
| Z no history | 4 | 0 | 0 | n/a | n/a | $0.0168 | $0.9406 |

### Filler turns without a Read call (gen v5 guard)

| Arm | Sessions | Turns with no Read call | Retry turns sent |
|---|---|---|---|
| A plain | 4 | 0 | 0 |
| B Lossless Handoff | 4 | 0 | 0 |
| O no compaction | 4 | 0 | 0 |
| Z no history | 4 | 0 | 0 |

A filler turn with no Read call gets one retry turn before the quiet counter counts it. Runs of gen v4 or older have no data here (shown as 0).


The plant turns (epoch 0) are part of the main-turn cost, so a short run overstates the cost per compaction. Predict a full run as: sessions x (plant cost + compactions x cost per compaction + forks x cost per fork). Take the numbers from a pilot at the same model and window.


## Lost in the middle, phrasing and auto-recall (probe design 2)

Types: R,D,X,S,E,C (M has no single plant turn, so no position). Position = third of the nominal session timeline of the plant turn (start, middle, end). Compaction rows (condition `compact`): probes at the largest N only, and the position is nominal, so read it as 'compactions survived', not as a distance in tokens. No-compaction rows: the one probe after the full session. Cell = rate [95% bootstrap interval over seeds] (correct/total).

### Table L0: did the auto-recall hook fire? (arm R, from auto-recall.jsonl of the arm)

The hook shows at most 3 hits (TOP_HITS) per prompt. A probe asks up to 4 facts, so one fire cannot cover every fact. "Fired with hits" counts forks whose log line has hits above 0.

| Condition | Phrasing | Probe forks | Fired | Rate | Fired with hits > 0 | Timed out |
|---|---|---|---|---|---|---|
| no data | | | | | | |

### Table L1: accuracy by position (arm, condition, phrasing)

| Arm | Condition | Phrasing | start | middle | end | middle minus mean(start, end) |
|---|---|---|---|---|---|---|
| A plain | compact (compactions survived) | cued | 54% [50%, 62%] (26/48) | 62% [50%, 88%] (15/24) | 71% [54%, 92%] (17/24) | +0 pts [-8, +10] (4 seeds) |
| A plain | compact (compactions survived) | neutral | 45% [40%, 55%] (18/40) | 55% [40%, 85%] (11/20) | 65% [45%, 90%] (13/20) | +0 pts [-10, +12] (4 seeds) |
| B Lossless Handoff | compact (compactions survived) | cued | 100% [100%, 100%] (48/48) | 100% [100%, 100%] (24/24) | 100% [100%, 100%] (24/24) | +0 pts [+0, +0] (4 seeds) |
| B Lossless Handoff | compact (compactions survived) | neutral | 95% [85%, 100%] (38/40) | 95% [85%, 100%] (19/20) | 95% [85%, 100%] (19/20) | +0 pts [+0, +0] (4 seeds) |

### Table L2: accuracy by phrasing (all positions, types R D X S E only, so cued and neutral ask the same facts)

| Arm | Condition | cued | neutral | neutral minus cued |
|---|---|---|---|---|
| A plain | compact | 52% [41%, 71%] (42/80) | 52% [41%, 71%] (42/80) | +0 pts [+0, +0] (4 seeds) |
| B Lossless Handoff | compact | 100% [100%, 100%] (80/80) | 95% [85%, 100%] (76/80) | -5 pts [-15, +0] (4 seeds) |

### Table L2b: non-literal minus neutral (no compaction, probe design 3, types R D X S E)

| Arm | neutral | nonliteral | nonliteral minus neutral |
|---|---|---|---|

L2 is not a pure cue contrast. The neutral head also drops "Answer from this session's history only" and adds a rule to answer null when unsure. The cued head of design 2 adds a cue sentence. So L2 compares two prompt styles, not only the cue.

### Table L3: position curve, compaction minus no compaction (cued probes, paired by seed)

| Arm | start | middle | end |
|---|---|---|---|

### Table L5 model_search (H6 uses this): share of probe items whose fork searched by its own choice (recall.py call or an MCP tool call). A row of an old run without search_kinds counts as `either`

| Arm | Condition | Phrasing | start | middle | end |
|---|---|---|---|---|---|
| A plain | compact | cued | 38% [25%, 50%] (18/48) | 38% [25%, 50%] (9/24) | 38% [25%, 50%] (9/24) |
| A plain | compact | neutral | 15% [0%, 30%] (6/40) | 15% [0%, 30%] (3/20) | 15% [0%, 30%] (3/20) |
| B Lossless Handoff | compact | cued | 33% [33%, 33%] (16/48) | 33% [33%, 33%] (8/24) | 33% [33%, 33%] (8/24) |
| B Lossless Handoff | compact | neutral | 40% [40%, 40%] (16/40) | 40% [40%, 40%] (8/20) | 40% [40%, 40%] (8/20) |

### Table L5 injected: share of probe items whose fork got an auto-recall injection in the probe turn (rows of old runs have no kinds: not shown)

| Arm | Condition | Phrasing | start | middle | end |
|---|---|---|---|---|---|
| A plain | compact | cued | 0% [0%, 0%] (0/48) | 0% [0%, 0%] (0/24) | 0% [0%, 0%] (0/24) |
| A plain | compact | neutral | 0% [0%, 0%] (0/40) | 0% [0%, 0%] (0/20) | 0% [0%, 0%] (0/20) |
| B Lossless Handoff | compact | cued | 0% [0%, 0%] (0/48) | 0% [0%, 0%] (0/24) | 0% [0%, 0%] (0/24) |
| B Lossless Handoff | compact | neutral | 0% [0%, 0%] (0/40) | 0% [0%, 0%] (0/20) | 0% [0%, 0%] (0/20) |

### Table L5 either (the old column `searched`: recall.py call, MCP call or auto-recall injection)

| Arm | Condition | Phrasing | start | middle | end |
|---|---|---|---|---|---|
| A plain | compact | cued | 38% [25%, 50%] (18/48) | 38% [25%, 50%] (9/24) | 38% [25%, 50%] (9/24) |
| A plain | compact | neutral | 15% [0%, 30%] (6/40) | 15% [0%, 30%] (3/20) | 15% [0%, 30%] (3/20) |
| B Lossless Handoff | compact | cued | 33% [33%, 33%] (16/48) | 33% [33%, 33%] (8/24) | 33% [33%, 33%] (8/24) |
| B Lossless Handoff | compact | neutral | 40% [40%, 40%] (16/40) | 40% [40%, 40%] (8/20) | 40% [40%, 40%] (8/20) |

### Table L6: NULL CONTROL, B minus A with no compaction (cued)

In the no-compaction condition the core handoff (PreCompact summary, post-compact injection) never runs. B has nothing to restore, so B minus A here must be about 0. Only arms R and K add anything in this condition (auto-recall, recall rules, MCP search, reanchor). Do not credit the core handoff with any lost-in-the-middle gain.

B minus A, no compaction, cued: no data

Registered rule: |B minus A| <= 5 points and the interval holds 0. Result: **NO DATA**

### Table L4: pre-registered hypotheses (thresholds in thresholds.json, committed before any data)

| Hypothesis | Threshold | Measured | Result |
|---|---|---|---|
| H1 lost in the middle: arm A, no compaction, cued: middle minus mean(start, end) | <= -10 pts and CI upper bound < 0; TOO EASY if mean(start, end) > 95% | no data | NO DATA |
| H2 (expected, not blind; mixes compaction with session length) compaction loses facts: arm A cued, compaction minus no compaction | <= -15 pts and CI upper bound < 0 | no data | NO DATA |
| H3 auto-recall helps on cued probes: R minus B, compaction, cued | >= +5 pts and CI lower bound > 0 | no data | NO DATA |
| H4 no gain without a cue: R minus B, compaction, neutral | CI upper bound < +5 pts (no gain of 5 points or more); INVALID if neutral fire rate > 5% | no data | NO DATA |
| H5 (plumbing check, true by construction: the hook trigger depends only on the cue words of the probe text) the hook fires on cued probes and not on neutral probes | cued >= 90% and neutral <= 5% | no arm R data | NO DATA |
| H6 search rate (model_search): K minus B, no compaction, neutral probes (K, no data; reanchor cannot fire here) | > 0 pts and CI lower bound > 0 (K minus B search rate (used_lookup) on neutral probes, no compaction: point above 0 and CI lower bound above 0. Stop rule: if the pilot shows no higher search rate for K, stop and remove the levers.) | no data | NO DATA |
| H7 accuracy: K minus B, no compaction, neutral probes (K, no data; no claim about all levers) | >= +5 pts and CI lower bound > 0; TOO EASY if B accuracy > 95% | no data | NO DATA |

A hypothesis that is NOT SUPPORTED or REFUTED stays in the report. The thresholds do not change after a run.

---

## Added: kind of fact at 10 compactions

This section was added after the run. It counts the rows of `results.jsonl` with arm A or B, condition `compact` and `n_target` 10. It pools the cued and the neutral probes for the types R D X S E, so these counts are larger than in Table 2 (cued only). C, M and U have cued probes only, so their counts equal Table 2. The data is the same run, 4 seeds (2001 to 2004).

| Kind of fact | Type | Where the fact lived | A plain | B Lossless Handoff |
|---|---|---|---|---|
| Rule the user stated | R | user message | 32/32 (100%) | 32/32 (100%) |
| Decision | D | user message | 32/32 (100%) | 32/32 (100%) |
| Conflict, latest word wins | C | user message | 16/16 (100%) | 16/16 (100%) |
| Decision that replaced an older one | X | user message | 12/32 (38%) | 32/32 (100%) |
| Exact string, for example a deploy command | S | only in a file the agent read once | 4/32 (12%) | 28/32 (88%) |
| Dead end: exact error code of a failed approach | E | only in tool output | 4/32 (12%) | 32/32 (100%) |
| Order of three decisions | M | user messages at different times | 0/4 (0%) | 4/4 (100%) |
| Fact that only a subagent saw | U | subagent | 0/12 (0%) | 8/12 (67%) |

Plain Claude Code keeps the simple things the user said. It loses changes, order, exact strings and tool output.

Row U is different. The main session never saw this fact, and plain Claude Code scored 0 of 12 with no compaction too. It shows what recall adds.

At 10 compactions, 76 of the 192 probes of arm B in the table above used a lookup (a recall or transcript search) by their own choice: all 32 E probes, all 32 S probes and all 12 U probes. The fresh control (type F, 12 rows) is not in the table.
