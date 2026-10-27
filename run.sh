#!/bin/zsh
# Start the agent for today. Usage: ./run.sh [check|run|status|squareoff|backtest|journal|mode]
cd "$(dirname "$0")"
source .venv/bin/activate
exec python -m agent "${1:-run}" "${@:2}"
