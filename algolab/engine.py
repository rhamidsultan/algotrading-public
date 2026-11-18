"""The run loop. The same loop drives backtests (historical feed) and paper trading (live feed)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, time, timedelta

import pandas as pd

from .broker import CostModel, PaperBroker
from .metrics import add_excursions, compute_metrics, cycles_table
from .models import Contract, Fill, Quote

SETTLE_TIME = time(15, 30)


class Context:
    """What a strategy sees and can do on each bar."""

    def __init__(self, feed, broker: PaperBroker, logs: list, strategy=None):
        self.feed, self.broker, self.logs = feed, broker, logs
        self.strategy = strategy
        self._why_cache = (None, None)

    # market
    @property
    def now(self):
        return self.feed.now

    @property
    def spot(self) -> float:
        return self.feed.spot

    @property
    def vix(self):
        return self.feed.vix

    @property
    def lot_size(self) -> int:
        """Lot size of contracts expiring today or later (see lot_size_for for a given expiry)."""
        return self.feed.lot_size_for(self.now.date())

    def lot_size_for(self, expiry: date) -> int:
        return self.feed.lot_size_for(expiry)

    def is_trading_day(self, d: date) -> bool:
        return self.feed.is_trading_day(d)

    def next_trading_day(self, d: date) -> date:
        return self.feed.next_trading_day(d)

    def atm_strike(self) -> int:
        step = self.feed.strike_step
        return int(round(self.spot / step) * step)

    def expiries(self) -> list[date]:
        return self.feed.expiries()

    def pick_expiry(self, min_dte: int = 0) -> date | None:
        today = self.now.date()
        return next((e for e in self.expiries() if (e - today).days >= min_dte), None)

    def option(self, expiry: date, strike: int, right: str) -> Contract:
        return Contract(self.feed.underlying, expiry, int(strike), right)

    def quote(self, contract: Contract) -> Quote:
        return self.feed.quote(contract)

    def quotes(self, contracts) -> dict:
        """Quotes for several contracts in one go (one request on a live feed)."""
        return self.feed.quotes(contracts)

    def listed_strikes(self, expiry: date, right: str) -> set | None:
        return self.feed.listed_strikes(expiry, right)

    @property
    def gives_day_ohlc(self) -> bool:
        """True when the feed knows each option's open and high so far today (live Zerodha quotes, real-price
        backtests), so a strategy started mid-day still sees them."""
        return bool(getattr(self.feed, "gives_day_ohlc", False))

    def day_ranges(self, contracts) -> dict:
        """{contract: (open, high so far today)} or None for one with no trade yet / no data."""
        f = getattr(self.feed, "day_range", None)
        if f is not None:
            return {c: f(c) for c in contracts}
        out = {}
        for c, q in self.quotes(contracts).items():
            o, h = getattr(q, "day_open", None), getattr(q, "day_high", None)
            out[c] = (float(o), float(max(o, h))) if o and h else None
        return out

    @property
    def strike_step(self) -> int:
        return self.feed.strike_step

    def price(self, contract: Contract) -> float:
        return self.feed.quote(contract).last

    def fill_price(self, contract: Contract, side: str) -> float:
        """The price an order would fill at now: best bid/ask when known, else last price less slippage."""
        return self.broker.fill_price(side, self.feed.quote(contract))

    # orders
    def sell(self, contract: Contract, lots: int = 1, reason: str = "", group: str = "") -> Fill:
        return self._order(contract, "SELL", lots * self.lot_size_for(contract.expiry), reason, group)

    def buy(self, contract: Contract, lots: int = 1, reason: str = "", group: str = "") -> Fill:
        return self._order(contract, "BUY", lots * self.lot_size_for(contract.expiry), reason, group)

    def close(self, contract: Contract, reason: str = "") -> Fill | None:
        pos = self.broker.positions.get(contract)
        if pos is None:
            return None
        return self._order(contract, "SELL" if pos.qty > 0 else "BUY", abs(pos.qty), reason, pos.group)

    def close_all(self, reason: str = "") -> None:
        for c in list(self.broker.positions):
            self.close(c, reason)

    def open_legs(self, legs: list) -> list:
        """Open several legs of one entry together: legs = [(contract, "BUY" | "SELL", lots, reason, group), ...].
        Backtests and paper take them one by one, exactly like separate buy()/sell() calls. A live session
        (broker.execute_many) places all the orders at once and waits for all the fills, so the second leg is not
        sent only after the first has filled. Returns the fills in the same order."""
        many = getattr(self.broker, "execute_many", None)
        if many is None or len(legs) < 2:
            return [self._order(c, side, lots * self.lot_size_for(c.expiry), reason, group)
                    for c, side, lots, reason, group in legs]
        items, notes = [], []
        for c, side, lots, reason, group in legs:
            qty = lots * self.lot_size_for(c.expiry)
            notes.append(self._why(c, True, reason))
            items.append((c, side, qty, self.feed.quote(c), reason, group))
        fills = many(self.now, items)
        for (c, side, qty, _q, reason, _g), fill, note in zip(items, fills, notes):
            if fill is not None and not fill.note:
                fill.note = note
            if fill is not None:
                self.log(f"{side} {qty} {c.label} @ {fill.price:.2f}" + (f" ({reason})" if reason else ""))
        return fills

    def _order(self, contract, side, qty, reason, group) -> Fill:
        pos = self.broker.positions.get(contract)
        opening = pos is None or (pos.qty > 0) == (side == "BUY")
        note = self._why(contract, opening, reason)
        if opening and not self._margin_ok(contract, qty if side == "BUY" else -qty, reason):
            return None
        fq = getattr(self.feed, "fill_quote", None)          # backtests: the price an order would really get
        size = getattr(self.feed, "check_size", None)
        if size:
            size(contract, qty)
        fill = self.broker.execute(self.now, contract, side, qty, fq(contract) if fq else self.feed.quote(contract), reason, group)
        if not fill.note:
            fill.note = note
        self.log(f"{side} {qty} {contract.label} @ {fill.price:.2f}" + (f" ({reason})" if reason else ""))
        return fill

    def _margin_ok(self, contract, signed_qty: int, reason: str) -> bool:
        """Estimated margin after this order against the account's equity (account.margin.check: warn counts the
        entries beyond it, refuse also skips them). Backtests only; live orders are checked by the broker."""
        cfg = getattr(self.feed, "cfg", None)
        if not cfg or not hasattr(self.feed, "fill_quote"):
            return True
        from .margin_est import positions_margin, settings
        need = positions_margin(self.broker, self.feed.quote, self.spot, cfg, extra=(contract, signed_qty))
        have = self.equity
        if need <= have:
            return True
        st = margin_stats(self.broker)
        st["over"] += 1
        if settings(cfg)["check"] == "refuse":
            st["refused"] += 1
            self.log(f"Not entered {contract.label} ({reason or 'entry'}): needs about Rs {need:,.0f} margin, "
                     f"the account has Rs {have:,.0f}")
            return False
        return True

    def _why(self, contract, opening: bool, reason: str = "") -> str:
        """The numbers behind an order: NIFTY and VIX then, the strike's distance and days left for a new
        leg, and for a closing leg the cycle P&L against the strategy's target and stop (taken once per
        moment, before the first leg moves, so every leg of one exit shows the level that triggered it)."""
        try:
            parts = []
            if opening:
                dist = contract.strike - self.spot
                atm = abs(dist) < self.feed.strike_step / 2
                parts.append("ATM" if atm else f"{abs(dist):,.0f} pts {'above' if dist > 0 else 'below'} NIFTY")
                dte = (contract.expiry - self.now.date()).days
                parts.append("expiry today" if dte == 0 else f"{dte} day{'s' if dte != 1 else ''} to expiry")
            else:
                key, st = self._why_cache
                if key != self.now:
                    st = self.strategy.status(self) if self.strategy is not None else None
                    self._why_cache = (self.now, st)
                if st and st.get("pnl") is not None:
                    txt = f"cycle P&L Rs {st['pnl']:,.0f}"
                    lv = [f"target Rs {st['target']:,.0f}" for _ in [0] if st.get("target") is not None]
                    lv += [f"stop Rs {st['stop']:,.0f}" for _ in [0] if st.get("stop") is not None]
                    parts.append(txt + (f" ({', '.join(lv)})" if lv else ""))
                plain = not any(w in reason.lower() for w in ("stop", "target"))
                for name, val in (list((st or {}).get("levels", {}).items())[:3] if plain else []):
                    if isinstance(val, (int, float)):
                        parts.append(f"{name} {val:,.0f}")
            mkt = f"NIFTY {self.spot:,.1f}"
            if self.vix:
                mkt += f", VIX {self.vix:.2f}"
            return " · ".join(parts + [mkt])
        except Exception:  # noqa: BLE001 - an explanation must never stop an order
            return ""

    # account
    @property
    def positions(self):
        return self.broker.positions

    @property
    def net_realized(self) -> float:
        return self.broker.net_realized

    @property
    def unrealized(self) -> float:
        return self.broker.unrealized(self.feed.quote)

    @property
    def equity(self) -> float:
        return self.broker.equity(self.feed.quote)

    def log(self, message: str) -> None:
        self.logs.append(f"{self.now:%Y-%m-%d %H:%M}  {message}")


def margin_stats(broker) -> dict:
    st = getattr(broker, "margin_stats", None)
    if st is None:
        st = broker.margin_stats = {"peak": 0.0, "sum": 0.0, "n": 0, "over": 0, "refused": 0}
    return st


class Stepper:
    """Advances one strategy by one feed moment. Several steppers can share one live feed."""

    def __init__(self, feed, strategy, broker: PaperBroker, logs: list | None = None, after_bar=None,
                 keep_curve: bool = True, risk=None, not_before: dict | None = None):
        self.feed, self.strategy, self.broker = feed, strategy, broker
        self.not_before = not_before or {}      # {date: time}: nothing happens that day before this time
        from .risk import RiskManager
        self.risk = risk if isinstance(risk, RiskManager) else (RiskManager(risk) if risk else None)
        self.logs = logs if logs is not None else []
        self.ctx = Context(feed, broker, self.logs, strategy)
        self.after_bar, self.keep_curve = after_bar, keep_curve
        self.curve: list = []
        self.prev_spot = None

    def step(self, now) -> None:
        feed, broker, ctx = self.feed, self.broker, self.ctx
        start = self.not_before.get(now.date())
        if start is not None and now.time() < start:
            return          # the session's first price came later that day (Compare with paper): start then too
        for c in list(broker.positions):
            if now.date() > c.expiry:
                px = feed.settle_spot(c.expiry) if hasattr(feed, "settle_spot") else None   # 1-second feed: the
                if px is None:                                                               # missed day's close
                    px = self.prev_spot if self.prev_spot is not None else feed.spot
                broker.settle(now, c, px)
                ctx.log(f"Settled {c.label} at expiry (late)")
            elif now.date() == c.expiry and now.time() >= SETTLE_TIME:
                f = broker.settle(now, c, feed.spot)
                if f is not None:
                    f.note = f"NIFTY {feed.spot:,.1f} at the close · intrinsic value {f.price:,.2f}"
                ctx.log(f"Settled {c.label} at expiry @ {f.price:.2f}")
        resting = self.risk.check(ctx) if self.risk is not None else False   # Orders & stops (algolab/risk.py)
        if not resting:
            self.strategy.on_bar(ctx)
        self.prev_spot = feed.spot
        if not getattr(feed, "bar_closed", True):
            return  # a moment inside the bar: strategies act on it, the equity curve does not
        point = (now, ctx.equity, feed.spot)
        if broker.positions and hasattr(feed, "fill_quote") and now.second == 0:   # backtests: margin the open legs
            # would block, once a minute (bars of 1 minute or more all end on second 0; 1-second backtests step 60x)
            from .margin_est import positions_margin
            m = positions_margin(broker, feed.quote, feed.spot, getattr(feed, "cfg", None))
            st = margin_stats(broker)
            st["peak"] = max(st["peak"], m)
            st["sum"] += m
            st["n"] += 1
        if self.keep_curve:
            self.curve.append(point)
        if self.after_bar:
            self.after_bar(ctx, point)


def run(feed, strategy, broker: PaperBroker, logs: list | None = None, after_bar=None, risk=None,
        not_before: dict | None = None) -> list:
    """Drive `strategy` over `feed`. Returns the equity curve as (time, equity, spot) rows. `risk`: Orders & stops.
    `not_before`: {date: time} to start each of those days later (see Stepper)."""
    stepper = Stepper(feed, strategy, broker, logs, after_bar, risk=risk, not_before=not_before)
    for now in feed:
        stepper.step(now)
    return stepper.curve


@dataclass
class Result:
    metrics: dict
    equity: pd.DataFrame
    trades: pd.DataFrame
    cycles: pd.DataFrame
    fills: pd.DataFrame
    logs: list = field(default_factory=list)


def make_broker(cfg: dict) -> PaperBroker:
    ex = cfg["execution"]
    return PaperBroker(cfg["account"]["capital"], CostModel(**cfg["costs"]), ex["slippage_pct"], cfg["market"]["tick_size"],
                       cfg.get("cost_history"), ex.get("min_half_spread"), ex.get("cost_stress", 1.0))


def build_result(broker: PaperBroker, curve: list, logs: list) -> Result:
    equity = pd.DataFrame(curve, columns=["time", "equity", "spot"]).set_index("time")
    trades = pd.DataFrame([asdict(t) for t in broker.trades])
    fills = pd.DataFrame([asdict(f) for f in broker.fills])
    cycles = add_excursions(cycles_table(trades), equity)
    metrics = compute_metrics(equity, trades, cycles, broker.capital, broker.total_costs)
    add_reference(metrics, equity, broker.total_costs)
    add_robustness(metrics, cycles, broker.total_costs)
    ms = getattr(broker, "margin_stats", None)
    if ms and ms["peak"] > 0:
        metrics["Peak margin (estimated)"] = round(ms["peak"], 0)
        metrics["Average margin in a trade (estimated)"] = round(ms["sum"] / max(ms["n"], 1), 0)
        net = metrics.get("Net P&L")
        if isinstance(net, (int, float)):
            metrics["Return on peak margin %"] = round(net / ms["peak"] * 100, 2)
    if ms and ms["over"]:
        metrics["Entries needing more margin than the account had"] = ms["over"]
    if ms and ms["refused"]:
        metrics["Entries skipped for margin"] = ms["refused"]
    if len(fills):
        priced = fills[fills.source != "settlement"]
        metrics["Fills on real option prices"] = f"{(priced.source == 'market').mean() * 100:.0f}%" if len(priced) else "n/a"
        if len(priced) and (priced.source == "model+smile").any():
            metrics["Estimates corrected by NSE closes"] = f"{(priced.source == 'model+smile').mean() * 100:.0f}%"
    return Result(metrics, equity, trades, cycles, fills, logs)


def add_reference(metrics: dict, equity: pd.DataFrame, total_costs: float) -> None:
    """Yardsticks to judge the result against: the index over the same period, and the P&L before charges
    (a strategy that only wins before charges, or only matches the index's drift, has no edge)."""
    net = metrics.get("Net P&L")
    if isinstance(net, (int, float)):
        metrics["P&L before charges"] = round(net + total_costs, 0)
    spot = equity["spot"].dropna() if "spot" in equity else None
    if spot is not None and len(spot) > 1 and spot.iloc[0] > 0:
        metrics["Index move over the period %"] = round((spot.iloc[-1] / spot.iloc[0] - 1) * 100, 2)


def add_robustness(metrics: dict, cycles: pd.DataFrame, total_costs: float, runs: int = 1000) -> None:
    """How much of the result is the order the cycles came in, and how it stands up to dearer trading:
    the cycles' P&Ls drawn again in random order (with replacement) 1,000 times, and the P&L with 50% more charges."""
    net = metrics.get("Net P&L")
    if isinstance(net, (int, float)):
        metrics["Net with charges 50% higher"] = round(net - 0.5 * total_costs, 0)
    if cycles is None or len(cycles) < 10 or "net_pnl" not in cycles:
        return
    import numpy as np
    pnl = pd.to_numeric(cycles["net_pnl"], errors="coerce").dropna().to_numpy(float)
    if len(pnl) < 10:
        return
    rng = np.random.default_rng(12345)                       # fixed: the same run always shows the same figures
    draws = rng.choice(pnl, size=(runs, len(pnl)), replace=True)
    paths = draws.cumsum(axis=1)
    dd = (paths - np.maximum.accumulate(np.maximum(paths, 0), axis=1)).min(axis=1)
    metrics["Monte Carlo: max drawdown, 1 in 20 worse than"] = round(float(np.percentile(dd, 5)), 0)
    metrics["Monte Carlo: chance of a losing period %"] = round(float((paths[:, -1] < 0).mean() * 100), 1)


def _days_with_real_atm(df: pd.DataFrame, store, cfg: dict) -> set:
    """Days whose nearest priced expiry has real prices for the at-the-money call and put that day
    (ATM taken from NIFTY about half an hour after the open)."""
    from datetime import datetime as _dt
    step, und = int(cfg["market"]["strike_step"]), cfg["market"]["underlying"]
    ok = set()
    priced = store.real_days()
    for d, g in df.groupby(df.index.date):
        if d not in priced:
            continue
        exps = store.expiries_on(d)
        if not exps:
            continue
        spot = float(g["close"].iloc[min(len(g) - 1, max(len(g) // 12, 1))])
        k = int(round(spot / step) * step)
        when = _dt.combine(d, time(15, 0))
        if all(store.price(Contract(und, exps[0], k, r), when, stale_ok=True) is not None for r in ("CE", "PE")):
            ok.add(d)
    return ok


def real_only(cfg: dict) -> bool:
    """Backtests and replays use real option prices only (pricing.real_only, on unless set to false)."""
    return bool(cfg.get("pricing", {}).get("real_only", True))


def real_price_bars(df: pd.DataFrame, store, cfg: dict) -> pd.DataFrame:
    """Keep only the days that have real option prices (Zerodha recordings and 1-minute candles,
    TrueData history) for the at-the-money call and put of the nearest priced expiry."""
    days = _days_with_real_atm(df, store, cfg)
    out = df[pd.Index(df.index.date).isin(days)]
    if out.empty:
        have = sorted(store.real_days())
        span = (f" Real option prices are stored for {have[0]:%d %b %Y} to {have[-1]:%d %b %Y}: pick dates "
                "in that range, or get more data (Data & broker tab)." if have else
                " No real option prices are stored yet: get them in the Data & broker tab.")
        raise ValueError("No days with real option prices in the selected range." + span)
    return out


def price_metrics(res: Result, feed, df: pd.DataFrame) -> None:
    st = getattr(feed, "price_stats", None) or {}
    res.metrics["Option prices"] = "real only"
    res.metrics["Days tested"] = len({t.date() for t in df.index})
    if st.get("next_trade"):
        res.metrics["Fills that waited for the next trade"] = st["next_trade"]
    if st.get("old_fill"):
        res.metrics["Fills at an old price (no trade within 15 min)"] = st["old_fill"]
    if st.get("previous_day"):
        res.metrics["Prices from the previous day"] = st["previous_day"]
    if st.get("missing"):
        res.metrics["Prices with no real trade (modelled)"] = st["missing"]
    if st.get("inside_bar"):
        res.metrics["Fills inside a bar (option high/low)"] = st["inside_bar"]
    if st.get("large_orders"):
        res.metrics["Orders over the volume limit (would move the price)"] = st["large_orders"]
    if st.get("thin_strikes"):
        res.metrics["Strikes skipped as thinly traded"] = st["thin_strikes"]


def backtest(cfg: dict, strategy, dataset: str, start=None, end=None, risk=None, not_before: dict | None = None) -> Result:
    from .sec1_feed import SEC1_DATASET
    if dataset == SEC1_DATASET:
        return backtest_sec1(cfg, strategy, start, end, risk, not_before)
    from .feeds import HistoricalFeed, OptionStore, load_dataset
    df = load_dataset(dataset, start, end)
    store = OptionStore(cfg["market"]["underlying"])
    real = real_only(cfg)
    dropped = []
    if real:
        before = sorted({t.date() for t in df.index})
        df = real_price_bars(df, store, cfg)
        kept = {t.date() for t in df.index}
        dropped = [d for d in before if d not in kept]
    feed = HistoricalFeed(df, cfg, store, real_only=real)
    broker = make_broker(cfg)
    logs: list = []
    risk = risk if risk is not None else cfg.get("risk")
    curve = run(feed, strategy, broker, logs, risk=risk or None, not_before=not_before)
    res = build_result(broker, curve, logs)
    ex = cfg.get("execution", {})
    stress, delay = float(ex.get("cost_stress", 1.0) or 1.0), int(ex.get("fill_delay_seconds", 0) or 0)
    res.metrics["Fills"] = ("conservative" if stress > 1 or delay else "normal") + \
        f" (spread x{stress:g}" + (f", {delay} s after the decision" if delay else "") + ")"
    if dropped:
        res.metrics["Days without real option prices (left out)"] = ", ".join(str(d) for d in dropped)
    if real:
        price_metrics(res, feed, df)
        if feed.missing:
            res.logs.append("Prices with no real trade nearby (modelled): " + "; ".join(feed.missing[:20]))
    return res


def backtest_sec1(cfg: dict, strategy, start=None, end=None, risk=None, not_before: dict | None = None) -> Result:
    """Step the strategy every second on the recorded 1-second bars (algolab/sec1_feed.py), as paper does."""
    from .config import to_date
    from .feeds import Feed, OptionStore
    from .sec1_feed import Sec1Feed, index_close_from_minute_set, price_metrics as sec1_price_metrics, sec1_days
    days = sec1_days(start, end)
    if not days:
        raise ValueError("No 1-second bars stored for those days (they are kept for the last few trading days only).")
    lo, hi = (to_date(start) if start is not None else days[0]), (to_date(end) if end is not None else days[-1])
    cal = Feed(cfg)
    want = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
    feed = Sec1Feed(cfg, days, store=OptionStore(cfg["market"]["underlying"]), index_close=index_close_from_minute_set)
    if not feed.days:
        raise ValueError("The 1-second bars of those days are incomplete: " + "; ".join(feed.skipped_days))
    gaps = [d for d in want if cal.is_trading_day(d) and d not in set(feed.days)]
    broker = make_broker(cfg)
    logs: list = []
    risk = risk if risk is not None else cfg.get("risk")
    curve = run(feed, strategy, broker, logs, risk=risk or None, not_before=not_before)
    res = build_result(broker, curve, logs)
    res.metrics["Fills"] = "best bid / ask known at that second (as paper)"
    sec1_price_metrics(res, feed)
    if gaps:
        res.metrics["Trading days without 1-second bars (left out)"] = ", ".join(str(d) for d in gaps)
    return res
