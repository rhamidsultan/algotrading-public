"""Real orders through Zerodha, for a paper-trading session switched to "shadow" or "live".

shadow  everything a live session would do - guards, prices, the exact orders it would send - but
        nothing is sent: fills are simulated as in paper trading, and every order is logged as
        "SHADOW". Use it for a week before going live.
live    orders are sent to Zerodha:
          - LIMIT orders at the best bid (sell) / ask (buy), product NRML (can be held overnight),
            re-priced to the new best bid/ask every few seconds; given up after `chase_seconds`
            or if the price would move more than `max_chase_pct` from the first price
          - after an option is sold, a protective SL buy order is left at Zerodha at
            `protect_mult` x the sold price, so a crash, a missed login or a server outage cannot
            leave a short option unprotected; it is cancelled before the leg is bought back
          - before the first order of a run, the session's positions are compared with Zerodha's;
            any difference stops the session (nothing is sent)
Guards (both modes): at most `max_lots` lots per order to open, `max_orders_per_day` orders to open,
and the session squares off and stops if the day's loss reaches `daily_loss_limit`. Closing orders
are never blocked. Anything unexpected (a rejection, a partial fill, a protective stop filling)
squares the session off, stops it and sends an alert.
"""
from __future__ import annotations

import json
import math
import smtplib
import threading
import time as _time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path

from .broker import PaperBroker
from .models import Contract, Quote


class Halt(RuntimeError):
    """The session must square off and stop."""


class CancelFailed(Halt):
    """A protective stop could not be confirmed cancelled: the buy-back was not sent."""


class Blocked(Halt):
    """The session must stop WITHOUT trading (e.g. its positions do not match Zerodha's)."""


@dataclass
class LiveSettings:
    mode: str = "shadow"             # shadow | live
    max_lots: int = 1                # per order that opens a position
    max_orders_per_day: int = 40     # orders that open positions
    daily_loss_limit: float = 0.0    # Rs; required (> 0) for live
    protect_mult: float = 3.0        # protective stop at this x the sold price (0 = none)
    chase_seconds: int = 30
    max_chase_pct: float = 5.0
    order_type: str = "limit_chase"  # limit_chase: best bid/ask, re-priced | market: one limit max_chase_pct through the market
    allow_over_max_lots: bool = False  # per session (chosen when deploying): an order above max_lots is placed with a
    #                                    warning and an alert instead of stopping the session. Daily loss limit and
    #                                    orders per day stay hard limits.

    @classmethod
    def from_dict(cls, d: dict | None) -> "LiveSettings":
        d = d or {}
        def cast(k, v):
            if isinstance(getattr(cls, k), bool):           # "false" must not read as True
                return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            return type(getattr(cls, k))(v)
        s = cls(**{k: cast(k, v) for k, v in d.items() if k in cls.__dataclass_fields__})
        if s.mode not in ("shadow", "live"):
            raise ValueError("mode must be shadow or live")
        if s.mode == "live" and s.daily_loss_limit <= 0:
            raise ValueError("A daily loss limit is required for live trading.")
        if s.max_lots < 1:
            raise ValueError("max_lots must be at least 1")
        if s.order_type not in ("limit_chase", "market"):
            raise ValueError("order_type must be limit_chase or market")
        return s


# ---- alerts --------------------------------------------------------------------------------
_outbox: "queue.Queue | None" = None
_sender: threading.Thread | None = None


def _send_later(job) -> None:
    """Email and Telegram go out from one background thread: an alert never holds up an order (a Gmail login takes
    1-3 s, up to 20 s when slow). At most 200 wait; beyond that the oldest are kept and new ones dropped (the session
    log and the phone notification still have them)."""
    global _outbox, _sender
    import queue
    if _outbox is None:
        _outbox = queue.Queue(maxsize=200)
    try:
        _outbox.put_nowait(job)
    except queue.Full:
        return
    if _sender is None or not _sender.is_alive():
        def drain():
            while True:
                fn = _outbox.get()
                try:
                    fn()
                except Exception:  # noqa: BLE001 - one failed alert must not stop the others
                    pass
                finally:
                    _outbox.task_done()
        _sender = threading.Thread(target=drain, name="algolab-alerts", daemon=True)
        _sender.start()


def make_notifier(trader_config: str | None, log=print, background: bool = True):
    """Email (Gmail) / Telegram through the portfolio dashboard's alert settings (config.json: set on Profile ›
    Email and Telegram alerts), read again for every alert so a change works at once. Sent in the background."""
    def send(subject: str, body: str) -> None:
        try:
            cfg = json.loads(Path(trader_config).read_text()) if trader_config else {}
        except (OSError, ValueError):
            cfg = {}
        e = cfg.get("email", {})
        if e.get("enabled"):
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, e["from"], e.get("to") or e["from"]
                msg.set_content(body)
                with smtplib.SMTP_SSL(e.get("smtp_host", "smtp.gmail.com"), e.get("smtp_port", 465), timeout=20) as s:
                    s.login(e["from"], e["app_password"])
                    s.send_message(msg)
            except Exception as ex:  # noqa: BLE001
                log(f"email alert failed: {ex}")
        t = cfg.get("telegram", {})
        if t.get("enabled"):
            try:
                data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": f"{subject}\n{body}"[:4000]}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data, timeout=20)
            except Exception as ex:  # noqa: BLE001
                log(f"telegram alert failed: {str(ex).replace(str(t.get('bot_token') or '?'), '…')}")

    def notify(subject: str, body: str) -> None:
        log(f"ALERT: {subject} - {body}")
        if background:
            _send_later(lambda: send(subject, body))
        else:
            send(subject, body)
    return notify


# ---- the broker --------------------------------------------------------------------------
class LiveBroker(PaperBroker):
    CANCEL_CONFIRM_TRIES = 5          # reads of a cancelled stop's status (1 s apart) before giving up
    POSITIONS_EVERY = 60              # seconds between position checks against Zerodha while trading
    _unowned_told: set = set()        # (day, symbol) already reported as held by no live session (one alert a day)

    def __init__(self, *a, settings: LiveSettings, notify=None, log=print, sleep=_time.sleep, **k):
        super().__init__(*a, **k)
        self.s = settings
        self.notify = notify or (lambda subj, body: None)
        self.log = log
        self.sleep = sleep
        self.kite = None
        self.symbol_of = lambda c: None
        self.protect: dict[str, str] = {}          # contract label -> protective SL order id
        self.protect_level: dict[str, float] = {}  # contract label -> its trigger now (moved with a trailing stop)
        self._follow_at: dict[str, float] = {}
        self._follow_n: dict[str, int] = {}
        self.orders_today = {"date": "", "n": 0}
        self.reconciled = False
        self.contracts: dict[str, Contract] = {}
        self.lot_of = lambda c: 1
        self.stream = None
        self.others = lambda: {}                   # Zerodha symbol -> qty held by the account's OTHER live sessions
        self._mismatch_prev: set = set()           # symbols that differed at the last position check

    # wiring to the live feed (its Zerodha client and contract list)
    def attach(self, feed) -> None:
        self.kite = getattr(feed, "kite", None)
        if hasattr(feed, "_symbols"):
            self.symbol_of = lambda c: feed._symbols.get((c.expiry, c.strike, c.right))
        else:   # simulated market (tests): every contract "exists"
            self.symbol_of = lambda c: f"NIFTY{c.expiry:%y%m%d}{c.strike}{c.right}"
        self.lot_of = lambda c: feed.lot_size_for(c.expiry)
        self.stream = getattr(feed, "stream", None)   # Zerodha's order updates arrive on the price stream

    # ---- persistence --------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = super().to_dict()
        d["live"] = {"protect": self.protect, "protect_level": self.protect_level, "orders_today": self.orders_today,
                     "contracts": {k: c.to_dict() for k, c in self.contracts.items()}}
        return d

    def load_dict(self, d: dict) -> None:
        super().load_dict(d)
        live = d.get("live") or {}
        self.protect = dict(live.get("protect", {}))
        self.protect_level = {k: float(v) for k, v in (live.get("protect_level") or {}).items()}
        self.orders_today = dict(live.get("orders_today", {"date": "", "n": 0}))
        self.contracts = {k: Contract.from_dict(v) for k, v in live.get("contracts", {}).items()}

    # ---- checks ---------------------------------------------------------------------------
    def reconcile(self) -> None:
        """Our open positions must match Zerodha's net NRML positions for the same contracts. Several live sessions
        may hold the same contract: Zerodha's net must then equal ours plus what the other live sessions hold."""
        if self.s.mode != "live" or self.reconciled:
            return
        net = {}
        for p in (self.kite.positions() or {}).get("net", []):
            if p.get("exchange") == "NFO" and p.get("product") == "NRML":
                net[p["tradingsymbol"]] = net.get(p["tradingsymbol"], 0) + int(p.get("quantity", 0))
        others = self.others() or {}
        diffs = []
        for c, pos in self.positions.items():
            sym = self.symbol_of(c)
            other = others.get(sym, 0) if sym else 0
            if sym is None or net.get(sym, 0) != pos.qty + other:
                diffs.append(f"{c.label}: ours {pos.qty}" + (f" + other live sessions {other}" if other else "")
                             + f", Zerodha {net.get(sym, 0) if sym else 'not listed'}")
        if diffs:
            raise Blocked("Positions differ from Zerodha (" + "; ".join(diffs) + "). Nothing was sent. "
                       "Square off or fix them in Kite, reset this session's account, then start again.")
        self.reconciled = True

    def _zerodha_net(self) -> dict:
        net = {}
        for p in (self.kite.positions() or {}).get("net", []):
            if p.get("exchange") == "NFO" and p.get("product") == "NRML":
                net[p["tradingsymbol"]] = net.get(p["tradingsymbol"], 0) + int(p.get("quantity", 0))
        return net

    def check_positions(self, now) -> None:
        """While trading, every POSITIONS_EVERY s: Zerodha's net NRML position in each contract this session holds must
        equal its own legs plus the other live sessions' legs. A difference seen at two checks in a row (so a
        fill Zerodha has not shown yet is not mistaken for one) stops the session without sending anything.
        Positions at Zerodha that no live session holds (a manual trade, a leftover stop that filled) are reported
        once a day; they do not stop the session (the Positions page keeps manual legs there too)."""
        if self.s.mode != "live" or self.kite is None:
            return
        net = self._zerodha_net()
        others = self.others() or {}
        mine: dict = {}
        diffs, now_bad = [], set()
        for c, pos in self.positions.items():
            sym = self.symbol_of(c)
            if sym:
                mine[sym] = mine.get(sym, 0) + pos.qty
        for sym, q in mine.items():
            other = others.get(sym, 0)
            if net.get(sym, 0) != q + other:
                now_bad.add(sym)
                diffs.append(f"{sym}: ours {q}" + (f" + other live sessions {other}" if other else "")
                             + f", Zerodha {net.get(sym, 0)}")
        repeat = now_bad & self._mismatch_prev
        self._mismatch_prev = now_bad
        if now_bad and not repeat:
            self.log("Position check: differs from Zerodha (" + "; ".join(diffs) + "); checking again in a minute")
        if repeat:
            raise Blocked("Positions differ from Zerodha at two checks in a row (" + "; ".join(diffs) + "). Stopped "
                          "without sending anything; protective stops at Zerodha stay. Fix it in Kite, then reset "
                          "this session's account.")
        day = now.date().isoformat()
        for sym, q in net.items():
            if q and sym not in mine and not others.get(sym) and (day, sym) not in LiveBroker._unowned_told:
                LiveBroker._unowned_told.add((day, sym))
                msg = (f"Zerodha holds {q:+d} {sym} (NRML) that no LIVE session holds: a manual trade, a Positions "
                       "page leg, or a leftover protective stop that filled. Check Kite.")
                self.log(msg)
                self.notify("Position held by no live session", msg)

    def _closing(self, contract: Contract, side: str) -> bool:
        pos = self.positions.get(contract)
        return pos is not None and ((pos.qty < 0 and side == "BUY") or (pos.qty > 0 and side == "SELL"))

    def _guard(self, now, contract, side, qty, closing) -> None:
        today = now.date().isoformat()
        if self.orders_today.get("date") != today:
            self.orders_today = {"date": today, "n": 0}
        if closing:
            return
        lot = max(1, int(self.lot_of(contract) or 1))
        if qty > self.s.max_lots * lot:
            msg = f"Order of {qty} units of {contract.label} is above the limit of {self.s.max_lots} lot(s)."
            if not self.s.allow_over_max_lots:
                raise Halt(msg)
            self.log(f"WARNING: {msg} Placed: larger orders are allowed for this session.")
            self.notify("Order above max lots", f"{msg} Placed, because larger orders are allowed for this session.")
        if self.orders_today["n"] >= self.s.max_orders_per_day:
            raise Halt(f"{self.orders_today['n']} orders to open positions today: the daily limit is reached.")
        sym = self.symbol_of(contract)
        other = (self.others() or {}).get(sym, 0) if sym else 0
        if other and (other > 0) != (side == "BUY"):
            raise Halt(f"{side} {qty} {contract.label} refused: another LIVE session holds {other:+d} of it. Zerodha "
                       "nets the two, so this session's exit would open a new position there (margin, rejection).")

    # ---- orders ------------------------------------------------------------------------------
    def _limit(self, side: str, quote: Quote) -> float:
        px = (quote.bid if side == "SELL" else quote.ask) or quote.last * (1 - self.slippage_pct / 100 if side == "SELL" else 1 + self.slippage_pct / 100)
        ticks = round(px / self.tick, 6)
        px = (math.floor(ticks) if side == "SELL" else math.ceil(ticks)) * self.tick
        return max(round(px, 2), self.tick)

    def _fresh_price(self, sym: str, side: str) -> float | None:
        try:
            d = self.kite.quote([f"NFO:{sym}"])[f"NFO:{sym}"]
            depth = d.get("depth") or {}
            book = depth.get("buy" if side == "SELL" else "sell") or [{}]
            return book[0].get("price") or d.get("last_price")
        except Exception:  # noqa: BLE001
            return None

    def _place_and_wait(self, sym: str, side: str, qty: int, quote: Quote) -> tuple[int, float, str]:
        first = price = self._limit(side, quote)
        market = self.s.order_type == "market"
        if market:   # one limit far enough through the market to fill at once (an exchange-safe "market" order)
            through = price * (1 + self.s.max_chase_pct / 100 if side == "BUY" else 1 - self.s.max_chase_pct / 100)
            ticks = round(through / self.tick, 6)
            first = price = max(round((math.ceil(ticks) if side == "BUY" else math.floor(ticks)) * self.tick, 2), self.tick)
        try:
            oid = self.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type=side,
                                        quantity=qty, product="NRML", order_type="LIMIT", price=price,
                                        validity="DAY", tag="algolab")
        except Exception as e:  # noqa: BLE001
            raise Halt(f"Zerodha refused the order {side} {qty} {sym}: {e}") from None
        self.log(f"Order {oid}: {side} {qty} {sym} LIMIT {price:.2f}")
        last = {}
        for i in range(1, self.s.chase_seconds + 1):
            st = self.stream
            if st is not None and st.healthy():
                # the order's updates are pushed on the stream: react within moments, and still
                # ask Zerodha directly every 3 s in case an update is missed
                upd = st.wait_order(oid, 1.0)
                last = upd if (upd is not None and i % 3) else (self.kite.order_history(oid) or [{}])[-1]
            else:
                self.sleep(1)
                last = (self.kite.order_history(oid) or [{}])[-1]
            status = str(last.get("status", "")).upper()
            if status == "COMPLETE":
                return int(last.get("filled_quantity") or qty), float(last.get("average_price") or price), oid
            if status in ("REJECTED", "CANCELLED"):
                filled = int(last.get("filled_quantity") or 0)
                if filled:
                    return filled, float(last.get("average_price") or price), oid
                raise Halt(f"Zerodha {status.lower()} {side} {qty} {sym}: {last.get('status_message') or ''}".strip())
            if i % 5 == 0 and not market:   # follow the market
                new = self._fresh_price(sym, side)
                if new:
                    new = self._limit(side, Quote(new, bid=new if side == "SELL" else None, ask=new if side == "BUY" else None))
                    if abs(new - first) / first * 100 <= self.s.max_chase_pct and new != price:
                        try:
                            self.kite.modify_order(variety="regular", order_id=oid, price=new)
                            price = new
                            self.log(f"Order {oid}: re-priced to {price:.2f}")
                        except Exception as e:  # noqa: BLE001
                            self.log(f"Order {oid}: re-price failed: {e}")
        try:
            self.kite.cancel_order(variety="regular", order_id=oid)
        except Exception:  # noqa: BLE001
            pass
        last = (self.kite.order_history(oid) or [last])[-1]
        filled = int(last.get("filled_quantity") or 0)
        if filled == 0:
            raise Halt(f"{side} {qty} {sym} did not fill within {self.s.chase_seconds}s; the order was cancelled.")
        return filled, float(last.get("average_price") or price), oid

    def _place_protect(self, contract, sym, sold_px, qty) -> None:
        if self.s.protect_mult <= 0:
            return
        trigger = round(math.ceil(round(sold_px * self.s.protect_mult / self.tick, 6)) * self.tick, 2)
        limit = round(math.ceil(round(trigger * 1.1 / self.tick, 6)) * self.tick, 2)
        try:
            oid = self.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="BUY",
                                        quantity=qty, product="NRML", order_type="SL", price=limit,
                                        trigger_price=trigger, validity="DAY", tag="algolab-stop")
        except Exception as e:  # noqa: BLE001
            self.notify("Protective stop NOT placed", f"{sym}: {e}. The short leg is unprotected at Zerodha.")
            return
        self.protect[contract.label] = str(oid)
        self.protect_level[contract.label] = trigger
        self.log(f"Protective stop {oid}: BUY {qty} {sym} trigger {trigger:.2f}")

    FOLLOW_EVERY = 5.0      # seconds between moves of one stop at Zerodha
    FOLLOW_MAX = 20         # moves per stop order (Zerodha limits changes to one order)

    def follow_stop(self, contract, level: float, now=None) -> None:
        """Orders & stops: a sold leg's stop (trailing) tighter than its protective stop at Zerodha moves that order's
        trigger down to it, so the latest stop holds at the exchange even if the server stops. Never loosens it."""
        label = contract.label
        cur = self.protect_level.get(label)
        if level is None or cur is None or label not in self.protect:
            return
        trig = round(math.ceil(round(level / self.tick, 6)) * self.tick, 2)
        if trig >= cur - self.tick / 2:
            return
        clock = _time.time()
        if clock - self._follow_at.get(label, 0.0) < self.FOLLOW_EVERY or self._follow_n.get(label, 0) >= self.FOLLOW_MAX:
            return
        self._follow_at[label] = clock
        if self.s.mode != "live":
            self.protect_level[label] = trig
            self.log(f"SHADOW (not sent): protective stop for {label} moved to trigger {trig:.2f}")
            return
        limit = round(math.ceil(round(trig * 1.1 / self.tick, 6)) * self.tick, 2)
        try:
            self.kite.modify_order(variety="regular", order_id=self.protect[label], trigger_price=trig, price=limit)
        except Exception as e:  # noqa: BLE001 - the engine's own stop still works; say so once
            self._follow_n[label] = self.FOLLOW_MAX
            self.notify("Stop at Zerodha not moved", f"{label}: {e}. The engine's stop {trig:.2f} still applies.")
            return
        self.protect_level[label] = trig
        self._follow_n[label] = self._follow_n.get(label, 0) + 1
        self.log(f"Protective stop {self.protect[label]}: trigger moved to {trig:.2f} (trailing)")

    def _cancel_protect(self, contract, now) -> bool:
        """Before buying a leg back: cancel its protective stop and wait until Zerodha confirms it is gone.
        True if the stop already bought the whole leg back. A stop that filled in part is booked first, so the
        buy-back covers only the rest. If the stop cannot be confirmed gone, nothing is bought (a second buy could
        flip the leg long): the stop stays on the books and CancelFailed is raised."""
        oid = self.protect.get(contract.label)
        if not oid or self.s.mode != "live":
            self.protect.pop(contract.label, None)
            self.protect_level.pop(contract.label, None)
            return False
        last = (self.kite.order_history(oid) or [{}])[-1]
        status = str(last.get("status", "")).upper()
        if status not in ("COMPLETE", "CANCELLED", "REJECTED"):
            try:
                self.kite.cancel_order(variety="regular", order_id=oid)
            except Exception as e:  # noqa: BLE001 - it may have filled meanwhile: read it again below
                self.log(f"Cancel of protective stop {oid} failed: {e}")
            for _ in range(self.CANCEL_CONFIRM_TRIES):
                last = (self.kite.order_history(oid) or [{}])[-1]
                status = str(last.get("status", "")).upper()
                if status in ("COMPLETE", "CANCELLED", "REJECTED"):
                    break
                self.sleep(1)
        if status not in ("COMPLETE", "CANCELLED", "REJECTED"):
            msg = (f"Protective stop {oid} for {contract.label} could not be cancelled (Zerodha shows "
                   f"{status.lower() or 'nothing'}). The leg was NOT bought back, so it cannot be bought twice; "
                   "its stop is still at Zerodha. Check Kite.")
            self.notify("Protective stop not cancelled", msg)
            raise CancelFailed(msg)
        self.protect.pop(contract.label, None)
        self.protect_level.pop(contract.label, None)
        filled = int(last.get("filled_quantity") or 0)
        if status == "COMPLETE" or filled:
            pos = self.positions.get(contract)
            if pos is not None and filled:
                last = {**last, "filled_quantity": min(filled, abs(pos.qty))}
            self._record_stop(contract, last, now)
            if status != "COMPLETE":
                self.log(f"Protective stop {oid} for {contract.label} filled {filled} before it was cancelled")
                self.notify("Protective stop partly filled", f"{contract.label}: {filled} bought by stop {oid}; "
                            "the rest is bought back now.")
        return contract not in self.positions

    def cancel_all_protective(self, now) -> list:
        """A flat session that stops: cancel any protective stop it still has at Zerodha (none should be left).
        Returns what needs a look in Kite: stops that could not be cancelled, and stops that filled (fully or partly)
        while the session held nothing, which leaves an extra position at Zerodha. Each entry names the order id."""
        problems = []
        for label, oid in list(self.protect.items()):
            if self.s.mode != "live" or not oid:
                self.protect.pop(label, None)
                self.protect_level.pop(label, None)
                continue
            try:
                last = (self.kite.order_history(oid) or [{}])[-1]
                status = str(last.get("status", "")).upper()
                filled = int(last.get("filled_quantity") or 0)
                if status not in ("COMPLETE", "REJECTED", "CANCELLED"):
                    self.kite.cancel_order(variety="regular", order_id=oid)
                    self.log(f"Protective stop {oid} for {label} cancelled (session stopped with no positions)")
                if filled:
                    problems.append(f"protective stop {oid} for {label} bought {filled} while this session held "
                                    "nothing: an extra position may be open")
                    self.notify("Protective stop filled with no position", problems[-1])
            except Exception as e:  # noqa: BLE001 - keep it on the books so it is not forgotten
                self.log(f"Cancel of protective stop {oid} failed: {e}")
                problems.append(f"protective stop {oid} for {label} could not be cancelled ({e}): cancel it in Kite")
                continue
            self.protect.pop(label, None)
            self.protect_level.pop(label, None)
        return problems

    def _record_stop(self, contract, last, now) -> None:
        pos = self.positions.get(contract)
        if pos is None:
            return
        qty = int(last.get("filled_quantity") or abs(pos.qty))
        px = float(last.get("average_price") or 0)
        cost = self.schedule.at(now).order_cost("BUY", px * qty)
        self._apply(now, contract, "BUY", qty, px, cost, "Protective stop at Zerodha", pos.group, "live")

    def check_protective_stops(self, now, from_stream: bool = False) -> None:
        """A protective stop that filled means the market ran far: record it and stop the session.
        from_stream: only look at order updates already received on the price stream (no request)."""
        if self.s.mode != "live" or not self.protect:
            return
        if from_stream and self.stream is None:
            return
        hit = []
        for label, oid in list(self.protect.items()):
            if from_stream:
                last = self.stream.order(oid)
                if not last:
                    continue
            else:
                last = (self.kite.order_history(oid) or [{}])[-1]
            status = str(last.get("status", "")).upper()
            if status == "COMPLETE":
                c = next((c for c in self.positions if c.label == label), None)
                if c is not None:
                    self._record_stop(c, last, now)
                self.protect.pop(label, None)
                self.protect_level.pop(label, None)
                hit.append(label)
            elif status in ("REJECTED", "CANCELLED"):
                self.protect.pop(label, None)
                self.protect_level.pop(label, None)
                self.notify("Protective stop gone", f"{label}: Zerodha shows it {status.lower()}. The leg is unprotected.")
        if hit:
            raise Halt("Protective stop filled at Zerodha for " + ", ".join(hit))

    def execute(self, now, contract, side, qty, quote, reason="", group=""):
        closing = self._closing(contract, side)
        self._guard(now, contract, side, qty, closing)
        sym = self.symbol_of(contract)
        if sym is None:
            raise Halt(f"{contract.label} is not listed on Zerodha.")
        if not closing:
            self.orders_today["n"] += 1
        self.contracts[contract.label] = contract
        if self.s.mode == "shadow":
            self.log(f"SHADOW (not sent): {side} {qty} {sym} LIMIT {self._limit(side, quote):.2f} NRML"
                     + (f"; then protective SL BUY at {self.s.protect_mult:g}x" if side == "SELL" and not closing and self.s.protect_mult > 0 else ""))
            return super().execute(now, contract, side, qty, quote, reason, group)
        if self.kite is None:
            raise Halt("No Zerodha connection.")
        self.reconcile()
        if closing and side == "BUY":
            if self._cancel_protect(contract, now):
                return self.fills[-1]      # the protective stop already bought it back
            qty = min(qty, abs(self.positions[contract].qty))   # a stop that filled in part leaves less to buy
        filled, avg, oid = self._place_and_wait(sym, side, qty, quote)
        cost = self.schedule.at(now).order_cost(side, avg * filled)
        fill = self._apply(now, contract, side, filled, round(avg, 2), cost, reason, group, "live")
        self.notify(f"Algo Lab: {side} {filled} {sym}", f"at {avg:.2f} ({reason}); order {oid}")
        if filled < qty:
            raise Halt(f"Only {filled} of {qty} {sym} filled.")
        if side == "SELL" and not closing:
            self._place_protect(contract, sym, avg, filled)
        return fill

    def execute_many(self, now, items):
        """Several orders of one entry, items = [(contract, side, qty, quote, reason, group), ...]. Shadow takes them
        one by one (nothing is sent). Live checks each one first, then places all the orders at the same time and waits
        for all the fills, so a second leg is not delayed by the wait for the first. Every leg that filled is booked
        (and gets its protective stop) before a failure of another leg stops the session, which then squares off."""
        if self.s.mode != "live" or len(items) < 2:
            return [self.execute(now, *it) for it in items]
        if self.kite is None:
            raise Halt("No Zerodha connection.")
        plan = []
        for contract, side, qty, quote, reason, group in items:
            closing = self._closing(contract, side)
            self._guard(now, contract, side, qty, closing)
            sym = self.symbol_of(contract)
            if sym is None:
                raise Halt(f"{contract.label} is not listed on Zerodha.")
            if not closing:
                self.orders_today["n"] += 1
            self.contracts[contract.label] = contract
            plan.append((contract, side, qty, quote, reason, group, sym, closing))
        self.reconcile()
        fills: list = [None] * len(plan)
        todo = []
        for i, (contract, side, qty, quote, reason, group, sym, closing) in enumerate(plan):
            if closing and side == "BUY":
                if self._cancel_protect(contract, now):
                    fills[i] = self.fills[-1]   # the protective stop already bought it back
                    continue
                left = abs(self.positions[contract].qty)
                if left < qty:                  # a stop that filled in part leaves less to buy
                    plan[i] = (contract, side, left, quote, reason, group, sym, closing)
            todo.append(i)
        from concurrent.futures import ThreadPoolExecutor
        results: dict = {}
        with ThreadPoolExecutor(max_workers=max(1, len(todo))) as pool:
            futs = {i: pool.submit(self._place_and_wait, plan[i][6], plan[i][1], plan[i][2], plan[i][3]) for i in todo}
            for i, f in futs.items():
                try:
                    results[i] = f.result()
                except Exception as e:  # noqa: BLE001 - Halt / Blocked from one leg: the other legs are still booked
                    results[i] = e
        first_error = None
        for i in todo:
            contract, side, qty, quote, reason, group, sym, closing = plan[i]
            r = results[i]
            if isinstance(r, BaseException):
                first_error = first_error or r
                continue
            filled, avg, oid = r
            cost = self.schedule.at(now).order_cost(side, avg * filled)
            fills[i] = self._apply(now, contract, side, filled, round(avg, 2), cost, reason, group, "live")
            self.notify(f"Algo Lab: {side} {filled} {sym}", f"at {avg:.2f} ({reason}); order {oid}")
            if filled < qty:
                first_error = first_error or Halt(f"Only {filled} of {qty} {sym} filled.")
            if side == "SELL" and not closing:
                self._place_protect(contract, sym, avg, filled)
        if first_error is not None:
            raise first_error
        return fills


def settings_summary(s: LiveSettings) -> str:
    d = asdict(s)
    return ", ".join(f"{k} {v}" for k, v in d.items())
