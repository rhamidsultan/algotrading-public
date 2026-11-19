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

    def place_order(self, *a, **k):
        if k.get("order_type") == "SL" and (k.get("trigger_price") or 0) >= self.limit:
            self.refused = getattr(self, "refused", 0) + 1
            raise InputException("Trigger price for stoploss buy orders cannot be above the upper circuit price."
                                 + (f" Please try placing the order with trigger price below {self.limit}." if self.say_number else ""))
        return super().place_order(*a, **k)

    def quote(self, symbols):
        out = super().quote(symbols)
        for s in symbols:
            if s.startswith("NFO:"):
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
