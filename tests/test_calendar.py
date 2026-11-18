"""Paper/Live page: calendar of closed-cycle P&L."""
from pathlib import Path


def test_closed_cycle_calendar():
    """Paper/Live calendar: closed cycles grouped by the day they closed, net of charges."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "algolab_panel_cal", Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def tr(group, entry, exit_, net, costs=20.0, reason="target"):
        return {"group": group, "entry_time": entry, "exit_time": exit_, "net_pnl": net, "costs": costs, "reason": reason}
    trades = [tr("c1", "2026-10-13T09:20:00", "2026-10-14T15:10:00", 1500), tr("c1", "2026-10-13T09:20:00", "2026-10-14T15:12:00", -400),
              tr("c2", "2026-10-14T10:00:00", "2026-10-14T14:00:00", 300),
              tr("c3", "2026-10-14T11:00:00", "2026-10-15T09:30:00", -900, reason="stop")]   # closes the next day
    cal = mod._calendar(trades)
    assert [d["date"] for d in cal] == ["2026-10-14", "2026-10-15"]
    assert cal[0]["net"] == 1400 and len(cal[0]["cycles"]) == 2
    c1 = next(c for c in cal[0]["cycles"] if c["cycle"] == "c1")
    assert c1["net"] == 1100 and c1["legs"] == 2 and c1["closed"] == "2026-10-14 15:12"
    assert cal[1]["net"] == -900 and cal[1]["cycles"][0]["reason"] == "stop"
    assert mod._calendar([]) == []
