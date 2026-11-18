#!/usr/bin/env bash
# Adds the "US stocks" (IBKR) page to the portfolio dashboard on the server.
#
#   sudo bash install_ibkr_panel.sh
#
# 1. Saves app.py and the pages to /opt/trader/backup-ibkr-<time>/.
# 2. Moves the IBKR sync's data to /opt/trader/data/ibkr so the dashboard can read it,
#    and restarts the ibkr-sync service (the IB Gateway itself is not touched).
# 3. Copies ibkr_panel.py and templates/us.html, registers the page in app.py and
#    adds a "US stocks" link to the Portfolio page.
# 4. Restarts the dashboard and checks it; puts everything back if anything fails.
# Safe to run again. The paper engines are not touched.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo bash install_ibkr_panel.sh"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
T="${TRADER_DIR:-/opt/trader}"
IB="${IBKR_DIR:-/home/ubuntu/ibkr}"
PY="$T/venv/bin/python"
[ -f "$T/app.py" ] && [ -x "$PY" ] || { echo "No dashboard found in $T. Nothing was changed."; exit 1; }
[ -f "$IB/.env" ] || { echo "No IBKR sync found in $IB. Nothing was changed."; exit 1; }
USR="$(stat -c %U "$T/app.py")"
IBUSR="$(stat -c %U "$IB/ibkr_sync.py")"
PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$T/config.json")"
DATA="$T/data/ibkr"

STAMP="$(date +%Y%m%d-%H%M%S)"
BK="$T/backup-ibkr-$STAMP"
echo "==> Saving the current files to $BK"
mkdir -p "$BK/templates"
cp -p "$T/app.py" "$BK/"
cp -p "$T"/templates/*.html "$BK/templates/"
cp -p "$IB/.env" "$BK/ibkr.env"
[ -f "$T/ibkr_panel.py" ] && cp -p "$T/ibkr_panel.py" "$BK/"

restore() {
    echo "!! $1"
    echo "   Putting the dashboard back exactly as it was."
    cp -p "$BK/app.py" "$T/app.py"
    cp -p "$BK"/templates/*.html "$T/templates/"
    if [ -f "$BK/ibkr_panel.py" ]; then cp -p "$BK/ibkr_panel.py" "$T/"; else rm -f "$T/ibkr_panel.py"; fi
    [ -f "$BK/templates/us.html" ] || rm -f "$T/templates/us.html"
    systemctl restart trader-app || true
    echo "   Done. (IBKR data stays in $DATA; the sync keeps working.) Please send me the lines above."
    exit 1
}

echo "==> Giving the dashboard read access to the IBKR data ($DATA)"
usermod -aG "$USR" "$IBUSR"                       # lets the sync reach $T/data
install -d -o "$IBUSR" -g "$USR" -m 2750 "$DATA"  # sync writes, dashboard reads
OLD="$(grep -E '^DATA_DIR=' "$IB/.env" | cut -d= -f2- || true)"
case "$OLD" in /*) OLDP="$OLD" ;; *) OLDP="$IB/${OLD#./}" ;; esac
if [ -n "$OLD" ] && [ "$OLDP" != "$DATA" ] && [ -d "$OLDP" ]; then
    cp -pn "$OLDP"/ibkr.db "$OLDP"/ibkr_snapshot.json "$DATA/" 2>/dev/null || true
fi
chown -R "$IBUSR:$USR" "$DATA"; chmod 640 "$DATA"/* 2>/dev/null || true
if grep -qE '^SYNC_INTERVAL_SEC=' "$IB/.env"; then sed -i "s|^SYNC_INTERVAL_SEC=.*|SYNC_INTERVAL_SEC=60|" "$IB/.env"; else echo "SYNC_INTERVAL_SEC=60" >> "$IB/.env"; fi
if grep -qE '^DATA_DIR=' "$IB/.env"; then sed -i "s|^DATA_DIR=.*|DATA_DIR=$DATA|" "$IB/.env"; else echo "DATA_DIR=$DATA" >> "$IB/.env"; fi
grep -q '^UMask' /etc/systemd/system/ibkr-sync.service || sed -i 's|^\[Service\]|[Service]\nUMask=0027|' /etc/systemd/system/ibkr-sync.service
sudo -u "$IBUSR" test -w "$DATA" || chmod g+x "$T" "$T/data"
sudo -u "$IBUSR" test -w "$DATA" || { echo "!! The IBKR sync cannot write to $DATA. Nothing else was changed."; sed -i "s|^DATA_DIR=.*|DATA_DIR=$OLD|" "$IB/.env"; exit 1; }
systemctl daemon-reload
systemctl restart ibkr-sync
echo "    ibkr-sync now writes to $DATA, every minute"

echo "==> Copying the US stocks page"
install -o "$USR" -g "$USR" -m 644 "$HERE/ibkr_panel.py" "$T/ibkr_panel.py"
install -o "$USR" -g "$USR" -m 644 "$HERE/templates/us.html" "$T/templates/us.html"

echo "==> Registering it in app.py and adding the link"
python3 - "$T" <<'PY' || restore "app.py or the pages could not be updated."
import sys
from pathlib import Path
t = Path(sys.argv[1])
app = t / "app.py"
s = app.read_text()
block = ('# >>> ibkr page (US stocks from the IBKR sync; added by install_ibkr_panel.sh)\n'
         'try:\n'
         '    from ibkr_panel import panel as _ibkr_panel\n'
         '    app.register_blueprint(_ibkr_panel)\n'
         'except Exception as _e:  # the add-on must never stop the portfolio app\n'
         '    print("US stocks page not loaded:", _e, flush=True)\n'
         '# <<< ibkr page\n\n')
if "# >>> ibkr page" not in s:
    marker = '\nif __name__ == "__main__":'
    if marker not in s:
        sys.exit("app.py has no main block to insert before")
    i = s.rindex(marker)
    s = s[:i + 1] + block + s[i + 1:]
    app.write_text(s)
    print("    app.py: US stocks page registered")
else:
    print("    app.py: already registered")
p = t / "templates" / "dashboard.html"
h = p.read_text()
old = '<a href="/stock">Stock history</a>'
if 'href="/us"' in h:
    print("    dashboard.html: link already there")
elif old in h:
    p.write_text(h.replace(old, old + ' · <a href="/us">US stocks</a>', 1))
    print("    dashboard.html: US stocks link added")
else:
    print("    dashboard.html: top line not recognised; open /us directly")
PY

ROUTES="$(cd "$T" && sudo -u "$USR" "$PY" -c "import app; print(' '.join(sorted(r.rule for r in app.app.url_map.iter_rules())))" 2>&1 | tail -1 || true)"
for r in /us /api/us /stock; do
    case " $ROUTES " in *" $r "*) ;; *) restore "The app does not load correctly (no $r): $ROUTES" ;; esac
done

echo "==> Restarting the portfolio dashboard"
systemctl restart trader-app
CODE=000
for _ in $(seq 1 30); do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/signin" || true)"
    [ "$CODE" = 200 ] && break
    sleep 1
done
[ "$CODE" = 200 ] || restore "The dashboard did not come back (HTTP $CODE)."

CHECK="$(python3 - "$T/config.json" "$PORT" <<'PY' 2>&1 || true
import http.cookiejar, json, sys, urllib.parse, urllib.request
cfg = json.load(open(sys.argv[1]))
base = f"http://127.0.0.1:{sys.argv[2]}"
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
form = urllib.parse.urlencode({"username": cfg["dashboard_user"], "password": cfg["dashboard_password"]}).encode()
web.open(base + "/signin", form, timeout=20)
api = json.load(web.open(base + "/api/us", timeout=20))
page = web.open(base + "/us", timeout=20).read().decode()
if "<title>US stocks</title>" not in page:
    print("unexpected: page")
elif not api.get("ok"):
    print("ok-nodata " + api.get("error", ""))
else:
    print(f"ok {len(api['positions'])} holdings, updated {api['updated_ist']}")
PY
)"
LAST="$(echo "$CHECK" | tail -1)"
case "$LAST" in ok*) echo "    $LAST" ;; *) restore "A page did not respond as expected: $LAST" ;; esac

HOST="$(awk '!/^[[:space:]]*#/ && $1 ~ /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/ && $2=="{" {print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null || true)"
cat <<DONE

Done. The Portfolio page's top line now has a "US stocks" link:  https://${HOST:-<your dashboard address>}/us
The files from before this change are kept in $BK
DONE
