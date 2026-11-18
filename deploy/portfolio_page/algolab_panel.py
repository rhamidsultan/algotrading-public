"""Paper-trading and live-trading pages for the portfolio dashboard.

/paper shows PAPER sessions (simulated orders). Several sessions can run the same strategy with
different settings, each with its own account. /live shows SHADOW and LIVE sessions; at most one
LIVE session (real orders) runs at a time.

The strategies run in the separate Algo Lab engine (its own background service, /opt/algolab).
This page only shows what the engine is doing and passes start / stop requests to it through
small files, so a problem in the engine can never take the portfolio dashboard down.

Added to app.py by algo-trading-platform/deploy/install_server.sh. Sign-in is the dashboard's own.
"""
import csv
import json
import os
import re
import base64
import shutil
import sys
import threading
import time
from html import escape as _esc
from pathlib import Path

from flask import Blueprint, Response, has_request_context, jsonify, redirect, request, send_file, session

ENGINE = Path(os.environ.get("ALGOLAB_DIR", "/opt/algolab"))
RT = ENGINE / "runtime"

panel = Blueprint("paper", __name__)


# ---- who is signed in and what they may see ------------------------------------------------------
# One login by default; users_panel.py replaces these when it loads (several logins, each with
# access to chosen pages).
def current_user():
    return (session.get("user") or "") if has_request_context() else ""


def user_dir(user=None):
    """Folder for the signed-in person's own files (profile, photo)."""
    return _profile_dir()


def path_allowed(path, method="GET"):
    return True


def can(area, level="view"):
    """area: portfolio, us, paper, live, backtests, market_data; level: view or use."""
    return True


def owner_name():
    """The dashboard's own login (config.json dashboard_user): the owner."""
    main = sys.modules.get("__main__")
    cfg = getattr(main, "CFG", None) or _json(Path(__file__).resolve().parent / "config.json", {}) or {}
    return cfg.get("dashboard_user", "admin")


def user_active(user):
    return bool(user) and user == owner_name()


def user_version(user):
    return 0


def push_allowed(user, msg):
    return True


def log_activity(event, user=None, **detail):   # replaced by users_panel
    pass


def amounts_hidden():
    """True when the signed-in login sees the portfolio as percentages only (users_panel)."""
    return False


_USERS_LOADED = False   # set by users_panel


@panel.before_app_request
def _only_owner_without_users():
    """If the logins add-on is missing, only the owner's own sign-in counts (never another login's)."""
    if not _USERS_LOADED and session.get("user") and session.get("user") != owner_name():
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify(error="not signed in"), 401
        return redirect("/signin")
    return None


def _json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _age(path):
    try:
        return time.time() - float(path.read_text())
    except (OSError, ValueError):
        return 1e9


def _write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _closed_legs(b):
    """Legs already closed in a cycle that is still open (a roll, one side stopped out), shown under its open positions
    at quantity 0 the way the broker's positions page shows them, with what each booked."""
    open_groups = {str(p.get("group")) for p in b.get("positions") or []}
    out = []
    for t in b.get("trades") or []:
        if str(t.get("group")) in open_groups:
            out.append({"Contract": t["contract"], "Qty": 0, "Side": t["side"], "Was": t["qty"] * (-1 if t["side"] == "SHORT" else 1),
                        "Entry price": t["entry_price"], "Exit price": t["exit_price"], "P&L": t["gross_pnl"],
                        "Charges": t["costs"], "Net": t["net_pnl"], "Closed": str(t["exit_time"])[:16].replace("T", " "),
                        "Reason": t.get("reason", "")})
    return sorted(out, key=lambda r: r["Closed"], reverse=True)


def _cycles(trades):
    groups = {}
    for t in trades:
        g = groups.setdefault(t["group"], {"cycle": t["group"], "entry": t["entry_time"], "exit": t["exit_time"],
                                           "legs": 0, "net": 0.0, "charges": 0.0, "reason": ""})
        g["entry"], g["exit"] = min(g["entry"], t["entry_time"]), max(g["exit"], t["exit_time"])
        g["legs"] += 1
        g["net"] += t["net_pnl"]
        g["charges"] += t["costs"]
        g["reason"] = t["reason"]
    return sorted(groups.values(), key=lambda g: g["entry"], reverse=True)


def _calendar(trades):
    """Closed cycles by the day they closed (IST date of the last leg's exit): net P&L after charges, and the
    cycles themselves, for the Paper/Live calendar."""
    days = {}
    for c in _cycles(trades):
        d = str(c["exit"])[:10]
        x = days.setdefault(d, {"date": d, "net": 0.0, "cycles": []})
        x["net"] += c["net"]
        x["cycles"].append({"cycle": c["cycle"], "net": round(c["net"], 2), "legs": c["legs"], "reason": c["reason"],
                            "opened": str(c["entry"])[:16].replace("T", " "), "closed": str(c["exit"])[:16].replace("T", " ")})
    for x in days.values():
        x["net"] = round(x["net"], 2)
    return [days[d] for d in sorted(days)]


def _pnl_history(path, capital, limit=300):
    rows = []
    try:
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((r["time"][:16], round(float(r["equity"]) - capital, 2)))
    except (OSError, ValueError, KeyError):
        return []
    step = max(1, len(rows) // limit)
    out = rows[::step]
    if rows and out[-1] != rows[-1]:
        out.append(rows[-1])
    return out


def _cycle_window(b):
    """(start, end, label) of the cycle the P&L chart's "Current cycle" view shows: the open cycle (from its first
    entry, including legs already closed in it), else the last closed cycle. None when nothing has traded."""
    pos = b.get("positions") or []
    trades = b.get("trades") or []
    if pos:
        groups = {str(p.get("group")) for p in pos}
        starts = [str(p.get("opened_at")) for p in pos if p.get("opened_at")]
        starts += [str(t["entry_time"]) for t in trades if str(t.get("group")) in groups]
        return (min(starts)[:16].replace("T", " "), None, "Current cycle") if starts else None
    cyc = _cycles(trades)
    if not cyc:
        return None
    c = cyc[0]
    return str(c["entry"])[:16].replace("T", " "), str(c["exit"])[:16].replace("T", " "), "Last cycle"


def _cycle_curve(path, capital, b, limit=400):
    """The P&L of the current (or last) cycle alone: account history from the cycle's first entry to its exit (or now),
    measured from the account just before the entry, so it starts at 0. {"points", "hi", "lo", "label", "from", "to"}."""
    w = _cycle_window(b)
    if not w:
        return None
    start, end, label = w
    rows = []
    try:
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((r["time"][:16].replace("T", " "), float(r["equity"]) - capital))
    except (OSError, ValueError, KeyError):
        return None
    before = [v for t, v in rows if t < start]
    base = before[-1] if before else 0.0
    pts = [(t, round(v - base, 2)) for t, v in rows if t >= start and (end is None or t <= end)]
    if not pts:
        return None
    hi, lo = max(pts, key=lambda p: p[1]), min(pts, key=lambda p: p[1])
    step = max(1, len(pts) // limit)
    thin = pts[::step]
    for k in (hi, lo, pts[-1]):
        if k not in thin:
            thin.append(k)
    thin.sort()
    return {"points": thin, "hi": hi, "lo": lo, "label": label, "from": start, "to": end or pts[-1][0]}


def _pnl_analysis(path, capital, trades, limit=400):
    """From a session's account history (one point a minute): the chart points (thinned, keeping the high and
    low), the highest and lowest P&L, P&L by day (day change, close, intraday high/low, trades, charges) and
    key metrics including the largest fall from a peak (max drawdown)."""
    rows = []
    try:
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((r["time"][:16], round(float(r["equity"]) - capital, 2)))
    except (OSError, ValueError, KeyError):
        rows = []
    out = {"points": [], "hi": None, "lo": None, "daily": [], "metrics": {}}
    if not rows:
        return out
    hi = max(rows, key=lambda r: r[1])
    lo = min(rows, key=lambda r: r[1])
    step = max(1, len(rows) // limit)
    pts = rows[::step]
    keep = {hi, lo, rows[-1]}
    pts = sorted(set(pts) | keep)
    out.update(points=pts, hi=list(hi), lo=list(lo))

    days, order = {}, []
    for t, v in rows:
        d = t[:10]
        if d not in days:
            days[d] = {"date": d, "open": v, "close": v, "high": v, "low": v, "high_at": t[11:16], "low_at": t[11:16]}
            order.append(d)
        x = days[d]
        x["close"] = v
        if v > x["high"]:
            x["high"], x["high_at"] = v, t[11:16]
        if v < x["low"]:
            x["low"], x["low_at"] = v, t[11:16]
    per_day = {}
    for tr in trades:
        d = str(tr.get("exit_time", ""))[:10]
        p = per_day.setdefault(d, {"trades": 0, "charges": 0.0, "booked": 0.0})
        p["trades"] += 1
        p["charges"] += tr.get("costs", 0) or 0
        p["booked"] += tr.get("net_pnl", 0) or 0
    prev = 0.0
    for d in order:
        x = days[d]
        x["day"] = round(x["close"] - prev, 2)
        x["cumulative"] = x["close"]
        prev = x["close"]
        p = per_day.get(d, {})
        x.update(trades=p.get("trades", 0), charges=round(p.get("charges", 0.0), 2), booked=round(p.get("booked", 0.0), 2))
        out["daily"].append(x)

    peak, peak_at, dd, dd_from, dd_to = rows[0][1], rows[0][0], 0.0, "", ""
    for t, v in rows:
        if v > peak:
            peak, peak_at = v, t
        if v - peak < dd:
            dd, dd_from, dd_to = v - peak, peak_at, t
    day_vals = [x["day"] for x in out["daily"]]
    cyc = _cycles(trades)
    wins = [c["net"] for c in cyc if c["net"] > 0]
    losses = [c["net"] for c in cyc if c["net"] <= 0]
    best = max(out["daily"], key=lambda x: x["day"])
    worst = min(out["daily"], key=lambda x: x["day"])
    out["metrics"] = {
        "total_pnl": rows[-1][1], "highest_pnl": hi[1], "highest_at": hi[0], "lowest_pnl": lo[1], "lowest_at": lo[0],
        "max_drawdown": round(dd, 2), "drawdown_from": dd_from, "drawdown_to": dd_to,
        "return_pct": round(rows[-1][1] / capital * 100, 2) if capital else None,
        "days": len(day_vals), "winning_days": sum(1 for v in day_vals if v > 0),
        "losing_days": sum(1 for v in day_vals if v < 0),
        "best_day": best["day"], "best_day_date": best["date"], "worst_day": worst["day"], "worst_day_date": worst["date"],
        "average_day": round(sum(day_vals) / len(day_vals), 2) if day_vals else 0,
        "cycles": len(cyc), "winning_cycles": len(wins),
        "cycle_win_rate_pct": round(len(wins) / len(cyc) * 100, 1) if cyc else None,
        "average_win": round(sum(wins) / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "charges": round(sum(t.get("costs", 0) or 0 for t in trades), 2),
        "first_point": rows[0][0], "last_point": rows[-1][0], "capital": capital,
    }
    return out


# ---- payoff of the open legs ---------------------------------------------------------------------
# Self-contained (the dashboard does not import the engine). Same conventions as algolab/pricing.py:
# trading-time years (1/252 a day, 30% of a day overnight), expiry at 15:30, r 6.5%, dividend 1.2%.
import math as _m
from datetime import date as _date, datetime as _dt, timedelta as _td

_R, _Q = 0.065, 0.012


def _ncdf(x):
    return 0.5 * (1.0 + _m.erf(x / _m.sqrt(2.0)))


def _busdays(a, b):
    """Weekdays d with a <= d < b."""
    n, d = 0, a
    while d < b:
        n += d.weekday() < 5
        d += _td(days=1)
    return n


def _years(now, expiry):
    if now.date() > expiry:
        return 0.0
    close = _dt.combine(now.date(), _dt.min.time()).replace(hour=15, minute=30)
    left = min(max((close - now).total_seconds() / 60.0, 0.0), 375.0)
    today = 0.7 * left / 375.0 if now.weekday() < 5 else 0.0
    return (today + _busdays(now.date() + _td(days=1), expiry + _td(days=1))) / 252.0


def _bs(spot, strike, t, sigma, right):
    if t <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if right == "CE" else max(strike - spot, 0.0)
    vt = sigma * _m.sqrt(t)
    d1 = (_m.log(spot / strike) + (_R - _Q + 0.5 * sigma * sigma) * t) / vt
    d2 = d1 - vt
    if right == "CE":
        return spot * _m.exp(-_Q * t) * _ncdf(d1) - strike * _m.exp(-_R * t) * _ncdf(d2)
    return strike * _m.exp(-_R * t) * _ncdf(-d2) - spot * _m.exp(-_Q * t) * _ncdf(-d1)


def _iv(price, spot, strike, t, right):
    if t <= 0 or price <= 0:
        return None
    lo, hi = 0.01, 3.0
    if not (_bs(spot, strike, t, lo, right) <= price <= _bs(spot, strike, t, hi, right)):
        return None
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if _bs(spot, strike, t, mid, right) < price else (lo, mid)
    return 0.5 * (lo + hi)


def _payoff(s, points=161):
    """P&L of a session's open legs across index levels: at the first expiry (later legs valued at their
    implied volatility then) and today. Includes the open legs' entry charges, like the Open tile."""
    b = s.get("broker") or {}
    pos = b.get("positions") or []
    spot = float(s.get("spot") or 0)
    if not pos or spot <= 0:
        return None
    if len({p["contract"]["underlying"] for p in pos}) > 1:
        return None
    now = _dt.fromisoformat(str(s["time"])).replace(tzinfo=None)
    ltp = {p.get("Contract"): p.get("LTP") for p in s.get("positions") or []}
    fallback = (float(s["vix"]) / 100.0) if s.get("vix") else 0.13
    legs = []
    for p in pos:
        c = p["contract"]
        exp = _date.fromisoformat(c["expiry"])
        label = f"{c['underlying']} {exp.strftime('%d %b %y')} {c['strike']} {c['right']}"
        last = ltp.get(label)
        t = _years(now, exp)
        iv = _iv(float(last), spot, float(c["strike"]), t, c["right"]) if last else None
        legs.append({"label": label, "strike": float(c["strike"]), "right": c["right"], "qty": int(p["qty"]),
                     "avg": float(p["avg_price"]), "last": last, "expiry": c["expiry"], "t": t,
                     "iv": round(iv if iv else fallback, 4), "iv_from": "price" if iv else "VIX"})
    charges = sum(float(p.get("entry_costs") or 0) for p in pos)
    first = min(_date.fromisoformat(l["expiry"]) for l in legs)
    t_first = _years(now, first)

    def pnl(x, shift_t=0.0, iv_add=0.0):
        tot = -charges
        for l in legs:
            v = _bs(x, l["strike"], max(l["t"] - shift_t, 0.0), max(l["iv"] + iv_add, 0.01), l["right"])
            tot += (v - l["avg"]) * l["qty"]
        return tot

    strikes = [l["strike"] for l in legs]
    lo = min(spot * 0.94, min(strikes) - 0.02 * spot)
    hi = max(spot * 1.06, max(strikes) + 0.02 * spot)
    xs = sorted({round(lo + (hi - lo) * i / (points - 1), 1) for i in range(points)} | set(strikes) | {spot})
    points = len(xs)
    at_exp = [round(pnl(x, t_first), 0) for x in xs]
    today = [round(pnl(x), 0) for x in xs]
    breakevens = []
    for i in range(1, points):
        a, c = at_exp[i - 1], at_exp[i]
        if (a < 0 <= c) or (a >= 0 > c):
            breakevens.append(round(xs[i - 1] + (xs[i] - xs[i - 1]) * (0 - a) / ((c - a) or 1), 0))
    h = spot * 0.001
    edge = lambda ys: ys[1] - ys[0], lambda ys: ys[-1] - ys[-2]
    return {
        "underlying": pos[0]["contract"]["underlying"], "spot": spot, "time": str(s["time"]),
        "first_expiry": first.isoformat(), "days_to_first": round(t_first * 252, 2), "charges": round(charges, 2),
        "x": [round(v, 1) for v in xs], "expiry": at_exp, "today": today,
        "now": round(pnl(spot), 0), "breakevens": breakevens,
        "max_profit": max(at_exp), "max_loss": min(at_exp),
        "loss_open_below": edge[0](at_exp) > 0 and at_exp[0] < 0, "loss_open_above": edge[1](at_exp) < 0 and at_exp[-1] < 0,
        "delta": round((pnl(spot + h) - pnl(spot - h)) / (2 * h), 1),
        "theta": round(pnl(spot, 1 / 252) - pnl(spot), 0),
        "vega": round(pnl(spot, 0.0, 0.01) - pnl(spot), 0),
        "legs": legs,
    }


SESS = RT / "sessions"


def _slots():
    """Session folders (one per strategy). Before the engine's first start after the update, the old
    single-session files are shown as one session."""
    out = []
    if SESS.is_dir():
        for d in sorted(SESS.iterdir()):
            if d.is_dir() and ((d / "state.json").exists() or (d / "desired.json").exists()):
                out.append({"id": d.name, "state": d / "state.json", "equity": d / "equity.csv",
                            "desired": d / "desired.json", "beat": d / "heartbeat", "status": d / "status.txt",
                            "stop": d / "STOP"})
    if not out:
        legacy = _json(RT / "paper_state.json") or _json(RT / "paper_desired.json")
        if legacy and legacy.get("strategy"):
            out.append({"id": legacy["strategy"], "state": RT / "paper_state.json", "equity": RT / "paper_equity.csv",
                        "desired": RT / "paper_desired.json", "beat": RT / "heartbeat",
                        "status": RT / "service_status.txt", "stop": RT / "STOP"})
    return out


def _slot(sid):
    return next((x for x in _slots() if x["id"] == sid), None)


def _mode(x):
    d = _json(x["desired"]) or _json(x["state"]) or {}
    return d.get("mode", "paper")


RISK_DEFAULTS = RT / "risk_defaults.json"     # Orders & stops saved per strategy (owner): {strategy: {key: value}}


def _catalog():
    """Strategies and their settings (written by the engine), with the Orders & stops defaults saved for each."""
    cat = _json(RT / "strategies.json", []) or []
    saved = _json(RISK_DEFAULTS, {}) or {}
    for c in cat:
        mine = saved.get(c.get("name")) or {}
        if mine:
            for p in c.get("params", []):
                k = str(p.get("key", ""))
                if k.startswith("risk.") and k[5:] in mine:
                    p["default"] = mine[k[5:]]
    return cat


def _split_risk(params):
    """("risk.<key>" settings from the pages) -> (strategy settings, Orders & stops settings)."""
    own, risk = {}, {}
    for k, v in (params or {}).items():
        (risk.__setitem__(k[5:], v) if str(k).startswith("risk.") else own.__setitem__(k, v))
    return own, risk


@panel.route("/api/risk-defaults", methods=["POST"])
def api_risk_defaults():
    """Save a strategy's Orders & stops as its default for new sessions, backtests and sweeps (owner)."""
    b = request.get_json(silent=True) or {}
    name = str(b.get("strategy") or "")
    cat = {c["name"]: c for c in _json(RT / "strategies.json", []) or []}
    if name not in cat:
        return jsonify(error="Unknown strategy."), 400
    keys = {str(p["key"])[5:]: p for p in cat[name]["params"] if str(p.get("key", "")).startswith("risk.")}
    clean = {}
    for k, v in (b.get("risk") or {}).items():
        p = keys.get(k)
        if p is None:
            continue
        try:
            clean[k] = (v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on")) if p["type"] == "bool" else \
                {"int": int, "float": float}.get(p["type"], str)(float(v) if p["type"] == "int" else v)
        except (TypeError, ValueError):
            return jsonify(error=f"'{p['label']}' needs a number."), 400
        if p.get("choices") and clean[k] not in p["choices"]:
            return jsonify(error=f"'{p['label']}' must be one of {', '.join(p['choices'])}."), 400
        if isinstance(clean[k], (int, float)) and not isinstance(clean[k], bool) and clean[k] < 0:
            return jsonify(error=f"'{p['label']}' cannot be negative."), 400
    saved = _json(RISK_DEFAULTS, {}) or {}
    saved[name] = clean
    _write(RISK_DEFAULTS, saved)
    return jsonify(ok=True, saved=clean)


# ---- several LIVE sessions at once ----
LIVE_SETTINGS = RT / "live_settings.json"    # {"max_sessions": n}: how many LIVE sessions may run at the same time
MAX_LIVE_DEFAULT, MAX_LIVE_TOP = 3, 10


def _max_live():
    try:
        n = int((_json(LIVE_SETTINGS) or {}).get("max_sessions", MAX_LIVE_DEFAULT))
    except (TypeError, ValueError):
        return MAX_LIVE_DEFAULT
    return max(1, min(MAX_LIVE_TOP, n))


def _live_running(exclude=""):
    """LIVE sessions that are running or waiting to start (a Stop not yet finished still counts)."""
    return [x["id"] for x in _slots()
            if x["id"] != exclude and _mode(x) == "live" and (x["desired"].exists() or _age(x["beat"]) < 15)]


@panel.route("/api/live/settings", methods=["GET", "POST"])
def api_live_settings():
    """GET: the LIVE session limit and how many run. POST (owner): {"max_sessions": n}, 1–10."""
    if request.method == "POST":
        if (current_user() or owner_name()) != owner_name():
            return jsonify(error="Only the owner can change the LIVE session limit."), 403
        try:
            n = int((request.get_json(silent=True) or {}).get("max_sessions"))
        except (TypeError, ValueError):
            return jsonify(error="Enter a whole number."), 400
        if not 1 <= n <= MAX_LIVE_TOP:
            return jsonify(error=f"Allow between 1 and {MAX_LIVE_TOP} LIVE sessions."), 400
        try:
            _write(LIVE_SETTINGS, {**(_json(LIVE_SETTINGS) or {}), "max_sessions": n})
        except OSError as e:
            return jsonify(error=f"Could not save: {e}"), 500
    return jsonify(max_sessions=_max_live(), running=_live_running(),
                   is_owner=(current_user() or owner_name()) == owner_name())


@panel.route("/api/live/stop-all", methods=["POST"])
def api_live_stop_all():
    """Stop every LIVE session: each squares off its own legs (at the next market update if prices are needed) and stops."""
    if not can("live", "use"):
        return jsonify(error="Not allowed."), 403
    ids = _live_running()
    for sid in ids:
        (SESS / sid / "KILL").write_text("kill")
    try:
        log_activity("live_stop_all", sessions=ids)
    except Exception:  # noqa: BLE001
        pass
    return jsonify(ok=True, stopped=ids)


# ---- presets (kept by Algo Studio, studio/): backtests and sessions record the one they were started with ----
PRESETS = RT / "presets.json"           # {strategy: {preset id: {name, params, source, created, updated, version}}}


def _presets(name=None):
    allp = _json(PRESETS, {}) or {}
    return allp.get(name, {}) if name else allp


def _preset_ref(strategy, ref):
    """{"id", "name"} of a saved preset of this strategy, from what a page sent (an id, or {"id": …}); else None."""
    pid = ref.get("id") if isinstance(ref, dict) else ref
    p = _presets(strategy).get(str(pid or ""))
    return {"id": str(pid), "name": p["name"], "version": p.get("version", 1)} if p else None


def _session_view(x):
    s = _json(x["state"])
    desired = _json(x["desired"])
    running = _age(x["beat"]) < 15
    info = desired or s or {}
    out = {"id": x["id"], "running": running, "desired": desired, "waiting": "", "session": None, "equity": [],
           "strategy": info.get("strategy", x["id"]), "label": info.get("label", ""), "mode": info.get("mode", "paper"),
           "stopped_because": "", "killing": (x["state"].parent / "KILL").exists(),
           "started_by": (_json(x["state"].parent / "started_by.json", {}) or {}).get("user", "")
           if x["state"].parent != RT else ""}
    try:
        reason = x["status"].read_text(encoding="utf-8").strip()
    except OSError:
        reason = ""
    if desired and not running:
        out["waiting"] = "" if reason in ("", "starting") else reason
    if not desired and reason.startswith("Stopped:"):
        out["stopped_because"] = reason[len("Stopped:"):].strip()
    if s:
        b = s["broker"]
        entry_charges = sum(p.get("entry_costs", 0) for p in b["positions"])   # already paid on open legs
        out["session"] = {
            "strategy": s["strategy"], "params": s["params"], "feed": s["feed"], "status": s.get("status"),
            "mode": s.get("mode", "paper"), "live": s.get("live"), "risk": s.get("risk"), "preset": s.get("preset"),
            "time": s["time"], "spot": s["spot"], "vix": s.get("vix"), "equity": s["equity"],
            "capital": b["capital"], "total": s["equity"] - b["capital"],
            "booked": sum(t["net_pnl"] for t in b["trades"]),          # closed trades, after their charges
            "open": s["unrealized"] - entry_charges,                    # open legs now, after entry charges
            "charges": b["total_costs"], "positions": s["positions"], "cycles": _cycles(b["trades"])[:60],
            "log": s.get("logs", [])[-60:][::-1], "calendar": _calendar(b["trades"]),
            "closed_legs": _closed_legs(b),
        }
        an = _pnl_analysis(x["equity"], b["capital"], b["trades"])
        out["equity"] = an["points"]
        out["pnl"] = {"hi": an["hi"], "lo": an["lo"], "daily": an["daily"][-120:], "metrics": an["metrics"]}
        try:
            out["pnl"]["cycle"] = _cycle_curve(x["equity"], b["capital"], b)
        except (KeyError, TypeError, ValueError):
            out["pnl"]["cycle"] = None
        try:
            out["payoff"] = _payoff(s)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            out["payoff"] = None
    return out


def _page(kind, view):
    return Response(PAGE.replace("__KIND__", kind).replace("__VIEW__", view), mimetype="text/html")


@panel.route("/paper")
def page():
    return _page("paper", "list")


@panel.route("/paper/new")
def page_new():
    return _page("paper", "new")


@panel.route("/paper/overview")
def page_overview():
    return _page("paper", "overview")


@panel.route("/live/overview")
def page_live_overview():
    return _page("live", "overview")


@panel.route("/live")
def page_live():
    return _page("live", "list")


@panel.route("/live/new")
def page_live_new():
    return _page("live", "new")


@panel.route("/api/paper")
def api_state():
    kind = request.args.get("kind")
    views = [_session_view(x) for x in _slots() if can("paper" if _mode(x) == "paper" else "live")]
    if kind == "paper":
        views = [v for v in views if v["mode"] == "paper"]
    elif kind == "live":
        views = [v for v in views if v["mode"] != "paper"]
    feed = _json(RT / "feed_status.json")
    if feed is not None:
        try:
            feed["age"] = round(time.time() - (RT / "feed_status.json").stat().st_mtime, 1)
        except OSError:
            pass
    return jsonify({
        "feed": feed,
        "margins": _margins({v["id"] for v in views}),
        "installed": (RT / "strategies.json").exists(),
        "service": _age(RT / "service_heartbeat") < 45,
        "catalog": _catalog(),
        "sessions": views,
    })


@panel.route("/api/paper/ticks")
def api_ticks():
    """The latest prices and P&L of each running session (written by the engine every step, runtime/sessions/<id>/tick.json),
    for the page's 1-second refresh. Small: no history, no log."""
    kind = request.args.get("kind")
    out = {}
    for x in _slots():
        mode = _mode(x)
        if (kind == "paper" and mode != "paper") or (kind == "live" and mode == "paper"):
            continue
        if not can("paper" if mode == "paper" else "live") or _age(x["beat"]) >= 15:
            continue
        t = _json(x["state"].parent / "tick.json")
        if t:
            out[x["id"]] = t
    return jsonify(sessions=out)


@panel.route("/api/paper/cycle")
def api_cycle_legs():
    """The closed positions of one cycle (Paper/Live › Closed cycles, tap a cycle): each leg's contract, side, quantity,
    entry and exit, charges and net. Asked for when a cycle is opened, so the page's 3-second refresh stays small."""
    x = _slot(request.args.get("id", ""))
    if not x or not can("paper" if _mode(x) == "paper" else "live"):
        return jsonify(error="No such session."), 404
    cycle = request.args.get("cycle", "")
    trades = ((_json(x["state"]) or {}).get("broker") or {}).get("trades") or []
    legs = sorted((t for t in trades if str(t.get("group")) == cycle), key=lambda t: (str(t.get("exit_time")), str(t.get("contract"))))
    keys = ("contract", "side", "qty", "entry_time", "exit_time", "entry_price", "exit_price", "gross_pnl", "costs", "net_pnl", "reason")
    return jsonify(cycle=cycle, legs=[{k: t.get(k) for k in keys} for t in legs])


def _margins(visible=None):
    """Zerodha's margin per strategy (default settings) and per session's open legs (runtime/margins.json,
    written by the engine's margin check every 30 minutes in market hours)."""
    m = _json(RT / "margins.json")
    if not m:
        return None
    keep = ("margin", "unit", "net_credit", "without_hedge", "legs", "error")
    return {"time": m.get("time"), "error": m.get("error", ""), "lot_size": m.get("lot_size"),
            "requested": (RT / "margins_request").exists(),
            "strategies": {k: {kk: v.get(kk) for kk in keep if kk in v} for k, v in (m.get("strategies") or {}).items()},
            "sessions": {k: v["margin"] for k, v in (m.get("sessions") or {}).items()
                         if "margin" in v and (visible is None or k in visible)}}


@panel.route("/api/paper/margins", methods=["POST"])
def api_margins_refresh():
    """Ask the engine's margin check to run now (it looks every 5 minutes)."""
    try:
        RT.mkdir(parents=True, exist_ok=True)
        (RT / "margins_request").write_text(time.strftime("%Y-%m-%d %H:%M"))
    except OSError as e:
        return jsonify(error=f"Could not ask the engine: {e}"), 500
    return jsonify(ok=True)


def _new_id(strategy):
    sid, n = strategy, 1
    while (SESS / sid).exists():
        n += 1
        sid = f"{strategy}-{n}"
    return sid


_METRIC_LABELS = [("total_pnl", "Total P&L (Rs, after charges)"), ("return_pct", "Return on capital (%)"),
                  ("capital", "Capital (Rs)"), ("highest_pnl", "Highest P&L (Rs)"), ("highest_at", "Highest at"),
                  ("lowest_pnl", "Lowest P&L (Rs)"), ("lowest_at", "Lowest at"), ("max_drawdown", "Max drawdown (Rs)"),
                  ("drawdown_from", "Drawdown from"), ("drawdown_to", "Drawdown to"), ("days", "Trading days"),
                  ("winning_days", "Winning days"), ("losing_days", "Losing days"), ("best_day", "Best day (Rs)"),
                  ("best_day_date", "Best day date"), ("worst_day", "Worst day (Rs)"), ("worst_day_date", "Worst day date"),
                  ("average_day", "Average day (Rs)"), ("cycles", "Closed cycles"), ("winning_cycles", "Winning cycles"),
                  ("cycle_win_rate_pct", "Cycle win rate (%)"), ("average_win", "Average winning cycle (Rs)"),
                  ("average_loss", "Average losing cycle (Rs)"), ("charges", "Charges paid (Rs)"),
                  ("first_point", "History from"), ("last_point", "History to")]


@panel.route("/api/paper/metrics.csv")
def api_metrics_csv():
    """Key metrics and P&L by day: one session (?id=...) or a summary of every session (?kind=paper|live)."""
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    sid = request.args.get("id", "")
    if sid:
        x = _slot(sid)
        s = _json(x["state"]) if x else None
        if not s or not can("paper" if s.get("mode", "paper") == "paper" else "live"):
            return jsonify(error="No such session."), 404
        b = s["broker"]
        an = _pnl_analysis(x["equity"], b["capital"], b["trades"], limit=10)
        m = an["metrics"]
        w.writerow(["Session", sid])
        w.writerow(["Strategy", s.get("strategy")])
        w.writerow(["Mode", s.get("mode", "paper")])
        w.writerow(["Settings", "; ".join(f"{k}={v}" for k, v in (s.get("params") or {}).items())])
        w.writerow([])
        w.writerow(["Key metric", "Value"])
        for k, label in _METRIC_LABELS:
            w.writerow([label, m.get(k, "")])
        w.writerow([])
        w.writerow(["Date", "Day P&L", "Cumulative P&L", "Day high", "High at", "Day low", "Low at",
                    "Trades closed", "Booked that day", "Charges"])
        for d in an["daily"]:
            w.writerow([d["date"], d["day"], d["cumulative"], d["high"], d["high_at"], d["low"], d["low_at"],
                        d["trades"], d["booked"], d["charges"]])
        name = f"{sid}-metrics-{time.strftime('%Y%m%d')}.csv"
    else:
        kind = request.args.get("kind", "paper")
        w.writerow(["Session", "Strategy", "Mode", "Name"] + [label for _, label in _METRIC_LABELS])
        for x in _slots():
            s = _json(x["state"])
            if not s or (kind == "paper") != (s.get("mode", "paper") == "paper") or not can("paper" if kind == "paper" else "live"):
                continue
            b = s["broker"]
            m = _pnl_analysis(x["equity"], b["capital"], b["trades"], limit=10)["metrics"]
            w.writerow([x["id"], s.get("strategy"), s.get("mode", "paper"), s.get("label", "")] + [m.get(k, "") for k, _ in _METRIC_LABELS])
        name = f"{kind}-sessions-metrics-{time.strftime('%Y%m%d')}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@panel.route("/api/paper/start", methods=["POST"])
def api_start():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Expected JSON."), 400
    catalog = {c["name"]: c for c in _catalog()}
    strat = catalog.get(body.get("strategy"))
    if not strat:
        return jsonify(error="Unknown strategy."), 400
    if body.get("feed") not in ("kite", "sim"):
        return jsonify(error="Unknown market feed."), 400
    params = {}
    for p in strat["params"]:
        if p["key"] not in (body.get("params") or {}):
            continue
        v = body["params"][p["key"]]
        try:
            params[p["key"]] = {"int": int, "float": float, "bool": bool}.get(p["type"], str)(v)
        except (TypeError, ValueError):
            return jsonify(error=f"'{p['label']}' needs a {p['type']} value."), 400
    for p in strat["params"]:
        if p.get("choices") and p["key"] in params and params[p["key"]] not in p["choices"]:
            return jsonify(error=f"'{p['label']}' must be one of {', '.join(map(str, p['choices']))}."), 400
    params, risk = _split_risk(params)
    if not risk and isinstance(body.get("risk"), dict):        # Resume: the session's saved Orders & stops
        risk = {k: v for k, v in body["risk"].items() if any(str(p["key"]) == "risk." + k for p in strat["params"])}
    want = {"strategy": strat["name"], "params": params, "feed": body["feed"]}
    if risk:
        want["risk"] = risk
    ref = _preset_ref(strat["name"], body.get("preset"))
    if ref:
        want["preset"] = ref
    label = re.sub(r"\s+", " ", str(body.get("label") or "")).strip()[:40]
    if label:
        want["label"] = label
    mode = body.get("mode", "paper")
    if mode not in ("paper", "shadow", "live"):
        return jsonify(error="Unknown mode."), 400
    if mode != "paper":
        if body["feed"] != "kite":
            return jsonify(error="Shadow and live trading need Zerodha live prices."), 400
        raw = body.get("live") or {}
        try:
            live = {"max_lots": int(raw.get("max_lots", 1)), "daily_loss_limit": float(raw.get("daily_loss_limit", 0)),
                    "max_orders_per_day": int(raw.get("max_orders_per_day", 40)),
                    "protect_mult": float(raw.get("protect_mult", 3)),
                    # per session: place an order above max lots with a warning instead of stopping (algolab/live.py)
                    "allow_over_max_lots": raw.get("allow_over_max_lots") in (True, "true", "1", 1, "on")}
        except (TypeError, ValueError):
            return jsonify(error="Check the live-trading limits: they must be numbers."), 400
        if live["max_lots"] < 1 or live["max_orders_per_day"] < 1:
            return jsonify(error="Lots and orders per day must be at least 1."), 400
        if mode == "live":
            if not strat.get("live_ready"):
                return jsonify(error="This strategy is not enabled for live orders yet; shadow mode works."), 400
            if live["daily_loss_limit"] <= 0:
                return jsonify(error="Set a daily loss limit for live trading."), 400
            if str(body.get("confirm", "")).strip().upper() != "LIVE":
                return jsonify(error="Type LIVE to confirm real orders."), 400
        want.update(mode=mode, live=live)
    sid = str(body.get("id") or "")
    if sid:   # resume (or change the settings of) an existing session
        x = _slot(sid)
        if not x:
            return jsonify(error="No such session."), 404
        before = _json(x["desired"]) or _json(x["state"]) or {}
        if before.get("strategy", sid) != strat["name"]:
            return jsonify(error="That session runs a different strategy."), 400
        if before.get("mode", "paper") != mode:
            return jsonify(error="A session keeps its mode: start a new session for another mode."), 400
        if not label and before.get("label"):
            want["label"] = before["label"]
    else:
        sid = _new_id(strat["name"])
    if mode == "live":
        others = _live_running(exclude=sid)
        cap = _max_live()
        if len(others) >= cap:
            return jsonify(error=f"{len(others)} LIVE session{'s' if len(others) != 1 else ''} already running "
                                 f"({', '.join(others)}); the limit is {cap}. Stop one first, or raise the limit on the Live page."), 400
    d = SESS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "STOP").unlink(missing_ok=True)
    (d / "status.txt").unlink(missing_ok=True)
    _write(d / "desired.json", want)
    try:
        _write(d / "started_by.json", {"user": current_user(), "at": time.strftime("%Y-%m-%d %H:%M")})
    except OSError:
        pass
    return jsonify(ok=True, id=sid)


@panel.route("/api/paper/kill", methods=["POST"])
def api_kill():
    x = _target()
    if not x:
        return jsonify(error="No such session."), 404
    if not (x["desired"].exists() or _age(x["beat"]) < 15):
        return jsonify(error="The session is not running."), 400
    (x["state"].parent / "KILL").write_text("kill")
    return jsonify(ok=True)


def _target():
    body = request.get_json(silent=True) or {}
    return _slot(str(body.get("id", "")))


@panel.route("/api/paper/stop", methods=["POST"])
def api_stop():
    x = _target()
    if not x:
        return jsonify(error="No such session."), 404
    x["desired"].unlink(missing_ok=True)
    x["stop"].write_text("stop")
    return jsonify(ok=True)


@panel.route("/api/paper/reset", methods=["POST"])
def api_reset():
    x = _target()
    if not x:
        return jsonify(error="No such session."), 404
    if _age(x["beat"]) < 15 or x["desired"].exists():
        return jsonify(error="Stop the session first."), 400
    for f in (x["state"], x["equity"]):
        f.unlink(missing_ok=True)
    return jsonify(ok=True)


@panel.route("/api/paper/delete", methods=["POST"])
def api_delete():
    sid = str((request.get_json(silent=True) or {}).get("id", ""))
    d = SESS / sid
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", sid) or not d.is_dir():
        return jsonify(error="No such session."), 404
    if _age(d / "heartbeat") < 15 or (d / "desired.json").exists():
        return jsonify(error="Stop the session first."), 400
    state = _json(d / "state.json") or {}
    if state.get("mode") == "live" and state.get("positions"):
        return jsonify(error="This live session still has open positions in its account."), 400
    shutil.rmtree(d, ignore_errors=True)
    return jsonify(ok=True)


# ---- Market data page: what the engine has stored ----------------------------------------------
# NIFTY / India VIX bars (data/spot), recorded option prices (data/options/NIFTY/<expiry>/<strike><CE|PE>.csv),
# NSE closing prices (data/eod/days) and the daily recording's log. Read-only; standard library only
# (the dashboard's Python has no pandas).
DATA = ENGINE / "data"
OPT = DATA / "options" / "NIFTY"
_cache = {}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTRACT = re.compile(r"^(\d+)(CE|PE)\.csv$")


def _cached(path, fn):
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), fn.__name__)
    hit = _cache.get(key)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    val = fn(path)
    _cache[key] = ((st.st_mtime, st.st_size), val)
    return val


def _edges(path):
    """(header, first data row, last data row, number of data rows) of a CSV without loading it. Files over
    1 MB are not read through: their row count is estimated from the size (marked by a float)."""
    size = path.stat().st_size
    with path.open("rb") as f:
        header = f.readline().decode("utf-8", "replace").strip()
        first = f.readline().decode("utf-8", "replace").strip()
        head_len = f.tell()
        if size <= 1 << 20:
            rows = (1 if first else 0)
            for chunk in iter(lambda: f.read(1 << 20), b""):
                rows += chunk.count(b"\n")
        else:
            rows = None
        f.seek(max(0, size - 4096))
        tail = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    last = tail[-1].strip() if tail else first
    if last == header:
        return header, "", "", 0
    if rows is None:
        avg = max(1.0, (sum(len(x) + 1 for x in tail[1:]) / max(1, len(tail) - 1)) if len(tail) > 1 else len(first) + 1)
        rows = float(round((size - head_len) / avg) + 1)
    return header, first, last, rows


def _spot_days(path):
    """One pass over an index file: bars per day and each day's last close and VIX."""
    per_day, last = {}, {}
    with path.open(encoding="utf-8") as f:
        cols = next(f, "").strip().split(",")
        ci, vi = (cols.index("close") if "close" in cols else 4), (cols.index("vix") if "vix" in cols else None)
        for line in f:
            d = line[:10]
            per_day[d] = per_day.get(d, 0) + 1
            last[d] = line
    daily = {}
    for d, line in last.items():
        p = line.strip().split(",")
        try:
            daily[d] = (float(p[ci]), float(p[vi]) if vi is not None and len(p) > vi and p[vi] not in ("", "nan") else None)
        except (ValueError, IndexError):
            pass
    return {"per_day": per_day, "daily": daily, "has_vix": vi is not None}


def _spot_info(path):
    header, first, last, rows = _edges(path)
    cols = header.split(",")
    days = _cached(path, _spot_days) or {"per_day": {}}
    per_day = days["per_day"]
    lastv = dict(zip(cols, last.split(","))) if last else {}
    order = sorted(per_day)
    full = max(per_day.values()) if per_day else 0
    exact = sum(per_day.values())
    name = path.stem
    return {"name": name, "rows": exact, "from": first[:16], "to": last[:16], "days": len(order),
            "close": lastv.get("close"), "vix": lastv.get("vix"), "has_vix": "vix" in cols,
            "interval": "1 minute" if "1minute" in name or name.endswith("_minute") else "5 minutes",
            "underlying": _spot_underlying(name),
            "short_days": [[d, per_day[d]] for d in order[-60:] if per_day[d] < 0.9 * full][-8:],
            "size_mb": round(path.stat().st_size / 1e6, 2)}


_UNDERLYINGS = ("BANKNIFTY", "SENSEX", "NIFTY")   # longest names first: BANKNIFTY also contains NIFTY


def _spot_underlying(name):
    n = name.upper()
    for u in _UNDERLYINGS:
        if re.search(rf"(^|_){u}_", n):
            return u
    return ""


_SET_RE = re.compile(r"(NIFTY|BANKNIFTY|SENSEX)_minute")   # one 1-minute set per index (algolab/datasets.py)


def _spot_files(u=None):
    """The index price sets the screens show: <INDEX>_minute.csv (TrueData history + Zerodha's daily bars). Other
    files in data/spot (the 5-minute bars, old copies) stay out of sight; data/archive is never shown."""
    d = DATA / "spot"
    if not d.is_dir():
        return []
    files = [f for f in sorted(d.glob("*.csv")) if _SET_RE.fullmatch(f.stem)]
    if u:
        files = [f for f in files if _spot_underlying(f.stem) == u]
    return files


def _sec1_days():
    """1-second bars kept on the server (algolab/sec1.py): [{"day", "instruments", "mb"}], newest last."""
    root = DATA / "sec1"
    out = []
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name):
                continue
            files = list(p.glob("*.csv"))
            idx = _cached(p / "NIFTY.csv", _sec1_scan) if (p / "NIFTY.csv").exists() else None
            out.append({"day": p.name, "instruments": len(files),
                        "mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
                        "coverage": idx["coverage"] if idx else 0, "gaps": idx["gaps"] if idx else []})
    return out


def _sec1_keep():
    """Trading days of 1-second bars kept: the dashboard's setting (runtime/sec1_settings.json), else config.yaml, else 5."""
    try:
        over = json.loads((ENGINE / "runtime" / "sec1_settings.json").read_text(encoding="utf-8")) or {}
        if over.get("keep_trading_days"):
            return max(1, int(over["keep_trading_days"]))
    except (OSError, ValueError, TypeError):
        pass
    try:
        import yaml
        cfg = yaml.safe_load((ENGINE / "config.yaml").read_text(encoding="utf-8")) or {}
        return max(1, int((cfg.get("sec1") or {}).get("keep_trading_days", 5)))
    except Exception:  # noqa: BLE001
        return 5


def _opt_dir(u):
    u = (u or "NIFTY").upper()
    return DATA / "options" / u if re.fullmatch(r"[A-Z]+", u) else DATA / "options" / "NIFTY"


def _underlyings():
    base = DATA / "options"
    have = sorted(p.name for p in base.iterdir() if p.is_dir() and re.fullmatch(r"[A-Z]+", p.name)) if base.is_dir() else []
    order = ["NIFTY", "BANKNIFTY", "SENSEX"]
    return sorted(set(have) | {u for u in order if any(_spot_underlying(f.stem) == u for f in _spot_files())},
                  key=lambda u: (order.index(u) if u in order else 9, u)) or ["NIFTY"]


def _contract_info(path):
    _, first, last, rows = _edges(path)
    parts = last.split(",")
    return {"rows": rows, "from": first[:16], "to": last[:16], "last": parts[1] if len(parts) > 1 else "",
            "quotes": len(parts) > 2 and parts[2] != ""}


def _expiries(u="NIFTY"):
    opt = _opt_dir(u)
    if not opt.is_dir():
        return []
    out = []
    for d in sorted((x for x in opt.iterdir() if x.is_dir() and _DAY.match(x.name)), reverse=True):
        files = [(f, _CONTRACT.match(f.name)) for f in d.iterdir()]
        files = [(f, m) for f, m in files if m]
        infos = [(int(m.group(1)), m.group(2), _cached(f, _contract_info)) for f, m in files]
        infos = [x for x in infos if x[2] and x[2]["rows"]]
        if not infos:
            continue
        strikes = sorted({k for k, _, _ in infos})
        froms = [i["from"] for _, _, i in infos if i["from"]]
        out.append({"expiry": d.name, "contracts": len(infos), "ce": sum(1 for _, r, _ in infos if r == "CE"),
                    "pe": sum(1 for _, r, _ in infos if r == "PE"), "lo": strikes[0], "hi": strikes[-1],
                    "rows": int(sum(i["rows"] for _, _, i in infos)),
                    "approx": any(isinstance(i["rows"], float) for _, _, i in infos),
                    "from": min(froms) if froms else "", "to": max(i["to"] for _, _, i in infos),
                    "live_quotes": sum(1 for _, _, i in infos if i["quotes"])})
    return out


def _coverage(u):
    """Per trading day: index bars stored, and option contracts with prices that day (from each contract file's
    first and last price; every weekday in between counts)."""
    def build():
        from datetime import date as _d, timedelta as _t
        days = {}
        for f in _spot_files(u)[:1]:
            for d, n in (_cached(f, _spot_days) or {"per_day": {}})["per_day"].items():
                days.setdefault(d, [0, 0])[0] = n
        opt = _opt_dir(u)
        if opt.is_dir():
            for e in opt.iterdir():
                if not (e.is_dir() and _DAY.match(e.name)):
                    continue
                for f in e.iterdir():
                    if not _CONTRACT.match(f.name):
                        continue
                    i = _cached(f, _contract_info)
                    if not i or not i["from"]:
                        continue
                    a, b = _d.fromisoformat(i["from"][:10]), _d.fromisoformat(i["to"][:10])
                    while a <= b:
                        if a.weekday() < 5 or a.isoformat() in days:
                            days.setdefault(a.isoformat(), [0, 0])[1] += 1
                        a += _t(days=1)
        return [[d] + days[d] for d in sorted(days)]
    key = ("coverage", u)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    val = build()
    _cache[key] = (time.time(), val)
    return val


def _disk():
    """Space used by the stored data, by folder (refreshed every 10 minutes)."""
    hit = _cache.get("disk")
    if hit and time.time() - hit[0] < 600:
        return hit[1]

    def size(p):
        tot = 0
        for root, _, files in os.walk(p):
            for n in files:
                try:
                    tot += os.stat(os.path.join(root, n)).st_size
                except OSError:
                    pass
        return tot
    parts = []
    if DATA.is_dir():
        for p in sorted(DATA.iterdir()):
            if p.is_dir():
                if p.name == "options":
                    parts += [{"name": f"Options {q.name}", "bytes": size(q)} for q in sorted(p.iterdir()) if q.is_dir()]
                else:
                    parts.append({"name": {"spot": "Index bars", "truedata": "TrueData raw copies", "eod": "NSE closing prices",
                                           "review": "Data reviews"}.get(p.name, p.name), "bytes": size(p)})
    try:
        st = os.statvfs(str(DATA if DATA.is_dir() else ENGINE))
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except (OSError, AttributeError):
        free = total = None
    val = {"parts": [x for x in parts if x["bytes"]], "free": free, "total": total}
    _cache["disk"] = (time.time(), val)
    return val


def _next_recording():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    t = now.replace(hour=15, minute=45, second=0, microsecond=0)
    if now >= t:
        t += timedelta(days=1)
    while t.weekday() >= 5:
        t += timedelta(days=1)
    return t.strftime("%a %d %b, 15:45 IST")


@panel.route("/market-data")
def market_data_page():
    return Response(DATA_PAGE, mimetype="text/html")


# ---- Data page: look inside the stored 1-second bars (one day, one instrument, candles of any length) ----------
_SEC1_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_SEC1_NAME = re.compile(r"NIFTY|INDIAVIX|\d{4}-\d{2}-\d{2}_\d+(CE|PE)")
_SEC1_EVERY = (1, 5, 30, 60, 300, 1800)
_SEC1_OPEN, _SEC1_CLOSE = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60      # the session the bars cover (seconds of the day)
_SEC1_MAX_CANDLES = 6000
_SEC1_GAP_MIN = 5            # a stretch of this many minutes or more without a tick is shown as a gap


def _sec1_scan(path):
    """One stored file: {"rows", "minutes" with a tick between 09:15 and 15:30, "last" close, "gaps" of 5+ minutes}."""
    rows, mins, last = 0, set(), None
    try:
        with path.open(encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                rows += 1
                t = line[11:19]
                if len(t) == 8 and t[2] == ":":
                    sec = int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])
                    if _SEC1_OPEN <= sec < _SEC1_CLOSE:
                        mins.add((sec - _SEC1_OPEN) // 60)
                parts = line.split(",", 5)
                if len(parts) > 4:
                    last = parts[4]
    except OSError:
        pass
    total = (_SEC1_CLOSE - _SEC1_OPEN) // 60
    gaps, run = [], None
    for m in range(total + 1):
        if m < total and m not in mins:
            run = m if run is None else run
        elif run is not None:
            if m - run >= _SEC1_GAP_MIN:
                gaps.append([_hhmm(_SEC1_OPEN + run * 60), _hhmm(_SEC1_OPEN + m * 60)])
            run = None
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = None
    return {"rows": rows, "minutes": len(mins), "coverage": round(len(mins) / total * 100, 1), "gaps": gaps, "last": last}


def _hhmm(sec):
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def _sec1_used(day):
    """File names of the options the sessions traded that day (from their saved fills)."""
    from datetime import datetime as _d
    out = set()
    root = ENGINE / "runtime" / "sessions"
    for st in (_json(p) for p in (root.glob("*/state.json") if root.is_dir() else [])):
        for f in ((st or {}).get("broker") or {}).get("fills", []) or []:
            if not str(f.get("time", "")).startswith(day):
                continue
            parts = str(f.get("contract", "")).split()        # NIFTY 03 Nov 26 22650 PE
            try:
                exp = _d.strptime(" ".join(parts[1:4]), "%d %b %y").date()
                out.add(f"{exp:%Y-%m-%d}_{int(float(parts[4]))}{parts[5]}")
            except (ValueError, IndexError):
                continue
    return out


def _sec1_instruments(day):
    """Stored files of one day: index and VIX, then every option recorded that day (strike, right, expiry, rows,
    share of the session with ticks, whether a session traded it)."""
    folder = DATA / "sec1" / day
    used = _sec1_used(day)
    out = []
    for f in sorted(folder.glob("*.csv")) if folder.is_dir() else []:
        if not _SEC1_NAME.fullmatch(f.stem):
            continue
        info = _cached(f, _sec1_scan) or {"rows": 0, "coverage": 0, "gaps": [], "last": None}
        base = {"name": f.stem, "rows": info["rows"], "coverage": info["coverage"]}
        if f.stem in ("NIFTY", "INDIAVIX"):
            out.append({**base, "kind": "index", "last": info["last"]})
        else:
            e, rest = f.stem.split("_")
            out.append({**base, "kind": "option", "expiry": e, "strike": int(rest[:-2]), "right": rest[-2:],
                        "in_use": f.stem in used})
    return out


@panel.route("/api/market-data/sec1/instruments")
def sec1_instruments():
    day = request.args.get("day") or ""
    if not _SEC1_DAY.fullmatch(day) or not (DATA / "sec1" / day).is_dir():
        return jsonify(error="No 1-second bars for that day."), 404
    inst = _sec1_instruments(day)
    nifty = next((i for i in inst if i["name"] == "NIFTY"), None)
    strikes = sorted({i["strike"] for i in inst if i["kind"] == "option"})
    atm = min(strikes, key=lambda k: abs(k - nifty["last"])) if strikes and nifty and nifty.get("last") else None
    return jsonify(day=day, instruments=inst, atm=atm)


@panel.route("/api/market-data/sec1/candles")
def sec1_candles():
    """Candles of one stored 1-second file, `every` seconds long: open, high, low, close, last bid / ask,
    last volume and open interest, and the ticks in the candle."""
    day, name = request.args.get("day") or "", request.args.get("name") or ""
    try:
        every = int(request.args.get("every") or 1)
    except ValueError:
        every = 0
    if not _SEC1_DAY.fullmatch(day) or not _SEC1_NAME.fullmatch(name) or every not in _SEC1_EVERY:
        return jsonify(error="Pick a day, an instrument and a candle length."), 400
    def _secs(v, default):
        m = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", v or "")
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0) if m else default
    win_lo, win_hi = _secs(request.args.get("from"), 0), _secs(request.args.get("to"), 86400)
    path = DATA / "sec1" / day / f"{name}.csv"
    if not path.is_file():
        return jsonify(error="Not stored."), 404
    groups: dict = {}
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                t = _dt.fromisoformat(r["time"])
                close = float(r["close"])
            except (KeyError, TypeError, ValueError):
                continue
            secs = t.hour * 3600 + t.minute * 60 + t.second
            if not win_lo <= secs < win_hi:
                continue
            key = (secs // every) * every
            hi, lo = r.get("high") or close, r.get("low") or close
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"o": float(r.get("open") or close), "h": float(hi), "l": float(lo),
                                   "c": close, "bid": None, "ask": None, "vol": None, "oi": None, "ticks": 0}
            g["h"], g["l"], g["c"] = max(g["h"], float(hi)), min(g["l"], float(lo)), close
            for k, col in (("bid", "bid"), ("ask", "ask"), ("vol", "volume"), ("oi", "oi")):
                if r.get(col) not in (None, ""):
                    g[k] = float(r[col])
            g["ticks"] += int(float(r.get("ticks") or 0))
    candles = []
    for key in sorted(groups):
        g = groups[key]
        candles.append([f"{key // 3600:02d}:{key % 3600 // 60:02d}:{key % 60:02d}", g["o"], g["h"], g["l"], g["c"],
                        g["bid"], g["ask"], g["vol"], g["oi"], g["ticks"]])
    if len(candles) > _SEC1_MAX_CANDLES:      # e.g. 1-second candles over a whole day: zoom in first
        return jsonify(day=day, name=name, every=every, candles=[], total=len(candles), too_many=True,
                       limit=_SEC1_MAX_CANDLES)
    return jsonify(day=day, name=name, every=every, candles=candles, total=len(candles))


@panel.route("/api/market-data/summary")
def market_data_summary():
    u = (request.args.get("u") or "NIFTY").upper()
    spot = [_cached(f, _spot_info) for f in _spot_files()]
    eod_days = sorted(p.name[:10] for p in (DATA / "eod" / "days").glob("*.csv.gz")) if (DATA / "eod" / "days").is_dir() else []
    smile = DATA / "eod" / "NIFTY_smile.csv.gz"
    try:
        log = (DATA / "collect_log.txt").read_text(encoding="utf-8").splitlines()[-15:][::-1]
    except OSError:
        log = []
    return jsonify(installed=DATA.is_dir(), underlying=u, underlyings=_underlyings(), spot=[s for s in spot if s],
                   expiries=_expiries(u),
                   eod={"days": len(eod_days), "from": eod_days[0] if eod_days else "", "to": eod_days[-1] if eod_days else "",
                        "smile": time.strftime("%Y-%m-%d %H:%M", time.localtime(smile.stat().st_mtime)) if smile.exists() else ""},
                   log=log, next_recording=_next_recording(), sec1={"days": _sec1_days(), "keep": _sec1_keep()})


@panel.route("/api/market-data/coverage")
def market_data_coverage():
    u = (request.args.get("u") or "NIFTY").upper()
    if u not in _underlyings():
        return jsonify(error="No such underlying."), 404
    return jsonify(underlying=u, days=_coverage(u))


@panel.route("/api/market-data/disk")
def market_data_disk():
    return jsonify(_disk())


def _thin(rows, limit):
    step = max(1, len(rows) // limit)
    out = rows[::step]
    if rows and out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def _tail_rows(path, days):
    """Bars of the last `days` days, reading only the end of the file."""
    size = path.stat().st_size
    want = days * 400 * 80 + 4096
    with path.open("rb") as f:
        header = f.readline().decode().strip().split(",")
        f.seek(max(f.tell(), size - want))
        if f.tell() > len(",".join(header)) + 1:
            f.readline()   # drop a partial line
        lines = f.read().decode("utf-8", "replace").splitlines()
    ci = header.index("close") if "close" in header else 4
    vi = header.index("vix") if "vix" in header else None
    rows = []
    for line in lines:
        p = line.split(",")
        if len(p) > ci and p[0][:4].isdigit():
            rows.append((p[0][:16], p[ci], p[vi] if vi is not None and len(p) > vi else None))
    keep = set(sorted({r[0][:10] for r in rows})[-days:])
    return [r for r in rows if r[0][:10] in keep]


@panel.route("/api/market-data/spot")
def market_data_spot():
    name = request.args.get("dataset") or ""
    u = (request.args.get("u") or "NIFTY").upper()
    days = max(1, min(int(request.args.get("days", 5) or 5), 5000))
    if not name:
        files = _spot_files(u)
        if not files:
            return jsonify(error="No index bars stored for " + u), 404
        name = files[0].stem
    path = DATA / "spot" / f"{name}.csv"
    if not re.fullmatch(r"[A-Za-z0-9_]+", name) or not path.exists():
        return jsonify(error="No such data set."), 404
    num = lambda x: float(x) if x not in (None, "", "nan") else None  # noqa: E731
    if days > 15:   # one point a day (the day's last bar) for long ranges
        daily = (_cached(path, _spot_days) or {"daily": {}})["daily"]
        pts = [[d, c, v] for d, (c, v) in sorted(daily.items())[-days:]]
        return jsonify(dataset=name, points=_thin(pts, 1500), daily=True)
    rows = _thin(_tail_rows(path, days), 1500)
    return jsonify(dataset=name, points=[[t, num(c), num(v)] for t, c, v in rows], daily=False)


def _contract_path():
    expiry, strike, right = request.args.get("expiry", ""), request.args.get("strike", ""), request.args.get("right", "")
    if not _DAY.match(expiry) or not strike.isdigit() or right not in ("CE", "PE"):
        return None
    p = _opt_dir(request.args.get("u")) / expiry / f"{int(strike)}{right}.csv"
    return p if p.exists() else None


@panel.route("/api/market-data/chain")
def market_data_chain():
    expiry, u = request.args.get("expiry", ""), (request.args.get("u") or "NIFTY").upper()
    d = _opt_dir(u) / expiry
    if not _DAY.match(expiry) or not d.is_dir():
        return jsonify(error="No such expiry."), 404
    chain = {}
    for f in d.iterdir():
        m = _CONTRACT.match(f.name)
        if not m:
            continue
        info = _cached(f, _contract_info)
        if info and info["rows"]:
            chain.setdefault(int(m.group(1)), {})[m.group(2)] = info
    spot = None
    latest = max((i["to"] for s in chain.values() for i in s.values() if i["to"]), default="")
    for s in _spot_files(u):
        info = _cached(s, _spot_info)
        daily = (_cached(s, _spot_days) or {"daily": {}})["daily"]
        day = latest[:10]
        if day and day in daily:   # the index on the day of the chain's last prices
            spot = {"close": daily[day][0], "time": day + " close"}
            if info and info.get("to", "")[:10] == day:
                spot = {"close": float(info["close"]), "time": info["to"]}
            break
        if info and info.get("close"):
            spot = {"close": float(info["close"]), "time": info["to"]}
            break
    return jsonify(expiry=expiry, underlying=u, spot=spot, strikes=[{"strike": k, **chain[k]} for k in sorted(chain)])


@panel.route("/api/market-data/series")
def market_data_series():
    p = _contract_path()
    if p is None:
        return jsonify(error="No recorded prices for that contract."), 404
    with p.open(encoding="utf-8") as f:
        rows = [(r["time"][:16], r.get("last"), r.get("bid"), r.get("ask")) for r in csv.DictReader(f)]
    rows = sorted(rows)
    num = lambda x: float(x) if x not in (None, "") else None  # noqa: E731
    return jsonify(rows=len(rows), points=[[t, num(a), num(b), num(c)] for t, a, b, c in _thin(rows, 2000)])


@panel.route("/api/market-data/download")
def market_data_download():
    p = _contract_path()
    if p is None:
        return jsonify(error="No recorded prices for that contract."), 404
    u = _opt_dir(request.args.get("u")).name
    name = f"{u}_{request.args['expiry']}_{p.name}"
    return Response(p.read_bytes(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


DATA_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market data · Portfolio</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed;--warn:#a06a1a;--vix:#b7791f;
  --c0:#ebedf0;--c1:#c6dbfb;--c2:#8db6f6;--c3:#4f8bef;--c4:#2f6fed;--spotonly:#d7dbe2}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff;--warn:#e0b34a;--vix:#e0b34a;
  --c0:#20242d;--c1:#1d3460;--c2:#28508f;--c3:#3a6fc4;--c4:#6c9bff;--spotonly:#3a3f4b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:20px;margin:0}
h2{font-size:15px;margin:0;color:var(--text);font-weight:650}
.status{font-size:13px;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.big{font-size:20px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.sub{font-size:13px;margin-top:2px;color:var(--muted);font-variant-numeric:tabular-nums}
.block{margin-top:12px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}
td.px{cursor:pointer;color:var(--accent)}
td.none{color:var(--muted)}
td.atm{font-weight:700;color:var(--accent)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--up);margin-left:5px;vertical-align:middle}
.seg{display:flex;gap:6px;flex-wrap:wrap}
.seg button{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13px;cursor:pointer}
.seg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tabs button{font:inherit;font-weight:600;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:8px 16px;cursor:pointer}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
select{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text)}
.chartbox{height:280px;position:relative;margin-top:10px}
.note{color:var(--muted);font-size:12px;margin-top:8px}
.empty{color:var(--muted);font-size:14px;padding:6px 0}
.log{font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);white-space:pre-wrap;margin:0;max-height:220px;overflow:auto}
.log .bad{color:var(--down)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
a{color:var(--accent)}
.hide{display:none}
details{margin-top:12px}
summary{cursor:pointer;font-weight:650}
/* coverage calendar: one column per week, Mon-Fri down */
.cal{--sz:13px;display:flex;gap:3px;overflow-x:auto;padding:22px 2px 6px;position:relative}
.cal .wk{display:grid;grid-template-rows:repeat(5,var(--sz));gap:3px;position:relative}
.cal .wk .m{position:absolute;top:-18px;left:0;font-size:12px;color:var(--muted);white-space:nowrap}
.cal .c{width:var(--sz);height:var(--sz);border-radius:3px;background:var(--c0)}
.cal .c.s{background:var(--spotonly)}.cal .c.l1{background:var(--c1)}.cal .c.l2{background:var(--c2)}.cal .c.l3{background:var(--c3)}.cal .c.l4{background:var(--c4)}
.cal .c.short{box-shadow:inset 0 0 0 2px var(--warn)}
.cal .c.sel{outline:2px solid var(--text);outline-offset:1px}
.legend{display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:4px}
.daybox{margin-top:8px;font-size:13px;min-height:20px}
.bar{height:10px;border-radius:5px;background:var(--line);overflow:hidden;display:flex}
.bar span{display:block;height:100%}
.kv{display:grid;grid-template-columns:1fr auto;gap:4px 12px;font-size:13px;margin-top:10px;font-variant-numeric:tabular-nums}
.kv i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.expbox{height:300px;overflow-y:auto;position:relative;margin-top:10px}
.chainbox{max-height:420px;overflow:auto}
.chainbox thead th{position:sticky;top:0;background:var(--card);z-index:1}
/* the four totals in one slim card */
.strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px 14px;padding:10px 14px}
.strip>div{display:flex;gap:8px;align-items:center;min-width:0}
.strip>div>div{min-width:0}
.strip .si{font-size:16px;opacity:.8;flex:none}
.strip b{display:block;font-size:14.5px;font-weight:650;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.strip small{display:block;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.fold>summary{display:flex;align-items:center;gap:10px;min-height:32px;list-style:none}
details.fold>summary::-webkit-details-marker{display:none}
details.fold>summary::after{content:"\203A";margin-left:auto;color:var(--muted);font-size:20px;transition:transform .2s}
details.fold[open]>summary::after{transform:rotate(90deg)}
details.fold{margin-top:12px}
@media (max-width:760px){.strip{grid-template-columns:1fr 1fr}.big{font-size:17px}.chartbox{height:230px}}
.pnlseg{display:flex;gap:6px;margin:2px 0 8px}.pnlseg button{font:inherit;font-size:12px;padding:4px 12px;border-radius:999px;border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer}.pnlseg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.s1ctl{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}.s1ctl label{font-size:13px;color:var(--muted);display:flex;gap:6px;align-items:center}
.s1pick{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chipb{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:4px 10px;font-size:13px;cursor:pointer;font-variant-numeric:tabular-nums}
.chipb small{color:var(--muted);font-size:11px}.chipb.on{border-color:var(--accent);background:var(--accent);color:#fff}
.s1opts{display:flex;gap:16px;flex-wrap:wrap;margin:6px 0 10px}.s1col{min-width:200px;flex:1}.s1col b{font-size:13px;color:var(--muted)}
.s1col>div{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.daygrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:10px 0 16px}
.dayb{font:inherit;text-align:left;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text);display:flex;flex-direction:column;gap:6px;cursor:pointer}
.dayb.sel{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--card));box-shadow:inset 0 0 0 1px var(--accent)}
.dayb .cov{position:relative;display:block;height:8px;border-radius:4px;background:var(--accent);overflow:hidden}
.dayb .cov i{position:absolute;top:0;bottom:0;background:var(--line)}
.s1cols{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}.s1left{flex:1 1 300px;max-width:400px;min-width:0}.s1right{flex:999 1 520px;min-width:0}
.s1cols h3{font-size:15px;margin:0}
.lhead,.lrow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;align-items:center}
.lhead{font-size:12px;color:var(--muted);padding:6px 4px 4px}.lhead span:nth-child(2){text-align:center}.lhead span:nth-child(3){text-align:right}
.ladder{position:relative;max-height:440px;overflow-y:auto;display:flex;flex-direction:column;gap:3px;margin-bottom:8px}
.lrow .k{text-align:center;font-size:13px;font-variant-numeric:tabular-nums}.lrow .k.atm{font-weight:700}
.lad{font:inherit;font-size:12px;width:100%;padding:6px;border-radius:6px;border:1px solid var(--line);color:var(--text);cursor:pointer;display:flex;gap:6px;align-items:center}
.lrow span:first-child .lad{justify-content:flex-start}.lrow span:last-child .lad{justify-content:flex-end}
.lad.hi{background:color-mix(in srgb,var(--accent) 20%,var(--card))}.lad.mid{background:color-mix(in srgb,var(--accent) 10%,var(--card))}.lad.lo{background:var(--card)}
.lad.sel{border-color:var(--accent);box-shadow:inset 0 0 0 2px var(--accent);font-weight:600}.lad.none{display:block;text-align:center;color:var(--muted);border-style:dashed;cursor:default}
.dot{width:7px;height:7px;border-radius:50%;background:var(--warn);display:inline-block}
.segs{display:flex;gap:4px;margin-left:auto}.segb{font:inherit;font-size:13px;padding:6px 11px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
.segb.on{border-color:var(--accent);background:var(--accent);color:#fff;font-weight:600}
.s1tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.s1tools .s1pick{margin:0}
.zoom{margin-left:auto;display:flex;gap:4px}.zoom button{font:inherit;min-width:36px;height:34px;padding:0 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer;font-size:14px}
.readout{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;min-height:18px;margin-bottom:4px}
.overbox{position:relative;height:40px;border-radius:6px;background:color-mix(in srgb,var(--muted) 10%,var(--card));margin-top:4px;touch-action:none;cursor:pointer}
.overbox canvas{position:absolute;inset:0}.overbox .win{position:absolute;top:0;bottom:0;border:2px solid var(--accent);border-radius:6px;background:color-mix(in srgb,var(--accent) 14%,transparent);box-sizing:border-box;cursor:grab}
.axisrow{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:2px}
</style>
</head>
<body>
<div class="wrap">
  <header><h1>Market data</h1><div class="status" id="status">Loading…</div></header>
  <div class="tabs" id="tabs"></div>

  <div class="card strip">
    <div title="Index bars stored"><span class="si">&#128200;</span><div><b id="cSpot">–</b><small id="cSpotSub"></small></div></div>
    <div title="Option prices stored"><span class="si">&#9881;</span><div><b id="cOpt">–</b><small id="cOptSub"></small></div></div>
    <div title="Disk used by the data"><span class="si">&#128190;</span><div><b id="cDisk">–</b><small id="cDiskSub"></small></div></div>
    <div title="Daily recording after the close"><span class="si">&#9201;</span><div><b id="cRec">–</b><small id="cRecSub"></small></div></div>
  </div>

  <div class="card block">
    <div class="row"><h2>What is stored, day by day</h2><span class="sub" id="covSub"></span></div>
    <div class="cal" id="cal"></div>
    <div class="legend"><span><i style="background:var(--c0)"></i>nothing</span><span><i style="background:var(--spotonly)"></i>index only</span>
      <span>options: <i style="background:var(--c1)"></i><i style="background:var(--c2)"></i><i style="background:var(--c3)"></i><i style="background:var(--c4)"></i> more contracts</span>
      <span><i style="background:var(--c0);box-shadow:inset 0 0 0 2px var(--warn)"></i>fewer index bars than a full day</span></div>
    <div class="daybox" id="daybox">Tap a day for what is stored that day.</div>
  </div>

  <details class="card fold" id="expFold">
    <summary><h2>Expiries</h2><span class="sub" id="expSub"></span></summary>
    <div class="expbox" id="expBox"><canvas id="expChart"></canvas></div>
    <div class="note">Each bar runs from an expiry's first stored price to its last; darker bars have more contracts. Tap one to see its option chain.</div>
  </details>

  <div class="card block hide" id="chainCard">
    <div class="row"><h2 id="chainTitle">Chain</h2><div class="row" style="gap:8px"><span class="sub" id="chainSpot"></span>
      <div class="seg" id="chainView"><button class="on" data-v="chart">Chart</button><button data-v="table">Table</button></div></div></div>
    <div class="chartbox" id="chainChartBox"><canvas id="chainChart"></canvas></div>
    <div class="scroll chainbox hide" id="chainBox"><table id="chainTable"></table></div>
    <div class="note" id="chainNote"></div>
  </div>

  <div class="card block hide" id="seriesCard">
    <div class="row"><div><h2 id="seriesTitle">Contract</h2><div class="sub" id="seriesPeriod"></div></div><a id="dl" href="#">Download CSV</a></div>
    <div class="chartbox"><canvas id="seriesChart"></canvas></div>
    <div class="note" id="seriesNote"></div>
  </div>

  <div class="card block hide" id="s1Empty"><div class="row"><h2>1-second recordings</h2></div>
    <div class="note">Nothing recorded yet: recording runs while the paper engine is on Zerodha prices, once the day's Zerodha login is in. Zerodha keeps no history finer than 1 minute, so a day not recorded cannot be fetched later.</div></div>

  <div class="card block hide" id="s1Card">
    <div class="row"><h2>1-second recordings</h2><span class="sub" id="s1Sub"></span></div>
    <div class="sub">Each bar is 09:15 to 15:30; grey marks 5 minutes or more without a tick.</div>
    <div class="daygrid" id="s1DayGrid"></div>
    <div class="s1cols">
      <div class="s1left">
        <div class="row"><h3 id="s1OptTitle">Options</h3><span class="sub" id="s1OptCount"></span></div>
        <div class="s1pick" id="s1Idx"><button type="button" class="chipb on" data-name="NIFTY">NIFTY</button><button type="button" class="chipb" data-name="INDIAVIX">India VIX</button></div>
        <div class="s1pick" id="s1Exp"></div>
        <div class="lhead"><span>Call</span><span>Strike</span><span>Put</span></div>
        <div class="ladder" id="s1Ladder"></div>
        <div class="sub">Each cell: share of 09:15–15:30 with ticks. A dot: a session traded it that day. Tap to chart it.</div>
      </div>
      <div class="s1right">
        <div class="row"><h3 id="s1Title">NIFTY</h3><span class="segs" id="s1Every" role="group" aria-label="Candle length"></span></div>
        <div class="s1tools"><span class="sub">Period</span><span id="s1Periods" class="s1pick"></span>
          <span class="zoom"><button type="button" id="s1Out" aria-label="Zoom out">−</button><button type="button" id="s1In" aria-label="Zoom in">+</button><button type="button" id="s1All">Whole day</button></span></div>
        <div class="readout" id="s1Read"></div>
        <div class="chartbox" style="height:300px"><canvas id="s1Chart"></canvas></div>
        <div class="sub" style="margin-top:8px">Whole day: drag the window to move it, tap outside it to jump there</div>
        <div class="overbox" id="s1OverBox"><canvas id="s1Over"></canvas><div class="win" id="s1Win"></div></div>
        <div class="axisrow"><span>09:15</span><span>11:00</span><span>13:00</span><span>15:30</span></div>
        <div class="note" id="s1Info"></div>
        <details><summary>Candles as a table (last 300)</summary><div class="scroll" style="margin-top:8px"><table id="s1Rows"></table></div></details>
      </div>
    </div>
  </div>

  <div class="card block"><div class="row"><span class="sub">The index and India VIX chart is on the <a href="/chart#stored">Chart</a> page.</span></div></div>

  <div class="card block">
    <details id="moreSets"><summary>Index 1-minute sets</summary><div class="scroll" style="margin-top:8px"><table id="spotTable"></table></div></details>
    <details><summary>Disk use by folder</summary><div id="diskBox"></div></details>
    <details><summary>Recording log</summary><pre class="log" id="log" style="margin-top:8px"></pre></details>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const n0 = x => Number(x).toLocaleString("en-IN", {maximumFractionDigits: 0});
const n2 = x => x == null || x === "" ? "–" : Number(x).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const big = x => x >= 1e7 ? (x / 1e7).toFixed(2) + " Cr" : x >= 1e5 ? (x / 1e5).toFixed(1) + " L" : n0(x);
const gb = b => b == null ? "–" : b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(0) + " MB";
const fmtDay = s => { if (!s) return "–"; const d = new Date(s.slice(0, 10) + "T00:00"); return isNaN(d) ? s : d.toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "numeric"}); };
const fmtWhen = s => { if (!s) return "–"; const d = new Date(s.replace(" ", "T")); return isNaN(d) ? s : d.toLocaleString("en-IN", {day: "numeric", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}); };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const RANGES = [["1D", 1], ["5D", 5], ["1M", 22], ["6M", 130], ["1Y", 260], ["All", 5000]];
const NAMES = {NIFTY: "NIFTY", BANKNIFTY: "BANK NIFTY", SENSEX: "SENSEX"};
const QS = new URLSearchParams(location.search);
let U = (QS.get("u") || "NIFTY").toUpperCase(), sum = null, days = 22, charts = {}, chainExp = "", chainData = null, chainView = "chart", dataset = "", cov = null;

async function get(url) { const r = await fetch(url, {cache: "no-store"}); if (r.status === 401) { location.href = "/signin?next=/market-data"; throw new Error("sign in"); } const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j; }
function chart(id, canvas, cfg) { if (charts[id]) charts[id].destroy(); charts[id] = new Chart(canvas, cfg); return charts[id]; }
function axes(extra) {
  const m = css("--muted"), l = css("--line");
  return Object.assign({x: {ticks: {maxTicksLimit: innerWidth < 600 ? 3 : 6, maxRotation: 0, color: m}, grid: {display: false}},
                        y: {ticks: {color: m}, grid: {color: l}}}, extra || {});
}

function pickUnderlying(u) {
  U = u; chainExp = ""; dataset = ""; $("chainCard").classList.add("hide"); $("seriesCard").classList.add("hide");
  history.replaceState(null, "", "/market-data?u=" + u);
  loadSummary().catch(e => $("status").textContent = "Cannot load: " + e.message); loadCoverage();
}

// ---- 1-second recordings: days, the options of a day, and a chart that zooms ----
const S1_OPEN = 9 * 3600 + 15 * 60, S1_CLOSE = 15 * 3600 + 30 * 60;
const S1_EVERY = [["1s", 1], ["1m", 60], ["5m", 300], ["30m", 1800]];
const S1_PERIODS = [["15 min", 900], ["1 hour", 3600], ["Morning", "am"], ["Day", "day"]];
let s1 = {days: [], day: "", name: "NIFTY", every: 60, a: S1_OPEN, b: S1_CLOSE, inst: null, exp: "", candles: [], over: null};
const s1hms = x => `${String(Math.floor(x / 3600)).padStart(2, "0")}:${String(Math.floor(x % 3600 / 60)).padStart(2, "0")}:${String(x % 60).padStart(2, "0")}`;
const s1secs = t => { const p = String(t).split(":").map(Number); return p[0] * 3600 + p[1] * 60 + (p[2] || 0); };
const s1label = n => n === "NIFTY" ? "NIFTY" : n === "INDIAVIX" ? "India VIX" : (() => { const [e, r] = n.split("_"); return `NIFTY ${fmtDay(e).replace(/ \d{4}$/, "")} ${r.slice(0, -2)} ${r.slice(-2)}`; })();

function s1Days(info) {
  s1.days = (info.days || []).slice().reverse();
  $("s1Card").classList.toggle("hide", !s1.days.length);
  $("s1Empty").classList.toggle("hide", !!s1.days.length);
  const mb = s1.days.reduce((a, d) => a + d.mb, 0);
  $("s1Sub").textContent = `kept ${info.keep} trading days · ${mb.toFixed(0)} MB`;
  if (!s1.days.length) return;
  if (!s1.day || !s1.days.some(d => d.day === s1.day)) s1.day = s1.days[0].day;
  const span = S1_CLOSE - S1_OPEN;
  $("s1DayGrid").innerHTML = s1.days.slice().reverse().map(d => {
    const gaps = (d.gaps || []).map(g => { const a = s1secs(g[0]) - S1_OPEN, b = s1secs(g[1]) - S1_OPEN;
      return `<i style="left:${a / span * 100}%;width:${(b - a) / span * 100}%"></i>`; }).join("");
    const note = d.coverage < 99 && (d.gaps || []).length ? `Partial: no ticks ${d.gaps.map(g => g[0] + "–" + g[1]).slice(0, 2).join(", ")}` : `${d.instruments - 2} options · ${d.mb} MB`;
    return `<button type="button" class="dayb ${d.day === s1.day ? "sel" : ""}" data-day="${d.day}"><b>${fmtDay(d.day).replace(/ \d{4}$/, "")}</b>
      <span class="cov" title="${d.coverage}% of 09:15–15:30 has ticks">${gaps}</span><span class="sub">${esc(note)}</span></button>`;
  }).join("");
  document.querySelectorAll("#s1DayGrid .dayb").forEach(b => b.onclick = () => { s1.day = b.dataset.day; s1Days(info); s1LoadDay().catch(s1err); });
}
function s1err(e) { $("s1Info").textContent = e.message; }

async function s1LoadDay() {
  s1.inst = await get("/api/market-data/sec1/instruments?day=" + s1.day);
  const opts = s1.inst.instruments.filter(i => i.kind === "option");
  const exps = [...new Set(opts.map(o => o.expiry))].sort();
  if (!exps.includes(s1.exp)) s1.exp = exps[0] || "";
  $("s1Exp").innerHTML = exps.map(e => `<button type="button" class="chipb ${e === s1.exp ? "on" : ""}" data-exp="${e}">${fmtDay(e).replace(/ \d{4}$/, "")}${e === exps[0] ? " (nearest)" : ""}</button>`).join("");
  document.querySelectorAll("#s1Exp .chipb").forEach(b => b.onclick = () => { s1.exp = b.dataset.exp; s1LoadDay().catch(s1err); });
  $("s1OptTitle").textContent = `Options on ${fmtDay(s1.day).replace(/ \d{4}$/, "")}`;
  $("s1OptCount").textContent = `${opts.length} recorded`;
  const mine = opts.filter(o => o.expiry === s1.exp), by = {};
  mine.forEach(o => { (by[o.strike] = by[o.strike] || {})[o.right] = o; });
  const ks = Object.keys(by).map(Number).sort((a, b) => a - b), atm = s1.inst.atm;
  const shade = c => c >= 97 ? "hi" : c >= 80 ? "mid" : "lo";
  const cell = (o, right) => o ? `<button type="button" class="lad ${shade(o.coverage)} ${s1.name === o.name ? "sel" : ""}" data-name="${o.name}" title="${o.rows.toLocaleString("en-IN")} seconds with ticks">${right === "PE" && o.in_use ? '<span class="dot" aria-label="used by a session"></span>' : ""}${Math.round(o.coverage)}%${right === "CE" && o.in_use ? '<span class="dot" aria-label="used by a session"></span>' : ""}</button>` : '<span class="lad none">–</span>';
  $("s1Ladder").innerHTML = ks.length ? ks.map(k => `<div class="lrow"><span>${cell(by[k].CE, "CE")}</span><span class="k ${k === atm ? "atm" : ""}">${k}${k === atm ? " ATM" : ""}</span><span>${cell(by[k].PE, "PE")}</span></div>`).join("")
    : '<div class="sub">No options recorded for this expiry.</div>';
  document.querySelectorAll("#s1Ladder .lad[data-name], #s1Idx .chipb").forEach(b => b.onclick = () => s1Pick(b.dataset.name));
  const lad = $("s1Ladder"), row = lad.querySelector(".k.atm");
  if (row) lad.scrollTop = row.parentElement.offsetTop - lad.clientHeight / 2;
  if (!s1.inst.instruments.some(i => i.name === s1.name)) s1.name = "NIFTY";
  document.querySelectorAll("#s1Idx .chipb").forEach(b => b.classList.toggle("on", b.dataset.name === s1.name));
  s1.over = null;
  await s1Draw();
}
function s1Pick(name) {
  s1.name = name; s1.over = null;
  document.querySelectorAll("#s1Ladder .lad").forEach(b => b.classList.toggle("sel", b.dataset.name === name));
  document.querySelectorAll("#s1Idx .chipb").forEach(b => b.classList.toggle("on", b.dataset.name === name));
  s1Draw().catch(s1err);
}
function s1Controls() {
  $("s1Every").innerHTML = S1_EVERY.map(([l, v]) => `<button type="button" class="segb ${v === s1.every ? "on" : ""}" data-every="${v}">${l}</button>`).join("");
  const w = s1.b - s1.a, cur = w >= S1_CLOSE - S1_OPEN ? "day" : (s1.a === S1_OPEN && s1.b === 12 * 3600 + 30 * 60 ? "am" : w);
  $("s1Periods").innerHTML = S1_PERIODS.map(([l, v]) => `<button type="button" class="chipb ${v === cur ? "on" : ""}" data-period="${v}">${l}</button>`).join("");
  document.querySelectorAll("#s1Every .segb").forEach(b => b.onclick = () => { s1.every = +b.dataset.every; s1Draw().catch(s1err); });
  document.querySelectorAll("#s1Periods .chipb").forEach(b => b.onclick = () => {
    const v = b.dataset.period;
    if (v === "day") s1Window(S1_OPEN, S1_CLOSE);
    else if (v === "am") s1Window(S1_OPEN, 12 * 3600 + 30 * 60);
    else { const c = (s1.a + s1.b) / 2, h = +v / 2; s1Window(c - h, c + h); }
  });
}
function s1Window(a, b) {
  const w = Math.max(60, Math.min(S1_CLOSE - S1_OPEN, b - a));
  a = Math.max(S1_OPEN, Math.min(S1_CLOSE - w, a));
  s1.a = Math.round(a); s1.b = Math.round(a + w);
  s1Draw().catch(s1err);
}
function s1Zoom(f) { const c = (s1.a + s1.b) / 2, h = (s1.b - s1.a) * f / 2; s1Window(c - h, c + h); }

async function s1Draw() {
  s1Controls();
  $("s1Title").textContent = `${s1label(s1.name)} · ${fmtDay(s1.day).replace(/ \d{4}$/, "")}`;
  const q = `day=${s1.day}&name=${encodeURIComponent(s1.name)}`;
  if (!s1.over) s1.over = await get(`/api/market-data/sec1/candles?${q}&every=60`);
  s1Overview();
  const j = await get(`/api/market-data/sec1/candles?${q}&every=${s1.every}&from=${s1hms(s1.a)}&to=${s1hms(s1.b)}`);
  if (j.too_many) {
    s1.candles = [];
    if (charts.s1) { charts.s1.destroy(); delete charts.s1; }
    $("s1Info").textContent = `${j.total.toLocaleString("en-IN")} candles of ${S1_EVERY.find(e => e[1] === s1.every)[0]} in this period: zoom in (1 hour or less for 1-second candles) or pick a longer candle.`;
    $("s1Rows").innerHTML = ""; $("s1Read").textContent = "";
    return;
  }
  const c = s1.candles = j.candles;
  const up = css("--up") || "#087a4c", dn = css("--down") || "#c8323a";
  const body = c.map(r => r[1] === r[4] ? [r[1] - 0.025, r[4] + 0.025] : [Math.min(r[1], r[4]), Math.max(r[1], r[4])]);
  const col = c.map(r => r[4] >= r[1] ? up : dn);
  chart("s1", $("s1Chart"), {
    type: "bar",
    data: {labels: c.map(r => s1.every >= 60 ? r[0].slice(0, 5) : r[0]), datasets: [
      {label: "wick", data: c.map(r => [r[3], r[2]]), backgroundColor: col, barPercentage: 0.12, categoryPercentage: 1, grouped: false},
      {label: "body", data: body, backgroundColor: col, barPercentage: 0.75, categoryPercentage: 1, grouped: false}]},
    options: {animation: false, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
      plugins: {legend: {display: false}, tooltip: {enabled: false}},
      onHover: (e, els) => s1Read(els.length ? els[0].index : c.length - 1),
      scales: axes({y: {beginAtZero: false, grace: "4%", ticks: {color: css("--muted")}, grid: {color: css("--line")}}})}
  });
  s1Read(c.length - 1);
  $("s1Info").textContent = c.length ? `${c.length.toLocaleString("en-IN")} candles · ${c[0][0].slice(0, 5)} – ${c[c.length - 1][0].slice(0, 5)}` : "Nothing recorded for this instrument in this period.";
  $("s1Rows").innerHTML = c.length
    ? "<tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Bid</th><th>Ask</th><th>Volume</th><th>OI</th><th>Ticks</th></tr>" +
      c.slice(-300).reverse().map(r => `<tr><td>${r[0]}</td><td>${n2(r[1])}</td><td>${n2(r[2])}</td><td>${n2(r[3])}</td><td>${n2(r[4])}</td><td>${n2(r[5])}</td><td>${n2(r[6])}</td><td>${r[7] == null ? "–" : n0(r[7])}</td><td>${r[8] == null ? "–" : n0(r[8])}</td><td>${n0(r[9])}</td></tr>`).join("")
    : '<tr><td class="empty">Nothing recorded in this period.</td></tr>';
}
function s1Read(i) {
  const r = s1.candles[i];
  $("s1Read").innerHTML = r ? [r[0], `O ${n2(r[1])}`, `H ${n2(r[2])}`, `L ${n2(r[3])}`, `C ${n2(r[4])}`,
    r[5] != null || r[6] != null ? `Bid ${n2(r[5])} · Ask ${n2(r[6])}` : "", r[7] != null ? `Vol ${big(r[7])}` : "", `${n0(r[9])} ticks`]
    .filter(Boolean).map(x => `<span>${x}</span>`).join("") : "";
}
function s1Overview() {
  const cv = $("s1Over"), box = cv.parentElement, w = box.clientWidth, h = 40, dpr = devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr; cv.style.width = w + "px"; cv.style.height = h + "px";
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
  const c = (s1.over && s1.over.candles) || [], span = S1_CLOSE - S1_OPEN;
  if (c.length) {
    const ys = c.map(r => r[4]), lo = Math.min(...ys), hi = Math.max(...ys);
    g.strokeStyle = css("--muted"); g.lineWidth = 1; g.beginPath();
    c.forEach((r, i) => { const x = (s1secs(r[0]) - S1_OPEN) / span * w, y = h - 4 - (hi > lo ? (r[4] - lo) / (hi - lo) : 0.5) * (h - 8);
      i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  }
  const win = $("s1Win");
  win.style.left = ((s1.a - S1_OPEN) / span * 100) + "%"; win.style.width = ((s1.b - s1.a) / span * 100) + "%";
}
(function s1Drag() {
  let drag = null;
  const box = () => $("s1Over").parentElement;
  const at = e => { const r = box().getBoundingClientRect(); return S1_OPEN + Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (S1_CLOSE - S1_OPEN); };
  document.addEventListener("pointerdown", e => {
    if (!e.target.closest || !e.target.closest("#s1OverBox")) return;
    const t = at(e), w = s1.b - s1.a;
    if (t < s1.a || t > s1.b) { s1Window(t - w / 2, t + w / 2); return; }
    drag = {x: t, a: s1.a, w}; e.preventDefault();
  });
  document.addEventListener("pointermove", e => {
    if (!drag) return;
    const a = Math.max(S1_OPEN, Math.min(S1_CLOSE - drag.w, drag.a + at(e) - drag.x));
    s1.a = Math.round(a); s1.b = Math.round(a + drag.w); s1Overview();
  });
  document.addEventListener("pointerup", () => { if (drag) { drag = null; s1Draw().catch(s1err); } });
})();
$("s1In").onclick = () => s1Zoom(0.5);
$("s1Out").onclick = () => s1Zoom(2);
$("s1All").onclick = () => s1Window(S1_OPEN, S1_CLOSE);
addEventListener("resize", () => { if (s1.over) s1Overview(); });

async function s1LoadDays() { if (s1.days.length) await s1LoadDay(); }

async function loadSummary() {
  sum = await get("/api/market-data/summary?u=" + U);
  if (!sum.installed) { $("status").textContent = "No data folder yet: the Algo Lab engine has not recorded anything."; return; }
  $("tabs").innerHTML = sum.underlyings.map(u => `<button class="${u === U ? "on" : ""}" data-u="${u}">${NAMES[u] || u}</button>`).join("");
  document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => pickUnderlying(b.dataset.u));
  const mine = sum.spot.filter(s => s.underlying === U), sp = mine[0];
  if (sp) { $("cSpot").textContent = `${n0(sp.days)} days`; $("cSpotSub").textContent = `${fmtDay(sp.from)} – ${fmtDay(sp.to)} · ${sp.interval} · ${big(sp.rows)} bars`; }
  else { $("cSpot").textContent = "None"; $("cSpotSub").textContent = `No ${NAMES[U] || U} bars stored`; }
  const ex = sum.expiries, contracts = ex.reduce((a, e) => a + e.contracts, 0), rows = ex.reduce((a, e) => a + e.rows, 0);
  const lastRec = ex.map(e => e.to).sort().pop();
  $("cOpt").textContent = ex.length ? `${n0(contracts)} contracts` : "None yet";
  $("cOptSub").textContent = ex.length ? `${ex.length} expiries · ${ex.some(e => e.approx) ? "~" : ""}${big(rows)} prices · up to ${fmtDay(lastRec)}` : "No option prices for " + (NAMES[U] || U);
  const last = (sum.log || [])[0] || "";
  $("cRec").textContent = last ? (last.includes("FAILED") ? "Last run failed" : "Last run OK") : "Not logged yet";
  $("cRec").style.color = last.includes("FAILED") ? css("--down") : "";
  const why = last.includes("FAILED") ? last.split("FAILED:").slice(1).join("FAILED:").trim() : "";
  $("cRecSub").textContent = TZ.text(why ? why : (last ? last.slice(0, 16) + " · " : "") + "next " + sum.next_recording);   // a failure says why
  $("cRec").parentElement.title = TZ.text(last ? last + "\nNext: " + sum.next_recording : "");
  $("status").textContent = `Stored on the server · ${NAMES[U] || U}` + (lastRec ? ` · option prices up to ${fmtWhen(lastRec)}` : "");
  $("log").innerHTML = (sum.log || []).map(l => `<span class="${l.includes("FAILED") ? "bad" : ""}">${esc(l)}</span>`).join("\n") || "Nothing logged yet. The recording runs each trading day at 15:45 IST.";
  s1Days(sum.sec1 || {days: [], keep: 5});
  s1LoadDays().catch(s1err);
  $("spotTable").innerHTML = sum.spot.length ? "<tr><th>Data set</th><th>Bars</th><th>From</th><th>To</th><th>Days</th><th>Last close</th><th>VIX</th><th>Size</th></tr>" + sum.spot.map(s =>
    `<tr><td>${esc(s.name)}</td><td>${n0(s.rows)}</td><td>${fmtWhen(s.from)}</td><td>${fmtWhen(s.to)}</td><td>${n0(s.days)}</td><td>${n2(s.close)}</td><td>${n2(s.vix)}</td><td>${s.size_mb} MB</td></tr>`).join("")
    : '<tr><td class="empty">No index bars stored yet.</td></tr>';
  drawExpiries(ex);
  if (!chainExp && ex.length) {
    const today = new Date().toISOString().slice(0, 10), upcoming = ex.filter(e => e.expiry >= today);
    loadChain((upcoming.length ? upcoming[upcoming.length - 1] : ex[0]).expiry);
  }
}

async function loadDisk() {
  const d = await get("/api/market-data/disk").catch(() => null);
  if (!d) return;
  const used = d.parts.reduce((a, p) => a + p.bytes, 0);
  $("cDisk").textContent = gb(used);
  $("cDiskSub").textContent = d.free != null ? `${gb(d.free)} free of ${gb(d.total)}` : "data folder";
  const pal = [css("--c4"), css("--up"), css("--vix"), css("--c2"), css("--down"), css("--muted"), css("--c3")];
  $("diskBox").innerHTML = `<div class="bar" style="margin-top:10px">${d.parts.map((p, i) => `<span style="width:${(p.bytes / Math.max(used, 1) * 100).toFixed(2)}%;background:${pal[i % pal.length]}"></span>`).join("")}</div>
    <div class="kv">${d.parts.map((p, i) => `<span><i style="background:${pal[i % pal.length]}"></i>${esc(p.name)}</span><b>${gb(p.bytes)}</b>`).join("")}
    ${d.free != null ? `<span>Free on the server's disk</span><b>${gb(d.free)}</b>` : ""}</div>`;
}

// ---- coverage calendar ----
async function loadCoverage() {
  $("cal").innerHTML = '<span class="empty">Loading…</span>';
  try { cov = (await get("/api/market-data/coverage?u=" + U)).days; } catch (e) { $("cal").innerHTML = `<span class="empty">${esc(e.message)}</span>`; return; }
  if (!cov.length) { $("cal").innerHTML = '<span class="empty">Nothing stored yet.</span>'; $("covSub").textContent = ""; return; }
  const by = Object.fromEntries(cov.map(r => [r[0], r]));
  const fullBars = Math.max(...cov.map(r => r[1])), maxC = Math.max(1, ...cov.map(r => r[2]));
  const start = new Date(cov[0][0] + "T00:00"), end = new Date(cov[cov.length - 1][0] + "T00:00");
  const mon = new Date(start); mon.setDate(mon.getDate() - ((mon.getDay() + 6) % 7));
  let html = "", lastMonth = -1, withOpt = 0, withSpot = 0;
  for (let w = new Date(mon); w <= end; w.setDate(w.getDate() + 7)) {
    let col = "";
    for (let i = 0; i < 5; i++) {
      const d = new Date(w); d.setDate(d.getDate() + i);
      const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      const r = by[key];
      let cls = "c";
      if (r) {
        if (r[2]) { withOpt++; cls += " l" + Math.min(4, 1 + Math.floor(r[2] / maxC * 4 - 1e-9)); } else if (r[1]) cls += " s";
        if (r[1]) withSpot++;
        if (r[1] && r[1] < 0.9 * fullBars) cls += " short";
      }
      col += `<div class="${cls}" data-d="${key}" title="${key}${r ? ` · ${n0(r[1])} index bars · ${n0(r[2])} contracts` : " · nothing"}"></div>`;
    }
    const m = w.getMonth(), label = m !== lastMonth ? `<span class="m">${w.toLocaleDateString("en-IN", {month: "short"})}${m === 0 || lastMonth === -1 ? " " + String(w.getFullYear()).slice(2) : ""}</span>` : "";
    lastMonth = m;
    html += `<div class="wk">${label}${col}</div>`;
  }
  $("cal").innerHTML = html;
  const weeks = $("cal").children.length, room = $("cal").clientWidth - 4;   // bigger squares when there is room
  $("cal").style.setProperty("--sz", Math.max(11, Math.min(24, Math.floor(room / Math.max(weeks, 1)) - 3)) + "px");
  $("cal").scrollLeft = $("cal").scrollWidth;
  $("covSub").textContent = `${n0(withSpot)} days with index bars · ${n0(withOpt)} with option prices`;
  $("cal").querySelectorAll(".c[data-d]").forEach(c => c.onclick = () => {
    $("cal").querySelectorAll(".c.sel").forEach(x => x.classList.remove("sel")); c.classList.add("sel");
    const r = by[c.dataset.d], when = fmtDay(c.dataset.d) + " (" + new Date(c.dataset.d + "T00:00").toLocaleDateString("en-IN", {weekday: "short"}) + ")";
    const exps = sum ? sum.expiries.filter(e => e.from.slice(0, 10) <= c.dataset.d && e.to.slice(0, 10) >= c.dataset.d).map(e => e.expiry).sort() : [];
    $("daybox").innerHTML = r ? `<b>${when}</b>: ${n0(r[1])} index bars${r[1] && r[1] < 0.9 * fullBars ? ` <span style="color:var(--warn)">(a full day has ${n0(fullBars)})</span>` : ""} · ${n0(r[2])} option contracts with prices`
      + (exps.length ? ` · expiries ${exps.map(e => `<a href="#" data-e="${e}">${fmtDay(e)}</a>`).join(", ")}` : "")
      : `<b>${when}</b>: nothing stored (holiday or not recorded).`;
    $("daybox").querySelectorAll("a[data-e]").forEach(a => a.onclick = ev => { ev.preventDefault(); loadChain(a.dataset.e); });
  });
}

// ---- index chart ----
// ---- expiries timeline ----
function drawExpiries(ex) {
  const list = ex.slice().sort((a, b) => a.expiry.localeCompare(b.expiry));
  $("expSub").textContent = list.length ? `${list.length} expiries · ${fmtDay(list[0].expiry)} – ${fmtDay(list[list.length - 1].expiry)}` : "";
  if (!list.length) { if (charts.exp) { charts.exp.destroy(); delete charts.exp; } $("expBox").style.height = "40px"; return; }
  const h = Math.max(120, list.length * 16 + 40);
  const maxC = Math.max(...list.map(e => e.contracts));
  const shade = e => [css("--c1"), css("--c2"), css("--c3"), css("--c4")][Math.min(3, Math.floor(e.contracts / maxC * 4 - 1e-9))];
  const t = s => new Date((s || "").slice(0, 10) + "T12:00").getTime();
  const wrap = $("expBox"); wrap.innerHTML = `<div style="height:${h}px;position:relative"><canvas id="expChart"></canvas></div>`;
  const c = chart("exp", $("expChart"), {type: "bar",
    data: {labels: list.map(e => fmtDay(e.expiry)), datasets: [{data: list.map(e => [t(e.from), t(e.to) + 864e5 * 0.6]),
      backgroundColor: list.map(e => e.expiry === chainExp ? css("--accent") : shade(e)), borderSkipped: false, borderRadius: 3, barPercentage: 0.8, categoryPercentage: 1}]},
    options: {indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {legend: {display: false}, tooltip: {callbacks: {label: c => { const e = list[c.dataIndex];
        return [`${e.ce} calls · ${e.pe} puts · strikes ${n0(e.lo)}–${n0(e.hi)}`, `${e.approx ? "~" : ""}${n0(e.rows)} prices · ${fmtDay(e.from)} → ${fmtDay(e.to)}`]; }}}},
      onClick: (ev, els) => { if (els.length) loadChain(list[els[0].index].expiry); },
      scales: {x: {type: "linear", position: "top", beginAtZero: false, min: Math.min(...list.map(e => t(e.from))) - 864e5 * 2,
                    max: Math.max(...list.map(e => t(e.to))) + 864e5 * 3, ticks: {color: css("--muted"), maxTicksLimit: innerWidth < 600 ? 4 : 8,
                    callback: v => new Date(v).toLocaleDateString("en-IN", {month: "short", year: "2-digit"})}, grid: {color: css("--line")}},
               y: {ticks: {color: css("--muted"), font: {size: 11}, autoSkip: false}, grid: {display: false}}}}});
  const idx = list.findIndex(e => e.expiry === chainExp);
  wrap.scrollTop = idx >= 0 ? Math.max(0, idx * 16 - 120) : wrap.scrollHeight;
  return c;
}

// ---- option chain ----
async function loadChain(exp) {
  chainExp = exp;
  if (sum) drawExpiries(sum.expiries);
  chainData = await get(`/api/market-data/chain?u=${U}&expiry=${exp}`);
  $("chainCard").classList.remove("hide");
  $("chainTitle").textContent = `${NAMES[U] || U} · expiry ${fmtDay(exp)}`;
  const d = chainData, spot = d.spot ? d.spot.close : null;
  $("chainSpot").textContent = spot ? `index ${n2(spot)} (${fmtWhen(d.spot.time)})` : "";
  drawChain();
}
function drawChain() {
  const d = chainData; if (!d) return;
  const spot = d.spot ? d.spot.close : null;
  const atm = spot && d.strikes.length ? d.strikes.reduce((b, s) => Math.abs(s.strike - spot) < Math.abs(b - spot) ? s.strike : b, d.strikes[0].strike) : null;
  const latest = d.strikes.flatMap(s => [s.CE, s.PE]).filter(Boolean).map(x => x.to).sort().pop() || "";
  $("chainChartBox").classList.toggle("hide", chainView !== "chart"); $("chainBox").classList.toggle("hide", chainView !== "table");
  document.querySelectorAll("#chainView button").forEach(b => b.classList.toggle("on", b.dataset.v === chainView));
  if (chainView === "chart") {
    const ks = d.strikes.map(s => s.strike), num = x => x && x.last !== "" ? Number(x.last) : null;
    const spotLine = {id: "spot", afterDatasetsDraw(c) { if (!spot) return; const X = c.scales.x.getPixelForValue(spot), a = c.chartArea, g = c.ctx;
      if (X < a.left || X > a.right) return; g.save(); g.strokeStyle = css("--muted"); g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X, a.top); g.lineTo(X, a.bottom); g.stroke();
      g.fillStyle = css("--muted"); g.font = "11px system-ui"; g.fillText("index", X + 4, a.top + 10); g.restore(); }};
    chart("chain", $("chainChart"), {type: "line", plugins: [spotLine],
      data: {datasets: [
        {label: "Calls", data: d.strikes.filter(s => s.CE).map(s => ({x: s.strike, y: num(s.CE)})), borderColor: css("--up"), backgroundColor: css("--up"), borderWidth: 1.6, pointRadius: 2.5, tension: 0.2},
        {label: "Puts", data: d.strikes.filter(s => s.PE).map(s => ({x: s.strike, y: num(s.PE)})), borderColor: css("--down"), backgroundColor: css("--down"), borderWidth: 1.6, pointRadius: 2.5, tension: 0.2}]},
      options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: "nearest", axis: "x", intersect: false},
        onClick: (ev, els) => { if (!els.length) return;   // both lines answer at a strike: take the one nearest the tap
          const p = els.reduce((a, e) => Math.hypot(e.element.x - ev.x, e.element.y - ev.y) < Math.hypot(a.element.x - ev.x, a.element.y - ev.y) ? e : a);
          const s = c2s(p); if (s) loadSeries(chainExp, s[0], s[1]); },
        plugins: {legend: {labels: {color: css("--muted"), boxWidth: 12}}, tooltip: {callbacks: {title: c => "Strike " + n0(c[0].parsed.x), label: c => `${c.dataset.label}: ${n2(c.parsed.y)}`}}},
        scales: {x: {type: "linear", min: ks[0], max: ks[ks.length - 1], ticks: {color: css("--muted"), maxTicksLimit: 7, callback: v => n0(v)}, grid: {display: false}},
                 y: {type: "logarithmic", ticks: {color: css("--muted"), maxTicksLimit: 6, callback: v => v >= 1 ? n0(v) : String(v)}, grid: {color: css("--line")}}}}});
    function c2s(p) { const ds = charts.chain.data.datasets[p.datasetIndex]; const pt = ds.data[p.index]; return pt ? [pt.x, p.datasetIndex === 0 ? "CE" : "PE"] : null; }
  } else {
    const cell = (s, r) => s[r] ? `<td class="px" data-k="${s.strike}" data-r="${r}" title="${n0(s[r].rows)} prices, last ${esc(s[r].to)}">${n2(s[r].last)}${s[r].quotes ? '<span class="dot"></span>' : ""}${s[r].to < latest ? `<div class="sub" style="font-size:12px">${fmtWhen(s[r].to)}</div>` : ""}</td>` : '<td class="none">–</td>';
    $("chainTable").innerHTML = "<thead><tr><th>Call</th><th style='text-align:center'>Strike</th><th>Put</th></tr></thead><tbody>" +
      d.strikes.slice().reverse().map(s => `<tr>${cell(s, "CE")}<td style="text-align:center" class="${s.strike === atm ? "atm" : ""}">${n0(s.strike)}</td>${cell(s, "PE")}</tr>`).join("") + "</tbody>";
    document.querySelectorAll("#chainTable td.px").forEach(td => td.onclick = () => loadSeries(chainExp, td.dataset.k, td.dataset.r));
    const a = document.querySelector("#chainTable td.atm"), box = $("chainBox");
    if (a) box.scrollTop = a.parentElement.offsetTop - box.clientHeight / 2;
  }
  $("chainNote").textContent = `Last stored price of each contract (${fmtWhen(latest)}) · ${d.strikes.length} strikes · price axis is logarithmic · tap a ${chainView === "chart" ? "point" : "price"} for its history.`;
}
document.querySelectorAll("#chainView button").forEach(b => b.onclick = () => { chainView = b.dataset.v; drawChain(); });

async function loadSeries(exp, k, r) {
  const q = `u=${U}&expiry=${exp}&strike=${k}&right=${r}`;
  const d = await get("/api/market-data/series?" + q);
  $("seriesCard").classList.remove("hide");
  $("seriesTitle").textContent = `${NAMES[U] || U} ${fmtDay(exp)} ${n0(k)} ${r}`;
  $("dl").href = "/api/market-data/download?" + q;
  const pts = d.points, hasQ = pts.some(p => p[2] != null);
  const ds = [{label: "Last", data: pts.map(p => p[1]), borderColor: css("--accent"), borderWidth: 1.6, pointRadius: 0}];
  if (hasQ) ds.push({label: "Bid", data: pts.map(p => p[2]), borderColor: css("--up"), borderWidth: 1, pointRadius: 0, spanGaps: true},
                    {label: "Ask", data: pts.map(p => p[3]), borderColor: css("--down"), borderWidth: 1, pointRadius: 0, spanGaps: true});
  chart("series", $("seriesChart"), {type: "line", data: {labels: pts.map(p => fmtWhen(p[0])), datasets: ds},
    options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: "index", intersect: false},
      plugins: {legend: {display: hasQ, labels: {color: css("--muted"), boxWidth: 12}}}, scales: axes()}});
  $("seriesPeriod").textContent = pts.length ? `${fmtWhen(pts[0][0])} → ${fmtWhen(pts[pts.length - 1][0])} · ${n0(d.rows)} prices` : "No prices stored";
  $("seriesNote").textContent = pts.length < d.rows ? "Chart thinned to fit; the download has every price." : "";
  $("seriesCard").scrollIntoView({behavior: "smooth", block: "start"});
}

$("expFold").addEventListener("toggle", () => { if (charts.exp) charts.exp.resize(); });
loadSummary().catch(e => $("status").textContent = "Cannot load: " + e.message);
loadCoverage(); loadDisk();
setInterval(() => { if (!document.hidden) loadSummary().catch(() => {}); }, 120000);
</script>
</body>
</html>
"""


# ---- Summary: everything important on one page ----------------------------------------------------
def _ist_now():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _pct(part, base):
    try:
        return round(part / base * 100, 2) if base else None
    except (TypeError, ZeroDivisionError):
        return None


def _india_summary(hidden):
    main = sys.modules.get("__main__")
    state = getattr(main, "STATE", None)
    if not isinstance(state, dict) or not state.get("summary"):
        return None
    s, rows = dict(state["summary"]), list(state.get("holdings") or [])
    day_of = None
    try:   # market closed: the last trading day's day P&L, as on the Portfolio page (until the next open)
        if hasattr(main, "last_session_day"):
            with main.db() as con:
                day_of = main.last_session_day(con)
                if day_of:
                    o = {"summary": s, "holdings": rows}
                    main.hold_last_session(con, o, day_of)
                    s, rows = o["summary"], o["holdings"]
    except Exception:  # noqa: BLE001 - the live figures still show
        day_of = None
    movers = sorted((r for r in rows if r.get("day_pct") is not None), key=lambda r: r["day_pct"])
    out = {"value": s.get("value"), "invested": s.get("invested"), "pnl": s.get("pnl"), "pnl_pct": s.get("pnl_pct"),
           "day_pnl": s.get("day_pnl"), "day_pct": s.get("day_pct"), "count": s.get("count"),
           "gold_pct": s.get("gold_pct"), "updated": state.get("updated"), "error": state.get("error"), "day_of": day_of,
           "logged_in": bool(state.get("logged_in")) or bool(getattr(main, "DEMO", False)),
           "login_due": _login_due(main, state),
           "best": [{"symbol": r["symbol"], "day_pct": r["day_pct"]} for r in movers[::-1][:3] if r["day_pct"] > 0],
           "worst": [{"symbol": r["symbol"], "day_pct": r["day_pct"]} for r in movers[:3] if r["day_pct"] < 0]}
    try:
        out["market_open"] = bool(main.market_open())
        out["paused"] = bool(main.PAUSED["on"])
    except Exception:  # noqa: BLE001
        pass
    if not hidden:
        try:
            with main.db() as con:
                out["alerts"] = [dict(r) for r in con.execute("SELECT ts, message FROM alerts ORDER BY ts DESC LIMIT 3")]
        except Exception:  # noqa: BLE001
            out["alerts"] = []
    else:
        for k in ("value", "invested", "pnl", "day_pnl"):
            out[k] = None
    return out


def us_installed():
    """US stocks (IBKR) is optional: its menu item and Summary card show only once the IBKR sync has a data folder."""
    return (Path(__file__).resolve().parent / "data" / "ibkr").is_dir()


def _login_due(main, state):
    """Ask for today's Zerodha login only on trading days (not weekends or NSE holidays)."""
    if state.get("logged_in") or getattr(main, "DEMO", False):
        return False
    fn = getattr(main, "login_due", None)
    try:
        return bool(fn()) if fn else True
    except Exception:  # noqa: BLE001
        return True


def _us_summary(hidden):
    snap = _json(Path(__file__).resolve().parent / "data" / "ibkr" / "ibkr_snapshot.json")
    if not snap:
        return None
    s = snap.get("summary_usd") or {}
    pos = snap.get("positions") or []
    invested = sum((p.get("qty") or 0) * (p.get("avg_cost") or 0) for p in pos)
    value = sum(p.get("market_value") or 0 for p in pos)
    day = s.get("daily_pnl")
    net = s.get("net_liquidation")
    age = None
    try:
        from datetime import datetime, timezone
        age = round((datetime.now(timezone.utc) - datetime.fromisoformat(snap["timestamp_utc"])).total_seconds() / 60)
    except (KeyError, ValueError, TypeError):
        pass
    out = {"net_liq": net, "value": value, "invested": invested, "pnl": value - invested if pos else None,
           "pnl_pct": _pct(value - invested, invested), "day_pnl": day,
           "day_pct": _pct(day, (net - day) if (net is not None and day is not None) else None),
           "usd_inr": snap.get("usd_inr"), "count": len(pos), "updated": snap.get("timestamp_ist"), "age_min": age}
    if hidden:
        for k in ("net_liq", "value", "invested", "pnl", "day_pnl"):
            out[k] = None
    return out


def _sessions_summary():
    today = _ist_now().date().isoformat()
    out = {"paper": {"running": 0, "total": 0, "today": 0.0, "pnl": 0.0, "margin": 0.0, "sessions": []},
           "live": {"running": 0, "total": 0, "today": 0.0, "pnl": 0.0, "margin": 0.0, "sessions": []}}
    market = None
    mg = (_margins() or {}).get("sessions") or {}
    for x in _slots():
        kind = "paper" if _mode(x) == "paper" else "live"
        if not can(kind):
            continue
        g = out[kind]
        running = _age(x["beat"]) < 15
        s = _json(x["state"])
        g["total"] += 1
        g["running"] += int(running)
        row = {"id": x["id"], "running": running, "mode": _mode(x),
               "label": ((_json(x["desired"]) or s or {}).get("label") or "")}
        if s:
            b = s["broker"]
            total = s["equity"] - b["capital"]
            day = 0.0
            try:
                an = _pnl_analysis(x["equity"], b["capital"], b["trades"], limit=5)
                d = next((d for d in an["daily"] if d["date"] == today), None)
                day = d["day"] if d else 0.0
            except Exception:  # noqa: BLE001
                pass
            row.update(strategy=s.get("strategy"), pnl=round(total, 2), today=round(day, 2),
                       open_legs=len(s.get("positions") or []))
            g["pnl"] += total
            g["today"] += day
            if s.get("spot") and str(s.get("time", "")).startswith(today) and (market is None or s["time"] > market["time"]):
                market = {"time": s["time"], "spot": s["spot"], "vix": s.get("vix")}
        if mg.get(x["id"]) and s and s.get("positions"):
            row["margin"] = mg[x["id"]]
            g["margin"] += mg[x["id"]]
        g["sessions"].append(row)
    for g in out.values():
        g["sessions"].sort(key=lambda r: (not r["running"], -abs(r.get("today", 0))))
        g["sessions"] = g["sessions"][:6]
        g["today"], g["pnl"], g["margin"] = round(g["today"], 2), round(g["pnl"], 2), round(g["margin"], 2)
    return out, market


_SERVICES = [("trader-app", "Dashboard"), ("algolab-paper", "Trading engine"), ("ibkr-sync", "IBKR sync"),
             ("docker", "IB Gateway (Docker)"), ("caddy", "HTTPS (Caddy)"), ("algolab-collect.timer", "Daily price saving"),
             ("algolab-margins.timer", "Margin check")]
_server_cache = {}
_STATS = RT / "server_stats.json"         # CPU and memory every minute, the last 24 hours (for the Health graphs)
_stats_lock = threading.Lock()
_stats = {"started": False, "points": [], "last": None}


def _cpu_times():
    f = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    v = [int(x) for x in f]
    idle = v[3] + (v[4] if len(v) > 4 else 0)               # idle + waiting for disk
    return sum(v), idle


def _mem_pct():
    mi = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mi[k] = int(v.split()[0])
    return round((mi["MemTotal"] - mi.get("MemAvailable", mi.get("MemFree", 0))) / mi["MemTotal"] * 100, 1)


def _sample_once():
    total, idle = _cpu_times()
    last, _stats["last"] = _stats["last"], (total, idle)
    if last is None or total <= last[0]:
        return None
    cpu = round((1 - (idle - last[1]) / (total - last[0])) * 100, 1)
    pt = [int(time.time()), max(0.0, min(100.0, cpu)), _mem_pct()]
    with _stats_lock:
        _stats["points"] = [p for p in _stats["points"] if p[0] > pt[0] - 86400] + [pt]
    return pt


def _sampler():
    n = 0
    while True:
        try:
            if _sample_once():
                n += 1
                if n % 5 == 0:
                    with _stats_lock:
                        data = list(_stats["points"])
                    tmp = _STATS.with_suffix(".tmp")
                    tmp.write_text(json.dumps(data))
                    os.replace(tmp, _STATS)
        except (OSError, ValueError, KeyError, ZeroDivisionError):
            pass
        time.sleep(60)


def _start_sampler():
    """Once per dashboard process: keep CPU and memory every minute (the history survives restarts in a file)."""
    if _stats["started"] or not Path("/proc/stat").exists():
        return
    _stats["started"] = True
    try:
        _stats["points"] = [p for p in json.loads(_STATS.read_text()) if p[0] > time.time() - 86400]
    except (OSError, ValueError, TypeError, IndexError):
        pass
    try:
        _stats["last"] = _cpu_times()
    except (OSError, ValueError):
        pass
    threading.Thread(target=_sampler, name="server-stats", daemon=True).start()


def _history(points, buckets=96):
    """[[time, cpu %, ram %], ...] averaged into about 15-minute buckets for the small graphs."""
    if not points:
        return []
    span = 86400 / buckets
    out = {}
    for t, c, m in points:
        k = int(t // span)
        a = out.setdefault(k, [0, 0.0, 0.0])
        a[0] += 1
        a[1] += c
        a[2] += m
    return [[int(k * span), round(a[1] / a[0], 1), round(a[2] / a[0], 1)] for k, a in sorted(out.items())]


def _server():
    """Memory, disk, load, uptime, services and the biggest processes (read from /proc; no root needed)."""
    if _server_cache.get("at", 0) > time.time() - 10:
        return _server_cache["data"]
    import subprocess
    out = {}
    try:
        mi = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mi[k] = int(v.split()[0]) * 1024
        out["memory"] = {"total": mi["MemTotal"], "used": mi["MemTotal"] - mi.get("MemAvailable", mi.get("MemFree", 0)),
                         "swap_total": mi.get("SwapTotal", 0), "swap_used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)}
    except (OSError, ValueError, KeyError):
        pass
    try:
        st = os.statvfs("/")
        out["disk"] = {"total": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize}
    except OSError:
        pass
    _start_sampler()
    with _stats_lock:
        pts = list(_stats["points"])
    if pts:
        out["cpu"] = pts[-1][1]
        out["cpu_avg"] = round(sum(p[1] for p in pts[-15:]) / len(pts[-15:]), 1)    # the last 15 minutes
    out["history"] = _history(pts)
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
        out["cores"] = os.cpu_count()
        out["uptime_h"] = round(float(Path("/proc/uptime").read_text().split()[0]) / 3600, 1)
    except (OSError, ValueError):
        pass
    svcs = []
    for unit, label in _SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=3)
            state = r.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            state = "unknown"
        if state in ("inactive", "unknown") and unit in ("ibkr-sync", "docker", "algolab-margins.timer"):
            try:
                r = subprocess.run(["systemctl", "cat", unit], capture_output=True, text=True, timeout=3)
                if r.returncode != 0:
                    continue          # not installed on this server
            except (OSError, subprocess.SubprocessError):
                continue
        svcs.append({"unit": unit, "label": label, "state": state})
    out["services"] = svcs
    groups = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            rss = 0
            for line in (d / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
            if not rss:
                continue
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (OSError, ValueError):
            continue
        low = cmd.lower()
        name = ("IB Gateway (Java)" if "java" in low and ("ibgateway" in low or "ibc" in low or "jts" in low) else
                "Chromium (IB Gateway login)" if "chrom" in low else
                "Trading engine" if "paper-service" in low else
                "Dashboard" if "app.py" in low else
                "IBKR sync" if "ibkr_sync" in low else
                "Caddy" if "caddy" in low else
                "Docker" if "dockerd" in low or "containerd" in low else None)
        if name is None:
            continue
        g = groups.setdefault(name, {"name": name, "rss": 0, "count": 0})
        g["rss"] += rss
        g["count"] += 1
    out["processes"] = sorted(groups.values(), key=lambda g: -g["rss"])[:7]
    _server_cache.update(at=time.time(), data=out)
    return out


def _recording_status(lines):
    """The daily price recording, from its log (data/collect_log.txt): the last run, when, whether it failed and why,
    and how many runs in a row failed. One failed run is caught up by the next (it fetches the last 7 days again)."""
    lines = [l.strip() for l in lines if l.strip()]
    last = lines[-1] if lines else ""
    in_row = 0
    for line in reversed(lines):
        if "FAILED" not in line:
            break
        in_row += 1
    when = re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", last)
    return {"last": last[:160], "when": when.group(0) if when else "", "failed": in_row > 0,
            "why": last.split("FAILED:", 1)[1].strip()[:200] if "FAILED:" in last else "",
            "failed_in_row": in_row, "next": _next_recording()}


def _health():
    main = sys.modules.get("__main__")
    feed = _json(RT / "feed_status.json")
    try:
        lines = (DATA / "collect_log.txt").read_text(encoding="utf-8").splitlines()[-10:]
    except OSError:
        lines = []
    state = getattr(main, "STATE", None) or {}
    from datetime import datetime as _d, timedelta as _td, timezone as _tz
    now = _d.now(_tz(_td(hours=5, minutes=30)))
    market = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
    if hasattr(main, "market_open"):
        market = bool(main.market_open())   # also knows the NSE holidays
    return {"engine": _age(RT / "service_heartbeat") < 45, "market_open": market,
            "feed": {"source": feed.get("source"), "age": round(_age(RT / "feed_status.json"))} if feed else None,
            "zerodha": bool(state.get("logged_in")) if state else None,
            "recording": _recording_status(lines),
            "server": _server(), "ibkr": _ibkr_status()}


@panel.route("/summary")
def summary_page():
    return Response(SUMMARY_PAGE, mimetype="text/html")


@panel.route("/api/summary")
def api_summary():
    hidden = amounts_hidden()
    out = {"time": _ist_now().strftime("%H:%M:%S"), "hidden": hidden, "owner": current_user() == owner_name()
           or not _USERS_LOADED, "name": (load_profile().get("name") or "").split(" ")[0]}
    if can("portfolio"):
        out["india"] = _india_summary(hidden)
    if can("us") and us_installed():
        out["us"] = _us_summary(hidden)
    i, u = out.get("india"), out.get("us")
    if i and u and not hidden and i.get("value") is not None and u.get("net_liq") is not None and u.get("usd_inr"):
        fx = u["usd_inr"]
        day = (i.get("day_pnl") or 0) + (u.get("day_pnl") or 0) * fx
        total = i["value"] + u["net_liq"] * fx
        out["networth"] = {"total": round(total, 2), "day": round(day, 2), "day_pct": _pct(day, total - day),
                           "india": i["value"], "us_inr": round(u["net_liq"] * fx, 2), "usd_inr": fx}
    if can("paper") or can("live"):
        sessions, market = _sessions_summary()
        if can("paper"):
            out["paper"] = sessions["paper"]
        if can("live"):
            out["live"] = sessions["live"]
        if market:
            main = sys.modules.get("__main__")
            market["login_due"] = _login_due(main, getattr(main, "STATE", None) or {})
        out["market"] = market
    out["indices"] = _summary_indices()
    if out["owner"]:
        out["health"] = _health()
    if path_allowed("/api/server-update"):
        out["server_update"] = _update_status()
    return jsonify(out)


def _summary_indices():
    """NIFTY 50, BANK NIFTY, SENSEX and India VIX with their change from the previous close, or None when
    Zerodha is not logged in (the page then shows the engine's last NIFTY price)."""
    names = [("NIFTY", "NSE:NIFTY 50"), ("BANK NIFTY", "NSE:NIFTY BANK"), ("SENSEX", "BSE:SENSEX"), ("India VIX", "NSE:INDIA VIX")]
    try:
        qs = _quotes(tuple(sym for _, sym in names))
    except Exception:  # noqa: BLE001 - not logged in, or Zerodha unreachable: the card falls back
        return None
    return [dict(_quote_view(sym, qs[sym]), name=n) for n, sym in names if sym in qs] or None


SUMMARY_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Summary · Portfolio</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--warn:#a06a1a;--accent:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--warn:#e0a84a;--accent:#6c9bff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:12px}
h1{font-size:20px;margin:0}
.when{color:var(--muted);font-size:13px}
.ibkr{margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:12px}.ibkr .ia{display:flex;gap:8px;margin-top:8px}
.ibkr button{flex:1;font:inherit;font-size:14px;padding:8px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);cursor:pointer}
.ibkr button[data-a=stop]{color:var(--down)}.ibkr button:disabled{opacity:.5}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:block;color:inherit;text-decoration:none}
a.card:hover{border-color:var(--accent)}
.card h2{font-size:13px;margin:0 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;display:flex;justify-content:space-between;gap:8px}
.card h2 span{text-transform:none;letter-spacing:0;font-weight:500}
.big{font-size:26px;font-weight:700;letter-spacing:-.01em}
.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-top:1px solid var(--line);font-size:14px}
.row:first-of-type{border-top:0}
.row .k{color:var(--muted)}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}.mut{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:3px 9px;white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);margin-right:6px;vertical-align:1px}
.dot.on{background:var(--up)}.dot.bad{background:var(--down)}.dot.warn{background:var(--warn)}
.sess{display:flex;justify-content:space-between;gap:8px;font-size:14px;padding:5px 0;border-top:1px solid var(--line)}
.sess .nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wide{grid-column:1/-1}
.alert{font-size:13px;padding:5px 0;border-top:1px solid var(--line)}.alert small{color:var(--muted);margin-left:6px}
.empty{color:var(--muted);font-size:14px}
.hide{display:none!important}
.hdr{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end}
.tg{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text);border:1px solid var(--line);border-radius:999px;padding:4px 10px;background:var(--card);cursor:pointer}
.tg input{accent-color:var(--accent);margin:0}
a.row.idx{color:inherit;text-decoration:none}
.ic{width:15px;height:15px;display:inline-block;vertical-align:-3px;flex:none}
.ic.live{color:var(--up)}.ic.stale,.ic.key,.ic.pause{color:var(--warn)}.ic.closed{color:var(--muted)}
.card h2 .ic{width:15px;height:15px}
.nums{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;font-variant-numeric:tabular-nums}
.nums .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.nums .lbl small{text-transform:none;letter-spacing:0;font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 3px}
.nums .nv{font-size:19px;font-weight:700;line-height:1.25;white-space:nowrap}
.nums .ns{font-size:12px;color:var(--muted)}
.nums>div:first-child .nv{font-size:22px}
.chip.mut{color:var(--muted);display:inline-flex;align-items:center;gap:4px}.chip .ic{width:13px;height:13px;vertical-align:-2px}
.pips{display:inline-flex;gap:2px;align-items:center;flex:none}
.pips i{width:3px;height:11px;border-radius:1px;background:var(--accent);opacity:.8}
.pips small{font-size:12px;color:var(--muted);font-weight:600;margin-left:2px}
.slist{margin-top:10px;font-variant-numeric:tabular-nums}
.srow{display:grid;grid-template-columns:minmax(0,1fr) repeat(2,minmax(64px,auto));gap:0 12px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:14px}
.srow.hd{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;padding:2px 0 4px;border-top:0}
.srow .n{text-align:right;white-space:nowrap}.srow .n.t{font-weight:700}
.sname{display:flex;align-items:center;gap:6px;min-width:0;font-weight:600}
.sname .tx{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sname .dot{margin-right:0}
.split{display:flex;height:8px;border-radius:4px;overflow:hidden;margin:10px 0 4px;background:var(--line)}
.split i{display:block;height:100%;background:var(--accent)}.split i.us{background:var(--warn)}
.splitk{display:flex;gap:14px}.splitk i{display:inline-block;width:9px;height:9px;border-radius:2px;background:var(--accent);margin-right:5px;vertical-align:0}
.splitk i.us{background:var(--warn)}
.card h2 b{color:var(--text)}
details.more{margin-top:8px;font-size:14px}
details.more summary{cursor:pointer;color:var(--accent);font-size:13px;padding:4px 0}
.btnl{display:inline-flex;align-items:center;gap:6px;margin-top:10px;font-size:14px;font-weight:600;color:var(--warn);text-decoration:none}
@media (max-width:600px){header h1{font-size:17px}.nums .nv{font-size:17px}.nums>div:first-child .nv{font-size:19px}}
a.row.idx:hover .k{color:var(--accent)}
@media (max-width:600px){.wrap{padding:12px}.grid{grid-template-columns:1fr;gap:10px}.big{font-size:24px}}
.upd{grid-column:1/-1;border-left:4px solid var(--line)}
.upd.waiting{border-left-color:var(--warn)}.upd.updating{border-left-color:var(--accent)}.upd.failed{border-left-color:var(--down)}
.upd .ut{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px}
.upd ul{margin:6px 0 0;padding-left:18px;font-size:13px;color:var(--muted)}
.upd .ua{display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap}
.upd button{font:inherit;font-weight:600;border:0;border-radius:999px;padding:8px 16px;background:var(--accent);color:#fff;cursor:pointer}
.upd button:disabled{opacity:.6}
.upd.updating .ut .dot{animation:pulse 1.2s ease-in-out infinite}@keyframes pulse{50%{opacity:.3}}
.updline{grid-column:1/-1;font-size:13px;color:var(--muted);text-decoration:none;display:flex;align-items:center;gap:4px}
</style>
</head>
<body>
<div class="wrap">
  <header><h1 id="hello">Summary</h1><span class="hdr"><label class="tg hide" id="usBox"><input type="checkbox" id="incUS"> Include US</label><span class="when" id="when"></span></span></header>
  <div class="grid" id="grid"><div class="card empty">Loading…</div></div>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const inr = (x, d = 0) => x == null ? "–" : (x < 0 ? "−" : "") + "₹" + Math.abs(x).toLocaleString("en-IN", {maximumFractionDigits: d, minimumFractionDigits: d});
const usd = (x, d = 0) => x == null ? "–" : (x < 0 ? "−" : "") + "$" + Math.abs(x).toLocaleString("en-US", {maximumFractionDigits: d, minimumFractionDigits: d});
// coloured figures carry no + / −: green and red already say which way
const sgn = (f, x, d) => x == null ? "–" : f(Math.abs(x), d);
const pct = x => x == null ? "–" : Math.abs(x).toFixed(2) + "%";
const cls = x => x == null ? "" : x >= 0 ? "up" : "down";
const short = x => { if (x == null) return "–"; const a = Math.abs(x), s = x < 0 ? "−" : "";
  return a >= 1e7 ? s + "₹" + (a / 1e7).toFixed(2) + " Cr" : a >= 1e5 ? s + "₹" + (a / 1e5).toFixed(2) + " L" : inr(x); };
function row(k, v, c) { return `<div class="row"><span class="k">${k}</span><span class="${c || ""}">${v}</span></div>`; }
function money(f, x, p, hidden) { return hidden ? `<span class="${cls(p)}">${pct(p)}</span>` : `<span class="${cls(x)}">${sgn(f, x)}</span> <span class="${cls(p)}" style="font-size:.85em">${pct(p)}</span>`; }

// line icons in place of words (the title says what each means), as on Paper > Overview
const ICONS = {
  live: '<path d="M5 12a7 7 0 0 1 2-5M19 12a7 7 0 0 0-2-5M8.5 12a3.5 3.5 0 0 1 1-2.5M15.5 12a3.5 3.5 0 0 0-1-2.5"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><path d="M12 14v6"/>',
  closed: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  stale: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  pause: '<path d="M9 5v14M15 5v14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3"/>',
  gold: '<path d="M4 18h16l-3-8H7z"/><path d="M8 10l2-5h4l2 5"/>',
  stack: '<path d="M12 4l8 4-8 4-8-4z"/><path d="M4 12l8 4 8-4M4 16l8 4 8-4"/>',
  wallet: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 15h2"/>',
  bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 21h4"/>'};
const icon = (k, title) => `<svg class="ic ${k}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title>${ICONS[k]}</svg>`;
const pips = n => n ? `<span class="pips" title="${n} open leg${n > 1 ? "s" : ""}">${"<i></i>".repeat(Math.min(n, 6))}${n > 6 ? `<small>${n}</small>` : ""}</span>` : "";
const SHORT = {short_straddle: "Positional straddle", intraday_straddle: "Intraday straddle", intraday_strangle: "Intraday strangle",
  premium_strangle: "Premium strangle", iron_fly: "Iron fly / condor", event_iron_fly: "Event iron fly", double_calendar: "Double calendar",
  put_credit_spread: "Put credit spread", blaze_butterfly: "Blaze butterfly", straddle_trend_switch: "Straddle grid trend"};
const amt = x => x == null ? "–" : Math.abs(x).toLocaleString("en-IN", {maximumFractionDigits: 0});
// two or three headline numbers side by side
function nums(list) {
  return `<div class="nums">${list.map(([lbl, v, c, sub]) => `<div><div class="lbl">${lbl}</div><div class="nv ${c || ""}">${v}</div>${sub ? `<div class="ns">${sub}</div>` : ""}</div>`).join("")}</div>`;
}
const chg = (f, x, p, h) => h ? `<span class="${cls(p)}">${pct(p)}</span>` : `<span class="${cls(x)}">${sgn(f, x)}</span>`;
const today = t => { const d = new Date(String(t).replace(" ", "T")); if (isNaN(d)) return esc(t);
  const n = new Date(), y = new Date(n - 864e5), hm = d.toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false});
  return d.toDateString() === n.toDateString() ? hm : d.toDateString() === y.toDateString() ? "Yesterday " + hm : d.toLocaleDateString("en-IN", {day: "numeric", month: "short"}) + " " + hm; };

function india(d, h) {
  if (!d) return `<a class="card" href="/"><h2>India</h2><div class="empty">No data yet.</div></a>`;
  const st = (d.login_due !== undefined ? d.login_due : !d.logged_in) ? `<span class="warn">${icon("key", "Zerodha login needed")} log in</span>` : d.paused ? `<span class="warn">${icon("pause", "Sync paused")}</span>`
    : d.market_open ? icon("live", "Market open: live prices") : icon("closed", "Market closed");
  const mv = (d.best || []).map(r => `<span class="chip up">${esc(r.symbol)} ${pct(r.day_pct)}</span>`).join("") +
             (d.worst || []).map(r => `<span class="chip down">${esc(r.symbol)} ${pct(r.day_pct)}</span>`).join("");
  return `<a class="card" href="/"><h2>India <span>${st}</span></h2>
    ${nums([["Value", h ? `<span class="${cls(d.pnl_pct)}">${pct(d.pnl_pct)}</span>` : short(d.value)],
            [d.day_of ? new Date(d.day_of + "T00:00").toLocaleDateString("en-IN", {weekday: "short", day: "numeric", month: "short"}).replace(",", "") : "Today",
             chg(inr, d.day_pnl, d.day_pct, h), "", h ? "" : `<span class="${cls(d.day_pct)}">${pct(d.day_pct)}</span>`],
            ["Total P&amp;L", chg(short, d.pnl, d.pnl_pct, h), "", h ? "" : `<span class="${cls(d.pnl_pct)}">${pct(d.pnl_pct)}</span>`]])}
    <div class="chips">${d.count != null ? `<span class="chip mut">${icon("stack", "Holdings")} ${d.count}</span>` : ""}${d.gold_pct != null ? `<span class="chip mut">${icon("gold", "Gold share")} ${d.gold_pct.toFixed(1)}%</span>` : ""}${mv}</div>
    ${d.error ? '<div class="when down" style="margin-top:6px">Last refresh failed</div>' : ""}</a>`;
}
function us(d, h) {
  if (!d) return `<a class="card" href="/us"><h2>US</h2><div class="empty">No IBKR data yet.</div></a>`;
  const stale = d.age_min != null && d.age_min > 60;
  return `<a class="card" href="/us"><h2>US <span>${stale ? `<span class="warn">${icon("stale", "Not updated for " + Math.round(d.age_min / 60) + " h")} ${Math.round(d.age_min / 60)} h</span>` : ""}</span></h2>
    ${nums([["Value", h ? `<span class="${cls(d.pnl_pct)}">${pct(d.pnl_pct)}</span>` : usd(d.net_liq), "", h || !d.usd_inr ? "" : short(d.net_liq * d.usd_inr)],
            ["Today", chg(usd, d.day_pnl, d.day_pct, h), "", h ? "" : `<span class="${cls(d.day_pct)}">${pct(d.day_pct)}</span>`],
            ["Unrealised", chg(usd, d.pnl, d.pnl_pct, h), "", h ? "" : `<span class="${cls(d.pnl_pct)}">${pct(d.pnl_pct)}</span>`]])}
    <div class="chips"><span class="chip mut">${icon("stack", "Positions")} ${d.count ?? "–"}</span></div></a>`;
}
function trading(title, href, d) {
  if (!d) return "";
  const list = d.sessions.map(s => `<div class="srow"><span class="sname"><span class="dot ${s.running ? "on" : ""}" title="${s.running ? "Running" : "Stopped"}"></span><span class="tx">${esc(s.label || SHORT[s.strategy] || s.id)}</span>${pips(s.open_legs)}${s.mode !== "paper" ? ` <span class="chip">${esc(s.mode.toUpperCase())}</span>` : ""}</span>
      <span class="n ${cls(s.today)}">${s.today != null ? amt(s.today) : "–"}</span><span class="n t ${cls(s.pnl)}">${s.pnl != null ? amt(s.pnl) : "–"}</span></div>`).join("");
  return `<a class="card" href="${href}"><h2>${title} <span><b>${d.running}/${d.total}</b> running</span></h2>
    ${nums([["Today <small>net</small>", d.total ? sgn(inr, d.today) : "–", cls(d.today)], ["Total", d.total ? sgn(short, d.pnl) : "–", cls(d.pnl)]].concat(d.margin ? [[icon("wallet", "Margin for open legs") + " Margin", short(d.margin)]] : []))}
    ${list ? `<div class="slist"><div class="srow hd"><span></span><span class="n">Today</span><span class="n">Total ₹</span></div>${list}</div>` : '<div class="empty" style="margin-top:8px">No sessions yet.</div>'}</a>`;
}
function networth(n, i) {
  const ind = n ? n.india : i.value, usi = n ? n.us_inr : 0, tot = n ? n.total : i.value;
  const day = n ? n.day : i.day_pnl, dp = n ? n.day_pct : i.day_pct, pI = tot ? ind / tot * 100 : 100;
  return `<div class="card wide"><h2>Net worth <span>${n ? `India + US · ₹${n.usd_inr.toFixed(2)}/$` : "India only"}</span></h2>
    <div class="big">${short(tot)} <span class="${cls(day)}" style="font-size:16px;font-weight:600">${sgn(inr, day)} ${pct(dp)}</span></div>
    ${n ? `<div class="split" title="India ${short(ind)} · US ${short(usi)}"><i style="width:${pI.toFixed(1)}%"></i><i class="us" style="width:${(100 - pI).toFixed(1)}%"></i></div>
    <div class="when splitk"><span><i></i>India ${short(ind)}</span><span><i class="us"></i>US ${short(usi)}</span></div>` : ""}</div>`;
}
function market(m, idx) {
  const lvl = x => Number(x).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  if (idx && idx.length) {
    const rows = idx.map(i => `<a class="row idx" href="/chart?s=${encodeURIComponent(i.symbol)}"><span class="k">${esc(i.name)}</span>
      <span><b>${lvl(i.last)}</b> ${i.change == null ? "" : `<span class="${cls(i.change)}">${i.change >= 0 ? "▲" : "▼"} ${lvl(Math.abs(i.change))} <span style="font-size:.85em">${pct(i.pct)}</span></span>`}</span></a>`).join("");
    const t = idx.map(i => i.time).filter(Boolean).sort().pop();
    return `<div class="card"><h2>Market <span class="mut">${t ? today(t) : ""}</span></h2>${rows}</div>`;
  }
  if (!m) return "";
  return `<div class="card"><h2>Market <span class="mut">${today(m.time)}</span></h2>
    ${row("NIFTY 50", Number(m.spot).toLocaleString("en-IN", {maximumFractionDigits: 2}))}${m.vix ? row("India VIX", Number(m.vix).toFixed(2)) : ""}
    ${m.login_due === false ? "" : `<div class="when" style="margin-top:4px">${icon("key", "Zerodha login needed")} Bank Nifty, Sensex and day changes after today's Zerodha login.</div>`}</div>`;
}
const gb = x => x == null ? "–" : x >= 1e9 ? (x / 1e9).toFixed(1) + " GB" : (x / 1e6).toFixed(0) + " MB";
function bar(used, total) {
  const p = total ? Math.min(100, used / total * 100) : 0, c = p > 90 ? "var(--down)" : p > 75 ? "var(--warn)" : "var(--accent)";
  return `<div style="height:6px;border-radius:3px;background:var(--line);margin:4px 0 2px"><div style="height:6px;border-radius:3px;width:${p.toFixed(0)}%;background:${c}"></div></div>`;
}
// The daily price recording: one failed run is amber (the next run fetches the last 7 days again and fills the gap);
// red when two or more runs in a row failed. The text says when and why.
function recLight(r) {
  const next = TZ.text(r.next || ""), when = r.when ? TZ.text(r.when) : "";
  if (!r.failed) return ["Price saving", "on", (when ? `Last run ${when} · ` : "") + `next ${next}`];
  const why = r.why ? `: ${r.why}` : "";
  return ["Price saving", r.failed_in_row > 1 ? "bad" : "warn", r.failed_in_row > 1
    ? `The last ${r.failed_in_row} runs failed (latest ${when}${why}). Next run ${next}.`
    : `Failed ${when}${why}. The next run (${next}) fetches the last 7 days again, so the missing prices are filled in then.`];
}
// System and Server in one card: a row of green / amber / red lights; details on tap
function ago(sec) { return sec < 90 ? Math.round(sec) + " s" : sec < 5400 ? Math.round(sec / 60) + " min" : Math.round(sec / 3600) + " h"; }
// prices: what the engine is using now, said plainly (an old price outside market hours is normal)
function prices(h) {
  const f = h.feed;
  if (!f) return ["", "No session is using Zerodha prices."];
  if (f.source === "closed" || h.market_open === false) return ["", "Market closed: live prices start at 9:15 IST."];
  const how = f.source === "stream" ? "Live stream (Zerodha ticks)" : f.source === "poll" ? "Asking Zerodha every few seconds (stream not available)" : f.source;
  if (f.age > 30) return ["warn", `No new price for ${ago(f.age)}. The engine may be waiting for today's Zerodha login, or no session uses Zerodha prices.`];
  return [f.source === "stream" ? "on" : "warn", how];
}
// a small 24-hour line (SVG), 0-100 %
function spark(hist, i, color) {
  if (!hist || hist.length < 2) return '<div class="mut" style="font-size:12px">Graph appears after a few minutes</div>';
  const w = 300, ht = 40, t0 = hist[0][0], t1 = hist[hist.length - 1][0] || t0 + 1;
  const pts = hist.map(p => `${((p[0] - t0) / Math.max(t1 - t0, 1) * w).toFixed(1)},${(ht - p[i] / 100 * ht).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${w} ${ht}" preserveAspectRatio="none" style="width:100%;height:40px;display:block" role="img" aria-label="last 24 hours">
    <line x1="0" y1="${ht * 0.25}" x2="${w}" y2="${ht * 0.25}" stroke="var(--line)" stroke-dasharray="3 3"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>
    <div class="mut" style="font-size:11px;display:flex;justify-content:space-between"><span>${ago(Date.now() / 1000 - t0)} ago</span><span>dashed line = 75%</span><span>now</span></div>`;
}
function gauge(label, pct, text, hist, i) {
  const c = pct > 90 ? "var(--down)" : pct > 75 ? "var(--warn)" : "var(--accent)";
  return `<div style="margin-top:10px"><div class="row" style="border:0;padding:0"><span class="k">${label}</span><span><b>${pct == null ? "–" : Math.round(pct) + "%"}</b> <span class="mut">${text}</span></span></div>
    ${bar(pct || 0, 100)}${hist === undefined ? "" : spark(hist, i, c)}</div>`;
}
function health(h) {
  if (!h) return "";
  const s = h.server || {}, m = s.memory, d = s.disk, pc = (u, t) => t ? Math.round(u / t * 100) : null;
  const [pcl, pt] = prices(h);
  const lights = [
    ["Engine", h.engine ? "on" : "bad", h.engine ? "Trading engine running" : "Trading engine not running"],
    ["Zerodha", h.zerodha === null ? "" : h.zerodha ? "on" : "warn", h.zerodha ? "Logged in today" : "Login needed"],
    ["Prices", pcl, pt],
    recLight(h.recording)];
  const ram = m ? pc(m.used, m.total) : null, disk = d ? pc(d.used, d.total) : null;
  const cpu = s.cpu_avg != null ? s.cpu_avg : s.load && s.cores ? Math.min(100, s.load[1] / s.cores * 100) : null;
  const res = [["CPU", cpu], ["Memory", ram], ["Disk", disk]].map(([k, p]) => [k, p == null ? "" : p > 90 ? "bad" : p > 75 ? "warn" : "on", `${k} ${Math.round(p)}% used`]);
  const all = lights.concat(res);
  const bad = all.filter(l => l[1] === "bad").length, warn = all.filter(l => l[1] === "warn").length;
  const svc = (s.services || []).map(x => `<span class="chip"><span class="dot ${x.state === "active" ? "on" : x.state === "failed" ? "bad" : "warn"}"></span>${esc(x.label)}${x.state === "active" ? "" : " · " + esc(x.state)}</span>`).join("");
  const procs = (s.processes || []).map(p => row(esc(p.name) + (p.count > 1 ? ` <span class="mut">×${p.count}</span>` : ""), gb(p.rss))).join("");
  return `<div class="card"><h2>Health <span class="${bad ? "down" : warn ? "warn" : "up"}">${bad ? bad + " problem" + (bad > 1 ? "s" : "") : warn ? warn + " to check" : "all OK"}</span></h2>
    <div class="chips">${lights.map(([k, c, t]) => `<span class="chip" title="${esc(t)}"><span class="dot ${c}"></span>${esc(k)}</span>`).join("")}</div>
    ${lights.filter(([k, c]) => (c === "bad" || c === "warn") && k !== "Zerodha" && k !== "Prices").map(([k, c, t]) =>
      `<div class="when" style="margin-top:4px"><span class="dot ${c}"></span> <b>${esc(k)}</b> · ${esc(t)}</div>`).join("")}
    ${h.zerodha === false ? `<a class="btnl" href="/login">${icon("key", "")} Log in to Zerodha</a>` : ""}
    <div class="when" style="margin-top:6px">Prices: ${esc(pt)}</div>
    ${gauge("CPU busy", cpu, s.cores ? `of ${s.cores} cores, last 15 min` : "", s.history, 1)}
    ${m ? gauge("Memory", ram, `${gb(m.used)} of ${gb(m.total)}`, s.history, 2) : ""}
    ${d ? gauge("Disk", disk, `${gb(d.total - d.used)} free of ${gb(d.total)}`) : ""}
    ${ibkrBox(h.ibkr)}
    <details class="more"><summary>Details</summary>
      ${row("Engine", esc(lights[0][2]))}${row("Zerodha", esc(lights[1][2]))}${row("Price saving", esc(lights[3][2]))}
      <div class="when" style="margin-top:6px"><b>What is price saving?</b> Every weekday at 15:45 IST the server saves the day's NIFTY and option
        prices. Backtests, replays and the weekly paper-vs-backtest check need them: a missed day is a day with no real prices.</div>
      ${s.uptime_h != null ? row("Server up", s.uptime_h >= 48 ? Math.round(s.uptime_h / 24) + " days" : s.uptime_h + " h") : ""}
      <div class="chips">${svc}</div>${procs ? `<div style="margin-top:8px"><div class="k" style="margin-bottom:2px">Memory by program</div>${procs}</div>` : ""}
    </details></div>`;
}
function server(s) { return ""; }   // part of Health now
// IBKR (US holdings): IB Gateway + the sync, started and stopped from here (two taps; the server does it as root)
let ibkrAsk = "";
function ibkrBox(k) {
  if (!k) return "";
  const on = k.gateway || k.sync, last = k.last || {};
  const st = k.busy ? (last.message || "Working") + "…" : on ? `Running${k.memory ? " · uses " + gb(k.memory) : ""}${k.gateway && !k.sync ? " · sync stopped" : !k.gateway && k.sync ? " · gateway not running" : ""}` : "Stopped";
  const btn = (a, t) => `<button type="button" class="ibkrb" data-a="${a}"${k.busy || !k.control ? " disabled" : ""}>${ibkrAsk === a ? "Tap again to " + t.toLowerCase() : t}</button>`;
  return `<div class="ibkr"><div><span class="dot ${k.busy ? "warn" : on ? "on" : ""}"></span> <b>IBKR</b> <span class="mut">${esc(st)}</span></div>
    <div class="ia">${on ? btn("restart", "Restart") + btn("stop", "Stop") : btn("start", "Start")}</div>
    ${!k.busy && last.status === "failed" ? `<div class="when down">${esc(last.message)}</div>` : !k.busy && last.message && last.status === "done" ? `<div class="when">${esc(last.message)}</div>` : ""}
    ${!k.control ? '<div class="when">Start and stop arrive with the next server update.</div>' : ""}</div>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest(".ibkrb"); if (!b) return;
  const a = b.dataset.a;
  if (ibkrAsk !== a) { ibkrAsk = a; load(); setTimeout(() => { if (ibkrAsk === a) { ibkrAsk = ""; load(); } }, 5000); return; }
  ibkrAsk = ""; b.disabled = true;
  try { await fetch("/api/ibkr-control", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: a})}); } catch (err) {}
  load(); setTimeout(load, 4000);
});
// The page redraws itself every 15 s: keep what the reader opened (Details) and where they scrolled inside a card,
// matched by the card's title and the element's place in it, so a refresh never closes what is being read.
function keepView(box, draw) {
  const key = el => { const c = el.closest(".card"), h = c && c.querySelector("h2");
    const title = h && h.childNodes[0] ? h.childNodes[0].textContent.trim() : "";
    return title + "|" + el.tagName + "|" + [...(c || box).querySelectorAll(el.tagName)].indexOf(el); };
  const opened = new Map([...box.querySelectorAll("details")].map(d => [key(d), d.open]));
  const scrolled = new Map([...box.querySelectorAll("*")].filter(e => e.scrollTop || e.scrollLeft).map(e => [key(e), [e.scrollTop, e.scrollLeft]]));
  draw();
  box.querySelectorAll("details").forEach(d => { const k = key(d); if (opened.has(k)) d.open = opened.get(k); });
  if (scrolled.size) box.querySelectorAll("*").forEach(e => { const s = scrolled.get(key(e)); if (s) { e.scrollTop = s[0]; e.scrollLeft = s[1]; } });
}
// Include US: remembered on this device; when off, the US card is hidden and net worth is India only
let withUS = true;
try { withUS = localStorage.getItem("summary.us") !== "0"; } catch (e) {}
// Server version (owner): a card while an update waits, runs or failed; one quiet line when up to date
let updAsk = false;
function updateBar(u) {
  if (!u) return "";
  const n = u.behind, more = `<a href="/profile#srvCard" style="font-size:13px">Details</a>`;
  if (u.state === "current" || u.state === "unknown" || u.state === "setup")
    return `<a class="updline" href="/profile#srvCard"><span class="dot ${u.state === "current" ? "on" : ""}"></span>${esc(u.label)}${u.at ? ` · checked ${esc(new Date(u.at).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false}))}` : ""}</a>`;
  if (u.state === "updating")
    return `<div class="card upd updating"><div class="ut"><span class="dot" style="background:var(--accent)"></span>Updating the server…</div>
      <div class="when" style="margin-top:4px">${esc(u.message || "")} · trading sessions resume by themselves; this page comes back in a minute.</div></div>`;
  if (u.state === "failed")
    return `<div class="card upd failed"><div class="ut"><span class="dot bad"></span>${esc(u.label)}</div>
      <div class="when" style="margin-top:4px">${esc(u.message || "")}</div><div class="ua">${more}</div></div>`;
  return `<div class="card upd waiting"><div class="ut"><span class="dot warn"></span>Server update waiting${n ? ` · ${n} change${n > 1 ? "s" : ""}` : ""}</div>
    ${(u.pending || []).length ? `<ul>${u.pending.slice(0, 3).map(l => `<li>${esc(l.replace(/^[0-9a-f]{7,} /, ""))}</li>`).join("")}</ul>` : ""}
    <div class="ua"><button type="button" id="updNow"${u.checking ? " disabled" : ""}>${updAsk ? "Tap again to update" : "Update now"}</button>${more}</div></div>`;
}
document.addEventListener("click", async e => {     // two taps: the update restarts the dashboard and the trading engine for a few seconds
  if (!e.target.closest || !e.target.closest("#updNow")) return;
  if (!updAsk) { updAsk = true; e.target.textContent = "Tap again to update"; setTimeout(() => { updAsk = false; load(); }, 5000); return; }
  updAsk = false; e.target.disabled = true; e.target.textContent = "Starting…";
  try { await fetch("/api/server-update", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "update"})}); } catch (err) {}
  load();
});
function alerts(a) {
  if (!a || !a.length) return "";
  return `<div class="card"><h2>Alerts <span class="mut">${icon("bell", "Price alerts")}</span></h2>${a.slice(0, 5).map(x => `<div class="alert">${esc(x.message)}<small>${today(x.ts)}</small></div>`).join("")}</div>`;
}
async function load() {
  try {
    const r = await fetch("/api/summary", {cache: "no-store"});
    if (r.status === 401) { location.href = "/signin?next=/summary"; return; }
    const d = await r.json();
    const hr = new Date().getHours();
    $("hello").textContent = (hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening") + (d.name ? ", " + d.name : "");
    $("when").textContent = (TZ.hm(d.time) + " " + TZ.label).trim();
    let html = updateBar(d.server_update);
    $("usBox").classList.toggle("hide", !("us" in d));
    if ("us" in d && withUS) { if (d.networth) html += networth(d.networth); }
    else if (d.india && !d.hidden && d.india.value != null) html += networth(null, d.india);
    html += market(d.market, d.indices);
    if ("india" in d) html += india(d.india, d.hidden);
    if ("us" in d && withUS) html += us(d.us, d.hidden);
    if ("paper" in d) html += trading("Paper trading", "/paper/overview", d.paper);
    if ("live" in d && d.live.total) html += trading("Live &amp; shadow", "/live/overview", d.live);
    html += (d.india ? alerts(d.india.alerts) : "") + health(d.health);
    keepView($("grid"), () => { $("grid").innerHTML = html || '<div class="card empty">Nothing to show for your login yet.</div>'; });
  } catch (e) { $("when").textContent = "Could not refresh: " + e.message; }
}
$("incUS").checked = withUS;
$("incUS").onchange = () => { withUS = $("incUS").checked; try { localStorage.setItem("summary.us", withUS ? "1" : "0"); } catch (e) {} load(); };
load();
setInterval(() => { if (!document.hidden) load(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
</script>
</body>
</html>
"""


# ---- profile: name, photo, theme -------------------------------------------------------------
# Kept beside the dashboard (its data folder), so it stays when the engine is updated.
ACCENTS = {"blue": ("#2f6fed", "#6c9bff"), "violet": ("#6d4ae8", "#a08bff"), "teal": ("#0f8b8d", "#3cc4c6"),
           "green": ("#1f8a4c", "#4cc985"), "orange": ("#c65a0b", "#ff9a4d"), "pink": ("#c2337a", "#f06aa8"),
           "slate": ("#475569", "#9aa8bd")}
START_PAGES = {"/summary": "Summary", "/": "Portfolio", "/stock": "Stocks", "/us": "US stocks", "/paper": "Paper trading",
               "/live": "Live trading", "/backtests": "Backtests", "/market-data": "Market data"}
THEMES = ("system", "light", "dark")
DEFAULT_PROFILE = {"name": "", "email": "", "about": "", "theme": "system", "accent": "blue", "start_page": "/",
                   "photo": "", "photo_v": 0, "time_zone": "ist"}
TIME_ZONES = ("ist", "local")
_PHOTO_TYPES = {b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"\x89PNG": ("png", "image/png")}
_profile_cache = {}


def _profile_dir():
    for d in (Path(__file__).resolve().parent / "data", RT):
        if d.is_dir() and os.access(d, os.W_OK):
            return d
    return Path(__file__).resolve().parent / "data"


def load_profile(user=None):
    """The signed-in person's profile (or `user`'s)."""
    path = user_dir(user) / "profile.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return dict(DEFAULT_PROFILE)
    hit = _profile_cache.get(str(path))
    if hit is None or hit[0] != mtime:
        hit = _profile_cache[str(path)] = (mtime, {**DEFAULT_PROFILE, **(_json(path, {}) or {})})
    return dict(hit[1])


def _save_profile(prof):
    d = user_dir()
    d.mkdir(parents=True, exist_ok=True)
    _write(d / "profile.json", prof)
    _profile_cache.clear()


@panel.route("/profile")
def profile_page():
    return Response(PROFILE_PAGE, mimetype="text/html")


@panel.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    prof = load_profile()
    if request.method == "POST":
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Expected JSON."), 400
        for key, limit in (("name", 60), ("email", 120), ("about", 200)):
            if key in body:
                prof[key] = re.sub(r"\s+", " ", str(body[key] or "")).strip()[:limit]
        if prof["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", prof["email"]):
            return jsonify(error="That email address does not look right."), 400
        if body.get("theme") in THEMES:
            prof["theme"] = body["theme"]
        if body.get("accent") in ACCENTS:
            prof["accent"] = body["accent"]
        if body.get("start_page") in START_PAGES and path_allowed(body["start_page"]):
            prof["start_page"] = body["start_page"]
        if body.get("time_zone") in TIME_ZONES:
            prof["time_zone"] = body["time_zone"]
        try:
            _save_profile(prof)
        except OSError as e:
            return jsonify(error=f"Could not save the profile on the server: {e}"), 500
    return jsonify(profile=prof, accents=ACCENTS, themes=THEMES,
                   start_pages={k: v for k, v in START_PAGES.items() if path_allowed(k)})


# ---- email (Gmail) and Telegram alerts (owner only) ----------------------------------------------------------------
# The dashboard's config.json holds them ("email", "telegram"). The dashboard's own alerts and the Algo Lab engine
# (live / shadow sessions: fills, stops, square-offs, closes that fail) read them from there for every alert, so a
# change works at once. Passwords and tokens are never sent back to the page.
def _trader_cfg_path():
    main = sys.modules.get("__main__")
    return Path(getattr(main, "CONFIG_PATH", None) or Path(__file__).resolve().parent / "config.json")


def _alert_view(cfg):
    e, t = cfg.get("email") or {}, cfg.get("telegram") or {}
    return {"email": {"enabled": bool(e.get("enabled")), "from": e.get("from") or "", "to": e.get("to") or "",
                      "has_password": bool(e.get("app_password")) and "PASTE" not in str(e.get("app_password"))},
            "telegram": {"enabled": bool(t.get("enabled")), "chat_id": str(t.get("chat_id") or ""),
                         "has_token": bool(t.get("bot_token"))}}


def _telegram(token, method, data=None, timeout=15):
    import urllib.parse
    import urllib.request
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", body, timeout=timeout) as r:
        return json.loads(r.read().decode())


@panel.route("/api/alert-channels", methods=["GET", "POST"])
def api_alert_channels():
    """GET: what is set (no password or token). POST {email: {enabled, from, to, app_password}, telegram: {enabled,
    bot_token, chat_id}}: save. An empty password or token keeps the saved one."""
    path = _trader_cfg_path()
    cfg = _json(path, None)
    if not isinstance(cfg, dict):
        return jsonify(error=f"The dashboard settings ({path.name}) could not be read."), 500
    if request.method == "GET":
        return jsonify(_alert_view(cfg))
    body = request.get_json(silent=True) or {}
    e_in, t_in = body.get("email") or {}, body.get("telegram") or {}
    e, t = dict(cfg.get("email") or {}), dict(cfg.get("telegram") or {})
    e["enabled"] = bool(e_in.get("enabled"))
    for k in ("from", "to"):
        e[k] = str(e_in.get(k) or "").strip()
    pw = "".join(str(e_in.get("app_password") or "").split())     # Google shows it in groups of four
    if pw:
        e["app_password"] = pw
    if "PASTE" in str(e.get("app_password", "")):
        e["app_password"] = ""
    e.setdefault("smtp_host", "smtp.gmail.com")
    e.setdefault("smtp_port", 465)
    t["enabled"] = bool(t_in.get("enabled"))
    t["chat_id"] = str(t_in.get("chat_id") or "").strip()
    tok = str(t_in.get("bot_token") or "").strip()
    if tok:
        t["bot_token"] = tok
    if e["enabled"]:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e["from"]):
            return jsonify(error="Gmail: enter the Gmail address that sends the alerts."), 400
        if not e["to"]:
            e["to"] = e["from"]
        if not e.get("app_password"):
            return jsonify(error="Gmail: paste the 16-letter app password (not your Gmail password)."), 400
    if t["enabled"]:
        if not re.fullmatch(r"\d+:[\w-]{20,}", t.get("bot_token") or ""):
            return jsonify(error="Telegram: paste the bot token BotFather gave you (it looks like 123456:ABC...)."), 400
        if not re.fullmatch(r"-?\d+", t["chat_id"]):
            return jsonify(error="Telegram: tap Find my chat id after sending your bot a message."), 400
    cfg["email"], cfg["telegram"] = e, t
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as ex:
        return jsonify(error=f"Could not save the settings: {ex}"), 500
    main = sys.modules.get("__main__")
    live = getattr(main, "CFG", None)
    if isinstance(live, dict):                     # the dashboard's own alerts use the new settings at once
        live["email"], live["telegram"] = e, t
    return jsonify(_alert_view(cfg))


@panel.route("/api/alert-channels/test", methods=["POST"])
def api_alert_channels_test():
    """Send a test through every channel that is on; one line per channel."""
    main = sys.modules.get("__main__")
    send = getattr(main, "notify", None)
    if send is None:
        return jsonify(error="The dashboard's alert sender is not available."), 500
    out = send("Algo Lab test alert", "Alerts from the dashboard and from live trading will arrive here.")
    return jsonify(result=out)


@panel.route("/api/alert-channels/telegram-chat", methods=["POST"])
def api_alert_channels_chat():
    """Find the chat id: the newest chat that sent the bot a message (send the bot any message first)."""
    body = request.get_json(silent=True) or {}
    tok = str(body.get("bot_token") or "").strip() or str((_json(_trader_cfg_path(), {}) or {}).get("telegram", {}).get("bot_token") or "")
    if not tok:
        return jsonify(error="Paste the bot token first."), 400
    try:
        me = _telegram(tok, "getMe")
        upd = _telegram(tok, "getUpdates", {"limit": 20, "timeout": 0})
    except Exception as ex:  # noqa: BLE001
        msg = str(ex)
        if "401" in msg or "404" in msg:
            msg = "Telegram did not accept this bot token. Copy it again from BotFather."
        return jsonify(error=msg.replace(tok, "…")), 400
    chats = []
    for u in reversed(upd.get("result") or []):
        m = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        ch = m.get("chat") or {}
        if ch.get("id") is not None and all(c["id"] != ch["id"] for c in chats):
            name = ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("username") or ""
            chats.append({"id": str(ch["id"]), "name": name})
    bot = (me.get("result") or {}).get("username") or ""
    if not chats:
        return jsonify(error=f"No message found. Open Telegram, send any message to @{bot}, then tap again.", bot=bot), 404
    return jsonify(chat_id=chats[0]["id"], name=chats[0]["name"], bot=bot, chats=chats)


# ---- update the server from GitHub (owner only; see deploy/self_update.sh) ----------------------------------
@panel.route("/api/server-update", methods=["GET", "POST"])
def api_server_update():
    """GET: running version, latest merged version on GitHub, last result and log. POST {action: check|update}:
    ask the server's updater (a root service watching for runtime/self-update.request) to do it."""
    req = RT / "self-update.request"
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("check", "update"):
            return jsonify(error="Unknown action."), 400
        try:
            RT.mkdir(parents=True, exist_ok=True)
            req.write_text(action)
        except OSError as e:
            return jsonify(error=f"Could not ask the server: {e}"), 500
    st = _json(RT / "self-update.json", {}) or {}
    installed = (Path(os.environ.get("SELF_UPDATE_HOME", "/opt/algo-trading")) / "self_update.sh").exists()
    try:
        log = (RT / "self-update.log").read_text(errors="replace").splitlines()[-80:]
    except OSError:
        log = []
    return jsonify(**{**st, "installed": installed, "requested": req.exists(), "log": log,
                      "repo_keys_url": _repo_keys_url(), "summary": _update_status()})


def _ibkr_status(server=None):
    """IBKR on this server: installed?, IB Gateway and the sync running?, its memory, and the last start / stop asked
    from the dashboard (deploy/ibkr_control.sh writes runtime/ibkr-control.json)."""
    srv = server or _server()
    svc = {x["unit"]: x["state"] for x in srv.get("services", [])}
    if "ibkr-sync" not in svc:
        return None
    procs = {p["name"]: p for p in srv.get("processes", [])}
    gw = procs.get("IB Gateway (Java)")
    ctl = _json(RT / "ibkr-control.json", {}) or {}
    return {"gateway": bool(gw), "sync": svc.get("ibkr-sync") == "active",
            "memory": (gw or {}).get("rss", 0) + (procs.get("IBKR sync") or {}).get("rss", 0),
            "busy": (RT / "ibkr.request").exists() or ctl.get("status") == "working",
            "last": ctl, "control": (Path(os.environ.get("SELF_UPDATE_HOME", "/opt/algo-trading")) / "ibkr_control.sh").exists()}


@panel.route("/api/ibkr-control", methods=["GET", "POST"])
def api_ibkr_control():
    """GET: IBKR's state. POST {action: start|stop|restart}: ask the root helper (algolab-ibkr.path) to do it."""
    if request.method == "POST":
        action = (request.get_json(silent=True) or {}).get("action")
        if action not in ("start", "stop", "restart"):
            return jsonify(error="Unknown action."), 400
        try:
            RT.mkdir(parents=True, exist_ok=True)
            (RT / "ibkr.request").write_text(action)
        except OSError as e:
            return jsonify(error=f"Could not ask the server: {e}"), 500
        _server_cache.clear()
    return jsonify(ibkr=_ibkr_status())


UPDATE_WORDS = {"current": "Up to date", "waiting": "Update waiting", "updating": "Updating…", "failed": "Update failed",
                "setup": "Updates not set up", "unknown": "Not checked yet"}


def _update_status():
    """Where the server's code stands against GitHub, in one word for the menu, Summary and Profile: updating,
    waiting (merged changes not on the server yet), current, failed (the last update or check), setup (the updater or
    its GitHub key is missing) or unknown. The server checks GitHub quietly every 30 minutes (self_update.sh peek)."""
    st = _json(RT / "self-update.json", {}) or {}
    try:
        req = (RT / "self-update.request").read_text().strip()
    except OSError:
        req = ""
    installed = (Path(os.environ.get("SELF_UPDATE_HOME", "/opt/algo-trading")) / "self_update.sh").exists()
    status, behind = st.get("status"), st.get("behind")
    if req == "update" or (status == "working" and str(st.get("message", "")).startswith("Updating")):
        state = "updating"
    elif not installed or status == "needs_key":
        state = "setup"
    elif status == "failed":
        state = "failed"
    elif isinstance(behind, int) and behind > 0:
        state = "waiting"
    elif behind == 0:
        state = "current"
    else:
        state = "unknown"
    return {"state": state, "label": UPDATE_WORDS[state], "behind": behind if isinstance(behind, int) else None,
            "deployed": str(st.get("deployed") or "")[:7], "pending": list(st.get("pending") or [])[:5],
            "message": st.get("message", ""), "at": st.get("at", ""), "checking": req == "check" or (
                status == "working" and str(st.get("message", "")).startswith("Checking"))}


def _repo_keys_url():
    """GitHub's "add deploy key" page for the repo this server updates from (None until one is set)."""
    home = Path(os.environ.get("SELF_UPDATE_HOME", "/opt/algo-trading"))
    try:
        repo = (home / "repo").read_text().strip()
    except OSError:
        return None
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", repo)
    return f"https://github.com/{m.group(1)}/settings/keys/new" if m else None


@panel.route("/api/profile/photo", methods=["POST", "DELETE"])
def api_profile_photo():
    prof, d = load_profile(), user_dir()
    if request.method == "DELETE":
        for old in d.glob("profile_photo.*"):
            old.unlink(missing_ok=True)
        prof.update(photo="", photo_v=int(time.time()))
        _save_profile(prof)
        return jsonify(ok=True, profile=prof)
    body = request.get_json(silent=True) or {}
    m = re.fullmatch(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=\s]+)", str(body.get("data", "")))
    if not m:
        return jsonify(error="Send the photo as an image."), 400
    raw = base64.b64decode(m.group(1))
    kind = next((v for k, v in _PHOTO_TYPES.items() if raw.startswith(k)), None)
    if kind is None and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind = ("webp", "image/webp")
    if kind is None:
        return jsonify(error="Use a JPEG, PNG or WebP photo."), 400
    if len(raw) > 2_000_000:
        return jsonify(error="The photo is too large (over 2 MB)."), 400
    try:
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("profile_photo.*"):
            old.unlink(missing_ok=True)
        (d / f"profile_photo.{kind[0]}").write_bytes(raw)
        prof.update(photo=f"profile_photo.{kind[0]}", photo_v=int(time.time()))
        _save_profile(prof)
    except OSError as e:
        return jsonify(error=f"Could not save the photo on the server: {e}"), 500
    return jsonify(ok=True, profile=prof)


@panel.route("/profile/photo")
def profile_photo():
    prof = load_profile()
    p = user_dir() / prof.get("photo", "")
    if not prof.get("photo") or not p.is_file():
        return Response(status=404)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(p.suffix[1:], "application/octet-stream")
    resp = send_file(p, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=31536000"
    return resp


PROFILE_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile · Portfolio</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 14px}
h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px}
.me{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.photo{width:96px;height:96px;border-radius:50%;object-fit:cover;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-size:34px;font-weight:700;flex:none}
.hello{font-size:22px;font-weight:650}
.sub{color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
input[type=text],input[type=email],select,textarea{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);font:inherit}
textarea{resize:vertical;min-height:60px}
.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:inherit;font-size:14px;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.btn.stop{background:transparent;color:var(--down);border:1px solid var(--line)}
.btn:disabled{opacity:.5}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.themes{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.th{border:1px solid var(--line);border-radius:10px;padding:8px;cursor:pointer;background:transparent;color:var(--text);font:inherit;text-align:center}
.th.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.pv{height:58px;border-radius:7px;margin-bottom:6px;display:flex;overflow:hidden;border:1px solid var(--line)}
.pv i{flex:1;display:block}
.pv .l{background:#f5f6f8}.pv .l::after,.pv .d::after{content:"";display:block;margin:10px 8px;height:10px;border-radius:3px;background:var(--accent)}
.pv .d{background:#0f1115}
.swatches{display:flex;gap:10px;flex-wrap:wrap}
.sw{width:36px;height:36px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;outline-offset:2px}
.sw.on{outline:2px solid var(--text)}
.msg{font-size:13px}.msg.ok{color:var(--up)}.msg.err{color:var(--down)}
.srvpre{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all;background:var(--bg);border-radius:8px;padding:8px;margin:6px 0 0;max-height:260px;overflow:auto;color:var(--muted)}
.hide{display:none!important}
.hide{display:none}
.srvst{display:flex;align-items:center;gap:10px;font-size:18px;margin:-4px 0 6px}
.srvst .sd{width:12px;height:12px;border-radius:50%;background:var(--muted);flex:none}
.srvst.current .sd{background:var(--up)}.srvst.current b{color:var(--up)}
.srvst.waiting .sd{background:#e8a317}.srvst.failed .sd{background:var(--down)}.srvst.failed b{color:var(--down)}
.srvst.updating .sd{background:var(--accent);animation:srvp 1.2s ease-in-out infinite}@keyframes srvp{50%{opacity:.3}}
.item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}
.item:last-child{border-bottom:0}
.item .sub{font-size:12px}
.chk{display:flex;gap:10px;align-items:flex-start;padding:7px 0;font-size:14px;cursor:pointer}
.chk input{margin-top:3px}
.link{background:none;border:0;color:var(--down);cursor:pointer;font:inherit;font-size:13px;padding:0}
input[type=password]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);font:inherit}
.ach{border-top:1px solid var(--line);margin-top:12px;padding-top:4px}
.ach:first-of-type{border-top:0;margin-top:0;padding-top:0}
.ach .chk{font-weight:600}
.ach ol{padding-left:18px;margin:4px 0 0}
.ach ol li{margin:2px 0}
.alst{font-size:13px;margin-left:auto;color:var(--muted)}.alst.on{color:var(--up)}
#alCard a{color:var(--accent)}
@media (max-width:520px){.themes{grid-template-columns:repeat(3,1fr)}.photo{width:80px;height:80px;font-size:28px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Profile</h1>

  <div class="card hide" id="srvCard">
    <h2>Server version</h2>
    <div class="srvst" id="srvState"><span class="sd"></span><b>Checking…</b></div>
    <div class="sub" id="srvLine">Loading…</div>
    <div id="srvKey" class="hide" style="margin-top:10px">
      <div class="sub">One-time setup: give the server read-only access to the code on GitHub.</div>
      <ol class="sub" style="padding-left:18px;margin:6px 0">
        <li>Copy this key: <button class="btn ghost" id="srvCopy" type="button" style="padding:4px 10px;font-size:13px">Copy</button></li>
        <li>Open <a id="srvKeyLink" href="#" target="_blank" rel="noopener">GitHub › Deploy keys › Add</a>, title "Lightsail server", paste the key, leave "Allow write access" <b>off</b>, Add key.</li>
        <li>Come back and tap <b>Check</b>.</li>
      </ol>
      <pre class="srvpre" id="srvPub"></pre>
    </div>
    <ul class="sub" id="srvPending" style="padding-left:18px;margin:8px 0"></ul>
    <div class="row" style="margin-top:8px">
      <button class="btn ghost" id="srvCheck" type="button">Check</button>
      <button class="btn" id="srvUpdate" type="button">Update now</button>
      <span class="msg" id="srvMsg"></span>
    </div>
    <details style="margin-top:8px"><summary class="sub">Version and last update log</summary><pre class="srvpre" id="srvLog"></pre></details>
  </div>

  <div class="card hide" id="alCard">
    <h2>Email and Telegram alerts</h2>
    <div class="sub">Live and shadow sessions send an alert for every order, protective stop, square-off and close that
      fails; the dashboard sends its price alerts. They go to your phone's notifications already; switch on Gmail and
      Telegram to get them there too.</div>
    <div class="ach">
      <label class="chk"><input type="checkbox" id="alEmailOn"> Gmail <span class="alst" id="alEmailSt"></span></label>
      <div id="alEmailBox">
        <label for="alFrom">Gmail address that sends the alerts</label>
        <input id="alFrom" type="email" maxlength="120" autocomplete="off" placeholder="you@gmail.com">
        <label for="alPw">App password</label>
        <input id="alPw" type="password" maxlength="40" autocomplete="new-password" placeholder="16 letters">
        <div class="sub" style="margin-top:4px">Not your Gmail password: make one at
          <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">Google › App passwords</a>
          (needs 2-Step Verification), name it "Algo Lab", copy the 16 letters here.</div>
        <label for="alTo">Send to (empty = the same address)</label>
        <input id="alTo" type="email" maxlength="120" autocomplete="off">
      </div>
    </div>
    <div class="ach">
      <label class="chk"><input type="checkbox" id="alTgOn"> Telegram <span class="alst" id="alTgSt"></span></label>
      <div id="alTgBox">
        <ol class="sub">
          <li>In Telegram, open <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>, send
            <b>/newbot</b>, choose a name, and copy the token it gives you.</li>
          <li>Open your new bot and send it any message (e.g. "hi").</li>
          <li>Paste the token below and tap <b>Find my chat id</b>.</li>
        </ol>
        <label for="alTok">Bot token</label>
        <input id="alTok" type="password" maxlength="80" autocomplete="off" placeholder="123456789:AA...">
        <label for="alChat">Chat id</label>
        <div class="row" style="margin-top:0">
          <input id="alChat" type="text" maxlength="20" autocomplete="off" style="flex:1;min-width:140px">
          <button class="btn ghost" id="alFind" type="button">Find my chat id</button>
        </div>
      </div>
    </div>
    <div class="row">
      <button class="btn" id="alSave" type="button">Save</button>
      <button class="btn ghost" id="alTest" type="button">Send a test</button>
      <span class="msg" id="alMsg"></span>
    </div>
  </div>

  <div class="card">
    <div class="me">
      <div id="photoBox"></div>
      <div style="flex:1;min-width:180px">
        <div class="hello" id="hello">–</div>
        <div class="sub" id="helloSub"></div>
        <div class="row">
          <label class="btn ghost" style="margin:0">Change photo<input type="file" id="file" accept="image/*" class="hide"></label>
          <button class="btn stop hide" id="rmPhoto" type="button">Remove photo</button>
          <span class="msg" id="photoMsg"></span>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>About you</h2>
    <label for="name">Name</label><input id="name" type="text" maxlength="60" autocomplete="name">
    <label for="email">Email (shown here only)</label><input id="email" type="email" maxlength="120" autocomplete="email">
    <label for="about">Note</label><textarea id="about" maxlength="200" placeholder="e.g. trading goals, risk limits"></textarea>
  </div>

  <div class="card">
    <h2>Appearance</h2>
    <div class="themes" id="themes"></div>
    <label>Accent colour</label>
    <div class="swatches" id="swatches"></div>
    <label for="start">Open this page after signing in</label>
    <select id="start"></select>
    <label for="tz">Show times in</label>
    <select id="tz"><option value="ist">India time (IST)</option><option value="local" id="tzLocal">This device's time</option></select>
  </div>

  <div class="row" style="margin-bottom:12px"><button class="btn" id="save" type="button">Save</button><span class="msg" id="msg"></span></div>

  <div class="card" id="installCard">
    <h2>Install as an app</h2>
    <div class="sub" id="installText"></div>
  </div>

  <div class="card">
    <h2>Face ID / fingerprint sign-in</h2>
    <div class="sub" id="pkText">Sign in with Face ID, Touch ID or your phone's fingerprint instead of the password. Set it up once on each device.</div>
    <div id="pkList"></div>
    <div class="row"><button class="btn" id="pkAdd" type="button">Set up on this device</button><span class="msg" id="pkMsg"></span></div>
  </div>

  <div class="card">
    <h2>Notifications</h2>
    <div class="sub" id="nText"></div>
    <div id="nPrefs"></div>
    <div class="row"><button class="btn" id="nOn" type="button">Turn on for this device</button>
      <button class="btn ghost" id="nTest" type="button">Send a test</button><span class="msg" id="nMsg"></span></div>
    <div id="nDevices"></div>
  </div>

  <div class="card">
    <h2>Account</h2>
    <div class="sub" id="acctText"></div>
    <div class="row" style="margin-top:8px">
      <a class="btn ghost hide" id="acctPw" href="/account/password" style="text-decoration:none">Change password</a>
      <a class="btn ghost hide" id="acctUsers" href="/users" style="text-decoration:none">Users and access</a>
      <a class="btn stop" href="/signout" style="text-decoration:none">Sign out</a></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let P = null, A = {}, theme = "system", accent = "blue";
const dark = () => theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);

// ---- server version: update from GitHub (owner only; other logins never see this card) ----
let srvAsk = false, srvTimer = null;
const sha = s => s ? s.slice(0, 7) : "–";
async function srvLoad() {
  let s;
  try { s = await api("/api/server-update"); } catch (e) {
    if (/HTTP 403|not allowed/i.test(e.message)) return;            // not the owner
    $("srvCard").classList.remove("hide"); $("srvMsg").className = "msg"; $("srvMsg").textContent = "Restarting… (the dashboard restarts during an update)";
    $("srvState").className = "srvst updating"; $("srvState").innerHTML = '<span class="sd"></span><b>Updating…</b>';
    srvTimer = setTimeout(srvLoad, 4000); return;
  }
  $("srvCard").classList.remove("hide");
  const busy = s.requested || s.status === "working";
  const u = s.summary || {}, word = u.state === "waiting" && u.behind ? `${u.label} · ${u.behind} change${u.behind > 1 ? "s" : ""}` : (u.label || "");
  $("srvState").className = "srvst " + (u.state || "");
  $("srvState").innerHTML = `<span class="sd"></span><b>${esc(word)}</b>`;
  $("srvUpdate").classList.toggle("ghost", u.state === "current");      // the main button only when there is something to install
  if (!s.installed) $("srvLine").textContent = "Not set up yet: run one update from the Mac (update-server.command) to install the updater.";
  else if (s.status === "needs_key") $("srvLine").textContent = "GitHub does not know this server yet.";
  else $("srvLine").innerHTML = (s.behind ? `<b>${s.behind}</b> update${s.behind > 1 ? "s" : ""} waiting` : "")
    + (s.at ? `${s.behind ? " · " : ""}checked ${esc(new Date(s.at).toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}))}` : "");
  $("srvKey").classList.toggle("hide", s.status !== "needs_key");
  $("srvPub").textContent = s.public_key || ""; $("srvKeyLink").href = s.repo_keys_url || "https://github.com";
  const plain = t => String(t || "").replace(/\b[0-9a-f]{7,40}\b ?/g, "").trim();     // drop commit codes
  $("srvPending").innerHTML = (s.pending || []).map(l => `<li>${esc(plain(l))}</li>`).join("");
  $("srvLog").textContent = (s.deployed ? `Version ${sha(s.deployed)}\n\n` : "") + ((s.log || []).join("\n") || "No update from GitHub yet.");
  $("srvCheck").disabled = busy || !s.installed; $("srvUpdate").disabled = busy || !s.installed || s.status === "needs_key";
  $("srvUpdate").textContent = srvAsk ? "Tap again to update" : "Update now";
  const m = $("srvMsg");
  m.className = "msg" + (s.status === "failed" ? " err" : s.status === "updated" ? " ok" : "");
  m.textContent = busy ? (s.status === "working" ? plain(s.message) + "…" : "Starting…") : s.status === "needs_key" || (s.status === "updated" && s.behind === 0) ? "" : plain(s.message);
  clearTimeout(srvTimer);
  if (busy) srvTimer = setTimeout(srvLoad, 3000);
}
async function srvDo(action) {
  try { await api("/api/server-update", {method: "POST", body: JSON.stringify({action})}); } catch (e) { $("srvMsg").className = "msg err"; $("srvMsg").textContent = e.message; return; }
  srvAsk = false; srvLoad();
}
$("srvCopy").onclick = () => { navigator.clipboard && navigator.clipboard.writeText($("srvPub").textContent); $("srvCopy").textContent = "Copied"; };
$("srvCheck").onclick = () => srvDo("check");
$("srvUpdate").onclick = () => {   // two taps: the update restarts the paper engine and the dashboard for a few seconds
  if (!srvAsk) { srvAsk = true; $("srvUpdate").textContent = "Tap again to update"; setTimeout(() => { srvAsk = false; $("srvUpdate").textContent = "Update now"; }, 5000); return; }
  srvDo("update");
};

async function api(url, opts) {
  const r = await fetch(url, Object.assign({cache: "no-store", headers: {"Content-Type": "application/json"}}, opts || {}));
  if (r.status === 401) { location.href = "/signin?next=/profile"; throw new Error("sign in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
function greet(name) {
  const h = new Date().getHours(), part = h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  return name ? `${part}, ${name.split(" ")[0]}` : part;
}
function drawPhoto() {
  const n = (P.name || "").trim(), ini = n.split(/\s+/).slice(0, 2).map(w => w[0] || "").join("").toUpperCase() || "Me";
  $("photoBox").innerHTML = P.photo ? `<img class="photo" src="/profile/photo?v=${P.photo_v}" alt="">` : `<div class="photo">${esc(ini)}</div>`;
  $("rmPhoto").classList.toggle("hide", !P.photo);
  $("hello").textContent = greet(n);
  $("helloSub").textContent = P.email || (n || P.photo ? "Your dashboard profile" : "Add your name, photo and theme below.");
}
function drawChoices() {
  const L = {system: "Same as device", light: "Light", dark: "Dark"};
  $("themes").innerHTML = ["system", "light", "dark"].map(t => `<button type="button" class="th ${t === theme ? "on" : ""}" data-t="${t}">
    <div class="pv">${t === "dark" ? '<i class="d"></i>' : t === "light" ? '<i class="l"></i>' : '<i class="l"></i><i class="d"></i>'}</div>${L[t]}</button>`).join("");
  document.querySelectorAll(".th").forEach(b => b.onclick = () => { theme = b.dataset.t; drawChoices(); });
  $("swatches").innerHTML = Object.entries(A).map(([k, v]) => `<button type="button" class="sw ${k === accent ? "on" : ""}" data-a="${k}" title="${k}" style="background:${dark() ? v[1] : v[0]}"></button>`).join("");
  document.querySelectorAll(".sw").forEach(b => b.onclick = () => {
    accent = b.dataset.a; document.documentElement.style.setProperty("--accent", dark() ? A[accent][1] : A[accent][0]); drawChoices();
  });
}
async function load() {
  const j = await api("/api/profile");
  P = j.profile; A = j.accents; theme = P.theme; accent = P.accent;
  $("name").value = P.name; $("email").value = P.email; $("about").value = P.about;
  $("tz").value = P.time_zone || "ist";
  $("tzLocal").textContent = "This device's time (" + (Intl.DateTimeFormat().resolvedOptions().timeZone || "local") + ")";
  $("start").innerHTML = Object.entries(j.start_pages).map(([k, v]) => `<option value="${k}" ${k === P.start_page ? "selected" : ""}>${esc(v)}</option>`).join("");
  drawPhoto(); drawChoices();
}
$("save").onclick = async () => {
  $("msg").className = "msg"; $("msg").textContent = "";
  try {
    $("save").disabled = true;
    const before = P.theme + P.accent + P.name + P.photo_v + P.time_zone;
    const j = await api("/api/profile", {method: "POST", body: JSON.stringify({name: $("name").value, email: $("email").value,
      about: $("about").value, theme, accent, start_page: $("start").value, time_zone: $("tz").value})});
    P = j.profile;
    if (P.theme + P.accent + P.name + P.photo_v + P.time_zone !== before) { location.reload(); return; }   // menu and colours on this page
    $("msg").className = "msg ok"; $("msg").textContent = "Saved";
    drawPhoto();
  } catch (e) { $("msg").className = "msg err"; $("msg").textContent = e.message; } finally { $("save").disabled = false; }
};
// photos are cut to a 320 px square in the browser, so even a large phone photo uploads quickly
$("file").onchange = () => {
  const f = $("file").files[0];
  if (!f) return;
  $("photoMsg").className = "msg"; $("photoMsg").textContent = "Uploading…";
  const img = new Image();
  img.onload = async () => {
    const s = Math.min(img.naturalWidth, img.naturalHeight), c = document.createElement("canvas");
    c.width = c.height = 320;
    c.getContext("2d").drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, 320, 320);
    URL.revokeObjectURL(img.src);
    try {
      const j = await api("/api/profile/photo", {method: "POST", body: JSON.stringify({data: c.toDataURL("image/jpeg", 0.88)})});
      P = j.profile; location.reload();
    } catch (e) { $("photoMsg").className = "msg err"; $("photoMsg").textContent = e.message; }
  };
  img.onerror = () => { $("photoMsg").className = "msg err"; $("photoMsg").textContent = "That file could not be read as a photo."; };
  img.src = URL.createObjectURL(f);
};
$("rmPhoto").onclick = async () => {
  try { const j = await api("/api/profile/photo", {method: "DELETE"}); P = j.profile; location.reload(); }
  catch (e) { $("photoMsg").className = "msg err"; $("photoMsg").textContent = e.message; }
};
load().catch(e => { $("msg").className = "msg err"; $("msg").textContent = "Cannot load the profile: " + e.message; });
srvLoad();

// ---- app: install, Face ID / fingerprint, notifications -------------------------------------------
const ub = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0));
const eb = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent), standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
const deviceName = () => isIOS ? (/iPad/.test(navigator.userAgent) ? "iPad" : "iPhone") : /Android/.test(navigator.userAgent) ? "Android phone" : /Mac/.test(navigator.userAgent) ? "Mac" : "This computer";
const https = location.protocol === "https:";
$("installText").innerHTML = standalone ? "Running as an installed app on this device."
  : !https ? 'Open the dashboard at its HTTPS address to install it.'
  : isIOS ? 'iPhone / iPad: tap <b>Share</b> (the square with an arrow) in Safari, then <b>Add to Home Screen</b>. Open it from the new icon: notifications on iPhone work only there (iOS 16.4 or later).'
  : /Android/.test(navigator.userAgent) ? 'Android: open Chrome\'s menu (⋮) and tap <b>Install app</b> (or <b>Add to Home screen</b>).'
  : 'On a computer: Chrome or Edge show an install button in the address bar. On your phone, open this page and use Add to Home Screen.';

async function pkLoad() {
  const j = await api("/api/passkeys");
  $("pkList").innerHTML = j.passkeys.map(p => `<div class="item"><div>${esc(p.name)}<div class="sub">added ${esc(p.created)}${p.last_used ? " · last used " + esc(p.last_used) : ""}</div></div>
    <button class="link" data-pk="${esc(p.id)}" type="button">Remove</button></div>`).join("");
  document.querySelectorAll("[data-pk]").forEach(b => b.onclick = async () => { await api("/api/passkeys/" + encodeURIComponent(b.dataset.pk), {method: "DELETE"}); pkLoad(); });
  const ok = j.https && window.PublicKeyCredential;
  $("pkAdd").disabled = !ok;
  if (!ok) { $("pkMsg").className = "msg"; $("pkMsg").textContent = j.https ? "This browser does not support passkeys." : "Open the HTTPS address to set this up."; }
}
$("pkAdd").onclick = async () => {
  $("pkMsg").className = "msg"; $("pkMsg").textContent = "";
  try {
    const o = await api("/api/passkey/register/options", {method: "POST"});
    const pk = o.publicKey;
    pk.challenge = ub(pk.challenge); pk.user.id = ub(pk.user.id);
    pk.excludeCredentials = (pk.excludeCredentials || []).map(c => ({type: c.type, id: ub(c.id)}));
    const c = await navigator.credentials.create({publicKey: pk});
    await api("/api/passkey/register", {method: "POST", body: JSON.stringify({id: c.id, name: deviceName(),
      clientDataJSON: eb(c.response.clientDataJSON), attestationObject: eb(c.response.attestationObject)})});
    $("pkMsg").className = "msg ok"; $("pkMsg").textContent = "Done: next time, sign in with Face ID / fingerprint.";
    pkLoad();
  } catch (e) { if (e.name !== "NotAllowedError") { $("pkMsg").className = "msg err"; $("pkMsg").textContent = e.message; } }
};

let N = null, mySub = null;
const myId = async ep => { const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(ep)); return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 12); };
async function nLoad() {
  N = await api("/api/push");
  const reg = "serviceWorker" in navigator ? await navigator.serviceWorker.getRegistration("/") : null;
  mySub = reg && reg.pushManager ? await reg.pushManager.getSubscription() : null;
  const id = mySub ? await myId(mySub.endpoint) : null, me = N.devices.find(d => d.id === id);
  const supported = https && "serviceWorker" in navigator && "PushManager" in window;
  $("nText").textContent = !supported ? (isIOS && !standalone ? "On iPhone, add the app to your home screen first and open it from there (iOS 16.4 or later)." : "Notifications need the HTTPS address and a browser that supports them.")
    : me ? "On for this device. Choose what to receive:" : "Get alerts on this device: trading events, the end-of-day P&L and login reminders.";
  $("nOn").classList.toggle("hide", !!me || !supported);
  $("nTest").classList.toggle("hide", !me);
  $("nPrefs").innerHTML = me ? Object.entries(N.categories).map(([k, v]) => `<label class="chk"><input type="checkbox" data-cat="${k}" ${me.prefs[k] !== false ? "checked" : ""}> ${esc(v)}</label>`).join("") : "";
  document.querySelectorAll("[data-cat]").forEach(c => c.onchange = async () => {
    const prefs = {}; document.querySelectorAll("[data-cat]").forEach(x => prefs[x.dataset.cat] = x.checked);
    await api("/api/push/prefs", {method: "POST", body: JSON.stringify({id, prefs})});
  });
  $("nDevices").innerHTML = N.devices.length ? `<div class="sub" style="margin-top:12px">Devices with notifications:</div>` + N.devices.map(d => `<div class="item"><div>${esc(d.name)}${d.id === id ? " (this device)" : ""}<div class="sub">since ${esc(d.created)}${d.last_sent ? " · last sent " + esc(d.last_sent) : ""}</div></div>
    <button class="link" data-dev="${d.id}" type="button">Remove</button></div>`).join("") : "";
  document.querySelectorAll("[data-dev]").forEach(b => b.onclick = async () => {
    if (b.dataset.dev === id && mySub) await mySub.unsubscribe().catch(() => {});
    await api("/api/push/remove", {method: "POST", body: JSON.stringify({id: b.dataset.dev})}); nLoad();
  });
}
$("nOn").onclick = async () => {
  $("nMsg").className = "msg"; $("nMsg").textContent = "";
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") throw new Error("Notifications are blocked for this site in the phone's settings.");
    const reg = await navigator.serviceWorker.register("/sw.js", {scope: "/"});
    await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: ub(N.public_key)});
    await api("/api/push/subscribe", {method: "POST", body: JSON.stringify({subscription: sub.toJSON(), name: deviceName()})});
    await nLoad();
    $("nMsg").className = "msg ok"; $("nMsg").textContent = "On. Send a test to check.";
  } catch (e) { $("nMsg").className = "msg err"; $("nMsg").textContent = e.message; }
};
$("nTest").onclick = async () => {
  try { const id = mySub ? await myId(mySub.endpoint) : ""; await api("/api/push/test", {method: "POST", body: JSON.stringify({id})});
        $("nMsg").className = "msg ok"; $("nMsg").textContent = "Sent: it should arrive in a few seconds."; }
  catch (e) { $("nMsg").className = "msg err"; $("nMsg").textContent = e.message; }
};
pkLoad().catch(() => {}); nLoad().catch(e => { $("nText").textContent = e.message; });

// ---- Gmail / Telegram alerts (owner only) ----
let AL = null;
function alDraw() {
  const e = AL.email, t = AL.telegram;
  $("alEmailOn").checked = e.enabled; $("alFrom").value = e.from; $("alTo").value = e.to === e.from ? "" : e.to;
  $("alPw").value = ""; $("alPw").placeholder = e.has_password ? "Saved (leave empty to keep it)" : "16 letters";
  $("alTgOn").checked = t.enabled; $("alChat").value = t.chat_id;
  $("alTok").value = ""; $("alTok").placeholder = t.has_token ? "Saved (leave empty to keep it)" : "123456789:AA...";
  const st = (id, on) => { $(id).textContent = on ? "On" : "Off"; $(id).className = "alst" + (on ? " on" : ""); };
  st("alEmailSt", e.enabled); st("alTgSt", t.enabled);
  alBoxes();
}
function alBoxes() {
  $("alEmailBox").classList.toggle("hide", !$("alEmailOn").checked);
  $("alTgBox").classList.toggle("hide", !$("alTgOn").checked);
}
function alSay(text, ok) { $("alMsg").className = "msg " + (ok ? "ok" : "err"); $("alMsg").textContent = text; }
$("alEmailOn").onchange = alBoxes; $("alTgOn").onchange = alBoxes;
async function alSave() {
  AL = await api("/api/alert-channels", {method: "POST", body: JSON.stringify({
    email: {enabled: $("alEmailOn").checked, from: $("alFrom").value.trim(), to: $("alTo").value.trim(), app_password: $("alPw").value},
    telegram: {enabled: $("alTgOn").checked, bot_token: $("alTok").value.trim(), chat_id: $("alChat").value.trim()}})});
  alDraw();
}
$("alSave").onclick = async () => {
  $("alSave").disabled = true;
  try { await alSave(); alSay("Saved. Tap Send a test to check.", true); } catch (e) { alSay(e.message, false); }
  $("alSave").disabled = false;
};
$("alTest").onclick = async () => {
  $("alTest").disabled = true; alSay("Sending…", true);
  try {
    if ($("alPw").value || $("alTok").value) await alSave();          // typed but not saved yet
    const r = await api("/api/alert-channels/test", {method: "POST"});
    const lines = r.result || [], bad = lines.some(l => /failed|no channel/i.test(l));
    alSay(lines.join(" · ").replace(/^no channel enabled.*$/, "Gmail and Telegram are both off."), !bad);
  } catch (e) { alSay(e.message, false); }
  $("alTest").disabled = false;
};
$("alFind").onclick = async () => {
  $("alFind").disabled = true; alSay("Asking Telegram…", true);
  try {
    const r = await api("/api/alert-channels/telegram-chat", {method: "POST", body: JSON.stringify({bot_token: $("alTok").value.trim()})});
    $("alChat").value = r.chat_id;
    alSay(`Found: ${r.name || "your chat"} with @${r.bot}. Tap Save.`, true);
  } catch (e) { alSay(e.message, false); }
  $("alFind").disabled = false;
};
api("/api/alert-channels").then(a => { AL = a; $("alCard").classList.remove("hide"); alDraw(); }).catch(() => {});   // owner only
fetch("/api/account", {cache: "no-store"}).then(r => r.ok ? r.json() : null).then(a => {
  if (!a || !a.user) return;
  $("acctText").textContent = "Signed in as " + a.user + (a.owner ? " (owner: full access)" : "") +
    (a.hide_amounts ? ". Amounts are hidden for this login." : "");
  $("acctPw").classList.remove("hide");
  if (a.owner) $("acctUsers").classList.remove("hide");
}).catch(() => {});
</script>
</body>
</html>
"""


# ---- one menu on every page of the dashboard ------------------------------------------
# The dashboard's pages each used to carry their own few links. This adds the same menu to every
# page the dashboard serves (its own pages, this one, Backtests, US stocks...) and takes the old
# links out of each page's status line, without editing the other pages' files.
# Paper and Live open on their Overview; the Chart sits under Data (16 Oct); backtests have their own Algo Lab menu (18 Oct)
NAV = [("Summary", "/summary", ("/summary",)), ("Portfolio", "/", ("/", "/gold", "/stock", "/us")),
       ("Paper", "/paper/overview", ("/paper", "/paper/overview", "/paper/new")),
       ("Live", "/live/overview", ("/live", "/live/overview", "/positions", "/live/new", "/order-test")),
       ("Algo Lab", "/backtests/run", ("/backtests", "/backtests/run", "/backtests/sweep", "/backtests/compare")),
       ("Studio", "/studio/", ("/studio/",)),
       ("Data", "/market-data", ("/market-data", "/chart"))]
SUBNAV = {"/": [("India", "/"), ("Stocks", "/stock"), ("US", "/us")],
          "/paper/overview": [("Overview", "/paper/overview"), ("Sessions", "/paper"), ("Start a session", "/paper/new")],
          "/live/overview": [("Overview", "/live/overview"), ("Positions", "/positions"), ("Sessions", "/live"), ("Start a session", "/live/new"), ("Order test", "/order-test")],
          "/backtests/run": [("Run", "/backtests/run"), ("Sweep", "/backtests/sweep"), ("Compare with paper", "/backtests/compare"), ("Results", "/backtests")],
          "/market-data": [("Stored data", "/market-data"), ("Chart", "/chart")]}
_NAV_PATHS = {"/profile", "/summary", "/", "/stock", "/us", "/paper", "/paper/new", "/paper/overview", "/live", "/live/new", "/live/overview", "/backtests", "/backtests/run", "/backtests/sweep", "/backtests/compare", "/studio/", "/algo", "/gold", "/order-test", "/positions", "/market-data", "/chart", "/signout"}
_NO_NAV = ("/signin", "/login", "/callback", "/signout", "/offline")

NAV_CSS = """<style id="sitenav-css">
.sitenav,.sitenav *{box-sizing:border-box}
.sitenav .sn-bar,.sitenav .sn-in,.sitenav .sn-sub{height:auto;background:none;border:0;border-radius:0;box-shadow:none}
.sitenav{position:sticky;top:0;z-index:50;background:var(--card,#fff);border-bottom:1px solid var(--line,#e5e7eb);
  font:14px/1.2 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding-top:env(safe-area-inset-top)}
.sitenav .sn-bar{max-width:1100px;margin:0 auto;display:flex;align-items:center;gap:6px;padding-right:12px}
.sitenav .sn-in{flex:1 1 auto;min-width:0;display:flex;align-items:center;gap:4px;padding:6px 12px;
  overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}
.sitenav .sn-in::-webkit-scrollbar{display:none}
.sitenav a{color:var(--muted,#646b78);text-decoration:none;padding:8px 11px;border-radius:999px;white-space:nowrap;font-weight:550}
.sitenav a:hover{color:var(--text,#16181d);background:var(--bg,#f5f6f8)}
.sitenav a.on{color:#fff;background:var(--accent,#2f6fed)}
.sitenav a.sn-me{display:flex;align-items:center;gap:7px;padding:4px 10px 4px 4px;color:var(--text,#16181d)}
.sitenav a.sn-me.on{background:var(--bg,#f5f6f8);color:var(--text,#16181d)}
.sitenav .sn-av{width:30px;height:30px;border-radius:50%;object-fit:cover;display:inline-flex;align-items:center;justify-content:center;
  background:var(--accent,#2f6fed);color:#fff;font-size:12px;font-weight:700;flex:none}
@media (max-width:600px){.sitenav a.sn-me .sn-nm{display:none}.sitenav a.sn-me{padding:4px}}
.sitenav a.sn-me{position:relative}
.sitenav .sn-badge{position:absolute;left:28px;top:2px;width:11px;height:11px;border-radius:50%;border:2px solid var(--card,#fff)}
.sitenav .sn-badge.waiting{background:#e8a317}.sitenav .sn-badge.failed{background:#d64545}
.sitenav .sn-badge.updating{background:var(--accent,#2f6fed);animation:snpulse 1.2s ease-in-out infinite}
@keyframes snpulse{50%{opacity:.35}}
.sitenav .sn-sub{max-width:1100px;margin:0 auto;display:flex;gap:4px;padding:0 12px 6px;overflow-x:auto;scrollbar-width:none}
.sitenav .sn-sub::-webkit-scrollbar{display:none}
.sitenav .sn-sub a{font-size:13px;font-weight:500;padding:6px 10px}
.sitenav .sn-sub a.on{color:var(--accent,#2f6fed);background:transparent;box-shadow:inset 0 -2px 0 var(--accent,#2f6fed);border-radius:0}
html{-webkit-text-size-adjust:100%}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
canvas{max-width:100%}
@media (max-width:600px){
  .wrap{padding:12px !important}
  header{margin-bottom:10px !important}
  h1{font-size:18px !important}
  .cards{grid-template-columns:1fr 1fr !important;gap:8px !important}
  .grid2{grid-template-columns:1fr !important}
  .tiles{grid-template-columns:1fr 1fr !important}
  .card{padding:12px !important}
  .card .big{font-size:18px !important}
  th,td{padding:7px 5px !important;font-size:13px}
  .form{grid-template-columns:1fr 1fr !important}
  .btn{min-height:40px}
  .row .btn{flex:1 1 auto}
  .chartbox{height:200px !important}
}
@media (max-width:600px){.sitenav .sn-in{-webkit-mask-image:linear-gradient(to right,#000 88%,transparent);mask-image:linear-gradient(to right,#000 88%,transparent)}}
@media (max-width:400px){.form{grid-template-columns:1fr !important}.sitenav a{padding:8px 9px}}
/* accessibility on every page (UI review, 16 Oct): calmer motion when asked, readable text on blue in dark mode,
   bigger tap areas for folded sections */
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none !important;transition:none !important;scroll-behavior:auto !important}}
@media (prefers-color-scheme:dark){button.on,.sitenav .sn-in a.on,.btn:not(.ghost):not(.stop),.ibtn.pri,.ibtn.on,.ibtn.armed{color:#0f1115 !important}}
summary{min-height:32px}
:focus-visible{outline:2px solid var(--accent,#2f6fed);outline-offset:2px}
</style>"""


def _avatar(prof, size=30):
    name = (prof.get("name") or "").strip()
    initials = "".join(w[0] for w in name.split()[:2]).upper() or "Me"
    if prof.get("photo"):
        return f'<img class="sn-av" src="/profile/photo?v={_esc(str(prof.get("photo_v", "")))}" alt="" width="{size}" height="{size}">'
    return f'<span class="sn-av">{_esc(initials)}</span>'


def _nav_html(path, prof=None):
    prof = prof or {}
    us = us_installed()
    def shown(h):
        return path_allowed(h) and (us or h != "/us")
    def target(href):
        """The menu item's own page, or its first sub-page this login may open (None: hide the item)."""
        if path_allowed(href):
            return href
        return next((h for _, h in SUBNAV.get(href, []) if shown(h)), None)
    items = "".join(f'<a href="{target(href)}"{" class=on" if path in paths else ""}>{label}</a>'
                    for label, href, paths in NAV if target(href))
    first = _esc((prof.get("name") or "").split(" ")[0]) or "Profile"
    badge, tip = "", "Profile and theme"
    if path_allowed("/api/server-update"):        # the owner: a dot on the photo while an update waits, runs or failed
        u = _update_status()
        if u["state"] in ("waiting", "updating", "failed"):
            tip = u["label"] + (f": {u['behind']} change{'s' if u['behind'] != 1 else ''}" if u["state"] == "waiting" and u["behind"] else "")
            badge = f'<span class="sn-badge {u["state"]}" aria-label="{_esc(tip)}"></span>'
    me = (f'<a class="sn-me{" on" if path == "/profile" else ""}" href="/profile{"#srvCard" if badge else ""}" title="{_esc(tip)}">'
          f'{_avatar(prof)}{badge}<span class="sn-nm">{first}</span></a>')
    html = f'<nav class="sitenav"><div class="sn-bar"><div class="sn-in">{items}</div>{me}</div>'
    for _, href, paths in NAV:
        if href in SUBNAV and path in paths:
            html += '<div class="sn-sub">' + "".join(
                f'<a href="{h}"{" class=on" if h == path else ""}>{label}</a>' for label, h in SUBNAV[href]
                if shown(h)) + "</div>"
    return html + ("</nav><script>(function(){var a=document.querySelector('.sitenav .sn-in a.on');"
                   "if(a&&a.scrollIntoView)a.scrollIntoView({inline:'center',block:'nearest'});})();</script>")


_STATUS = re.compile(r'(<div class="status">)(.*?)(</div>)', re.S)
_LINK = re.compile(r'<a href="(/[^"]*)">[^<]*</a>')


def _clean_status(m):
    inner = _LINK.sub(lambda a: "" if a.group(1).split("?")[0] in _NAV_PATHS else a.group(0), m.group(2))
    inner = inner.replace('<span id="other"></span>', "")
    inner = re.sub(r"(\s*·\s*){2,}", " · ", inner)
    inner = re.sub(r"\s*·\s*$", "", inner)
    inner = re.sub(r"^\s*·\s*", "", inner)
    return m.group(1) + inner + m.group(3)


def add_menu(html, path, prof=None):
    """The page with the shared menu at the top and the old links removed (once only)."""
    if "sitenav-css" in html or "<body" not in html:
        return html
    html = _STATUS.sub(_clean_status, html, count=1)
    html = html.replace("</head>", NAV_CSS + "\n</head>", 1)
    html = html.replace('<div class="wrap">', '<div class="wrap" role="main">', 1)
    return re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _nav_html(path, prof), html, count=1)


_DARK_MQ = re.compile(r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)")


# Times: the engine and the dashboard write IST everywhere. With "This device's time" chosen in the profile, every time
# on every page is shown in the phone's or computer's own time zone instead. One script at the top of each page:
#  - makes `new Date("2026-10-16 11:20")` (no zone given) mean 11:20 IST (a bare 00:00 stays a calendar day, as the
#    calendars use it), and shows dates in IST, or in the device's zone when "This device's time" is chosen;
#  - gives TZ.hm(text) for places that cut the time out of a string, and TZ.label ("IST" or "") for labels.
TZ_JS = r"""<script id="tz-js">(function () {
  var LOCAL = __LOCAL__, IST = 330, D = Date;
  var MON = {jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, sept: 8, oct: 9, nov: 10, dec: 11};
  function utc(y, mo, d, h, mi, s) { return D.UTC(y, mo, d, h, mi, s || 0) - IST * 60000; }
  function istToday() { var n = new D(D.now() + IST * 60000); return [n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate()]; }
  // an IST text time -> milliseconds, or null: "2026-10-16 11:20[:05]", "16 Oct 2026, 11:20[:05]", "16 Oct, 11:20", "11:20"
  function parse(s) {
    s = String(s == null ? "" : s).trim(); var m;
    if ((m = /^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?$/.exec(s))) return utc(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
    if ((m = /^(\d{1,2}) ([A-Za-z]{3,4}) ?(\d{4})?,? (\d{1,2}):(\d\d)(?::(\d\d))?$/.exec(s)) && m[2].toLowerCase() in MON)
      return utc(m[3] ? +m[3] : istToday()[0], MON[m[2].toLowerCase()], +m[1], +m[4], +m[5], +(m[6] || 0));
    if ((m = /^(\d{1,2}):(\d\d)(?::(\d\d))?$/.exec(s))) { var t = istToday(); return utc(t[0], t[1], t[2], +m[1], +m[2], +(m[3] || 0)); }
    return null;
  }
  function hm(s) {
    var ms = parse(s); if (ms === null) { var m = /(\d{1,2}:\d\d)/.exec(String(s || "")); return m ? m[1] : String(s || ""); }
    var o = {hour: "2-digit", minute: "2-digit", hour12: false}; if (!LOCAL) o.timeZone = "Asia/Kolkata";
    return new D(ms).toLocaleTimeString("en-IN", o);
  }
  // times inside a sentence from the server ("Fri 16 Oct, 15:45 IST", "2026-10-15 15:45 recorded ..."), in the chosen zone
  function text(s) {
    s = String(s == null ? "" : s); if (!LOCAL) return s;
    return s.replace(/(\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?)/g, function (x) { return x.slice(0, 11) + hm(x); })
            .replace(/\b(\d{1,2}:\d\d) IST\b/g, function (x, h) { return hm(h); });
  }
  window.TZ = {local: LOCAL, label: LOCAL ? "" : "IST", parse: parse, hm: hm, text: text,
    zone: LOCAL ? (Intl.DateTimeFormat().resolvedOptions().timeZone || "local") : "Asia/Kolkata"};
  if (!LOCAL) {   // India time: format every date in IST, whatever the device's own zone
    ["toLocaleString", "toLocaleDateString", "toLocaleTimeString"].forEach(function (k) {
      var f = D.prototype[k];
      D.prototype[k] = function (loc, o) { o = Object.assign({}, o || {}); if (!o.timeZone) o.timeZone = "Asia/Kolkata"; return f.call(this, loc, o); };
    });
  }
  function TZDate() {
    if (!(this instanceof TZDate)) return D();
    if (arguments.length === 1 && typeof arguments[0] === "string") {
      var s = arguments[0].trim(), m = /^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d)(?:\.\d+)?)?$/.exec(s);
      if (m && !(m[4] === "00" && m[5] === "00" && !+(m[6] || 0))) return new D(utc(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)));
    }
    return new (Function.prototype.bind.apply(D, [null].concat([].slice.call(arguments))))();
  }
  TZDate.prototype = D.prototype; TZDate.now = D.now; TZDate.UTC = D.UTC;
  TZDate.parse = function (s) { var d = new TZDate(s); return d.getTime(); };
  window.Date = TZDate;
})();</script>"""


def apply_theme(html, prof):
    """Theme (system / light / dark) and accent colour from the profile, on any page."""
    if "theme-css" in html or "</head>" not in html:
        return html
    light, dark = ACCENTS.get(prof.get("accent"), ACCENTS["blue"])
    theme = prof.get("theme", "system")
    css = (f'<style id="theme-css">:root{{--accent:{light};color-scheme:{"light dark" if theme == "system" else theme}}}'
           f'@media (prefers-color-scheme:dark){{:root{{--accent:{dark}}}}}</style>')
    html = html.replace("</head>", css + "\n</head>", 1)
    tz = TZ_JS.replace("__LOCAL__", "true" if prof.get("time_zone") == "local" else "false")
    html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + tz, html, count=1)
    if theme == "dark":       # the page's own dark colours, always
        html = _DARK_MQ.sub("@media all", html)
    elif theme == "light":    # never the dark colours
        html = _DARK_MQ.sub("@media not all", html)
    if theme != "system":
        html = html.replace("matchMedia(\"(prefers-color-scheme: dark)\")", "matchMedia(\"(max-width:0px)\")")
    return html


@panel.before_app_request
def _old_blaze_page():
    """The Blaze Butterfly runs as an ordinary strategy now; its old page leads to Paper trading."""
    if request.path == "/algo":
        return redirect("/paper")
    return None


@panel.before_app_request
def _start_page():
    """Once per sign-in, opening the dashboard's home goes to the start page chosen in the profile."""
    try:
        if request.method == "GET" and request.path == "/" and session.get("user") and not session.get("landed"):
            session["landed"] = True
            target = load_profile().get("start_page", "/")
            if target != "/" and target in START_PAGES and path_allowed(target):
                return redirect(target)
    except Exception:  # noqa: BLE001
        pass
    return None


HTML_FILTERS = []   # more page additions (pwa_panel: app manifest, sign-in with Face ID)


@panel.after_app_request
def _menu_on_every_page(resp):
    try:
        if (request.method != "GET" or resp.status_code != 200 or resp.mimetype != "text/html"
                or resp.direct_passthrough or resp.is_streamed):
            return resp
        html, prof = resp.get_data(as_text=True), load_profile()
        if not request.path.startswith(_NO_NAV):
            html = add_menu(html, request.path, prof)
        for f in HTML_FILTERS:
            html = f(html, request.path)
        resp.set_data(apply_theme(html, prof))
    except Exception:  # noqa: BLE001 - the menu must never break a page
        pass
    return resp


# ---- live chart and index moves (Zerodha, through the dashboard's own Kite login) ---------------------------
# /chart: type a symbol (NSE/BSE stock, index, or an F&O contract) and see its live price and chart. Symbols on
# the Portfolio and Paper/Live pages open it. A strip with NIFTY, SENSEX and NIFTY BANK's day change is added at
# the top of the Portfolio page. Quotes and candles come from Zerodha (kite.quote / kite.historical_data).
from datetime import datetime as _cdt, timedelta as _ctd

INDICES = [("NIFTY", "NSE:NIFTY 50"), ("SENSEX", "BSE:SENSEX"), ("BANK NIFTY", "NSE:NIFTY BANK")]
ALIASES = {"NIFTY": "NSE:NIFTY 50", "NIFTY50": "NSE:NIFTY 50", "NIFTY 50": "NSE:NIFTY 50",
           "BANKNIFTY": "NSE:NIFTY BANK", "BANK NIFTY": "NSE:NIFTY BANK", "NIFTY BANK": "NSE:NIFTY BANK",
           "SENSEX": "BSE:SENSEX", "VIX": "NSE:INDIA VIX", "INDIA VIX": "NSE:INDIA VIX", "INDIAVIX": "NSE:INDIA VIX",
           "FINNIFTY": "NSE:NIFTY FIN SERVICE", "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "BANKEX": "BSE:BANKEX"}
RANGES = {   # range: (interval, calendar days back)
    "1d": ("minute", 7), "5d": ("5minute", 10), "1m": ("30minute", 35), "6m": ("day", 190),
    "1y": ("day", 370), "5y": ("day", 1830)}
_CACHE = {}


class ChartError(Exception):
    pass


def _kite():
    k = getattr(sys.modules.get("__main__"), "kite", None)
    if k is None or not getattr(k, "access_token", None):
        raise ChartError("Zerodha is not logged in today. Log in from the Portfolio page, then try again.")
    return k


def _remember(key, seconds, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    if len(_CACHE) > 300:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
            _CACHE.pop(k, None)
    return val


def resolve_symbol(text):
    """What the person typed -> Zerodha's EXCHANGE:SYMBOL. Plain names are NSE; F&O contracts NFO (BFO for
    SENSEX/BANKEX)."""
    s = re.sub(r"\s+", " ", str(text or "")).strip().upper()
    if not s:
        raise ChartError("Type a symbol, for example RELIANCE, NIFTY or NIFTY26OCT25000CE.")
    if s in ALIASES:
        return ALIASES[s]
    if ":" in s:
        ex, sym = s.split(":", 1)
        return f"{ex.strip()}:{ALIASES.get(sym.strip(), 'x:' + sym.strip()).split(':', 1)[1]}"
    if re.match(r"^[A-Z&-]+\d.*(CE|PE|FUT)$", s):
        return ("BFO:" if s.startswith(("SENSEX", "BANKEX")) else "NFO:") + s
    return "NSE:" + s


def _fo_instruments(exchange):
    """Index option contracts of one F&O exchange, loaded once a day: (name, expiry, strike, type) -> symbol."""
    def load():
        keep = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        out = {}
        for i in _kite().instruments(exchange):
            if i.get("name") in keep and i.get("instrument_type") in ("CE", "PE"):
                out[(i["name"], str(i["expiry"])[:10], float(i["strike"]), i["instrument_type"])] = i["tradingsymbol"]
        return out
    return _remember(("fo", exchange, _cdt.now().date().isoformat()), 86400, load)


def contract_symbol(underlying, expiry, strike, right):
    """An Algo Lab contract (NIFTY, 2026-10-20, 25000, CE) -> NFO:NIFTY2662025000CE (Zerodha's name)."""
    u = str(underlying).upper()
    ex = "BFO" if u in ("SENSEX", "BANKEX") else "NFO"
    sym = _fo_instruments(ex).get((u, str(expiry)[:10], float(strike), str(right).upper()))
    if not sym:
        raise ChartError(f"Zerodha has no {u} {expiry} {strike} {right} contract (expired contracts are not listed).")
    return f"{ex}:{sym}"


def _symbol_from_request():
    a = request.args
    if a.get("u") and a.get("e") and a.get("k") and a.get("r"):
        return contract_symbol(a["u"], a["e"], a["k"], a["r"])
    return resolve_symbol(a.get("s"))


def _quotes(symbols):
    def load():
        try:
            return _kite().quote(list(symbols))
        except ChartError:
            raise
        except Exception as e:  # noqa: BLE001 - Zerodha's own errors come back as text
            raise ChartError(f"Zerodha: {e}") from e
    return _remember(("q",) + tuple(symbols), 1.0, load)


def _quote_view(sym, q):
    o = q.get("ohlc") or {}
    last, prev = q.get("last_price"), o.get("close")
    chg = (last - prev) if (last is not None and prev) else None
    t = q.get("last_trade_time") or q.get("timestamp")
    return {"symbol": sym, "token": q.get("instrument_token"), "last": last, "prev_close": prev,
            "change": None if chg is None else round(chg, 2), "pct": None if chg is None else round(chg / prev * 100, 2),
            "open": o.get("open"), "high": o.get("high"), "low": o.get("low"), "volume": q.get("volume"),
            "oi": q.get("oi"), "time": t.isoformat() if hasattr(t, "isoformat") else (str(t) if t else None)}


def _err(e, code=400):
    return jsonify(error=str(e)), code


# ---- backtests run on the server (Paper › Run a backtest) ----------------------------------
# The page writes a request into the engine's runtime/backtest-queue/; the engine's algolab-backtest path unit runs it
# at low priority (algolab/backtest_queue.py) and puts the result on the Backtests page.
BT_QUEUE, BT_JOBS = RT / "backtest-queue", RT / "backtest-jobs"
BT_MAX_QUEUED = 3
BT_SWEEP_MAX = 60          # combinations on the server
BT_SWEEP_MAX_MAC = 400     # and on the Mac (faster, several at once)
_BT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bt_datasets():
    """The price sets a backtest can be asked to use: one 1-minute set per index (algolab/datasets.py)."""
    out = []
    for f in sorted((ENGINE / "data" / "spot").glob("*.csv")):
        if not _SET_RE.fullmatch(f.stem):
            continue
        first = last = None
        try:
            with f.open("rb") as fh:
                fh.readline()
                first = fh.readline().decode(errors="replace").split(",")[0][:10]
                fh.seek(max(0, f.stat().st_size - 4096))
                tail = fh.read().decode(errors="replace").strip().splitlines()
                last = tail[-1].split(",")[0][:10] if tail else None
        except OSError:
            pass
        out.append({"name": f.stem, "from": first if first and _BT_DATE.match(first) else None,
                    "to": last if last and _BT_DATE.match(last) else None})
    return out


def _bt_jobs(n=15, light=True):
    """Newest jobs first; `light` keeps only a sweep's best 10 rows (the full set: /api/backtest-runner/job/<id>)."""
    jobs = []
    for f in sorted(BT_JOBS.glob("*.json"), reverse=True)[:n]:
        j = _json(f)
        if j:
            if light and isinstance(j.get("rows"), list) and len(j["rows"]) > 10:
                j = {**j, "rows": j["rows"][:10], "rows_total": len(j["rows"])}
            jobs.append(j)
    return jobs


@panel.route("/api/td-live-test", methods=["GET", "POST"])
def api_td_live_test():
    """Algo Lab › Run › TrueData real-time test. POST: ask the Mac helper to count TrueData's ticks per second for two
    minutes (the TrueData login is on the Mac). GET: the latest test and its result."""
    tests = [j for j in (_json(f) for f in sorted(BT_JOBS.glob("*.json"), reverse=True)[:200]) if j and j.get("kind") == "td_live_test"]
    if request.method == "POST":
        if not can("backtests", "use"):
            return jsonify(error="This login may not run tests."), 403
        if any(j.get("status") in ("queued", "running") for j in tests):
            return jsonify(error="A test is already waiting or running."), 409
        mcx = (request.get_json(silent=True) or {}).get("set") == "mcx"
        now = _ist_now()
        if mcx and (now.weekday() >= 5 or not ((9, 0) <= (now.hour, now.minute) < (23, 20))):
            return jsonify(error="MCX is closed: try between 9:00 and 23:20 IST on a trading day."), 400
        if not mcx and (now.weekday() >= 5 or not ((9, 15) <= (now.hour, now.minute) < (15, 25))):
            return jsonify(error="The market is closed: try between 9:15 and 15:25 IST on a trading day."), 400
        new_id = lambda: _dt.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]", "a", base64.b32encode(os.urandom(5)).decode().lower()[:4])  # noqa: E731
        jid = new_id()
        job = {"id": jid, "kind": "td_live_test", "target": "mac", "title": "TrueData real-time test", "strategy": "",
               "seconds": 120, "user": current_user() or "", "queued": _dt.now().isoformat(timespec="seconds"), "status": "queued"}
        if mcx:     # the Mac counts TrueData, the server counts Zerodha, over the same two minutes
            names = ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]
            kid = new_id()
            while kid == jid:
                kid = new_id()
            start = time.time() + 45
            job.update(set="mcx", symbols=[n + "-I" for n in names], start_at=start, pair=kid, title="TrueData vs Zerodha: MCX")
            kjob = {"id": kid, "kind": "kite_tick_test", "target": "server", "title": "Zerodha ticks: MCX", "strategy": "", "set": "mcx",
                    "names": names, "seconds": 120, "start_at": start, "user": job["user"], "queued": job["queued"], "status": "queued"}
            tq = RT / "tick-test-queue"           # its own runner on the server (algolab-ticktest.path): never waits for backtests
            BT_JOBS.mkdir(parents=True, exist_ok=True)
            tq.mkdir(parents=True, exist_ok=True)
            (BT_JOBS / f"{kid}.json").write_text(json.dumps(kjob, indent=1))
            tmp = BT_JOBS / f".{kid}.request"
            tmp.write_text(json.dumps(kjob))
            tmp.rename(tq / f"{kid}.json")
        BT_JOBS.mkdir(parents=True, exist_ok=True)
        _job_write(job)
        tests.insert(0, job)
    t = tests[0] if tests else None
    pair = _json(BT_JOBS / f"{t['pair']}.json") if t and t.get("pair") and re.fullmatch(r"[0-9a-z-]{10,40}", t["pair"]) else None
    return jsonify(test=t, pair=pair, mac=_mac_status())


@panel.route("/api/backtest-runner/finished")
def api_backtest_finished():
    """Finished sweeps (and backtests that failed) for the Results page, newest first, without their result tables."""
    out = []
    for f in sorted(BT_JOBS.glob("*.json"), reverse=True)[:300]:
        j = _json(f)
        if not j or j.get("kind") in ("td_live_test", "kite_tick_test") or j.get("status") not in ("done", "failed") or (j.get("kind") != "sweep" and j.get("status") == "done"):
            continue
        rows = j.get("rows") or []
        per = (j.get("periods") or ["all"])[0]
        opt = j.get("optimise") if isinstance(j.get("optimise"), dict) else None
        best = (opt["picks"][0].get("total_net") if opt and opt.get("picks") else
                rows[0].get(f"{per}_net") if rows and not opt else None)
        out.append({k: j.get(k) for k in ("id", "kind", "status", "strategy", "title", "dataset", "from", "to", "target",
                                          "message", "total", "folds", "finished")}
                   | {"optimise": bool(j.get("optimise")), "best": best, "picks": len(opt["picks"]) if opt else None})
        if len(out) >= 100:
            break
    return jsonify(jobs=out)


@panel.route("/api/backtest-runner/job/<jid>/delete", methods=["POST"])
def api_backtest_job_delete(jid):
    """Remove a finished sweep or a failed backtest from the list (a saved run is deleted on the Results page)."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", jid or ""):
        return jsonify(error="No such job."), 404
    f = BT_JOBS / f"{jid}.json"
    j = _json(f)
    if not j:
        return jsonify(error="No such job."), 404
    if j.get("status") in ("queued", "running"):
        return jsonify(error="It is still running."), 409
    f.unlink(missing_ok=True)
    return jsonify(ok=True)


@panel.route("/api/backtest-runner/job/<jid>")
def api_backtest_job(jid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", jid or ""):
        return jsonify(error="No such job."), 404
    j = _json(BT_JOBS / f"{jid}.json")
    return jsonify(job=j) if j else (jsonify(error="No such job."), 404)


@panel.route("/backtests/run")
def backtest_run_page():
    return Response(RUN_PAGE, mimetype="text/html")


@panel.route("/backtests/sweep")
def backtest_sweep_page():
    return Response(SWEEP_PAGE, mimetype="text/html")


@panel.route("/api/backtest-runner", methods=["GET", "POST"])
def api_backtest_runner():
    catalog = _catalog()
    if request.method == "POST":
        if not can("backtests", "use"):
            return jsonify(error="This login may not run backtests."), 403
        b = request.get_json(silent=True) or {}
        strat = next((c for c in catalog if c.get("name") == b.get("strategy")), None)
        if strat is None:
            return jsonify(error="Pick a strategy."), 400
        names = {d["name"] for d in _bt_datasets()}
        if b.get("dataset") not in names:
            return jsonify(error="Pick a data set."), 400
        start, end = (b.get("from") or "").strip(), (b.get("to") or "").strip()
        if (start and not _BT_DATE.match(start)) or (end and not _BT_DATE.match(end)) or (start and end and end < start):
            return jsonify(error="Dates must be YYYY-MM-DD, the end after the start."), 400
        keys = {pp["key"]: pp for pp in strat.get("params", [])}
        params = {}
        for k, v in (b.get("params") or {}).items():
            if k not in keys:
                return jsonify(error=f"Unknown setting '{k}'."), 400
            if keys[k].get("choices") and v not in keys[k]["choices"]:
                return jsonify(error=f"'{keys[k]['label']}' must be one of {', '.join(map(str, keys[k]['choices']))}."), 400
            if v != keys[k].get("default") or k.startswith("risk."):    # Orders & stops: always sent (saved defaults)
                params[k] = v
        grid = {}
        split, n_folds, opt = None, 0, {}
        if b.get("kind") == "sweep":
            for k, given in (b.get("grid") or {}).items():
                if k not in keys:
                    return jsonify(error=f"Unknown setting '{k}'."), 400
                vals = [str(v).strip() for v in (given if isinstance(given, list) else str(given).split(",")) if str(v).strip()]
                if keys[k].get("choices") and any(v not in map(str, keys[k]["choices"]) for v in vals):
                    return jsonify(error=f"'{keys[k]['label']}' must be one of {', '.join(map(str, keys[k]['choices']))}."), 400
                if len(vals) > 1:
                    grid[k] = list(dict.fromkeys(vals))
            if not grid:
                return jsonify(error="Vary at least one setting (two or more values)."), 400
            total = 1
            for v in grid.values():
                total *= len(v)
            limit = BT_SWEEP_MAX_MAC if b.get("target") == "mac" else BT_SWEEP_MAX
            optimise = bool(b.get("optimise"))
            if optimise:
                if total > 200_000:
                    return jsonify(error=f"{total:,} combinations to choose from; at most 200,000. Use bigger steps."), 400
                try:
                    opt = {"optimise": True, "folds": int(b.get("folds") or 4), "lock_days": int(b.get("lock_days") or 75),
                           "min_cycles": int(b.get("min_cycles") or 30), "max_dd": float(b.get("max_dd") or 0)}
                except (TypeError, ValueError):
                    return jsonify(error="Periods, locked-away days, cycles and drawdown must be numbers."), 400
                if not (3 <= opt["folds"] <= 8 and 20 <= opt["lock_days"] <= 200 and 1 <= opt["min_cycles"] <= 1000
                        and opt["max_dd"] >= 0):
                    return jsonify(error="Use 3–8 periods, 20–200 locked-away days, at least 1 cycle, a drawdown of 0 or more."), 400
                total = 1                                     # tests at most the sweep limit, sampled from the grid
            if total > limit:
                return jsonify(error=f"That is {total} combinations; at most {limit} on the {'Mac' if limit == BT_SWEEP_MAX_MAC else 'server'}. "
                                     "Narrow the ranges or use bigger steps."), 400
            split = (b.get("split") or "").strip() or None
            if split and (not _BT_DATE.match(split) or (start and split <= start) or (end and split > end)):
                return jsonify(error="The out-of-sample date must fall between From and To."), 400
            try:
                n_folds = int(b.get("folds") or 0)
            except (TypeError, ValueError):
                n_folds = 0
            if n_folds and not 3 <= n_folds <= 8:
                return jsonify(error="Walk-forward takes 3 to 8 periods."), 400
            if n_folds or opt:
                split = None
            if opt:
                n_folds = 0
            params = {k: v for k, v in params.items() if k not in grid}
        queued = [j for j in _bt_jobs(50) if j.get("status") in ("queued", "running")]
        if len(queued) >= BT_MAX_QUEUED:
            return jsonify(error=f"{len(queued)} backtests are already waiting; try again when one finishes."), 429
        jid = _dt.now().strftime("%Y%m%d-%H%M%S") + "-" + base64.b32encode(os.urandom(5)).decode().lower()[:4]
        jid = re.sub(r"[^a-z0-9-]", "a", jid)
        target = "mac" if b.get("target") == "mac" else "server"
        fills = "conservative" if b.get("fills") == "conservative" else "normal"
        try:
            min_volume = max(0, min(10_000_000, int(float(b.get("min_volume") or 0))))
        except (TypeError, ValueError):
            min_volume = 0
        job = {"id": jid, "strategy": strat["name"], "title": strat.get("title"), "params": params, "target": target, "fills": fills,
               **({"min_volume": min_volume} if min_volume else {}),
               **({"preset": _preset_ref(strat["name"], b.get("preset"))} if _preset_ref(strat["name"], b.get("preset")) else {}),
               **({"kind": "sweep", "grid": grid, "split": split} if grid else {}),
               **({"folds": n_folds} if grid and n_folds else {}),
               **(opt if grid else {}),
               "dataset": b["dataset"], "from": start or None, "to": end or None,
               "note": str(b.get("note") or "")[:200], "user": current_user() or "",
               "queued": _dt.now().isoformat(timespec="seconds"), "status": "queued"}
        try:
            BT_JOBS.mkdir(parents=True, exist_ok=True)
            BT_QUEUE.mkdir(parents=True, exist_ok=True)
            (BT_JOBS / f"{jid}.json").write_text(json.dumps(job, indent=1))
            if target == "mac":                                # the Mac helper collects it (/api/lab-agent/claim)
                return jsonify(ok=True, job=job)
            tmp = BT_JOBS / f".{jid}.request"                 # written beside, moved in whole: the watcher

            tmp.write_text(json.dumps(job))
            tmp.rename(BT_QUEUE / f"{jid}.json")              # never sees half a file
        except OSError as e:
            return jsonify(error=f"Could not ask the server: {e}"), 500
        return jsonify(ok=True, job=job)
    return jsonify(strategies=catalog, datasets=_bt_datasets(), sec1={"days": [d["day"] for d in _sec1_days()], "keep": _sec1_keep()}, jobs=_bt_jobs(),
                   may_run=can("backtests", "use"), max_queued=BT_MAX_QUEUED, sweep_max=BT_SWEEP_MAX, sweep_max_mac=BT_SWEEP_MAX_MAC, mac=_mac_status())


# ---- Backtests › Compare with paper: sessions against 1-minute and 1-second backtests, day by day ------------------
CMP_DIR = ENGINE / "runtime" / "compare"
SEC1_KEEP_MAX = 30


def _market_hours_now():
    """True on a weekday between 09:15 and 15:30 IST: comparisons wait for the close (they would slow the engine)."""
    now = _ist_now()
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) < (15, 30)


def _cmp_sessions():
    out = []
    root = ENGINE / "runtime" / "sessions"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            st = _json(d / "state.json")
            if not st or st.get("feed") != "kite":
                continue
            out.append({"id": d.name, "label": st.get("label") or "", "strategy": st.get("strategy", ""),
                        "mode": st.get("mode", "paper"), "status": st.get("status", "")})
    return out


@panel.route("/backtests/compare")
def backtest_compare_page():
    return Response(COMPARE_PAGE, mimetype="text/html")


@panel.route("/api/compare-period", methods=["GET", "POST"])
def api_compare_period():
    if request.method == "POST":
        if not can("backtests", "use"):
            return jsonify(error="This login may not run backtests."), 403
        if _market_hours_now():
            return jsonify(error="Comparisons run after the close (15:30 IST), so the paper and live engine is not slowed."), 409
        b = request.get_json(silent=True) or {}
        start, end = (b.get("from") or "").strip(), (b.get("to") or "").strip()
        if not (_BT_DATE.match(start) and _BT_DATE.match(end)) or end < start:
            return jsonify(error="Pick the first and last day (the last on or after the first)."), 400
        bars = [x for x in ("1m", "1s") if x in (b.get("bars") or [])]
        if not bars:
            return jsonify(error="Tick 1-minute, 1-second or both."), 400
        known = {x["id"] for x in _cmp_sessions()}
        picked = [str(x) for x in (b.get("sessions") or []) if str(x) in known]
        if b.get("sessions") and not picked:
            return jsonify(error="None of those sessions exists."), 400
        queued = [j for j in _bt_jobs(50) if j.get("status") in ("queued", "running")]
        if len(queued) >= BT_MAX_QUEUED:
            return jsonify(error=f"{len(queued)} jobs are already waiting; try again when one finishes."), 429
        jid = re.sub(r"[^a-z0-9-]", "a", _dt.now().strftime("%Y%m%d-%H%M%S") + "-" + base64.b32encode(os.urandom(5)).decode().lower()[:4])
        job = {"id": jid, "kind": "compare_period", "title": "Compare with paper", "from": start, "to": end, "bars": bars,
               "sessions": picked, "target": "server", "user": current_user() or "",
               "queued": _dt.now().isoformat(timespec="seconds"), "status": "queued"}
        try:
            BT_JOBS.mkdir(parents=True, exist_ok=True)
            BT_QUEUE.mkdir(parents=True, exist_ok=True)
            (BT_JOBS / f"{jid}.json").write_text(json.dumps(job, indent=1))
            tmp = BT_JOBS / f".{jid}.request"
            tmp.write_text(json.dumps(job))
            tmp.rename(BT_QUEUE / f"{jid}.json")
        except OSError as e:
            return jsonify(error=f"Could not ask the server: {e}"), 500
        return jsonify(ok=True, job=job)
    jobs = [j for j in _bt_jobs(50) if j.get("kind") == "compare_period"][:15]
    return jsonify(sessions=_cmp_sessions(), sec1={"days": [d["day"] for d in _sec1_days()], "keep": _sec1_keep()},
                   jobs=jobs, market_hours=_market_hours_now(), may_run=can("backtests", "use"),
                   is_owner=(current_user() or owner_name()) == owner_name())


@panel.route("/api/compare-period/report/<jid>")
def api_compare_report(jid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", jid or ""):
        return jsonify(error="No such report."), 404
    path = CMP_DIR / f"{jid}.json"
    rep = _json(path)
    if not rep:
        return jsonify(error="That report is not ready (or was not kept)."), 404
    if request.args.get("csv"):
        lines = ["session,strategy,mode,day,actual,bt_1m,gap_1m,bt_1s,gap_1s,flag,notes"]
        for r in rep.get("sessions", []):
            for d in r.get("days", []):
                cell = lambda k: "" if d.get(k) is None else f"{d[k]:.0f}"  # noqa: E731
                lines.append(",".join([r["session"], r.get("strategy", ""), r.get("mode", ""), d["day"], cell("actual"),
                                       cell("bt_1m"), cell("gap_1m"), cell("bt_1s"), cell("gap_1s"),
                                       "yes" if d.get("flag") else "", "; ".join(d.get("notes", []))]))
        return Response("\n".join(lines) + "\n", mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=compare-{rep.get('from')}-{rep.get('to')}.csv"})
    return jsonify(rep)


@panel.route("/api/compare-period/<jid>/delete", methods=["POST"])
def api_compare_delete(jid):
    """Delete one comparison: its report and its job record. Not while it is queued or running."""
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", jid or ""):
        return jsonify(error="No such comparison."), 404
    if not can("backtests", "use"):
        return jsonify(error="This login may not delete comparisons."), 403
    job = _json(BT_JOBS / f"{jid}.json")
    if not job or job.get("kind") != "compare_period":
        return jsonify(error="No such comparison."), 404
    if job.get("status") in ("queued", "running"):
        return jsonify(error="That comparison is still running. Delete it once it has finished."), 409
    removed = []
    for path in (CMP_DIR / f"{jid}.json", BT_JOBS / f"{jid}.json"):
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
        except OSError as e:
            return jsonify(error=f"Could not delete {path.name}: {e}"), 500
    return jsonify(ok=True, removed=removed)


@panel.route("/api/compare-period/keep", methods=["POST"])
def api_compare_keep():
    """Owner: how many trading days of 1-second bars the server keeps (runtime/sec1_settings.json; the 15:45 job prunes)."""
    b = request.get_json(silent=True) or {}
    try:
        n = int(b.get("keep_trading_days"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a whole number of trading days."), 400
    if not 1 <= n <= SEC1_KEEP_MAX:
        return jsonify(error=f"Keep between 1 and {SEC1_KEEP_MAX} trading days."), 400
    days = _sec1_days()
    per_day = (sum(d["mb"] for d in days) / len(days)) if days else 0
    try:
        free_mb = shutil.disk_usage(ENGINE).free / 1e6
    except OSError:
        free_mb = None
    if free_mb is not None and per_day and per_day * max(0, n - len(days)) > free_mb * 0.8:
        return jsonify(error=f"{n} days would need about {per_day * n:,.0f} MB; the server has {free_mb:,.0f} MB free."), 400
    path = ENGINE / "runtime" / "sec1_settings.json"
    try:
        cur = _json(path) or {}
        cur["keep_trading_days"] = n
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur))
        tmp.replace(path)
    except OSError as e:
        return jsonify(error=f"Could not save: {e}"), 500
    return jsonify(ok=True, keep=n, mb_per_day=round(per_day, 1))


# ---- the Mac helper (algolab/lab_agent.py) collects jobs meant for the Mac and reports back ----------------
MAC_BEAT = RT / "mac-agent.json"
MAC_ONLINE_S = 60
_BT_FIELDS = {"status", "message", "progress", "run", "metrics", "sweep", "columns", "rows", "total",
              "periods", "luck", "walk", "fold_dates", "optimise", "td"}          # sweep extras: walk-forward, Find best settings


def _mac_status():
    beat = _json(MAC_BEAT, {}) or {}
    try:
        age = time.time() - MAC_BEAT.stat().st_mtime         # the file's own time: written on every call
    except OSError:
        age = None
    return {"online": age is not None and age < MAC_ONLINE_S, "name": beat.get("name"), "seen": beat.get("time"),
            "can": beat.get("can") or []}


def _job_write(job):
    f = BT_JOBS / f"{job['id']}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1))
    os.replace(tmp, f)


@panel.route("/api/lab-agent/claim", methods=["POST"])
def api_lab_agent_claim():
    if not can("backtests", "use"):
        return jsonify(error="This login may not run backtests."), 403
    b = request.get_json(silent=True) or {}
    can_do = [str(k) for k in (b.get("can") or [])][:20]           # job kinds this helper runs (older helpers: none sent)
    try:
        RT.mkdir(parents=True, exist_ok=True)
        MAC_BEAT.write_text(json.dumps({"name": str(b.get("name") or "Mac")[:40], "time": _dt.now().isoformat(timespec="seconds"),
                                        "can": can_do}))
    except OSError:
        pass
    if b.get("beat_only"):                                  # "still here, busy with a job"
        return jsonify(job=None)
    # The helper asks for work only when it is idle: a job it still has "running" was lost (the helper restarted,
    # crashed or ran out of memory). Say so on the page instead of leaving it running for ever.
    runner = str(b.get("name") or "Mac")[:40]
    for f in BT_JOBS.glob("*.json"):
        j = _json(f)
        if j and j.get("target") == "mac" and j.get("status") == "running" and j.get("runner", runner) == runner:
            j.update(status="failed", finished=_dt.now().isoformat(timespec="seconds"),
                     message="The Mac stopped working on it (the helper restarted, or the Mac ran short of memory). "
                             "Send it again; keep the Mac awake and on power.")
            _job_write(j)
    waiting = sorted((j for j in (_json(f) for f in BT_JOBS.glob("*.json")) if j and j.get("target") == "mac"
                      and j.get("status") == "queued" and (j.get("kind") != "td_live_test" or "td_live_test" in can_do)),
                     key=lambda j: j["id"])
    if not waiting:
        return jsonify(job=None)
    job = waiting[0]
    job.update(status="running", started=_dt.now().isoformat(timespec="seconds"), runner=str(b.get("name") or "Mac")[:40])
    _job_write(job)
    return jsonify(job=job)


@panel.route("/api/lab-agent/status", methods=["POST"])
def api_lab_agent_status():
    if not can("backtests", "use"):
        return jsonify(error="This login may not run backtests."), 403
    b = request.get_json(silent=True) or {}
    jid = str(b.get("id") or "")
    job = _json(BT_JOBS / f"{jid}.json") if re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", jid) else None
    if not job or job.get("target") != "mac":
        return jsonify(error="No such Mac job."), 404
    job.update({k: v for k, v in b.items() if k in _BT_FIELDS})
    if job.get("status") in ("done", "failed"):
        job["finished"] = _dt.now().isoformat(timespec="seconds")
    _job_write(job)
    try:
        MAC_BEAT.touch()
    except OSError:
        pass
    return jsonify(ok=True)


@panel.route("/chart")
def chart_page():
    return Response(CHART_PAGE, mimetype="text/html")


@panel.route("/api/chart/quote")
def chart_quote():
    try:
        sym = _symbol_from_request()
        q = _quotes((sym,)).get(sym)
        if not q:
            raise ChartError(f"Zerodha does not know {sym}. Check the spelling, or add the exchange (BSE:..., NFO:...).")
        return jsonify(_quote_view(sym, q))
    except ChartError as e:
        return _err(e)


@panel.route("/api/chart/candles")
def chart_candles():
    rng = request.args.get("range", "1d")
    if rng not in RANGES:
        return _err("Unknown range")
    try:
        sym = _symbol_from_request()
        q = _quotes((sym,)).get(sym)
        if not q:
            raise ChartError(f"Zerodha does not know {sym}.")
        interval, back = RANGES[rng]
        token, now = q["instrument_token"], _cdt.now()

        def load():
            try:
                rows = _kite().historical_data(token, now - _ctd(days=back), now, interval)
            except Exception as e:  # noqa: BLE001
                raise ChartError(f"Zerodha: {e}") from e
            return [[str(r["date"])[:16].replace(" ", "T"), r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0]
                    for r in rows]
        rows = _remember(("c", token, rng), 20 if interval != "day" else 300, load)
        if rng == "1d" and rows:   # the latest trading day only
            day = rows[-1][0][:10]
            rows = [r for r in rows if r[0][:10] == day]
        elif rng == "5d" and rows:
            days = sorted({r[0][:10] for r in rows})[-5:]
            rows = [r for r in rows if r[0][:10] >= days[0]]
        return jsonify(symbol=sym, range=rng, interval=interval, candles=rows)
    except ChartError as e:
        return _err(e)


@panel.route("/api/chart/search")
def chart_search():
    """Names that start with (or contain) what was typed: NSE stocks and NSE/BSE indices, loaded once a day."""
    term = re.sub(r"\s+", " ", request.args.get("q", "")).strip().upper()
    if len(term) < 2:
        return jsonify(results=[])
    try:
        def load():
            k = _kite()
            out = [(f"NSE:{i['tradingsymbol']}", i.get("name") or "", i.get("segment"))
                   for i in k.instruments("NSE") if i.get("segment") in ("NSE", "INDICES") and i.get("instrument_type") == "EQ"
                   or i.get("segment") == "INDICES"]
            out += [(f"BSE:{i['tradingsymbol']}", i.get("name") or "", "INDICES")
                    for i in k.instruments("BSE") if i.get("segment") == "INDICES"]
            return out
        allsyms = _remember(("search", _cdt.now().date().isoformat()), 86400, load)
    except ChartError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001
        return _err(f"Zerodha: {e}")
    starts = [r for r in allsyms if r[0].split(":", 1)[1].startswith(term)]
    within = [r for r in allsyms if r not in starts and (term in r[0] or term in r[1].upper())]
    pick = sorted(starts, key=lambda r: (r[2] != "INDICES", len(r[0])))[:12] + within[:max(0, 12 - len(starts))]
    return jsonify(results=[{"symbol": s, "name": n, "index": g == "INDICES"} for s, n, g in pick[:12]])


@panel.route("/api/chart/indices")
def chart_indices():
    """NIFTY, SENSEX and NIFTY BANK: last price and change from the previous close."""
    try:
        qs = _quotes(tuple(s for _, s in INDICES))
    except ChartError as e:
        return _err(e, 503)
    return jsonify(indices=[dict(_quote_view(s, qs[s]), name=n) for n, s in INDICES if s in qs])


INDEX_STRIP = """<div id="idxStrip" style="display:none"></div>
<style>
#idxStrip{display:flex;gap:8px;flex-wrap:wrap;max-width:1100px;margin:10px auto 0;padding:0 16px;
  font:13px/1.3 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-variant-numeric:tabular-nums}
#idxStrip a{flex:1 1 0;min-width:0;display:block;text-decoration:none;color:var(--text,#16181d);background:var(--card,#fff);
  border:1px solid var(--line,#e5e7eb);border-radius:10px;padding:8px 10px}
#idxStrip .n{font-size:12px;color:var(--muted,#646b78);text-transform:uppercase;letter-spacing:.04em}
#idxStrip .v{font-size:15px;font-weight:650}
#idxStrip .c{font-size:12px}
#idxStrip .up{color:var(--up,#087a4c)}#idxStrip .down{color:var(--down,#c8323a)}
</style>
<script>
(function () {
  var box = document.getElementById("idxStrip");
  function f(v, d) { return Number(v).toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d}); }
  function draw(list) {
    box.innerHTML = list.map(function (i) {
      var c = i.change == null ? "" : i.change >= 0 ? "up" : "down", s = i.change == null ? "" : (i.change >= 0 ? "&#9650; " : "&#9660; ");
      return '<a href="/chart?s=' + encodeURIComponent(i.symbol) + '"><div class="n">' + i.name + '</div><div class="v">' + f(i.last, 2) +
        '</div><div class="c ' + c + '"><b>' + (i.change == null ? "–" : s + f(Math.abs(i.change), 2)) + '</b>' +
        (i.pct == null ? "" : " (" + Math.abs(i.pct).toFixed(2) + "%)") + '</div></a>';
    }).join("");
    box.style.display = list.length ? "flex" : "none";
  }
  function open() { var t = new Date(Date.now() + 330 * 60000), m = t.getUTCHours() * 60 + t.getUTCMinutes(), d = t.getUTCDay();
    return d > 0 && d < 6 && m >= 555 && m <= 940; }
  function load() {
    if (document.hidden) return;
    fetch("/api/chart/indices", {cache: "no-store"}).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j && j.indices) draw(j.indices); }).catch(function () {});
  }
  load();
  setInterval(function () { if (open()) load(); }, 5000);
  setInterval(function () { if (!open()) load(); }, 300000);
  // holdings: a tap on a stock's name opens its live chart (the chart page links back to its history)
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a.sym");
    if (!a || e.metaKey || e.ctrlKey) return;
    var m = /[?&]symbol=([^&]+)/.exec(a.getAttribute("href") || "");
    if (!m) return;
    e.preventDefault();
    location.href = "/chart?h=1&s=" + encodeURIComponent("NSE:" + decodeURIComponent(m[1]));
  });
})();
</script>"""


def _portfolio_additions(html, path):
    """Portfolio (India) page: the index strip under the menu; stock names open the live chart."""
    if path != "/" or "idxStrip" in html:
        return html
    if "</nav>" in html:
        return html.replace("</nav>", "</nav>" + INDEX_STRIP, 1)
    return re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + INDEX_STRIP, html, count=1)


HTML_FILTERS.append(_portfolio_additions)


CHART_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chart · Portfolio</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed;--warn:#a06a1a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff;--warn:#e0b34a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type=text]{flex:1;min-width:0;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
.btn{font:inherit;font-size:14px;padding:8px 12px;border-radius:10px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;text-decoration:none}
.btn.ghost{background:none;color:var(--accent)}
.chip{font:inherit;font-size:13px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:none;color:var(--text);cursor:pointer}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.sugg{position:relative}
.sugg .list{position:absolute;left:0;right:0;top:100%;z-index:20;background:var(--card);border:1px solid var(--line);border-radius:10px;margin-top:4px;max-height:320px;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.12)}
.sugg .list div{padding:8px 12px;cursor:pointer;display:flex;justify-content:space-between;gap:10px}
.sugg .list div:hover,.sugg .list div.on{background:var(--bg)}
.sugg .list span{color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
h1{font-size:20px;margin:0}
.price{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
.chg{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.down{color:var(--down)}
.muted{color:var(--muted);font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-top:12px}
.tile .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.tile .v{font-weight:600;font-variant-numeric:tabular-nums}
.chartbox{height:380px;position:relative;margin-top:10px}
.err{color:var(--down);margin-top:10px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}
.dot.live{background:var(--up)}
@media (max-width:760px){.tiles{grid-template-columns:repeat(3,1fr)}.chartbox{height:300px}.price{font-size:26px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="row sugg">
      <input type="text" id="sym" aria-label="Symbol" placeholder="Symbol: RELIANCE, NIFTY, SENSEX, NIFTY26OCT25000CE…" autocomplete="off" autocapitalize="characters" spellcheck="false">
      <button class="btn" id="go" style="flex:none">Show</button>
      <div class="list" id="list" hidden></div>
    </div>
    <div class="row" style="margin-top:10px" id="quick"></div>
  </div>

  <div class="card" id="main" hidden>
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div><h1 id="title"></h1><div class="muted" id="sub"></div></div>
      <div style="text-align:right"><div class="price" id="last">–</div><div class="chg" id="chg"></div>
        <div class="muted"><span class="dot" id="dot"></span> <span id="when"></span></div></div>
    </div>
    <div class="tiles" id="tiles"></div>
    <div class="row" style="margin-top:14px;justify-content:space-between">
      <div class="row" id="ranges"></div>
      <div class="row"><button class="chip on" data-style="line">Line</button><button class="chip" data-style="candles">Candles</button></div>
    </div>
    <div class="chartbox"><canvas id="cv"></canvas></div>
    <div class="muted" id="note" style="margin-top:6px"></div>
    <div id="links" style="margin-top:8px"></div>
  </div>
  <div class="err" id="err"></div>

  <div class="card" id="stored">
    <div class="row" style="justify-content:space-between">
      <h1 id="stTitle" style="font-size:17px">Index and India VIX</h1>
      <div class="row"><select id="stSet" aria-label="Data set" style="font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text)"></select></div>
    </div>
    <div class="row" style="margin-top:8px;justify-content:space-between"><div class="row" id="stUnd"></div><div class="row" id="stRange"></div></div>
    <div class="chartbox" style="height:300px"><canvas id="stCv"></canvas></div>
    <div class="muted" id="stNote" style="margin-top:6px"></div>
  </div>
</div>
<script>
// ---- the index and India VIX from the stored data (moved here from Data, 16 Oct) ----
(function () {
  const $ = id => document.getElementById(id), css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const n2 = x => x == null ? "–" : Number(x).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const fmtDay = s => { const d = new Date(String(s).slice(0, 10) + "T00:00"); return isNaN(d) ? s : d.toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "2-digit"}); };
  const fmtWhen = s => { const d = new Date(String(s).replace(" ", "T")); return isNaN(d) ? s : d.toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}); };
  const RANGES = [["1D", 1], ["5D", 5], ["1M", 22], ["6M", 130], ["1Y", 260], ["All", 5000]];
  const NAMES = {NIFTY: "NIFTY", BANKNIFTY: "BANK NIFTY", SENSEX: "SENSEX"};
  let U = "NIFTY", days = 22, set = "", chart = null, sets = {};
  async function get(url) { const r = await fetch(url, {cache: "no-store"}); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.status); return j; }
  async function loadSets() {
    try {
      const s = await get("/api/market-data/summary?u=" + U);
      sets = s.spot.filter(x => x.underlying === U).map(x => x.name);
      $("stUnd").innerHTML = (s.underlyings || []).map(u => `<button class="chip ${u === U ? "on" : ""}" data-u="${u}">${NAMES[u] || u}</button>`).join("");
      $("stUnd").querySelectorAll("button").forEach(b => b.onclick = () => { U = b.dataset.u; set = ""; loadSets(); });
      $("stSet").innerHTML = sets.map(n => `<option value="${esc(n)}" ${n === set ? "selected" : ""}>${esc(n)}</option>`).join("");
      $("stSet").hidden = sets.length < 2;
    } catch (e) { /* the chart below says what is missing */ }
    load();
  }
  async function load() {
    $("stRange").innerHTML = RANGES.map(([k, v]) => `<button class="chip ${v === days ? "on" : ""}" data-d="${v}">${k}</button>`).join("");
    $("stRange").querySelectorAll("button").forEach(b => b.onclick = () => { days = +b.dataset.d; load(); });
    $("stTitle").textContent = `${NAMES[U] || U}${U === "NIFTY" ? " and India VIX" : ""} · stored`;
    let d;
    try { d = await get(`/api/market-data/spot?u=${U}&days=${days}` + (set ? "&dataset=" + set : "")); }
    catch (e) { $("stNote").textContent = e.message; if (chart) { chart.destroy(); chart = null; } return; }
    set = d.dataset;
    const pts = d.points, hasVix = pts.some(p => p[2] != null), m = css("--muted");
    const ds = [{label: NAMES[U] || U, data: pts.map(p => p[1]), borderColor: css("--accent"), backgroundColor: css("--accent") + "14", fill: true, borderWidth: 1.6, pointRadius: 0, yAxisID: "y"}];
    if (hasVix) ds.push({label: "India VIX", data: pts.map(p => p[2]), borderColor: css("--warn"), borderWidth: 1.2, pointRadius: 0, yAxisID: "y2"});
    if (chart) chart.destroy();
    chart = new Chart($("stCv"), {type: "line", data: {labels: pts.map(p => d.daily ? fmtDay(p[0]) : fmtWhen(p[0])), datasets: ds},
      options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: "index", intersect: false},
        plugins: {legend: {display: hasVix, labels: {color: m, boxWidth: 12}}},
        scales: Object.assign({x: {ticks: {maxTicksLimit: innerWidth < 600 ? 3 : 6, maxRotation: 0, color: m}, grid: {display: false}},
                               y: {ticks: {color: m}, grid: {color: css("--line")}}},
                              hasVix ? {y2: {position: "right", ticks: {color: css("--warn")}, grid: {display: false}}} : {})}});
    const ch = pts.length > 1 ? pts[pts.length - 1][1] - pts[0][1] : 0;
    $("stNote").innerHTML = pts.length ? `${d.daily ? "Daily closes" : "Bars"} from ${esc(d.dataset)} · ${fmtDay(pts[0][0])} → ${fmtDay(pts[pts.length - 1][0])} · change <b class="${ch >= 0 ? "up" : "down"}">${ch >= 0 ? "▲" : "▼"} ${n2(Math.abs(ch))}</b>` : "No bars in this range.";
  }
  $("stSet").onchange = () => { set = $("stSet").value; load(); };
  loadSets();
})();
</script>
<script>
const $ = id => document.getElementById(id);
const QS = new URLSearchParams(location.search);
const RANGES = [["1d", "1D"], ["5d", "5D"], ["1m", "1M"], ["6m", "6M"], ["1y", "1Y"], ["5y", "5Y"]];
const QUICK = ["NIFTY", "BANKNIFTY", "SENSEX", "VIX"];
let cur = null, rng = "1d", style = "line", chart = null, candles = [], q = null, timer = null, ctimer = null;
const store = {get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } },
               set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};
const num = (v, d = 2) => v == null ? "–" : Number(v).toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d});
const big = v => v == null ? "–" : v >= 1e7 ? (v / 1e7).toFixed(2) + " Cr" : v >= 1e5 ? (v / 1e5).toFixed(2) + " L" : Number(v).toLocaleString("en-IN");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function marketOpen() { const t = new Date(Date.now() + 330 * 60000), m = t.getUTCHours() * 60 + t.getUTCMinutes(), d = t.getUTCDay();
  return d > 0 && d < 6 && m >= 555 && m <= 940; }

function params() {
  if (!cur) return "";
  return cur.contract ? `u=${encodeURIComponent(cur.u)}&e=${encodeURIComponent(cur.e)}&k=${encodeURIComponent(cur.k)}&r=${encodeURIComponent(cur.r)}`
                      : `s=${encodeURIComponent(cur.s)}`;
}

function quick() {
  const recent = store.get("chart.recent", []).filter(s => !QUICK.includes(s) && !/^(NSE|BSE):(NIFTY 50|NIFTY BANK|SENSEX|INDIA VIX)$/.test(s)).slice(0, 6);
  $("quick").innerHTML = [...QUICK, ...recent].map(s => `<button class="chip" data-q="${esc(s)}">${esc(s.replace(/^NSE:/, ""))}</button>`).join("");
  $("quick").querySelectorAll("[data-q]").forEach(b => b.onclick = () => open({s: b.dataset.q}));
}

function open(sel, push = true) {
  cur = sel; q = null; candles = [];
  $("err").textContent = ""; $("main").hidden = false; $("title").textContent = sel.s || `${sel.u} ${sel.e} ${sel.k} ${sel.r}`;
  $("sub").textContent = ""; $("last").textContent = "–"; $("chg").textContent = ""; $("tiles").innerHTML = ""; $("links").innerHTML = "";
  if (push) history.replaceState(null, "", "/chart?" + params() + (QS.get("h") && sel === first ? "&h=1" : ""));
  loadQuote(true); loadCandles();
}

async function getJSON(url) {
  const r = await fetch(url, {cache: "no-store"});
  if (r.status === 401) { location.href = "/signin?next=" + encodeURIComponent(location.pathname + location.search); throw new Error("sign in"); }
  const j = await r.json().catch(() => ({error: "The server sent no answer"}));
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

async function loadQuote(firstTime) {
  if (!cur) return;
  try {
    q = await getJSON("/api/chart/quote?" + params());
    $("err").textContent = "";
    if (firstTime) {
      const name = q.symbol;
      $("title").textContent = name.replace(/^(NSE|BSE):/, "");
      $("sub").textContent = name.split(":")[0] + (cur.contract ? ` · ${cur.u} ${cur.e} ${cur.k} ${cur.r}` : "");
      const recent = store.get("chart.recent", []).filter(s => s !== name);
      if (!cur.contract) { recent.unshift(name); store.set("chart.recent", recent.slice(0, 8)); quick(); }
      const plain = name.startsWith("NSE:") && !/ /.test(name) && !cur.contract;
      if (plain) $("links").innerHTML = `<a class="btn ghost" href="/stock?symbol=${encodeURIComponent(name.slice(4))}">Your history in this stock</a>`;
    }
    $("last").textContent = num(q.last);
    const c = q.change;
    $("chg").className = "chg " + (c == null ? "" : c >= 0 ? "up" : "down");
    $("chg").textContent = c == null ? "" : `${c >= 0 ? "▲ " : "▼ "}${num(Math.abs(c))} (${Math.abs(q.pct).toFixed(2)}%)`;
    $("dot").className = "dot" + (marketOpen() ? " live" : "");
    $("when").textContent = (marketOpen() ? "Live · " : "Last trade · ") + (q.time ? new Date(q.time.replace(" ", "T")).toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}) : "");
    const t = [["Open", num(q.open)], ["High", num(q.high)], ["Low", num(q.low)], ["Prev. close", num(q.prev_close)]];
    if (q.volume) t.push(["Volume", big(q.volume)]);
    if (q.oi) t.push(["Open interest", big(q.oi)]);
    $("tiles").innerHTML = t.map(([l, v]) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("");
    liveCandle();
  } catch (e) { if (firstTime) { $("main").hidden = true; } $("err").textContent = e.message; }
}

async function loadCandles() {
  if (!cur) return;
  try {
    const j = await getJSON(`/api/chart/candles?range=${rng}&` + params());
    candles = j.candles; draw();
    $("note").textContent = candles.length ? `${candles.length} ${j.interval === "minute" ? "1-minute" : j.interval === "day" ? "daily" : j.interval.replace("minute", "-minute")} bars · Zerodha` : "No price history for this range.";
  } catch (e) { $("note").textContent = ""; $("err").textContent = e.message; }
}

// during market hours the latest price moves the last bar (1D and 5D views)
function liveCandle() {
  if (!q || q.last == null || !candles.length || !["1d", "5d"].includes(rng) || !marketOpen()) return;
  const step = rng === "1d" ? 1 : 5, now = new Date(Date.now() + 330 * 60000);
  const mins = Math.floor((now.getUTCHours() * 60 + now.getUTCMinutes()) / step) * step;
  const key = now.toISOString().slice(0, 10) + "T" + String(Math.floor(mins / 60)).padStart(2, "0") + ":" + String(mins % 60).padStart(2, "0");
  const last = candles[candles.length - 1];
  if (last[0] === key) { last[2] = Math.max(last[2], q.last); last[3] = Math.min(last[3], q.last); last[4] = q.last; }
  else if (key > last[0] && key.slice(0, 10) === last[0].slice(0, 10)) candles.push([key, q.last, q.last, q.last, q.last, 0]);
  else return;
  draw(true);
}

function label(t) {
  const d = new Date(t);
  if (rng === "1d") return d.toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", hour12: false});
  if (rng === "5d" || rng === "1m") return d.toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false});
  return d.toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "2-digit"});
}

function draw(quiet) {
  const up = css("--up"), down = css("--down"), acc = css("--accent"), muted = css("--muted"), line = css("--line");
  const labels = candles.map(c => label(c[0]));
  const prev = q && q.prev_close && rng === "1d" ? q.prev_close : null;
  let ds;
  if (style === "candles") {
    const col = candles.map(c => c[4] >= c[1] ? up : down);
    ds = [{type: "bar", label: "High–low", data: candles.map(c => [c[3], c[2]]), backgroundColor: col, barPercentage: 0.12, categoryPercentage: 1, grouped: false, order: 2},
          {type: "bar", label: "Open–close", data: candles.map(c => c[1] === c[4] ? [c[1] - 0.0001, c[4] + 0.0001] : [c[1], c[4]]), backgroundColor: col, barPercentage: 0.7, categoryPercentage: 1, grouped: false, order: 1}];
  } else {
    const first = candles.length ? candles[0][1] : 0, last = candles.length ? candles[candles.length - 1][4] : 0;
    const colr = (prev ?? first) <= last ? up : down;
    ds = [{type: "line", label: "Price", data: candles.map(c => c[4]), borderColor: colr, borderWidth: 1.6, pointRadius: 0, tension: 0,
           fill: true, backgroundColor: colr + "18"}];
  }
  if (prev) ds.push({type: "line", label: "Prev. close", data: candles.map(() => prev), borderColor: muted, borderDash: [4, 4], borderWidth: 1, pointRadius: 0});
  if (chart && quiet && chart.data.labels.length === labels.length && chart.config.options._style === style) {
    chart.data.labels = labels; chart.data.datasets.forEach((d, i) => { if (ds[i]) { d.data = ds[i].data; if (ds[i].backgroundColor) d.backgroundColor = ds[i].backgroundColor; if (ds[i].borderColor) d.borderColor = ds[i].borderColor; } });
    chart.update("none"); return;
  }
  if (chart) chart.destroy();
  chart = new Chart($("cv"), {data: {labels, datasets: ds},
    options: {_style: style, responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: "index", intersect: false},
      plugins: {legend: {display: false}, tooltip: {callbacks: {
        title: c => candles[c[0].dataIndex] ? new Date(candles[c[0].dataIndex][0]).toLocaleString("en-IN", {day: "numeric", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}) : "",
        label: c => { const k = candles[c.dataIndex]; if (!k) return "";
          if (style === "candles") return c.datasetIndex === 1 ? `O ${num(k[1])}  H ${num(k[2])}  L ${num(k[3])}  C ${num(k[4])}` : null;
          return c.dataset.label === "Prev. close" ? `Prev. close ${num(c.parsed.y)}` : `${num(k[4])}${k[5] ? "  vol " + big(k[5]) : ""}`; }}}},
      scales: {x: {ticks: {maxTicksLimit: 6, maxRotation: 0, color: muted, autoSkip: true}, grid: {display: false}},
               y: {position: "right", beginAtZero: false, grace: "2%", ticks: {color: muted, callback: v => num(v, 0)}, grid: {color: line}}}}});
}

// ---- symbol box with suggestions ----
let sTimer = null, sIdx = -1, sRes = [];
function showList(res) {
  sRes = res; sIdx = -1;
  $("list").hidden = !res.length;
  $("list").innerHTML = res.map((r, i) => `<div data-i="${i}"><b>${esc(r.symbol.replace(/^NSE:/, ""))}</b><span>${esc(r.name)}${r.index ? " · index" : ""}</span></div>`).join("");
  $("list").querySelectorAll("div").forEach(d => d.onmousedown = e => { e.preventDefault(); pick(sRes[+d.dataset.i].symbol); });
}
function pick(s) { $("sym").value = s.replace(/^NSE:/, ""); $("list").hidden = true; open({s}); $("sym").blur(); }
$("sym").oninput = () => { clearTimeout(sTimer); const v = $("sym").value.trim();
  if (v.length < 2) { showList([]); return; }
  sTimer = setTimeout(() => getJSON("/api/chart/search?q=" + encodeURIComponent(v)).then(j => showList(j.results)).catch(() => showList([])), 200); };
$("sym").onkeydown = e => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); sIdx = Math.max(-1, Math.min(sRes.length - 1, sIdx + (e.key === "ArrowDown" ? 1 : -1)));
    $("list").querySelectorAll("div").forEach((d, i) => d.classList.toggle("on", i === sIdx)); }
  else if (e.key === "Enter") { e.preventDefault(); if (sIdx >= 0) pick(sRes[sIdx].symbol); else go(); }
  else if (e.key === "Escape") $("list").hidden = true; };
$("sym").onblur = () => setTimeout(() => $("list").hidden = true, 150);
function go() { const v = $("sym").value.trim(); if (v) { $("list").hidden = true; open({s: v}); } }
$("go").onclick = go;

$("ranges").innerHTML = RANGES.map(([k, l]) => `<button class="chip ${k === rng ? "on" : ""}" data-r="${k}">${l}</button>`).join("");
$("ranges").querySelectorAll("[data-r]").forEach(b => b.onclick = () => { rng = b.dataset.r;
  $("ranges").querySelectorAll("[data-r]").forEach(x => x.classList.toggle("on", x === b)); candles = []; if (chart) { chart.destroy(); chart = null; } loadCandles(); });
document.querySelectorAll("[data-style]").forEach(b => b.onclick = () => { style = b.dataset.style; store.set("chart.style", style);
  document.querySelectorAll("[data-style]").forEach(x => x.classList.toggle("on", x === b)); if (chart) { chart.destroy(); chart = null; } draw(); });
style = store.get("chart.style", "line");
document.querySelectorAll("[data-style]").forEach(x => x.classList.toggle("on", x.dataset.style === style));

quick();
const first = QS.get("u") ? {contract: true, u: QS.get("u"), e: QS.get("e"), k: QS.get("k"), r: QS.get("r")} : QS.get("s") ? {s: QS.get("s")} : {s: "NIFTY"};
open(first, false);
if (first.s) $("sym").value = first.s.replace(/^NSE:/, "");
let ticks = 0;
setInterval(() => { ticks++; if (!document.hidden && cur && (marketOpen() || ticks % 15 === 0)) loadQuote(false); }, 2000);
setInterval(() => { if (!document.hidden && cur && marketOpen()) loadCandles(); }, 60000);
</script>
</body>
</html>
"""


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading sessions · Portfolio</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed;--warn:#a06a1a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff;--warn:#e0b34a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:20px;margin:0}
h2{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600}
.status{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.status a{color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}
.dot.live{background:var(--up)}.dot.wait{background:var(--warn)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.card .big{font-size:22px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.card .sub{font-size:13px;margin-top:2px;font-variant-numeric:tabular-nums;color:var(--muted)}
.up{color:var(--up)}.down{color:var(--down)}
.block{margin-top:12px}
.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
a.btn{text-decoration:none;display:inline-block}
.note a{color:var(--accent)}
table.summary{margin-top:12px}
table.summary td{vertical-align:top}
table.summary tr.pick{cursor:pointer}
table.summary tr.pick:hover td{background:var(--bg)}
table.summary tr.sel td{background:var(--bg)}
table.summary tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.nm{font-weight:600;white-space:normal;min-width:150px}
.mt{font-size:12px;color:var(--muted);font-weight:400;white-space:normal}
.picks{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.scard{text-align:left;background:transparent;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer;font:inherit;display:flex;flex-direction:column;gap:6px}
.scard:hover{border-color:var(--accent)}
.scard.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.scard b{font-size:15px}
.scard .mg{font-size:13px;font-weight:600}.scard .mg small{font-weight:400;color:var(--muted)}
.link{background:none;border:0;color:var(--accent);cursor:pointer;font:inherit;padding:0}
#marginNote{font-size:14px;margin-top:10px}#marginNote .mt{font-size:12px;color:var(--muted);margin-top:3px}
.scard .d{font-size:13px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.scard .run{font-size:12px;color:var(--up)}
.tag{font-size:12px;font-weight:600;padding:1px 6px;border-radius:5px;border:1px solid var(--line);color:var(--muted);margin-right:4px;white-space:nowrap}
.tag.good{border-color:var(--up);color:var(--up)}.tag.risk{border-color:var(--warn);color:var(--warn)}
@media (max-width:860px){.picks{grid-template-columns:1fr 1fr}}
@media (max-width:520px){.picks{grid-template-columns:1fr}.nm{min-width:120px}}
.btn.stop{background:var(--down)}
.btn:disabled{opacity:.5;cursor:default}
.form{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 14px}
.form label{font-size:13px;color:var(--muted);display:block;margin-bottom:4px}
.form input,.form select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);font-size:15px}
.form input[type=checkbox]{width:auto}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.desc{color:var(--muted);font-size:13px;margin:6px 0 12px}
.feeds{display:flex;gap:16px;flex-wrap:wrap;font-size:14px;margin:4px 0 12px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}
.empty{color:var(--muted);font-size:14px;padding:6px 0}
.chartbox{height:230px;position:relative}
details.sec{border-top:1px solid var(--line);margin-top:4px}
details.sec>summary{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 2px;cursor:pointer;list-style:none;font-weight:650;font-size:15px}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary .st::before{content:"\25B8";display:inline-block;width:16px;color:var(--muted);transition:transform .15s}
details.sec[open]>summary .st::before{transform:rotate(90deg)}
details.sec>summary .sn{font-weight:500;font-size:12.5px;color:var(--muted);text-align:right}
details.sec>.sb{padding:0 0 12px}
.secbar{justify-content:flex-end;font-size:13px;margin-top:12px;color:var(--muted);gap:6px}
.cal .grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;margin-top:8px}
.cal .hd{font-size:12px;color:var(--muted);text-align:center;text-transform:uppercase;letter-spacing:.04em}
.cal .d{min-height:52px;border:1px solid var(--line);border-radius:8px;padding:4px 5px;font-size:12px;line-height:1.2;
  font-variant-numeric:tabular-nums;overflow:hidden;background:var(--card)}
.cal .d.x{border-color:transparent;background:none}
.cal .d.has{cursor:pointer}.cal .d.pick{outline:2px solid var(--accent);outline-offset:-1px}
.cal .d .n{color:var(--muted)}.cal .d .v{font-weight:650;font-size:12px;margin-top:3px;white-space:nowrap}
.cal .d .k{color:var(--muted);font-size:11px}
.cal .nav{display:flex;align-items:center;gap:8px}
.cal .nav button{font:inherit;border:1px solid var(--line);background:none;color:var(--text);border-radius:8px;padding:2px 10px;cursor:pointer}
.cal .nav button:disabled{opacity:.35;cursor:default}
@media (max-width:520px){.cal .d{min-height:46px;padding:3px}.cal .d .v{font-size:11px}.cal .d .k{display:none}}
.paygrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.paymini{border:1px solid var(--line);border-radius:10px;padding:10px;cursor:pointer}
.paymini.sel{border-color:var(--accent)}
.minibox{height:170px;position:relative;margin:6px 0}
.log{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);max-height:260px;overflow:auto;white-space:pre-wrap;margin:0}
.banner{background:var(--card);border:1px solid var(--warn);border-radius:12px;padding:12px 14px;margin-bottom:12px;font-size:14px}
.err{color:var(--down);font-size:14px}
.note{color:var(--muted);font-size:12px;margin-top:8px}
.hide{display:none}
.sess{margin-top:14px}
.sess-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
.sess-head h2{margin:0;color:var(--text);font-size:16px}
.sess-head .meta{font-size:13px;color:var(--muted);margin-top:3px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}
.tile .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.tile .big{font-size:19px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .sub{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
details{margin-top:10px}
summary{cursor:pointer;color:var(--muted);font-size:13px;font-weight:600}
h3{font-size:13px;color:var(--muted);margin:14px 0 6px;font-weight:600}
.total{display:flex;gap:18px;flex-wrap:wrap;align-items:baseline}
.chips{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.chip{display:flex;flex-direction:column;align-items:flex-start;gap:1px;min-width:0;border:1px solid var(--line);border-radius:999px;
  padding:7px 14px;background:var(--card);color:var(--text);font:inherit;cursor:pointer;text-align:left}
.chip .cn{font-size:13px;font-weight:600;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip .cv{font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums}
.chip .cl{font-weight:400;color:var(--muted)}
.chip .dot{width:7px;height:7px}
.chip.sel{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.chipcard{padding:10px}
@media (max-width:420px){.chip{padding:6px 10px}.chip .cl{display:none}}
.total .big{font-size:24px}
.badge{font-size:12px;font-weight:700;letter-spacing:.05em;padding:2px 7px;border-radius:5px;margin-left:6px;vertical-align:middle;border:1px solid var(--line);color:var(--muted)}
.badge.shadow{border-color:var(--warn);color:var(--warn)}
.badge.live{background:var(--down);border-color:var(--down);color:#fff}
.stopped{border:1px solid var(--warn);border-radius:8px;padding:8px 10px;margin-top:10px;font-size:13px}
.livebox{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:4px 0 12px}
.livebox.live{border-color:var(--down)}
@media (max-width:720px){.tiles{grid-template-columns:repeat(2,1fr)}}
/* session card: one header line, icon buttons, three numbers, two-line rows */
.sess-head{align-items:center}
.sess-head h2{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.acts{display:flex;gap:6px;margin-left:auto}
.ibtn.danger{color:var(--down);border-color:color-mix(in srgb,var(--down) 45%,var(--line))}
.ibtn.armed{background:var(--down);border-color:var(--down);color:#fff}
.ibtn.armed span{font-size:12.5px}
.ibtn.sm{height:30px;min-width:30px;padding:0 6px;color:var(--muted)}
.ibtn.sm .ic{width:15px;height:15px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip2{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 8px;white-space:nowrap}
.chip2 b{color:var(--text);font-weight:600}
.chip2.warn{color:var(--warn);border-color:var(--warn)}
.chip2 .ic{width:13px;height:13px}
.knums{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:10px;margin-top:12px}
.kn .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.kn .lbl small{text-transform:none;letter-spacing:0;font-size:12px;border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:3px}
.kn .big{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.2}
.kn .ks{font-size:12px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.ixline{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin-top:10px;font-variant-numeric:tabular-nums}
.ixline b{color:var(--text);font-weight:600}
.ixline .secbar{margin:0 0 0 auto;display:flex;gap:4px}
.plist{font-variant-numeric:tabular-nums}
.prow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 2px;border-top:1px solid var(--line)}
.prow:first-child{border-top:0}
.prow .pc{font-weight:600;font-size:14px;color:var(--text);text-decoration:none;display:inline-block;padding:4px 8px 4px 0;margin:-4px 0}
a.pc:hover{color:var(--accent)}
.prow .ps{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--muted)}
.prow .ps .ic{width:12px;height:12px}
.prow .pr{text-align:right}.prow .pr .ps{justify-content:flex-end}
.prow .pv{font-weight:650;font-size:14.5px}
.closedhd{margin:10px 2px 2px;font-size:12px}.plist.closed .prow{opacity:.72}
details.cyc{border-top:1px solid var(--line)}details.cyc:first-child{border-top:0}
details.cyc>summary.prow{border-top:0;cursor:pointer;list-style:none}
details.cyc>summary::-webkit-details-marker{display:none}
details.cyc>summary .pc::after{content:" \25B8";color:var(--muted);font-weight:400}
details.cyc[open]>summary .pc::after{content:" \25BE"}
details.cyc .legs{padding:0 2px 10px}
table.lt td{vertical-align:top}table.lt .mt{white-space:nowrap}
.cnt{font-size:12px;color:var(--muted);font-weight:500;margin-left:4px}
.emptybox{grid-column:1/-1;display:flex;flex-direction:column;align-items:center;gap:10px;padding:18px 8px;color:var(--muted);text-align:center}
.emptybox>.ic{width:30px;height:30px;color:var(--muted)}
details.sec>summary .st{display:inline-flex;align-items:center;gap:6px}
@media (max-width:600px){.kn .big{font-size:18px}.knums{gap:8px}}
/* icons in place of words: price feed, stale data, open legs */
.ic{width:14px;height:14px;display:inline-block;vertical-align:-2px;flex:none}
.ic.live{color:var(--up)}.ic.sim,.ic.stale,.ic.poll{color:var(--warn)}.ic.closed{color:var(--muted)}
.status .ic{width:15px;height:15px}
.status b{color:var(--text);font-weight:600}
.pips{display:inline-flex;gap:2px;align-items:center;flex:none}
.pips i{width:3px;height:11px;border-radius:1px;background:var(--accent);opacity:.8}
.pips small{font-size:12px;color:var(--muted);font-weight:600;margin-left:2px}
/* totals and the session list on Overview */
.tot{display:flex;gap:22px;align-items:flex-end;flex-wrap:wrap}
.tot .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.tot .lbl small{text-transform:none;letter-spacing:0;font-size:12px;border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:4px}
.tot .big{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.15}
.tacts{margin-left:auto;display:flex;gap:8px}
.ibtn{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:38px;min-width:38px;padding:0 11px;border-radius:9px;
  border:1px solid var(--line);color:var(--accent);background:transparent;text-decoration:none;font-size:13px;font-weight:600}
.ibtn.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.ibtn.dep{width:auto;padding:0 10px;gap:6px}.ibtn.dep span{font-size:13px;font-weight:600}
.depbox{border:1px solid var(--accent);border-radius:12px;padding:14px;margin-bottom:14px;background:color-mix(in srgb,var(--accent) 5%,var(--card))}
.depbox h2{margin:0 0 4px}.depbox h3{font-size:14px;margin:14px 0 8px}
.dlist{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.dlist span{font-size:13px;padding:6px 10px;border-radius:8px;background:var(--card);border:1px solid var(--line)}
.dlist b{color:var(--muted);font-weight:600;margin-right:4px}
.dw{font-size:13px;padding:10px 12px;border-radius:10px;margin-bottom:8px;border:1px solid var(--line);background:var(--card)}
.dw.bad{border-color:color-mix(in srgb,var(--down) 45%,var(--card));background:color-mix(in srgb,var(--down) 8%,var(--card))}
.dw.warn{border-color:color-mix(in srgb,var(--warn) 45%,var(--card));background:color-mix(in srgb,var(--warn) 8%,var(--card))}
.dw.ok{border-color:color-mix(in srgb,var(--up) 40%,var(--card));background:color-mix(in srgb,var(--up) 7%,var(--card))}
.drow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}.btn.sm{padding:6px 12px;font-size:13px;min-height:0}
.inl{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--text);margin:0}.inl input{width:auto}
.ibtn .ic{width:17px;height:17px;vertical-align:0}
.tsub{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12.5px;margin-top:6px}
.livectl{flex-wrap:wrap;gap:8px}.livectl .grow{flex:1}.livectl input{width:3.2em;font:inherit;padding:2px 4px}
.livectl button{font:inherit;font-size:12.5px;padding:4px 10px;border-radius:8px;border:1px solid var(--line, #ccc);background:transparent;color:inherit}
.livectl button.danger{border-color:var(--neg, #c0392b);color:var(--neg, #c0392b)}.livectl button.armed{background:var(--neg, #c0392b);color:#fff}
.slist{margin-top:8px;font-variant-numeric:tabular-nums}
.srow{display:grid;grid-template-columns:minmax(0,1fr) repeat(3,minmax(66px,auto));gap:0 12px;align-items:center;
  padding:9px 4px;border-top:1px solid var(--line);cursor:pointer;color:inherit;text-decoration:none}
.srow.hd{cursor:default;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;padding:2px 4px 5px;border-top:0}
.srow .n{text-align:right;font-size:14px;white-space:nowrap}
.srow .n.t{font-weight:700}
.srow:not(.hd):hover{background:var(--bg)}
.srow.flat .sname,.srow.flat .n{opacity:.55}
.sname{display:flex;align-items:center;gap:6px;min-width:0;font-weight:600;font-size:14px;line-height:1.25}
.sname .tx{min-width:0;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.sname .badge{margin-left:0}
.lgd{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:-4px 0 10px}
.lgd span{display:inline-flex;align-items:center;gap:5px}
.sw{display:inline-block;width:14px;height:0;border-top:2px solid var(--accent)}
.sw.ex{border-top-color:var(--muted)}
.sw.ix{width:8px;height:8px;border:0;border-radius:50%;background:var(--accent)}
@media (max-width:600px){
  header h1{display:none}
  header{margin-bottom:8px !important}
  .tot{gap:16px}.tot .big{font-size:21px}
  .ibtn .wide{display:none}
}
@media (max-width:420px){
  .srow{grid-template-columns:minmax(0,1fr) repeat(3,minmax(52px,auto));gap:0 8px;padding:8px 2px}
  .srow .n,.sname{font-size:13px}
}
@media (max-width:720px){.cards{grid-template-columns:repeat(2,1fr)}.form{grid-template-columns:1fr 1fr}}
@media (max-width:420px){.form{grid-template-columns:1fr}}
.mfilt{display:flex;gap:6px;margin:8px 0 2px}.mfilt button{font:inherit;font-size:12.5px;padding:4px 12px;border-radius:999px;border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer}
.mfilt button.on{background:var(--accent);border-color:var(--accent);color:#fff}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="title">Paper trading</h1>
    <div class="status"><span class="dot" id="dot"></span><span id="status">Loading…</span></div>
  </header>

  <div class="banner hide" id="banner"></div>

  <div id="listView">
    <div class="card" id="totalCard">
      <div class="tot">
        <div><div class="lbl"><span id="grandLabel">Total</span><small title="after all Zerodha charges">net</small></div><div class="big" id="grand">–</div></div>
        <div><div class="lbl">Today</div><div class="big" id="grandDay">–</div></div>
        <div class="tacts"><a class="ibtn" id="dlAll" href="#" download title="Download metrics (CSV)" aria-label="Download metrics (CSV)"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>CSV</a>
          <a class="ibtn pri" id="newBtn" href="#" title="Start a session" aria-label="Start a session"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg><span class="wide">New session</span></a></div>
      </div>
      <div class="mfilt hide" id="modeFilt" role="group" aria-label="Show sessions"><button type="button" data-m="all">Both</button><button type="button" data-m="live">LIVE</button><button type="button" data-m="shadow">SHADOW</button></div>
      <div class="tsub" id="grandSub"></div>
      <div class="tsub livectl hide" id="liveCtl"></div>
      <div class="slist" id="summary"></div>
    </div>
    <div class="card chipcard" id="chipCard"><div class="chips" id="chips"></div></div>
    <div id="detail"></div>
  </div>

  <div id="overView" class="hide">
    <div class="card" id="payoffs"><h2 style="margin-top:0">Payoffs</h2>
      <div class="lgd"><span><i class="sw ex"></i>At expiry</span><span><i class="sw"></i>Today</span><span><i class="sw ix"></i>Index now</span></div>
      <div class="paygrid" id="payGrid"></div></div>
    <div class="card cal" id="calAll"></div>
  </div>

  <div id="newView" class="hide">
    <div class="card block" id="picker"></div>
    <div class="card block" id="controls"></div>
  </div>
  <div class="note" id="footNote"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const inr = x => (x < 0 ? "-₹" : "₹") + Math.abs(x).toLocaleString("en-IN", {maximumFractionDigits: 0});
const sign = x => x > 0 ? "up" : x < 0 ? "down" : "";
const ainr = x => inr(Math.abs(x || 0));   // coloured figures carry no + / −: green and red already say which way
const lakh = x => x == null ? "–" : Math.abs(x) >= 1e5 ? (x < 0 ? "-" : "") + "₹" + (Math.abs(x) / 1e5).toFixed(2) + " L" : inr(x);
const MG = () => last.margins || {strategies: {}, sessions: {}};
const mtime = t => { const d = new Date(String(t).replace(" ", "T")); return isNaN(d) ? t : d.toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}); };
function marginOf(name) { const m = MG().strategies[name]; return m && m.margin ? m : null; }
function marginNote() {
  const box = $("marginNote"); if (!box) return;
  const st = stratOf($("strat").value), m = marginOf(st.name), mg = MG();
  if (!m) { box.innerHTML = mg.time ? `Margin: not available for this strategy at ${esc(TZ.hm(mg.time))}${(mg.strategies[st.name] || {}).error ? " (" + esc(mg.strategies[st.name].error) + ")" : ""}.` : "Margin: the engine has not checked Zerodha's margins yet."; return; }
  const el = $("p_lots") || $("p_multiplier"), n = Math.max(1, parseInt(el ? el.value : 1) || 1);
  const legs = (m.legs || []).map(l => `${esc(l.text)} ×${l.qty}${l.price != null ? " @ " + l.price.toFixed(2) : ""}`).join(" · ");
  box.innerHTML = `<b>Margin about ${lakh(m.margin * n)}</b> for ${n} ${st.name === "blaze_butterfly" ? "unit" : "lot"}${n > 1 ? "s" : ""} (Zerodha, ${lakh(m.margin)} per ${esc(m.unit)}`
    + `${m.without_hedge > m.margin ? `; ${lakh(m.without_hedge)} without the hedge benefit` : ""}).`
    + `<div class="mt">Entry legs at ${esc(mg.time ? TZ.hm(mg.time) : "")} with default settings: ${legs}${m.net_credit != null ? ` · net ${m.net_credit >= 0 ? "credit" : "debit"} ${inr(Math.abs(m.net_credit))}` : ""}</div>`;
}
async function refreshMargins(btn) {
  btn.disabled = true;
  try { const r = await fetch("/api/paper/margins", {method: "POST"}); const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || r.status); btn.textContent = "Asked: updates within 5 minutes"; }
  catch (e) { btn.textContent = e.message; }
}
const FEEDS = {kite: "Zerodha live prices", sim: "Simulated prices (fast replay, for checking only)"};
// small line icons used in place of words (title text says what they mean)
const ICONS = {
  live: '<path d="M5 12a7 7 0 0 1 2-5M19 12a7 7 0 0 0-2-5M8.5 12a3.5 3.5 0 0 1 1-2.5M15.5 12a3.5 3.5 0 0 0-1-2.5"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><path d="M12 14v6"/>',
  poll: '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>',
  sim: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3M7.5 15h9"/>',
  closed: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  stale: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  squareoff: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  play: '<path d="M7 5l12 7-12 7z"/>',
  reset: '<path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v5h5"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  wallet: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 15h2"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
  expand: '<path d="M7 9l5 5 5-5M7 4l5 5 5-5"/>',
  collapse: '<path d="M7 15l5-5 5 5M7 20l5-5 5 5"/>',
  sold: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  bought: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  deploy: '<path d="M12 16V4M6 10l6-6 6 6"/><path d="M5 20h14"/>'};
// an icon button; armed (second tap needed) shows its warning text
const ibtn = (act, k, title, cls, armedText) => `<button type="button" class="ibtn ${cls || ""}${armedText ? " armed" : ""}" data-act="${act}" title="${esc(title)}" aria-label="${esc(title)}">${icon(k, title)}${armedText ? `<span>${esc(armedText)}</span>` : ""}</button>`;
const icon = (k, title) => `<svg class="ic ${k}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title>${ICONS[k]}</svg>`;
// the price feed as an icon: nothing for Zerodha live prices (the page header shows the live feed once), a flask for simulated
const feedIcon = f => f === "sim" ? icon("sim", FEEDS.sim) : "";
// one bar per open leg (up to 6)
const pips = n => n ? `<span class="pips" title="${n} open leg${n > 1 ? "s" : ""}">${"<i></i>".repeat(Math.min(n, 6))}${n > 6 ? `<small>${n}</small>` : ""}</span>` : "";
// short names for lists (the session card keeps the full title)
const SHORT = {short_straddle: "Positional straddle", intraday_straddle: "Intraday straddle", intraday_strangle: "Intraday strangle",
  premium_strangle: "Premium strangle", iron_fly: "Iron fly / condor", event_iron_fly: "Event iron fly", double_calendar: "Double calendar",
  put_credit_spread: "Put credit spread", blaze_butterfly: "Blaze butterfly", straddle_trend_switch: "Straddle grid trend"};
const shortName = x => (SHORT[x.strategy] || (stratOf(x.strategy) || {title: x.strategy}).title.split(" (")[0]) + (x.label ? " · " + x.label : "");
// the newest update of any session: shown once in the page header instead of on every row
const tms = t => { const d = new Date(String(t || "").replace(" ", "T")); return isNaN(d) ? 0 : d.getTime(); };
const latestTime = () => Math.max(0, ...(last.sessions || []).filter(x => x.session).map(x => tms(x.session.time)));
const clock = ms => { if (!ms) return ""; const d = new Date(ms), now = new Date();
  return d.toLocaleString("en-IN", d.toDateString() === now.toDateString() ? {hour: "2-digit", minute: "2-digit", hour12: false}
    : {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}); };
const KIND = "__KIND__", LIVEPAGE = KIND === "live", VIEW = "__VIEW__", NEWVIEW = VIEW === "new", OVERVIEW = VIEW === "overview";
const QS = new URLSearchParams(location.search);
let last = null, busy = false, formFor = "", pickFor = "", copied = false;
let selected = decodeURIComponent(location.hash.slice(1));
const TITLE = NEWVIEW ? (LIVEPAGE ? "Start a shadow or live session" : "Start a paper session")
  : (LIVEPAGE ? "Live trading" : "Paper trading") + (OVERVIEW ? " · overview" : "");
$("title").textContent = TITLE;
document.title = TITLE + " · Portfolio";
$("listView").classList.toggle("hide", NEWVIEW);
$("newView").classList.toggle("hide", !NEWVIEW);
$("overView").classList.toggle("hide", !OVERVIEW);
if (OVERVIEW) { $("chipCard").classList.add("hide"); $("detail").classList.add("hide"); }
else if (!NEWVIEW) $("totalCard").classList.add("hide");   // Sessions: strategies as buttons; totals and the list are on Overview
$("newBtn").href = "/" + KIND + "/new";
$("dlAll").href = "/api/paper/metrics.csv?kind=" + KIND;
// Live › Overview: show LIVE, SHADOW or both (remembered on this device)
const MODE_FILTER = LIVEPAGE && OVERVIEW;
let modeFilter = "all";
try { const m = localStorage.getItem("live.overview.mode"); if (["all", "live", "shadow"].includes(m)) modeFilter = m; } catch (e) {}
const modeOfX = x => (x.desired || {}).mode || (x.session && x.session.mode) || "paper";
const inView = x => !MODE_FILTER || modeFilter === "all" || modeOfX(x) === modeFilter;
const shown = () => (last.sessions || []).filter(inView);
function drawModeFilt() {
  const el = $("modeFilt");
  el.classList.toggle("hide", !MODE_FILTER);
  el.querySelectorAll("button").forEach(b => { b.classList.toggle("on", b.dataset.m === modeFilter); b.setAttribute("aria-pressed", String(b.dataset.m === modeFilter)); });
  $("grandLabel").textContent = !LIVEPAGE ? "Total" : !MODE_FILTER || modeFilter === "all" ? "Total · shadow + live" : "Total · " + modeFilter;
}
$("modeFilt").querySelectorAll("button").forEach(b => b.onclick = () => {
  modeFilter = b.dataset.m;
  try { localStorage.setItem("live.overview.mode", modeFilter); } catch (e) {}
  drawModeFilt(); render();
});
drawModeFilt();
$("footNote").textContent = LIVEPAGE
  ? "SHADOW sessions check everything a live session would do but send nothing. Only sessions marked LIVE send real orders. Several LIVE sessions may run at once, each with its own lots, orders and daily loss limit, and may hold the same contract; before its first order each one checks that Zerodha's positions equal the sum of all LIVE sessions' legs. Every Zerodha charge is deducted."
  : "Paper only: no orders reach Zerodha. Each session has its own account; every Zerodha charge is deducted.";
const charts = {}, armed = {};
// which sections of a session are open: remembered on this device (per session and section)
let open = {};
try { open = JSON.parse(localStorage.getItem(KIND + ".open") || "{}") || {}; } catch (e) { open = {}; }
function saveOpen() { try { localStorage.setItem(KIND + ".open", JSON.stringify(open)); } catch (e) {} }
// a collapsible section: title (with an optional note on the right), body, open by default or not
function sec(id, k, title, body, def, note) {
  const isOpen = open[id + k] === undefined ? def : open[id + k];
  return `<details class="sec" data-k="${k}" ${isOpen ? "open" : ""}><summary><span class="st">${title}</span>${note ? `<span class="sn">${note}</span>` : ""}</summary><div class="sb">${body}</div></details>`;
}

async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
const stratOf = name => (last.catalog || []).find(c => c.name === name);
const isOn = x => x.running || !!x.desired;

// ---- start form -------------------------------------------------------------
function renderForm() {
  const c = $("controls"), cat = last.catalog || [];
  const head = "Settings";
  if (!cat.length) { c.innerHTML = `<h2>${head}</h2><div class="empty">The Algo Lab engine has not started yet.</div>`; return; }
  const key = cat.map(x => x.name).join(",");
  if (key === formFor && $("strat")) return;   // keep what the user is typing
  formFor = key;
  const choices = LIVEPAGE ? {shadow: MODES.shadow, live: MODES.live} : {};
  c.innerHTML = `<h2>${head}</h2>
    <select id="strat" class="hide">${cat.map(x => `<option value="${esc(x.name)}">${esc(x.title)}</option>`).join("")}</select>
    <div class="form"><div><label>Strategy</label><div id="stratName" style="font-weight:650;padding:8px 0">–</div></div>
      <div><label for="label">Name (optional, e.g. "target 15")</label><input id="label" type="text" maxlength="40" autocomplete="off"></div></div>
    <div class="desc" id="desc"></div>
    <div class="desc" id="presetNote"></div>
    <div class="feeds ${LIVEPAGE ? "hide" : ""}">${Object.entries(FEEDS).map(([k, v]) => `<label><input type="radio" name="feed" value="${k}" ${k === "kite" ? "checked" : ""}> ${v}</label>`).join("")}</div>
    <div class="feeds ${LIVEPAGE ? "" : "hide"}" id="modes">${Object.entries(choices).map(([k, v]) => `<label><input type="radio" name="mode" value="${k}" ${k === "shadow" ? "checked" : ""}> ${v}</label>`).join("")}</div>
    <div class="livebox ${LIVEPAGE ? "" : "hide"}" id="livebox">
      <div class="desc" id="liveDesc"></div>
      <div class="form">
        <div><label for="l_max_lots">Max lots per order</label><input id="l_max_lots" type="number" min="1" step="1" value="1"></div>
        <div><label for="l_daily_loss_limit">Daily loss limit (₹) - squares off and stops</label><input id="l_daily_loss_limit" type="number" min="0" step="500" value="5000"></div>
        <div><label for="l_max_orders_per_day">Max orders a day to open positions</label><input id="l_max_orders_per_day" type="number" min="1" step="1" value="40"></div>
        <div id="confirmBox" class="hide"><label for="l_confirm">Type LIVE to confirm real orders</label><input id="l_confirm" type="text" autocomplete="off"></div>
      </div>
    </div>
    <div class="form" id="params"></div>
    <div id="marginNote"></div>
    <div class="row"><button class="btn" id="startBtn">Start</button><span class="err" id="formErr"></span></div>
    <div class="note">Starts a new session with its own account. To continue an existing one, use Resume on its card in Sessions.</div>`;
  $("strat").onchange = () => { fillParams(); modeUI(); };
  const want = QS.get("strategy");
  if (want && stratOf(want)) $("strat").value = want;
  document.querySelectorAll("input[name=mode]").forEach(r => r.onchange = modeUI);
  $("startBtn").onclick = start;
  fillParams();
  modeUI();
  applyPreset((k, v) => { const el = $("p_" + k); if (!el) return; if (el.type === "checkbox") el.checked = !!v; else el.value = v; }, marginNote);
}

// Algo Lab › Strategies: open with a saved preset (?strategy=…&preset=…): its values fill the form
let PRESET = null;
async function applyPreset(setVal, done) {
  const q = new URLSearchParams(location.search), sn = q.get("strategy"), id = q.get("preset");
  if (!sn || !id) return;
  try {
    const r = await fetch("/studio/api/strategies/" + encodeURIComponent(sn), {cache: "no-store"}); if (!r.ok) return;
    const p = ((await r.json()).presets || []).find(x => x.id === id); if (!p) return;
    PRESET = {id: p.id, strategy: sn, name: p.name};
    for (const [k, v] of Object.entries(p.params || {})) setVal(k, v);
    const n = document.getElementById("presetNote");
    if (n) n.innerHTML = `Preset <b>${esc(p.name)}</b> (v${p.version}) · <a href="/studio/#/s/${encodeURIComponent(sn)}/presets">change</a>`;
    if (done) done();
  } catch (e) {}
}
const presetFor = name => PRESET && PRESET.strategy === name ? {id: PRESET.id} : null;
const MODES = {paper: "Paper (simulated orders)", shadow: "Shadow (checks real orders, sends none)", live: "LIVE (real orders)"};
const modeOf = () => LIVEPAGE ? ((document.querySelector("input[name=mode]:checked") || {}).value || "shadow") : "paper";
const feedOf = () => LIVEPAGE ? "kite" : document.querySelector("input[name=feed]:checked").value;
function modeUI() {
  if (!LIVEPAGE) return;
  const ready = !!(stratOf($("strat").value) || {}).live_ready, liveRadio = document.querySelector("input[name=mode][value=live]");
  liveRadio.disabled = !ready;
  liveRadio.parentElement.title = ready ? "" : "Not enabled for live orders yet";
  if (!ready && liveRadio.checked) document.querySelector("input[name=mode][value=shadow]").checked = true;
  const m = modeOf();
  $("livebox").classList.toggle("live", m === "live");
  $("confirmBox").classList.toggle("hide", m !== "live");
  $("liveDesc").textContent = m === "live"
    ? "REAL ORDERS will be sent to Zerodha from the server. Limit orders, NRML; a protective stop is left at Zerodha for every option sold. Any rejection or partial fill squares off and stops the session, and you get an alert."
    : "Shadow: everything a live session does - limits, prices, the exact orders - but nothing is sent; fills are simulated. Run this first.";
  $("startBtn").textContent = m === "live" ? "Start LIVE trading" : "Start shadow session";
  $("startBtn").classList.toggle("stop", m === "live");
}


// ---- Orders & stops (algolab/risk.py): the "risk.*" settings, folded under one heading ----
function riskSplit(ps) { return [ps.filter(p => p.group !== "risk"), ps.filter(p => p.group === "risk")]; }
const RISK_SECTIONS = [["", ["stops"]], ["Each leg", ["leg_unit", "leg_stop", "leg_trail_every", "leg_trail_by", "leg_trail_after", "leg_trail_gap"]],
  ["Whole position (Rs)", ["cycle_loss", "cycle_target", "cycle_lock_after", "cycle_giveback", "reenter_after_stop"]], ["Day (Rs)", ["day_loss", "day_profit"]],
  ["Orders (live and shadow)", ["order_type", "chase_seconds", "max_chase_pct", "protect_mult"]]];
function riskRows(ps, row, orders) {
  const by = Object.fromEntries(ps.map(p => [p.key.slice(5), p]));
  return RISK_SECTIONS.filter(([t]) => orders || !t.startsWith("Orders")).map(([t, keys]) => {
    const rows = keys.filter(k => by[k]).map(k => row(by[k])).join("");
    return rows ? (t ? `<div style="grid-column:1/-1;margin:12px 0 2px;font-weight:600">${t}</div>` : "") + rows : "";
  }).join("");
}
function riskBox(st, rowsHtml) {
  if (!rowsHtml) return "";
  const own = st.own_stops || [];
  return `<details class="riskbox" style="grid-column:1/-1;margin-top:8px"><summary><b>Orders &amp; stops</b> <span class="muted">same rules in backtests, paper and live</span></summary>
    <div class="muted" style="font-size:13px;margin:6px 0">${own.length ? "This strategy's own stops: " + esc(own.join("; ")) + ". " : ""}<b>own</b> = the strategy's own stops only (the values below are not used) · <b>framework</b> = these only (its own are switched off) · <b>both</b> = whichever is hit first. 0 = off.</div>
    ${rowsHtml}<div style="margin-top:8px"><button type="button" class="riskSave" data-s="${esc(st.name)}">Save as this strategy's default</button> <span class="riskMsg muted"></span></div></details>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest(".riskSave"); if (!b) return;
  const risk = {};
  document.querySelectorAll('[id^="p_risk."], [id^="f_risk."]').forEach(el => { risk[el.id.slice(7)] = el.type === "checkbox" ? el.checked : el.value; });
  const msg = b.parentElement.querySelector(".riskMsg");
  try {
    const r = await fetch("/api/risk-defaults", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({strategy: b.dataset.s, risk})});
    const j = await r.json(); msg.textContent = r.ok ? "Saved: new sessions, backtests and sweeps start with these." : (j.error || "Could not save.");
  } catch (err) { msg.textContent = err.message; }
});
function fillParams(vals) {
  const st = stratOf($("strat").value);
  vals = vals || {};
  $("desc").textContent = st.description || "";
  if ($("stratName")) $("stratName").textContent = st.title;
  document.querySelectorAll(".scard").forEach(b => b.classList.toggle("on", b.dataset.s === st.name));
  const [ownPs, riskPs] = riskSplit(st.params);
  const row = p => {
    const v = vals[p.key] ?? p.default, id = "p_" + p.key;
    if (p.type === "bool") return `<div><label><input type="checkbox" id="${id}" ${v ? "checked" : ""}> ${esc(p.label)}</label></div>`;
    if (p.choices) return `<div><label for="${id}">${esc(p.label)}</label><select id="${id}">${p.choices.map(o => `<option ${o == v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
    const num = p.type === "int" || p.type === "float";
    return `<div><label for="${id}">${esc(p.label)}</label><input id="${id}" ${num ? `type="number" step="${p.type === "int" ? 1 : "any"}"` : 'type="text"'} ${p.min != null ? `min="${p.min}"` : ""} value="${esc(v)}"></div>`;
  };
  $("params").innerHTML = ownPs.map(row).join("") + riskBox(st, riskRows(riskPs, row, true));
  $("params").oninput = marginNote;
  marginNote();
}

function copyToForm(x) {
  const s = x.session || {}, d = x.desired || {};
  if (!$("strat")) return;
  $("strat").value = x.strategy;
  const rk = Object.fromEntries(Object.entries(d.risk || s.risk || {}).map(([k, v]) => ["risk." + k, v]));
  fillParams(Object.assign({}, d.params || s.params || {}, rk));
  modeUI();
  const feed = d.feed || s.feed;
  const r = document.querySelector(`input[name=feed][value="${feed}"]`); if (r) r.checked = true;
  $("label").value = ""; $("label").placeholder = (x.label ? x.label + " (copy)" : "e.g. target 15");
  $("controls").scrollIntoView({behavior: "smooth"});
  $("label").focus();
}

// ---- Deploy: the same settings to Shadow or LIVE, with what could make it behave unlike the paper run ----------
let DEP = null;     // {src: the session deployed, allow: place orders above max lots with a warning}
function deployInit(x) {
  const s = x.session || {}, d = x.desired || {}, srcMode = d.mode || s.mode || "paper", lim = d.live || s.live || {};
  DEP = {src: x, srcMode, allow: !!lim.allow_over_max_lots};
  const st = stratOf(x.strategy) || {title: x.strategy, live_ready: false};
  const want = srcMode === "shadow" && st.live_ready ? "live" : "shadow";
  const r = document.querySelector(`input[name=mode][value=${want}]`); if (r && !r.disabled) r.checked = true;
  modeUI();                  // the LIVE confirmation box and the button follow the mode
  if (lim.max_lots) $("l_max_lots").value = lim.max_lots;
  if (lim.daily_loss_limit) $("l_daily_loss_limit").value = lim.daily_loss_limit;
  if (lim.max_orders_per_day) $("l_max_orders_per_day").value = lim.max_orders_per_day;
  const name = x.label || x.id;
  $("label").value = ""; $("label").placeholder = name + " · " + want;
  const params = d.params || s.params || {}, risk = d.risk || s.risk || {};
  const changed = (st.params || []).filter(p => !p.key.startsWith("risk.") && params[p.key] !== undefined && params[p.key] != p.default).length;
  const box = document.createElement("div");
  box.id = "deployBox"; box.className = "depbox";
  box.innerHTML = `<h2>Deploy “${esc(name)}”</h2>
    <div class="desc">A new session with the same strategy and settings. The ${esc(srcMode)} session keeps running unless you stop it.</div>
    <div class="dlist"><span><b>Strategy</b> ${esc(st.title || x.strategy)}</span>${(d.preset || s.preset) ? `<span><b>Preset</b> ${esc((d.preset || s.preset).name || (d.preset || s.preset).id || "")}</span>` : ""}
      <span><b>Settings</b> all carried over (${changed} changed from the defaults)</span><span><b>Orders &amp; stops</b> ${Object.keys(risk).length ? Object.keys(risk).length + " set" : "defaults"}</span></div>
    <h3>Could make it behave unlike the ${esc(srcMode)} run</h3><div id="deployWarn"></div>`;
  $("controls").insertBefore(box, $("controls").firstChild);
  ["l_max_lots", "l_daily_loss_limit", "l_max_orders_per_day"].forEach(id => $(id).addEventListener("input", deployWarn));
  document.querySelectorAll("input[name=mode]").forEach(r => r.addEventListener("change", () => { deployWarn(); const m = modeOf(); $("label").placeholder = name + " · " + m; }));
  $("params").addEventListener("input", deployWarn);
  $("controls").addEventListener("click", e => {
    const b = e.target.closest && e.target.closest("[data-dep]");
    if (!b) return;
    if (b.dataset.dep === "raise") { $("l_max_lots").value = b.dataset.n; DEP.allow = false; }
    deployWarn();
  });
  $("controls").addEventListener("change", e => { if (e.target.id === "d_allow") { DEP.allow = e.target.checked; deployWarn(); } });
  deployWarn();
  box.scrollIntoView({behavior: "smooth"});
}
function deployLargest() {      // the largest number of lots the strategy's settings can order (its *lots settings)
  const st = stratOf($("strat").value) || {params: []};
  let best = {n: 1, key: ""};
  st.params.filter(p => /lots$/.test(p.key) && !p.key.startsWith("risk.") && p.type !== "bool").forEach(p => {
    const el = $("p_" + p.key), v = el ? Number(el.value) : NaN;
    if (v > best.n) best = {n: v, key: p.key, label: p.label};
  });
  return best;
}
function deployWarn() {
  if (!DEP || !$("deployWarn")) return;
  const m = modeOf(), live = m === "live", st = stratOf($("strat").value) || {}, src = DEP.src, srcS = src.session || {};
  const val = k => { const el = $("p_" + k); return el ? (el.type === "checkbox" ? el.checked : el.value) : undefined; };
  const big = deployLargest(), maxLots = Number($("l_max_lots").value) || 1, items = [];
  if (big.n > maxLots) {
    items.push(["bad", `<b>Orders larger than max lots.</b> ${esc(big.key)} is ${big.n} lots; max lots per order is ${maxLots}. ${DEP.allow
      ? "Larger orders are allowed for this session: each one is placed with a warning and an alert."
      : "Such an order would be refused and the session would stop."}
      <div class="drow"><button type="button" class="btn sm" data-dep="raise" data-n="${big.n}">Raise max lots to ${big.n}</button>
      <label class="inl"><input type="checkbox" id="d_allow" ${DEP.allow ? "checked" : ""}> Keep ${maxLots}, allow larger orders anyway (warn and alert each time)</label></div>`]);
  } else items.push(["ok", `Max lots ${maxLots} covers the largest lots setting${big.key ? ` (${esc(big.key)} ${big.n})` : ""}.`]);
  const ot = val("risk.order_type") || "limit_chase", chase = val("risk.chase_seconds"), pct = val("risk.max_chase_pct");
  items.push(["warn", ot === "market"
    ? `<b>Order type: market.</b> Each order is one limit ${pct || 5}% through the market: fills can be worse than the bid / ask the ${esc(DEP.srcMode)} run used.`
    : `<b>Order type: limit and chase.</b> The ${esc(DEP.srcMode)} run fills at the second's bid / ask. ${live ? "LIVE" : "Shadow"} sends a limit and moves it for up to ${chase || 30} s / ${pct || 5}%: fills can be later and dearer.`]);
  if (live) {
    if (!st.live_ready) items.push(["bad", "<b>LIVE is off for this strategy</b> (not enabled for live orders). Deploy it to Shadow."]);
    const pm = Number(val("risk.protect_mult") || 3);
    if (pm > 0) items.push(["warn", `<b>Protective stops.</b> For every option sold, a stop is left at Zerodha at ${pm}× the sold price. ${DEP.srcMode === "shadow" ? "Shadow only logged them; here they are real orders that can fill." : "Paper had none."}`]);
    if (!(Number($("l_daily_loss_limit").value) > 0)) items.push(["bad", "<b>Daily loss limit needed.</b> LIVE needs one: it squares off and stops at that loss."]);
    const days = (srcS.calendar || []).length;
    if (DEP.srcMode === "paper") items.push(["warn", "<b>Not run in Shadow.</b> Shadow runs every live check and builds the exact orders without sending them: run it first to see what LIVE would refuse."]);
    else if (days < 10) items.push(["warn", `<b>Short shadow history.</b> ${days} trading day${days === 1 ? "" : "s"} with trades in shadow; the stage gate asks for 10.`]);
  }
  if ((src.desired || {}).feed === "sim" || srcS.feed === "sim") items.push(["warn", "<b>The source ran on simulated prices.</b> Its results say little about Zerodha prices."]);
  $("deployWarn").innerHTML = items.map(([k, h]) => `<div class="dw ${k}">${h}</div>`).join("") +
    '<div class="desc">These warn; they do not block. You decide.</div>';
}

async function start() {
  const st = stratOf($("strat").value), params = {};
  for (const p of st.params) { const el = $("p_" + p.key); params[p.key] = p.type === "bool" ? el.checked : el.value; }
  $("formErr").textContent = "";
  try {
    busy = true; $("startBtn").disabled = true;
    const mode = modeOf(), body = {strategy: st.name, feed: feedOf(), params, mode, label: $("label").value, preset: presetFor(st.name)};
    if (mode !== "paper") {
      body.live = {max_lots: $("l_max_lots").value, daily_loss_limit: $("l_daily_loss_limit").value,
                   max_orders_per_day: $("l_max_orders_per_day").value,    // order type, chase and protective stop: Orders & stops
                   allow_over_max_lots: !!(DEP && DEP.allow && deployLargest().n > Number($("l_max_lots").value))};
      if (DEP && !body.label) body.label = ((DEP.src.label || DEP.src.id) + " · " + mode).slice(0, 40);
      body.confirm = $("l_confirm").value;
    }
    const j = await post("/api/paper/start", body);
    location.href = "/" + KIND + "#" + encodeURIComponent(j.id);
    return;
  } catch (e) { $("formErr").textContent = e.message; } finally { busy = false; if ($("startBtn")) $("startBtn").disabled = false; }
}

async function act(kind, id, btn) {
  if (kind === "kill" && !(Date.now() - (armed[id + "k"] || 0) < 5000)) { armed[id + "k"] = Date.now(); btn.textContent = (btn.title || "").startsWith("Stop (no") ? "Tap again to stop" : "Tap again: close all positions and stop"; return; }
  if (kind === "reset" && !(Date.now() - (armed[id] || 0) < 5000)) { armed[id] = Date.now(); btn.textContent = "Tap again to clear its account"; return; }
  if (kind === "delete" && !(Date.now() - (armed[id + "d"] || 0) < 5000)) { armed[id + "d"] = Date.now(); btn.textContent = "Tap again to delete this session"; return; }
  if (kind === "copy") { location.href = `/${KIND}/new?copy=${encodeURIComponent(id)}`; return; }
  if (kind === "deploy") { location.href = `/live/new?copy=${encodeURIComponent(id)}&deploy=1`; return; }
  delete armed[id]; delete armed[id + "d"];
  btn.disabled = true;
  try {
    if (kind === "resume") {
      const x = last.sessions.find(s => s.id === id), s = x.session;
      if ((s.mode || "paper") === "live") { alert("To restart live trading, start it again from the form below (it asks you to confirm)."); btn.disabled = false; return; }
      await post("/api/paper/start", {id, strategy: s.strategy, feed: s.feed, params: s.params, mode: s.mode || "paper", live: s.live, label: x.label, risk: s.risk || (x.desired || {}).risk, preset: (x.desired || {}).preset || s.preset});
    } else await post("/api/paper/" + kind, {id});
  } catch (e) { alert(e.message); }
  setTimeout(() => load(true), kind === "stop" ? 2500 : 300);
}

// ---- one card per session ------------------------------------------------------
function sessionCard(x) {
  const s = x.session, st = stratOf(x.strategy) || {title: x.strategy, params: []};
  const d = x.desired || {}, params = d.params || (s && s.params) || {}, feed = d.feed || (s && s.feed);
  const state = x.running ? ["live", ""] : x.desired ? ["wait", "Waiting to start" + (x.waiting ? ": " + x.waiting : "")] : ["", "Stopped"];
  // settings changed from the strategy's defaults, as small chips
  const shown = st.params.filter(p => params[p.key] !== undefined && params[p.key] != p.default)
    .map(p => `<span class="chip2" title="${esc(p.label)}">${esc(p.label.split(/[,(]/)[0].replace(/ \(.*$/, "").slice(0, 22))} <b>${esc(params[p.key])}</b></span>`).join("");
  const mode = (d.mode || (s && s.mode) || "paper");
  const now = Date.now(), armedK = now - (armed[x.id + "k"] || 0) < 5000, armedR = now - (armed[x.id] || 0) < 5000, armedD = now - (armed[x.id + "d"] || 0) < 5000;
  const buttons = isOn(x)
    ? (x.killing ? `<span class="mt">${s && (s.positions || []).length ? "Squaring off at the next market update…" : "Stopping…"}</span>`
       : (s && !(s.positions || []).length
          ? ibtn("kill", "stop", "Stop (no open positions)", mode === "paper" ? "" : "danger", armedK ? "Tap again to stop" : "")
          : ibtn("kill", "squareoff", "Close all positions and stop", mode === "paper" ? "" : "danger", armedK ? "Tap again to square off" : ""))
         + (mode === "paper" ? ibtn("stop", "stop", "Stop (keep positions)", "danger") : ""))
    : (s ? ibtn("resume", "play", "Resume", "pri") + ibtn("reset", "reset", "Reset account", "", armedR ? "Tap again to clear" : "") : "")
      + ibtn("delete", "trash", "Delete session", "", armedD ? "Tap again to delete" : "");
  const copyBtn = ibtn("copy", "copy", "Copy to a new session")
    + (mode === "paper" || mode === "shadow" ? `<button type="button" class="ibtn pri dep" data-act="deploy" title="Deploy to Shadow or LIVE with the same settings">${icon("deploy", "Deploy")}<span>Deploy</span></button>` : "");
  const badge = LIVEPAGE || mode !== "paper" ? `<span class="badge ${mode}">${mode.toUpperCase()}</span>` : "";
  const lim = (d.live || (s && s.live)) || null;
  const limits = lim ? `<span class="chip2" title="Live limits">${icon("shield", "Live limits")} ${lim.max_lots} lot${lim.allow_over_max_lots ? " (larger allowed)" : ""} · ${lakh(lim.daily_loss_limit)}/day${lim.protect_mult > 0 ? " · " + lim.protect_mult + "×" : ""}</span>` : "";
  const margin = MG().sessions[x.id] ? `<span class="chip2" title="Margin for its open legs">${icon("wallet", "Margin for its open legs")} ${lakh(MG().sessions[x.id])}</span>` : "";
  const who = x.started_by ? `<span class="chip2" title="Started by ${esc(x.started_by)}">${icon("user", "Started by")} ${esc(x.started_by)}</span>` : "";
  const stoppedNote = x.stopped_because ? `<div class="stopped">Stopped by itself: ${esc(x.stopped_because)}</div>` : "";
  let body = '<div class="empty">No updates yet.</div>';
  if (s) {
    const t = new Date(s.time), day = dayPnl(x);
    const num = (lbl, v, cls, sub, tk) => `<div class="kn"><div class="lbl">${lbl}</div><div class="big ${cls}" data-tk="${tk}">${v}</div>${sub ? `<div class="ks">${sub}</div>` : ""}</div>`;
    body = `<div class="knums">
        ${num('Total <small title="after all Zerodha charges">net</small>', ainr(s.total), sign(s.total), `booked ${inr(s.booked)} · charges ${inr(s.charges)}`, "total")}
        ${num("Today", day == null ? "–" : ainr(day), sign(day || 0), "", "today")}
        ${num("Unrealized", s.positions.length ? ainr(s.open) : "–", sign(s.open), s.positions.length ? pips(s.positions.length) : "flat", "open")}
      </div>
      <div class="ixline">${esc(LIVEPAGE ? "" : "")}${(s.positions[0] && s.positions[0].Contract.split(" ")[0]) || "NIFTY"} <b data-tk="spot">${Number(s.spot).toLocaleString("en-IN", {maximumFractionDigits: 2})}</b>${s.vix ? ` · VIX <b data-tk="vix">${Number(s.vix).toFixed(2)}</b>` : ""}${x.running ? ` <span class="tkage" data-tk="age"></span>` : " · " + t.toLocaleString("en-IN", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false})}
        <span class="secbar"><button class="ibtn sm" type="button" data-all="1" title="Expand all" aria-label="Expand all">${icon("expand", "Expand all")}</button><button class="ibtn sm" type="button" data-all="0" title="Collapse all" aria-label="Collapse all">${icon("collapse", "Collapse all")}</button></span></div>
      ${sec(x.id, "positions", `Positions ${pips(s.positions.length)}`, (s.positions.length ? `<div class="plist" data-tk="plist">${s.positions.map(posRow).join("")}</div>` : '<div class="empty">Flat: no open positions.</div>')
            + ((s.closed_legs || []).length ? `<div class="sub closedhd">Closed in this cycle · booked <span class="${sign(s.closed_legs.reduce((a, l) => a + l.Net, 0))}">${ainr(s.closed_legs.reduce((a, l) => a + l.Net, 0))}</span> after charges</div><div class="plist closed">${s.closed_legs.map(closedRow).join("")}</div>` : ""),
            true, s.positions.length ? `<span class="${sign(s.open)}" data-tk="open2">${ainr(s.open)}</span>` : "")}
      ${x.payoff ? sec(x.id, "payoff", "Payoff", payoffBlock(x), true, `now <span class="${sign(x.payoff.now)}">${ainr(x.payoff.now)}</span>`) : ""}
      ${(() => { const b = pnlParts(x); return sec(x.id, "pnl", "P&amp;L", b.chart, true, b.note)
          + (b.metrics ? sec(x.id, "metrics", "Key metrics", b.metrics, false, b.metricsNote) : "")
          + (b.daily ? sec(x.id, "dailyp", "Daily P&amp;L", b.daily, false, b.dailyNote) : ""); })()}
      ${sec(x.id, "calendar", "Calendar", calBlock(x.id, s.calendar), true, (s.calendar || []).length ? `${s.calendar.length} days` : "")}
      ${sec(x.id, "cycles", `Closed cycles <span class="cnt">${s.cycles.length}</span>`, s.cycles.length ? `<div class="plist">${s.cycles.map(c => cycRow(c, x.id)).join("")}</div>` : '<div class="empty">No closed cycles yet.</div>', false,
            s.cycles.length ? `<span class="${sign(s.booked)}">${ainr(s.booked)}</span>` : "")}
      ${sec(x.id, "log", "Activity", `<pre class="log">${esc(s.log.join("\n") || "No activity yet.")}</pre>`, false)}`;
  }
  return `<div class="card sess" data-id="${esc(x.id)}">
    <div class="sess-head"><h2><span class="dot ${state[0]}" title="${esc(state[1] || "Running")}"></span> ${esc(st.title.split(" (")[0])}${x.label ? " · " + esc(x.label) : ""}${badge}${feed === "sim" ? " " + icon("sim", FEEDS.sim) : ""}</h2>
      <div class="acts">${buttons}${copyBtn}</div></div>
    ${feed === "sim" ? `<div class="meta"><span class="chip2 warn" title="Made-up prices replayed fast (a trading day in about 40 seconds): checks that the strategy runs, not how it does. Not counted as paper trading in Algo Studio; stops by itself after a replayed year.">Simulated prices · fast replay${s && s.time ? " · replay at " + esc(new Date(s.time).toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "numeric"})) : ""}</span></div>` : ""}
    ${state[1] || shown || limits || margin || who ? `<div class="meta">${state[1] ? `<span class="chip2 warn">${esc(state[1])}</span>` : ""}${limits}${margin}${shown}${who}</div>` : ""}
    ${stoppedNote}${body}</div>`;
}

// an open leg in two lines: strike and type (link to its chart), side and expiry; P&L, entry price to now
function posRow(p) {
  const m = /^(\S+) (\d{2}) (\w{3}) (\d{2}) (\S+) (CE|PE)$/.exec(p.Contract || "");
  const short = m ? `${Number(m[5]).toLocaleString("en-IN")} ${m[6]}` : esc(p.Contract), exp = m ? `${Number(m[2])} ${m[3]}` : "";
  const sold = p.Qty < 0;
  return `<div class="prow"><div><a class="pc" href="${(chartLink(p.Contract).match(/href="([^"]+)"/) || [, "#"])[1]}">${short}</a>
      <div class="ps">${icon(sold ? "sold" : "bought", sold ? "Sold" : "Bought")}${Math.abs(p.Qty)} · ${exp}</div></div>
    <div class="pr"><div class="pv ${sign(p["P&L"])}">${ainr(p["P&L"])}</div><div class="ps">${p["Avg price"].toFixed(2)} → ${p.LTP.toFixed(2)}</div></div></div>`;
}
// a leg closed in the open cycle: quantity 0 (was sold/bought N), entry -> exit, what it booked before charges
function closedRow(p) {
  const m = /^(\S+) (\d{2}) (\w{3}) (\d{2}) (\S+) (CE|PE)$/.exec(p.Contract || "");
  const short = m ? `${Number(m[5]).toLocaleString("en-IN")} ${m[6]}` : esc(p.Contract), exp = m ? `${Number(m[2])} ${m[3]}` : "";
  return `<div class="prow"><div><a class="pc" href="${(chartLink(p.Contract).match(/href="([^"]+)"/) || [, "#"])[1]}">${short}</a>
      <div class="ps">0 · was ${p.Was < 0 ? "sold" : "bought"} ${Math.abs(p.Was)} · ${exp} · ${esc(p.Reason)} ${esc(TZ.hm(p.Closed))}</div></div>
    <div class="pr"><div class="pv ${sign(p["P&L"])}">${ainr(p["P&L"])}</div><div class="ps">${Number(p["Entry price"]).toFixed(2)} → ${Number(p["Exit price"]).toFixed(2)}</div></div></div>`;
}
// a closed cycle in two lines (when and why it closed; net P&L and legs); tap to see the positions it closed
const cycLegs = {}, cycAsked = {};
function cycRow(c, sid) {
  const a = whenShort(c.entry.replace("T", " ")), b = whenShort(c.exit.replace("T", " ")), legs = cycLegs[sid + "|" + c.cycle];
  return `<details class="cyc" data-k="cyc:${esc(c.cycle)}" data-cycle="${esc(c.cycle)}"><summary class="prow"><div><div class="pc">${esc(b)}</div><div class="ps">from ${esc(a)} · ${esc(c.reason)}</div></div>
    <div class="pr"><div class="pv ${sign(c.net)}">${ainr(c.net)}</div><div class="ps">${c.legs} legs · charges ${inr(c.charges)}</div></div></summary>
    <div class="legs">${legs ? legsTable(legs) : '<div class="empty">Loading the positions…</div>'}</div></details>`;
}
function legsTable(legs) {
  if (!legs.length) return '<div class="empty">No positions stored for this cycle.</div>';
  const px = v => v == null ? "–" : Number(v).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return `<div class="scroll"><table class="lt"><tr><th>Position</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Net</th></tr>${legs.map(l =>
    `<tr><td>${chartLink(l.contract)}<div class="mt">${esc(l.reason || "")}</div></td><td>${l.side === "SHORT" ? "−" : "+"}${l.qty}</td>
      <td>${px(l.entry_price)}<div class="mt">${esc(mtime(String(l.entry_time).replace("T", " ").slice(0, 16)))}</div></td>
      <td>${px(l.exit_price)}<div class="mt">${esc(mtime(String(l.exit_time).replace("T", " ").slice(0, 16)))}</div></td>
      <td class="${sign(l.net_pnl)}">${ainr(l.net_pnl)}<div class="mt">charges ${inr(l.costs)}</div></td></tr>`).join("")}</table></div>`;
}
async function loadLegs(sid, cycle) {
  const k = sid + "|" + cycle;
  if (cycLegs[k] || cycAsked[k]) return;
  cycAsked[k] = true;
  try {
    const r = await fetch(`/api/paper/cycle?id=${encodeURIComponent(sid)}&cycle=${encodeURIComponent(cycle)}`, {cache: "no-store"});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    cycLegs[k] = j.legs || [];
  } catch (e) { delete cycAsked[k]; cycLegs[k] = null; }
  document.querySelectorAll(`#detail .sess[data-id="${CSS.escape(sid)}"] details.cyc[data-cycle="${CSS.escape(cycle)}"] .legs`).forEach(el => {
    el.innerHTML = cycLegs[k] ? legsTable(cycLegs[k]) : '<div class="empty">Could not load the positions. Close and open again to retry.</div>'; });
}

// a contract label ("NIFTY 20 Oct 26 25000 CE") as a link to its live chart
const MON = {Jan: "01", Feb: "02", Mar: "03", Apr: "04", May: "05", Jun: "06", Jul: "07", Aug: "08", Sep: "09", Oct: "10", Nov: "11", Dec: "12"};
function chartLink(label) {
  const m = /^(\S+) (\d{2}) (\w{3}) (\d{2}) (\d+(?:\.\d+)?) (CE|PE)$/.exec(label || "");
  if (!m || !MON[m[3]]) return esc(label);
  const q = `u=${encodeURIComponent(m[1])}&e=20${m[4]}-${MON[m[3]]}-${m[2]}&k=${m[5]}&r=${m[6]}`;
  return `<a href="/chart?${q}" title="Live chart">${esc(label)}</a>`;
}

// ---- payoff of the open legs: at the first expiry, today, and a what-if (days ahead, volatility) ----------
const pay = {};   // slider settings per session, kept across refreshes
const nf = v => Number(v).toLocaleString("en-IN", {maximumFractionDigits: 0});
function ncdf(x) { const t = 1 / (1 + 0.2316419 * Math.abs(x)), d = 0.3989422804014327 * Math.exp(-x * x / 2);
  const p = d * t * (0.31938153 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  return x >= 0 ? 1 - p : p; }
function bsJ(S, K, t, v, right) {
  if (t <= 0 || v <= 0) return right === "CE" ? Math.max(S - K, 0) : Math.max(K - S, 0);
  const r = 0.065, q = 0.012, vt = v * Math.sqrt(t), d1 = (Math.log(S / K) + (r - q + v * v / 2) * t) / vt, d2 = d1 - vt;
  return right === "CE" ? S * Math.exp(-q * t) * ncdf(d1) - K * Math.exp(-r * t) * ncdf(d2)
                        : K * Math.exp(-r * t) * ncdf(-d2) - S * Math.exp(-q * t) * ncdf(-d1);
}
function whatIf(p, days, ivPts) {
  return p.x.map(S => p.legs.reduce((a, l) => a + (bsJ(S, l.strike, Math.max(l.t - days / 252, 0), Math.max(l.iv + ivPts / 100, 0.01), l.right) - l.avg) * l.qty, -p.charges));
}
function payoffBlock(x) {
  const p = x.payoff;
  if (!p) return "";
  const st = pay[x.id] || (pay[x.id] = {days: 0, iv: 0});
  const maxD = Math.max(0, Math.floor(p.days_to_first * 4) / 4);
  if (st.days > maxD) st.days = maxD;
  const tile = (label, value, sub, cls) => `<div class="tile"><div class="label">${label}</div><div class="big ${cls || ""}" style="font-size:16px">${value}</div><div class="sub">${sub || ""}</div></div>`;
  const legs = p.legs.map(l => `<tr><td>${esc(l.label)}</td><td>${l.qty}</td><td>${(l.iv * 100).toFixed(1)}%${l.iv_from === "VIX" ? " <span class='mt'>(VIX)</span>" : ""}</td></tr>`).join("");
  return `<div class="payoff" data-pay="${esc(x.id)}">
    <div class="row" style="justify-content:flex-end;margin-top:0">
      <span class="sub" style="font-size:12px">exp ${dayShort(p.first_expiry)} · ${p.days_to_first.toFixed(1)} trading days left</span></div>
    <div class="chartbox"><canvas data-payoff></canvas></div>
    <div class="row" style="gap:14px;flex-wrap:wrap;margin-top:8px">
      <label style="flex:1;min-width:200px">Days ahead: <b data-dv>${st.days}</b><input type="range" data-days min="0" max="${maxD}" step="0.25" value="${st.days}" style="width:100%" ${maxD ? "" : "disabled"}></label>
      <label style="flex:1;min-width:200px">Volatility change: <b data-iv>${st.iv > 0 ? "+" : ""}${st.iv} pts</b><input type="range" data-ivs min="-10" max="10" step="0.5" value="${st.iv}" style="width:100%"></label>
      <button class="btn ghost" data-payreset>Back to now</button></div>
    <div class="knums">
      <div class="kn"><div class="lbl">Now</div><div class="big ${sign(p.now)}">${ainr(p.now)}</div><div class="ks">at today's prices</div></div>
      <div class="kn"><div class="lbl">Max ▲</div><div class="big up">${inr(p.max_profit)}</div><div class="ks">at expiry</div></div>
      <div class="kn"><div class="lbl">Max ▼</div><div class="big down">${ainr(p.max_loss)}${p.loss_open_below || p.loss_open_above ? "+" : ""}</div>
        <div class="ks">${p.loss_open_below || p.loss_open_above ? "no floor " + (p.loss_open_below && p.loss_open_above ? "↕" : p.loss_open_below ? "↓" : "↑") : "at expiry"}</div></div>
    </div>
    <div class="ixline">${p.breakevens.length ? `BE <b>${p.breakevens.map(nf).join(" · ")}</b> · ` : ""}<span title="₹ per 1-point index move">Δ <b class="${sign(p.delta)}">${Math.abs(p.delta)}</b></span> ·
      <span title="one trading day, same index">Θ <b class="${sign(p.theta)}">${ainr(p.theta)}</b>/day</span> · <span title="per +1 volatility point">ν <b class="${sign(p.vega)}">${ainr(p.vega)}</b>/pt</span></div>
    <details data-k="paylegs"><summary>Volatility used per leg</summary><div class="scroll"><table><tr><th>Contract</th><th>Qty</th><th>Implied vol.</th></tr>${legs}</table></div></details>
  </div>`;
}

function payChart(canvas, p, compact) {
  const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
  const pts = ys => p.x.map((x, i) => ({x, y: ys[i]}));
  const ds = [
    {label: "At expiry " + dayShort(p.first_expiry), data: pts(p.expiry), borderColor: v("--muted"), borderWidth: 1.6, pointRadius: 0, tension: 0},
    {label: "Today", data: pts(p.today), borderColor: v("--accent"), borderWidth: 1.8, pointRadius: 0, tension: 0},
    {label: "What-if", data: [], borderColor: "#f59e0b", borderWidth: 1.8, borderDash: [5, 4], pointRadius: 0, tension: 0, hidden: true},
    {label: "Index now", data: [{x: p.spot, y: p.now}], type: "scatter", pointRadius: compact ? 4 : 5, pointBackgroundColor: v("--accent"), borderColor: v("--accent")},
    {label: "_profit", data: pts(p.expiry.map(y => Math.max(y, 0))), borderWidth: 0, pointRadius: 0, tension: 0, fill: "origin", backgroundColor: "rgba(22,163,74,.12)"},
    {label: "_loss", data: pts(p.expiry.map(y => Math.min(y, 0))), borderWidth: 0, pointRadius: 0, tension: 0, fill: "origin", backgroundColor: "rgba(220,38,38,.12)"}];
  const spotLine = {id: "spotLine", afterDatasetsDraw(c) { const xs = c.scales.x, a = c.chartArea, X = xs.getPixelForValue(p.spot), g = c.ctx;
    g.save(); g.strokeStyle = v("--accent"); g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X, a.top); g.lineTo(X, a.bottom); g.stroke(); g.restore(); }};
  return new Chart(canvas, {type: "line", data: {datasets: ds}, plugins: [spotLine],
    options: {responsive: true, maintainAspectRatio: false, animation: false, parsing: false, interaction: {mode: "nearest", axis: "x", intersect: false},
      plugins: {legend: {display: !compact, labels: {color: v("--muted"), boxWidth: 12, filter: (i, d) => i.text !== "Index now" && !i.text.startsWith("_") && !(d.datasets[i.datasetIndex] || {}).hidden}},
        tooltip: {filter: c => !c.dataset.label.startsWith("_"), callbacks: {title: c => p.underlying + " " + nf(c[0].parsed.x), label: c => `${c.dataset.label}: ${inr(c.parsed.y)}`}}},
      scales: {x: {type: "linear", min: p.x[0], max: p.x[p.x.length - 1], ticks: {maxTicksLimit: compact ? 4 : 7, color: v("--muted"), callback: n => nf(n)}, grid: {display: false}},
               y: {ticks: {maxTicksLimit: compact ? 5 : 8, callback: n => inr(n), color: v("--muted")}, grid: {color: c => c.tick.value === 0 ? v("--muted") : v("--line")}}}}});
}

function drawPayoff(id, box, p) {
  if (!box || !p || !window.Chart) return;
  const st = pay[id], pts = ys => p.x.map((x, i) => ({x, y: ys[i]}));
  charts[id + "-pay"] = payChart(box.querySelector("canvas[data-payoff]"), p, false);
  const update = () => {
    const ch = charts[id + "-pay"]; if (!ch) return;
    box.querySelector("[data-dv]").textContent = st.days;
    box.querySelector("[data-iv]").textContent = (st.iv > 0 ? "+" : "") + st.iv + " pts";
    const on = st.days > 0 || st.iv !== 0;
    ch.data.datasets[2].hidden = !on;
    ch.data.datasets[2].data = on ? pts(whatIf(p, st.days, st.iv)) : [];
    ch.data.datasets[2].label = `What-if: +${st.days} days, ${st.iv >= 0 ? "+" : ""}${st.iv} vol`;
    ch.update("none");
  };
  box.querySelector("[data-days]").oninput = e => { st.days = +e.target.value; update(); };
  box.querySelector("[data-ivs]").oninput = e => { st.iv = +e.target.value; update(); };
  box.querySelector("[data-payreset]").onclick = () => { st.days = 0; st.iv = 0; box.querySelector("[data-days]").value = 0; box.querySelector("[data-ivs]").value = 0; update(); };
  update();
}

// every session with open legs, one small payoff chart each; tap one to open it below
function renderPayoffs() {
  const ss = shown().filter(x => x.payoff), grid = $("payGrid");
  if (!ss.length) { grid.innerHTML = '<div class="empty">No session has open positions right now.</div>'; return; }
  Object.keys(charts).filter(k => k.startsWith("mini-")).forEach(k => { charts[k].destroy(); delete charts[k]; });
  const nameOf = shortName;
  grid.innerHTML = ss.map(x => { const p = x.payoff, open = p.loss_open_below || p.loss_open_above;
    return `<div class="paymini ${x.id === selected ? "sel" : ""}" data-id="${esc(x.id)}">
      <div class="row" style="justify-content:space-between;margin:0"><b>${esc(nameOf(x))}</b><span class="${sign(p.now)}"><b>${ainr(p.now)}</b></span></div>
      <div class="mt" style="display:flex;align-items:center;gap:6px">${pips(p.legs.length)}<span>${esc(p.underlying)} ${nf(p.spot)} · exp ${dayShort(p.first_expiry)}</span></div>
      <div class="minibox"><canvas></canvas></div>
      <div class="mt">Max <span class="up">▲ ${ainr(p.max_profit)}</span> <span class="down">▼ ${ainr(p.max_loss)}${open ? "+" : ""}</span>${p.breakevens.length ? " · BE " + p.breakevens.map(nf).join(" / ") : ""}</div></div>`; }).join("");
  grid.querySelectorAll(".paymini").forEach(el => {
    const x = ss.find(s => s.id === el.dataset.id);
    if (window.Chart) charts["mini-" + x.id] = payChart(el.querySelector("canvas"), x.payoff, true);
    el.onclick = () => { open[x.id + "payoff"] = true; saveOpen(); location.href = `/${KIND}#${encodeURIComponent(x.id)}`; };
  });
}

// ---- calendar of closed cycles: each day's booked P&L (after charges), by the day the cycle closed ----------
const calMonth = {}, calPick = {};
const ymOf = d => d.slice(0, 7);
const kfmt = v => { const a = Math.abs(v), s = v < 0 ? "−" : "+";
  return a >= 1e7 ? s + (a / 1e7).toFixed(2) + "Cr" : a >= 1e5 ? s + (a / 1e5).toFixed(2) + "L" : a >= 1e3 ? s + (a / 1e3).toFixed(1) + "k" : s + Math.round(a); };
function calBlock(id, cal) {
  if (!cal || !cal.length) return `<div class="empty">No closed cycles yet.</div>`;
  return `<div class="cal" data-cal="${esc(id)}"></div>`;
}
function calHTML(id, cal, title) {
  const months = [...new Set(cal.map(x => ymOf(x.date)))].sort();
  let ym = calMonth[id];
  if (!ym || !months.includes(ym)) ym = calMonth[id] = months[months.length - 1];
  const byDay = Object.fromEntries(cal.map(x => [x.date, x]));
  const [Y, M] = ym.split("-").map(Number), first = new Date(Date.UTC(Y, M - 1, 1)), dim = new Date(Date.UTC(Y, M, 0)).getUTCDate();
  const lead = (first.getUTCDay() + 6) % 7;   // weeks start on Monday
  const inMonth = cal.filter(x => ymOf(x.date) === ym), tot = inMonth.reduce((a, x) => a + x.net, 0);
  const mx = Math.max(1, ...inMonth.map(x => Math.abs(x.net)));
  const up = getComputedStyle(document.documentElement).getPropertyValue("--up").trim(), dn = getComputedStyle(document.documentElement).getPropertyValue("--down").trim();
  const tint = v => { const a = Math.round(18 + 52 * Math.min(1, Math.abs(v) / mx)).toString(16).padStart(2, "0"); return (v >= 0 ? up : dn) + a; };
  let cells = "";
  for (let i = 0; i < lead; i++) cells += `<div class="d x"></div>`;
  for (let d = 1; d <= dim; d++) {
    const key = `${ym}-${String(d).padStart(2, "0")}`, x = byDay[key];
    cells += x ? `<div class="d has ${calPick[id] === key ? "pick" : ""}" data-day="${key}" style="background:${tint(x.net)}"><div class="n">${d}</div><div class="v ${sign(x.net)}">${kfmt(Math.abs(x.net)).replace(/^\+/, "")}</div><div class="k">${x.cycles.length} cycle${x.cycles.length > 1 ? "s" : ""}</div></div>`
               : `<div class="d"><div class="n">${d}</div></div>`;
  }
  const i = months.indexOf(ym), wins = inMonth.filter(x => x.net > 0).length, losses = inMonth.filter(x => x.net < 0).length;
  const mName = first.toLocaleDateString("en-IN", {month: "long", year: "numeric", timeZone: "UTC"});
  const pick = calPick[id] && byDay[calPick[id]] && ymOf(calPick[id]) === ym ? byDay[calPick[id]] : null;
  const detail = pick ? `<div class="scroll" style="margin-top:8px"><table><tr><th>${dayShort(pick.date)}</th><th>Opened</th><th>Closed</th><th>Legs</th><th>Exit</th><th>Net P&amp;L</th></tr>` +
      pick.cycles.map(c => `<tr><td>${esc(c.session ? c.session + " · " : "")}${esc(c.cycle)}</td><td>${esc(mtime(c.opened))}</td><td>${esc(TZ.hm(c.closed))}</td><td>${c.legs}</td><td>${esc(c.reason)}</td><td class="${sign(c.net)}">${ainr(c.net)}</td></tr>`).join("") + "</table></div>" : "";
  return `<div class="row" style="justify-content:space-between;margin-top:14px;flex-wrap:wrap;gap:8px">
      <h3 style="margin:0">${title}</h3>
      <div class="nav"><button data-m="-1" ${i <= 0 ? "disabled" : ""} aria-label="Previous month">‹</button><b>${mName}</b><button data-m="1" ${i >= months.length - 1 ? "disabled" : ""} aria-label="Next month">›</button></div></div>
    <div class="sub" style="font-size:12px;margin-top:4px">Booked <b class="${sign(tot)}">${ainr(tot)}</b> net · days <span class="up">▲ ${wins}</span> <span class="down">▼ ${losses}</span> · ${inMonth.reduce((a, x) => a + x.cycles.length, 0)} cycles</div>
    <div class="grid">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map(h => `<div class="hd">${h}</div>`).join("")}${cells}</div>${detail}`;
}
function wireCal(box, id, cal, title) {
  if (!box || !cal || !cal.length) return;
  const draw = () => {
    box.innerHTML = calHTML(id, cal, title === undefined ? "Calendar of closed cycles" : title);
    const months = [...new Set(cal.map(x => ymOf(x.date)))].sort();
    box.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { const j = months.indexOf(calMonth[id]) + Number(b.dataset.m);
      if (months[j]) { calMonth[id] = months[j]; calPick[id] = ""; draw(); } });
    box.querySelectorAll("[data-day]").forEach(c => c.onclick = () => { calPick[id] = calPick[id] === c.dataset.day ? "" : c.dataset.day; draw(); });
  };
  draw();
}
function renderCalAll() {
  const box = $("calAll"), ss = shown().filter(x => x.session && x.session.calendar && x.session.calendar.length);
  const nameOf = shortName;
  const days = {};
  ss.forEach(x => x.session.calendar.forEach(d => { const t = days[d.date] || (days[d.date] = {date: d.date, net: 0, cycles: []});
    t.net += d.net; d.cycles.forEach(c => t.cycles.push(Object.assign({session: nameOf(x)}, c))); }));
  const cal = Object.values(days).sort((a, b) => a.date.localeCompare(b.date));
  if (!ss.length) { box.innerHTML = '<h2 style="margin-top:0">Calendar of closed cycles</h2><div class="empty">No closed cycles yet.</div>'; return; }
  wireCal(box, "__all__", cal, "Closed cycles");
}

// ---- P&L: chart with its high and low, key metrics, P&L by day ---------------------------------
const whenShort = t => { if (!t) return ""; const d = new Date(String(t).replace(" ", "T")); return isNaN(d) ? t : d.toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}); };
const dayShort = t => { const d = new Date(String(t) + "T00:00"); return isNaN(d) ? t : d.toLocaleDateString("en-IN", {day: "numeric", month: "short"}); };
let pnlViews = {};
try { pnlViews = JSON.parse(localStorage.getItem(KIND + ".pnlview") || "{}") || {}; } catch (e) { pnlViews = {}; }
const pnlView = id => pnlViews[id] || "all";
function setPnlView(id, v) { pnlViews[id] = v; try { localStorage.setItem(KIND + ".pnlview", JSON.stringify(pnlViews)); } catch (e) {} }

function pnlMetrics(x, m, tile) {
  return `<div class="tiles">
      ${tile("Max drawdown", ainr(m.max_drawdown), m.drawdown_from ? `${whenShort(m.drawdown_from)} → ${whenShort(m.drawdown_to)}` : "", m.max_drawdown < 0 ? "down" : "")}
      ${tile("Best day", ainr(m.best_day), dayShort(m.best_day_date), sign(m.best_day))}
      ${tile("Worst day", ainr(m.worst_day), dayShort(m.worst_day_date), sign(m.worst_day))}
      ${tile("Winning days", `${m.winning_days} / ${m.days}`, `average day ${inr(m.average_day)}`)}
      ${tile("Cycles won", m.cycles ? `${m.winning_cycles} / ${m.cycles}` : "–", m.cycles ? `${m.cycle_win_rate_pct}% · avg win ${m.average_win != null ? inr(m.average_win) : "–"} · avg loss ${m.average_loss != null ? inr(m.average_loss) : "–"}` : "no closed cycles yet")}
      ${tile("Return on capital", m.return_pct != null ? Math.abs(m.return_pct) + "%" : "–", "capital " + inr(m.capital), sign(m.return_pct))}
      ${tile("Highest P&L", ainr(m.highest_pnl), whenShort(m.highest_at), sign(m.highest_pnl))}
      ${tile("Lowest P&L", ainr(m.lowest_pnl), whenShort(m.lowest_at), sign(m.lowest_pnl))}
    </div>
    <div style="margin-top:10px"><a class="btn ghost" href="/api/paper/metrics.csv?id=${encodeURIComponent(x.id)}" download>Download key metrics (CSV)</a></div>`;
}

function pnlParts(x) {
  const p = x.pnl || {}, m = p.metrics || {}, daily = p.daily || [];
  if (!m.days) return {chart: '<div class="chartbox"><canvas data-chart></canvas></div>', note: ""};
  const tile = (label, value, sub, cls) => `<div class="tile"><div class="label">${label}</div><div class="big ${cls || ""}" style="font-size:16px">${value}</div><div class="sub">${sub || ""}</div></div>`;
  const rows = daily.slice().reverse().map(d => `<tr><td>${dayShort(d.date)}</td><td class="${sign(d.day)}">${ainr(d.day)}</td><td class="${sign(d.cumulative)}">${ainr(d.cumulative)}</td>
      <td class="${sign(d.high)}">${ainr(d.high)} <span class="mt">${d.high_at}</span></td><td class="${sign(d.low)}">${ainr(d.low)} <span class="mt">${d.low_at}</span></td><td>${d.trades || "–"}</td><td>${d.charges ? inr(d.charges) : "–"}</td></tr>`).join("");
  const cy = p.cycle, view = pnlView(x.id);
  const seg = cy ? `<div class="seg pnlseg" data-pnlv="${esc(x.id)}"><button type="button" data-v="all" class="${view === "all" ? "on" : ""}">Overall</button><button type="button" data-v="cycle" class="${view === "cycle" ? "on" : ""}">${esc(cy.label)}</button></div>` : "";
  const all = {
    note: `<span class="${sign(m.highest_pnl)}">▲ ${ainr(m.highest_pnl)}</span> · <span class="${sign(m.lowest_pnl)}">▼ ${ainr(m.lowest_pnl)}</span>`,
    chart: `${seg}<div class="sub" style="font-size:12px"><span class="up">●</span> high ${dayShort(String(m.highest_at).slice(0, 10))} · <span class="down">●</span> low ${dayShort(String(m.lowest_at).slice(0, 10))}</div>
      <div class="chartbox"><canvas data-chart></canvas></div>`,
    metricsNote: `drawdown <span class="down">${ainr(m.max_drawdown)}</span>`,
    metrics: pnlMetrics(x, m, tile),
    dailyNote: `${daily.length} days`,
    daily: `<div class="chartbox"><canvas data-daily></canvas></div>
    <details data-k="daily"><summary>P&amp;L by day (${daily.length})</summary><div class="scroll"><table>
      <tr><th>Day</th><th>Day P&amp;L</th><th>Cumulative</th><th>Day high</th><th>Day low</th><th>Trades</th><th>Charges</th></tr>${rows}</table></div></details>`,
  };
  if (!(cy && view === "cycle")) return all;
  const now = cy.points.length ? cy.points[cy.points.length - 1][1] : 0;
  return {...all,                                // the current (or last) cycle alone, measured from its first entry
    note: `${esc(cy.label.toLowerCase())} <span class="${sign(now)}">${ainr(now)}</span>`,
    chart: `${seg}<div class="sub" style="font-size:12px">from ${whenShort(cy.from)}${cy.to ? " to " + whenShort(cy.to) : ""} · <span class="up">●</span> high ${ainr(cy.hi[1])} · <span class="down">●</span> low ${ainr(cy.lo[1])}</div>
      <div class="chartbox"><canvas data-chart></canvas></div>`};
}

function drawDaily(id, canvas, daily) {
  if (!canvas || !window.Chart || !daily || !daily.length) return;
  const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
  charts[id] = new Chart(canvas, {data: {labels: daily.map(d => dayShort(d.date)), datasets: [
      {type: "bar", label: "Day P&L", data: daily.map(d => d.day), backgroundColor: daily.map(d => d.day >= 0 ? v("--up") : v("--down")), yAxisID: "y", order: 2},
      {type: "line", label: "Cumulative", data: daily.map(d => d.cumulative), borderColor: v("--accent"), backgroundColor: v("--accent"), borderWidth: 1.8, pointRadius: daily.length > 40 ? 0 : 2, tension: 0, yAxisID: "y", order: 1}]},
    options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: "index", intersect: false},
      plugins: {legend: {labels: {color: v("--muted"), boxWidth: 12}}, tooltip: {callbacks: {label: c => `${c.dataset.label}: ${inr(c.parsed.y)}`}}},
      scales: {x: {ticks: {maxTicksLimit: 8, maxRotation: 0, color: v("--muted")}, grid: {display: false}},
               y: {ticks: {callback: n => inr(n), color: v("--muted")}, grid: {color: c => c.tick.value === 0 ? v("--muted") : v("--line")}}}}});
}

function drawChart(id, canvas, points, pnl) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  if (!canvas || !window.Chart || points.length < 2) return;
  const css = getComputedStyle(document.documentElement);
  const hi = pnl && pnl.hi, lo = pnl && pnl.lo;
  const mark = (pt) => points.map(p => pt && p[0] === pt[0] && p[1] === pt[1] ? p[1] : null);
  const ds = [{data: points.map(p => p[1]), borderColor: css.getPropertyValue("--accent").trim(), borderWidth: 1.6, pointRadius: 0, tension: 0, label: "P&L"}];
  if (hi) ds.push({data: mark(hi), label: "Highest", pointRadius: 5, pointHoverRadius: 6, showLine: false, pointBackgroundColor: css.getPropertyValue("--up").trim(), borderColor: css.getPropertyValue("--up").trim()});
  if (lo) ds.push({data: mark(lo), label: "Lowest", pointRadius: 5, pointHoverRadius: 6, showLine: false, pointBackgroundColor: css.getPropertyValue("--down").trim(), borderColor: css.getPropertyValue("--down").trim()});
  charts[id] = new Chart(canvas, {type: "line",
    data: {labels: points.map(p => whenShort(p[0])), datasets: ds},
    options: {responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {legend: {display: false}, tooltip: {filter: c => c.parsed.y != null, callbacks: {label: c => (c.datasetIndex ? c.dataset.label + " " : "") + inr(c.parsed.y)}}},
      scales: {x: {ticks: {maxTicksLimit: innerWidth < 600 ? 3 : 5, maxRotation: 0, autoSkip: true, color: css.getPropertyValue("--muted").trim(),
                         callback: function (v) { const l = String(this.getLabelForValue(v)); return l.replace(/,? \d{2}:\d{2}$/, ""); }}, grid: {display: false}},
               y: {ticks: {callback: v => inr(v), color: css.getPropertyValue("--muted").trim(), maxTicksLimit: 6},
                   grid: {color: c => c.tick.value === 0 ? css.getPropertyValue("--muted").trim() : css.getPropertyValue("--line").trim()}}}}});
}

// ---- one row per session -------------------------------------------------------------------
// today's P&L of a session (IST date): the last day of its account history, if that day is today
const todayIST = () => new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);
// Today = the account now less its value at the end of the previous trading day. The day's own history is one point a
// minute, so its last point can be up to a minute old: use the live total (the same prices as Open) instead.
function dayPnl(x) {
  const d = ((x.pnl || {}).daily) || [], t = d[d.length - 1], s = x.session;
  if (!t || t.date !== todayIST()) return s ? (s.time && String(s.time).slice(0, 10) === todayIST() && s.total != null ? s.total - (t ? t.cumulative : 0) : 0) : null;
  const before = d.length > 1 ? d[d.length - 2].cumulative : 0;
  return s && s.total != null ? Math.round((s.total - before) * 100) / 100 : t.day;
}
function items() {
  const newest = latestTime();
  const out = shown().map(x => {
    const s = x.session, d = x.desired || {}, t = s ? tms(s.time) : 0;
    return {id: x.id, name: shortName(x), mode: d.mode || (s && s.mode) || "paper",
            state: x.running ? "live" : x.desired ? "wait" : "", stateText: x.running ? "Running" : x.desired ? "Waiting to start" : "Stopped",
            legs: s ? s.positions.length : 0, open: s ? s.open : null, total: s ? s.total : null, time: s ? s.time : "", day: dayPnl(x),
            feed: d.feed || (s && s.feed) || "",
            // a running session whose last update is 3+ minutes behind the newest one
            stale: x.running && t && newest - t > 180000 ? clock(t) : ""};
  });
  const rank = i => (i.legs > 0 ? 0 : 2) + (i.state === "live" ? 0 : 1);
  return out.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

const when = t => { if (!t) return ""; const d = new Date(t); return isNaN(d) ? "" : d.toLocaleString("en-IN", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false}); };
function select(id) {
  selected = id;
  history.replaceState(null, "", "#" + encodeURIComponent(id));
  renderChips(); renderDetail();
  if (innerWidth < 760) $("detail").scrollIntoView({behavior: "smooth", block: "start"});
}

const amt = x => x == null ? "–" : (x < 0 ? "-" : "") + Math.abs(x).toLocaleString("en-IN", {maximumFractionDigits: 0});
function renderSummary() {
  const it = items();
  if (!it.find(i => i.id === selected)) selected = it.length ? it[0].id : "";
  $("summary").innerHTML = it.length
    ? `<div class="srow hd"><span></span><span class="n">Unrealized</span><span class="n">Today</span><span class="n">Total ₹</span></div>` + it.map(i =>
      `<a class="srow${i.legs ? "" : " flat"}" href="/${KIND}#${encodeURIComponent(i.id)}" title="${esc(i.stateText)}">
        <span class="sname"><span class="dot ${i.state}"></span><span class="tx">${esc(i.name)}</span>${pips(i.legs)}${feedIcon(i.feed)}${i.stale ? icon("stale", "No update since " + i.stale) : ""}${LIVEPAGE || i.mode !== "paper" ? `<span class="badge ${i.mode}">${i.mode.toUpperCase()}</span>` : ""}</span>
        <span class="n ${sign(i.open || 0)}">${i.legs ? amt(Math.abs(i.open)) : "–"}</span><span class="n ${sign(i.day || 0)}">${i.day == null ? "–" : amt(Math.abs(i.day))}</span><span class="n t ${sign(i.total || 0)}">${i.total == null ? "–" : amt(Math.abs(i.total))}</span></a>`).join("")
    : MODE_FILTER && modeFilter !== "all" && (last.sessions || []).length ? `<div class="empty">No ${modeFilter.toUpperCase()} sessions. <a href="#" onclick="$('modeFilt').querySelector('[data-m=all]').click();return false">Show both</a>.</div>`
    : `<div class="empty">No sessions yet. <a href="/${KIND}/new">Start one</a>.</div>`;
}

// Sessions page: one oval button per strategy (name and today's P&L), three to a row
function renderChips() {
  const it = items();
  if (!it.find(i => i.id === selected)) selected = it.length ? it[0].id : "";
  $("chips").innerHTML = it.length ? it.map(i => `<button type="button" class="chip ${i.id === selected ? "sel" : ""}" data-id="${esc(i.id)}" title="${esc(i.stateText)}">
      <span class="cn"><span class="dot ${i.state}"></span> ${esc(i.name)}</span>
      <span class="cv ${sign(i.day || 0)}">${i.day == null ? "–" : ainr(i.day)}<span class="cl"> today</span></span></button>`).join("")
    : `<div class="emptybox">${icon(LIVEPAGE ? "shield" : "sim", "")}<div>${LIVEPAGE ? "No shadow or live session." : "No paper session yet."}</div>
        <a class="ibtn pri" href="/${KIND}/new"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>New session</a></div>`;
  document.querySelectorAll("#chips .chip").forEach(b => b.onclick = () => select(b.dataset.id));
}

function renderDetail() {
  const box = $("detail"), ss = last.sessions || [];

  Object.keys(charts).forEach(k => { charts[k].destroy(); delete charts[k]; });
  const x = ss.find(s => s.id === selected);
  // the card is redrawn every few seconds in market hours: keep where the reader scrolled inside a section (Activity
  // log, tables), by section and place, so the log does not jump back to the top while being read
  const where = el => { const d = el.closest("details[data-k]"); return (d ? d.dataset.k : "") + "|" + [...box.querySelectorAll(".log, .scroll")].indexOf(el); };
  const scrolled = new Map([...box.querySelectorAll(".log, .scroll")].filter(e => e.scrollTop || e.scrollLeft).map(e => [where(e), [e.scrollTop, e.scrollLeft]]));
  const sameSession = box.dataset.id === (x ? x.id : "");
  box.innerHTML = x ? sessionCard(x) : "";
  box.dataset.id = x ? x.id : "";
  box.querySelectorAll(".sess").forEach(card => {
    const id = card.dataset.id;
    card.querySelectorAll("details").forEach(d => {
      if (!d.classList.contains("sec")) d.open = !!open[id + d.dataset.k];
      d.addEventListener("toggle", () => {
        open[id + d.dataset.k] = d.open; saveOpen();
        if (d.open) Object.values(charts).forEach(c => { try { c.resize(); } catch (e) {} });
      });
    });
    card.querySelectorAll("details.cyc").forEach(d => {   // an opened cycle asks for its positions
      const need = () => { if (d.open) loadLegs(id, d.dataset.cycle); };
      d.addEventListener("toggle", need); need();
    });
    card.querySelectorAll("[data-all]").forEach(b => b.onclick = () => {
      card.querySelectorAll("details.sec").forEach(d => { d.open = b.dataset.all === "1"; });
    });
    card.querySelectorAll("button[data-act]").forEach(b => b.onclick = () => act(b.dataset.act, id, b));
    const cyc = (x.pnl || {}).cycle;
    if (cyc && pnlView(id) === "cycle") drawChart(id, card.querySelector("canvas[data-chart]"), cyc.points, cyc);
    else drawChart(id, card.querySelector("canvas[data-chart]"), x.equity || [], x.pnl);
    card.querySelectorAll(".pnlseg button").forEach(b => b.onclick = () => { setPnlView(id, b.dataset.v); render(); });
    drawPayoff(id, card.querySelector(".payoff"), x.payoff);
    wireCal(card.querySelector(".cal"), x.id, x.session && x.session.calendar, "");
    drawDaily(id + "-daily", card.querySelector("canvas[data-daily]"), (x.pnl || {}).daily);
  });
  if (sameSession) box.querySelectorAll(".log, .scroll").forEach(e => { const s = scrolled.get(where(e)); if (s) { e.scrollTop = s[0]; e.scrollLeft = s[1]; } });
}

// ---- pick a strategy (Start page) ---------------------------------------------------------------
const TAGS = {short_straddle: [["Unlimited risk", "risk"], ["Positional"]], premium_strangle: [["Unlimited risk", "risk"], ["Positional"]],
  intraday_straddle: [["Intraday"]], intraday_strangle: [["Intraday"], ["Day filters"]],
  iron_fly: [["Defined risk", "good"], ["Weekly"], ["Filters"]], double_calendar: [["Defined risk", "good"], ["Two expiries"]],
  put_credit_spread: [["Defined risk", "good"], ["Bullish"]], event_iron_fly: [["Defined risk", "good"], ["Events only"]],
  blaze_butterfly: [["Defined risk", "good"], ["Weekly"]]};
function renderPicker() {
  const cat = last.catalog || [], ss = last.sessions || [];
  const key = cat.map(c => c.name).join(",") + "|" + ss.filter(isOn).map(x => x.strategy).join(",") + "|" + (MG().time || "") + (MG().requested ? "r" : "");
  if (key === pickFor) return;
  pickFor = key;
  const mg = MG();
  $("picker").innerHTML = `<h2>1 · Choose a strategy</h2><div class="mt" style="margin:-6px 0 10px">${mg.time
      ? `Margins: Zerodha's basket margin for each strategy's entry legs (default settings, 1 lot), checked ${esc(mtime(mg.time))}`
      : "Margins appear once the engine has checked them with Zerodha"}${mg.error ? ` · <span class="down">last check failed: ${esc(mg.error)}</span>` : ""}
      · <button class="link" type="button" id="mRefresh">${mg.requested ? "update asked" : "update now"}</button></div><div class="picks">${cat.map(c => {
    const n = ss.filter(x => x.strategy === c.name && isOn(x)).length;
    const tags = (TAGS[c.name] || []).concat(LIVEPAGE && c.live_ready ? [["Live ready", "good"]] : []);
    return `<button type="button" class="scard" data-s="${esc(c.name)}"><b>${esc(c.title)}</b>
      <span>${tags.map(([t, k]) => `<span class="tag ${k || ""}">${esc(t)}</span>`).join("")}</span>
      <span class="d">${esc(c.description)}</span>${marginOf(c.name) ? `<span class="mg">Margin ${lakh(marginOf(c.name).margin)} <small>per ${esc(marginOf(c.name).unit)}</small></span>` : ""}${n ? `<span class="run">${n} running</span>` : ""}</button>`;
  }).join("")}</div>`;
  if ($("mRefresh")) $("mRefresh").onclick = e => { e.stopPropagation(); refreshMargins(e.target); };
  document.querySelectorAll(".scard").forEach(b => b.onclick = () => {
    $("strat").value = b.dataset.s; fillParams(); modeUI();
    $("controls").scrollIntoView({behavior: "smooth", block: "start"});
  });
  if ($("strat")) document.querySelectorAll(".scard").forEach(b => b.classList.toggle("on", b.dataset.s === $("strat").value));
}

// where live prices come from (the engine writes runtime/feed_status.json every few seconds)
function feedText(f) {
  if (!f || f.age == null || f.age > 90) return "";
  if (f.source === "closed") return " · " + icon("closed", "Market closed") + " Closed";
  const s = f.stream || {};
  if (f.source === "stream") return " · " + icon("live", `Zerodha live stream: ${s.subscribed || 0} instruments${s.last_message_age != null ? ", last tick " + s.last_message_age + " s ago" : ""}`) + " Live";
  return " · " + icon("poll", `Zerodha prices every ${f.poll_seconds} s` + (s && s.error ? " (stream down)" : "")) + ` ${f.poll_seconds}s`;
}

function render() {
  const ss = last.sessions || [], live = ss.some(x => x.running), want = ss.some(x => x.desired);
  $("dot").className = "dot" + (live ? " live" : want ? " wait" : "");
  const at = clock(latestTime());
  if (!last.installed) $("status").textContent = "Engine not set up";
  else if (!last.service) $("status").textContent = "Engine service is not running";
  else $("status").innerHTML = `<b>${ss.filter(x => x.running).length}${ss.length ? "/" + ss.length : ""}</b> running` + feedText(last.feed)
    + (at ? ` · <b title="Prices as of">${esc(at)}</b>` : "");
  const banner = $("banner");
  if (last.installed && !last.service) { banner.textContent = "The Algo Lab background service is not running on the server. Check it with: sudo systemctl status algolab-paper"; banner.classList.remove("hide"); }
  else banner.classList.add("hide");

  if (NEWVIEW) {
    if (!busy) renderForm();
    renderPicker();
    if (!copied && QS.get("copy")) {
      copied = true;
      const x = ss.find(s => s.id === QS.get("copy"));
      if (x) copyToForm(x);
      if (x && QS.get("deploy") && LIVEPAGE) deployInit(x);
    }
    return;
  }
  const withS = shown().filter(x => x.session);
  const total = withS.reduce((a, x) => a + x.session.total, 0);
  $("grand").textContent = withS.length ? ainr(total) : "–"; $("grand").className = "big " + sign(total);
  const dayTot = withS.reduce((a, x) => a + (dayPnl(x) || 0), 0);
  $("grandDay").textContent = withS.length ? ainr(dayTot) : "–"; $("grandDay").className = "big " + sign(dayTot);
  const openLegs = withS.reduce((a, x) => a + x.session.positions.length, 0);
  $("grandSub").innerHTML = withS.length ? `${pips(Math.min(openLegs, 1))} ${openLegs} open leg${openLegs === 1 ? "" : "s"}` : (MODE_FILTER && modeFilter !== "all" ? `No ${modeFilter.toUpperCase()} sessions.` : LIVEPAGE ? "No shadow or live sessions yet." : "No paper sessions yet.");
  // The cards below are rebuilt on every refresh. Hold their height while they are rebuilt and put the page
  // back where it was, so reading the bottom of a long page (iPhone Safari especially) never jumps up.
  const y = window.scrollY, held = ["detail", "payGrid", "calAll", "chips"].map(id => $(id)).filter(Boolean);
  held.forEach(el => { el.style.minHeight = el.offsetHeight + "px"; });
  if (OVERVIEW) { renderSummary(); renderPayoffs(); renderCalAll(); }
  else { renderChips(); renderDetail(); }
  if (Math.abs(window.scrollY - y) > 1) window.scrollTo(0, y);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    held.forEach(el => { el.style.minHeight = ""; });
    if (Math.abs(window.scrollY - y) > 1 && Date.now() - userMoved > 400) window.scrollTo(0, y);
  }));
}

// pause the refresh while the page is being scrolled or touched, and skip it when nothing changed
let userMoved = 0, lastSig = "";
["scroll", "touchstart", "touchmove", "wheel"].forEach(ev => addEventListener(ev, () => { userMoved = Date.now(); }, {passive: true}));
async function load(force) {
  try {
    const r = await fetch("/api/paper?kind=" + KIND, {cache: "no-store"});
    if (r.status === 401) { location.href = "/signin?next=/" + KIND; return; }
    const text = await r.text();
    if (!force && text === lastSig) return;
    lastSig = text;
    last = JSON.parse(text);
    render();
  } catch (e) { $("status").textContent = "Cannot reach the server"; }
}
load(true);
// Live page: how many LIVE sessions run, the limit (owner can change it) and Stop all LIVE
let liveCtl = null, stopAllArmed = 0;
async function loadLiveCtl() {
  if (!LIVEPAGE || (document.activeElement && document.activeElement.id === "maxLive")) return;
  try { const r = await fetch("/api/live/settings", {cache: "no-store"}); if (r.ok) { liveCtl = await r.json(); drawLiveCtl(); } } catch (e) {}
}
function drawLiveCtl() {
  const el = $("liveCtl"), c = liveCtl;
  if (!c) return;
  el.classList.remove("hide");
  const n = c.running.length, armedNow = Date.now() - stopAllArmed < 5000;
  el.innerHTML = `<span class="grow">LIVE sessions: <b>${n}</b> of ${c.is_owner
      ? `<input id="maxLive" type="number" min="1" max="10" value="${c.max_sessions}" aria-label="Most LIVE sessions at once"> allowed`
      : `${c.max_sessions} allowed`}</span>`
    + (n ? `<button type="button" id="stopAllLive" class="danger${armedNow ? " armed" : ""}">${armedNow ? `Tap again: square off and stop all ${n}` : "Stop all LIVE"}</button>` : "");
  const inp = $("maxLive");
  if (inp) inp.onchange = async () => {
    const r = await fetch("/api/live/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({max_sessions: +inp.value})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) alert(j.error || "Could not save.");
    inp.blur(); loadLiveCtl();
  };
  const b = $("stopAllLive");
  if (b) b.onclick = async () => {
    if (Date.now() - stopAllArmed >= 5000) { stopAllArmed = Date.now(); drawLiveCtl(); setTimeout(drawLiveCtl, 5100); return; }
    stopAllArmed = 0;
    const r = await fetch("/api/live/stop-all", {method: "POST"});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) alert(j.error || "Could not stop.");
    loadLiveCtl(); load(true);
  };
}
loadLiveCtl();
setInterval(() => { if (!document.hidden) loadLiveCtl(); }, 5000);
setInterval(() => { if (!document.hidden && Date.now() - userMoved > 1500 && !document.activeElement.closest("#controls, .payoff input"))
  load(Object.values(armed).some(t => Date.now() - t < 8000)); }, 3000);   // redraw while a "Tap again" button waits

// Live prices every second in market hours: the engine writes each session's latest prices and P&L (tick.json) every
// step; only the numbers are changed in place (no redraw), so it never jumps while being read or scrolled.
function istMarketOpen() {
  const n = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60000), m = n.getHours() * 60 + n.getMinutes();
  return n.getDay() > 0 && n.getDay() < 6 && m >= 555 && m <= 930;
}
function setTk(root, k, html, cls) {
  root.querySelectorAll(`[data-tk="${k}"]`).forEach(el => { if (el.innerHTML !== html) el.innerHTML = html; if (cls !== undefined && el.className !== cls) el.className = cls; });
}
let tickBusy = false;
async function ticks() {
  if (tickBusy || document.hidden || !last || !istMarketOpen()) return;
  tickBusy = true;
  try {
    const r = await fetch("/api/paper/ticks?kind=" + KIND, {cache: "no-store"});
    if (!r.ok) return;
    const j = await r.json();
    let reload = false;
    for (const x of last.sessions || []) {
      const t = (j.sessions || {})[x.id], s = x.session;
      if (!t || !s || t.time <= s.time) continue;
      if (t.positions.length !== s.positions.length || t.positions.some((p, i) => p.Contract !== s.positions[i].Contract || p.Qty !== s.positions[i].Qty)) { reload = true; continue; }
      Object.assign(s, {time: t.time, spot: t.spot, vix: t.vix, equity: t.equity, total: t.equity - s.capital,
                        open: t.unrealized - (t.entry_charges || 0), positions: t.positions});
      const card = document.querySelector(`.sess[data-id="${CSS.escape(x.id)}"]`);
      if (card) {
        const day = dayPnl(x);
        setTk(card, "total", ainr(s.total), "big " + sign(s.total));
        setTk(card, "today", day == null ? "–" : ainr(day), "big " + sign(day || 0));
        setTk(card, "open", s.positions.length ? ainr(s.open) : "–", "big " + sign(s.open));
        setTk(card, "open2", ainr(s.open), sign(s.open));
        setTk(card, "spot", Number(s.spot).toLocaleString("en-IN", {maximumFractionDigits: 2}));
        if (s.vix) setTk(card, "vix", Number(s.vix).toFixed(2));
        setTk(card, "plist", s.positions.map(posRow).join(""));
        setTk(card, "age", "· " + new Date(s.time).toLocaleTimeString("en-IN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}));
      }
    }
    if (OVERVIEW) renderSummary(); else renderChips();
    if (reload) load(true);              // a leg opened or closed: the full view
  } catch (e) { /* the 3-second refresh reports problems */ } finally { tickBusy = false; }
}
setInterval(ticks, 1000);
</script>
</body>
</html>
"""


# ---- the dashboard as a phone app (installable, Face ID sign-in, notifications) ----------------------
def push(title, body, url="/", category="trading"):   # replaced by pwa_panel when it loads
    pass


class _This:
    """This module's names, for the add-ons below (also when loaded without being in sys.modules)."""
    def __getattr__(self, k):
        try:
            return globals()[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k, v):
        globals()[k] = v


sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import pwa_panel as _pwa
    _pwa.attach(_This())
except Exception as _e:  # noqa: BLE001 - the dashboard must work without it
    print(f"App features (install, Face ID, notifications) not loaded: {_e}")

# ---- several logins, each with access to chosen pages ---------------------------------------------
try:
    import users_panel as _users
    _users.attach(_This())
except Exception as _e:  # noqa: BLE001 - without it the dashboard keeps its single login
    print(f"Logins and access (users_panel) not loaded: {_e}")


RUN_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run a backtest · Portfolio</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed;--warn:#a06a1a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff;--warn:#e0b34a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 10px}
.muted{color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
select,input{width:100%;font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);min-width:0}
input[type=checkbox]{width:auto}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.two>div{min-width:0}
.btn{font:inherit;font-weight:600;padding:10px 16px;border-radius:10px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;margin-top:14px;width:100%}
.btn:disabled{opacity:.5;cursor:default}
details summary{cursor:pointer;color:var(--accent);margin-top:12px}
.err{color:var(--down);margin-top:10px;font-size:14px}
.job{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
.job:first-child{border-top:0}
.job .b{flex:1;min-width:0}
.job .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
a.job{color:inherit;text-decoration:none}
.st{flex:none;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center}
.st svg{width:18px;height:18px}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
table.sw{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}table.sw th,table.sw td{padding:4px 7px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}table.sw th{color:var(--muted);font-weight:600;font-size:12px}
.seg{display:flex;gap:6px}.seg button{flex:1;font:inherit;font-size:14px;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);cursor:pointer}.seg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.mini{font:inherit;font-size:12px;padding:3px 9px;border-radius:8px;border:1px solid var(--accent);background:none;color:var(--accent);cursor:pointer}
.figs{display:flex;gap:12px;flex-wrap:wrap;font-variant-numeric:tabular-nums;font-size:14px;margin-top:2px}
.spin{animation:sp 1.2s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
@media (max-width:420px){.two{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1 id="pgTitle">Run a backtest</h1>
    <div class="muted" id="pgSub">Runs on your Mac (faster) or on the server (always on, low priority so trading is not slowed). The result appears under Results.</div>
    <div class="muted" id="presetNote" style="margin-top:4px"></div>
    <label for="strategy">Strategy</label>
    <select id="strategy"></select>
    <div class="muted" id="desc" style="margin-top:6px"></div>
    <label for="dataset">Index prices</label>
    <select id="dataset"></select>
    <div class="muted" id="sec1Note" style="margin-top:6px"></div>
    <div class="two">
      <div><label for="from">From</label><input type="date" id="from"></div>
      <div><label for="to">To</label><input type="date" id="to"></div>
    </div>
    <details id="setBox"><summary>Settings</summary><div id="params"></div></details>
    <div class="muted" id="combos" style="margin-top:8px"></div>
    <label for="fills">Fills</label>
    <select id="fills"><option value="normal">Normal: spread by premium, at the decision</option><option value="conservative">Conservative: spread x2, a minute later</option></select>
    <label for="minVol">Skip strikes with fewer contracts traded today than (0 = off; needs TrueData volume)</label>
    <input id="minVol" type="number" min="0" step="1000" value="0" inputmode="numeric">
    <label>Run on</label>
    <div class="seg" id="target"><button type="button" data-t="mac">Mac <span id="macSt"></span></button><button type="button" data-t="server">Server</button></div>
    <label for="note">Note (optional)</label>
    <input type="text" id="note" maxlength="200" placeholder="e.g. first look at real prices">
    <button class="btn" id="run">Run</button>
    <div class="err" id="err" role="alert"></div>
  </div>
  <div class="card">
    <h2>In progress</h2>
    <div id="jobs" class="muted">Loading…</div>
  </div>
  <div class="card" id="tdCard">
    <h2>TrueData real-time test</h2>
    <div class="muted">How many price updates a second TrueData's live feed sends, against Zerodha's about one: the Mac counts them
      for 2 minutes on NIFTY, the NIFTY future and this week's at-the-money call and put. Market hours only (9:15–15:25 IST).</div>
    <button class="btn" id="tdRun" type="button" style="margin-top:10px">Test TrueData live</button>
    <button class="btn" id="tdMcx" type="button" style="margin-top:10px">MCX: TrueData vs Zerodha</button>
    <div class="muted" style="margin-top:4px">MCX: gold, silver, crude oil and natural gas futures, TrueData on the Mac and Zerodha on the server
      counted over the same 2 minutes (9:00–23:20 IST).</div>
    <div class="muted" id="tdSt" style="margin-top:8px"></div>
    <div style="overflow-x:auto"><table class="sw" id="tdTbl" style="margin-top:6px"></table></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const SVG = {
  queued: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  running: '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>',
  done: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  failed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18"/></svg>'};
const TITLE = {queued: "Waiting", running: "Running", done: "Done", failed: "Failed"};
const CLS = {queued: "muted", running: "warn", done: "up", failed: "down"};
let D = null, timer = null;
const SWEEP = location.pathname === "/backtests/sweep";
if (SWEEP) {
  document.title = "Sweep · Portfolio";
  $("pgTitle").textContent = "Sweep settings";
  $("pgSub").textContent = "Give one or two settings several values, separated by commas (e.g. 10, 15, 20). Every combination is backtested and ranked by return / drawdown.";
  $("setBox").open = true; $("run").textContent = "Run sweep";
}
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const inr = v => v == null || isNaN(v) ? "–" : "₹" + Math.round(Math.abs(v)).toLocaleString("en-IN");
const dd = s => s ? new Date(s + "T00:00").toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "2-digit"}) : "start";

async function load() {
  const r = await fetch("/api/backtest-runner", {cache: "no-store"});
  if (r.status === 401) { location.href = "/signin?next=/backtests/run"; return; }
  const first = !D; D = await r.json();
  if (first) fill();
  macState();
  jobs();
}

// Algo Lab › Strategies: open with a saved preset (?strategy=…&preset=…): its values fill the form
let PRESET = null;
async function applyPreset(setVal, done) {
  const q = new URLSearchParams(location.search), sn = q.get("strategy"), id = q.get("preset");
  if (!sn || !id) return;
  try {
    const r = await fetch("/studio/api/strategies/" + encodeURIComponent(sn), {cache: "no-store"}); if (!r.ok) return;
    const p = ((await r.json()).presets || []).find(x => x.id === id); if (!p) return;
    PRESET = {id: p.id, strategy: sn, name: p.name};
    for (const [k, v] of Object.entries(p.params || {})) setVal(k, v);
    const n = document.getElementById("presetNote");
    if (n) n.innerHTML = `Preset <b>${esc(p.name)}</b> (v${p.version}) · <a href="/studio/#/s/${encodeURIComponent(sn)}/presets">change</a>`;
    if (done) done();
  } catch (e) {}
}
const presetFor = name => PRESET && PRESET.strategy === name ? {id: PRESET.id} : null;
function fill() {
  $("strategy").innerHTML = D.strategies.map(s => `<option value="${esc(s.name)}">${esc(s.title || s.name)}</option>`).join("");
  $("dataset").innerHTML = D.datasets.map(d => `<option value="${esc(d.name)}">${esc(d.name.replace("_minute", ""))} 1-minute${d.from ? ` · ${dd(d.from)} – ${dd(d.to)}` : ""}</option>`).join("");
  if ($("sec1Note")) { const s1 = (D.sec1 || {}).days || []; $("sec1Note").textContent = s1.length ? `1-second bars: ${s1.length} day${s1.length === 1 ? "" : "s"} (${dd(s1[0])} – ${dd(s1[s1.length - 1])}), for Compare with paper` : "1-second bars: none recorded yet"; }
  const want = new URLSearchParams(location.search).get("strategy");
  if (want && D.strategies.some(s => s.name === want)) $("strategy").value = want;
  const td = D.datasets.find(d => d.name === "NIFTY_minute"); if (td) $("dataset").value = td.name;
  strat(); range();
  applyPreset((k, v) => { const el = $("p_" + k); if (!el) return; if (el.type === "checkbox") el.checked = !!v; else el.value = v; });
  if (!D.may_run) { $("run").disabled = true; $("err").textContent = "This login may look at backtests but not run them."; }
  if (!D.datasets.length) { $("run").disabled = true; $("err").textContent = "No index prices on the server yet."; }
}
let TARGET = null;
function target(t) {
  TARGET = t;
  document.querySelectorAll("#target button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  try { localStorage.setItem("bt-target", t); } catch (e) {}
}
function macState() {
  const m = D.mac || {};
  $("macSt").textContent = m.online ? "· online" : "· asleep";
  if (!TARGET) { let saved = null; try { saved = localStorage.getItem("bt-target"); } catch (e) {}
    target(saved === "server" ? "server" : m.online ? "mac" : "server"); }
}
function cur() { return D.strategies.find(s => s.name === $("strategy").value); }

// ---- Orders & stops (algolab/risk.py): the "risk.*" settings, folded under one heading ----
function riskSplit(ps) { return [ps.filter(p => p.group !== "risk"), ps.filter(p => p.group === "risk")]; }
const RISK_SECTIONS = [["", ["stops"]], ["Each leg", ["leg_unit", "leg_stop", "leg_trail_every", "leg_trail_by", "leg_trail_after", "leg_trail_gap"]],
  ["Whole position (Rs)", ["cycle_loss", "cycle_target", "cycle_lock_after", "cycle_giveback", "reenter_after_stop"]], ["Day (Rs)", ["day_loss", "day_profit"]],
  ["Orders (live and shadow)", ["order_type", "chase_seconds", "max_chase_pct", "protect_mult"]]];
function riskRows(ps, row, orders) {
  const by = Object.fromEntries(ps.map(p => [p.key.slice(5), p]));
  return RISK_SECTIONS.filter(([t]) => orders || !t.startsWith("Orders")).map(([t, keys]) => {
    const rows = keys.filter(k => by[k]).map(k => row(by[k])).join("");
    return rows ? (t ? `<div style="grid-column:1/-1;margin:12px 0 2px;font-weight:600">${t}</div>` : "") + rows : "";
  }).join("");
}
function riskBox(st, rowsHtml) {
  if (!rowsHtml) return "";
  const own = st.own_stops || [];
  return `<details class="riskbox" style="grid-column:1/-1;margin-top:8px"><summary><b>Orders &amp; stops</b> <span class="muted">same rules in backtests, paper and live</span></summary>
    <div class="muted" style="font-size:13px;margin:6px 0">${own.length ? "This strategy's own stops: " + esc(own.join("; ")) + ". " : ""}<b>own</b> = the strategy's own stops only (the values below are not used) · <b>framework</b> = these only (its own are switched off) · <b>both</b> = whichever is hit first. 0 = off.</div>
    ${rowsHtml}<div style="margin-top:8px"><button type="button" class="riskSave" data-s="${esc(st.name)}">Save as this strategy's default</button> <span class="riskMsg muted"></span></div></details>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest(".riskSave"); if (!b) return;
  const risk = {};
  document.querySelectorAll('[id^="p_risk."], [id^="f_risk."]').forEach(el => { risk[el.id.slice(7)] = el.type === "checkbox" ? el.checked : el.value; });
  const msg = b.parentElement.querySelector(".riskMsg");
  try {
    const r = await fetch("/api/risk-defaults", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({strategy: b.dataset.s, risk})});
    const j = await r.json(); msg.textContent = r.ok ? "Saved: new sessions, backtests and sweeps start with these." : (j.error || "Could not save.");
  } catch (err) { msg.textContent = err.message; }
});
function strat() {
  const s = cur(); if (!s) return;
  $("desc").textContent = s.description || "";
  const [ownPs, riskPs] = riskSplit(s.params);
  const row = p => {
    const id = "p_" + p.key;
    let field;
    if (p.choices && SWEEP) field = `<input id="${id}" type="text" value="${esc(p.default)}" placeholder="${esc(p.choices.join(", "))}">`;
    else if (p.choices) field = `<select id="${id}">${p.choices.map(c => `<option${c === p.default ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
    else if (p.type === "bool") field = `<input type="checkbox" id="${id}"${p.default ? " checked" : ""}>`;
    else if (SWEEP) field = `<input id="${id}" type="text" value="${esc(p.default)}" inputmode="decimal" placeholder="${esc(p.default)}">`;
    else field = `<input id="${id}" value="${esc(p.default)}" ${p.type === "str" ? 'type="text"' : 'type="number" step="any" inputmode="decimal"'}${p.min != null ? ` min="${p.min}"` : ""}>`;
    return `<label for="${id}">${esc(p.label)}</label>${field}`;
  };
  $("params").innerHTML = ownPs.map(row).join("") + riskBox(s, riskRows(riskPs, row, false));
  count();
}
function count() {
  if (!SWEEP || !cur()) return;
  let n = 1;
  for (const p of cur().params) { const el = $("p_" + p.key); if (el && el.type === "text") n *= Math.max(1, el.value.split(",").filter(v => v.trim()).length); }
  $("combos").textContent = `${n} combination${n === 1 ? "" : "s"}` + (n > D.sweep_max ? ` · at most ${D.sweep_max}` : "");
  $("combos").className = n > D.sweep_max ? "err" : "muted";
}
function range() {
  const d = D.datasets.find(x => x.name === $("dataset").value); if (!d) return;
  $("from").min = $("to").min = d.from || ""; $("from").max = $("to").max = d.to || "";
  if (d.to) { $("to").value = d.to;
    const f = d.to.replace(/^\d{4}/, n => String(+n - 1)); $("from").value = d.from && f < d.from ? d.from : f; }
}
function params() {
  const out = {};
  for (const p of cur().params) {
    const el = $("p_" + p.key); if (!el) continue;
    let v = p.type === "bool" ? el.checked : el.value;
    if (p.type === "int") v = parseInt(v, 10); else if (p.type === "float") v = parseFloat(v);
    if (p.type !== "bool" && p.type !== "str" && isNaN(v)) throw new Error(`"${p.label}" needs a number.`);
    out[p.key] = v;
  }
  return out;
}
function sweepBody() {
  const grid = {}, base = {};
  for (const p of cur().params) {
    const el = $("p_" + p.key); if (!el) continue;
    if (p.type === "bool") { base[p.key] = el.checked; continue; }
    const vals = el.value.split(",").map(v => v.trim()).filter(Boolean);
    if (vals.length > 1) grid[p.key] = vals.join(",");
    else if (vals.length === 1) base[p.key] = p.type === "int" ? parseInt(vals[0], 10) : p.type === "float" ? parseFloat(vals[0]) : vals[0];
  }
  return {kind: "sweep", grid, params: base};
}
async function run(extra) {
  $("err").textContent = "";
  let body;
  try { body = {strategy: $("strategy").value, dataset: $("dataset").value, from: $("from").value, to: $("to").value,
                note: $("note").value, target: TARGET, fills: $("fills").value, min_volume: $("minVol").value, preset: presetFor($("strategy").value), ...(SWEEP && !extra ? sweepBody() : {params: params()}), ...(extra || {})}; } catch (e) { $("err").textContent = e.message; return; }
  $("run").disabled = true;
  try {
    const r = await fetch("/api/backtest-runner", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Could not start it.");
    $("note").value = "";
    await load();
  } catch (e) { $("err").textContent = e.message; }
  $("run").disabled = !D.may_run;
}
function sweepJob(j) {
  const keys = Object.keys(j.grid || {}), st = j.status;
  const prog = j.progress ? ` · ${j.progress[0]} of ${j.progress[1]}` : "";
  const head = `<div class="t"><a href="/backtests/sweep?job=${encodeURIComponent(j.id)}" style="color:inherit">Sweep · ${esc(j.title || j.strategy)}</a></div><div class="muted">${j.target === "mac" ? "Mac" : "Server"} · ${esc(j.dataset)} · ${dd(j.from)} – ${j.to ? dd(j.to) : "latest"} · ${keys.map(esc).join(" × ")}${st === "running" ? prog : ""}</div>`;
  let body = st === "failed" ? `<div class="down" style="font-size:13px">${esc(j.message)}</div>` : "";
  if (st === "done" && (j.rows || []).length) {
    body = `<div style="overflow-x:auto;margin-top:6px"><table class="sw"><tr>${keys.map(k => `<th>${esc(k)}</th>`).join("")}<th>Net</th><th>Ret/DD</th><th>Wins</th><th></th></tr>` +
      j.rows.slice(0, 10).map((r, i) => `<tr>${keys.map(k => `<td>${esc(r[k])}</td>`).join("")}<td class="${r.all_net >= 0 ? "up" : "down"}">${inr(r.all_net)}</td>
        <td>${r.all_ret_dd == null ? "–" : Number(r.all_ret_dd).toFixed(2)}</td><td>${r.all_win_pct == null ? "–" : Math.round(r.all_win_pct) + "%"}</td>
        <td><button class="mini" data-j="${esc(j.id)}" data-i="${i}" title="Run this one as a full backtest">Run</button></td></tr>`).join("") + "</table></div>";
  }
  return `<div class="job"><span class="st ${CLS[st] || "muted"}" title="${TITLE[st] || st}">${SVG[st] || ""}</span><div class="b">${head}${body}</div></div>`;
}
$("jobs").addEventListener("click", e => {
  const b = e.target.closest("button.mini"); if (!b) return;
  const j = (D.jobs || []).find(x => x.id === b.dataset.j), r = j && j.rows[+b.dataset.i]; if (!r) return;
  const params = Object.assign({}, j.params);
  for (const k of Object.keys(j.grid)) { const p = (D.strategies.find(s => s.name === j.strategy) || {params: []}).params.find(x => x.key === k);
    params[k] = p && p.type === "int" ? parseInt(r[k], 10) : p && p.type === "float" ? parseFloat(r[k]) : r[k]; }
  $("strategy").value = j.strategy;
  run({kind: undefined, grid: undefined, strategy: j.strategy, dataset: j.dataset, from: j.from || "", to: j.to || "", params, note: "from sweep"});
});
// only what is waiting or running; a job that finishes while the page is open leaves a one-line pointer to Results
const SEEN = {}, ENDED = [];
function inProgress(all, kind) {
  for (const j of all) {
    if (j.status === "queued" || j.status === "running") SEEN[j.id] = 1;
    else if (j.kind !== "td_live_test" && j.kind !== "kite_tick_test" && SEEN[j.id] && !ENDED.some(x => x.id === j.id) && (!kind || (j.kind === "sweep") === (kind === "sweep"))) ENDED.unshift(j);
  }
  return all.filter(j => j.kind !== "td_live_test" && j.kind !== "kite_tick_test" && (j.status === "queued" || j.status === "running") && (!kind || (j.kind === "sweep") === (kind === "sweep")));
}
function ended() {
  return ENDED.slice(0, 3).map(j => {
    const link = j.status === "done" ? (j.kind === "sweep" ? `/backtests/sweep?job=${encodeURIComponent(j.id)}` : j.run ? `/backtests?run=${encodeURIComponent(j.run)}` : "/backtests") : "";
    return `<div class="muted" style="font-size:13px;margin-top:6px">${j.status === "done" ? "Finished" : '<span class="down">Stopped</span>'}: ${esc(j.title || j.strategy)}${j.status === "done" ? ` · <a href="${link}">open in Results</a>` : ` · ${esc(j.message || "")}`}</div>`;
  }).join("");
}
function jobs() {
  const list = inProgress(D.jobs || [], "run").concat(inProgress(D.jobs || [], "sweep"));
  $("jobs").className = list.length ? "" : "muted";
  $("jobs").innerHTML = list.length ? list.map(j => {
    const m = j.metrics || {}, net = m["Net P&L"];
    const figs = j.status === "done" ? `<div class="figs"><span class="${net >= 0 ? "up" : "down"}">${inr(net)}</span>
        <span title="Win rate">${m["Win rate %"] != null ? Math.round(m["Win rate %"]) + "% wins" : ""}</span>
        <span title="Cycles">${m["Cycles"] != null ? m["Cycles"] + " cycles" : ""}</span>
        <span class="down" title="Max drawdown">${m["Max drawdown"] != null ? "↓ " + inr(m["Max drawdown"]) : ""}</span></div>`
      : j.status === "failed" ? `<div class="down" style="font-size:13px">${esc(j.message)}</div>` : "";
    if (j.kind === "sweep") return sweepJob(j);
    const head = `<div class="t">${esc(j.title || j.strategy)}</div><div class="muted">${j.target === "mac" ? "Mac" : "Server"} · ${esc(j.dataset)} · ${dd(j.from)} – ${j.to ? dd(j.to) : "latest"}${j.note ? " · " + esc(j.note) : ""}${j.target === "mac" && j.status === "queued" && !(D.mac || {}).online ? " · waits for the Mac" : ""}${j.target === "mac" && j.status === "running" && !(D.mac || {}).online ? " · Mac asleep: carries on when it wakes" : ""}</div>`;
    const inner = `<span class="st ${CLS[j.status] || "muted"}" title="${TITLE[j.status] || j.status}" aria-label="${TITLE[j.status] || j.status}">${SVG[j.status] || ""}</span><div class="b">${head}${figs}</div>`;
    return j.status === "done" && j.run ? `<a class="job" href="/backtests?run=${encodeURIComponent(j.run)}">${inner}</a>` : `<div class="job">${inner}</div>`;
  }).join("") + ended() : `Nothing running. Finished backtests are in <a href="/backtests">Results</a>.${ended()}`;
  clearTimeout(timer);
  if (list.some(j => j.status === "queued" || j.status === "running")) timer = setTimeout(load, 5000);
}
$("strategy").onchange = strat; $("dataset").onchange = range; $("run").onclick = () => run();
$("params").addEventListener("input", count);
$("target").addEventListener("click", e => { const b = e.target.closest("button"); if (b) target(b.dataset.t); });
load().catch(e => $("jobs").textContent = "Cannot load: " + e.message);
// TrueData real-time test (run by the Mac helper)
let tdTimer = null;
function tdShow(t, mac) {
  const st = $("tdSt"), tbl = $("tdTbl");
  tbl.innerHTML = "";
  if (!t) { st.textContent = mac && !mac.online ? "The Mac is asleep: it runs the test when it wakes." : ""; return; }
  const when = new Date(t.queued).toLocaleString("en-IN", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false});
  const old = mac && mac.online && !(mac.can || []).includes("td_live_test");
  if (t.status === "queued") st.innerHTML = old ? `<span class="down">Waiting: the Mac helper runs older code. On the Mac: <code>git pull</code> in ~/Trading/algotrading, then double-click deploy/install-mac-agent.command. The test then starts by itself.</span>`
    : `Sent ${when} · ${mac && mac.online ? "the Mac starts it in a moment" : "waits for the Mac (asleep or off)"}`;
  else if (t.status === "running") st.textContent = "The Mac is counting ticks (about 2 minutes)…";
  else if (t.status === "failed") st.innerHTML = `<span class="down">Did not run (${esc(when)}): ${esc(t.message || "")}</span>`;
  else {
    const r = (t.td && t.td.results) || [];
    if (t.set === "mcx") { tdBoth(t, PAIR, when); clearTimeout(tdTimer); if (!PAIR || PAIR.status === "queued" || PAIR.status === "running") tdTimer = setTimeout(tdLoad, 5000); return; }
    const opt = r.filter(x => /(CE|PE)$/.test(x.symbol)), avg = opt.length ? opt.reduce((a, x) => a + x.per_second, 0) / opt.length : null;
    st.innerHTML = `Result of ${esc(when)}` + (avg != null ? ` · options: <b>${avg.toFixed(1)}</b> updates a second (Zerodha about 1)` : "")
      + (r.length ? "" : " · no ticks arrived: the trial may not include real-time data");
    tbl.innerHTML = r.length ? `<tr><th>Symbol</th><th>a second</th><th>most in 1 s</th><th>seconds with 2+</th><th>trades</th></tr>` + r.map(x =>
      `<tr><td>${esc(x.symbol)}</td><td>${x.per_second}</td><td>${x.most_in_one_second}</td><td>${x.share_2_or_more_pct}%</td><td>${x.trades}</td></tr>`).join("") : "";
  }
  clearTimeout(tdTimer);
  if (t.status === "queued" || t.status === "running") tdTimer = setTimeout(tdLoad, 5000);
}
async function tdLoad() {
  try { const r = await fetch("/api/td-live-test", {cache: "no-store"}); if (r.ok) { const j = await r.json(); PAIR = j.pair; tdShow(j.test, j.mac); } } catch (e) {}
}
let PAIR = null;
function tdBoth(t, k, when) {
  const st = $("tdSt"), tbl = $("tdTbl");
  const td = {}, ze = {};
  ((t.td && t.td.results) || []).forEach(x => td[x.symbol] = x);
  ((k && k.kite && k.kite.results) || []).forEach(x => ze[x.symbol] = x);
  const syms = [...new Set([...(t.symbols || []), ...Object.keys(td), ...Object.keys(ze)])];
  const zs = !k ? "not found" : k.status === "failed" ? "failed: " + (k.message || "") : k.status === "done" ? "done" : k.status;
  st.innerHTML = `MCX test of ${esc(when)} · TrueData (Mac): ${esc(t.status)} · Zerodha (server): ${esc(zs)}`;
  const cell = x => x ? `${x.per_second}/s <span class="muted">(max ${x.most_in_one_second}, 2+ in ${x.share_2_or_more_pct}%, ${x.trades} trades)</span>` : "–";
  tbl.innerHTML = `<tr><th>Future</th><th>TrueData</th><th>Zerodha</th></tr>` + syms.map(s => `<tr><td>${esc(s)}</td><td>${cell(td[s])}</td><td>${cell(ze[s])}</td></tr>`).join("");
}
$("tdMcx").onclick = async () => {
  $("tdMcx").disabled = true;
  try {
    const r = await fetch("/api/td-live-test", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({set: "mcx"})}); const j = await r.json();
    if (!r.ok) $("tdSt").innerHTML = `<span class="down">${esc(j.error || "Could not send it.")}</span>`; else { PAIR = j.pair; tdShow(j.test, j.mac); }
  } catch (e) { $("tdSt").textContent = e.message; }
  $("tdMcx").disabled = false;
};
$("tdRun").onclick = async () => {
  $("tdRun").disabled = true;
  try {
    const r = await fetch("/api/td-live-test", {method: "POST"}); const j = await r.json();
    if (!r.ok) $("tdSt").innerHTML = `<span class="down">${esc(j.error || "Could not send it.")}</span>`; else tdShow(j.test, j.mac);
  } catch (e) { $("tdSt").textContent = e.message; }
  $("tdRun").disabled = false;
};
tdLoad();
</script>
</body>
</html>
"""


SWEEP_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sweep · Portfolio</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed;--warn:#a06a1a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff;--warn:#e0b34a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 10px}
.muted{color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
select,input{width:100%;font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);min-width:0}
input[type=checkbox]{width:20px;height:20px;padding:0;accent-color:var(--accent)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}.two>div{min-width:0}
.btn{font:inherit;font-weight:600;padding:10px 16px;border-radius:10px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;margin-top:14px;width:100%}
.btn:disabled{opacity:.5;cursor:default}
.seg{display:flex;gap:6px}.seg button{flex:1;font:inherit;font-size:14px;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);cursor:pointer}
.seg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.err{color:var(--down);margin-top:10px;font-size:14px}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
.prow{border-top:1px solid var(--line);padding:9px 0}
.prow .ph{display:flex;align-items:center;gap:10px}
.prow .ph span{flex:1;min-width:0;font-size:14px}
.prow .ph small{color:var(--muted);font-size:12px}
.rng{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:6px}
.rng input{padding:7px 9px;font-size:14px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.chips label{display:flex;align-items:center;gap:5px;margin:0;font-size:13px;color:var(--text);border:1px solid var(--line);border-radius:999px;padding:4px 10px}
.chips input{width:16px;height:16px}
.job{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line);cursor:pointer}
.job:first-child{border-top:0}.note-box{border:1px solid var(--line);border-left:3px solid var(--amber,#c98a00);border-radius:6px;padding:8px 10px;margin:8px 0;background:var(--accent-soft)}
.job.sel{background:var(--bg)}
.job .b{flex:1;min-width:0}.job .t{font-weight:600}
.st{flex:none;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center}.st svg{width:18px;height:18px}
.spin{animation:sp 1.2s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;width:100%}
th,td{padding:6px 7px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}
.mini{font:inherit;font-size:12px;padding:3px 9px;border-radius:8px;border:1px solid var(--accent);background:none;color:var(--accent);cursor:pointer}
.chartbox{position:relative;height:300px;margin-top:8px}
.heat td{text-align:center;min-width:44px;color:#fff;font-weight:600}
.heat th{text-align:center}
details summary{cursor:pointer;color:var(--accent);margin-top:6px}
.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}.three>div{min-width:0}
@media (max-width:420px){.two{grid-template-columns:1fr}.three{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Sweep settings</h1>
    <div class="muted" id="presetNote"></div>
    <div class="muted">Tick <b>Vary</b> for each setting to test. Numbers and times take a range (from, to, step; times step in minutes); choices take a set. Every combination is backtested; with the out-of-sample check, you choose on the earlier period and see how it held up on the later one.</div>
    <label for="strategy">Strategy</label><select id="strategy"></select>
    <label for="dataset">Index prices</label><select id="dataset"></select>
    <div class="two">
      <div><label for="from">From</label><input type="date" id="from"></div>
      <div><label for="to">To</label><input type="date" id="to"></div>
    </div>
    <label><input type="checkbox" id="useSplit" checked style="vertical-align:-4px;margin-right:6px">Out-of-sample check from</label>
    <input type="date" id="split">
    <label>Mode</label>
    <div class="seg" id="mode"><button type="button" data-m="sweep" class="on">Sweep</button><button type="button" data-m="best">Find best settings</button></div>
    <div id="bestBox" hidden>
      <div class="muted" style="margin-top:6px">Chooses on all but the last months (locked away), period by period, drops fragile settings and ranks the rest on steadiness, not on the biggest profit. The top 3 are re-checked with Conservative fills and on the locked-away months.</div>
      <div class="two">
        <div><label for="bFolds">Periods</label><select id="bFolds"><option>3</option><option selected>4</option><option>5</option><option>6</option></select></div>
        <div><label for="bLock">Locked-away days</label><input id="bLock" type="number" min="20" max="200" value="75" inputmode="numeric"></div>
      </div>
      <div class="two">
        <div><label for="bDd">Largest drawdown you accept (Rs, 0 = any)</label><input id="bDd" type="number" min="0" step="5000" value="0" inputmode="numeric"></div>
        <div><label for="bCyc">At least this many cycles</label><input id="bCyc" type="number" min="1" value="30" inputmode="numeric"></div>
      </div>
    </div>
    <div id="sweepOnly">
    <label for="folds">Walk-forward (instead of the out-of-sample date)</label>
    <select id="folds"><option value="0">Off</option><option value="3">3 periods</option><option value="4">4 periods</option><option value="5">5 periods</option><option value="6">6 periods</option></select>
    </div>
    <div id="params" style="margin-top:12px"></div>
    <label for="fills">Fills</label>
    <select id="fills"><option value="normal">Normal: spread by premium, at the decision</option><option value="conservative">Conservative: spread x2, a minute later</option></select>
    <label for="minVol">Skip strikes with fewer contracts traded today than (0 = off; needs TrueData volume)</label>
    <input id="minVol" type="number" min="0" step="1000" value="0" inputmode="numeric">
    <label>Run on</label>
    <div class="seg" id="target"><button type="button" data-t="mac">Mac <span id="macSt"></span></button><button type="button" data-t="server">Server</button></div>
    <div class="muted" id="combos" style="margin-top:10px"></div>
    <button class="btn" id="run">Run sweep</button>
    <div class="err" id="err" role="alert"></div>
  </div>

  <div class="card"><h2>In progress</h2><div id="jobs" class="muted">Loading…</div></div>

  <div class="card" id="res" hidden>
    <h2 id="resTitle"></h2>
    <div class="muted" id="resSub"></div>
    <div class="three" style="margin-top:6px">
      <div><label for="metric">Rank by</label><select id="metric"></select></div>
      <div><label for="order">Order</label><select id="order"><option value="best">Best in-sample</option><option value="robust">Most robust</option></select></div>
      <div><label>&nbsp;</label><button class="mini" id="csv" style="width:100%;padding:9px">Download CSV</button></div>
    </div>
    <div id="bestRes" hidden>
      <div id="verdict" style="margin-top:10px;font-weight:600"></div>
      <div class="muted" id="bestSub" style="margin-top:4px"></div>
      <div id="picks"></div>
    </div>
    <div class="muted" id="luck" style="margin-top:8px"></div>
    <div id="walkBox" hidden><h2 style="margin-top:16px">Walk-forward</h2>
      <div class="muted">Each period's best settings, then how they did on the next period, which they never saw. Added up, that is what choosing by the past would have made.</div>
      <div class="scroll" style="margin-top:8px"><table id="walk"></table></div></div>
    <div class="scroll" style="margin-top:10px"><table id="tbl"></table></div>
    <div class="muted" id="tblNote" style="margin-top:6px"></div>
    <div id="scatterBox"><h2 style="margin-top:16px">In-sample vs out-of-sample</h2><div class="chartbox"><canvas id="scatter"></canvas></div>
      <div class="muted">Points along a rising line: the in-sample ranking carried over. A shapeless cloud: the winners were mostly luck.</div></div>
    <div id="heatBox"><h2 style="margin-top:16px">Heatmap</h2>
      <div class="three"><div><label for="hx">Across</label><select id="hx"></select></div><div><label for="hy">Down</label><select id="hy"></select></div>
        <div><label for="hp">Period</label><select id="hp"></select></div></div>
      <div class="scroll" style="margin-top:8px"><table class="heat" id="heat"></table></div>
      <div class="muted" id="heatNote"></div></div>
    <div id="lineBox"><h2 style="margin-top:16px" id="lineTitle"></h2><div class="chartbox"><canvas id="line"></canvas></div></div>
    <details><summary>How to use a sweep without fooling yourself</summary><div class="muted" style="margin-top:6px">
      <p><b>Vary two or three settings,</b> with a handful of values each. Hundreds of combinations almost guarantee one that looks great by chance.</p>
      <p><b>Choose on the in-sample columns only,</b> then look at out-of-sample: if the winners fall apart there, the edge was not real.</p>
      <p><b>Prefer a plateau to a peak.</b> Order by Most robust: a combination whose neighbours (one step away on any setting) are also good is more likely to keep working than a lone best cell on the heatmap.</p>
      <p><b>Check enough cycles:</b> a result built on 15 cycles says little; aim for 50 or more.</p>
      <p><b>Last step:</b> Run the chosen combination over the whole period, then paper-trade it before going live.</p></div></details>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const inr = v => v == null || isNaN(v) ? "–" : "₹" + Math.round(Math.abs(v)).toLocaleString("en-IN");
const dd = s => s ? new Date(s + "T00:00").toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "2-digit"}) : "start";
const METRICS = {ret_dd: "Return / drawdown", net: "Net P&L", win_pct: "Win rate %", max_dd: "Max drawdown", worst_week: "Worst week", cycles: "Cycles"};
const MONEY = new Set(["net", "max_dd", "worst_week"]);
const SVG = {queued: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  running: '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>',
  done: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  failed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18"/></svg>'};
const CLS = {queued: "muted", running: "warn", done: "up", failed: "down"};
let D = null, TARGET = null, timer = null, OPEN = null, charts = {};

async function load() {
  const r = await fetch("/api/backtest-runner", {cache: "no-store"});
  if (r.status === 401) { location.href = "/signin?next=/backtests/sweep"; return; }
  const first = !D; D = await r.json();
  if (first) fill();
  macState(); jobs();
}

// Algo Lab › Strategies: open with a saved preset (?strategy=…&preset=…): its values fill the form
let PRESET = null;
async function applyPreset(setVal, done) {
  const q = new URLSearchParams(location.search), sn = q.get("strategy"), id = q.get("preset");
  if (!sn || !id) return;
  try {
    const r = await fetch("/studio/api/strategies/" + encodeURIComponent(sn), {cache: "no-store"}); if (!r.ok) return;
    const p = ((await r.json()).presets || []).find(x => x.id === id); if (!p) return;
    PRESET = {id: p.id, strategy: sn, name: p.name};
    for (const [k, v] of Object.entries(p.params || {})) setVal(k, v);
    const n = document.getElementById("presetNote");
    if (n) n.innerHTML = `Preset <b>${esc(p.name)}</b> (v${p.version}) · <a href="/studio/#/s/${encodeURIComponent(sn)}/presets">change</a>`;
    if (done) done();
  } catch (e) {}
}
const presetFor = name => PRESET && PRESET.strategy === name ? {id: PRESET.id} : null;
function fill() {
  $("strategy").innerHTML = D.strategies.map(s => `<option value="${esc(s.name)}">${esc(s.title || s.name)}</option>`).join("");
  $("dataset").innerHTML = D.datasets.map(d => `<option value="${esc(d.name)}">${esc(d.name.replace("_minute", ""))} 1-minute${d.from ? ` · ${dd(d.from)} – ${dd(d.to)}` : ""}</option>`).join("");
  const td = D.datasets.find(d => d.name === "NIFTY_minute"); if (td) $("dataset").value = td.name;
  const want = new URLSearchParams(location.search).get("strategy");
  if (want && D.strategies.some(s => s.name === want)) $("strategy").value = want;
  strat(); range();
  applyPreset((k, v) => { const el = $("f_" + k); if (!el) return; if (el.type === "checkbox") el.checked = !!v; else el.value = v; }, count);
  if (new URLSearchParams(location.search).get("mode") === "best") mode("best");
  const j = new URLSearchParams(location.search).get("job"); if (j) openJob(j);
}
function macState() {
  const m = D.mac || {};
  $("macSt").textContent = m.online ? "· online" : "· asleep";
  if (!TARGET) { let saved = null; try { saved = localStorage.getItem("bt-target"); } catch (e) {}
    target(saved === "server" ? "server" : m.online ? "mac" : "server"); }
}
function target(t) { TARGET = t; document.querySelectorAll("#target button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  try { localStorage.setItem("bt-target", t); } catch (e) {} count(); }
const cur = () => D.strategies.find(s => s.name === $("strategy").value);
const isTime = p => typeof p.default === "string" && /^\d{1,2}:\d{2}$/.test(p.default);
const isNum = p => p.type === "int" || p.type === "float";

// ---- Orders & stops (algolab/risk.py): the "risk.*" settings, folded under one heading ----
function riskSplit(ps) { return [ps.filter(p => p.group !== "risk"), ps.filter(p => p.group === "risk")]; }
const RISK_SECTIONS = [["", ["stops"]], ["Each leg", ["leg_unit", "leg_stop", "leg_trail_every", "leg_trail_by", "leg_trail_after", "leg_trail_gap"]],
  ["Whole position (Rs)", ["cycle_loss", "cycle_target", "cycle_lock_after", "cycle_giveback", "reenter_after_stop"]], ["Day (Rs)", ["day_loss", "day_profit"]],
  ["Orders (live and shadow)", ["order_type", "chase_seconds", "max_chase_pct", "protect_mult"]]];
function riskRows(ps, row, orders) {
  const by = Object.fromEntries(ps.map(p => [p.key.slice(5), p]));
  return RISK_SECTIONS.filter(([t]) => orders || !t.startsWith("Orders")).map(([t, keys]) => {
    const rows = keys.filter(k => by[k]).map(k => row(by[k])).join("");
    return rows ? (t ? `<div style="grid-column:1/-1;margin:12px 0 2px;font-weight:600">${t}</div>` : "") + rows : "";
  }).join("");
}
function riskBox(st, rowsHtml) {
  if (!rowsHtml) return "";
  const own = st.own_stops || [];
  return `<details class="riskbox" style="grid-column:1/-1;margin-top:8px"><summary><b>Orders &amp; stops</b> <span class="muted">same rules in backtests, paper and live</span></summary>
    <div class="muted" style="font-size:13px;margin:6px 0">${own.length ? "This strategy's own stops: " + esc(own.join("; ")) + ". " : ""}<b>own</b> = the strategy's own stops only (the values below are not used) · <b>framework</b> = these only (its own are switched off) · <b>both</b> = whichever is hit first. 0 = off.</div>
    ${rowsHtml}<div style="margin-top:8px"><button type="button" class="riskSave" data-s="${esc(st.name)}">Save as this strategy's default</button> <span class="riskMsg muted"></span></div></details>`;
}
document.addEventListener("click", async e => {
  const b = e.target.closest(".riskSave"); if (!b) return;
  const risk = {};
  document.querySelectorAll('[id^="p_risk."], [id^="f_risk."]').forEach(el => { risk[el.id.slice(7)] = el.type === "checkbox" ? el.checked : el.value; });
  const msg = b.parentElement.querySelector(".riskMsg");
  try {
    const r = await fetch("/api/risk-defaults", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({strategy: b.dataset.s, risk})});
    const j = await r.json(); msg.textContent = r.ok ? "Saved: new sessions, backtests and sweeps start with these." : (j.error || "Could not save.");
  } catch (err) { msg.textContent = err.message; }
});
function strat() {
  const [ownPs, riskPs] = riskSplit(cur().params);
  const row = p => {
    const k = p.key;
    let vary;
    if (p.type === "bool") vary = `<div class="muted" style="margin-top:4px">on and off</div>`;
    else if (p.choices) vary = `<div class="chips">${p.choices.map(c => `<label><input type="checkbox" data-c="${esc(c)}" checked>${esc(c)}</label>`).join("")}</div>`;
    else if (isNum(p) || isTime(p)) vary = `<div class="rng"><input id="a_${k}" value="${esc(p.default)}" aria-label="from"><input id="b_${k}" value="${esc(p.default)}" aria-label="to">
        <input id="s_${k}" value="${isTime(p) ? 10 : p.type === "int" ? 1 : 5}" aria-label="step"></div><div class="muted" style="font-size:11px;margin-top:2px">from · to · step${isTime(p) ? " (minutes)" : ""}</div>`;
    else vary = `<input id="l_${k}" value="${esc(p.default)}" placeholder="values, comma-separated" style="margin-top:6px">`;
    let fixed;
    if (p.type === "bool") fixed = `<input type="checkbox" id="f_${k}"${p.default ? " checked" : ""}>`;
    else if (p.choices) fixed = `<select id="f_${k}" style="margin-top:6px">${p.choices.map(c => `<option${c === p.default ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
    else fixed = `<input id="f_${k}" value="${esc(p.default)}" style="margin-top:6px">`;
    return `<div class="prow" data-k="${k}"><div class="ph"><span>${esc(p.label)}</span><small>Vary</small><input type="checkbox" class="vary" aria-label="Vary ${esc(p.label)}"></div>
      <div class="fx">${fixed}</div><div class="vy" hidden>${vary}</div></div>`;
  };
  $("params").innerHTML = ownPs.map(row).join("") + riskBox(cur(), riskRows(riskPs, row, false));
  count();
}
function steps(p, a, b, st) {
  if (isTime(p)) {
    const m = t => { const [h, mm] = String(t).trim().split(":").map(Number); return h * 60 + mm; };
    const A = m(a), B = m(b), S = Math.round(+st);
    if (!(S > 0) || isNaN(A) || isNaN(B) || B < A) throw new Error(`${p.label}: from must be before to, step at least 1 minute`);
    const out = []; for (let v = A; v <= B; v += S) out.push(String(Math.floor(v / 60)).padStart(2, "0") + ":" + String(v % 60).padStart(2, "0")); return out;
  }
  const A = +a, B = +b, S = +st;
  if (!(S > 0) || isNaN(A) || isNaN(B) || B < A) throw new Error(`${p.label}: from must be at most to, step above 0`);
  const n = Math.round((B - A) / S + 1e-9) + 1, out = [];
  for (let i = 0; i < n && i < 1000; i++) { const v = A + i * S; out.push(p.type === "int" ? String(Math.round(v)) : String(+v.toFixed(6))); }
  return out;
}
function gather() {
  const grid = {}, params = {};
  for (const p of cur().params) {
    const row = document.querySelector(`.prow[data-k="${p.key}"]`); if (!row) continue;   // e.g. live-only order settings
    const vary = row.querySelector(".vary").checked;
    if (!vary) {
      const el = $("f_" + p.key);
      params[p.key] = p.type === "bool" ? el.checked : p.type === "int" ? parseInt(el.value, 10) : p.type === "float" ? parseFloat(el.value) : el.value;
      continue;
    }
    if (p.type === "bool") grid[p.key] = ["True", "False"];
    else if (p.choices) grid[p.key] = [...row.querySelectorAll(".chips input:checked")].map(i => i.dataset.c);
    else if (isNum(p) || isTime(p)) grid[p.key] = steps(p, $("a_" + p.key).value, $("b_" + p.key).value, $("s_" + p.key).value);
    else grid[p.key] = $("l_" + p.key).value.split(",").map(v => v.trim()).filter(Boolean);
  }
  return {grid, params};
}
function count() {
  if (!D || !cur()) return;
  const lim = TARGET === "mac" ? D.sweep_max_mac : D.sweep_max;
  try {
    const {grid} = gather(), keys = Object.keys(grid).filter(k => grid[k].length > 1);
    const n = keys.reduce((a, k) => a * grid[k].length, keys.length ? 1 : 0);
    if (MODE === "best") {
      const over = n > 200000;
      $("combos").className = over ? "err" : "muted";
      $("combos").textContent = !keys.length ? "Tick Vary for at least one setting (two or more values)."
        : `${n.toLocaleString()} combination${n === 1 ? "" : "s"} to choose from` + (over ? " · at most 200,000: use bigger steps" : ` · tests up to ${Math.min(n, lim)} on the ${TARGET === "mac" ? "Mac" : "server"}${n > lim ? " (a spread, then around the best)" : ""}`);
      $("run").disabled = !keys.length || over || !D.may_run;
      $("run").textContent = "Find best settings";
      return;
    }
    $("run").textContent = "Run sweep";
    $("combos").className = n > lim ? "err" : "muted";
    $("combos").textContent = !keys.length ? "Tick Vary for at least one setting (two or more values)."
      : `${n} combination${n === 1 ? "" : "s"}` + (n > lim ? ` · at most ${lim} on the ${TARGET === "mac" ? "Mac" : "server"}` : n > 100 ? " · many: more combinations make a lucky winner more likely" : "");
    $("run").disabled = !keys.length || n > lim || !D.may_run;
  } catch (e) { $("combos").className = "err"; $("combos").textContent = e.message; $("run").disabled = true; }
}
function range() {
  const d = D.datasets.find(x => x.name === $("dataset").value); if (!d) return;
  for (const id of ["from", "to", "split"]) { $(id).min = d.from || ""; $(id).max = d.to || ""; }
  if (d.to) { $("to").value = d.to; const f = d.to.replace(/^\d{4}/, n => String(+n - 1)); $("from").value = d.from && f < d.from ? d.from : f; }
  splitDefault();
}
function splitDefault() {
  const a = new Date($("from").value + "T00:00"), b = new Date($("to").value + "T00:00");
  if (isNaN(a) || isNaN(b)) return;
  const s = new Date(a.getTime() + (b - a) * 0.7);
  $("split").value = `${s.getFullYear()}-${String(s.getMonth() + 1).padStart(2, "0")}-${String(s.getDate()).padStart(2, "0")}`;
}
async function run() {
  $("err").textContent = "";
  let g;
  try { g = gather(); } catch (e) { $("err").textContent = e.message; return; }
  const body = {kind: "sweep", strategy: $("strategy").value, dataset: $("dataset").value, from: $("from").value, to: $("to").value,
                split: MODE === "sweep" && $("useSplit").checked && +$("folds").value === 0 ? $("split").value : "", folds: MODE === "best" ? +$("bFolds").value : +$("folds").value,
                ...(MODE === "best" ? {optimise: true, lock_days: +$("bLock").value, max_dd: +$("bDd").value, min_cycles: +$("bCyc").value} : {}), target: TARGET, fills: $("fills").value, min_volume: $("minVol").value, grid: g.grid, params: g.params, preset: presetFor($("strategy").value)};
  $("run").disabled = true;
  try {
    const r = await fetch("/api/backtest-runner", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || "Could not start it.");
    await load();
  } catch (e) { $("err").textContent = e.message; }
  count();
}
const SEEN = {}, ENDED = [];
function jobs() {
  for (const j of D.jobs || []) {
    if (j.kind !== "sweep") continue;
    if (j.status === "queued" || j.status === "running") SEEN[j.id] = 1;
    else if (SEEN[j.id] && !ENDED.some(x => x.id === j.id)) ENDED.unshift(j);
  }
  const list = (D.jobs || []).filter(j => j.kind === "sweep" && (j.status === "queued" || j.status === "running"));
  const tail = ENDED.slice(0, 3).map(j => `<div class="muted" style="font-size:13px;margin-top:6px">${j.status === "done"
    ? `Finished: ${esc(j.title || j.strategy)} · <a href="#" data-open="${esc(j.id)}">show results</a>` : `<span class="down">Stopped</span>: ${esc(j.title || j.strategy)} · ${esc(j.message || "")}`}</div>`).join("");
  $("jobs").className = list.length ? "" : "muted";
  $("jobs").innerHTML = list.length ? list.map(j => {
    const keys = Object.keys(j.grid || {}), prog = j.progress ? ` · ${j.progress[0]} of ${j.progress[1]}` : "";
    const best = j.status === "done" && j.rows && j.rows[0];
    const per = (j.periods || ["all"])[0];
    return `<div class="job${OPEN && OPEN.id === j.id ? " sel" : ""}" data-j="${esc(j.id)}"><span class="st ${CLS[j.status] || "muted"}">${SVG[j.status] || ""}</span>
      <div class="b"><div class="t">${esc(j.title || j.strategy)}${j.optimise ? " · find best settings" : ""}</div>
      <div class="muted">${j.target === "mac" ? "Mac" : "Server"} · ${esc(j.dataset)} · ${dd(j.from)} – ${j.to ? dd(j.to) : "latest"} · ${keys.map(esc).join(" × ")} · ${j.total || keys.reduce((a, k) => a * j.grid[k].length, 1)} combinations${j.status === "running" ? prog : ""}${j.target === "mac" && j.status === "queued" && !(D.mac || {}).online ? " · waits for the Mac" : ""}</div>
      ${j.status === "failed" ? `<div class="down" style="font-size:13px">${esc(j.message)}</div>` : ""}
      ${best ? `<div class="muted">Best ${j.split ? "in-sample" : ""}: ${keys.map(k => `${esc(k)} ${esc(best[k])}`).join(", ")} · <span class="${best[per + "_net"] >= 0 ? "up" : "down"}">${inr(best[per + "_net"])}</span></div>` : ""}</div></div>`;
  }).join("") + tail : `Nothing running. Finished sweeps are in <a href="/backtests">Results</a>.${tail}`;
  clearTimeout(timer);
  if (list.some(j => j.status === "queued" || j.status === "running")) timer = setTimeout(load, 5000);
}
async function openJob(id) {
  const r = await fetch(`/api/backtest-runner/job/${encodeURIComponent(id)}`, {cache: "no-store"});
  if (!r.ok) return;
  const j = (await r.json()).job;
  if (j.status !== "done") return;
  OPEN = j; jobs(); showResults();
  history.replaceState(null, "", "/backtests/sweep?job=" + encodeURIComponent(id));
  $("res").hidden = false; $("res").scrollIntoView({behavior: "smooth"});
}
function perName(p) { const f = (OPEN && OPEN.fold_dates || {})[p]; return {in: "In-sample", out: "Out-of-sample", all: "Whole period"}[p] || (f ? `Period ${p.slice(1)}: ${dd(f[0])} – ${dd(f[1])}` : p); }
function varied() { return Object.keys(OPEN.grid || {}).filter(k => OPEN.grid[k].length > 1); }
function label(k) { const s = D.strategies.find(x => x.name === OPEN.strategy); const p = s && s.params.find(x => x.key === k); return p ? p.label : k; }
function short(k) { const t = label(k).split(" (")[0].replace(/^\.\.\./, ""); return t.length > 16 ? t.slice(0, 15) + "…" : t; }
function fmt(m, v) { return v == null ? "–" : MONEY.has(m) ? inr(v) : m === "win_pct" ? Math.round(v) + "%" : m === "cycles" ? v : (+v).toFixed(2); }
function sorted() {
  const per = OPEN.periods || ["all"], m = $("metric").value, key = $("order").value === "robust" ? "robust_" + m : per[0] + "_" + m;
  const sign = m === "max_dd" || m === "worst_week" ? 1 : 1;   // drawdowns are negative numbers: higher is better
  return OPEN.rows.slice().sort((a, b) => (b[key] ?? -Infinity) * sign - (a[key] ?? -Infinity) * sign);
}
function showResults() {
  const j = OPEN, per = j.periods || ["all"], keys = varied();
  $("resTitle").textContent = `${j.title || j.strategy} · ${j.rows.length} combinations`;
  $("resSub").textContent = `${j.dataset} · ` + (j.split ? `in-sample ${dd(j.from)} – ${dd(j.split)} · out-of-sample ${dd(j.split)} – ${j.to ? dd(j.to) : "latest"}` : `${dd(j.from)} – ${j.to ? dd(j.to) : "latest"}`) + ` · ${j.target === "mac" ? "ran on the Mac" : "ran on the server"}`;
  $("metric").innerHTML = Object.entries(METRICS).filter(([m]) => j.columns.includes(per[0] + "_" + m)).map(([m, t]) => `<option value="${m}">${t}</option>`).join("");
  $("order").options[0].textContent = j.split ? "Best in-sample" : "Best result";
  const opts = keys.map(k => `<option value="${esc(k)}">${esc(label(k))}</option>`).join("");
  $("hx").innerHTML = opts; $("hy").innerHTML = opts; if (keys.length > 1) $("hy").value = keys[1];
  $("hp").innerHTML = per.map(p => `<option value="${p}">${perName(p)}</option>`).join("");
  $("luck").textContent = j.luck || "";
  const o = j.optimise && typeof j.optimise === "object" ? j.optimise : null;
  $("bestRes").hidden = !o;
  if (o) {
    $("verdict").textContent = o.verdict;
    const drop = Object.entries(o.dropped || {}).map(([w, n]) => `${n} ${w}`).join("; ");
    $("bestSub").textContent = `${o.tested} combinations tested, ${o.passed} passed the filters` + (drop ? ` (dropped: ${drop})` : "") +
      ` · chosen on ${dd(j.from || o.fold_dates.f1[0])} – ${dd(o.fold_dates[Object.keys(o.fold_dates).pop()][1])} · locked away ${dd(o.lock[0])} – ${dd(o.lock[1])}`;
    $("picks").innerHTML = (o.picks || []).map((p, i) => `<div class="job" style="display:block;margin-top:10px">
      <div class="t">Pick ${i + 1}</div><div>${Object.entries(p.params).map(([k, v]) => `${esc(label(k))}: <b>${esc(v)}</b>`).join("<br>")}</div>
      <ul style="margin:6px 0 6px 18px;padding:0">${p.reasons.map(r => `<li>${esc(Object.keys(p.params).reduce((t, k) => t.split(k).join(label(k)), r))}</li>`).join("")}</ul>
      <div class="muted">Periods: ${p.period_nets.map(v => `<span class="${(v || 0) >= 0 ? "up" : "down"}">${inr(v)}</span>`).join(" · ")} · worst drawdown ${inr(p.worst_dd)}${p.mc_dd != null ? ` · 1 in 20 reshuffles worse than ${inr(p.mc_dd)}` : ""}${p.ret_margin != null ? ` · return on margin ${p.ret_margin}%` : ""} · ${p.cycles} cycles</div>
      <div style="margin-top:8px"><button class="mini" data-pick="${i}">Start paper session</button> <button class="mini" data-pickrun="${i}">Backtest it</button> <button class="mini" data-picksave="${i}">Save as preset</button></div></div>`).join("");
  }
  const w = j.walk;
  $("walkBox").hidden = !(w && w.steps && w.steps.length);
  if (w && w.steps && w.steps.length) {
    $("walk").innerHTML = `<tr><th>Chosen on</th><th>Settings</th><th>Then made</th><th>Max drawdown</th><th>Cycles</th></tr>` +
      w.steps.map(s => `<tr><td>${dd(s.train[0])} – ${dd(s.train[1])}</td><td>${Object.entries(s.params).filter(([k]) => keys.includes(k)).map(([k, v]) => `${esc(short(k))} ${esc(v)}`).join(", ")}${s.same_as_before ? ' <span class="muted">(same)</span>' : ""}</td>
        <td class="${(s.test_net || 0) >= 0 ? "up" : "down"}">${inr(s.test_net)}</td><td>${s.test_max_dd == null ? "–" : inr(s.test_max_dd)}</td><td>${s.test_cycles ?? "–"}</td></tr>`).join("") +
      `<tr><td><b>Total</b></td><td></td><td class="${w.net >= 0 ? "up" : "down"}"><b>${inr(w.net)}</b></td><td></td><td></td></tr>`;
  }
  draw();
}
function draw() {
  const j = OPEN, per = j.periods || ["all"], keys = varied(), m = $("metric").value, rows = sorted();
  const cols = [...per.map(p => [p + "_" + m, `${{in: "In", out: "Out", all: ""}[p] ?? "P" + p.slice(1)} ${METRICS[m]}`.trim()]),
                ...["net", "win_pct", "cycles", "max_dd"].filter(x => x !== m).map(x => [per[0] + "_" + x, `${per[0] === "in" ? "In " : ""}${METRICS[x]}`]),
                ["robust_" + m, "Robust"]];
  $("tbl").innerHTML = `<tr><th>#</th>${keys.map(k => `<th title="${esc(label(k))}">${esc(short(k))}</th>`).join("")}${cols.map(c => `<th>${esc(c[1])}</th>`).join("")}<th></th></tr>` +
    rows.slice(0, 50).map((r, i) => `<tr><td>${i + 1}</td>${keys.map(k => `<td>${esc(r[k])}</td>`).join("")}${cols.map(([c]) => {
      const mm = c.replace(/^(in|out|all|robust)_/, ""), v = r[c];
      return `<td class="${MONEY.has(mm) && v != null ? (v >= 0 ? "up" : "down") : ""}">${fmt(mm, v)}</td>`; }).join("")}
      <td><button class="mini" data-i="${OPEN.rows.indexOf(r)}">Run</button></td></tr>`).join("");
  $("tblNote").textContent = rows.length > 50 ? `Top 50 of ${rows.length}; Download CSV has them all.` : "";
  Object.values(charts).forEach(c => c.destroy()); charts = {};
  const grid = css("--line"), muted = css("--muted");
  $("scatterBox").hidden = !j.split;
  if (j.split) charts.s = new Chart($("scatter"), {type: "scatter",
    data: {datasets: [{data: j.rows.map(r => ({x: r["in_" + m], y: r["out_" + m], r})), pointRadius: 5,
      backgroundColor: j.rows.map(r => r["out_" + m] >= 0 ? css("--up") : css("--down"))}]},
    options: {maintainAspectRatio: false, animation: false, plugins: {legend: {display: false},
      tooltip: {callbacks: {label: c => keys.map(k => `${k}=${c.raw.r[k]}`).join(", ") + ` · in ${fmt(m, c.raw.x)} · out ${fmt(m, c.raw.y)}`}}},
      scales: {x: {title: {display: true, text: "In-sample", color: muted}, grid: {color: grid}, ticks: {color: muted}},
               y: {title: {display: true, text: "Out-of-sample", color: muted}, grid: {color: grid}, ticks: {color: muted}}}}});
  $("heatBox").hidden = keys.length < 2; $("lineBox").hidden = keys.length !== 1;
  if (keys.length >= 2) heat();
  if (keys.length === 1) {
    const k = keys[0], vals = j.grid[k];
    $("lineTitle").textContent = `${METRICS[m]} by ${label(k)}`;
    charts.l = new Chart($("line"), {type: "line", data: {labels: vals, datasets: per.map((p, i) => ({label: perName(p),
      data: vals.map(v => { const r = j.rows.find(x => String(x[k]) === String(v)); return r ? r[p + "_" + m] : null; }),
      borderColor: [css("--accent"), css("--warn"), css("--up"), css("--down"), muted, grid][i % 6], backgroundColor: [css("--accent"), css("--warn"), css("--up"), css("--down"), muted, grid][i % 6], tension: 0, spanGaps: true}))},
      options: {maintainAspectRatio: false, animation: false, plugins: {legend: {labels: {color: muted}}},
        scales: {x: {grid: {display: false}, ticks: {color: muted}}, y: {grid: {color: grid}, ticks: {color: muted}}}}});
  }
}
function heat() {
  const j = OPEN, m = $("metric").value, x = $("hx").value, y = $("hy").value, p = $("hp").value, col = p + "_" + m;
  if (x === y) { $("heat").innerHTML = ""; $("heatNote").textContent = "Pick two different settings."; return; }
  const xs = j.grid[x], ys = j.grid[y], cell = {};
  for (const r of j.rows) { const key = r[x] + "|" + r[y], v = r[col]; if (v == null) continue; (cell[key] = cell[key] || []).push(v); }
  const avg = a => a && a.length ? a.reduce((s, v) => s + v, 0) / a.length : null;
  const all = Object.values(cell).map(avg).filter(v => v != null), big = Math.max(1e-9, ...all.map(Math.abs));
  $("heat").innerHTML = `<tr><th title="${esc(label(y))} down, ${esc(label(x))} across">↓ →</th>${xs.map(v => `<th>${esc(v)}</th>`).join("")}</tr>` +
    ys.map(yv => `<tr><th>${esc(yv)}</th>${xs.map(xv => { const v = avg(cell[xv + "|" + yv]);
      if (v == null) return "<td></td>";
      const a = (0.25 + 0.75 * Math.min(1, Math.abs(v) / big)).toFixed(2), bg = v >= 0 ? css("--up") : css("--down");
      return `<td style="background:color-mix(in srgb, ${bg} ${Math.round(a * 100)}%, transparent)">${MONEY.has(m) ? (Math.abs(v) >= 1e5 ? (v / 1e5).toFixed(1) + "L" : Math.round(v / 1000) + "k") : fmt(m, v)}</td>`; }).join("")}</tr>`).join("");
  $("heatNote").textContent = `${label(y)} down, ${label(x)} across` + (varied().length > 2 ? "; other settings averaged." : ".");
}
function csv() {
  const j = OPEN, cols = j.columns, lines = [cols.join(",")].concat(j.rows.map(r => cols.map(c => r[c] == null ? "" : String(r[c]).includes(",") ? `"${r[c]}"` : r[c]).join(",")));
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([lines.join("\n")], {type: "text/csv"}));
  a.download = `${j.sweep || "sweep"}.csv`; a.click();
}
$("strategy").onchange = strat; $("dataset").onchange = () => { range(); };
$("from").onchange = splitDefault; $("to").onchange = splitDefault;
$("useSplit").onchange = () => { $("split").disabled = !$("useSplit").checked; };
var MODE = "sweep";
function mode(m) { MODE = m; for (const b of $("mode").querySelectorAll("button")) b.classList.toggle("on", b.dataset.m === m);
  $("bestBox").hidden = m !== "best"; $("sweepOnly").hidden = m === "best"; $("useSplit").closest("label").hidden = m === "best"; $("split").hidden = m === "best"; count(); }
$("mode").addEventListener("click", e => { const b = e.target.closest("button"); if (b) mode(b.dataset.m); });
function pickParams(i) {
  const p = OPEN.optimise.picks[i], s = D.strategies.find(x => x.name === OPEN.strategy), params = Object.assign({}, OPEN.params);
  for (const [k, v] of Object.entries(p.params)) { const d = s && s.params.find(x => x.key === k);
    params[k] = d && d.type === "int" ? parseInt(v, 10) : d && d.type === "float" ? parseFloat(v) : d && d.type === "bool" ? v === "True" : v; }
  return params;
}
$("picks").addEventListener("click", async e => {
  const b = e.target.closest("button.mini"); if (!b) return;
  if (b.dataset.picksave != null) {          // Algo Lab › Strategies: keep this pick as a named preset
    const i = +b.dataset.picksave, day = new Date().toLocaleDateString("en-IN", {day: "numeric", month: "short"});
    b.disabled = true;
    const r = await fetch("/studio/api/presets", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({strategy: OPEN.strategy, name: `Best pick ${i + 1} · ${day}`, params: pickParams(i), source: `Find best settings, pick ${i + 1}, ${day}`})});
    const j = await r.json().catch(() => ({}));
    b.textContent = r.ok ? "Saved: see Studio" : (j.error || "Failed");
    return;
  }
  const i = +(b.dataset.pick ?? b.dataset.pickrun), params = pickParams(i);
  b.disabled = true;
  const start = b.dataset.pick != null;
  const res = await fetch(start ? "/api/paper/start" : "/api/backtest-runner", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(start ? {strategy: OPEN.strategy, feed: "kite", mode: "paper", params, label: `best pick ${i + 1}`}
      : {strategy: OPEN.strategy, dataset: OPEN.dataset, from: OPEN.from || "", to: OPEN.to || "", params, target: TARGET, fills: "normal", note: `best pick ${i + 1}`})});
  const j = await res.json().catch(() => ({}));
  b.textContent = res.ok ? (start ? "Started: see Paper" : "Sent: see Run, then Results") : (j.error || "Failed");
});
$("params").addEventListener("change", e => {
  const v = e.target.closest(".vary"); if (v) { const row = v.closest(".prow"); row.querySelector(".fx").hidden = v.checked; row.querySelector(".vy").hidden = !v.checked; }
  count(); });
$("params").addEventListener("input", count);
$("target").addEventListener("click", e => { const b = e.target.closest("button"); if (b) target(b.dataset.t); });
$("run").onclick = run;
$("jobs").addEventListener("click", e => {
  const a = e.target.closest("a[data-open]"); if (a) { e.preventDefault(); openJob(a.dataset.open); return; }
  const d = e.target.closest(".job"); if (d) openJob(d.dataset.j); });
for (const id of ["metric", "order"]) $(id).onchange = draw;
for (const id of ["hx", "hy", "hp"]) $(id).onchange = heat;
$("csv").onclick = csv;
$("tbl").addEventListener("click", async e => {
  const b = e.target.closest("button.mini"); if (!b) return;
  const r = OPEN.rows[+b.dataset.i], s = D.strategies.find(x => x.name === OPEN.strategy), params = Object.assign({}, OPEN.params);
  for (const k of Object.keys(OPEN.grid)) { const p = s && s.params.find(x => x.key === k);
    params[k] = p && p.type === "int" ? parseInt(r[k], 10) : p && p.type === "float" ? parseFloat(r[k]) : p && p.type === "bool" ? r[k] === "True" : r[k]; }
  b.disabled = true; b.textContent = "Sent";
  const res = await fetch("/api/backtest-runner", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({strategy: OPEN.strategy, dataset: OPEN.dataset, from: OPEN.from || "", to: OPEN.to || "", params, target: TARGET, fills: OPEN.fills || "normal", note: "from sweep"})});
  if (!res.ok) { b.textContent = "Failed"; return; }
  b.title = "Running: see Algo Lab › Run, then Results";
});
load().catch(e => $("jobs").textContent = "Cannot load: " + e.message);
</script>
</body>
</html>
"""


COMPARE_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compare with paper · Portfolio</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed;--warn:#a06a1a;--accent-soft:rgba(47,111,237,.10)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff;--warn:#e0b34a;--accent-soft:rgba(108,155,255,.16)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 10px}
.muted{color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
select,input{width:100%;font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);min-width:0}
input[type=checkbox],input[type=radio]{width:auto}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.two>div{min-width:0}
.btn{font:inherit;font-weight:600;padding:10px 16px;border-radius:10px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;margin-top:14px;width:100%}
.btn:disabled{opacity:.5;cursor:default}
details summary{cursor:pointer;color:var(--accent);margin-top:12px}
.err{color:var(--down);margin-top:10px;font-size:14px}
.job{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
.job:first-child{border-top:0}
.job .b{flex:1;min-width:0}
.job .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
a.job{color:inherit;text-decoration:none}
.jobrow{display:flex;align-items:flex-start;gap:8px}.jobrow>a.job{flex:1;min-width:0}
.delx{font:inherit;font-size:12px;padding:4px 10px;margin-top:12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--down);cursor:pointer;white-space:nowrap}
.job.sel{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--accent);border-radius:8px;padding-left:8px}
.job.sel .t{color:var(--accent)}
.st{flex:none;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center}
.st svg{width:18px;height:18px}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
table.sw{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}table.sw th,table.sw td{padding:4px 7px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}table.sw th{color:var(--muted);font-weight:600;font-size:12px}
.seg{display:flex;gap:6px}.seg button{flex:1;font:inherit;font-size:14px;padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);cursor:pointer}.seg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.mini{font:inherit;font-size:12px;padding:3px 9px;border-radius:8px;border:1px solid var(--accent);background:none;color:var(--accent);cursor:pointer}
.figs{display:flex;gap:12px;flex-wrap:wrap;font-variant-numeric:tabular-nums;font-size:14px;margin-top:2px}
.spin{animation:sp 1.2s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
@media (max-width:420px){.two{grid-template-columns:1fr}}
.wrap{max-width:1000px}
.chk{display:flex;gap:16px;flex-wrap:wrap;margin-top:4px}.chk label{display:flex;align-items:center;gap:6px;margin:0;color:var(--text);font-size:14px}
.quick{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.slist{max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:6px 10px;margin-top:6px}
.slist label{display:flex;gap:8px;align-items:center;margin:4px 0;color:var(--text);font-size:14px}
.badge{font-size:11px;font-weight:700;padding:1px 6px;border-radius:6px;border:1px solid var(--line);color:var(--muted)}
.badge.live{border-color:var(--down);color:var(--down)}.badge.shadow{border-color:var(--warn);color:var(--warn)}
.scroll{overflow:auto}
table.sw td.l,table.sw th.l{text-align:left}
tr.flag td{background:color-mix(in srgb,var(--down) 9%,transparent)}
.note{font-size:12px;color:var(--warn)}
.tot{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.tot div{border:1px solid var(--line);border-radius:10px;padding:10px}
.tot b{display:block;font-size:13px;color:var(--muted);font-weight:600;margin-bottom:4px}
.sess{border-top:1px solid var(--line);padding-top:10px;margin-top:12px}
.tbtn{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--accent);cursor:pointer}
.tdet>td{background:var(--bg);padding:8px}.tkind{margin:4px 0 10px}.tkind>b{font-size:13px}
.trd{margin-top:4px;font-size:12px}.trd td,.trd th{white-space:nowrap}
.sess h3{font-size:14px;margin:0 0 6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.wrap{max-width:1240px}.ptitle{font-size:22px;margin:4px 0 12px}
.cols{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}.side{flex:1 1 300px;max-width:380px;min-width:0}.main{flex:999 1 560px;min-width:0}
.sub{font-size:12px;color:var(--muted)}.b{font-weight:600}.big{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums}
.tgrid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.tc{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.why{display:flex;gap:8px;align-items:flex-start;margin-top:10px;padding:10px 12px;border-radius:10px;font-size:13px;color:var(--text);
  background:color-mix(in srgb,var(--warn) 10%,var(--card));border:1px solid color-mix(in srgb,var(--warn) 35%,var(--card))}.why svg{flex:none;color:var(--warn);margin-top:1px}
.scard h3{font-size:16px;margin:0}.shead{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.right{margin-left:auto}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px}
.pill.red{background:color-mix(in srgb,var(--down) 13%,var(--card));color:var(--down)}.pill.amber{background:color-mix(in srgb,var(--warn) 13%,var(--card));color:var(--warn)}
.pill.grey{background:color-mix(in srgb,var(--muted) 13%,var(--card));color:var(--muted)}
.tgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.tcol .big{font-size:18px}
.bar{height:8px;border-radius:4px;background:color-mix(in srgb,var(--muted) 14%,var(--card));margin-top:4px}.bar i{display:block;height:8px;border-radius:4px}
.tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:14px 0 8px}
.seg2{font:inherit;font-size:13px;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}
.seg2.on{border-color:var(--accent);background:var(--accent);color:#fff;font-weight:600}
.box{border:1px solid var(--line);border-radius:10px}
table.legs{border-collapse:collapse;width:100%;min-width:760px;font-size:13px;font-variant-numeric:tabular-nums;table-layout:fixed}
table.legs th:first-child,table.legs td:first-child{width:150px}table.legs .nw{white-space:nowrap}
table.legs th{text-align:left;padding:8px 10px;color:var(--muted);font-weight:600;font-size:12px;background:color-mix(in srgb,var(--muted) 6%,var(--card));border-bottom:1px solid var(--line)}
table.legs td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}table.legs tr:last-child td{border-bottom:0}
table.legs tr.miss td{background:color-mix(in srgb,var(--down) 5%,var(--card))}
table.days tr[data-act]{cursor:pointer}table.days tr.on td{box-shadow:inset 0 -2px 0 var(--accent)}
</style>
</head>
<body>
<div class="wrap">
<h1 class="ptitle">Compare with paper</h1>
<div class="cols"><div class="side">
  <div class="card">
    <h2>New comparison</h2>
    <div class="muted">Each session that traded in the period (paper, shadow and live) is backtested with its own settings on the same days: on the 1-minute set, and stepped every second on the recorded 1-second bars. The backtests start flat at 09:15 on the first day. Runs on the server after the close.</div>
    <div class="two">
      <div><label for="from">From</label><input type="date" id="from"></div>
      <div><label for="to">To</label><input type="date" id="to"></div>
    </div>
    <div class="quick"><button class="mini" type="button" data-q="1">Last trading day</button><button class="mini" type="button" data-q="5">Last 5 trading days</button><button class="mini" type="button" data-q="w">This week</button></div>
    <label>Backtest on</label>
    <div class="chk"><label><input type="checkbox" id="b1m" checked> 1-minute bars</label><label><input type="checkbox" id="b1s" checked> 1-second bars</label></div>
    <div class="muted" id="sec1Info" style="margin-top:6px"></div>
    <label>Sessions</label>
    <div class="chk"><label><input type="radio" name="sw" id="swAll" checked> All that traded</label><label><input type="radio" name="sw" id="swPick"> Pick</label></div>
    <div class="slist" id="slist" hidden></div>
    <button class="btn" id="run" type="button">Compare</button>
    <div class="err" id="err"></div>
  </div>

  <div class="card"><h2>Comparisons</h2><div id="jobs" class="muted">Loading…</div></div>

  <details class="card" id="keepCard" hidden>
    <summary>1-second bars kept</summary>
    <div class="muted" style="margin-top:8px" id="keepInfo"></div>
    <div class="two" style="align-items:end"><div><label for="keep">Trading days to keep</label><input type="number" id="keep" min="1" max="30" step="1"></div>
      <div><button class="btn" id="keepSave" type="button" style="margin-top:0">Save</button></div></div>
    <div class="muted">Older days are deleted after the 15:45 recording run. Lowering this deletes the extra days at the next run.</div>
    <div class="err" id="keepErr"></div>
  </details>
</div>
<div class="main">
  <div class="card" id="repCard" hidden>
    <div style="display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap"><h2 id="repTitle" style="font-size:18px">Report</h2><a id="csv" href="#">Download CSV</a></div>
    <div id="rep"></div>
  </div>
  <div id="sessCards"></div>
</div></div>
</div>
<script>
const $ = id => document.getElementById(id);
const SVG = {
  queued: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  running: '<svg class="spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>',
  done: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  failed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18"/></svg>'};
const CLS = {queued: "muted", running: "warn", done: "up", failed: "down"};
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const dd = s => s ? new Date(s + "T00:00").toLocaleDateString("en-IN", {day: "numeric", month: "short"}) : "";
const amt = v => v == null ? "–" : (v < 0 ? "-" : "") + "₹" + Math.round(Math.abs(v)).toLocaleString("en-IN");
const cls = v => v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";
const iso = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
let D = null, timer = null, shown = null;

function istToday() { const d = new Date(new Date().toLocaleString("en-US", {timeZone: "Asia/Kolkata"})); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
function backDays(n) {                  // the last n weekdays up to today (holidays are skipped by the job itself)
  const out = []; const d = istToday();
  while (out.length < n) { if (d.getDay() % 6) out.push(new Date(d)); d.setDate(d.getDate() - 1); }
  return out;
}
function quick(q) {
  if (q === "w") { const t = istToday(), m = new Date(t); m.setDate(t.getDate() - ((t.getDay() + 6) % 7)); $("from").value = iso(m); $("to").value = iso(t); }
  else { const ds = backDays(+q); $("from").value = iso(ds[ds.length - 1]); $("to").value = iso(ds[0]); }
  info();
}
function info() {
  const s1 = (D && D.sec1) || {days: [], keep: 5}, f = $("from").value, t = $("to").value;
  const inside = s1.days.filter(d => (!f || d >= f) && (!t || d <= t));
  $("sec1Info").textContent = s1.days.length
    ? `1-second bars kept: ${s1.days.length} day${s1.days.length === 1 ? "" : "s"} (${dd(s1.days[0])} – ${dd(s1.days[s1.days.length - 1])}); ${inside.length} in this period. Days without them show "n/a".`
    : "1-second bars: none recorded yet (recording runs while the paper engine is on Zerodha prices).";
}
function fill() {
  const by = {paper: [], shadow: [], live: []};
  D.sessions.forEach(s => (by[s.mode] || by.paper).push(s));
  $("slist").innerHTML = Object.entries(by).filter(([, l]) => l.length).map(([m, l]) =>
    l.map(s => `<label><input type="checkbox" value="${esc(s.id)}"> ${esc(s.label || s.id)} <span class="muted">${esc(s.strategy)}</span> <span class="badge ${m}">${m.toUpperCase()}</span></label>`).join("")).join("");
  if (!$("from").value) quick("1");
  if (D.is_owner) { $("keepCard").hidden = false; $("keep").value = D.sec1.keep; }
  $("keepInfo").textContent = `Now: ${D.sec1.keep} trading days; ${D.sec1.days.length} stored.`;
}
function runState() {
  const busy = D.jobs.some(j => j.status === "queued" || j.status === "running");
  $("run").disabled = !D.may_run || D.market_hours;
  $("err").textContent = D.market_hours ? "Comparisons run after the close (15:30 IST), so the paper and live engine is not slowed." : (D.may_run ? "" : "This login may not run comparisons.");
  return busy;
}
function jobs() {
  $("jobs").innerHTML = D.jobs.length ? D.jobs.map(j => {
    const p = j.progress && j.status === "running" ? ` · ${j.progress[0]}/${j.progress[1]} sessions` : "";
    const can = j.status === "done" || j.status === "failed";
    const row = `<a class="job${j.id === shown ? " sel" : ""}" href="#${esc(j.id)}" data-id="${esc(j.id)}"${j.id === shown ? ' aria-current="true"' : ""}><span class="st ${CLS[j.status] || ""}">${SVG[j.status] || ""}</span><span class="b">
      <div class="t">${dd(j.from)} – ${dd(j.to)} · ${(j.bars || []).join(" + ")}${j.sessions && j.sessions.length ? " · " + j.sessions.length + " picked" : ""}</div>
      <div class="muted">${esc(j.status === "done" ? j.message || "" : j.status === "failed" ? j.message || "Failed" : (j.status === "running" ? "Running" : "Waiting") + p)}</div></span></a>`;
    return `<div class="jobrow">${row}${can ? `<button type="button" class="delx" data-del="${esc(j.id)}">${delAsk === j.id ? "Tap again to delete" : "Delete"}</button>` : ""}</div>`;
  }).join("") : "None yet.";
  document.querySelectorAll("#jobs a.job").forEach(a => a.onclick = e => { e.preventDefault(); const j = D.jobs.find(x => x.id === a.dataset.id); if (j && j.status === "done") report(j.id); });
  document.querySelectorAll("#jobs .delx").forEach(b => b.onclick = () => deleteJob(b.dataset.del));
}
let delAsk = "";
async function deleteJob(id) {
  if (delAsk !== id) { delAsk = id; jobs(); return; }          // first tap asks, second tap deletes
  delAsk = "";
  try {
    const r = await fetch("/api/compare-period/" + encodeURIComponent(id) + "/delete", {method: "POST"});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    if (shown === id) { shown = null; REP = null; $("repCard").hidden = true; $("sessCards").innerHTML = ""; history.replaceState(null, "", location.pathname); }
    await load();
  } catch (e) { $("err").textContent = e.message; jobs(); }
}
async function load() {
  const r = await fetch("/api/compare-period", {cache: "no-store"});
  if (r.status === 401) { location.href = "/signin?next=/backtests/compare"; return; }
  const first = !D; D = await r.json();
  if (first) fill();
  info(); jobs();
  const busy = runState();
  clearTimeout(timer); if (busy) timer = setTimeout(() => load().catch(() => {}), 3000);
  const want = location.hash.slice(1);
  if (first && want && D.jobs.some(j => j.id === want && j.status === "done")) report(want);
  else if (first && !want) { const j = D.jobs.find(x => x.status === "done"); if (j) report(j.id); }
}
// ---- the report: totals, why the gap, then each session leg by leg ----
const KNAME = {actual: "Session", bt_1m: "1-minute backtest", bt_1s: "1-second backtest"};
const kindsOf = bars => ["actual", ...bars.map(b => "bt_" + b)];
const short = c => { const p = String(c || "").split(" "); return p.length >= 2 ? p.slice(-2).join(" ") : c; };
const expOf = c => { const p = String(c || "").split(" "); return p.length >= 5 ? p.slice(1, 3).join(" ") : ""; };
const p2 = v => v == null ? "–" : Number(v).toFixed(2);
const HALT_KIND = {"max lots": "max lots", "orders per day": "orders per day", "daily loss": "daily loss limit",
  "rejected": "order rejected", "positions differ": "positions differ from Zerodha", "dashboard": "stopped from the dashboard"};
let REP = null, VIEW = {};

function fillsLine(b, f) {
  if (!f) return "";
  const diff = f.avg_diff == null ? "–" : `${f.avg_diff > 0 ? "+" : ""}${f.avg_diff.toFixed(2)}/unit${f.avg_diff_pct != null ? ` (${f.avg_diff_pct}%)` : ""}`;
  return `<span class="${f.flag ? "down" : ""}">${b}: ${f.matched} fills matched, session ${diff} vs backtest · only in session ${f.paper_only} · only in backtest ${f.backtest_only}</span>`;
}

// one row per leg: the n-th trade of a contract in each run, side by side
function legRows(byKind, bars) {
  const kinds = kindsOf(bars), groups = {};
  kinds.forEach(k => ((byKind || {})[k] || []).forEach(t => { const g = groups[t.contract] = groups[t.contract] || {}; (g[k] = g[k] || []).push(t); }));
  const rows = [];
  Object.entries(groups).forEach(([c, g]) => {
    kinds.forEach(k => (g[k] || []).sort((a, b) => a.entry < b.entry ? -1 : a.entry > b.entry ? 1 : 0));
    const n = Math.max(...kinds.map(k => (g[k] || []).length));
    for (let i = 0; i < n; i++) {
      const cells = {}; kinds.forEach(k => { cells[k] = (g[k] || [])[i] || null; });
      const first = kinds.map(k => cells[k] && cells[k].entry).filter(Boolean).sort()[0] || "";
      rows.push({contract: c, cells, first});
    }
  });
  return rows.sort((a, b) => a.first < b.first ? -1 : a.first > b.first ? 1 : 0);
}

function haltBefore(halts, t) {       // the session's stop that came before this time (HH:MM:SS), if any
  return (halts || []).find(h => !t || h.time <= t.slice(0, 5)) || null;
}

function legsTable(s, day, bars) {
  const rows = legRows((s.trades || {})[day], bars), kinds = kindsOf(bars), halts = (s.halts || {})[day];
  if (!rows.length) return '<div class="muted">No trades that day in any run.</div>';
  const cell = (r, k) => {
    const t = r.cells[k], base = r.cells.actual;
    if (!t) {
      if (k !== "actual") return '<td><span class="pill grey">Not in this backtest</span></td>';
      const h = haltBefore(halts, r.first);
      return `<td><span class="pill red">Not traded</span>${h ? `<div class="sub">${esc(h.time)} session stopped: ${esc(h.reason)}</div>` : ""}</td>`;
    }
    const gap = k !== "actual" && base ? ` <span class="sub">· ${t.net - base.net >= 0 ? "+" : "-"}₹${Math.abs(Math.round(t.net - base.net)).toLocaleString("en-IN")}</span>` : "";
    return `<td><div class="nw">${esc(t.entry)} → ${esc(t.exit)}</div><div class="nw">${p2(t.entry_price)} → ${p2(t.exit_price)}</div><div class="${cls(t.net)} b">${amt(t.net)}${gap}</div>${k !== "actual" && base && t.reason === base.reason ? "" : `<div class="sub">${esc(t.reason)}</div>`}</td>`;
  };
  return `<div class="scroll box"><table class="legs"><thead><tr><th>Leg</th>${kinds.map(k => `<th>${KNAME[k]}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => {
      const any = kinds.map(k => r.cells[k]).find(Boolean);
      const miss = kinds.some(k => !r.cells[k]);
      return `<tr class="${miss ? "miss" : ""}"><td><b>${esc(short(r.contract))}</b> <span class="sub">${esc(expOf(r.contract))}</span><div class="sub">${esc((any.side || "").toLowerCase())} ${any.qty}</div></td>${kinds.map(k => cell(r, k)).join("")}</tr>`;
    }).join("") + "</tbody></table></div>";
}

function fillsTable(s, day, bars) {
  const rows = legRows((s.trades || {})[day], bars), kinds = kindsOf(bars);
  const ref = bars.includes("1s") ? "bt_1s" : "bt_" + bars[0];
  if (!rows.length) return '<div class="muted">No fills that day.</div>';
  const fills = [];
  rows.forEach(r => {
    const any = kinds.map(k => r.cells[k]).find(Boolean), opening = any.side === "SHORT" ? "SELL" : "BUY";
    fills.push({act: opening, c: r.contract, px: k => r.cells[k] && r.cells[k].entry_price, at: k => r.cells[k] && r.cells[k].entry, order: r.first});
    fills.push({act: opening === "SELL" ? "BUY" : "SELL", c: r.contract, px: k => r.cells[k] && r.cells[k].exit_price, at: k => r.cells[k] && r.cells[k].exit,
                order: kinds.map(k => r.cells[k] && r.cells[k].exit).filter(Boolean).sort()[0] || ""});
  });
  fills.sort((a, b) => a.order < b.order ? -1 : a.order > b.order ? 1 : 0);
  const verdict = f => {
    const a = f.px("actual"), b = f.px(ref);
    if (a == null && b != null) return '<span class="sub">only in backtests</span>';
    if (b == null) return '<span class="sub">only in the session</span>';
    const d = a - b, good = f.act === "BUY" ? d <= 0 : d >= 0;
    const word = f.act === "BUY" ? (d <= 0 ? "cheaper" : "dearer") : (d >= 0 ? "higher" : "lower");
    return Math.abs(d) < 0.005 ? '<span class="sub">same</span>' : `<span class="${good ? "up" : "down"}">${Math.abs(d).toFixed(2)} ${word}</span>`;
  };
  return `<div class="scroll box"><table class="legs"><thead><tr><th>Fill</th>${kinds.map(k => `<th>${KNAME[k]}</th>`).join("")}<th>Session vs ${KNAME[ref].replace(" backtest", "")}</th></tr></thead><tbody>` +
    fills.map(f => `<tr class="${f.px("actual") == null ? "miss" : ""}"><td>${f.act} ${esc(short(f.c))}</td>${kinds.map(k => f.px(k) == null ? '<td class="sub">–</td>' : `<td>${p2(f.px(k))} <span class="sub">· ${esc(f.at(k))}</span></td>`).join("")}<td>${verdict(f)}</td></tr>`).join("") +
    "</tbody></table></div>";
}

function sessionCard(s, i, bars) {
  if (s.error) return `<div class="card"><h3>${esc(s.label || s.session)} <span class="badge ${esc(s.mode)}">${esc((s.mode || "").toUpperCase())}</span></h3><div class="err">${esc(s.error)}</div></div>`;
  const days = s.days || [], halts = s.halts || {}, starts = s.starts || {};
  const v = VIEW[s.session] = VIEW[s.session] || {
    day: (days.find(d => d.flag || (halts[d.day] || []).length) || days[0] || {}).day,
    mode: "legs", open: days.some(d => d.flag) || Object.keys(halts).length > 0};
  const badges = [];
  Object.entries(halts).forEach(([d, hs]) => hs.forEach(h => badges.push(`<span class="pill red">Halted ${days.length > 1 ? dd(d) + " " : ""}${esc(h.time)} · ${esc(HALT_KIND[h.kind] || h.kind)}</span>`)));
  Object.entries(starts).forEach(([d, t]) => badges.push(`<span class="pill amber">Late start ${days.length > 1 ? dd(d) + " " : ""}${esc(t)}</span>`));
  if (s.labels && s.labels.carried) badges.push('<span class="pill amber">Held positions from before</span>');
  if (s.labels && (s.labels.holiday || []).length) badges.push('<span class="pill amber">Holiday fills</span>');
  const totals = [["Session", s.actual_net], ...bars.map(b => [KNAME["bt_" + b], (s.bars[b] || {}).error ? null : (s.bars[b] || {}).net])];
  const top = Math.max(1, ...totals.map(t => Math.abs(t[1] || 0)));
  const tot = totals.map(([n, x]) => `<div class="tcol"><div class="sub">${n}</div><div class="big ${cls(x)}">${x == null ? "n/a" : amt(x)}</div>
    <div class="bar"><i style="width:${Math.round(Math.abs(x || 0) / top * 100)}%;background:${(x || 0) >= 0 ? "var(--up)" : "var(--down)"}"></i></div></div>`).join("");
  const errs = bars.filter(b => (s.bars[b] || {}).error).map(b => `<div class="sub down">${b}: ${esc(s.bars[b].error)}</div>`).join("");
  let body = "";
  if (v.open) {
    const dayTable = days.length > 1 ? `<div class="scroll"><table class="sw days"><tr><th class="l">Day</th><th>Session</th>${bars.map(b => `<th>${b}</th><th>Gap</th>`).join("")}<th class="l">Notes</th></tr>` +
      days.map(d => `<tr class="${d.flag ? "flag" : ""} ${d.day === v.day ? "on" : ""}" data-s="${i}" data-act="day" data-day="${d.day}"><td class="l">${dd(d.day)}</td><td class="${cls(d.actual)}">${amt(d.actual)}</td>${bars.map(b => d["bt_" + b] == null ? '<td class="sub">n/a</td><td></td>' : `<td class="${cls(d["bt_" + b])}">${amt(d["bt_" + b])}</td><td class="${cls(d["gap_" + b])}">${amt(d["gap_" + b])}</td>`).join("")}<td class="l note">${esc((d.notes || []).join(", "))}</td></tr>`).join("") + "</table></div>" : "";
    const f = (s.bars[bars.includes("1s") ? "1s" : bars[0]] || {}).fills;
    body = `${dayTable}
      <div class="tools"><span class="sub">${days.length > 1 ? dd(v.day) + " · " : ""}Show</span>
        <button type="button" class="seg2 ${v.mode === "legs" ? "on" : ""}" data-s="${i}" data-act="legs">Trades side by side</button>
        <button type="button" class="seg2 ${v.mode === "fills" ? "on" : ""}" data-s="${i}" data-act="fills">Fill prices</button>
        <span class="sub right">${f ? `Fills matched ${f.matched} · only in backtest ${f.backtest_only} · only in session ${f.paper_only}` : ""}</span></div>
      ${v.mode === "legs" ? legsTable(s, v.day, bars) : fillsTable(s, v.day, bars)}
      <div class="sub">${bars.map(b => fillsLine(b, (s.bars[b] || {}).fills)).filter(Boolean).join("<br>")}</div>
      <div class="sub">Entries can be seconds apart: a strategy acts on its own clock, counted from when each run started.</div>`;
  }
  return `<div class="card scard"><div class="shead"><h3>${esc(s.label || s.session)}</h3><span class="sub">${esc(s.strategy)}</span>
      <span class="badge ${esc(s.mode)}">${esc((s.mode || "").toUpperCase())}</span>${badges.join("")}
      <button type="button" class="mini right" data-s="${i}" data-act="open">${v.open ? "Hide trades" : "Show trades"}</button></div>
    <div class="tgrid">${tot}</div>${errs}${body}</div>`;
}

function notCompared(j) {
  const lines = (j.sessions || []).map(s => {
    const lo = s.left_out || {}, n = (lo.trades || []).length, h = (lo.held || []).length;
    if (!n && !h) return "";
    const from = dd((s.period || {}).from || j.from), parts = [];
    if (n) parts.push(`${n} trade${n > 1 ? "s" : ""} (${amt(lo.net)})`);
    if (h) parts.push(`${h} position${h > 1 ? "s" : ""} still held`);
    return `<div class="sub"><b>${esc(s.label || s.session)}</b> · ${esc(s.strategy)}: ${parts.join(" and ")} opened before ${esc(from)}, so not compared (a backtest starts flat).</div>`;
  }).filter(Boolean).join("");
  return lines ? `<div class="note-box"><b>Not compared</b>${lines}</div>` : "";
}

function whyBanner(j) {
  const out = [];
  (j.sessions || []).forEach(s => Object.entries(s.halts || {}).forEach(([d, hs]) => hs.forEach(h => {
    const after = Object.values(((s.trades || {})[d]) || {}).flat().length && legRows(s.trades[d], j.bars || ["1m"]).some(r => !r.cells.actual && r.first.slice(0, 5) >= h.time);
    out.push(`<b>${esc(s.label || s.session)} stopped at ${esc(h.time)}${(j.sessions || []).length && (j.from !== j.to) ? " on " + dd(d) : ""}</b>: ${esc(h.reason)}${after ? " Trades after that are only in the backtests, which do not apply live limits." : ""}`);
  })));
  return out.length ? `<div class="why"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3l10 18H2L12 3z"/><path d="M12 10v5"/><path d="M12 18h.01"/></svg><div>${out.join("<br>")}</div></div>` : "";
}

function renderReport() {
  const j = REP; if (!j) return;
  const bars = j.bars || ["1m"], t = j.totals || {};
  const card = (name, x, ref) => `<div class="tc"><div class="sub">${name}</div><div class="big ${cls(x)}">${x == null ? "n/a" : amt(x)}</div>${ref != null && x != null ? `<div class="sub">${amt(Math.abs(x - ref)).replace("-", "")} ${x >= ref ? "better" : "worse"} than the sessions</div>` : ""}</div>`;
  const paper = t.paper || {}, live = t.live || {};
  let html = `<div class="tgrid3">${card("Sessions (paper + shadow)", paper.actual)}${bars.map(b => card(KNAME["bt_" + b], paper["bt_" + b], paper.actual)).join("")}</div>`;
  if (live.actual != null) html += `<div class="sub" style="margin-top:6px">Live (real money, never added to paper): ${amt(live.actual)}${bars.map(b => live["bt_" + b] != null ? ` · ${b} ${amt(live["bt_" + b])}` : "").join("")}</div>`;
  html += whyBanner(j);
  html += `<div class="sub" style="margin-top:6px">Gap = backtest minus actual. A day is flagged when the gap is over ₹500 and over 10% of the day's actual P&amp;L. ${j.flagged} flag${j.flagged === 1 ? "" : "s"}.</div>`;
  if (!(j.sessions || []).length) html += '<div class="muted" style="margin-top:10px">No session traded in this period.</div>';
  $("rep").innerHTML = html;
  $("sessCards").innerHTML = (j.sessions || []).map((s, i) => sessionCard(s, i, bars)).join("") + notCompared(j);
}

document.addEventListener("click", e => {
  const el = e.target.closest && e.target.closest("[data-act]");
  if (!el || !REP || el.dataset.s == null) return;
  const s = REP.sessions[+el.dataset.s], v = VIEW[s.session];
  if (el.dataset.act === "open") v.open = !v.open;
  else if (el.dataset.act === "legs" || el.dataset.act === "fills") v.mode = el.dataset.act;
  else if (el.dataset.act === "day") v.day = el.dataset.day;
  renderReport();
});

async function report(id) {
  const r = await fetch("/api/compare-period/report/" + encodeURIComponent(id), {cache: "no-store"});
  const j = await r.json();
  $("repCard").hidden = false;
  if (!r.ok) { $("rep").innerHTML = `<div class="err">${esc(j.error)}</div>`; $("sessCards").innerHTML = ""; return; }
  shown = id; history.replaceState(null, "", "#" + id); jobs();
  $("repTitle").textContent = j.from === j.to ? dd(j.from) : `${dd(j.from)} – ${dd(j.to)}`;
  $("csv").href = "/api/compare-period/report/" + encodeURIComponent(id) + "?csv=1";
  REP = j; VIEW = {};
  renderReport();
}
document.querySelectorAll("[data-q]").forEach(b => b.onclick = () => quick(b.dataset.q));
$("from").onchange = $("to").onchange = info;
$("swAll").onchange = $("swPick").onchange = () => { $("slist").hidden = !$("swPick").checked; };
$("run").onclick = async () => {
  $("err").textContent = "";
  const bars = [$("b1m").checked ? "1m" : "", $("b1s").checked ? "1s" : ""].filter(Boolean);
  const sessions = $("swPick").checked ? [...document.querySelectorAll("#slist input:checked")].map(i => i.value) : [];
  if ($("swPick").checked && !sessions.length) { $("err").textContent = "Pick at least one session."; return; }
  $("run").disabled = true;
  try {
    const r = await fetch("/api/compare-period", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({from: $("from").value, to: $("to").value, bars, sessions})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    await load();
  } catch (e) { $("err").textContent = e.message; $("run").disabled = false; }
};
$("keepSave").onclick = async () => {
  $("keepErr").textContent = "";
  try {
    const r = await fetch("/api/compare-period/keep", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({keep_trading_days: +$("keep").value})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    D.sec1.keep = j.keep; $("keepInfo").textContent = `Now: ${j.keep} trading days; ${D.sec1.days.length} stored.`;
  } catch (e) { $("keepErr").textContent = e.message; }
};
load().catch(e => $("jobs").textContent = "Cannot load: " + e.message);
</script>
</body>
</html>
"""
