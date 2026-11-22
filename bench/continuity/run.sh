#!/bin/bash
# Continuity benchmark: the one command. See README.md.
#   run.sh slice   [options]      small run: haiku, window 30000, dev seeds
#   run.sh full    [options]      full run: sonnet, window 400000, report seeds
#   run.sh litm-smoke [options]   1 dev seed, arms A and R, no compaction only (about $12)
#   run.sh litm-pilot [options]   lost-in-the-middle pilot: 2 dev seeds, arms A,B,R, both conditions, both phrasings (add --dry-run to only print the cost)
#   run.sh litm-full  [options]   same design, 20 report seeds, arms A,B,R,Z,O
#   run.sh estimate               print the pilot cost and the full cost of the lost-in-the-middle design (no model call)
#   run.sh resume  <run-id> [--budget-usd X] [--jobs N]
#   run.sh report  <run-id> [--publish] [--draws N]
#   run.sh regrade <run-id|results-folder>   (grade the stored raw replies again, rebuild the report, no model call)
#   run.sh aa      <run-id-1> <run-id-2> [--arm A|B] [--draws N]   (A/A noise floor)
#   run.sh selfcheck [--model M] [--lh-path P]   (live check of the transcript deny rules)
#   run.sh cleanup [--all] [--dry-run] [--work-base DIR]
# Options of slice and full: run `python3 runner.py run slice --help`.
# Exit: 0 finished, 1 a session or a control gate failed, 2 usage error, 3 paused (budget).
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  slice|full|litm-smoke|litm-pilot|litm-full)
    python3 "$D/runner.py" run "$cmd" "$@"
    rc=$?
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
  estimate) exec python3 "$D/runner.py" estimate ;;
  selfcheck) exec python3 "$D/runner.py" selfcheck "$@" ;;
  report)  exec python3 "$D/report.py" "$@" ;;
  cleanup) exec python3 "$D/runner.py" cleanup "$@" ;;
  *) sed -n 2,12p "$0" >&2; exit 2 ;;
esac
