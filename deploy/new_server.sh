#!/usr/bin/env bash
# Sets up a new server from scratch: hardening, the portfolio dashboard, the Algo Lab engine, the phone app and
# logins, and the dashboard's Update button. Run it on a new Ubuntu 24.04 server, from the copy of the repo that
# docs/SERVER_SETUP.md has you clone into /opt/algo-trading/src:
#
#     sudo bash /opt/algo-trading/src/deploy/new_server.sh
#
# Safe to run again: each step keeps what is already there (settings, passwords, data). US stocks (IBKR) is not
# part of it; add it later only if you want it:  sudo bash deploy/add_us_stocks.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo:  sudo bash deploy/new_server.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SRC"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Needs Ubuntu 22.04 or later (Python 3.10+)."; exit 1; }
FRESH=0; [ -f /opt/trader/config.json ] || FRESH=1

echo "######## 1/4  Server basics and the morning Zerodha login page"
bash server/portfolio/server/setup.sh
echo "######## 2/4  Portfolio dashboard"
bash server/portfolio/server/deploy.sh | tee /root/dashboard-first-run.txt
# the dashboard's old built-in paper engine is replaced by Algo Lab; never start it on a new server
systemctl disable -q --now trader-paper 2>/dev/null || true
echo "######## 3/4  Algo Lab engine (paper and live trading, price recording)"
bash deploy/install_server.sh
echo "######## 4/4  Phone app, logins, and the Update button"
bash deploy/update_engine.sh

IP="$(curl -s --max-time 5 https://api.ipify.org || echo '<server address>')"
PORT="$(python3 -c 'import json; print(json.load(open("/opt/trader/config.json")).get("port", 5010))' 2>/dev/null || echo 5010)"
cat <<DONE

==================================================================================================
Set up. Next (docs/SERVER_SETUP.md has the details):
  1. Lightsail > Networking: allow TCP $PORT (the dashboard) and TCP 8080 (the morning login page).
  2. Your Zerodha key:   sudo nano /opt/trader/.env      (KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID)
     then:               sudo systemctl restart trader-auth trader-app algolab-paper
     In the Kite developer console, set the app's redirect URL to   http://$IP:$PORT/callback
  3. Open the dashboard: http://$IP:$PORT/
DONE
[ "$FRESH" = 1 ] && grep -m1 "Sign in with" /root/dashboard-first-run.txt | sed 's/^/     /' \
  && echo "     (also saved in /root/dashboard-first-run.txt: change it on the dashboard, then delete that file)"
cat <<DONE
  4. Updates: Profile > Server version shows a key. Add it on GitHub (your repo > Settings > Deploy keys,
     read-only), then tap Check. After that, Update now pulls each merged change.
  5. Optional: a web address with HTTPS (needed for the phone app):  sudo bash deploy/add_https.sh <your.domain>
     Optional: US stocks from Interactive Brokers:                  sudo bash deploy/add_us_stocks.sh
==================================================================================================
DONE
