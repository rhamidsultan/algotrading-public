"""Orders & stops (algolab/risk.py): leg stops and trailing, position and day limits, the same for every strategy."""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from algolab import risk  # noqa: E402
from algolab.config import load_config  # noqa: E402
from algolab.engine import backtest  # noqa: E402
from algolab.models import Contract, Position, Trade  # noqa: E402
from algolab.strategy import load_strategies  # noqa: E402


def S(**kw):
    return risk.settings({"stops": "framework", **kw})


def test_leg_trailing_matches_the_doubler_rule():
    s = S(leg_unit="points", leg_stop=20, leg_trail_every=1, leg_trail_by=2, leg_trail_gap=10)
    lvl = lambda best, prev=None: risk.leg_stop_level(s, 65, 56.0, best, prev)  # noqa: E731
    assert lvl(56) == 36 and lvl(61) == 46 and lvl(66) == 56 and lvl(80) == 70        # bought at 56 (doubler's example)
    assert lvl(70, prev=60) == 60                                                       # never moves back down


def test_sold_leg_stop_above_and_trailing_down_in_pct():
    s = S(leg_stop=30, leg_trail_every=10, leg_trail_after=20)                          # % of the sold price
    lvl = lambda best: risk.leg_stop_level(s, -65, 100.0, best)  # noqa: E731
    assert lvl(100) == 130                     # 30% above the sold price
    assert lvl(85) == 130                      # gained 15: trailing not started (starts after 20)
    assert lvl(80) == 130                      # gained 20: trailing starts from here
    assert lvl(70) == 120 and lvl(50) == 100   # each further 10 gained moves it down 10


class _Broker:
    def __init__(self):
        self.positions, self.trades = {}, []


class _Ctx:
    """Just enough of the engine's Context for the risk manager."""
    def __init__(self, prices, equity=300000.0):
        self.broker, self.prices, self.equity, self.now, self.closed, self.logs = _Broker(), prices, equity, datetime(2026, 10, 20, 10, 0), [], []

    @property
    def positions(self):
        return self.broker.positions

    def price(self, c):
        return self.prices[c]

    def close(self, c, reason=""):
        self.closed.append((c.label, reason))
        self.broker.positions.pop(c, None)

    def close_all(self, reason=""):
        for c in list(self.broker.positions):
            self.close(c, reason)

    def log(self, m):
        self.logs.append(m)


def _c(k, r):
    return Contract("NIFTY", date(2026, 10, 20), k, r)


def test_manager_leg_stop_position_limit_and_day_limit():
    ce, pe = _c(25000, "CE"), _c(25000, "PE")
    ctx = _Ctx({ce: 100.0, pe: 100.0})
    ctx.broker.positions = {ce: Position(ce, -65, 100.0, ctx.now, "001", 20.0), pe: Position(pe, -65, 100.0, ctx.now, "001", 20.0)}
    m = risk.RiskManager({"stops": "framework", "leg_stop": 30})
    assert m.check(ctx) is False and not ctx.closed
    ctx.prices[ce] = 131.0
    m.check(ctx)
    assert ctx.closed[0][0] == ce.label and "stop 130.00" in ctx.closed[0][1] and pe in ctx.broker.positions
    # whole position: loss limit closes every leg of the group and rests the strategy until tomorrow
    ctx2 = _Ctx({ce: 100.0, pe: 100.0})
    ctx2.broker.positions = {ce: Position(ce, -65, 100.0, ctx2.now, "002", 20.0), pe: Position(pe, -65, 100.0, ctx2.now, "002", 20.0)}
    m2 = risk.RiskManager({"stops": "both", "cycle_loss": 3000})
    m2.check(ctx2)
    ctx2.prices[ce] = 150.0                                  # -3250 - 40 charges on the open legs
    assert m2.check(ctx2) is True and not ctx2.broker.positions and "Position loss limit" in ctx2.closed[0][1]
    # day loss limit
    ctx3 = _Ctx({ce: 100.0})
    ctx3.broker.positions = {ce: Position(ce, -65, 100.0, ctx3.now, "003", 0.0)}
    m3 = risk.RiskManager({"stops": "framework", "day_loss": 5000})
    m3.check(ctx3)
    ctx3.equity -= 6000
    assert m3.check(ctx3) is True and "Day loss limit" in ctx3.closed[0][1]
    ctx3.now = datetime(2026, 10, 21, 9, 30)                  # next day: trades again
    assert m3.check(ctx3) is False


def test_own_means_off_and_framework_switches_strategy_stops_off():
    assert not risk.active(risk.settings({"stops": "own", "leg_stop": 30}))
    assert risk.active(risk.settings({"stops": "both", "day_loss": 1}))
    cls = load_strategies()["short_straddle"]
    assert risk.strategy_params(cls, {"lots": 2}, {"stops": "framework"}) == {"lots": 2, "stop_loss_pct": 0.0, "target_pct": 0.0}
    assert risk.strategy_params(cls, {"lots": 2}, {"stops": "both"}) == {"lots": 2}
    with pytest.raises(ValueError):
        risk.settings({"stops": "sometimes"})
    with pytest.raises(ValueError):
        risk.settings({"leg_stop": -1})


def _bt(r):
    cfg = load_config()
    cfg["pricing"]["real_only"] = False
    cfg["pricing"]["smile"] = "off"
    cls = load_strategies()["intraday_straddle"]
    strat = cls(**risk.strategy_params(cls, {}, r))
    return backtest(cfg, strat, "SAMPLE_NIFTY_5minute", date(2025, 1, 1), date(2025, 1, 31), risk=r)


def test_backtest_uses_the_framework():
    base = _bt(None)
    own = _bt({"stops": "own", "leg_stop": 10})
    assert own.metrics["Net P&L"] == base.metrics["Net P&L"]                 # "own": nothing changes
    fw = _bt({"stops": "framework", "leg_stop": 10})
    assert any("Leg stop" in str(r) for r in fw.trades["reason"]) and fw.metrics["Net P&L"] != base.metrics["Net P&L"]
