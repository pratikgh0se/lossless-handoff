# Mutation gate

This gate measures whether the tests catch faults. It is a manual or weekly command. CI does not run it. Spec: F91.d in `docs/spec.md`.

`faults/` holds 20 seeded faults. Each fault is a small unified diff against the current code. The first line of a diff names it:

```
# fault M01 | feature F90 | type proof-check-drops-recall-id-form | tests: test_f90_grammar.py test_f47_proof_receipts.py
```

The `tests:` field names the code-blind test files that should catch the fault (names only).

## Run it

```bash
tools/mutation/run.sh --list           # ids, features and types only
tools/mutation/run.sh --check-diffs    # git apply --check for each diff against this repo
tools/mutation/run.sh M01 M05          # run only these faults
tools/mutation/run.sh                  # run all faults (slow: one full smoke run for each)
LH_TESTS_DIR=../lossless-handoff-tests tools/mutation/run.sh   # also run the named code-blind files
```

For each fault, `run.sh` copies the repo to a temp folder, applies the diff, runs `tests/smoke.sh` (with `LH_MUTATION_RUN=1`, so the smoke test skips its own mutation-gate checks) and, if `LH_TESTS_DIR` is set, runs the test files that the header names. It prints one line:

```
<fault id> <feature> <fault type> caught|survived
```

At the end it prints `mutation score: <caught>/<total>`. A `survived` line means no test noticed the fault: add a test or tighten one.

## Add a fault

1. Write a diff with 1 line of change and 1 line of context on each side. Test it with `git apply --check`.
2. Give it the header line and a new id.
3. Run `tools/mutation/run.sh --check-diffs`. The smoke test also checks that each diff applies, so change a diff when the code it targets moves.
