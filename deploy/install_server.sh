#!/usr/bin/env bash
# Adds Algo Lab paper trading to the portfolio dashboard server.
#
#   sudo bash deploy/install_server.sh
#
# What it does:
#   - copies the Algo Lab engine to /opt/algolab and runs it as a background service, as the same
#     user as the portfolio app, reusing that app's Zerodha key and morning login (it never logs in)
#   - stores real NIFTY and option prices after each close, for backtests on your Mac
#   - adds a "Paper trading" page to the portfolio dashboard (behind its existing sign-in)
# The portfolio app's files are backed up first and restored automatically if anything fails.
# Safe to run again after copying a newer version. To remove:  sudo bash deploy/uninstall_server.sh
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/install_server.sh"; exit 1; }
command -v apt-get >/dev/null || { echo "This installer supports Ubuntu and Debian servers only."; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
TRADER_DIR="${TRADER_DIR:-/opt/trader}"
ENGINE="${ENGINE_DIR:-/opt/algolab}"
APP_SERVICE="${APP_SERVICE:-trader-app}"
[ -f "$TRADER_DIR/app.py" ] || { echo "The portfolio app was not found in $TRADER_DIR."; exit 1; }
RUN_USER="$(stat -c %U "$TRADER_DIR/app.py")"
TRADER_PY="$TRADER_DIR/venv/bin/python"; [ -x "$TRADER_PY" ] || TRADER_PY="$(command -v python3)"
APP_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$TRADER_DIR/config.json" 2>/dev/null || echo 5000)"
as_user() { sudo -u "$RUN_USER" "$@"; }

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip rsync curl >/dev/null

MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 1500 ] && [ -z "$(swapon --noheadings 2>/dev/null)" ] && [ ! -e /swapfile ]; then
  echo "==> Small server (${MEM_MB} MB memory): adding 1 GB of swap so both apps have room"
  if fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    rm -f /swapfile; echo "    (could not add swap; continuing)"
  fi
fi

echo "==> Copying the Algo Lab engine to $ENGINE"
mkdir -p "$ENGINE"
rsync -a --delete --exclude '/venv/' --exclude '/.venv/' --exclude '/runtime/' --exclude '/data/' \
  --exclude '/secrets.yaml' --exclude '/config.yaml' --exclude '__pycache__/' --exclude '/.pytest_cache/' \
  --exclude '/.streamlit/' --exclude '/.git/' "$SRC/" "$ENGINE/"
[ -f "$ENGINE/config.yaml" ] || cp "$SRC/config.yaml" "$ENGINE/config.yaml"
mkdir -p "$ENGINE/runtime" "$ENGINE/data/spot" "$ENGINE/data/options"

# Reuse the portfolio app's key (.env or config.json) and its daily login file.
python3 - "$TRADER_DIR" "$ENGINE/secrets.yaml" <<'PY'
import json, sys
from pathlib import Path
trader, out = Path(sys.argv[1]), Path(sys.argv[2])
try:
    cfg = json.loads((trader / "config.json").read_text())
except (OSError, ValueError):
    cfg = {}
env = {}
try:
    for line in (trader / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
except OSError:
    pass
token_file = trader / cfg.get("token_file", "token.json")
lines = ["# Algo Lab reuses the portfolio app's Zerodha key and its morning login; it never logs in itself.",
         "kite:", f'  env_file: "{trader / ".env"}"', f'  token_file: "{token_file}"']
if not env.get("KITE_API_KEY") and cfg.get("api_key") and "PASTE" not in cfg["api_key"]:
    lines.append(f'  api_key: "{cfg["api_key"]}"')
out.write_text("\n".join(lines) + "\n")
PY
chown -R "$RUN_USER:$RUN_USER" "$ENGINE"
chmod 600 "$ENGINE/secrets.yaml"

echo "==> Installing Python packages for the engine (a few minutes on a small server)"
[ -x "$ENGINE/venv/bin/python" ] || as_user python3 -m venv "$ENGINE/venv"
as_user "$ENGINE/venv/bin/pip" install -q --upgrade pip
as_user "$ENGINE/venv/bin/pip" install -q -r "$ENGINE/requirements-server.txt"
(cd "$ENGINE" && as_user "$ENGINE/venv/bin/python" run.py catalog)
if ! (cd "$ENGINE" && as_user "$ENGINE/venv/bin/python" -c "from algolab import kite; kite.load_secrets()" >/dev/null 2>&1); then
  echo "    Note: the Zerodha API key was not found in $TRADER_DIR/.env or config.json."
  echo "    Live prices will wait until it is there; the simulated market works regardless."
fi

echo "==> Setting up the engine's background services"
cat > /etc/systemd/system/algolab-paper.service <<UNIT
[Unit]
Description=Algo Lab paper-trading engine
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$ENGINE
Environment=PYTHONUNBUFFERED=1
ExecStart=$ENGINE/venv/bin/python run.py paper-service
Restart=always
RestartSec=5
# if the server ever runs short of memory, give this up before the portfolio app
OOMScoreAdjust=500

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/algolab-collect.service <<UNIT
[Unit]
Description=Algo Lab: store the day's NIFTY and option prices

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$ENGINE
ExecStart=$ENGINE/venv/bin/python run.py collect
OOMScoreAdjust=500
UNIT
cat > /etc/systemd/system/algolab-collect.timer <<UNIT
[Unit]
Description=Algo Lab: store prices after each trading day's close

[Timer]
OnCalendar=Mon..Fri 15:45 Asia/Kolkata

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable -q algolab-paper algolab-collect.timer
systemctl restart algolab-paper
systemctl start algolab-collect.timer

echo "==> Adding the Paper trading page to the portfolio dashboard"
for f in app.py templates/dashboard.html templates/stock.html; do
  if [ -f "$TRADER_DIR/$f" ] && [ ! -f "$TRADER_DIR/$f.before-algolab" ]; then
    cp -p "$TRADER_DIR/$f" "$TRADER_DIR/$f.before-algolab"
  fi
done
install -o "$RUN_USER" -g "$RUN_USER" -m 444 "$SRC/deploy/portfolio_page/algolab_panel.py" "$TRADER_DIR/algolab_panel.py"

rollback() {
  echo "!! $1"
  echo "   Restoring the portfolio app exactly as it was."
  for f in app.py templates/dashboard.html templates/stock.html; do
    if [ -f "$TRADER_DIR/$f.before-algolab" ]; then cp -p "$TRADER_DIR/$f.before-algolab" "$TRADER_DIR/$f"; fi
  done
  rm -f "$TRADER_DIR/algolab_panel.py"
  systemctl restart "$APP_SERVICE" || true
  echo "   The portfolio app is back to its previous version. Please send me the lines above."
  exit 1
}

python3 - "$TRADER_DIR" <<'PY' || rollback "The portfolio app's files have an unexpected layout."
import sys
from pathlib import Path
d = Path(sys.argv[1])

def rewrite(p, s):
    with p.open("w") as f:   # in place: keeps the file's owner and permissions
        f.write(s)

app = d / "app.py"
s = app.read_text()
if "# >>> algolab page" not in s:
    anchor = '\nif __name__ == "__main__":'
    if s.count(anchor) != 1:
        sys.exit(1)
    block = ('# >>> algolab page (paper trading; added by algo-trading-platform/deploy/install_server.sh)\n'
             'try:\n'
             '    from algolab_panel import panel as _algolab_panel\n'
             '    app.register_blueprint(_algolab_panel)\n'
             'except Exception as _e:  # the add-on must never stop the portfolio app\n'
             '    print("Paper trading page not loaded:", _e, flush=True)\n'
             '# <<< algolab page\n\n')
    rewrite(app, s.replace(anchor, "\n" + block + anchor.lstrip("\n"), 1))

links = (("dashboard.html", '<a href="/stock">Stock history</a>',
          '<a href="/stock">Stock history</a> · <a href="/paper">Paper trading</a>'),
         ("stock.html", '<a href="/">Portfolio</a>', '<a href="/">Portfolio</a> · <a href="/paper">Paper trading</a>'))
for name, old, new in links:   # a missing link is cosmetic: the page still works at /paper
    p = d / "templates" / name
    if p.exists():
        t = p.read_text()
        if 'href="/paper"' not in t and t.count(old) == 1:
            rewrite(p, t.replace(old, new, 1))
PY

ROUTES="$(cd "$TRADER_DIR" && as_user "$TRADER_PY" -c "import app; print(' '.join(sorted(r.rule for r in app.app.url_map.iter_rules())))" 2>&1 | tail -1 || true)"
case " $ROUTES " in *" /paper "*) ;; *) rollback "The page could not be added to the portfolio app: $ROUTES" ;; esac

echo "==> Restarting the portfolio dashboard (a few seconds)"
systemctl restart "$APP_SERVICE"
CODE=000
for _ in $(seq 1 30); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT/signin" || true)"
  [ "$CODE" = 200 ] && break
  sleep 1
done
[ "$CODE" = 200 ] || rollback "The portfolio dashboard did not come back after the change (HTTP $CODE)."
# Sign in the way a browser does and load the new page's data, to be sure it works end to end.
CHECK="$(python3 - "$TRADER_DIR/config.json" "$APP_PORT" <<'PY' 2>&1 || true
import http.cookiejar, json, sys, urllib.parse, urllib.request
cfg = json.load(open(sys.argv[1]))
base = f"http://127.0.0.1:{sys.argv[2]}"
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
form = urllib.parse.urlencode({"username": cfg["dashboard_user"], "password": cfg["dashboard_password"]}).encode()
web.open(base + "/signin", form, timeout=20)
data = json.load(web.open(base + "/api/paper", timeout=20))
print("ok" if "catalog" in data else "unexpected reply")
PY
)"
[ "$(echo "$CHECK" | tail -1)" = ok ] || rollback "The new page did not respond as expected: $(echo "$CHECK" | tail -1)"

HOST="$(awk '!/^[[:space:]]*#/ && $1 ~ /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/ && $2=="{" {print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null || true)"
cat <<DONE

Done. Open your portfolio dashboard and tap "Paper trading" at the top, or go straight to
   https://${HOST:-<your dashboard address>}/paper

- Sessions use your morning Zerodha login automatically; Algo Lab never logs in by itself.
- Real NIFTY and option prices are stored after each close (3:45 pm IST) for backtests on your Mac.

Useful later:
   sudo systemctl status algolab-paper           is the engine running?
   sudo journalctl -u algolab-paper -f           its live log
   sudo journalctl -u algolab-collect -n 20      the last price collection
   sudo bash deploy/uninstall_server.sh          remove it and restore the portfolio app
DONE
