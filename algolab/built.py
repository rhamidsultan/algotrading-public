"""Strategies made in Algo Studio's Strategy Developer: a saved spec (JSON), not code, run by one engine strategy.

Each spec in runtime/built_strategies/<name>.json becomes a strategy of its own (name "b_<slug>"), listed with the
built-in ones: backtests, sweeps, paper, shadow and live run it exactly like any other strategy, and Orders & stops
apply as usual.

  entry     days: every trading day | expiry day only | N days before the nearest expiry
            from / until: entries allowed between these times (IST)
            condition: none (enter at "from") | a leg's price reaches X times its price at "from" (each leg on its own)
                       | NIFTY moves X points from its level at "from" (up, down or either) | VIX between A and B
  legs      buy or sell, call or put, expiry (nearest, next, monthly), strike by: ATM ± steps (+ = out of the money),
            premium nearest ₹X (out of the money), or X points from spot; lots per leg
  re-entry  after a leg's stop: enter that leg again when its price is back at its first entry price, up to N entries
  exits     square-off time; hold intraday or to the expiry day; stop per leg (% or points against its entry);
            day loss / day profit in ₹ (0 = off)

The numbers a trader tunes are settings (PARAMS), so presets, sweeps and Find best work on built strategies too.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, time
from pathlib import Path

from .config import RUNTIME
from .strategy import Param, Strategy, parse_time

PREFIX = "b_"
MAX_LEGS = 6


def spec_dir() -> Path:
    return Path(os.environ.get("BUILT_STRATEGIES") or (RUNTIME / "built_strategies"))


# ---- the spec ----------------------------------------------------------------------------------------------------
TEMPLATES = {
    "blank": {"title": "", "legs": []},
    "straddle": {"title": "Short straddle", "legs": [{"side": "sell", "right": "CE", "strike_by": "atm", "steps": 0},
                                                     {"side": "sell", "right": "PE", "strike_by": "atm", "steps": 0}]},
    "strangle": {"title": "Short strangle", "legs": [{"side": "sell", "right": "CE", "strike_by": "atm", "steps": 4},
                                                     {"side": "sell", "right": "PE", "strike_by": "atm", "steps": 4}]},
    "iron_fly": {"title": "Iron fly", "legs": [{"side": "sell", "right": "CE", "strike_by": "atm", "steps": 0},
                                               {"side": "sell", "right": "PE", "strike_by": "atm", "steps": 0},
                                               {"side": "buy", "right": "CE", "strike_by": "atm", "steps": 6},
                                               {"side": "buy", "right": "PE", "strike_by": "atm", "steps": 6}]},
    "spread": {"title": "Put credit spread", "legs": [{"side": "sell", "right": "PE", "strike_by": "atm", "steps": 2},
                                                      {"side": "buy", "right": "PE", "strike_by": "atm", "steps": 6}]},
    "buy_move": {"title": "Buy on a move", "entry": {"days": "expiry", "from": "11:00", "until": "14:45",
                                                     "condition": {"kind": "leg_multiple", "x": 2.0}},
                 "legs": [{"side": "buy", "right": "CE", "strike_by": "premium", "premium": 30},
                          {"side": "buy", "right": "PE", "strike_by": "premium", "premium": 30}],
                 "exit": {"leg_stop": 20, "leg_stop_unit": "pts"}, "reentry": {"on": True, "max_entries": 3}},
}

ENTRY_DEFAULT = {"days": "every", "dte": 0, "from": "09:20", "until": "14:00",
                 "condition": {"kind": "none", "x": 2.0, "points": 100, "dir": "either", "vix_min": 0, "vix_max": 0}}
EXIT_DEFAULT = {"time": "15:15", "hold": "intraday", "leg_stop": 0, "leg_stop_unit": "pct", "day_stop": 0, "day_target": 0}
LEG_DEFAULT = {"side": "sell", "right": "CE", "expiry": "nearest", "strike_by": "atm", "steps": 0, "premium": 50,
               "points": 200, "lots": 1}


def slug(title: str) -> str:
    return PREFIX + (re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")[:40] or "strategy")


def _time(v, what) -> str:
    try:
        t = parse_time(v)
    except (ValueError, TypeError):
        raise ValueError(f"{what}: a time like 09:20.") from None
    if not (time(9, 15) <= t <= time(15, 30)):
        raise ValueError(f"{what}: between 09:15 and 15:30.")
    return f"{t:%H:%M}"


def _num(v, what, lo=None, hi=None, kind=float):
    try:
        x = kind(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"{what}: a number.") from None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError(f"{what}: between {lo:g} and {hi:g}." if hi is not None else f"{what}: at least {lo:g}.")
    return x


def _pick(v, what, options):
    if v not in options:
        raise ValueError(f"{what}: one of {', '.join(options)}.")
    return v


def validate(spec: dict) -> dict:
    """A clean spec, or ValueError saying what to fix (in words for the page)."""
    if not isinstance(spec, dict):
        raise ValueError("Expected a strategy.")
    title = re.sub(r"\s+", " ", str(spec.get("title") or "")).strip()[:60]
    if len(title) < 3:
        raise ValueError("Give the strategy a name (3 characters or more).")
    desc = re.sub(r"\s+", " ", str(spec.get("description") or "")).strip()[:400]
    e = {**ENTRY_DEFAULT, **(spec.get("entry") or {})}
    c = {**ENTRY_DEFAULT["condition"], **(e.get("condition") or {})}
    entry = {"days": _pick(e["days"], "Entry days", ("every", "expiry", "dte")),
             "dte": int(_num(e.get("dte", 0), "Days before expiry", 0, 30)),
             "from": _time(e["from"], "Entries from"), "until": _time(e["until"], "No new entries after")}
    if entry["until"] <= entry["from"]:
        raise ValueError("No new entries after: later than Entries from.")
    cond = {"kind": _pick(c["kind"], "Condition", ("none", "leg_multiple", "spot_move", "vix"))}
    if cond["kind"] == "leg_multiple":
        cond["x"] = _num(c.get("x"), "Leg price multiple", 0.1, 20)
        if abs(cond["x"] - 1) < 0.05:
            raise ValueError("Leg price multiple: not 1 (e.g. 2 = doubles, 0.5 = halves).")
    elif cond["kind"] == "spot_move":
        cond["points"] = _num(c.get("points"), "NIFTY move (points)", 1, 5000)
        cond["dir"] = _pick(c.get("dir", "either"), "Direction", ("up", "down", "either"))
    elif cond["kind"] == "vix":
        cond["vix_min"], cond["vix_max"] = _num(c.get("vix_min") or 0, "VIX at least", 0, 100), _num(c.get("vix_max") or 0, "VIX at most", 0, 100)
        if cond["vix_max"] and cond["vix_max"] < cond["vix_min"]:
            raise ValueError("VIX at most: not below VIX at least.")
    entry["condition"] = cond
    legs = []
    raw = spec.get("legs") or []
    if not isinstance(raw, list) or not raw:
        raise ValueError("Add at least one leg.")
    if len(raw) > MAX_LEGS:
        raise ValueError(f"At most {MAX_LEGS} legs.")
    for i, lg in enumerate(raw, 1):
        lg = {**LEG_DEFAULT, **(lg or {})}
        w = f"Leg {i}"
        leg = {"side": _pick(lg["side"], f"{w} buy or sell", ("buy", "sell")), "right": _pick(lg["right"], f"{w} call or put", ("CE", "PE")),
               "expiry": _pick(lg["expiry"], f"{w} expiry", ("nearest", "next", "monthly")),
               "strike_by": _pick(lg["strike_by"], f"{w} strike by", ("atm", "premium", "points")),
               "lots": int(_num(lg["lots"], f"{w} lots", 1, 50))}
        if leg["strike_by"] == "atm":
            leg["steps"] = int(_num(lg["steps"], f"{w} steps from ATM", -40, 40))
        elif leg["strike_by"] == "premium":
            leg["premium"] = _num(lg["premium"], f"{w} premium (₹)", 0.5, 5000)
        else:
            leg["points"] = int(_num(lg["points"], f"{w} points from spot", -5000, 5000))
        legs.append(leg)
    x = {**EXIT_DEFAULT, **(spec.get("exit") or {})}
    ex = {"time": _time(x["time"], "Square-off"), "hold": _pick(x["hold"], "Hold", ("intraday", "expiry")),
          "leg_stop": _num(x["leg_stop"] or 0, "Stop per leg", 0, 100000),
          "leg_stop_unit": _pick(x["leg_stop_unit"], "Stop per leg in", ("pct", "pts")),
          "day_stop": _num(x["day_stop"] or 0, "Day loss (₹)", 0, 10_000_000), "day_target": _num(x["day_target"] or 0, "Day profit (₹)", 0, 10_000_000)}
    if ex["hold"] == "intraday" and ex["time"] <= entry["from"]:
        raise ValueError("Square-off: later than Entries from.")
    r = spec.get("reentry") or {}
    re_ = {"on": bool(r.get("on")), "max_entries": int(_num(r.get("max_entries", 2), "Entries per leg", 1, 10))}
    return {"v": 1, "title": title, "description": desc, "entry": entry, "legs": legs, "exit": ex, "reentry": re_,
            "name": str(spec.get("name") or "") or slug(title)}


def describe(s: dict) -> str:
    e, x, c = s["entry"], s["exit"], s["entry"]["condition"]
    days = {"every": "Every trading day", "expiry": "Expiry days only", "dte": f"{e['dte']} days before expiry"}[e["days"]]
    cond = {"none": f"enters at {e['from']}", "leg_multiple": f"from {e['from']}, enters a leg when its price is {c.get('x', 0):g}x its price then",
            "spot_move": f"from {e['from']}, enters when NIFTY moves {c.get('points', 0):g} points ({c.get('dir')})",
            "vix": f"enters at {e['from']} if VIX is {c.get('vix_min', 0):g}–{c.get('vix_max', 0) or '∞'}"}[c["kind"]]

    def leg(lg):
        k = {"atm": (f"ATM{lg['steps']:+d} steps" if lg.get("steps") else "ATM"), "premium": f"premium near ₹{lg.get('premium', 0):g}",
             "points": f"{lg.get('points', 0):+d} pts"}[lg["strike_by"]]
        return f"{lg['side']} {lg['lots']} {lg['right']} {k} ({lg['expiry']})"
    stop = f"; stop {x['leg_stop']:g}{'%' if x['leg_stop_unit'] == 'pct' else ' pts'} per leg" if x["leg_stop"] else ""
    return (f"{days}: {cond}. Legs: " + ", ".join(leg(lg) for lg in s["legs"]) +
            f". {'Square-off' if x['hold'] == 'intraday' else 'Held to the expiry day, square-off'} at {x['time']}{stop}"
            + (f"; re-enter a stopped leg at its entry price, up to {s['reentry']['max_entries']} entries" if s["reentry"]["on"] else "") + ".")


# ---- saving and loading ------------------------------------------------------------------------------------------
def load_specs() -> dict:
    out = {}
    d = spec_dir()
    for f in sorted(d.glob(f"{PREFIX}*.json")) if d.is_dir() else []:
        try:
            s = validate(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        s["name"] = f.stem
        out[f.stem] = s
    return out


def save_spec(spec: dict, name: str | None = None) -> dict:
    s = validate({**spec, "name": name or spec.get("name") or ""})
    if not re.fullmatch(PREFIX + r"[a-z0-9_]{1,40}", s["name"]):
        s["name"] = slug(s["title"])
    if not name:                                                   # new: a free name
        base, n = s["name"], 2
        while (spec_dir() / f"{s['name']}.json").exists():
            s["name"], n = f"{base}_{n}", n + 1
    d = spec_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{s['name']}.tmp"
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    os.replace(tmp, d / f"{s['name']}.json")
    return s


def delete_spec(name: str) -> None:
    f = spec_dir() / f"{name}.json"
    if not re.fullmatch(PREFIX + r"[a-z0-9_]{1,44}", name) or not f.exists():
        raise LookupError("No such strategy.")
    f.unlink()


def classes() -> dict:
    """One Strategy class per saved spec (algolab/strategy.py load_strategies adds them)."""
    out = {}
    for name, s in load_specs().items():
        out[name] = type(f"Built_{name}", (BuiltStrategy,), {
            "name": name, "title": s["title"], "description": s["description"] or describe(s), "SPEC": s,
            "PARAMS": params_for(s), "built": True})
    return out


def params_for(s: dict) -> dict:
    e, x = s["entry"], s["exit"]
    p = {"lots": Param(1, "Lots multiplier (each leg's lots × this)", min=1),
         "entry_from": Param(e["from"], "Entries from (IST, HH:MM)"),
         "entry_until": Param(e["until"], "No new entries after (IST, HH:MM)"),
         "exit_time": Param(x["time"], "Square-off (IST, HH:MM)"),
         "leg_stop": Param(float(x["leg_stop"]), "Stop per leg against its entry (0 = off)", min=0.0),
         "leg_stop_unit": Param(x["leg_stop_unit"], "Stop per leg in", choices=["pct", "pts"]),
         "day_stop": Param(float(x["day_stop"]), "Close at this loss in ₹ (0 = off)", min=0.0),
         "day_target": Param(float(x["day_target"]), "Close at this profit in ₹ (0 = off)", min=0.0),
         "max_entries": Param(int(s["reentry"]["max_entries"] if s["reentry"]["on"] else 1), "Entries per leg (1 = no re-entry)", min=1)}
    c = e["condition"]
    if c["kind"] == "leg_multiple":
        p["trigger_x"] = Param(float(c["x"]), "Enter a leg at this many times its price at Entries from", min=0.1)
    elif c["kind"] == "spot_move":
        p["move_points"] = Param(float(c["points"]), "Enter when NIFTY moves this many points", min=1.0)
    elif c["kind"] == "vix":
        p["vix_min"] = Param(float(c["vix_min"]), "Enter only if VIX at least (0 = off)", min=0.0)
        p["vix_max"] = Param(float(c["vix_max"]), "Enter only if VIX at most (0 = off)", min=0.0)
    return p


# ---- the engine strategy that runs a spec ------------------------------------------------------------------------
class BuiltStrategy(Strategy):
    """Runs a Strategy Developer spec. Subclassed once per spec (classes()); never listed itself (no name)."""
    name = ""
    SPEC: dict = {}
    live_ready = False
    STOP_OFF = {"leg_stop": 0.0, "day_stop": 0.0, "day_target": 0.0}

    def __init__(self, **params):
        super().__init__(**params)
        self.cyc: dict | None = None
        self.count = 0

    def get_state(self) -> dict:
        return {"cyc": self.cyc, "count": self.count}

    def set_state(self, s: dict) -> None:
        self.cyc, self.count = s.get("cyc"), s.get("count", 0)

    # -- choosing contracts
    def _expiry(self, ctx, which: str):
        today = ctx.now.date()
        exps = [e for e in ctx.expiries() if e >= today]
        if not exps:
            return None
        if which == "next":
            return exps[1] if len(exps) > 1 else None
        if which == "monthly":
            for i, e in enumerate(exps):
                if i + 1 == len(exps) or exps[i + 1].month != e.month:
                    return e
        return exps[0]

    def _strike(self, ctx, expiry, leg) -> int | None:
        step, atm, right = ctx.strike_step, ctx.atm_strike(), leg["right"]
        out = 1 if right == "CE" else -1                       # out of the money: calls up, puts down
        if leg["strike_by"] == "atm":
            k = atm + out * leg.get("steps", 0) * step
        elif leg["strike_by"] == "points":
            k = int(round((ctx.spot + out * leg.get("points", 0)) / step) * step)
        else:
            cands = [atm + out * i * step for i in range(0, 41)]
            listed = ctx.listed_strikes(expiry, right)
            if listed:
                cands = [k for k in cands if k in listed]
            qs = ctx.quotes([ctx.option(expiry, k, right) for k in cands]) if cands else {}
            best = None
            for c, q in qs.items():
                px = getattr(q, "last", None)
                if px and px > 0 and (best is None or abs(px - leg["premium"]) < abs(best[1] - leg["premium"])):
                    best = (c.strike, px)
            return best[0] if best else None
        listed = ctx.listed_strikes(expiry, right)
        if listed and k not in listed:
            k = min(listed, key=lambda x: (abs(x - k), x))
        return int(k)

    def _contract(self, ctx, leg_state):
        return ctx.option(date.fromisoformat(leg_state["expiry"]), leg_state["strike"], leg_state["right"])

    # -- the day
    def _entry_day(self, ctx) -> bool:
        e = self.SPEC["entry"]
        nearest = self._expiry(ctx, "nearest")
        if nearest is None:
            return False
        dte = (nearest - ctx.now.date()).days
        return e["days"] == "every" or (e["days"] == "expiry" and dte == 0) or (e["days"] == "dte" and dte == e["dte"])

    def on_bar(self, ctx) -> None:
        p, now = self.p, ctx.now
        t, today = now.time(), now.date().isoformat()
        c = self.cyc
        hold = self.SPEC["exit"]["hold"]
        if c and not c.get("closed"):
            if hold == "intraday" and c["date"] != today:
                return self._close(ctx, "Square-off (left from an earlier day)")
            if hold == "expiry" and c.get("last_day") and today > c["last_day"]:
                return self._close(ctx, "Square-off (expiry passed)")
            square = t >= parse_time(p.exit_time) and (hold == "intraday" or today >= (c.get("last_day") or today))
            if square:
                return self._close(ctx, "Square-off")
            pnl = ctx.net_realized - c["start"] + ctx.unrealized
            if p.day_stop > 0 and pnl <= -p.day_stop:
                return self._close(ctx, f"Loss limit ₹{p.day_stop:,.0f}")
            if p.day_target > 0 and pnl >= p.day_target:
                return self._close(ctx, f"Profit target ₹{p.day_target:,.0f}")
            return self._legs(ctx, t)
        if c and c.get("date") == today:
            return                                                   # done for today
        if ctx.positions and hold == "intraday":
            for k in list(ctx.positions):
                ctx.close(k, "Square-off (left from an earlier day)")
        if parse_time(p.entry_from) <= t < parse_time(p.entry_until) and self._entry_day(ctx):
            self._start(ctx, today)

    def _start(self, ctx, today: str) -> None:
        p, cond = self.p, self.SPEC["entry"]["condition"]
        if cond["kind"] == "vix":
            v = ctx.vix
            if (p.vix_min and (not v or v < p.vix_min)) or (p.vix_max and v and v > p.vix_max):
                self.cyc = {"date": today, "closed": True, "skip": f"VIX {v or 0:.1f} outside {p.vix_min:g}–{p.vix_max or '∞'}"}
                ctx.log(f"No trade today: {self.cyc['skip']}")
                return
        legs = []
        for i, lg in enumerate(self.SPEC["legs"]):
            exp = self._expiry(ctx, lg["expiry"])
            k = self._strike(ctx, exp, lg) if exp else None
            if k is None:
                ctx.log(f"Leg {i + 1}: no {lg['right']} strike found; no trade today")
                self.cyc = {"date": today, "closed": True, "skip": "no strike"}
                return
            ref = ctx.price(ctx.option(exp, k, lg["right"]))
            legs.append({"i": i, "side": lg["side"], "right": lg["right"], "expiry": exp.isoformat(), "strike": k,
                         "lots": lg["lots"], "ref": ref, "open": False, "entries": 0, "first": None, "entry": None, "high": None})
        last_day = min(lg["expiry"] for lg in legs)
        self.count += 1
        self.cyc = {"date": today, "last_day": last_day, "group": f"{self.count:03d} | {ctx.now:%d %b %y}", "legs": legs,
                    "start": ctx.net_realized, "spot_ref": ctx.spot, "triggered": cond["kind"] in ("none", "vix")}
        ctx.log(f"Cycle {self.cyc['group']}: " + ", ".join(f"{x['side']} {x['strike']} {x['right']}" for x in legs)
                + (f" (waiting for the entry condition)" if not self.cyc["triggered"] else ""))
        self._legs(ctx, ctx.now.time())

    def _stop_hit(self, leg, px) -> bool:
        p = self.p
        if p.leg_stop <= 0 or leg["entry"] is None:
            return False
        dist = leg["entry"] * p.leg_stop / 100 if p.leg_stop_unit == "pct" else p.leg_stop
        return px >= leg["entry"] + dist if leg["side"] == "sell" else px <= leg["entry"] - dist

    def _legs(self, ctx, t) -> None:
        p, c, cond = self.p, self.cyc, self.SPEC["entry"]["condition"]
        can_enter = t < parse_time(p.entry_until)
        if not c["triggered"] and cond["kind"] == "spot_move":
            mv = ctx.spot - c["spot_ref"]
            d = cond.get("dir", "either")
            if (d == "up" and mv >= p.move_points) or (d == "down" and -mv >= p.move_points) or (d == "either" and abs(mv) >= p.move_points):
                c["triggered"] = True
                ctx.log(f"NIFTY moved {mv:+.0f} points: entering")
        # buy legs first, so a hedge is in place before a short (lower margin live)
        for leg in sorted(c["legs"], key=lambda x: x["side"] != "buy"):
            k = self._contract(ctx, leg)
            px = ctx.price(k)
            if leg["open"]:
                if self._stop_hit(leg, px):
                    ctx.close(k, f"Leg stop {leg['strike']} {leg['right']} at {px:.2f}")
                    leg["open"] = False
                continue
            if not can_enter or leg["entries"] >= p.max_entries:
                continue
            if leg["entries"] == 0:
                if cond["kind"] == "leg_multiple":
                    x = p.trigger_x
                    if not (px >= leg["ref"] * x if x > 1 else px <= leg["ref"] * x):
                        continue
                elif not c["triggered"]:
                    continue
            else:                                                   # re-entry: back at its first entry price
                if not (px <= leg["first"] if leg["side"] == "sell" else px >= leg["first"]):
                    continue
            lots = leg["lots"] * p.lots
            why = "Entry" if leg["entries"] == 0 else f"Re-entry {leg['entries']}"
            f = ctx.sell(k, lots, why, c["group"]) if leg["side"] == "sell" else ctx.buy(k, lots, why, c["group"])
            leg.update(open=True, entry=f.price, entries=leg["entries"] + 1, first=leg["first"] if leg["first"] is not None else f.price)

    def _close(self, ctx, reason: str) -> None:
        for k, pos in sorted(list(ctx.positions.items()), key=lambda kv: kv[1].qty > 0):   # shorts first
            ctx.close(k, reason)
        c = self.cyc
        if c:
            for leg in c.get("legs", []):
                leg["open"] = False
            c["closed"] = True
            if "start" in c:
                ctx.log(f"Cycle {c.get('group')} closed ({reason}): net ₹{ctx.net_realized - c['start']:,.0f}")

    def status(self, ctx) -> dict | None:
        c = self.cyc
        if not c or c.get("closed"):
            return {"note": (c or {}).get("skip") or f"Flat: next entry from {self.p.entry_from}"}
        return {"pnl": ctx.net_realized - c["start"] + ctx.unrealized,
                "note": "; ".join(f"{x['side']} {x['strike']} {x['right']}{' open' if x['open'] else ''}" for x in c["legs"])}
