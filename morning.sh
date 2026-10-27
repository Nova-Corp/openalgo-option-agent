#!/bin/zsh
# Daily start. Usage: ./morning.sh                (indices from active_underlyings in config.yaml)
#                     ./morning.sh NIFTY SENSEX   (exactly these indices)
# Starts one agent per index, stops agents for indices you did not ask for (only when they are flat),
# starts the dashboard, waits for your AliceBlue login, opens the dashboard.
cd "$(dirname "$0")"
setopt null_glob
mkdir -p logs state
source .venv/bin/activate

if [ $# -gt 0 ]; then
  WANT=(); for a in "$@"; do WANT+=("${a:u}"); done
else
  WANT=($(python -c 'import yaml;print(" ".join(yaml.safe_load(open("config.yaml")).get("active_underlyings",["NIFTY"])))'))
fi
for u in "${WANT[@]}"; do
  case "$u" in NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|BANKEX) ;; *) echo "unknown index $u. usage: ./morning.sh [NIFTY|BANKNIFTY|SENSEX ...]"; exit 2 ;; esac
done
echo "== Indices: ${WANT[*]}"

echo "== 1. OpenAlgo app"
if pgrep -qf "OpenAlgo Desktop.app"; then echo "   running"; else echo "   starting it"; open -ga "OpenAlgo Desktop"; fi
for i in {1..40}; do curl -s -m 2 -o /dev/null http://127.0.0.1:5000/ && break; sleep 1; done
curl -s -m 2 -o /dev/null http://127.0.0.1:5000/ && echo "   API up on :5000" || echo "   WARNING: OpenAlgo API not answering on :5000"

echo "== 2. Agents"
[ -f STOP ] && echo "   STOP file present: agents will not trade. Delete it (rm STOP) and run this again."
agent_pid() { python -c "import json,os,sys; p='state/$1/running.json'; d=json.load(open(p)) if os.path.exists(p) else {}; pid=int(d.get('pid',0) or 0)
try:
  if pid>0: os.kill(pid,0); print(pid)
  else: print('')
except Exception: print('')" 2>/dev/null; }
has_open_trade() { python -c "import json,os,sys,datetime; p='state/$1/%s.json'%datetime.date.today(); d=json.load(open(p)) if os.path.exists(p) else {}; sys.exit(0 if any(t.get('status') in ('ENTERING','OPEN','EXITING') for t in d.get('trades',[])) else 1)" 2>/dev/null; }
# stop agents for indices not wanted (only if flat)
for d in state/*/; do
  u=$(basename "$d"); pid=$(agent_pid "$u")
  [ -z "$pid" ] && continue
  if [[ " ${WANT[*]} " != *" $u "* ]]; then
    if has_open_trade "$u"; then echo "   $u: running with an OPEN trade, not stopping it (wanted: ${WANT[*]})"
    else echo "   $u: stopping (not in today's list)"; kill "$pid" 2>/dev/null; fi
  fi
done
sleep 1
for u in "${WANT[@]}"; do
  pid=$(agent_pid "$u")
  if [ -n "$pid" ]; then echo "   $u: running (pid $pid)"; continue; fi
  mkdir -p "state/$u"
  nohup caffeinate -i ./run.sh run --forever -u "$u" >> "logs/agent-$u.out.log" 2>&1 &
  disown
  sleep 3
  pid=$(agent_pid "$u")
  [ -n "$pid" ] && echo "   $u: started (pid $pid)" || echo "   $u: FAILED to start; see logs/agent-$u.out.log"
done

echo "== 3. Dashboard"
if ! curl -s -m 2 -o /dev/null http://127.0.0.1:8787/; then
  nohup ./run.sh dashboard >> logs/dashboard.out.log 2>&1 &
  disown
  for i in {1..20}; do curl -s -m 2 -o /dev/null http://127.0.0.1:8787/ && break; sleep 1; done
fi
curl -s -m 2 -o /dev/null http://127.0.0.1:8787/ && echo "   up at http://127.0.0.1:8787/" || echo "   FAILED to start; see logs/dashboard.out.log"

echo "== 4. Broker login"
if ./run.sh ping >/dev/null 2>&1; then
  ./run.sh ping
else
  echo "   NOT logged in. Opening OpenAlgo: log in to AliceBlue there. Waiting..."
  open -a "OpenAlgo Desktop"
  until ./run.sh ping >/dev/null 2>&1; do sleep 10; printf "."; done
  echo; ./run.sh ping
fi

echo "== 5. Dashboard"
if curl -s -m 2 -o /dev/null http://127.0.0.1:8787/; then
  open http://127.0.0.1:8787/
  echo "All set. Agents for ${WANT[*]} trade on their own from 09:15 and square off at 15:10."
else
  echo "   dashboard is not reachable, not opening the browser. Check logs/dashboard.out.log"
fi
