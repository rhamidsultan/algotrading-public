"""No Zerodha login is asked for on weekends or NSE holidays (from the engine's config.yaml)."""
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def app(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    (tmp_path / "config.yaml").write_text('market:\n  holidays:   # NSE\n    - "2026-10-20"\n    - "2026-11-10"\naccount:\n  capital: 1\n')
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    sys.path.insert(0, str(ROOT / "server" / "portfolio"))
    import importlib
    import app as mod
    importlib.reload(mod)
    return mod


def test_trading_days_skip_weekends_and_holidays(app):
    assert app.nse_holidays() == {"2026-10-20", "2026-11-10"}
    assert app.trading_day(date(2026, 10, 19)) and not app.trading_day(date(2026, 10, 20))   # Mon yes, Tue holiday
    assert not app.trading_day(date(2026, 10, 18))                                           # Sunday
    assert app.login_due(datetime(2026, 10, 19, 8, 50, tzinfo=IST))
    assert not app.login_due(datetime(2026, 10, 20, 8, 50, tzinfo=IST))
    assert not app.login_due(datetime(2026, 10, 17, 10, 0, tzinfo=IST))                     # Saturday
    assert not app.login_due(datetime(2026, 10, 19, 18, 0, tzinfo=IST))                     # after the recording
    assert not app.market_open(datetime(2026, 10, 20, 10, 0, tzinfo=IST))


def test_pages_ask_only_when_due():
    html = (ROOT / "server/portfolio/templates/dashboard.html").read_text()
    assert "$('loginBox').classList.toggle('hide', !due);" in html and "due ? ['key', 'Zerodha login needed']" in html
    panel = (ROOT / "deploy/portfolio_page/algolab_panel.py").read_text()
    assert '"login_due": _login_due(main, state)' in panel and 'm.login_due === false ? ""' in panel
    pwa = (ROOT / "deploy/portfolio_page/pwa_panel.py").read_text()
    assert "main.trading_day(now.date())" in pwa
