"""US stocks (IBKR) page for the portfolio dashboard.

Reads what ibkr_sync.py (the read-only IBKR sync on this server) writes:
  <IBKR_DATA_DIR>/ibkr_snapshot.json   latest holdings, cash and P&L
  <IBKR_DATA_DIR>/ibkr.db              every sync, for the charts
Nothing here talks to IBKR or places orders.

Routes (behind the dashboard's sign-in):
  GET /us        the page
  GET /api/us    snapshot + derived figures + chart series
"""
import json
import os
import sqlite3
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, render_template

DATA = Path(os.getenv("IBKR_DATA_DIR", "/opt/trader/data/ibkr"))
NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
STALE_MIN = 5  # sync runs every minute inside the window

panel = Blueprint("ibkr_panel", __name__)


def _in_window(now):
    t = now.astimezone(NY)
    return t.weekday() < 5 and dtime(9, 0) <= t.time() <= dtime(16, 30)


def _ny_day(ts):
    return datetime.fromisoformat(ts).astimezone(NY).date().isoformat()


def _series():
    """Per-day last values and today's intraday day P&L, from the history DB."""
    db = DATA / "ibkr.db"
    if not db.exists():
        return [], []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT ts, net_liq, daily_pnl, usd_inr FROM account_history "
            "WHERE ts >= datetime('now', '-400 days') ORDER BY ts").fetchall()
    finally:
        con.close()
    by_day = {}
    for ts, net_liq, daily, fx in rows:
        by_day[_ny_day(ts)] = {"day": _ny_day(ts), "net_liq": net_liq, "daily_pnl": daily, "usd_inr": fx}
    days = list(by_day.values())
    today = days[-1]["day"] if days else None
    intraday = [{"t": datetime.fromisoformat(ts).astimezone(IST).strftime("%H:%M"), "daily_pnl": d, "usd_inr": fx}
                for ts, _, d, fx in rows if today and _ny_day(ts) == today and d is not None]
    return days, intraday


@panel.route("/us")
def us_page():
    return render_template("us.html")


@panel.route("/api/us")
def api_us():
    snap_path = DATA / "ibkr_snapshot.json"
    if not snap_path.exists():
        return jsonify(ok=False, error="No IBKR data yet. Is the ibkr-sync service running?")
    try:
        snap = json.loads(snap_path.read_text())
    except (OSError, ValueError) as e:
        return jsonify(ok=False, error=f"Could not read the IBKR snapshot: {e}")

    now = datetime.now(timezone.utc)
    age_min = (now - datetime.fromisoformat(snap["timestamp_utc"])).total_seconds() / 60
    in_window = _in_window(now)
    positions = snap.get("positions", [])
    invested = sum((p.get("qty") or 0) * (p.get("avg_cost") or 0) for p in positions)
    value = sum(p.get("market_value") or 0 for p in positions)
    try:
        days, intraday = _series()
    except sqlite3.Error as e:
        days, intraday = [], []
        print("IBKR page: history not read:", e, flush=True)

    return jsonify(
        ok=True,
        updated_ist=snap.get("timestamp_ist"),
        age_min=round(age_min, 1),
        in_window=in_window,
        stale=in_window and age_min > STALE_MIN,
        usd_inr=snap.get("usd_inr"),
        summary=snap.get("summary_usd", {}),
        invested=round(invested, 2),
        value=round(value, 2),
        positions=positions,
        days=days,
        intraday=intraday,
    )
