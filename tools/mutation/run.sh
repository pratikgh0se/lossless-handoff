#!/usr/bin/env bash
# F91.d mutation gate: plant one seeded fault at a time and see whether the tests catch it.
# A manual or weekly command. CI does not run it. Each fault is a small unified diff in tools/mutation/faults/.
# The first line of a diff names the fault: "# fault <id> | feature <F> | type <type> | tests: <file> <file>".
#
# Usage:
#   tools/mutation/run.sh                 run every fault
#   tools/mutation/run.sh M01 M05         run only these faults
#   tools/mutation/run.sh --list          print "<id> <feature> <type>" for each fault, run nothing
#   tools/mutation/run.sh --check-diffs   run "git apply --check" for each diff against this repo, run nothing
#
# For each fault: copy the repo to a temp folder, apply the diff, run tests/smoke.sh. If LH_TESTS_DIR is set (a clone of
# the code-blind tests repo), also run the test files that the header names. One line is printed per fault:
#   <id> <feature> <type> caught|survived
# At the end: "mutation score: <caught>/<total>".
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FDIR="$ROOT/tools/mutation/faults"

fid() { basename "$1" .diff; }
hfeature() { sed -n '1s/^.*| feature \([^ ]*\) |.*$/\1/p' "$1"; }
htype() { sed -n '1s/^.*| type \([^ ]*\) |.*$/\1/p' "$1"; }
htests() { sed -n '1s/^.*| tests: *//p' "$1"; }

MODE=run
WANT=()
for a in "$@"; do
  case "$a" in
    --list) MODE=list ;;
    --check-diffs) MODE=check ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) WANT+=("$a") ;;
  esac
done

FILES=()
for f in "$FDIR"/*.diff; do
  [ -e "$f" ] || continue
  if [ "${#WANT[@]}" -gt 0 ]; then
    hit=0
    for w in "${WANT[@]}"; do [ "$w" = "$(fid "$f")" ] && hit=1; done
    [ "$hit" = 1 ] || continue
  fi
  FILES+=("$f")
done
[ "${#FILES[@]}" -gt 0 ] || { echo "no faults found" >&2; exit 2; }

if [ "$MODE" = list ]; then
  for f in "${FILES[@]}"; do echo "$(fid "$f") $(hfeature "$f") $(htype "$f")"; done
  exit 0
fi

if [ "$MODE" = check ]; then
  bad=0
  for f in "${FILES[@]}"; do
    if (cd "$ROOT" && git apply --check "$f"); then echo "ok: $(fid "$f") applies"; else echo "FAIL: $(fid "$f") does not apply" >&2; bad=1; fi
  done
  exit "$bad"
fi

W0="$(mktemp -d)"
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$W0"' EXIT
caught=0; total=0
for f in "${FILES[@]}"; do
  id="$(fid "$f")"; W="$W0/$id"; mkdir -p "$W"
  (cd "$ROOT" && tar cf - --exclude=.git .) | (cd "$W" && tar xf -)
  total=$((total + 1))
  verdict=survived
  if ! (cd "$W" && git apply "$f") 2>"$W0/$id.apply"; then
    echo "$id: the diff does not apply: $(cat "$W0/$id.apply")" >&2
    verdict=survived
  else
    if ! LH_MUTATION_RUN=1 bash "$W/tests/smoke.sh" >"$W0/$id.smoke" 2>&1; then
      verdict=caught
    elif [ -n "${LH_TESTS_DIR:-}" ]; then
      for t in $(htests "$f"); do
        if ! HANDOFF_HOOKS_DIR="$W/hooks" LOSSLESS_HANDOFF_REPO="$W" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 \
            python3 -W ignore -m unittest discover -s "$LH_TESTS_DIR" -p "$t" >"$W0/$id.$t.out" 2>&1; then
          verdict=caught
          break
        fi
      done
    fi
  fi
  [ "$verdict" = caught ] && caught=$((caught + 1))
  echo "$id $(hfeature "$f") $(htype "$f") $verdict"
done
echo "mutation score: $caught/$total"
