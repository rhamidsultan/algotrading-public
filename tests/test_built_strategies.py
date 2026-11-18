"""Strategy Developer (algolab/built.py): strategies saved as specs, run by the same engine as the built-in ones."""
import json
from datetime import date

import pytest

from algolab import built
from algolab.config import load_config


@pytest.fixture()
def specs(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILT_STRATEGIES", str(tmp_path / "built"))
    return tmp_path / "built"


def _cfg():
    cfg = load_config()
    cfg["pricing"]["real_only"] = False
    cfg["pricing"]["smile"] = "off"
    return cfg


def test_specs_are_checked_in_words(specs):
    with pytest.raises(ValueError, match="name"):
        built.validate({"title": "x", "legs": [{}]})
    with pytest.raises(ValueError, match="at least one leg"):
        built.validate({"title": "No legs"})
    with pytest.raises(ValueError, match="Leg 1 call or put"):
        built.validate({"title": "Bad leg", "legs": [{"right": "XX"}]})
    with pytest.raises(ValueError, match="later than Entries from"):
        built.validate({"title": "Times", "legs": [{}], "entry": {"from": "14:00", "until": "10:00"}})
    with pytest.raises(ValueError, match="between 09:15 and 15:30"):
        built.validate({"title": "Times", "legs": [{}], "exit": {"time": "16:00"}})
    s = built.validate({"title": "My straddle", "legs": [{"right": "CE"}, {"right": "PE"}], "evil": "x"})
    assert s["name"] == "b_my_straddle" and "evil" not in s and s["legs"][0]["strike_by"] == "atm"


def test_saved_specs_become_strategies_and_trade(specs):
    from algolab.engine import backtest
    from algolab.strategy import load_strategies
    built.save_spec({**built.TEMPLATES["straddle"], "title": "Straddle 30", "exit": {"leg_stop": 30},
                     "reentry": {"on": True, "max_entries": 2}})
    built.save_spec({**built.TEMPLATES["buy_move"], "title": "Doubler"})
    built.save_spec({**built.TEMPLATES["iron_fly"], "title": "Fly to expiry", "exit": {"hold": "expiry"}})
    again = built.save_spec({**built.TEMPLATES["straddle"], "title": "Straddle 30"})
    assert again["name"] == "b_straddle_30_2"                               # a new one never overwrites
    (specs / "b_broken.json").write_text("{not json")                        # a broken file is skipped
    S = load_strategies()
    assert {"b_straddle_30", "b_doubler", "b_fly_to_expiry", "short_straddle"} <= set(S) and "b_broken" not in S
    cls = S["b_straddle_30"]
    assert cls.live_ready is False and set(cls.STOP_OFF) <= set(cls.PARAMS) and cls.PARAMS["leg_stop"].default == 30
    res = backtest(_cfg(), cls(), "SAMPLE_NIFTY_5minute", date(2025, 1, 1), date(2025, 1, 31))
    assert len(res.trades) > 0 and any("Leg stop" in x for x in res.logs)
    res = backtest(_cfg(), S["b_doubler"](), "SAMPLE_NIFTY_5minute", date(2025, 1, 1), date(2025, 1, 31))
    assert any("waiting for the entry condition" in x for x in res.logs)
    res = backtest(_cfg(), S["b_fly_to_expiry"](lots=2), "SAMPLE_NIFTY_5minute", date(2025, 1, 1), date(2025, 1, 31))
    assert len(res.trades) and res.trades["qty"].abs().min() == 150             # 1 lot per leg x 2, lot 75 in Jan 2025
    built.delete_spec("b_doubler")
    assert "b_doubler" not in load_strategies()


def test_studio_strategy_developer(tmp_path, monkeypatch, specs):
    import sys
    sys.path.insert(0, str(tmp_path.parent))
    from test_strategies_page import _studio, VIEWER
    c = _studio(tmp_path, monkeypatch)
    b = c.get("/studio/api/builder").get_json()
    assert "iron_fly" in b["templates"] and b["max_legs"] == 6
    spec = {**b["templates"]["strangle"], "title": "Wide strangle", "exit": {"leg_stop": 25}}
    chk = c.post("/studio/api/built/check", json={"spec": spec}).get_json()
    assert chk["ok"] and "ATM+4 steps" in chk["describe"]
    assert c.post("/studio/api/built/check", json={"spec": {"title": "x"}}).get_json()["ok"] is False
    r = c.post("/studio/api/built", json={"spec": spec}).get_json()
    assert r["name"] == "b_wide_strangle"
    lst = {s["name"]: s for s in c.get("/studio/api/strategies").get_json()["strategies"]}
    assert lst["b_wide_strangle"]["built"] is True and lst["b_wide_strangle"]["stage"] == "Draft"
    cat = json.loads((tmp_path / "runtime" / "strategies.json").read_text())       # the dashboard's list too
    assert any(x["name"] == "b_wide_strangle" for x in cat)
    assert c.get("/studio/api/built/b_wide_strangle").get_json()["spec"]["exit"]["leg_stop"] == 25
    spec["exit"] = {"leg_stop": 40}
    assert c.post("/studio/api/built", json={"spec": spec, "name": "b_wide_strangle"}).get_json()["name"] == "b_wide_strangle"
    assert c.post("/studio/api/built", json={"spec": spec, "name": "b_ghost"}).status_code == 404
    assert c.post("/studio/api/built", json={"spec": {"title": "No legs"}}).status_code == 400
    d = tmp_path / "runtime" / "sessions" / "b_wide_strangle"
    d.mkdir(parents=True)
    (d / "desired.json").write_text(json.dumps({"strategy": "b_wide_strangle", "feed": "sim"}))
    assert c.post("/studio/api/built/delete", json={"name": "b_wide_strangle"}).status_code == 400   # running
    (d / "desired.json").unlink()
    (d / "state.json").write_text("{}")
    assert c.post("/studio/api/built/delete", json={"name": "b_wide_strangle"}).status_code == 200
    assert c.get("/studio/api/built/b_wide_strangle").status_code == 404
    viewer = _studio(tmp_path, monkeypatch, who=VIEWER)
    assert viewer.post("/studio/api/built", json={"spec": spec}).status_code == 403
