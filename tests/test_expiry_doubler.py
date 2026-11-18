"""expiry_doubler: buy the cheap option once it doubles, stop up 2 per point gained to 10 behind the high, re-enter 10 above."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab.models import Contract  # noqa: E402
from algolab.strategy import load_strategies  # noqa: E402

EXP = date(2026, 10, 20)          # a Tuesday expiry


class FakeCtx:
    """Prices per contract come from `script`; the watched strikes are priced from `chain` at watch time."""

    def __init__(self, chain, spot=25000.0):
        self.chain, self.spot, self.now = chain, spot, datetime(2026, 10, 20, 9, 15)
        self.px, self.positions, self.fills, self.logs = {}, {}, [], []
        self.strike_step = 50

    def expiries(self):
        return [EXP, EXP + timedelta(days=7)]

    def option(self, e, k, r):
        return Contract("NIFTY", e, int(k), r)

    def listed_strikes(self, e, r):
        return None

    def _last(self, c):
        return self.px.get(c, self.chain.get((c.strike, c.right), 0.0))

    def quotes(self, cs):
        return {c: SimpleNamespace(last=self._last(c)) for c in cs}

    def price(self, c):
        return self._last(c)

    def buy(self, c, lots=1, reason="", group=""):
        self.positions[c] = SimpleNamespace(qty=65 * lots)
        self.fills.append(("BUY", c.strike, c.right, self._last(c), reason, self.now.time()))
        return SimpleNamespace(price=self._last(c))

    def close(self, c, reason=""):
        if self.positions.pop(c, None) is not None:
            self.fills.append(("SELL", c.strike, c.right, self._last(c), reason, self.now.time()))

    def close_all(self, reason=""):
        for c in list(self.positions):
            self.close(c, reason)

    def log(self, m):
        self.logs.append(m)


CHAIN = {(25300, "CE"): 28.0, (25350, "CE"): 19.0, (25250, "CE"): 41.0,
         (24700, "PE"): 27.0, (24750, "PE"): 36.0, (24650, "PE"): 18.0}


def _strategy(**kw):
    return load_strategies()["expiry_doubler"](**kw)


def _walk(s, ctx, contract, prices, start=(11, 1)):
    t = datetime(2026, 10, 20, *start)
    for px in prices:
        ctx.now = t
        ctx.px[contract] = px
        s.on_bar(ctx)
        t += timedelta(minutes=1)


def test_watches_the_dearest_option_under_30_on_each_side():
    s, ctx = _strategy(), FakeCtx(CHAIN)
    ctx.now = datetime(2026, 10, 20, 11, 0)
    s.on_bar(ctx)
    assert s.sides["CE"]["strike"] == 25300 and s.sides["CE"]["ref"] == 28.0
    assert s.sides["PE"]["strike"] == 24700 and s.sides["PE"]["ref"] == 27.0


def test_not_on_other_days():
    s, ctx = _strategy(), FakeCtx(CHAIN)
    ctx.now = datetime(2026, 10, 19, 9, 30)                  # Monday, expiry is tomorrow
    s.on_bar(ctx)
    assert not s.sides and not ctx.fills


def test_buys_on_doubling_and_trails_the_stop():
    s, ctx = _strategy(sides="CE"), FakeCtx(CHAIN)
    ce = ctx.option(EXP, 25300, "CE")
    ctx.now = datetime(2026, 10, 20, 11, 0)
    s.on_bar(ctx)
    _walk(s, ctx, ce, [40, 55.9, 56, 60])                    # 56 = 2 x 28: bought at 56
    assert ctx.fills[0][:4] == ("BUY", 25300, "CE", 56)
    assert s.sides["CE"]["pos"]["sl"] == 56 - 20 + 8          # high 60: 4 points up -> stop up 8, to 44
    _walk(s, ctx, ce, [76, 70], start=(11, 10))               # high 76: stop follows 10 behind -> 66
    assert s.sides["CE"]["pos"]["sl"] == 66
    _walk(s, ctx, ce, [66], start=(11, 20))
    assert ctx.fills[-1][0] == "SELL" and ctx.fills[-1][3] == 66


def test_stop_never_moves_down_and_steps_are_whole():
    s = _strategy()
    assert s.stop_level(56, 56) == 36 and s.stop_level(56, 56.9) == 36 and s.stop_level(56, 57) == 38
    assert s.stop_level(56, 61) == 46 and s.stop_level(56, 66) == 56     # 10 up: stop at the entry
    assert s.stop_level(56, 80) == 70 and s.stop_level(56, 100) == 90    # then 10 behind the high
    assert _strategy(trail_gap=0).stop_level(56, 60) == 44            # no gap limit: 2 per point


def test_reenters_10_above_the_stop_out_and_stops_after_max_entries():
    s, ctx = _strategy(sides="CE", max_entries=2), FakeCtx(CHAIN)
    ce = ctx.option(EXP, 25300, "CE")
    ctx.now = datetime(2026, 10, 20, 11, 0)
    s.on_bar(ctx)
    _walk(s, ctx, ce, [56, 36])                               # in at 56, out at 36
    _walk(s, ctx, ce, [45, 45.9, 46], start=(12, 0))          # back in at 36 + 10
    buys = [f for f in ctx.fills if f[0] == "BUY"]
    assert [b[3] for b in buys] == [56, 46] and "Re-entry" in buys[1][4]
    assert s.sides["CE"]["pos"]["sl"] == 26                  # 46 - 20
    _walk(s, ctx, ce, [26, 60, 80], start=(12, 10))           # stopped again; max 2 entries: no more
    assert [f[0] for f in ctx.fills] == ["BUY", "SELL", "BUY", "SELL"]


def test_square_off_and_no_late_entries():
    s, ctx = _strategy(sides="PE"), FakeCtx(CHAIN)
    pe = ctx.option(EXP, 24700, "PE")
    ctx.now = datetime(2026, 10, 20, 11, 0)
    s.on_bar(ctx)
    _walk(s, ctx, pe, [60], start=(14, 50))                   # doubled, but after 14:45
    assert not ctx.fills
    s3, ctx3 = _strategy(sides="PE"), FakeCtx(CHAIN)
    ctx3.now = datetime(2026, 10, 20, 10, 30)
    s3.on_bar(ctx3)
    assert not s3.sides                                       # nothing before 11:00
    s2, ctx2 = _strategy(sides="PE"), FakeCtx(CHAIN)
    ctx2.now = datetime(2026, 10, 20, 11, 0)
    s2.on_bar(ctx2)
    _walk(s2, ctx2, pe, [54, 70], start=(14, 0))
    _walk(s2, ctx2, pe, [72], start=(15, 15))
    assert [f[0] for f in ctx2.fills] == ["BUY", "SELL"] and ctx2.fills[-1][4] == "Square-off"


def test_low_reference_measures_from_the_lowest_price():
    s, ctx = _strategy(sides="CE", reference="low"), FakeCtx(CHAIN)
    ce = ctx.option(EXP, 25300, "CE")
    ctx.now = datetime(2026, 10, 20, 11, 0)
    s.on_bar(ctx)
    _walk(s, ctx, ce, [20, 12, 23.9, 24])                     # low 12 -> buys at 24
    assert ctx.fills and ctx.fills[0][3] == 24


def test_state_survives_a_restart():
    s, ctx = _strategy(sides="CE"), FakeCtx(CHAIN)
    ctx.now = datetime(2026, 10, 20, 11, 0)
    s.on_bar(ctx)
    _walk(s, ctx, ctx.option(EXP, 25300, "CE"), [56])
    s2 = _strategy(sides="CE")
    s2.set_state(s.get_state())
    assert s2.sides["CE"]["pos"]["entry"] == 56 and s2.status(ctx)["note"].startswith("CE 25300: in at 56")


def test_runs_in_the_engine_on_sample_data():
    from algolab.config import load_config
    from algolab.engine import build_result, make_broker, run
    from algolab.feeds import HistoricalFeed, make_sample
    cfg = load_config()
    cfg["pricing"]["smile"] = "off"
    cfg["pricing"]["real_only"] = False                       # synthetic data: options priced by the model
    df = make_sample("2026-01-01", "2026-04-30")
    df.index = df["datetime"] + timedelta(minutes=5)
    broker, logs = make_broker(cfg), []
    res = build_result(broker, run(HistoricalFeed(df, cfg), _strategy(), broker, logs), logs)
    assert not broker.positions                               # always flat after the day
    days = {f.time.date() for f in res.fills.itertuples()} if len(res.fills) else set()
    assert all(d.weekday() == 1 for d in days if d >= date(2025, 9, 1))   # NIFTY expiries are Tuesdays
    assert any("Watching" in m for m in map(str, logs))
