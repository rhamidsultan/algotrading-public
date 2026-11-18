"""open_high_retest: after 13:00, buy an option whose open is still the day's high when it climbs back within 15
points of the open; target the open; stop 10 below entry, trailing best - (open - best) near the target."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab.models import Contract  # noqa: E402
from algolab.strategy import load_strategies  # noqa: E402

DAY = date(2026, 10, 19)          # a Monday
EXP = date(2026, 10, 20)          # Tuesday expiry
CE = Contract("NIFTY", EXP, 25000, "CE")


class FakeCtx:
    """Every watched option is priced from `px` (default 1.0, under min_open, so it is dropped at once)."""

    def __init__(self, spot=25000.0):
        self.spot, self.now = spot, datetime(DAY.year, DAY.month, DAY.day, 9, 16)
        self.px, self.positions, self.fills, self.logs = {}, {}, [], []
        self.strike_step = 50

    def pick_expiry(self, min_dte=0):
        return EXP

    def atm_strike(self):
        return int(round(self.spot / 50) * 50)

    def option(self, e, k, r):
        return Contract("NIFTY", e, int(k), r)

    def listed_strikes(self, e, r):
        return None

    def quotes(self, cs):
        return {c: SimpleNamespace(last=self.px.get(c, 1.0), source="market") for c in cs}

    def buy(self, c, lots=1, reason="", group=""):
        self.positions[c] = SimpleNamespace(qty=65 * lots)
        self.fills.append(("BUY", c.strike, c.right, self.px[c], reason, self.now.time()))
        return SimpleNamespace(price=self.px[c])

    def close(self, c, reason=""):
        if self.positions.pop(c, None) is not None:
            self.fills.append(("SELL", c.strike, c.right, self.px[c], reason, self.now.time()))

    def close_all(self, reason=""):
        for c in list(self.positions):
            self.close(c, reason)

    def log(self, m):
        self.logs.append(m)


def _strategy(**kw):
    return load_strategies()["open_high_retest"](**kw)


def _walk(s, ctx, prices, start, contract=CE):
    t = datetime(DAY.year, DAY.month, DAY.day, *start)
    for px in prices:
        ctx.now = t
        ctx.px[contract] = px
        s.on_bar(ctx)
        t += timedelta(minutes=1)


def _day(s, ctx, morning=(220, 200, 190)):
    """Opens at 220 (the high) and falls during the morning."""
    _walk(s, ctx, morning, (9, 16))


def test_example_from_the_idea_trails_to_216_and_exits():
    s, ctx = _strategy(), FakeCtx()
    _day(s, ctx)
    _walk(s, ctx, [195, 206], (13, 0))                    # armed below 205, bought at 206 (within 15 of 220)
    assert ctx.fills[0][:4] == ("BUY", 25000, "CE", 206)
    w = s.day["watch"]["25000CE"]
    assert w["sl"] == 196                                 # 10 below the entry
    _walk(s, ctx, [216], (13, 5))
    assert w["sl"] == 212                                 # 216 - (220 - 216)
    _walk(s, ctx, [218], (13, 6))
    assert w["sl"] == 216                                 # 218 - (220 - 218)
    _walk(s, ctx, [217, 216], (13, 7))
    assert [f[0] for f in ctx.fills] == ["BUY", "SELL"] and ctx.fills[-1][3] == 216 and "Stop" in ctx.fills[-1][4]


def test_target_is_the_open():
    s, ctx = _strategy(), FakeCtx()
    _day(s, ctx)
    _walk(s, ctx, [190, 208, 214, 220], (13, 0))
    assert [f[0] for f in ctx.fills] == ["BUY", "SELL"] and ctx.fills[-1][3] == 220 and "Target" in ctx.fills[-1][4]


def test_stop_ten_below_entry_and_one_trade_per_option():
    s, ctx = _strategy(), FakeCtx()
    _day(s, ctx)
    _walk(s, ctx, [190, 207, 197], (13, 0))
    assert [f[3] for f in ctx.fills] == [207, 197]
    _walk(s, ctx, [190, 210], (13, 30))                   # same option again: no second trade
    assert len(ctx.fills) == 2


def test_stop_never_moves_down_and_trail_starts_past_halfway():
    s = _strategy()
    assert s.stop_level(206, 206, 220) == 196             # 2 x 206 - 220 = 192 < 196
    assert s.stop_level(206, 208, 220) == 196             # 196 exactly: halfway point
    assert s.stop_level(206, 210, 220) == 200
    assert _strategy(trail=False).stop_level(206, 218, 220) == 196


def test_no_entry_when_open_is_not_the_high():
    s, ctx = _strategy(), FakeCtx()
    _walk(s, ctx, [220, 221, 190], (9, 16))               # went above the open: dropped
    _walk(s, ctx, [190, 210], (13, 0))
    assert not ctx.fills and s.day["watch"]["25000CE"]["state"] == "dropped"


def test_nothing_before_13_and_no_buy_without_approach():
    s, ctx = _strategy(), FakeCtx()
    _walk(s, ctx, [220, 190, 210], (9, 16))               # back near the open in the morning: too early
    assert not ctx.fills
    _walk(s, ctx, [210, 212], (13, 0))                    # at 13:00 already in the zone: not armed yet
    assert not ctx.fills
    _walk(s, ctx, [200, 209], (13, 5))                    # drops below 205, then back: bought
    assert ctx.fills[0][3] == 209
    s2, ctx2 = _strategy(require_approach=False), FakeCtx()
    _walk(s2, ctx2, [220, 190, 210], (9, 16))
    _walk(s2, ctx2, [210], (13, 0))
    assert ctx2.fills[0][3] == 210


def test_late_entries_and_square_off():
    s, ctx = _strategy(), FakeCtx()
    _day(s, ctx)
    _walk(s, ctx, [190, 210], (15, 1))                    # after 15:00: no entry
    assert not ctx.fills
    s2, ctx2 = _strategy(), FakeCtx()
    _day(s2, ctx2)
    _walk(s2, ctx2, [190, 210, 212], (14, 58))
    _walk(s2, ctx2, [213], (15, 15))
    assert [f[0] for f in ctx2.fills] == ["BUY", "SELL"] and ctx2.fills[-1][4] == "Square-off"


class FeedCtx(FakeCtx):
    """A feed that knows each option's open and high so far today (Zerodha quotes, real-price backtests)."""
    gives_day_ohlc = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ranges = {}

    def day_ranges(self, cs):
        return {c: self.ranges.get(c) for c in cs}


def test_open_and_high_come_from_the_feed_so_a_mid_day_start_trades():
    s, ctx = _strategy(), FeedCtx()
    ctx.ranges[CE] = (220.0, 220.0)                       # opened at 220, never above
    _walk(s, ctx, [180], (11, 0))                         # before 13:00: nothing to do
    assert s.day is None and not ctx.fills
    _walk(s, ctx, [190, 207], (13, 40))                   # session started at 13:40
    assert ctx.fills[0][:4] == ("BUY", 25000, "CE", 207) and s.day["watch"]["25000CE"]["open"] == 220
    _walk(s, ctx, [220], (13, 45))
    assert ctx.fills[-1][0] == "SELL" and "Target" in ctx.fills[-1][4]


def test_feed_high_above_the_open_drops_the_option():
    s, ctx = _strategy(), FeedCtx()
    ctx.ranges[CE] = (220.0, 224.5)                       # traded above its open this morning
    _walk(s, ctx, [190, 207], (13, 0))
    assert not ctx.fills and s.day["watch"]["25000CE"]["state"] == "dropped"
    s2, ctx2 = _strategy(high_tolerance=5), FeedCtx()
    ctx2.ranges[CE] = (220.0, 224.5)
    _walk(s2, ctx2, [190, 207], (13, 0))
    assert ctx2.fills and ctx2.fills[0][3] == 207


def test_store_day_range_uses_only_what_was_known(tmp_path):
    from algolab.feeds import OptionStore
    c = Contract("NIFTY", EXP, 25000, "CE")
    d = tmp_path / "options" / "NIFTY" / EXP.isoformat()
    d.mkdir(parents=True)
    (d / "25000CE.csv").write_text("time,last,bid,ask\n2026-10-16 15:29:00,300,,\n2026-10-19 09:16:00,220,,\n"
                                    "2026-10-19 09:30:00,215,,\n2026-10-19 11:00:00,230,,\n2026-10-19 12:00:00,190,,\n")
    st = OptionStore("NIFTY", root=tmp_path / "options")
    assert st.day_range(c, datetime(2026, 10, 19, 9, 0)) is None          # not traded yet today
    assert st.day_range(c, datetime(2026, 10, 19, 10, 0)) == (220.0, 220.0)
    assert st.day_range(c, datetime(2026, 10, 19, 13, 0)) == (220.0, 230.0)


def test_cheap_options_and_max_open():
    s, ctx = _strategy(min_open=250.0), FakeCtx()
    _day(s, ctx)
    assert s.day["watch"]["25000CE"]["state"] == "dropped"
    s2, ctx2 = _strategy(max_open=1), FakeCtx()
    pe = Contract("NIFTY", EXP, 25000, "PE")
    for px_ce, px_pe, hm in ((220, 180, (9, 16)), (190, 150, (13, 0)), (210, 170, (13, 1))):
        ctx2.px[pe] = px_pe
        _walk(s2, ctx2, [px_ce], hm)
    assert len([f for f in ctx2.fills if f[0] == "BUY"]) == 1


def test_state_survives_a_restart():
    s, ctx = _strategy(), FakeCtx()
    _day(s, ctx)
    _walk(s, ctx, [190, 206], (13, 0))
    s2 = _strategy()
    s2.set_state(s.get_state())
    assert s2.day["watch"]["25000CE"]["entry"] == 206 and "in at 206" in s2.status(ctx)["note"]


def test_runs_in_the_engine_on_sample_data():
    from algolab.config import load_config
    from algolab.engine import build_result, make_broker, run
    from algolab.feeds import HistoricalFeed, make_sample
    cfg = load_config()
    cfg["pricing"]["smile"] = "off"
    cfg["pricing"]["real_only"] = False                   # synthetic data: options priced by the model
    df = make_sample("2026-01-01", "2026-03-31")
    df.index = df["datetime"] + timedelta(minutes=5)
    broker, logs = make_broker(cfg), []
    res = build_result(broker, run(HistoricalFeed(df, cfg), _strategy(), broker, logs), logs)
    assert not broker.positions                           # flat after every day
    if len(res.fills):
        assert all(f.time.time() >= datetime(2026, 1, 1, 13, 0).time() for f in res.fills.itertuples())


def test_live_quotes_carry_zerodha_open_and_high():
    from algolab.engine import Context
    from algolab.kite import KiteFeed
    from algolab.models import Quote
    assert KiteFeed.gives_day_ohlc
    feed = SimpleNamespace(gives_day_ohlc=True, quotes=lambda cs: {
        c: Quote(207.0, 206.5, 207.5, "market", 220.0, 220.0) if c.strike == 25000 else Quote(5.0, source="market")
        for c in cs})
    ctx = Context(feed, broker=None, logs=[])
    pe = Contract("NIFTY", EXP, 24000, "PE")
    assert ctx.gives_day_ohlc and ctx.day_ranges([CE, pe]) == {CE: (220.0, 220.0), pe: None}
    parsed = KiteFeed._parse(SimpleNamespace(_record=lambda c, q: None), CE,
                             {"last_price": 207.0, "ohlc": {"open": 220.0, "high": 221.0}, "depth": {}})
    assert (parsed.day_open, parsed.day_high) == (220.0, 221.0)
