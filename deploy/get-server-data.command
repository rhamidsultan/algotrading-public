#!/bin/bash
# Double-click to copy the prices the server has recorded into this folder's data/ for backtests on the Mac:
# Index 1-minute sets (data/spot), option prices (data/options), 1-second bars of the last few trading days (data/sec1)
# and NSE closing prices (data/eod).
# Only new or changed files are copied, so running it again later is quick. Nothing on the server changes.
cd "$(dirname "$0")/.." || exit 1
. deploy/server_login.sh                 # which server and key (~/.algo-trading/server.env)
KEY="$ALGO_KEY"
HOST="$ALGO_HOST"
chmod 600 "$KEY"
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20"
{
  echo "Copying the server's price data to data/ ... ($(date '+%d %b %Y %H:%M'))"
  "$PY" run.py sync-data --host "$HOST" --key "$KEY"
  status=$?
  if [ "$status" = 0 ]; then
    mkdir -p data/eod
    rsync -az --rsync-path="sudo -n rsync" -e "$SSH" "$HOST:/opt/algolab/data/eod/" data/eod/ 2>/dev/null \
      && echo "NSE closing prices copied (data/eod)." || echo "(No NSE closing prices on the server to copy.)"
    rsync -az --rsync-path="sudo -n rsync" -e "$SSH" "$HOST:/opt/algolab/data/collect_log.txt" data/ 2>/dev/null || true
  fi
  echo
  echo "Data on this Mac now:"
  du -sh data/spot data/options data/sec1 data/eod 2>/dev/null
  echo "1-second days: $(ls data/sec1 2>/dev/null | tr '\n' ' ')"
  echo "option price files: $(find data/options -name '*.csv' | wc -l | tr -d ' ')"
  ls data/spot
  echo "exit status: $status"
} 2>&1 | tee deploy/last-data-sync.log
echo; echo "Finished. You can close this window."
