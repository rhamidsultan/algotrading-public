"""
IBKR US holdings sync (read-only).

Connects to IB Gateway, pulls account summary, positions and day P&L,
converts to INR, and writes:
  data/ibkr_snapshot.json   - latest snapshot for the dashboard
  data/ibkr.db (SQLite)     - history tables for future trend analysis

Sync window: US market days, 09:00-16:30 New York time
(30 min before open to 30 min after close). Outside it the script idles.

Usage:
  python ibkr_sync.py           # loop forever (systemd service)
  python ibkr_sync.py --once    # one sync now, ignoring the window (testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_async import IB

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

HOST = os.getenv("IB_HOST", "127.0.0.1")
PORT = int(os.getenv("IB_PORT", "4001"))
CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "21"))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
INTERVAL = int(os.getenv("SYNC_INTERVAL_SEC", "300"))
FX_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR"

WINDOW_START = dtime(9, 0)
WINDOW_END = dtime(16, 30)

SUMMARY_TAGS = {
    "NetLiquidation", "TotalCashValue", "GrossPositionValue",
    "UnrealizedPnL", "RealizedPnL", "AvailableFunds",
}

log = logging.getLogger("ibkr_sync")


# ---------- helpers ----------
def in_sync_window(now: datetime | None = None) -> bool:
    """True on weekdays between 09:00 and 16:30 New York time.
    (US market holidays are not excluded; a sync on a holiday is harmless.)"""
    t = (now or datetime.now(timezone.utc)).astimezone(NY)
    return t.weekday() < 5 and WINDOW_START <= t.time() <= WINDOW_END


def clean(x):
    """IBKR uses NaN / huge sentinels for 'no value'."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or abs(f) > 1e300:
        return None
    return round(f, 4)


_fx_cache: dict = {"rate": None, "at": 0.0}
# Several free sources, tried in order. Each needs a normal User-Agent: some refuse Python's default (HTTP 403).
FX_SOURCES = [
    (FX_URL, lambda j: j["rates"]["INR"]),                                        # ECB via frankfurter
    ("https://open.er-api.com/v6/latest/USD", lambda j: j["rates"]["INR"]),
    ("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json", lambda j: j["usd"]["inr"]),
]
FX_FILE = DATA_DIR / "usd_inr.json"   # last good rate, so a restart never starts without one


def usd_inr() -> float | None:
    """USD->INR reference rate, cached 6 h; the last good rate is kept on disk."""
    if _fx_cache["rate"] is None and FX_FILE.exists():
        try:
            saved = json.loads(FX_FILE.read_text())
            _fx_cache.update(rate=float(saved["rate"]), at=float(saved.get("at", 0)))
        except Exception:  # noqa: BLE001
            pass
    if _fx_cache["rate"] and time.time() - _fx_cache["at"] < 6 * 3600:
        return _fx_cache["rate"]
    errors = []
    for url, pick in FX_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (portfolio-dashboard ibkr-sync)",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                rate = float(pick(json.load(r)))
            if not 40 < rate < 200:
                raise ValueError(f"implausible rate {rate}")
            _fx_cache.update(rate=rate, at=time.time())
            try:
                FX_FILE.write_text(json.dumps({"rate": rate, "at": _fx_cache["at"], "source": url}))
            except OSError:
                pass
            return rate
        except Exception as e:  # try the next source
            errors.append(f"{url.split('/')[2]}: {e}")
    log.warning("FX fetch failed (keeping last rate %s): %s", _fx_cache["rate"], "; ".join(errors))
    return _fx_cache["rate"]
    try:
        with urllib.request.urlopen(FX_URL, timeout=10) as r:
            rate = float(json.load(r)["rates"]["INR"])
        _fx_cache.update(rate=rate, at=time.time())
    except Exception as e:  # keep last known rate
        log.warning("FX fetch failed: %s", e)
    return _fx_cache["rate"]


# ---------- storage ----------
def db_connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA_DIR / "ibkr.db")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS account_history (
        ts TEXT, account TEXT, net_liq REAL, cash REAL, positions_value REAL,
        unrealized_pnl REAL, realized_pnl REAL, daily_pnl REAL, usd_inr REAL
    );
    CREATE TABLE IF NOT EXISTS positions_history (
        ts TEXT, account TEXT, symbol TEXT, con_id INTEGER, exchange TEXT,
        currency TEXT, qty REAL, avg_cost REAL, price REAL, market_value REAL,
        unrealized_pnl REAL, daily_pnl REAL
    );
    CREATE INDEX IF NOT EXISTS ix_pos_ts ON positions_history(ts);
    CREATE INDEX IF NOT EXISTS ix_acc_ts ON account_history(ts);
    """)
    return con


def save(snapshot: dict) -> None:
    con = db_connect()
    ts, acc = snapshot["timestamp_utc"], snapshot["account"]
    s = snapshot["summary_usd"]
    con.execute(
        "INSERT INTO account_history VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, acc, s["net_liquidation"], s["cash"], s["positions_value"],
         s["unrealized_pnl"], s["realized_pnl"], s["daily_pnl"], snapshot["usd_inr"]),
    )
    con.executemany(
        "INSERT INTO positions_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(ts, acc, p["symbol"], p["con_id"], p["exchange"], p["currency"], p["qty"],
          p["avg_cost"], p["price"], p["market_value"], p["unrealized_pnl"], p["daily_pnl"])
         for p in snapshot["positions"]],
    )
    con.commit()
    con.close()

    tmp = DATA_DIR / "ibkr_snapshot.json.tmp"
    tmp.write_text(json.dumps(snapshot, indent=2))
    tmp.replace(DATA_DIR / "ibkr_snapshot.json")  # atomic swap for the dashboard


# ---------- IBKR ----------
def fetch(ib: IB) -> dict:
    accounts = ib.managedAccounts()
    if not accounts:
        raise RuntimeError("No account visible on this gateway session")
    acc = accounts[0]

    summary = {v.tag: v.value for v in ib.accountSummary(acc)
               if v.tag in SUMMARY_TAGS and v.currency in ("USD", "BASE", "")}

    pnl = ib.reqPnL(acc)
    items = ib.portfolio(acc)
    singles = {it.contract.conId: ib.reqPnLSingle(acc, "", it.contract.conId) for it in items}
    ib.sleep(3)  # let P&L subscriptions deliver

    positions = []
    for it in items:
        c = it.contract
        positions.append({
            "symbol": c.localSymbol or c.symbol,
            "con_id": c.conId,
            "sec_type": c.secType,
            "exchange": c.primaryExchange or c.exchange,
            "currency": c.currency,
            "qty": clean(it.position),
            "avg_cost": clean(it.averageCost),
            "price": clean(it.marketPrice),
            "market_value": clean(it.marketValue),
            "unrealized_pnl": clean(it.unrealizedPNL),
            "daily_pnl": clean(singles[c.conId].dailyPnL),
        })

    ib.cancelPnL(acc)
    for con_id in singles:
        ib.cancelPnLSingle(acc, "", con_id)

    rate = usd_inr()
    s = {
        "net_liquidation": clean(summary.get("NetLiquidation")),
        "cash": clean(summary.get("TotalCashValue")),
        "positions_value": clean(summary.get("GrossPositionValue")),
        "unrealized_pnl": clean(summary.get("UnrealizedPnL")),
        "realized_pnl": clean(summary.get("RealizedPnL")),
        "daily_pnl": clean(pnl.dailyPnL),
    }
    now = datetime.now(timezone.utc)
    return {
        "timestamp_utc": now.isoformat(timespec="seconds"),
        "timestamp_ist": now.astimezone(IST).strftime("%d %b %Y %H:%M IST"),
        "account": acc,
        "usd_inr": rate,
        "summary_usd": s,
        "summary_inr": {k: (round(v * rate, 2) if v is not None and rate else None)
                        for k, v in s.items()},
        "positions": sorted(positions, key=lambda p: -(p["market_value"] or 0)),
    }


def sync_once() -> dict:
    ib = IB()
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=20)
        snap = fetch(ib)
        save(snap)
        log.info("Synced %d positions, net liq $%s, day P&L $%s",
                 len(snap["positions"]), snap["summary_usd"]["net_liquidation"],
                 snap["summary_usd"]["daily_pnl"])
        return snap
    finally:
        if ib.isConnected():
            ib.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="sync now, ignore the market window")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.once:
        snap = sync_once()
        print(json.dumps(snap, indent=2))
        return

    did_closing_sync = False
    while True:
        if in_sync_window():
            did_closing_sync = False
            try:
                sync_once()
            except Exception as e:
                log.error("Sync failed (gateway down or awaiting 2FA?): %s", e)
            time.sleep(INTERVAL)
        else:
            if not did_closing_sync:  # one final sync right after the window closes
                try:
                    sync_once()
                except Exception as e:
                    log.error("Closing sync failed: %s", e)
                did_closing_sync = True
            time.sleep(60)


if __name__ == "__main__":
    main()
