"""How fast is TrueData's real-time feed? Count its ticks per second during market hours (trial check, 19 Oct 2026).

    python run.py truedata live-test [--seconds 120]

Subscribes to NIFTY 50, the NIFTY future and the at-the-money call and put of the nearest weekly expiry, then counts,
for each, how many ticks arrived, the most in any one second, the share of seconds with more than one tick, and how
many of the ticks were real trades (the traded quantity went up). Zerodha's stream gives about one update a second per
instrument, so this shows what TrueData would add. Prints a short report and saves it to runs/td_live_test_<time>.json.

Needs TrueData's package (python -m pip install truedata) and the TrueData login in secrets.yaml. Only reads prices.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from .config import ROOT, load_config


def summarise(arrivals: dict, trades: dict, seconds: float) -> list[dict]:
    """arrivals: {symbol: [arrival monotonic times]}, trades: {symbol: ticks whose traded quantity rose}."""
    out = []
    for sym, ts in arrivals.items():
        per_sec = defaultdict(int)
        for t in ts:
            per_sec[int(t)] += 1
        busy = sum(1 for n in per_sec.values() if n > 1)
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        out.append({"symbol": sym, "ticks": len(ts), "per_second": round(len(ts) / max(seconds, 1e-9), 2),
                    "most_in_one_second": max(per_sec.values(), default=0),
                    "seconds_with_ticks": len(per_sec), "seconds_with_2_or_more": busy,
                    "share_2_or_more_pct": round(busy / len(per_sec) * 100, 1) if per_sec else 0.0,
                    "median_gap_ms": round(gaps[len(gaps) // 2] * 1000) if gaps else None,
                    "trades": trades.get(sym, 0)})
    return sorted(out, key=lambda r: -r["ticks"])


def atm_symbols(spot: float, cfg: dict) -> list[str]:
    from .feeds import weekly_expiries
    from .models import Contract
    from .truedata import option_symbol
    step = int(cfg["market"].get("strike_step", 50))
    today = date.today()
    exps = [e for e in weekly_expiries(today, date.fromordinal(today.toordinal() + 14), cfg["market"]["expiry_rules"]) if e >= today]
    atm = int(round(spot / step) * step)
    return [option_symbol(Contract("NIFTY", exps[0], atm, r)) for r in ("CE", "PE")] if exps else []


PORTS = (8086, 8084)          # 8086: trial accounts (TrueData support, 20 Oct 2026); 8084: paid real-time (the package's default).
                              # Not 8082: that is TrueData's Market Replay.


def _refusal(user: str, pw: str, port: int, url: str = "push.truedata.in", connect=None) -> str | None:
    """TrueData's first reply on this port, if it is a refusal ({"success": false, "message": ...}); else None."""
    try:
        if connect is None:
            import ssl
            import websocket
            sslopt = {}
            try:
                import certifi
                sslopt = {"ca_certs": certifi.where(), "cert_reqs": ssl.CERT_REQUIRED}
            except ImportError:
                pass
            connect = lambda u: websocket.create_connection(u, timeout=15, sslopt=sslopt)  # noqa: E731
        ws = connect(f"wss://{url}:{port}?user={user}&password={pw}")
        try:
            msg = json.loads(ws.recv())
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001  no answer: let the package try (and report what it can)
        return None
    if isinstance(msg, dict) and msg.get("success") is False:
        extra = f" (valid until {str(msg['validity'])[:10]})" if msg.get("validity") else ""
        return str(msg.get("message") or "refused")[:160] + extra
    return None


def _connect(TD_live, user: str, pw: str, log, wait: float = 25.0):
    """(connected TD_live, None) or (None, what TrueData said). Its package waits for the connection without a limit and
    writes the reason for a refusal only to its own log, so each port is tried in a thread with a limit, its log kept."""
    import logging
    import re as _re

    class Grab(logging.Handler):
        def __init__(self):
            super().__init__(logging.INFO)
            self.msgs = []

        def emit(self, record):
            m = _re.sub(r"\x1b\[[0-9;]*m", "", record.getMessage()).strip()
            if m and "Logger ready" not in m and "Defining" not in m and m not in self.msgs:
                self.msgs.append(m[:200])
    said: list = []
    for port in PORTS:
        why = _refusal(user, pw, port)
        if why:                                   # TrueData answered and said no (e.g. "User Subscription Expired")
            said.append(f"port {port}: {why}")
            log(f"Port {port}: TrueData said: {why}")
            continue
        grab, box = Grab(), {}

        def connect():
            try:
                box["td"] = TD_live(user, pw, live_port=port, log_handler=grab)
            except TypeError:                     # an older package without these arguments
                box["td"] = TD_live(user, pw)
            except Exception as e:  # noqa: BLE001
                box["error"] = e
        th = threading.Thread(target=connect, daemon=True)
        th.start()
        th.join(wait)
        if "td" in box:
            log(f"Connected to TrueData on port {port}")
            return box["td"], None
        said += [f"port {port}: {m}" for m in grab.msgs[-3:]] + ([f"port {port}: {box['error']}"] if "error" in box else [])
        log(f"Port {port}: no connection" + (f" ({grab.msgs[-1]})" if grab.msgs else ""))
    return None, said


def run(seconds: float = 120, cfg: dict | None = None, log=print, symbols: list[str] | None = None,
        start_at: float | None = None) -> dict:
    """symbols: TrueData symbols to watch instead of NIFTY and its at-the-money options (e.g. GOLD-I, SILVER-I);
    start_at: a clock time (time.time()) to start counting at, to count together with Zerodha's side."""
    try:
        from truedata import TD_live
    except ImportError:
        raise SystemExit("TrueData's package is missing. Install it first:  .venv/bin/python -m pip install truedata")
    from .truedata import credentials
    cfg = cfg or load_config()
    user, pw = credentials()
    arrivals, trades, last_ttq, first = defaultdict(list), defaultdict(int), {}, {}
    lock = threading.Lock()
    t0 = time.monotonic()
    td, said = _connect(TD_live, user, pw, log)
    if td is None:
        raise SystemExit("Could not connect to TrueData's real-time feed (tried ports " + ", ".join(map(str, PORTS)) + ")"
                         + (": TrueData said: " + " | ".join(said) if said else
                            ". No answer: the trial may not include real-time data, or the Mac's network blocks those ports."))

    @td.trade_callback
    def on_tick(tick):
        now = time.monotonic() - t0
        sym = getattr(tick, "symbol", "?")
        with lock:
            arrivals[sym].append(now)
            ttq = getattr(tick, "ttq", None)
            if ttq is not None and sym in last_ttq and ttq > last_ttq[sym]:
                trades[sym] += 1
            if ttq is not None:
                last_ttq[sym] = ttq
            first.setdefault(sym, getattr(tick, "ltp", None))

    if symbols:
        base, opts, spot = list(symbols), [], None
        td.start_live_data(base)
        log("Connected to TrueData. Watching " + ", ".join(base))
        time.sleep(3)
    else:
        base = ["NIFTY 50", "NIFTY-I"]
        td.start_live_data(base)
        log("Connected to TrueData. Waiting for NIFTY to pick the at-the-money options…")
        for _ in range(30):
            time.sleep(1)
            if first.get("NIFTY 50") or first.get("NIFTY-I"):
                break
        spot = first.get("NIFTY 50") or first.get("NIFTY-I")
        opts = atm_symbols(float(spot), cfg) if spot else []
        if opts:
            td.start_live_data(opts)
            log("Also watching " + ", ".join(opts))
    if start_at:                                # count over the same seconds as Zerodha's side (on the server)
        wait = start_at - time.time()
        if 0 < wait < 300:
            log(f"Starting in {wait:.0f} s, together with the Zerodha count")
            time.sleep(wait)
    with lock:                                  # count from here, all symbols over the same time
        arrivals.clear()
        trades.clear()
    t1 = time.monotonic() - t0
    log(f"Counting ticks for {seconds:.0f} seconds…")
    time.sleep(seconds)
    with lock:
        arr = {k: [t - t1 for t in v] for k, v in arrivals.items()}
        tr = dict(trades)
    try:
        td.disconnect()
    except Exception:  # noqa: BLE001
        pass
    rows = summarise({**{k: [] for k in base + opts}, **arr}, tr, seconds)
    report = {"time": datetime.now().isoformat(timespec="seconds"), "seconds": seconds, "spot": spot, "source": "truedata",
              "symbols": base + opts, "results": rows}
    out = ROOT / "runs" / f"td_live_test_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    log("")
    log(f"{'Symbol':<22}{'ticks/s':>8}{'max/s':>7}{'2+ in a s':>11}{'trades':>8}")
    for r in rows:
        log(f"{r['symbol']:<22}{r['per_second']:>8}{r['most_in_one_second']:>7}{str(r['share_2_or_more_pct']) + '%':>11}{r['trades']:>8}")
    if not rows:
        log("No ticks arrived. Is the market open, and does the trial include real-time data?")
    log(f"\nSaved: {out}")
    return report


def main(argv=None) -> int:
    """python -m algolab.td_live_test [--seconds 120] [--symbols GOLD-I,SILVER-I] [--start-at T] [--out report.json]
    (the Mac helper runs it this way, as its own program: TrueData's package leaves threads behind when it cannot connect)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--start-at", type=float, default=None)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    try:
        rep = run(a.seconds, symbols=[x for x in a.symbols.split(",") if x] or None, start_at=a.start_at,
                  log=lambda m: print(m, flush=True))
    except SystemExit as e:
        print(f"STOP: {e}", flush=True)
        if a.out:
            Path(a.out).write_text(json.dumps({"error": str(e)}))
        return 2
    if a.out:
        Path(a.out).write_text(json.dumps(rep))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    import os
    code = main()
    os._exit(code)                 # do not wait for TrueData's reconnect threads
