"""Paper / shadow / live session card: "Unrealized" for open legs, and a P&L chart of the current cycle alone."""
from test_defined_risk import _panel


def _equity(path, rows):
    path.write_text("time,equity\n" + "".join(f"{t},{e}\n" for t, e in rows))


def test_current_cycle_curve_starts_at_zero_from_the_first_entry(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    eq = tmp_path / "equity.csv"
    _equity(eq, [("2026-10-22T09:30", 301000), ("2026-10-22T12:35", 302000), ("2026-10-22T12:40", 301500),
                 ("2026-10-23T10:00", 303500), ("2026-10-23T11:00", 302800)])
    b = {"positions": [{"opened_at": "2026-10-22T13:00:00", "group": "c2"}],
         "trades": [{"group": "c2", "entry_time": "2026-10-22T12:36:00", "exit_time": "2026-10-22T14:00:00",
                     "net_pnl": -300.0, "costs": 40.0, "reason": "roll"},
                    {"group": "c1", "entry_time": "2026-10-21T09:20:00", "exit_time": "2026-10-21T15:15:00",
                     "net_pnl": 1000.0, "costs": 50.0, "reason": "exit"}]}
    assert mod._cycle_window(b) == ("2026-10-22 12:36", None, "Current cycle")   # a leg closed in it started it
    cy = mod._cycle_curve(eq, 300000, b)
    assert cy["label"] == "Current cycle" and cy["from"] == "2026-10-22 12:36"
    assert cy["points"][0] == ("2026-10-22 12:40", -500.0)          # measured from 12:35, the last point before
    assert cy["points"][-1] == ("2026-10-23 11:00", 800.0) and cy["hi"][1] == 1500.0


def test_flat_session_shows_its_last_cycle(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    eq = tmp_path / "equity.csv"
    _equity(eq, [("2026-10-21T09:00", 300000), ("2026-10-21T09:20", 300100), ("2026-10-21T15:15", 301000),
                 ("2026-10-22T10:00", 301000)])
    b = {"positions": [], "trades": [{"group": "c1", "entry_time": "2026-10-21T09:20:00", "exit_time": "2026-10-21T15:15:00",
                                      "net_pnl": 1000.0, "costs": 50.0, "reason": "exit"}]}
    cy = mod._cycle_curve(eq, 300000, b)
    assert cy["label"] == "Last cycle" and cy["to"] == "2026-10-21 15:15"
    assert [p[1] for p in cy["points"]] == [100.0, 1000.0]          # stops at the cycle's exit
    assert mod._cycle_curve(eq, 300000, {"positions": [], "trades": []}) is None


def test_card_says_unrealized_and_offers_both_chart_views(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    page = open(mod.__file__, encoding="utf-8").read()
    assert 'num("Unrealized"' in page and '<span class="n">Unrealized</span>' in page
    assert 'data-v="all"' in page and 'data-v="cycle"' in page and ".pnlview" in page
