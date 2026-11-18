"""Puts the dashboard's extra pages back into the portfolio app, without replacing anything else.

Adds, only where missing:
  - the Algo Lab "Paper trading" page (straddle) and its links, exactly as Algo Lab's installer does
  - the Blaze Butterfly paper page (/algo) and its link
Safe to run more than once. Usage: python3 merge_pages.py /opt/trader
"""
import sys
from pathlib import Path

d = Path(sys.argv[1])
HERE = Path(__file__).resolve().parent


def rewrite(p, s):
    with p.open("w") as f:  # in place: keeps owner and permissions
        f.write(s)


def fail(msg):
    sys.exit("Unexpected file layout: " + msg)


# ---- app.py
app = d / "app.py"
s = app.read_text()
if "# >>> algolab page" not in s:
    anchor = '\nif __name__ == "__main__":'
    if s.count(anchor) != 1:
        fail("app.py main block")
    block = ('# >>> algolab page (paper trading; added by algo-trading-platform/deploy/install_server.sh)\n'
             'try:\n'
             '    from algolab_panel import panel as _algolab_panel\n'
             '    app.register_blueprint(_algolab_panel)\n'
             'except Exception as _e:  # the add-on must never stop the portfolio app\n'
             '    print("Paper trading page not loaded:", _e, flush=True)\n'
             '# <<< algolab page\n\n')
    s = s.replace(anchor, "\n" + block + anchor.lstrip("\n"), 1)
    print("  app.py: Paper trading (straddle) page added back")
if 'PAPER_DB = BASE / "data" / "paper.db"' not in s:
    routes = (HERE / "blaze_routes.txt").read_text()
    anchor = "# ---------------------------------------------------------------- startup\ndef lan_ip():"
    if s.count(anchor) == 1:
        s = s.replace(anchor, routes + anchor, 1)
    elif s.count("\ndef lan_ip():") == 1:
        s = s.replace("\ndef lan_ip():", "\n" + routes + "def lan_ip():", 1)
    else:
        fail("app.py startup section")
    print("  app.py: Blaze Butterfly page added")
rewrite(app, s)

# ---- page links
dash = d / "templates" / "dashboard.html"
t = dash.read_text()
t = t.replace(' · <a href="/algo">Algo (paper)</a>', "")  # the link from today's earlier install
if 'href="/paper"' not in t:
    old = '<a href="/stock">Stock history</a>'
    if t.count(old) != 1:
        fail("dashboard.html links")
    t = t.replace(old, old + ' · <a href="/paper">Paper trading</a>', 1)
    print("  dashboard.html: Paper trading link added back")
if 'href="/algo"' not in t:
    old = '<a href="/paper">Paper trading</a>'
    if t.count(old) != 1:
        fail("dashboard.html paper link")
    t = t.replace(old, old + ' · <a href="/algo">Blaze Butterfly</a>', 1)
    print("  dashboard.html: Blaze Butterfly link added")
rewrite(dash, t)

stock = d / "templates" / "stock.html"
if stock.exists():
    t = stock.read_text()
    old = '<a href="/">Portfolio</a>'
    if 'href="/paper"' not in t and t.count(old) == 1:
        rewrite(stock, t.replace(old, old + ' · <a href="/paper">Paper trading</a>', 1))
        print("  stock.html: Paper trading link added back")
print("ok")
