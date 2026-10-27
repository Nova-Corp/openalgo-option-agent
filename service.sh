#!/bin/zsh
# Run the agent and the dashboard as macOS login services (launchd).
# Usage: ./service.sh install | uninstall | start | stop | restart | status
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
AGENT=com.trade.option-agent
DASH=com.trade.option-dashboard
DOMAIN="gui/$(id -u)"

mkplist() {  # label, shell command
  cat > "$LA/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>-lc</string><string>$2</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/logs/$1.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$1.err.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string></dict>
</dict></plist>
PLIST
}

kill_stray() {  # copies started by hand in a terminal must not run beside the service
  local pids
  pids=$(pgrep -f "python -m agent run --forever" || true)
  if [ -n "$pids" ]; then echo "  stopping agent copies started from a terminal: $pids"; kill $pids 2>/dev/null; sleep 2; fi
  pids=$(pgrep -f "python -m agent dashboard" || true)
  if [ -n "$pids" ]; then echo "  stopping dashboard copies started from a terminal: $pids"; kill $pids 2>/dev/null; sleep 1; fi
}

load()   { launchctl bootstrap "$DOMAIN" "$LA/$1.plist" 2>/dev/null || launchctl kickstart "$DOMAIN/$1" 2>/dev/null; }
unload() { launchctl bootout "$DOMAIN/$1" 2>/dev/null; }
state()  { launchctl print "$DOMAIN/$1" 2>/dev/null | awk '/state =/{print $3; exit}'; }

case "${1:-status}" in
  install)
    mkdir -p "$LA" "$DIR/logs"
    mkplist $AGENT "open -ga 'OpenAlgo Desktop'; cd '$DIR' && exec caffeinate -i ./run.sh run --forever"
    mkplist $DASH  "cd '$DIR' && exec ./run.sh dashboard"
    unload $AGENT; unload $DASH
    kill_stray
    load $AGENT; load $DASH
    echo "installed: both start at every login and restart if they crash."
    "$0" status ;;
  uninstall)
    unload $AGENT; unload $DASH
    rm -f "$LA/$AGENT.plist" "$LA/$DASH.plist"
    echo "removed login services (nothing starts automatically now)" ;;
  start)   rm -f "$DIR/STOP"; kill_stray; load $AGENT; load $DASH; "$0" status ;;
  stop)    unload $AGENT; unload $DASH; echo "stopped (an open trade stays open in OpenAlgo; use ./run.sh squareoff if needed)" ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  status)
    for s in $AGENT $DASH; do
      st=$(state $s); [ -z "$st" ] && st="not loaded"
      printf "  %-28s %s\n" "$s" "$st"
    done
    [ -f "$DIR/STOP" ] && echo "  STOP file present: agent will not trade until it is deleted (./service.sh start removes it)"
    curl -s -m 2 -o /dev/null http://127.0.0.1:8787/ && echo "  dashboard: http://127.0.0.1:8787/" || echo "  dashboard: not reachable" ;;
  *) echo "usage: $0 install|uninstall|start|stop|restart|status"; exit 2 ;;
esac
