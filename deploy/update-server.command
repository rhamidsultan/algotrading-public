#!/bin/bash
# Double-click to update the Algo Lab engine on the Lightsail server (runs deploy/paste-engine-update.txt there).
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
chmod 600 "$KEY"
# A new instance (e.g. the 2 GB move of 13 Oct 2026) may have new SSH host keys at the same address. Forget the old
# ones once, so ssh learns the new instance's keys instead of refusing to connect.
# Your own web address for the dashboard (once):  DASH_HOST=dash.example.com bash deploy/update-server.command
if [ ! -f deploy/.hostkey-reset-2gb ]; then
  ssh-keygen -R "$ALGO_SERVER_ADDR" >/dev/null 2>&1 || true
  touch deploy/.hostkey-reset-2gb
fi
echo "Updating the server... (this takes a minute or two)"
DASH_HOST="$(echo "${DASH_HOST:-}" | tr -cd 'A-Za-z0-9.-')"
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20 "$ALGO_HOST" "DASH_HOST='$DASH_HOST' bash -s" \
  < deploy/paste-engine-update.txt 2>&1 | tee deploy/last-server-update.log
echo "exit status: ${PIPESTATUS[0]}" | tee -a deploy/last-server-update.log
echo; echo "Finished. You can close this window."
