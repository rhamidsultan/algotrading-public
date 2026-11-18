"""A leg held overnight must still close the next day. Protective stops are DAY orders: the next morning Zerodha no
longer knows their order ids ("Couldn't find that order_id") and its order book shows only today's orders. Before this
fix every buy-back of such a leg failed on that error, every second, with no alert and no square-off."""
from datetime import timedelta

import pytest

from algolab.models import Contract, Quote
from test_platform import NOW, FakeOrders, _live_broker, _OrderBook

TOMORROW = NOW + timedelta(days=1)


class _Book(_OrderBook):
    """The order dict; called like Kite's orders(), it can fail or come back empty (a lagging order book)."""

    def __call__(self):
        if self.fake.book_error:
            raise self.fake.book_error
        return [] if self.fake.book_lags else super().__call__()


class NextDay(FakeOrders):
    """FakeOrders that can start a new trading day: yesterday's orders leave the order book and their ids are no
    longer known, while the positions they built are carried (NRML)."""

    def __init__(self, expiry):
        super().__init__(expiry)
        self.old = {}
        self.history_error = None
        self.positions_error = None
        self.book_error = None
        self.book_lags = False                     # today's order book comes back empty (Zerodha trouble)
        self.orders = _Book()
        self.orders.fake = self
        self.ask = 18.0

    def new_day(self):
        for oid in list(self.orders):
            self.old[oid] = self.orders.pop(oid)

    def order_history(self, oid):
        if self.history_error:
            raise self.history_error
        if oid in self.old:
            raise Exception("Couldn't find that `order_id`.")
        return super().order_history(oid)

    def cancel_order(self, variety, order_id, **k):
        if order_id in self.old:
            raise Exception("Couldn't find that `order_id`.")
        return super().cancel_order(variety, order_id, **k)

    def quote(self, symbols):
        out = super().quote(symbols)                       # the index as FakeKite gives it
        if self.ask is not None:                           # options at a set price (protective stop tests)
            out.update({s: {"last_price": self.ask, "depth": {"buy": [{"price": self.ask - 0.2}], "sell": [{"price": self.ask}]}}
                        for s in symbols if not s.startswith("NSE:")})
        return out

    def positions(self):
        if self.positions_error:
            raise self.positions_error
        held = {}
        for o in list(self.old.values()):
            if o["filled_quantity"]:
                held[o["sym"]] = held.get(o["sym"], 0) + (o["filled_quantity"] if o["side"] == "BUY" else -o["filled_quantity"])
        today = set()
        for p in super().positions()["net"]:
            held[p["tradingsymbol"]] = held.get(p["tradingsymbol"], 0) + p["quantity"]
            today.add(p["tradingsymbol"])
        return {"net": [{"tradingsymbol": s, "exchange": "NFO", "product": "NRML", "quantity": q}   # as Kite: a position
                        for s, q in held.items() if q or s in today]}                            # closed today stays at 0


def _overnight_short():
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    old_stop = b.protect[c.label]
    k.new_day()
    return k, b, c, old_stop


def test_a_leg_held_overnight_is_bought_back_the_next_day():
    k, b, c, old_stop = _overnight_short()
    f = b.execute(TOMORROW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and f.price == 10.1
    buys = [o for o in k.sent() if o["side"] == "BUY"]
    assert len(buys) == 1 and buys[0]["qty"] == 65                       # bought back once
    stops = k.sent("SL")                                                 # today's orders only
    assert len(stops) == 1 and stops[0]["status"] == "CANCELLED"         # the new day's stop, taken off for the exit
    assert not b.protect and old_stop not in k.orders


def test_the_new_day_puts_a_fresh_stop_at_zerodha_for_a_short_leg():
    k, b, c, old_stop = _overnight_short()
    b.reconcile(TOMORROW)
    new = k.sent("SL")
    assert len(new) == 1 and new[0]["status"] == "TRIGGER PENDING" and new[0]["qty"] == 65
    assert new[0]["trigger"] == pytest.approx(59.7)                      # yesterday's trigger
    k2, b2, c2, _ = _overnight_short()
    k2.ask = 60.0                                                        # the market opened above it
    b2.reconcile(TOMORROW)
    assert k2.sent("SL")[0]["trigger"] == pytest.approx(72.0)            # above the market (a buy stop must be)
    assert b.protect[c.label] == new[0]["order_id"] != old_stop
    assert b.reconciled and b.reconciled_on == TOMORROW.date().isoformat()


def test_the_stop_check_renews_a_stop_gone_with_an_earlier_day():
    k, b, c, old_stop = _overnight_short()
    b.reconciled_on = TOMORROW.date().isoformat()                        # as if checked today already
    b.check_protective_stops(TOMORROW)                                   # used to raise "Couldn't find that order_id"
    assert b.protect[c.label] != old_stop and k.sent("SL")[0]["status"] == "TRIGGER PENDING"
    assert b.positions[c].qty == -65


def test_a_stop_that_filled_before_the_close_is_never_bought_again():
    from algolab.live import Blocked
    k, b, c, old_stop = _overnight_short()
    k.old[old_stop].update(status="COMPLETE", filled_quantity=65, average_price=58.0)   # filled after the last check
    with pytest.raises(Blocked, match="differ from Zerodha"):
        b.execute(TOMORROW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not k.sent() and not k.sent("SL")                             # nothing sent: no long position opened
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()   # even if the day's check was skipped
    with pytest.raises(Blocked, match="may have filled"):
        b.execute(TOMORROW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not k.sent()


def test_a_read_error_in_the_stop_check_does_not_raise():
    k, b, c, _ = _overnight_short()
    b.reconcile(TOMORROW)
    k.history_error = Exception("Gateway timed out")
    b.check_protective_stops(TOMORROW)                                   # logged, not raised: the strategy still runs
    assert b.protect[c.label] == k.sent("SL")[0]["order_id"]


def test_an_order_from_an_earlier_day_does_not_block_the_next_morning():
    k, b, c, _ = _overnight_short()
    b.working["algolabold1"] = {"sym": "NIFTY26O1324300CE", "side": "BUY", "qty": 65, "label": c.label,
                                "group": "g", "oid": "1", "booked": 0}
    k.old["1"]["status"] = "CANCELLED"
    b.reconcile(TOMORROW)                                                # used to stop: "still working at Zerodha"
    assert not b.working and b.reconciled


def test_stopping_a_session_with_a_gone_stop():
    k, b, c, old = _overnight_short()
    b.positions.clear()                                                  # the session holds nothing...
    problems = b.cancel_all_protective(TOMORROW)                         # ...but Zerodha still has the short
    assert len(problems) == 1 and "Zerodha holds -65" in problems[0] and not b.protect
    k2, b2, c2, old2 = _overnight_short()
    k2.old[old2].update(status="COMPLETE", filled_quantity=65, average_price=58.0)   # its stop bought it back
    b2.positions.clear()
    assert b2.cancel_all_protective(TOMORROW) == [] and not b2.protect   # flat at Zerodha too: nothing to check


def test_a_new_stop_can_be_trailed_again():
    k, b, c, _ = _overnight_short()
    b._follow_n[c.label] = b.FOLLOW_MAX                                  # yesterday's stop was moved its limit
    b.reconcile(TOMORROW)
    assert c.label not in b._follow_n


def _session_env(tmp_path, monkeypatch, fake):
    from datetime import datetime

    from algolab import kite, paper
    from algolab.feeds import OptionStore
    from test_platform import _paper_tmp
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    return cfg, clock, sid, want


def test_a_check_that_cannot_read_zerodha_does_not_stop_the_strategy(tmp_path, monkeypatch):
    from datetime import date

    from algolab import paper
    from algolab.live import LiveBroker
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None                                                # FakeKite's option prices
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)

    def broken(self, now, from_stream=False):
        raise RuntimeError("Gateway timed out")
    monkeypatch.setattr(LiveBroker, "check_protective_stops", broken)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert len(s["positions"]) == 2                               # the strategy ran (it entered) despite the error
    assert any("Zerodha: protective stops not checked" in line for line in s["logs"])


def test_overnight_legs_close_the_next_day_end_to_end(tmp_path, monkeypatch):
    from datetime import date, datetime

    from algolab import paper
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None                                                # FakeKite's option prices
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert len(paper.read_state(sid)["positions"]) == 2
    fake.new_day()                                                # the next morning
    clock[0] = datetime(2026, 10, 8, 9, 30)
    paper.kill(sid)                                               # Stop & square off
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["positions"] and paper.read_desired(sid) is None, paper.session_status(sid)
    assert "squared off" in paper.session_status(sid)
    net = {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"] if p["quantity"]}
    assert not net                                                # flat at Zerodha: each leg bought back once
    assert not any(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL"))


# ---- found by the order-safety review (17 Nov) -------------------------------------------------------------------
def _today_short():
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    return k, b, c


def test_a_stop_placed_today_is_never_taken_as_gone():
    """H1: a read timeout plus an empty (lagging) order book must not look like a stop of an earlier day."""
    k, b, c = _today_short()
    stop = b.protect[c.label]
    k.history_error, k.book_lags = Exception("Gateway timed out"), True
    b.check_protective_stops(NOW)
    k.history_error = Exception("Couldn't find that `order_id`.")         # even this answer, for today's order
    b.check_protective_stops(NOW)
    assert b.protect[c.label] == stop and len(k.sent("SL")) == 1          # never a second stop
    from algolab.live import Halt
    with pytest.raises(Exception):                                        # an exit cannot pass it by either
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not [o for o in k.sent() if o["side"] == "BUY"] and k.sent("SL")[0]["status"] == "TRIGGER PENDING"
    k.history_error, k.book_lags = None, False
    b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and k.sent("SL")[0]["status"] == "CANCELLED"


def test_a_gone_stop_stays_tracked_until_a_new_one_is_placed():
    """H2: a positions read that fails while a gone stop is handled leaves it to the next check, never unprotected."""
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    k.positions_error = Exception("Gateway timed out")
    b.check_protective_stops(TOMORROW)
    assert b.protect[c.label] == old and not k.sent("SL")                # kept: tried again at the next check
    k.positions_error = None
    b.check_protective_stops(TOMORROW)
    assert b.protect[c.label] == k.sent("SL")[0]["order_id"] and k.sent("SL")[0]["status"] == "TRIGGER PENDING"


def test_a_stop_that_cannot_be_placed_again_is_retried_and_told_once():
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    b.PENDING_FORGET = 0                                                 # a refused placement: ruled out at once
    b.GONE_CONFIRM = 0                                                   # a difference counts at the second check
    told = []
    b.notify = lambda subj, body: told.append(subj)
    real = k.place_order
    k.place_order = lambda **kw: (_ for _ in ()).throw(Exception("RMS: blocked"))
    b.check_protective_stops(TOMORROW)
    b.check_protective_stops(TOMORROW)
    assert c.label in b.renew and c.label not in b.protect
    assert told.count("Protective stop NOT renewed") == 1
    k.place_order = real
    b.check_protective_stops(TOMORROW)
    assert b.protect[c.label] == k.sent("SL")[0]["order_id"] and not b.renew


def test_a_filled_stop_still_stops_the_session_when_another_stop_is_gone():
    """H2: a stop that filled and a gone stop in the same check: the Halt is not lost."""
    from algolab.live import Halt
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    ce, pe = Contract("NIFTY", e, 24300, "CE"), Contract("NIFTY", e, 23700, "PE")
    b.execute(NOW, ce, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    k.new_day()                                                           # the CE was held overnight
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    b._today(TOMORROW)
    b.execute(TOMORROW, pe, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    pe_stop = b.protect[pe.label]
    k.orders[pe_stop].update(status="COMPLETE", filled_quantity=65, average_price=60.0)
    with pytest.raises(Halt, match="Protective stop filled"):
        b.check_protective_stops(TOMORROW)
    assert pe not in b.positions and ce.label in b.protect                # the CE's stop is read again at square-off


def test_a_two_leg_exit_takes_no_stop_off_before_checking_a_gone_one():
    """H3: one leg's stop is current, the other's ended yesterday and may have filled: Blocked before anything moves."""
    from algolab.live import Blocked
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    ce, pe = Contract("NIFTY", e, 24300, "CE"), Contract("NIFTY", e, 23700, "PE")
    b.execute(NOW, pe, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    pe_old = b.protect[pe.label]
    k.new_day()
    k.old[pe_old].update(status="COMPLETE", filled_quantity=65, average_price=58.0)   # filled at yesterday's close
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    b._today(TOMORROW)
    b.execute(TOMORROW, ce, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    ce_stop = k.orders[b.protect[ce.label]]
    q = Quote(10.0, bid=9.9, ask=10.1, source="market")
    with pytest.raises(Blocked):
        b.execute_many(TOMORROW, [(ce, "BUY", 65, q, "Exit", "g"), (pe, "BUY", 65, q, "Exit", "g")])
    assert ce_stop["status"] == "TRIGGER PENDING" and not [o for o in k.sent() if o["side"] == "BUY"]


def test_a_lagging_position_read_is_read_again_before_blocking():
    """H4: Zerodha's positions a moment behind: read again, then buy back once."""
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    real, calls = k.positions, []

    def lagging():
        calls.append(1)
        out = real()
        return {"net": []} if len(calls) == 1 else out                    # the first read misses the position
    k.positions = lagging
    b.execute(TOMORROW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and len([o for o in k.sent() if o["side"] == "BUY"]) == 1 and len(calls) >= 2


def test_a_mismatch_in_the_morning_still_renews_the_stops_of_the_legs_that_match():
    """M1: one leg differs from Zerodha: the session stops, but the other short leg gets its stop back first."""
    from algolab.live import Blocked
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    ce, pe = Contract("NIFTY", e, 24300, "CE"), Contract("NIFTY", e, 23700, "PE")
    for c in (ce, pe):
        b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    pe_old = b.protect[pe.label]
    k.new_day()
    k.old[pe_old].update(status="COMPLETE", filled_quantity=65, average_price=58.0)
    with pytest.raises(Blocked, match="NO protective stop at Zerodha: NIFTY 13 Oct 26 23700 PE"):
        b.reconcile(TOMORROW)
    assert [o["sym"] for o in k.sent("SL")] == ["NIFTY26O1324300CE"] and b.protect[ce.label] == k.sent("SL")[0]["order_id"]


def test_a_leg_past_its_expiry_does_not_stop_the_session_next_morning():
    """M2: a leg left to settle at expiry is settled by the engine the next morning, not reported as a mismatch."""
    k = NextDay(NOW.date())                                                # expires today
    b, e = _live_broker(k)
    c = Contract("NIFTY", NOW.date(), 24300, "CE")
    b.symbol_of = lambda x: None if x.expiry < TOMORROW.date() and b._day >= TOMORROW.date().isoformat() else "NIFTYEXP24300CE"
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    k.new_day()
    k.old = {}                                                             # settled at Zerodha: gone from positions
    b.reconcile(TOMORROW)                                                  # used to raise "Positions differ"
    b.check_protective_stops(TOMORROW)
    assert b.reconciled and not b.protect and not k.sent("SL")


def test_selling_more_of_a_short_leg_keeps_one_stop_for_all_of_it():
    """Found by the review: a second sale used to leave the first stop at Zerodha untracked (two could fill)."""
    k, b, c = _today_short()
    b.s.max_lots = 2
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Grid sell", "g")
    live = [o for o in k.sent("SL") if o["status"] == "TRIGGER PENDING"]
    assert len(live) == 1 and live[0]["qty"] == 130 and b.protect[c.label] == live[0]["order_id"]


def test_a_stop_that_filled_in_part_and_was_cancelled_is_booked():
    from algolab.live import Halt
    k, b, c = _today_short()
    k.orders[b.protect[c.label]].update(status="CANCELLED", filled_quantity=25, average_price=60.0)
    with pytest.raises(Halt, match="Protective stop filled"):
        b.check_protective_stops(NOW)
    assert b.positions[c].qty == -40


def test_zerodha_failing_for_two_minutes_sends_one_alert(tmp_path, monkeypatch):
    from datetime import date

    from algolab import paper
    from algolab.live import LiveBroker
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(paper.Session, "FAILING_ALERT_AFTER", 0)
    monkeypatch.setattr(paper.Session, "STOPS_EVERY", 0)             # a full stop check at every step

    def broken(self, now, from_stream=False):
        raise RuntimeError("Gateway timed out")
    monkeypatch.setattr(LiveBroker, "check_protective_stops", broken)
    paper.run_group(cfg, want, "kite", max_bars=3)
    logs = paper.read_state(sid)["logs"]
    assert sum("ALERT: Algo Lab cannot check Zerodha" in line for line in logs) == 1


# ---- found by the second order-safety review (17 Nov) ------------------------------------------------------------
def test_a_stop_whose_answer_was_lost_is_adopted_never_placed_twice():
    """C1: Zerodha took the stop but the answer timed out: it is found by its own tag; retries never pile up stops."""
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    real = k.place_order

    def took_it_but_timed_out(**kw):
        real(**kw)
        raise Exception("Read timed out")
    k.place_order = took_it_but_timed_out
    for _ in range(3):
        b.check_protective_stops(TOMORROW)
    live = [o for o in k.sent("SL") if o["status"] == "TRIGGER PENDING"]
    assert len(live) == 1 and b.protect[c.label] == live[0]["order_id"] and live[0]["tag"].startswith("algolab-stop")


def test_no_second_stop_while_it_cannot_be_told_whether_the_first_got_there():
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    real = k.place_order

    def took_it_but_timed_out(**kw):
        real(**kw)
        k.book_error = Exception("Gateway timed out")                    # and the order book cannot be read either
        raise Exception("Read timed out")
    k.place_order = took_it_but_timed_out
    b.check_protective_stops(TOMORROW)                                    # the gone stop is renewed: answer lost
    k.place_order = real
    b._renew_stops()
    b._renew_stops()
    assert len(k.sent("SL")) == 1 and c.label in b.pending_stop          # waits: never a second one
    k.book_error = None
    b._renew_stops()                                                      # the book is back: the first is adopted
    assert len(k.sent("SL")) == 1 and b.protect[c.label] == k.sent("SL")[0]["order_id"] and not b.pending_stop


def test_a_stop_is_never_placed_again_on_a_leg_closed_in_kite():
    """Second review H1(a): placing a stop again first checks Zerodha still holds the leg."""
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    b.PENDING_FORGET = 0                                                 # a refused placement: ruled out at once
    told = []
    b.notify = lambda subj, body: told.append(subj)
    real = k.place_order
    k.place_order = lambda **kw: (_ for _ in ()).throw(Exception("RMS: blocked"))
    b.check_protective_stops(TOMORROW)                                    # cannot be placed: kept to try again
    assert c.label in b.renew
    k.place_order = real
    k.place_order(variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1324300CE", transaction_type="BUY",
                  quantity=65, product="NRML", order_type="LIMIT", price=20.0)   # bought back by hand in Kite
    from algolab.live import Blocked
    b.check_protective_stops(TOMORROW)                                    # positions may lag: not decided yet
    b.check_protective_stops(TOMORROW)                                    # still within 20 s: not decided yet
    assert not k.sent("SL") and c.label in b.renew
    b._gone_odd = {k_: t - 21 for k_, t in b._gone_odd.items()}           # the difference has lasted 21 s
    with pytest.raises(Blocked, match="No stop was placed again"):        # real: stops without trading
        b.check_protective_stops(TOMORROW)
    assert not k.sent("SL") and c.label not in b.renew


def test_a_retry_on_a_new_day_puts_no_stop_on_a_leg_zerodha_no_longer_holds():
    """Second review H1(b): retry_close on the CE must not renew the PE's stop when the PE's stop filled yesterday."""
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    ce, pe = Contract("NIFTY", e, 24300, "CE"), Contract("NIFTY", e, 23700, "PE")
    for c in (ce, pe):
        b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    pe_old = b.protect[pe.label]
    k.new_day()
    k.old[pe_old].update(status="COMPLETE", filled_quantity=65, average_price=58.0)
    try:
        b.retry_close(TOMORROW, ce, Quote(10.0, bid=9.9, ask=10.1, source="market"))
    except Exception:  # noqa: BLE001 - whatever the CE does, the PE must get no stop
        pass
    from algolab.live import Blocked
    for _ in range(3):
        try:
            b._renew_stops()
        except Blocked:                                                   # the PE is not held: the session stops
            break
    assert not [o for o in k.sent("SL") if o["sym"] == "NIFTY26O1323700PE"]


def test_a_morning_mismatch_names_every_leg_without_a_stop():
    """Second review H2: a leg that differs keeps yesterday's stop id but has no stop at Zerodha: it is named."""
    from algolab.live import Blocked
    k, b, c, old = _overnight_short()
    k.old[old].update(status="CANCELLED", filled_quantity=25, average_price=58.0)    # part filled, then expired
    with pytest.raises(Blocked, match="NO protective stop at Zerodha: NIFTY 13 Oct 26 24300 CE"):
        b.reconcile(TOMORROW)


def test_stop_reads_failing_for_two_minutes_send_one_alert(tmp_path, monkeypatch):
    """Second review M1: stops that cannot be read at the full checks count, whatever happens in between."""
    from datetime import date

    from algolab import paper
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)                        # enters: two short legs with stops
    monkeypatch.setattr(paper.Session, "FAILING_ALERT_AFTER", 0)
    monkeypatch.setattr(paper.Session, "STOPS_EVERY", 0)
    fake.history_error = Exception("Gateway timed out")
    paper.run_group(cfg, want, "kite", max_bars=3)
    logs = paper.read_state(sid)["logs"]
    assert sum("ALERT: Algo Lab cannot check Zerodha" in line for line in logs) == 1
    assert len(paper.read_state(sid)["positions"]) == 2


def test_a_stop_saved_before_stops_were_dated_is_kept_if_zerodha_still_has_it():
    """Second review L1: the day this is deployed, a stop placed earlier that day has no date: asked, not dropped."""
    k, b, c = _today_short()
    stop = b.protect[c.label]
    b.protect_day.clear()
    b.reconciled = False
    k.book_lags = True                                                     # the order book is late at the restart
    b.reconcile(NOW)
    assert b.protect[c.label] == stop and len(k.sent("SL")) == 1


def test_a_filled_stop_found_while_resizing_stops_the_session():
    """Second review L4."""
    from algolab.live import Halt
    k, b, c = _today_short()
    b.s.max_lots = 2
    k.orders[b.protect[c.label]].update(status="COMPLETE", filled_quantity=65, average_price=60.0)
    with pytest.raises(Halt, match="Protective stop filled"):
        b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Grid sell", "g")


# ---- found by the third order-safety review (17 Nov) -------------------------------------------------------------
def _short_with_lost_stop():
    """A sale whose protective stop reached Zerodha, but the answer was lost and the order book could not be read."""
    k = NextDay(NOW.date() + timedelta(days=7))
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24300, "CE")
    real = k.place_order

    def lost(**kw):
        oid = real(**kw)
        if kw.get("order_type") == "SL":
            k.book_error = Exception("Gateway timed out")
            raise Exception("Read timed out")
        return oid
    k.place_order = lost
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    k.place_order = real
    assert c.label in b.pending_stop and c.label not in b.protect and c.label in b.renew
    return k, b, c


def test_no_buy_back_while_a_lost_stop_may_be_live():
    from algolab.live import CancelFailed
    k, b, c = _short_with_lost_stop()
    with pytest.raises(CancelFailed, match="NOT bought back"):
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not [o for o in k.sent() if o["side"] == "BUY"]
    k.book_error = None                                                   # the book can be read again
    b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and len([o for o in k.sent() if o["side"] == "BUY"]) == 1
    assert [o["status"] for o in k.sent("SL")] == ["CANCELLED"]           # the lost stop was found and cancelled


def test_a_lost_stop_is_found_at_the_next_check():
    k, b, c = _short_with_lost_stop()
    k.book_error = None
    b.check_protective_stops(NOW)
    assert b.protect[c.label] == k.sent("SL")[0]["order_id"] and not b.pending_stop and len(k.sent("SL")) == 1


def test_stopping_a_flat_session_cancels_a_lost_stop():
    k, b, c = _short_with_lost_stop()
    b.positions.clear()
    k.book_error = None
    b.cancel_all_protective(NOW)
    assert [o["status"] for o in k.sent("SL")] == ["CANCELLED"] and not b.pending_stop
    k2, b2, c2 = _short_with_lost_stop()
    b2.positions.clear()
    problems = b2.cancel_all_protective(NOW)                               # still unreadable: named for Kite
    assert problems and "cancel it in Kite" in problems[0]


def test_the_stop_read_count_is_reset_when_no_stops_are_left():
    k, b, c = _today_short()
    k.history_error = Exception("Gateway timed out")
    b.check_protective_stops(NOW)
    assert b.stop_reads_failed == 1
    b.protect.clear()
    b.check_protective_stops(NOW)
    assert b.stop_reads_failed == 0


# ---- found by the fourth order-safety review (17 Nov) ------------------------------------------------------------
def test_a_lost_stop_that_filled_is_booked_not_bought_again():
    """A stop whose answer was lost then filled: found, booked as the stop's fill, the session stops; no second buy."""
    from algolab.live import Halt
    k, b, c = _short_with_lost_stop()
    sl = k.sent("SL")[0]
    sl.update(status="COMPLETE", filled_quantity=65, average_price=60.0)
    k.book_error = None
    with pytest.raises(Halt):
        for _ in range(4):
            b.check_protective_stops(NOW)
    assert not b.positions and b.trades[-1].exit_price == 60.0             # booked at the stop's fill
    assert not [o for o in k.sent() if o["side"] == "BUY"]                  # never bought a second time
    assert not {p["tradingsymbol"]: p["quantity"] for p in k.positions()["net"] if p["quantity"]}   # flat at Zerodha


class _SizedBook(_Book):
    def __call__(self):
        rows = super().__call__()
        for r in rows:
            r["quantity"] = self[r["order_id"]]["qty"]
        return rows


def test_a_found_stop_for_an_older_size_is_resized():
    k, b, c = _short_with_lost_stop()
    k.orders = _SizedBook({**k.orders})
    k.orders.fake = k
    b.s.max_lots = 2
    k.book_error = Exception("Gateway timed out")
    try:
        b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Grid sell", "g")
    except Exception:  # noqa: BLE001 - the stop cannot be told yet
        pass
    assert b.positions[c].qty == -130
    k.book_error = None
    b.check_protective_stops(NOW)
    live = [o for o in k.sent("SL") if o["status"] == "TRIGGER PENDING"]
    assert len(live) == 1 and live[0]["qty"] == 130 and b.protect[c.label] == live[0]["order_id"]


# ---- found by the fifth order-safety review (17 Nov) -------------------------------------------------------------
def test_a_short_glitch_in_zerodha_positions_does_not_end_a_close():
    """A leg waiting for a new stop must not be taken as gone because positions blinked for a second or two (it used
    to abort another leg's close in closing-only mode, leaving this leg open with no stop)."""
    k, b, c, old = _overnight_short()
    b.reconciled, b.reconciled_on = True, TOMORROW.date().isoformat()
    real, n = k.positions, [0]

    def blink():
        n[0] += 1
        return {"net": []} if n[0] <= 4 else real()
    k.positions = blink
    for _ in range(4):                                                    # used to raise Blocked on the 2nd read
        b._renew_stops() if b.renew else b.check_protective_stops(TOMORROW)
    k.positions = real
    b.check_protective_stops(TOMORROW)
    assert b.protect.get(c.label) == k.sent("SL")[0]["order_id"] and b.positions[c].qty == -65


def test_a_found_stop_on_a_leg_not_held_is_cancelled_when_the_session_stops():
    from algolab.live import Blocked
    k, b, c = _short_with_lost_stop()
    b.GONE_CONFIRM = 0
    k.place_order(variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1324300CE", transaction_type="BUY",
                  quantity=65, product="NRML", order_type="LIMIT", price=20.0)   # closed by hand in Kite
    k.book_error = None
    b.pending_stop[c.label]["at"] = 0                                     # old enough to decide
    b.renew.add(c.label)
    with pytest.raises(Blocked, match="was cancelled at Zerodha"):
        b._renew_stops()
    assert [o["status"] for o in k.sent("SL")] == ["CANCELLED"]
