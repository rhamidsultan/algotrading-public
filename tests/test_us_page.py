"""The US page template in the Overview style: no links in the status line, rows instead of a table."""
import re
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parent.parent / "server" / "portfolio" / "templates"


def test_us_page_overview_style():
    jinja2 = pytest.importorskip("jinja2")
    html = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL))).get_template("us.html").render()
    status = re.search(r'<div class="status">(.*?)</div>', html, re.S).group(1)
    assert "href=" not in status
    for el in ('id="cUSD"', 'id="cINR"', 'id="vDay"', 'id="vTot"', 'id="chart"', "const ICONS", "/api/us", "ibkr-login"):
        assert el in html, el
    assert "<table" not in html and " sh<" not in html   # quantities say "shares" so hidden-amount logins mask them
