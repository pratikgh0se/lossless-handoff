#!/usr/bin/env bash
# Local proof runner for Lossless Handoff.
# Runs every test layer from one command and writes a dated report in docs/proof/.
#
# Usage: tests/local-proof.sh [--no-real] [--real-only] [--tests-dir DIR] [--repeat N]
#   --no-real        skip layers 5 and 6 (the real Claude Code compactions; they cost money)
#   --real-only      run only layers 5 and 6 (and the isolation check)
#   --tests-dir DIR  use an existing checkout of lossless-handoff-tests
#   --repeat N       how many times to run the acceptance suite (default 3)
#
# Layers: 1 static, 2 smoke, 3 acceptance, 4 fresh clone, 5 real end to end (3 runs),
#         6 real stress (parallel, killed hook, missing hook), 7 claims lint (information only).
# Safety: every install uses a temporary HOME and LOSSLESS_HANDOFF_NO_LAUNCHCTL=1. The real
# ~/.claude, the real state folder and the real launchd are only read, never written.
# Exit 0 only when every layer except layer 7 passes.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_REPO_URL="git@github.com:pratikgh0se/lossless-handoff-tests.git"
TESTS_REPO_HTTPS="https://github.com/pratikgh0se/lossless-handoff-tests.git"
CLONE_URL="https://github.com/pratikgh0se/lossless-handoff"
COST_CAP="4.00"

NO_REAL=0; REAL_ONLY=0; TESTS_DIR=""; REPEAT=3
while [ $# -gt 0 ]; do
  case "$1" in
    --no-real) NO_REAL=1 ;;
    --real-only) REAL_ONLY=1 ;;
    --tests-dir) shift; TESTS_DIR="${1:-}" ;;
    --repeat) shift; REPEAT="${1:-3}" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$NO_REAL" = 1 ] && [ "$REAL_ONLY" = 1 ]; then echo "--no-real and --real-only cannot be used together" >&2; exit 2; fi
case "$REPEAT" in ''|*[!0-9]*) echo "--repeat needs a number" >&2; exit 2 ;; esac
[ "$REPEAT" -ge 1 ] || { echo "--repeat needs 1 or more" >&2; exit 2; }
if [ -n "$TESTS_DIR" ]; then TESTS_DIR="$(cd "$TESTS_DIR" && pwd)" || exit 2; fi

REAL_HOME="$HOME"
RAW="$(mktemp -d "${TMPDIR:-/tmp}/lossless-proof.XXXXXX")"
RAW="$(cd "$RAW" && pwd -P)"
RESULTS="$RAW/results.tsv"; : >"$RESULTS"
DETAIL="$RAW/detail.md"; : >"$DETAIL"
LOGS="$RAW/logs"; mkdir -p "$LOGS"
export PYTHONPYCACHEPREFIX="$RAW/pycache"
export PYTHONDONTWRITEBYTECODE=1
unset HANDOFF_STATE_DIR HANDOFF_HOOK_CHILD CLAUDE_CODE_AUTO_COMPACT_WINDOW

say() { printf '%s\n' "$*"; }
now() { date +%s; }
# record NAME STATUS COUNTS SECONDS LOG   (STATUS: PASS, FAIL, SKIP or INFO)
record() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >>"$RESULTS"; say "[$2] $1 - $3 (${4}s)"; }
detail() { printf '%s\n' "$*" >>"$DETAIL"; }

# ---------------------------------------------------------------- isolation snapshot
REAL_STATE="$REAL_HOME/.local/state/claude-handoffs"
snapshot() {
  local out="$1"
  {
    for f in health.jsonl receipts.jsonl; do
      if [ -f "$REAL_STATE/$f" ]; then printf '%s lines=%s\n' "$f" "$(wc -l <"$REAL_STATE/$f" | tr -d ' ')"; else printf '%s absent\n' "$f"; fi
    done
    if [ -f "$REAL_HOME/.claude/hooks/.handoff-manifest.json" ]; then
      printf 'manifest mtime=%s\n' "$(stat -f %m "$REAL_HOME/.claude/hooks/.handoff-manifest.json" 2>/dev/null || stat -c %Y "$REAL_HOME/.claude/hooks/.handoff-manifest.json")"
    else
      printf 'manifest absent\n'
    fi
    printf 'launchctl: %s\n' "$( (launchctl list 2>/dev/null | awk '{print $3}' | grep -i lossless | sort | tr '\n' ' ') || true)"
  } >"$out"
}
snapshot "$RAW/isolation-before.txt"

# ---------------------------------------------------------------- shared temp install
# make_install DIR REPO_ROOT [seed-json]: install into DIR as HOME. Never touches the real HOME.
make_install() {
  local home="$1" repo="$2" seed="${3:-}"
  [ -n "$seed" ] || seed='{}'
  mkdir -p "$home/.claude"
  printf '%s\n' "$seed" >"$home/.claude/settings.json"
  ( cd "$repo" && HOME="$home" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 RECALL_EMBED_URL=http://127.0.0.1:9 ./install.sh --no-nightly )
}

OVERALL=0
fail_overall() { OVERALL=1; }

# ---------------------------------------------------------------- layer 1: static
layer_static() {
  local t0 log="$LOGS/1-static.log" nsh=0 npy=0 bad=0 f
  t0="$(now)"
  {
    cd "$ROOT" || exit 1
    local shells=()
    while IFS= read -r f; do shells+=("$f"); done < <(find . -path ./.git -prune -o -type f \( -name '*.sh' -o -name 'install.sh' -o -name 'uninstall.sh' \) -print | sort)
    for f in "${shells[@]}"; do
      nsh=$((nsh + 1))
      if bash -n "$f"; then echo "bash -n ok: $f"; else echo "bash -n FAIL: $f"; bad=$((bad + 1)); fi
      if command -v shellcheck >/dev/null; then
        if shellcheck --severity=warning "$f"; then echo "shellcheck ok: $f"; else echo "shellcheck FAIL: $f"; bad=$((bad + 1)); fi
      else
        echo "shellcheck missing"; bad=$((bad + 1))
      fi
    done
    local pys=()
    while IFS= read -r f; do pys+=("$f"); done < <(find hooks tools tests -type f -name '*.py' | sort)
    for f in "${pys[@]}"; do
      npy=$((npy + 1))
      if python3 -m py_compile "$f"; then echo "py_compile ok: $f"; else echo "py_compile FAIL: $f"; bad=$((bad + 1)); fi
    done
    echo "SUMMARY shell=$nsh python=$npy bad=$bad"
  } >"$log" 2>&1
  local sum; sum="$(grep '^SUMMARY' "$log" | tail -1)"
  nsh="$(sed -n 's/.*shell=\([0-9]*\).*/\1/p' <<<"$sum")"; npy="$(sed -n 's/.*python=\([0-9]*\).*/\1/p' <<<"$sum")"; bad="$(sed -n 's/.*bad=\([0-9]*\).*/\1/p' <<<"$sum")"
  if [ "${bad:-1}" = 0 ]; then record "1 Static" PASS "$nsh shell files (bash -n, shellcheck), $npy python files (py_compile)" "$(($(now) - t0))" "$log"
  else record "1 Static" FAIL "${bad:-?} problems in $nsh shell and $npy python files" "$(($(now) - t0))" "$log"; fail_overall; fi
}

# ---------------------------------------------------------------- layer 2: smoke
layer_smoke() {
  local t0 log="$LOGS/2-smoke.log" rc n
  t0="$(now)"
  ( cd "$ROOT" && bash tests/smoke.sh ) >"$log" 2>&1; rc=$?
  n="$(grep -c '^ok:' "$log")"
  if [ "$rc" = 0 ] && grep -q '^ALL PASSED' "$log"; then record "2 Smoke" PASS "$n checks ok, ALL PASSED" "$(($(now) - t0))" "$log"
  else record "2 Smoke" FAIL "exit $rc, $n checks ok; last: $(grep -E '^FAIL' "$log" | tail -1)" "$(($(now) - t0))" "$log"; fail_overall; fi
}

# ---------------------------------------------------------------- layer 3: acceptance
TESTS_COMMIT="n/a"; TESTS_NOTE=""
layer_acceptance() {
  local t0 log="$LOGS/3-acceptance.log" ref="" i rc
  t0="$(now)"
  {
    if [ -z "$TESTS_DIR" ]; then
      TESTS_DIR="$RAW/tests-repo"
      if ! git clone --quiet "$TESTS_REPO_URL" "$TESTS_DIR" 2>&1; then
        echo "ssh clone failed; trying https"
        rm -rf "$TESTS_DIR"
        git clone --quiet "$TESTS_REPO_HTTPS" "$TESTS_DIR" 2>&1
      fi
    fi
    if [ -f "$ROOT/tests.ref" ]; then
      ref="$(tr -d ' \n\r' <"$ROOT/tests.ref")"
      if (cd "$TESTS_DIR" && git -c advice.detachedHead=false checkout --quiet "$ref" 2>&1); then TESTS_NOTE="pinned by tests.ref"; else TESTS_NOTE="tests.ref $ref could not be checked out"; fi
    else
      (cd "$TESTS_DIR" && git -c advice.detachedHead=false checkout --quiet main 2>&1) || true
      TESTS_NOTE="tests.ref is absent; the suite ran at main"
    fi
    TESTS_COMMIT="$(cd "$TESTS_DIR" && git rev-parse --short=10 HEAD 2>/dev/null || echo unknown)"
    echo "tests commit $TESTS_COMMIT ($TESTS_NOTE)"
  } >"$log" 2>&1
  if [ ! -d "$TESTS_DIR" ] || [ "$TESTS_COMMIT" = unknown ]; then
    record "3 Acceptance" FAIL "tests checkout failed (see log)" "$(($(now) - t0))" "$log"; fail_overall; return
  fi
  local acc_home="$RAW/home-acceptance"; mkdir -p "$acc_home"
  : >"$RAW/acc-summaries.txt"
  for i in $(seq 1 "$REPEAT"); do
    # Output goes to a log only. The report keeps just the summary line and FAIL/ERROR names.
    ( cd "$TESTS_DIR" && HOME="$acc_home" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 RECALL_EMBED_URL=http://127.0.0.1:9 \
        HANDOFF_HOOKS_DIR="$ROOT/hooks" LOSSLESS_HANDOFF_REPO="$ROOT" \
        python3 -W ignore -m unittest discover -s "$TESTS_DIR" ) >"$LOGS/3-acceptance-run$i.log" 2>&1; rc=$?
    echo "$rc" >"$RAW/acc-rc-$i"
    grep -E '^(FAIL|ERROR): ' "$LOGS/3-acceptance-run$i.log" | sed 's/^[A-Z]*: //' | sort -u >"$RAW/acc-fails-$i.txt"
    printf 'run %s: %s | %s\n' "$i" "$(grep -E '^Ran [0-9]+ tests?' "$LOGS/3-acceptance-run$i.log" | tail -1)" \
      "$(grep -E '^(OK|FAILED)' "$LOGS/3-acceptance-run$i.log" | tail -1)" >>"$RAW/acc-summaries.txt"
  done
  # classify: failing in every run = hard failure; failing in some runs = flaky (rerun alone 3 times)
  local all_fail="$RAW/acc-allfail.txt" any_fail="$RAW/acc-anyfail.txt" hard=0 hard_names="" flaky_names="" flaky_bad=""
  cat "$RAW"/acc-fails-*.txt | sort -u >"$any_fail"
  : >"$all_fail"
  local name cnt
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    cnt="$(grep -lxF -- "$name" "$RAW"/acc-fails-*.txt | wc -l | tr -d ' ')"
    if [ "$cnt" = "$REPEAT" ]; then echo "$name" >>"$all_fail"; fi
  done <"$any_fail"
  for i in $(seq 1 "$REPEAT"); do
    # a run that exits non-zero with no FAIL/ERROR name (an import crash, for example) is a hard failure
    if [ "$(cat "$RAW/acc-rc-$i")" != 0 ] && [ ! -s "$RAW/acc-fails-$i.txt" ]; then hard=1; hard_names="$hard_names run$i exited $(cat "$RAW/acc-rc-$i") with no FAIL or ERROR name;"; fi
  done
  [ -s "$all_fail" ] && { hard=1; hard_names="$hard_names $(wc -l <"$all_fail" | tr -d ' ') test(s) failed in every run;"; }
  local alone_lines="" k ok_alone tid
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    grep -qxF -- "$name" "$all_fail" && continue
    flaky_names="$flaky_names$name; "
    tid="$(sed -n 's/.*(\(.*\))$/\1/p' <<<"$name")"; [ -n "$tid" ] || tid="$name"
    ok_alone=0
    for k in 1 2 3; do
      ( cd "$TESTS_DIR" && HOME="$acc_home" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 RECALL_EMBED_URL=http://127.0.0.1:9 \
          HANDOFF_HOOKS_DIR="$ROOT/hooks" LOSSLESS_HANDOFF_REPO="$ROOT" python3 -W ignore -m unittest "$tid" ) >"$LOGS/3-alone-$k.log" 2>&1 && ok_alone=$((ok_alone + 1))
    done
    alone_lines="$alone_lines- flaky: $name; alone $ok_alone of 3 passed"$'\n'
    [ "$ok_alone" = 3 ] || flaky_bad="$flaky_bad $name;"
  done <"$any_fail"
  {
    echo "### Layer 3 detail"; echo
    echo "Tests commit: \`$TESTS_COMMIT\` ($TESTS_NOTE)."; echo
    echo '```'; cat "$RAW/acc-summaries.txt"; echo '```'; echo
    if [ -s "$any_fail" ]; then
      echo "Tests that failed in at least one run (name and assertion line only):"; echo
      for i in $(seq 1 "$REPEAT"); do
        awk '/^(FAIL|ERROR): /{n=$0; sub(/^[A-Z]*: /,"",n); next} n!="" && /^[A-Za-z_.]*(Error|Exception)/{print "- " n " :: " substr($0,1,300); n=""}' "$LOGS/3-acceptance-run$i.log"
      done | sort -u
      echo
    fi
    [ -n "$alone_lines" ] && { printf '%s' "$alone_lines"; echo; }
    local fe; fe="$(grep -hE 'FileNotFoundError' "$LOGS"/3-acceptance-run*.log 2>/dev/null | wc -l | tr -d ' ')"
    [ "$fe" != 0 ] && { echo "FileNotFoundError lines in the output: $fe (the tests may still expect an older repo layout)."; echo; }
  } >>"$DETAIL"
  local counts; counts="$(sed 's/^run [0-9]*: //' "$RAW/acc-summaries.txt" | tr '\n' ';' | sed 's/;$//')"
  if [ "$hard" = 0 ] && [ -z "$flaky_bad" ]; then
    record "3 Acceptance" PASS "$REPEAT runs at tests $TESTS_COMMIT; ${counts:-no summary}${flaky_names:+; flaky but 3 of 3 alone: $flaky_names}" "$(($(now) - t0))" "$LOGS/3-acceptance-run1.log"
  else
    record "3 Acceptance" FAIL "$REPEAT runs at tests $TESTS_COMMIT; ${counts:-no summary};${hard_names}${flaky_bad:+ flaky not stable alone:$flaky_bad}" "$(($(now) - t0))" "$LOGS/3-acceptance-run1.log"
    fail_overall
  fi
}

# ---------------------------------------------------------------- layer 4: fresh clone
PUSHED_SHA=""; PUSHED_NOTE=""
layer_fresh_clone() {
  local t0 log="$LOGS/4-fresh-clone.log" br c rb sha="" cl="$RAW/fresh-clone" home="$RAW/home-fresh" S H out head_sha
  t0="$(now)"
  head_sha="$(git -C "$ROOT" rev-parse HEAD)"
  br="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  {
    git -C "$ROOT" fetch --quiet origin 2>&1 || echo "fetch failed"
    if git -C "$ROOT" rev-parse --verify --quiet "origin/$br" >/dev/null; then
      sha="$(git -C "$ROOT" rev-parse "origin/$br")"; rb="$br"
    else
      # the current branch is not pushed: use the nearest ancestor commit that a remote branch holds
      for c in $(git -C "$ROOT" rev-list HEAD | head -50); do
        rb=""
        for cand in $(git -C "$ROOT" branch -r --contains "$c" 2>/dev/null | grep -v 'HEAD ->' | sed 's/^ *//'); do
          [ "$cand" = origin/main ] && continue
          [ -z "$rb" ] && rb="$cand"
          [ "$(git -C "$ROOT" rev-parse "$cand")" = "$c" ] && { rb="$cand"; break; }   # prefer a branch whose tip is this commit
        done
        [ -z "$rb" ] && rb="$(git -C "$ROOT" branch -r --contains "$c" 2>/dev/null | grep -v 'HEAD ->' | sed 's/^ *//' | head -1)"
        if [ -n "$rb" ]; then sha="$c"; rb="${rb#origin/}"; break; fi
      done
    fi
    PUSHED_SHA="$sha"
    if [ "$sha" = "$head_sha" ]; then PUSHED_NOTE="pushed commit equals local HEAD"; else PUSHED_NOTE="pushed commit ${sha:0:10} (origin/$rb) differs from local HEAD ${head_sha:0:10}"; fi
    echo "branch=$br pushed=$sha ($rb) note: $PUSHED_NOTE"
    [ -n "$sha" ] || { echo "no pushed commit found"; exit 1; }
    git clone --quiet "$CLONE_URL" "$cl" 2>&1 || { rm -rf "$cl"; git clone --quiet "git@github.com:pratikgh0se/lossless-handoff.git" "$cl" 2>&1; } || exit 1
    git -C "$cl" -c advice.detachedHead=false checkout --quiet "$sha" 2>&1 || exit 1
    echo "cloned at $(git -C "$cl" rev-parse --short=10 HEAD)"
    mkdir -p "$home/.claude"
    S="$home/.claude/settings.json"; H="$home/.claude/hooks"
    printf '{"model":"opus","env":{"FOO":"1"}}\n' >"$S"; cp "$S" "$RAW/fresh-original.json"
    run_in() { ( cd "$cl" && HOME="$home" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 RECALL_EMBED_URL=http://127.0.0.1:9 "$@" ); }
    run_in ./install.sh --no-nightly >"$RAW/fresh-install.out" 2>&1 || { echo "STEP install FAIL"; cat "$RAW/fresh-install.out"; exit 1; }
    echo "STEP install ok"
    out="$(HOME="$home" python3 "$H/postcompact-inject.py" --check 2>&1)" || { echo "STEP check FAIL exit"; exit 1; }
    [ -z "$out" ] || { echo "STEP check FAIL printed: $out"; exit 1; }
    echo "STEP check prints nothing ok"
    local v1 v2
    v1="$(sed -n 's/^HOOK_VERSION = "\(.*\)"/\1/p' "$H/precompact-handoff.py")"; v2="$(sed -n 's/^HOOK_VERSION = "\(.*\)"/\1/p' "$H/postcompact-inject.py")"
    [ -n "$v1" ] && [ "$v1" = "$v2" ] || { echo "STEP versions FAIL: $v1 vs $v2"; exit 1; }
    echo "STEP hook versions match ($v1) ok"
    run_in ./uninstall.sh >"$RAW/fresh-uninstall1.out" 2>&1 || { echo "STEP uninstall FAIL"; exit 1; }
    python3 - "$S" "$RAW/fresh-original.json" <<'PY' || { echo "STEP restore FAIL: settings differ from the original"; exit 1; }
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8")); b = json.load(open(sys.argv[2], encoding="utf-8"))
sys.exit(0 if a == b else 1)
PY
    echo "STEP uninstall restores settings ok"
    cp "$S" "$RAW/fresh-after1.json"
    run_in ./uninstall.sh >"$RAW/fresh-uninstall2.out" 2>&1 || { echo "STEP second uninstall FAIL exit"; exit 1; }
    cmp -s "$S" "$RAW/fresh-after1.json" || { echo "STEP second uninstall FAIL: settings changed"; exit 1; }
    for f in precompact-handoff.py postcompact-inject.py handoff-nudge.py recall.py claims.json .handoff-manifest.json; do
      [ ! -e "$H/$f" ] || { echo "STEP second uninstall FAIL: $f is back"; exit 1; }
    done
    echo "STEP second uninstall does nothing ok"
    echo "FRESH_CLONE_OK"
  } >"$log" 2>&1
  if grep -q '^FRESH_CLONE_OK' "$log"; then
    record "4 Fresh clone" PASS "clone at ${PUSHED_SHA:0:10}; install, --check silent, versions match, uninstall restores, 2nd uninstall no-op" "$(($(now) - t0))" "$log"
  else
    record "4 Fresh clone" FAIL "$(grep -E 'STEP .*FAIL|no pushed|failed' "$log" | head -2 | tr '\n' ' ')" "$(($(now) - t0))" "$log"; fail_overall
  fi
}

# ---------------------------------------------------------------- layers 5 and 6: real compactions
REAL_STATE_LAST=""
REAL_JSON="$RAW/real-result.json"
layer_real() {
  local t0 log="$LOGS/5-6-real.log" rc home="$RAW/home-real" work="$RAW/real-work"
  t0="$(now)"
  if ! command -v claude >/dev/null; then
    record "5 Real end to end" SKIP "claude not on PATH" 0 "$log"; record "6 Real stress" SKIP "claude not on PATH" 0 "$log"; fail_overall; return
  fi
  mkdir -p "$work"
  if ! make_install "$home" "$ROOT" >"$LOGS/real-install.log" 2>&1; then
    record "5 Real end to end" FAIL "temp install failed" 0 "$LOGS/real-install.log"; record "6 Real stress" SKIP "no install" 0 "$log"; fail_overall; return
  fi
  python3 "$ROOT/tests/local-proof-real.py" --hooks "$home/.claude/hooks" --settings "$home/.claude/settings.json" \
    --work "$work" --out "$REAL_JSON" --runs 3 --cap "$COST_CAP" >"$log" 2>&1; rc=$?
  python3 - "$REAL_JSON" "$RESULTS" "$DETAIL" "$RAW" "$log" "$(($(now) - t0))" <<'PY'
import json, os, sys
path, results, detail, raw, log, secs = sys.argv[1:7]
try:
    d = json.load(open(path))
except Exception as e:
    d = None
def rec(name, status, counts, s):
    with open(results, "a") as f:
        f.write("\t".join([name, status, counts, str(s), log]) + "\n")
    print("[%s] %s - %s (%ss)" % (status, name, counts, s))
if not d:
    rec("5 Real end to end", "FAIL", "driver wrote no result (see log)", secs)
    rec("6 Real stress", "FAIL", "driver wrote no result (see log)", secs)
    sys.exit(0)
out = []
e = d.get("e2e")
if e:
    runs = e["runs"]
    c = "%d of 3 runs ok; main compactions %s; subagent compactions %s; receipts %s main + %s subagent" % (
        sum(1 for r in runs if r["ok"]), "/".join(str(r["main_compactions"]) for r in runs),
        "/".join(str(r["sub_compactions"]) for r in runs), "/".join(str(r["receipts_main"]) for r in runs),
        "/".join(str(r["receipts_sub"]) for r in runs))
    rec("5 Real end to end", "PASS" if e.get("ok") and len(runs) == 3 else "FAIL", c, secs)
    out += ["### Layer 5 detail", ""]
    for r in runs:
        out.append("- Run %d (session %s, %s turns, cost $%.3f): main %s, subagent %s compactions; receipts %s main, %s subagent; "
                   "subagent handoff files %s; main-only code in subagent handoff: %s."
                   % (r["run"], r["sid"], r["turns"], r["cost"], r["main_compactions"], r["sub_compactions"], r["receipts_main"],
                      r["receipts_sub"], r["sub_handoff_files"], "yes " + str(r["leaks"]) if r["leaks"] else "no"))
        out.append("  - seed codes, main: %s; subagent: %s" % (", ".join(r["codes"].get("main", [])), ", ".join(r["codes"].get("sub", []))))
        out.append("  - `%s`" % r["prove_line"])
        bad = [k for k, v in r["checks"].items() if not v]
        if bad:
            out.append("  - FAILED checks: %s %s" % (", ".join(bad), "; ".join(r["errors"])))
    out.append("")
elif d.get("stopped"):
    rec("5 Real end to end", "FAIL", d["stopped"], secs)
else:
    rec("5 Real end to end", "SKIP", "not run", secs)
s = d.get("stress")
if s:
    sc = s["scenarios"]
    names = {x["name"]: x for x in sc}
    c = "; ".join("%s %s" % (x["name"], "ok" if x["ok"] else "FAIL") for x in sc)
    rec("6 Real stress", "PASS" if s.get("ok") and len(sc) == 3 else "FAIL", c, secs)
    out += ["### Layer 6 detail", ""]
    for x in sc:
        out.append("- %s (cost $%.3f): %s" % (x["name"], x.get("cost", 0), "ok" if x["ok"] else "FAILED"))
        for k, v in x["checks"].items():
            out.append("  - %s: %s" % (k, "pass" if v else "FAIL"))
        if x.get("prove_line"):
            out.append("  - `%s`" % x["prove_line"])
        for key in ("sessions_info", "health_status", "reconcile", "first_text", "compactions", "receipts", "jsonl_files", "jsonl_bad_lines"):
            if key in x:
                out.append("  - %s: %s" % (key, json.dumps(x[key])))
        if x.get("errors"):
            out.append("  - errors: %s" % "; ".join(x["errors"]))
    out.append("")
elif d.get("stopped"):
    rec("6 Real stress", "FAIL", "not run: " + d["stopped"], secs)
else:
    rec("6 Real stress", "SKIP", "not run", secs)
with open(detail, "a") as f:
    f.write("\n".join(out) + "\n")
with open(os.path.join(raw, "real-cost.txt"), "w") as f:
    f.write("%s %s\n" % (d.get("cost_total"), d.get("cap")))
if d.get("stopped"):
    with open(os.path.join(raw, "real-stopped.txt"), "w") as f:
        f.write(d["stopped"])
if e and e.get("runs"):
    with open(os.path.join(raw, "e2e-state.txt"), "w") as f:
        f.write(e["runs"][-1]["state"])
PY
  # the python block wrote the layer lines to RESULTS; fold failures into the exit code
  grep -E '^(5|6) ' "$RESULTS" | grep -q $'\tFAIL\t' && fail_overall
  [ -f "$RAW/e2e-state.txt" ] && REAL_STATE_LAST="$(cat "$RAW/e2e-state.txt")"
  return 0
}

# ---------------------------------------------------------------- layer 7: claims (information only)
layer_claims() {
  local t0 log="$LOGS/7-claims.log" rc status
  t0="$(now)"
  if [ -z "$REAL_STATE_LAST" ] || [ ! -d "$REAL_STATE_LAST" ]; then
    record "7 Claims (info)" INFO "no e2e state to lint (real layers did not run)" 0 "$log"; return
  fi
  ( cd "$ROOT" && HANDOFF_STATE_DIR="$REAL_STATE_LAST" RECALL_EMBED_URL=http://127.0.0.1:9 python3 hooks/precompact-handoff.py --claims-lint README.md ) >"$log" 2>&1; rc=$?
  if [ "$rc" = 0 ]; then status="claims-lint exit 0: every cited claim passes"; else status="claims-lint exit $rc: $(head -c 300 "$log" | tr '\n' ' ')"; fi
  record "7 Claims (info)" INFO "$status" "$(($(now) - t0))" "$log"
}

# ---------------------------------------------------------------- run
if [ "$REAL_ONLY" = 0 ]; then
  layer_static
  layer_smoke
  layer_acceptance
  layer_fresh_clone
fi
if [ "$NO_REAL" = 0 ]; then
  layer_real
  layer_claims
fi

snapshot "$RAW/isolation-after.txt"
ISO="PASS"
cmp -s "$RAW/isolation-before.txt" "$RAW/isolation-after.txt" || { ISO="FAIL"; fail_overall; }

# ---------------------------------------------------------------- report
SHA="$(git -C "$ROOT" rev-parse --short=7 HEAD)"
DATE="$(date +%Y-%m-%d)"
OUTDIR="$ROOT/docs/proof"; mkdir -p "$OUTDIR"
REPORT="$OUTDIR/$DATE-$SHA.md"
HOOKS_COMMIT="$(git -C "$ROOT" log -1 --format=%h -- hooks)"
DIRTY=""; [ -n "$(git -C "$ROOT" status --porcelain -- . ':!docs/proof' | head -1)" ] && DIRTY=" (working tree has uncommitted changes)"
COST="not run"; [ -f "$RAW/real-cost.txt" ] && COST="$(awk '{printf "$%.2f of a $%s cap", $1, $2}' "$RAW/real-cost.txt")"
NFAIL="$(grep -c $'\tFAIL\t' "$RESULTS")"
if [ "$OVERALL" = 0 ]; then VERDICT="PASS: every layer passed and the isolation check held."; else VERDICT="FAIL: $NFAIL layer(s) failed or the isolation check failed (see the table)."; fi
[ -f "$RAW/real-stopped.txt" ] && VERDICT="$VERDICT The real layers stopped early: $(cat "$RAW/real-stopped.txt")."
CLAUDE_V="$(claude --version 2>/dev/null | head -1)"; [ -n "$CLAUDE_V" ] || CLAUDE_V="not installed"

{
  echo "# Local proof $DATE ($SHA)"
  echo
  echo "**Verdict: $VERDICT**"
  echo
  echo "| Layer | Result | Counts | Time |"
  echo "|---|---|---|---|"
  while IFS=$'\t' read -r n s c t _; do
    printf '| %s | %s | %s | %ss |\n' "$n" "$s" "${c//|/\\|}" "$t"
  done <"$RESULTS"
  printf '| Isolation check | %s | see below | - |\n' "$ISO"
  echo
  echo "## Versions"
  echo
  echo "- Hooks commit: \`$SHA\`$DIRTY (last change in \`hooks/\`: \`$HOOKS_COMMIT\`), branch \`$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)\`."
  echo "- Tests commit: \`$TESTS_COMMIT\`${TESTS_NOTE:+ ($TESTS_NOTE)}."
  [ -n "$PUSHED_NOTE" ] && echo "- Fresh clone: $PUSHED_NOTE."
  echo "- Claude Code: $CLAUDE_V."
  echo "- OS: $(uname -sr) ($(sw_vers -productVersion 2>/dev/null || echo n/a)); Python: $(python3 --version 2>&1)."
  echo "- Total model cost of the real layers: $COST (sum of \`total_cost_usd\` of the \`claude -p\` results). The hook's own summary calls ran on haiku (\`HANDOFF_SUMMARY_MODEL=haiku\`) and are not in this sum."
  echo "- Real-layer settings: window 30000 tokens, model haiku, \`--setting-sources local\` with the temp install's hooks, project folders named \`handoff-canary-*\`."
  echo
  echo "## Isolation check"
  echo
  echo "Before and after the run, the real files and jobs were compared. Result: **$ISO**."
  echo
  echo '```'
  echo "before:"; cat "$RAW/isolation-before.txt"; echo "after:"; cat "$RAW/isolation-after.txt"
  echo '```'
  echo
  echo "## Details"
  echo
  cat "$DETAIL"
  echo "Layer 7 is information only. Raw logs stay outside the repo."
} >"$RAW/report.raw.md"
# keep the real home path and temp paths out of the report
python3 - "$RAW/report.raw.md" "$REPORT" "$REAL_HOME" "$RAW" "${TMPDIR:-/tmp}" <<'PY'
import os, sys
src, dst, home, raw, tmp = sys.argv[1:6]
t = open(src, encoding="utf-8").read()
for p in sorted({raw, os.path.realpath(raw), raw.replace("/private/var", "/var")}, key=len, reverse=True):
    t = t.replace(p, "<tmp>")
for p in sorted({home, os.path.realpath(home)}, key=len, reverse=True):
    t = t.replace(p, "~")
open(dst, "w", encoding="utf-8").write(t)
PY

say ""
say "Verdict: $VERDICT"
say "Report: $REPORT"
say "Raw logs: $RAW"
exit "$OVERALL"
