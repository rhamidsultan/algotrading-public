#!/usr/bin/env python3
"""
NIFTY ATM straddle-BUY grid backtest (intraday, 1- or 5-minute data), with a sweep mode.

Base rules
----------
Entry   : NIFTY spot 5-min RSI(14) between 40 and 60, inside the entry window.
          Buy 2 lots ATM CE + 2 lots ATM PE (nearest expiry, strike rounded to 50).
Grid    : Each leg is managed on its OWN premium, from the price of its last trade:
            premium +step from last trade -> SELL 1 lot
            premium -step from last trade -> BUY 1 lot
Re-entry: When any leg goes flat, open a fresh straddle at the next bar where RSI is in band.
Exit    : Square off everything at 15:15.

Tweakable levers (CONFIG / --set key=value / sweep grid)
--------------------------------------------------------
  step            grid step in premium points
  initial_lots    lots per leg at entry
  max_lots        cap on lots per leg (None = no cap). At the cap, further drops are ignored
                  and the leg's reference stays at its last buy, so the next sale needs a
                  full step of recovery from that buy.
  last_buy        no averaging-down buys after this time (None = allowed all day)
  rsi_low/high    entry RSI band (0/100 = no filter)
  entry_start     first time a straddle may be opened
  entry_end       last time a straddle may be opened
  square_off      time everything is closed
  strike_offset   0 = ATM straddle; 1 = CE one strike above ATM and PE one strike below (strangle)
  skip_expiry_day on an expiry day trade the next expiry instead
  max_straddles   cap on straddles per day (None = unlimited)
  day_stop        stop for the day when day MTM <= -day_stop rupees (None = off)
  day_target      stop for the day when day MTM >= day_target rupees (None = off)
  vix_min/vix_max only enter when India VIX is inside this range (needs a 'vix' column in spot)
  cut_after_buys  leg stop-loss: after N averaging buys in a row (no sell between), the next
                  step down sells the whole leg instead of buying again; the flat leg re-enters
  positional_after_buys
                  after N averaging buys in a row a leg turns POSITIONAL: no more buys, it is not
                  squared off at 15:15 and is carried overnight until it exits or its expiry day
  positional_mode "grid": keep selling 1 lot every +step from the last trade;
                  "breakeven": sell the whole leg once premium >= its average cost.
                  Day P&L includes the overnight change in carried legs. The daily stop and
                  trailing stop close intraday legs only; positional legs keep their own exit.
  trend_after_buys
                  TREND SWITCH: when one leg has averaged down N times in a row (calls falling =
                  NIFTY trending down; puts falling = trending up), close all neutral legs and buy
                  trend_lots ATM options in the trend direction (puts in a downtrend), managed with a
                  trailing stop from their peak (trend_trail_pts points, or trend_trail_pct %).
                  trend_pyramid adds 1 lot per +step (up to trend_max_lots); trend_resume returns
                  to neutral straddles after the trend trade exits; trend_carry holds it overnight.
  trail_start     trailing stop on the day's combined MTM: arms once MTM >= trail_start
  trail_gap       ...exits everything (and stops for the day) when MTM <= peak - trail_gap
  trail_pct       ...or when MTM <= peak * (1 - trail_pct/100), if trail_gap is None

Fills   : decisions and fills use each candle's CLOSE.

Usage
-----
  Single run : python3 straddle_grid_backtest.py --spot S.csv --options OPT_DIR --days 3
               python3 straddle_grid_backtest.py ... --set max_lots=4 --set last_buy=14:00
  Sweep      : python3 straddle_grid_backtest.py --spot S.csv --options OPT_DIR --days 30 --sweep
"""

import argparse
import glob
import itertools
import os
import re
import sys

import numpy as np
import pandas as pd

# ----------------------------------------------------------------- settings
CONFIG = dict(
    lot_size=65,             # NIFTY lot size - check it matches the period you test
    initial_lots=2,
    step=20.0,
    max_lots=None,
    last_buy=None,
    rsi_period=14,
    rsi_tf="5min",
    rsi_low=40.0,
    rsi_high=60.0,
    entry_start="09:20",
    entry_end="14:45",
    square_off="15:15",
    strike_step=50,
    strike_offset=0,
    skip_expiry_day=False,
    max_straddles=None,
    day_stop=None,
    day_target=None,
    vix_min=None,
    vix_max=None,
    cut_after_buys=None,
    positional_after_buys=None,  # after this many averaging buys in a row the leg turns positional
    positional_mode="grid",      # positional exit: "grid" (sell 1 lot per +step) or "breakeven"
    trend_after_buys=None,   # after N averaging buys in a row on one leg, switch to the trend
    trend_lots=2,            # lots of the trend-side ATM option bought on the switch
    trend_trail_pts=None,    # trailing stop on the trend option: exit when it falls this many points from its peak
    trend_trail_pct=30,      # ...or this % from its peak (used when trend_trail_pts is None)
    trend_pyramid=False,     # add 1 lot every +step from the last add
    trend_max_lots=6,        # cap when pyramiding
    trend_resume=True,       # after the trend trade exits, go back to neutral straddles (RSI permitting)
    trend_carry=False,       # carry the trend trade overnight (until trail hit or expiry) instead of 15:15 exit     # after this many averaging buys in a row, the next step down sells the whole leg
    trail_start=None,        # trailing stop arms once day MTM reaches this profit (Rs)
    trail_gap=None,          # ...then exits when MTM falls this many Rs below the day's peak
    trail_pct=None,          # ...or this % of the peak profit (used when trail_gap is None)
    # charges (approximate, Zerodha-style)
    brokerage_per_order=20.0,
    stt_sell_pct=0.15,       # % of sell-side premium turnover
    other_charges_pct=0.06,  # exchange txn + SEBI + stamp + GST, % of turnover (rough)
)

# Sweep grid: every combination is run. Keep it modest - with few days of data,
# a big grid mostly finds lucky noise.
SWEEP = dict(
    step=[20, 30, 40],
    max_lots=[None, 4, 3],
    last_buy=[None, "14:00", "13:00"],
    rsi_band=[(40, 60), (45, 55), (0, 100)],
    entry_start=["09:20", "09:45"],
    strike_offset=[0, 1],
    day_stop=[None, 10000],
)

ALIASES = {
    "timestamp": ["timestamp", "datetime", "time", "date_time", "ts", "date"],
    "expiry": ["expiry", "expiry_date", "expirydate", "exp"],
    "strike": ["strike", "strike_price", "strikeprice"],
    "option_type": ["option_type", "optiontype", "opt_type", "type", "right",
                    "instrument_type", "cp", "call_put"],
    "close": ["close", "last", "ltp", "c", "last_price"],
    "vix": ["vix", "india_vix", "indiavix"],
}


# ----------------------------------------------------------------- loading
def read_any(path):
    if path.lower().endswith((".parquet", ".pq")):
        return pd.read_parquet(path)
    return pd.read_csv(path)


def normalise(df, needed, optional=()):
    lower = {c.lower().strip(): c for c in df.columns}
    rename, keep = {}, list(needed)
    for want in list(needed) + list(optional):
        for alias in ALIASES.get(want, [want]):
            if alias in lower:
                rename[lower[alias]] = want
                if want in optional:
                    keep.append(want)
                break
        else:
            if want in needed:
                sys.exit(f"Missing column '{want}'. Found columns: {list(df.columns)}")
    return df.rename(columns=rename)[keep]


def load_spot(path):
    df = normalise(read_any(path), ["timestamp", "close"], optional=["vix"])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    return df.sort_values("timestamp").drop_duplicates("timestamp").set_index("timestamp")


def load_options_dir(root, bar="5min"):
    """Folder layout: <root>/<YYYY-MM-DD expiry>/<strike><CE|PE>.csv with columns time,last,..."""
    frames = []
    for f in glob.glob(os.path.join(root, "*", "*.csv")):
        m = re.match(r"(\d+(?:\.\d+)?)(CE|PE)\.csv$", os.path.basename(f), re.I)
        if not m:
            continue
        d = normalise(pd.read_csv(f), ["timestamp", "close"])
        d["timestamp"] = pd.to_datetime(d["timestamp"]).dt.tz_localize(None)
        d = d.dropna(subset=["close"])
        if d.empty:
            continue
        # Historical vendor bars are stamped at bar END (first bar 09:20, all on exact 5-min marks);
        # live recordings are stamped when the tick arrived. Shift historical days back one bar
        # so they line up with spot bars, which are stamped at bar START.
        ts = d["timestamp"]
        day = ts.dt.date
        g = pd.DataFrame({"day": day, "al": (ts.dt.second == 0) & (ts.dt.minute % 5 == 0),
                          "t": ts.dt.time})
        h = g.groupby("day").agg(all_al=("al", "all"), first=("t", "min"))
        hist_days = set(h[h.all_al & (h["first"] >= pd.Timestamp("09:20").time())].index)
        d.loc[day.isin(hist_days), "timestamp"] -= pd.Timedelta(minutes=5)
        # keep the last price in each bar (bar stamped at its start), at the spot data's bar size
        d = d.set_index("timestamp")["close"].resample(bar).last().dropna().reset_index()
        d["expiry"] = pd.Timestamp(os.path.basename(os.path.dirname(f)))
        d["strike"] = float(m.group(1))
        d["option_type"] = m.group(2).upper()
        frames.append(d)
    if not frames:
        sys.exit(f"No option CSVs found under {root}")
    return pd.concat(frames, ignore_index=True).sort_values("timestamp")


def load_options(path, bar="5min"):
    if os.path.isdir(path):
        return load_options_dir(path, bar)
    df = normalise(read_any(path), ["timestamp", "expiry", "strike", "option_type", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.normalize()
    df["strike"] = df["strike"].astype(float)
    df["option_type"] = df["option_type"].astype(str).str.upper().str.strip()
    df["option_type"] = df["option_type"].replace({"CALL": "CE", "PUT": "PE", "C": "CE", "P": "PE"})
    return df.sort_values("timestamp")


# ----------------------------------------------------------------- RSI
def wilder_rsi(close, period):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(100.0).where(avg_loss.notna())


def rsi_on_bars(spot, cfg):
    """RSI on cfg['rsi_tf'] bars, mapped onto each spot bar using only completed bars."""
    bars = spot["close"].resample(cfg["rsi_tf"], closed="left", label="right").last().dropna()
    rsi = wilder_rsi(bars, cfg["rsi_period"]).reset_index()
    rsi.columns = ["avail_at", "rsi"]
    one = spot.reset_index()[["timestamp"]]
    bar = spot.index.to_series().diff().dropna().mode().iloc[0]
    one["decide_at"] = one["timestamp"] + bar      # a bar stamped t closes at t+bar
    m = pd.merge_asof(one.sort_values("decide_at"), rsi.sort_values("avail_at"),
                      left_on="decide_at", right_on="avail_at", direction="backward")
    return m.set_index("timestamp")["rsi"]


# ----------------------------------------------------------------- data prep (done once)
def bar_size(spot):
    return spot.index.to_series().diff().dropna().mode().iloc[0]


def prepare(spot, opts, cfg, days):
    rsi = rsi_on_bars(spot, cfg)
    limit = max(1, int(pd.Timedelta(minutes=15) / bar_size(spot)))   # option prices up to 15 min stale
    ctx = []
    for d in days:
        sd = spot[spot.index.date == d]
        od = opts[opts["timestamp"].dt.date == d]
        idx = sd.index
        px_fresh, px_any = {}, {}
        for (e, k, o), g in od.groupby(["expiry", "strike", "option_type"]):
            s = g.set_index("timestamp")["close"].groupby(level=0).last()
            s = s.reindex(s.index.union(idx)).ffill()
            fresh = g.set_index("timestamp")["close"].groupby(level=0).last()
            fresh = fresh.reindex(fresh.index.union(idx)).ffill(limit=limit)
            px_any[(e, k, o)] = s.reindex(idx).to_dict()
            px_fresh[(e, k, o)] = fresh.reindex(idx).to_dict()
        exps = sorted({e for (e, _, _) in px_any if e >= pd.Timestamp(d)})
        ctx.append(dict(
            day=d, idx=list(idx),
            spot=sd["close"].to_dict(),
            vix=sd["vix"].to_dict() if "vix" in sd else {},
            rsi=rsi[rsi.index.date == d].to_dict(),
            exps=exps, px_fresh=px_fresh, px_any=px_any))
    return ctx


# ----------------------------------------------------------------- simulation
def _t(day, hhmm):
    return None if hhmm is None else pd.Timestamp(f"{day} {hhmm}")


def charges(side, qty, price, cfg):
    turnover = qty * price
    c = cfg["brokerage_per_order"] + turnover * cfg["other_charges_pct"] / 100
    if side == "SELL":
        c += turnover * cfg["stt_sell_pct"] / 100
    return c


def run_period(ctx, cfg, log=True):
    """Run the days in order, carrying positional legs from one day to the next.
    Returns one (summary, trades, mtm_path) per day (None where nothing could trade)."""
    carry, out, sid = [], [], [0]
    for i, c in enumerate(ctx):
        res, carry = run_day(c, cfg, carry, sid, log, is_last=(i == len(ctx) - 1))
        out.append(res)
    return out


def run_day(c, cfg, carry, sid_counter, log=True, is_last=False):
    day, lot, step = c["day"], cfg["lot_size"], cfg["step"]
    today = pd.Timestamp(day)
    expiry = None
    if c["exps"]:
        expiry = c["exps"][0]
        if cfg["skip_expiry_day"] and expiry == today and len(c["exps"]) > 1:
            expiry = c["exps"][1]
    if expiry is None and not carry:
        return None, carry
    t_es, t_ee, t_sq = _t(day, cfg["entry_start"]), _t(day, cfg["entry_end"]), _t(day, cfg["square_off"])
    t_lb = _t(day, cfg["last_buy"])
    pf, pa = c["px_fresh"], c["px_any"]
    pos_after = cfg.get("positional_after_buys")
    pos_mode = cfg.get("positional_mode", "grid")

    def price(l, t, stale_ok=False):
        v = (pa if stale_ok else pf).get((l["e"], l["k"], l["o"]), {}).get(t)
        return None if v is None or np.isnan(v) else float(v)

    def value(l, t):
        p = price(l, t, stale_ok=True)
        return l["lots"] * lot * (p if p is not None else l["mark"])

    legs = list(carry)
    start_value = sum(l["lots"] * lot * l["mark"] for l in carry)   # yesterday's closing mark
    start_cost = sum(l["cost"] * lot for l in carry)
    trades = []
    trend_on = any(l.get("kind") == "trend" and l["lots"] > 0 for l in carry)
    st = dict(cash=0.0, want=not trend_on, halt=False, orders=0, charges=0.0, lots_bought=0,
              peak_out=start_cost, peak_mtm=0.0, straddles=0, tagged=0,
              trend_on=trend_on, trends=0, switch=None)
    mtm_path = []

    def record(t, l, side, n, p, reason):
        qty = n * lot
        ch = charges(side, qty, p, cfg)
        flow = (p * qty if side == "SELL" else -p * qty) - ch
        st["cash"] += flow
        st["orders"] += 1
        st["charges"] += ch
        if side == "BUY":
            st["lots_bought"] += n
            l["cost"] += p * n
        else:
            before = l["lots"] + n
            l["cost"] -= (l["cost"] / before) * n if before else 0
        st["peak_out"] = max(st["peak_out"], start_cost - st["cash"])
        if log:
            trades.append(dict(date=day, time=t.strftime("%H:%M"), straddle=l["sid"], leg=l["o"],
                               strike=int(l["k"]), expiry=l["e"].date(), side=side, lots=n, qty=qty,
                               price=round(p, 2), reason=reason, lots_after=l["lots"],
                               positional="yes" if l["pos"] else "", charges=round(ch, 2),
                               cash_flow=round(flow, 2)))

    def close(l, t, reason):
        p = price(l, t, stale_ok=True)
        if p is None:
            p = l["mark"]
        n, l["lots"] = l["lots"], 0
        record(t, l, "SELL", n, p, reason)

    def manage_positional(l, t):
        p = price(l, t)
        if p is None:
            return
        if pos_mode == "breakeven":
            if p >= l["cost"] / l["lots"]:
                close(l, t, "positional_breakeven_exit")
            return
        n_sell = 0
        while l["lots"] - n_sell > 0 and p >= l["ref"] + step:
            n_sell += 1
            l["ref"] += step
        if n_sell:
            l["lots"] -= n_sell
            record(t, l, "SELL", n_sell, p, "positional_grid_sell")

    def manage_trend(l, t):
        p = price(l, t)
        if p is None:
            return
        if cfg["trend_pyramid"]:
            n_add = 0
            while p >= l["ref"] + step and l["lots"] + n_add < cfg["trend_max_lots"]:
                n_add += 1
                l["ref"] += step
            if n_add:
                l["lots"] += n_add
                l["maxl"] = max(l["maxl"], l["lots"])
                record(t, l, "BUY", n_add, p, "trend_add")
        l["peak"] = max(l["peak"], p)
        gap = (cfg["trend_trail_pts"] if cfg["trend_trail_pts"] is not None
               else l["peak"] * cfg["trend_trail_pct"] / 100)
        if p <= l["peak"] - gap:
            close(l, t, f"trend_trail_exit (peak {l['peak']:.1f})")
            st["trend_on"] = False
            if cfg["trend_resume"]:
                st["want"] = True

    def switch_to_trend(t, direction, why):
        for l in legs:
            if l["lots"] > 0 and l.get("kind") != "trend" and not l["pos"]:
                close(l, t, f"switch_to_trend ({why})")
        if trend_exp is None:
            return
        spot = c["spot"][t]
        k = round(spot / cfg["strike_step"]) * cfg["strike_step"]
        nl = dict(o=direction, k=k, e=trend_exp)
        p = price(dict(nl, lots=0, mark=0), t)
        if p is None:
            return
        sid_counter[0] += 1
        l = dict(nl, sid=sid_counter[0], lots=cfg["trend_lots"], ref=p, maxl=cfg["trend_lots"], row=0,
                 pos=bool(cfg["trend_carry"]), cost=0.0, mark=p, kind="trend", peak=p)
        legs.append(l)
        record(t, l, "BUY", cfg["trend_lots"], p, f"trend_entry {'down' if direction == 'PE' else 'up'} (spot {spot:.0f})")
        st["trend_on"], st["want"] = True, False
        st["trends"] += 1

    trend_exp = expiry

    for t in c["idx"]:
        if t >= t_sq:
            for l in legs:
                if l["lots"] <= 0:
                    continue
                if not l["pos"]:
                    close(l, t, "square_off")
                    if l.get("kind") == "trend":
                        st["trend_on"] = False
                elif l["e"] <= today:
                    close(l, t, "positional_expiry_exit")
            mtm_path.append(st["cash"] + sum(value(l, t) for l in legs if l["lots"] > 0) - start_value)
            break

        for l in legs:
            if l["lots"] <= 0:
                continue
            if l.get("kind") == "trend":
                if not st["halt"] or l["pos"]:
                    manage_trend(l, t)
                continue
            if l["pos"]:
                manage_positional(l, t)
                continue
            if st["halt"]:
                continue
            p = price(l, t)
            if p is None:
                continue
            n_sell = 0
            while l["lots"] - n_sell > 0 and p >= l["ref"] + step:
                n_sell += 1
                l["ref"] += step
            if n_sell:
                l["lots"] -= n_sell
                l["row"] = 0
                record(t, l, "SELL", n_sell, p, "grid_sell")
                if l["lots"] == 0:
                    st["want"] = True
                continue
            if (cfg["cut_after_buys"] is not None and l["row"] >= cfg["cut_after_buys"]
                    and p <= l["ref"] - step):
                rows = l["row"]
                close(l, t, f"cut_loss (after {rows} buys)")
                st["want"] = True
                continue
            if t_lb is not None and t > t_lb:
                continue
            n_buy = 0
            while p <= l["ref"] - step and (cfg["max_lots"] is None or l["lots"] + n_buy < cfg["max_lots"]):
                n_buy += 1
                l["ref"] -= step
            if n_buy:
                l["lots"] += n_buy
                l["row"] += n_buy
                l["maxl"] = max(l["maxl"], l["lots"])
                reason = "grid_buy"
                if pos_after is not None and l["row"] >= pos_after:
                    l["pos"] = True
                    st["tagged"] += 1
                    reason = f"grid_buy -> positional (after {l['row']} buys)"
                record(t, l, "BUY", n_buy, p, reason)
                if (cfg["trend_after_buys"] is not None and not st["trend_on"] and st["switch"] is None
                        and l["row"] >= cfg["trend_after_buys"]):
                    st["switch"] = ("PE" if l["o"] == "CE" else "CE", f"{l['o']} fell {l['row']} steps")

        if st["switch"] is not None:
            switch_to_trend(t, *st["switch"])
            st["switch"] = None

        r = c["rsi"].get(t, np.nan)
        vix = c["vix"].get(t)
        if (expiry is not None and not st["halt"] and st["want"] and not st["trend_on"] and t_es <= t <= t_ee
                and not np.isnan(r) and cfg["rsi_low"] <= r <= cfg["rsi_high"]
                and (cfg["max_straddles"] is None or st["straddles"] < cfg["max_straddles"])
                and (cfg["vix_min"] is None or (vix is not None and vix >= cfg["vix_min"]))
                and (cfg["vix_max"] is None or (vix is not None and vix <= cfg["vix_max"]))):
            spot = c["spot"][t]
            atm = round(spot / cfg["strike_step"]) * cfg["strike_step"]
            kc = atm + cfg["strike_offset"] * cfg["strike_step"]
            kp = atm - cfg["strike_offset"] * cfg["strike_step"]
            new = [dict(o="CE", k=kc, e=expiry), dict(o="PE", k=kp, e=expiry)]
            ps = [price(dict(n, lots=0, mark=0), t) for n in new]
            if None not in ps:
                sid_counter[0] += 1
                st["straddles"] += 1
                for n, p in zip(new, ps):
                    l = dict(n, sid=sid_counter[0], lots=cfg["initial_lots"], ref=p, maxl=cfg["initial_lots"],
                             row=0, pos=False, cost=0.0, mark=p)
                    legs.append(l)
                    record(t, l, "BUY", cfg["initial_lots"], p, f"entry (spot {spot:.0f}, RSI {r:.1f})")
                st["want"] = False

        mtm = st["cash"] + sum(value(l, t) for l in legs if l["lots"] > 0) - start_value
        mtm_path.append(mtm)
        if st["halt"]:
            continue
        st["peak_mtm"] = max(st["peak_mtm"], mtm)
        trail_hit = False
        if cfg["trail_start"] is not None and st["peak_mtm"] >= cfg["trail_start"]:
            gap = (cfg["trail_gap"] if cfg["trail_gap"] is not None
                   else st["peak_mtm"] * (cfg["trail_pct"] or 0) / 100)
            trail_hit = mtm <= st["peak_mtm"] - gap
        if ((cfg["day_stop"] is not None and mtm <= -cfg["day_stop"]) or
                (cfg["day_target"] is not None and mtm >= cfg["day_target"]) or trail_hit):
            reason = ("trail_stop" if trail_hit and mtm > -(cfg["day_stop"] or float("inf"))
                      else "day_stop" if mtm < 0 else "day_target")
            for l in legs:                      # positional / carried legs are left to their own exit
                if l["lots"] > 0 and not l["pos"]:
                    close(l, t, reason)
                    if l.get("kind") == "trend":
                        st["trend_on"] = False
            mtm_path[-1] = st["cash"] + sum(value(l, t) for l in legs if l["lots"] > 0) - start_value
            st["halt"] = True

    last_t = c["idx"][min(len(mtm_path), len(c["idx"])) - 1] if mtm_path else c["idx"][-1]
    new_carry = []
    open_value = 0.0
    for l in legs:
        if l["lots"] > 0:
            p = price(l, last_t, stale_ok=True)
            l["mark"] = p if p is not None else l["mark"]
            open_value += l["lots"] * lot * l["mark"]
            new_carry.append(l)
    net = st["cash"] + open_value - start_value
    day_legs = [l for l in legs if l not in carry] or legs
    summary = dict(
        date=day, expiry=expiry.date() if expiry is not None else None,
        straddles=st["straddles"], orders=st["orders"], lots_bought=st["lots_bought"],
        max_lots_one_leg=max((l["maxl"] for l in day_legs), default=0),
        peak_premium_out=round(st["peak_out"]),
        gross_pnl=round(net + st["charges"]), charges=round(st["charges"]), net_pnl=round(net),
        worst_intraday_mtm=round(min(mtm_path)) if mtm_path else 0,
        best_intraday_mtm=round(max(mtm_path)) if mtm_path else 0,
        went_positional=st["tagged"], trend_trades=st["trends"], carried_in=len(carry),
        open_positional_lots=sum(l["lots"] for l in new_carry),
        open_positional_value=round(open_value))
    if st["orders"] == 0 and not carry:
        summary.update(peak_premium_out=0, worst_intraday_mtm=0, best_intraday_mtm=0)
    return (summary, trades, mtm_path), new_carry


# ----------------------------------------------------------------- scoring
def score(daily):
    pnl = np.array([d["net_pnl"] for d in daily], dtype=float)
    traded = np.array([d["orders"] > 0 for d in daily])
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    eq = np.cumsum(pnl)
    dd = (eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min() if len(eq) else 0
    peak_cap = max([d["peak_premium_out"] for d in daily] + [0])
    return dict(
        net=pnl.sum(),
        days=int(traded.sum()),
        win_pct=100 * len(wins) / max(traded.sum(), 1),
        avg_win=wins.mean() if len(wins) else 0.0,
        avg_loss=losses.mean() if len(losses) else 0.0,
        profit_factor=(wins.sum() / -losses.sum()) if len(losses) else np.inf,
        worst_day=pnl.min() if len(pnl) else 0.0,
        max_dd=min(dd, 0),
        peak_capital=peak_cap,
        ret_on_cap_pct=100 * pnl.sum() / peak_cap if peak_cap else 0.0,
        charges=sum(d["charges"] for d in daily),
    )


def expand_sweep(grid):
    keys = list(grid)
    for vals in itertools.product(*grid.values()):
        combo = dict(zip(keys, vals))
        if "rsi_band" in combo:
            combo["rsi_low"], combo["rsi_high"] = combo.pop("rsi_band")
        if "positional" in combo:
            pz = combo.pop("positional") or [None, "grid"]
            combo["positional_after_buys"], combo["positional_mode"] = pz
        if "trend" in combo:
            td = combo.pop("trend") or {}
            combo["trend_after_buys"] = td.get("after")
            for k2 in ("lots", "trail_pts", "trail_pct", "pyramid", "resume", "carry"):
                if k2 in td:
                    combo["trend_" + k2] = td[k2]
        if "trail" in combo:
            tr = combo.pop("trail") or [None, None, None]
            combo["trail_start"], combo["trail_gap"], combo["trail_pct"] = tr
        yield combo


def label(combo):
    def f(v):
        return "off" if v is None else v
    return (f"step {combo['step']:g} | cap {f(combo['max_lots'])} | last buy {f(combo['last_buy'])} | "
            f"RSI {combo['rsi_low']:g}-{combo['rsi_high']:g} | start {combo['entry_start']} | "
            f"{'strangle' if combo['strike_offset'] else 'ATM'} | stop {f(combo['day_stop'])}"
            + (f" | cut after {combo['cut_after_buys']} buys" if combo.get("cut_after_buys") else " | cut off")
            + (f" | positional after {combo['positional_after_buys']} buys ({combo.get('positional_mode', 'grid')})"
               if combo.get("positional_after_buys") else "")
            + trend_label(combo)
            + trail_label(combo))


def trend_label(c):
    if c.get("trend_after_buys") is None:
        return " | trend off"
    tr = (f"{c['trend_trail_pts']:g}pt" if c.get("trend_trail_pts") is not None
          else f"{c.get('trend_trail_pct', 30):g}%")
    extra = "".join([" +pyramid" if c.get("trend_pyramid") else "",
                     " carry" if c.get("trend_carry") else "",
                     "" if c.get("trend_resume", True) else " no-resume"])
    return f" | trend after {c['trend_after_buys']} buys, {c.get('trend_lots', 2)} lots, trail {tr}{extra}"


def trail_label(c):
    if c.get("trail_start") is None:
        return " | trail off"
    how = f"-{c['trail_gap']:g}" if c.get("trail_gap") is not None else f"-{c['trail_pct']:g}%"
    return f" | trail {c['trail_start']:g} {how}"


def run_sweep(ctx, base, out, check_days, grid=None):
    rows = []
    days = [c["day"] for c in ctx]
    tune_set = set(days[:-check_days]) if check_days and len(days) > check_days else set(days)
    combos = list(expand_sweep(grid or SWEEP))
    print(f"Running {len(combos)} combinations over {len(days)} days "
          f"(tune: {min(tune_set)}..{max(tune_set)}, check: last {check_days})...", flush=True)
    for i, combo in enumerate(combos, 1):
        cfg = dict(base, **combo)
        daily = [r[0] for r in run_period(ctx, cfg, log=False) if r]
        s_all = score(daily)
        s_tune = score([d for d in daily if d["date"] in tune_set])
        s_chk = score([d for d in daily if d["date"] not in tune_set])
        row = dict(combo)
        row["label"] = label(cfg)
        row.update({k: s_all[k] for k in s_all})
        row["tune_net"] = s_tune["net"]
        row["check_net"] = s_chk["net"]
        for d in daily:
            row[f"pnl_{d['date']}"] = d["net_pnl"]
        rows.append(row)
        if i % 100 == 0:
            print(f"  {i}/{len(combos)}", flush=True)
    df = pd.DataFrame(rows).sort_values("net", ascending=False)
    df.to_csv(os.path.join(out, "sweep_results.csv"), index=False)
    return df


# ----------------------------------------------------------------- main
def parse_set(items):
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        if k not in CONFIG:
            sys.exit(f"Unknown setting '{k}'. Options: {list(CONFIG)}")
        if v.lower() in ("none", "off", ""):
            out[k] = None
        elif v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif re.fullmatch(r"-?\d+", v):
            out[k] = int(v)
        elif re.fullmatch(r"-?\d*\.\d+", v):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spot", required=True, help="NIFTY spot file (csv/parquet), optional vix column")
    ap.add_argument("--options", required=True, help="options file or folder of per-contract CSVs")
    ap.add_argument("--days", type=int, default=3, help="use the last N trading days with option data")
    ap.add_argument("--out", default="backtest_out", help="output folder")
    ap.add_argument("--step", type=float, help="override grid step")
    ap.add_argument("--lot-size", type=int, help="override lot size")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="override any CONFIG value")
    ap.add_argument("--resample-spot", help="e.g. 5min: run 1-minute data as 5-minute bars (to compare bar sizes)")
    ap.add_argument("--sweep", action="store_true", help="run every combination in SWEEP")
    ap.add_argument("--grid", help="sweep: JSON file of {setting: [values]} replacing SWEEP "
                    "(null = off; 'rsi_band': [[40,60]]; 'trail': [null, [start, gap, pct]]; "
                    "'positional': [null, [3, 'grid'], [3, 'breakeven']])")
    ap.add_argument("--check-days", type=int, default=3,
                    help="sweep: hold back the newest N days as a check set (default 3)")
    a = ap.parse_args()

    cfg = dict(CONFIG)
    if a.step:
        cfg["step"] = a.step
    if a.lot_size:
        cfg["lot_size"] = a.lot_size
    cfg.update(parse_set(a.set))

    spot = load_spot(a.spot)
    if a.resample_spot:
        spot = spot.resample(a.resample_spot).agg({c: ("last" if c in ("close", "vix") else "first") for c in spot.columns}).dropna(subset=["close"])
    opts = load_options(a.options, pd.tseries.frequencies.to_offset(bar_size(spot)).freqstr)
    opt_days = set(opts["timestamp"].dt.date)
    days = sorted(d for d in set(spot.index.date) if d in opt_days)[-a.days:]
    if not days:
        sys.exit("No days with both spot and option data.")
    ctx = prepare(spot, opts, cfg, days)
    os.makedirs(a.out, exist_ok=True)
    pd.set_option("display.width", 220)

    if a.sweep:
        grid = None
        if a.grid:
            import json
            with open(a.grid) as fh:
                grid = json.load(fh)
        df = run_sweep(ctx, cfg, a.out, a.check_days, grid)
        cols = ["label", "net", "win_pct", "profit_factor", "worst_day", "max_dd",
                "peak_capital", "ret_on_cap_pct", "tune_net", "check_net"]
        print("\nTop 15 by net P&L:")
        print(df[cols].head(15).round(1).to_string(index=False))
        print(f"\nAll results: {os.path.join(a.out, 'sweep_results.csv')}")
        return

    all_trades, summaries, curves = [], [], []
    for c, res in zip(ctx, run_period(ctx, cfg)):
        if res and (res[0]["orders"] or res[0]["carried_in"]):
            summaries.append(res[0])
            all_trades += res[1]
            curves.append((c["idx"][:len(res[2])], res[2]))
    sdf = pd.DataFrame(summaries)
    pd.DataFrame(all_trades).to_csv(os.path.join(a.out, "trades.csv"), index=False)
    sdf.to_csv(os.path.join(a.out, "daily_summary.csv"), index=False)
    print(f"\nGrid step {cfg['step']:g} pts | lot size {cfg['lot_size']} | "
          f"RSI {cfg['rsi_low']:g}-{cfg['rsi_high']:g} on {cfg['rsi_tf']}\n")
    print(sdf.to_string(index=False) if not sdf.empty else "No trades taken.")
    if not sdf.empty:
        s = score(summaries)
        print(f"\nTOTAL net P&L: Rs {s['net']:,.0f} | win days {s['win_pct']:.0f}% | "
              f"profit factor {s['profit_factor']:.2f} | worst day {s['worst_day']:,.0f}")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, len(curves), figsize=(5 * len(curves), 3.5), squeeze=False)
        for ax, (x, y), s in zip(axes[0], curves, summaries):
            ax.plot(x, y)
            ax.axhline(0, color="grey", lw=0.8)
            ax.set_title(f"{s['date']}  net Rs {s['net_pnl']:,.0f}")
            ax.tick_params(axis="x", labelrotation=45)
        fig.tight_layout()
        fig.savefig(os.path.join(a.out, "intraday_mtm.png"), dpi=110)
    except ImportError:
        pass
    print(f"\nFiles written to {a.out}/")


if __name__ == "__main__":
    main()
