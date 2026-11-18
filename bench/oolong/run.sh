#!/bin/bash
# OOLONG benchmark: the one command. See README.md.
#   run.sh fetch [--lens 32768,131072] [--cache DIR]   get the data (network, harness side only)
#   run.sh selftest [--data DIR]                        scorer self-test
#   run.sh run [options]                                run; then report (python3 harness.py run --help)
#   run.sh resume <run-id> [--budget-usd X] [--jobs N]
#   run.sh report <run-id> [--draws N]
#   run.sh selfcheck [--model M] [--arms A,B,C]         sandbox proof, stores evidence/
#   run.sh cleanup [--all] [--dry-run] [--work-base DIR]
# Exit: 0 finished, 1 a unit failed, 2 usage error, 3 paused (budget).
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  fetch)    exec python3 "$D/fetch.py" "$@" ;;
  selftest) exec python3 "$D/test_scorer.py" "$@" ;;
  run|resume)
    python3 "$D/harness.py" "$cmd" "$@"
    rc=$?
    last="$(cat "$D/results/.last" 2>/dev/null)"
    [ "$cmd" = resume ] && last="${1:-$last}"
    if [ "$rc" -ne 3 ] && [ -n "$last" ] && [ -s "$D/results/$last/results.jsonl" ]; then
      python3 "$D/report.py" "$last" >/dev/null || rc=$((rc == 0 ? 1 : rc))
      echo "report: $D/results/$last/summary.md"
    fi
    exit "$rc" ;;
  report)   exec python3 "$D/report.py" "$@" ;;
  selfcheck) exec python3 "$D/harness.py" selfcheck "$@" ;;
  cleanup)  exec python3 "$D/harness.py" cleanup "$@" ;;
  *) sed -n 2,11p "$0" >&2; exit 2 ;;
esac
