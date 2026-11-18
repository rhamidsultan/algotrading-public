"""Paper / Live › Deploy: a paper or shadow session's settings to a new Shadow or LIVE session, with warnings."""
import json

import pytest

from test_defined_risk import _panel


@pytest.fixture
def app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    rt = tmp_path / "runtime"
    (rt / "sessions" / "trend-1").mkdir(parents=True)
    (rt / "strategies.json").write_text(json.dumps([{"name": "straddle_trend_switch", "title": "Straddle grid", "live_ready": True,
        "params": [{"key": "lots", "label": "Lots", "default": 2, "type": "int"}, {"key": "trend_lots", "label": "Trend lots", "default": 4, "type": "int"}]}]))
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return a.test_client(), rt


def test_the_pages_carry_the_deploy_flow(app):
    c, _ = app
    page = c.get("/live").get_data(as_text=True)
    for needle in ('data-act="deploy"', "deployInit", "deployWarn", "allow_over_max_lots", "Orders larger than max lots"):
        assert needle in page
    assert "/live/new?copy=" in page and "&deploy=1" in page


def test_a_deployed_session_starts_as_its_own_session_and_the_source_keeps_running(app):
    c, rt = app
    (rt / "sessions" / "trend-1" / "desired.json").write_text(json.dumps({"strategy": "straddle_trend_switch", "feed": "kite", "mode": "shadow"}))
    body = {"strategy": "straddle_trend_switch", "feed": "kite", "params": {"lots": 1, "trend_lots": 6}, "mode": "live", "confirm": "LIVE",
            "label": "Long options · live", "live": {"max_lots": "3", "daily_loss_limit": "5000", "max_orders_per_day": "40",
                                                     "allow_over_max_lots": True}}
    r = c.post("/api/paper/start", json=body)
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["id"]
    want = json.loads((rt / "sessions" / sid / "desired.json").read_text())
    assert sid != "trend-1" and want["mode"] == "live" and want["params"]["trend_lots"] == 6 and want["live"]["max_lots"] == 3
    assert (rt / "sessions" / "trend-1" / "desired.json").exists()                      # the shadow session keeps running
