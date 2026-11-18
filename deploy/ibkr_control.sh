#!/usr/bin/env bash
# Starts or stops IBKR (IB Gateway in Docker and the US holdings sync) when the dashboard asks.
#
# The dashboard (Summary › Health › IBKR) writes "start", "stop" or "restart" into $ENGINE/runtime/ibkr.request; the
# algolab-ibkr.path unit (installed by update_engine.sh) runs this as root. The result goes to runtime/ibkr-control.json,
# which the dashboard shows. Stopping frees about 1 GB of memory and stays stopped after a server restart, until
# started again; nothing is deleted (the gateway's settings and the US holdings history stay).
set -uo pipefail
ENGINE="${ENGINE_DIR:-/opt/algolab}"
RT="$ENGINE/runtime"
REQ="$RT/ibkr.request"
OUT="$RT/ibkr-control.json"
OWNER="$(stat -c %U "$ENGINE/run.py" 2>/dev/null || echo root)"

ACT="$(tr -dc 'a-z' < "$REQ" 2>/dev/null || true)"
rm -f "$REQ"

state() {    # state <working|done|failed> <action> <message>
  python3 - "$OUT" "$1" "$2" "$3" <<'PY'
import json, os, sys
from datetime import datetime
out, status, action, msg = sys.argv[1:5]
tmp = out + ".tmp"
with open(tmp, "w") as f:
    json.dump({"status": status, "action": action, "message": msg, "at": datetime.now().isoformat(timespec="seconds")}, f)
os.replace(tmp, out)
PY
  chown "$OWNER" "$OUT" 2>/dev/null || true
}

case "$ACT" in start|stop|restart) ;; *) state failed "$ACT" "Unknown request '$ACT'."; exit 0 ;; esac

IB="$(systemctl show -p WorkingDirectory --value ibkr-sync 2>/dev/null || true)"
if [ -z "$IB" ] || [ ! -f "$IB/docker-compose.yml" ]; then
  state failed "$ACT" "IBKR is not installed on this server (deploy/add_us_stocks.sh adds it)."
  exit 0
fi

stop_all() {
  systemctl disable --now ibkr-sync >/dev/null 2>&1 || true
  (cd "$IB" && docker compose down) >/dev/null 2>&1
}
start_all() {
  (cd "$IB" && docker compose up -d) >/dev/null 2>&1 || return 1
  systemctl enable --now ibkr-sync >/dev/null 2>&1 || return 1
}

case "$ACT" in
  stop)
    state working stop "Stopping IBKR"
    if stop_all; then state done stop "IBKR stopped. It stays off until you start it."
    else state failed stop "Could not stop IB Gateway: $(cd "$IB" && docker compose ps 2>&1 | tail -1)"; fi ;;
  start)
    state working start "Starting IBKR"
    if start_all; then state done start "IBKR started. If IBKR Mobile asks, approve the login."
    else state failed start "Could not start IBKR: $(cd "$IB" && docker compose logs --tail 3 2>&1 | tail -1)"; fi ;;
  restart)
    state working restart "Restarting IBKR"
    stop_all
    if start_all; then state done restart "IBKR restarted. If IBKR Mobile asks, approve the login."
    else state failed restart "Could not start IBKR again: $(cd "$IB" && docker compose logs --tail 3 2>&1 | tail -1)"; fi ;;
esac
