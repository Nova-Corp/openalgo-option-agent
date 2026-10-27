#!/bin/zsh
# Start the agent. Usage: ./run.sh [check|run|status|squareoff|backtest|journal|dashboard|ping|mode] [args] [-u INDEX]
# -u/--underlying may appear anywhere, e.g.  ./run.sh run --forever -u SENSEX
cd "$(dirname "$0")"
source .venv/bin/activate
cmd="${1:-run}"; shift
und=(); rest=()
while [ $# -gt 0 ]; do
  case "$1" in
    -u|--underlying) und=(-u "$2"); shift 2 ;;
    *) rest+=("$1"); shift ;;
  esac
done
exec python -m agent "${und[@]}" "$cmd" "${rest[@]}"
