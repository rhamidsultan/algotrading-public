#!/usr/bin/env bash
# Optional: US stocks from Interactive Brokers (IBKR) on the dashboard (Portfolio > US, and the Summary card).
# Leave it out if you have no IBKR account: the dashboard hides US until this is installed.
#
#     sudo bash deploy/add_us_stocks.sh
#
# What it does (read-only: the gateway runs with READ_ONLY_API and listens only inside the server):
#   1. installs Docker and runs IB Gateway in it, logging in with your IBKR user (asked here, kept in ~/ibkr/.env);
#   2. installs the sync that reads your US holdings every minute in US market hours;
#   3. adds the US page to the dashboard (server/portfolio/server/ibkr/install_ibkr_panel.sh).
# Needs about 1 GB of memory: a 2 GB server or larger. Approve the login in IBKR Mobile when it asks.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/add_us_stocks.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$SRC/server/portfolio/server/ibkr/sync"
U="${SUDO_USER:-ubuntu}"
IB="${IBKR_DIR:-$(getent passwd "$U" | cut -d: -f6)/ibkr}"
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
[ "$MEM_MB" -ge 1800 ] || { echo "This server has ${MEM_MB} MB of memory; IB Gateway needs a 2 GB server or larger. Nothing was changed."; exit 1; }

echo "==> Docker"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 python3-venv >/dev/null
fi
systemctl enable -q --now docker

echo "==> IBKR files in $IB"
install -d -o "$U" -g "$U" "$IB" "$IB/tws_settings" "$IB/data"
for f in docker-compose.yml ibkr_sync.py requirements.txt; do install -o "$U" -g "$U" -m 644 "$SYNC/$f" "$IB/$f"; done
if [ ! -f "$IB/.env" ]; then
  read -r -p "  IBKR user name: " IBU
  read -r -s -p "  IBKR password (not shown): " IBP; echo
  [ -n "$IBU" ] && [ -n "$IBP" ] || { echo "Both are needed. Nothing else was changed."; exit 1; }
  VNC="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
  umask 077
  printf 'TWS_USERID=%s\nTWS_PASSWORD=%s\nVNC_SERVER_PASSWORD=%s\nDATA_DIR=%s\nSYNC_INTERVAL_SEC=60\n' "$IBU" "$IBP" "$VNC" "$IB/data" > "$IB/.env"
  chown "$U:$U" "$IB/.env"; chmod 600 "$IB/.env"
  unset IBP
fi

echo "==> IB Gateway (approve the login in IBKR Mobile on your phone now)"
(cd "$IB" && docker compose up -d)
echo "==> The sync"
[ -x "$IB/venv/bin/python" ] || sudo -u "$U" python3 -m venv "$IB/venv"
sudo -u "$U" "$IB/venv/bin/pip" install -q -r "$IB/requirements.txt"
sed -e "s|^User=.*|User=$U|" -e "s|/home/ubuntu/ibkr|$IB|g" "$SYNC/ibkr-sync.service" > /etc/systemd/system/ibkr-sync.service
systemctl daemon-reload
systemctl enable -q --now ibkr-sync

echo "==> The US page on the dashboard"
IBKR_DIR="$IB" bash "$SRC/server/portfolio/server/ibkr/install_ibkr_panel.sh"
cat <<DONE

US stocks added. Holdings appear once the gateway is logged in (approve it in IBKR Mobile), during US market hours.
  Gateway log:   cd $IB && sudo docker compose logs --tail 50
  Sync log:      sudo journalctl -u ibkr-sync -n 30
  IBKR asks for a fresh approval about once a week; on the Mac, server/portfolio/server/ibkr/mac/ibkr-login helps.
To remove it later:  sudo systemctl disable --now ibkr-sync; cd $IB && sudo docker compose down
DONE
