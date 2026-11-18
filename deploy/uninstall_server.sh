#!/usr/bin/env bash
# Removes Algo Lab from the portfolio server and restores the portfolio app's own files.
#   sudo bash deploy/uninstall_server.sh            (keeps /opt/algolab with its data)
#   sudo PURGE=1 bash deploy/uninstall_server.sh     (deletes /opt/algolab as well)
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
TRADER_DIR="${TRADER_DIR:-/opt/trader}"
ENGINE="${ENGINE_DIR:-/opt/algolab}"
APP_SERVICE="${APP_SERVICE:-trader-app}"
systemctl disable --now algolab-paper algolab-collect.timer 2>/dev/null
rm -f /etc/systemd/system/algolab-paper.service /etc/systemd/system/algolab-collect.service \
      /etc/systemd/system/algolab-collect.timer
systemctl daemon-reload
for f in app.py templates/dashboard.html templates/stock.html; do
  if [ -f "$TRADER_DIR/$f.before-algolab" ]; then
    cp -p "$TRADER_DIR/$f.before-algolab" "$TRADER_DIR/$f" && rm -f "$TRADER_DIR/$f.before-algolab"
  fi
done
rm -f "$TRADER_DIR/algolab_panel.py"
systemctl restart "$APP_SERVICE"
if [ "${PURGE:-0}" = 1 ]; then rm -rf "$ENGINE"; echo "Deleted $ENGINE."; fi
echo "Algo Lab removed; the portfolio app is back to its own files."
