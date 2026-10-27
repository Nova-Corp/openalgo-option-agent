#!/bin/zsh
# Morning check. Run after you log in to the Mac: ./morning.sh
# Starts the agent and the dashboard if they are not running, waits for your AliceBlue login, opens the dashboard.
cd "$(dirname "$0")"
mkdir -p logs

echo "== 1. OpenAlgo app"
if pgrep -qf "OpenAlgo Desktop.app"; then echo "   running"; else echo "   starting it"; open -ga "OpenAlgo Desktop"; fi
for i in {1..40}; do curl -s -m 2 -o /dev/null http://127.0.0.1:5000/ && break; sleep 1; done
curl -s -m 2 -o /dev/null http://127.0.0.1:5000/ && echo "   API up on :5000" || echo "   WARNING: OpenAlgo API not answering on :5000"

echo "== 2. Agent"
if [ -f STOP ]; then
  echo "   STOP file present: the agent will not trade. Delete it (rm STOP) and run this again if you want to trade."
fi
if pgrep -qf "python -m agent run --forever"; then
  echo "   running"
else
  if launchctl print "gui/$(id -u)/com.trade.option-agent" >/dev/null 2>&1; then
    echo "   login service installed but not running; kickstarting it"
    launchctl kickstart "gui/$(id -u)/com.trade.option-agent"
  else
    echo "   starting it in the background"
    nohup caffeinate -i ./run.sh run --forever >> logs/agent.out.log 2>&1 &
    disown
  fi
  sleep 3
  pgrep -qf "python -m agent run --forever" && echo "   started" || echo "   FAILED to start; see logs/agent.out.log"
fi

echo "== 3. Dashboard"
if ! curl -s -m 2 -o /dev/null http://127.0.0.1:8787/; then
  if launchctl print "gui/$(id -u)/com.trade.option-dashboard" >/dev/null 2>&1; then
    launchctl kickstart "gui/$(id -u)/com.trade.option-dashboard"
  else
    nohup ./run.sh dashboard >> logs/dashboard.out.log 2>&1 &
    disown
  fi
  for i in {1..20}; do curl -s -m 2 -o /dev/null http://127.0.0.1:8787/ && break; sleep 1; done
fi
curl -s -m 2 -o /dev/null http://127.0.0.1:8787/ && echo "   up at http://127.0.0.1:8787/" || echo "   FAILED to start; see logs/dashboard.out.log"

echo "== 4. Broker login"
source .venv/bin/activate
if ./run.sh ping >/dev/null 2>&1; then
  ./run.sh ping
else
  echo "   NOT logged in. Opening OpenAlgo: log in to AliceBlue there. Waiting..."
  open -a "OpenAlgo Desktop"
  until ./run.sh ping >/dev/null 2>&1; do sleep 10; printf "."; done
  echo; ./run.sh ping
fi

echo "== 5. Opening the dashboard"
if curl -s -m 2 -o /dev/null http://127.0.0.1:8787/; then
  open http://127.0.0.1:8787/
  echo "All set. The agent trades on its own from 09:15 and squares off at 15:10."
else
  echo "   dashboard is not reachable, not opening the browser. Check logs/dashboard.out.log"
fi
