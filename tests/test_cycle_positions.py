"""Paper/Live: the positions a cycle closed — under Positions while the cycle is open (quantity 0, what each booked), and
behind a tap on each row of Closed cycles once it is over."""
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("cryptography")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_summary import _state  # noqa: E402
from test_users import OWNER, OWNER_PW, add_user, first_password, make_app, signin  # noqa: E402


def test_open_cycle_lists_its_closed_legs_and_a_closed_cycle_its_positions(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    d = _state(tmp_path, "iron_fly", "paper", today)
    _state(tmp_path, "iron_fly-live", "live", today)
    signin(c, OWNER, OWNER_PW)

    s = next(x for x in c.get("/api/paper?kind=paper").get_json()["sessions"] if x["id"] == "iron_fly")["session"]
    leg = s["closed_legs"][0]                     # cycle 001 is still open: its rolled put shows under Positions
    assert (leg["Contract"], leg["Qty"], leg["Was"], leg["Exit price"], leg["P&L"], leg["Net"]) == (
        "NIFTY 20 Oct 26 23900 PE", 0, -65, 30.0, 650.0, 600.0)
    assert leg["Reason"] == "Roll PE" and leg["Closed"].endswith("13:00")

    st = json.loads((d / "state.json").read_text())   # the cycle ends: nothing open, it moves to Closed cycles
    st["broker"]["positions"] = []
    (d / "state.json").write_text(json.dumps(st))
    s = next(x for x in c.get("/api/paper?kind=paper").get_json()["sessions"] if x["id"] == "iron_fly")["session"]
    assert s["closed_legs"] == [] and s["cycles"][0]["cycle"] == "001"
    j = c.get("/api/paper/cycle?id=iron_fly&cycle=001").get_json()
    assert j["cycle"] == "001" and len(j["legs"]) == 1
    l = j["legs"][0]
    assert (l["contract"], l["side"], l["qty"], l["entry_price"], l["exit_price"], l["net_pnl"], l["costs"]) == (
        "NIFTY 20 Oct 26 23900 PE", "SHORT", 65, 40.0, 30.0, 600.0, 50.0)
    assert c.get("/api/paper/cycle?id=iron_fly&cycle=999").get_json()["legs"] == []
    assert c.get("/api/paper/cycle?id=nobody&cycle=001").status_code == 404

    add_user(c, "vik", {"paper": "view"})            # a paper-only login sees paper cycles, never live ones
    first_password(c, "vik")
    assert c.get("/api/paper/cycle?id=iron_fly&cycle=001").status_code == 200
    assert c.get("/api/paper/cycle?id=iron_fly-live&cycle=001").status_code == 404


def test_page_opens_a_cycle_to_its_positions():
    page = (Path(__file__).resolve().parents[1] / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    assert '<details class="cyc" data-k="cyc:' in page and "function legsTable(legs)" in page
    assert "/api/paper/cycle?id=" in page and "s.cycles.map(c => cycRow(c, x.id))" in page
    assert "Closed in this cycle" in page and "s.closed_legs.map(closedRow)" in page
