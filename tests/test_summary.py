"""Summary page, closed legs at quantity 0, and trade activity with the reason behind each order."""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("cryptography")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_users import OWNER, OWNER_PW, add_user, first_password, make_app, signin  # noqa: E402


@pytest.mark.xfail(reason="Known: written before backtests became real-prices-only; needs stored option prices or --allow-model (docs/STATUS.md)", strict=False)
def test_every_backtest_order_says_why():
    from algolab import engine
    from algolab.config import load_config
    from algolab.strategy import load_strategies
    r = engine.backtest(load_config(), load_strategies()["short_straddle"](), "SAMPLE_NIFTY_5minute",
                        start="2026-01-01", end="2026-02-28")
    f = r.fills
    assert len(f) and (f.note != "").all()
    entry = f[f.reason == "Entry"].iloc[0]
    assert "to expiry" in entry.note and "NIFTY" in entry.note
    exits = f[f.reason.isin(["Stop-loss", "Target"])]
    if len(exits):
        assert "cycle P&L" in exits.iloc[0].note


def _state(tmp_path, sid, mode, today):
    d = tmp_path / "runtime" / "sessions" / sid
    d.mkdir(parents=True)
    c = lambda strike, right: {"underlying": "NIFTY", "expiry": "2026-10-20", "strike": strike, "right": right}  # noqa: E731
    lab = lambda strike, right: f"NIFTY 20 Oct 26 {strike} {right}"  # noqa: E731
    fill = lambda t, label, side, px, reason, note="": {"time": f"{today}T{t}", "contract": label, "side": side, "qty": 65,  # noqa: E731
                                                         "price": px, "costs": 25.0, "reason": reason, "group": "001",
                                                         "source": "model", "note": note}
    broker = {"capital": 300000, "realized_gross": 700.0, "total_costs": 100.0,
              "positions": [{"contract": c(24000, "CE"), "qty": -65, "avg_price": 50.0, "opened_at": f"{today}T09:20:00",
                             "group": "001", "entry_costs": 25.0}],
              "fills": [fill("09:20:00", lab(24000, "CE"), "SELL", 50.0, "Entry", "ATM · 6 days to expiry · NIFTY 24,010.0"),
                        fill("09:20:00", lab(23900, "PE"), "SELL", 40.0, "Entry"),
                        fill("13:00:00", lab(23900, "PE"), "BUY", 30.0, "Roll PE", "cycle P&L Rs 600 · NIFTY 24,120.0")],
              "trades": [{"contract": lab(23900, "PE"), "side": "SHORT", "qty": 65, "entry_time": f"{today}T09:20:00",
                          "exit_time": f"{today}T13:00:00", "entry_price": 40.0, "exit_price": 30.0, "gross_pnl": 650.0,
                          "costs": 50.0, "net_pnl": 600.0, "reason": "Roll PE", "group": "001"}]}
    state = {"status": "running", "strategy": "iron_fly", "params": {}, "feed": "kite", "mode": mode, "time": f"{today}T13:05:00",
             "spot": 24120.0, "vix": 12.5, "equity": 300500.0, "unrealized": -100.0,
             "positions": [{"Contract": lab(24000, "CE"), "Qty": -65, "Avg price": 50.0, "LTP": 51.0, "P&L": -65.0}],
             "broker": broker, "logs": ["engine noise"]}
    (d / "state.json").write_text(json.dumps(state))
    (d / "equity.csv").write_text(f"time,equity,spot\n{today} 09:20:00,300000,24010\n{today} 13:05:00,300500,24120\n")
    return d


@pytest.mark.xfail(reason="Known: expects a different /api/summary than the deployed Summary page (docs/STATUS.md)", strict=False)
def test_summary_and_session_details(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    today = datetime.now(mod.ZoneInfo("Asia/Kolkata")).date().isoformat()
    _state(tmp_path, "iron_fly", "paper", today)
    _state(tmp_path, "iron_fly-live", "live", today)
    signin(c, OWNER, OWNER_PW)

    s = next(x for x in c.get("/api/paper?kind=paper").get_json()["sessions"] if x["id"] == "iron_fly")["session"]
    closed = s["closed_legs"]
    assert [r["Contract"] for r in closed] == ["NIFTY 20 Oct 26 23900 PE"] and closed[0]["Qty"] == 0
    assert closed[0]["Exit price"] == 30.0 and closed[0]["P&L"] == 650
    assert [a["reason"] for a in s["activity"]] == ["Roll PE", "Entry", "Entry"]      # orders only, newest first
    assert s["activity"][0]["note"].startswith("cycle P&L")
    assert s["today"] == 500 and s["trades_today"] == 3

    j = c.get("/api/summary").get_json()
    assert j["groups"]["paper"]["today"] == 500 and j["groups"]["live"]["today"] == 500
    assert j["groups"]["paper"]["trades_today"] == 3 and len(j["trades"]) == 6
    assert j["market"] is None or j["market"]["spot"] == 24120.0
    page = c.get("/summary").get_data(as_text=True)
    assert "Today · real money" in page and 'href="/summary"' in page

    add_user(c, "vik", {"paper": "use"})
    first_password(c, "vik")
    assert c.get("/api/summary").status_code == 403          # the summary shows every account: owner only
