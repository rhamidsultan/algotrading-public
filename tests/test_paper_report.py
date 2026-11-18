"""Weekly paper-vs-backtest report: same strategy, same days, compared day by day and fill by fill."""
import json

import pytest
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from algolab import paper, paper_report, runs  # noqa: E402
from algolab.config import load_config  # noqa: E402


def test_fill_matching_counts_dearer_paper_fills_against_paper():
    bt = pd.DataFrame([{"time": "2025-01-06 09:30:00", "contract": "X", "side": "SELL", "price": 100.0, "source": "model"},
                       {"time": "2025-01-06 15:00:00", "contract": "X", "side": "BUY", "price": 80.0, "source": "model"},
                       {"time": "2025-01-07 10:00:00", "contract": "Y", "side": "BUY", "price": 5.0, "source": "model"}])
    paper_fills = [{"time": "2025-01-06T09:31:00", "contract": "X", "side": "SELL", "price": 99.0},   # sold 1 lower
                   {"time": "2025-01-06T15:02:00", "contract": "X", "side": "BUY", "price": 81.0},    # bought 1 higher
                   {"time": "2025-01-08T10:00:00", "contract": "Z", "side": "BUY", "price": 3.0}]
    m = paper_report.match_fills(paper_fills, bt)
    assert m == {"matched": 2, "paper_only": 1, "backtest_only": 1, "avg_diff": 1.0}


def test_weekly_report_compares_a_paper_session_with_its_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(paper_report, "OUT", tmp_path / "paper_report.json")
    monkeypatch.setattr(paper_report, "LATEST", tmp_path / "paper_compare.json")
    monkeypatch.setattr(paper_report, "ist_now", lambda: datetime(2025, 1, 10, 9, 0))
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("BACKTESTS_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("ALGOLAB_PUSH_DIR", str(tmp_path / "push"))
    d = tmp_path / "sessions" / "intraday_straddle"
    d.mkdir(parents=True)
    trade = {"contract": "NIFTY 09JAN25 24000 CE", "side": "SHORT", "qty": 75, "entry_time": "2025-01-06T09:30:00",
             "exit_time": "2025-01-06T15:15:00", "entry_price": 100, "exit_price": 90, "gross_pnl": 750, "costs": 50,
             "net_pnl": 700, "reason": "", "group": ""}
    (d / "state.json").write_text(json.dumps({"id": "intraday_straddle", "strategy": "intraday_straddle", "params": {},
                                              "feed": "kite", "mode": "paper", "broker": {"trades": [trade], "fills": []}}))
    (tmp_path / "sessions" / "sim").mkdir()
    (tmp_path / "sessions" / "sim" / "state.json").write_text(json.dumps({"strategy": "x", "feed": "sim"}))  # not on live prices
    cfg = load_config()
    cfg["pricing"]["real_only"] = False
    cfg["pricing"]["smile"] = "off"
    cfg["paper_report"] = {"dataset": "SAMPLE_NIFTY_5minute"}
    out = paper_report.weekly(cfg, log=lambda *_: None)
    assert out["from"] == "2025-01-04" and len(out["sessions"]) == 1
    r = out["sessions"][0]
    assert "error" not in r, r
    assert r["paper_net"] == 700 and r["diff"] == r["backtest_net"] - 700
    assert any(row["day"] == "2025-01-06" and row["paper"] == 700 for row in r["days"])
    meta = json.loads((tmp_path / "store" / r["run"] / "run.json").read_text())
    assert meta["metrics"]["Paper P&L (same days)"] == 700 and "Paper vs backtest" in meta["note"]
    assert json.loads((tmp_path / "paper_report.json").read_text())["sessions"][0]["session"] == "intraday_straddle"
    assert list((tmp_path / "push").glob("*.json"))
    assert json.loads((tmp_path / "paper_compare.json").read_text())["intraday_straddle"]["paper_net"] == 700


def test_studio_compare_job_runs_one_session(tmp_path, monkeypatch):
    """Algo Studio › Analysis › Compare: a "compare" job on the backtest queue, with the session's Orders & stops."""
    from algolab import backtest_queue as bq, config
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(paper_report, "LATEST", tmp_path / "paper_compare.json")
    monkeypatch.setattr(config, "ist_now", lambda: datetime(2025, 1, 10, 9, 0))
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    d = tmp_path / "sessions" / "s1"
    d.mkdir(parents=True)
    trade = {"exit_time": "2025-01-06T15:15:00", "net_pnl": 700}
    (d / "state.json").write_text(json.dumps({"strategy": "intraday_straddle", "params": {}, "feed": "kite", "mode": "shadow",
                                              "risk": {"stops": "framework", "day_loss": 5000}, "preset": {"id": "p", "name": "P"},
                                              "broker": {"trades": [trade], "fills": []}}))
    cfg = load_config()
    cfg["pricing"]["real_only"] = False
    cfg["pricing"]["smile"] = "off"
    cfg["paper_report"] = {"dataset": "SAMPLE_NIFTY_5minute"}
    out = bq.run_compare({"kind": "compare", "session": "s1", "days": 7}, store=tmp_path / "store", cfg=cfg)
    meta = json.loads((tmp_path / "store" / out["run"] / "run.json").read_text())
    assert meta["config"]["risk"]["day_loss"] == 5000 and meta["preset"]["name"] == "P"
    rep = json.loads((tmp_path / "paper_compare.json").read_text())["s1"]
    assert rep["from"] == "2025-01-04" and rep["paper_net"] == 700 and rep["run"] == out["run"]
    with pytest.raises(ValueError):
        bq.run_compare({"session": "ghost"}, store=tmp_path / "store", cfg=cfg)
