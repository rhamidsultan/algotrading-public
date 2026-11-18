"""How fast is Zerodha's live stream? Count its ticks per instrument for a while (the Zerodha side of the TrueData test).

    python run.py tick-test-queue      (server: algolab-ticktest.path runs it when the dashboard asks; never waits
                                        for backtests: Algo Lab › Run › TrueData test › MCX)

Runs on the server (it has the day's Zerodha login). Names are MCX commodities (their nearest future) or NIFTY
(the index) / NIFTY-I (the nearest NIFTY future). Opens its own connection to Zerodha's stream (the paper engine
keeps its own; Zerodha allows three per login), subscribes in full mode, and counts per instrument: ticks, the most
in one second, seconds with two or more, and ticks that were trades (the traded volume went up). Only reads prices.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MCX = ("GOLD", "GOLDM", "SILVER", "SILVERM", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC")


def mcx_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    return now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (23, 25)


def tokens_for(kite, names: list[str]) -> dict:
    """{label: instrument token}: MCX names -> their nearest future (label NAME-I), NIFTY -> index, NIFTY-I -> future."""
    out, today = {}, date.today()
    want_mcx = [n for n in names if n.upper() in MCX]
    if want_mcx:
        futs = [i for i in kite.instruments("MCX") if i.get("instrument_type") == "FUT" and i.get("expiry") and i["expiry"] >= today]
        for n in want_mcx:
            mine = sorted((i for i in futs if i.get("name") == n.upper()), key=lambda i: i["expiry"])
            if mine:
                out[f"{n.upper()}-I"] = int(mine[0]["instrument_token"])
    if any(n.upper() == "NIFTY" for n in names):
        out["NIFTY 50"] = 256265
    if any(n.upper() == "NIFTY-I" for n in names):
        futs = sorted((i for i in kite.instruments("NFO") if i.get("name") == "NIFTY" and i.get("instrument_type") == "FUT"
                       and i.get("expiry") and i["expiry"] >= today), key=lambda i: i["expiry"])
        if futs:
            out["NIFTY-I"] = int(futs[0]["instrument_token"])
    return out


def count(ws, tokens: dict, seconds: float, clock=time.monotonic) -> tuple[dict, dict]:
    """Subscribe and count ticks on an open Kite websocket. Returns ({label: [arrival times]}, {label: trades})."""
    from .stream import parse_binary
    label = {t: k for k, t in tokens.items()}
    toks = sorted(tokens.values())
    ws.send(json.dumps({"a": "subscribe", "v": toks}))
    ws.send(json.dumps({"a": "mode", "v": ["full", toks]}))
    arrivals, trades, vol = defaultdict(list), defaultdict(int), {}
    t0 = clock()
    first = True
    while clock() - t0 < seconds:
        try:
            msg = ws.recv()
        except Exception as e:  # noqa: BLE001  timeouts: keep listening
            if "timed out" in str(e).lower() or type(e).__name__ in ("WebSocketTimeoutException", "TimeoutError"):
                continue
            raise
        if not isinstance(msg, (bytes, bytearray)):
            continue                                       # text: order updates, errors
        now = clock() - t0
        for t in parse_binary(bytes(msg)):
            k = label.get(t["token"])
            if not k:
                continue
            if first or k not in vol:                      # the snapshot sent on subscribing is not a tick
                vol[k] = t.get("volume")
                continue
            arrivals[k].append(now)
            v = t.get("volume")
            if v is not None and vol.get(k) is not None and v > vol[k]:
                trades[k] += 1
            vol[k] = v
        first = False if vol else first
    return dict(arrivals), dict(trades)


def run(seconds: float = 120, names: list[str] | None = None, log=print, start_at: float | None = None) -> dict:
    from .kite import client, load_secrets, session_info
    from .stream import WS_URL
    from .td_live_test import summarise
    names = names or ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS"]
    kite = client()
    tokens = tokens_for(kite, names)
    if not tokens:
        raise SystemExit("None of these instruments were found at Zerodha: " + ", ".join(names))
    import ssl
    import websocket
    sslopt = {}
    try:
        import certifi
        sslopt = {"ca_certs": certifi.where(), "cert_reqs": ssl.CERT_REQUIRED}
    except ImportError:
        pass
    url = f"{WS_URL}?api_key={load_secrets()['api_key']}&access_token={session_info()['access_token']}"
    ws = websocket.create_connection(url, timeout=15, sslopt=sslopt, header=["X-Kite-Version: 3"])
    ws.settimeout(1.0)
    if start_at:                                           # start together with the Mac's TrueData count
        wait = start_at - time.time()
        if 0 < wait < 300:
            log(f"Starting in {wait:.0f} s, together with the TrueData count")
            time.sleep(wait)
    log("Counting Zerodha ticks for " + ", ".join(tokens) + f" for {seconds:.0f} seconds…")
    try:
        arr, tr = count(ws, tokens, seconds)
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    rows = summarise({k: arr.get(k, []) for k in tokens}, tr, seconds)
    for r in rows:
        log(f"{r['symbol']:<16}{r['per_second']:>8}/s  max {r['most_in_one_second']}/s  2+ in {r['share_2_or_more_pct']}% of seconds")
    return {"time": datetime.now().isoformat(timespec="seconds"), "seconds": seconds, "source": "zerodha",
            "symbols": list(tokens), "results": rows}


def process() -> int:
    """Run the Zerodha tick counts the dashboard asked for (runtime/tick-test-queue), one by one. Returns how many."""
    from .backtest_queue import ID_OK, _status, run_kite_ticks
    from .config import RUNTIME
    queue = RUNTIME / "tick-test-queue"
    queue.mkdir(parents=True, exist_ok=True)
    done = 0
    for path in sorted(queue.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job = {}
        path.unlink(missing_ok=True)
        if not ID_OK.match(path.stem):
            continue
        job = {**job, "id": path.stem}
        _status(job, status="running", started=datetime.now().isoformat(timespec="seconds"), message="")
        try:
            _status(job, status="done", finished=datetime.now().isoformat(timespec="seconds"), **run_kite_ticks(job))
        except Exception as e:  # noqa: BLE001  shown on the page
            _status(job, status="failed", finished=datetime.now().isoformat(timespec="seconds"), message=f"{type(e).__name__}: {e}"[:400])
        done += 1
    return done
