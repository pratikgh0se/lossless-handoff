# Tests and contributing

The three test layers and the code-blind rule. This page is part of the [docs index](README.md).

## Tests

Three layers run on every push and pull request (GitHub Actions, Ubuntu and macOS):

1. **Static checks:** `bash -n`, shellcheck and `py_compile`.
2. **Smoke test:** `bash tests/smoke.sh`. It does install, the hooks, recall, the canary and uninstall in a temporary HOME.
3. **Acceptance tests:** the code-blind suite in [lossless-handoff-tests](https://github.com/pratikgh0se/lossless-handoff-tests), at the commit named in `tests.ref`.

**The code-blind rule.** A writer who never reads the hook code writes the acceptance tests only from [`docs/spec.md`](spec.md). The people and agents who change the hooks never read the tests. They see only the test names and the assertion output. So a behaviour change starts in `docs/spec.md`. The tests follow the spec, and the hooks follow the tests.

**When a pull request changes behaviour:**

1. Amend `docs/spec.md` (a new or changed AC number).
2. Update the tests in lossless-handoff-tests from the spec.
3. Set `tests.ref` in this repository to the new tests commit, in the same pull request.

Run the acceptance tests locally:

```bash
git clone https://github.com/pratikgh0se/lossless-handoff-tests ../lossless-handoff-tests
git -C ../lossless-handoff-tests checkout "$(cat tests.ref)"
HANDOFF_HOOKS_DIR="$PWD/hooks" LOSSLESS_HANDOFF_REPO="$PWD" LOSSLESS_HANDOFF_NO_LAUNCHCTL=1 \
  python3 -W ignore -m unittest discover -s ../lossless-handoff-tests
```

The tests use a temporary HOME and never call `launchctl`, so your own install stays as it is.
