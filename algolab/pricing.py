"""Black-Scholes option pricing, used when no recorded option price is available."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import numpy as np

_SQRT2 = math.sqrt(2.0)
EXPIRY_TIME = time(15, 30)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def year_fraction(now: datetime, expiry: date, overnight_share: float = 0.3) -> float:
    """Time to expiry in years, counted in trading time: each trading day is 1/252 of a year, of
    which `overnight_share` belongs to the gap before the open and the rest decays through the
    session. Weekends carry no time. This matches how option premium actually decays intraday
    far better than calendar time does."""
    today = now.date()
    if today > expiry:
        return 0.0
    close = datetime.combine(today, EXPIRY_TIME)
    minutes_left = min(max((close - now).total_seconds() / 60.0, 0.0), 375.0)
    today_part = (1.0 - overnight_share) * minutes_left / 375.0 if today.weekday() < 5 else 0.0
    future_days = int(np.busday_count(today + timedelta(days=1), expiry + timedelta(days=1)))
    return (today_part + future_days) / 252.0


def bs_price(spot: float, strike: float, t: float, sigma: float, right: str,
             r: float = 0.065, q: float = 0.0) -> float:
    intrinsic = max(spot - strike, 0.0) if right == "CE" else max(strike - spot, 0.0)
    if t <= 0 or sigma <= 0:
        return intrinsic
    vt = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / vt
    d2 = d1 - vt
    if right == "CE":
        return spot * math.exp(-q * t) * _cdf(d1) - strike * math.exp(-r * t) * _cdf(d2)
    return strike * math.exp(-r * t) * _cdf(-d2) - spot * math.exp(-q * t) * _cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float, right: str,
                r: float = 0.065, q: float = 0.0) -> float | None:
    if t <= 0 or price <= 0:
        return None
    lo, hi = 0.005, 4.0
    if not (bs_price(spot, strike, t, lo, right, r, q) <= price <= bs_price(spot, strike, t, hi, right, r, q)):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, right, r, q) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
