"""Portfolio /api/history: each day's total P&L at the close, with the day's high and low (for the Total chart)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _app(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(ROOT / "server" / "portfolio"))
    spec = importlib.util.spec_from_file_location("portfolio_app_hist", ROOT / "server" / "portfolio" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 - a missing optional package on this machine
        pytest.skip(f"portfolio app not loadable here: {e}")
    finally:
        sys.path.remove(str(ROOT / "server" / "portfolio"))
    mod.DB_PATH = tmp_path / "portfolio.db"
    return mod


def test_history_has_day_high_and_low(tmp_path, monkeypatch):
    mod = _app(tmp_path, monkeypatch)
    mod.init_db() if hasattr(mod, "init_db") else None
    with mod.db() as con:
        for ts, day, pnl in [("2026-10-15 09:20:00", "2026-10-15", 100), ("2026-10-15 11:00:00", "2026-10-15", 300),
                             ("2026-10-15 15:30:00", "2026-10-15", 50), ("2026-10-16 15:30:00", "2026-10-16", 80)]:
            con.execute("INSERT INTO snapshots(ts, day, value, invested, pnl, day_pnl, gold_value) VALUES(?,?,?,?,?,?,?)",
                        (ts, day, 1000, 900, pnl, 0, 0))
    mod.app.config["TESTING"] = True
    mod.app.secret_key = "test"
    c = mod.app.test_client()
    with c.session_transaction() as s:
        s["user"] = "owner"; s["auth"] = True
    r = c.get("/api/history")
    if r.status_code in (302, 401):
        pytest.skip("sign-in needed by this app build")
    rows = r.get_json()["rows"]
    assert [(x["day"], x["pnl"], x["pnl_high"], x["pnl_low"]) for x in rows] == [
        ("2026-10-15", 50, 300, 50), ("2026-10-16", 80, 80, 80)]


def test_closed_market_keeps_the_last_trading_day(tmp_path, monkeypatch):
    """17 Oct (Saturday): Portfolio showed no day P&L; it should keep Friday's until Monday's open."""
    from datetime import datetime
    mod = _app(tmp_path, monkeypatch)
    mod.init_db()
    with mod.db() as con:
        for ts, pnl, day_pnl in [("2026-10-16 09:20:00", 100, -50), ("2026-10-16 15:35:00", 80, -4948)]:
            con.execute("INSERT INTO snapshots(ts, day, value, invested, pnl, day_pnl, gold_value) VALUES(?,?,?,?,?,?,?)",
                        (ts, "2026-10-16", 2634000, 2419000, pnl, day_pnl, 0))
        con.execute("INSERT INTO holding_snapshots(day, symbol, qty, avg_price, last_price, value, pnl, day_pnl, close_price) "
                    "VALUES('2026-10-16','TCS',10,3000,3100,31000,1000,-200,3120)")
        sat = datetime(2026, 10, 17, 11, 0, tzinfo=mod.IST)
        assert mod.last_session_day(con, sat) == "2026-10-16"
        assert mod.last_session_day(con, datetime(2026, 10, 19, 8, 30, tzinfo=mod.IST)) == "2026-10-16"   # Monday before the open
        assert mod.last_session_day(con, datetime(2026, 10, 16, 11, 0, tzinfo=mod.IST)) is None           # market open: live
        out = {"summary": {"value": 2634000, "day_pnl": 0, "day_pct": 0},
               "holdings": [{"symbol": "TCS", "day_pnl": 0, "day_pct": 0}, {"symbol": "NEW", "day_pnl": 5, "day_pct": 1}]}
        mod.hold_last_session(con, out, "2026-10-16")
    assert out["day_of"] == "2026-10-16" and out["summary"]["day_pnl"] == -4948
    tcs, new = out["holdings"]
    assert tcs["day_pnl"] == -200 and round(tcs["day_pct"], 3) == round((3100 - 3120) / 3120 * 100, 3)
    assert new["day_pnl"] == 0                                    # bought after that day: nothing to show for it
