"""Paper / Live pages: Sessions (strategy buttons and one strategy's details) and Overview (totals, list,
payoffs, calendar for all sessions)."""
import pytest

from test_defined_risk import _panel


def test_overview_pages_and_menu(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    for path, kind in (("/paper/overview", "paper"), ("/live/overview", "live")):
        page = c.get(path).get_data(as_text=True)
        assert f'KIND = "{kind}"' in page and 'VIEW = "overview"' in page
        assert 'id="payGrid"' in page and 'id="calAll"' in page and 'id="summary"' in page
    sessions = c.get("/paper").get_data(as_text=True)
    assert 'VIEW = "list"' in sessions and 'id="chips"' in sessions and "function renderChips" in sessions
    assert '<a href="/paper/overview">Overview</a>' in sessions                 # in the Paper sub-menu
    assert '<a href="/paper/overview" class=on>Overview</a>' in c.get("/paper/overview").get_data(as_text=True)
    assert "/paper/overview" in mod._NAV_PATHS and "/live/overview" in mod._NAV_PATHS


def test_overview_is_compact_with_icons(tmp_path, monkeypatch):
    """Overview rows: state and price feed as icons, open legs as bars, no time on each row (one time in the header)."""
    flask = pytest.importorskip("flask")
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    page = app.test_client().get("/paper/overview").get_data(as_text=True)
    assert 'class="slist" id="summary"' in page and "<table class=\"summary\"" not in page   # a list that fits a phone
    assert 'aria-label="Download metrics (CSV)"' in page and 'aria-label="Start a session"' in page
    summary = page[page.index("function renderSummary"):page.index("function renderChips")]
    assert "feedIcon(i.feed)" in summary and "pips(i.legs)" in summary
    assert "when(i.time)" not in summary and "stateText)}${" not in summary           # no "Running · Zerodha live prices · time"
    assert "clock(latestTime())" in page                                             # the time, once, at the top


def test_session_card_is_compact(tmp_path, monkeypatch):
    """The session card uses icon buttons and two-line rows (no tables that run off a phone screen)."""
    flask = pytest.importorskip("flask")
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    page = mod.PAGE
    assert "function posRow" in page and "function cycRow" in page and 'lb("kill", "squareoff"' in page
    assert "<th>Sold / bought at</th>" not in page            # the wide positions table is gone
