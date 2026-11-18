"""Backtests page for the portfolio dashboard.

Backtests run on the Mac (Algo Lab). `python run.py publish` there uploads each saved run here, and
this page shows them: headline figures, the P&L and drawdown curves, the settings used and every
cycle. Nothing here trades or talks to Zerodha. Runs are stored in data/backtests/<run name>/.

Routes (all behind the dashboard's sign-in):
  GET  /backtests                      the page
  GET  /api/backtests                  list of runs
  GET  /api/backtests/<name>           one run (curves, cycles; trades with ?trades=1)
  POST /api/backtests/upload           form: name, file=zip of the run's files
  POST /api/backtests/<name>/delete
  GET  /api/backtests/history?symbol=NIFTY 50&from=YYYY-MM-DD&to=YYYY-MM-DD&interval=5minute
       index candles fetched here with the server's own Zerodha login, so Algo Lab on the Mac can
       download history without logging in itself (Zerodha keeps one login per API key, and the
       login token never leaves the server).
"""
import csv
import io
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import json
import re
import shutil
import zipfile
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

BASE = Path(__file__).resolve().parent
STORE = BASE / "data" / "backtests"
NAME_OK = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]{0,99}$")
FILES = {"run.json", "equity_daily.csv", "cycles.csv", "trades.csv", "log.txt"}
MAX_UNPACKED = 40 * 1024 * 1024
LIST_METRICS = ["Net P&L", "Return on capital %", "Max drawdown", "Max drawdown %", "Return / drawdown",
                "Worst week", "Cycles", "Win rate %", "Profit factor", "Fills on real option prices"]

panel = Blueprint("backtests_panel", __name__)


def _run_dir(name):
    if not NAME_OK.match(name or ""):
        return None
    d = STORE / name
    return d if (d / "run.json").is_file() else None


def _meta(d):
    try:
        # NaN / Infinity are not valid JSON for the browser: turn them into null
        return json.loads((d / "run.json").read_text(encoding="utf-8"), parse_constant=lambda c: None)
    except (OSError, ValueError):
        return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return f if f == f else None


def _csv(path, limit=None):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        rows = []
        for i, r in enumerate(csv.DictReader(f)):
            if limit and i >= limit:
                break
            rows.append({k: _num(v) if k not in ("group", "contract", "reason", "exit_reason", "side",
                                                 "entry_time", "exit_time", "date") else v
                         for k, v in r.items()})
        return rows


def _capital(meta):
    try:
        return float(meta["config"]["account"]["capital"])
    except (KeyError, TypeError, ValueError):
        return 0.0


@panel.route("/backtests")
def backtests_page():
    return render_template("backtests.html")


@panel.route("/api/backtests")
def backtests_list():
    runs = []
    if STORE.is_dir():
        for d in sorted(STORE.iterdir(), reverse=True):
            m = _meta(d) if d.is_dir() else None
            if not m:
                continue
            mt = m.get("metrics") or {}
            runs.append({"name": d.name, "saved": m.get("saved"), "strategy": m.get("strategy"),
                         "dataset": m.get("dataset"), "from": m.get("from"), "to": m.get("to"),
                         "data_from": m.get("data_from"), "data_to": m.get("data_to"),
                         "note": m.get("note", ""), "code": m.get("code"), "params": m.get("params", {}),
                         "capital": _capital(m), "metrics": {k: mt.get(k) for k in LIST_METRICS}})
    return jsonify(runs=runs)


@panel.route("/api/backtests/<name>")
def backtests_one(name):
    d = _run_dir(name)
    if not d:
        return jsonify(error="no such run"), 404
    m = _meta(d) or {}
    cfg = m.get("config") or {}
    out = {"name": name, "meta": {k: v for k, v in m.items() if k != "config"},
           "capital": _capital(m),
           "settings": {"costs": cfg.get("costs"), "slippage_pct": (cfg.get("execution") or {}).get("slippage_pct"),
                        "intrabar": (cfg.get("execution") or {}).get("intrabar"),
                        "pricing": cfg.get("pricing")},
           "equity": _csv(d / "equity_daily.csv"), "cycles": _csv(d / "cycles.csv")}
    if request.args.get("trades"):
        out["trades"] = _csv(d / "trades.csv", limit=5000)
    if request.args.get("log"):
        try:
            out["log"] = (d / "log.txt").read_text(encoding="utf-8").splitlines()[-400:]
        except OSError:
            out["log"] = []
    return jsonify(out)


@panel.route("/api/backtests/upload", methods=["POST"])
def backtests_upload():
    name = (request.form.get("name") or "").strip()
    f = request.files.get("file")
    if not NAME_OK.match(name) or f is None:
        return jsonify(error="send a run name and a zip file"), 400
    try:
        z = zipfile.ZipFile(io.BytesIO(f.read()))
    except zipfile.BadZipFile:
        return jsonify(error="not a zip file"), 400
    members = z.infolist()
    names = {i.filename for i in members}
    if "run.json" not in names or not names <= FILES or sum(i.file_size for i in members) > MAX_UNPACKED:
        return jsonify(error=f"zip must hold run.json and only: {', '.join(sorted(FILES))}"), 400
    try:
        json.loads(z.read("run.json").decode("utf-8"))
    except ValueError:
        return jsonify(error="run.json is not valid JSON"), 400
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = STORE / f".incoming-{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    for i in members:
        (tmp / i.filename).write_bytes(z.read(i))
    final = STORE / name
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    return jsonify(ok=True, name=name)


@panel.route("/api/backtests/<name>/delete", methods=["POST"])
def backtests_delete(name):
    d = _run_dir(name)
    if not d:
        return jsonify(error="no such run"), 404
    shutil.rmtree(d)
    return jsonify(ok=True)


# ---------------------------------------------------------------- history for Algo Lab
IST = ZoneInfo("Asia/Kolkata")
INDEX_TOKENS = {"NIFTY 50": 256265, "INDIA VIX": 264969}
INTERVAL_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100, "15minute": 200,
                 "30minute": 200, "60minute": 400, "day": 2000}


def _server_kite():
    """A Kite client on the dashboard's own login of today, or (None, reason)."""
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
        return None, "the server has no Zerodha login yet"
    # Zerodha logins end around 6 am IST: a token saved before the latest 6 am is stale
    now = datetime.now(IST)
    cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    if when < cutoff:
        return None, "the server's Zerodha login is from before this morning: log in on the dashboard first"
    if not key:
        return None, "KITE_API_KEY not found on the server"
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=key)
    k.set_access_token(token)
    return k, None


@panel.route("/api/backtests/history")
def backtests_history():
    symbol = request.args.get("symbol", "NIFTY 50")
    interval = request.args.get("interval", "5minute")
    if symbol not in INDEX_TOKENS or interval not in INTERVAL_DAYS:
        return jsonify(error=f"symbol must be one of {list(INDEX_TOKENS)}, interval one of {list(INTERVAL_DAYS)}"), 400
    try:
        start = date.fromisoformat(request.args["from"])
        end = date.fromisoformat(request.args.get("to") or date.today().isoformat())
    except (KeyError, ValueError):
        return jsonify(error="from (and optionally to) must be YYYY-MM-DD"), 400
    if (end - start).days > 4000 or end < start:
        return jsonify(error="date range too long or reversed"), 400
    kite, why = _server_kite()
    if kite is None:
        return jsonify(error=why), 409
    span = timedelta(days=INTERVAL_DAYS[interval] - 1)
    rows, cur = [], start
    try:
        while cur <= end:
            to = min(cur + span, end)
            for c in kite.historical_data(INDEX_TOKENS[symbol], datetime.combine(cur, datetime.min.time()),
                                          datetime.combine(to, datetime.max.time().replace(microsecond=0)), interval):
                d = c["date"]
                rows.append([(d.replace(tzinfo=None) if hasattr(d, "tzinfo") else d).isoformat(sep=" "),
                             c["open"], c["high"], c["low"], c["close"]])
            cur = to + timedelta(days=1)
            time.sleep(0.35)  # Zerodha allows 3 history requests a second
    except Exception as e:  # noqa: BLE001 - report Zerodha's message to the Mac
        msg = str(e)
        if "permission" in msg.lower() or "subscription" in msg.lower():
            msg += " (the Kite Connect plan must include historical data)"
        return jsonify(error=f"Zerodha: {msg}"), 502
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["datetime", "open", "high", "low", "close"])
    w.writerows(rows)
    return out.getvalue(), 200, {"Content-Type": "text/csv"}
