"""Backtests › Compare with paper: the page, its job, the report and the days-of-1-second-bars setting."""
import json

import pytest

from test_defined_risk import _panel


@pytest.fixture
def app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    rt = tmp_path / "runtime"
    for sid, feed, mode in (("strangle", "kite", "paper"), ("live1", "kite", "live"), ("simmy", "sim", "paper")):
        d = rt / "sessions" / sid
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"strategy": "premium_strangle", "feed": feed, "mode": mode, "label": sid}))
    day = tmp_path / "data" / "sec1" / "2026-10-27"
    day.mkdir(parents=True)
    (day / "NIFTY.csv").write_text("time\n" + "x" * 2_000_000)
    mod = _panel(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_market_hours_now", lambda: False)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return a.test_client(), mod, rt


def test_page_lists_zerodha_sessions_and_1_second_days(app):
    c, mod, _ = app
    j = c.get("/api/compare-period").get_json()
    assert [s["id"] for s in j["sessions"]] == ["live1", "strangle"]          # the simulated session is left out
    assert j["sec1"]["days"] == ["2026-10-27"] and j["sec1"]["keep"] == 5 and j["market_hours"] is False
    page = c.get("/backtests/compare").get_data(as_text=True)
    assert "Compare with paper" in page and "/api/compare-period" in page
    assert ("Compare with paper", "/backtests/compare") in mod.SUBNAV["/backtests/run"]


def test_run_queues_a_job_after_the_close_only(app, monkeypatch):
    c, mod, rt = app
    body = {"from": "2026-10-27", "to": "2026-10-28", "bars": ["1m", "1s"], "sessions": ["strangle"]}
    r = c.post("/api/compare-period", json=body)
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    q = json.loads((rt / "backtest-queue" / f"{job['id']}.json").read_text())
    assert q["kind"] == "compare_period" and q["bars"] == ["1m", "1s"] and q["sessions"] == ["strangle"]
    for bad in ({"to": "2026-10-26"}, {"bars": []}, {"sessions": ["nope"]}, {"from": "27-10-2026"}):
        assert c.post("/api/compare-period", json={**body, **bad}).status_code == 400
    monkeypatch.setattr(mod, "_market_hours_now", lambda: True)
    assert c.post("/api/compare-period", json=body).status_code == 409


def test_report_and_csv(app):
    c, mod, rt = app
    (rt / "compare").mkdir()
    rep = {"from": "2026-10-27", "to": "2026-10-27", "bars": ["1m"], "flagged": 1, "totals": {"paper": {}, "live": {}},
           "sessions": [{"session": "strangle", "strategy": "premium_strangle", "mode": "paper", "labels": {}, "bars": {},
                         "days": [{"day": "2026-10-27", "actual": 1000, "bt_1m": 1700, "gap_1m": 700, "flag": True, "notes": ["late start 12:19"]}]}]}
    (rt / "compare" / "20261027-160000-abcd.json").write_text(json.dumps(rep))
    assert c.get("/api/compare-period/report/20261027-160000-abcd").get_json()["flagged"] == 1
    csv = c.get("/api/compare-period/report/20261027-160000-abcd?csv=1").get_data(as_text=True).splitlines()
    assert csv[1] == "strangle,premium_strangle,paper,2026-10-27,1000,1700,700,,,yes,late start 12:19"
    assert c.get("/api/compare-period/report/..%2fx").status_code == 404


def test_days_kept_setting(app, monkeypatch):
    c, mod, rt = app
    assert c.post("/api/compare-period/keep", json={"keep_trading_days": 0}).status_code == 400
    assert c.post("/api/compare-period/keep", json={"keep_trading_days": 31}).status_code == 400
    r = c.post("/api/compare-period/keep", json={"keep_trading_days": 8})
    assert r.status_code == 200 and json.loads((rt / "sec1_settings.json").read_text()) == {"keep_trading_days": 8}
    assert c.get("/api/compare-period").get_json()["sec1"]["keep"] == 8
    from algolab import sec1
    monkeypatch.setattr(sec1, "OVERRIDE", rt / "sec1_settings.json")
    assert sec1.settings({"sec1": {"keep_trading_days": 5}})["keep_trading_days"] == 8   # the recorder's pruning follows it


def test_permissions():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("up", Path(__file__).resolve().parents[1] / "deploy" / "portfolio_page" / "users_panel.py")
    up = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(up)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"users panel needs the dashboard: {e}")
    assert up.classify("GET", "/backtests/compare")[1] == ("backtests",)
    assert up.classify("POST", "/api/compare-period")[1] == ("backtests",)
    assert up.classify("POST", "/api/compare-period/keep")[0] == "owner"
    assert up.classify("POST", "/api/compare-period/20261027-213000-abcd/delete")[1] == ("backtests",)
