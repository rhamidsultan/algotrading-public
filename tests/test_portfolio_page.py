"""The Portfolio (India) page template: the controls other parts of the dashboard rely on are still there."""
import re
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parent.parent / "server" / "portfolio" / "templates"


def _page():
    jinja2 = pytest.importorskip("jinja2")
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL))).get_template("dashboard.html").render(demo=False)


def test_portfolio_page_keeps_the_controls_other_pages_use():
    html = _page()
    # users_panel hides these for other logins; the menu cleans the status line
    for el in ('id="pauseBtn"', 'id="pauseBox"', 'id="testBtn"', 'id="loginBox"', 'href="/export.csv"', 'class="status"',
               'action="/login/paste"', 'href="/login"'):
        assert el in html, el
    status = re.search(r'<div class="status">(.*?)</div>', html, re.S).group(1)
    assert "href=" not in status                       # no links left for the menu to remove


def test_portfolio_page_overview_style():
    html = _page()
    assert "Send test alert" not in re.sub(r"<script>.*?</script>", "", html, flags=re.S)  # an icon inside folded Alerts
    assert '<details class="card sec" id="alertSec">' in html
    for el in ('id="day"', 'id="vDay"', 'id="vTot"', 'const ICONS', "localStorage.setItem('pf.view'"):
        assert el in html, el
    assert "DEMO" in _page_demo()


def _page_demo():
    jinja2 = pytest.importorskip("jinja2")
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL))).get_template("dashboard.html").render(demo=True)


def test_holdings_folded_with_up_down_today_and_total():
    html = _page()
    assert '<details class="card sec" id="holdSec">' in html and "'pf.holdings'" in html   # folded, remembered per device
    assert "updown('day_pct', d.day_of ? DAY.split(' ')[0] : 'today') + updown('pnl', 'total')" in html
    assert 'id="dayNote"' in html and 'id="dayLbl"' in html               # closed market: last trading day, with a note


def test_stock_trades_filter_by_buys_or_sells():
    jinja2 = pytest.importorskip("jinja2")
    html = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL))).get_template("stock.html").render(message="")
    assert 'data-side="${sd}"' in html and "every.filter(t => t.side === side)" in html
