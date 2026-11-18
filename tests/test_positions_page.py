"""Positions page: live positions from Zerodha, GTT SL/target, manual close (against a fake Zerodha)."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYM_S = "NIFTY26N0322650PE"     # short, NRML
SYM_L = "NIFTY26N0323000CE"     # long, NRML
SYM_M = "NIFTY26N0322500PE"     # short, MIS


class FakeKite:
    def __init__(self):
        self.pos = {SYM_S: (-65, "NRML", 60.0), SYM_L: (65, "NRML", 40.0), SYM_M: (-65, "MIS", 30.0)}
        self.ltp = {SYM_S: 50.0, SYM_L: 45.0, SYM_M: 28.0}
        self.gtts, self.orders_ = [], []
        self.calls = []
        self.history = {}
        self.fill_after = 2          # polls before an exit order completes (None: never)
        self.polls = {}
        self.n = 0
        self.lag = 0                 # position reads that still show the old quantity after a fill
        self.place_raises = False
        self.lag_on_fill = 0

    def positions(self):
        if self.lag:
            self.lag -= 1
            return self._stale
        net = [{"exchange": "NFO", "tradingsymbol": s, "quantity": q, "product": pr, "average_price": a,
                "multiplier": 1, "buy_value": (q * a if q > 0 else 0), "sell_value": (-q * a if q < 0 else 0),
                "overnight_quantity": 0, "last_price": self.ltp[s]} for s, (q, pr, a) in self.pos.items()]
        return {"net": net}

    def quote(self, keys):
        out = {}
        for k in keys:
            s = k.split(":")[1]
            l = self.ltp.get(s, 10.0)
            out[k] = {"last_price": l, "upper_circuit_limit": 200.0, "lower_circuit_limit": 0.05, "ohlc": {"close": l},
                      "depth": {"buy": [{"price": l - 0.5}], "sell": [{"price": l + 0.5}]}}
        return out

    def get_gtts(self):
        return [dict(g) for g in self.gtts]

    def orders(self):
        return list(self.orders_)

    def place_gtt(self, **kw):
        self.calls.append(("place_gtt", kw))
        self.n += 1
        self.gtts.append({"id": self.n, "type": kw["trigger_type"], "status": "active",
                          "condition": {"exchange": kw["exchange"], "tradingsymbol": kw["tradingsymbol"],
                                        "trigger_values": kw["trigger_values"], "last_price": kw["last_price"]},
                          "orders": [dict(o) for o in kw["orders"]]})
        return {"trigger_id": self.n}

    def modify_gtt(self, **kw):
        self.calls.append(("modify_gtt", kw))

    def delete_gtt(self, gid):
        self.calls.append(("delete_gtt", gid))
        self.gtts = [g for g in self.gtts if g["id"] != gid]

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        oid = f"9{len(self.calls)}"
        self.history[oid] = kw
        self.polls[oid] = 0
        if kw.get("tag"):
            self.orders_.append({"order_id": oid, "tradingsymbol": kw["tradingsymbol"], "exchange": "NFO", "tag": kw["tag"],
                                 "transaction_type": kw["transaction_type"], "product": kw["product"], "status": "OPEN",
                                 "order_type": "LIMIT", "variety": "regular", "order_timestamp": "2026-10-29 11:00:05"})
        if self.place_raises:
            raise TimeoutError("read timed out")
        return oid

    def _done(self, oid, status):
        for o in self.orders_:
            if o["order_id"] == oid:
                o["status"] = status

    def order_history(self, oid):
        if oid not in self.history:          # an order placed in Kite: cancelled once it left the book
            o = next((x for x in self.orders_ if x["order_id"] == oid), None)
            return [{"status": o["status"] if o else "CANCELLED", "filled_quantity": 0}]
        kw = self.history[oid]
        self.polls[oid] += 1
        if kw.get("cancelled"):
            self._done(oid, "CANCELLED")
            return [{"status": "CANCELLED", "filled_quantity": kw.get("filled", 0), "average_price": kw["price"] if kw.get("filled") else 0}]
        if self.fill_after is not None and self.polls[oid] >= self.fill_after:
            s = kw["tradingsymbol"]
            if self.pos[s][0] != 0 and self.lag_on_fill:
                self._stale, self.lag = self.positions(), self.lag_on_fill
                self.lag_on_fill = 0
            self.pos[s] = (0,) + self.pos[s][1:]
            self._done(oid, "COMPLETE")
            return [{"status": "COMPLETE", "filled_quantity": kw["quantity"], "average_price": kw["price"]}]
        return [{"status": "OPEN", "filled_quantity": 0}]

    def modify_order(self, **kw):
        self.calls.append(("modify_order", kw))
        self.history[kw["order_id"]]["price"] = kw["price"]

    def cancel_order(self, **kw):
        self.calls.append(("cancel_order", kw))
        if kw["order_id"] in self.history:
            self.history[kw["order_id"]]["cancelled"] = True
            self._done(kw["order_id"], "CANCELLED")
        else:
            self.orders_ = [o for o in self.orders_ if o["order_id"] != kw["order_id"]]


def _load(tmp_path, monkeypatch, kite):
    pytest.importorskip("flask")
    spec = importlib.util.spec_from_file_location("positions_panel_t", ROOT / "server" / "portfolio" / "positions_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BASE = tmp_path
    mod.ENGINE = tmp_path / "engine"
    mod.JOBS.clear()
    mod.BUSY.clear()
    mod._invalidate()
    monkeypatch.setattr(mod, "_kite", lambda: (kite, None))
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    monkeypatch.setattr(mod, "_notify", lambda s, b: mod.__dict__.setdefault("ALERTS", []).append((s, b)))
    monkeypatch.setattr(mod, "_now", lambda: datetime(2026, 10, 29, 11, 0, tzinfo=mod.IST))
    import flask
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    return mod, app.test_client()


def _live_session(tmp_path, sym_strike=22650, right="PE", mode="live"):
    d = tmp_path / "engine" / "runtime" / "sessions" / "premium_strangle-3"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"mode": mode, "broker": {"positions": [
        {"contract": {"underlying": "NIFTY", "expiry": "2026-11-03", "strike": sym_strike, "right": right}, "qty": -65}]}}))


def test_parse_and_names():
    spec = importlib.util.spec_from_file_location("pp", ROOT / "server" / "portfolio" / "positions_panel.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError:
        pytest.skip("needs flask")
    assert mod.parse_option("NIFTY26N0322650PE") == ("NIFTY", 2026, 11, 3, 22650, "PE")
    assert mod.parse_option("NIFTY26O2124000CE") == ("NIFTY", 2026, 10, 21, 24000, "CE")
    assert mod.parse_option("BANKNIFTY26NOV52000CE") == ("BANKNIFTY", 2026, 11, None, 52000, "CE")
    assert mod.parse_option("RELIANCE") is None
    assert mod.pretty("NIFTY26N0322650PE") == "NIFTY 03 Nov 22650 PE"


def test_snapshot_rows_pnl_and_algo_lock(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    _live_session(tmp_path)
    d = c.get("/api/positions").get_json()
    rows = {r["symbol"]: r for r in d["positions"]}
    assert rows[SYM_S]["pnl"] == pytest.approx(65 * (60 - 50))          # short gains as price falls
    assert rows[SYM_L]["pnl"] == pytest.approx(65 * (45 - 40))
    assert rows[SYM_S]["algo"] == "premium_strangle-3" and rows[SYM_S]["locked"] and not rows[SYM_S]["gtt_ok"]
    assert rows[SYM_L]["algo"] is None and rows[SYM_L]["gtt_ok"]
    assert not rows[SYM_M]["gtt_ok"] and rows[SYM_M]["locked"] is None    # MIS: close only
    assert rows[SYM_L]["bid"] == 44.5 and rows[SYM_L]["ask"] == 45.5
    assert d["unprotected"] == 2                                           # long + MIS (algo leg excluded)


def test_shadow_session_does_not_lock(tmp_path, monkeypatch):
    mod, c = _load(tmp_path, monkeypatch, FakeKite())
    _live_session(tmp_path, mode="shadow")
    rows = {r["symbol"]: r for r in c.get("/api/positions").get_json()["positions"]}
    assert rows[SYM_S]["algo"] is None


def test_protect_short_places_oco_with_capped_limits(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    r = c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 190, "target": 20})
    assert r.status_code == 200, r.get_json()
    name, kw = k.calls[-1]
    assert name == "place_gtt" and kw["trigger_type"] == "two-leg" and kw["trigger_values"] == [20.0, 190.0]
    lo, hi = kw["orders"]
    assert lo["transaction_type"] == hi["transaction_type"] == "BUY" and lo["quantity"] == 65 and lo["product"] == "NRML"
    assert lo["price"] == pytest.approx(20.2)                 # target limit 1% through
    assert hi["price"] == pytest.approx(200.0)                # 190 x 1.10 = 209 capped at the upper circuit
    view = c.get("/api/positions").get_json()
    g = next(x for x in view["positions"] if x["symbol"] == SYM_S)["gtt"]
    assert g["sl"] == 190 and g["target"] == 20 and g["qty_ok"]
    # a second save modifies the same GTT
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    assert k.calls[-1][0] == "modify_gtt" and k.calls[-1][1]["trigger_type"] == "single"
    assert k.calls[-1][1]["orders"][0]["price"] == pytest.approx(132.0)


def test_protect_long_and_single(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    assert c.post("/api/positions/protect", json={"key": f"NFO:{SYM_L}:NRML", "sl": 36}).status_code == 200
    kw = k.calls[-1][1]
    assert kw["trigger_type"] == "single" and kw["orders"][0]["transaction_type"] == "SELL"
    assert kw["orders"][0]["price"] == pytest.approx(32.40)
    g = next(x for x in c.get("/api/positions").get_json()["positions"] if x["symbol"] == SYM_L)["gtt"]
    assert g["sl"] == 36 and g["target"] is None


@pytest.mark.parametrize("body,needle", [
    ({"key": f"NFO:{SYM_S}:NRML", "sl": 45}, "would trigger at once"),          # short SL below LTP
    ({"key": f"NFO:{SYM_S}:NRML", "sl": 250}, "upper circuit"),                 # the 19/21 Oct finding
    ({"key": f"NFO:{SYM_S}:NRML", "target": 55}, "Target"),
    ({"key": f"NFO:{SYM_L}:NRML", "sl": 50}, "would trigger at once"),
    ({"key": f"NFO:{SYM_M}:MIS", "sl": 40}, "NRML or CNC"),
    ({"key": f"NFO:{SYM_L}:NRML", "sl": -3}, "positive"),
    ({"key": "NFO:NOPE26N0310CE:NRML", "sl": 3}, "no longer open"),
])
def test_protect_refusals(tmp_path, monkeypatch, body, needle):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    r = c.post("/api/positions/protect", json=body)
    assert r.status_code == 400 and needle in r.get_json()["error"], r.get_json()
    assert not any(n in ("place_gtt", "modify_gtt", "delete_gtt") for n, _ in k.calls)


def test_algo_leg_is_read_only(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    _live_session(tmp_path)
    assert "live session" in c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 90}).get_json()["error"]
    assert "live session" in c.post("/api/positions/close", json={"key": f"NFO:{SYM_S}:NRML"}).get_json()["error"]
    assert not k.calls


def test_remove_gtt(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_L}:NRML", "sl": 36, "target": 60})
    r = c.post("/api/positions/protect", json={"key": f"NFO:{SYM_L}:NRML", "sl": None, "target": None})
    assert r.status_code == 200 and k.calls[-1] == ("delete_gtt", 1) and not k.gtts


def _job(mod, key, k):
    net = k.positions()["net"]
    p = mod._find(net, key)
    job = {"id": "j1", "key": key, "exchange": "NFO", "symbol": key.split(":")[1], "name": key, "product": p["product"],
           "qty": p["quantity"], "side": mod._exit_side(p["quantity"]), "status": "working", "order_id": None, "price": None,
           "first_price": None, "filled": 0, "avg": None, "saved_gtts": [], "saved_orders": [], "ltp": None,
           "phase": "start", "steps": [], "started": "2026-10-29T11:00:00", "ended": None}
    mod.JOBS[job["id"]] = job
    mod.BUSY[key] = job["id"]
    return job


def test_close_fills_deletes_gtt_first_and_cancels_pending(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    k.orders_ = [{"order_id": "555", "tradingsymbol": SYM_S, "exchange": "NFO", "status": "TRIGGER PENDING",
                  "transaction_type": "BUY", "order_type": "SL", "variety": "regular", "product": "NRML",
                  "trigger_price": 120.0, "price": 132.0}]
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    names = [n for n, _ in k.calls]
    assert names.index("delete_gtt") < names.index("place_order")       # never both live
    po = next(kw for n, kw in k.calls if n == "place_order")
    assert po["transaction_type"] == "BUY" and po["quantity"] == 65 and po["order_type"] == "LIMIT"
    assert po["price"] == pytest.approx(50.5) and po["product"] == "NRML" and len(po["tag"]) <= 20
    assert job["status"] == "done" and not k.gtts
    assert names.index("cancel_order") < names.index("place_order")     # SL order off before the exit
    assert [kw["order_type"] for n, kw in k.calls if n == "place_order"] == ["LIMIT"]   # nothing put back
    assert ("cancel_order", {"variety": "regular", "order_id": "555"}) in k.calls
    assert f"NFO:{SYM_S}:NRML" not in mod.BUSY


def test_close_not_filled_reprices_cancels_and_restores_gtt(tmp_path, monkeypatch):
    k = FakeKite()
    k.fill_after = None
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120, "target": 20})
    hist = k.order_history

    def moving(oid):                    # the market rises after 3 s: the order follows the ask
        if k.polls[oid] == 3:
            k.ltp[SYM_S] = 51.0
        return hist(oid)
    k.order_history = moving
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    names = [n for n, _ in k.calls]
    assert "modify_order" in names and "cancel_order" in names
    assert next(kw for n, kw in k.calls if n == "modify_order")["price"] == pytest.approx(51.5)
    assert job["status"] == "not_filled"
    restored = [kw for n, kw in k.calls if n == "place_gtt"][-1]
    assert restored["trigger_values"] == [20.0, 120.0] and all(o["quantity"] == 65 for o in restored["orders"])
    assert len(k.gtts) == 1 and mod.ALERTS


def test_close_partial_restores_gtt_for_the_rest(tmp_path, monkeypatch):
    k = FakeKite()
    k.fill_after = None
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    orig_cancel = k.cancel_order

    def cancel_partial(**kw):           # 40 filled before the cancel
        k.history[kw["order_id"]]["filled"] = 40
        k.pos[SYM_S] = (-25, "NRML", 60.0)
        orig_cancel(**kw)
    k.cancel_order = cancel_partial
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert job["status"] == "partial" and job["filled"] == 40
    restored = [kw for n, kw in k.calls if n == "place_gtt"][-1]
    assert restored["orders"][0]["quantity"] == 25


def test_close_endpoint_guards(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    assert "changed" in c.post("/api/positions/close", json={"key": f"NFO:{SYM_L}:NRML", "qty": 130}).get_json()["error"]
    monkeypatch.setattr(mod, "_now", lambda: datetime(2026, 10, 29, 16, 0, tzinfo=mod.IST))
    assert "closed" in c.post("/api/positions/close", json={"key": f"NFO:{SYM_L}:NRML"}).get_json()["error"]
    assert not any(n == "place_order" for n, _ in k.calls)


def test_recovery_after_restart(tmp_path, monkeypatch):
    k = FakeKite()
    k.fill_after = None
    mod, c = _load(tmp_path, monkeypatch, k)
    oid = k.place_order(tradingsymbol=SYM_S, quantity=65, price=50.5, transaction_type="BUY", product="NRML", tag=mod.TAG)
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    job.update(order_id=oid, phase="chasing", saved_gtts=[{"type": "single", "trigger_values": [120.0], "id": 7,
                                        "orders": [{"transaction_type": "BUY", "quantity": 65, "order_type": "LIMIT", "product": "NRML", "price": 132.0}]}])
    mod._save_jobs()
    mod.JOBS.clear()
    mod.BUSY.clear()
    saved = json.loads((tmp_path / "data" / "positions_jobs.json").read_text())
    assert saved[0]["status"] == "working"
    mod._recover_one(saved[0], k)
    assert ("cancel_order", {"variety": "regular", "order_id": oid}) in k.calls
    assert k.calls[-1][0] == "place_gtt" and saved[0]["status"] == "not_filled"


def test_wiring():
    app = (ROOT / "server" / "portfolio" / "app.py").read_text()
    assert "from positions_panel import panel" in app
    users = (ROOT / "deploy" / "portfolio_page" / "users_panel.py").read_text()
    assert '"/positions", "/api/positions")' in users                      # owner only
    nav = (ROOT / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    assert '("Positions", "/positions")' in nav
    upd = (ROOT / "deploy" / "update_engine.sh").read_text()
    assert "positions_panel" in upd and 'NEW_OK=" positions_panel.py "' in upd and '"/positions"' in upd
    assert "server/portfolio/positions_panel.py" in (ROOT / "deploy" / "make_server_update.sh").read_text()


def test_close_refused_while_gtt_order_is_working(tmp_path, monkeypatch):
    """Review #2: a GTT that just triggered has an exit order working; a second exit must not be sent."""
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    k.orders_ = [{"order_id": "777", "tradingsymbol": SYM_S, "exchange": "NFO", "status": "OPEN", "transaction_type": "BUY",
                  "order_type": "LIMIT", "variety": "regular", "product": "NRML"}]
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    names = [n for n, _ in k.calls]
    assert "place_order" not in names and "delete_gtt" not in names and job["status"] == "failed"
    assert any("check Kite" in a[0] for a in mod.ALERTS)


def test_place_order_error_is_found_by_tag(tmp_path, monkeypatch):
    """Review #3: place_order timed out but the order reached Zerodha: it is followed, not forgotten."""
    k = FakeKite()
    k.place_raises = True
    mod, c = _load(tmp_path, monkeypatch, k)
    job = _job(mod, f"NFO:{SYM_L}:NRML", k)
    mod.run_close(job, k)
    assert job["order_id"] and job["status"] == "done" and k.pos[SYM_L][0] == 0


def test_lagging_positions_do_not_bring_the_stop_back(tmp_path, monkeypatch):
    """Review #1: Kite's position list still shows the old quantity for a moment after the fill."""
    k = FakeKite()
    k.lag_on_fill = 3
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert job["status"] == "done" and not k.gtts
    assert [n for n, _ in k.calls].count("place_gtt") == 1          # only the original one


def test_disagreeing_positions_put_nothing_back(tmp_path, monkeypatch):
    k = FakeKite()
    k.lag_on_fill = 50                 # never catches up
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert job["status"] == "failed" and [n for n, _ in k.calls].count("place_gtt") == 1
    assert any("check Kite" in a[0] for a in mod.ALERTS)


def test_unconfirmed_cancel_puts_nothing_back(tmp_path, monkeypatch):
    """Review #4: the cancel never confirms: the order may still fill, so no stop goes back next to it."""
    k = FakeKite()
    k.fill_after = None
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    k.cancel_order = lambda **kw: (_ for _ in ()).throw(RuntimeError("timeout"))
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert job["status"] == "failed" and [n for n, _ in k.calls].count("place_gtt") == 1


def test_all_exit_gtts_removed_and_protect_refuses_duplicates(tmp_path, monkeypatch):
    """Review #5: two GTTs on one contract (one set in Kite)."""
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    k.place_gtt(trigger_type="single", tradingsymbol=SYM_S, exchange="NFO", trigger_values=[150.0], last_price=50.0,
                orders=[{"transaction_type": "BUY", "quantity": 65, "order_type": "LIMIT", "product": "NRML", "price": 165.0}])
    assert "2 GTTs" in c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 110}).get_json()["error"]
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert job["status"] == "done" and not k.gtts


def test_key_includes_product_and_big_positions_locked(tmp_path, monkeypatch):
    k = FakeKite()
    k.pos["NIFTY26N0322000PE"] = (-1950, "NRML", 10.0)
    k.ltp["NIFTY26N0322000PE"] = 9.0
    mod, c = _load(tmp_path, monkeypatch, k)
    rows = {r["symbol"]: r for r in c.get("/api/positions").get_json()["positions"]}
    assert rows[SYM_M]["key"] == f"NFO:{SYM_M}:MIS"
    assert "Kite" in rows["NIFTY26N0322000PE"]["locked"] and not rows["NIFTY26N0322000PE"]["gtt_ok"]
    assert c.post("/api/positions/close", json={"key": "NFO:NIFTY26N0322000PE:NRML"}).status_code == 400


def test_unreadable_session_file_locks_fno(tmp_path, monkeypatch):
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    d = tmp_path / "engine" / "runtime" / "sessions" / "x"
    d.mkdir(parents=True)
    (d / "state.json").write_text("{half")
    rows = c.get("/api/positions").get_json()["positions"]
    assert all(r["locked"] for r in rows)


def test_recovery_runs_once(tmp_path, monkeypatch):
    """Review #10: two dashboard processes must not both put a stop back."""
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "positions_jobs.json").write_text("[]")
    monkeypatch.setattr(mod, "_RECOVERY_LOCK", [])
    assert mod.recover_pending() == [] and len(mod._RECOVERY_LOCK) == 1
    assert mod.recover_pending() == [] and len(mod._RECOVERY_LOCK) == 1     # second owner refused by the lock


def test_gtt_fill_between_reads_stops_the_close(tmp_path, monkeypatch):
    """Review 2 #1: the GTT triggers and fills while its delete is sent; positions still lag: no second exit."""
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    c.post("/api/positions/protect", json={"key": f"NFO:{SYM_S}:NRML", "sl": 120})
    real_delete = k.delete_gtt

    def fired(gid):
        real_delete(gid)
        k.orders_.append({"order_id": "888", "tradingsymbol": SYM_S, "exchange": "NFO", "status": "COMPLETE", "filled_quantity": 65,
                          "transaction_type": "BUY", "product": "NRML", "order_type": "LIMIT"})
    k.delete_gtt = fired
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert not any(n == "place_order" for n, _ in k.calls)


def test_equity_gtts_left_alone(tmp_path, monkeypatch):
    """Review 2 #2: a SELL GTT on an equity may protect holdings: not touched, and SL/target is F&O only."""
    k = FakeKite()
    k.pos = {"ITC": (10, "CNC", 400.0)}
    k.ltp = {"ITC": 410.0}
    orig = k.positions

    def eq():
        d = orig()
        for p in d["net"]:
            p["exchange"] = "NSE"
        return d
    k.positions = eq
    k.place_gtt(trigger_type="single", tradingsymbol="ITC", exchange="NSE", trigger_values=[380.0], last_price=410.0,
                orders=[{"transaction_type": "SELL", "quantity": 500, "order_type": "LIMIT", "product": "CNC", "price": 375.0}])
    k.calls.clear()
    mod, c = _load(tmp_path, monkeypatch, k)
    assert "F&O only" in c.post("/api/positions/protect", json={"key": "NSE:ITC:CNC", "sl": 390}).get_json()["error"]
    row = c.get("/api/positions").get_json()["positions"][0]
    assert not row["gtt_ok"]
    job = _job(mod, "NSE:ITC:CNC", k)
    job["exchange"] = "NSE"
    mod.run_close(job, k)
    assert not any(n == "delete_gtt" for n, _ in k.calls) and len(k.gtts) == 1


def test_restore_never_bigger_than_each_stop(tmp_path, monkeypatch):
    """Review 2 #3: a GTT for 25 units is put back for 25, not for the whole position."""
    k = FakeKite()
    k.fill_after = None
    mod, c = _load(tmp_path, monkeypatch, k)
    k.place_gtt(trigger_type="single", tradingsymbol=SYM_S, exchange="NFO", trigger_values=[120.0], last_price=50.0,
                orders=[{"transaction_type": "BUY", "quantity": 25, "order_type": "LIMIT", "product": "NRML", "price": 132.0}])
    job = _job(mod, f"NFO:{SYM_S}:NRML", k)
    mod.run_close(job, k)
    assert job["status"] == "not_filled"
    assert [kw for n, kw in k.calls if n == "place_gtt"][-1]["orders"][0]["quantity"] == 25


def test_lock_held_elsewhere_keeps_the_job_and_alerts(tmp_path, monkeypatch):
    """Review 2 #4: recovery cannot take the lock: the stuck close stays in the list and an alert goes out."""
    k = FakeKite()
    mod, c = _load(tmp_path, monkeypatch, k)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "positions_jobs.json").write_text(json.dumps([{"id": "s1", "key": f"NFO:{SYM_S}:NRML", "status": "working",
                                                                         "name": "stuck", "started": "2026-10-29T10:00:00", "steps": []}]))
    monkeypatch.setattr(mod, "_RECOVERY_LOCK", [])
    monkeypatch.setattr(mod, "_take_recovery_lock", lambda: False)
    started = []
    monkeypatch.setattr(mod.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: started.append((target, args))})())
    assert mod.recover_pending() == [] and "s1" in mod.JOBS
    target, args = started[0]
    target(*args, tries=1)
    assert any("check Zerodha" in a[0] for a in mod.ALERTS)
