#!/usr/bin/env bash
# On the Mac: writes deploy/paste-engine-update.txt, one line to paste into the server's terminal
# (Lightsail "Connect using SSH") that updates the server's Algo Lab engine to this folder's code.
#
#   bash deploy/make_server_update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git rev-parse HEAD > deploy/COMMIT 2>/dev/null || rm -f deploy/COMMIT   # which GitHub version this is
git remote get-url origin > deploy/REPO 2>/dev/null || rm -f deploy/REPO   # which GitHub repo (for the Update button)
B64="$(COPYFILE_DISABLE=1 tar -czf - --exclude='__pycache__' algolab run.py requirements-server.txt deploy/update_engine.sh deploy/self_update.sh deploy/set_address.sh $( [ -f deploy/COMMIT ] && echo deploy/COMMIT ) $( [ -f deploy/REPO ] && echo deploy/REPO ) deploy/portfolio_page/algolab_panel.py deploy/portfolio_page/pwa_panel.py deploy/portfolio_page/users_panel.py server/portfolio/templates server/portfolio/app.py server/portfolio/backtests_panel.py server/portfolio/ibkr_panel.py server/portfolio/order_test_panel.py server/portfolio/positions_panel.py server/portfolio/paper_engine.py server/portfolio/server/auth_server.py server/portfolio/server/kite_session.py | base64 | tr -d '\n')"
echo "rm -rf ~/algolab-update && mkdir -p ~/algolab-update && cd ~/algolab-update && echo '$B64' | base64 -d | tar xz --warning=no-unknown-keyword && sudo DASH_HOST=\"\${DASH_HOST:-}\" bash deploy/update_engine.sh" > deploy/paste-engine-update.txt
echo "Wrote deploy/paste-engine-update.txt ($(wc -c < deploy/paste-engine-update.txt | tr -d ' ') characters)."
echo "Open it, copy everything, and paste it into the server's terminal."
