"""Positions page (23 Nov 2026): trail a position's GTT SL behind its best price, and SL / target entered as % of
the average price (the page converts it to a price)."""
import json

from test_positions_page import SYM_L, SYM_S, FakeKite, _load

KEY_S, KEY_L = f"NFO:{SYM_S}:NRML", f"NFO:{SYM_L}:NRML"


class Kite(FakeKite):
    def modify_gtt(self, **kw):                      # Zerodha keeps the new trigger
        super().modify_gtt(**kw)
        for g in self.gtts:
            if g["id"] == kw["trigger_id"]:
                g["condition"].update(trigger_values=kw["trigger_values"], last_price=kw["last_price"])
                g["orders"] = [dict(o) for o in kw["orders"]]
                g["type"] = kw["trigger_type"]


def _fresh(mod):
    """The next pass reads new prices (on the server the passes are 15 s apart; quotes are spaced 1 s)."""
    mod._invalidate()
    mod._QLAST[0] = 0.0
    mod._CACHE["quotes"] = {}


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("POSITIONS_TRAIL", "0")
    k = Kite()
    mod, c = _load(tmp_path, monkeypatch, k)
    return k, mod, c


def _sl(mod, k, key, qty):
    exch, sym, product = key.split(":")
    g = mod._gtts_for(k.gtts, exch, sym, mod._exit_side(qty), product)[0]
    return mod._gtt_view(g, qty)["sl"]


def test_a_short_positions_sl_trails_down_and_never_back_up(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    assert c.post("/api/positions/protect", json={"key": KEY_S, "sl": 70, "target": 20}).get_json()["ok"]
    r = c.post("/api/positions/trail", json={"key": KEY_S, "by": 10, "unit": "pct"}).get_json()
    assert r["ok"] and "55.00" in r["message"]                    # best 50 + 10%
    _fresh(mod)
    mod.trail_once(k)
    assert _sl(mod, k, KEY_S, -65) == 55.0
    k.ltp[SYM_S] = 45.0
    _fresh(mod)
    mod.trail_once(k)
    assert _sl(mod, k, KEY_S, -65) == 49.5
    k.ltp[SYM_S] = 48.0                                           # back up: the SL stays
    _fresh(mod)
    n = len([x for x in k.calls if x[0] == "modify_gtt"])
    mod.trail_once(k)
    assert _sl(mod, k, KEY_S, -65) == 49.5 and len([x for x in k.calls if x[0] == "modify_gtt"]) == n
    g = mod._gtt_view(k.gtts[0], -65)
    assert g["target"] == 20.0                                     # the target stays


def test_a_long_positions_sl_trails_up_in_points(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    c.post("/api/positions/protect", json={"key": KEY_L, "sl": 30})
    c.post("/api/positions/trail", json={"key": KEY_L, "by": 5, "unit": "pts"})
    k.ltp[SYM_L] = 52.0
    _fresh(mod)
    mod.trail_once(k)
    assert _sl(mod, k, KEY_L, 65) == 47.0


def test_the_trail_needs_an_sl_and_ends_with_the_position(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    assert c.post("/api/positions/trail", json={"key": KEY_S, "by": 10}).status_code == 400   # no SL yet
    c.post("/api/positions/protect", json={"key": KEY_S, "sl": 70})
    c.post("/api/positions/trail", json={"key": KEY_S, "by": 10})
    k.pos[SYM_S] = (0, "NRML", 60.0)                              # closed (e.g. the GTT triggered)
    _fresh(mod)
    out = mod.trail_once(k)
    assert "trail ended" in out[0] and not mod._trails_rw()


def test_no_move_while_a_close_runs(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    c.post("/api/positions/protect", json={"key": KEY_S, "sl": 70})
    c.post("/api/positions/trail", json={"key": KEY_S, "by": 10})
    (tmp_path / "data" / "positions_jobs.json").write_text(json.dumps([{"id": "x", "key": KEY_S, "status": "working"}]))
    _fresh(mod)
    mod.trail_once(k)
    assert not [x for x in k.calls if x[0] == "modify_gtt"]


def test_trail_off_and_the_page(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    c.post("/api/positions/protect", json={"key": KEY_S, "sl": 70})
    c.post("/api/positions/trail", json={"key": KEY_S, "by": 10})
    rows = {r["key"]: r for r in c.get("/api/positions").get_json()["positions"]}
    assert rows[KEY_S]["trail"]["by"] == 10
    assert c.post("/api/positions/trail", json={"key": KEY_S, "by": 0}).get_json()["ok"] and not mod._trails_rw()
    page = c.get("/positions").get_data(as_text=True)
    assert "% of avg" in page and "Trail SL by" in page


def test_trail_level():
    from test_positions_page import _load  # noqa: F401
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("pp", Path(__file__).resolve().parents[1] / "server/portfolio/positions_panel.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.trail_level(-65, 50.0, 10, "pct") == 55.0 and m.trail_level(65, 50.0, 10, "pct") == 45.0
    assert m.trail_level(-65, 50.02, 10, "pct") == 55.05            # a short's SL rounds up (never tighter by rounding)


def test_a_trail_ends_when_the_position_is_opened_again_or_its_gtt_is_replaced(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    c.post("/api/positions/protect", json={"key": KEY_S, "sl": 70})
    c.post("/api/positions/trail", json={"key": KEY_S, "by": 10})
    k.pos[SYM_S] = (-65, "NRML", 52.0)                            # closed and sold again, same size
    _fresh(mod)
    assert "opened again" in mod.trail_once(k)[0] and not mod._trails_rw()


def test_a_stale_or_one_sided_price_does_not_move_the_stop(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    c.post("/api/positions/protect", json={"key": KEY_S, "sl": 70})
    c.post("/api/positions/trail", json={"key": KEY_S, "by": 10})
    real = k.quote
    k.quote = lambda keys: {kk: {**v, "depth": {"buy": [], "sell": v["depth"]["sell"]}} for kk, v in real(keys).items()}
    _fresh(mod)
    mod.trail_once(k)
    assert not [x for x in k.calls if x[0] == "modify_gtt"]      # no bid: not moved
    k.quote = lambda keys: {kk: {**v, "last_trade_time": "2026-10-29 10:40:00"} for kk, v in real(keys).items()}
    _fresh(mod)
    mod.trail_once(k)
    assert not [x for x in k.calls if x[0] == "modify_gtt"]      # last trade 20 min old: not moved


def test_the_page_never_converts_prices_as_percent_and_never_removes_on_save(tmp_path, monkeypatch):
    k, mod, c = _env(tmp_path, monkeypatch)
    page = c.get("/positions").get_data(as_text=True)
    assert 'el.dataset.unit="px";setUnit(el,"px")' in page        # opening the editor: prices, as they are
    assert "away(r,kind)*(px/r.avg-1)*100" in page                 # signed %: an SL past the average stays there
    assert "remove:true" in page and "To delete the stop, tap Remove GTT" in page
