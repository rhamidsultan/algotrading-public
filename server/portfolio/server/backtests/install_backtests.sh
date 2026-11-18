#!/usr/bin/env bash
# Adds the Backtests page to the portfolio dashboard on the server.
#
#   sudo bash install_backtests.sh
#
# 1. Saves app.py and the pages it touches to /opt/trader/backup-backtests-<time>/.
# 2. Copies backtests_panel.py and templates/backtests.html, and registers the page in app.py.
# 3. Adds a "Backtests" link to the top line of the Portfolio and Blaze Butterfly pages.
# 4. Restarts the dashboard and checks it; puts everything back as it was if anything fails.
# Safe to run again. The paper engines are not touched or restarted.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo bash install_backtests.sh"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
T="${TRADER_DIR:-/opt/trader}"
PY="$T/venv/bin/python"
[ -f "$T/app.py" ] && [ -x "$PY" ] || { echo "No dashboard found in $T. Nothing was changed."; exit 1; }
USR="$(stat -c %U "$T/app.py")"
PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$T/config.json")"

STAMP="$(date +%Y%m%d-%H%M%S)"
BK="$T/backup-backtests-$STAMP"
echo "==> Saving the current files to $BK"
mkdir -p "$BK/templates"
cp -p "$T/app.py" "$BK/"
cp -p "$T"/templates/*.html "$BK/templates/"
[ -f "$T/backtests_panel.py" ] && cp -p "$T/backtests_panel.py" "$BK/"

restore() {
    echo "!! $1"
    echo "   Putting the dashboard back exactly as it was."
    cp -p "$BK/app.py" "$T/app.py"
    cp -p "$BK"/templates/*.html "$T/templates/"
    if [ -f "$BK/backtests_panel.py" ]; then cp -p "$BK/backtests_panel.py" "$T/"; else rm -f "$T/backtests_panel.py"; fi
    [ -f "$BK/templates/backtests.html" ] || rm -f "$T/templates/backtests.html"
    systemctl restart trader-app || true
    echo "   Done. Nothing else was changed. Please send me the lines above."
    exit 1
}

echo "==> Copying the Backtests page"
install -o "$USR" -g "$USR" -m 644 "$HERE/backtests_panel.py" "$T/backtests_panel.py"
install -o "$USR" -g "$USR" -m 644 "$HERE/templates/backtests.html" "$T/templates/backtests.html"
install -d -o "$USR" -g "$USR" -m 750 "$T/data/backtests"

echo "==> Registering it in app.py and adding the links"
python3 - "$T" <<'PY' || restore "app.py or the pages could not be updated."
import sys
from pathlib import Path
t = Path(sys.argv[1])
app = t / "app.py"
s = app.read_text()
block = ('# >>> backtests page (runs uploaded from Algo Lab on the Mac; added by install_backtests.sh)\n'
         'try:\n'
         '    from backtests_panel import panel as _backtests_panel\n'
         '    app.register_blueprint(_backtests_panel)\n'
         'except Exception as _e:  # the add-on must never stop the portfolio app\n'
         '    print("Backtests page not loaded:", _e, flush=True)\n'
         '# <<< backtests page\n\n')
if "# >>> backtests page" not in s:
    marker = '\nif __name__ == "__main__":'
    if marker not in s:
        sys.exit("app.py has no main block to insert before")
    i = s.rindex(marker)
    s = s[:i + 1] + block + s[i + 1:]
    app.write_text(s)
    print("    app.py: Backtests page registered")
else:
    print("    app.py: already registered")
links = {
    "dashboard.html": ('<a href="/algo">Blaze Butterfly</a>', '<a href="/algo">Blaze Butterfly</a> · <a href="/backtests">Backtests</a>'),
    "algo.html": ('<a href="/">Portfolio</a>', '<a href="/">Portfolio</a> · <a href="/backtests">Backtests</a>'),
}
for name, (old, new) in links.items():
    p = t / "templates" / name
    if not p.exists():
        continue
    h = p.read_text()
    if 'href="/backtests"' in h:
        print(f"    {name}: link already there")
    elif old in h:
        p.write_text(h.replace(old, new, 1))
        print(f"    {name}: Backtests link added")
    else:
        print(f"    {name}: top line not recognised; open /backtests directly")
PY

ROUTES="$(cd "$T" && sudo -u "$USR" "$PY" -c "import app; print(' '.join(sorted(r.rule for r in app.app.url_map.iter_rules())))" 2>&1 | tail -1 || true)"
for r in /backtests /api/backtests /algo /stock; do
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
runs = json.load(web.open(base + "/api/backtests", timeout=20))
page = web.open(base + "/backtests", timeout=20).read().decode()
home = web.open(base + "/", timeout=20).read().decode()
ok = "runs" in runs and "<title>Backtests</title>" in page and 'href="/algo"' in home
print("ok" if ok else f"unexpected: api={'runs' in runs} page={'Backtests' in page} home={'/algo' in home}")
PY
)"
[ "$(echo "$CHECK" | tail -1)" = ok ] || restore "A page did not respond as expected: $(echo "$CHECK" | tail -1)"

HOST="$(awk '!/^[[:space:]]*#/ && $1 ~ /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/ && $2=="{" {print $1; exit}' /etc/caddy/Caddyfile 2>/dev/null || true)"
URL="https://${HOST:-<your dashboard address>}"
cat <<DONE

Done. The dashboard's top line now has a Backtests link:  $URL/backtests

On the Mac, put this in ~/AI - VS/algo-lab/secrets.yaml (below the kite: section):

dashboard:
  url: "$URL"
  user: "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dashboard_user"])' "$T/config.json")"
  password: "your dashboard password"

then send runs with:  .venv/bin/python run.py publish
The files from before this change are kept in $BK
DONE
