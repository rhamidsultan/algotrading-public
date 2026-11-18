"""Real orders through Zerodha, for a paper-trading session switched to "shadow" or "live".

shadow  everything a live session would do - guards, prices, the exact orders it would send - but
        nothing is sent: fills are simulated as in paper trading, and every order is logged as
        "SHADOW". Use it for a week before going live.
live    orders are sent to Zerodha:
          - LIMIT orders at the best bid (sell) / ask (buy), product NRML (can be held overnight),
            re-priced to the new best bid/ask every few seconds; an order that OPENS a position is
            given up after `chase_seconds` or if the price would move more than `max_chase_pct`
            (at least MIN_CHASE_TICKS ticks) from the first price
          - an order that CLOSES a position is not given up after `chase_seconds`: it is re-sent
            further through the best price each round (CLOSE_STEPS percent, at most
            `close_max_through_pct` and never past the circuit limit) for up to
            `close_retry_seconds` more. Each order is confirmed cancelled before the next is sent,
            so a leg is never bought twice. If it still cannot be closed, its protective stop is
            put back at Zerodha and the session keeps retrying (paper.py, closing only)
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
import secrets
import smtplib
import threading
import time as _time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from pathlib import Path

from .broker import PaperBroker
from .config import ist_now
from .models import Contract, Quote


class Halt(RuntimeError):
    """The session must square off and stop."""


class CancelFailed(Halt):
    """A protective stop could not be confirmed cancelled: the buy-back was not sent."""


class Unconfirmed(Halt):
    """An order may still fill (its cancel was never confirmed, or Zerodha did not answer when it was sent). It is kept
    in LiveBroker.working and nothing more is sent for that leg until it is read as finished. `filled`/`avg`: what did
    fill in this attempt (all its orders), to be booked by the caller."""

    def __init__(self, msg: str, filled: int = 0, avg: float = 0.0):
        super().__init__(msg)
        self.filled, self.avg = filled, avg


class NotPlaced(Halt):
    """Zerodha surely did not take the order (it is not in the day's order book): safe to send again."""


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
    close_retry_seconds: int = 60    # a closing order not filled in chase_seconds keeps going this much longer
    close_max_through_pct: float = 30.0  # ...at most this far through the best price (a limit still fills at the best
    #                                      price there is: this only bounds a fill into an empty book)

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
        if s.close_retry_seconds < 0 or s.close_max_through_pct <= 0:
            raise ValueError("close_retry_seconds must be 0 or more and close_max_through_pct above 0")
        return s


# ---- alerts --------------------------------------------------------------------------------
def make_notifier(trader_config: str | None, log=print):
    """Email / Telegram through the portfolio dashboard's alert settings (config.json), if any."""
    def notify(subject: str, body: str) -> None:
        log(f"ALERT: {subject} - {body}")
        try:
            cfg = json.loads(Path(trader_config).read_text()) if trader_config else {}
        except (OSError, ValueError):
            cfg = {}
        e = cfg.get("email", {})
        if e.get("enabled"):
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, e["from"], e["to"]
                msg.set_content(body)
                with smtplib.SMTP_SSL(e.get("smtp_host", "smtp.gmail.com"), e.get("smtp_port", 465), timeout=20) as s:
                    s.login(e["from"], e["app_password"])
                    s.send_message(msg)
            except Exception as ex:  # noqa: BLE001
                log(f"email alert failed: {ex}")
        t = cfg.get("telegram", {})
        if t.get("enabled"):
            try:
                data = urllib.parse.urlencode({"chat_id": t["chat_id"], "text": f"{subject}\n{body}"}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", data, timeout=20)
            except Exception as ex:  # noqa: BLE001
                log(f"telegram alert failed: {ex}")
    return notify


# ---- the broker --------------------------------------------------------------------------
class LiveBroker(PaperBroker):
    CANCEL_CONFIRM_TRIES = 5          # reads of a cancelled order's status (1 s apart) before giving up
    POSITIONS_EVERY = 60              # seconds between position checks against Zerodha while trading
    MIN_CHASE_TICKS = 3               # an order may always follow the market this many ticks (5% of a Rs 0.60 option
    #                                   is less than one tick: before this, cheap options were never re-priced)
    CLOSE_ROUND_SECONDS = 10          # each further-through try of a closing order
    CLOSE_STEPS = (2.0, 5.0, 10.0, 20.0, 30.0)   # % through the best price, one step per round (capped by settings)
    REPROTECT_ABOVE = 1.2             # a stop put back after a failed close: at least this x the price now
    LOOKUPS_AFTER_ERROR = 3           # reads of the order book (LOOKUP_GAP s apart) for an order sent with an error answer
    LOOKUP_GAP = 3.0
    _unowned_told: set = set()        # (day, symbol) already reported as held by no live session (one alert a day)

    def __init__(self, *a, settings: LiveSettings, notify=None, log=print, sleep=None, **k):
        super().__init__(*a, **k)
        self.s = settings
        self.notify = notify or (lambda subj, body: None)
        self.log = log
        self.sleep = sleep or (lambda seconds: _time.sleep(seconds))
        self.kite = None
        self.symbol_of = lambda c: None
        self.protect: dict[str, str] = {}          # contract label -> protective SL order id
        self.protect_level: dict[str, float] = {}  # contract label -> its trigger now (moved with a trailing stop)
        self.released: dict[str, float] = {}       # contract label -> trigger of a stop taken off to close the leg
        self.working: dict[str, dict] = {}         # tag -> order sent and not yet known finished (also on disk)
        self.journal: Path | None = None           # where `working` is written the moment it changes (restart-safe)
        self._lock = threading.RLock()             # execute_many works orders from threads
        self._quoted = 0.0
        self._held_odd: set = set()                # symbols whose Zerodha position did not fit at the last retry
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
                     "released": self.released, "working": self.working, "contracts": {k: c.to_dict() for k, c in self.contracts.items()}}
        return d

    def load_dict(self, d: dict) -> None:
        super().load_dict(d)
        live = d.get("live") or {}
        self.protect = dict(live.get("protect", {}))
        self.protect_level = {k: float(v) for k, v in (live.get("protect_level") or {}).items()}
        self.released = {k: float(v) for k, v in (live.get("released") or {}).items()}
        self.working = {k: dict(v) for k, v in (live.get("working") or {}).items() if isinstance(v, dict)}
        self._read_journal()
        self.orders_today = dict(live.get("orders_today", {"date": "", "n": 0}))
        self.contracts = {k: Contract.from_dict(v) for k, v in live.get("contracts", {}).items()}

    # ---- checks ---------------------------------------------------------------------------
    def reconcile(self) -> None:
        """Our open positions must match Zerodha's net NRML positions for the same contracts. Several live sessions
        may hold the same contract: Zerodha's net must then equal ours plus what the other live sessions hold."""
        if self.s.mode != "live" or self.reconciled:
            return
        self._read_journal()
        left = self.settle_working(None)
        if left:
            raise Halt("Orders sent before a restart are still working at Zerodha (" + "; ".join(left) + "). Nothing "
                       "more was sent.")
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
        try:   # a stop that is not in today's order book is gone (DAY orders end with the day)
            today = {str(o.get("order_id")): str(o.get("status", "")).upper() for o in self.kite.orders() or []}
        except Exception:  # noqa: BLE001 - unknown: keep them
            today = None
        for c in list(self.positions):          # a short leg whose stop was off when the engine stopped gets it back
            sym = self.symbol_of(c)
            oid = self.protect.get(c.label)
            if oid and today is not None and today.get(oid, "CANCELLED") in ("CANCELLED", "REJECTED"):
                self.log(f"Protective stop {oid} for {c.label} is no longer at Zerodha; placing a new one")
                self.protect.pop(c.label, None)
                level = self.protect_level.pop(c.label, None)
                if level:
                    self.released[c.label] = level
            if sym:
                self._reprotect(c, sym)

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

    def _tick_px(self, side: str, px: float) -> float:
        """A limit price on the tick grid, rounded towards the market (up to buy, down to sell)."""
        ticks = round(px / self.tick, 6)
        return max(round((math.ceil(ticks) if side == "BUY" else math.floor(ticks)) * self.tick, 2), self.tick)

    # ---- orders in flight (restart-safe) -------------------------------------------------
    def _read_journal(self) -> None:
        if self.journal is None:
            return
        try:
            on_disk = json.loads(self.journal.read_text())
        except (OSError, ValueError):
            return
        with self._lock:
            for tag, w in (on_disk or {}).items():
                self.working.setdefault(tag, dict(w))

    def _write_journal(self) -> None:
        if self.journal is None:
            return
        try:
            tmp = self.journal.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.working))
            tmp.replace(self.journal)
        except OSError as e:
            self.log(f"working orders not saved: {e}")

    def _track(self, tag: str, **fields) -> None:
        with self._lock:
            if fields.get("done"):
                self.working.pop(tag, None)
            else:
                self.working.setdefault(tag, {}).update(fields)
            self._write_journal()

    def _find_by_tag(self, tag: str) -> dict | None:
        """The day's order with this tag at Zerodha, None if there is none. Raises if the order book cannot be read."""
        for o in self.kite.orders() or []:
            if o.get("tag") == tag:
                return o
        return None

    def _open_orders(self, sym: str) -> list:
        """Orders still working at Zerodha for this symbol, Algo Lab's own protective stops left out (any session's:
        they only trigger far away). Raises if they cannot be read."""
        return [o for o in self.kite.orders() or [] if o.get("tradingsymbol") == sym
                and not str(o.get("tag") or "").startswith("algolab-stop")
                and str(o.get("status", "")).upper() not in self.FINISHED]

    def settle_working(self, now, label: str | None = None) -> list:
        """Read every order that may still fill (all, or one leg's): book what it filled that is not booked yet and
        forget it once finished. Returns what is still working (as text), after trying to cancel it once more."""
        left = []
        for tag, w in list(self.working.items()):
            if label is not None and w.get("label") != label:
                continue
            oid = w.get("oid") or ""
            try:
                if not oid:                         # sent, but Zerodha's answer was lost: look it up by its tag
                    o = self._find_by_tag(tag)
                    if o is None:
                        self._track(tag, done=True)  # never reached Zerodha
                        continue
                    oid = str(o.get("order_id"))
                    self._track(tag, oid=oid)
                last = self._confirm_cancel(oid, {})
            except Exception as e:  # noqa: BLE001
                left.append(f"{w.get('label')}: order {oid or tag} not read ({e})")
                continue
            if str(last.get("status", "")).upper() not in self.FINISHED:
                left.append(f"{w.get('label')}: order {oid} still working")
                continue
            got = int(last.get("filled_quantity") or 0) - int(w.get("booked") or 0)
            c = self.contracts.get(w.get("label"))
            if got > 0 and c is None:
                self.notify("Algo Lab: fill of an unknown leg", f"order {oid}: {w.get('side')} {got} {w.get('sym')} "
                            "filled, but this session has no such leg (e.g. after a reset). Check Kite.")
            if got > 0 and c is not None:
                side = w["side"]
                pos = self.positions.get(c)
                if pos is not None and ((pos.qty < 0) == (side == "BUY")):
                    got = min(got, abs(pos.qty))    # never book a close beyond the leg
                avg = float(last.get("average_price") or 0)
                when = now or ist_now()
                self._apply(when, c, side, got, round(avg, 2), self.schedule.at(when).order_cost(side, avg * got),
                            "Fill read later", w.get("group", ""), "live")
                msg = f"order {oid}: {side} {got} {w.get('sym')} at {avg:.2f} filled while unread; booked now"
                self.log(msg)
                self.notify("Algo Lab: late fill booked", f"{w.get('label')}: {msg}")
            self._track(tag, done=True)
            if c is not None and c in self.positions:
                if got > 0 and w.get("side") == "SELL":
                    self._resize_protect(c, w.get("sym"))
                self._reprotect(c, w.get("sym"))
        return left

    def _resize_protect(self, contract, sym) -> None:
        """A short leg that grew (a late fill) while it had a stop: replace the stop so it covers the whole leg."""
        if contract.label not in self.protect or self.s.mode != "live":
            return
        try:
            if self._cancel_protect(contract, ist_now()):
                return
        except Exception as e:  # noqa: BLE001 - a stop not confirmed gone stays; a read error: put one back if off
            self.notify("Protective stop not resized", f"{contract.label}: {e}")
        self._reprotect(contract, sym)

    def _touch(self, sym: str, side: str) -> tuple[float | None, float | None]:
        """The best price to trade against now (ask to buy, bid to sell; last price if that side is empty) and the
        circuit limit on that side, from one quote request (at most one a second from this session: Zerodha's limit).
        (None, None) if Zerodha does not answer."""
        with self._lock:
            wait = 1.0 - (_time.monotonic() - self._quoted)
            if 0 < wait <= 1.0:
                self.sleep(wait)
            self._quoted = _time.monotonic()
            try:
                d = self.kite.quote([f"NFO:{sym}"])[f"NFO:{sym}"]
            except Exception:  # noqa: BLE001
                return None, None
        depth = d.get("depth") or {}
        book = depth.get("buy" if side == "SELL" else "sell") or [{}]
        px = (book[0] or {}).get("price") or d.get("last_price")
        band = d.get("upper_circuit_limit") if side == "BUY" else d.get("lower_circuit_limit")
        return (float(px) if px else None), (float(band) if band else None)

    def _fresh_price(self, sym: str, side: str) -> float | None:
        return self._touch(sym, side)[0]

    def _confirm_cancel(self, oid: str, last: dict) -> dict:
        """Cancel an order and read it until Zerodha shows it finished (CANCELLED, COMPLETE or REJECTED), so what it
        filled is known before anything else is sent. Returns its last state (not finished if it never confirmed)."""
        try:
            self.kite.cancel_order(variety="regular", order_id=oid)
        except Exception as e:  # noqa: BLE001 - it may have filled meanwhile: read it below
            self.log(f"Order {oid}: cancel failed: {e}")
        for n in range(self.CANCEL_CONFIRM_TRIES):
            try:
                last = (self.kite.order_history(oid) or [last])[-1]
            except Exception as e:  # noqa: BLE001
                self.log(f"Order {oid}: status not read: {e}")
            if str(last.get("status", "")).upper() in self.FINISHED:
                return last
            if n < self.CANCEL_CONFIRM_TRIES - 1:
                self.sleep(1)
        return last

    FINISHED = ("COMPLETE", "CANCELLED", "REJECTED")

    def _work(self, sym, side, qty, price, seconds, reprice, label="", group="") -> tuple[int, float, str, str, str, int]:
        """Place one LIMIT order and work it for `seconds`: every 5 s reprice(current price) may give a new price.
        Not filled by then: cancelled and confirmed. Returns (filled, average price, order id, status, message, seconds
        used) with status COMPLETE, REJECTED, CANCELLED (by Zerodha or the exchange) or TIMEOUT (we cancelled it).
        Raises NotPlaced if Zerodha surely did not take it, Unconfirmed if it may still fill (it stays in `working`,
        on disk from before it is sent, so a restart finds it)."""
        tag = "algolab" + secrets.token_hex(3)
        self._track(tag, sym=sym, side=side, qty=qty, label=label, group=group, oid="", booked=0)
        try:
            oid = str(self.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type=side,
                                            quantity=qty, product="NRML", order_type="LIMIT", price=price,
                                            validity="DAY", tag=tag))
        except Exception as e:  # noqa: BLE001 - it may still have reached Zerodha: look for it before anything else
            found = None
            for n in range(self.LOOKUPS_AFTER_ERROR):   # a queued order can show in the order book a few seconds late
                try:
                    found = self._find_by_tag(tag)
                except Exception:  # noqa: BLE001
                    raise Unconfirmed(f"{side} {qty} {sym}: Zerodha did not answer ({e}) and its order book could not "
                                      "be read; nothing more is sent until it is.") from None
                if found is not None:
                    break
                if n < self.LOOKUPS_AFTER_ERROR - 1:
                    self.sleep(self.LOOKUP_GAP)
            if found is None:
                self._track(tag, done=True)
                raise NotPlaced(f"Zerodha refused the order {side} {qty} {sym}: {e}") from None
            oid = str(found.get("order_id"))
            self.log(f"Order {oid}: placed although Zerodha answered with an error ({e})")
        self._track(tag, oid=oid)
        self.log(f"Order {oid}: {side} {qty} {sym} LIMIT {price:.2f}")
        last: dict = {}
        for i in range(1, seconds + 1):
            try:
                st = self.stream
                if st is not None and st.healthy():
                    # the order's updates are pushed on the stream: react within moments, and still
                    # ask Zerodha directly every 3 s in case an update is missed
                    upd = st.wait_order(oid, 1.0)
                    last = upd if (upd is not None and i % 3) else (self.kite.order_history(oid) or [{}])[-1]
                else:
                    self.sleep(1)
                    last = (self.kite.order_history(oid) or [{}])[-1]
            except Exception as e:  # noqa: BLE001 - a missed read: the order is still ours to work
                self.log(f"Order {oid}: status not read: {e}")
                continue
            status = str(last.get("status", "")).upper()
            if status == "COMPLETE":
                self._track(tag, done=True)
                return (int(last.get("filled_quantity") or qty), float(last.get("average_price") or price), oid, status,
                        "", i)
            if status in ("REJECTED", "CANCELLED"):
                self._track(tag, done=True)
                return (int(last.get("filled_quantity") or 0), float(last.get("average_price") or price), oid, status,
                        str(last.get("status_message") or ""), i)
            if i % 5 == 0 and i < seconds:   # follow the market
                new = reprice(price)
                if new and new != price:
                    try:
                        self.kite.modify_order(variety="regular", order_id=oid, price=new)
                        price = new
                        self.log(f"Order {oid}: re-priced to {price:.2f}")
                    except Exception as e:  # noqa: BLE001
                        self.log(f"Order {oid}: re-price failed: {e}")
        last = self._confirm_cancel(oid, last)
        status = str(last.get("status", "")).upper()
        filled = int(last.get("filled_quantity") or 0)
        avg = float(last.get("average_price") or price)
        if status not in self.FINISHED:
            self._track(tag, booked=filled)
            raise Unconfirmed(f"{side} {qty} {sym}: order {oid} could not be confirmed cancelled ({filled} filled). "
                              "Nothing more is sent for it until it is read, so it cannot fill twice.", filled, avg)
        self._track(tag, done=True)
        if status == "COMPLETE":
            filled = filled or qty
        return filled, avg, oid, ("COMPLETE" if status == "COMPLETE" else "TIMEOUT"), "", seconds

    def _place_and_wait(self, sym: str, side: str, qty: int, quote: Quote, closing: bool = False, label: str = "",
                        group: str = "", retry: bool = False) -> tuple[int, float, str]:
        """Send one order and wait for it. Opening: the chase, then given up. Closing: the chase, then _close_rest
        (retry: straight to _close_rest at its furthest step, for one short try). Raises Unconfirmed with what filled
        if an order may still fill."""
        if retry and closing:
            return self._close_rest(sym, side, qty, 0, 0.0, "", "RETRY", "", label, group,
                                    budget=2 * self.CLOSE_ROUND_SECONDS, start_top=True)
        first = price = self._limit(side, quote)
        market = self.s.order_type == "market"
        if market:   # one limit far enough through the market to fill at once (an exchange-safe "market" order)
            first = price = self._tick_px(side, price * (1 + self.s.max_chase_pct / 100 if side == "BUY"
                                                         else 1 - self.s.max_chase_pct / 100))
        room = max(first * self.s.max_chase_pct / 100, self.MIN_CHASE_TICKS * self.tick) + 1e-9

        def follow(cur):
            if market:
                return None
            new = self._fresh_price(sym, side)
            if not new:
                return None
            new = self._tick_px(side, new)
            return new if abs(new - first) <= room else None

        try:
            filled, avg, oid, status, msg, _ = self._work(sym, side, qty, price, self.s.chase_seconds, follow, label, group)
        except NotPlaced as e:
            if not closing:
                raise
            self.log(f"{e}; trying again")
            filled, avg, oid, status, msg = 0, 0.0, "", "ERROR", str(e)
        if filled >= qty or (filled and not closing):
            return min(filled, qty), avg, oid      # a part fill is booked; execute() then stops the session
        if not closing:
            if status in ("REJECTED", "CANCELLED"):
                raise Halt(f"Zerodha {status.lower()} {side} {qty} {sym}: {msg}".strip())
            raise Halt(f"{side} {qty} {sym} did not fill within {self.s.chase_seconds}s; the order was cancelled.")
        return self._close_rest(sym, side, qty, filled, avg, oid, status, msg, label, group)

    def _close_rest(self, sym, side, qty, filled, avg, oid, status, msg, label="", group="", budget=None,
                    start_top=False) -> tuple[int, float, str]:
        """A closing order that did not fill in full within chase_seconds: send what is left again, further through the
        best price each 10 s round (CLOSE_STEPS, capped at close_max_through_pct and the circuit limit) for
        close_retry_seconds (or `budget`). A step Zerodha rejects (e.g. a price band) is not tried again: the furthest
        step is lowered below it. Every order is confirmed finished before the next is sent. Returns what filled in
        all; raises Halt if nothing did, Unconfirmed (with what filled) if an order may still fill."""
        buy = side == "BUY"
        value = filled * avg
        cap = self.s.close_max_through_pct
        steps = [p for p in self.CLOSE_STEPS if p < cap] + [cap]
        top = len(steps) - 1
        budget = self.s.close_retry_seconds if budget is None else budget
        if status != "RETRY":
            why = f"{status.lower()} ({msg})" if msg else f"not filled in {self.s.chase_seconds}s"
            self.log(f"Closing {side} {qty} {sym}: {why}, {filled} filled; sending the rest further through the market "
                     f"(up to {cap:g}%, {budget}s)")
        k = top if start_top else 0
        spent, loops = 0, 0
        while spent < budget and loops < 4 * budget:
            loops += 1
            left = qty - filled
            if left <= 0:
                break
            pct = steps[min(k, top)]

            def aim(pct=pct):
                touch, band = self._touch(sym, side)
                if not touch:
                    return None
                px = self._tick_px(side, touch * (1 + pct / 100 if buy else 1 - pct / 100))
                if band:
                    px = min(px, self._tick_px("SELL", band)) if buy else max(px, self._tick_px("BUY", band))
                return px

            px = aim()
            if px is None:
                self.log(f"Closing {sym}: no price from Zerodha; trying again")
                self.sleep(1)
                spent += 1
                continue

            def further(cur, aim=aim):   # only ever further through: a higher buy limit still fills at the best ask
                new = aim()
                return new if new is not None and (new > cur if buy else new < cur) else None

            try:
                f, a, oid2, st, m, used = self._work(sym, side, left, px, self.CLOSE_ROUND_SECONDS, further, label, group)
            except NotPlaced as e:
                self.log(f"Closing {sym}: {e}; trying again")
                self.sleep(1)
                spent += 1
                continue
            except Unconfirmed as e:
                total = filled + e.filled
                raise Unconfirmed(str(e), total, (value + e.filled * e.avg) / total if total else 0.0) from None
            spent += used
            oid = oid2
            if f:
                filled += f
                value += f * a
            if st in ("REJECTED", "CANCELLED") and not f:
                self.log(f"Closing {sym}: Zerodha {st.lower()} the order at {px:.2f} ({m}); not going that far again")
                top = max(0, min(k, top) - 1)
                k = top
            else:
                k = min(k + 1, top)
        if filled == 0:
            raise Halt(f"{side} {qty} {sym} did not fill: tried {budget}s more, up to {cap:g}% through the best price; "
                       "every order was cancelled.")
        return min(filled, qty), value / filled, oid

    def _place_protect(self, contract, sym, sold_px, qty, trigger: float | None = None) -> bool:
        if self.s.protect_mult <= 0:
            return False
        if trigger is None:
            trigger = sold_px * self.s.protect_mult
        trigger = round(math.ceil(round(trigger / self.tick, 6)) * self.tick, 2)
        limit = round(math.ceil(round(trigger * 1.1 / self.tick, 6)) * self.tick, 2)
        try:
            oid = self.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="BUY",
                                        quantity=qty, product="NRML", order_type="SL", price=limit,
                                        trigger_price=trigger, validity="DAY", tag="algolab-stop")
        except Exception as e:  # noqa: BLE001
            self.notify("Protective stop NOT placed", f"{sym}: {e}. The short leg is unprotected at Zerodha.")
            return False
        self.protect[contract.label] = str(oid)
        self.protect_level[contract.label] = trigger
        self.log(f"Protective stop {oid}: BUY {qty} {sym} trigger {trigger:.2f}")
        return True

    def _reprotect(self, contract, sym) -> None:
        """A short leg that could not be bought back after its protective stop was taken off: put the stop back at
        Zerodha for what is still held, at its old trigger, or REPROTECT_ABOVE x the price now if the market is
        already past that (a buy stop must trigger above the market)."""
        pos = self.positions.get(contract)
        if self.s.mode != "live" or pos is None or pos.qty >= 0 or contract.label in self.protect:
            return
        if any(w.get("label") == contract.label and w.get("side") == "BUY" for w in self.working.values()):
            return                               # a buy that may still fill: a stop as well could buy it twice
        level = self.released.get(contract.label) or pos.avg_price * self.s.protect_mult
        now_px = self._fresh_price(sym, "BUY")
        if now_px:
            level = max(level, now_px * self.REPROTECT_ABOVE)
        if self._place_protect(contract, sym, pos.avg_price, abs(pos.qty), trigger=level):
            self.released.pop(contract.label, None)
            self.notify("Protective stop put back", f"{contract.label}: could not be bought back yet; a stop is back at "
                        f"Zerodha (trigger {self.protect_level[contract.label]:.2f}) while Algo Lab keeps trying.")

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
        level = self.protect_level.pop(contract.label, None)
        if level is not None and status == "CANCELLED":
            self.released[contract.label] = level
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

    def _book_unconfirmed(self, now, contract, side, e, reason, group, sym, closing) -> None:
        if e.filled:
            cost = self.schedule.at(now).order_cost(side, e.avg * e.filled)
            self._apply(now, contract, side, e.filled, round(e.avg, 2), cost, reason, group, "live")
        if closing:
            self._reprotect(contract, sym)          # skipped while the buy may still fill
        elif side == "SELL" and e.filled:
            self._resize_protect(contract, sym)     # a leg that already had a stop
            if contract.label not in self.protect:
                self._place_protect(contract, sym, e.avg, abs(self.positions[contract].qty))
        self.notify("Order not confirmed at Zerodha", f"{contract.label}: {e}")

    def execute(self, now, contract, side, qty, quote, reason="", group="", retry=False):
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
        try:
            filled, avg, oid = self._place_and_wait(sym, side, qty, quote, closing=closing, label=contract.label,
                                                    group=group, retry=retry)
        except Unconfirmed as e:                    # may still fill: it stays in `working`; book what did fill
            self._book_unconfirmed(now, contract, side, e, reason, group, sym, closing)
            raise
        except Halt:
            if closing:
                self._reprotect(contract, sym)      # its stop was taken off for this buy-back: put it back
            raise
        cost = self.schedule.at(now).order_cost(side, avg * filled)
        fill = self._apply(now, contract, side, filled, round(avg, 2), cost, reason, group, "live")
        self.notify(f"Algo Lab: {side} {filled} {sym}", f"at {avg:.2f} ({reason}); order {oid}")
        if closing:
            self._reprotect(contract, sym)          # only if part of a short leg is still held
            if contract not in self.positions:
                self.released.pop(contract.label, None)
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
            futs = {i: pool.submit(self._place_and_wait, plan[i][6], plan[i][1], plan[i][2], plan[i][3], plan[i][7],
                                   plan[i][0].label, plan[i][5]) for i in todo}
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
                if isinstance(r, Unconfirmed):
                    self._book_unconfirmed(now, contract, side, r, reason, group, sym, closing)
                elif closing:
                    self._reprotect(contract, sym)
                continue
            filled, avg, oid = r
            cost = self.schedule.at(now).order_cost(side, avg * filled)
            fills[i] = self._apply(now, contract, side, filled, round(avg, 2), cost, reason, group, "live")
            self.notify(f"Algo Lab: {side} {filled} {sym}", f"at {avg:.2f} ({reason}); order {oid}")
            if closing:
                self._reprotect(contract, sym)
                if contract not in self.positions:
                    self.released.pop(contract.label, None)
            if filled < qty:
                first_error = first_error or Halt(f"Only {filled} of {qty} {sym} filled.")
            if side == "SELL" and not closing:
                self._place_protect(contract, sym, avg, filled)
        if first_error is not None:
            raise first_error
        return fills


    def retry_close(self, now, contract, quote, reason="Square-off (retry)", quick=False):
        """Close a leg for a square-off, or again after one that failed, sending only what Zerodha still holds for this
        session, so a retry can never open a new position:
          - an earlier order that may still fill is read (and its fill booked) first; still working: nothing is sent
          - any other order working at Zerodha on the symbol (e.g. one placed in Kite): nothing is sent
          - a leg closed in Kite or by its stop meanwhile is booked as closed and not traded again
          - Zerodha's position on the other side, or larger: Blocked (after two reads in a row: positions lag)
        quick: one short try straight at the furthest step (the closing-only retries, so other sessions are not held up).
        Returns the fill, or None if nothing needed sending. Raises Halt if it is still not closed."""
        pos = self.positions.get(contract)
        if pos is None:
            return None
        if self.s.mode != "live":
            return self.execute(now, contract, "BUY" if pos.qty < 0 else "SELL", abs(pos.qty), quote, reason, pos.group)
        if self.kite is None:
            raise Halt("No Zerodha connection.")
        self.reconciled = True          # the per-symbol check below replaces the whole-account one here
        sym = self.symbol_of(contract)
        left = self.settle_working(now, contract.label)
        if left:
            raise Halt("; ".join(left) + "; nothing more sent")
        pos = self.positions.get(contract)
        if pos is None:
            return self.fills[-1]
        try:
            others_open = [o for o in self._open_orders(sym)]
        except Exception as e:  # noqa: BLE001
            raise Halt(f"Zerodha's order book not read ({e}); nothing sent") from None
        if others_open:
            ids = ", ".join(str(o.get("order_id")) for o in others_open)
            raise Halt(f"order {ids} is working at Zerodha for {sym} (e.g. placed in Kite); nothing more sent, so "
                       "the two cannot both fill")

        def held_now():
            held = self._zerodha_net().get(sym, 0) - (self.others() or {}).get(sym, 0)
            if held * pos.qty < 0 or abs(held) > abs(pos.qty):   # nothing touched: its stop stays
                msg = (f"{contract.label}: Zerodha holds {held:+d} for this session, the session {pos.qty:+d}. Not "
                       "traded, so nothing can be doubled.")
                if sym in self._held_odd:
                    raise Blocked(msg + " Check Kite.")
                self._held_odd.add(sym)
                raise Halt(msg + " Reading again at the next try.")
            self._held_odd.discard(sym)
            return held

        held_now()
        if pos.qty < 0 and self._cancel_protect(contract, now):
            return self.fills[-1]               # its stop bought it back meanwhile
        try:
            pos = self.positions[contract]
            held = held_now()                   # read again: the stop is off now, so this is final
            side = "BUY" if pos.qty < 0 else "SELL"
            gone = abs(pos.qty) - abs(held)
            if gone:   # closed outside Algo Lab: booked at the price now (Zerodha's own fill price is in Kite)
                if (self.others() or {}).get(sym):
                    raise Blocked(f"{contract.label}: Zerodha holds {gone} less than this session and another live "
                                  "session holds the same contract (its stop may have filled): whose it is cannot be "
                                  "told. Not traded. Check Kite.")
                px = self._limit(side, quote)
                self._apply(now, contract, side, gone, px, self.schedule.at(now).order_cost(side, px * gone),
                            "Closed outside Algo Lab (Kite)", pos.group, "live")
                msg = f"{contract.label}: {gone} closed outside Algo Lab (Kite); booked at {px:.2f}, Kite has the real price"
                self.log(msg)
                self.notify("Algo Lab: leg closed outside", msg)
                if contract not in self.positions:
                    self.released.pop(contract.label, None)
                    return self.fills[-1]
            pos = self.positions[contract]
            return self.execute(now, contract, side, abs(pos.qty), quote, reason, pos.group, retry=quick)
        except Unconfirmed:
            raise
        except Exception:
            self._reprotect(contract, sym)      # whatever went wrong after its stop came off: put it back
            raise


def settings_summary(s: LiveSettings) -> str:
    d = asdict(s)
    return ", ".join(f"{k} {v}" for k, v in d.items())
