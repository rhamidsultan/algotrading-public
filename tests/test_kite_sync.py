"""Live legs squared off by hand in Kite (or on the Positions page): synced, booked at that order's price, the strategy
keeps the other legs. Before this, a leg closed in Kite stopped the session ("Positions differ from Zerodha") and it
could only be reset."""
from datetime import timedelta

import pytest

from algolab.models import Contract, Quote
from test_live_next_day import TOMORROW, NextDay, _session_env
from test_platform import NOW, _live_broker

Q = Quote(20.0, bid=19.9, ask=20.1, source="market")
SYM = "NIFTY26O1324300CE"


def _short(lots=1):
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    b.s.max_lots = max(b.s.max_lots, lots)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65 * lots, Q, "Entry", "g")
    return k, b, c


def _by_hand(k, sym, side, qty, price, tag=None):
    """An order Hamid placed himself in Kite (no Algo Lab tag), filled."""
    oid = k.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type=side, quantity=qty,
                        product="NRML", order_type="LIMIT", price=price, tag=tag)
    return oid


def test_a_leg_squared_off_in_kite_is_booked_at_its_price():
    k, b, c = _short()
    told = []
    b.notify = lambda subj, body: told.append(body)
    stop = k.sent("SL")[0]
    _by_hand(k, SYM, "BUY", 65, 14.5)
    n = len(k.sent())
    b.check_positions(NOW)                                       # the minute check: no stop, no mismatch
    assert not b.positions and b.trades[-1].exit_price == 14.5 and b.trades[-1].reason == "Closed in Kite"
    assert stop["status"] == "CANCELLED" and len(k.sent()) == n  # its stop taken off; nothing bought by Algo Lab
    assert b.outside == [(c, 65)] and any("squared off in Kite, booked at 14.50" in t for t in told)
    b.check_positions(NOW)
    b.check_positions(NOW)                                       # nothing left to differ: no Blocked
    assert len(b.trades) == 1                                    # the same Kite order is not booked twice


def test_part_of_a_leg_squared_off_keeps_the_rest_with_its_own_stop():
    k, b, c = _short(lots=2)
    _by_hand(k, SYM, "BUY", 65, 15.0)
    b.check_positions(NOW)
    assert b.positions[c].qty == -65 and b.trades[-1].qty == 65 and b.trades[-1].exit_price == 15.0
    live = [o for o in k.sent("SL") if o["status"] == "TRIGGER PENDING"]
    assert len(live) == 1 and live[0]["qty"] == 65 and b.protect[c.label] == live[0]["order_id"]


def _closed_without_an_order(k):
    k.orders["x"] = {"order_id": "x", "sym": SYM, "side": "BUY", "qty": 65, "type": "LIMIT", "status": "COMPLETE",
                     "filled_quantity": 65, "average_price": 0, "product": "MIS", "tag": None}   # not a usable order


def test_without_a_kite_order_nothing_is_booked_by_itself_only_a_sync_books_an_estimate():
    from algolab.live import Blocked
    k, b, c = _short()
    k.ask = 16.0
    _closed_without_an_order(k)
    stop = k.sent("SL")[0]
    b.check_positions(NOW)                                       # e.g. one odd read: never booked by itself
    assert b.positions[c].qty == -65 and stop["status"] == "TRIGGER PENDING"
    with pytest.raises(Blocked, match="press Sync"):
        b.check_positions(NOW)                                   # as before: stopped, nothing sent, the stop stays
    assert stop["status"] == "TRIGGER PENDING" and not b.trades
    b.sync_outside(NOW, explicit=True)                           # Sync on the card
    t = b.trades[-1]
    assert not b.positions and t.reason == "Closed in Kite (price estimated)" and t.exit_price == 16.0
    assert stop["status"] == "CANCELLED"


def test_an_empty_positions_answer_is_never_taken_as_closed():
    k, b, c = _short()
    real = k.positions
    k.positions = lambda: {"net": []}
    _by_hand(k, SYM, "BUY", 1, 14.0)                             # even with a Kite order on the symbol
    assert b.sync_outside(NOW) == [] and b.positions[c].qty == -65
    k.positions = real


def test_a_kite_order_from_before_the_leg_was_opened_is_not_its_close():
    k, b, c = _short()
    _by_hand(k, SYM, "BUY", 65, 14.0)
    oid = next(o for o in k.orders.values() if o["tag"] is None)["order_id"]
    k.orders[oid]["order_timestamp"] = "2026-10-01 10:00:00"     # an earlier round trip of yours
    assert b.sync_outside(NOW) == [] and b.positions[c].qty == -65


def test_any_order_working_on_the_symbol_waits():
    k, b, c = _short()
    _by_hand(k, SYM, "BUY", 65, 14.0)
    k.orders["w"] = {"order_id": "w", "sym": SYM, "side": "BUY", "qty": 65, "type": "LIMIT", "status": "OPEN",
                     "filled_quantity": 0, "average_price": 0, "product": "NRML", "tag": None}   # yours, still working
    assert b.sync_outside(NOW) == [] and b.positions[c].qty == -65


def test_an_exit_right_after_a_square_off_in_kite_sends_nothing():
    """The strategy exits before the next check: the leg is booked from the Kite order, no buy-back (it would open a
    long)."""
    k, b, c = _short()
    b.reconcile(NOW)
    _by_hand(k, SYM, "BUY", 65, 14.0)
    n = len([o for o in k.sent() if o["side"] == "BUY" and o["type"] != "SL"])
    f = b.execute(NOW, c, "BUY", 65, Q, "Exit", "g")
    assert len([o for o in k.sent() if o["side"] == "BUY" and o["type"] != "SL"]) == n
    assert not b.positions and f.reason == "Closed in Kite" and f.price == 14.0


def test_an_exit_after_a_square_off_with_no_kite_order_is_blocked():
    from algolab.live import Blocked
    k, b, c = _short()
    b.reconcile(NOW)
    _closed_without_an_order(k)
    n = len(k.sent())
    with pytest.raises(Blocked, match="Not traded"):
        b.execute(NOW, c, "BUY", 65, Q, "Exit", "g")
    assert len(k.sent()) == n


def test_more_at_zerodha_or_the_other_way_round_is_not_synced():
    from algolab.live import Blocked
    k, b, c = _short()
    _by_hand(k, SYM, "SELL", 65, 20.0)                           # sold more by hand: not a square-off
    b.check_positions(NOW)
    with pytest.raises(Blocked, match="two checks in a row"):
        b.check_positions(NOW)
    assert b.positions[c].qty == -65 and not b.trades


def test_a_stop_fill_is_still_a_stop_fill():
    from algolab.live import Halt
    k, b, c = _short()
    k.sent("SL")[0].update(status="COMPLETE", filled_quantity=65, average_price=60.0)
    with pytest.raises(Halt, match="Protective stop filled"):
        b.check_positions(NOW)
    assert not b.positions and b.trades[-1].exit_price == 60.0 and "Kite" not in b.trades[-1].reason


def test_a_contract_another_live_session_holds_is_not_guessed():
    from algolab.live import Blocked
    k, b, c = _short()
    b.others = lambda: {SYM: -65}
    k.place_order(variety="regular", exchange="NFO", tradingsymbol=SYM, transaction_type="SELL", quantity=65,
                  product="NRML", order_type="LIMIT", price=20.0, tag="algolab-other")      # the other session's leg
    _by_hand(k, SYM, "BUY", 65, 14.0)                            # one of the two closed in Kite: whose?
    b.check_positions(NOW)
    with pytest.raises(Blocked):
        b.check_positions(NOW)
    assert b.positions[c].qty == -65


def test_a_leg_squared_off_overnight_is_booked_by_a_sync_in_the_morning():
    from algolab.live import Blocked
    k, b, c = _short()
    _by_hand(k, SYM, "BUY", 65, 12.0)                            # yesterday, by hand
    k.new_day()                                                  # today's order book no longer has it
    k.ask = 9.0
    with pytest.raises(Blocked, match="press Sync on the card"):
        b.reconcile(TOMORROW)                                    # no order to match: not booked by itself
    assert b.sync_outside(TOMORROW, explicit=True) == []         # Zerodha shows nothing at all: pressed again
    b.sync_outside(TOMORROW, explicit=True)
    b.reconcile(TOMORROW)
    t = b.trades[-1]
    assert not b.positions and b.reconciled and t.reason == "Closed in Kite (price estimated)" and t.exit_price == 9.0
    assert not k.sent("SL")                                      # no new stop for a leg that is gone


def test_a_sync_request_books_at_once():
    k, b, c = _short()
    _by_hand(k, SYM, "BUY", 65, 13.0)
    done = b.sync_outside(NOW, explicit=True)
    assert len(done) == 1 and not b.positions and b.trades[-1].exit_price == 13.0


def test_the_session_syncs_and_the_strategy_keeps_the_other_leg(tmp_path, monkeypatch):
    """End to end (premium strangle, live): the CE is squared off in Kite; after a restart the session books it and
    keeps the PE: it used to exit the PE too ("A leg is missing") or, before that, stop the session."""
    from datetime import date

    from algolab import paper
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    ce = next(p for p in s["positions"] if p["Contract"].endswith("CE"))
    sym = next(o["sym"] for o in fake.sent() if o["side"] == "SELL" and o["sym"].endswith("CE"))
    _by_hand(fake, sym, "BUY", abs(int(ce["Qty"])), 5.0)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert [p["Contract"][-2:] for p in s["positions"]] == ["PE"], s["logs"][-8:]
    assert paper.read_desired(sid) is not None                  # still running
    assert any("squared off in Kite: the other leg is kept" in line for line in s["logs"])
    net = {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"] if p["quantity"]}
    assert sym not in net and len(net) == 1                     # Algo Lab sent nothing for the CE


def test_a_stopped_session_can_be_synced_without_trading(tmp_path, monkeypatch):
    from datetime import date

    from algolab import paper
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    paper.kill(sid) if False else paper.Slot(sid).desired.unlink()     # stopped, legs still held
    sym = next(o["sym"] for o in fake.sent() if o["side"] == "SELL" and o["sym"].endswith("CE"))
    _by_hand(fake, sym, "BUY", 65, 5.0)
    d = paper.read_state(sid)
    desired = {"strategy": d["strategy"], "params": d["params"], "feed": "kite", "mode": "live",
               "live": {"max_lots": 1, "daily_loss_limit": 3000}, "sync_only": True}
    paper.Slot(sid).mk()
    import json
    paper.Slot(sid).desired.write_text(json.dumps(desired))     # what Sync on the card writes
    n = len(fake.sent())
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert [p["Contract"][-2:] for p in s["positions"]] == ["PE"] and paper.read_desired(sid) is None
    assert "Synced with Zerodha" in paper.session_status(sid)
    assert len(fake.sent()) == n                                # no order sent


def test_intraday_strangle_does_not_re_enter_a_leg_closed_in_kite():
    from algolab.strategies.intraday_strangle import IntradayStrangle

    class Ctx:
        positions = {}
        logs = []

        def log(self, m):
            self.logs.append(m)
    st = IntradayStrangle()
    c = Contract("NIFTY", NOW.date(), 24300, "CE")
    st.day = {"date": NOW.date().isoformat(), "legs": {"CE": {"strike": 24300, "open": True, "reentries": 0, "first": 20,
                                                           "sold": 20, "stop": 30}}}
    st.on_outside_close(Ctx(), c, 65)
    leg = st.day["legs"]["CE"]
    assert leg["open"] is False and leg["reentries"] == st.p.reentries and leg["manual"]


def test_premium_strangle_keeps_the_other_leg():
    from algolab.strategies.premium_strangle import PremiumStrangle
    st = PremiumStrangle()
    st.cycle = {"group": "001", "expiry": NOW.date().isoformat(), "ce": 24300, "pe": 23700, "rolls": 0, "start_realized": 0}

    class Ctx:
        positions = {}

        def log(self, m):
            pass
    st.on_outside_close(Ctx(), Contract("NIFTY", NOW.date(), 24300, "CE"), 65)
    assert st.cycle["manual"] == ["CE"]


def test_dashboard_sync_route(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import json

    from test_defined_risk import _panel
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    c = a.test_client()
    d = mod.SESS / "premium_strangle-3"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"strategy": "premium_strangle", "mode": "live", "params": {"lots": 1},
                                              "live": {"max_lots": 1, "daily_loss_limit": 3000, "mode": "live"},
                                              "label": "PS", "positions": [{"Contract": "x"}]}))
    r = c.post("/api/paper/sync", json={"id": "premium_strangle-3"}).get_json()   # stopped: started only to sync
    want = json.loads((d / "desired.json").read_text())
    assert r["started"] and want["sync_only"] and want["mode"] == "live" and want["live"] == {"max_lots": 1, "daily_loss_limit": 3000}
    r = c.post("/api/paper/sync", json={"id": "premium_strangle-3"}).get_json()   # running: a request file
    assert r["started"] is False and (d / "SYNC").exists()
    p = mod.SESS / "iron_fly"
    p.mkdir()
    (p / "state.json").write_text(json.dumps({"strategy": "iron_fly", "mode": "paper"}))
    assert c.post("/api/paper/sync", json={"id": "iron_fly"}).status_code == 400


def test_a_sync_only_run_never_squares_off(tmp_path, monkeypatch):
    """Sync on a stopped session whose stop filled meanwhile: booked as the stop's fill and it stops; the other leg is
    not bought back (the sync sends no trades)."""
    from datetime import date

    from algolab import paper
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    paper.Slot(sid).desired.unlink()                            # stopped, legs still held
    stop = next(o for o in fake.orders.values() if o["type"] == "SL" and o["sym"].endswith("CE"))
    stop.update(status="COMPLETE", filled_quantity=stop["qty"], average_price=40.0)
    d = paper.read_state(sid)
    import json
    paper.Slot(sid).desired.write_text(json.dumps({"strategy": d["strategy"], "params": d["params"], "feed": "kite",
                                                   "mode": "live", "live": {"max_lots": 1, "daily_loss_limit": 3000},
                                                   "sync_only": True}))
    n = len(fake.sent())
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert len(fake.sent()) == n and paper.read_desired(sid) is None
    assert [p["Contract"][-2:] for p in s["positions"]] == ["PE"]


def test_a_kite_order_booked_by_one_session_is_not_booked_by_another():
    """The account's live sessions share one record of the Kite orders booked (paper.Group): an order already used
    by one session is not taken as another session's close."""
    k, b, c = _short()
    _by_hand(k, SYM, "BUY", 65, 14.0)
    oid = next(o for o in k.orders.values() if o["tag"] is None)["order_id"]
    b.synced[oid] = 65                                          # booked by another live session
    assert b.sync_outside(NOW) == [] and b.positions[c].qty == -65


def test_sync_is_refused_while_a_session_is_still_closing(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import json

    from test_defined_risk import _panel
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    d = mod.SESS / "premium_strangle-4"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"strategy": "premium_strangle", "mode": "live", "closing_only": True,
                                              "halted": "Stopped", "positions": [{"Contract": "x"}]}))
    (d / "desired.json").write_text(json.dumps({"strategy": "premium_strangle", "mode": "live"}))
    assert a.test_client().post("/api/paper/sync", json={"id": "premium_strangle-4"}).status_code == 400
    assert not (d / "SYNC").exists()


def test_a_sync_with_no_positions_at_zerodha_books_only_when_pressed_again():
    """Zerodha's positions answer empty (e.g. outside market hours): one Sync press books nothing and keeps the stops;
    a second press (every leg really closed on an earlier day) books them."""
    k, b, c = _short()
    real = k.positions
    k.positions = lambda: {"net": []}
    assert b.sync_outside(NOW, explicit=True) == [] and b.positions[c].qty == -65
    assert k.sent("SL")[0]["status"] == "TRIGGER PENDING"
    assert len(b.sync_outside(NOW, explicit=True)) == 1 and not b.positions
    k.positions = real


def test_two_sync_presses_on_a_stopped_card_book_legs_closed_on_an_earlier_day(tmp_path, monkeypatch):
    """Every leg closed in Kite yesterday: Zerodha shows no positions. The first Sync books nothing; the second (each
    a new sync-only run) books them, estimated."""
    import json
    from datetime import date

    from algolab import paper
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    paper.Slot(sid).desired.unlink()                            # stopped, legs held
    for o in [o for o in fake.orders.values() if o["side"] == "SELL" and o["type"] != "SL"]:
        _by_hand(fake, o["sym"], "BUY", o["filled_quantity"], 5.0)
    fake.new_day()                                              # a day later: no positions, no orders today
    clock[0] = clock[0] + timedelta(days=1)
    d = paper.read_state(sid)
    desired = {"strategy": d["strategy"], "params": d["params"], "feed": "kite", "mode": "live",
               "live": {"max_lots": 1, "daily_loss_limit": 3000}, "sync_only": True}
    paper.Slot(sid).desired.write_text(json.dumps(desired))
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert len(paper.read_state(sid)["positions"]) == 2 and paper.read_desired(sid) is None
    paper.Slot(sid).desired.write_text(json.dumps(desired))     # pressed again
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["positions"] and paper.read_desired(sid) is None, s["logs"][-6:]
    assert all(t["reason"] == "Closed in Kite (price estimated)" for t in s["broker"]["trades"])
