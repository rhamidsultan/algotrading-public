"""Margin each strategy needs, and the margin each session's open legs would block, from Zerodha.

For every strategy, its own entry rules are run once at today's prices (default settings, 1 lot, or 1
unit for the Blaze Butterfly) on a throw-away account; the resulting legs are sent to Zerodha's basket
margin calculator, which applies the spread / hedge benefit exactly as it would for real orders
(product NRML). The legs of each session's open positions are priced the same way. Nothing is ordered.

Written to runtime/margins.json, which the dashboard shows on the strategy cards, the sessions and the
Summary page.

  python -m algolab.margins           compute now
  python -m algolab.margins --auto    (systemd timer, every 5 minutes) only when due: every 30 minutes
                                      in market hours, when the dashboard asks (runtime/margins_request),
                                      or when the file is missing or more than 18 hours old
"""
from __future__ import annotations

import json
import sys
import time as _time
from datetime import date, time

from .config import RUNTIME, ist_now
from .models import Contract

OUT = RUNTIME / "margins.json"
REQUEST = RUNTIME / "margins_request"
ATTEMPT = RUNTIME / "margins_attempt"
MARKET = (time(9, 20), time(15, 30))


# ---- the legs a strategy would open now -------------------------------------------------------------
def _expiry_in(ctx, lo: int, hi: int):
    today = ctx.now.date()
    ex = ctx.expiries()
    return (next((e for e in ex if lo <= (e - today).days <= hi), None)
            or next((e for e in ex if (e - today).days >= lo), None))


def enter_now(strategy, ctx) -> str:
    """Make `strategy` open its entry legs on `ctx` right now (ignoring its entry time and filters).
    Returns what one 'unit' of the margin means."""
    from .strategies._common import snap
    p, name = strategy.p, strategy.name
    if name == "blaze_butterfly":
        ahead = ctx.expiries()
        if len(ahead) < 2:
            raise ValueError("next week's expiry not listed")
        expiry, spot = ahead[1], ctx.spot
        lo = int(spot / 50) * 50
        a = lo + 50 if spot - lo > 25 else lo
        r = a - 50 if a % 100 == 50 else a
        k1, k2, k3 = r - p.upper_off, r - p.short_off, r - p.lower_off
        ctx.buy(ctx.option(expiry, k1, "PE"), p.multiplier, "Margin check")
        ctx.buy(ctx.option(expiry, k3, "PE"), p.multiplier, "Margin check")
        ctx.sell(ctx.option(expiry, k2, "PE"), 2 * p.multiplier, "Margin check")
        return f"{p.multiplier} unit (1 / 2 / 1 lots)"
    if name == "double_calendar":
        front = _expiry_in(ctx, p.front_min_dte, p.front_max_dte)
        ex = ctx.expiries()
        if front is None or ex.index(front) + p.back_weeks >= len(ex):
            raise ValueError("expiries not listed")
        strategy._enter(ctx, front, ex[ex.index(front) + p.back_weeks])
        return f"{p.lots} lot per leg"
    if name in ("intraday_straddle", "intraday_strangle"):
        expiry = ctx.pick_expiry(getattr(p, "min_dte", 0))
        atm, off = ctx.atm_strike(), getattr(p, "offset", 0)
        ctx.sell(ctx.option(expiry, snap(ctx, expiry, "CE", atm + off, +1), "CE"), p.lots, "Margin check")
        ctx.sell(ctx.option(expiry, snap(ctx, expiry, "PE", atm - off, -1), "PE"), p.lots, "Margin check")
        return f"{p.lots} lot per leg"
    if name == "expiry_doubler":            # buys only: the margin is the premium of the option it would buy
        expiry = ctx.pick_expiry(0)
        if expiry is None:
            raise ValueError("no expiry listed")
        right = "PE" if p.sides == "PE" else "CE"
        k, _ = strategy._pick(ctx, expiry, right)
        if k is None:
            raise ValueError(f"no {right} priced under Rs {p.max_premium:g}")
        ctx.buy(ctx.option(expiry, k, right), p.lots, "Margin check")
        return f"{p.lots} lot, one option bought (the premium)"
    if name == "open_high_retest":          # buys only: the premium of an at-the-money option
        expiry = ctx.pick_expiry(getattr(p, "min_dte", 0))
        if expiry is None:
            raise ValueError("no expiry listed")
        right = "PE" if p.sides == "PE" else "CE"
        ctx.buy(ctx.option(expiry, snap(ctx, expiry, right, ctx.atm_strike()), right), p.lots, "Margin check")
        return f"{p.lots} lot, one option bought (the premium)"
    if name == "opening_move_itm":          # buys only: the premium of the in-the-money call it would buy
        expiry = ctx.pick_expiry(getattr(p, "min_dte", 0))
        if expiry is None:
            raise ValueError("no expiry listed")
        k = snap(ctx, expiry, "CE", ctx.atm_strike() - p.itm_points, -1)
        ctx.buy(ctx.option(expiry, k, "CE"), p.lots, "Margin check")
        return f"{p.lots} lot, one in-the-money option bought (the premium)"
    if not hasattr(strategy, "_enter"):
        raise ValueError("no entry rule to check")
    expiry = _expiry_in(ctx, getattr(p, "min_dte", getattr(p, "min_dte_after", 1)), getattr(p, "max_dte", 14))
    if expiry is None:
        raise ValueError("no expiry listed")
    if name == "event_iron_fly":            # the iron fly it opens the day before an event
        from .strategies.iron_fly import IronFly
        IronFly._enter(strategy, ctx, expiry)
    else:
        strategy._enter(ctx, expiry)
    return f"{getattr(p, 'lots', 1)} lot per leg"


# ---- Zerodha's basket margin ------------------------------------------------------------------------
def basket_margin(kite, symbols: dict, legs) -> dict:
    """legs: [(Contract, signed qty)]. Zerodha's margin for opening them together (NRML)."""
    orders = []
    for c, qty in sorted(legs, key=lambda x: x[1] < 0):          # bought legs first (hedges)
        sym = symbols.get((c.expiry, c.strike, c.right))
        if not sym:
            raise ValueError(f"{c.label} is not listed")
        orders.append({"exchange": "NFO", "tradingsymbol": sym, "transaction_type": "BUY" if qty > 0 else "SELL",
                       "variety": "regular", "product": "NRML", "order_type": "MARKET", "quantity": abs(int(qty)),
                       "price": 0, "trigger_price": 0})
    r = kite.basket_order_margins(orders, consider_positions=False)
    final, initial = r.get("final") or {}, r.get("initial") or {}
    return {"margin": round(float(final.get("total") or 0), 2), "span": round(float(final.get("span") or 0), 2),
            "exposure": round(float(final.get("exposure") or 0), 2),
            "premium": round(float(final.get("option_premium") or 0), 2),
            "without_hedge": round(float(initial.get("total") or 0), 2)}


def _leg_text(c: Contract, qty: int, price: float | None) -> dict:
    return {"text": f"{'BUY' if qty > 0 else 'SELL'} {c.strike} {c.right} {c.expiry:%d %b}", "qty": abs(int(qty)),
            "price": round(price, 2) if price is not None else None}


# ---- everything --------------------------------------------------------------------------------------
def compute(cfg: dict | None = None, kite=None, log=print) -> dict:
    from .config import load_config
    from .engine import Context, make_broker
    from .kite import SPOT_SYMBOL, VIX_SYMBOL, KiteFeed, client
    from .paper import SESSIONS
    from .strategy import load_strategies
    cfg = cfg or load_config()
    kite = kite or client()
    feed = KiteFeed(kite, cfg, record=False, log=lambda m: None)
    feed._load_chain()
    q = kite.quote([SPOT_SYMBOL, VIX_SYMBOL])
    feed.now, feed.spot = ist_now(), float(q[SPOT_SYMBOL]["last_price"])
    feed.vix = (q.get(VIX_SYMBOL) or {}).get("last_price")
    out = {"time": f"{ist_now():%Y-%m-%d %H:%M}", "spot": feed.spot, "vix": feed.vix, "lot_size": feed.lot_size,
           "product": "NRML", "strategies": {}, "sessions": {}}
    for name, cls in load_strategies().items():
        try:
            strategy = cls()
            broker = make_broker(cfg)
            ctx = Context(feed, broker, [])
            unit = enter_now(strategy, ctx)
            legs = [(c, pos.qty) for c, pos in broker.positions.items()]
            if not legs:
                out["strategies"][name] = {"title": cls.title, "error": "would not enter at today's prices"}
                continue
            m = basket_margin(kite, feed._symbols, legs)
            credit = -sum(pos.qty * pos.avg_price for pos in broker.positions.values())
            out["strategies"][name] = {"title": cls.title, "unit": unit, **m, "net_credit": round(credit, 2),
                                       "legs": [_leg_text(c, pos.qty, pos.avg_price) for c, pos in broker.positions.items()]}
        except Exception as e:  # noqa: BLE001 - one strategy must not stop the others
            out["strategies"][name] = {"title": getattr(cls, "title", name), "error": f"{type(e).__name__}: {e}"[:200]}
        _time.sleep(0.4)
    if SESSIONS.is_dir():
        for d in sorted(SESSIONS.iterdir()):
            try:
                st = json.loads((d / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pos = (st.get("broker") or {}).get("positions") or []
            if not pos:
                continue
            try:
                legs = [(Contract.from_dict(x["contract"]), int(x["qty"])) for x in pos]
                legs = [(c, n) for c, n in legs if c.expiry >= ist_now().date()]
                if legs:
                    out["sessions"][d.name] = {**basket_margin(kite, feed._symbols, legs), "legs": len(legs)}
            except Exception as e:  # noqa: BLE001
                out["sessions"][d.name] = {"error": f"{type(e).__name__}: {e}"[:200]}
            _time.sleep(0.4)
    return out


def write(data: dict) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
    tmp.replace(OUT)


def due(now=None) -> str:
    """Why a refresh is due now ('' = not due)."""
    now = now or ist_now()
    if REQUEST.exists():
        return "asked from the dashboard"
    try:
        age = _time.time() - OUT.stat().st_mtime
    except OSError:
        age = None
    try:
        tried = _time.time() - ATTEMPT.stat().st_mtime
    except OSError:
        tried = 1e9
    if now.weekday() < 5 and MARKET[0] <= now.time() <= MARKET[1] and (age is None or age >= 30 * 60) and tried >= 25 * 60:
        return "market hours"
    if (age is None or age > 18 * 3600) and tried >= 60 * 60:
        return "no recent figures"
    return ""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    why = due() if "--auto" in argv else "asked"
    if not why:
        return 0
    RUNTIME.mkdir(parents=True, exist_ok=True)
    ATTEMPT.write_text(f"{ist_now():%Y-%m-%d %H:%M}", encoding="utf-8")
    REQUEST.unlink(missing_ok=True)
    try:
        data = compute()
    except Exception as e:  # noqa: BLE001 - usually: no Zerodha login today
        old = {}
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        old["error"] = f"{ist_now():%d %b %H:%M}: {type(e).__name__}: {e}"[:240]
        write(old)
        print(f"Margins not updated ({why}): {e}")
        return 1
    write(data)
    ok = sum(1 for s in data["strategies"].values() if "margin" in s)
    print(f"Margins updated ({why}): {ok} strategies, {len(data['sessions'])} sessions with open legs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
