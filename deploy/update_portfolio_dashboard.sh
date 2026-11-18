#!/usr/bin/env bash
# Updates the portfolio dashboard's main page:
#   - invested + P&L = current value shown as one labelled line and one bar (replacing the four cards)
#   - the "Today" day-P&L chart starts at the market open
# Backs up the page first and restores it automatically if the dashboard does not come back.
#   sudo bash deploy/update_portfolio_dashboard.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/update_portfolio_dashboard.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
TRADER_DIR="${TRADER_DIR:-/opt/trader}"
APP_SERVICE="${APP_SERVICE:-trader-app}"
PAGE="$TRADER_DIR/templates/dashboard.html"
[ -f "$PAGE" ] || { echo "The portfolio dashboard page was not found at $PAGE."; exit 1; }
APP_PORT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5000))' "$TRADER_DIR/config.json" 2>/dev/null || echo 5000)"

STAMP="$(date +%Y%m%d-%H%M%S)"
cp -p "$PAGE" "$PAGE.bak-$STAMP"
# Also update the copy the Algo Lab uninstaller would restore, so removing Algo Lab keeps these changes.
TARGETS=("$PAGE")
[ -f "$PAGE.before-algolab" ] && TARGETS+=("$PAGE.before-algolab")
python3 "$SRC/portfolio_page/dashboard_changes.py" "${TARGETS[@]}"

echo "==> Restarting the portfolio dashboard (a few seconds)"
systemctl restart "$APP_SERVICE"
CODE=000
for _ in $(seq 1 30); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$APP_PORT/signin" || true)"
  [ "$CODE" = 200 ] && break
  sleep 1
done
if [ "$CODE" != 200 ]; then
  cp -p "$PAGE.bak-$STAMP" "$PAGE"
  systemctl restart "$APP_SERVICE" || true
  echo "!! The dashboard did not come back (HTTP $CODE); the previous page was restored."
  exit 1
fi
echo "Done. Reload the portfolio dashboard to see the new chart. (Previous page saved as $(basename "$PAGE").bak-$STAMP)"
