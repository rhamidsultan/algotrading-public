"""Bring in the daily IBKR snapshot made by the Claude scheduled task (no IB Gateway needed).

The task reads the account through Claude's IBKR connection after the US close and saves
`ibkr_snapshot.json` (the same shape ibkr_sync.py writes) on the repo's `ibkr-snapshot` branch.
deploy/ibkr_snapshot_pull.sh fetches that file and runs:

  python3 snapshot_import.py <snapshot.json>          # DATA dir from IBKR_DATA_DIR

It only takes a snapshot newer than the one the US page already has, so a running ibkr-sync
(live, every minute) always wins. Adds one row to ibkr.db for the charts, fills the USD/INR rate
if the task left it empty, and swaps the page's snapshot file atomically. Standard library only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
FX_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR"
SUMMARY_KEYS = ("net_liquidation", "cash", "positions_value", "unrealized_pnl", "realized_pnl", "daily_pnl")


def _ts(snap: dict) -> datetime:
    return datetime.fromisoformat(snap["timestamp_utc"])


def _rate(data: Path) -> float | None:
    try:
        return float(json.loads((data / "usd_inr.json").read_text())["rate"])
    except Exception:  # noqa: BLE001
        pass
    try:
        req = urllib.request.Request(FX_URL, headers={"User-Agent": "Mozilla/5.0 (ibkr-snapshot-import)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rate = float(json.load(r)["rates"]["INR"])
        return rate if 40 < rate < 200 else None
    except Exception:  # noqa: BLE001
        return None


def check(snap: dict) -> dict:
    """Refuse anything that is not a usable snapshot (raises ValueError)."""
    try:
        _ts(snap)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"bad timestamp_utc: {e}") from None
    s = snap.get("summary_usd")
    if not isinstance(s, dict) or not isinstance(s.get("net_liquidation"), (int, float)):
        raise ValueError("summary_usd.net_liquidation missing")
    if not isinstance(snap.get("positions"), list):
        raise ValueError("positions missing")
    for p in snap["positions"]:
        if not p.get("symbol"):
            raise ValueError("a position has no symbol")
    return snap


def _own_like(path: Path, ref: Path) -> None:
    """Root runs this; keep files owned like the data folder so the sync and the page can use them."""
    try:
        st = ref.stat()
        os.chown(path, st.st_uid, st.st_gid)
        path.chmod(0o664)
    except OSError:
        pass


def import_snapshot(snap: dict, data: Path) -> str:
    check(snap)
    data.mkdir(parents=True, exist_ok=True)
    cur = data / "ibkr_snapshot.json"
    if cur.exists():
        try:
            if _ts(json.loads(cur.read_text())) >= _ts(snap):
                return "skipped: the page already has this or newer data"
        except (OSError, ValueError, KeyError):
            pass                                            # unreadable current file: replace it

    s = {k: snap["summary_usd"].get(k) for k in SUMMARY_KEYS}
    snap["summary_usd"] = s
    if not snap.get("usd_inr"):
        snap["usd_inr"] = _rate(data)
    rate = snap["usd_inr"]
    snap["summary_inr"] = {k: (round(v * rate, 2) if v is not None and rate else None) for k, v in s.items()}
    snap.setdefault("account", "IBKR")
    snap["timestamp_ist"] = _ts(snap).astimezone(IST).strftime("%d %b %Y %H:%M IST")
    snap["source"] = "claude-daily"
    snap["positions"] = sorted(snap["positions"], key=lambda p: -(p.get("market_value") or 0))

    db = data / "ibkr.db"
    new_db = not db.exists()
    con = sqlite3.connect(db, timeout=10)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS account_history (
            ts TEXT, account TEXT, net_liq REAL, cash REAL, positions_value REAL,
            unrealized_pnl REAL, realized_pnl REAL, daily_pnl REAL, usd_inr REAL);
        CREATE TABLE IF NOT EXISTS positions_history (
            ts TEXT, account TEXT, symbol TEXT, con_id INTEGER, exchange TEXT,
            currency TEXT, qty REAL, avg_cost REAL, price REAL, market_value REAL,
            unrealized_pnl REAL, daily_pnl REAL);
        CREATE INDEX IF NOT EXISTS ix_pos_ts ON positions_history(ts);
        CREATE INDEX IF NOT EXISTS ix_acc_ts ON account_history(ts);
        """)
        ts, acc = snap["timestamp_utc"], snap["account"]
        con.execute("INSERT INTO account_history VALUES (?,?,?,?,?,?,?,?,?)",
                    (ts, acc, s["net_liquidation"], s["cash"], s["positions_value"],
                     s["unrealized_pnl"], s["realized_pnl"], s["daily_pnl"], rate))
        con.executemany("INSERT INTO positions_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(ts, acc, p["symbol"], p.get("con_id"), p.get("exchange"), p.get("currency"), p.get("qty"),
                          p.get("avg_cost"), p.get("price"), p.get("market_value"), p.get("unrealized_pnl"),
                          p.get("daily_pnl")) for p in snap["positions"]])
        con.commit()
    finally:
        con.close()
    if new_db:
        _own_like(db, data)

    tmp = data / "ibkr_snapshot.json.tmp"
    tmp.write_text(json.dumps(snap, indent=2))
    _own_like(tmp, data)
    tmp.replace(cur)
    return f"imported {snap['timestamp_ist']}: {len(snap['positions'])} positions, net ${s['net_liquidation']}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    data = Path(os.getenv("IBKR_DATA_DIR", "/opt/trader/data/ibkr"))
    try:
        print(import_snapshot(json.loads(Path(argv[1]).read_text()), data))
        return 0
    except (OSError, ValueError, sqlite3.Error) as e:
        print(f"IBKR snapshot not imported: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
