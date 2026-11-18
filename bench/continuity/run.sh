#!/bin/bash
# Continuity benchmark: the one command. See README.md.
#   run.sh slice   [options]      small run: haiku, window 30000, dev seeds
#   run.sh full    [options]      full run: sonnet, window 400000, report seeds
#   run.sh resume  <run-id> [--budget-usd X] [--jobs N]
#   run.sh report  <run-id> [--publish] [--draws N]
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
  slice|full)
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
  aa)      exec python3 "$D/report.py" aa "$@" ;;
  selfcheck) exec python3 "$D/runner.py" selfcheck "$@" ;;
  report)  exec python3 "$D/report.py" "$@" ;;
  cleanup) exec python3 "$D/runner.py" cleanup "$@" ;;
  *) sed -n 2,11p "$0" >&2; exit 2 ;;
esac
