#!/bin/bash
# Double-click: shuts IBKR down on the server to free memory - IB Gateway (Docker), its login browser
# (Chromium) and the IBKR sync service. Nothing is deleted or uninstalled; deploy/start-ibkr.command
# brings it back. Until then the dashboard's US holdings keep their last synced values.
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
HOST="$ALGO_HOST"
chmod 600 "$KEY"
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20 "$HOST" 'bash -s' <<'REMOTE' 2>&1 | tee deploy/last-ibkr-stop.log
mem() { free -m | awk 'NR==2{print "  memory in use " $3 " MB of " $2 " MB, available " $7 " MB"}'; }
echo "Before:"; mem
if systemctl cat ibkr-sync >/dev/null 2>&1; then
  sudo systemctl disable --now ibkr-sync >/dev/null 2>&1 && echo "IBKR sync: stopped (and will not start at boot)"
fi
if command -v docker >/dev/null 2>&1 && systemctl is-active -q docker; then
  found=0
  while read -r id name image; do
    [ -z "$id" ] && continue
    found=1
    sudo docker update --restart=no "$id" >/dev/null
    sudo docker stop "$id" >/dev/null && echo "IB Gateway container stopped: $name ($image)"
  done < <(sudo docker ps -a --format '{{.ID}} {{.Names}} {{.Image}}' | grep -iE 'gateway|ibkr|ibc|ib-|tws')
  [ "$found" = 0 ] && echo "No IB Gateway container found."
  if [ -z "$(sudo docker ps -q)" ]; then
    sudo systemctl stop docker.socket docker containerd >/dev/null 2>&1 && echo "Docker: nothing else was running, so it is stopped too"
  else
    echo "Docker left running for these other containers:"; sudo docker ps --format '  {{.Names}} ({{.Image}})'
  fi
fi
if pgrep -fi chrom >/dev/null; then sudo pkill -fi chrom && echo "IB Gateway login browser (Chromium) closed"; fi
sleep 3; echo "After:"; mem
echo "Trading engine: $(systemctl is-active algolab-paper)   Dashboard: $(systemctl is-active trader-app)"
REMOTE
echo; echo "Finished. You can close this window. (deploy/start-ibkr.command turns IBKR back on.)"
