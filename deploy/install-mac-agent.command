#!/bin/bash
# On the Mac, once: starts the Algo Lab helper at login, so backtests and sweeps sent to "Mac" on the dashboard
# (Algo Lab › Run / Sweep › Run on: Mac) run here. Safe to run again (it restarts the helper with the latest code).
#   bash deploy/install-mac-agent.command            install / restart
#   bash deploy/install-mac-agent.command remove     stop it and remove the login item
cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"
LABEL="com.algotrading.lab-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/algolab-agent.log"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null
if [ "${1:-}" = remove ]; then rm -f "$PLIST"; echo "The Algo Lab helper is stopped and removed."; exit 0; fi

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "Run setup first:  bash setup_mac.sh"; exit 1; }
echo "==> Checking the dashboard sign-in (secrets.yaml: dashboard url / user / password)"
"$PY" - <<'PYEOF' || exit 1
import sys
from algolab.publish import PublishError, settings, signed_in_session
try:
    cfg = settings()
    signed_in_session(cfg)
    print(f"    Signed in to {cfg['url']}")
except PublishError as e:
    sys.exit(str(e))
PYEOF
echo "==> Prices: the helper copies new ones from the server before each job (needs ~/.algo-trading/server.env)"
[ -f "$HOME/.algo-trading/server.env" ] || echo "    Not set yet: open any deploy/*.command launcher once (e.g. get-server-data.command) to set the server."

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>run.py</string><string>lab-agent</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
sleep 3
if launchctl list | grep -q "$LABEL"; then
  echo
  echo "The Algo Lab helper is running and starts by itself at login."
  echo "On the dashboard (Algo Lab › Run) the Mac now shows as online. While the Mac sleeps, Mac jobs wait."
  echo "Log: $LOG    Remove: bash deploy/install-mac-agent.command remove"
else
  echo "!! The helper did not start. Last lines of $LOG:"; tail -5 "$LOG" 2>/dev/null
fi
