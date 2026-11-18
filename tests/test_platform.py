import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab import feeds, kite, paper  # noqa: E402
from algolab.broker import CostModel, PaperBroker  # noqa: E402
from algolab.config import load_config  # noqa: E402
from algolab.engine import build_result, make_broker, run  # noqa: E402
from algolab.feeds import HistoricalFeed, OptionStore, make_sample, weekly_expiries  # noqa: E402
from algolab.models import Contract, Quote  # noqa: E402
from algolab.pricing import bs_price, implied_vol, year_fraction  # noqa: E402
from algolab.strategy import load_strategies  # noqa: E402

CFG = load_config()
CFG["pricing"]["smile"] = "off"  # tests must not depend on NSE data imported on this computer
CFG["pricing"]["real_only"] = False  # synthetic test data: options are priced by the model
NOW = datetime(2026, 10, 7, 9, 30)
C = Contract("NIFTY", date(2026, 10, 13), 24000, "CE")


def sample_df(start="2026-06-01", end="2026-09-30"):
    df = make_sample(start, end)
    df.index = df["datetime"] + timedelta(minutes=5)
    return df


def test_pricing_parity_and_iv():
    t = year_fraction(NOW, C.expiry)
    call = bs_price(24000, 24000, t, 0.13, "CE", 0.065, 0.012)
    put = bs_price(24000, 24000, t, 0.13, "PE", 0.065, 0.012)
    import math
    assert call - put == pytest.approx(24000 * math.exp(-0.012 * t) - 24000 * math.exp(-0.065 * t), abs=1e-6)
    assert implied_vol(call, 24000, 24000, t, "CE", 0.065, 0.012) == pytest.approx(0.13, abs=1e-4)
    assert bs_price(24100, 24000, 0, 0.13, "CE") == 100
    assert year_fraction(datetime(2026, 10, 13, 15, 30), C.expiry) == 0
    # Wed 09:15 -> next Tue close = 5 sessions, less the overnight share of the first one
    assert year_fraction(datetime(2026, 10, 7, 9, 15), C.expiry) == pytest.approx((0.7 + 4) / 252)


def test_costs_match_hand_calculation():
    cm = CostModel()
    value = 100 * 65
    sell = (20 + value * (0.03553 + 0.0001) / 100) * 1.18 + value * 0.0015
    buy = (20 + value * (0.03553 + 0.0001) / 100) * 1.18 + value * 0.00003
    assert cm.order_cost("SELL", value) == pytest.approx(sell)
    assert cm.order_cost("BUY", value) == pytest.approx(buy)


def test_broker_round_trip_and_settlement():
    b = PaperBroker(300000, CostModel(), slippage_pct=1.0)
    b.execute(NOW, C, "SELL", 65, Quote(100.0))
    assert b.positions[C].qty == -65 and b.positions[C].avg_price == 99.0
    b.execute(NOW, C, "BUY", 65, Quote(50.0, bid=49.0, ask=50.5, source="market"))
    assert not b.positions
    t = b.trades[0]
    assert t.gross_pnl == pytest.approx((99.0 - 50.5) * 65)
    assert t.net_pnl == pytest.approx(t.gross_pnl - b.total_costs, abs=0.02)
    assert b.equity(lambda c: Quote(0)) == pytest.approx(300000 + b.net_realized)
    b.execute(NOW, C, "SELL", 65, Quote(100.0))
    b.settle(NOW, C, 24120.0)
    assert b.trades[-1].exit_price == 120.0 and b.trades[-1].reason == "Expiry settlement"
    restored = PaperBroker(1, CostModel())
    restored.load_dict(json.loads(json.dumps(b.to_dict())))
    assert restored.net_realized == pytest.approx(b.net_realized) and len(restored.trades) == 2


def test_expiry_calendar_switches_to_tuesday_and_skips_holidays():
    rules = CFG["market"]["expiry_rules"]
    ex = weekly_expiries(date(2025, 8, 18), date(2025, 9, 16), rules)
    assert ex == [date(2025, 8, 21), date(2025, 8, 28), date(2025, 9, 2), date(2025, 9, 9), date(2025, 9, 16)]
    days = {date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 14)}  # Tue 13th missing = holiday
    assert date(2026, 10, 12) in weekly_expiries(date(2026, 10, 5), date(2026, 10, 14), rules, days)


@pytest.mark.parametrize("name", ["short_straddle", "intraday_straddle"])
def test_backtest_accounts_reconcile(name):
    broker = make_broker(CFG)
    logs = []
    strat = load_strategies()[name]()
    res = build_result(broker, run(HistoricalFeed(sample_df(), CFG), strat, broker, logs), logs)
    assert len(res.cycles) > 5
    open_pnl = broker.unrealized(lambda c: Quote(0)) if not broker.positions else None
    booked = res.trades.net_pnl.sum()
    if not broker.positions:
        assert res.equity.equity.iloc[-1] - broker.capital == pytest.approx(booked, abs=1.0)
    assert res.metrics["Charges paid"] > 0
    assert all(q % CFG["market"]["lot_size"] == 0 for q in res.fills.qty)


def test_positional_straddle_holds_overnight_and_stops_out():
    broker = make_broker(CFG)
    strat = load_strategies()["short_straddle"](stop_loss_pct=25, adjust_points=0)
    res = build_result(broker, run(HistoricalFeed(sample_df(), CFG), strat, broker), [])
    held = pd.to_datetime(res.cycles.exit_time).dt.date != pd.to_datetime(res.cycles.entry_time).dt.date
    assert held.any()
    stops = res.cycles[res.cycles.exit_reason == "Stop-loss"]
    assert len(stops) and (stops.net_pnl < 0).all()
    assert res.trades.groupby("group").contract.count().eq(2).all()  # no rolls when adjustments are off


def test_recorded_option_prices_override_model(tmp_path):
    store = OptionStore("NIFTY", root=tmp_path)
    df = sample_df("2026-09-01", "2026-09-10")
    feed = HistoricalFeed(df, {**CFG, "execution": {**CFG["execution"], "intrabar": "close"}}, store)
    it = iter(feed)
    now = next(it)
    c = Contract("NIFTY", feed.expiries()[0], 24000, "CE")
    assert feed.quote(c).source == "model"
    store.append(c, now + timedelta(minutes=5), 123.45)
    store._cache.clear()
    next(it)
    q = feed.quote(c)
    assert q.source == "market" and q.last == 123.45


class FakeKite:
    def __init__(self, expiry):
        self.expiry = expiry

    def instruments(self, exchange=None):
        if exchange == "NSE":
            return [{"tradingsymbol": "NIFTY 50", "segment": "INDICES", "instrument_token": 256265}]
        return [{"name": "NIFTY", "instrument_type": r, "expiry": self.expiry, "strike": float(k),
                 "tradingsymbol": f"NIFTYX{k}{r}", "instrument_token": k * 10 + i, "lot_size": 65}
                for k in range(23500, 24550, 50) for i, r in enumerate(("CE", "PE"))]

    def quote(self, symbols):
        out = {}
        for s in symbols:
            if s.startswith("NSE:"):
                out[s] = {"last_price": 24010.0 if "NIFTY" in s else 13.2, "timestamp": kite.ist_now()}
            else:
                out[s] = {"last_price": 101.0, "depth": {"buy": [{"price": 100.5}], "sell": [{"price": 101.5}]}}
        return out

    def historical_data(self, token, start, end, interval):
        return [{"date": pd.Timestamp("2026-10-07 09:15", tz="Asia/Kolkata").to_pydatetime(),
                 "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 0}]


def test_kite_feed_uses_bid_ask_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(feeds, "OPTION_DIR", tmp_path / "options")
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    monkeypatch.setattr(kite, "ist_now", lambda: datetime(2026, 10, 7, 10, 0))
    expiry = date(2026, 10, 13)
    feed = kite.KiteFeed(FakeKite(expiry), CFG, control=type("C", (), {"sleep": lambda self, s: False})())
    assert next(iter(feed)) == datetime(2026, 10, 7, 10, 0)
    assert feed.spot == 24010.0 and feed.expiries() == [expiry] and feed.lot_size == 65
    c = Contract("NIFTY", expiry, 24000, "CE")
    q = feed.quote(c)
    assert (q.bid, q.ask, q.source) == (100.5, 101.5, "market")
    b = make_broker(CFG)
    assert b.execute(feed.now, c, "SELL", 65, q).price == 100.5
    assert feed.store.path(c).exists()
    df = kite.fetch_candles(FakeKite(expiry), 1, date(2026, 10, 1), date(2026, 10, 7), "5minute")
    assert df.datetime.iloc[0] == pd.Timestamp("2026-10-07 09:15")


def _paper_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "RUNTIME", tmp_path)
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(paper, "SERVICE_BEAT", tmp_path / "service_heartbeat")
    monkeypatch.setattr(paper, "CATALOG", tmp_path / "strategies.json")
    cfg = load_config()
    cfg["pricing"]["smile"] = "off"
    cfg["paper"]["sim_seconds_per_bar"] = 0.001
    return cfg


def test_paper_session_saves_and_resumes(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    paper.run_paper(cfg, "short_straddle", {}, "sim", fresh=True, max_bars=30)
    s1 = paper.read_state("short_straddle")
    assert s1["status"] == "stopped" and len(s1["positions"]) == 2 and not paper.is_running()
    paper.run_paper(cfg, "short_straddle", {}, "sim", max_bars=30)
    s2 = paper.read_state("short_straddle")
    assert s2["time"] > s1["time"] and len(s2["broker"]["fills"]) >= 2
    assert s2["broker"]["fills"][0] == s1["broker"]["fills"][0]
    assert len(pd.read_csv(paper.equity_file("short_straddle"))) == 60


def test_old_single_session_files_are_moved(tmp_path, monkeypatch):
    _paper_tmp(tmp_path, monkeypatch)
    (tmp_path / "paper_state.json").write_text(json.dumps({"strategy": "short_straddle", "feed": "kite", "x": 1}))
    (tmp_path / "paper_desired.json").write_text(json.dumps({"strategy": "short_straddle", "params": {}, "feed": "kite"}))
    (tmp_path / "paper_equity.csv").write_text("time,equity,spot\n")
    paper.migrate_legacy()
    assert paper.sessions() == ["short_straddle"] and paper.read_state("short_straddle")["x"] == 1
    assert paper.read_desired("short_straddle")["feed"] == "kite" and not (tmp_path / "paper_state.json").exists()


def test_service_runs_sessions_side_by_side(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    # the simulated market starts today with a seed from the clock: pin both, so the strangle always finds its
    # strikes (some days and seeds it had none within its 20 bars, and the test failed now and then)
    from algolab import feeds
    real_sample = feeds.make_sample
    monkeypatch.setattr(feeds, "make_sample", lambda start, end, interval=5, seed=7, s0=24000.0: real_sample(start, end, interval, 7, s0))
    monkeypatch.setattr(paper, "ist_now", lambda: datetime(2026, 10, 7, 9, 0))
    real, ran = paper.run_group, set()

    def once(c, w, kind, **k):
        """Each session runs its 20 bars once. (The service restarts a session that has finished, which in
        this test would add bars at a pace set by the machine's speed, and a strangle may then have exited.)"""
        key = tuple(sorted(w()))
        if key in ran:
            return
        ran.add(key)
        real(cfg, w, kind, max_bars=20)
    monkeypatch.setattr(paper, "run_group", once)
    paper.set_desired("short_straddle", {"lots": 2}, "sim")
    paper.set_desired("premium_strangle", {"lots": 1}, "sim")
    paper.run_service(idle=0.05, retry=0.05, max_loops=6)
    a, b = paper.read_state("short_straddle"), paper.read_state("premium_strangle")
    assert a["params"]["lots"] == 2 and a["positions"][0]["Qty"] == -130
    assert b["strategy"] == "premium_strangle" and len(b["positions"]) == 2 and b["broker"]["capital"] == a["broker"]["capital"]
    assert paper.service_alive() and set(paper.sessions()) == {"short_straddle", "premium_strangle"}
    tick = json.loads((paper.SESSIONS / "short_straddle" / "tick.json").read_text())      # the page's 1-second refresh
    assert tick["time"] == a["time"] and tick["equity"] == a["equity"] and tick["positions"] == a["positions"]
    # stopping one leaves the other's account alone
    paper.request_stop("premium_strangle")
    assert paper.read_desired("premium_strangle") is None and paper.read_desired("short_straddle")
    # the same strategy in a second session with other settings, its own account
    sid = paper.set_desired("short_straddle", {"lots": 1}, "sim", sid=paper.new_id("short_straddle"), label="one lot")
    assert sid == "short_straddle-2"
    paper.run_service(idle=0.05, retry=0.05, max_loops=6)
    c2 = paper.read_state(sid)
    assert c2["id"] == sid and c2["label"] == "one lot" and c2["positions"][0]["Qty"] == -65
    assert paper.read_state("short_straddle")["params"]["lots"] == 2
    # a live session that cannot start is reported and retried, not dropped
    paper.set_desired("premium_strangle", {}, "kite")
    monkeypatch.setattr(paper, "run_group", lambda *a, **k: (_ for _ in ()).throw(kite.KiteError("Not logged in to Zerodha today")))
    paper.run_service(idle=0.05, retry=0.05, max_loops=3)
    assert "Not logged in" in paper.session_status("premium_strangle")
    assert paper.read_desired("premium_strangle")["feed"] == "kite"


def test_live_sessions_share_one_quote_request_per_update(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    calls = []

    class CountingKite(FakeKite):
        def quote(self, symbols):
            calls.append(len(symbols))
            clock[0] += timedelta(seconds=30)
            return super().quote(symbols)

    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(CountingKite(date(2026, 10, 13)), c, g, log=g.log))
    want = {"short_straddle": {"strategy": "short_straddle", "params": {"entry_time": "09:30", "min_dte": 0}, "feed": "kite"},
            "premium_strangle": {"strategy": "premium_strangle", "params": {"min_dte": 0}, "feed": "kite"}}
    paper.run_group(cfg, lambda: want, "kite", max_bars=10)
    a, b = paper.read_state("short_straddle"), paper.read_state("premium_strangle")
    assert len(a["positions"]) == 2 and len(b["positions"]) == 2
    # after entry, each update is ONE request with the index, VIX and both sessions' four options
    steady = calls[-3:]
    assert all(23500 <= int(x["Contract"].split()[4]) <= 24500 for x in b["positions"])  # listed strikes only
    assert steady and all(n == 2 + 4 for n in steady), calls


def test_kite_feed_adopts_new_login(tmp_path, monkeypatch):
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    monkeypatch.setattr(kite, "ist_now", lambda: datetime(2026, 10, 7, 10, 0))
    token = {"access_token": "old"}
    monkeypatch.setattr(kite, "session_info", lambda: dict(token))
    fake = FakeKite(date(2026, 10, 13))
    fake.set_access_token = lambda t: setattr(fake, "token", t)
    feed = kite.KiteFeed(fake, CFG, control=type("C", (), {"sleep": lambda self, s: True})(), log=lambda m: None)
    it = iter(feed)
    next(it)
    assert not hasattr(fake, "token")
    token["access_token"] = "new"
    next(it)
    assert fake.token == "new"


@pytest.mark.parametrize("content", ['{"access_token": "abc123def456ghi789", "user": "x"}',
                                     '{"session": {"kite_access_token": "abc123def456ghi789"}}',
                                     'KITE_ACCESS_TOKEN="abc123def456ghi789"\nOTHER=1', "abc123def456ghi789\n"])
def test_reuses_another_apps_login(tmp_path, monkeypatch, content):
    f = tmp_path / "token"
    f.write_text(content)
    assert kite.read_shared_token(f) == "abc123def456ghi789"
    monkeypatch.setattr(kite, "shared_token_file", lambda: f)
    assert kite.session_info()["access_token"] == "abc123def456ghi789"
    with pytest.raises(kite.KiteError):
        kite.complete_login("anything")
    import os
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime - 3 * 86400))   # a login from days ago is not valid
    assert kite.session_info() is None


# ---- engine realism -----------------------------------------------------
def test_lot_size_and_charges_follow_history():
    feed = HistoricalFeed(sample_df("2026-09-01", "2026-09-05"), CFG)
    assert feed.lot_size_for(date(2024, 3, 28)) == 50
    assert feed.lot_size_for(date(2024, 8, 1)) == 25
    assert feed.lot_size_for(date(2025, 6, 5)) == 75
    assert feed.lot_size_for(date(2026, 1, 6)) == 65
    b = make_broker(CFG)
    assert b.schedule.at(date(2024, 6, 1)).stt_sell_pct == 0.0625
    assert b.schedule.at(date(2025, 6, 1)).stt_sell_pct == 0.1
    assert b.schedule.at(date(2026, 6, 1)).stt_sell_pct == CFG["costs"]["stt_sell_pct"]


def test_intrabar_walk_visits_open_high_low_close():
    df = pd.DataFrame({"open": [100.0], "high": [110.0], "low": [95.0], "close": [105.0], "vix": [13.0]},
                      index=[pd.Timestamp("2026-09-01 09:20")])
    df = pd.concat([df, df.assign(open=105.0, high=106.0, low=104.0, close=105.5)
                    .set_axis([pd.Timestamp("2026-09-01 09:25")])])
    feed = HistoricalFeed(df, CFG)
    seen = [(t.strftime("%H:%M:%S"), feed.spot, feed.bar_closed) for t in feed][:4]
    assert seen == [("09:16:15", 100.0, False), ("09:17:30", 95.0, False),
                    ("09:18:45", 110.0, False), ("09:20:00", 105.0, True)]


def test_intrabar_stop_catches_moves_the_close_hides():
    """A stop is hit inside bars far more often than at bar closes - the OHLC walk must find more."""
    def stops(mode):
        cfg = {**CFG, "execution": {**CFG["execution"], "intrabar": mode}}
        broker = make_broker(cfg)
        strat = load_strategies()["short_straddle"](stop_loss_pct=20, adjust_points=0)
        res = build_result(broker, run(HistoricalFeed(sample_df(), cfg), strat, broker), [])
        assert len(res.equity) == len(sample_df())  # one equity point per bar either way
        return (res.cycles.exit_reason == "Stop-loss").sum()
    assert stops("ohlc") >= stops("close")


# ---- Blaze Butterfly ----------------------------------------------------
def blaze_run(start="2026-01-01", end="2026-09-25", **params):
    df = make_sample(start, end)
    df.index = df["datetime"] + timedelta(minutes=5)
    broker = make_broker(CFG)
    logs = []
    strat = load_strategies()["blaze_butterfly"](**params)
    res = build_result(broker, run(HistoricalFeed(df, CFG), strat, broker, logs), logs)
    return res, broker, logs


def test_blaze_reference_strike_rounding():
    from algolab.strategies.blaze_butterfly import BlazeButterfly

    class Ctx:
        def __init__(self, spot):
            self.spot, self.now = spot, datetime(2026, 10, 12, 15, 10)

        def expiries(self):
            return [date(2026, 10, 13), date(2026, 10, 20)]

        def lot_size_for(self, e):
            return 65

        def option(self, e, k, r):
            return k

        def fill_price(self, k, side):  # credit falls as strikes move down
            return {"BUY": 1.0, "SELL": 1.0}[side] * max(k - 23000, 1) / 10

    s = BlazeButterfly(credit_cap_pct=100.0)  # first candidate always accepted
    for spot, r in [(25124.9, 25100), (25125.1, 25100), (25174, 25100), (25176, 25200), (25226, 25200)]:
        assert s._select(Ctx(spot))["R"] == r, spot
    sel = s._select(Ctx(25124.9))
    assert sel["k"] == (24900, 24700, 24300) and sel["d"] == 0 and sel["expiry"] == date(2026, 10, 20)


def test_blaze_follows_entry_and_exit_rules():
    res, broker, logs = blaze_run(credit_cap_pct=5.0, debit_cap_pct=5.0)  # wide band so it trades
    assert len(res.cycles) >= 10 and not broker.positions
    entries = res.fills[res.fills.reason == "Entry"]
    times = pd.to_datetime(entries.time)
    assert (times.dt.weekday == 0).all()  # Monday entries after the 2025 cutover
    assert times.dt.strftime("%H:%M").between("15:10", "15:11").all()
    for _, legs in entries.groupby("group"):
        q = legs.set_index("side").qty.groupby(level=0).sum()
        assert q["BUY"] == 2 * 65 and q["SELL"] == 2 * 65  # 1 + 1 lots bought, 2 lots sold
    for _, c in res.cycles.iterrows():
        entry, exit_ = pd.Timestamp(c.entry_time), pd.Timestamp(c.exit_time)
        assert exit_ > entry and (exit_ - entry).days <= 4  # out by the end of the entry week
        assert exit_.weekday() < 5
    exits = res.fills[res.fills.reason != "Entry"]
    for _, legs in exits.groupby(["group", "time"]):
        assert list(legs.side)[0] == "BUY" or (legs.side == "SELL").all()  # shorts bought back first
    assert set(res.cycles.exit_reason) <= {"Stop-loss", "Target", "Crossing: NIFTY at or below K1",
                                           "Time exit (last trading day of the week)"}


def test_blaze_old_era_enters_on_wednesday():
    res, _, _ = blaze_run("2025-03-01", "2025-08-20", credit_cap_pct=5.0, debit_cap_pct=5.0)
    times = pd.to_datetime(res.fills[res.fills.reason == "Entry"].time)
    assert len(times) and (times.dt.weekday == 2).all()
    assert (res.fills[res.fills.reason == "Entry"].qty % 75 == 0).all()  # 2025 lot size


# ---- research tools -----------------------------------------------------
def test_sweep_grid_parsing():
    from algolab.sweep import parse_grid
    assert parse_grid(["stop_loss_pct=20,30", "target_pct=40:80:20"]) == {
        "stop_loss_pct": ["20", "30"], "target_pct": ["40", "60", "80"]}


def test_saved_run_is_reproducible(tmp_path):
    from algolab.runs import list_runs, save_run
    broker = make_broker(CFG)
    strat = load_strategies()["short_straddle"](stop_loss_pct=30)
    res = build_result(broker, run(HistoricalFeed(sample_df("2026-06-01", "2026-07-31"), CFG), strat, broker), [])
    out = save_run(res, strat, "SAMPLE_NIFTY_5minute", "2026-06-01", "2026-07-31", CFG, "unit test", root=tmp_path)
    meta = json.loads((out / "run.json").read_text())
    assert meta["params"]["stop_loss_pct"] == 30 and meta["config"]["costs"] == CFG["costs"]
    assert meta["code"] and "Worst week" in meta["metrics"]
    rebuilt = load_strategies()[meta["strategy"]](**meta["params"])
    assert rebuilt.params == strat.params
    assert list_runs(tmp_path).iloc[0].note == "unit test"


def test_publish_packs_a_small_daily_run(tmp_path):
    import io
    import zipfile
    from algolab.publish import pack
    from algolab.runs import save_run
    broker = make_broker(CFG)
    strat = load_strategies()["short_straddle"]()
    res = build_result(broker, run(HistoricalFeed(sample_df("2026-06-01", "2026-06-30"), CFG), strat, broker), [])
    out = save_run(res, strat, "SAMPLE_NIFTY_5minute", None, None, CFG, root=tmp_path)
    z = zipfile.ZipFile(io.BytesIO(pack(out)))
    assert set(z.namelist()) == {"run.json", "equity_daily.csv", "cycles.csv", "trades.csv", "fills.csv", "log.txt"}
    daily = pd.read_csv(z.open("equity_daily.csv"))
    assert len(daily) == len(set(res.equity.index.date)) and (daily.low <= daily.equity).all()
    json.loads(z.read("run.json"))  # valid JSON for the server


def test_replay_matches_the_backtest_bar_by_bar(tmp_path, monkeypatch):
    from algolab import feeds as fmod
    from algolab.replay import record
    df = make_sample("2026-08-01", "2026-09-30")
    monkeypatch.setattr(fmod, "SPOT_DIR", tmp_path)
    df.to_csv(tmp_path / "T.csv", index=False)
    strat = load_strategies()["short_straddle"]()
    rp = record(CFG, strat, "T", "2026-09-01", "2026-09-30", warmup_days=10)
    shown = fmod.load_dataset("T", "2026-09-01", "2026-09-30")
    assert len(rp) == len(shown) and len(rp.bars) == len(shown)
    full = fmod.load_dataset("T", "2026-08-22", "2026-09-30")
    broker = make_broker(CFG)
    curve = run(HistoricalFeed(full, CFG, fmod.OptionStore("NIFTY")), load_strategies()["short_straddle"](), broker)
    assert rp.frames[-1]["equity"] == pytest.approx(curve[-1][1])  # replay ends exactly where the backtest does
    j = rp.next_event(0)
    assert j is not None and rp.frames[j]["fills"] > 0 and rp.prev_event(j + 1) == j
    with_pos = [f for f in rp.frames if f["positions"]]
    assert with_pos and with_pos[0]["status"]["target"] > 0 > with_pos[0]["status"]["stop"]
    assert set(rp.fills.frame) == {k for k, f in enumerate(rp.frames) if f["fills"]}


def test_shared_login_uses_created_stamp_and_6am_flush(tmp_path, monkeypatch):
    f = tmp_path / "access_token.json"
    monkeypatch.setattr(kite, "shared_token_file", lambda: f)
    monkeypatch.setattr(kite, "ist_now", lambda: datetime(2026, 10, 12, 10, 0))
    f.write_text(json.dumps({"access_token": "tok12345678901234567", "created": "2026-10-12T08:05:00+05:30"}))
    assert kite.session_info()["access_token"] == "tok12345678901234567"
    f.write_text(json.dumps({"access_token": "tok12345678901234567", "created": "2026-10-12T05:59:00+05:30"}))
    assert kite.session_info() is None                      # issued before the 6 am flush
    monkeypatch.setattr(kite, "ist_now", lambda: datetime(2026, 10, 13, 5, 0))
    f.write_text(json.dumps({"access_token": "tok12345678901234567", "created": "2026-10-12T08:05:00+05:30"}))
    assert kite.session_info() is not None                  # still valid until 6 am the next day
    with pytest.raises(kite.KiteError, match="morning login"):
        monkeypatch.setattr(kite, "ist_now", lambda: datetime(2026, 10, 13, 7, 0))
        kite.client()


def test_portfolio_page_api(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    (tmp_path / "runtime").mkdir()
    spec = importlib.util.spec_from_file_location(
        "algolab_panel", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(paper, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(paper, "CATALOG", tmp_path / "runtime" / "strategies.json")
    paper.write_catalog()
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    assert c.get("/paper").status_code == 200
    st = c.get("/api/paper").get_json()
    assert st["installed"] and st["sessions"] == [] and st["catalog"][0]["name"] == "short_straddle"
    assert c.post("/api/paper/start", json={"strategy": "nope", "feed": "sim"}).status_code == 400
    assert c.post("/api/paper/start", json={"strategy": "short_straddle", "feed": "sim", "params": {"lots": "x"}}).status_code == 400
    assert c.post("/api/paper/start", data="strategy=short_straddle").status_code == 400      # forms are refused
    assert c.post("/api/paper/start", json={"strategy": "short_straddle", "feed": "kite",
                                            "params": {"lots": "2", "stop_loss_pct": "30"}}).status_code == 200
    assert c.post("/api/paper/start", json={"strategy": "premium_strangle", "feed": "sim", "params": {}}).status_code == 200
    sess = tmp_path / "runtime" / "sessions"
    want = json.loads((sess / "short_straddle" / "desired.json").read_text())
    assert want == {"strategy": "short_straddle", "params": {"lots": 2, "stop_loss_pct": 30.0}, "feed": "kite"}
    # the engine reads exactly what the page wrote
    monkeypatch.setattr(paper, "SESSIONS", sess)
    assert set(paper.sessions()) == {"short_straddle", "premium_strangle"}
    assert paper.read_desired("premium_strangle")["feed"] == "sim"
    st = c.get("/api/paper").get_json()
    assert {x["id"] for x in st["sessions"]} == {"short_straddle", "premium_strangle"}
    assert all(x["desired"] and not x["running"] for x in st["sessions"])
    assert c.post("/api/paper/reset", json={"id": "short_straddle"}).status_code == 400       # not while wanted
    assert c.post("/api/paper/stop", json={"id": "nope"}).status_code == 404
    assert c.post("/api/paper/stop", json={"id": "short_straddle"}).status_code == 200
    assert not (sess / "short_straddle" / "desired.json").exists() and (sess / "short_straddle" / "STOP").exists()
    assert (sess / "premium_strangle" / "desired.json").exists()                               # the other keeps running
    # live trading needs the live feed, a daily loss limit and a typed confirmation
    live = {"strategy": "premium_strangle", "feed": "kite", "params": {}, "mode": "live",
            "live": {"max_lots": "1", "daily_loss_limit": "4000"}}
    assert c.post("/api/paper/start", json={**live, "feed": "sim"}).status_code == 400
    assert c.post("/api/paper/start", json=live).status_code == 400                             # no confirmation
    assert c.post("/api/paper/start", json={**live, "confirm": "live", "live": {"daily_loss_limit": 0}}).status_code == 400
    assert c.post("/api/paper/start", json={**live, "confirm": "LIVE", "strategy": "short_straddle"}).status_code == 400  # not live-ready
    r = c.post("/api/paper/start", json={**live, "confirm": "LIVE"})
    assert r.status_code == 200 and r.get_json()["id"] == "premium_strangle-2"                  # its own session
    want = json.loads((sess / "premium_strangle-2" / "desired.json").read_text())
    assert want["mode"] == "live" and want["live"]["daily_loss_limit"] == 4000.0 and want["live"]["max_lots"] == 1
    assert want["live"]["allow_over_max_lots"] is False                                         # off unless chosen
    assert c.post("/api/live/settings", json={"max_sessions": 1}).status_code == 200
    r = c.post("/api/paper/start", json={**live, "confirm": "LIVE"})
    assert r.status_code == 400 and "the limit is 1" in r.get_json()["error"]                   # LIVE session limit
    r = c.post("/api/paper/start", json={**live, "mode": "shadow"})
    assert r.status_code == 400 and "Shadow mode was removed" in r.get_json()["error"]           # no more Shadow
    assert c.post("/api/live/settings", json={"max_sessions": 3}).status_code == 200
    r = c.post("/api/paper/start", json={**live, "confirm": "LIVE", "live": {**live["live"], "allow_over_max_lots": True}})
    assert json.loads((sess / r.get_json()["id"] / "desired.json").read_text())["live"]["allow_over_max_lots"] is True
    # the two pages show their own sessions
    paper_ids = {x["id"] for x in c.get("/api/paper?kind=paper").get_json()["sessions"]}
    live_ids = {x["id"] for x in c.get("/api/paper?kind=live").get_json()["sessions"]}
    assert paper_ids == {"premium_strangle"} and live_ids == {"premium_strangle-2", "premium_strangle-3"}
    assert c.get("/live").status_code == 200 and b"__KIND__" not in c.get("/live").data
    # a session keeps its mode and strategy when resumed
    assert c.post("/api/paper/start", json={**live, "mode": "paper", "id": "premium_strangle-2"}).status_code == 400
    assert c.post("/api/paper/start", json={"strategy": "short_straddle", "feed": "sim", "id": "premium_strangle"}).status_code == 400
    assert c.post("/api/paper/kill", json={"id": "premium_strangle-2"}).status_code == 200
    assert (sess / "premium_strangle-2" / "KILL").exists()
    assert c.post("/api/paper/kill", json={"id": "short_straddle"}).status_code == 404          # stopped, no account
    # more paper sessions of one strategy, with other settings and a name
    r = c.post("/api/paper/start", json={"strategy": "premium_strangle", "feed": "sim", "params": {"target_premium": "15"}, "label": " target  15 "})
    assert r.get_json()["id"] == "premium_strangle-4"
    want = json.loads((sess / "premium_strangle-4" / "desired.json").read_text())
    assert want["label"] == "target 15" and want["params"]["target_premium"] == 15.0
    # resuming keeps the name; deleting needs the session stopped
    assert c.post("/api/paper/start", json={"strategy": "premium_strangle", "feed": "sim", "id": "premium_strangle-4",
                                            "params": {"target_premium": "12"}}).get_json()["id"] == "premium_strangle-4"
    assert json.loads((sess / "premium_strangle-4" / "desired.json").read_text())["label"] == "target 15"
    assert c.post("/api/paper/delete", json={"id": "premium_strangle-4"}).status_code == 400
    c.post("/api/paper/stop", json={"id": "premium_strangle-4"})
    assert c.post("/api/paper/delete", json={"id": "premium_strangle-4"}).status_code == 200 and not (sess / "premium_strangle-4").exists()
    assert c.post("/api/paper/delete", json={"id": "../runtime"}).status_code == 404


def test_live_feed_fetches_everything_in_one_request_per_update(tmp_path, monkeypatch):
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    fake = FakeKite(date(2026, 10, 13))
    calls = []
    real_quote = fake.quote
    price = [101.0]

    def quote(symbols):
        calls.append(list(symbols))
        out = real_quote(symbols)
        for s in symbols:
            if s.startswith("NFO:"):
                out[s]["last_price"] = price[0]
        return out
    fake.quote = quote

    class Tick:
        def sleep(self, s):
            clock[0] += timedelta(seconds=s)
            return True
    feed = kite.KiteFeed(fake, CFG, control=Tick(), log=lambda m: None)
    it = iter(feed)
    next(it)
    c = Contract("NIFTY", date(2026, 10, 13), 24000, "CE")
    assert feed.quote(c).last == 101.0            # first use: its own request
    assert feed.poll == 2
    calls.clear()
    for p in (102.0, 103.5):
        price[0] = p
        next(it)
        assert feed.quote(c).last == p            # moves on every update...
    assert len(calls) == 2 and all(len(x) == 3 for x in calls)   # ...with one request each (index, VIX, option)
    rows = (tmp_path / "options" / "NIFTY" / "2026-10-13" / "24000CE.csv").read_text().strip().splitlines()
    assert len(rows) == 2                         # header + one recorded price for this minute


def test_sync_from_server_merges_spot_history(tmp_path, monkeypatch):
    import subprocess
    from algolab import kite as k
    monkeypatch.setattr(k, "DATA", tmp_path)
    monkeypatch.setattr(k, "SPOT_DIR", tmp_path / "spot")
    (tmp_path / "spot").mkdir()
    old = pd.DataFrame({"datetime": pd.date_range("2023-01-02 09:15", periods=3, freq="5min"), "close": [1.0, 2, 3]})
    old.to_csv(tmp_path / "spot" / "NIFTY_5minute.csv", index=False)
    key = tmp_path / "key.pem"
    key.write_text("x")

    def fake_run(cmd, **kw):
        dest = Path(cmd[-1].rstrip("/"))
        if dest.name == ".server_spot":
            pd.DataFrame({"datetime": pd.date_range("2026-10-12 09:15", periods=2, freq="5min"),
                          "close": [9.0, 10]}).to_csv(dest / "NIFTY_5minute.csv", index=False)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda n: None)
    k.sync_from_server("ubuntu@x", str(key))
    merged = pd.read_csv(tmp_path / "spot" / "NIFTY_5minute.csv")
    assert len(merged) == 5   # the long history here is kept, the server's new days added


# ---- NSE closing prices and the smile correction --------------------------------
def _udiff_csv(d, rows):
    head = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
            "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
            "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty")
    lines = [head]
    for sym, inst, e, k, r, px, und in rows:
        lines.append(f"{d:%Y-%m-%d},{d:%Y-%m-%d},FO,NSE,{inst},1,,{sym},,{e:%Y-%m-%d},{e:%Y-%m-%d},{k},{r},X,"
                     f"{px},{px},{px},{px},{px},{px},{und},{px},100,0,250,1,10,F1,75")
    return "\n".join(lines).encode()


def test_bhavcopy_parses_both_nse_layouts():
    import io as _io
    import zipfile as _zf
    from algolab.eod import parse_bhavcopy
    d, e = date(2026, 10, 9), date(2026, 10, 13)
    new = _udiff_csv(d, [("NIFTY", "IDO", e, 25000, "PE", 50.5, 25200.0), ("BANKNIFTY", "IDO", e, 55000, "PE", 9, 0),
                         ("NIFTY", "IDF", e, 0, "XX", 25210, 0), ("NIFTY", "IDO", e, 25400, "CE", 40, 25200.0)])
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_0_0_0_20261009_F_0000.csv", new)
    a = parse_bhavcopy(buf.getvalue())
    assert list(a.right) == ["PE", "CE"] and list(a.strike) == [25000, 25400] and a.expiry.iloc[0] == e
    assert a.underlying.iloc[0] == 25200.0 and a.date.iloc[0] == d
    old = ("INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,"
           "OPEN_INT,CHG_IN_OI,TIMESTAMP,\n"
           "OPTIDX,NIFTY,05-Jan-2023,17800,PE,10,12,9,11.5,11.5,5000,1,100,0,03-JAN-2023,\n"
           "OPTSTK,TCS,25-Jan-2023,3200,PE,10,12,9,11.5,11.5,5,1,100,0,03-JAN-2023,\n").encode()
    b = parse_bhavcopy(old)
    assert len(b) == 1 and b.expiry.iloc[0] == date(2023, 1, 5) and b.close.iloc[0] == 11.5
    assert b.date.iloc[0] == date(2023, 1, 3) and b.volume.iloc[0] == 5000


def test_smile_corrects_estimates_without_look_ahead(tmp_path, monkeypatch):
    """Make closes with a known put skew, build the smile, and check prices use the day before."""
    import math as _m
    from algolab import eod
    from algolab import feeds as fmod
    monkeypatch.setattr(eod, "DAYS_DIR", tmp_path / "days")
    monkeypatch.setattr(eod, "SMILE_FILE", tmp_path / "smile.csv.gz")
    monkeypatch.setattr(eod, "SPOT_DIR", tmp_path / "spot")
    (tmp_path / "days").mkdir()
    (tmp_path / "spot").mkdir()
    spot_rows, cfg = [], CFG
    p = cfg["pricing"]
    days = [d.date() for d in pd.bdate_range("2026-09-01", "2026-09-30")]
    skew = lambda z: 1.0 + 0.12 * max(-z, 0)  # noqa: E731  far puts richer, like the real market
    for i, d in enumerate(days):
        s0, vix = 25000 + 20 * i, 12.0 + (i % 3)
        spot_rows.append((datetime.combine(d, datetime.min.time()).replace(hour=15, minute=25), s0, vix))
        when = datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30)
        rows = []
        for e in [x for x in days + [date(2026, 10, 6), date(2026, 10, 13)] if x.weekday() == 1 and x > d][:2]:
            t = year_fraction(when, e, p["overnight_variance_share"])
            for k in range(23000, 26050, 100):
                right = "PE" if k <= s0 else "CE"
                z = _m.log(k / s0) / (vix / 100 * _m.sqrt(t))
                px = bs_price(s0, k, t, vix / 100 * skew(z), right, p["risk_free_rate"], p["dividend_yield"])
                if px >= 0.5:
                    rows.append({"expiry": e, "strike": k, "right": right, "close": round(px, 2), "settle": px,
                                 "volume": 100, "oi": 10, "underlying": s0})
        pd.DataFrame(rows).to_csv(tmp_path / "days" / f"{d.isoformat()}.csv.gz", index=False)
    pd.DataFrame(spot_rows, columns=["datetime", "close", "vix"]).to_csv(tmp_path / "spot" / "NIFTY_5minute.csv", index=False)
    table = eod.build_smile(cfg, log=lambda m: None)
    far = table[table.z < -2.5]
    assert len(far) and (far.ratio > 1.25).all()  # the skew was recovered from the prices
    sm = eod.Smile.load(tmp_path / "smile.csv.gz")
    d1, d2 = days[5], days[6]
    t = year_fraction(datetime.combine(d2, datetime.min.time()).replace(hour=12), date(2026, 9, 15))
    r = sm.ratio(datetime.combine(d2, datetime.min.time()).replace(hour=12), date(2026, 9, 15), 23800, 25120, 0.13, t)
    assert r > 1.2
    assert sm.ratio(datetime.combine(days[0], datetime.min.time()).replace(hour=12), date(2026, 9, 8),
                    23800, 25000, 0.13, 0.02) is None  # nothing from before the first day: no look-ahead
    report = eod.validate(cfg, table)
    far_row = report.loc["far puts (z < -3)"] if "far puts (z < -3)" in report.index else report.iloc[0]
    assert far_row.plain_vix_bias_pct < -20 and far_row.with_smile_error_pct < far_row.plain_vix_error_pct
    # the feed uses it and labels the fills
    fmod._SMILE_CACHE.clear()
    monkeypatch.setattr(eod, "SMILE_FILE", tmp_path / "smile.csv.gz")
    cfg_on = {**CFG, "pricing": {**CFG["pricing"], "smile": "auto"}}
    df = make_sample("2026-09-01", "2026-09-30", s0=25000.0)
    df.index = df["datetime"] + timedelta(minutes=5)
    feed = HistoricalFeed(df, cfg_on)
    for now in feed:
        if now.date() == d2 and now.hour == 12:
            break
    c = Contract("NIFTY", date(2026, 9, 15), int(round(feed.spot / 100) * 100) - 1200, "PE")
    q = feed.quote(c)
    assert q.source == "model+smile"
    plain = HistoricalFeed(df, CFG)
    for now in plain:
        if now == feed.now:
            break
    assert q.last > plain.quote(c).last


def test_daily_collection_reaches_the_far_puts(tmp_path, monkeypatch):
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    monkeypatch.setattr(kite, "ist_now", lambda: datetime(2026, 10, 7, 16, 0))
    monkeypatch.setattr(kite, "_time", type("T", (), {"sleep": staticmethod(lambda s: None)}))

    class WideKite(FakeKite):
        def instruments(self, exchange=None):
            if exchange == "NSE":
                return super().instruments(exchange)
            return [{"name": "NIFTY", "instrument_type": r, "expiry": e, "strike": float(k),
                     "tradingsymbol": f"N{e:%d}{k}{r}", "instrument_token": k * 10 + i, "lot_size": 65}
                    for e in (date(2026, 10, 13), date(2026, 10, 20), date(2026, 10, 27))
                    for k in range(21000, 27050, 50) for i, r in enumerate(("CE", "PE"))]

    seen = []
    monkeypatch.setattr(kite, "fetch_candles", lambda k, tok, a, b, iv: seen.append(tok) or pd.DataFrame(
        {"datetime": [pd.Timestamp("2026-10-07 09:15")], "close": [1.5]}))
    n = kite.download_options(10, 2, "5minute", days=7, kite=WideKite(None), cfg=CFG, puts_below=30)
    strikes_pe = sorted({t // 10 for t in seen if t % 10 == 1})
    strikes_ce = sorted({t // 10 for t in seen if t % 10 == 0})
    # spot 24010 -> ATM 24000: calls 23500-24500, puts 22500 (30 strikes below) to 24500, two expiries
    assert strikes_ce == list(range(23500, 24550, 50)) and strikes_pe == list(range(22500, 24550, 50))
    assert n == 2 * (21 + 41)
    assert (tmp_path / "options" / "NIFTY" / "2026-10-20" / "22500PE.csv").exists()
    assert not (tmp_path / "options" / "NIFTY" / "2026-10-27").exists()
    plan = kite.download_options(10, 2, kite=WideKite(None), cfg=CFG, puts_below=30, plan_only=True)
    assert "41 PE strikes 22500 to 24500" in plan and "124 contracts" in plan


# ---- premium-matched strangle -------------------------------------------------------------
class _StrangleCtx:
    """Just enough of the engine for the strategy: prices come from a function of strike and side."""

    def __init__(self, spot, pricer):
        from algolab.models import Position
        self.Position = Position
        self.spot, self.pricer, self.strike_step = spot, pricer, 50
        self.now = datetime(2026, 10, 7, 9, 20)
        self.positions, self.trades, self.logs, self.net_realized, self.unrealized = {}, [], [], 0.0, 0.0

    def expiries(self):
        return [date(2026, 10, 13)]

    def option(self, e, k, r):
        return Contract("NIFTY", e, int(k), r)

    def price(self, c):
        return self.pricer(c.strike, c.right, self.spot)

    def quotes(self, cs):
        return {c: Quote(self.price(c)) for c in cs}

    def listed_strikes(self, e, r):
        return None

    def sell(self, c, lots=1, reason="", group=""):
        self.positions[c] = self.Position(c, -65 * lots, self.price(c), self.now, group)
        self.trades.append(("SELL", c.strike, c.right, reason))

    def close(self, c, reason=""):
        self.positions.pop(c)
        self.trades.append(("BUY", c.strike, c.right, reason))

    def log(self, m):
        self.logs.append(m)


def _toy_price(k, right, spot):
    """Premium falls by half for every 100 points out of the money (Rs 160 at the money)."""
    otm = (k - spot) if right == "CE" else (spot - k)
    return 160 * 0.5 ** (max(otm, 0) / 100) + max(-otm, 0)


def test_premium_strangle_enters_near_target_and_rolls_the_cheaper_leg():
    from algolab.strategies.premium_strangle import PremiumStrangle
    s = PremiumStrangle(min_dte=0)
    ctx = _StrangleCtx(24000, _toy_price)
    s.on_bar(ctx)
    sold = {(k, r) for side, k, r, _ in ctx.trades}
    assert sold == {(24300, "CE"), (23700, "PE")}       # Rs 20 each: 300 points out
    # NIFTY falls 100: the put doubles to 40, the call halves to 10 -> 4x: roll the call down
    ctx.spot = 23900
    ctx.now = ctx.now.replace(hour=10)
    s.on_bar(ctx)
    assert ctx.trades[2] == ("BUY", 24300, "CE", ctx.trades[2][3]) and ctx.trades[3][:3] == ("SELL", 24100, "CE")
    assert s.cycle["ce"] == 24100 and s.cycle["rolls"] == 1    # 24100 CE = Rs 40, like the put
    # no roll while the legs are within 2x of each other
    n = len(ctx.trades)
    s.on_bar(ctx)
    assert len(ctx.trades) == n
    # NIFTY falls through the put: matching the put's premium needs the call at the put's strike -> exit
    ctx.spot = 23500
    s.on_bar(ctx)
    assert s.cycle is None and not ctx.positions and "Strikes met" in ctx.trades[-1][3]
    # no new cycle the same day; one the next trading day
    s.on_bar(ctx)
    assert not ctx.positions
    ctx.now = datetime(2026, 10, 8, 9, 20)
    s.on_bar(ctx)
    assert len(ctx.positions) == 2


def test_premium_strangle_backtests_and_exits_by_expiry():
    broker = make_broker(CFG)
    strat = load_strategies()["premium_strangle"]()
    res = build_result(broker, run(HistoricalFeed(sample_df("2026-06-01", "2026-07-31"), CFG), strat, broker), [])
    assert len(res.cycles) > 3
    for _, c in res.cycles.iterrows():
        assert pd.Timestamp(c.exit_time).date() <= pd.Timestamp(c.entry_time).date() + timedelta(days=8)
    assert set(res.cycles.exit_reason.str.split(" at").str[0]) <= {"Strikes met", "Expiry-day exit"}


# ---- live orders (fake Zerodha) -----------------------------------------------------------
class _OrderBook(dict):
    """The fake's orders by id; called like Kite's orders(), it gives the day's order book (the fields read)."""

    def __call__(self):
        return [{"order_id": o["order_id"], "tradingsymbol": o["sym"], "transaction_type": o["side"], "tag": o.get("tag"),
                 "order_type": o["type"], "status": o["status"], "filled_quantity": o["filled_quantity"],
                 "average_price": o.get("average_price", 0), "product": o.get("product", "NRML"),
                 "order_timestamp": o.get("order_timestamp")}
                for o in self.values()]


class FakeOrders(FakeKite):
    """FakeKite plus an order book. `mode` decides what happens to the next LIMIT order."""

    def __init__(self, expiry, mode="fill"):
        super().__init__(expiry)
        self.mode, self.orders, self.n, self.history_calls = mode, _OrderBook(), 0, 0

    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, trigger_price=None, validity=None, tag=None, **k):
        self.n += 1
        oid = str(self.n)
        o = {"order_id": oid, "sym": tradingsymbol, "side": transaction_type, "qty": quantity, "type": order_type,
             "price": price, "trigger": trigger_price, "product": product, "filled_quantity": 0, "average_price": 0,
             "tag": tag}
        if order_type == "SL":
            o["status"] = "TRIGGER PENDING"
        elif self.mode == "fill":
            o.update(status="COMPLETE", filled_quantity=quantity, average_price=price)
        elif self.mode == "reject":
            o.update(status="REJECTED", status_message="Insufficient funds")
        elif self.mode == "partial":
            o.update(status="OPEN", filled_quantity=quantity // 2, average_price=price)
        else:
            o["status"] = "OPEN"
        self.orders[oid] = o
        return oid

    def order_history(self, oid):
        self.history_calls += 1
        return [dict(self.orders[oid])]

    def modify_order(self, variety, order_id, price=None, **k):
        self.orders[order_id]["price"] = price

    def cancel_order(self, variety, order_id, **k):
        o = self.orders[order_id]
        if o["status"] in ("OPEN", "TRIGGER PENDING"):
            o["status"] = "CANCELLED"

    def positions(self):
        net = {}
        for o in self.orders.values():
            if o["filled_quantity"] and o["status"] in ("COMPLETE", "CANCELLED", "OPEN"):
                net[o["sym"]] = net.get(o["sym"], 0) + (o["filled_quantity"] if o["side"] == "BUY" else -o["filled_quantity"])
        return {"net": [{"tradingsymbol": s, "exchange": "NFO", "product": "NRML", "quantity": q} for s, q in net.items()]}

    def sent(self, kind="LIMIT"):
        return [o for o in self.orders.values() if o["type"] == kind]


def _live_broker(kite_, mode="live", **settings):
    from algolab.broker import CostModel
    from algolab.live import LiveBroker, LiveSettings
    s = LiveSettings.from_dict({"mode": mode, "daily_loss_limit": 5000, **settings})
    b = LiveBroker(300000, CostModel(), 0.5, 0.05, CFG.get("cost_history"), settings=s, sleep=lambda x: None,
                   log=lambda m: None)
    expiry = date(2026, 10, 13)
    feed = type("F", (), {"kite": kite_, "_symbols": {(expiry, k, r): f"NIFTY26O13{k}{r}" for k in range(23500, 24550, 50)
                                                      for r in ("CE", "PE")},
                          "lot_size_for": lambda self, e: 65})()
    b.attach(feed)
    return b, expiry


def test_live_orders_fill_and_leave_a_protective_stop():
    from algolab.live import Halt
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    f = b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g1")
    sell = k.sent()[0]
    assert (sell["side"], sell["qty"], sell["price"], sell["product"]) == ("SELL", 65, 19.9, "NRML")
    assert f.price == 19.9 and f.source == "live" and b.positions[c].qty == -65
    sl = k.sent("SL")[0]
    assert sl["side"] == "BUY" and sl["trigger"] == pytest.approx(59.7) and sl["status"] == "TRIGGER PENDING"
    # buying back cancels the stop first
    b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g1")
    assert sl["status"] == "CANCELLED" and not b.positions and k.sent()[-1]["side"] == "BUY"
    # guards: too many lots to open
    with pytest.raises(Halt, match="above the limit"):
        b.execute(NOW, c, "SELL", 130, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g2")
    # the account round-trips through the saved state
    b2, _ = _live_broker(k)
    b2.load_dict(json.loads(json.dumps(b.to_dict())))
    assert b2.orders_today == b.orders_today and len(b2.trades) == 1


@pytest.mark.parametrize("mode, match", [("reject", "rejected"), ("never", "did not fill"), ("partial", "Only 32 of 65")])
def test_live_order_problems_stop_the_session(mode, match):
    from algolab.live import Halt
    k = FakeOrders(date(2026, 10, 13), mode)
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    with pytest.raises(Halt, match=match):
        b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    if mode == "partial":   # what did fill is on the books, so it can be squared off
        assert b.positions[c].qty == -32
    else:
        assert not b.positions


def test_protective_stop_fill_and_mismatch_are_caught():
    from algolab.live import Blocked, Halt
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    b.reconcile(NOW)   # matches Zerodha
    sl = k.sent("SL")[0]
    sl.update(status="COMPLETE", filled_quantity=65, average_price=61.0)
    with pytest.raises(Halt, match="Protective stop filled"):
        b.check_protective_stops(NOW)
    assert not b.positions and b.trades[-1].exit_price == 61.0
    # a position Zerodha does not have (and no Kite order of yours closed it) blocks the next start
    b3, _ = _live_broker(FakeOrders(date(2026, 10, 13)))
    b3._apply(NOW, c, "SELL", 65, 20.0, 0.0, "Entry", "g", "live")
    with pytest.raises(Blocked, match="differ from Zerodha"):
        b3.reconcile(NOW)


def test_shadow_session_sends_nothing_and_kill_squares_off(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    fake = {}

    def feed(c, g):
        fake["k"] = FakeOrders(date(2026, 10, 13))
        return kite.KiteFeed(fake["k"], c, g, log=g.log)
    monkeypatch.setattr(paper, "_kite_feed", feed)
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "shadow", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert s["mode"] == "shadow" and len(s["positions"]) == 2 and not fake["k"].orders
    assert any("SHADOW (not sent): SELL 65" in line for line in s["logs"])
    # Stop & square off from the dashboard
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["positions"] and "Stopped from the dashboard" in s["halted"]
    assert paper.read_desired(sid) is None and "squared off" in paper.session_status(sid)


def test_live_needs_a_daily_loss_limit():
    from algolab.live import LiveSettings
    with pytest.raises(ValueError, match="daily loss limit"):
        LiveSettings.from_dict({"mode": "live"})


def test_live_session_end_to_end_with_fake_zerodha(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    fake = FakeOrders(date(2026, 10, 13))
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    sells = [o for o in fake.sent() if o["side"] == "SELL"]
    assert s["mode"] == "live" and len(s["positions"]) == 2 and len(sells) == 2
    assert len(fake.sent("SL")) == 2 and all(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL"))
    net = {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"]}
    assert sorted(net.values()) == [-65, -65]
    # resumes after a restart (positions match Zerodha), then Stop & square off
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["positions"] and all(q == 0 for q in {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"]}.values())
    assert all(o["status"] == "CANCELLED" for o in fake.sent("SL"))     # stops cancelled before buying back
    assert paper.read_desired(sid) is None and "squared off" in paper.session_status(sid)


def _real_store(tmp_path, spot: pd.DataFrame, day: date, expiry: date) -> OptionStore:
    """Real-looking option prices for one day: every strike within 600 points, every 5 minutes."""
    store = OptionStore("NIFTY", root=tmp_path / "options")
    bars = spot[pd.to_datetime(spot.datetime).dt.date == day]
    for k in range(int(bars.close.min() // 50 * 50) - 600, int(bars.close.max() // 50 * 50) + 650, 50):
        for right in ("CE", "PE"):
            c = Contract("NIFTY", expiry, k, right)
            t = pd.to_datetime(bars.datetime) + pd.Timedelta(minutes=5)
            px = [max(c.intrinsic(s), 0) + 60.0 for s in bars.close]
            store.save_frame(c, pd.DataFrame({"time": t, "last": px, "bid": "", "ask": ""}))
    return store


def test_real_prices_only_backtest_and_replay(tmp_path, monkeypatch):
    """With pricing.real_only (the default) only days with stored real option prices are tested, the
    strategy trades the expiry those prices belong to, and every fill uses a stored price."""
    from algolab import engine as emod
    from algolab import feeds as fmod
    from algolab.replay import record
    df = make_sample("2026-09-14", "2026-09-18")
    monkeypatch.setattr(fmod, "SPOT_DIR", tmp_path)
    df.to_csv(tmp_path / "T.csv", index=False)
    day, expiry = date(2026, 9, 16), date(2026, 9, 22)
    store = _real_store(tmp_path, df, day, expiry)
    from algolab import replay as rmod
    monkeypatch.setattr(fmod, "OptionStore", lambda und, **kw: store)
    monkeypatch.setattr(rmod, "OptionStore", lambda und, **kw: store)
    cfg = load_config()
    cfg["pricing"].update(smile="off", real_only=True)
    res = emod.backtest(cfg, load_strategies()["intraday_straddle"](), "T")
    assert res.metrics["Option prices"] == "real only" and res.metrics["Days tested"] == 1
    assert len(res.fills) >= 4 and set(res.fills.source) == {"market"}
    assert {pd.Timestamp(t).date() for t in res.fills.time} == {day}
    assert all("22 Sep 26" in c for c in res.fills.contract)
    rp = record(cfg, load_strategies()["intraday_straddle"](), "T", "2026-09-14", "2026-09-18", warmup_days=0)
    assert {pd.Timestamp(f["time"]).date() for f in rp.frames} == {day} and set(rp.fills.priced) == {"market"}
    with pytest.raises(ValueError, match="No days with real option prices"):
        emod.backtest(cfg, load_strategies()["intraday_straddle"](), "T", "2026-09-17", "2026-09-18")
    cfg["pricing"]["real_only"] = False           # the model is still there when asked for
    assert emod.backtest(cfg, load_strategies()["intraday_straddle"](), "T").metrics.get("Option prices") is None


def test_payoff_of_open_legs():
    """The Paper page's payoff chart: breakevens, max profit/loss and the open P&L now for an iron fly."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "algolab_panel_payoff", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def leg(k, right, qty, avg, ltp):
        c = {"underlying": "NIFTY", "expiry": "2026-10-20", "strike": k, "right": right}
        return ({"contract": c, "qty": qty, "avg_price": avg, "opened_at": "2026-10-14T09:20:00", "group": "g",
                 "entry_costs": 20.0},
                {"Contract": f"NIFTY 20 Oct 26 {k} {right}", "LTP": ltp})
    legs = [leg(25000, "CE", -65, 180, 150), leg(25000, "PE", -65, 170, 140),
            leg(25500, "CE", 65, 30, 20), leg(24500, "PE", 65, 35, 25)]
    s = {"time": "2026-10-15T11:00:00+05:30", "spot": 25010.0, "vix": 12.5,
         "broker": {"positions": [a for a, _ in legs]}, "positions": [b for _, b in legs]}
    p = mod._payoff(s)
    credit = (180 + 170 - 30 - 35) * 65
    assert p["max_profit"] == credit - 80                       # at 25000, after entry charges
    assert p["max_loss"] == credit - 500 * 65 - 80              # wing width minus the credit
    assert not p["loss_open_below"] and not p["loss_open_above"]
    assert [round(b) for b in p["breakevens"]] == [24716, 25284]
    assert abs(p["now"] - ((30 + 30 - 10 - 10) * 65 - 80)) <= 1  # model at today's prices = live open P&L
    assert all(l["iv_from"] == "price" for l in p["legs"]) and p["theta"] > 0 and p["vega"] < 0
    assert p["today"][p["x"].index(25010.0)] == p["now"]

    naked = {**s, "broker": {"positions": [a for a, _ in legs[:2]]}, "positions": [b for _, b in legs[:2]]}
    q = mod._payoff(naked)
    assert q["loss_open_below"] and q["loss_open_above"]       # a short straddle's loss has no floor
    assert mod._payoff({**s, "broker": {"positions": []}}) is None


class _FakeKite:
    """Enough of Zerodha's client for the chart page: quotes, candles and the instrument lists."""
    access_token = "tok"

    def __init__(self):
        self.calls = []

    def quote(self, symbols):
        self.calls.append(("quote", tuple(symbols)))
        base = {"NSE:NIFTY 50": (25110.5, 25010.5), "BSE:SENSEX": (81900.0, 82000.0), "NSE:NIFTY BANK": (56000.0, 56000.0),
                "NSE:RELIANCE": (1400.0, 1380.0), "NFO:NIFTY26O2025000CE": (150.0, 140.0)}
        return {s: {"instrument_token": 1000 + i, "last_price": base[s][0], "ohlc": {"open": 1, "high": 2, "low": 0.5, "close": base[s][1]},
                    "volume": 10, "oi": 0, "last_trade_time": None} for i, s in enumerate(symbols) if s in base}

    def historical_data(self, token, start, end, interval):
        from datetime import datetime as dt
        return [{"date": dt(2026, 10, 14, 15, 29), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 5},
                {"date": dt(2026, 10, 15, 9, 15), "open": 1, "high": 2, "low": 0.5, "close": 1.6, "volume": 5},
                {"date": dt(2026, 10, 15, 9, 16), "open": 1.6, "high": 2, "low": 1.1, "close": 1.7, "volume": 5}]

    def instruments(self, exchange):
        from datetime import date
        if exchange == "NFO":
            return [{"name": "NIFTY", "expiry": date(2026, 10, 20), "strike": 25000.0, "instrument_type": "CE",
                     "tradingsymbol": "NIFTY26O2025000CE"}]
        if exchange == "NSE":
            return [{"tradingsymbol": "RELIANCE", "name": "RELIANCE INDUSTRIES", "segment": "NSE", "instrument_type": "EQ"},
                    {"tradingsymbol": "NIFTY 50", "name": "NIFTY 50", "segment": "INDICES", "instrument_type": "EQ"}]
        return [{"tradingsymbol": "SENSEX", "name": "SENSEX", "segment": "INDICES", "instrument_type": "EQ"}]


def test_live_chart_and_index_strip(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import importlib.util
    import sys
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "algolab_panel_chart", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.resolve_symbol("reliance") == "NSE:RELIANCE"
    assert mod.resolve_symbol("banknifty") == "NSE:NIFTY BANK"
    assert mod.resolve_symbol("NIFTY26OCT25000CE") == "NFO:NIFTY26OCT25000CE"
    assert mod.resolve_symbol("SENSEX2610281000PE") == "BFO:SENSEX2610281000PE"
    assert mod.resolve_symbol("bse:sensex") == "BSE:SENSEX"

    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "kite", None, raising=False)
    r = c.get("/api/chart/indices")
    assert r.status_code == 503 and "log" in r.get_json()["error"].lower()        # no Zerodha login yet

    monkeypatch.setattr(main, "kite", _FakeKite(), raising=False)
    mod._CACHE.clear()
    idx = {i["name"]: i for i in c.get("/api/chart/indices").get_json()["indices"]}
    assert idx["NIFTY"]["change"] == 100.0 and idx["NIFTY"]["pct"] == 0.4
    assert idx["SENSEX"]["change"] == -100.0 and idx["BANK NIFTY"]["change"] == 0.0

    q = c.get("/api/chart/quote?u=NIFTY&e=2026-10-20&k=25000&r=CE").get_json()   # a Paper position's contract
    assert q["symbol"] == "NFO:NIFTY26O2025000CE" and q["change"] == 10.0
    assert c.get("/api/chart/quote?u=NIFTY&e=2026-10-27&k=25000&r=CE").status_code == 400
    assert c.get("/api/chart/quote?s=nosuch").status_code == 400

    d = c.get("/api/chart/candles?s=RELIANCE&range=1d").get_json()
    assert [k[0] for k in d["candles"]] == ["2026-10-15T09:15", "2026-10-15T09:16"]   # the latest day only
    assert c.get("/api/chart/candles?s=RELIANCE&range=9y").status_code == 400
    found = c.get("/api/chart/search?q=REL").get_json()["results"]
    assert found[0]["symbol"] == "NSE:RELIANCE"
    assert c.get("/chart").status_code == 200

    page = "<html><head></head><body><nav class=sitenav></nav><div>holdings</div></body></html>"
    assert "idxStrip" in mod._portfolio_additions(page, "/")
    assert "idxStrip" not in mod._portfolio_additions(page, "/paper")


def test_paper_session_keeps_its_orders_and_stops(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    from algolab import feeds
    real_sample = feeds.make_sample
    monkeypatch.setattr(feeds, "make_sample", lambda start, end, interval=5, seed=7, s0=24000.0: real_sample(start, end, interval, 7, s0))
    monkeypatch.setattr(paper, "ist_now", lambda: datetime(2026, 10, 7, 9, 0))
    paper.set_desired("short_straddle", {"lots": 1}, "sim", risk={"stops": "framework", "leg_stop": 25, "day_loss": 20000})
    paper.run_group(cfg, lambda: paper._desired_all(), "sim", max_bars=20)
    st = paper.read_state("short_straddle")
    assert st["risk"]["leg_stop"] == 25 and st["risk"]["stops"] == "framework"
    assert st["params"]["stop_loss_pct"] == 0.0                           # its own stop is off: the framework's is used
    assert st["risk_state"]["day"] and isinstance(st["risk_state"]["legs"], dict)


def test_live_market_orders_and_the_stop_follows_the_trail():
    k = FakeOrders(date(2026, 10, 13))
    k.modify_order = lambda variety, order_id, price=None, trigger_price=None, **kw: k.orders[order_id].update(price=price, trigger=trigger_price)
    b, e = _live_broker(k, order_type="market", max_chase_pct=5)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g1")
    assert k.sent()[0]["price"] == pytest.approx(18.9)                       # one limit 5% through the bid: fills at once
    sl = k.sent("SL")[0]
    assert sl["trigger"] == pytest.approx(56.7)                              # 3x the price it filled at
    b.FOLLOW_EVERY = 0
    b.follow_stop(c, 26.0, NOW)                                               # the trailing stop is tighter: moved down
    assert sl["trigger"] == pytest.approx(26.0) and b.protect_level[c.label] == pytest.approx(26.0)
    b.follow_stop(c, 30.0, NOW)                                               # never loosened
    assert sl["trigger"] == pytest.approx(26.0)
    from algolab.live import LiveSettings
    with pytest.raises(ValueError):
        LiveSettings.from_dict({"mode": "shadow", "order_type": "stop"})


def test_framework_trailing_moves_the_zerodha_stop():
    """The risk manager hands each sold leg's stop to the live broker, which moves the protective stop."""
    from algolab.risk import RiskManager
    k = FakeOrders(date(2026, 10, 13))
    k.modify_order = lambda variety, order_id, price=None, trigger_price=None, **kw: k.orders[order_id].update(price=price, trigger=trigger_price)
    b, e = _live_broker(k)
    b.FOLLOW_EVERY = 0
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=20.0, ask=20.1, source="market"), "Entry", "g1")
    prices = {c: 20.0}
    ctx = type("C", (), {"broker": b, "now": NOW, "equity": 300000.0, "positions": property(lambda s: b.positions),
                         "price": lambda s, x: prices[x], "close": lambda s, x, r="": None, "close_all": lambda s, r="": None,
                         "log": lambda s, m: None})()
    m = RiskManager({"stops": "framework", "leg_stop": 50, "leg_trail_every": 10})       # stop 30 (50% above 20), trails
    m.check(ctx)
    assert k.sent("SL")[0]["trigger"] == pytest.approx(30.0)
    prices[c] = 14.0                                                           # gained 30%: stop down 3 steps of 2
    m.check(ctx)
    assert k.sent("SL")[0]["trigger"] == pytest.approx(24.0)


def test_larger_orders_allowed_for_a_session_are_placed_with_a_warning():
    """Per session (chosen when deploying): an order above max lots is sent with a warning and an alert instead
    of stopping the session; the orders-per-day limit still stops it."""
    from algolab.live import Halt, LiveSettings
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k, allow_over_max_lots=True, max_orders_per_day=2)
    alerts, logs = [], []
    b.notify, b.log = (lambda s, body: alerts.append(s)), logs.append
    c = Contract("NIFTY", e, 24300, "CE")
    f = b.execute(NOW, c, "SELL", 130, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g1")
    assert f is not None and b.positions[c].qty == -130 and k.sent()[0]["qty"] == 130
    assert alerts[0] == "Order above max lots" and any(m.startswith("WARNING: Order of 130 units") for m in logs)
    c2 = Contract("NIFTY", e, 24350, "CE")
    b.execute(NOW, c2, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g2")
    with pytest.raises(Halt, match="daily limit"):                   # orders per day: still a hard limit
        b.execute(NOW, Contract("NIFTY", e, 24400, "CE"), "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g3")
    assert LiveSettings.from_dict({"allow_over_max_lots": "false"}).allow_over_max_lots is False
    assert LiveSettings.from_dict({"allow_over_max_lots": "true"}).allow_over_max_lots is True
    assert LiveSettings.from_dict({}).allow_over_max_lots is False                  # off unless chosen


class _TwoLegKite(FakeOrders):
    """A LIMIT order only completes once the other leg's order has been sent: a second leg that waits for the first
    to fill would never get its turn."""

    def order_history(self, oid):
        o = self.orders[oid]
        if o["type"] == "LIMIT" and o["status"] == "OPEN" and len(self.sent()) >= 2:
            o.update(status="COMPLETE", filled_quantity=o["qty"], average_price=o["price"])
        return super().order_history(oid)


def _two_legs(e):
    q = Quote(20.0, bid=19.9, ask=20.1, source="market")
    return [(Contract("NIFTY", e, 24300, "CE"), "SELL", 65, q, "Entry", "g"),
            (Contract("NIFTY", e, 24300, "PE"), "SELL", 65, q, "Entry", "g")]


def test_live_entry_legs_are_sent_together_and_both_get_a_stop():
    import time as _t
    k = _TwoLegKite(date(2026, 10, 13), "never")
    b, e = _live_broker(k)
    b.sleep = lambda s: _t.sleep(0.01)
    fills = b.execute_many(NOW, _two_legs(e))
    assert len(fills) == 2 and all(f.source == "live" for f in fills)
    assert sorted(o["side"] for o in k.sent()) == ["SELL", "SELL"] and len(k.sent("SL")) == 2
    assert sorted(c.right for c in b.positions) == ["CE", "PE"] and b.orders_today["n"] == 2


def test_live_entry_leg_that_fails_still_books_the_other_and_stops_the_session():
    from algolab.live import Halt

    class OneRejected(FakeOrders):
        def place_order(self, *a, **k):
            oid = super().place_order(*a, **k)
            if self.orders[oid]["sym"].endswith("PE") and self.orders[oid]["type"] == "LIMIT":
                self.orders[oid].update(status="REJECTED", filled_quantity=0, average_price=0,
                                        status_message="Insufficient funds")
            return oid

    k = OneRejected(date(2026, 10, 13))
    b, e = _live_broker(k)
    with pytest.raises(Halt, match="rejected"):
        b.execute_many(NOW, _two_legs(e))
    assert [c.right for c in b.positions] == ["CE"] and len(k.sent("SL")) == 1   # the filled leg is on the books


def test_shadow_entry_legs_send_nothing():
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k, mode="shadow")
    assert len(b.execute_many(NOW, _two_legs(e))) == 2
    assert not k.orders and sorted(c.right for c in b.positions) == ["CE", "PE"]


def _shadow_with_legs(tmp_path, monkeypatch):
    """A shadow session that has opened its two legs (as in test_shadow_session_sends_nothing_and_kill_squares_off)."""
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(FakeOrders(date(2026, 10, 13)), c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "shadow", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert len(paper.read_state(sid)["positions"]) == 2
    return cfg, sid, want


def test_stop_with_no_positions_works_while_the_market_is_closed(tmp_path, monkeypatch):
    """Stop on a flat session used to wait for the next price update (09:15 when pressed before the open)."""
    cfg = _paper_tmp(tmp_path, monkeypatch)
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "shadow", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    g = paper.Group(cfg, want)
    g.sync()
    assert sid in g.members
    paper.kill(sid)
    assert g.sleep(3) is False                      # no feed update at all: the group notices within a second
    assert not g.members and paper.read_desired(sid) is None
    assert not (paper.Slot(sid).dir / "KILL").exists()
    assert "no open positions" in paper.session_status(sid)


def test_stop_with_positions_waits_for_prices_and_says_so(tmp_path, monkeypatch):
    cfg, sid, want = _shadow_with_legs(tmp_path, monkeypatch)
    g = paper.Group(cfg, want)
    g.sync()
    paper.kill(sid)
    assert g.sleep(2.5) is True                     # still running: the square-off needs live prices
    s = g.members[sid]
    assert not s.halted and s.broker.positions and (s.slot.dir / "KILL").exists()
    assert "next market update" in paper.session_status(sid)
    g.sleep(1.5)
    assert sum("Stop requested" in line for line in s.logs) == 1   # said once, not every second
    # the next update squares off as before
    paper.run_group(cfg, want, "kite", max_bars=3)
    st = paper.read_state(sid)
    assert not st["positions"] and "Stopped from the dashboard" in st["halted"] and paper.read_desired(sid) is None


def test_stop_for_a_session_that_is_not_running(tmp_path, monkeypatch):
    """E.g. waiting for the day's Zerodha login: a flat session stops; one with positions, or one whose group is
    running (it may be in the middle of an order), is left alone."""
    cfg = _paper_tmp(tmp_path, monkeypatch)
    flat = paper.set_desired("premium_strangle", {}, "kite", "shadow", {"max_lots": 1, "daily_loss_limit": 3000})
    held = paper.set_desired("iron_fly", {}, "kite", "shadow", {"max_lots": 1, "daily_loss_limit": 3000})
    paper._write_json(paper.Slot(held).state, {"positions": [{"Contract": "x", "Qty": -65}], "broker": {"positions": [{}]}})
    for sid in (flat, held):
        paper.kill(sid)
    desired = paper._desired_all()
    assert paper.stop_idle_kills(desired, {"kite"}) == []          # group running: Group.sleep handles it
    assert paper.stop_idle_kills(desired, set()) == [flat]
    assert paper.read_desired(flat) is None and "no open positions" in paper.session_status(flat)
    assert not (paper.Slot(flat).dir / "KILL").exists()
    assert paper.read_desired(held) is not None and (paper.Slot(held).dir / "KILL").exists()


def test_a_flat_session_that_stops_cancels_a_leftover_protective_stop():
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    sl = k.sent("SL")[0]
    b.positions.clear()                              # flat in the books, but the stop was never cancelled
    assert b.cancel_all_protective(NOW) == [] and sl["status"] == "CANCELLED" and not b.protect
    # a stop that filled while flat, and one Zerodha will not cancel, are both reported with the order id
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    c2 = Contract("NIFTY", e, 24400, "CE")
    b.execute(NOW, c2, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    sl1, sl2 = k.sent("SL")[-2:]
    sl1.update(status="COMPLETE", filled_quantity=65, average_price=61.0)
    b.positions.clear()
    k.cancel_order = lambda **kw: (_ for _ in ()).throw(RuntimeError("network"))
    problems = b.cancel_all_protective(NOW)
    assert any(sl1["order_id"] in p and "extra position" in p for p in problems)
    assert any(sl2["order_id"] in p and "could not be cancelled" in p for p in problems)
    assert list(b.protect) == [c2.label]            # kept: not forgotten


def test_stop_waits_for_a_session_file_that_cannot_be_read_or_a_leftover_stop(tmp_path, monkeypatch):
    _paper_tmp(tmp_path, monkeypatch)
    bad = paper.set_desired("premium_strangle", {}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    stop = paper.set_desired("iron_fly", {}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    paper.Slot(bad).state.write_text("{not json")
    paper._write_json(paper.Slot(stop).state, {"positions": [], "broker": {"positions": [], "live": {"protect": {"x": "9"}}},
                                              "logs": []})
    for sid in (bad, stop):
        paper.kill(sid)
    assert paper.stop_idle_kills(paper._desired_all(), set()) == []




def test_several_live_sessions_limit_and_stop_all(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    (tmp_path / "runtime").mkdir()
    spec = importlib.util.spec_from_file_location(
        "algolab_panel_multi", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(paper, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(paper, "CATALOG", tmp_path / "runtime" / "strategies.json")
    paper.write_catalog()
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    live = {"strategy": "premium_strangle", "feed": "kite", "params": {}, "mode": "live", "confirm": "LIVE",
            "live": {"max_lots": "1", "daily_loss_limit": "4000"}}
    assert c.get("/api/live/settings").get_json() == {"max_sessions": 3, "running": [], "is_owner": True}
    ids = [c.post("/api/paper/start", json=live).get_json()["id"] for _ in range(3)]        # default: 3 at once
    assert len(set(ids)) == 3
    r = c.post("/api/paper/start", json=live)
    assert r.status_code == 400 and "the limit is 3" in r.get_json()["error"]
    assert c.post("/api/live/settings", json={"max_sessions": 0}).status_code == 400
    assert c.post("/api/live/settings", json={"max_sessions": 11}).status_code == 400
    assert c.post("/api/live/settings", json={"max_sessions": 4}).get_json()["max_sessions"] == 4
    assert c.post("/api/paper/start", json=live).status_code == 200
    assert c.post("/api/paper/start", json={**live, "mode": "shadow"}).status_code == 400     # Shadow was removed
    assert len(c.get("/api/live/settings").get_json()["running"]) == 4
    # Stop all LIVE: every live session gets Stop & square off, shadow and paper are left alone
    stopped = c.post("/api/live/stop-all").get_json()["stopped"]
    sess = tmp_path / "runtime" / "sessions"
    assert len(stopped) == 4 and all((sess / i / "KILL").exists() for i in stopped)
    assert [p.parent.name for p in sess.glob("*/KILL")] and len(list(sess.glob("*/KILL"))) == 4


def test_live_sessions_holding_the_same_contract_reconcile_against_their_sum():
    """Two live sessions short the same option: Zerodha shows -130; each session holds -65."""
    from algolab.live import Blocked
    k = FakeOrders(date(2026, 10, 13))
    a, e = _live_broker(k)
    b, _ = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    a.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    b.reconciled = True            # b started flat: its first check passed before a traded
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    assert {p["tradingsymbol"]: p["quantity"] for p in k.positions()["net"]} == {"NIFTY26O1324300CE": -130}
    # after a restart each one checks before its first order
    a2, _ = _live_broker(k)
    a2.load_dict(json.loads(json.dumps(a.to_dict())))
    with pytest.raises(Blocked, match="ours -65, Zerodha -130"):
        a2.reconcile(NOW)             # alone it looks wrong
    a2.others = lambda: {"NIFTY26O1324300CE": -65}
    a2.reconcile(NOW)                 # with the other live session's -65 it matches
    a3, _ = _live_broker(k)
    a3.load_dict(json.loads(json.dumps(a.to_dict())))
    a3.others = lambda: {"NIFTY26O1324300CE": -130}
    with pytest.raises(Blocked, match="other live sessions -130"):
        a3.reconcile(NOW)
    # each session has its own protective stop for its own 65
    assert len(k.sent("SL")) == 2 and all(o["qty"] == 65 for o in k.sent("SL"))


def test_group_tells_each_live_session_what_the_others_hold(tmp_path, monkeypatch):
    """Two LIVE sessions of premium_strangle on one fake Zerodha account: both enter, both reconcile after a restart."""
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    fake = FakeOrders(date(2026, 10, 13))
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    lim = {"max_lots": 1, "daily_loss_limit": 3000}
    s1 = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", lim)
    s2 = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", lim, sid="premium_strangle-2")
    want = lambda: {s: paper.read_desired(s) for s in (s1, s2) if paper.read_desired(s)}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    for s in (s1, s2):
        assert len(paper.read_state(s)["positions"]) == 2, paper.read_state(s)["logs"][-5:]
    net = {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"]}
    assert sorted(net.values()) == [-130, -130]                     # both sold the same two strikes
    # restart: each reconciles against Zerodha's -130 with the other's -65 counted, and keeps running
    paper.run_group(cfg, want, "kite", max_bars=3)
    for s in (s1, s2):
        st = paper.read_state(s)
        assert not st["halted"] and len(st["positions"]) == 2
        assert not any("differ from Zerodha" in line for line in st["logs"])
    # Stop & square off one: the other keeps its legs and Zerodha shows only those
    paper.kill(s1)
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert not paper.read_state(s1)["positions"] and len(paper.read_state(s2)["positions"]) == 2
    assert sorted({p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"]}.values()) == [-65, -65]


# ---- multi-live safety: stop cancel, opposite legs, position check -------------------------
def test_buy_back_waits_for_the_stop_cancel_and_never_buys_twice():
    from algolab.live import CancelFailed
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    sl = k.sent("SL")[0]
    k.cancel_order = lambda variety, order_id, **kw: (_ for _ in ()).throw(RuntimeError("network"))   # cancel fails
    buys_before = len([o for o in k.sent() if o["side"] == "BUY"])
    with pytest.raises(CancelFailed, match="NOT bought back"):
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1), "Exit", "g")
    assert len([o for o in k.sent() if o["side"] == "BUY"]) == buys_before      # no buy-back sent
    assert b.protect[c.label] == sl["order_id"] and b.positions[c].qty == -65      # still on the books


def test_stop_that_fills_while_cancelling_is_booked_not_bought_again():
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    sl = k.sent("SL")[0]

    def cancel_too_late(variety, order_id, **kw):      # the stop fired just before the cancel arrived
        sl.update(status="COMPLETE", filled_quantity=65, average_price=58.0)
        raise RuntimeError("Order cannot be cancelled: already complete")
    k.cancel_order = cancel_too_late
    n = len(k.sent())
    b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1), "Exit", "g")
    assert len(k.sent()) == n and not b.positions and b.trades[-1].exit_price == 58.0


def test_partly_filled_stop_leaves_only_the_rest_to_buy():
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    sl = k.sent("SL")[0]
    sl.update(status="OPEN", filled_quantity=25, average_price=58.0)    # triggered, part filled
    b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1), "Exit", "g")
    assert sl["status"] == "CANCELLED" and k.sent()[-1]["qty"] == 40 and not b.positions


def test_opposite_leg_in_a_contract_another_live_session_holds_is_refused():
    from algolab.live import Halt
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.others = lambda: {"NIFTY26O1324300CE": -65}
    with pytest.raises(Halt, match="another LIVE session holds -65"):
        b.execute(NOW, c, "BUY", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    assert not k.sent()
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")   # same direction is fine
    assert b.positions[c].qty == -65


def test_position_check_needs_two_misses_and_reports_unowned_once():
    from algolab.live import Blocked, LiveBroker
    LiveBroker._unowned_told.clear()
    k = FakeOrders(date(2026, 10, 13))
    told = []
    b, e = _live_broker(k)
    b.notify = lambda subj, body: told.append(subj)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    b.check_positions(NOW)                                      # matches
    k.orders["x"] = {"order_id": "x", "sym": "NIFTY26O1324400PE", "side": "SELL", "qty": 65, "type": "LIMIT",
                     "status": "COMPLETE", "filled_quantity": 65, "average_price": 30, "product": "NRML"}
    b.check_positions(NOW); b.check_positions(NOW)             # held by nobody: told once, no stop
    assert told.count("Position held by no live session") == 1
    k.orders["y"] = {"order_id": "y", "sym": "NIFTY26O1324300CE", "side": "BUY", "qty": 65, "type": "LIMIT",
                     "status": "COMPLETE", "filled_quantity": 65, "average_price": 30, "product": "MIS"}
    b.check_positions(NOW)                                      # first miss (no order of yours closed it): logged
    with pytest.raises(Blocked, match="two checks in a row"):
        b.check_positions(NOW)


def test_position_check_counts_other_live_sessions():
    k = FakeOrders(date(2026, 10, 13))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    k.orders["z"] = {"order_id": "z", "sym": "NIFTY26O1324300CE", "side": "SELL", "qty": 65, "type": "LIMIT",
                     "status": "COMPLETE", "filled_quantity": 65, "average_price": 20, "product": "NRML"}
    b.others = lambda: {"NIFTY26O1324300CE": -65}
    b.check_positions(NOW); b.check_positions(NOW)              # -130 at Zerodha = -65 ours + -65 other: fine
