#!/bin/bash
# Double-click: turns IBKR back on on the server after stop-ibkr.command - Docker with the IB Gateway
# container, and the IBKR sync service. Then log in again: run ibkr-login on the Mac and approve in IBKR Mobile.
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
HOST="$ALGO_HOST"
chmod 600 "$KEY"
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20 "$HOST" 'bash -s' <<'REMOTE' 2>&1 | tee deploy/last-ibkr-start.log
if command -v docker >/dev/null 2>&1; then
  sudo systemctl start docker >/dev/null 2>&1
  found=0
  while read -r id name image; do
    [ -z "$id" ] && continue
    found=1
    sudo docker update --restart=unless-stopped "$id" >/dev/null
    sudo docker start "$id" >/dev/null && echo "IB Gateway container started: $name ($image)"
  done < <(sudo docker ps -a --format '{{.ID}} {{.Names}} {{.Image}}' | grep -iE 'gateway|ibkr|ibc|ib-|tws')
  [ "$found" = 0 ] && echo "No IB Gateway container found."
fi
if systemctl cat ibkr-sync >/dev/null 2>&1; then
  sudo systemctl enable --now ibkr-sync >/dev/null 2>&1 && echo "IBKR sync: running (and starts at boot)"
fi
free -m | awk 'NR==2{print "Memory in use " $3 " MB of " $2 " MB, available " $7 " MB"}'
echo "Next: log in to IBKR again (run ibkr-login on the Mac and approve in IBKR Mobile)."
REMOTE
echo; echo "Finished. You can close this window."
