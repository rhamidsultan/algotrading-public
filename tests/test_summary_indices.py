"""Summary page: NIFTY, BANK NIFTY and SENSEX with their day change; Include US switch on the page."""
import sys

import pytest

from test_defined_risk import _panel
from test_platform import _FakeKite


def test_summary_indices(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    monkeypatch.setattr(sys.modules["__main__"], "kite", None, raising=False)
    assert c.get("/api/summary").get_json()["indices"] is None          # no Zerodha login: the card falls back
    monkeypatch.setattr(sys.modules["__main__"], "kite", _FakeKite(), raising=False)
    mod._CACHE.clear()
    idx = {i["name"]: i for i in c.get("/api/summary").get_json()["indices"]}
    assert list(idx) == ["NIFTY", "BANK NIFTY", "SENSEX"]
    assert (idx["NIFTY"]["change"], idx["NIFTY"]["pct"]) == (100.0, 0.4) and idx["SENSEX"]["change"] == -100.0
    page = c.get("/summary").get_data(as_text=True)
    assert 'id="incUS"' in page and "summary.us" in page and "India only" in page
