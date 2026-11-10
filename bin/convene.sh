#!/usr/bin/env bash
# convene.sh: convene the advisory jury (v3). Every input is a file; missing
# files are an error. The principal decides; file the outcome with resolve.py.
set -euo pipefail
cd "$(dirname "$0")/.."
D="${1:?usage: convene.sh <directive.md> <objection.md> <context.md> [plea.md]}"
O="${2:?usage: convene.sh <directive.md> <objection.md> <context.md> [plea.md]}"
C="${3:?usage: convene.sh <directive.md> <objection.md> <context.md> [plea.md]}"
P="${4:-}"
python3 agent_jury.py --directive-file "$D" --objection-file "$O" --context "$C" ${P:+--plea-file "$P"}
echo
echo "Advisory verdict. The principal decides; file it with:"
echo "  python3 resolve.py <case-file> --outcome <verdict> --by NAME [--note ...]"
