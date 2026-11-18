#!/bin/bash
# Double-click on the TrueData trial's last day: tops up everything TrueData has that nothing else will give
# after the trial, for NIFTY, Bank Nifty and Sensex:
#   1. index, VIX and near-month future, last 40 days (to today)
#   2. option candles (1-minute, with OI) for contracts that expired since the last run AND the contracts
#      still open, their days so far (Zerodha never gives expired contracts)
#   3. tick history with best bid/ask, last 5 trading days, near the money (TrueData keeps only 5 days)
#   4. NIFTY 50 stocks and their futures, the same 40 days
# Safe to stop (close the window) and run again: it carries on where it left off.
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
FROM=$(date -v-40d +%F 2>/dev/null || date -d '40 days ago' +%F)
{
  echo "TrueData last-day download ($(date '+%d %b %Y %H:%M')), from $FROM"
  echo "== Checking the login"
  if ! "$PY" run.py truedata test; then
    echo; echo "Login failed: check the truedata user and password in secrets.yaml (and that the trial is active)."
    exit 1
  fi
  for U in NIFTY BANKNIFTY SENSEX; do
    echo; echo "== $U: index, future and option candles (expired + open contracts)"
    "$PY" run.py truedata all --open --underlying "$U" --from "$FROM"
    echo; echo "== $U: ticks with bid/ask (last 5 trading days)"
    "$PY" run.py truedata ticks --underlying "$U" --from "$FROM"
  done
  echo; echo "== NIFTY 50 stocks and futures"
  "$PY" run.py truedata stocks --from "$FROM"
  echo; echo "== Progress"
  "$PY" run.py truedata status
} 2>&1 | tee -a deploy/last-truedata.log
echo; echo "Finished (or stopped). Run this again to continue. You can close this window."
