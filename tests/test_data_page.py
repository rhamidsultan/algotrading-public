"""Data page: coverage calendar, several underlyings, disk use, fast reads of big files."""
import pytest

from test_defined_risk import _panel


def _setup(tmp_path):
    data = tmp_path / "data"
    (data / "spot").mkdir(parents=True)
    (data / "spot" / "NIFTY_minute.csv").write_text(
        "datetime,open,high,low,close,vix\n" + "".join(f"2026-10-12 09:{15 + i}:00,1,1,1,{22500 + i},14\n" for i in range(4))
        + "2026-10-13 09:15:00,1,1,1,22600,14.1\n")
    (data / "spot" / "BANKNIFTY_minute.csv").write_text("datetime,open,high,low,close\n2026-10-13 09:15:00,1,1,1,51000,\n")
    n = data / "options" / "NIFTY" / "2026-10-20"
    n.mkdir(parents=True)
    (n / "22600CE.csv").write_text("time,last,bid,ask\n2026-10-12 09:20:00,101.5,,\n2026-10-14 09:21:00,102,,\n")
    (n / "22600PE.csv").write_text("time,last,bid,ask\n2026-10-13 09:20:00,95,,\n")
    b = data / "options" / "BANKNIFTY" / "2026-10-28"
    b.mkdir(parents=True)
    (b / "51000CE.csv").write_text("time,last,bid,ask\n2026-10-13 09:20:00,400,,\n")
    return data


def test_data_page_coverage_and_underlyings(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    _setup(tmp_path)
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    s = c.get("/api/market-data/summary?u=BANKNIFTY").get_json()
    assert s["underlyings"] == ["NIFTY", "BANKNIFTY"] and s["expiries"][0]["expiry"] == "2026-10-28"
    assert {x["name"]: x["underlying"] for x in s["spot"]} == {"NIFTY_minute": "NIFTY", "BANKNIFTY_minute": "BANKNIFTY"}
    cov = {d: (bars, contracts) for d, bars, contracts in c.get("/api/market-data/coverage?u=NIFTY").get_json()["days"]}
    assert cov["2026-10-12"] == (4, 1) and cov["2026-10-13"] == (1, 2) and cov["2026-10-14"] == (0, 1)
    assert c.get("/api/market-data/coverage?u=XYZ").status_code == 404
    sp = c.get("/api/market-data/spot?u=BANKNIFTY&days=1").get_json()
    assert sp["dataset"] == "BANKNIFTY_minute" and sp["points"][0][1] == 51000.0
    daily = c.get("/api/market-data/spot?u=NIFTY&days=30").get_json()
    assert daily["daily"] and daily["points"] == [["2026-10-12", 22503.0, 14.0], ["2026-10-13", 22600.0, 14.1]]
    ch = c.get("/api/market-data/chain?u=BANKNIFTY&expiry=2026-10-28").get_json()
    assert ch["strikes"][0]["CE"]["last"] == "400" and ch["spot"]["close"] == 51000
    assert c.get("/api/market-data/series?u=BANKNIFTY&expiry=2026-10-28&strike=51000&right=CE").get_json()["rows"] == 1
    disk = c.get("/api/market-data/disk").get_json()
    assert {p["name"] for p in disk["parts"]} >= {"Index bars", "Options NIFTY", "Options BANKNIFTY"}


def test_big_files_are_not_read_through(tmp_path, monkeypatch):
    _setup(tmp_path)
    mod = _panel(tmp_path, monkeypatch)
    big = tmp_path / "big.csv"
    line = "2026-10-13 09:20:00,101.50,101.40,101.60\n"
    big.write_text("time,last,bid,ask\n" + line * 40000)            # about 1.6 MB
    header, first, last, rows = mod._edges(big)
    assert isinstance(rows, float) and abs(rows - 40000) < 40       # estimated from the size
    assert last.startswith("2026-10-13") and header == "time,last,bid,ask"


def test_data_page_layout_follow_ups():
    import importlib.util, os
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    page = src[src.index('DATA_PAGE = r"""'):src.index('# ---- Summary: everything important on one page')]
    assert '<div class="card strip">' in page and '<div class="cards">' not in page          # one slim totals card
    assert '<details class="card fold" id="expFold">' in page                                  # expiries folded by default
    assert 'id="spotChart"' not in page and 'href="/chart#stored"' in page                        # index + VIX chart moved to Chart
    assert 'id="seriesPeriod"' in page                                                         # the contract's period in its heading
    assert "Math.hypot(e.element.x - ev.x" in page                                             # a tap on the put line opens the put



def test_index_and_vix_chart_on_the_chart_page():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    page = src[src.index('CHART_PAGE = r"""'):]
    page = page[:page.index('"""', 20)]
    assert 'id="stored"' in page and "/api/market-data/spot?u=" in page and 'id="stCv"' in page
