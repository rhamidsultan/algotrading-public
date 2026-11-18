#!/bin/bash
# Double-click (while the TrueData trial is active): downloads 6 months of 1-minute NIFTY, India VIX and
# expired NIFTY option candles from TrueData into data/ for real-price backtests.
# Needs your trial login in secrets.yaml:   truedata:  user: "..."  password: "..."
# Safe to stop (close the window) and run again: it carries on where it left off.
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
{
  echo "TrueData download ($(date '+%d %b %Y %H:%M'))"
  echo "== Checking the login"
  if ! "$PY" run.py truedata test; then
    echo; echo "Login failed: check the truedata user and password in secrets.yaml (and that the trial is active)."
    exit 1
  fi
  echo; echo "== Downloading (NIFTY + VIX, contract list, then option candles nearest the money first)"
  "$PY" run.py truedata all
  echo; echo "== Progress"
  "$PY" run.py truedata status
} 2>&1 | tee -a deploy/last-truedata.log
echo; echo "Finished (or stopped). Run this again to continue. You can close this window."
