#!/bin/zsh
# Stop the agents. Usage: ./stop.sh            (square off open trades, then stop every agent)
#                         ./stop.sh --keep     (stop agents but leave any open trade in OpenAlgo)
#                         ./stop.sh --all      (also stop the dashboard)
cd "$(dirname "$0")"
setopt null_glob
source .venv/bin/activate
KEEP=0; ALL=0
for a in "$@"; do case "$a" in --keep) KEEP=1 ;; --all) ALL=1 ;; esac; done

pids=()
for d in state/*/; do
  u=$(basename "$d")
  pid=$(python -c "import json,os; p='state/$u/running.json'; d=json.load(open(p)) if os.path.exists(p) else {}; pid=int(d.get('pid',0) or 0)
try:
  if pid>0: os.kill(pid,0); print(pid)
except Exception: pass" 2>/dev/null)
  [ -n "$pid" ] && { pids+=("$pid"); echo "$u: running (pid $pid)"; }
done
if [ ${#pids} -eq 0 ]; then
  echo "no agent is running."
else
  if [ $KEEP -eq 1 ]; then
    echo "stopping agents, leaving open trades as they are"
    kill "${pids[@]}" 2>/dev/null
  else
    echo "asking agents to square off and exit (STOP file)..."
    touch STOP
    for i in {1..90}; do
      alive=0; for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
      [ $alive -eq 0 ] && break
      sleep 1
    done
    for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && { echo "pid $p did not exit; killing it"; kill "$p"; }; done
    rm -f STOP
  fi
  sleep 1
  echo "agents stopped."
fi
if [ $ALL -eq 1 ]; then
  dp=$(pgrep -f "python -m agent dashboard"); [ -n "$dp" ] && kill $dp && echo "dashboard stopped."
fi
./run.sh status 2>/dev/null | grep -E "running|stopped" | head -5
