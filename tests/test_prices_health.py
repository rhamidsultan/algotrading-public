"""27 Nov 2026: Summary › Prices said "No new price for 277778 h" with prices flowing. The check read the engine's
feed_status.json (JSON) as a bare timestamp, failed, and printed its "never" value (1e9 s) as hours. Now: the age
from the file's own time; a warning only when a RUNNING session uses Zerodha prices; no sentinel ever shown."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from test_defined_risk import _panel

IST = timezone(timedelta(hours=5, minutes=30))


def _env(tmp_path, monkeypatch, feed_age=None, running_kite=True):
    mod = _panel(tmp_path, monkeypatch)
    if feed_age is not None:
        t = datetime.now(IST) - timedelta(seconds=feed_age)
        mod.RT.mkdir(parents=True, exist_ok=True)
        (mod.RT / "feed_status.json").write_text(json.dumps({"time": t.isoformat(timespec="seconds"), "source": "stream"}))
    d = mod.SESS / "is-1"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"strategy": "intraday_strangle", "mode": "live"}))
    if running_kite:
        (d / "desired.json").write_text(json.dumps({"strategy": "intraday_strangle", "feed": "kite", "mode": "live"}))
        (d / "heartbeat").write_text(str(time.time()))
    return mod


def test_a_fresh_price_for_a_running_session(tmp_path, monkeypatch):
    f = _env(tmp_path, monkeypatch, feed_age=3)._feed_health()
    assert f["needs"] == ["is-1"] and f["age"] <= 5 and f["source"] == "stream"


def test_a_stale_price_for_a_running_session(tmp_path, monkeypatch):
    f = _env(tmp_path, monkeypatch, feed_age=600)._feed_health()
    assert f["needs"] == ["is-1"] and 590 <= f["age"] <= 610      # a real age, not 1e9


def test_no_running_session_needs_prices(tmp_path, monkeypatch):
    f = _env(tmp_path, monkeypatch, feed_age=600, running_kite=False)._feed_health()
    assert f["needs"] == []


def test_no_price_saved_yet(tmp_path, monkeypatch):
    f = _env(tmp_path, monkeypatch, feed_age=None)._feed_health()
    assert f["age"] is None and f["needs"] == ["is-1"]


def test_the_page_never_shows_the_sentinel(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _env(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.register_blueprint(mod.panel)
    page = a.test_client().get("/summary").get_data(as_text=True)
    assert "No session needs Zerodha prices right now." in page and "No price yet today for" in page
    assert "or no session uses Zerodha prices" not in page
