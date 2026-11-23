"""Positions page for the portfolio dashboard: open Zerodha positions with live prices and P&L,
SL / target held at Zerodha as a GTT, and a manual close.

Routes (behind the dashboard's sign-in; owner only, see users_panel.OWNER_PREFIX):
  GET  /positions                     the page
  GET  /api/positions                 open positions, prices, P&L, GTT stops, pending orders, closes in progress
  POST /api/positions/protect         {key, sl, target}: place / change / remove the position's GTT (OCO or single)
  POST /api/positions/close           {key, qty}: close the whole position (LIMIT at bid/ask, re-priced for 30 s)
  GET  /api/positions/jobs            closes in progress and the last few finished ones
  POST /api/positions/trail           {key, by, unit: "pct"|"pts"}: trail the GTT's SL behind the best price (by 0: off)
A position's key is EXCHANGE:TRADINGSYMBOL:PRODUCT (Kite lists NRML and MIS of one contract as separate rows).

Rules (Hamid, 29 Oct 2026):
- SL and target are one GTT at Zerodha (OCO when both are set), so they hold with the server down and overnight.
  A GTT sends a LIMIT order when it triggers; the SL limit is `SL_BUFFER` through the trigger (capped at the
  circuit limit), so a fill is likely but not guaranteed. GTT needs NRML or CNC: MIS positions can only be closed.
- An SL trigger beyond the circuit limit is refused (it could never fill: the 19 and 21 Oct live findings).
- Positions held by a LIVE Algo Lab session are read-only here (changing them makes that session stop on a
  mismatch). A session file that cannot be read locks every F&O position until it can.
- Close (checked by an independent order-safety review, 29 Oct):
  1. the price is read first; an exit order already working on the contract (e.g. a GTT that just triggered)
     stops the close before anything is changed;
  2. every exit-side GTT on the contract is deleted and every not-yet-triggered exit-side SL order cancelled
     (both saved), then the position and orders are read again: if anything moved, the close stops;
  3. a LIMIT order at the best bid (sell) / ask (buy), re-priced every 5 s for up to 30 s, at most 5% from the
     first price; a place_order that errors is looked up by its tag in the order book (it may have reached Zerodha);
  4. the order is cancelled if still open and followed until it is final. What is still open is worked out from
     the order's own fill (Kite's position list lags) and must agree with the position list before anything
     is put back. Not final, disagreeing, or another exit order working: nothing is put back and a
     "check Kite now" alert goes out (better no new stop than two exits);
  5. fully closed: any exit-side GTT or order left on the contract is cancelled. Not (fully) filled: the saved
     GTTs and SL orders are put back for what is still open (retried), and an alert says so, or says NO stop.
- Trailing SL (Hamid, 23 Nov 2026): a position's GTT SL follows its best price since the trail was set, `by` % of
  that price or points behind it; it only ever moves in the position's favour, never past the last price, never
  while a close runs (in any dashboard process) or on a position a LIVE session holds. One process moves them
  (a file lock), every TRAIL_EVERY s in market hours; the target stays. The trail ends (with an alert) when the
  position closes or changes size, or its GTT is gone.
- A dashboard restart during a close is recovered from `data/positions_jobs.json` (once: a file lock), using the
  same step 4-5 checks. Quote calls are spaced to Kite's 1 per second.
"""
import fcntl
import json
import math
import os
import re
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, jsonify, request

BASE = Path(__file__).resolve().parent
ENGINE = Path(os.environ.get("ALGOLAB_DIR", "/opt/algolab"))
IST = ZoneInfo("Asia/Kolkata")

TICK = 0.05
SL_BUFFER = 0.10        # SL limit this far through the trigger (architecture default: 127.50 -> 140.25)
TARGET_BUFFER = 0.01    # target limit this far through the trigger
CHASE_SECONDS = 30
REPRICE_EVERY = 5
MAX_CHASE_PCT = 5.0
EXCHANGES = {"NFO", "BFO", "NSE", "BSE"}
GTT_PRODUCTS = {"NRML", "CNC"}
# Most units this page sends in one order or GTT. Kept under the exchanges' freeze quantities (which change);
# bigger positions are closed in Kite, which slices them.
MAX_UNITS = {"NIFTY": 1300, "BANKNIFTY": 600, "FINNIFTY": 1200, "MIDCPNIFTY": 2000, "SENSEX": 800, "BANKEX": 600}
MAX_UNITS_OTHER = 500
FINAL = {"COMPLETE", "CANCELLED", "REJECTED"}
FNO = {"NFO", "BFO"}            # GTTs and stop orders are handled only here: an equity SELL GTT may protect holdings
OPEN_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "MODIFY PENDING", "OPEN PENDING", "VALIDATION PENDING",
                 "PUT ORDER REQ RECEIVED", "MODIFY VALIDATION PENDING", "CANCEL PENDING", "MODIFY ORDER REQ RECEIVED",
                 "TRIGGER PENDING MODIFY"}
TRAIL_EVERY = 15                # seconds between trail checks (market hours)
TAG = "positionsclose"          # Kite tag: alphanumeric, at most 20 characters
KEY_OK = re.compile(r"^(NFO|BFO|NSE|BSE|MCX|CDS):[A-Z0-9&_.-]{1,40}:[A-Z]{2,5}$")

panel = Blueprint("positions", __name__)

_sleep = time.sleep
_mono = time.monotonic
_LOCK = threading.Lock()
_QLOCK = threading.Lock()
_QLAST = [0.0]
_KEYLOCKS: dict = {}
_CACHE = {"pos_t": 0.0, "pos": None, "quotes": {}, "slow_t": 0.0, "gtts": None, "orders": None}
JOBS: dict = {}                  # job id -> job
BUSY: dict = {}                  # position key -> job id while a close runs
_RECOVERY_LOCK = []              # the open lock file while this process owns recovery


def _now():
    return datetime.now(IST)


def _data_dir():
    d = BASE / "data"
    d.mkdir(exist_ok=True)
    return d


def _jobs_file():
    return _data_dir() / "positions_jobs.json"


def _log_action(**rec):
    try:
        with open(_data_dir() / "positions_actions.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": _now().isoformat(timespec="seconds"), **rec}, default=str) + "\n")
    except OSError:
        pass


def _keylock(key):
    with _LOCK:
        return _KEYLOCKS.setdefault(key, threading.Lock())


def _split(key):
    exch, sym, product = key.split(":")
    return exch, sym, product


def _max_units(sym):
    o = parse_option(sym)
    return MAX_UNITS.get(o[0], MAX_UNITS_OTHER) if o else MAX_UNITS_OTHER


# ---- Zerodha ----------------------------------------------------------------------------------
def _kite():
    """A Kite client from the dashboard's own login (config.json + token file), or (None, why)."""
    try:
        cfg = json.loads((BASE / "config.json").read_text())
    except (OSError, ValueError):
        return None, "config.json not readable"
    key = os.environ.get("KITE_API_KEY") or cfg.get("api_key")
    tok_path = BASE / cfg.get("token_file", "token.json")
    try:
        token = json.loads(tok_path.read_text())["access_token"]
        when = datetime.fromtimestamp(tok_path.stat().st_mtime, IST)
    except (OSError, ValueError, KeyError):
        return None, "The server has no Zerodha login yet. Log in on the dashboard first."
    now = _now()
    cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    if when < cutoff:
        return None, "The server's Zerodha login is from before this morning. Log in on the dashboard first."
    if not key:
        return None, "Kite API key not found on the server"
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=key)
    k.set_access_token(token)
    return k, None


def _quote(k, keys, tries=1, wait=True):
    """kite.quote spaced to Kite's limit of 1 request per second (shared by the page and closes), with retries.
    wait=False (page polls): give up at once if a quote is in flight or due later; the page keeps its last prices."""
    err = None
    for i in range(tries):
        if not wait:
            if not _QLOCK.acquire(blocking=False):
                raise RuntimeError("quote busy")
            _QLOCK.release()
            if _QLAST[0] + 1.05 - _mono() > 0:
                raise RuntimeError("quote due later")
        with _QLOCK:
            pause = _QLAST[0] + 1.05 - _mono()
            if pause > 0:
                _sleep(pause)
            _QLAST[0] = _mono()
            try:
                return k.quote(keys) or {}
            except Exception as e:  # noqa: BLE001
                err = e
        if i + 1 < tries:
            _sleep(1)
    raise err


def _retry(fn, tries=3):
    err = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            err = e
            if i + 1 < tries:
                _sleep(1)
    raise err


def _notify(subject, body):
    """Phone notification (if the app add-on is loaded) plus email / Telegram from config.json."""
    print(f"[ALERT] {subject}: {body}", flush=True)
    mod = sys.modules.get("algolab_panel")
    if mod is not None and hasattr(mod, "push"):
        try:
            mod.push(subject, body, url="/positions")
        except Exception:  # noqa: BLE001
            pass
    try:
        cfg = json.loads((BASE / "config.json").read_text())
    except (OSError, ValueError):
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
            print("   email failed:", ex, flush=True)
    t = cfg.get("telegram", {})
    if t.get("enabled"):
        try:
            data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": f"{subject}\n{body}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data, timeout=20)
        except Exception as ex:  # noqa: BLE001
            print("   telegram failed:", ex, flush=True)


# ---- prices -----------------------------------------------------------------------------------
def _up(x):
    return round(math.ceil(round(x / TICK, 6)) * TICK, 2)


def _down(x):
    return max(round(math.floor(round(x / TICK, 6)) * TICK, 2), TICK)


def _best(q, side):
    """Best price to trade at: the bid for a sell, the ask for a buy (last price if the book is empty)."""
    depth = (q or {}).get("depth") or {}
    book = depth.get("buy" if side == "SELL" else "sell") or []
    px = next((float(b["price"]) for b in book if b.get("price")), None)
    ltp = float((q or {}).get("last_price") or 0)
    if not px:
        if not ltp:
            return None
        px = ltp * (0.995 if side == "SELL" else 1.005)
    px = _down(px) if side == "SELL" else _up(px)
    up_c, lo_c = (q or {}).get("upper_circuit_limit"), (q or {}).get("lower_circuit_limit")
    if side == "BUY" and up_c:
        px = min(px, _down(float(up_c)))
    if side == "SELL" and lo_c:
        px = max(px, _up(float(lo_c)))
    return px


# ---- which positions a live Algo Lab session holds ----------------------------------------------
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_WEEKLY = re.compile(r"^([A-Z]+?)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$")
_MONTHLY = re.compile(r"^([A-Z]+?)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+)(CE|PE)$")
UNREADABLE = "?"


def parse_option(sym):
    """NIFTY26N0322650PE -> (NIFTY, 2026, 11, 3, 22650, PE); monthly NIFTY26NOV22650PE -> day None."""
    m = _MONTHLY.match(sym)
    if m:
        return m.group(1), 2000 + int(m.group(2)), _MONTHS[m.group(3)], None, int(m.group(4)), m.group(5)
    m = _WEEKLY.match(sym)
    if m:
        mon = {"O": 10, "N": 11, "D": 12}.get(m.group(3)) or int(m.group(3))
        return m.group(1), 2000 + int(m.group(2)), mon, int(m.group(4)), int(m.group(5)), m.group(6)
    return None


def _algo_held():
    """Contracts open in LIVE Algo Lab sessions (running or stopped): [(underlying, expiry, strike, right, session)].
    A session file that cannot be read gives (UNREADABLE, ..., session): every F&O position counts as held."""
    out = []
    root = ENGINE / "runtime" / "sessions"
    for p in (sorted(root.glob("*/state.json")) if root.is_dir() else []):
        st = None
        for _ in range(3):           # the engine rewrites the file every step: a half-written read is retried
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                st = None
                time.sleep(0.05)
        if st is None:
            out.append((UNREADABLE, None, None, None, p.parent.name))
            continue
        if (st or {}).get("mode") != "live":
            continue
        for pos in ((st.get("broker") or {}).get("positions") or []):
            c = pos.get("contract") or {}
            try:
                if int(pos.get("qty") or 0) != 0:
                    out.append((c["underlying"], date.fromisoformat(c["expiry"]), int(c["strike"]), c["right"], p.parent.name))
            except (KeyError, ValueError, TypeError):
                out.append((UNREADABLE, None, None, None, p.parent.name))
    return out


def held_by(sym, held):
    """The live session holding this tradingsymbol, or None. A doubtful match counts as held (read-only is safe)."""
    for h in held:
        if h[0] == UNREADABLE:
            return h[4]
    o = parse_option(sym)
    if not o:
        return None
    und, y, mon, day, strike, right = o
    for h_und, exp, h_strike, h_right, sid in held:
        if (h_und == und and h_strike == strike and h_right == right and exp.year == y and exp.month == mon
                and (day is None or exp.day == day)):
            return sid
    return None


def pretty(sym):
    o = parse_option(sym)
    if not o:
        return sym
    und, y, mon, day, strike, right = o
    mname = list(_MONTHS)[mon - 1].title()
    return f"{und} {day:02d} {mname} {strike} {right}" if day else f"{und} {mname} {strike} {right} (monthly)"


# ---- GTTs and exit orders ---------------------------------------------------------------------------
def _exit_side(qty):
    return "SELL" if qty > 0 else "BUY"


def _gtt_view(g, qty):
    """A Kite GTT read as this position's SL / target."""
    cond = g.get("condition") or {}
    trig = [float(x) for x in cond.get("trigger_values") or []]
    orders = g.get("orders") or []
    sl = target = sl_lim = tgt_lim = None
    if g.get("type") == "two-leg" and len(trig) == 2 and len(orders) == 2:
        lo, hi = (trig[0], orders[0]), (trig[1], orders[1])
        (sl, sl_o), (target, t_o) = (lo, hi) if qty > 0 else (hi, lo)
        sl_lim, tgt_lim = sl_o.get("price"), t_o.get("price")
    elif trig and orders:
        ref = float(cond.get("last_price") or 0)
        below = trig[0] < ref
        if (qty > 0) == below:
            sl, sl_lim = trig[0], orders[0].get("price")
        else:
            target, tgt_lim = trig[0], orders[0].get("price")
    gq = int(orders[0].get("quantity") or 0) if orders else 0
    return {"id": g.get("id"), "type": g.get("type"), "sl": sl, "sl_limit": sl_lim, "target": target,
            "target_limit": tgt_lim, "qty": gq, "qty_ok": gq == abs(qty)}


def _gtts_for(gtts, exch, sym, side, product=None, statuses=("active",)):
    """GTTs on this contract whose orders are all on the exit side (and this product, when given)."""
    out = []
    for g in gtts or []:
        cond = g.get("condition") or {}
        orders = g.get("orders") or []
        if (str(g.get("status", "")).lower() in statuses and cond.get("exchange") == exch
                and cond.get("tradingsymbol") == sym and orders
                and all(o.get("transaction_type") == side for o in orders)
                and (product is None or all((o.get("product") or product) == product for o in orders))):
            out.append(g)
    return out


def _gtt_for(gtts, exch, sym, qty, product):
    found = _gtts_for(gtts, exch, sym, _exit_side(qty), product)
    return found[0] if found else None


def _exit_orders(orders, exch, sym, side, product=None, skip=None):
    """Not-finished orders on this contract on the exit side."""
    return [o for o in orders or [] if o.get("tradingsymbol") == sym and o.get("exchange") == exch
            and o.get("status") in OPEN_STATUSES and o.get("transaction_type") == side
            and (product is None or o.get("product") == product) and str(o.get("order_id")) != str(skip)]


def _exit_filled(orders, exch, sym, side, product):
    """Units filled today by exit-side orders on this contract and product (any status)."""
    return sum(int(o.get("filled_quantity") or 0) for o in orders or [] if o.get("tradingsymbol") == sym
               and o.get("exchange") == exch and o.get("transaction_type") == side and o.get("product") == product)


def build_gtt(qty, product, ltp, sl, target, quote):
    """(trigger_type, trigger_values, orders, info) for this position, or raises ValueError with the reason."""
    side, q = _exit_side(qty), abs(qty)
    long = qty > 0
    up_c = float(quote.get("upper_circuit_limit") or 0) or None
    lo_c = float(quote.get("lower_circuit_limit") or 0) or None
    legs = []                      # (trigger, limit, kind)
    if sl:
        if long and sl >= ltp:
            raise ValueError(f"SL {sl:.2f} is at or above the last price {ltp:.2f}: it would trigger at once.")
        if not long and sl <= ltp:
            raise ValueError(f"SL {sl:.2f} is at or below the last price {ltp:.2f}: it would trigger at once.")
        if not long and up_c and sl >= up_c:
            raise ValueError(f"SL {sl:.2f} is at or above today's upper circuit {up_c:.2f}: a buy there could never fill. "
                             "Set it lower, or close the position.")
        if long and lo_c and sl <= lo_c:
            raise ValueError(f"SL {sl:.2f} is at or below today's lower circuit {lo_c:.2f}. Set it higher, or close the position.")
        if long:
            lim = _down(sl * (1 - SL_BUFFER))
            if lo_c:
                lim = max(lim, _up(lo_c))
        else:
            lim = _up(sl * (1 + SL_BUFFER))
            if up_c:
                lim = min(lim, _down(up_c))
        legs.append((round(sl, 2), lim, "sl"))
    if target:
        if long and target <= ltp:
            raise ValueError(f"Target {target:.2f} is at or below the last price {ltp:.2f}.")
        if not long and target >= ltp:
            raise ValueError(f"Target {target:.2f} is at or above the last price {ltp:.2f}.")
        if long and up_c and target >= up_c:
            raise ValueError(f"Target {target:.2f} is at or above today's upper circuit {up_c:.2f}.")
        if not long and lo_c and target <= lo_c:
            raise ValueError(f"Target {target:.2f} is at or below today's lower circuit {lo_c:.2f}.")
        lim = _down(target * (1 - TARGET_BUFFER)) if long else _up(target * (1 + TARGET_BUFFER))
        legs.append((round(target, 2), lim, "target"))
    if not legs:
        raise ValueError("Enter an SL, a target or both.")
    legs.sort(key=lambda x: x[0])
    orders = [{"transaction_type": side, "quantity": q, "order_type": "LIMIT", "product": product, "price": lim}
              for _, lim, _ in legs]
    info = {kind: {"trigger": t, "limit": lim} for t, lim, kind in legs}
    return ("two-leg" if len(legs) == 2 else "single"), [t for t, _, _ in legs], orders, info


# ---- the snapshot the page shows ------------------------------------------------------------------
def _fetch(k, force_slow=False):
    """Positions + quotes (cached 1 s); GTTs and orders every 5 s or after an action."""
    with _LOCK:
        t = _mono()
        fresh = _CACHE["pos"] is not None and t - _CACHE["pos_t"] < 1.0
        slow_due = force_slow or _CACHE["gtts"] is None or t - _CACHE["slow_t"] >= 5.0
        if fresh and not slow_due:
            return _CACHE["pos"], _CACHE["quotes"], _CACHE["gtts"], _CACHE["orders"]
    net = (k.positions() or {}).get("net", [])
    keys = sorted({f"{p['exchange']}:{p['tradingsymbol']}" for p in net
                   if int(p.get("quantity") or 0) != 0 and p.get("exchange") in EXCHANGES})
    try:
        quotes = _quote(k, keys, wait=False) if keys else {}
    except Exception:  # noqa: BLE001 - keep the last prices for a moment
        quotes = _CACHE["quotes"] or {}
    with _LOCK:
        if slow_due:
            _CACHE["gtts"], _CACHE["orders"], _CACHE["slow_t"] = k.get_gtts(), k.orders(), _mono()
        _CACHE.update(pos_t=_mono(), pos=net, quotes=quotes)
        return net, quotes, _CACHE["gtts"], _CACHE["orders"]


def _invalidate():
    with _LOCK:
        _CACHE.update(pos=None, gtts=None, pos_t=0.0, slow_t=0.0)


def snapshot(k, force_slow=False):
    net, quotes, gtts, orders = _fetch(k, force_slow)
    held = _algo_held()
    try:
        trails = _trails_rw()
    except OSError:
        trails = {}
    rows, closed = [], []
    for p in net:
        exch, sym, qty, product = p["exchange"], p["tradingsymbol"], int(p.get("quantity") or 0), p.get("product")
        key = f"{exch}:{sym}:{product}"
        mult = float(p.get("multiplier") or 1)
        q = quotes.get(f"{exch}:{sym}") or {}
        ltp = float(q.get("last_price") or p.get("last_price") or 0)
        pnl = float(p.get("sell_value") or 0) - float(p.get("buy_value") or 0) + qty * ltp * mult
        if qty == 0:
            closed.append({"key": key, "symbol": sym, "name": pretty(sym), "product": product, "pnl": round(pnl, 2)})
            continue
        sid = held_by(sym, held) if exch in ("NFO", "BFO") else None
        g = _gtt_for(gtts, exch, sym, qty, product)
        side = _exit_side(qty)
        pending = [{"id": o["order_id"], "side": o["transaction_type"], "type": o.get("order_type"), "qty": o.get("pending_quantity") or o.get("quantity"),
                    "price": o.get("price"), "trigger": o.get("trigger_price"), "status": o["status"], "tag": o.get("tag")}
                   for o in orders or [] if o.get("tradingsymbol") == sym and o.get("exchange") == exch
                   and o.get("product") == product and o.get("status") in OPEN_STATUSES]
        why = None
        if sid:
            why = (f"Session {sid}'s file could not be read just now: F&O positions are locked until it can."
                   if any(h[0] == UNREADABLE and h[4] == sid for h in held) else
                   f"Held by live session {sid}. Manage it from that session.")
        elif exch not in EXCHANGES:
            why = f"{exch} positions are shown only."
        elif abs(qty) > _max_units(sym):
            why = f"{abs(qty)} units is more than this page sends in one order ({_max_units(sym)}). Use Kite, which slices it."
        depth = q.get("depth") or {}
        bid = next((b["price"] for b in depth.get("buy") or [] if b.get("price")), None)
        ask = next((b["price"] for b in depth.get("sell") or [] if b.get("price")), None)
        rows.append({
            "key": key, "exchange": exch, "symbol": sym, "name": pretty(sym), "product": product, "qty": qty,
            "side": "long" if qty > 0 else "short", "avg": float(p.get("average_price") or 0), "ltp": ltp,
            "bid": bid, "ask": ask, "close": (q.get("ohlc") or {}).get("close"), "pnl": round(pnl, 2),
            "upper_circuit": q.get("upper_circuit_limit"), "lower_circuit": q.get("lower_circuit_limit"),
            "overnight": int(p.get("overnight_quantity") or 0) != 0,
            "gtt": _gtt_view(g, qty) if g else None, "pending": pending, "exit_side": side,
            "algo": sid, "locked": why, "gtt_ok": product in GTT_PRODUCTS and exch in FNO and not why,
            "busy": BUSY.get(key), "trail": trails.get(key),
        })
    rows.sort(key=lambda r: (r["exchange"], r["symbol"], r["product"] or ""))
    total = round(sum(r["pnl"] for r in rows) + sum(c["pnl"] for c in closed), 2)
    return {"as_of": _now().strftime("%H:%M:%S"), "positions": rows, "closed": closed,
            "open_pnl": round(sum(r["pnl"] for r in rows), 2), "day_pnl": total,
            "unprotected": sum(1 for r in rows if not r["gtt"] and not r["algo"] and not any(o["type"] in ("SL", "SL-M") for o in r["pending"])),
            "jobs": _jobs_view()}


def _find(net, key):
    exch, sym, product = _split(key)
    for p in net:
        if p["exchange"] == exch and p["tradingsymbol"] == sym and p.get("product") == product:
            return p
    return None


def _qty(k, key):
    return int((_find((k.positions() or {}).get("net", []), key) or {}).get("quantity") or 0)


def _err(msg, code=400):
    return jsonify(error=str(msg)), code


# ---- routes -----------------------------------------------------------------------------------------
@panel.route("/api/positions")
def api_positions():
    _start_trailer()
    k, why = _kite()
    if k is None:
        return jsonify(login=False, error=why, jobs=_jobs_view())
    try:
        return jsonify(login=True, **snapshot(k))
    except Exception as e:  # noqa: BLE001
        return jsonify(login=True, error=f"Zerodha: {e}", jobs=_jobs_view())


def _num(v):
    if v in (None, "", 0, "0"):
        return None
    x = float(v)
    if x <= 0 or not math.isfinite(x):
        raise ValueError("Prices must be positive numbers.")
    return round(round(x / TICK) * TICK, 2)


def _locked_reason(key, qty):
    exch, sym, product = _split(key)
    if exch not in EXCHANGES:
        return f"{exch} positions are shown only."
    sid = held_by(sym, _algo_held()) if exch in ("NFO", "BFO") else None
    if sid:
        return f"Held by live session {sid} (or its file could not be read). Manage it from that session."
    if abs(qty) > _max_units(sym):
        return f"{abs(qty)} units is more than this page sends in one order ({_max_units(sym)}). Use Kite, which slices it."
    return None


@panel.route("/api/positions/protect", methods=["POST"])
def api_protect():
    d = request.get_json(silent=True) or {}
    key = str(d.get("key", ""))
    if not KEY_OK.match(key):
        return _err("Unknown position")
    try:
        sl, target = _num(d.get("sl")), _num(d.get("target"))
    except (TypeError, ValueError) as e:
        return _err(e if isinstance(e, ValueError) and "positive" in str(e) else "SL and target must be numbers")
    k, why = _kite()
    if k is None:
        return _err(why)
    with _keylock(key):             # a close cannot start while this runs, and this refuses while a close runs
        if key in BUSY:
            return _err("A close is running for this position.")
        try:
            net, quotes, gtts, orders = _fetch(k, force_slow=True)
            p = _find(net, key)
            qty = int((p or {}).get("quantity") or 0)
            if not qty:
                return _err("This position is no longer open.")
            exch, sym, product = _split(key)
            why = _locked_reason(key, qty)
            if why:
                return _err(why)
            if product not in GTT_PRODUCTS:
                return _err(f"Zerodha GTTs need NRML or CNC; this position is {product}. You can still close it here.")
            if exch not in FNO:
                return _err("SL / target here is for F&O only (an equity GTT can cover your holdings too). Use Kite for equity.")
            found = _gtts_for(gtts, exch, sym, _exit_side(qty), product)
            if len(found) > 1:
                return _err(f"This position has {len(found)} GTTs at Zerodha. Delete the extra ones in Kite first.")
            g = found[0] if found else None
            if sl is None and target is None:
                if not g:
                    return _err("There is no GTT to remove.")
                k.delete_gtt(g["id"])
                _log_action(action="gtt_delete", key=key, gtt=g["id"])
                return jsonify(ok=True, message="GTT removed. This position has no stop at Zerodha now.")
            q = quotes.get(f"{exch}:{sym}") or _quote(k, [f"{exch}:{sym}"], tries=3).get(f"{exch}:{sym}") or {}
            ltp = float(q.get("last_price") or 0)
            if not ltp:
                return _err("No last price for this contract right now.")
            ttype, trig, gorders, info = build_gtt(qty, product, ltp, sl, target, q)
            if g:
                k.modify_gtt(trigger_id=g["id"], trigger_type=ttype, tradingsymbol=sym, exchange=exch,
                             trigger_values=trig, last_price=ltp, orders=gorders)
                gid, verb = g["id"], "changed"
            else:
                gid = k.place_gtt(trigger_type=ttype, tradingsymbol=sym, exchange=exch, trigger_values=trig,
                                  last_price=ltp, orders=gorders)
                gid = gid.get("trigger_id") if isinstance(gid, dict) else gid
                verb = "placed"
            _log_action(action="gtt_" + verb, key=key, gtt=gid, qty=qty, trigger=trig, orders=gorders)
            parts = []
            if "sl" in info:
                parts.append(f"SL {info['sl']['trigger']:.2f} (limit {info['sl']['limit']:.2f})")
            if "target" in info:
                parts.append(f"target {info['target']['trigger']:.2f} (limit {info['target']['limit']:.2f})")
            return jsonify(ok=True, gtt=gid, info=info, message=f"GTT {verb} at Zerodha: " + ", ".join(parts) + ".")
        except ValueError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(f"Zerodha refused: {e}")
        finally:
            _invalidate()


@panel.route("/api/positions/close", methods=["POST"])
def api_close():
    d = request.get_json(silent=True) or {}
    key = str(d.get("key", ""))
    if not KEY_OK.match(key):
        return _err("Unknown position")
    now = _now()
    if now.weekday() >= 5 or not (now.replace(hour=9, minute=15, second=0, microsecond=0) <= now
                                  <= now.replace(hour=15, minute=40, second=0, microsecond=0)):
        return _err("The market is closed. A close can be sent from 09:15 to 15:40 IST on trading days.")
    try:
        expect = None if d.get("qty") is None else int(d.get("qty"))
    except (TypeError, ValueError):
        return _err("Bad quantity")
    k, why = _kite()
    if k is None:
        return _err(why)
    with _keylock(key):
        if key in BUSY:
            return _err("A close is already running for this position.")
        try:
            net = (k.positions() or {}).get("net", [])
        except Exception as e:  # noqa: BLE001
            return _err(f"Zerodha: {e}")
        p = _find(net, key)
        qty = int((p or {}).get("quantity") or 0)
        if not qty:
            return _err("This position is no longer open.")
        why = _locked_reason(key, qty)
        if why:
            return _err(why)
        if expect is not None and expect != qty:
            return _err(f"The position changed to {qty} since the page showed it. Check and try again.")
        exch, sym, product = _split(key)
        job = {"id": uuid.uuid4().hex[:10], "key": key, "exchange": exch, "symbol": sym, "name": pretty(sym),
               "product": product, "qty": qty, "side": _exit_side(qty), "status": "working",
               "order_id": None, "price": None, "first_price": None, "filled": 0, "avg": None,
               "saved_gtts": [], "saved_orders": [], "ltp": None, "phase": "start",
               "steps": [], "started": now.isoformat(timespec="seconds"), "ended": None}
        BUSY[key] = job["id"]
        JOBS[job["id"]] = job
        _save_jobs()
    threading.Thread(target=run_close, args=(job,), daemon=True).start()
    return jsonify(ok=True, job=job["id"])


@panel.route("/api/positions/jobs")
def api_jobs():
    return jsonify(jobs=_jobs_view())


# ---- the close ------------------------------------------------------------------------------------------
class Stop(RuntimeError):
    """The close stops; what was changed is settled and put back if safe."""


def _step(job, msg):
    job["steps"].append({"t": _now().strftime("%H:%M:%S"), "msg": msg})
    _save_jobs()


def _jobs_view():
    out = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:8]
    hide = ("saved_gtts", "saved_orders")
    return [{k: v for k, v in j.items() if k not in hide} | {"had_gtt": bool(j.get("saved_gtts"))} for j in out]


def _save_jobs():
    try:
        keep = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]
        tmp = _jobs_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, default=str), encoding="utf-8")
        tmp.replace(_jobs_file())
    except OSError:
        pass


def _last(k, oid):
    hist = k.order_history(oid) or [{}]
    return hist[-1]


def _adopt_order(k, job):
    """After a place_order that errored (or a restart before its id was saved): our order on this contract, by tag."""
    if job.get("order_id"):
        return job["order_id"]
    for o in k.orders() or []:
        if (o.get("tag") == TAG and o.get("tradingsymbol") == job["symbol"] and o.get("exchange") == job["exchange"]
                and o.get("transaction_type") == job["side"] and o.get("product") == job["product"]
                and str(o.get("order_timestamp") or "9999") >= job["started"].replace("T", " ")[:19]):
            job["order_id"] = str(o["order_id"])
            _step(job, f"Found order {o['order_id']} at Zerodha by its tag")
            return job["order_id"]
    return None


def _finish_order(k, job):
    """Cancel our order if it is still open and follow it until Zerodha reports it final. Returns True if final."""
    oid = job.get("order_id")
    if not oid:
        return True
    last = _last(k, oid)
    if str(last.get("status", "")).upper() not in FINAL:
        try:
            k.cancel_order(variety="regular", order_id=oid)
            _step(job, "Order cancelled (not filled in time)")
        except Exception as e:  # noqa: BLE001
            _step(job, f"Cancel: {e}")
    for _ in range(10):
        st = str(last.get("status", "")).upper()
        job["filled"] = int(last.get("filled_quantity") or 0)
        job["avg"] = float(last.get("average_price") or 0) or None
        if st in FINAL:
            return True
        _sleep(1)
        last = _last(k, oid)
    st = str(last.get("status", "")).upper()
    job["filled"] = int(last.get("filled_quantity") or 0)
    return st in FINAL


def run_close(job, k=None):
    """The close, steps 1-5 of the module notes."""
    key, exch, sym, side, product = job["key"], job["exchange"], job["symbol"], job["side"], job["product"]
    qkey, orig = f"{exch}:{sym}", job["qty"]
    try:
        if k is None:
            k, why = _kite()
            if k is None:
                raise Stop(why)
        # 1. price first; nothing working on the exit side already
        q = _quote(k, [qkey], tries=3).get(qkey) or {}
        first = _best(q, side)
        job["ltp"] = float(q.get("last_price") or 0) or None
        if not first:
            raise Stop("No price for this contract right now; nothing was changed.")
        orders0 = k.orders()
        working = [o for o in _exit_orders(orders0, exch, sym, side) if o.get("status") != "TRIGGER PENDING"]
        filled0 = _exit_filled(orders0, exch, sym, side, product)
        fno = exch in FNO
        gtts0 = k.get_gtts() if fno else []
        trig0 = {g.get("id") for g in _gtts_for(gtts0, exch, sym, side, product, statuses=("triggered",))}
        if working:
            raise Stop(f"An exit order is already working at Zerodha ({working[0]['order_id']}, {working[0]['status']}): "
                       "nothing was changed. Wait for it or cancel it in Kite.")
        # 2. take the stops off (saved first), then check nothing moved
        gtts = _gtts_for(gtts0, exch, sym, side, product) if fno else []
        sls = [o for o in _exit_orders(orders0, exch, sym, side, product) if o.get("status") == "TRIGGER PENDING"] if fno else []
        job["saved_gtts"] = [{"type": g.get("type"), "trigger_values": (g.get("condition") or {}).get("trigger_values"),
                              "orders": [{kk: o.get(kk) for kk in ("transaction_type", "quantity", "order_type", "product", "price")}
                                         for o in g.get("orders") or []], "id": g.get("id"),
                              "qty": int(((g.get("orders") or [{}])[0]).get("quantity") or 0)} for g in gtts]
        job["saved_orders"] = [{kk: o.get(kk) for kk in ("order_id", "order_type", "price", "trigger_price", "variety", "validity")}
                               | {"qty": int(o.get("pending_quantity") or o.get("quantity") or 0)} for o in sls]
        job["phase"] = "stops_off"
        _save_jobs()
        for g in gtts:
            k.delete_gtt(g["id"])
            _step(job, f"GTT {g['id']} removed (put back if the close does not fill)")
        for o in sls:
            k.cancel_order(variety=o.get("variety") or "regular", order_id=o["order_id"])
            for _ in range(6):        # a cancel can sit in CANCEL PENDING for a moment
                if str(_last(k, o["order_id"]).get("status", "")).upper() in FINAL:
                    break
                _sleep(1)
            _step(job, f"SL order {o['order_id']} cancelled (put back if the close does not fill)")
        _invalidate()
        now_qty = _qty(k, key)
        orders1 = k.orders()
        working = _exit_orders(orders1, exch, sym, side)
        grew = _exit_filled(orders1, exch, sym, side, product) - filled0
        trig1 = {g.get("id") for g in _gtts_for(k.get_gtts(), exch, sym, side, product, statuses=("triggered",))} if fno else set()
        if now_qty != orig or working or grew > 0 or trig1 - trig0:
            raise Stop(f"Something changed while the stops came off (position {now_qty}, {len(working)} exit order(s) "
                       f"working, {grew} filled, {len(trig1 - trig0)} GTT(s) triggered): the close was not sent.")
        # 3. the exit order
        price = first
        job["phase"] = "sending"
        _save_jobs()
        try:
            oid = k.place_order(variety="regular", exchange=exch, tradingsymbol=sym, transaction_type=side, quantity=abs(orig),
                                product=product, order_type="LIMIT", price=price, validity="DAY", tag=TAG)
            job["order_id"] = str(oid)
        except Exception as e:  # noqa: BLE001
            _step(job, f"Zerodha answered with an error: {e}")
            _sleep(2)
            if not _adopt_order(k, job):
                raise Stop(f"The close order was not placed ({e}).") from None
        job.update(price=price, first_price=first, phase="chasing")
        _step(job, f"Order {job['order_id']}: {side} {abs(orig)} LIMIT {price:.2f}")
        _log_action(action="close_sent", key=key, order=job["order_id"], side=side, qty=abs(orig), price=price)
        oid = job["order_id"]
        for i in range(1, CHASE_SECONDS + 1):
            _sleep(1)
            last = _last(k, oid)
            status = str(last.get("status", "")).upper()
            job["filled"] = int(last.get("filled_quantity") or 0)
            if status in FINAL:
                if status != "COMPLETE":
                    _step(job, f"Zerodha {status.lower()}: {last.get('status_message') or ''}".strip())
                break
            if i % REPRICE_EVERY == 0:
                try:
                    new = _best(_quote(k, [qkey]).get(qkey) or {}, side)
                except Exception:  # noqa: BLE001
                    new = None
                if new and new != price and abs(new - first) / first * 100 <= MAX_CHASE_PCT:
                    try:
                        k.modify_order(variety="regular", order_id=oid, price=new)
                        price = new
                        job["price"] = price
                        _step(job, f"Re-priced to {price:.2f}")
                    except Exception as e:  # noqa: BLE001
                        _step(job, f"Re-price failed: {e}")
        _settle(job, k)
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, Stop):
            job["status"] = "failed"
        _step(job, f"Stopped: {e}")
        try:
            if k is not None:
                _settle(job, k, stopped=True)
        except Exception as e2:  # noqa: BLE001
            job["status"] = "failed"
            _step(job, f"Check after the stop failed: {e2}")
            _notify("Close FAILED: check Zerodha now", f"{job['name']}: {e}. Then: {e2}. Open Kite and check the position and its stop.")
    finally:
        job["phase"] = "finished"
        job["ended"] = job["ended"] or _now().isoformat(timespec="seconds")
        with _LOCK:
            BUSY.pop(key, None)
        _invalidate()
        _save_jobs()


def _settle(job, k, stopped=False):
    """Steps 4-5: decide what is open from the order's own fill, check it against the positions, put stops back
    only when that is certain, and alert otherwise."""
    key, exch, sym, side, product, orig = job["key"], job["exchange"], job["symbol"], job["side"], job["product"], job["qty"]
    if job.get("phase") in ("sending", "chasing"):
        _adopt_order(k, job)
    final = _finish_order(k, job)
    filled = int(job.get("filled") or 0)
    expect = (abs(orig) - filled) * (1 if orig > 0 else -1)
    if not final:
        job["status"] = "failed"
        _step(job, "Zerodha has not confirmed the order as finished: nothing put back")
        _notify("Close unclear: check Kite now", f"{job['name']}: order {job.get('order_id')} is not confirmed finished. "
                "No stop was put back. Check the order and the position in Kite.")
        return
    rem = None
    for _ in range(6):                       # Kite's position list can lag the fill by a moment
        rem = _qty(k, key)
        if rem == expect:
            break
        _sleep(1)
    _log_action(action="close_result", key=key, order=job.get("order_id"), filled=filled, avg=job.get("avg"),
                expected=expect, positions=rem)
    if rem != expect:
        job["status"] = "failed"
        _step(job, f"Position shows {rem}, the order says {expect} is open: nothing put back")
        _notify("Close unclear: check Kite now", f"{job['name']}: Kite shows {rem}, the close order says {expect} is open. "
                "No stop was put back. Check the position in Kite.")
        return
    if rem == 0:
        if job["status"] == "working" or not stopped:
            job["status"] = "done"
        avg = job.get("avg")
        _step(job, f"Closed at {avg:.2f}" if avg else "Closed")
        if exch not in FNO:
            return
        for g in _gtts_for(k.get_gtts(), exch, sym, side, product):   # left alone they would open a new position
            try:
                k.delete_gtt(g["id"])
                _step(job, f"Deleted GTT {g['id']} (position is closed)")
            except Exception as e:  # noqa: BLE001
                _notify("Delete this GTT in Kite", f"{job['name']} is closed but GTT {g['id']} is still active: {e}")
        for o in _exit_orders(k.orders(), exch, sym, side, product, skip=job.get("order_id")):
            try:
                k.cancel_order(variety=o.get("variety") or "regular", order_id=o["order_id"])
                _step(job, f"Cancelled pending {o.get('order_type')} order {o['order_id']} (position is closed)")
            except Exception as e:  # noqa: BLE001
                _notify("Cancel this order in Kite", f"{job['name']} is closed but order {o['order_id']} is still pending: {e}")
        return
    if job["status"] == "working":
        job["status"] = "partial" if filled else "not_filled"
    msg = f"{job['name']}: {filled} of {abs(orig)} closed, {rem} still open."
    working = _exit_orders(k.orders(), exch, sym, side)
    if working:
        job["status"] = "failed"
        _step(job, f"Exit order {working[0]['order_id']} still working at Zerodha: stops not put back")
        _notify("Close unclear: check Kite now", msg + f" Exit order {working[0]['order_id']} is still working, so no stop "
                "was put back. Check Kite.")
        return
    lost = _restore(job, k, abs(rem))
    if lost:
        msg += " NO stop at Zerodha: " + "; ".join(lost)
        _notify("NO STOP: act now", msg)
    else:
        if job.get("saved_gtts") or job.get("saved_orders"):
            msg += " The stop is back at Zerodha."
        _notify("Close not completed", msg)


def _restore(job, k, units):
    """Put the saved GTTs / SL orders back for `units`. Returns what could not be put back."""
    exch, sym, side = job["exchange"], job["symbol"], job["side"]
    lost = []
    gtts = job.get("saved_gtts") or []
    if gtts and _gtts_for(k.get_gtts(), exch, sym, side, job["product"]):
        _step(job, "A GTT is already active on this contract: not adding another")
        gtts = []
    sls = job.get("saved_orders") or []
    if sls and any(o.get("status") == "TRIGGER PENDING" for o in _exit_orders(k.orders(), exch, sym, side, job["product"])):
        _step(job, "An SL order is already pending on this contract: not adding another")
        sls = []
    for g in gtts:
        n = min(int(g.get("qty") or units), units)
        def place(g=g):
            try:
                ltp = float((_quote(k, [f"{exch}:{sym}"]).get(f"{exch}:{sym}") or {}).get("last_price") or 0)
            except Exception:  # noqa: BLE001
                ltp = 0
            ltp = ltp or float(job.get("ltp") or 0)
            return k.place_gtt(trigger_type=g["type"], tradingsymbol=sym, exchange=exch, trigger_values=g["trigger_values"],
                               last_price=ltp, orders=[dict(o, quantity=n) for o in g["orders"]])
        try:
            gid = _retry(place)
            gid = gid.get("trigger_id") if isinstance(gid, dict) else gid
            _step(job, f"GTT put back for {n} (id {gid})")
            _log_action(action="gtt_restored", key=job["key"], gtt=gid, qty=n)
        except Exception as e:  # noqa: BLE001
            _step(job, f"GTT could NOT be put back: {e}")
            lost.append(f"GTT {g.get('trigger_values')} ({e})")
    for o in sls:
        n = min(int(o.get("qty") or units), units)
        try:
            oid = _retry(lambda o=o, n=n: k.place_order(variety=o.get("variety") or "regular", exchange=exch, tradingsymbol=sym,
                                                   transaction_type=side, quantity=n, product=job["product"],
                                                   order_type=o.get("order_type") or "SL", price=o.get("price"),
                                                   trigger_price=o.get("trigger_price"), validity=o.get("validity") or "DAY"))
            _step(job, f"SL order put back for {n} (order {oid})")
        except Exception as e:  # noqa: BLE001
            _step(job, f"SL order could NOT be put back: {e}")
            lost.append(f"SL order trigger {o.get('trigger_price')} ({e})")
    return lost


def recover_pending():
    """At start-up: closes that were running when the dashboard stopped are settled (one process only)."""
    if not (BASE / "data" / "positions_jobs.json").exists():
        return []
    try:
        saved = json.loads(_jobs_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = []
    for j in saved or []:            # merged first, so a later save never drops a stuck close
        JOBS.setdefault(j["id"], j)
    if not _take_recovery_lock():
        stuck = [j for j in saved or [] if j.get("status") == "working"]
        if stuck:
            threading.Thread(target=_wait_for_lock, args=(stuck,), daemon=True).start()
        return []
    todo = [j for j in saved or [] if j.get("status") == "working"]
    for j in todo:
        BUSY[j["key"]] = j["id"]
        threading.Thread(target=_recover_one, args=(j,), daemon=True).start()
    return todo


def _take_recovery_lock():
    if _RECOVERY_LOCK:
        return True
    try:
        fh = open(_data_dir() / "positions_jobs.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False                 # another dashboard process owns recovery
    _RECOVERY_LOCK.append(fh)
    return True


def _wait_for_lock(stuck, tries=20):
    """Another process held the lock at start-up: take over when it lets go, or say that nobody checked."""
    for _ in range(tries):
        _sleep(30)
        if _take_recovery_lock():
            try:                      # the other process may have settled them meanwhile: use its latest file
                latest = {x["id"]: x for x in json.loads(_jobs_file().read_text(encoding="utf-8")) or []}
            except (OSError, ValueError):
                latest = {}
            JOBS.update(latest)
            for j in stuck:
                cur = JOBS.get(j["id"], j)
                if cur.get("status") == "working" and cur["key"] not in BUSY:
                    BUSY[cur["key"]] = cur["id"]
                    _recover_one(cur)
            return
    _notify("Close interrupted: check Zerodha now", "A close was running when the dashboard restarted and could not be "
            "checked: " + ", ".join(j.get("name", j["key"]) for j in stuck) + ". Check the position and its stop in Kite.")


def _recover_one(job, k=None):
    try:
        if k is None:
            for _ in range(60):
                k, _why = _kite()
                if k is not None:
                    break
                _sleep(10)
            if k is None:
                raise RuntimeError("no Zerodha login")
        _step(job, "Dashboard restarted during the close: checking")
        if job.get("phase") in ("start", None):
            job["status"] = "not_filled"
            _step(job, "Nothing had been changed at Zerodha")
            return
        _settle(job, k, stopped=True)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        _step(job, f"Recovery failed: {e}")
        _notify("Close interrupted: check Zerodha now", f"{job['name']}: the dashboard restarted during a close and could not check it ({e}).")
    finally:
        job["phase"] = "finished"
        job["ended"] = job["ended"] or _now().isoformat(timespec="seconds")
        BUSY.pop(job["key"], None)
        _save_jobs()


try:
    recover_pending()
except Exception as _e:  # noqa: BLE001 - recovery must never stop the dashboard
    print("Positions: recovery check failed:", _e, flush=True)


# ---- trailing SL ------------------------------------------------------------------------------------
def _trails_file():
    return _data_dir() / "positions_trails.json"


def _trails_rw(change=None):
    """Read the trails ({key: {...}}), or change them under a file lock (several dashboard processes)."""
    f = _trails_file()
    with open(_data_dir() / "positions_trails.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except (OSError, ValueError):
            cur = {}
        if change is not None:
            change(cur)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(cur, indent=1), encoding="utf-8")
            tmp.replace(f)
        return cur


def trail_level(qty, best, by, unit):
    """The SL that trails `best` (the lowest price since the trail was set for a short, the highest for a long)."""
    gap = best * by / 100.0 if unit == "pct" else by
    return _up(best + gap) if qty < 0 else _down(best - gap)


def _closing_anywhere(key):
    """A close running for this position in this process or another one (the jobs file)."""
    if key in BUSY:
        return True
    try:
        jobs = json.loads(_jobs_file().read_text(encoding="utf-8")) if _jobs_file().exists() else []
    except (OSError, ValueError):
        return True                   # cannot tell: do not touch the GTT
    return any(j.get("key") == key and j.get("status") == "working" for j in jobs or [])


def trail_once(k, now=None):
    """One pass: move each trailed GTT SL that its best price allows. Returns lines for the log."""
    out = []
    trails = _trails_rw()
    for key, t in list(trails.items()):
        def drop(why, tell=True):
            _trails_rw(lambda cur: cur.pop(key, None))
            _log_action(action="trail_end", key=key, why=why)
            if tell:
                _notify("Trailing SL ended", f"{pretty(_split(key)[1])}: {why}")
            out.append(f"{key}: trail ended ({why})")
        with _keylock(key):
            if _closing_anywhere(key):
                continue
            net, quotes, gtts, _orders = _fetch(k, force_slow=True)
            p = _find(net, key)
            qty = int((p or {}).get("quantity") or 0)
            exch, sym, product = _split(key)
            if not qty or qty != int(t.get("qty") or 0):
                drop("the position closed or changed size", tell=bool(qty))
                continue
            why = _locked_reason(key, qty)
            if why:
                drop(why)
                continue
            found = _gtts_for(gtts, exch, sym, _exit_side(qty), product)
            if len(found) != 1:
                drop("its GTT is gone (triggered or removed)" if not found else "it has more than one GTT")
                continue
            g = _gtt_view(found[0], qty)
            if not g["sl"] or not g["qty_ok"]:
                drop("its GTT has no SL for the whole position")
                continue
            q = quotes.get(f"{exch}:{sym}") or {}
            ltp = float(q.get("last_price") or 0)
            if ltp <= 0:
                continue
            best = min(float(t["best"]), ltp) if qty < 0 else max(float(t["best"]), ltp)
            new = trail_level(qty, best, float(t["by"]), t.get("unit", "pct"))
            better = new < g["sl"] - TICK / 2 if qty < 0 else new > g["sl"] + TICK / 2
            if better:
                try:
                    ttype, trig, gorders, info = build_gtt(qty, product, ltp, new, g["target"], q)
                    k.modify_gtt(trigger_id=g["id"], trigger_type=ttype, tradingsymbol=sym, exchange=exch,
                                 trigger_values=trig, last_price=ltp, orders=gorders)
                    _log_action(action="gtt_trail", key=key, gtt=g["id"], sl_from=g["sl"], sl_to=new, best=best, ltp=ltp)
                    out.append(f"{key}: SL {g['sl']:.2f} -> {new:.2f} (best {best:.2f})")
                except ValueError as e:
                    out.append(f"{key}: not moved ({e})")
                except Exception as e:  # noqa: BLE001 - tried again at the next pass
                    out.append(f"{key}: Zerodha refused the move ({e})")
                finally:
                    _invalidate()
            if best != float(t["best"]):
                _trails_rw(lambda cur: cur.get(key) is not None and cur[key].update(best=best))
    return out


def _market_open(now):
    return now.weekday() < 5 and (now.replace(hour=9, minute=15, second=0, microsecond=0) <= now
                                  <= now.replace(hour=15, minute=30, second=0, microsecond=0))


def _trail_loop():
    try:
        fh = open(_data_dir() / "positions_trailer.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)        # one dashboard process moves the stops
    except OSError:
        return
    while True:
        _sleep(TRAIL_EVERY)
        try:
            if not _market_open(_now()) or not _trails_rw():
                continue
            k, _why = _kite()
            if k is not None:
                for line in trail_once(k):
                    print("Positions trail:", line, flush=True)
        except Exception as e:  # noqa: BLE001 - the loop must keep running
            print("Positions trail failed:", e, flush=True)


@panel.route("/api/positions/trail", methods=["POST"])
def api_trail():
    d = request.get_json(silent=True) or {}
    key = str(d.get("key", ""))
    if not KEY_OK.match(key):
        return _err("Unknown position")
    unit = "pts" if d.get("unit") == "pts" else "pct"
    try:
        by = float(d.get("by") or 0)
    except (TypeError, ValueError):
        return _err("Trail by must be a number")
    if by < 0 or not math.isfinite(by) or (unit == "pct" and by >= 100):
        return _err("Trail by must be above 0 (and below 100%)")
    if by == 0:
        _trails_rw(lambda cur: cur.pop(key, None))
        _log_action(action="trail_off", key=key)
        return jsonify(ok=True, message="Trailing SL off. The GTT stays where it is.")
    k, why = _kite()
    if k is None:
        return _err(why)
    with _keylock(key):
        if _closing_anywhere(key):
            return _err("A close is running for this position.")
        net, quotes, gtts, _o = _fetch(k, force_slow=True)
        p = _find(net, key)
        qty = int((p or {}).get("quantity") or 0)
        if not qty:
            return _err("This position is no longer open.")
        why = _locked_reason(key, qty)
        if why:
            return _err(why)
        exch, sym, product = _split(key)
        found = _gtts_for(gtts, exch, sym, _exit_side(qty), product)
        g = _gtt_view(found[0], qty) if len(found) == 1 else None
        if not g or not g["sl"]:
            return _err("Set an SL first (Save to Zerodha): the trail moves that GTT's SL.")
        ltp = float((quotes.get(f"{exch}:{sym}") or {}).get("last_price") or p.get("last_price") or 0)
        if ltp <= 0:
            return _err("No last price for this contract right now.")
        rec = {"by": by, "unit": unit, "qty": qty, "best": ltp, "set_at": _now().isoformat(timespec="seconds")}
        _trails_rw(lambda cur: cur.__setitem__(key, rec))
        _log_action(action="trail_on", key=key, **rec)
    lvl = trail_level(qty, ltp, by, unit)
    return jsonify(ok=True, message=f"Trailing SL on: {by:g}{'%' if unit == 'pct' else ' pts'} behind the best price "
                   f"(now {ltp:.2f}, so {lvl:.2f}). The SL ({g['sl']:.2f}) only moves in your favour.")


_TRAILER = []


def _start_trailer():
    """Started with the page's first request (not at import: tests and tools import this module)."""
    with _LOCK:
        if _TRAILER or os.environ.get("POSITIONS_TRAIL", "1") == "0":
            return
        _TRAILER.append(threading.Thread(target=_trail_loop, daemon=True))
    _TRAILER[0].start()


@panel.route("/positions")
def positions_page():
    return Response(PAGE.replace("__SLB__", f"{SL_BUFFER * 100:.0f}").replace("__CHASE__", str(CHASE_SECONDS)),
                    mimetype="text/html")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Positions</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e3e6eb;--accent:#2f6fed;
  --up:#127a43;--down:#c62828;--warn:#a15c00;--warnbg:#fff4e0;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--text:#e7e9ee;--muted:#9aa1ad;--line:#2a2f38;
  --up:#4cc38a;--down:#ef7a7a;--warn:#e0a24a;--warnbg:#2c2414;--chip:#232831;color-scheme:dark}}
*{box-sizing:border-box}
[hidden]{display:none!important}
#list{display:grid;gap:12px}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px;display:grid;gap:12px}
h1{font-size:1.35rem;margin:0}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.small{font-size:.82rem;color:var(--muted)}
.num{font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--up);margin-right:6px;vertical-align:1px}
.dot.stale{background:var(--warn)}.dot.off{background:var(--down)}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.tile .l{font-size:.78rem;color:var(--muted)}.tile .v{font-size:1.15rem;font-weight:650}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
.card,.pos{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.pos{display:grid;gap:10px}
.pos .h{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.nm{font-weight:650;font-size:1.02rem}
.chip{display:inline-block;font-size:.72rem;font-weight:600;padding:2px 7px;border-radius:999px;background:var(--chip);color:var(--muted);margin-right:4px}
.chip.short{color:var(--down)}.chip.long{color:var(--up)}
.pnl{font-size:1.15rem;font-weight:700;text-align:right;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.grid div{font-size:.78rem;color:var(--muted)}.grid b{display:block;font-size:.95rem;color:var(--text);font-weight:600}
.prot{font-size:.88rem;padding:8px 10px;border-radius:8px;background:var(--chip)}
.prot.none{background:var(--warnbg);color:var(--warn);font-weight:600}
.lock{font-size:.85rem;padding:8px 10px;border-radius:8px;background:var(--chip)}
.acts{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{font:600 15px system-ui,-apple-system,sans-serif;padding:11px 12px;border-radius:10px;border:1px solid var(--line);
  background:var(--bg);color:var(--text);min-height:44px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
button.danger{background:#c62828;border-color:#c62828;color:#fff}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.ghost{background:transparent}
.ed,.cf{display:grid;gap:10px;border-top:1px solid var(--line);padding-top:10px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
label{display:grid;gap:4px;font-size:.8rem;color:var(--muted)}
input{font:16px system-ui;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);width:100%}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips button{min-height:32px;padding:4px 10px;font-size:.8rem;border-radius:999px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;justify-self:start}
.seg button{border:0;border-radius:0;min-height:36px;padding:6px 14px;font-size:.85rem;background:transparent}
.seg button.on{background:var(--accent);color:#fff}
.trl{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:end;border-top:1px dashed var(--line);padding-top:10px}
.tin{display:flex;gap:6px}.tin select{font:16px system-ui;padding:9px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text)}
.msg{padding:9px 10px;border-radius:8px;font-size:.88rem}
.msg.err{background:color-mix(in srgb,var(--down) 14%,transparent)}.msg.ok{background:color-mix(in srgb,var(--up) 14%,transparent)}
.cf{border:2px solid var(--warn);border-radius:10px;padding:10px}
.job{font-size:.85rem;border-left:3px solid var(--accent);padding:4px 10px}
.job ol{margin:4px 0 0;padding-left:18px}
details summary{cursor:pointer;font-weight:600;padding:2px 0}
table{width:100%;border-collapse:collapse;font-size:.88rem}
td{padding:6px 2px;border-bottom:1px solid var(--line)}td:last-child{text-align:right}
.empty{text-align:center;padding:26px 12px;color:var(--muted)}
@media (max-width:480px){.grid{grid-template-columns:repeat(2,1fr)}.tile .v{font-size:1rem}}
</style></head><body><div class="wrap">
<div class="top"><h1>Positions</h1><span class="small num" id="asof"><span class="dot off"></span>Connecting…</span></div>
<div class="tiles">
  <div class="tile"><div class="l">Open P&amp;L</div><div class="v num" id="tOpen">–</div></div>
  <div class="tile"><div class="l">Day P&amp;L (incl. closed)</div><div class="v num" id="tDay">–</div></div>
  <div class="tile"><div class="l">No stop at Zerodha</div><div class="v num" id="tUnp">–</div></div>
</div>
<div id="banner"></div>
<div id="list"></div>
<div class="card" id="closedBox" hidden><details id="closedDet"><summary>Closed today</summary><table id="closed"></table></details></div>
<div class="card" id="jobsBox" hidden><details id="jobsDet"><summary>Closes from this page</summary><div id="jobs"></div></details></div>
<p class="small">Live from your Zerodha account. SL and target are one GTT at Zerodha, so they work with this server off and overnight.
When a GTT triggers it sends a LIMIT order (SL limit __SLB__% through the trigger, capped at the circuit): it usually fills, but a fast spike can skip it.
Close sends a LIMIT at the best bid or ask, re-priced every 5 s for __CHASE__ s; the GTT is removed first and put back if it does not fill.
Legs held by a live Algo Lab session are read-only here. P&amp;L is before charges.</p>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const inr=(x,d=0)=>Number(x).toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d});
const px=x=>x==null?"–":Number(x).toFixed(2);
function pl(x){if(x==null)return"–";const v=Math.round(x);return v>0?`<span class="up">▲ +₹${inr(v)}</span>`:v<0?`<span class="down">▼ −₹${inr(-v)}</span>`:`₹0`}
const tick=(x,up)=>{const t=Math.round(x/0.05*1e6)/1e6;return((up?Math.ceil(t):Math.floor(t))*0.05).toFixed(2)};
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{cache:"no-store"});
 let j={};try{j=await r.json()}catch(e){j={error:"HTTP "+r.status}}if(!r.ok||j.error&&body)throw new Error(j.error||("HTTP "+r.status));return j}
let data=null,okAt=0,timer=null;const cards={};

function card(r){
 const el=document.createElement("div");el.className="pos";el.dataset.key=r.key;
 el.innerHTML=`<div class="h"><div><div class="nm"></div><div class="sub small"></div></div><div class="pnl num"></div></div>
 <div class="grid num"><div>Qty<b class="q"></b></div><div>Avg<b class="a"></b></div><div>LTP<b class="l"></b></div><div>Bid / Ask<b class="ba"></b></div></div>
 <div class="prot"></div><div class="pend small" hidden></div><div class="lock" hidden></div><div class="job" hidden></div>
 <div class="acts"><button class="bEdit">SL / target</button><button class="bClose danger">Close</button></div>
 <div class="ed" hidden>
  <div class="seg" role="group" aria-label="Enter as"><button type="button" class="uPx on" data-u="px">₹ price</button><button type="button" class="uPct" data-u="pct">% of avg</button></div>
  <div class="row2"><label><span class="lSl">Stop-loss trigger</span><input class="iSl" type="number" inputmode="decimal" step="0.05" min="0" placeholder="none"></label>
  <label><span class="lTg">Target trigger</span><input class="iTg" type="number" inputmode="decimal" step="0.05" min="0" placeholder="none"></label></div>
  <div class="chips cSl"></div><div class="chips cTg"></div>
  <div class="small hint"></div>
  <div class="acts"><button class="bSave primary">Save to Zerodha</button><button class="bCancel ghost">Cancel</button></div>
  <button class="bRemove ghost" hidden>Remove GTT</button>
  <div class="trl"><label>Trail SL by<span class="tin"><input class="iTr" type="number" inputmode="decimal" step="0.5" min="0" placeholder="off"><select class="iTrU"><option value="pct">%</option><option value="pts">pts</option></select></span></label>
   <button class="bTrail ghost">Trail</button><button class="bTrailOff ghost" hidden>Stop trailing</button></div>
  <div class="small tHint"></div>
  <div class="eMsg"></div>
 </div>
 <div class="cf" hidden><div class="cfText"></div><div class="acts"><button class="bBack ghost">Back</button><button class="bGo danger">Close now</button></div></div>`;
 const q=s=>el.querySelector(s);
 q(".bEdit").onclick=()=>{const r=cur(el);const ed=q(".ed");if(!ed.hidden){ed.hidden=true;return}
  q(".iSl").value=r.gtt&&r.gtt.sl?r.gtt.sl.toFixed(2):"";q(".iTg").value=r.gtt&&r.gtt.target?r.gtt.target.toFixed(2):"";
  setUnit(el,"px");q(".bRemove").hidden=!r.gtt;q(".eMsg").innerHTML="";chips(el,r);hint(el);
  q(".iTr").value=r.trail?r.trail.by:"";q(".iTrU").value=r.trail?r.trail.unit:"pct";q(".bTrailOff").hidden=!r.trail;tHint(el);
  q(".cf").hidden=true;ed.hidden=false;q(".iSl").focus()};
 q(".uPx").onclick=()=>setUnit(el,"px");q(".uPct").onclick=()=>setUnit(el,"pct");
 q(".iTr").oninput=q(".iTrU").onchange=()=>tHint(el);
 q(".bTrail").onclick=()=>trail(el,false);q(".bTrailOff").onclick=()=>trail(el,true);
 q(".bCancel").onclick=()=>{q(".ed").hidden=true};
 q(".iSl").oninput=q(".iTg").oninput=()=>hint(el);
 q(".bSave").onclick=()=>save(el,false);q(".bRemove").onclick=()=>save(el,true);
 q(".bClose").onclick=()=>{const r=cur(el);q(".ed").hidden=true;
  const est=r.exit_side==="SELL"?r.bid:r.ask;
  q(".cfText").innerHTML=`<b>${r.exit_side} ${Math.abs(r.qty)} ${esc(r.name)}</b> · ${esc(r.product)}<br>LIMIT at the best ${r.exit_side==="SELL"?"bid":"ask"}${est?` (now ₹${px(est)})`:""}, re-priced every 5 s for up to __CHASE__ s, never more than 5% from the first price.`+
   (r.gtt?`<br>The GTT is removed first and put back if this does not fill.`:"")+
   (r.pending.length?`<br>${r.pending.length} pending order(s) on this contract are cancelled once it is closed.`:"");
  q(".bGo").disabled=false;q(".bGo").textContent="Close now";q(".cf").hidden=false};
 q(".bBack").onclick=()=>{q(".cf").hidden=true};
 q(".bGo").onclick=async()=>{const r=cur(el);const b=q(".bGo");b.disabled=true;b.textContent="Sending…";
  try{await api("/api/positions/close",{key:r.key,qty:r.qty});q(".cf").hidden=true;load(true)}
  catch(e){q(".cf").hidden=true;q(".job").hidden=false;q(".job").innerHTML=`<div class="msg err">${esc(e.message)}</div>`}};
 return el}
const cur=el=>(data.positions||[]).find(p=>p.key===el.dataset.key)||{};

function chips(el,r){
 const pct=el.dataset.unit==="pct",a=r.avg,s=r.side==="short",set=(cls,sel,list)=>{const c=el.querySelector(cls);
  c.innerHTML=list.map(([t,v])=>`<button type="button" data-v="${pct?pctOf(r,sel===".iSl"?"sl":"tg",parseFloat(v)).toFixed(0):v}">${t}</button>`).join("");
  c.querySelectorAll("button").forEach(b=>b.onclick=()=>{el.querySelector(sel).value=b.dataset.v;hint(el)})};
 if(!a)return;
 set(".cSl",".iSl",s?[["SL 1.5× avg",tick(a*1.5,1)],["2×",tick(a*2,1)],["3×",tick(a*3,1)]]:[["SL −20%",tick(a*.8,0)],["−30%",tick(a*.7,0)],["−50%",tick(a*.5,0)]]);
 set(".cTg",".iTg",s?[["Target 50% of avg",tick(a*.5,0)],["25%",tick(a*.25,0)],["10%",tick(a*.1,0)]]:[["Target +30%",tick(a*1.3,1)],["+50%",tick(a*1.5,1)],["+100%",tick(a*2,1)]])}

// SL / target entered as a price or as % of the average price (a short's SL is above it, its target below)
function setUnit(el,u){const q=s=>el.querySelector(s),r=cur(el),was=el.dataset.unit||"px";if(was!==u){
  for(const [cls,kind] of [[".iSl","sl"],[".iTg","tg"]]){const v=parseFloat(q(cls).value);if(v>0)q(cls).value=u==="pct"?pctOf(r,kind,v).toFixed(1):priceOf(r,kind,v)}}
 el.dataset.unit=u;q(".uPx").classList.toggle("on",u==="px");q(".uPct").classList.toggle("on",u==="pct");
 q(".lSl").textContent=u==="pct"?"Stop-loss, % from avg":"Stop-loss trigger";q(".lTg").textContent=u==="pct"?"Target, % from avg":"Target trigger";
 q(".iSl").step=q(".iTg").step=u==="pct"?"1":"0.05";chips(el,r);hint(el)}
const away=(r,kind)=>(r.side==="short")===(kind==="sl")?1:-1;     // +1: above the average price
const priceOf=(r,kind,pct)=>tick(r.avg*(1+away(r,kind)*pct/100),away(r,kind)>0);
const pctOf=(r,kind,px)=>Math.abs(px/r.avg-1)*100;
function val(el,cls,kind){const v=parseFloat(el.querySelector(cls).value);if(!(v>0))return NaN;return el.dataset.unit==="pct"?parseFloat(priceOf(cur(el),kind,v)):v}
function tHint(el){const r=cur(el),q=s=>el.querySelector(s),by=parseFloat(q(".iTr").value),u=q(".iTrU").value;
 if(!(by>0)){q(".tHint").innerHTML=r.trail?`Trailing ${r.trail.by}${r.trail.unit==="pct"?"%":" pts"} behind the best price (${px(r.trail.best)}).`:"";return}
 const short=r.side==="short",gap=u==="pct"?r.ltp*by/100:by,lvl=short?r.ltp+gap:r.ltp-gap;
 q(".tHint").innerHTML=`The SL follows the ${short?"lowest":"highest"} price from now, ${by}${u==="pct"?"%":" pts"} ${short?"above":"below"} it (at ${px(r.ltp)} that is ${tick(lvl,short)}). It only moves in your favour; checked every 15 s in market hours. Needs an SL saved first.`}
async function trail(el,off){const r=cur(el),q=s=>el.querySelector(s);const b=off?q(".bTrailOff"):q(".bTrail");b.disabled=true;
 try{const j=await api("/api/positions/trail",{key:r.key,by:off?0:parseFloat(q(".iTr").value)||0,unit:q(".iTrU").value});
  q(".eMsg").innerHTML=`<div class="msg ok">${esc(j.message)}</div>`;load(true)}
 catch(e){q(".eMsg").innerHTML=`<div class="msg err">${esc(e.message)}</div>`}finally{b.disabled=false}}

function hint(el){const r=cur(el),q=s=>el.querySelector(s);const sl=val(el,".iSl","sl"),tg=val(el,".iTg","tg");
 const n=Math.abs(r.qty),side=r.exit_side,short=r.side==="short",out=[];
 if(sl>0){let lim=short?Math.min(sl*(1+__SLB__/100),r.upper_circuit||1e9):Math.max(sl*(1-__SLB__/100),r.lower_circuit||0);
  const loss=short?(lim-r.avg)*n:(r.avg-lim)*n;
  out.push(`SL: ${side} ${n} at limit ${tick(lim,short)} if LTP ${short?"rises to":"falls to"} ${sl.toFixed(2)}. Loss from avg about ₹${inr(Math.max(loss,0))}, more if it doesn't fill.`);
  if(short&&r.upper_circuit&&sl>=r.upper_circuit)out.push(`<span class="down">Above the upper circuit ₹${px(r.upper_circuit)}: Zerodha can't fill it.</span>`)}
 if(tg>0){const gain=short?(r.avg-tg)*n:(tg-r.avg)*n;out.push(`Target: ${side} ${n} if LTP ${short?"falls to":"rises to"} ${tg.toFixed(2)}. Gain from avg about ₹${inr(gain)}.`)}
 if(!(sl>0)&&!(tg>0))out.push(r.gtt?"Both empty: Save removes the GTT.":"Enter an SL, a target or both.");
 q(".hint").innerHTML=out.join("<br>")}

async function save(el,remove){const r=cur(el),q=s=>el.querySelector(s);const b=remove?q(".bRemove"):q(".bSave");b.disabled=true;
 const sl=val(el,".iSl","sl"),tg=val(el,".iTg","tg");
 const body={key:r.key,sl:remove||!(sl>0)?null:sl,target:remove||!(tg>0)?null:tg};
 try{const j=await api("/api/positions/protect",body);q(".eMsg").innerHTML=`<div class="msg ok">${esc(j.message)}</div>`;setTimeout(()=>{q(".ed").hidden=true},1800);load(true)}
 catch(e){q(".eMsg").innerHTML=`<div class="msg err">${esc(e.message)}</div>`}finally{b.disabled=false}}

function fill(el,r,jobs){const q=s=>el.querySelector(s);
 q(".nm").textContent=r.name;
 q(".sub").innerHTML=`<span class="chip ${r.side}">${r.side==="short"?"SHORT":"LONG"}</span><span class="chip">${esc(r.product)}</span><span class="chip">${esc(r.exchange)}</span>${r.overnight?'<span class="chip">overnight</span>':""}`;
 q(".pnl").innerHTML=pl(r.pnl);q(".q").textContent=r.qty;q(".a").textContent=px(r.avg);q(".l").textContent=px(r.ltp);
 q(".ba").textContent=`${px(r.bid)} / ${px(r.ask)}`;
 const p=q(".prot"),g=r.gtt;
 if(r.algo){p.className="prot";p.textContent="Stops managed by the live session."}
 else if(g){const parts=[];if(g.sl)parts.push(`SL <b>${px(g.sl)}</b> <span class="small">(limit ${px(g.sl_limit)})</span>`);if(g.target)parts.push(`Target <b>${px(g.target)}</b> <span class="small">(limit ${px(g.target_limit)})</span>`);
  if(r.trail)parts.push(`trailing ${r.trail.by}${r.trail.unit==="pct"?"%":" pts"}`);
  p.className="prot";p.innerHTML=`GTT at Zerodha: ${parts.join(" · ")}`+(g.qty_ok?"":` <span class="warn">· GTT is for ${g.qty}, position is ${Math.abs(r.qty)}: save again to fix</span>`)}
 else if(r.pending.some(o=>o.type==="SL"||o.type==="SL-M")){p.className="prot";p.textContent="SL order at Zerodha (day order, ends at the close)."}
 else{p.className="prot none";p.textContent=r.gtt_ok||r.locked?"No stop at Zerodha":`No stop at Zerodha · ${r.product} can't hold a GTT: close it here or in Kite`}
 const pe=q(".pend");pe.hidden=!r.pending.length;pe.innerHTML="Pending: "+r.pending.map(o=>`${o.side} ${o.qty} ${o.type}${o.trigger?` trg ${px(o.trigger)}`:""}${o.price?` @ ${px(o.price)}`:""} (${esc(o.status.toLowerCase())})`).join(" · ");
 const lk=q(".lock");lk.hidden=!r.locked;lk.innerHTML=r.locked?esc(r.locked)+(r.algo?' <a href="/live">Open Live sessions</a>':""):"";
 const busy=!!r.busy;q(".acts").hidden=!!r.locked;q(".bEdit").hidden=!r.gtt_ok;q(".bEdit").disabled=busy;q(".bClose").disabled=busy;
 if(!r.gtt_ok)q(".acts").style.gridTemplateColumns="1fr";
 if(r.locked){q(".ed").hidden=true;q(".cf").hidden=true}
 const j=jobs.find(x=>x.key===r.key&&(x.status==="working"||Date.now()-Date.parse(x.ended||0)<120000));const jb=q(".job");
 if(j&&!(jb.querySelector(".msg.err")&&!busy)){jb.hidden=false;jb.innerHTML=jobHtml(j)}else if(!busy&&!jb.querySelector(".msg.err"))jb.hidden=true}

const STATUS={working:"Closing…",done:"Closed",partial:"Partly closed",not_filled:"Not filled",failed:"Failed"};
function jobHtml(j){return`<b>${STATUS[j.status]||j.status}</b> ${esc(j.side)} ${Math.abs(j.qty)} ${esc(j.name)}${j.filled?` · filled ${j.filled}${j.avg?` @ ${px(j.avg)}`:""}`:""}<ol class="small">${j.steps.map(s=>`<li>${esc(s.t)} ${esc(s.msg)}</li>`).join("")}</ol>`}

function render(){
 const d=data,list=$("list");
 if(!d.login){$("banner").innerHTML=`<div class="card">${esc(d.error||"Not logged in")} <a href="/login">Log in to Zerodha</a></div>`}
 else if(d.error){$("banner").innerHTML=`<div class="msg err">${esc(d.error)}</div>`}else $("banner").innerHTML="";
 const rows=d.positions||[],jobs=d.jobs||[];
 if(d.positions){$("tOpen").innerHTML=pl(d.open_pnl);$("tDay").innerHTML=pl(d.day_pnl);
  $("tUnp").innerHTML=d.unprotected?`<span class="warn">${d.unprotected}</span>`:"0"}
 const keys=new Set(rows.map(r=>r.key));
 for(const k in cards)if(!keys.has(k)){cards[k].remove();delete cards[k]}
 if(d.positions&&!rows.length){if(!$("emptyMsg"))list.insertAdjacentHTML("afterbegin",'<div class="card empty" id="emptyMsg">No open positions.</div>')}
 else $("emptyMsg")?.remove();
 rows.forEach((r,i)=>{let el=cards[r.key];if(!el){el=cards[r.key]=card(r)}
  if(list.children[i]!==el)list.insertBefore(el,list.children[i]||null);fill(el,r,jobs)});
 const cl=d.closed||[];$("closedBox").hidden=!cl.length;
 $("closed").innerHTML=cl.map(c=>`<tr><td>${esc(c.name)} <span class="chip">${esc(c.product)}</span></td><td class="num">${pl(c.pnl)}</td></tr>`).join("");
 $("jobsBox").hidden=!jobs.length;$("jobs").innerHTML=jobs.map(j=>`<div class="job">${jobHtml(j)}<div class="small">${esc((j.started||"").slice(11,19))}${j.order_id?` · order ${esc(j.order_id)}`:""}</div></div>`).join("")}

function clock(){const a=$("asof");if(!data){return}const age=(Date.now()-okAt)/1000;
 const cls=!data.login?"off":age>6?"stale":"";a.innerHTML=`<span class="dot ${cls}"></span>${data.as_of?"Updated "+esc(data.as_of)+" IST":"Waiting"}${age>6?` · ${Math.round(age)} s ago`:""}`}
async function load(now){clearTimeout(timer);
 try{data=await api("/api/positions");okAt=Date.now();render()}catch(e){if(!data)$("banner").innerHTML=`<div class="msg err">${esc(e.message)}</div>`}
 clock();if(document.hidden)return;
 const fast=data&&data.login&&((data.positions||[]).length||(data.jobs||[]).some(j=>j.status==="working"));
 timer=setTimeout(load,fast?1000:5000)}
document.addEventListener("visibilitychange",()=>{if(!document.hidden)load()});
setInterval(clock,1000);load();
</script></body></html>
"""
