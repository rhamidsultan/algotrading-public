#!/bin/bash
# Starts the dashboard and opens it in your browser. Leave this window open; Ctrl+C stops it.
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || { echo "Run setup first:  bash setup_mac.sh"; exit 1; }
(sleep 4; open http://localhost:8501 2>/dev/null || xdg-open http://localhost:8501 2>/dev/null) &
exec .venv/bin/python -m streamlit run dashboard.py --server.port 8501
