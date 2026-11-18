"""Zerodha portfolio dashboard: live holdings, P&L, history and alerts.

Run:   python app.py          (real account, needs config.json)
       python app.py --demo   (fake data, no Zerodha login needed)

Runs on a laptop or on a cloud server. On a server the Kite keys can come from a
.env file beside this script (KITE_API_KEY, KITE_API_SECRET), and the daily token
can be shared with the separate login page through "token_file" in config.json.

Read-only: this app never places, modifies or cancels orders.
"""
import argparse
import csv
import hmac
import io
import json
import os
import re
import random
import secrets
import smtplib
import socket
import sqlite3
import struct
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from markupsafe import escape

BASE = Path(__file__).parent
IST = ZoneInfo("Asia/Kolkata")
DB_PATH = BASE / "portfolio.db"
TOKEN_PATH = BASE / "token.json"
CONFIG_PATH = BASE / "config.json"
SECRET_PATH = BASE / ".secret_key"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # tradebook uploads
lock = threading.Lock()
STATE = {"holdings": [], "summary": {}, "updated": None, "error": None, "logged_in": False}
CFG = {}
DEMO = False
kite = None
_last_snapshot = 0.0
FAILS = {}  # ip -> (failed attempts, window start)
RAW = []  # last holdings list from Kite
apply_lock = threading.Lock()
tick_cond = threading.Condition()  # wakes browser streams on every price update
tick_seq = [0]
# Live tick stream (needs the paid Kite Connect plan). Falls back to polling when unavailable.
STREAM = {"status": "off", "detail": "", "ws": None, "ltp": {}, "last_tick": 0.0}


# ---------------------------------------------------------------- helpers
def now_ist():
    return datetime.now(IST)


_HOLIDAYS = {"mtime": None, "days": set()}


def nse_holidays():
    """NSE weekday holidays from the Algo Lab engine's config.yaml (market: holidays), re-read when it changes."""
    path = Path(os.environ.get("ALGOLAB_DIR", "/opt/algolab")) / "config.yaml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _HOLIDAYS["days"]
    if mtime != _HOLIDAYS["mtime"]:
        text = path.read_text(encoding="utf-8", errors="replace")
        block = re.search(r"^\s*holidays:.*?\n((?:\s*-\s*[\"']?\d{4}-\d{2}-\d{2}[\"']?.*\n?)*)", text, re.M)
        _HOLIDAYS["days"] = set(re.findall(r"\d{4}-\d{2}-\d{2}", block.group(1))) if block else set()
        _HOLIDAYS["mtime"] = mtime
    return _HOLIDAYS["days"]


def shipped_holidays():
    """The holiday list that comes with the engine's code (algolab/nse_holidays.txt)."""
    path = Path(os.environ.get("ALGOLAB_DIR", "/opt/algolab")) / "algolab" / "nse_holidays.txt"
    try:
        return set(re.findall(r"^\s*(\d{4}-\d{2}-\d{2})", path.read_text(encoding="utf-8"), re.M))
    except OSError:
        return set()


def trading_day(d=None):
    """Weekday and not an NSE holiday."""
    d = d or now_ist().date()
    return d.weekday() < 5 and d.isoformat() not in (nse_holidays() | shipped_holidays())


def login_due(t=None):
    """Today's Zerodha login is worth asking for: a trading day, before the after-close recording is done."""
    t = t or now_ist()
    return trading_day(t.date()) and t.time() <= dtime(16, 0)


def market_open(t=None):
    t = t or now_ist()
    return trading_day(t.date()) and dtime(9, 15) <= t.time() <= dtime(15, 30)


def sync_window(t=None):
    """When the app talks to Zerodha: trading days, 30 minutes before the open to 30 minutes after the close."""
    t = t or now_ist()
    return trading_day(t.date()) and dtime(8, 45) <= t.time() <= dtime(16, 0)


PAUSED = {"on": False, "since": ""}  # manual pause, kept across restarts in the settings table


def sync_on():
    """True when the app may talk to Zerodha: inside the sync window and not paused by hand."""
    return not PAUSED["on"] and (DEMO or sync_window())


def market_phase():
    return "open" if market_open() else "sync" if sync_window() else "closed"


def load_settings():
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key='sync_paused'").fetchone()
    if row and row["value"]:
        PAUSED.update(on=True, since=row["value"])


def set_paused(on):
    since = now_ist().strftime("%d %b %Y, %H:%M IST") if on else ""
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES('sync_paused', ?)", (since,))
    PAUSED.update(on=bool(on), since=since)
    if on and STREAM["ws"]:
        try:
            STREAM["ws"].close()
        except Exception:  # noqa: BLE001
            pass


def seconds_to_sync(t=None):
    """Seconds until the next sync window opens (0 while inside one)."""
    t = t or now_ist()
    if sync_window(t):
        return 0
    nxt = t.replace(hour=8, minute=45, second=0, microsecond=0)
    if t >= nxt:
        nxt += timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return int((nxt - t).total_seconds())


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots(
                ts TEXT PRIMARY KEY, day TEXT, value REAL, invested REAL,
                pnl REAL, day_pnl REAL, gold_value REAL);
            CREATE TABLE IF NOT EXISTS holding_snapshots(
                day TEXT, symbol TEXT, qty REAL, avg_price REAL, last_price REAL,
                value REAL, pnl REAL, PRIMARY KEY(day, symbol));
            CREATE TABLE IF NOT EXISTS alerts(
                day TEXT, key TEXT, ts TEXT, message TEXT, PRIMARY KEY(day, key));
            CREATE TABLE IF NOT EXISTS trades(
                day TEXT, ts TEXT, symbol TEXT, isin TEXT, exchange TEXT, side TEXT, qty REAL, price REAL,
                trade_id TEXT, order_id TEXT, source TEXT, PRIMARY KEY(day, exchange, trade_id));
            CREATE TABLE IF NOT EXISTS prices(
                symbol TEXT, day TEXT, close REAL, PRIMARY KEY(symbol, day));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS pnl_extremes(
                scope TEXT, metric TEXT, high REAL, high_ts TEXT, low REAL, low_ts TEXT,
                PRIMARY KEY(scope, metric));
            """
        )
        have = {r["name"] for r in con.execute("PRAGMA table_info(holding_snapshots)")}
        for col in ("day_pnl", "close_price", "day_high", "day_low"):
            if col not in have:
                con.execute(f"ALTER TABLE holding_snapshots ADD COLUMN {col} REAL")
        have = {r["name"] for r in con.execute("PRAGMA table_info(prices)")}
        for col in ("high", "low"):
            if col not in have:
                con.execute(f"ALTER TABLE prices ADD COLUMN {col} REAL")


def is_gold(symbol):
    if symbol in CFG.get("non_gold_symbols", []):
        return False
    return symbol in CFG.get("gold_symbols", []) or "GOLD" in symbol


# ---------------------------------------------------------------- data
def fetch_holdings():
    """Return raw holdings in Kite's format (real or demo)."""
    if DEMO:
        return demo_holdings()
    return kite.holdings()


_demo = None


def demo_holdings():
    global _demo
    if _demo is None:
        base = [
            ("RELIANCE", 40, 1310.0, 1402.5), ("HDFCBANK", 60, 1580.0, 1721.0),
            ("INFY", 35, 1650.0, 1548.2), ("TCS", 12, 3900.0, 4105.0),
            ("ITC", 200, 410.0, 438.6), ("LT", 15, 3350.0, 3612.0),
            ("GOLDBEES", 1500, 68.0, 96.4), ("SBIN", 90, 720.0, 812.3),
        ]
        _demo = [
            {"tradingsymbol": s, "exchange": "NSE", "quantity": q, "t1_quantity": 0,
             "average_price": a, "close_price": c, "last_price": c}
            for s, q, a, c in base
        ]
    for h in _demo:
        h["last_price"] = round(h["last_price"] * (1 + random.uniform(-0.004, 0.0045)), 2)
    return [dict(h) for h in _demo]


def build(raw):
    rows = []
    for h in raw:
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0) + (h.get("collateral_quantity") or 0)
        if qty <= 0:
            continue
        avg, last = h.get("average_price") or 0, h.get("last_price") or 0
        close = h.get("close_price") or last
        invested, value = avg * qty, last * qty
        rows.append({
            "symbol": h["tradingsymbol"], "exchange": h.get("exchange", ""),
            "asset": "Gold" if is_gold(h["tradingsymbol"]) else "Equity",
            "qty": qty, "avg_price": avg, "last_price": last, "close_price": close,
            "invested": invested, "value": value, "pnl": value - invested,
            "pnl_pct": (value - invested) / invested * 100 if invested else 0,
            "day_pnl": (last - close) * qty,
            "day_pct": (last - close) / close * 100 if close else 0,
        })
    rows.sort(key=lambda r: -r["value"])
    value = sum(r["value"] for r in rows)
    invested = sum(r["invested"] for r in rows)
    day_pnl = sum(r["day_pnl"] for r in rows)
    gold = sum(r["value"] for r in rows if r["asset"] == "Gold")
    prev = value - day_pnl
    for r in rows:
        r["weight"] = r["value"] / value * 100 if value else 0
    summary = {
        "value": value, "invested": invested, "pnl": value - invested,
        "pnl_pct": (value - invested) / invested * 100 if invested else 0,
        "day_pnl": day_pnl, "day_pct": day_pnl / prev * 100 if prev else 0,
        "gold_value": gold, "gold_pct": gold / value * 100 if value else 0,
        "count": len(rows),
    }
    return rows, summary


def save_snapshot(rows, s):
    t = now_ist()
    day = t.strftime("%Y-%m-%d")
    with db() as con:
        con.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?)",
                    (t.strftime("%Y-%m-%d %H:%M:%S"), day, s["value"], s["invested"],
                     s["pnl"], s["day_pnl"], s["gold_value"]))
        con.executemany(
            "INSERT INTO holding_snapshots"
            "(day, symbol, qty, avg_price, last_price, value, pnl, day_pnl, close_price, day_high, day_low) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day, symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price, "
            "last_price=excluded.last_price, value=excluded.value, pnl=excluded.pnl, day_pnl=excluded.day_pnl, "
            "close_price=excluded.close_price, "
            "day_high=MAX(COALESCE(day_high, excluded.last_price), excluded.last_price), "
            "day_low=MIN(COALESCE(day_low, excluded.last_price), excluded.last_price)",
            [(day, r["symbol"], r["qty"], r["avg_price"], r["last_price"], r["value"], r["pnl"],
              r["day_pnl"], r["close_price"], r["last_price"], r["last_price"]) for r in rows])


def refresh():
    """Poll holdings (quantities, averages, prices) from Kite."""
    global RAW
    RAW = fetch_holdings()
    apply_prices()


def apply_prices():
    """Rebuild the portfolio from the last holdings plus any newer streamed prices."""
    streaming = time.time() - STREAM["last_tick"] < 120
    raw = [dict(h, last_price=STREAM["ltp"].get(h.get("instrument_token"), h.get("last_price")))
           if streaming else h for h in RAW]
    with apply_lock:
        _apply(raw)


def _apply(raw):
    global _last_snapshot
    rows, summary = build(raw)
    with lock:
        STATE.update(holdings=rows, summary=summary, error=None,
                     updated=now_ist().strftime("%d %b %Y, %H:%M:%S IST"))
    with tick_cond:
        tick_seq[0] += 1
        tick_cond.notify_all()
    live = market_open() or DEMO
    first_today = not _has_snapshot_today()
    every = CFG.get("snapshot_minutes", 1) * 60
    if rows and ((live and time.time() - _last_snapshot >= every) or first_today
                 or _needs_closing_snapshot()):
        save_snapshot(rows, summary)
        _last_snapshot = time.time()
    if live and rows and time.time() - _last_checks[0] >= 2:
        _last_checks[0] = time.time()
        update_extremes(summary)
        check_alerts(rows, summary)


_last_checks = [0.0]


# ---------------------------------------------------------------- live stream
def parse_ticks(buf):
    """Decode a Kite WebSocket binary message into {instrument_token: last_price}."""
    out = {}
    if len(buf) < 4:
        return out  # 1-byte heartbeat
    count, i = struct.unpack(">H", buf[:2])[0], 2
    for _ in range(count):
        size = struct.unpack(">H", buf[i:i + 2])[0]
        packet, i = buf[i + 2:i + 2 + size], i + 2 + size
        if size >= 8:
            token, price = struct.unpack(">II", packet[:8])
            segment = token & 0xFF  # currency segments use different price scales
            out[token] = price / (10_000_000.0 if segment == 3 else 10_000.0 if segment == 6 else 100.0)
    return out


def set_stream(status, detail=""):
    if (status, detail) != (STREAM["status"], STREAM["detail"]):
        print(f"[live stream] {status}: {detail}", flush=True)
    STREAM["status"], STREAM["detail"] = status, detail


def stream_loop():
    try:
        import certifi  # trusted certificates; Mac Python often has none of its own
        import websocket  # websocket-client
    except ImportError:
        set_stream("unavailable", "The websocket-client library is missing. Stop the app, run "
                                  "'pip3 install -r requirements.txt', then start it again.")
        return

    while True:
        if DEMO or not CFG.get("live_stream", True):
            set_stream("off", "Live stream switched off in config.json")
            time.sleep(30)
            continue
        if not (STATE["logged_in"] and RAW and market_open()) or PAUSED["on"]:
            set_stream("idle", "Sync paused by you" if PAUSED["on"] else "Starts during market hours once logged in")
            time.sleep(5)
            continue
        url = f"{CFG.get('ws_url', 'wss://ws.kite.trade')}?api_key={CFG['api_key']}&access_token={kite.access_token}"
        try:
            ws = websocket.create_connection(url, timeout=30, sslopt={"ca_certs": certifi.where()})
        except websocket.WebSocketBadStatusException as e:
            code = getattr(e, "status_code", 0)
            if code in (401, 403):
                set_stream("unavailable", f"Zerodha refused live data (HTTP {code}). This API key needs the "
                                          "Kite Connect plan; using the 60-second refresh and retrying every minute.")
                time.sleep(60)
            else:
                set_stream("retrying", f"Stream refused (HTTP {code}); retrying")
                time.sleep(30)
            continue
        except Exception as e:  # noqa: BLE001
            set_stream("retrying", f"Cannot connect: {e}")
            time.sleep(15)
            continue

        STREAM["ws"], subscribed, got_tick = ws, set(), False
        set_stream("connecting", "Connected, waiting for prices")
        try:
            while market_open() and not PAUSED["on"]:
                want = {h["instrument_token"] for h in RAW if h.get("instrument_token")}
                if want != subscribed:
                    if subscribed - want:
                        ws.send(json.dumps({"a": "unsubscribe", "v": sorted(subscribed - want)}))
                    if want - subscribed:
                        ws.send(json.dumps({"a": "subscribe", "v": sorted(want - subscribed)}))
                        ws.send(json.dumps({"a": "mode", "v": ["ltp", sorted(want - subscribed)]}))
                    subscribed = want
                msg = ws.recv()
                if isinstance(msg, bytes):
                    ticks = parse_ticks(msg)
                    if ticks:
                        STREAM["ltp"].update(ticks)
                        STREAM["last_tick"] = time.time()
                        if not got_tick:
                            got_tick = True
                            set_stream("live", "Prices stream tick by tick")
                        apply_prices()
                elif msg:
                    try:
                        m = json.loads(msg)
                    except ValueError:
                        continue
                    if m.get("type") == "error":
                        set_stream("unavailable" if not got_tick else "retrying", str(m.get("data")))
        except Exception as e:  # noqa: BLE001 - socket dropped or closed for a new login
            if STREAM["status"] in ("live", "connecting"):
                set_stream("retrying", f"Stream dropped ({type(e).__name__}); reconnecting")
        finally:
            STREAM["ws"] = None
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if STREAM["status"] == "unavailable":
            time.sleep(60)
        else:
            time.sleep(3)


# Day P&L high/low is tracked per trading day; total P&L high/low across all days ("ALL").
EXTREMES = (("day", "today", "day_pnl"), ("total", "ALL", "pnl"))


def update_extremes(s):
    t = now_ist()
    ts, day = t.strftime("%Y-%m-%d %H:%M:%S"), t.strftime("%Y-%m-%d")
    with db() as con:
        for _, scope, metric in EXTREMES:
            scope = day if scope == "today" else scope
            v = s[metric]
            row = con.execute("SELECT high, low FROM pnl_extremes WHERE scope=? AND metric=?",
                              (scope, metric)).fetchone()
            if row is None:
                con.execute("INSERT INTO pnl_extremes VALUES(?,?,?,?,?,?)", (scope, metric, v, ts, v, ts))
                continue
            if v > row["high"]:
                con.execute("UPDATE pnl_extremes SET high=?, high_ts=? WHERE scope=? AND metric=?",
                            (v, ts, scope, metric))
            if v < row["low"]:
                con.execute("UPDATE pnl_extremes SET low=?, low_ts=? WHERE scope=? AND metric=?",
                            (v, ts, scope, metric))


def extremes_payload(con, s, day=None):
    """High/low for each tracked P&L plus how far the current figure is from each.

    Percentages are of current portfolio value: a percentage of P&L itself swings wildly
    (or divides by zero) whenever P&L is near zero.
    """
    out = {}
    if not s:
        return out
    day = day or now_ist().strftime("%Y-%m-%d")
    for key, scope, metric in EXTREMES:
        scope = day if scope == "today" else scope
        row = con.execute("SELECT * FROM pnl_extremes WHERE scope=? AND metric=?", (scope, metric)).fetchone()
        if row is None:
            continue
        cur, value = s[metric], s["value"] or 1
        out[key] = {
            "high": row["high"], "high_ts": row["high_ts"], "low": row["low"], "low_ts": row["low_ts"],
            "current": cur, "from_high": cur - row["high"], "from_low": cur - row["low"],
            "from_high_pct": (cur - row["high"]) / value * 100,
            "from_low_pct": (cur - row["low"]) / value * 100,
        }
    return out


def _has_snapshot_today():
    with db() as con:
        return con.execute("SELECT 1 FROM snapshots WHERE day=? LIMIT 1",
                           (now_ist().strftime("%Y-%m-%d"),)).fetchone() is not None


CLOSE_SNAPSHOT = dtime(15, 35)  # a few minutes after the close, once prices settle


def _needs_closing_snapshot():
    """True once per trading day: after the close, if the last saved snapshot is from before it."""
    t = now_ist()
    if DEMO or t.weekday() >= 5 or not (CLOSE_SNAPSHOT <= t.time() <= dtime(18, 0)):
        return False
    day = t.strftime("%Y-%m-%d")
    with db() as con:
        last = con.execute("SELECT MAX(ts) FROM snapshots WHERE day=?", (day,)).fetchone()[0]
    return bool(last) and last < f"{day} {CLOSE_SNAPSHOT.strftime('%H:%M:%S')}"


# ---------------------------------------------------------------- Kite token
_token_seen = [None]  # modified-time of the token file we last tried


def token_stale(mtime):
    """Zerodha clears tokens around 6 am IST, so an older one is expired; no need to ask Zerodha."""
    t = now_ist()
    cutoff = t.replace(hour=6, minute=0, second=0, microsecond=0)
    if t < cutoff:
        cutoff -= timedelta(days=1)
    return mtime < cutoff.timestamp()


def write_token(access_token):
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_name(TOKEN_PATH.name + ".tmp")
    tmp.write_text(json.dumps({"access_token": access_token, "created": now_ist().isoformat()}))
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_PATH)


def adopt_token_file():
    """Use the saved Kite token if it is new and still valid.

    The file is written by this app's own login, or on a server by the separate
    morning login page, so one Zerodha login serves both.
    """
    try:
        mtime = TOKEN_PATH.stat().st_mtime
        if mtime == _token_seen[0]:
            return False
        if token_stale(mtime):
            _token_seen[0] = mtime
            return False
        token = json.loads(TOKEN_PATH.read_text())["access_token"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    try:
        kite.set_access_token(token)
        kite.profile()  # raises if the token has expired
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "TokenException":
            _token_seen[0] = mtime  # expired: wait for a newer file
        return False
    _token_seen[0] = mtime
    logged_in_now()
    return True


def logged_in_now():
    with lock:
        STATE["logged_in"], STATE["error"] = True, None
    if STREAM["ws"]:  # reconnect the live stream with the new token
        try:
            STREAM["ws"].close()
        except Exception:  # noqa: BLE001
            pass


def finish_login(request_token):
    """Exchange Zerodha's request_token for the day's access token. Returns an error or None."""
    if not request_token:
        return "Zerodha did not return a request_token."
    try:
        data = kite.generate_session(request_token, api_secret=CFG["api_secret"])
    except Exception as e:  # noqa: BLE001
        return str(e)
    kite.set_access_token(data["access_token"])
    write_token(data["access_token"])
    try:
        _token_seen[0] = TOKEN_PATH.stat().st_mtime
    except OSError:
        pass
    logged_in_now()
    try:
        refresh()
    except Exception as e:  # noqa: BLE001
        with lock:
            STATE["error"] = str(e)
    return None


def poll_loop():
    loaded = False  # one fetch is allowed outside the sync window so the page is never blank
    while True:
        if not DEMO and STATE["logged_in"] and _token_seen[0] and token_stale(_token_seen[0]):
            with lock:
                STATE["logged_in"] = False  # past 6 am: today's login is needed
        if not DEMO and not STATE["logged_in"]:
            adopt_token_file()
        if not PAUSED["on"] and (DEMO or (STATE["logged_in"] and (sync_window() or not loaded))):
            try:
                refresh()
                loaded = True
                sync_extras()
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                name = type(e).__name__
                with lock:
                    STATE["error"] = f"{name}: {e}"
                    if name == "TokenException":
                        STATE["logged_in"] = False
                print("Refresh failed:", name, e)
        fast = CFG.get("poll_seconds", 60) if not DEMO else 5
        if PAUSED["on"]:
            time.sleep(5)  # paused by hand: no requests, but notice a resume quickly
        elif not DEMO and not STATE["logged_in"]:
            time.sleep(10)  # waiting for the morning login
        else:
            time.sleep(fast if (market_open() or DEMO) else 300)


# ---------------------------------------------------------------- trades and per-stock history
TRADE_COLS = ("day", "ts", "symbol", "isin", "exchange", "side", "qty", "price", "trade_id", "order_id")
PRICE_NOTE = [""]  # why Zerodha's daily prices are not being used, if they are not
_extras = {"trades": 0.0, "prices": 0.0}


def norm_id(x):
    return str(x if x is not None else "").strip().lstrip("0") or "0"


def store_trades(rows, source):
    """Save trades, skipping any already stored. Returns how many were new."""
    new = 0
    with db() as con:
        for r in rows:
            cur = con.execute(
                "INSERT OR IGNORE INTO trades(day, ts, symbol, isin, exchange, side, qty, price, trade_id, order_id, source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(r[c] for c in TRADE_COLS) + (source,))
            new += cur.rowcount
    return new


def parse_tradebook(name, data):
    """Read a Zerodha Console tradebook (Excel or CSV) into trade rows."""
    if name.lower().endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Excel files need the openpyxl package. Download the tradebook as CSV instead.")
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
        table = [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        table = list(csv.reader(io.StringIO(data.decode("utf-8-sig", "replace"))))
    key = lambda c: str(c if c is not None else "").strip().lower().replace(" ", "_")  # noqa: E731
    head = next((i for i, r in enumerate(table) if {"symbol", "trade_date", "trade_type"} <= {key(c) for c in r}), None)
    if head is None:
        raise ValueError("This does not look like a Zerodha tradebook (no Symbol / Trade Date header).")
    cols = [key(c) for c in table[head]]
    out = []
    for r in table[head + 1:]:
        d = dict(zip(cols, r))
        side = str(d.get("trade_type") or "").strip().lower()
        if not d.get("symbol") or side not in ("buy", "sell") or d.get("quantity") in (None, ""):
            continue
        day = str(d["trade_date"])[:10]
        out.append({
            "day": day, "ts": str(d.get("order_execution_time") or day).replace("T", " ")[:19],
            "symbol": str(d["symbol"]).strip(), "isin": str(d.get("isin") or "").strip(),
            "exchange": str(d.get("exchange") or "").strip(), "side": side,
            "qty": float(d["quantity"]), "price": float(d["price"]),
            "trade_id": norm_id(d.get("trade_id")), "order_id": str(d.get("order_id") or "").strip()})
    return out


def capture_trades():
    """Zerodha only keeps the day's trades, so store them before they disappear."""
    rows = []
    for t in kite.trades():
        if t.get("exchange") not in ("NSE", "BSE"):
            continue
        when = t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp") or now_ist()
        ts = when.strftime("%Y-%m-%d %H:%M:%S") if hasattr(when, "strftime") else str(when)[:19]
        rows.append({
            "day": ts[:10], "ts": ts, "symbol": t.get("tradingsymbol"), "isin": "",
            "exchange": t.get("exchange"), "side": str(t.get("transaction_type", "")).lower(),
            "qty": float(t.get("quantity") or 0), "price": float(t.get("average_price") or 0),
            "trade_id": norm_id(t.get("trade_id")), "order_id": str(t.get("order_id") or "")})
    return store_trades([r for r in rows if r["side"] in ("buy", "sell") and r["qty"] > 0], "api")


def update_prices():
    """Daily high, low and close for every current holding from Zerodha's historical data API."""
    today = now_ist().date()
    for h in list(RAW):
        sym, token = h.get("tradingsymbol"), h.get("instrument_token")
        if not token or not sym:
            continue
        time.sleep(0.4)  # stay well inside Zerodha's 3 requests a second
        with db() as con:
            first = con.execute("SELECT MIN(day) FROM trades WHERE symbol=?", (sym,)).fetchone()[0]
            last = con.execute("SELECT MAX(day) FROM prices WHERE symbol=?", (sym,)).fetchone()[0]
            if con.execute("SELECT 1 FROM prices WHERE symbol=? AND high IS NULL LIMIT 1", (sym,)).fetchone():
                last = None  # older rows lack high/low: fetch the whole period again
        start = last or first
        if not start:
            continue
        start = max(datetime.strptime(start, "%Y-%m-%d").date(), today - timedelta(days=1900))
        candles = kite.historical_data(token, start, today, "day")
        with db() as con:
            con.executemany("INSERT OR REPLACE INTO prices(symbol, day, close, high, low) VALUES(?,?,?,?,?)",
                            [(sym, c["date"].strftime("%Y-%m-%d"), c["close"], c["high"], c["low"]) for c in candles])
    PRICE_NOTE[0] = ""


def sync_extras(force=False):
    """Trades every 15 minutes and daily prices every 3 hours, inside the sync window only."""
    if DEMO or not STATE["logged_in"] or not (force or sync_on()):
        return
    now = time.time()
    if force or now - _extras["trades"] >= 900:
        _extras["trades"] = now
        try:
            capture_trades()
        except Exception as e:  # noqa: BLE001
            print("Trade capture failed:", type(e).__name__, e, flush=True)
    if force or now - _extras["prices"] >= 3 * 3600:
        _extras["prices"] = now
        try:
            update_prices()
        except Exception as e:  # noqa: BLE001
            PRICE_NOTE[0] = f"Zerodha's daily price history is not available ({type(e).__name__}: {e})."
            print("Price history failed:", type(e).__name__, e, flush=True)


ACTION_FILES = ("corporate_actions.json", "corporate_actions.local.json")  # the second is for your own additions


def load_actions():
    """Corporate actions (splits, bonuses, mergers, demergers, allotments) entered by hand."""
    out = []
    for name in ACTION_FILES:
        path = BASE / name
        if path.exists():
            try:
                out += json.loads(path.read_text(encoding="utf-8"))
            except ValueError as e:
                print(f"Could not read {name}: {e}", flush=True)
    return sorted(out, key=lambda a: a["day"])


def replay():
    """Replay every stored trade and corporate action in date order, keeping first-in-first-out lots.

    Returns {symbol: book}. A book holds the remaining lots, the units and cost after each day something
    happened ("states"), booked profit, money in and out by month, and the actions that touched the stock.
    """
    with db() as con:
        trades = [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY ts, trade_id")]
    books = {}

    def book(s):
        return books.setdefault(s, {"lots": [], "states": [], "realized": 0.0, "oversold": False, "monthly": {},
                                    "actions": [], "factors": [], "trades": []})

    def mark(s, day):
        b = book(s)
        state = (day, sum(lot[0] for lot in b["lots"]), sum(lot[0] * lot[1] for lot in b["lots"]))
        if b["states"] and b["states"][-1][0] == day:
            b["states"][-1] = state
        else:
            b["states"].append(state)

    def cash(s, day, key, amount):
        m = book(s)["monthly"].setdefault(day[:7], {"month": day[:7], "bought": 0.0, "sold": 0.0})
        m[key] += amount

    def buy(s, day, q, p, traded=None):
        if q > 1e-9:
            book(s)["lots"].append([q, p, day])
        cash(s, day, "bought", (q if traded is None else traded) * p)

    def sell(s, day, q, p, traded=None):
        b = book(s)
        if p is not None:
            cash(s, day, "sold", (q if traded is None else traded) * p)
        while q > 1e-9 and b["lots"]:  # oldest units first, as Zerodha does
            lot = b["lots"][0]
            take = min(q, lot[0])
            if p is not None:
                b["realized"] += take * (p - lot[1])
            lot[0] -= take
            q -= take
            if lot[0] <= 1e-9:
                b["lots"].pop(0)
        if q > 1e-9:
            b["oversold"] = True  # sold more than was ever recorded as bought

    def trim(lots):
        """Drop a fractional share from the newest lots (companies pay cash for fractions)."""
        total = sum(lot[0] for lot in lots)
        extra = total - int(total + 1e-9)
        for lot in reversed(lots):
            if extra <= 1e-9:
                break
            cut = min(extra, lot[0])
            lot[0] -= cut
            extra -= cut
        return [lot for lot in lots if lot[0] > 1e-9]

    def receive(s, lots):
        b = book(s)
        b["lots"] = sorted(b["lots"] + lots, key=lambda lot: lot[2])  # received shares keep their purchase dates

    def act(a):
        s, day, kind = a["symbol"], a["day"], a["type"]
        src = book(s)
        total = sum(lot[0] for lot in src["lots"])
        if kind == "split":
            for lot in src["lots"]:
                lot[0], lot[1] = lot[0] * a["mult"], lot[1] / a["mult"]
            src["factors"].append((day, a["mult"]))
        elif kind == "bonus":
            free = int(total * a["num"] / a["den"] + 1e-9)
            if free:
                src["lots"].append([free, 0.0, day])
            src["factors"].append((day, (a["num"] + a["den"]) / a["den"]))
        elif kind == "merge" and total > 0:  # also used for a plain rename (ratio 1)
            if a.get("units"):
                k = a["units"] / total
                moved = [[lot[0] * k, lot[1] / k, lot[2]] for lot in src["lots"]]
            else:
                moved = trim([[lot[0] * a["ratio"], lot[1] / a["ratio"], lot[2]] for lot in src["lots"]])
            src["lots"] = []
            receive(a["to"], moved)
        elif kind == "demerge" and total > 0:
            share = a["cost_pct"] / 100
            new = [[lot[0] * a["ratio"], lot[1] * share / a["ratio"], lot[2]] for lot in src["lots"]]
            for lot in src["lots"]:
                lot[1] *= 1 - share
            receive(a["to"], trim(new))
        elif kind == "acquire":
            buy(s, day, a["units"], a["price"])
        elif kind == "dispose":
            sell(s, day, a["units"], a.get("price"))
        for sym in (s, a.get("to")):
            if sym:
                book(sym)["actions"].append({"day": day, "text": a.get("text", kind), "basis": a.get("basis", "")})
                mark(sym, day)

    # Buys and sells of the same stock on the same day cancel each other first (intraday), as Zerodha
    # treats them; only what is left over changes the holding.
    same_day = {}
    for t in trades:
        t["left"] = t["qty"]
        same_day.setdefault((t["symbol"], t["day"]), []).append(t)
    for (sym, _), group in same_day.items():
        buys = [t for t in group if t["side"] == "buy"]
        for s in (t for t in group if t["side"] == "sell"):
            for bt in buys:
                k = min(s["left"], bt["left"])
                if k > 1e-9:
                    s["left"] -= k
                    bt["left"] -= k
                    book(sym)["realized"] += k * (s["price"] - bt["price"])

    events = [(a["day"], 0, "", a) for a in load_actions()] + [(t["day"], 1, t["ts"], t) for t in trades]
    for day, kind, _, x in sorted(events, key=lambda e: e[:3]):
        if kind == 0:
            act(x)
            continue
        book(x["symbol"])["trades"].append(x)
        (buy if x["side"] == "buy" else sell)(x["symbol"], day, x["left"], x["price"], traded=x["qty"])
        mark(x["symbol"], day)
    return books


def stock_choices(books):
    """Stocks the history page can show: current holdings first (largest first), then the rest."""
    with lock:
        held = [h["symbol"] for h in STATE["holdings"]]
    return ([{"symbol": s, "held": True} for s in held] +
            [{"symbol": s, "held": False} for s in sorted(books) if s not in held])


def stock_payload(symbol):
    """Invested amount, value and P&L of one stock over time, from stored trades and corporate actions."""
    books = replay()
    choices = stock_choices(books)
    if symbol not in [c["symbol"] for c in choices]:
        symbol = choices[0]["symbol"] if choices else ""
    b = books.get(symbol) or {"lots": [], "states": [], "realized": 0.0, "oversold": False, "monthly": {},
                              "actions": [], "factors": [], "trades": []}
    trades = b["trades"]

    px = {}  # day -> [close, high, low, adjusted]; later sources override earlier ones
    for t in trades:  # a day's own trades give a rough close, high and low
        p = px.setdefault(t["day"], [t["price"], t["price"], t["price"], False])
        p[0], p[1], p[2] = t["price"], max(p[1], t["price"]), min(p[2], t["price"])
    history = False
    with db() as con:
        for r in con.execute("SELECT day, last_price, day_high, day_low FROM holding_snapshots WHERE symbol=?", (symbol,)):
            if r["last_price"]:
                c = r["last_price"]
                px[r["day"]] = [c, max(r["day_high"] or c, c), min(r["day_low"] or c, c), False]
        for r in con.execute("SELECT day, close, high, low FROM prices WHERE symbol=?", (symbol,)):
            c = r["close"]  # Zerodha's history is already adjusted for splits and bonuses
            px[r["day"]], history = [c, r["high"] or c, r["low"] or c, True], True
    with lock:
        held = [h for h in STATE["holdings"] if h["symbol"] == symbol]
        logged_in = STATE["logged_in"]
    today = now_ist().strftime("%Y-%m-%d")
    for h in held:
        c = h["last_price"]
        p = px.get(today, [c, c, c, False])
        px[today] = [c, max(p[1], c), min(p[2], c), False]

    def unadjust(day):  # turn an adjusted price back into what it was on the day
        f = 1.0
        for when, mult in b["factors"]:
            if when > day:
                f *= mult
        return f

    states, series, i, qty, invested, last = b["states"], [], 0, 0.0, 0.0, None
    first = states[0][0] if states else None
    for day in sorted(set(px) | {s[0] for s in states}):
        while i < len(states) and states[i][0] <= day:
            _, qty, invested = states[i]
            i += 1
        if first is None or day < first:
            continue
        if day in px:
            c, h, lo, adj = px[day]
            f = unadjust(day) if adj else 1.0
            last = c * f
            c, h, lo = c * f, h * f, lo * f
        elif last is not None:
            c = h = lo = last  # no price that day: carry the last close forward
        else:
            continue
        series.append({"day": day, "qty": qty, "invested": round(invested, 2), "value": round(qty * c, 2),
                       "value_high": round(qty * h, 2), "value_low": round(qty * lo, 2),
                       "pnl": round(qty * c - invested, 2), "pnl_high": round(qty * h - invested, 2),
                       "pnl_low": round(qty * lo - invested, 2)})

    book_qty = sum(lot[0] for lot in b["lots"])
    book_cost = sum(lot[0] * lot[1] for lot in b["lots"])
    summary = {}
    if states:
        value = series[-1]["value"] if series else 0.0
        summary = {"qty": book_qty, "invested": round(book_cost, 2), "value": value, "pnl": round(value - book_cost, 2),
                   "pnl_pct": (value - book_cost) / book_cost * 100 if book_cost else 0,
                   "avg_cost": book_cost / book_qty if book_qty else 0, "realized": round(b["realized"], 2),
                   "first_day": first, "last_price": last}
    holding_qty = sum(h["qty"] for h in held) if held or logged_in and RAW else None
    if held:  # Zerodha's own figures are the authority for what is held now
        h = held[0]
        summary.update(qty=h["qty"], invested=round(h["invested"], 2), value=round(h["value"], 2),
                       pnl=round(h["pnl"], 2), pnl_pct=h["pnl_pct"], avg_cost=h["avg_price"],
                       last_price=h["last_price"], realized=round(b["realized"], 2), first_day=first)
    adds_up = not b["oversold"] and (holding_qty is None or abs(holding_qty - book_qty) < 0.5)
    cost_gap = None
    if held and adds_up and held[0]["invested"]:
        cost_gap = round(book_cost - held[0]["invested"], 2)  # near zero when the history fully explains the holding
    if summary and not adds_up:
        summary["realized"] = None  # booked profit cannot be trusted when the units do not add up
    return {
        "symbol": symbol, "choices": choices, "summary": summary, "series": series,
        "monthly": [b["monthly"][k] for k in sorted(b["monthly"])],
        "recent": [{k: t[k] for k in ("day", "symbol", "side", "qty", "price")} for t in trades[::-1]],
        "trade_count": len(trades), "first_trade": trades[0]["day"] if trades else None,
        "last_trade": trades[-1]["day"] if trades else None,
        "actions": b["actions"][::-1],
        "recon": {"holding_qty": holding_qty, "trade_qty": book_qty, "ok": adds_up, "cost_gap": cost_gap},
        "price_source": ("High, low and close are Zerodha's daily prices, applied to the units held at the end of each day."
                         if history else
                         "High, low and close come from your own trades and this app's tracking, so they are rough and "
                         "days without a trade are flat."),
        "price_note": PRICE_NOTE[0], "logged_in": logged_in or DEMO, "login_due": (not (logged_in or DEMO)) and login_due(),
        "sync_paused": PAUSED["on"],
        "sync_active": sync_on() or DEMO, "next_sync_seconds": 0 if DEMO else seconds_to_sync()}


# ---------------------------------------------------------------- alerts
def fmt_inr(x):
    return f"Rs {x:,.0f}"


def check_alerts(rows, s):
    a = CFG.get("alerts", {})
    found = []  # (key, message)

    move = a.get("holding_day_move_pct")
    if move:
        for r in rows:
            if abs(r["day_pct"]) >= move:
                d = "up" if r["day_pct"] > 0 else "down"
                found.append((f"move:{r['symbol']}:{d}",
                              f"{r['symbol']} is {d} {abs(r['day_pct']):.1f}% today "
                              f"(LTP {r['last_price']:.2f}, day P&L {fmt_inr(r['day_pnl'])})"))

    pmove = a.get("portfolio_day_move_pct")
    if pmove and abs(s["day_pct"]) >= pmove:
        d = "up" if s["day_pct"] > 0 else "down"
        found.append((f"portfolio:{d}",
                      f"Portfolio is {d} {abs(s['day_pct']):.1f}% today "
                      f"({fmt_inr(s['day_pnl'])}); value {fmt_inr(s['value'])}"))

    band = a.get("gold_allocation_band_pct")
    if band and s["value"]:
        lo, hi = band
        if s["gold_pct"] < lo or s["gold_pct"] > hi:
            side = "below" if s["gold_pct"] < lo else "above"
            found.append((f"gold:{side}",
                          f"Gold allocation is {s['gold_pct']:.1f}%, {side} your {lo}-{hi}% band"))

    by_symbol = {r["symbol"]: r for r in rows}
    for lvl in a.get("price_levels", []):
        r = by_symbol.get(lvl.get("symbol"))
        if not r:
            continue
        if lvl.get("above") and r["last_price"] >= lvl["above"]:
            found.append((f"level:{r['symbol']}:above:{lvl['above']}",
                          f"{r['symbol']} crossed above {lvl['above']} (LTP {r['last_price']:.2f})"))
        if lvl.get("below") and r["last_price"] <= lvl["below"]:
            found.append((f"level:{r['symbol']}:below:{lvl['below']}",
                          f"{r['symbol']} fell below {lvl['below']} (LTP {r['last_price']:.2f})"))

    t = now_ist()
    day = t.strftime("%Y-%m-%d")
    new = []
    with db() as con:
        for key, msg in found:  # each alert fires at most once per day
            cur = con.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?)",
                              (day, key, t.strftime("%Y-%m-%d %H:%M:%S"), msg))
            if cur.rowcount:
                new.append(msg)
    if new:
        notify("Portfolio alert" if len(new) == 1 else f"{len(new)} portfolio alerts", "\n".join(new))


def notify(subject, body):
    """Send through every channel that is configured. Returns a status list."""
    print(f"[ALERT] {subject}: {body}")
    out = []
    e = CFG.get("email", {})
    if e.get("enabled"):
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, e["from"], e["to"]
            msg.set_content(body)
            with smtplib.SMTP_SSL(e.get("smtp_host", "smtp.gmail.com"), e.get("smtp_port", 465), timeout=20) as s:
                s.login(e["from"], e["app_password"])
                s.send_message(msg)
            out.append("email sent")
        except Exception as ex:  # noqa: BLE001
            out.append(f"email failed: {ex}")
    t = CFG.get("telegram", {})
    if t.get("enabled"):
        try:
            data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": f"{subject}\n{body}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data, timeout=20)
            out.append("telegram sent")
        except Exception as ex:  # noqa: BLE001
            out.append(f"telegram failed: {ex}")
    for line in out:
        print("  ", line)
    return out or ["no channel enabled in config.json (alert shown on dashboard only)"]


# ---------------------------------------------------------------- sign-in
def setup_auth():
    if SECRET_PATH.exists():
        key = SECRET_PATH.read_text().strip()
    else:
        key = secrets.token_hex(32)
        SECRET_PATH.write_text(key)
    app.secret_key = key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))


@app.before_request
def require_signin():
    if request.endpoint in ("signin", "static") or session.get("user"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="not signed in"), 401
    nxt = request.full_path if request.query_string else request.path
    return redirect(url_for("signin", next=nxt))


@app.route("/signin", methods=["GET", "POST"])
def signin():
    error = None
    if request.method == "POST":
        ip = request.remote_addr
        n, since = FAILS.get(ip, (0, 0.0))
        if time.time() - since > 900:  # 15-minute window
            n, since = 0, time.time()
        if n >= 5:
            error = "Too many wrong attempts. Try again in 15 minutes."
        else:
            u = request.form.get("username", "").encode()
            p = request.form.get("password", "").encode()
            ok_u = hmac.compare_digest(u, CFG["dashboard_user"].encode())
            ok_p = hmac.compare_digest(p, CFG["dashboard_password"].encode())
            if ok_u and ok_p:
                FAILS.pop(ip, None)
                session.clear()
                session["user"] = CFG["dashboard_user"]
                session.permanent = bool(request.form.get("remember"))
                nxt = request.args.get("next", "/")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = "/"
                return redirect(nxt)
            FAILS[ip] = (n + 1, since)
            time.sleep(1)
            error = "Wrong username or password."
    return render_template("signin.html", error=error), (401 if error else 200)


@app.route("/signout")
def signout():
    session.clear()
    return redirect(url_for("signin"))


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("dashboard.html", demo=DEMO)


@app.route("/login")
def login():
    if DEMO:
        return redirect("/")
    return redirect(kite.login_url())


LOGIN_FAILED = ('Login failed: {}<br><br>The token from Zerodha works once and only for a few minutes. '
                '<a href="/login">Log in again</a> or <a href="/">go back</a>.')


@app.route("/callback")
def callback():
    err = finish_login(request.args.get("request_token"))
    if err:
        return LOGIN_FAILED.format(escape(err)), 400
    return redirect("/")


@app.route("/login/paste", methods=["POST"])
def login_paste():
    """For when Zerodha's redirect cannot reach this app (e.g. it runs on a server):
    paste the address shown after the Zerodha login, or just the request_token."""
    if DEMO:
        return redirect("/")
    value = request.form.get("value", "").strip()
    if "request_token" in value:
        value = urllib.parse.parse_qs(urllib.parse.urlparse(value).query).get("request_token", [""])[0]
    err = finish_login(value)
    if err:
        return LOGIN_FAILED.format(escape(err)), 400
    return redirect("/")


def last_session_day(con, t=None):
    """Outside market hours: the last day the market was open with snapshots taken (Friday's on a weekend, the day
    before until 9:15 the next morning, today's after the close). None while the market is open."""
    t = t or now_ist()
    if market_open(t):
        return None
    row = con.execute(f"SELECT MAX(day) AS d FROM snapshots WHERE day <= ? AND substr(ts, 12, 5) >= '09:15' AND {TRADING_DAY}",
                      (t.strftime("%Y-%m-%d"),)).fetchone()
    return row["d"] if row and row["d"] else None


def hold_last_session(con, out, day):
    """Keep a closed day's figures on screen until the next open: the day P&L of the portfolio and of each holding,
    as recorded that day (Zerodha resets the day's change after the close, and on weekends)."""
    last = con.execute("SELECT day_pnl, value FROM snapshots WHERE day=? ORDER BY ts DESC LIMIT 1", (day,)).fetchone()
    if last is None:
        return
    s = dict(out.get("summary") or {})
    if s:
        s["day_pnl"] = last["day_pnl"] or 0
        prev = (last["value"] or 0) - s["day_pnl"]
        s["day_pct"] = s["day_pnl"] / prev * 100 if prev else 0
        out["summary"] = s
    by = {r["symbol"]: r for r in con.execute(
        "SELECT symbol, day_pnl, last_price, close_price FROM holding_snapshots WHERE day=?", (day,))}
    rows = []
    for h in out.get("holdings") or []:
        h = dict(h)
        r = by.get(h["symbol"])
        if r is not None:
            h["day_pnl"] = r["day_pnl"] or 0
            h["day_pct"] = (r["last_price"] - r["close_price"]) / r["close_price"] * 100 if r["close_price"] else 0
        else:
            h["day_pnl"], h["day_pct"] = 0, 0
        rows.append(h)
    out["holdings"] = rows
    out["day_of"] = day


@app.route("/api/data")
def api_data():
    today = now_ist().strftime("%Y-%m-%d")
    with db() as con:
        held = last_session_day(con)        # outside market hours: show the last trading day's figures
        if held:
            today = held
        intraday = [dict(r) for r in con.execute(
            "SELECT ts, value, pnl, day_pnl FROM snapshots WHERE day=? ORDER BY ts", (today,))]
        daily = [dict(r) for r in con.execute(
            "SELECT day, value, invested, pnl, day_pnl FROM snapshots WHERE ts IN "
            "(SELECT MAX(ts) FROM snapshots GROUP BY day) ORDER BY day")]
        alerts = [dict(r) for r in con.execute(
            "SELECT ts, message FROM alerts ORDER BY ts DESC LIMIT 30")]
        with lock:
            out = dict(STATE)
        if held:
            hold_last_session(con, out, held)
        extremes = extremes_payload(con, out["summary"], held)
    out.update(intraday=intraday, daily=daily, alerts=alerts, extremes=extremes,
               stream={"status": STREAM["status"], "detail": STREAM["detail"]},
               market_open=market_open(), sync_active=sync_on(), sync_paused=PAUSED["on"],
               paused_since=PAUSED["since"], market_phase=market_phase(),
               next_sync_seconds=0 if DEMO else seconds_to_sync(), demo=DEMO)
    return jsonify(out)


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Pause or resume all contact with Zerodha by hand."""
    body = request.get_json(silent=True) or {}
    set_paused(bool(body.get("paused")))
    return jsonify(sync_paused=PAUSED["on"], paused_since=PAUSED["since"])


@app.route("/stock")
def stock_page():
    return render_template("stock.html", message=request.args.get("m", ""))


@app.route("/gold")
def gold_page():  # the first version of this page was gold-only
    with lock:
        gold = [h["symbol"] for h in STATE["holdings"] if h["asset"] == "Gold"]
    return redirect(url_for("stock_page", symbol=gold[0]) if gold else url_for("stock_page"))


@app.route("/api/stock")
def api_stock():
    return jsonify(stock_payload(request.args.get("symbol", "").strip()))


@app.route("/stock/import", methods=["POST"])
def stock_import():
    new = total = 0
    problems = []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        try:
            rows = parse_tradebook(f.filename, f.read())
        except Exception as e:  # noqa: BLE001
            problems.append(f"{f.filename}: {e}")
            continue
        total += len(rows)
        new += store_trades(rows, "console")
    if new:
        sync_extras(force=True)  # fetch the price history for the newly imported period
    msg = f"Imported {new} new trades ({total - new} were already stored)."
    if problems:
        msg += " Could not read: " + "; ".join(problems)
    return redirect(url_for("stock_page", m=msg))


TRADING_DAY = "strftime('%w', day) NOT IN ('0', '6')"  # skip Saturday and Sunday rows


@app.route("/api/history")
def api_history():
    """One row per trading day (the day's last snapshot) for the whole portfolio or one stock."""
    symbol = request.args.get("symbol", "").strip()
    with db() as con:
        symbols = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM holding_snapshots ORDER BY symbol")]
        if symbol:
            # a stock's total P&L at the day's high and low price (units and average cost of that day)
            cur = con.execute(
                "SELECT day, day_pnl, pnl, value, qty * (day_high - avg_price) AS pnl_high, "
                f"qty * (day_low - avg_price) AS pnl_low FROM holding_snapshots WHERE symbol=? AND {TRADING_DAY} "
                "ORDER BY day DESC LIMIT 250", (symbol,))
        else:
            # the portfolio's total P&L: the day's last snapshot, and its highest and lowest snapshot that day
            cur = con.execute(
                "SELECT s.day, s.day_pnl, s.pnl, s.value, e.hi AS pnl_high, e.lo AS pnl_low FROM snapshots s JOIN "
                "(SELECT day AS d, MAX(ts) AS mts, MAX(pnl) AS hi, MIN(pnl) AS lo FROM snapshots GROUP BY day) e "
                f"ON s.ts = e.mts WHERE {TRADING_DAY.replace('day', 's.day')} ORDER BY s.day DESC LIMIT 250")
        rows = [dict(r) for r in cur][::-1]
    for r in rows:
        r["day_pnl"] = r["day_pnl"] or 0
        for k in ("pnl_high", "pnl_low"):          # no high / low recorded that day: the close stands for both
            if r.get(k) is None:
                r[k] = r["pnl"]
        if r["pnl"] is not None:
            r["pnl_high"], r["pnl_low"] = max(r["pnl_high"], r["pnl"]), min(r["pnl_low"], r["pnl"])
    return jsonify(symbol=symbol, symbols=symbols, rows=rows)


@app.route("/export.csv")
def export_csv():
    """The full per-stock daily log, for analysis in a spreadsheet."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["day", "symbol", "qty", "avg_price", "prev_close", "last_price", "value", "day_pnl", "total_pnl"])
    with db() as con:
        for r in con.execute("SELECT day, symbol, qty, avg_price, close_price, last_price, value, day_pnl, pnl "
                             "FROM holding_snapshots ORDER BY day, symbol"):
            w.writerow(list(r))
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=portfolio-history.csv"})


@app.route("/api/live")
def api_live():
    """Server-sent events: pushes prices and P&L to the browser as soon as each update arrives."""
    def gen():
        seen = -1
        yield "retry: 3000\n\n"
        while True:
            with tick_cond:
                tick_cond.wait_for(lambda: tick_seq[0] != seen, timeout=15)
                current = tick_seq[0]
            if current == seen:
                yield ": keepalive\n\n"
                continue
            seen = current
            with lock:
                out = {k: STATE[k] for k in ("holdings", "summary", "updated", "logged_in", "error")}
            with db() as con:
                out["extremes"] = extremes_payload(con, out["summary"])
            out["stream"] = {"status": STREAM["status"], "detail": STREAM["detail"]}
            yield f"data: {json.dumps(out)}\n\n"
            time.sleep(0.2)  # at most 5 pushes a second per browser

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/test-alert", methods=["POST"])
def test_alert():
    return jsonify(result=notify("Test alert", "Your portfolio dashboard alerts are working."))


# ---------------------------------------------------------------- paper trading (Algo page)
# The trader-paper service (paper_engine.py) does the trading; this page only reads its
# database and passes Pause / Resume / Square-off requests to it. Nothing here places orders.
PAPER_DB = BASE / "data" / "paper.db"


def paper_db():
    con = sqlite3.connect(PAPER_DB, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def next_entry_day(eng_mod, settings, rt):
    """The next date and window the strategy will try to enter."""
    cal = eng_mod.Calendar(settings["holidays"])
    t = now_ist()
    d = t.date()
    for _ in range(21):
        ewd = 3 if d.isoformat() <= settings["era_cutover"] else 1
        nxt = cal.next_trading_day(d)
        last = (nxt - d).days >= 8 - d.isoweekday() if d.weekday() < 5 else True
        future = d > t.date() or (t.hour * 100 + t.minute) < settings["entry_end"]
        if d.isoweekday() == ewd and cal.is_trading_day(d) and not last and future \
                and rt.get("entered_date") != d.isoformat() and rt.get("skip_date") != d.isoformat():
            return d.strftime("%a %d %b %Y")
        d += timedelta(days=1)
    return ""


@app.route("/algo")
def algo_page():
    return render_template("algo.html")


@app.route("/api/algo")
def api_algo():
    if not PAPER_DB.exists():
        return jsonify(installed=False)
    import paper_engine as pe  # beside this file
    settings = pe.load_settings()
    with paper_db() as con:
        rt = dict(pe.RUNTIME_DEFAULTS)
        rt.update({r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key, value FROM runtime")})
        runs = [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 104")]
        current = runs[0] if runs and runs[0]["status"] == "open" else None
        last = runs[0] if runs else None
        legs = [dict(r) for r in con.execute("SELECT * FROM legs WHERE run_id=? ORDER BY set_no, leg_no",
                                             (last["id"] if last else -1,))]
        mtm = [dict(r) for r in con.execute("SELECT ts, pnl, spot FROM mtm WHERE run_id=? ORDER BY ts",
                                            (last["id"] if last else -1,))]
        events = [dict(r) for r in con.execute("SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT 60")]
        skips = con.execute("SELECT COUNT(*) FROM events WHERE level='skip'").fetchone()[0]
        pending = [r["cmd"] for r in con.execute("SELECT cmd FROM commands WHERE done IN (0, 2)")]
        last_skip = con.execute("SELECT msg FROM events WHERE level='skip' ORDER BY id DESC LIMIT 1").fetchone()
    today = now_ist().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    skip_note = last_skip["msg"].replace("Week skipped: ", "") if last_skip and rt["skip_date"] >= week_start else ""
    for x in legs:
        sign = 1 if x["side"] == "B" else -1
        px = x["exit_px"] if x["status"] == "closed" else x["ltp"]
        x["pnl"] = sign * ((px or 0) - x["entry_px"]) * x["qty"]
    closed = [r for r in runs if r["status"] == "closed"]
    hb_age = None
    if rt.get("heartbeat"):
        try:
            hb_age = (now_ist() - datetime.fromisoformat(rt["heartbeat"])).total_seconds()
        except ValueError:
            pass
    keep = ("margin_per_unit", "multiplier", "permit_mode", "permit_date", "entry_start", "entry_end",
            "short_cutoff", "exit_time", "credit_cap_pct", "debit_cap_pct", "target_pct", "stop_pct")
    return jsonify(
        installed=True, strategy=pe.STRATEGY, runtime=rt, heartbeat_age=hb_age,
        settings={k: settings[k] for k in keep}, current=current, last=last, legs=legs, mtm=mtm,
        runs=runs, events=events, pending=pending, skip_note=skip_note, next_entry=next_entry_day(pe, settings, rt),
        stats={"runs": len(closed), "wins": sum(1 for r in closed if (r["pnl"] or 0) > 0),
               "total": sum(r["pnl"] or 0 for r in closed), "skips": skips})


@app.route("/api/algo/command", methods=["POST"])
def api_algo_command():
    cmd = (request.get_json(silent=True) or {}).get("cmd")
    if cmd not in ("pause", "resume", "squareoff"):
        return jsonify(error="unknown command"), 400
    if not PAPER_DB.exists():
        return jsonify(error="the paper engine has not run yet"), 400
    with paper_db() as con:
        con.execute("INSERT INTO commands(ts, cmd) VALUES(?, ?)", (now_ist().strftime("%Y-%m-%d %H:%M:%S"), cmd))
    return jsonify(ok=True)


# ---------------------------------------------------------------- startup
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "your-laptop-ip"


def load_env():
    """Read KEY=value lines from a .env file beside this script (used on the server)."""
    path = BASE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    global CFG, DEMO, kite, DB_PATH, TOKEN_PATH
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true", help="run with fake data")
    args = p.parse_args()
    DEMO = args.demo

    if CONFIG_PATH.exists():
        CFG = json.loads(CONFIG_PATH.read_text())
    elif DEMO:
        CFG = json.loads((BASE / "config.example.json").read_text())
    else:
        raise SystemExit("config.json not found. Copy config.example.json to config.json and fill it in.")

    load_env()
    for cfg_key, env_key in (("api_key", "KITE_API_KEY"), ("api_secret", "KITE_API_SECRET")):
        if os.environ.get(env_key):
            CFG[cfg_key] = os.environ[env_key]
    TOKEN_PATH = BASE / CFG.get("token_file", "token.json")

    if not DEMO and (not CFG.get("dashboard_user") or CFG.get("dashboard_password", "change-me") == "change-me"):
        raise SystemExit("Set dashboard_user and dashboard_password in config.json (anything but 'change-me').")
    if DEMO:
        DB_PATH = BASE / "demo.db"
    init_db()
    load_settings()
    setup_auth()

    if not DEMO:
        from kiteconnect import KiteConnect
        if "PASTE" in CFG.get("api_key", "PASTE") or "PASTE" in CFG.get("api_secret", "PASTE"):
            raise SystemExit("Put your Kite api_key and api_secret in config.json first.")
        kite = KiteConnect(api_key=CFG["api_key"])
        adopt_token_file()

    port = CFG.get("port", 5000)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()
    if busy:
        raise SystemExit(
            f"Port {port} is already in use. On a Mac this is usually AirPlay Receiver: turn it off in "
            "System Settings > General > AirDrop & Handoff, or see 'Port 5000 on a Mac' in README.md.")
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=stream_loop, daemon=True).start()
    print(f"\n  Dashboard on this machine : http://127.0.0.1:{port}")
    print(f"  Dashboard on your phone   : http://{lan_ip()}:{port}  (same Wi-Fi; on a server use its public IP)\n",
          flush=True)
    try:
        from waitress import serve  # sturdier web server, installed on the cloud server
    except ImportError:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        serve(app, host="0.0.0.0", port=port, threads=16)


# >>> algolab page (paper trading; added by algo-trading-platform/deploy/install_server.sh)
try:
    from algolab_panel import panel as _algolab_panel
    app.register_blueprint(_algolab_panel)
except Exception as _e:  # the add-on must never stop the portfolio app
    print("Paper trading page not loaded:", _e, flush=True)
# <<< algolab page

# >>> backtests page (runs uploaded from Algo Lab on the Mac; added by install_backtests.sh)
try:
    from backtests_panel import panel as _backtests_panel
    app.register_blueprint(_backtests_panel)
except Exception as _e:  # the add-on must never stop the portfolio app
    print("Backtests page not loaded:", _e, flush=True)
# <<< backtests page

# >>> order test page (added by install_order_test.sh)
try:
    from order_test_panel import panel as _order_test_panel
    app.register_blueprint(_order_test_panel)
except Exception as _e:
    print("Order test page not loaded:", _e, flush=True)
# <<< order test page

# >>> positions page (open Zerodha positions: live P&L, GTT SL/target, close)
try:
    from positions_panel import panel as _positions_panel
    app.register_blueprint(_positions_panel)
except Exception as _e:  # the add-on must never stop the portfolio app
    print("Positions page not loaded:", _e, flush=True)
# <<< positions page

# >>> ibkr page (US stocks from the IBKR sync; added by install_ibkr_panel.sh)
try:
    from ibkr_panel import panel as _ibkr_panel
    app.register_blueprint(_ibkr_panel)
except Exception as _e:  # the add-on must never stop the portfolio app
    print("US stocks page not loaded:", _e, flush=True)
# <<< ibkr page

if __name__ == "__main__":
    main()
