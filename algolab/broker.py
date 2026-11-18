"""Simulated broker: fills, charges, positions and P&L. Used by backtests and paper trading alike."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime

from .models import Contract, Fill, Position, Quote, Trade


@dataclass
class CostModel:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.15
    stt_exercise_pct: float = 0.15
    exchange_txn_pct: float = 0.03553
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0

    def order_cost(self, side: str, value: float) -> float:
        """All charges for one executed option order of the given premium value."""
        fees = self.brokerage_per_order + value * (self.exchange_txn_pct + self.sebi_pct) / 100.0
        cost = fees * (1.0 + self.gst_pct / 100.0)
        if side == "SELL":
            cost += value * self.stt_sell_pct / 100.0
        else:
            cost += value * self.stamp_buy_pct / 100.0
        return cost

    def exercise_cost(self, intrinsic_value: float) -> float:
        return intrinsic_value * self.stt_exercise_pct / 100.0


class CostSchedule:
    """Charges that changed over time. `history` holds earlier rates as {until: date, name: value, ...};
    a trade before `until` uses that entry's values (the earliest such entry), anything later uses
    `current`. Lets a multi-year backtest pay the STT and exchange fees that applied on each day."""

    def __init__(self, current: CostModel, history: list | None = None):
        from .config import to_date
        self.current = current
        self.periods = sorted(
            ((to_date(h["until"]), replace(current, **{k: v for k, v in h.items() if k != "until"}))
             for h in history or []), key=lambda p: p[0])

    def at(self, when) -> CostModel:
        d = when.date() if isinstance(when, datetime) else when
        for until, model in self.periods:
            if d < until:
                return model
        return self.current


class PaperBroker:
    def __init__(self, capital: float, costs: CostModel, slippage_pct: float = 0.5, tick: float = 0.05,
                 history: list | None = None, min_half_spread: list | None = None, stress: float = 1.0):
        self.capital = float(capital)
        self.costs = costs
        self.schedule = CostSchedule(costs, history)
        self.slippage_pct = slippage_pct
        self.min_half_spread = sorted([float(a), float(b)] for a, b in (min_half_spread or []))
        self.stress = max(float(stress or 1.0), 0.0)
        self.tick = tick
        self.positions: dict[Contract, Position] = {}
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self.realized_gross = 0.0
        self.total_costs = 0.0

    # ---- prices -------------------------------------------------------
    def _round(self, price: float) -> float:
        return max(round(round(price / self.tick) * self.tick, 2), 0.0)

    def half_spread(self, last: float) -> float:
        """What one side costs without a bid/ask: slippage_pct of the premium, but at least the minimum for its
        price band (cheap options trade with spreads of several ticks), times the stress factor."""
        floor = next((rs for below, rs in self.min_half_spread if last < below), 0.0)
        return max(last * self.slippage_pct / 100.0, floor) * self.stress

    def fill_price(self, side: str, quote: Quote) -> float:
        if quote.bid and quote.ask and self.stress > 1:      # stress widens a real spread around its middle
            mid = (quote.bid + quote.ask) / 2
            quote = Quote(last=quote.last, bid=mid - (mid - quote.bid) * self.stress, ask=mid + (quote.ask - mid) * self.stress,
                          source=quote.source)
        if side == "SELL":
            px = quote.bid if quote.bid else quote.last - self.half_spread(quote.last)
        else:
            px = quote.ask if quote.ask else quote.last + self.half_spread(quote.last)
        px = self._round(px)
        return max(px, self.tick) if side == "BUY" else px

    # ---- orders -------------------------------------------------------
    def execute(self, now: datetime, contract: Contract, side: str, qty: int, quote: Quote,
                reason: str = "", group: str = "") -> Fill:
        price = self.fill_price(side, quote)
        cost = self.schedule.at(now).order_cost(side, price * qty)
        return self._apply(now, contract, side, qty, price, cost, reason, group, quote.source)

    def settle(self, now: datetime, contract: Contract, spot: float) -> Fill | None:
        """Cash-settle an expired position at intrinsic value."""
        pos = self.positions.get(contract)
        if pos is None:
            return None
        price = round(contract.intrinsic(spot), 2)
        side = "SELL" if pos.qty > 0 else "BUY"
        cost = self.schedule.at(now).exercise_cost(price * pos.qty) if pos.qty > 0 else 0.0
        return self._apply(now, contract, side, abs(pos.qty), price, cost, "Expiry settlement", pos.group, "settlement")

    def _apply(self, now, contract, side, qty, price, cost, reason, group, source) -> Fill:
        signed = qty if side == "BUY" else -qty
        self.total_costs += cost
        fill = Fill(now, contract.label, side, qty, price, round(cost, 2), reason, group, source)
        self.fills.append(fill)
        pos = self.positions.get(contract)
        if pos is None or (pos.qty > 0) == (signed > 0):
            if pos is None:
                self.positions[contract] = Position(contract, signed, price, now, group, cost)
            else:
                total = pos.qty + signed
                pos.avg_price = (pos.avg_price * pos.qty + price * signed) / total
                pos.qty = total
                pos.entry_costs += cost
            return fill
        closing = min(qty, abs(pos.qty))
        direction = 1 if pos.qty > 0 else -1
        gross = (price - pos.avg_price) * closing * direction
        entry_share = pos.entry_costs * closing / abs(pos.qty)
        exit_share = cost * closing / qty
        self.realized_gross += gross
        self.trades.append(Trade(
            contract.label, "LONG" if direction > 0 else "SHORT", closing, pos.opened_at, now,
            pos.avg_price, price, round(gross, 2), round(entry_share + exit_share, 2),
            round(gross - entry_share - exit_share, 2), reason, pos.group))
        pos.entry_costs -= entry_share
        pos.qty += closing * (-direction)
        if pos.qty == 0:
            del self.positions[contract]
        leftover = qty - closing
        if leftover:
            self.positions[contract] = Position(contract, leftover * (1 if side == "BUY" else -1),
                                                price, now, group, cost - exit_share)
        return fill

    # ---- P&L ----------------------------------------------------------
    @property
    def net_realized(self) -> float:
        return self.realized_gross - self.total_costs

    def unrealized(self, quote_fn) -> float:
        return sum((quote_fn(c).last - p.avg_price) * p.qty for c, p in self.positions.items())

    def equity(self, quote_fn) -> float:
        return self.capital + self.net_realized + self.unrealized(quote_fn)

    # ---- persistence --------------------------------------------------
    def to_dict(self) -> dict:
        def ser(obj):
            d = asdict(obj)
            return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}
        return {
            "capital": self.capital, "realized_gross": self.realized_gross, "total_costs": self.total_costs,
            "positions": [{"contract": c.to_dict(), "qty": p.qty, "avg_price": p.avg_price,
                           "opened_at": p.opened_at.isoformat(), "group": p.group,
                           "entry_costs": p.entry_costs} for c, p in self.positions.items()],
            "fills": [ser(f) for f in self.fills], "trades": [ser(t) for t in self.trades],
        }

    def load_dict(self, d: dict) -> None:
        self.capital = d["capital"]
        self.realized_gross = d["realized_gross"]
        self.total_costs = d["total_costs"]
        self.positions = {}
        for p in d["positions"]:
            c = Contract.from_dict(p["contract"])
            self.positions[c] = Position(c, p["qty"], p["avg_price"], datetime.fromisoformat(p["opened_at"]),
                                         p["group"], p["entry_costs"])
        self.fills = [Fill(**{**f, "time": datetime.fromisoformat(f["time"])}) for f in d["fills"]]
        self.trades = [Trade(**{**t, "entry_time": datetime.fromisoformat(t["entry_time"]),
                                "exit_time": datetime.fromisoformat(t["exit_time"])}) for t in d["trades"]]
