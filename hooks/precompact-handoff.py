#!/usr/bin/env python3
"""PreCompact hook: writes the handoff file from facts and the transcript. It opens no network connection itself.
It runs the local `claude -p` command for the summary and the decisions merge. Set HANDOFF_SUMMARY=0 and HANDOFF_LEDGER=0
to stop that. It may also start the recall indexer. The same file holds --reconcile, --prove and --claims. Always exits 0."""
import json, os, sys, time, subprocess, shutil, glob, datetime, tempfile, re, hashlib, threading
os.umask(0o077)  # every file this hook (and the indexer it starts) creates is private: mode 0600, folders 0700
try:
    import fcntl
except Exception:  # pragma: no cover (non-POSIX): ledger updates are skipped
    fcntl = None

if os.environ.get("HANDOFF_HOOK_CHILD"):  # spawned by our own summary call: never recurse
    sys.exit(0)

_T0 = time.monotonic()
HOOK_VERSION = "1.21.0"
# The steer text stays in this file: the smoke test reads it from here.
STEER_TEXT = """Lossless Handoff guidance for this summary:
- Quote the user's own words for every decision, rule and correction, with the date if known.
- Keep exact commands, file paths, numbers, ids and error text. Do not paraphrase them.
- List work that is still running (agents, jobs, background commands) with its ids and state.
- End with the exact next step.
- A structured handoff and the project decisions list are injected after this compaction, and the full transcript stays searchable with recall, so keep old history short and keep the current state complete.
"""


_STEER = [None]  # F53.e: "printed" or "off"; None when this run is not a PreCompact event

try:  # the parts live in hooks/lossless_handoff/. A missing or broken folder must never break a session.
    # Load by path, with no import: an import goes through sys.modules, so a second copy of this file would get the parts of the first.
    _PKG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lossless_handoff")
    if not os.path.isdir(_PKG):  # this file may be a symlink to the real hooks folder
        _PKG = os.path.join(os.path.dirname(os.path.realpath(__file__)), "lossless_handoff")
    _INIT = os.path.join(_PKG, "__init__.py")
    _LH = {"__file__": _INIT, "__name__": "lossless_handoff"}
    with open(_INIT, encoding="utf-8") as _f:
        exec(compile(_f.read(), _INIT, "exec"), _LH)
    _LH["load_into"](globals())
except BaseException as _e:
    if isinstance(_e, KeyboardInterrupt):
        raise
    sys.stderr.write("lossless-handoff: the hooks/lossless_handoff folder is missing or broken (%s: %s). Run install.sh again.\n"
                     % (type(_e).__name__, str(_e)[:200]))
    if __name__ == "__main__":
        sys.exit(0)
    raise


_exit_code = 0
try:
    if "--ledger-maintain" in sys.argv[1:]:
        _a = sys.argv[1:]
        _i = _a.index("--ledger-maintain")
        ledger_maintain(_a[_i + 1] if _i + 1 < len(_a) else os.getcwd())
    elif "--recover" in sys.argv[1:]:
        recover()
    elif "--reconcile" in sys.argv[1:] or "--prove" in sys.argv[1:]:
        _a = sys.argv[1:]
        _days = 7
        try:
            if "--days" in _a and int(_a[_a.index("--days") + 1]) >= 1:
                _days = int(_a[_a.index("--days") + 1])
        except Exception:
            pass
        if "--reconcile" in _a:
            reconcile(_days)
        if "--prove" in _a:
            prove(_days)
    elif "--claims-lint" in sys.argv[1:]:
        _a = sys.argv[1:]
        _i = _a.index("--claims-lint")
        claims_lint(_a[_i + 1] if _i + 1 < len(_a) and not _a[_i + 1].startswith("--") else "")
    elif "--claims" in sys.argv[1:]:
        claims_print()
    elif "--ledger-repair" in sys.argv[1:]:
        _a = sys.argv[1:]
        _i = _a.index("--ledger-repair")
        ledger_repair(_a[_i + 1] if _i + 1 < len(_a) else os.getcwd())
    else:
        main()
except BaseException as _e:
    if isinstance(_e, SystemExit):
        _exit_code = _e.code if isinstance(_e.code, int) else 0  # F42.4: --ledger-maintain exits 2 on a bad dir
    elif not isinstance(_e, KeyboardInterrupt):
        log_error("unknown", _e)
sys.exit(_exit_code)
