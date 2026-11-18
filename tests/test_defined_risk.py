"""The defined-risk strategies (iron fly, double calendar, put credit spread, event iron fly), the
intraday strangle, their regime filters, and the dashboard's shared menu."""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab import events  # noqa: E402
from algolab.config import load_config  # noqa: E402
from algolab.engine import build_result, make_broker, run  # noqa: E402
from algolab.feeds import HistoricalFeed, make_sample  # noqa: E402
from algolab.strategy import load_strategies  # noqa: E402

CFG = load_config()
CFG["pricing"]["smile"] = "off"
CFG["pricing"]["real_only"] = False  # synthetic test data: options are priced by the model
NEW = ["iron_fly", "double_calendar", "put_credit_spread", "intraday_strangle", "event_iron_fly"]


def sample_df(start="2026-01-01", end="2026-09-30", vix=None):
    df = make_sample(start, end)
    df.index = df["datetime"] + timedelta(minutes=5)
    if vix is not None:
        df["vix"] = vix
    return df


def backtest(name, df=None, **params):
    broker = make_broker(CFG)
    logs = []
    strat = load_strategies()[name](**params)
    res = build_result(broker, run(HistoricalFeed(sample_df() if df is None else df, CFG), strat, broker, logs), logs)
    return res, broker, logs


@pytest.mark.parametrize("name", NEW)
def test_new_strategies_are_listed_with_live_and_filters(name):
    cls = load_strategies()[name]
    assert cls.live_ready and cls.title and cls.description
    s = cls()
    assert s.get_state() is not None
    s.set_state(s.get_state())


@pytest.mark.parametrize("name", [n for n in NEW if n != "event_iron_fly"])
def test_backtests_reconcile_and_trade_whole_lots(name):
    res, broker, _ = backtest(name)
    assert len(res.cycles) >= 3
    if not broker.positions:
        assert res.equity.equity.iloc[-1] - broker.capital == pytest.approx(res.trades.net_pnl.sum(), abs=1.0)
    assert all(q % 65 == 0 for q in res.fills.qty)


def test_iron_fly_buys_wings_first_and_caps_the_loss():
    res, _, logs = backtest("iron_fly", vix_min=0, vix_max=0, vix_spike_pct=0, gap_pct=0, iv_rv_min=0)
    f = res.fills[res.fills.reason.str.startswith("Entry")]
    first = f.groupby("group").head(4)
    for _, g in first.groupby("group"):
        assert list(g.side) == ["BUY", "BUY", "SELL", "SELL"]
    for _, g in res.trades.groupby("group"):
        if len(g) != 4:
            continue
        strikes = {(r.side, r.contract.split()[-1]): int(r.contract.split()[-2]) for r in g.itertuples()}
        assert strikes[("LONG", "CE")] > strikes[("SHORT", "CE")] and strikes[("LONG", "PE")] < strikes[("SHORT", "PE")]
        width = max(strikes[("LONG", "CE")] - strikes[("SHORT", "CE")], strikes[("SHORT", "PE")] - strikes[("LONG", "PE")])
        assert g.net_pnl.sum() >= -width * 65 - 1000            # never worse than the wing width (plus charges)
    # exits buy back the shorts before selling the wings
    ex = res.fills[~res.fills.reason.str.startswith("Entry")]
    for _, g in ex.groupby(["group", "time"]):
        sides = list(g.side)
        assert sides == sorted(sides)                             # BUYs (closing shorts) before SELLs


def test_filters_skip_entries_when_vix_is_out_of_band():
    res, _, logs = backtest("iron_fly", df=sample_df(vix=35.0))
    assert len(res.trades) == 0
    assert any("VIX 35.0 above" in l for l in logs)
    res, _, logs = backtest("iron_fly", df=sample_df(vix=35.0), vix_max=0)
    assert len(res.cycles) > 0


def test_positional_strategies_stay_out_of_scheduled_events(monkeypatch):
    monkeypatch.setattr(events, "all_events", lambda: tuple((date(2026, 1, 1) + timedelta(days=i), "Test event")
                                                              for i in range(0, 400)))
    res, _, logs = backtest("iron_fly", vix_min=0, vix_max=0, gap_pct=0, iv_rv_min=0)
    assert len(res.trades) == 0 and any("scheduled event" in l for l in logs)
    res, _, _ = backtest("iron_fly", vix_min=0, vix_max=0, gap_pct=0, iv_rv_min=0, events="ignore")
    assert len(res.cycles) > 0


def test_event_iron_fly_enters_the_day_before_and_exits_on_the_event_day(monkeypatch):
    days = [date(2026, 3, 11), date(2026, 5, 13), date(2026, 7, 15)]   # Wednesdays in the sample period
    monkeypatch.setattr(events, "all_events", lambda: tuple((d, "Test policy") for d in days))
    res, _, logs = backtest("event_iron_fly", vix_max=0)
    assert len(res.cycles) == 3
    for c in res.cycles.itertuples():
        entry, exit_ = pd.Timestamp(c.entry_time), pd.Timestamp(c.exit_time)
        assert entry.date() + timedelta(days=1) in days and exit_.date() in days
        assert entry.strftime("%H:%M") == "14:45" or entry.time() > pd.Timestamp("14:45").time()


def test_double_calendar_sells_near_buys_far_and_leaves_before_near_expiry():
    res, _, _ = backtest("double_calendar", vix_min=0, vix_max=0, gap_pct=0)
    for _, g in res.trades.groupby("group"):
        exp = {r.side: pd.to_datetime(" ".join(r.contract.split()[1:4]), format="%d %b %y") for r in g.itertuples()}
        assert exp["LONG"] > exp["SHORT"]
        exit_day = pd.to_datetime(g.exit_time).max().normalize()
        assert exit_day < exp["SHORT"] or "Stop" in " ".join(g.reason) or "Target" in " ".join(g.reason)


def test_put_credit_spread_follows_the_trend_filter():
    res, _, logs = backtest("put_credit_spread", vix_min=0, vix_max=0, gap_pct=0, trend_days=20)
    below = {l[:10] for l in logs if "below its 20-day average" in l}
    entered = {str(pd.Timestamp(t))[:10] for t in res.cycles.entry_time}
    assert below and not (below & entered)
    assert all(t.contract.endswith("PE") for t in res.trades.itertuples())
    res2, _, _ = backtest("put_credit_spread", vix_min=0, vix_max=0, gap_pct=0, trend_days=0)
    assert len(res2.cycles) > len(res.cycles)


def test_intraday_strangle_is_flat_every_evening_and_trails_to_cost():
    res, broker, logs = backtest("intraday_strangle", vix_min=0, vix_max=0, gap_pct=0, vix_spike_pct=0)
    t = res.trades
    assert (pd.to_datetime(t.entry_time).dt.date == pd.to_datetime(t.exit_time).dt.date).all()
    assert (pd.to_datetime(t.exit_time).dt.strftime("%H:%M") <= "15:15").all()
    assert any("stop moved to its sold price" in l for l in logs)
    assert not broker.positions or pd.Timestamp(res.equity.index[-1]).strftime("%H:%M") < "15:15"


def test_bool_settings_accept_text():
    s = load_strategies()["intraday_strangle"](trail_to_cost="false")
    assert s.p.trail_to_cost is False
    assert load_strategies()["intraday_strangle"](trail_to_cost="1").p.trail_to_cost is True


def test_events_file_adds_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "ROOT", tmp_path)
    events.all_events.cache_clear()
    (tmp_path / "events.yaml").write_text("- {date: 2030-01-02, name: Test}\n")
    try:
        assert events.events_between(date(2030, 1, 1), date(2030, 1, 3)) == [(date(2030, 1, 2), "Test")]
        assert events.next_event(date(2026, 10, 8))[0] == date(2026, 12, 4)
    finally:
        events.all_events.cache_clear()


# ---- the dashboard's shared menu --------------------------------------------------------------
def _panel(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "algolab_panel_menu", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAGE = """<!doctype html><html><head><title>x</title></head><body><div class="wrap"><header><h1>Portfolio</h1>
<div class="status"><span class="dot" id="dot"></span><span id="status">Loading…</span> · <a href="/stock">Stock history</a> · <a href="/us">US stocks</a> · <a href="/paper">Paper trading</a> · <a href="/algo">Blaze Butterfly</a> · <a href="/backtests">Backtests</a> · <button class="link" id="pauseBtn" type="button">Pause sync</button> · <a href="/signout">Sign out</a></div>
</header><a href="/export.csv">Download</a></div></body></html>"""


def test_menu_replaces_the_old_links_on_every_page(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    app.add_url_rule("/", "home", lambda: PAGE)
    app.add_url_rule("/algo", "algo", lambda: PAGE.replace("Portfolio", "Blaze"))
    app.add_url_rule("/backtests", "backtests", lambda: PAGE.replace("Portfolio", "Backtests"))
    app.add_url_rule("/signin", "signin", lambda: "<html><head></head><body>sign in</body></html>")
    app.add_url_rule("/api/x", "api", lambda: flask.jsonify(a=1))
    c = app.test_client()
    home = c.get("/").get_data(as_text=True)
    status = home[home.index('<div class="status">'):]
    status = status[:status.index("</div>") + 6]
    assert 'href="/stock"' not in status and 'href="/signout"' not in status
    assert 'id="pauseBtn"' in status and status.count("·") == 1        # one separator left: status · Pause sync
    assert '<nav class="sitenav">' in home and '<a href="/" class=on>Portfolio</a>' in home
    assert 'href="/export.csv"' in home                                 # other links on the page stay
    old = c.get("/algo")                                                # the retired Blaze page
    assert old.status_code == 302 and old.headers["Location"] == "/paper"
    bt = c.get("/backtests").get_data(as_text=True)                     # Backtests sit under Algo Lab
    assert '<a href="/backtests/run" class=on>Algo Lab</a>' in bt and 'class="sn-sub"' in bt
    assert '<a href="/backtests" class=on>Results</a>' in bt and ">Backtests</a></div><a" not in bt
    top = re.search(r'<div class="sn-in">(.*?)</div>', bt, re.S).group(1)
    assert "Backtests" not in top and "Blaze" not in bt.split("</nav>")[0]
    assert "sitenav" not in c.get("/signin").get_data(as_text=True)
    assert c.get("/api/x").get_json() == {"a": 1}
    paper_page = c.get("/paper").get_data(as_text=True)
    assert paper_page.count('<nav class="sitenav">') == 1 and 'id="other"' not in paper_page
    assert mod.add_menu(home, "/") == home                              # only once
    new = c.get("/paper/new").get_data(as_text=True)
    assert '<a href="/paper/overview" class=on>Paper</a>' in new and '<a href="/paper/new" class=on>Start a session</a>' in new
    assert "__VIEW__" not in new and "__KIND__" not in new and 'VIEW = "new"' in new
    assert 'VIEW = "list"' in paper_page and 'id="summary"' in paper_page
    live_new = c.get("/live/new").get_data(as_text=True)
    assert 'KIND = "live"' in live_new and '<a href="/live/new" class=on>Start a session</a>' in live_new


def test_market_data_page_reads_the_stored_prices(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    data = tmp_path / "data"
    (data / "spot").mkdir(parents=True)
    (data / "spot" / "NIFTY_minute.csv").write_text(
        "datetime,open,high,low,close,vix\n2026-10-12 09:15:00,1,1,1,22500.5,14.2\n2026-10-12 09:20:00,1,1,1,22510,14.3\n"
        "2026-10-13 09:15:00,1,1,1,22600,14.1\n")
    exp = data / "options" / "NIFTY" / "2026-10-19"
    exp.mkdir(parents=True)
    (exp / "22600CE.csv").write_text("time,last,bid,ask\n2026-10-13 09:20:00,101.5,,\n2026-10-13 09:21:00,102,101.9,102.1\n")
    (exp / "22600PE.csv").write_text("time,last,bid,ask\n2026-10-13 09:20:00,95,,\n")
    (exp / "notes.txt").write_text("ignored")
    (data / "collect_log.txt").write_text("2026-10-13 15:45  NIFTY bars stored: 3; option contracts updated: 2\n")
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    page = c.get("/market-data").get_data(as_text=True)
    assert "Market data" in page and '<a href="/market-data" class=on>Data</a>' in page
    assert '<a href="/chart">Chart</a>' in page.split("</nav>")[0]          # Chart is a sub-page of Data
    s = c.get("/api/market-data/summary").get_json()
    assert s["spot"][0]["rows"] == 3 and s["spot"][0]["days"] == 2 and s["spot"][0]["close"] == "22600"
    e = s["expiries"][0]
    assert (e["expiry"], e["ce"], e["pe"], e["rows"], e["live_quotes"]) == ("2026-10-19", 1, 1, 3, 1)
    assert s["log"][0].startswith("2026-10-13 15:45")
    ch = c.get("/api/market-data/chain?expiry=2026-10-19").get_json()
    assert ch["strikes"][0]["CE"]["last"] == "102" and ch["spot"]["close"] == 22600
    se = c.get("/api/market-data/series?expiry=2026-10-19&strike=22600&right=CE").get_json()
    assert se["rows"] == 2 and se["points"][-1] == ["2026-10-13 09:21", 102.0, 101.9, 102.1]
    assert c.get("/api/market-data/spot?days=1").get_json()["points"][0][1] == 22600.0
    dl = c.get("/api/market-data/download?expiry=2026-10-19&strike=22600&right=PE")
    assert dl.status_code == 200 and b"95" in dl.data
    for bad in ("expiry=../../x&strike=1&right=CE", "expiry=2026-10-19&strike=a&right=CE", "expiry=2026-10-19&strike=1&right=XX"):
        assert c.get("/api/market-data/series?" + bad).status_code == 404
    assert c.get("/api/market-data/chain?expiry=..").status_code == 404
    assert c.get("/api/market-data/spot?dataset=../x").status_code == 404


def test_profile_theme_and_photo(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import base64
    mod = _panel(tmp_path, monkeypatch)
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(mod, "_profile_dir", lambda: tmp_path / "runtime")
    app = flask.Flask("t")
    app.secret_key = "x"
    app.register_blueprint(mod.panel)
    app.add_url_rule("/", "home", lambda: PAGE.replace("</head>", "<style>@media (prefers-color-scheme:dark){:root{--bg:#000}}</style></head>"))
    app.add_url_rule("/signin", "signin", lambda: "<html><head></head><body>x</body></html>")
    c = app.test_client()
    home = c.get("/").get_data(as_text=True)
    assert 'href="/profile"' in home and ">Profile<" in home and "@media (prefers-color-scheme:dark)" in home
    assert c.get("/profile").status_code == 200
    r = c.post("/api/profile", json={"name": "  Hamid   Sultan ", "theme": "dark", "accent": "violet", "start_page": "/paper",
                                     "email": "h@example.com"})
    assert r.status_code == 200 and r.get_json()["profile"]["name"] == "Hamid Sultan"
    assert c.post("/api/profile", json={"email": "nope"}).status_code == 400
    assert c.post("/api/profile", json={"theme": "neon", "accent": "x", "start_page": "/evil"}).get_json()["profile"]["theme"] == "dark"
    home = c.get("/").get_data(as_text=True)
    assert "@media all{:root{--bg:#000}}" in home and "#a08bff" in home and ">Hamid<" in home and ">HS<" in home
    assert "theme-css" in c.get("/signin").get_data(as_text=True)          # the sign-in page follows the theme too
    c.post("/api/profile", json={"theme": "light"})
    assert "@media not all{:root{--bg:#000}}" in c.get("/").get_data(as_text=True)
    # photo
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
    assert c.post("/api/profile/photo", json={"data": "data:image/png;base64," + base64.b64encode(b"GIF89a").decode()}).status_code == 400
    assert c.post("/api/profile/photo", json={"data": "data:image/png;base64," + base64.b64encode(png).decode()}).status_code == 200
    assert c.get("/profile/photo").data == png and '<img class="sn-av" src="/profile/photo?v=' in c.get("/").get_data(as_text=True)
    assert c.delete("/api/profile/photo").status_code == 200 and c.get("/profile/photo").status_code == 404
    assert not list((tmp_path / "runtime").glob("profile_photo.*"))
    # start page, once per sign-in
    with c.session_transaction() as s:
        s["user"] = "admin"
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/paper")
    assert c.get("/").status_code == 200


def test_paper_pnl_by_day_high_low_and_metrics_csv(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    d = tmp_path / "runtime" / "sessions" / "iron_fly"
    d.mkdir(parents=True)
    (d / "equity.csv").write_text("time,equity,spot\n"
        "2026-10-12 09:30:00,300000,1\n2026-10-12 11:00:00,301500,1\n2026-10-12 15:29:00,300800,1\n"
        "2026-10-13 09:30:00,299000,1\n2026-10-13 12:00:00,302400,1\n2026-10-13 15:29:00,302000,1\n")
    trades = [{"group": "001", "entry_time": "2026-10-12T09:30", "exit_time": "2026-10-13T12:00", "net_pnl": 2000.0,
               "costs": 150.0, "reason": "Target"}]
    import json as _j
    (d / "state.json").write_text(_j.dumps({"strategy": "iron_fly", "params": {"lots": 1}, "feed": "kite", "mode": "paper",
        "time": "2026-10-13T15:29", "spot": 1, "equity": 302000, "unrealized": 0, "positions": [], "logs": [],
        "broker": {"capital": 300000, "trades": trades, "positions": [], "total_costs": 150.0}}))
    an = mod._pnl_analysis(d / "equity.csv", 300000, trades)
    assert an["hi"] == ["2026-10-13 12:00", 2400.0] and an["lo"] == ["2026-10-13 09:30", -1000.0]
    assert [x["day"] for x in an["daily"]] == [800.0, 1200.0] and an["daily"][1]["cumulative"] == 2000.0
    assert an["daily"][0]["high"] == 1500.0 and an["daily"][1]["low"] == -1000.0 and an["daily"][1]["trades"] == 1
    m = an["metrics"]
    assert m["max_drawdown"] == -2500.0 and m["drawdown_from"] == "2026-10-12 11:00" and m["drawdown_to"] == "2026-10-13 09:30"
    assert (m["winning_days"], m["best_day"], m["worst_day"], m["cycle_win_rate_pct"]) == (2, 1200.0, 800.0, 100.0)
    assert {tuple(p) for p in an["points"]} >= {("2026-10-13 12:00", 2400.0), ("2026-10-13 09:30", -1000.0)}
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    v = c.get("/api/paper").get_json()["sessions"][0]
    assert v["pnl"]["metrics"]["max_drawdown"] == -2500.0 and len(v["pnl"]["daily"]) == 2
    one = c.get("/api/paper/metrics.csv?id=iron_fly")
    text = one.get_data(as_text=True)
    assert one.status_code == 200 and "attachment" in one.headers["Content-Disposition"]
    assert "Max drawdown (Rs),-2500.0" in text and "2026-10-13,1200.0,2000.0" in text
    allcsv = c.get("/api/paper/metrics.csv?kind=paper").get_data(as_text=True)
    assert allcsv.splitlines()[1].startswith("iron_fly,iron_fly,paper")
    assert c.get("/api/paper/metrics.csv?id=nope").status_code == 404


def test_menu_adds_accessibility_basics(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    page = '<html><head></head><body><div class="wrap"><p>x</p></div></body></html>'
    out = mod.add_menu(page, "/")
    assert '<div class="wrap" role="main">' in out                 # a main landmark for screen readers
    assert "prefers-reduced-motion" in out and ":focus-visible" in out
    assert "summary{min-height:32px}" in out


def test_time_zone_choice_on_every_page(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    page = '<html><head><title>x</title></head><body><div class="wrap"></div></body></html>'
    ist = mod.apply_theme(page, {**mod.DEFAULT_PROFILE})
    local = mod.apply_theme(page, {**mod.DEFAULT_PROFILE, "time_zone": "local"})
    assert 'id="tz-js"' in ist and "var LOCAL = false" in ist and "var LOCAL = true" in local
    assert ist.index('id="tz-js"') < ist.index("<title>")              # before any page script
    assert mod.DEFAULT_PROFILE["time_zone"] == "ist" and mod.TIME_ZONES == ("ist", "local")
    assert 'id="tz"' in mod.PROFILE_PAGE and "time_zone: $(\"tz\").value" in mod.PROFILE_PAGE
