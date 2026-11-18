#!/usr/bin/env bash
# One-time setup for the trading server (Ubuntu on AWS Lightsail).
#
# Usage, from a copy of the repo on the server (deploy/new_server.sh runs it for you):
#
#     sudo bash server/portfolio/server/setup.sh
#
# Safe to run again after you add or change files.
set -euo pipefail

APP_DIR=/opt/trader
APP_USER=trader
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

echo "==> Clock to IST"
timedatectl set-timezone Asia/Kolkata

echo "==> Packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip ufw fail2ban

echo "==> SSH: keys only, no root login"
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin no
EOF
systemctl reload ssh

echo "==> Firewall: SSH + login page only"
ufw allow OpenSSH
ufw allow 8080/tcp
ufw --force enable

echo "==> App user and folder"
id "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data"
for f in auth_server.py kite_session.py main.py; do
    [ -f "$SRC_DIR/$f" ] && cp "$SRC_DIR/$f" "$APP_DIR/"
done

if [ ! -f "$APP_DIR/.env" ]; then
    cat > "$APP_DIR/.env" <<'EOF'
# Fill these in, then: sudo systemctl restart trader-auth
KITE_API_KEY=
KITE_API_SECRET=
KITE_USER_ID=
AUTH_PORT=8080
EOF
fi
chmod 600 "$APP_DIR/.env"

echo "==> Python environment"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet kiteconnect flask waitress python-dotenv pandas
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Services"
cat > /etc/systemd/system/trader-auth.service <<EOF
[Unit]
Description=Kite morning login page
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python auth_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/trader-app.service <<EOF
[Unit]
Description=Portfolio dashboard / trading script
After=network-online.target trader-auth.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python main.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now trader-auth
if [ -f "$APP_DIR/main.py" ]; then
    systemctl enable trader-app
    systemctl restart trader-app
else
    echo "    (no main.py yet - trader-app will be enabled when you add it and re-run)"
fi

IP=$(curl -s --max-time 5 https://api.ipify.org || echo "<server-ip>")
cat <<EOF

Done.
  1. Edit keys:        sudo nano $APP_DIR/.env   then   sudo systemctl restart trader-auth
  2. Lightsail console: Networking tab -> add rule TCP 8080
  3. Morning login:    http://$IP:8080/
  4. Logs:             journalctl -u trader-auth -f   /   journalctl -u trader-app -f
EOF
