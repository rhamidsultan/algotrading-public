"""Orders & stops: one set of stop rules for every strategy, the same in backtests, paper, shadow and live.

Settings (a dict, saved with each session, backtest and sweep; every value 0 = off):

  stops              own        the strategy's own stops only (as before; the rules below are not used)
                     framework  the rules below only (the strategy's own stops are switched off: STOP_OFF)
                     both       both: whichever is hit first
  Per leg (each option, on its own price; leg_unit "pct" of the entry price or "points")
    leg_stop           a stop this far against the leg from its entry
    leg_trail_every    trailing: for every this much the leg gains (from its entry, once leg_trail_after is reached)…
    leg_trail_by       …move the stop this much in its favour (0 = the same as leg_trail_every)
    leg_trail_after    trailing starts once the leg has gained this much (0 = at once)
    leg_trail_gap      the stop never comes closer than this to the leg's best price (0 = no limit)
                       A stop only ever moves in the leg's favour. Sold legs: the stop is above the price and trails
                       down as the price falls. Bought legs: below, trailing up.
  Whole position (a cycle: the legs a strategy opened together, its "group"), in Rs after charges
    cycle_loss         close the cycle at this loss
    cycle_target       close the cycle at this profit
    cycle_lock_after   profit lock: once the cycle has made this much…
    cycle_giveback     …close it if it gives back this much from its best
  Day, in Rs (the account's change since the day's first price)
    day_loss, day_profit   close everything and stop for the day
  reenter_after_stop   after a cycle stop, the strategy may open a new cycle the same day (day limits always end the day)
  Orders (live and shadow): order_type limit_chase | market, chase_seconds, max_chase_pct, protect_mult

A strategy lists its own stop settings and the values that switch them off in STOP_OFF.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "stops": "own",
    "leg_unit": "pct", "leg_stop": 0.0, "leg_trail_every": 0.0, "leg_trail_by": 0.0, "leg_trail_after": 0.0,
    "leg_trail_gap": 0.0,
    "cycle_loss": 0.0, "cycle_target": 0.0, "cycle_lock_after": 0.0, "cycle_giveback": 0.0,
    "day_loss": 0.0, "day_profit": 0.0, "reenter_after_stop": False,
    "order_type": "limit_chase", "chase_seconds": 30, "max_chase_pct": 5.0, "protect_mult": 3.0,
}
CHOICES = {"stops": ("own", "framework", "both"), "leg_unit": ("pct", "points"), "order_type": ("limit_chase", "market")}
LABELS = {
    "stops": "Stops: the strategy's own, these rules, or both",
    "leg_unit": "Leg values in % of the entry price or in points",
    "leg_stop": "Leg stop: this far against each leg from its entry",
    "leg_trail_every": "Trailing: for every this much a leg gains…",
    "leg_trail_by": "…move its stop this much (0 = the same)",
    "leg_trail_after": "Trailing starts once a leg has gained this much (0 = at once)",
    "leg_trail_gap": "The stop stays at least this far from the leg's best price (0 = no limit)",
    "cycle_loss": "Close the whole position at this loss (Rs)",
    "cycle_target": "Close the whole position at this profit (Rs)",
    "cycle_lock_after": "Profit lock: once the position has made this much (Rs)…",
    "cycle_giveback": "…close it if it gives back this much (Rs)",
    "day_loss": "Stop for the day at this loss (Rs)",
    "day_profit": "Stop for the day at this profit (Rs)",
    "reenter_after_stop": "After a position stop, allow a new entry the same day",
    "order_type": "Orders: limit at the best price, re-priced (chase), or market",
    "chase_seconds": "Re-price a limit order for up to this many seconds",
    "max_chase_pct": "…but not more than this % from the first price",
    "protect_mult": "Sold options: protective stop at Zerodha at this × the sold price (0 = none)",
}
STOP_KEYS = [k for k in DEFAULTS if k.startswith(("leg_", "cycle_", "day_"))] + ["reenter_after_stop"]
ORDER_KEYS = ["order_type", "chase_seconds", "max_chase_pct", "protect_mult"]


def settings(raw: dict | None) -> dict:
    """Clean settings: known keys only, typed like the defaults, choices checked."""
    out = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in DEFAULTS or v is None or v == "":
            continue
        d = DEFAULTS[k]
        if isinstance(d, bool):
            out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(d, (int, float)):
            try:
                out[k] = type(d)(float(v))
            except (TypeError, ValueError):
                raise ValueError(f"'{LABELS.get(k, k)}' needs a number")
            if out[k] < 0:
                raise ValueError(f"'{LABELS.get(k, k)}' cannot be negative")
        else:
            if k in CHOICES and str(v) not in CHOICES[k]:
                raise ValueError(f"'{LABELS.get(k, k)}' must be one of {', '.join(CHOICES[k])}")
            out[k] = str(v)
    return out


def active(s: dict) -> bool:
    """Are any of the framework's stops in use?"""
    return s.get("stops", "own") != "own" and any(s.get(k) for k in STOP_KEYS if k != "reenter_after_stop" and k != "leg_unit")


def strategy_params(cls, params: dict, s: dict | None) -> dict:
    """The strategy's settings with its own stops switched off when the framework's replace them."""
    if not s or s.get("stops") != "framework":
        return dict(params or {})
    return {**(params or {}), **getattr(cls, "STOP_OFF", {})}


def leg_stop_level(s: dict, qty: int, entry: float, best: float, prev: float | None = None) -> float | None:
    """The stop for one leg: qty > 0 bought (stop below), < 0 sold (stop above). best = the best price since entry
    (highest for bought, lowest for sold). prev = the stop so far (it never moves against the leg)."""
    if entry <= 0 or not qty:
        return prev
    pts = (lambda x: x) if s["leg_unit"] == "points" else (lambda x: entry * x / 100.0)
    sign = 1 if qty > 0 else -1                       # +1: gains when the price rises
    level = None
    if s["leg_stop"] > 0:
        level = entry - sign * pts(s["leg_stop"])
    if s["leg_trail_every"] > 0:
        gain = (best - entry) * sign
        after = pts(s["leg_trail_after"])
        if gain >= after:
            every, by = pts(s["leg_trail_every"]), pts(s["leg_trail_by"] or s["leg_trail_every"])
            steps = math.floor((gain - after) / every + 1e-9)
            base = level if level is not None else entry - sign * every
            trailed = base + sign * steps * by
            if s["leg_trail_gap"] > 0:
                cap = best - sign * pts(s["leg_trail_gap"])
                trailed = min(trailed, cap) if sign > 0 else max(trailed, cap)
            level = trailed if level is None else (max(level, trailed) if sign > 0 else min(level, trailed))
    if level is None:
        return prev
    level = round(level, 2)
    if prev is not None:
        level = max(level, prev) if sign > 0 else min(level, prev)
    return level


class RiskManager:
    """Applies the stops each moment, before the strategy acts. `check(ctx)` returns True when the strategy should be
    skipped now (stopped for the day, or after a position stop until the next day)."""

    def __init__(self, raw: dict | None):
        self.s = settings(raw)
        self.on = active(self.s)
        self.legs: dict = {}          # contract label -> {"best", "stop", "entry", "qty"}
        self.cycles: dict = {}        # group -> best P&L
        self.day = ""
        self.day_start = None
        self.paused = ""              # a date: the strategy rests (flat) until the next day

    def get_state(self) -> dict:
        return {"legs": self.legs, "cycles": self.cycles, "day": self.day, "day_start": self.day_start, "paused": self.paused}

    def set_state(self, st: dict | None) -> None:
        st = st or {}
        self.legs, self.cycles = dict(st.get("legs") or {}), dict(st.get("cycles") or {})
        self.day, self.day_start, self.paused = st.get("day", ""), st.get("day_start"), st.get("paused", "")

    def status(self) -> dict:
        """For the session card: each leg's current stop."""
        return {k: v["stop"] for k, v in self.legs.items() if v.get("stop") is not None}

    # ---- one moment --------------------------------------------------------------------
    def check(self, ctx) -> bool:
        if not self.on:
            return False
        today = ctx.now.date().isoformat()
        if today != self.day:
            self.day, self.day_start, self.cycles = today, ctx.equity, {}
            if self.paused and self.paused < today:
                self.paused = ""
        positions = ctx.broker.positions
        self._legs(ctx, positions)
        self._cycles(ctx)
        s = self.s
        if (s["day_loss"] > 0 or s["day_profit"] > 0) and self.day_start is not None:
            day = ctx.equity - self.day_start
            hit = ("Day loss limit" if s["day_loss"] > 0 and day <= -s["day_loss"] else
                   "Day profit target" if s["day_profit"] > 0 and day >= s["day_profit"] else "")
            if hit and self.paused != today:
                if ctx.positions:
                    ctx.close_all(f"{hit}: Rs {day:,.0f} today")
                ctx.log(f"{hit} reached (Rs {day:,.0f}): no new trades until tomorrow")
                self.paused = today
        return bool(self.paused == today and not ctx.positions)

    def _legs(self, ctx, positions) -> None:
        s = self.s
        use = s["leg_stop"] > 0 or s["leg_trail_every"] > 0
        seen = set()
        for c, p in list(positions.items()):
            key = c.label
            seen.add(key)
            if not use:
                continue
            px = ctx.price(c)
            if px is None or px <= 0:
                continue
            leg = self.legs.get(key)
            if leg is None or leg.get("qty") != p.qty or abs(leg.get("entry", 0) - p.avg_price) > 1e-6:
                leg = {"entry": p.avg_price, "qty": p.qty, "best": p.avg_price, "stop": None}
            leg["best"] = max(leg["best"], px) if p.qty > 0 else min(leg["best"], px)
            leg["stop"] = leg_stop_level(s, p.qty, p.avg_price, leg["best"], leg["stop"])
            self.legs[key] = leg
            st = leg["stop"]
            follow = getattr(ctx.broker, "follow_stop", None)       # live: the stop at Zerodha follows a sold leg's trail
            if follow is not None and p.qty < 0 and st is not None:
                follow(c, st, ctx.now)
            if st is not None and ((p.qty > 0 and px <= st) or (p.qty < 0 and px >= st)):
                trailed = st != leg_stop_level(s, p.qty, p.avg_price, p.avg_price)
                ctx.close(c, f"Leg {'trailing ' if trailed else ''}stop {st:.2f} (in at {p.avg_price:.2f}, "
                             f"{'high' if p.qty > 0 else 'low'} {leg['best']:.2f})")
                self.legs.pop(key, None)
                seen.discard(key)
        for k in [k for k in self.legs if k not in seen]:
            self.legs.pop(k, None)

    def _cycles(self, ctx) -> None:
        s = self.s
        if not (s["cycle_loss"] > 0 or s["cycle_target"] > 0 or (s["cycle_lock_after"] > 0 and s["cycle_giveback"] > 0)):
            return
        b = ctx.broker
        groups = {p.group for p in b.positions.values()}
        for g in groups:
            open_pnl = sum((ctx.price(c) - p.avg_price) * p.qty - p.entry_costs for c, p in b.positions.items() if p.group == g)
            booked = sum(t.net_pnl for t in b.trades if t.group == g)
            pnl = open_pnl + booked
            best = max(self.cycles.get(g, pnl), pnl)
            self.cycles[g] = best
            why = ("Position loss limit" if s["cycle_loss"] > 0 and pnl <= -s["cycle_loss"] else
                   "Position target" if s["cycle_target"] > 0 and pnl >= s["cycle_target"] else
                   "Profit lock" if s["cycle_lock_after"] > 0 and s["cycle_giveback"] > 0 and best >= s["cycle_lock_after"]
                   and pnl <= best - s["cycle_giveback"] else "")
            if not why:
                continue
            for c, p in list(b.positions.items()):
                if p.group == g:
                    ctx.close(c, f"{why}: Rs {pnl:,.0f} (best Rs {best:,.0f})")
            self.cycles.pop(g, None)
            if not s["reenter_after_stop"]:
                self.paused = ctx.now.date().isoformat()
                ctx.log(f"{why} on {g or 'the position'}: no new entry until tomorrow")


PREFIX = "risk."


def split(params: dict | None, risk: dict | None = None) -> tuple[dict, dict | None]:
    """Settings as the pages send them: strategy settings plus "risk.<key>" ones. Returns (strategy settings, risk
    settings or None), the "risk." ones added over `risk`."""
    own, r = {}, dict(risk or {})
    for k, v in (params or {}).items():
        if str(k).startswith(PREFIX):
            r[k[len(PREFIX):]] = v
        else:
            own[k] = v
    return own, (settings(r) if r else None)


def catalog_params(defaults: dict | None = None) -> list[dict]:
    """The settings for the pages (Start a session, Run, Sweep), as extra rows "risk.<key>" in group "risk"."""
    d = {**DEFAULTS, **(defaults or {})}
    out = []
    for k, v in DEFAULTS.items():
        out.append({"key": PREFIX + k, "label": LABELS.get(k, k), "default": d[k], "type": type(v).__name__,
                    "min": 0 if isinstance(v, (int, float)) and not isinstance(v, bool) else None,
                    "choices": list(CHOICES[k]) if k in CHOICES else None, "group": "risk"})
    return out
