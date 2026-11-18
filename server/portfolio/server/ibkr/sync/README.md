# IBKR US holdings sync — Phase 1 setup

Runs IB Gateway headless on your Lightsail server and syncs your US holdings
(read-only) into `data/ibkr_snapshot.json` and `data/ibkr.db` every minute
during US market hours (09:00–16:30 New York = 18:30–02:00 IST).

**Safety:** the gateway runs with `READ_ONLY_API=yes` and the API port is bound
to `127.0.0.1`, so nothing on the internet can reach it and no order can be placed.

---

## 0. Check the server has room (from Mac Terminal)
```bash
ssh ubuntu@<your-lightsail-ip>
free -h          # IB Gateway needs ~1 GB RAM. If total < 2 GB, upgrade the instance first.
```

## 1. Install Docker (once)
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && exit     # log out and back in
```

## 2. Copy these files to the server (from Mac Terminal, in the folder with the files)
```bash
ssh ubuntu@<ip> "mkdir -p ~/ibkr"
scp docker-compose.yml .env.example ibkr_sync.py requirements.txt ibkr-sync.service ubuntu@<ip>:~/ibkr/
```

## 3. Add your login
```bash
ssh ubuntu@<ip>
cd ~/ibkr && cp .env.example .env && nano .env    # fill TWS_USERID / TWS_PASSWORD
chmod 600 .env
mkdir -p tws_settings data
```

## 4. Start the gateway and approve 2FA
```bash
docker compose up -d
docker compose logs -f        # watch for the login
```
Your iPhone's **IBKR Mobile** app will get a login notification — approve it.
When logs show the login completed, press Ctrl+C.

## 5. Test one sync
```bash
sudo apt install -y python3-venv
python3 -m venv venv && venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
venv/bin/python ibkr_sync.py --once
```
You should see your holdings printed as JSON.

## 6. Run it permanently
```bash
sudo cp ibkr-sync.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ibkr-sync
journalctl -u ibkr-sync -f
```

---

## Day-to-day
- **2FA:** the gateway soft-restarts nightly without 2FA, but IBKR forces a full
  re-login about once a week. You'll get an IBKR Mobile prompt — approve it.
  Until you do, syncs log "awaiting 2FA" and the dashboard keeps the last snapshot.
- **Logs:** `docker compose logs --tail 50` (gateway), `journalctl -u ibkr-sync` (sync).
- **Restart gateway:** `docker compose restart`

## What the dashboard reads
`data/ibkr_snapshot.json`:
- `summary_usd` / `summary_inr`: net liquidation, cash, positions value, unrealized/realized P&L, **daily_pnl**
- `positions[]`: symbol, qty, avg_cost, price, market_value, unrealized_pnl, daily_pnl
- `usd_inr`, `timestamp_ist`

`data/ibkr.db` keeps every sync in `account_history` and `positions_history` for trend analysis later.

## Later phases
Phase 3 (paper trading) adds a second gateway in paper mode on port 4002 — no
change to this read-only setup.
