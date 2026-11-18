#!/bin/bash
# Double-click: the server downloads 1-minute NIFTY, India VIX and option prices from Zerodha with its
# own login, then they are copied into this folder's data/ for backtests on the Mac.
# Read-only: it places no orders and changes no settings. Takes about 5 minutes.
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
HOST="$ALGO_HOST"
chmod 600 "$KEY"
OPTS="-i $KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20"
{
  echo "Fetching 1-minute data through the server ... ($(date '+%d %b %Y %H:%M'))"
  if ! scp $OPTS deploy/fetch_minute_data.py "$HOST:/tmp/fetch_minute_data.py"; then
    echo "Could not reach the server."; exit 1
  fi
  ssh $OPTS "$HOST" 'cd /opt/algolab && sudo -u "$(stat -c %U run.py)" venv/bin/python /tmp/fetch_minute_data.py'
  status=$?
  if [ "$status" = 0 ]; then
    mkdir -p data/options_1min data/spot
    rsync -az -e "ssh $OPTS" "$HOST:/opt/algolab/data/options_1min/" data/options_1min/
    rsync -az -e "ssh $OPTS" "$HOST:/opt/algolab/data/spot/NIFTY_minute.csv" data/spot/
    echo "Copied to this Mac: data/spot/NIFTY_minute.csv and data/options_1min/ ($(find data/options_1min -name '*.csv' | wc -l | tr -d ' ') option files)"
  fi
  echo "exit status: $status"
} 2>&1 | tee deploy/last-minute-data.log
echo; echo "Finished. You can close this window."
