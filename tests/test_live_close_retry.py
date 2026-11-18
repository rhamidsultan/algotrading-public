"""Closing a live leg must not give up after one 30 s chase (9 Nov 2026: a short NIFTY 24900 CE on expiry day was
left open with "close these in Kite now"). A closing order is re-sent further through the market, never while an
earlier order may still fill; a leg that still cannot be closed gets its protective stop back and the session keeps
retrying, sending only what Zerodha still holds."""
from datetime import date, datetime, timedelta

import pytest

from algolab import kite, paper
from algolab.feeds import OptionStore
from algolab.models import Contract, Quote
from test_platform import NOW, FakeOrders, _live_broker, _paper_tmp


class Book(FakeOrders):
    """A one-contract order book. A LIMIT buy fills once its price reaches `fill_buy_at` (default: the ask), a sell
    once it reaches `fill_sell_at` (default: the bid). Also: reject the next N orders, cancels that never confirm."""

    def __init__(self, expiry, bid=19.9, ask=20.1):
        super().__init__(expiry, mode="book")
        self.bid, self.ask = bid, ask
        self.fill_buy_at = self.fill_sell_at = None
        self.upper = None
        self.reject_next = 0
        self.cancel_ok = True
        self.working_at_place = []          # LIMIT orders still working for the symbol when a new one is placed

    def place_order(self, **k):
        if k["order_type"] == "LIMIT":
            self.working_at_place.append(sum(1 for o in self.orders.values() if o["type"] == "LIMIT"
                                             and o["sym"] == k["tradingsymbol"] and o["status"] == "OPEN"))
        oid = super().place_order(**k)
        o = self.orders[oid]
        if o["type"] == "LIMIT" and self.reject_next:
            self.reject_next -= 1
            o.update(status="REJECTED", status_message="price outside the band")
        self._match(o)
        return oid

    def _match(self, o):
        if o["type"] != "LIMIT" or o["status"] != "OPEN":
            return
        buy_at = self.ask if self.fill_buy_at is None else self.fill_buy_at
        sell_at = self.bid if self.fill_sell_at is None else self.fill_sell_at
        if o["side"] == "BUY" and o["price"] >= buy_at - 1e-9:
            o.update(status="COMPLETE", filled_quantity=o["qty"], average_price=buy_at)
        elif o["side"] == "SELL" and o["price"] <= sell_at + 1e-9:
            o.update(status="COMPLETE", filled_quantity=o["qty"], average_price=sell_at)

    def order_history(self, oid):
        self._match(self.orders[oid])
        return super().order_history(oid)

    def modify_order(self, variety, order_id, price=None, **k):
        super().modify_order(variety, order_id, price=price, **k)
        self._match(self.orders[order_id])

    def cancel_order(self, variety, order_id, **k):
        if self.cancel_ok or self.orders[order_id]["type"] == "SL":
            super().cancel_order(variety, order_id, **k)

    def quote(self, symbols):
        return {s: {"last_price": self.ask, "depth": {"buy": [{"price": self.bid}], "sell": [{"price": self.ask}]},
                    **({"upper_circuit_limit": self.upper} if self.upper else {})} for s in symbols}

    def buys(self):
        return [o for o in self.sent() if o["side"] == "BUY"]


def _short(book, **settings):
    b, e = _live_broker(book, **settings)
    c = Contract("NIFTY", e, 24300, "CE")
    b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    assert b.positions[c].qty == -65 and book.sent("SL")[0]["status"] == "TRIGGER PENDING"
    return b, c


def test_closing_order_goes_further_through_the_market_until_it_fills():
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.bid, book.ask, book.fill_buy_at = 11.9, 12.0, 13.0   # the market ran away from the exit price and is thin
    f = b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and f.price == 13.0
    prices = [o["price"] for o in book.buys()]
    assert prices == [10.1, 12.25, 12.6, 13.2]     # the 30 s chase (12.00 is beyond its 5%), then 2%, 5%, 10% through
    assert all(n == 0 for n in book.working_at_place)          # never two orders working at once
    assert all(o["status"] in ("CANCELLED", "COMPLETE") for o in book.sent())
    assert book.sent("SL")[0]["status"] == "CANCELLED" and len(book.sent("SL")) == 1   # no stop left behind


def test_closing_order_never_goes_past_the_cap_or_the_circuit():
    book = Book(date(2026, 10, 13))
    b, c = _short(book, close_max_through_pct=10)
    book.bid, book.ask, book.fill_buy_at, book.upper = 11.9, 12.0, 99.0, 13.0
    from algolab.live import Halt
    with pytest.raises(Halt, match="did not fill: tried 60s more, up to 10% through"):
        b.execute(NOW, c, "BUY", 65, Quote(12.0, bid=11.9, ask=12.0, source="market"), "Exit", "g")
    assert max(o["price"] for o in book.buys()) <= 13.0
    assert all(n == 0 for n in book.working_at_place)


def test_a_leg_that_cannot_be_closed_gets_its_stop_back():
    from algolab.live import Halt
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.fill_buy_at = 999.0
    with pytest.raises(Halt, match="did not fill"):
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert b.positions[c].qty == -65
    old, new = book.sent("SL")
    assert old["status"] == "CANCELLED" and new["status"] == "TRIGGER PENDING"
    assert new["trigger"] == pytest.approx(59.7) and new["qty"] == 65 and b.protect[c.label] == new["order_id"]
    # the market already past the old trigger: the stop goes above the market instead (a buy stop must)
    book2 = Book(date(2026, 10, 13))
    b2, c2 = _short(book2)
    book2.bid, book2.ask, book2.fill_buy_at = 64.9, 65.0, 999.0
    with pytest.raises(Halt):
        b2.execute(NOW, c2, "BUY", 65, Quote(65.0, bid=64.9, ask=65.0, source="market"), "Exit", "g")
    assert book2.sent("SL")[-1]["trigger"] == pytest.approx(78.0)


def test_a_rejected_closing_order_is_sent_again():
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.bid, book.ask, book.reject_next = 9.9, 10.1, 1
    f = b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and f.price == 10.1 and [o["status"] for o in book.buys()] == ["REJECTED", "COMPLETE"]


def test_an_opening_order_still_gives_up_but_cheap_options_can_be_re_priced():
    from algolab.live import Halt
    book = Book(date(2026, 10, 13), bid=0.60, ask=0.65)
    b, e = _live_broker(book)
    c = Contract("NIFTY", e, 24300, "CE")
    book.fill_sell_at, book.bid = 0.55, 0.55   # one tick is 8% of this price: re-pricing used to be refused
    b.execute(NOW, c, "SELL", 65, Quote(0.6, bid=0.60, ask=0.65, source="market"), "Entry", "g")
    assert b.positions[c].qty == -65 and [o["price"] for o in book.sent()] == [0.55]
    book2 = Book(date(2026, 10, 13))
    b2, e2 = _live_broker(book2)
    book2.fill_sell_at = 1.0
    with pytest.raises(Halt, match="did not fill within 30s"):
        b2.execute(NOW, Contract("NIFTY", e2, 24300, "CE"), "SELL", 65, Quote(20.0, bid=19.9, ask=20.1), "Entry", "g")
    assert len(book2.sent()) == 1


def test_an_order_whose_cancel_is_not_confirmed_is_never_doubled():
    from algolab.live import Halt
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.fill_buy_at, book.cancel_ok = 999.0, False
    with pytest.raises(Halt, match="could not be confirmed cancelled"):
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert len(book.buys()) == 1 and [w["oid"] for w in b.working.values()] == [book.buys()[0]["order_id"]]
    assert len(book.sent("SL")) == 1          # no stop put back while a buy may still fill
    with pytest.raises(Halt, match="still working"):
        b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert len(book.buys()) == 1
    # it fills after all: the retry books it and sends nothing
    o = book.buys()[0]
    o.update(status="COMPLETE", filled_quantity=65, average_price=10.6)
    assert b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1)).price == 10.6
    assert not b.positions and len(book.buys()) == 1 and not b.working


def test_retry_sends_only_what_zerodha_still_holds():
    from algolab.live import Blocked
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    # closed by hand in Kite meanwhile: booked as closed outside, nothing sent, its stop taken off
    book.place_order(variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1324300CE", transaction_type="BUY",
                     quantity=65, product="NRML", order_type="LIMIT", price=30.0)
    n = len(book.buys())
    f = b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert not b.positions and "outside Algo Lab" in f.reason and len(book.buys()) == n
    assert all(o["status"] == "CANCELLED" for o in book.sent("SL"))
    # Zerodha on the other side of this session: not touched
    book2 = Book(date(2026, 10, 13))
    b2, c2 = _short(book2)
    book2.place_order(variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1324300CE", transaction_type="BUY",
                      quantity=130, product="NRML", order_type="LIMIT", price=30.0)
    n = len(book2.buys())
    from algolab.live import Halt
    with pytest.raises(Halt, match="Reading again"):           # once could be Zerodha's positions lagging
        b2.retry_close(NOW, c2, Quote(10.0, bid=9.9, ask=10.1))
    with pytest.raises(Blocked, match="Not traded"):            # twice in a row: left for a person
        b2.retry_close(NOW, c2, Quote(10.0, bid=9.9, ask=10.1))
    assert len(book2.buys()) == n and b2.positions[c2].qty == -65
    assert book2.sent("SL")[0]["status"] == "TRIGGER PENDING"   # its stop was never touched


def test_live_session_keeps_closing_by_itself_and_survives_a_restart(tmp_path, monkeypatch):
    """The 9 Nov case end to end: the square-off cannot close, the session does not ask for Kite but keeps trying
    (also after a restart) and closes the legs once the market lets it."""
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper.Session, "CLOSE_RETRY_EVERY", 0)
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    fake = FakeOrders(date(2026, 10, 13))
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert len(paper.read_state(sid)["positions"]) == 2
    fake.mode = "never"                                   # nothing fills: Stop & square off cannot close
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert len(s["positions"]) == 2 and s["closing_only"] and paper.read_desired(sid) is not None
    assert "keeps trying" in paper.session_status(sid) and "close these in Kite" not in paper.session_status(sid)
    assert sum(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL")) == 2      # both legs protected again
    fake.mode = "fill"                                    # the market lets it: the next try (after a restart) closes
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["positions"] and not s["closing_only"] and paper.read_desired(sid) is None
    assert "squared off (on try" in paper.session_status(sid)
    assert all(q == 0 for q in {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"]}.values())
    assert not any(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL"))


def test_retries_stop_at_the_close_and_then_ask_for_kite(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper.Session, "CLOSE_RETRY_EVERY", 0)
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    fake = FakeOrders(date(2026, 10, 13))
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    fake.mode = "never"
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert paper.read_state(sid)["closing_only"]
    clock[0] = datetime(2026, 10, 7, 15, 26)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["closing_only"] and paper.read_desired(sid) is None and len(s["positions"]) == 2
    assert "Retries stopped at 15:25 IST" in paper.session_status(sid)
    assert sum(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL")) == 2      # the stops stay at Zerodha


# ---- found by the order-safety review (10 Nov) -------------------------------------------------------------------
def test_part_fill_with_an_unconfirmed_cancel_is_booked_and_never_doubled():
    """C1: 30 of 65 filled, the cancel never confirmed: book the 30, keep the order, no stop and no second buy."""
    from algolab.live import Halt, Unconfirmed
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.fill_buy_at, book.cancel_ok = 999.0, False
    real = book.order_history

    def part(oid):
        o = book.orders[oid]
        if o["type"] == "LIMIT" and o["side"] == "BUY":
            o.update(filled_quantity=30, average_price=10.1)
        return real(oid)
    book.order_history = part
    with pytest.raises(Unconfirmed):
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert b.positions[c].qty == -35 and len(book.buys()) == 1
    assert len(book.sent("SL")) == 1                           # no stop while the buy may still fill
    with pytest.raises(Halt, match="still working"):
        b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert len(book.buys()) == 1
    book.order_history = real
    o = book.buys()[0]
    o.update(status="COMPLETE", filled_quantity=65, average_price=10.2)
    b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))       # the other 35 are booked, nothing sent
    assert not b.positions and len(book.buys()) == 1 and not b.working


def test_a_lost_answer_is_looked_up_before_anything_is_sent_again():
    """C2: Zerodha took the order but the answer was lost, or a status read failed mid-chase: no second order."""
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    real_place = book.place_order

    def lost(**k):
        real_place(**k)
        raise TimeoutError("read timed out")
    book.place_order = lost
    book.bid, book.ask = 9.9, 10.1
    f = b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and f.price == 10.1 and len(book.buys()) == 1
    # a status read fails while the order works: it is still worked, not replaced
    book2 = Book(date(2026, 10, 13))
    b2, c2 = _short(book2)
    real = book2.order_history
    calls = {"n": 0}

    def flaky(oid):
        if book2.orders[oid]["type"] == "LIMIT" and book2.orders[oid]["side"] == "BUY":
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("blip")
        return real(oid)
    book2.order_history = flaky
    book2.fill_buy_at = 10.1
    b2.execute(NOW, c2, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b2.positions and len(book2.buys()) == 1
    # refused for sure (not in the order book): sent again
    book3 = Book(date(2026, 10, 13))
    b3, c3 = _short(book3)
    real3 = book3.place_order
    n = {"left": 1}

    def refuse(**k):
        if k["order_type"] == "LIMIT" and n["left"]:
            n["left"] -= 1
            raise RuntimeError("Too many requests")
        return real3(**k)
    book3.place_order = refuse
    b3.execute(NOW, c3, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b3.positions and len(book3.buys()) == 1


def test_an_order_in_flight_at_a_restart_is_found_and_the_stop_comes_back(tmp_path):
    """H1: the engine restarts mid-close. The order was written to disk before it was sent: the restarted broker reads
    it, books its fill and puts the stop back on what is still short before anything else."""
    import json
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    b.journal = tmp_path / "working.json"
    book.fill_buy_at, book.cancel_ok = 999.0, False
    from algolab.live import Unconfirmed
    with pytest.raises(Unconfirmed):          # stands in for the moment of the restart: an order is out there
        b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert json.loads(b.journal.read_text())
    state = json.loads(json.dumps(b.to_dict()))
    state["live"]["working"] = {}             # as if the restart came before the state was saved
    o = book.buys()[0]
    o.update(status="CANCELLED", filled_quantity=40, average_price=10.1)
    b2, _ = _live_broker(book)
    b2.journal = b.journal
    b2.load_dict(state)
    b2.reconcile(NOW)
    assert b2.positions[c].qty == -25 and not b2.working
    assert b2.protect.get(c.label) and book.sent("SL")[-1]["qty"] == 25


def test_a_failure_after_the_stop_came_off_puts_it_back():
    """H2: anything going wrong in a retry after its stop was cancelled re-places the stop."""
    from algolab.live import Halt
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    real = book.positions
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("blip")
        return real()
    book.positions = flaky
    with pytest.raises(Exception):
        b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert b.positions[c].qty == -65 and b.protect.get(c.label)
    assert [o["status"] for o in book.sent("SL")] == ["CANCELLED", "TRIGGER PENDING"]


def test_another_order_working_on_the_symbol_stops_a_retry():
    """M2: an order placed in Kite on the same symbol: nothing more is sent."""
    from algolab.live import Halt
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.fill_buy_at = 999.0
    book.place_order(variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1324300CE", transaction_type="BUY",
                     quantity=65, product="NRML", order_type="LIMIT", price=5.0)
    n = len(book.buys())
    with pytest.raises(Halt, match="working at Zerodha"):
        b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert len(book.buys()) == n and book.sent("SL")[0]["status"] == "TRIGGER PENDING"


def test_a_shared_contract_short_at_zerodha_is_not_booked_as_closed():
    """H4: another live session holds the same contract and Zerodha shows less: whose leg went is not guessed."""
    from algolab.live import Blocked
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    b.others = lambda: {"NIFTY26O1324300CE": -65}               # the other session, whose stop filled unread
    with pytest.raises(Blocked, match="cannot be told"):
        b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert b.positions[c].qty == -65 and b.protect.get(c.label)


def test_stop_while_closing_ends_the_retries_and_keeps_the_stop(tmp_path, monkeypatch):
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper.Session, "CLOSE_RETRY_EVERY", 0)
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    fake = FakeOrders(date(2026, 10, 13))
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    fake.mode = "never"
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert paper.read_state(sid)["closing_only"]
    n = len(fake.sent())
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["closing_only"] and paper.read_desired(sid) is None and len(fake.sent()) == n
    assert "Retries stopped from the dashboard" in paper.session_status(sid)
    assert sum(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL")) == 2


# ---- second review (10 Nov) ----------------------------------------------------------------------------------------
def test_an_order_that_shows_late_in_the_order_book_is_not_sent_twice():
    """H1: the answer to place_order is lost and the order shows in Zerodha's order book only a few seconds later."""
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    real_place, real_orders = book.place_order, book.orders.__class__.__call__
    hidden = {"reads": 0}

    def lost(**k):
        real_place(**k)
        if k["order_type"] == "LIMIT":
            hidden["reads"] = 2                 # the next two order-book reads do not show it yet
            raise TimeoutError("read timed out")
    book.place_order = lost

    class LateBook(type(book.orders)):
        def __call__(self):
            if hidden["reads"]:
                hidden["reads"] -= 1
                return [o for o in real_orders(self) if o["order_type"] != "LIMIT" or o["transaction_type"] != "BUY"]
            return real_orders(self)
    book.orders = LateBook(book.orders)
    book.bid, book.ask = 9.9, 10.1
    f = b.execute(NOW, c, "BUY", 65, Quote(10.0, bid=9.9, ask=10.1, source="market"), "Exit", "g")
    assert not b.positions and f.price == 10.1 and len(book.buys()) == 1


def test_a_late_fill_that_grows_a_short_leg_resizes_its_stop():
    """M3: an opening SELL that was not confirmed fills in full later: the leg's stop covers all of it."""
    from algolab.live import Unconfirmed
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.fill_sell_at, book.cancel_ok = 1.0, False
    with pytest.raises(Unconfirmed):
        b.execute(NOW, c, "SELL", 65, Quote(20.0, bid=19.9, ask=20.1, source="market"), "Entry", "g")
    o = [x for x in book.sent() if x["side"] == "SELL"][-1]
    o.update(status="COMPLETE", filled_quantity=65, average_price=19.9)
    assert b.settle_working(NOW) == []
    assert b.positions[c].qty == -130
    live_stops = [x for x in book.sent("SL") if x["status"] == "TRIGGER PENDING"]
    assert len(live_stops) == 1 and live_stops[0]["qty"] == 130


def test_a_stop_from_an_earlier_day_is_replaced_at_start():
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    state = b.to_dict()
    book.orders.pop(b.protect[c.label])        # yesterday's DAY stop: not in today's order book
    b2, _ = _live_broker(book)
    b2.load_dict(state)
    b2.reconcile(NOW + timedelta(days=1))      # the next morning (before the leg's expiry)
    new = book.sent("SL")[-1]
    assert b2.protect[c.label] == new["order_id"] and new["trigger"] == pytest.approx(59.7) and new["qty"] == 65


def test_a_manual_stop_in_kite_blocks_a_retry():
    from algolab.live import Halt
    book = Book(date(2026, 10, 13))
    b, c = _short(book)
    book.place_order(variety="regular", exchange="NFO", tradingsymbol="NIFTY26O1324300CE", transaction_type="BUY",
                     quantity=65, product="NRML", order_type="SL", price=40.0, trigger_price=38.0, tag="mine")
    n = len(book.buys())
    with pytest.raises(Halt, match="working at Zerodha"):
        b.retry_close(NOW, c, Quote(10.0, bid=9.9, ask=10.1))
    assert len(book.buys()) == n


def test_square_off_includes_an_opening_order_that_filled_late(tmp_path, monkeypatch):
    """H2: an opening SELL whose fill was not read before the Stop: it is booked first and closed with the rest, so
    "squared off" is true and Zerodha ends flat."""
    import json
    cfg = _paper_tmp(tmp_path, monkeypatch)
    cfg["paper"]["live_poll_seconds"] = 0.01
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    monkeypatch.setattr(paper, "ist_now", lambda: clock[0])
    import algolab.live as live_mod
    monkeypatch.setattr(live_mod._time, "sleep", lambda s: None)
    fake = FakeOrders(date(2026, 10, 13))
    monkeypatch.setattr(paper, "_kite_feed", lambda c, g: kite.KiteFeed(fake, c, g, log=g.log))
    sid = paper.set_desired("premium_strangle", {"min_dte": 0}, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    leg = s["positions"][0]["Contract"]
    sym = next(o["sym"] for o in fake.sent() if o["side"] == "SELL")
    label = next(lbl for lbl in s["broker"]["live"]["contracts"] if fake_sym_matches(lbl, sym))
    oid = fake.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="SELL", quantity=65,
                           product="NRML", order_type="LIMIT", price=100.5, tag="algolabfeed01")   # fills (mode fill)
    (paper.Slot(sid).dir / "working.json").write_text(json.dumps(
        {"algolabfeed01": {"sym": sym, "side": "SELL", "qty": 65, "label": label, "group": "", "oid": oid, "booked": 0}}))
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert not s["positions"] and "squared off" in paper.session_status(sid) and leg
    assert all(q == 0 for q in {p["tradingsymbol"]: p["quantity"] for p in fake.positions()["net"]}.values())
    assert not any(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL"))


def fake_sym_matches(label: str, sym: str) -> bool:
    """FakeKite symbols are NIFTYX<strike><right>; labels read 'NIFTY 13 Oct 26 <strike> <right>'."""
    parts = label.split()
    return sym == f"NIFTYX{parts[-2]}{parts[-1]}"
