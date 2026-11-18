"""The Stocks page template: what the users panel and the hidden-amounts mask rely on is still there."""
import re
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parent.parent / "server" / "portfolio" / "templates"


def test_stock_page_overview_style():
    jinja2 = pytest.importorskip("jinja2")
    html = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL))).get_template("stock.html").render(message="Imported 3 trades")
    assert "Imported 3 trades" in html
    # the import form sits in its own card (hidden for logins that may not use the portfolio)
    assert re.search(r'<details class="card sec" id="impBox">.*?action="/stock/import"', html, re.S)
    status = re.search(r'<div class="status">(.*?)</div>', html, re.S).group(1)
    assert "href=" not in status
    for el in ('id="sym"', 'id="cMain"', 'id="views"', 'id="ranges"', 'id="qty"', "const ICONS", "/api/stock?symbol="):
        assert el in html, el
    assert "<table" not in html                     # trades are two-line rows that fit a phone
