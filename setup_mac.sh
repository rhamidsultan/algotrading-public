#!/bin/bash
# One-time setup on a Mac (or Linux):  bash setup_mac.sh
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 was not found."
  echo "If a window just offered to install developer tools, accept it, wait for it to finish, and run this again."
  echo "Otherwise install Python from https://www.python.org/downloads/ and run this again."
  exit 1
fi
echo "Using $($PY --version). Installing packages (a few minutes the first time)..."
"$PY" -m venv .venv || exit 1
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt || { echo "Package installation failed."; exit 1; }
ls data/spot/*.csv >/dev/null 2>&1 || .venv/bin/python run.py sample-data
[ -f secrets.yaml ] || { cp secrets.example.yaml secrets.yaml; chmod 600 secrets.yaml; echo "Created secrets.yaml: put your dashboard address and password in it."; }
# Keep a history of every change (each backtest records the code version it ran on).
if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1 && [ ! -d .git ]; then
  git init -q && git add -A && git -c user.name="Algo Lab" -c user.email="algolab@localhost" commit -qm "Algo Lab as installed" \
    && echo "Started a git history in this folder."
fi
echo
echo "Setup finished. Start the dashboard with:  bash start_dashboard.sh"
