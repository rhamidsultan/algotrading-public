#!/usr/bin/env python3
"""Paper-trading engine: Blaze Butterfly (NIFTY Zero-Adjustment).

PAPER ONLY. This script never places, modifies or cancels orders. It reads live
NIFTY prices from Zerodha (Kite Connect quote API), applies the rules of the
"Blaze Butterfly (NIFTY Zero-Adjustment)" spec and records simulated fills in
data/paper.db. The dashboard's Algo page (/algo) shows what it is doing.

Run:   python paper_engine.py           (what the trader-paper service runs)
       python paper_engine.py --check   (one-off: test prices, show today's strike candidates)

Strategy in short: a NIFTY put broken-wing butterfly on next week's expiry.
  buy 1 lot PE at K1 = R-200-d, sell 2 lots PE at K2 = R-400-d, buy 1 lot PE at K3 = R-800-d
  R = spot rounded to 50 (halfway rounds down), then down to the 100 below if it ends in 50.
  d = 0,100,...,500: the first candidate whose net premium is within -0.7%/+0.6% of
  margin_per_unit is taken. Entry Monday 15:10-15:12 (Wednesday before 2025-08-28).
  Exits (shorts first): P&L <= -1% or >= +1% of margin x multiplier; Tuesday (Thursday)
  spot <= K1; 14:30 on the last trading day of the week or Friday; no short by 15:14 on
  the entry day; filled premium outside the band. No adjustments, no same-week re-entry.

Paper fills: buys at the best ask, sells at the best bid (last price if that side is empty).
P&L shown is gross (before brokerage, STT and other charges).
"""
import argparse
import csv
import json
import os
import smtplib
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
IST = ZoneInfo("Asia/Kolkata")
DB_PATH = BASE / "data" / "paper.db"
SETTINGS_PATH = BASE / "paper_config.json"
STRATEGY = "Blaze Butterfly (NIFTY Zero-Adjustment)"
SPOT_KEY = "NSE:NIFTY 50"

# Values from the spec's INIT VARS. Override any of them in paper_config.json.
DEFAULTS = {
    "margin_per_unit": 87300,      # basket margin (Rs) for 1 unit = 1/2/1 lots
    "multiplier": 1,               # units traded (Tradetron's deploy multiplier)
    "permit_mode": 2,              # 2 = always permitted; 1 = only when permit_date == today
    "permit_date": "2000-01-01",
    "era_cutover": "2025-08-28",   # on/before: Wed entry / Thu crossing; after: Mon entry / Tue crossing
    "entry_start": 1510,           # HHMM
    "entry_end": 1512,             # HHMM, exclusive
    "short_cutoff": 1514,
    "unwind_time": 1514,
    "exit_time": 1430,
    "credit_cap_pct": 0.006,
    "debit_cap_pct": 0.007,
    "target_pct": 0.01,
    "stop_pct": 0.01,
    "upper_off": 200,
    "short_off": 400,
    "lower_off": 800,
    "shift_step": 100,
    "max_shift": 500,
    "reactivation_minutes": 360,
    "poll_seconds": 5,
    "notify": True,                # email/Telegram on entries, exits and skips (uses config.json)
    # NSE trading holidays on weekdays. Add next year's list when NSE publishes it.
    "holidays": [
        "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
        "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
        "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
    ],
}

NEVER = "2000-01-01"
RUNTIME_DEFAULTS = {
    "entry_wd": 1, "cross_wd": 2, "k1": 0, "k2": 0, "k3": 0, "shift_sel": 0, "ref_r": 0,
    "spot_snap": 0, "lot_sz": 0, "net_prem": 0, "m_entry": 0, "tgt_amt": 99999999,
    "stop_amt": -99999999, "c_hi": 99999999, "c_lo": -99999999, "wk_last_day": 1,
    "sel_date": NEVER, "entered_date": NEVER, "skip_date": NEVER, "sel_sig": NEVER,
    "exp_sel": NEVER, "reactivate_at": "", "paused": False, "run_id": 0, "heartbeat": "",
    "status": "", "login_wait": False, "login_alert_date": NEVER,
}


def now_ist():
    return datetime.now(IST)


def hhmm(t):
    return t.hour * 100 + t.minute


def load_settings():
    s = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        s.update(json.loads(SETTINGS_PATH.read_text()))
    return s


# ---------------------------------------------------------------- calendar
class Calendar:
    def __init__(self, holidays):
        self.holidays = {date.fromisoformat(h) for h in holidays}

    def is_trading_day(self, d):
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, d):
        n = d + timedelta(days=1)
        while not self.is_trading_day(n):
            n += timedelta(days=1)
        return n


# ---------------------------------------------------------------- market data
class MarketError(Exception):
    pass


class LoginNeeded(MarketError):
    pass


class KiteMarket:
    """Live prices from Zerodha. Shares the day's login with the dashboard (same token file)."""

    def __init__(self):
        cfg = {}
        cfg_path = BASE / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
        env = {}
        env_path = BASE / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
        self.api_key = os.environ.get("KITE_API_KEY") or env.get("KITE_API_KEY") or cfg.get("api_key", "")
        self.token_path = BASE / cfg.get("token_file", "token.json")
        self.kite = None
        self.token_mtime = None
        self.chain_day = None
        self.chain = {}      # (expiry, strike, type) -> instrument
        self.expiries = []

    def _token_fresh(self, mtime):
        t = now_ist()
        cutoff = t.replace(hour=6, minute=0, second=0, microsecond=0)
        if t < cutoff:
            cutoff -= timedelta(days=1)
        return mtime >= cutoff.timestamp()

    def connect(self):
        try:
            mtime = self.token_path.stat().st_mtime
        except OSError:
            raise LoginNeeded("No Zerodha login saved yet today.")
        if not self._token_fresh(mtime):
            raise LoginNeeded("Today's Zerodha login is needed (the saved one expired at 6 am).")
        if self.kite is None or mtime != self.token_mtime:
            from kiteconnect import KiteConnect
            token = json.loads(self.token_path.read_text())["access_token"]
            self.kite = KiteConnect(api_key=self.api_key)
            self.kite.set_access_token(token)
            self.token_mtime = mtime
        return self.kite

    def _call(self, fn, *a):
        kite = self.connect()
        try:
            return getattr(kite, fn)(*a)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "TokenException":
                self.kite = None
                raise LoginNeeded(f"Zerodha refused the login token: {e}")
            raise MarketError(f"{type(e).__name__}: {e}")

    def load_chain(self, today):
        if self.chain_day == today:
            return
        # Stream Zerodha's NFO instrument list and keep only NIFTY options (keeps memory low).
        self.connect()
        req = urllib.request.Request("https://api.kite.trade/instruments/NFO", headers={
            "X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{self.kite.access_token}"})
        chain, exps = {}, set()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                lines = (ln.decode("utf-8") for ln in resp)
                for r in csv.DictReader(lines):
                    if r.get("name") == "NIFTY" and r.get("segment") == "NFO-OPT":
                        exp = date.fromisoformat(r["expiry"])
                        r["lot_size"] = int(r["lot_size"])
                        chain[(exp, int(round(float(r["strike"]))), r["instrument_type"])] = r
                        exps.add(exp)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self.kite = None
                raise LoginNeeded("Zerodha refused the login token (instrument list).")
            raise MarketError(f"instrument list: HTTP {e.code}")
        except OSError as e:
            raise MarketError(f"instrument list: {e}")
        if not chain:
            raise MarketError("No NIFTY options found in Zerodha's instrument list.")
        self.chain, self.expiries, self.chain_day = chain, sorted(exps), today

    def next_week_expiry(self, today):
        """Tradetron 'Current Week +1': the weekly expiry after the nearest one (today counts)."""
        self.load_chain(today)
        ahead = [e for e in self.expiries if e >= today]
        return ahead[1] if len(ahead) > 1 else None

    def option(self, expiry, strike, opt_type="PE"):
        r = self.chain.get((expiry, int(strike), opt_type))
        if not r:
            return None
        return {"key": "NFO:" + r["tradingsymbol"], "symbol": r["tradingsymbol"], "lot_size": int(r["lot_size"])}

    def quotes(self, keys):
        """{key: {"ltp", "bid", "ask"}}; a missing side is 0."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        raw = self._call("quote", keys)
        out = {}
        for k in keys:
            q = raw.get(k)
            if not q:
                continue
            depth = q.get("depth") or {}
            buy, sell = depth.get("buy") or [], depth.get("sell") or []
            out[k] = {
                "ltp": float(q.get("last_price") or 0),
                "bid": float(buy[0]["price"]) if buy and buy[0].get("quantity") else 0.0,
                "ask": float(sell[0]["price"]) if sell and sell[0].get("quantity") else 0.0,
            }
        return out


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, entered_date TEXT, entered_ts TEXT, expiry TEXT, ref_r INTEGER,
  shift INTEGER, k1 INTEGER, k2 INTEGER, k3 INTEGER, spot_entry REAL, lot_size INTEGER,
  multiplier REAL, margin REAL, planned_net REAL, filled_net REAL, tgt_amt REAL, stop_amt REAL,
  status TEXT, exit_ts TEXT, exit_reason TEXT, pnl REAL, max_pnl REAL, min_pnl REAL, spot_exit REAL);
CREATE TABLE IF NOT EXISTS legs (
  id INTEGER PRIMARY KEY, run_id INTEGER, set_no INTEGER, leg_no INTEGER, side TEXT,
  strike INTEGER, opt_type TEXT, expiry TEXT, symbol TEXT, quote_key TEXT, lots REAL, qty INTEGER,
  entry_ts TEXT, entry_px REAL, ltp REAL, exit_ts TEXT, exit_px REAL, status TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS mtm (ts TEXT, run_id INTEGER, pnl REAL, spot REAL);
CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY, ts TEXT, cmd TEXT, done INTEGER DEFAULT 0);
"""


def open_db(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- notifications
def notify(subject, body):
    cfg_path = BASE / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except ValueError:
        cfg = {}
    e = cfg.get("email", {})
    if e.get("enabled"):
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, e["from"], e["to"]
            msg.set_content(body)
            with smtplib.SMTP_SSL(e.get("smtp_host", "smtp.gmail.com"), e.get("smtp_port", 465), timeout=20) as s:
                s.login(e["from"], e["app_password"])
                s.send_message(msg)
        except Exception as ex:  # noqa: BLE001
            print("email failed:", ex, flush=True)
    t = cfg.get("telegram", {})
    if t.get("enabled"):
        try:
            data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": f"{subject}\n{body}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data, timeout=20)
        except Exception as ex:  # noqa: BLE001
            print("telegram failed:", ex, flush=True)


# ---------------------------------------------------------------- the strategy
class Engine:
    def __init__(self, market, settings, db_path=None, notifier=notify):
        self.m = market
        self.s = settings
        self.cal = Calendar(settings["holidays"])
        self.con = open_db(db_path)
        self.notifier = notifier
        self.rt = dict(RUNTIME_DEFAULTS)
        for row in self.con.execute("SELECT key, value FROM runtime"):
            self.rt[row["key"]] = json.loads(row["value"])
        self.qcache = {}
        self.qcache_at = None
        self.last_mtm_min = None
        self.last_err = ("", 0.0)

    # ---- small helpers
    def set_rt(self, **kv):
        for k, v in kv.items():
            if k in self.rt and self.rt[k] == v and k != "heartbeat":
                continue
            self.rt[k] = v
            self.con.execute("INSERT OR REPLACE INTO runtime(key, value) VALUES(?, ?)", (k, json.dumps(v)))

    def log(self, level, msg, now=None, alert=False):
        ts = (now or now_ist()).strftime("%Y-%m-%d %H:%M:%S")
        self.con.execute("INSERT INTO events(ts, level, msg) VALUES(?, ?, ?)", (ts, level, msg))
        print(f"[{ts}] {level.upper()}: {msg}", flush=True)
        if alert and self.s.get("notify") and self.notifier:
            try:
                self.notifier(f"[Paper] {STRATEGY}: {level}", msg)
            except Exception:  # noqa: BLE001
                pass

    def open_legs(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM legs WHERE status='open' ORDER BY set_no, leg_no")]

    def count_open(self, side=None):
        legs = self.open_legs()
        if side:
            legs = [x for x in legs if x["side"] == side]
        return len(legs)

    def get_quotes(self, keys, now, max_age=2):
        """Quotes for keys, re-using ones fetched within max_age seconds."""
        fresh = self.qcache_at and (now - self.qcache_at).total_seconds() <= max_age
        need = [k for k in keys if not fresh or k not in self.qcache]
        if need:
            got = self.m.quotes(need if fresh else list(keys))
            if not fresh:
                self.qcache = {}
            self.qcache.update(got)
            self.qcache_at = now
        return {k: self.qcache[k] for k in keys if k in self.qcache}

    def mult(self):
        return float(self.s.get("multiplier") or 1)

    # ---- the spec's PYTHON BLOCK
    def python_block(self, now):
        s, today, tm, wd = self.s, now.date(), hhmm(now), now.isoweekday()
        today_s = today.isoformat()
        ewd, cwd = 1, 2
        if today_s <= s["era_cutover"]:
            ewd, cwd = 3, 4
        self.set_rt(entry_wd=ewd, cross_wd=cwd)

        nxt = self.cal.next_trading_day(today)
        lastflag = 2 if (nxt - today).days >= 8 - wd else 1
        self.set_rt(wk_last_day=lastflag)

        permit_ok = s["permit_mode"] == 2 or s["permit_date"] == today_s
        go = (wd == ewd and s["entry_start"] <= tm < s["entry_end"] and lastflag == 1 and permit_ok
              and self.rt["entered_date"] != today_s and self.rt["skip_date"] != today_s)
        if not go:
            return
        result = self.select(now)
        sel = result["sel"]
        if sel == 1:
            if self.rt["sel_sig"] != result["sig"]:
                mpu = float(s["margin_per_unit"])
                self.set_rt(k1=result["k1"], k2=result["k2"], k3=result["k3"], shift_sel=result["d"],
                            ref_r=result["R"], spot_snap=result["spot"], exp_sel=result["expiry"],
                            lot_sz=result["lot"], net_prem=result["net"], m_entry=mpu,
                            tgt_amt=s["target_pct"] * mpu * self.mult(),
                            stop_amt=-1 * s["stop_pct"] * mpu * self.mult(),
                            c_hi=s["credit_cap_pct"] * mpu, c_lo=-1 * s["debit_cap_pct"] * mpu,
                            sel_sig=result["sig"])
            if self.rt["sel_date"] != today_s:
                self.set_rt(sel_date=today_s)
                self.log("info", f"Selected {result['sig']} (R {result['R']}, shift {result['d']}, "
                                 f"net premium Rs {result['net']:.0f}/unit, expiry {result['expiry']})", now)
        else:
            if self.rt["sel_date"] == today_s:
                self.set_rt(sel_date="none")
            if sel == 2:
                self.set_rt(skip_date=today_s)
                self.log("skip", f"Week skipped: {result['why']}", now, alert=True)
            elif result.get("why") and self.rt.get("status") != result["why"]:
                self.log("warn", f"No selection yet: {result['why']} (will retry)", now)
        self.set_rt(status=result.get("why", ""))

    def select(self, now):
        """Strike search. sel: 1 = selected, 2 = skip the week, 0 = try again next cycle."""
        s, today = self.s, now.date()
        out = {"sel": 0, "why": ""}
        mpu = float(s["margin_per_unit"] or 0)
        if mpu <= 0:
            out["why"] = "margin_per_unit is not set"
            return out
        exp = self.m.next_week_expiry(today)
        if not exp:
            out["why"] = "next week's expiry not found"
            return out
        days = (exp - today).days
        if not 7 <= days <= 9:
            out.update(sel=0, why=f"next-week expiry {exp} is {days} days away (needs 7-9)")
            return out
        spot = self.get_quotes([SPOT_KEY], now).get(SPOT_KEY, {}).get("ltp", 0)
        if not spot:
            out["why"] = "no NIFTY spot price"
            return out
        lo = int(spot / 50) * 50
        a = lo + 50 if spot - lo > 25 else lo
        rr = a - 50 if a % 100 == 50 else a
        step, maxs = float(s["shift_step"]), float(s["max_shift"])
        cands = []
        for i in range(6):
            d = i * step
            if d > maxs:
                break
            ks = (rr - s["upper_off"] - d, rr - s["short_off"] - d, rr - s["lower_off"] - d)
            cands.append((int(d), [int(k) for k in ks]))
        opts = {k: self.m.option(exp, k) for _, ks in cands for k in ks}
        q = self.get_quotes([o["key"] for o in opts.values() if o], now)
        out.update(spot=spot, R=int(rr), expiry=exp.isoformat(), table=[])
        for d, (k1, k2, k3) in cands:
            o1, o2, o3 = opts[k1], opts[k2], opts[k3]
            p1 = q.get(o1["key"], {}).get("ask", 0) if o1 else 0
            p2 = q.get(o2["key"], {}).get("bid", 0) if o2 else 0
            p3 = q.get(o3["key"], {}).get("ask", 0) if o3 else 0
            lot = o2["lot_size"] if o2 else 0
            if not (p1 > 0 and p2 > 0 and p3 > 0 and lot):
                out["why"] = f"no bid/ask for {k1}/{k2}/{k3} PE"
                return out  # spec: missing price ends the search without skipping
            c = (2 * p2 - p1 - p3) * lot
            out["table"].append((d, k1, k2, k3, p1, p2, p3, lot, c))
            if c < -1 * s["debit_cap_pct"] * mpu:
                out.update(sel=2, why=f"debit Rs {-c:.0f} at shift {d} is beyond the "
                                      f"{s['debit_cap_pct'] * 100:.1f}% cap (Rs {s['debit_cap_pct'] * mpu:.0f})")
                return out
            if c <= s["credit_cap_pct"] * mpu:
                out.update(sel=1, d=d, k1=k1, k2=k2, k3=k3, lot=lot, net=c, why="",
                           sig=f"{k1}-{k2}-{k3}")
                return out
        out.update(sel=2, why=f"credit stayed above the {s['credit_cap_pct'] * 100:.1f}% cap "
                              f"(Rs {s['credit_cap_pct'] * mpu:.0f}) up to shift {int(maxs)}")
        return out

    # ---- entries
    def permit_ok(self, today_s):
        return self.s["permit_mode"] == 2 or self.s["permit_date"] == today_s

    def set1_ready(self, now):
        today_s, tm = now.date().isoformat(), hhmm(now)
        if self.rt.get("paused"):
            return False
        if self.rt.get("reactivate_at") and now.isoformat() < self.rt["reactivate_at"]:
            return False
        return (now.isoweekday() == self.rt["entry_wd"] and self.s["entry_start"] <= tm < self.s["entry_end"]
                and self.permit_ok(today_s) and self.rt["sel_date"] == today_s
                and self.rt["entered_date"] != today_s and float(self.s["margin_per_unit"]) > 0
                and self.count_open() == 0)

    def set2_ready(self, now):
        return (self.count_open("B") == 2 and self.count_open("S") == 0
                and self.rt["entered_date"] == now.date().isoformat() and hhmm(now) < self.s["short_cutoff"])

    def fill(self, quote, side):
        px = quote.get("ask" if side == "B" else "bid") or quote.get("ltp") or 0
        return float(px)

    def enter_set1(self, now):
        exp = date.fromisoformat(self.rt["exp_sel"])
        o1, o3 = self.m.option(exp, self.rt["k1"]), self.m.option(exp, self.rt["k3"])
        q = self.get_quotes([o1["key"], o3["key"]], now)
        p1, p3 = self.fill(q.get(o1["key"], {}), "B"), self.fill(q.get(o3["key"], {}), "B")
        if not (p1 > 0 and p3 > 0):
            self.log("warn", "Long legs not filled: no price (will retry)", now)
            return
        ts, mult = now.strftime("%Y-%m-%d %H:%M:%S"), self.mult()
        cur = self.con.execute(
            "INSERT INTO runs(entered_date, entered_ts, expiry, ref_r, shift, k1, k2, k3, spot_entry, lot_size,"
            " multiplier, margin, planned_net, tgt_amt, stop_amt, status, pnl, max_pnl, min_pnl)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',0,0,0)",
            (now.date().isoformat(), ts, self.rt["exp_sel"], self.rt["ref_r"], self.rt["shift_sel"],
             self.rt["k1"], self.rt["k2"], self.rt["k3"], self.rt["spot_snap"], self.rt["lot_sz"], mult,
             self.rt["m_entry"], self.rt["net_prem"], self.rt["tgt_amt"], self.rt["stop_amt"]))
        run_id = cur.lastrowid
        for leg_no, (o, k, p) in enumerate(((o1, self.rt["k1"], p1), (o3, self.rt["k3"], p3)), start=1):
            self.add_leg(run_id, 1, leg_no, "B", k, o, 1, p, ts)
        self.set_rt(entered_date=now.date().isoformat(), run_id=run_id)
        self.log("trade", f"Bought {int(mult)} lot PE {self.rt['k1']} @ {p1:.2f} and {int(mult)} lot PE "
                          f"{self.rt['k3']} @ {p3:.2f} (expiry {self.rt['exp_sel']})", now, alert=True)

    def add_leg(self, run_id, set_no, leg_no, side, strike, o, lots, px, ts):
        qty = int(round(lots * self.mult() * o["lot_size"]))
        self.con.execute(
            "INSERT INTO legs(run_id, set_no, leg_no, side, strike, opt_type, expiry, symbol, quote_key, lots, qty,"
            " entry_ts, entry_px, ltp, status) VALUES(?,?,?,?,?,'PE',?,?,?,?,?,?,?,?,'open')",
            (run_id, set_no, leg_no, side, int(strike), self.rt["exp_sel"], o["symbol"], o["key"],
             lots * self.mult(), qty, ts, px, px))

    def enter_set2(self, now):
        exp = date.fromisoformat(self.rt["exp_sel"])
        o2 = self.m.option(exp, self.rt["k2"])
        q = self.get_quotes([o2["key"]], now)
        p2 = self.fill(q.get(o2["key"], {}), "S")
        if not p2 > 0:
            self.log("warn", "Short leg not filled: no price (will retry)", now)
            return
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        run_id = self.rt["run_id"]
        self.add_leg(run_id, 2, 1, "S", self.rt["k2"], o2, 2, p2, ts)
        longs = {r["strike"]: r["entry_px"] for r in self.open_legs() if r["side"] == "B"}
        filled = (2 * p2 - longs.get(self.rt["k1"], 0) - longs.get(self.rt["k3"], 0)) * self.rt["lot_sz"]
        self.con.execute("UPDATE runs SET filled_net=? WHERE id=?", (filled, run_id))
        self.log("trade", f"Sold {int(2 * self.mult())} lots PE {self.rt['k2']} @ {p2:.2f}; filled net premium "
                          f"Rs {filled:.0f}/unit (planned Rs {self.rt['net_prem']:.0f})", now, alert=True)

    # ---- universal exit
    def mark(self, now):
        """Refresh leg prices; returns (run P&L, spot, quotes)."""
        legs = self.open_legs()
        q = self.get_quotes([SPOT_KEY] + [x["quote_key"] for x in legs], now)
        for x in legs:
            ltp = q.get(x["quote_key"], {}).get("ltp") or 0
            if ltp > 0:
                self.con.execute("UPDATE legs SET ltp=? WHERE id=?", (ltp, x["id"]))
        pnl = self.run_pnl(self.rt["run_id"])
        spot = q.get(SPOT_KEY, {}).get("ltp", 0)
        run = self.con.execute("SELECT max_pnl, min_pnl FROM runs WHERE id=?", (self.rt["run_id"],)).fetchone()
        if run:
            self.con.execute("UPDATE runs SET pnl=?, max_pnl=?, min_pnl=? WHERE id=?",
                             (pnl, max(run["max_pnl"] or 0, pnl), min(run["min_pnl"] or 0, pnl), self.rt["run_id"]))
        minute = now.strftime("%Y-%m-%d %H:%M")
        if minute != self.last_mtm_min:
            self.con.execute("INSERT INTO mtm(ts, run_id, pnl, spot) VALUES(?,?,?,?)",
                             (minute, self.rt["run_id"], pnl, spot))
            self.last_mtm_min = minute
        return pnl, spot, q

    def run_pnl(self, run_id):
        total = 0.0
        for x in self.con.execute("SELECT * FROM legs WHERE run_id=?", (run_id,)):
            px = x["exit_px"] if x["status"] == "closed" else x["ltp"]
            sign = 1 if x["side"] == "B" else -1
            total += sign * (px - x["entry_px"]) * x["qty"]
        return total

    def exit_reason(self, now, pnl, spot):
        rt, s, tm, wd, today_s = self.rt, self.s, hhmm(now), now.isoweekday(), now.date().isoformat()
        if self.count_open() == 0:
            return None
        if pnl <= rt["stop_amt"]:
            return f"stop loss (P&L Rs {pnl:.0f} <= Rs {rt['stop_amt']:.0f})"
        if wd == rt["cross_wd"] and spot > 0 and spot <= rt["k1"]:
            return f"crossing day: NIFTY {spot:.2f} <= K1 {rt['k1']}"
        if pnl >= rt["tgt_amt"]:
            return f"target (P&L Rs {pnl:.0f} >= Rs {rt['tgt_amt']:.0f})"
        if rt["wk_last_day"] == 2 and tm >= s["exit_time"]:
            return "time exit on the last trading day of the week"
        if wd == 5 and tm >= s["exit_time"]:
            return "Friday time exit"
        if rt["entered_date"] == today_s and tm >= s["unwind_time"] and self.count_open("S") == 0:
            return "incomplete basket: no short by the cut-off"
        if self.count_open("S") > 0:
            run = self.con.execute("SELECT filled_net FROM runs WHERE id=?", (rt["run_id"],)).fetchone()
            filled = run["filled_net"] if run else None
            if filled is not None and filled > rt["c_hi"]:
                return f"fill check: filled credit Rs {filled:.0f} above cap Rs {rt['c_hi']:.0f}"
            if filled is not None and filled < rt["c_lo"]:
                return f"fill check: filled debit Rs {-filled:.0f} beyond cap Rs {-rt['c_lo']:.0f}"
        return None

    def exit_all(self, now, reason, q=None, spot=0):
        """Square off: shorts first, then longs."""
        legs = self.open_legs()
        q = q or self.get_quotes([SPOT_KEY] + [x["quote_key"] for x in legs], now)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        parts = []
        for x in sorted(legs, key=lambda r: r["side"] != "S"):
            close_side = "S" if x["side"] == "B" else "B"
            px = self.fill(q.get(x["quote_key"], {}), close_side) or x["ltp"]
            self.con.execute("UPDATE legs SET exit_px=?, exit_ts=?, status='closed' WHERE id=?", (px, ts, x["id"]))
            parts.append(f"{'bought back' if x['side'] == 'S' else 'sold'} PE {x['strike']} @ {px:.2f}")
        pnl = self.run_pnl(self.rt["run_id"])
        spot = spot or q.get(SPOT_KEY, {}).get("ltp", 0)
        self.con.execute("UPDATE runs SET status='closed', exit_ts=?, exit_reason=?, pnl=?, spot_exit=? WHERE id=?",
                         (ts, reason, pnl, spot, self.rt["run_id"]))
        react = now + timedelta(minutes=float(self.s.get("reactivation_minutes", 360)))
        self.set_rt(reactivate_at=react.isoformat())
        self.log("trade", f"Exit ({reason}): {'; '.join(parts)}. Run P&L Rs {pnl:,.0f} (gross)", now, alert=True)

    # ---- commands from the dashboard
    def commands(self, now, in_market):
        for c in self.con.execute("SELECT * FROM commands WHERE done IN (0, 2) ORDER BY id").fetchall():
            cmd = c["cmd"]
            if cmd != "squareoff" and c["done"] == 2:
                continue
            if cmd == "pause":
                self.set_rt(paused=True)
                self.log("info", "New entries paused from the dashboard", now)
            elif cmd == "resume":
                self.set_rt(paused=False)
                self.log("info", "New entries resumed from the dashboard", now)
            elif cmd == "squareoff":
                if not self.count_open():
                    self.log("info", "Square-off requested but there are no open paper positions", now)
                elif not in_market:
                    if c["done"] == 0:
                        self.log("info", "Square-off requested outside market hours; it will run at the next open", now)
                        self.con.execute("UPDATE commands SET done=2 WHERE id=?", (c["id"],))
                    continue  # keep it pending
                else:
                    self.exit_all(now, "manual square-off from the dashboard")
            self.con.execute("UPDATE commands SET done=1 WHERE id=?", (c["id"],))

    # ---- one cycle
    def step(self, now):
        today = now.date()
        tm = hhmm(now)
        in_market = self.cal.is_trading_day(today) and 915 <= tm < 1530
        self.set_rt(heartbeat=now.isoformat(timespec="seconds"))
        self.commands(now, in_market)
        if not in_market:
            return False
        try:
            self.python_block(now)
            if self.count_open():
                pnl, spot, q = self.mark(now)
                reason = self.exit_reason(now, pnl, spot)
                if not reason:
                    run = self.con.execute("SELECT entered_date FROM runs WHERE id=?", (self.rt["run_id"],)).fetchone()
                    if run and self.week_end(date.fromisoformat(run["entered_date"])) < today:
                        reason = "missed the week's exit (engine was offline); closing now"
                if reason:
                    self.exit_all(now, reason, q, spot)
            if self.set1_ready(now):
                self.enter_set1(now)
            if self.set2_ready(now):
                self.enter_set2(now)
            if self.rt.get("login_wait"):
                self.set_rt(login_wait=False)
                self.log("info", "Zerodha login found; paper engine is watching prices", now)
        except LoginNeeded as e:
            if not self.rt.get("login_wait"):
                self.set_rt(login_wait=True)
                urgent = self.count_open() > 0 or now.isoweekday() == self.rt["entry_wd"]
                alert = urgent and self.rt.get("login_alert_date") != today.isoformat()
                if alert:
                    self.set_rt(login_alert_date=today.isoformat())
                self.log("warn", f"{e} Paper trading is waiting for the morning login.", now, alert=alert)
        return True

    def week_end(self, d):
        """Last trading day of d's week."""
        end = d
        n = d
        while n.isoweekday() < 5:
            n += timedelta(days=1)
            if self.cal.is_trading_day(n):
                end = n
        return end


# ---------------------------------------------------------------- entry points
def run_forever():
    settings = load_settings()
    eng = Engine(KiteMarket(), settings)
    eng.log("info", f"Paper engine started ({STRATEGY}); multiplier {settings['multiplier']}, "
                    f"margin_per_unit Rs {settings['margin_per_unit']}")
    settings_mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else None
    while True:
        now = now_ist()
        try:
            mt = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else None
            if mt != settings_mtime:
                eng.s = load_settings()
                eng.cal = Calendar(eng.s["holidays"])
                settings_mtime = mt
                eng.log("info", "paper_config.json changed; settings reloaded", now)
            active = eng.step(now)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            msg = f"{type(e).__name__}: {e}"
            if msg != eng.last_err[0] or time.time() - eng.last_err[1] > 300:
                eng.log("error", msg, now, alert=eng.count_open() > 0)
                traceback.print_exc()
                eng.last_err = (msg, time.time())
            active = True
        time.sleep(float(eng.s.get("poll_seconds", 5)) if active else 30)


def check():
    """Print what the engine sees right now, without trading."""
    s = load_settings()
    m = KiteMarket()
    today = now_ist().date()
    try:
        m.connect()
    except LoginNeeded as e:
        sys.exit(f"Login: {e} Log in on the dashboard or the morning login page first.")
    print("Login: OK")
    m.load_chain(today)
    exp = m.next_week_expiry(today)
    print(f"NIFTY option expiries ahead: {', '.join(str(e) for e in m.expiries[:4])}")
    print(f"Next-week expiry: {exp}  ({(exp - today).days} days away; entry needs 7-9)")
    q = m.quotes([SPOT_KEY])
    print(f"NIFTY spot: {q.get(SPOT_KEY, {}).get('ltp')}")
    eng = Engine(m, s, db_path=":memory:", notifier=None)
    r = eng.select(now_ist())
    print(f"R = {r.get('R')}")
    print(f"{'shift':>5} {'K1':>6} {'K2':>6} {'K3':>6} {'ask K1':>8} {'bid K2':>8} {'ask K3':>8} {'lot':>4} {'net Rs':>8}")
    for d, k1, k2, k3, p1, p2, p3, lot, c in r.get("table", []):
        print(f"{d:>5} {k1:>6} {k2:>6} {k3:>6} {p1:>8.2f} {p2:>8.2f} {p3:>8.2f} {lot:>4} {c:>8.0f}")
    band = (-s["debit_cap_pct"] * s["margin_per_unit"], s["credit_cap_pct"] * s["margin_per_unit"])
    print(f"Allowed net premium band: Rs {band[0]:.0f} to Rs {band[1]:.0f} per unit")
    verdict = {1: f"would select {r.get('sig')}", 2: "would skip the week", 0: "no decision"}[r["sel"]]
    print(f"Result: {verdict}{' - ' + r['why'] if r.get('why') else ''}")
    print("(--check only reads prices; it ignores the entry day and time.)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="test prices and show today's candidates, no trading")
    args = p.parse_args()
    if args.check:
        check()
    else:
        run_forever()
