#!/usr/bin/env bash
# Sets up Algo Lab on an Ubuntu / Debian server such as AWS Lightsail. Safe to run again after
# copying new files (it keeps your password, data and paper account) and leaves other apps alone.
#
#   sudo bash deploy/install.sh
#
# If the server already runs the Caddy web server, the dashboard is added to it on its own port,
# using the address and certificate Caddy already has. Otherwise nginx is installed for it.
#
# Optional settings, placed before "bash":
#   PORT=8443            public port (default: the first free one)
#   ADMIN_USER=myname     dashboard user name (default admin)
#   ADMIN_PASSWORD=...   set or change the password without being asked
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/install.sh"; exit 1; }
command -v apt-get >/dev/null || { echo "This installer supports Ubuntu and Debian servers only."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null \
  || { echo "Python 3.10 or newer is needed (Ubuntu 22.04 or later)."; exit 1; }

APP="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$APP")}"
PORT="${PORT:-}"
ADMIN_USER="${ADMIN_USER:-admin}"
UNIT=/etc/systemd/system/algolab-dashboard.service
NGINX_SITE=/etc/nginx/conf.d/algolab.conf
HTPASSWD=/etc/nginx/algolab.htpasswd
CERT=/etc/ssl/algolab
CADDYFILE=/etc/caddy/Caddyfile
CADDY_AUTH=/etc/caddy/algolab.auth
busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# ---- which web server will front the dashboard ---------------------------------------------
MODE=nginx; HOST=""; OLD_PORT=""
if [ -f "$NGINX_SITE" ]; then
  OLD_PORT="$(awk '$1=="listen"{print $2; exit}' "$NGINX_SITE")"
elif command -v caddy >/dev/null && systemctl is-active -q caddy && [ -f "$CADDYFILE" ]; then
  # the name Caddy already serves (and holds a certificate for)
  HOST="$(awk '!/^[[:space:]]*#/ && $1 ~ /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/ && $2=="{" {print $1; exit}' "$CADDYFILE")"
  if [ -n "$HOST" ]; then
    MODE=caddy
    OLD_PORT="$(sed -n '/^# >>> algolab/,/^# <<< algolab/p' "$CADDYFILE" | awk '$2=="{" && $1 ~ /:[0-9]+$/ {sub(/.*:/, "", $1); print $1; exit}')"
  fi
fi
if [ -z "$PORT" ]; then
  PORT="$OLD_PORT"
  if [ -z "$PORT" ]; then
    CANDIDATES="443 8443 8444 8445"; [ "$MODE" = caddy ] && CANDIDATES="8443 8444 8445"
    for p in $CANDIDATES; do busy "$p" || { PORT="$p"; break; }; done
  fi
  [ -n "$PORT" ] || { echo "No free public port found; choose one with PORT=..."; exit 1; }
elif [ "$PORT" != "$OLD_PORT" ] && busy "$PORT"; then
  echo "Port $PORT is already used by another program on this server. Choose another with PORT=..."
  exit 1
fi
# Internal port for the app itself (never exposed): keep an earlier choice, else the first free.
APP_PORT=""
[ -f "$UNIT" ] && APP_PORT="$(grep -o 'server.port [0-9]*' "$UNIT" | awk '{print $2}')"
if [ -z "$APP_PORT" ]; then
  for p in 8601 8602 8603 8604 8605; do busy "$p" || { APP_PORT="$p"; break; }; done
  [ -n "$APP_PORT" ] || { echo "No free internal port found (8601-8605)."; exit 1; }
fi
if [ "$MODE" = caddy ]; then
  echo "==> Found Caddy serving $HOST: adding the dashboard to it on port $PORT (your other app is untouched)"
else
  echo "==> Dashboard will be served on port $PORT (other sites on this server are left untouched)"
fi

# ---- system packages -----------------------------------------------------------------------
echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
if [ "$MODE" = nginx ]; then
  HAD_NGINX=0; command -v nginx >/dev/null && HAD_NGINX=1
  BLOCKER=0
  if [ ! -e /usr/sbin/policy-rc.d ]; then   # keeps a new nginx from starting before it is configured
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod +x /usr/sbin/policy-rc.d; BLOCKER=1
  fi
  trap '[ "$BLOCKER" = 1 ] && rm -f /usr/sbin/policy-rc.d' EXIT
  apt-get install -y -qq python3-venv python3-pip nginx openssl curl iproute2 >/dev/null
  [ "$BLOCKER" = 1 ] && rm -f /usr/sbin/policy-rc.d && BLOCKER=0
  [ "$HAD_NGINX" = 0 ] && rm -f /etc/nginx/sites-enabled/default   # no need for the welcome page on port 80
else
  apt-get install -y -qq python3-venv python3-pip openssl curl iproute2 >/dev/null
fi

MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 1500 ] && [ -z "$(swapon --noheadings 2>/dev/null)" ] && [ ! -e /swapfile ]; then
  echo "==> Small server (${MEM_MB} MB memory): adding 1 GB of swap so both apps have room"
  if fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    rm -f /swapfile; echo "    (could not add swap; continuing)"
  fi
fi

echo "==> Installing Python packages (a few minutes on a small server)"
chown -R "$RUN_USER" "$APP"
as_user() { sudo -u "$RUN_USER" "$@"; }
[ -x "$APP/.venv/bin/python" ] || as_user python3 -m venv "$APP/.venv"
as_user "$APP/.venv/bin/pip" install -q --upgrade pip
as_user "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
ls "$APP"/data/spot/*.csv >/dev/null 2>&1 || (cd "$APP" && as_user "$APP/.venv/bin/python" run.py sample-data >/dev/null)
[ -f "$APP/secrets.yaml" ] && chmod 600 "$APP/secrets.yaml"

# ---- password ------------------------------------------------------------------------------
echo "==> Securing the dashboard"
AUTH_FILE="$HTPASSWD"; [ "$MODE" = caddy ] && AUTH_FILE="$CADDY_AUTH"
PW=""; SHOW_PW=""
if [ ! -f "$AUTH_FILE" ] || [ -n "${ADMIN_PASSWORD:-}" ]; then
  PW="${ADMIN_PASSWORD:-}"
  if [ -z "$PW" ] && [ -t 0 ]; then
    while [ "${#PW}" -lt 10 ]; do read -r -s -p "Choose a dashboard password (10 or more characters): " PW; echo; done
  fi
  if [ -z "$PW" ]; then PW="$(openssl rand -base64 15)"; SHOW_PW="$PW"; fi
fi

# ---- background services -------------------------------------------------------------------
echo "==> Setting up background services"
unit() {  # name, description, command
  cat > "/etc/systemd/system/$1.service" <<UNITEOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP
Environment=PYTHONUNBUFFERED=1
ExecStart=$3
Restart=always
RestartSec=5
# if the server ever runs out of memory, give this up before your other apps
OOMScoreAdjust=500

[Install]
WantedBy=multi-user.target
UNITEOF
}
unit algolab-dashboard "Algo Lab dashboard" "$APP/.venv/bin/python -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port $APP_PORT --server.headless true --browser.gatherUsageStats false"
unit algolab-paper "Algo Lab paper-trading service" "$APP/.venv/bin/python run.py paper-service"
systemctl daemon-reload
systemctl enable -q algolab-dashboard algolab-paper
systemctl restart algolab-dashboard algolab-paper

# ---- web server ----------------------------------------------------------------------------
if [ "$MODE" = caddy ]; then
  if [ -n "$PW" ]; then
    printf '%s %s\n' "$ADMIN_USER" "$(caddy hash-password --plaintext "$PW")" > "$CADDY_AUTH"
    chmod 600 "$CADDY_AUTH"
  fi
  read -r AUTH_USER AUTH_HASH < "$CADDY_AUTH"
  ADMIN_USER="$AUTH_USER"
  [ -f "$CADDYFILE.before-algolab" ] || cp -p "$CADDYFILE" "$CADDYFILE.before-algolab"
  cp -p "$CADDYFILE" "$CADDYFILE.algolab-prev"
  NEW="$(mktemp)"; OK=0
  for directive in basic_auth basicauth; do   # the name differs between Caddy versions
    {
      sed '/^# >>> algolab/,/^# <<< algolab/d' "$CADDYFILE"
      printf '# >>> algolab (added by algo-trading-platform/deploy/install.sh; safe to delete this block)\n'
      printf '%s:%s {\n\t%s {\n\t\t%s %s\n\t}\n\treverse_proxy 127.0.0.1:%s\n}\n' \
        "$HOST" "$PORT" "$directive" "$AUTH_USER" "$AUTH_HASH" "$APP_PORT"
      printf '# <<< algolab\n'
    } > "$NEW"
    if caddy adapt --config "$NEW" --adapter caddyfile >/dev/null 2>&1; then OK=1; break; fi
  done
  if [ "$OK" != 1 ]; then
    rm -f "$NEW"
    echo "Could not build a valid Caddy configuration, so Caddy was left exactly as it was."
    echo "The app itself is running; send the output of:  caddy version"
    exit 1
  fi
  cat "$NEW" > "$CADDYFILE"; rm -f "$NEW"
  if ! systemctl reload caddy; then
    cat "$CADDYFILE.algolab-prev" > "$CADDYFILE"; systemctl reload caddy || true
    echo "Caddy did not accept the change, so its previous configuration was restored."
    exit 1
  fi
  URL="https://$HOST:$PORT"
  CERT_NOTE="   It uses the certificate your server already has, so there is no browser warning."
else
  if [ -n "$PW" ]; then
    printf '%s:%s\n' "$ADMIN_USER" "$(openssl passwd -apr1 "$PW")" > "$HTPASSWD"
    chown root:www-data "$HTPASSWD"; chmod 640 "$HTPASSWD"
  fi
  ADMIN_USER="$(cut -d: -f1 "$HTPASSWD" | head -1)"
  if [ ! -f "$CERT/cert.pem" ]; then
    mkdir -p "$CERT"
    openssl req -x509 -nodes -newkey rsa:2048 -days 825 -subj "/CN=algolab" \
      -keyout "$CERT/key.pem" -out "$CERT/cert.pem" 2>/dev/null
    chmod 600 "$CERT/key.pem"
  fi
  cat > "$NGINX_SITE" <<SITEEOF
# Algo Lab dashboard: HTTPS and a password in front of the app, which itself listens only locally.
server {
    listen $PORT ssl;
    server_name _;
    ssl_certificate     $CERT/cert.pem;
    ssl_certificate_key $CERT/key.pem;
    auth_basic           "Algo Lab";
    auth_basic_user_file $HTPASSWD;
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_read_timeout 86400;
    }
}
SITEEOF
  systemctl enable -q nginx
  nginx -t -q || { echo "The web server configuration has a problem (shown above); the dashboard is not reachable yet."; exit 1; }
  if systemctl is-active -q nginx; then systemctl reload nginx; else systemctl restart nginx; fi   # reload keeps other sites up
  IP="$(curl -fs --max-time 5 https://checkip.amazonaws.com 2>/dev/null || true)"
  SUFFIX=""; [ "$PORT" = 443 ] || SUFFIX=":$PORT"
  URL="https://${IP:-<your server IP>}$SUFFIX"
  CERT_NOTE="   The browser warns about the certificate (it is self-made, the connection is still encrypted):
   choose Advanced > Proceed."
fi

for _ in $(seq 1 40); do curl -fs "http://127.0.0.1:$APP_PORT/_stcore/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "http://127.0.0.1:$APP_PORT/_stcore/health" >/dev/null 2>&1 \
  || echo "WARNING: the dashboard has not come up yet. Check:  sudo journalctl -u algolab-dashboard -n 50"

cat <<DONEEOF

Done. Two things left:

1. In the Lightsail console open your instance > Networking > IPv4 firewall > Add rule:
   Custom TCP, port $PORT. Restricting it to your own IP address is safer still.
2. Open  $URL
$CERT_NOTE
   Sign in as "$ADMIN_USER" with your dashboard password.
DONEEOF
[ -n "$SHOW_PW" ] && echo "   Generated password (shown once, note it down): $SHOW_PW"
cat <<DONEEOF

Useful later:
   sudo systemctl status algolab-dashboard algolab-paper     are they running?
   sudo journalctl -u algolab-paper -f                        live paper-trading log
   sudo ADMIN_PASSWORD='new password' bash deploy/install.sh  change the password
DONEEOF
