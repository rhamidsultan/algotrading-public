"""Session card and session list call the P&L of open legs "Unrealized" (was "Open")."""
from test_defined_risk import _panel


def test_open_legs_pnl_is_called_unrealized(tmp_path, monkeypatch):
    page = open(_panel(tmp_path, monkeypatch).__file__, encoding="utf-8").read()
    assert 'num("Unrealized"' in page and '<span class="n">Unrealized</span><span class="n">Today</span>' in page
    assert 'num("Open"' not in page
