#!/bin/bash
# Thin wrapper for the opt-in handoff canary (installed by `install.sh --canary`).
# Finds the canary next to this file (installed copy: handoff-canary/) or in a repo checkout (../tools/handoff-canary/).
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for R in "$D/handoff-canary" "$D/../tools/handoff-canary"; do
  [ -x "$R/run.sh" ] && exec "$R/run.sh" "$@"
done
echo "handoff-canary: canary not found next to $D (run ./install.sh --canary)" >&2
exit 2
