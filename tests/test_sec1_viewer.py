"""Data page › 1-second bars viewer: the instruments of a day and candles of any length from one stored file."""
import pytest

from test_defined_risk import _panel


@pytest.fixture
def app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    day = tmp_path / "data" / "sec1" / "2026-10-27"
    day.mkdir(parents=True)
    head = "time,open,high,low,close,bid,ask,volume,oi,ticks,day_open,day_high"
    (day / "NIFTY.csv").write_text(head + "\n" +
        "2026-10-27 09:15:00,25000,25002,24998,25001,,,,,2,,\n"
        "2026-10-27 09:15:04,25001,25010,25000,25005,,,,,3,,\n"
        "2026-10-27 09:16:01,25005,25020,25004,25018,,,,,1,,\n")
    (day / "2026-10-27_25000CE.csv").write_text(head + "\n" +
        "2026-10-27 09:15:02,80,80,80,80,79.5,80.5,100,500,1,,\n"
        "2026-10-27 09:15:03,82,83,81,81.5,81.2,81.9,150,520,2,,\n")
    (day / "2026-10-27_25050PE.csv").write_text(head + "\n" +
        "2026-10-27 09:15:05,40,40,40,40,39.5,40.5,10,90,1,,\n")
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return a.test_client()


def test_lists_index_and_options_of_the_day(app):
    j = app.get("/api/market-data/sec1/instruments?day=2026-10-27").get_json()
    names = {i["name"]: i for i in j["instruments"]}
    assert set(names) == {"NIFTY", "2026-10-27_25000CE", "2026-10-27_25050PE"}
    assert names["NIFTY"]["kind"] == "index" and names["NIFTY"]["rows"] == 3
    ce = names["2026-10-27_25000CE"]
    assert (ce["strike"], ce["right"], ce["expiry"], ce["rows"]) == (25000, "CE", "2026-10-27", 2)


def test_candles_of_one_minute_hold_high_low_last_bid_ask_and_ticks(app):
    j = app.get("/api/market-data/sec1/candles?day=2026-10-27&name=NIFTY&every=60").get_json()
    assert j["total"] == 2 and [c[0] for c in j["candles"]] == ["09:15:00", "09:16:00"]
    first = j["candles"][0]          # open, high, low, close, bid, ask, volume, oi, ticks
    assert first[1:5] == [25000.0, 25010.0, 24998.0, 25005.0] and first[9] == 5
    j = app.get("/api/market-data/sec1/candles?day=2026-10-27&name=2026-10-27_25000CE&every=1").get_json()
    assert j["total"] == 2 and j["candles"][1][5] == 81.2 and j["candles"][1][6] == 81.9   # last bid / ask


def test_bad_input_is_refused(app):
    assert app.get("/api/market-data/sec1/candles?day=2026-10-27&name=NIFTY&every=7").status_code == 400
    assert app.get("/api/market-data/sec1/candles?day=../../x&name=NIFTY&every=60").status_code == 400
    assert app.get("/api/market-data/sec1/candles?day=2026-10-27&name=../secret&every=60").status_code == 400
    assert app.get("/api/market-data/sec1/instruments?day=2026-10-26").status_code == 404


def test_days_show_coverage_and_gaps(app, tmp_path):
    import importlib
    day = tmp_path / "data" / "sec1" / "2026-10-28"
    day.mkdir(parents=True)
    rows = ["time,open,high,low,close,bid,ask,volume,oi,ticks"]
    for m in list(range(0, 235)) + list(range(275, 375)):            # no ticks 13:10-13:50
        t = 9 * 3600 + 15 * 60 + m * 60
        rows.append(f"2026-10-28 {t // 3600:02d}:{t % 3600 // 60:02d}:05,1,1,1,24000,,,,,1")
    (day / "NIFTY.csv").write_text("\n".join(rows) + "\n")
    (day / "2026-11-03_24000CE.csv").write_text(rows[0] + "\n2026-10-28 09:15:01,90,90,90,90,89,91,1,1,1\n")
    (day / "2026-11-03_24050PE.csv").write_text(rows[0] + "\n2026-10-28 09:15:01,90,90,90,90,89,91,1,1,1\n")
    sess = tmp_path / "runtime" / "sessions" / "s1"
    sess.mkdir(parents=True)
    (sess / "state.json").write_text('{"broker": {"fills": [{"time": "2026-10-28T09:20:00", "contract": "NIFTY 03 Nov 26 24050 PE"}]}}')
    c = app
    j = c.get("/api/market-data/summary").get_json()
    d = next(x for x in j["sec1"]["days"] if x["day"] == "2026-10-28")
    assert d["coverage"] == round(335 / 375 * 100, 1) and d["gaps"] == [["13:10", "13:50"]]
    inst = c.get("/api/market-data/sec1/instruments?day=2026-10-28").get_json()
    by = {i["name"]: i for i in inst["instruments"]}
    assert by["2026-11-03_24050PE"]["in_use"] and not by["2026-11-03_24000CE"]["in_use"]
    assert inst["atm"] == 24000 and by["NIFTY"]["coverage"] == d["coverage"]


def test_candles_of_30_minutes_and_a_time_window(app):
    j = app.get("/api/market-data/sec1/candles?day=2026-10-27&name=NIFTY&every=1800").get_json()
    assert j["total"] == 1 and j["candles"][0][0] == "09:00:00"
    j = app.get("/api/market-data/sec1/candles?day=2026-10-27&name=NIFTY&every=1&from=09:15:03&to=09:16:00").get_json()
    assert [c[0] for c in j["candles"]] == ["09:15:04"]

