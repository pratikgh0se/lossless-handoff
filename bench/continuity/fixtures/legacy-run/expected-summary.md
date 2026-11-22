# Continuity benchmark: run legacy-run

**Controls: FAIL. Cost: $1.0000. Status: done.** real window 400000, model sonnet, Claude Code fixture, 4 seeds, gen v5, LH fixture

Command: `fixture`. Seeds: 1000,1001,1002,1003. Types: R,D,X,S,E,C,M,F. Arms: A,B,Z,O.

### Table 1: accuracy by arm and compaction count (types R D X S E C M pooled; U is in its own row in Table 2)

Label: real window 400000, model sonnet, Claude Code fixture, 4 seeds, gen v5, LH fixture. Exploratory unless the pair count passes the threshold. Interval: 95% bootstrap over seeds.

| Arm | N=1 | N=3 | N=10 |
|---|---|---|---|
| A plain | 25% [8%, 42%] (6/24) | 28% [13%, 39%] (21/76) | 24% [18%, 30%] (24/100) |
| B Lossless Handoff | 75% [58%, 83%] (18/24) | 70% [58%, 82%] (53/76) | 71% [65%, 80%] (71/100) |
| B minus A | +50 pts [+25, +75] (4 seeds) | +42 pts [+26, +58] (4 seeds) | +47 pts [+38, +58] (4 seeds) |

### Table 2: accuracy by probe type (cell = rate [interval] (correct/total))

**N = 1**

| Type | A plain | B Lossless Handoff | B minus A |
|---|---|---|---|
| R | 25% [0%, 75%] (1/4) | 75% [25%, 100%] (3/4) | +50 pts [+0, +100] (4 seeds) |
| D | 0% [0%, 0%] (0/4) | 100% [100%, 100%] (4/4) | +100 pts [+100, +100] (4 seeds) |
| X | 25% [0%, 75%] (1/4) | 75% [25%, 100%] (3/4) | +50 pts [+0, +100] (4 seeds) |
| S | 50% [0%, 100%] (2/4) | 50% [0%, 100%] (2/4) | +0 pts [+0, +0] (4 seeds) |
| E | 0% [0%, 0%] (0/4) | 75% [25%, 100%] (3/4) | +75 pts [+25, +100] (4 seeds) |
| U needs subagent search (Phase 3) | no data | no data | no data |
| C | 50% [0%, 100%] (2/4) | 75% [25%, 100%] (3/4) | +25 pts [-50, +100] (4 seeds) |
| M | no data | no data | no data |

**N = 3**

| Type | A plain | B Lossless Handoff | B minus A |
|---|---|---|---|
| R | 25% [8%, 33%] (3/12) | 67% [33%, 100%] (8/12) | +42 pts [+17, +67] (4 seeds) |
| D | 33% [0%, 67%] (4/12) | 58% [33%, 83%] (7/12) | +25 pts [-17, +50] (4 seeds) |
| X | 33% [0%, 67%] (4/12) | 75% [50%, 100%] (9/12) | +42 pts [-17, +100] (4 seeds) |
| S | 33% [8%, 50%] (4/12) | 67% [50%, 92%] (8/12) | +33 pts [+8, +58] (4 seeds) |
| E | 25% [0%, 50%] (3/12) | 67% [42%, 92%] (8/12) | +42 pts [-8, +67] (4 seeds) |
| U needs subagent search (Phase 3) | no data | no data | no data |
| C | 17% [0%, 33%] (2/12) | 83% [67%, 100%] (10/12) | +67 pts [+33, +100] (4 seeds) |
| M | 25% [0%, 75%] (1/4) | 75% [25%, 100%] (3/4) | +50 pts [-50, +100] (4 seeds) |

**N = 10**

| Type | A plain | B Lossless Handoff | B minus A |
|---|---|---|---|
| R | 31% [0%, 62%] (5/16) | 69% [50%, 88%] (11/16) | +38 pts [-6, +69] (4 seeds) |
| D | 38% [25%, 50%] (6/16) | 75% [44%, 100%] (12/16) | +38 pts [+12, +62] (4 seeds) |
| X | 31% [12%, 50%] (5/16) | 56% [50%, 69%] (9/16) | +25 pts [+6, +44] (4 seeds) |
| S | 6% [0%, 19%] (1/16) | 81% [75%, 94%] (13/16) | +75 pts [+62, +94] (4 seeds) |
| E | 19% [6%, 25%] (3/16) | 62% [38%, 88%] (10/16) | +44 pts [+25, +62] (4 seeds) |
| U needs subagent search (Phase 3) | no data | no data | no data |
| C | 25% [6%, 38%] (4/16) | 81% [44%, 100%] (13/16) | +56 pts [+38, +75] (4 seeds) |
| M | 0% [0%, 0%] (0/4) | 75% [25%, 100%] (3/4) | +75 pts [+25, +100] (4 seeds) |

### Table 3: gates (the benchmark must prove itself first)

| Gate | Threshold | Measured | Result |
|---|---|---|---|
| Amnesia arm Z (no history) | <= 2% | 0% (0/100) | PASS |
| Oracle arm O (no compaction) | >= 95% | 94% (94/100) | FAIL |
| Fresh control F, arm A | >= 95% | 100% (36/36) | PASS |
| Fresh control F, arm B | >= 95% | 94% (34/36) | FAIL |
| Fresh control F, arm O | >= 95% | 100% (12/12) | PASS |
| Parse rate, arm A | >= 98% | 100% (236/236) | PASS |
| Parse rate, arm B | >= 98% | 100% (236/236) | PASS |
| Parse rate, arm O | >= 98% | 100% (112/112) | PASS |
| Parse rate, arm Z (info) | none | 100% (112/112) | INFO |
| Abstain rate, arm A (info) | none | 0% (0/236) | INFO |
| Abstain rate, arm B (info) | none | 0% (0/236) | INFO |
| Abstain rate, arm O (info) | none | 0% (0/112) | INFO |
| Abstain rate, arm Z (info) | none | 0% (0/112) | INFO |
| Missed checkpoints (A, B) | <= 5% | 0% (0/24) | PASS |
| Invalid pairs (seed x N) | <= 20% | 0% (0/12) | PASS |
| Items planted before compaction 1 | 0 compactions at plant end | 12 of 12 sessions with 0 | PASS |
| Probe leaks into the main timeline | 0 | 0 | PASS |
| Arm B hook receipts vs compactions (info) | receipts >= compactions | 0 vs 40 | CHECK |
| Type U planted by a subagent (info) | main reads of led-* files = 0 | reads 0, subagent files 0 | PASS |
| Lookup rate, arm A (info) | none | 0% (0/236) | INFO |
| Lookup rate, arm B (info) | none | 0% (0/236) | INFO |

### Headroom (is the benchmark discriminative at this N?)

Rule (thresholds.json): oracle minus arm A at N must be at least 15 points, else no claim either way.

| N | Oracle O | Arm A | O minus A | Result |
|---|---|---|---|---|
| 1 | 94% | 25% | +69 pts | DISCRIMINATIVE |
| 3 | 94% | 28% | +66 pts | DISCRIMINATIVE |
| 10 | 94% | 24% | +70 pts | DISCRIMINATIVE |

### Accuracy by item age (compactions since the item was planted; types R D X S E C M)

| Arm | age 1 | age 2 | age 3 | age 6 | age 8 | age 9 | age 10 |
|---|---|---|---|---|---|---|---|
| A plain | 29% (15/52) | 33% (8/24) | 17% (4/24) | 17% (4/24) | 36% (10/28) | 21% (5/24) | 21% (5/24) |
| B Lossless Handoff | 73% (38/52) | 67% (16/24) | 71% (17/24) | 92% (22/24) | 68% (19/28) | 67% (16/24) | 58% (14/24) |

### Gain verdict (pre-registered in thresholds.json)

| Comparison | B minus A | Pairs | Verdict |
|---|---|---|---|
| B minus A at N=1, all types | +50 pts [+25, +75] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |
| B minus A at N=3, all types | +42 pts [+26, +58] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |
| B minus A at N=10, all types | +47 pts [+38, +58] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |
| B minus A at N=30, all types | no data | 0 | NO DATA |
| B minus A at N=10, type X | +25 pts [+6, +44] (4 seeds) | 4 | SMALL SAMPLE (4 pairs, minimum 20): no claim |

A result with no gain is stored and shown in the same way as a gain.

### Accuracy with and without lookup (items where the probe used a transcript search or recall.py)

| Arm | no lookup | with lookup |
|---|---|---|
| A plain | 26% (51/200) | no data |
| B Lossless Handoff | 71% (142/200) | no data |
| O no compaction | 94% (94/100) | no data |

### Seed spread (types R D X S E C M pooled; pooled rate, min-max over seeds, each seed)

| Arm | N | Pooled | Min-max over seeds | Per seed |
|---|---|---|---|---|
| A plain | 1 | 25% | 0%-50% | s1000 33%, s1001 0%, s1002 50%, s1003 17% |
| A plain | 3 | 28% | 5%-42% | s1000 42%, s1001 26%, s1002 5%, s1003 37% |
| A plain | 10 | 24% | 16%-32% | s1000 20%, s1001 16%, s1002 28%, s1003 32% |
| B Lossless Handoff | 1 | 75% | 50%-83% | s1000 50%, s1001 83%, s1002 83%, s1003 83% |
| B Lossless Handoff | 3 | 70% | 53%-84% | s1000 79%, s1001 84%, s1002 63%, s1003 53% |
| B Lossless Handoff | 10 | 71% | 64%-84% | s1000 84%, s1001 64%, s1002 68%, s1003 68% |

### Planted answers in the last summary (found/total; types R D S X C U; answer values only)

no data (no retention.jsonl, or no summary in it: the transcripts of this run were already deleted)

### Type U lookup trace (information only: tool calls of the fork after the probe)

no data (the result rows hold no lookup_calls)

### Cost per compaction (to predict the cost of a full run)

| Arm | Sessions | Main turns | Compactions | Turns per compaction | Main-turn cost per compaction | Cost per fork | Total cost |
|---|---|---|---|---|---|---|---|
| A plain | 4 | 80 | 40 | 2.0 | $0.0000 | n/a | $0.0000 |
| B Lossless Handoff | 4 | 80 | 40 | 2.0 | $0.0000 | n/a | $0.0000 |
| O no compaction | 4 | 80 | 0 | n/a | n/a | n/a | $0.0000 |

### Filler turns without a Read call (gen v5 guard)

| Arm | Sessions | Turns with no Read call | Retry turns sent |
|---|---|---|---|
| A plain | 4 | 0 | 0 |
| B Lossless Handoff | 4 | 0 | 0 |
| O no compaction | 4 | 0 | 0 |

A filler turn with no Read call gets one retry turn before the quiet counter counts it. Runs of gen v4 or older have no data here (shown as 0).


The plant turns (epoch 0) are part of the main-turn cost, so a short run overstates the cost per compaction. Predict a full run as: sessions x (plant cost + compactions x cost per compaction + forks x cost per fork). Take the numbers from a pilot at the same model and window.

