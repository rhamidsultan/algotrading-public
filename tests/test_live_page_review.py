"""Live page best practice (18 Nov 2026 review): money-moving buttons carry words on LIVE cards, a stopped LIVE
session that still holds legs is flagged (card banner, STOPPED on the overview) and cannot be deleted or reset
there, the start form lists the strategies that can trade live first and picks one, the P&L toggle is a real
control and Stop all LIVE is a full-size button."""
import pytest

from test_defined_risk import _panel


def test_the_live_page_carries_the_review_fixes(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.register_blueprint(mod.panel)
    page = a.test_client().get("/live").get_data(as_text=True)
    for needle in ('"Close all & stop"', '"Resume")', '"Sync")', "Stopped with ${s.positions.length} open leg",
                   'class="badge idle"', "(live && held ? \"\" : ibtn(\"delete\"", '["Paper only", ""]',
                   "const r = cat.find(c => c.live_ready)", ".pnlseg button{", "#stopAllLive{min-height:38px"):
        assert needle in page, needle
