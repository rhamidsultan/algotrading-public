#!/usr/bin/env bash
# Installs or updates the portfolio dashboard and the paper-trading engine on the trading server.
# Run after setup.sh, from the repo (or a folder holding app.py and templates/):
#
#     sudo bash deploy.sh
#
# Safe to run again for updates: config.json, paper_config.json, portfolio.db and
# data/paper.db are kept.
set -euo pipefail

APP_DIR=/opt/trader
APP_USER=trader
PORT=5010
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC_DIR/app.py" ] || SRC_DIR="$(cd "$SRC_DIR/.." && pwd)"   # run from the repo: the app sits one folder up

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -x "$APP_DIR/venv/bin/python" ] || { echo "Run setup.sh first."; exit 1; }

echo "==> Copying the dashboard"
mkdir -p "$APP_DIR/templates" "$APP_DIR/data"
if [ -f "$APP_DIR/app.py" ]; then
    BK="$APP_DIR/backup-deploy-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK/templates"
    cp -p "$APP_DIR/app.py" "$BK/"
    cp -p "$APP_DIR"/templates/*.html "$BK/templates/" 2>/dev/null || true
    echo "    previous app.py and pages saved in $BK"
fi
cp "$SRC_DIR/app.py" "$SRC_DIR/config.example.json" "$SRC_DIR/corporate_actions.json" "$APP_DIR/"
cp "$SRC_DIR/paper_engine.py" "$SRC_DIR/paper_config.example.json" "$APP_DIR/"
cp "$SRC_DIR/backtests_panel.py" "$APP_DIR/"   # Backtests page (runs sent from the Mac)
cp "$SRC_DIR/ibkr_panel.py" "$APP_DIR/"   # US stocks page (reads the IBKR sync data)
cp "$SRC_DIR/positions_panel.py" "$APP_DIR/"   # Positions page (live P&L, GTT SL/target, close)
mkdir -p "$APP_DIR/data/backtests"
if [ ! -f "$APP_DIR/paper_config.json" ]; then
    cp "$APP_DIR/paper_config.example.json" "$APP_DIR/paper_config.json"
fi
cp "$SRC_DIR"/templates/*.html "$APP_DIR/templates/"

echo "==> Python packages"
"$APP_DIR/venv/bin/pip" install --quiet flask kiteconnect waitress websocket-client certifi tzdata openpyxl

NEW_PASSWORD=""
if [ ! -f "$APP_DIR/config.json" ]; then
    echo "==> Creating config.json"
    NEW_PASSWORD="$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(9))')"
    DASH_PASSWORD="$NEW_PASSWORD" DASH_PORT="$PORT" "$APP_DIR/venv/bin/python" - "$APP_DIR" <<'PY'
import json, os, sys
from pathlib import Path
d = Path(sys.argv[1])
c = json.loads((d / "config.example.json").read_text())
c.update(api_key="", api_secret="",              # read from .env instead
         dashboard_user="admin", dashboard_password=os.environ["DASH_PASSWORD"],
         port=int(os.environ["DASH_PORT"]), token_file="data/access_token.json")
c["alerts"]["price_levels"] = []
(d / "config.json").write_text(json.dumps(c, indent=2) + "\n")
PY
fi
chmod 600 "$APP_DIR/config.json"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Service"
cat > /etc/systemd/system/trader-app.service <<UNIT
[Unit]
Description=Portfolio dashboard
After=network-online.target trader-auth.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/trader-paper.service <<UNIT
[Unit]
Description=Paper-trading engine (Blaze Butterfly) - never places orders
After=network-online.target trader-auth.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python paper_engine.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable trader-app trader-paper >/dev/null 2>&1
systemctl restart trader-app trader-paper
ufw allow "$PORT/tcp" >/dev/null

sleep 4
IP=$(curl -s --max-time 5 https://api.ipify.org || echo "<server-ip>")
echo
if systemctl is-active --quiet trader-app; then
    echo "Dashboard is running:  http://$IP:$PORT/"
else
    echo "Dashboard did NOT start. Last log lines:"
    journalctl -u trader-app -n 15 --no-pager
fi
if systemctl is-active --quiet trader-paper; then
    echo "Paper engine is running:  http://$IP:$PORT/algo   (logs: journalctl -u trader-paper -f)"
else
    echo "Paper engine did NOT start. Last log lines:"
    journalctl -u trader-paper -n 15 --no-pager
fi
if [ -n "$NEW_PASSWORD" ]; then
    echo "Sign in with   user: admin   password: $NEW_PASSWORD"
    echo "(Save this now. To change it: sudo nano $APP_DIR/config.json, then sudo systemctl restart trader-app)"
fi
echo "Also add a Lightsail firewall rule for TCP $PORT (Networking tab)."
