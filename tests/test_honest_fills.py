"""Honest fills: spreads by premium, decisions without future prices, fills that wait for a trade or come later."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from algolab.broker import CostModel, PaperBroker  # noqa: E402
from algolab.config import load_config  # noqa: E402
from algolab.feeds import HistoricalFeed, OptionStore  # noqa: E402
from algolab.models import Contract, Quote  # noqa: E402

EXP = date(2026, 10, 13)
DAY = date(2026, 10, 13)


def test_spread_has_a_floor_by_premium_and_a_stress_factor():
    b = PaperBroker(300000, CostModel(), 0.5, 0.05, min_half_spread=[[20, 0.10], [100, 0.20]])
    assert b.fill_price("BUY", Quote(last=10.0)) == 10.10            # 0.5% would be 0.05: the floor is 0.10
    assert b.fill_price("SELL", Quote(last=30.0)) == 29.80           # under 100: at least 0.20 (0.5% would be 0.15)
    assert b.fill_price("SELL", Quote(last=56.0)) == 55.70           # 0.5% = 0.28 is above the 0.20 floor
    assert b.fill_price("BUY", Quote(last=300.0)) == 301.50          # above 100: 0.5%
    s = PaperBroker(300000, CostModel(), 0.5, 0.05, min_half_spread=[[20, 0.10], [100, 0.20]], stress=2)
    assert s.fill_price("BUY", Quote(last=10.0)) == 10.20
    assert s.fill_price("BUY", Quote(last=50.0, bid=49.8, ask=50.2)) == 50.40   # a real spread widens around its middle
    assert b.fill_price("BUY", Quote(last=50.0, bid=49.8, ask=50.2)) == 50.20   # normal: the ask as it was


def _feed(tmp_path, prices, **ex):
    """A feed for 13 Oct 2026 with one option, 25000 CE, whose trades are `prices` {"HH:MM": price}."""
    d = tmp_path / "options" / "NIFTY" / EXP.isoformat()
    d.mkdir(parents=True)
    rows = [f"{DAY} {t}:00,{px},," for t, px in prices.items()]
    (d / "25000CE.csv").write_text("time,last,bid,ask\n" + "\n".join(rows) + "\n")
    store = OptionStore("NIFTY", root=tmp_path / "options")
    idx = pd.date_range(f"{DAY} 09:16", f"{DAY} 15:30", freq="1min")
    df = pd.DataFrame({"datetime": idx, "open": 25000.0, "high": 25000.0, "low": 25000.0, "close": 25000.0, "vix": 12.0}, index=idx)
    cfg = load_config()
    cfg["execution"].update(ex)
    feed = HistoricalFeed(df, cfg, store, real_only=True)
    return feed, Contract("NIFTY", EXP, 25000, "CE")


def _at(feed, hm):
    feed.now = datetime.combine(DAY, datetime.strptime(hm, "%H:%M").time())
    feed._cache = {}


def test_decisions_never_see_a_later_trade(tmp_path):
    feed, c = _feed(tmp_path, {"09:30": 40.0, "11:05": 80.0})
    _at(feed, "11:00")
    assert feed.quote(c).last == 40.0                        # what was known at 11:00, not the 80 traded at 11:05
    assert feed.fill_quote(c).last == 80.0                   # an order then waits for the next trade (40 is 90 min old)
    assert feed.price_stats["next_trade"] == 1


def test_recent_trade_fills_at_once_and_a_delay_moves_the_fill(tmp_path):
    feed, c = _feed(tmp_path, {"10:58": 40.0, "11:00": 41.0, "11:01": 47.0})
    _at(feed, "11:00")
    assert feed.fill_quote(c).last == 41.0
    feed2, c2 = _feed(tmp_path / "b", {"10:58": 40.0, "11:00": 41.0, "11:01": 47.0}, fill_delay_seconds=60)
    _at(feed2, "11:00")
    assert feed2.quote(c2).last == 41.0 and feed2.fill_quote(c2).last == 47.0   # decided on 41, filled a minute later


def test_no_trade_nearby_fills_at_the_old_price_and_counts_it(tmp_path):
    feed, c = _feed(tmp_path, {"09:30": 40.0, "14:00": 20.0})
    _at(feed, "11:00")
    assert feed.fill_quote(c).last == 40.0 and feed.price_stats["old_fill"] == 1


def test_conservative_job_settings():
    from algolab.backtest_queue import job_config
    cfg = job_config({"fills": "conservative"}, load_config())
    assert cfg["execution"]["cost_stress"] == 2 and cfg["execution"]["fill_delay_seconds"] == 60
    assert job_config({}, load_config())["execution"]["cost_stress"] == 1.0


def test_strike_list_holds_only_strikes_traded_so_far(tmp_path):
    d = tmp_path / "options" / "NIFTY" / EXP.isoformat()
    d.mkdir(parents=True)
    times = pd.date_range(f"{DAY} 09:16", f"{DAY} 15:29", freq="1min")
    (d / "25000CE.csv").write_text("time,last,bid,ask\n" + "\n".join(f"{t},50,," for t in times) + "\n")
    later = times[times >= pd.Timestamp(f"{DAY} 13:00")]
    (d / "25500CE.csv").write_text("time,last,bid,ask\n" + "\n".join(f"{t},5,," for t in later) + "\n")
    store = OptionStore("NIFTY", root=tmp_path / "options")
    idx = pd.date_range(f"{DAY} 09:16", f"{DAY} 15:30", freq="1min")
    df = pd.DataFrame({"datetime": idx, "open": 25000.0, "high": 25000.0, "low": 25000.0, "close": 25000.0, "vix": 12.0}, index=idx)
    feed = HistoricalFeed(df, load_config(), store, real_only=True)
    _at(feed, "11:00")
    assert feed.listed_strikes(EXP, "CE") == {25000}                    # 25500 first trades at 13:00
    _at(feed, "13:30")
    assert feed.listed_strikes(EXP, "CE") == {25000, 25500}


def test_more_figures_and_cycle_moves():
    from algolab.engine import build_result, make_broker, run
    from algolab.feeds import make_sample
    from algolab.strategy import load_strategies
    cfg = load_config()
    cfg["pricing"]["smile"], cfg["pricing"]["real_only"] = "off", False
    df = make_sample("2026-01-01", "2026-03-31")
    df.index = df["datetime"] + timedelta(minutes=5)
    broker = make_broker(cfg)
    res = build_result(broker, run(HistoricalFeed(df, cfg), load_strategies()["intraday_straddle"](), broker), [])
    m = res.metrics
    for k in ("Sortino (daily)", "Calmar (annual return / max drawdown %)", "Best day", "Worst day",
              "Expectancy per cycle", "Days in a trade %", "Avg worst move in a cycle", "Avg best move in a cycle"):
        assert k in m, k
    assert m["Worst day"] <= 0 <= m["Best day"] and 0 < m["Days in a trade %"] <= 100
    assert m["Expectancy per cycle"] == round(res.cycles.net_pnl.mean(), 0)
    assert (res.cycles.worst_move <= 0).all() and (res.cycles.best_move >= 0).all()


def test_reference_figures_and_server_code_version(tmp_path, monkeypatch):
    import pandas as pd
    from algolab import runs
    from algolab.engine import add_reference
    eq = pd.DataFrame({"equity": [100.0, 110.0], "spot": [20000.0, 21000.0]})
    m = {"Net P&L": 500.0}
    add_reference(m, eq, 120.0)
    assert m["P&L before charges"] == 620 and m["Index move over the period %"] == 5.0
    monkeypatch.setattr(runs, "ROOT", tmp_path)              # a server copy: no git, a stamped commit
    (tmp_path / ".deployed_commit").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert runs.code_version() == "0123456"
    (tmp_path / ".deployed_commit").write_text("not a commit")
    assert runs.code_version() == "not in git"


def test_liquidity_filter_uses_truedata_volume(tmp_path):
    import gzip
    trades = {f"{9 + (m + 16) // 60:02d}:{(m + 16) % 60:02d}": 40.0 for m in range(60)}   # 09:16-10:15, steady
    feed, c = _feed(tmp_path, trades, liquidity={"min_volume_today": 5000, "max_share_of_volume": 0.1,
                                                                        "window_minutes": 5})
    raw = tmp_path / "truedata" / "options" / "NIFTY" / EXP.isoformat()
    raw.mkdir(parents=True)
    rows = [f"{DAY} 09:{m:02d}:00,40,41,39,40,{1000 if m < 40 else 100},0" for m in range(15, 60)]
    with gzip.open(raw / "25000CE.csv.gz", "wt") as f:
        f.write("timestamp,open,high,low,close,volume,oi\n" + "\n".join(rows) + "\n")
    feed.store.raw_root = tmp_path / "truedata" / "options" / "NIFTY"
    st = feed.store
    assert st.volume(c, datetime.combine(DAY, datetime.min.time()), datetime(2026, 10, 13, 9, 20)) == 5000  # 09:15-09:19 bars
    _at(feed, "09:18")
    assert feed.listed_strikes(EXP, "CE") is None            # 3,000 traded so far: below the 5,000 floor, not offered
    assert feed.price_stats["thin_strikes"] == 1
    _at(feed, "09:31")
    assert feed.listed_strikes(EXP, "CE") == {25000}
    feed.check_size(c, 65)                                     # 65 vs 10% of 5,000 traded in the last 5 minutes: fine
    assert "large_orders" not in feed.price_stats
    _at(feed, "09:58")
    feed.check_size(c, 65)                                     # 65 vs 10% of 500: counted
    assert feed.price_stats["large_orders"] == 1
    from algolab.backtest_queue import job_config
    assert job_config({"min_volume": "20000"})["execution"]["liquidity"]["min_volume_today"] == 20000


def test_options_follow_their_high_and_low_inside_a_bar(tmp_path):
    import gzip
    d = tmp_path / "options" / "NIFTY" / EXP.isoformat()
    d.mkdir(parents=True)
    raw = tmp_path / "truedata" / "options" / "NIFTY" / EXP.isoformat()
    raw.mkdir(parents=True)
    for right, (o, h, lo, c) in {"CE": (40, 46, 30, 35), "PE": (50, 62, 45, 58)}.items():
        (d / f"25000{right}.csv").write_text("time,last,bid,ask\n" + "".join(
            f"{DAY} 09:{m:02d}:00,{c},,\n" for m in range(16, 30)))
        with gzip.open(raw / f"25000{right}.csv.gz", "wt") as f:
            f.write("timestamp,open,high,low,close,volume,oi\n" + "".join(
                f"{DAY} 09:{m:02d}:00,{o},{h},{lo},{c},1000,0\n" for m in range(15, 29)))
    store = OptionStore("NIFTY", root=tmp_path / "options")
    store.raw_root = tmp_path / "truedata" / "options" / "NIFTY"
    idx = pd.date_range(f"{DAY} 09:16", f"{DAY} 09:28", freq="1min")
    df = pd.DataFrame({"open": 25000.0, "high": 25040.0, "low": 24950.0, "close": 25010.0, "vix": 12.0}, index=idx)
    feed = HistoricalFeed(df, load_config(), store, real_only=True)
    ce, pe = Contract("NIFTY", EXP, 25000, "CE"), Contract("NIFTY", EXP, 25000, "PE")
    seen = {}
    for when in feed:
        if when.hour == 9 and 20 <= when.minute < 21 or (when.minute == 21 and when.second == 0):
            seen[feed.bar_point] = (feed.quote(ce).last, feed.quote(pe).last)
    assert seen["low"] == (30, 62)          # NIFTY at its low: the call at its low, the put at its high
    assert seen["high"] == (46, 45)
    assert seen["close"] == (35, 58)        # the close: the recorded trade, as before
    feed.bar_point, feed.bar_closed, feed._cache = "low", False, {}
    assert feed.fill_quote(ce).last == 30 and feed.price_stats["inside_bar"] == 1


def test_margin_estimate_and_return_on_margin():
    from algolab.margin_est import estimate
    e = date(2026, 10, 20)
    ce = lambda k: Contract("NIFTY", e, k, "CE")  # noqa: E731
    pe = lambda k: Contract("NIFTY", e, k, "PE")  # noqa: E731
    naked = estimate([(ce(25000), -65, 100.0), (pe(25000), -65, 100.0)], 25000)
    assert naked == 2 * 65 * 25000 * 0.08                    # a short straddle: 8% of NIFTY per sold unit
    fly = estimate([(ce(25000), -65, 100.0), (pe(25000), -65, 100.0), (ce(25200), 65, 30.0), (pe(24800), 65, 30.0)], 25000)
    assert fly == 65 * (200 - 140) + 2 * 65 * 25000 * 0.02    # an iron fly: worst loss (wing 200 - credit 140) + exposure
    assert estimate([(ce(25500), 130, 10.0)], 25000) == 1300  # bought: the premium
    assert estimate([], 25000) == 0


def test_backtest_reports_margin_figures():
    from algolab.engine import backtest
    from algolab.strategy import load_strategies
    cfg = load_config()
    cfg.setdefault("pricing", {})["real_only"] = False
    strat = load_strategies()["short_straddle"]()
    res = backtest(cfg, strat, "SAMPLE_NIFTY_5minute", date(2025, 1, 1), date(2025, 2, 28))
    assert res.metrics["Peak margin (estimated)"] > 0
    assert "Return on peak margin %" in res.metrics


def test_monte_carlo_cost_stress_and_walk_forward():
    from algolab.engine import add_robustness
    from algolab.sweep import folds, luck_note, walk_forward
    cyc = pd.DataFrame({"net_pnl": [1000.0, -500.0] * 10})
    m = {"Net P&L": 5000.0}
    add_robustness(m, cyc, 2000.0)
    assert m["Net with charges 50% higher"] == 4000
    assert m["Monte Carlo: max drawdown, 1 in 20 worse than"] < 0
    assert 0 <= m["Monte Carlo: chance of a losing period %"] < 50
    f = folds(date(2026, 1, 1), date(2026, 3, 31), 3)
    assert list(f) == ["f1", "f2", "f3"] and f["f1"][0] == date(2026, 1, 1) and f["f3"][1] == date(2026, 3, 31)
    assert f["f2"][0] == f["f1"][1] + timedelta(days=1)
    df = pd.DataFrame({"x": ["1", "2"], "f1_ret_dd": [2.0, 1.0], "f2_ret_dd": [0.5, 3.0], "f1_net": [10, 5],
                       "f2_net": [-100.0, 300.0], "f3_net": [50.0, 70.0]})
    w = walk_forward(df, {"x": ["1", "2"]}, f)
    assert [s["params"]["x"] for s in w["steps"]] == ["1", "2"]          # best on f1 is x=1, best on f2 is x=2
    assert [s["test_net"] for s in w["steps"]] == [-100.0, 70.0] and w["net"] == -30
    assert "12 combinations" in luck_note(df, 12, ["f1", "f2", "f3"]) and luck_note(df, 4, ["all"]) == ""
