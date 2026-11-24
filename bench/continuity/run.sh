#!/bin/bash
# Continuity benchmark: the one command. See README.md.
#   run.sh slice   [options]      small run: haiku, window 30000, dev seeds
#   run.sh full    [options]      full run: sonnet, window 400000, report seeds
#   run.sh preflight [options]    the cheapest full check (Haiku, under $3). It ends with a PASS/FAIL checklist (preflight.py)
#   run.sh check <run-id>         print the preflight checklist of a finished run again
#   run.sh litm3-smoke|litm3-nc-smoke|litm3-pilot|litm3-full   probe design 3 (about 300k tokens no-compaction, non-literal probes, arm K)
#   run.sh litm4-oracle-check|litm4-calibrate|litm4-calibrate-l5|litm4-smoke|litm4-pilot|litm4-full-v3   probe design 4 (update chains with interference, no-compaction condition; compaction as in design 3)
#   run.sh preflight4 [options]   the preflight with design 4 (Haiku). run.sh decide <run-id> prints the pick rule of a calibration run
#   run.sh proof-recheck [--compare ID]  arm B only on the proof seeds after F81, about $70, no PASS or FAIL (add --dry-run for the estimate)
#   run.sh proof   [options]      the proof test: 4 new seeds, arms A B O Z, N 1 3 10, about $165 (add --dry-run to only print the estimate). The live selfcheck runs first
#   run.sh litm-smoke [options]   1 dev seed, arms A and R, no compaction only (about $12)
#   run.sh litm-pilot [options]   lost-in-the-middle pilot: 2 dev seeds, arms A,B,R, both conditions, both phrasings (add --dry-run to only print the cost)
#   run.sh litm-full  [options]   same design, 20 report seeds, arms A,B,R,Z,O
#   run.sh retrieval-gate --trigger | --replay [--project DIR]            F79 ($0): F77 trigger gate with oracle replies; replay on real transcripts (counts only)
#   run.sh retrieval-gate [--level L] [--seeds FILE] [--hooks-dir DIR]   offline retrieval gate (F76, $0): the hooks of a folder on synthetic transcripts
#   run.sh estimate               print the pilot cost and the full cost of the lost-in-the-middle design (no model call)
#   run.sh resume  <run-id> [--budget-usd X] [--jobs N]
#   run.sh report  <run-id> [--publish] [--draws N]
#   run.sh regrade <run-id|results-folder>   (grade the stored raw replies again, rebuild the report, no model call)
#   run.sh aa      <run-id-1> <run-id-2> [--arm A|B] [--draws N]   (A/A noise floor)
#   run.sh selfcheck [--model M] [--lh-path P]   (live check of the transcript deny rules)
#   run.sh cleanup [--all] [--dry-run] [--work-base DIR]
# Options of slice and full: run `python3 runner.py run slice --help`.
# Exit: 0 finished, 1 a session or a control gate failed, 2 usage error, 3 paused (budget), 4 stopped by an abort gate (see gates.py).
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  slice|full|proof|proof-recheck|preflight|litm-smoke|litm-pilot|litm-full|litm3-smoke|litm3-nc-smoke|litm3-pilot|litm3-full|litm3-full-v3|litm3-full-v5|litm4-oracle-check|litm4-calibrate|litm4-calibrate-l5|litm4-smoke|litm4-pilot|litm4-full-v3|preflight4)
    python3 "$D/runner.py" run "$cmd" "$@"
    rc=$?
    if { [ "$cmd" = preflight ] || [ "$cmd" = preflight4 ]; } && [ "$rc" -ne 3 ] && [ "$rc" -ne 4 ]; then
      python3 "$D/preflight.py" "$(cat "$D/results/.last" 2>/dev/null)" || rc=1
    fi
    last="$(cat "$D/results/.last" 2>/dev/null)"
    if [ "$rc" -ne 3 ] && [ -n "$last" ] && [ -s "$D/results/$last/results.jsonl" ]; then
      python3 "$D/report.py" "$last" >/dev/null || rc=$((rc == 0 ? 1 : rc))
      echo "report: $D/results/$last/summary.md"
    fi
    exit "$rc" ;;
  resume)
    [ $# -ge 1 ] || { echo "usage: run.sh resume <run-id> [--budget-usd X] [--jobs N]" >&2; exit 2; }
    id="$1"; shift
    python3 "$D/runner.py" resume "$id" "$@"
    rc=$?
    if [ "$rc" -ne 3 ] && [ -s "$D/results/$id/results.jsonl" ]; then
      python3 "$D/report.py" "$id" >/dev/null || rc=$((rc == 0 ? 1 : rc))
      echo "report: $D/results/$id/summary.md"
    fi
    exit "$rc" ;;
  regrade) exec python3 "$D/regrade.py" "$@" ;;
  aa)      exec python3 "$D/report.py" aa "$@" ;;
  decide)  exec python3 "$D/design4.py" decide "$@" ;;
  oracle-check)  exec python3 "$D/design4.py" oracle-check "$@" ;;
  retrieval-gate) exec python3 "$D/retrieval_gate.py" "$@" ;;
  check)   exec python3 "$D/preflight.py" "$@" ;;
  estimate) exec python3 "$D/runner.py" estimate ;;
  selfcheck) exec python3 "$D/runner.py" selfcheck "$@" ;;
  report)  exec python3 "$D/report.py" "$@" ;;
  cleanup) exec python3 "$D/runner.py" cleanup "$@" ;;
  *) sed -n 2,12p "$0" >&2; exit 2 ;;
esac
