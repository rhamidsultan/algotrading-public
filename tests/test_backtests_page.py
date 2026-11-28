"""The Backtests page template in the Overview style."""
import re
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parent.parent / "server" / "portfolio" / "templates"


def test_backtests_page_overview_style():
    jinja2 = pytest.importorskip("jinja2")
    html = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TPL))).get_template("backtests.html").render()
    status = re.search(r'<div class="status">(.*?)</div>', html, re.S).group(1)
    assert "href=" not in status and "/algo" not in html          # the retired Blaze page is not linked
    for el in ('id="runs"', 'id="pnl"', 'id="dd"', 'id="btnLegs"', 'id="btnLog"', 'id="btnDelete"', 'id="cycles"', "const ICONS",
               "/api/backtests", "run.py publish", "data-cmp"):
        assert el in html, el
    assert html.count("<table") == 2                                # the legs detail and the breakdown


def test_each_cycle_opens_to_its_trades(tmp_path, monkeypatch):
    """A run sent from the Mac or run on the server carries its orders; the page asks for one cycle's legs and orders."""
    flask = pytest.importorskip("flask")
    import importlib.util
    import sys
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from algolab import backtest_queue as bq, runs
    from algolab.config import load_config
    monkeypatch.setattr(bq, "RUNTIME", tmp_path / "rt")
    monkeypatch.setattr(bq, "QUEUE", tmp_path / "rt" / "backtest-queue")
    monkeypatch.setattr(bq, "JOBS", tmp_path / "rt" / "backtest-jobs")
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "rt").mkdir()
    cfg = load_config()
    cfg["pricing"]["smile"], cfg["pricing"]["real_only"] = "off", False
    store = tmp_path / "store"
    out = bq.run_one({"id": "x", "strategy": "intraday_straddle", "dataset": "SAMPLE_NIFTY_5minute",
                      "from": "2025-01-01", "to": "2025-02-28"}, store, cfg)
    assert (store / out["run"] / "fills.csv").exists()                      # orders now travel with the run
    spec = importlib.util.spec_from_file_location("bt_panel", root / "server" / "portfolio" / "backtests_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STORE", store)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    one = c.get(f"/api/backtests/{out['run']}").get_json()
    group = one["cycles"][0]["group"]
    j = c.get(f"/api/backtests/{out['run']}?cycle={group}").get_json()
    assert j["has_fills"] and len(j["trades"]) == 2 and all(t["group"] == group for t in j["trades"])
    assert {f["side"] for f in j["fills"]} == {"BUY", "SELL"} and all(f["group"] == group for f in j["fills"])
    assert isinstance(j["trades"][0]["entry_price"], float) and isinstance(j["fills"][0]["time"], str)
    html = (TPL / "backtests.html").read_text()
    assert "toggleCycle" in html and "?cycle=" in html and 'role="button"' in html


def test_results_are_picked_by_strategy_in_sections_that_fold():
    """Results: a strategy picker at the top lists only that strategy's runs and sweeps; every section folds and
    is remembered (checked in a browser with sample runs of two strategies)."""
    html = (TPL / "backtests.html").read_text()
    assert 'id="stratSel"' in html and "All strategies" in html and "?strategy=" in html
    assert "const shown = () => runs.filter(r => !strat || r.strategy === strat)" in html
    folds = re.findall(r'<details class="card sec[^"]*"[^>]*data-k="(\w+)"', html)
    assert folds == ["runs", "sweeps", "chart", "calendar", "breakdown", "cycles", "settings", "figures"]
    assert 'store.set("open." + d.dataset.k' in html
