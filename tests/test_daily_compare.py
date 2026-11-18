"""Paper / shadow / live vs 1-minute and 1-second backtests, day by day (algolab/daily_compare.py)."""
import json
from types import SimpleNamespace

import pandas as pd

from algolab import daily_compare as dc


def _session(root, sid, mode, trades, fills, equity_rows, positions=()):
    d = root / sid
    d.mkdir(parents=True)
    st = {"id": sid, "strategy": "short_straddle", "feed": "kite", "mode": mode, "params": {}, "label": sid,
          "broker": {"capital": 300000, "positions": list(positions), "trades": trades, "fills": fills}}
    (d / "state.json").write_text(json.dumps(st))
    (d / "equity.csv").write_text("time,equity,spot\n" + "".join(f"{t},300000,25000\n" for t in equity_rows))


def _trade(entry, exit_, net):
    return {"entry_time": entry, "exit_time": exit_, "net_pnl": net, "group": "c", "contract": "x", "side": "SHORT",
            "qty": 65, "entry_price": 100, "exit_price": 90, "gross_pnl": net, "costs": 40, "reason": "exit"}


def _fill(t, price, reason="entry", side="SELL"):
    return {"time": t, "contract": "NIFTY 27 Oct 26 25000 CE", "side": side, "qty": 65, "price": price,
            "costs": 20, "reason": reason, "group": "c", "source": "market"}


def _result(trades, fills, metrics=None):
    return SimpleNamespace(trades=pd.DataFrame(trades), fills=pd.DataFrame(fills), metrics=metrics or {})


def test_day_by_day_gaps_flags_labels_and_live_apart(tmp_path, monkeypatch):
    import algolab.engine as eng
    import algolab.paper as paper
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(dc, "OUT", tmp_path / "compare")
    root = tmp_path / "sessions"
    _session(root, "strangle", "paper",
             [_trade("2026-10-27T09:20:00", "2026-10-27T15:15:00", 1000.0),
              _trade("2026-10-28T12:19:00", "2026-10-28T15:15:00", -200.0)],
             [_fill("2026-10-27T09:20:00", 100.0), _fill("2026-10-27T11:00:00", 120.0, "Leg stop hit", "BUY"),
              _fill("2026-10-28T12:19:00", 90.0)],
             ["2026-10-27 09:15:00", "2026-10-28 12:19:00"])
    _session(root, "live1", "live", [_trade("2026-10-27T09:20:00", "2026-10-27T15:15:00", 300.0)],
             [_fill("2026-10-27T09:20:00", 100.0)], ["2026-10-27 09:15:00"])
    _session(root, "sim", "paper", [], [], [])                                   # did not trade: left out
    st = json.loads((root / "sim" / "state.json").read_text()); st["feed"] = "sim"
    (root / "sim" / "state.json").write_text(json.dumps(st))

    def fake(cfg, strat, dataset, start, end, not_before=None):
        if dataset == "NIFTY_minute":
            return _result([{"exit_time": "2026-10-27 15:15", "net_pnl": 1100.0}, {"exit_time": "2026-10-28 15:15", "net_pnl": 900.0}],
                           [{"time": "2026-10-27 09:20:00", "contract": "NIFTY 27 Oct 26 25000 CE", "side": "SELL", "price": 101.0, "source": "market"},
                            {"time": "2026-10-27 11:00:30", "contract": "NIFTY 27 Oct 26 25000 CE", "side": "BUY", "price": 118.0, "source": "market"}])
        return _result([{"exit_time": "2026-10-27 15:15", "net_pnl": 990.0}], [],
                       {"Trading days without 1-second bars (left out)": "2026-10-28"})
    monkeypatch.setattr(eng, "backtest", fake)

    rep = dc.run("2026-10-27", "2026-10-28", ["1m", "1s"], cfg=None, job_id="20261028-160000-abcd")
    by = {r["session"]: r for r in rep["sessions"]}
    assert set(by) == {"strangle", "live1"}
    s = by["strangle"]
    d27, d28 = s["days"]
    assert (d27["actual"], d27["bt_1m"], d27["gap_1m"], d27["bt_1s"], d27["gap_1s"]) == (1000, 1100, 100, 990, -10)
    assert not d27["flag"]                                        # 100 < max(500, 10% of 1000)
    assert d28["bt_1s"] is None and d28["gap_1m"] == 1100 and d28["flag"]   # no 1-second bars that day
    assert d28["notes"] == ["late start 12:19"]
    assert s["starts"] == {"2026-10-28": "12:19:00"}          # the backtests start at the session's first price that day
    f = s["bars"]["1m"]["fills"]
    assert f["matched"] == 2 and f["avg_diff"] == 1.5             # sold 1 cheaper (+1 worse), bought back 2 dearer (+2)
    assert f["stops"]["matched"] == 1 and f["stops"]["avg_diff"] == 2.0
    assert rep["totals"]["live"] == {"actual": 300, "bt_1m": 2000.0, "bt_1s": 990.0}
    assert rep["totals"]["paper"]["actual"] == 800
    assert (tmp_path / "compare" / "20261028-160000-abcd.json").exists() and (tmp_path / "compare" / "latest.json").exists()
    csv = dc.to_csv(rep).splitlines()
    assert csv[0].startswith("session,strategy,mode,day") and any("late start 12:19" in l for l in csv)


def test_carried_positions_and_holiday_fills_are_labelled(tmp_path, monkeypatch):
    import algolab.engine as eng
    import algolab.paper as paper
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(dc, "OUT", tmp_path / "compare")
    _session(tmp_path / "sessions", "fly", "paper", [_trade("2026-10-16T09:20:00", "2026-10-21T15:15:00", 500.0)],
             [_fill("2026-10-20T10:00:00", 50.0)], ["2026-10-19 09:15:00", "2026-10-21 09:15:00"])
    monkeypatch.setattr(eng, "backtest", lambda *a: _result([], []))
    rep = dc.run("2026-10-19", "2026-10-21", ["1m"])
    r = rep["sessions"][0]
    assert r["labels"]["carried"] and r["labels"]["holiday"] == ["2026-10-20"]
    # the 500 trade was opened on the 16th, before the period: not counted, listed apart
    assert r["actual_net"] == 0 and r["left_out"]["net"] == 500.0 and len(r["left_out"]["trades"]) == 1
    assert r["left_out"]["trades"][0]["entry_day"] == "2026-10-16" and r["period"]["from"] == "2026-10-19"
    assert r["days"][-1]["actual"] == 0 and "left out" in r["days"][-1]["notes"][0]


def test_only_trades_opened_in_the_period_are_counted(tmp_path, monkeypatch):
    import algolab.engine as eng
    import algolab.paper as paper
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(dc, "OUT", tmp_path / "compare")
    held = _trade("2026-10-16T09:20:00", "2026-10-20T10:00:00", 500.0)
    new = _trade("2026-10-20T09:20:00", "2026-10-20T11:00:00", 200.0)
    _session(tmp_path / "sessions", "fly2", "paper", [held, new],
             [_fill("2026-10-20T10:00:00", 50.0, side="BUY"), _fill("2026-10-20T09:20:00", 90.0)],
             ["2026-10-20 09:15:00"])
    monkeypatch.setattr(eng, "backtest", lambda *a, **k: _result([], []))
    r = dc.run("2026-10-20", "2026-10-20", ["1m"])["sessions"][0]
    assert r["actual_net"] == 200.0 and r["days"][0]["actual"] == 200.0
    assert [t["net"] for t in r["trades"]["2026-10-20"]["actual"]] == [200.0]
    assert [t["net"] for t in r["left_out"]["trades"]] == [500.0]


def test_queue_job_runs_the_comparison(tmp_path, monkeypatch):
    from algolab import backtest_queue as bq
    seen = {}
    monkeypatch.setattr(dc, "run", lambda *a, **k: seen.update(a=a, k=k) or
                        {"sessions": [], "totals": {"paper": {}, "live": {}}, "flagged": 0})
    monkeypatch.setattr(bq, "_status", lambda job, **c: None)
    out = bq.run_compare_period({"id": "20261028-160000-abcd", "from": "2026-10-27", "to": "2026-10-28", "bars": ["1s"]}, cfg={})
    assert out["report"] == "20261028-160000-abcd" and "0 flag(s)" in out["message"]
    assert seen["a"][2] == ["1s"] and seen["k"]["job_id"] == "20261028-160000-abcd"


def test_nightly_queues_the_day_only_when_something_traded(tmp_path, monkeypatch):
    from datetime import date
    import algolab.paper as paper
    from algolab import backtest_queue as bq
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(bq, "QUEUE", tmp_path / "q")
    monkeypatch.setattr(bq, "JOBS", tmp_path / "j")
    (tmp_path / "sessions").mkdir()
    assert dc.enqueue_day(date(2026, 10, 27)) is None
    _session(tmp_path / "sessions", "s1", "paper", [_trade("2026-10-27T09:20:00", "2026-10-27T15:15:00", 10.0)], [], [])
    jid = dc.enqueue_day(date(2026, 10, 27))
    job = json.loads((tmp_path / "q" / f"{jid}.json").read_text())
    assert job["kind"] == "compare_period" and job["from"] == job["to"] == "2026-10-27" and job["bars"] == ["1m", "1s"]
    assert bq.ID_OK.match(jid)


def test_trade_rows_for_the_compare_page_have_times_and_prices():
    saved = [_trade("2026-10-27T09:20:00", "2026-10-27T15:15:30", 1000.0)]
    rows = dc._trade_list(saved)
    assert rows[0]["day"] == "2026-10-27" and rows[0]["entry"] == "09:20:00" and rows[0]["exit"] == "15:15:30"
    assert (rows[0]["entry_price"], rows[0]["exit_price"], rows[0]["net"], rows[0]["qty"]) == (100.0, 90.0, 1000, 65)
    df = pd.DataFrame([{"contract": "c", "side": "SHORT", "qty": 65, "entry_time": pd.Timestamp("2026-10-27 09:21:02"),
                        "exit_time": pd.Timestamp("2026-10-27 15:15:00"), "entry_price": 101.234, "exit_price": 90.0,
                        "net_pnl": 700.4, "reason": "square off"}])
    bt = dc._trade_list(df)
    assert bt[0]["entry"] == "09:21:02" and bt[0]["entry_price"] == 101.23 and bt[0]["net"] == 700 and bt[0]["reason"] == "square off"
    assert dc._trade_list(pd.DataFrame()) == [] and dc._trade_list(None) == []
def test_days_the_1_minute_backtest_drops_show_as_na(tmp_path, monkeypatch):
    import algolab.engine as eng
    import algolab.paper as paper
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(dc, "OUT", tmp_path / "compare")
    _session(tmp_path / "sessions", "s", "paper", [_trade("2026-10-27T09:20:00", "2026-10-27T15:15:00", 900.0)], [], [])
    monkeypatch.setattr(eng, "backtest", lambda *a, **kw: _result([], [], {"Days without real option prices (left out)": "2026-10-27"}))
    d = dc.run("2026-10-27", "2026-10-27", ["1m"])["sessions"][0]["days"][0]
    assert d["bt_1m"] is None and not d["flag"]                    # not "0 vs 900, flagged"


def test_late_start_is_kept_to_the_second(tmp_path):
    """A session that started at 09:55:41: the backtests start then, not at 09:55:00 (41 seconds earlier the
    1-second backtest sold at other prices, and every stop after followed from those)."""
    eq = tmp_path / "equity.csv"
    eq.write_text("time,equity,spot\n2026-10-28 09:55:41,300000,25000\n2026-10-28 09:55:42,300000,25000\n"
                  "2026-10-29 09:15:00,300000,25000\n")
    assert dc._first_price_by_day(eq) == {"2026-10-28": "09:55:41", "2026-10-29": "09:15:00"}


def test_a_session_stopped_by_a_live_limit_is_reported_with_its_reason():
    from datetime import date
    st = {"logs": ["2026-10-28 09:50  Started", "2026-10-28 10:04  Session stopped: Order of 390 units of NIFTY 03 Nov 26 22550 PE "
                   "is above the limit of 3 lot(s). Positions were squared off.", "2026-10-21 11:00  Session stopped: Daily loss limit reached (Rs -5,100)"]}
    h = dc._halts(st, date(2026, 10, 28), date(2026, 10, 28))
    assert list(h) == ["2026-10-28"] and h["2026-10-28"][0]["time"] == "10:04" and h["2026-10-28"][0]["kind"] == "max lots"
    assert dc._halt_kind("Daily loss limit reached (Rs -5,100)") == "daily loss"
    assert dc._halt_kind("Zerodha refused the order BUY 65 X: margin") == "rejected"
    assert dc._halt_kind("Stopped from the dashboard") == "dashboard"
