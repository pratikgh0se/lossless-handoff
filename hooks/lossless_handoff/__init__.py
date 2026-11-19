"""Parts of the PreCompact hook (hooks/precompact-handoff.py). This is a plain move of the old single file.
load_into() runs each part in the namespace of the entry file. So a caller that loads the entry file and sets a global
on it (for example HANDOFFS_DIR) still changes what every function sees."""
import os

# The order of PARTS is the order of the old single file. Top-level assignments run in this order and some use names
# from an earlier part, so do not sort or reorder this list.
PARTS = ("transcript", "summary", "ledger", "handoff", "runtime", "redaction", "pipeline", "reconcile", "prove", "claims")


def load_into(ns):
    here = os.path.dirname(os.path.abspath(__file__))  # an absolute path, so tracebacks name the part file and line
    code = []
    for name in PARTS:  # compile all first: a broken part stops the load before any part runs
        path = os.path.join(here, name + ".py")
        with open(path, encoding="utf-8") as f:
            code.append(compile(f.read(), path, "exec"))
    for c in code:
        exec(c, ns)
