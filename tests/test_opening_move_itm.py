"""opening_move_itm: the first minute's high and low are the day's high and low; until 09:45 buy an in-the-money
call when NIFTY breaks the high, or put when it breaks the low; 5-point trailing stop on the option, one trade a day,
square-off at 15:15."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab.config import load_config  # noqa: E402
from algolab.engine import build_result, make_broker, run  # noqa: E402
from algolab.feeds import HistoricalFeed, make_sample  # noqa: E402
from algolab.models import Contract  # noqa: E402
from algolab.strategy import load_strategies  # noqa: E402

DAY = date(2026, 10, 19)          # a Monday
EXP = date(2026, 10, 20)          # Tuesday expiry


class FakeCtx:
    def __init__(self, spot=25010.0):
        self.spot, self.now = spot, datetime(DAY.year, DAY.month, DAY.day, 9, 15)
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

    def price(self, c):
        return self.px.get(c, 100.0)

    def buy(self, c, lots=1, reason="", group=""):
        self.positions[c] = SimpleNamespace(qty=65 * lots)
        self.fills.append(("BUY", c.strike, c.right, self.price(c), reason, self.now.time()))
        return SimpleNamespace(price=self.price(c), qty=65 * lots)

    def close(self, c, reason=""):
        if self.positions.pop(c, None) is not None:
            self.fills.append(("SELL", c.strike, c.right, self.price(c), reason, self.now.time()))

    def close_all(self, reason=""):
        for c in list(self.positions):
            self.close(c, reason)

    def log(self, m):
        self.logs.append(m)


def _s(**kw):
    return load_strategies()["opening_move_itm"](**kw)


def _at(s, ctx, hm, spot=None, prices=None, sec=0, day=DAY):
    ctx.now = datetime(day.year, day.month, day.day, hm[0], hm[1], sec)
    if spot is not None:
        ctx.spot = spot
    for c, v in (prices or {}).items():
        ctx.px[c] = v
    s.on_bar(ctx)


def _range(s, ctx):
    """First minute: 25010, 25025 (high), 24995 (low). At 09:16 NIFTY 25012, inside the range: no trade yet."""
    _at(s, ctx, (9, 15), 25010.0)
    _at(s, ctx, (9, 15), 25025.0, sec=20)
    _at(s, ctx, (9, 15), 24995.0, sec=40)
    _at(s, ctx, (9, 16), 25012.0)


def _break_up(s, ctx, call_px=250.0, hm=(9, 21)):
    """Breaks the high at 25030: ATM 25050, call 100 in the money = 24950 CE."""
    c = Contract("NIFTY", EXP, 24950, "CE")
    _range(s, ctx)
    _at(s, ctx, hm, 25030.0, {c: call_px})
    return c


def test_first_minute_sets_day_high_and_low_and_no_trade_inside():
    s, ctx = _s(), FakeCtx()
    _range(s, ctx)
    _at(s, ctx, (9, 20), 25020.0)
    assert not ctx.fills and (s.day["high"], s.day["low"]) == (25025.0, 24995.0) and s.day["state"] == "watch"


def test_break_of_day_high_buys_itm_call():
    s, ctx = _s(), FakeCtx()
    c = _break_up(s, ctx)
    assert ctx.fills[0][:4] == ("BUY", 24950, "CE", 250.0) and ctx.fills[0][5].strftime("%H:%M") == "09:21"
    assert "broke the day's high 25,025.0" in ctx.fills[0][4]
    assert s.day["sl"] == 245.0 and c in ctx.positions


def test_break_of_day_low_buys_itm_put():
    s, ctx = _s(), FakeCtx()
    _range(s, ctx)
    _at(s, ctx, (9, 30), 24990.0)                         # ATM 25000 -> 25100 PE
    assert ctx.fills[0][:3] == ("BUY", 25100, "PE") and "day's low 24,995.0" in ctx.fills[0][4]


def test_break_after_first_30_minutes_is_ignored():
    s, ctx = _s(), FakeCtx()
    _range(s, ctx)
    _at(s, ctx, (9, 45), 25020.0)
    _at(s, ctx, (9, 46), 25100.0)
    assert not ctx.fills and "by 09:45" in s.day["skip"]


def test_break_buffer():
    s, ctx = _s(break_buffer=10.0), FakeCtx()
    _range(s, ctx)
    _at(s, ctx, (9, 20), 25030.0)                         # 5 above the high: not enough
    assert not ctx.fills
    _at(s, ctx, (9, 21), 25036.0)
    assert ctx.fills[0][2] == "CE"


def test_trailing_stop_follows_five_points_behind_and_never_drops():
    s, ctx = _s(), FakeCtx()
    c = _break_up(s, ctx)
    _at(s, ctx, (9, 22), prices={c: 256.0})
    assert s.day["sl"] == 251.0
    _at(s, ctx, (9, 23), prices={c: 262.0})
    assert s.day["sl"] == 257.0
    _at(s, ctx, (9, 24), prices={c: 259.0})              # pulls back: stop stays at 257
    assert s.day["sl"] == 257.0 and c in ctx.positions
    _at(s, ctx, (9, 25), prices={c: 256.5})
    assert [f[0] for f in ctx.fills] == ["BUY", "SELL"] and ctx.fills[-1][3] == 256.5 and "Stop 257.00" in ctx.fills[-1][4]


def test_initial_stop_five_below_entry_and_one_trade_a_day():
    s, ctx = _s(), FakeCtx()
    c = _break_up(s, ctx)
    _at(s, ctx, (9, 22), prices={c: 244.5})
    assert [f[3] for f in ctx.fills] == [250.0, 244.5]
    _at(s, ctx, (9, 23), 24900.0)                         # breaks the low too: no second trade
    assert len(ctx.fills) == 2 and s.day["state"] == "done"


def test_square_off_at_exit_time():
    s, ctx = _s(), FakeCtx()
    c = _break_up(s, ctx)
    _at(s, ctx, (15, 15), prices={c: 300.0})
    assert ctx.fills[-1][0] == "SELL" and ctx.fills[-1][4] == "Square-off" and not ctx.positions


def test_late_start_does_not_trade():
    s, ctx = _s(), FakeCtx()
    _at(s, ctx, (9, 40), 25100.0)
    assert not ctx.fills and "09:15" in s.day["skip"]


def test_nifty_basis_stop():
    s, ctx = _s(sl_basis="nifty"), FakeCtx()
    c = _break_up(s, ctx)
    assert s.day["sl"] == 25025.0
    _at(s, ctx, (9, 22), 25060.0, {c: 265.0})
    assert s.day["sl"] == 25055.0 and c in ctx.positions
    _at(s, ctx, (9, 23), 25054.0, {c: 262.0})
    assert ctx.fills[-1][0] == "SELL" and "NIFTY" in ctx.fills[-1][4]


def test_state_survives_restart_mid_morning():
    s, ctx = _s(), FakeCtx()
    _range(s, ctx)
    s2 = _s()
    s2.set_state(s.get_state())                           # paper engine restarted at 09:18
    _at(s2, ctx, (9, 18), 24990.0)
    assert ctx.fills[0][2] == "PE"


def test_backtest_on_one_minute_bars_trades_at_most_once_a_day_in_the_first_30_minutes():
    cfg = load_config()
    cfg["pricing"]["smile"] = "off"
    cfg["pricing"]["real_only"] = False                   # synthetic data: options priced by the model
    df = make_sample("2026-06-01", "2026-07-31", interval=1)
    df.index = df["datetime"] + timedelta(minutes=1)
    broker, logs = make_broker(cfg), []
    res = build_result(broker, run(HistoricalFeed(df, cfg), _s(), broker, logs), logs)
    buys = res.fills[res.fills.side == "BUY"]
    assert len(buys) > 10
    times = pd.to_datetime(buys.time)
    assert times.dt.date.value_counts().max() == 1
    hm = times.dt.strftime("%H:%M")
    assert ((hm >= "09:16") & (hm <= "09:45")).all()
    assert not broker.positions
    assert res.equity.equity.iloc[-1] - broker.capital == pytest.approx(res.trades.net_pnl.sum(), abs=1.0)
