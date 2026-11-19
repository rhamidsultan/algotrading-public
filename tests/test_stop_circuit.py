"""19 Nov 2026, intraday_strangle-10: the PE's protective stop (3 x 157.10 = 471.30) was above the option's upper
circuit limit (442.15). Zerodha refused it; each retry was taken for a lost answer, so when the PE's leg stop came
the session could not rule out a live stop, halted and squared off the CE too. Now the stop is placed at once just
below the circuit limit, and a refusal is never taken for a lost answer."""
from datetime import date, timedelta

from algolab.models import Contract, Quote
from test_platform import NOW, FakeOrders, _live_broker


class InputException(Exception):
    """As kiteconnect names Zerodha's refusals of an order's inputs."""


class Circuit(FakeOrders):
    """Zerodha's upper circuit limit for stop triggers, as on 19 Nov."""
    limit = 442.15
    say_number = True
    band_in_quote = True

    def place_order(self, *a, **k):
        if k.get("order_type") == "SL" and ((k.get("trigger_price") or 0) >= self.limit or (k.get("price") or 0) > self.limit):
            self.refused = getattr(self, "refused", 0) + 1
            raise InputException("Trigger price for stoploss buy orders cannot be above the upper circuit price."
                                 + (f" Please try placing the order with trigger price below {self.limit}." if self.say_number else ""))
        return super().place_order(*a, **k)

    def quote(self, symbols):
        out = super().quote(symbols)
        for s in symbols:
            if s.startswith("NFO:") and self.band_in_quote:
                out[s]["upper_circuit_limit"] = self.limit
        return out


def _sold(k, px=157.10):
    b, e = _live_broker(k)
    c = Contract("NIFTY", e, 24200, "PE")
    b.execute(NOW, c, "SELL", 65, Quote(px, bid=px - 0.1, ask=px + 0.1, source="market"), "Entry", "g")
    return b, c


def test_a_stop_above_the_circuit_limit_is_placed_just_below_it():
    k = Circuit(date(2026, 10, 13))
    b, c = _sold(k)
    stops = k.sent("SL")
    assert len(stops) == 1 and stops[0]["trigger"] == 442.1 and stops[0]["price"] <= 442.15
    assert b.protect[c.label] == stops[0]["order_id"] and not b.pending_stop and c.label not in b.renew


def test_a_refusal_still_caps_the_stop_when_no_quote_gives_the_limit():
    k = Circuit(date(2026, 10, 13))
    k.band_in_quote = False                                      # the limit only in Zerodha's refusal
    b, c = _sold(k)
    assert [s["trigger"] for s in k.sent("SL")] == [442.1] and k.refused == 1 and not b.pending_stop


def test_the_limit_is_read_from_a_quote_when_zerodha_names_no_number():
    k = Circuit(date(2026, 10, 13))
    k.say_number = False
    b, c = _sold(k)
    assert [s["trigger"] for s in k.sent("SL")] == [442.1] and not b.pending_stop


def test_a_stop_within_the_limit_is_unchanged():
    k = Circuit(date(2026, 10, 13))
    b, c = _sold(k, px=100.0)
    sold = b.fills[-1].price
    assert [s["trigger"] for s in k.sent("SL")] == [round(sold * 3, 2)] and not getattr(k, "refused", 0)


def test_a_refused_stop_is_not_taken_for_a_lost_answer_so_the_exit_closes_only_that_leg():
    """Any refusal (here one the cap cannot cure): no 'may still be at Zerodha' wait, so the strategy's exit of the
    leg is sent and nothing else is squared off."""
    class Refuses(FakeOrders):
        def place_order(self, *a, **k):
            if k.get("order_type") == "SL":
                raise InputException("Trigger price is not a multiple of tick size")
            return super().place_order(*a, **k)
    k = Refuses(date(2026, 10, 13))
    b, c = _sold(k)
    ce = Contract("NIFTY", c.expiry, 24200, "CE")
    b.execute(NOW, ce, "SELL", 65, Quote(137.05, bid=137.0, ask=137.1, source="market"), "Entry", "g")
    assert not b.pending_stop and c.label in b.renew
    b.check_protective_stops(NOW + timedelta(seconds=30))       # tried again at the check: refused again
    assert not b.pending_stop
    f = b.execute(NOW + timedelta(minutes=10), c, "BUY", 65, Quote(197.0, bid=196.9, ask=197.1, source="market"), "Stop", "g")
    assert f is not None and c not in b.positions and ce in b.positions   # only the PE closed: the CE runs on


def test_a_lost_answer_still_waits():
    """A timeout (no answer) is still treated as 'it may have reached Zerodha', so a stop is never placed twice."""
    class Slow(FakeOrders):
        def place_order(self, *a, **k):
            if k.get("order_type") == "SL":
                raise TimeoutError("Read timed out")
            return super().place_order(*a, **k)
    b, c = _sold(Slow(date(2026, 10, 13)))
    assert c.label in b.pending_stop


def test_a_limit_price_above_the_circuit_is_capped_too():
    """Sold at 140: trigger 420 is below the limit 442.15, but its limit price 462 is not."""
    k = Circuit(date(2026, 10, 13))
    b, c = _sold(k, px=140.1)
    stops = k.sent("SL")
    assert len(stops) == 1 and stops[0]["trigger"] < 442.15 and stops[0]["price"] <= 442.15
    assert stops[0]["price"] >= stops[0]["trigger"] and not b.pending_stop


class DataException(Exception):
    pass


class OrderException(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def test_errors_that_may_hide_a_placed_order_still_wait():
    """A gateway page that could not be read (it may mention a 'circuit breaker') or an order-system error with code
    500: the order may have got there, so no second stop is sent until it is ruled out."""
    for err in (DataException("Couldn't parse the JSON response: <html>upstream circuit breaker open</html>"),
                OrderException("Order system timed out", 500)):
        class Lost(FakeOrders):
            def place_order(self, *a, **k):
                if k.get("order_type") == "SL":
                    super().place_order(*a, **k)                 # it got there...
                    self.orders[str(self.n)]["tag"] = "hidden"   # ...but the order book does not show it yet
                    raise err
                return super().place_order(*a, **k)
        k = Lost(date(2026, 10, 13))
        b, c = _sold(k)
        assert c.label in b.pending_stop, type(err).__name__
        n = len(k.sent("SL"))
        b.check_protective_stops(NOW + timedelta(seconds=30))
        assert len(k.sent("SL")) == n                            # no second stop while it may be live


def test_an_order_exception_with_a_4xx_code_is_a_refusal():
    class Refuses(FakeOrders):
        def place_order(self, *a, **k):
            if k.get("order_type") == "SL":
                raise OrderException("Order rejected by RMS", 400)
            return super().place_order(*a, **k)
    b, c = _sold(Refuses(date(2026, 10, 13)))
    assert not b.pending_stop and c.label in b.renew


def test_a_stop_accepted_then_rejected_for_the_circuit_is_placed_again_below_it():
    k = Circuit(date(2026, 10, 13))
    k.limit = 10_000                                             # accepted at first...
    b, c = _sold(k)
    first = k.sent("SL")[0]
    first.update(status="REJECTED", status_message="RMS: trigger price above upper circuit; try below 442.15")
    k.limit = 442.15
    b.check_protective_stops(NOW + timedelta(seconds=30))        # ...then shown REJECTED
    assert b.stop_ceiling[c.label][1] == 442.15
    if c.label in b.renew:                                       # placed at this check or the next
        b.check_protective_stops(NOW + timedelta(seconds=60))
    live = [o for o in k.sent("SL") if o["status"] == "TRIGGER PENDING"]
    assert len(live) == 1 and live[0]["trigger"] <= 442.1 and live[0]["price"] <= 442.15


def test_a_limit_the_band_has_moved_past_does_not_block_the_stop():
    """The band widens during the day and the price trades above the old limit: a stop capped there is refused
    (below the last price, no circuit named), so it is tried again without the old limit."""
    class Moved(Circuit):
        def place_order(self, *a, **k):
            if k.get("order_type") == "SL" and (k.get("trigger_price") or 0) <= self.ltp:
                raise InputException("Trigger price should be higher than the last traded price.")
            return super().place_order(*a, **k)
    k = Moved(date(2026, 10, 13))
    k.ltp, k.band_in_quote = 0, False                            # no quote: the refusal names the limit
    b, c = _sold(k)
    assert b.stop_ceiling[c.label][1] == 442.15
    k.limit, k.ltp = 600.0, 450.0                                # later: band 600, price 450
    b._drop_stop(c.label, keep_level=False)
    k.sent("SL")[0]["status"] = "CANCELLED"
    b._place_protect(c, b.symbol_of(c), b.positions[c].avg_price, 65)
    live = [o for o in k.sent("SL") if o["status"] == "TRIGGER PENDING"]
    assert len(live) == 1 and 450 < live[0]["trigger"] < 600 and c.label not in b.stop_ceiling


def test_the_saved_limit_is_for_one_day_only():
    k = Circuit(date(2026, 10, 13))
    k.band_in_quote = False
    b, c = _sold(k)
    assert b._ceiling_of(c.label) == 442.15
    b._day = "2099-01-01"
    assert b._ceiling_of(c.label) is None


def test_a_stop_rejected_for_something_else_is_not_sent_again_and_again():
    """Rejected after it was accepted, not for the circuit (e.g. margin): as before, dropped with one alert, not
    re-sent at every check (that would use up the day's orders and flood the alerts)."""
    k = FakeOrders(date(2026, 10, 13))
    b, c = _sold(k)
    told = []
    b.notify = lambda subj, body: told.append(subj)
    k.sent("SL")[0].update(status="REJECTED", status_message="RMS: Margin exceeds")
    n = len(k.sent("SL"))
    for i in range(1, 6):
        b.check_protective_stops(NOW + timedelta(seconds=30 * i))
    assert len(k.sent("SL")) == n and told.count("Protective stop gone") == 1


def test_the_circuit_limit_is_read_first_so_the_first_stop_is_not_refused():
    """Hamid, 19 Nov: check the upper circuit and keep the stop below it. The quote's limit is read before the stop
    is sent: one order, no refusal."""
    k = Circuit(date(2026, 10, 13))
    b, c = _sold(k)
    stops = k.sent("SL")
    assert len(stops) == 1 and stops[0]["trigger"] == 442.1 and stops[0]["price"] <= 442.15
    assert not getattr(k, "refused", 0)                          # placed below it the first time
