"""Estimated margin for backtests: what the open legs would block at the broker, without asking Zerodha.

Zerodha's basket calculator (algolab/margins.py) only prices today's legs, so a backtest estimates instead:

  bought options       the premium paid
  sold options         `span_pct` + `exposure_pct` of NIFTY x quantity for each sold unit (about what SPAN and the
                       exposure margin come to near the money; far out-of-the-money shorts need less, so this leans high)
  hedged (per expiry)  when the calls and the puts of an expiry are each not net short (every short has a long
                       behind it), the worst loss at expiry plus the exposure margin on the shorts, if that is less

Calendars (short near, long far) get no benefit here, so their estimate is high. Settings: account.margin in
config.yaml. The figure is a guide for return on margin and for "would the account have had the money".
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

DEFAULTS = {"span_pct": 6.0, "exposure_pct": 2.0, "check": "warn"}


def settings(cfg: dict | None) -> dict:
    return {**DEFAULTS, **(((cfg or {}).get("account") or {}).get("margin") or {})}


def estimate(legs, spot: float, cfg: dict | None = None) -> float:
    """legs: iterable of (Contract, signed qty, price now). Returns rupees blocked (0 when flat)."""
    s = settings(cfg)
    span, expo = float(s["span_pct"]) / 100, float(s["exposure_pct"]) / 100
    by_expiry = defaultdict(list)
    for c, qty, px in legs:
        if qty:
            by_expiry[c.expiry].append((c, int(qty), float(px or 0.0)))
    total = 0.0
    grid = np.linspace(0.6, 1.4, 161) * spot
    for group in by_expiry.values():
        paid = sum(q * px for _, q, px in group if q > 0)
        short_units = sum(-q for _, q, _ in group if q < 0)
        naked = paid + short_units * spot * (span + expo)
        net_ce = sum(q for c, q, _ in group if c.right == "CE")
        net_pe = sum(q for c, q, _ in group if c.right == "PE")
        best = naked
        if short_units and net_ce >= 0 and net_pe >= 0:      # every short is covered: the loss is limited
            value = np.zeros_like(grid)
            for c, q, px in group:
                intrinsic = np.maximum(grid - c.strike, 0) if c.right == "CE" else np.maximum(c.strike - grid, 0)
                value += q * (intrinsic - px)
            hedged = max(0.0, -float(value.min())) + short_units * spot * expo
            best = min(naked, hedged)
        total += best
    return round(total, 0)


def positions_margin(broker, quote, spot: float, cfg: dict | None = None, extra=None) -> float:
    """The estimate for a broker's open positions (plus `extra` = (Contract, signed qty) about to be ordered)."""
    held = {c: p.qty for c, p in broker.positions.items()}
    if extra is not None:
        c, q = extra
        held[c] = held.get(c, 0) + q
    legs = []
    for c, q in held.items():
        if not q:
            continue
        try:
            px = quote(c).last
        except Exception:  # an unpriced leg: count it at its average price
            px = broker.positions[c].avg_price if c in broker.positions else 0.0
        legs.append((c, q, px if px is not None else 0.0))
    return estimate(legs, spot, cfg)
