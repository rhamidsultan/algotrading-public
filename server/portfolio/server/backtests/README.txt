Backtests page for the portfolio dashboard
==========================================
Shows backtests run on the Mac (Algo Lab) on the server dashboard at /backtests.

Install on the server (Lightsail browser SSH): paste the single line from paste-backtests.txt.
It unpacks into ~/backtests and runs:  sudo bash install_backtests.sh
The installer backs up app.py and the pages, registers the page, adds a "Backtests" link,
restarts the dashboard, checks it, and restores everything if a check fails.

Then on the Mac add the dashboard address and sign-in to ~/AI - VS/algo-lab/secrets.yaml and run
    .venv/bin/python run.py publish
(or press "Send to dashboard" under a result in Algo Lab).
