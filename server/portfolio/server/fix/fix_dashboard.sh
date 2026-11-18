#!/usr/bin/env bash
# Puts this morning's dashboard changes back and keeps the Blaze Butterfly page.
#
#   sudo bash ~/fix/fix_dashboard.sh
#
# 1. Saves the dashboard's current files to /opt/trader/backup-fix-<time>/.
# 2. Restores app.py, the main page and the Stock history page from the copies Algo Lab's installer
#    saved this morning (*.before-algolab): these have the server's own changes.
# 3. Re-applies this morning's main-page changes (value bar, Today chart from the open) if needed.
# 4. Adds back the Paper trading (straddle) page and adds the Blaze Butterfly page and links.
# 5. Restarts the dashboard and checks both pages; puts everything back as it was if anything fails.
# Neither paper engine is stopped or restarted.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo bash ~/fix/fix_dashboard.sh"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
T="${TRADER_DIR:-/opt/trader}"
PY="$T/venv/bin/python"
USR="$(stat -c %U "$T/app.py")"
PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$T/config.json")"

echo "==> Checking what is on the server"
missing=0
for f in app.py.before-algolab templates/dashboard.html.before-algolab templates/stock.html.before-algolab \
         algolab_panel.py paper_engine.py templates/algo.html; do
    if [ -f "$T/$f" ]; then echo "    found    $f"; else echo "    MISSING  $f"; missing=1; fi
done
[ "$missing" = 0 ] || { echo "Nothing was changed. Send me the lines above."; exit 1; }
for s in algolab-paper trader-paper trader-app; do echo "    $s: $(systemctl is-active $s || true)"; done

STAMP="$(date +%Y%m%d-%H%M%S)"
BK="$T/backup-fix-$STAMP"
echo "==> Saving the current files to $BK"
mkdir -p "$BK/templates"
cp -p "$T/app.py" "$BK/"
cp -p "$T"/templates/*.html "$BK/templates/"

restore() {
    echo "!! $1"
    echo "   Putting the dashboard back exactly as it was before this fix."
    cp -p "$BK/app.py" "$T/app.py"
    cp -p "$BK"/templates/*.html "$T/templates/"
    systemctl restart trader-app || true
    echo "   Done. Nothing else was changed. Please send me the lines above."
    exit 1
}

echo "==> Restoring the server's own versions (saved this morning by Algo Lab)"
cp -p "$T/app.py.before-algolab" "$T/app.py"
cp -p "$T/templates/dashboard.html.before-algolab" "$T/templates/dashboard.html"
cp -p "$T/templates/stock.html.before-algolab" "$T/templates/stock.html"

echo "==> This morning's main-page changes (value bar, Today chart from the open)"
python3 "$HERE/dashboard_changes.py" "$T/templates/dashboard.html" || restore "The main-page changes could not be applied."

echo "==> Adding the Paper trading and Blaze Butterfly pages"
python3 "$HERE/merge_pages.py" "$T" || restore "The pages could not be added."

ROUTES="$(cd "$T" && sudo -u "$USR" "$PY" -c "import app; print(' '.join(sorted(r.rule for r in app.app.url_map.iter_rules())))" 2>&1 | tail -1 || true)"
for r in /paper /api/paper /algo /api/algo /stock; do
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
paper = json.load(web.open(base + "/api/paper", timeout=20))
algo = json.load(web.open(base + "/api/algo", timeout=20))
page = web.open(base + "/", timeout=20).read().decode()
ok = "catalog" in paper and "installed" in algo and 'id="valueBar"' in page and 'href="/paper"' in page and 'href="/algo"' in page
print("ok" if ok else f"unexpected: paper={'catalog' in paper} algo={'installed' in algo} bar={'valueBar' in page}")
PY
)"
[ "$(echo "$CHECK" | tail -1)" = ok ] || restore "A page did not respond as expected: $(echo "$CHECK" | tail -1)"

HOST="$(awk '!/^[[:space:]]*#/ && $1 ~ /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/ && $2=="{" {print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null || true)"
cat <<DONE

Fixed. Reload the dashboard: the top line now has  Stock history · Paper trading · Blaze Butterfly
   Straddle (Algo Lab):  https://${HOST:-<your dashboard address>}/paper
   Blaze Butterfly:      https://${HOST:-<your dashboard address>}/algo
The files from before this fix are kept in $BK
DONE
