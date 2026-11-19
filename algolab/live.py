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
import re
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


class Unknown(Exception):
    """Not known yet (e.g. whether a stop whose answer was lost reached Zerodha): try again later."""


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
        self.reconciled_on = ""                    # the day reconcile() last passed: a new day checks everything again
        self.protect_day: dict[str, str] = {}      # contract label -> day its stop was placed (DAY orders end with it)
        self.renew: set[str] = set()               # short legs whose stop must still be placed again (tried each check)
        self._day = ""                             # today, as last given to the broker (dates new orders)
        self._read_err_at: dict[str, float] = {}   # "not read" log lines: when each kind was last logged
        self._gone_odd: dict[str, float] = {}      # symbol -> when it first did not fit Zerodha (deferred check)
        self._told: set = set()                    # alerts already sent (kind, label, day)
        self.pending_stop: dict[str, dict] = {}    # label -> {"tag", "at"} of a stop whose placement answer was lost
        self.stop_reads_failed = 0                 # protective stops not read at the last full check
        self._not_held: set = set()                # legs Zerodha no longer holds: no stop is placed again for them
        self.contracts: dict[str, Contract] = {}
        self.lot_of = lambda c: 1
        self.stream = None
        self.others = lambda: {}                   # Zerodha symbol -> qty held by the account's OTHER live sessions
        self._mismatch_prev: set = set()           # symbols that differed at the last position check
        self.synced: dict[str, int] = {}           # Kite order id -> units of it already booked as a close in Kite
        self._sync_seen: set = set()               # symbols seen with less at Zerodha, no Kite order found yet
        self.stop_ceiling: dict[str, tuple] = {}   # label -> (day, upper circuit limit Zerodha named for its stop)
        self.outside: list = []                    # (contract, units) closed in Kite, for the strategy (on_outside_close)
        self.exit_edits: dict[str, dict] = {}      # trade key -> {"was", "price", "by", "at"}: exit prices edited by hand

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
                     "protect_day": self.protect_day, "renew": sorted(self.renew), "pending_stop": self.pending_stop,
                     "synced": dict(list(self.synced.items())[-200:]), "empty_sync_at": self._empty_sync_at,
                     "exit_edits": self.exit_edits,
                     "released": self.released, "working": self.working, "contracts": {k: c.to_dict() for k, c in self.contracts.items()}}
        return d

    def load_dict(self, d: dict) -> None:
        super().load_dict(d)
        live = d.get("live") or {}
        self.protect = dict(live.get("protect", {}))
        self.protect_level = {k: float(v) for k, v in (live.get("protect_level") or {}).items()}
        self.released = {k: float(v) for k, v in (live.get("released") or {}).items()}
        self.protect_day = dict(live.get("protect_day") or {})
        self.renew = set(live.get("renew") or [])
        self.synced = {str(k): int(v) for k, v in (live.get("synced") or {}).items()}
        self._empty_sync_at = float(live.get("empty_sync_at") or 0.0)
        self.exit_edits = {str(k): dict(v) for k, v in (live.get("exit_edits") or {}).items() if isinstance(v, dict)}   # a Sync pressed once (a stopped card restarts)
        self.pending_stop = {k: (v if isinstance(v, dict) else {"tag": str(v), "at": 0.0})
                             for k, v in (live.get("pending_stop") or {}).items()}
        self.working = {k: dict(v) for k, v in (live.get("working") or {}).items() if isinstance(v, dict)}
        self._read_journal()
        self.orders_today = dict(live.get("orders_today", {"date": "", "n": 0}))
        self.contracts = {k: Contract.from_dict(v) for k, v in live.get("contracts", {}).items()}

    # ---- checks ---------------------------------------------------------------------------
    def reconcile(self, now=None) -> None:
        """Our open positions must match Zerodha's net NRML positions for the same contracts. Several live sessions
        may hold the same contract: Zerodha's net must then equal ours plus what the other live sessions hold.
        Runs before the first order and again on each new day: protective stops are DAY orders, so the morning after
        a leg was held overnight Zerodha no longer knows them; they are found gone here and placed again (also for
        the legs that do match when another leg does not). Legs past their expiry are left to the settlement."""
        day = self._today(now)
        if self.reconciled and self.reconciled_on and self.reconciled_on != day:
            self.reconciled = False
            self._gone_odd.clear()
            self.log("New day: checking positions and protective stops at Zerodha again")
        if self.s.mode != "live" or self.reconciled:
            return
        self._read_journal()
        left = self.settle_working(now)
        if left:
            raise Halt("Orders sent before a restart are still working at Zerodha (" + "; ".join(left) + "). Nothing "
                       "more was sent.")
        self.sync_outside(now)                        # legs squared off in Kite meanwhile, with their Kite order
        net = self._zerodha_net()
        others = self.others() or {}
        diffs, bad = [], set()
        for c, pos in self.positions.items():
            if self._expired(c, day):
                continue
            sym = self.symbol_of(c)
            other = others.get(sym, 0) if sym else 0
            if sym is None or net.get(sym, 0) != pos.qty + other:
                bad.add(c)
                diffs.append(f"{c.label}: ours {pos.qty}" + (f" + other live sessions {other}" if other else "")
                             + f", Zerodha {net.get(sym, 0) if sym else 'not listed'}")
        if not self.sync_only:                        # a sync-only run places no new orders
            self._renew_day_stops(day, skip=bad, tell=not diffs)
        if diffs:
            naked = [c.label for c in self.positions if self.positions[c].qty < 0 and not self._expired(c, day)
                     and (c.label not in self.protect or (self.protect_day.get(c.label) or "") < day)]
            raise Blocked("Positions differ from Zerodha (" + "; ".join(diffs) + "). Nothing was sent. "
                          + (f"NO protective stop at Zerodha: {', '.join(naked)}. " if naked else "")
                          + "A leg you squared off in Kite today is synced by itself; one closed on an earlier day "
                          "(or with no order of yours in Kite) is booked when you press Sync on the card. Anything "
                          "else (more at Zerodha, the other way round, a contract another live session also holds): "
                          "fix it in Kite, then Sync.")
        self.reconciled, self.reconciled_on = True, day

    def _renew_day_stops(self, day: str, skip=(), tell: bool = True) -> None:
        """Stops not in today's order book: ended with an earlier day (or cancelled): place them again for the short
        legs Zerodha still holds (`skip`: legs that did not match it). A stop placed today and not listed yet (the
        order book can be a few seconds late) is kept."""
        try:
            book = {str(o.get("order_id")): str(o.get("status", "")).upper() for o in self.kite.orders() or []}
        except Exception as e:  # noqa: BLE001 - unknown: keep them; the 30 s stop check reads them one by one
            self._not_read(f"Order book not read ({e}); stops checked one by one later", "book")
            book = None
        for c in list(self.positions):
            if c in skip or self._expired(c, day):
                continue
            label, oid = c.label, self.protect.get(c.label)
            if oid and book is not None:
                state = book.get(oid)
                placed = self.protect_day.get(label)
                if state is None and placed is None:      # saved before stops were dated: ask Zerodha for it
                    try:
                        state = str(self._order_last(oid, None, day).get("status", "")).upper()
                    except Exception:  # noqa: BLE001 - cannot tell: keep it
                        state = "UNKNOWN"
                    if state not in ("GONE", "CANCELLED", "REJECTED"):
                        self.protect_day[label] = day if state != "UNKNOWN" else ""
                if state in ("CANCELLED", "REJECTED", "GONE") or (state is None and (placed or "") < day):
                    self.log(f"Protective stop {oid} for {label} is no longer at Zerodha; placing a new one")
                    self._drop_stop(label)
            if c.label not in self.protect and self.positions[c].qty < 0:
                self.renew.add(label)
        self._renew_stops(tell)

    def _zerodha_net(self) -> dict:
        net = {}
        for p in (self.kite.positions() or {}).get("net", []):
            if p.get("exchange") == "NFO" and p.get("product") == "NRML":
                net[p["tradingsymbol"]] = net.get(p["tradingsymbol"], 0) + int(p.get("quantity", 0))
        return net

    KITE_CLOSE = "Closed in Kite"

    @staticmethod
    def trade_key(contract: str, exit_time, qty) -> str:
        """One closed leg (a trade): its contract, exit time (ISO, as saved) and quantity."""
        t = exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time)
        return f"{contract}|{t}|{int(qty)}"

    def edit_exit(self, key: str, price: float, by: str = "", at: str = "") -> str:
        """Correct the exit price of a closed leg (e.g. a square-off in Kite booked at an estimate): the trade, its
        exit fill and the realised P&L move by the difference; charges stay as booked. The first price booked is
        kept (exit_edits). Nothing is sent to Zerodha. Returns a line for the log; raises ValueError."""
        price = round(float(price), 2)
        if not price > 0:
            raise ValueError("The exit price must be above 0.")
        t = next((t for t in self.trades if self.trade_key(t.contract, t.exit_time, t.qty) == key), None)
        if t is None:
            raise ValueError("That closed leg was not found.")
        old = t.exit_price
        d = round((price - old) * t.qty * (1 if t.side == "LONG" else -1), 2)
        t.exit_price, t.gross_pnl, t.net_pnl = price, round(t.gross_pnl + d, 2), round(t.net_pnl + d, 2)
        self.realized_gross += d
        side = "SELL" if t.side == "LONG" else "BUY"
        f = next((f for f in reversed(self.fills) if f.contract == t.contract and f.time == t.exit_time and f.side == side
                  and f.price == old), None)
        if f is not None:
            f.price = price
        rec = self.exit_edits.get(key) or {"was": old}
        rec.update(price=price, by=by, at=at)
        self.exit_edits[key] = rec
        return (f"Exit price of {t.contract} ({t.qty}, closed {t.exit_time:%d %b %H:%M}) edited: {old:.2f} -> {price:.2f}"
                f" (booked first at {rec['was']:.2f}); P&L {'+' if d >= 0 else '-'}Rs {abs(d):,.0f}" + (f" by {by}" if by else ""))

    def _kite_closes(self, sym: str, side: str, want: int, since=None) -> tuple[dict, int, float]:
        """Orders in today's order book that closed up to `want` units of `sym`: filled, `side`, NRML, not sent by Algo
        Lab (its tags start with "algolab"), placed after the leg was opened (`since`), not booked before by any live
        session (self.synced, shared by the account's live sessions). Newest first.
        Returns ({order id: units used}, units found, their average price)."""
        used, got, value = {}, 0, 0.0
        book = self.kite.orders() or []
        when = lambda o: str(o.get("exchange_timestamp") or o.get("order_timestamp") or "")  # noqa: E731
        after = since.strftime("%Y-%m-%d %H:%M:%S") if hasattr(since, "strftime") else ""
        for o in sorted(book, key=when, reverse=True):
            if got >= want:
                break
            oid = str(o.get("order_id"))
            if (str(o.get("tradingsymbol")) != sym or str(o.get("transaction_type", "")).upper() != side
                    or str(o.get("product", "NRML")).upper() != "NRML" or str(o.get("tag") or "").startswith("algolab")):
                continue
            if after and when(o) and when(o)[:19] < after:
                continue                                   # before this leg was opened: not its close
            left = int(o.get("filled_quantity") or 0) - self.synced.get(oid, 0)
            px = float(o.get("average_price") or 0)
            if left <= 0 or px <= 0:
                continue
            q = min(left, want - got)
            used[oid], got, value = q, got + q, value + q * px
        return used, got, (value / got if got else 0.0)

    SYNC_RECHECK = 2.0      # seconds between the two reads of Zerodha's positions before a Sync books an estimate
    EMPTY_SYNC_CONFIRM = 600.0   # Zerodha shows no positions: a Sync books the legs only if pressed again this soon
    _empty_sync_at = 0.0
    sync_only = False       # a stopped session started only to sync (Sync on its card): no new stops are placed

    def _less_at_zerodha(self, c, net: dict, others: dict) -> int | None:
        """Zerodha's net for this leg (less the other live sessions) if it is a square-off of it, in full or in part,
        that can be booked now; None if it matches or is something else (left to the position check)."""
        pos, sym = self.positions.get(c), self.symbol_of(c)
        if pos is None or not sym:
            return None
        held = net.get(sym, 0) - others.get(sym, 0)
        if held == pos.qty:
            self._sync_seen.discard(sym)
            return None
        if held * pos.qty < 0 or abs(held) > abs(pos.qty) or others.get(sym):
            return None                                    # not a square-off: the position check decides
        if c.label in self.pending_stop or any(w.get("sym") == sym for w in self.working.values()) or self._open_orders(sym):
            return None                                    # an order still working there (any, also one placed in Kite)
        return held

    def sync_outside(self, now, explicit: bool = False, only=None) -> list[str]:
        """Legs this session holds that Zerodha now holds less of: squared off (in full or in part) by hand in Kite or
        on the Positions page. Booked closed at the price of that Kite order (placed after the leg was opened, not
        booked before). Positions are read twice and must agree; an empty list is never taken as "all closed".
        Without a matching order nothing is booked by itself (the position check then stops the session, as before):
        only a Sync from the card (`explicit`) books it, at the price now, marked "estimated".
        The leg's protective stop is read first: if it is what bought the leg, that is a stop fill (Halt, as at any
        check). Otherwise it is taken off, and the rest of a part-closed leg gets a stop for its size. The strategy
        hears of it (self.outside: on_outside_close) and keeps the other legs. Not synced: Zerodha holding more or
        the other way round, another live session in the same contract, any order still working in it.
        Returns one line per leg booked."""
        if self.s.mode != "live" or self.kite is None or not self.positions:
            return []
        day = self._today(now)
        net = self._zerodha_net()
        if not net:                                        # no positions at all: Zerodha may not have answered in full
            if not explicit:
                return []
            if _time.time() - self._empty_sync_at > self.EMPTY_SYNC_CONFIRM:   # a Sync: only when pressed again
                self._empty_sync_at = _time.time()
                self.log("Sync: Zerodha shows no positions at all, so nothing was booked. If every leg of this "
                         f"session really is closed, press Sync again within {self.EMPTY_SYNC_CONFIRM // 60:.0f} minutes.")
                return []
            self._empty_sync_at = 0.0
        others = self.others() or {}
        legs = [c for c in ([only] if only is not None else list(self.positions))
                if c in self.positions and not self._expired(c, day) and self._less_at_zerodha(c, net, others) is not None]
        if not legs:
            return []
        if explicit:
            self.sleep(self.SYNC_RECHECK)
        net2 = self._zerodha_net()                         # read again: one odd answer is never booked
        done = []
        for c in legs:
            pos, sym = self.positions.get(c), self.symbol_of(c)
            if pos is None:
                continue
            held = self._less_at_zerodha(c, net2, others)
            if held is None or held != net.get(sym, 0) - others.get(sym, 0):
                continue                                   # changed between the reads: looked at again next time
            side = "BUY" if pos.qty < 0 else "SELL"
            gone = abs(pos.qty) - abs(held)
            oid = self.protect.get(c.label) if pos.qty < 0 else None
            stop = self._order_last(oid, self.protect_day.get(c.label), day) if oid else {}
            gone_stop = str(stop.get("status", "")).upper() == "GONE"
            if not gone_stop and (str(stop.get("status", "")).upper() == "COMPLETE" or int(stop.get("filled_quantity") or 0)):
                self._cancel_protect(c, now)               # its own stop bought it: booked as the stop's fill
                raise Halt(f"Protective stop filled at Zerodha for {c.label}")
            used, got, avg = self._kite_closes(sym, side, gone, since=pos.opened_at)
            if got < gone and not explicit:
                if sym not in self._sync_seen:
                    self._sync_seen.add(sym)
                    self.log(f"{c.label}: Zerodha holds {held:+d}, the session {pos.qty:+d}, and Kite's order book has "
                             "no order of yours that closed it" + (" (its stop of an earlier day may have filled)" if gone_stop else "")
                             + ". Not booked by itself: if you closed it, press Sync on the card (booked at the price now).")
                continue
            self._sync_seen.discard(sym)
            note = ""
            if pos.qty < 0:                                # its stop first: never left live on a leg closed by hand
                if gone_stop:
                    self._drop_stop(c.label)               # ended with an earlier day
                    if got < gone:
                        note = " (its stop of an earlier day may have bought it: check the price)"
                n = len(self.fills)
                if self._cancel_protect(c, now) or len(self.fills) > n:
                    raise Halt(f"Protective stop filled at Zerodha for {c.label}")
            est = None
            if got < gone:
                est = self._fresh_price(sym, side) or (pos.avg_price if pos.avg_price else None)
            px = (avg * got + (est or 0.0) * (gone - got)) / gone if (got or est) else pos.avg_price
            px = round(px, 2)
            estimated = got < gone
            why = self.KITE_CLOSE + (" (price estimated)" if estimated else "")
            self._apply(now, c, side, gone, px, self.schedule.at(now).order_cost(side, px * gone), why,
                        pos.group, "kite")
            for k, q in used.items():
                self.synced[k] = self.synced.get(k, 0) + q
            if c in self.positions:
                if self.positions[c].qty < 0:
                    self._reprotect(c, sym)                # the rest of the leg keeps a stop for its size
            else:
                self.released.pop(c.label, None)
            self.outside.append((c, gone))
            msg = (f"{c.label}: {gone} squared off in Kite, booked at {px:.2f}"
                   + (" (Kite order " + ", ".join(used) + ")" if used and not estimated else "")
                   + (" - price ESTIMATED (no Kite order of yours found): edit the exit price on the card (Closed cycles)" if estimated else "")
                   + note)
            self.log(msg)
            self.notify("Algo Lab: leg squared off in Kite", msg)
            done.append(msg)
        return done

    def _closing_still_held(self, now, contract) -> None:
        """Before a closing order: Zerodha must still hold the leg. Squared off in Kite a moment ago (before the next
        check): booked from its Kite order and nothing is sent; no order found yet: Blocked, nothing sent (a
        buy-back would open a long). Its stop having filled is left to the cancel that follows (booked as the stop's)."""
        pos, sym = self.positions.get(contract), self.symbol_of(contract)
        if pos is None or not sym:
            return
        for attempt in (1, 2):
            held = self._zerodha_net().get(sym, 0) - (self.others() or {}).get(sym, 0)
            if held == pos.qty or held * pos.qty < 0 or abs(held) > abs(pos.qty):
                return                                     # matches, or something else: the checks decide
            if attempt == 1:
                self.sleep(2)                              # positions can lag a fill by a moment: read again
        oid = self.protect.get(contract.label) if pos.qty < 0 else None
        gone_stop = False
        if oid:
            stop = self._order_last(oid, self.protect_day.get(contract.label), self._today(now))
            st = str(stop.get("status", "")).upper()
            if st != "GONE" and (st == "COMPLETE" or int(stop.get("filled_quantity") or 0)):
                return                                     # its stop bought it: the cancel books that fill
            gone_stop = st == "GONE"
        for attempt in range(3):                           # Kite's order book can be a few seconds behind
            self.sync_outside(now, only=contract)
            if contract not in self.positions or abs(self.positions[contract].qty) <= abs(held) or attempt == 2:
                break
            self.sleep(2)
        pos = self.positions.get(contract)
        if pos is not None and abs(pos.qty) > abs(held):
            raise Blocked(f"{contract.label}: Zerodha holds {held:+d} for this session, the session {pos.qty:+d} ("
                          + ("its protective stop from an earlier day is no longer at Zerodha: it may have filled"
                             if gone_stop else "squared off in Kite?")
                          + "). Not traded, so no new position can be opened. If you closed it, press Sync on the card.")

    def check_positions(self, now) -> None:
        """While trading, every POSITIONS_EVERY s: Zerodha's net NRML position in each contract this session holds must
        equal its own legs plus the other live sessions' legs. A difference seen at two checks in a row (so a
        fill Zerodha has not shown yet is not mistaken for one) stops the session without sending anything.
        Positions at Zerodha that no live session holds (a manual trade, a leftover stop that filled) are reported
        once a day; they do not stop the session (the Positions page keeps manual legs there too)."""
        if self.s.mode != "live" or self.kite is None:
            return
        self.sync_outside(now)                    # legs squared off by hand in Kite: booked, not a mismatch
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
                          "without sending anything; protective stops at Zerodha stay. A leg you squared off in Kite "
                          "with no order of yours found (or closed on an earlier day): press Sync on the card. "
                          "Anything else (more at Zerodha, the other way round, a contract another live session also "
                          "holds): fix it in Kite, then Sync.")
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

    def _today(self, now=None) -> str:
        """Today's date (IST) as text; also remembered so orders placed from here are dated."""
        self._day = (now if hasattr(now, "date") else ist_now()).date().isoformat()
        return self._day

    GONE_WORDS = re.compile(r"couldn.?t find|not found|invalid order", re.I)

    def _order_last(self, oid: str, placed_on: str | None = None, today: str | None = None) -> dict:
        """The latest state of an order at Zerodha. GONE only for an order Zerodha no longer knows because it ended with
        an earlier day (DAY orders): Kite must answer "Couldn't find that order_id", the order must not be dated today
        (placed_on), and today's order book must not list it. If the book lists it, its state there is used. Any
        other read error (a timeout, too many requests) is raised: an order placed today is never taken as gone."""
        try:
            return (self.kite.order_history(oid) or [{}])[-1]
        except Exception as e:  # noqa: BLE001
            if not self.GONE_WORDS.search(str(e)):
                raise
            if placed_on and today and placed_on >= today:
                raise
            try:
                book = self.kite.orders() or []
            except Exception:  # noqa: BLE001 - cannot tell: the first error stands
                raise e from None
            for o in book:
                if str(o.get("order_id")) == str(oid):
                    return dict(o)
            return {"order_id": str(oid), "status": "GONE", "filled_quantity": 0}

    def _not_read(self, msg: str, key: str | None = None) -> None:
        """Log a check that could not be done, at most once a minute for each kind of message."""
        key = key or msg[:40]
        if _time.time() - self._read_err_at.get(key, 0.0) >= 60:
            self._read_err_at[key] = _time.time()
            self.log(msg)

    def _expired(self, contract, day: str) -> bool:
        return contract.expiry.isoformat() < day

    GONE_CONFIRM = 20.0     # seconds a difference must last before a deferred check takes it as real

    def _still_held(self, contract, defer: bool = False) -> bool:
        """A short leg whose protective stop is gone with an earlier day: Zerodha must still hold exactly this leg
        (net, less the other live sessions) before it is bought back or gets a new stop: the old stop may have
        filled. Zerodha's positions can lag a fill by a moment, so a difference must be seen twice: read again after
        2 s (defer: at later checks instead, returning False until it has lasted GONE_CONFIRM s). Then Blocked."""
        pos, sym = self.positions.get(contract), self.symbol_of(contract)
        if pos is None or not sym:
            return True
        for attempt in (1, 2):
            held = self._zerodha_net().get(sym, 0) - (self.others() or {}).get(sym, 0)
            if held == pos.qty:
                self._gone_odd.pop(sym, None)
                return True
            if defer:
                first = self._gone_odd.get(sym)
                if first is None or _time.time() - first > self.GONE_CONFIRM + 90:   # an old one: start again
                    first = self._gone_odd[sym] = _time.time()
                if _time.time() - first < self.GONE_CONFIRM:
                    self._not_read(f"{contract.label}: Zerodha shows {held:+d}, the session {pos.qty:+d}; checking again",
                                   "odd " + sym)
                    return False
                break
            if attempt == 2:
                break
            self.sleep(2)
        self._gone_odd.pop(sym, None)
        raise Blocked(f"{contract.label}: Zerodha holds {held:+d} for this session, the session {pos.qty:+d}, and its "
                      "protective stop from an earlier day is no longer at Zerodha (it may have filled). Not bought "
                      "back, so nothing can be doubled. Check Kite.")

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
                if str(self._order_last(oid, w.get("day"), self._today(now)).get("status")) == "GONE":
                    self._track(tag, done=True)                       # an order of an earlier day
                    msg = (f"Order {oid} ({w.get('side')} {w.get('qty')} {w.get('sym')}) is from an earlier day and no "
                           "longer at Zerodha; any fill it had shows in the positions checked next")
                    self.log(msg)
                    self.notify("Algo Lab: order from an earlier day", msg)
                    continue
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
                    self._resize_protect(c, w.get("sym"), now)
                self._reprotect(c, w.get("sym"))
        return left

    def _resize_protect(self, contract, sym, now=None) -> None:
        """A short leg that grew (a late fill) while it had a stop: replace the stop so it covers the whole leg."""
        if contract.label not in self.protect or self.s.mode != "live":
            return
        n = len(self.fills)
        whole = False
        try:
            whole = self._cancel_protect(contract, now or ist_now())
        except Exception as e:  # noqa: BLE001 - a stop not confirmed gone stays; a read error: put one back if off
            self.notify("Protective stop not resized", f"{contract.label}: {e}")
        if not whole:
            self._reprotect(contract, sym)
        if len(self.fills) > n:      # the old stop had filled (the market ran far): as a stop fill at a check
            raise Halt(f"Protective stop filled at Zerodha for {contract.label}")

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
        self._track(tag, sym=sym, side=side, qty=qty, label=label, group=group, oid="", booked=0,
                    day=self._day or ist_now().date().isoformat())
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

    @staticmethod
    def _answered_no(e: Exception) -> bool:
        """Zerodha answered and refused the order's inputs, so no order was made: an InputException, or an
        OrderException with a 4xx code. Never anything else (a timeout, a dropped connection, a gateway page that
        could not be read, an order-system error with code 500): the order may then have got there, and it is
        looked for before another is sent. Decided by the type and code only, never by words in the text."""
        name, code = type(e).__name__, getattr(e, "code", None)
        if name == "InputException":
            return True
        return name == "OrderException" and isinstance(code, int) and 400 <= code < 500

    @staticmethod
    def _named_ceiling(text: str) -> float | None:
        """The upper circuit limit a refusal names ("... trigger price below 442.15"), None if none."""
        if "circuit" not in str(text).lower():
            return None
        m = re.search(r"below\s+(?:Rs\.?\s*)?([0-9]+(?:\.[0-9]+)?)", str(text))
        return float(m.group(1)) if m else None

    def _circuit_ceiling(self, e: Exception, sym: str) -> float | None:
        """The upper circuit limit a refused stop ran into: the number Zerodha names, else the limit from a quote.
        None if the refusal was about something else."""
        if "circuit" not in str(e).lower():
            return None
        top = self._named_ceiling(str(e))
        if top is not None:
            return top
        band = self._touch(sym, "BUY")[1]
        return float(band) if band else None

    def _ceiling_of(self, label: str) -> float | None:
        """The circuit limit Zerodha named for this leg's stop today (bands move each day: an older one is dropped)."""
        got = self.stop_ceiling.get(label)
        if not got or got[0] != self._day:
            self.stop_ceiling.pop(label, None)
            return None
        return got[1]

    def _place_protect(self, contract, sym, sold_px, qty, trigger: float | None = None,
                       ceiling: float | None = None, _depth: int = 0) -> bool:
        """Place the SL BUY that protects a short leg at Zerodha. Each stop has its own tag: if Zerodha's answer is lost
        (a timeout), the order is looked up by its tag and adopted if it got there, so a stop is never placed twice.
        While that cannot be told (the order book cannot be read), no other stop is placed for the leg.
        A trigger above the option's upper circuit limit is refused by Zerodha (19 Nov: 3 x 157.10 = 471.30 against a
        limit of 442.15 left the PE without a stop): the limit is read from a quote first and the stop placed just
        below it (`ceiling`), which is nearer, so it protects more; if Zerodha still refuses it for the circuit (the
        band moved), it is placed again below the limit it names. A refusal is never taken for a lost answer."""
        if self.s.protect_mult <= 0:
            return False
        label = contract.label
        if label in self.pending_stop:    # an earlier placement whose answer was lost: find out first
            try:
                found = self._resolve_pending(label)
            except Exception as e:  # noqa: BLE001 - cannot tell yet: no second stop
                self._not_read(f"Protective stop for {label}: {e}; not placed again until that is known",
                               "pending " + label)
                self.renew.add(label)
                return False
            if found is not None and self._worth_keeping(found):
                self._adopt_stop(contract, sym, found, trigger or sold_px * self.s.protect_mult)
                self._fit_found(contract, sym, found, qty)
                return True
        if trigger is None:
            trigger = sold_px * self.s.protect_mult
        asked = trigger
        if ceiling is None:
            ceiling = self._ceiling_of(label)             # a limit Zerodha named for this leg's stop today
        if ceiling is None and _depth == 0:               # the option's upper circuit limit now, from one quote:
            band = self._touch(sym, "BUY")[1]             # the stop goes below it, so Zerodha does not refuse it
            if band and band > self.tick and (trigger >= band or round(trigger * 1.1, 2) > band):
                ceiling = float(band)
                self.log(f"Protective stop for {label}: trigger {trigger:.2f} is not below the upper circuit limit "
                         f"{band:.2f}; placed just below it")
        if ceiling is not None:                          # below the circuit limit: Zerodha refuses one above it
            trigger = min(trigger, math.floor(round(ceiling / self.tick, 6) - 1) * self.tick)
        trigger = round(math.ceil(round(trigger / self.tick, 6)) * self.tick, 2)
        limit = round(math.ceil(round(trigger * 1.1 / self.tick, 6)) * self.tick, 2)
        if ceiling is not None:
            limit = round(min(limit, math.floor(round(ceiling / self.tick, 6)) * self.tick), 2)
        tag = "algolab-stop" + secrets.token_hex(3)
        try:
            oid = self.kite.place_order(variety="regular", exchange="NFO", tradingsymbol=sym, transaction_type="BUY",
                                        quantity=qty, product="NRML", order_type="SL", price=limit,
                                        trigger_price=trigger, validity="DAY", tag=tag)
        except Exception as e:  # noqa: BLE001 - it may still have reached Zerodha: look for it before anything else
            refused = self._answered_no(e)
            if refused and _depth < 2:
                top = self._circuit_ceiling(e, sym)
                if top is not None and top > self.tick and (trigger >= top or limit > top) \
                        and (ceiling is None or top < ceiling - self.tick / 2):      # each retry lower: it ends
                    self.stop_ceiling[label] = (self._day, top)
                    self.log(f"Protective stop for {label}: trigger {trigger:.2f} / limit {limit:.2f} not below the "
                             f"upper circuit limit {top:.2f}; placed just below it instead")
                    return self._place_protect(contract, sym, sold_px, qty, trigger=trigger, ceiling=top,
                                               _depth=_depth + 1)
                if top is None and ceiling is not None and self.stop_ceiling.pop(label, None) is not None:
                    # capped at a limit the band has since moved past (e.g. the price is above it): try uncapped
                    self.log(f"Protective stop for {label}: refused below the old circuit limit {ceiling:.2f} ({e}); "
                             "trying without it")
                    return self._place_protect(contract, sym, sold_px, qty, trigger=asked, _depth=_depth + 1)
            found, unread = None, False
            for n in range(1 if refused else self.LOOKUPS_AFTER_ERROR):
                try:
                    found = self._find_by_tag(tag)
                    unread = False
                except Exception:  # noqa: BLE001
                    unread = True
                if found is not None:
                    break
                if not refused and n < self.LOOKUPS_AFTER_ERROR - 1:
                    self.sleep(self.LOOKUP_GAP)
            if found is not None and self._worth_keeping(found):
                self.log(f"Protective stop for {label}: placed although Zerodha answered with an error ({e})")
                return self._adopt_stop(contract, sym, found, trigger)
            if found is None and not refused:   # not listed (yet): kept until a read rules it out, never doubled
                self.pending_stop[label] = {"tag": tag, "at": _time.time()}
            self.renew.add(label)  # tried again at each check while the session runs
            msg = (f"{sym}: {e}. " + ("Zerodha refused it (if the option is at its circuit limit, no stop can be "
                   "placed until it moves). " if refused else "") + "The short leg is unprotected at Zerodha; Algo Lab "
                   "tries again at each check while the session runs.")
            self.log(f"Protective stop NOT placed: {msg}")
            if ("noplace", label, self._day) not in self._told:      # once a day per leg (retries are logged)
                self._told.add(("noplace", label, self._day))
                self.notify("Protective stop NOT placed", msg)
            return False
        self._adopt_stop(contract, sym, {"order_id": oid}, trigger)
        self.log(f"Protective stop {oid}: BUY {qty} {sym} trigger {trigger:.2f}")
        return True

    PENDING_FORGET = 60     # seconds after which a lost stop that today's order book does not list is ruled out

    def _resolve_pending(self, label: str) -> dict | None:
        """A stop whose placement answer was lost: its order if today's order book lists it now, else None. Raises
        if the book cannot be read, or if it is too early to rule it out (the book can be seconds late): until then
        the leg must be treated as if that stop may be live."""
        p = self.pending_stop.get(label)
        if not p:
            return None
        found = self._find_by_tag(p["tag"])                 # raises if the order book cannot be read
        if found is not None:
            del self.pending_stop[label]
            return found
        if _time.time() - float(p.get("at") or 0) < self.PENDING_FORGET:
            raise Unknown(f"a protective stop sent {int(_time.time() - float(p.get('at') or 0))} s ago, whose answer was "
                          "lost, may still show at Zerodha")
        del self.pending_stop[label]
        self.log(f"Protective stop for {label} whose answer was lost never reached Zerodha")
        return None

    def _cancel_found(self, oid: str, order: dict) -> str:
        """Cancel a stop at Zerodha that protects nothing the session holds (it could open a position). Returns what
        happened, for the message."""
        if str(order.get("status", "")).upper() in self.FINISHED:
            return f"Its protective stop {oid} is {str(order.get('status')).lower()} at Zerodha."
        last = self._confirm_cancel(oid, order)
        if str(last.get("status", "")).upper() == "CANCELLED" and not int(last.get("filled_quantity") or 0):
            self.protect = {k: v for k, v in self.protect.items() if v != oid}
            return f"Its protective stop {oid} was cancelled at Zerodha."
        return (f"Its protective stop {oid} is {str(last.get('status') or 'unknown').lower()} at Zerodha: cancel it in "
                "Kite (it could open a position).")

    @staticmethod
    def _worth_keeping(order: dict) -> bool:
        """A stop found at Zerodha is tracked unless it ended without buying anything (one that filled, even in part,
        must be booked by the stop check, which then stops the session)."""
        return (str(order.get("status", "")).upper() not in ("REJECTED", "CANCELLED")
                or int(order.get("filled_quantity") or 0) > 0)

    def _fit_found(self, contract, sym, order: dict, qty: int) -> None:
        """A stop found after its answer was lost may be for an older size of the leg (sold more since): resized."""
        size = int(order.get("quantity") or 0)
        if size and size != qty and str(order.get("status", "")).upper() not in self.FINISHED:
            self.log(f"Protective stop {order.get('order_id')} for {contract.label} is for {size}, the leg is {qty}: "
                     "replaced for the whole leg")
            self._resize_protect(contract, sym)

    def _adopt_stop(self, contract, sym, order: dict, trigger: float) -> bool:
        label = contract.label
        self._not_held.discard(label)
        self.protect[label] = str(order.get("order_id"))
        self.protect_level[label] = float(order.get("trigger_price") or trigger)
        self._follow_n.pop(label, None)              # a new order: it may be moved again (trailing)
        self.protect_day[label] = self._day or ist_now().date().isoformat()
        self.renew.discard(label)
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
        top = self._ceiling_of(label)
        if top is not None:                              # never above the circuit limit Zerodha named
            limit = round(max(trig, min(limit, math.floor(round(top / self.tick, 6)) * self.tick)), 2)
        try:
            self.kite.modify_order(variety="regular", order_id=self.protect[label], trigger_price=trig, price=limit)
        except Exception as e:  # noqa: BLE001 - the engine's own stop still works; say so once
            self._follow_n[label] = self.FOLLOW_MAX
            self.notify("Stop at Zerodha not moved", f"{label}: {e}. The engine's stop {trig:.2f} still applies.")
            return
        self.protect_level[label] = trig
        self._follow_n[label] = self._follow_n.get(label, 0) + 1
        self.log(f"Protective stop {self.protect[label]}: trigger moved to {trig:.2f} (trailing)")

    def _drop_stop(self, label: str, keep_level: bool = True) -> None:
        """Forget a stop that is no longer at Zerodha; its trigger is kept for the one placed again (_reprotect)."""
        self.protect.pop(label, None)
        self.protect_day.pop(label, None)
        level = self.protect_level.pop(label, None)
        if keep_level and level is not None:
            self.released[label] = level

    def _cancel_protect(self, contract, now) -> bool:
        """Before buying a leg back: cancel its protective stop and wait until Zerodha confirms it is gone.
        True if the stop already bought the whole leg back. A stop that filled in part is booked first, so the
        buy-back covers only the rest. If the stop cannot be confirmed gone, nothing is bought (a second buy could
        flip the leg long): the stop stays on the books and CancelFailed is raised."""
        if contract.label in self.pending_stop and self.s.mode == "live":   # a stop whose answer was lost may be live
            try:
                found = self._resolve_pending(contract.label)
            except Exception as e:  # noqa: BLE001
                msg = (f"{contract.label}: {e}. The leg was NOT bought back, so it cannot be bought twice; check Kite "
                       "for an SL order tagged algolab-stop.")
                self.notify("Protective stop not known", msg)
                raise CancelFailed(msg) from None
            if found is not None:
                self._adopt_stop(contract, self.symbol_of(contract), found, float(found.get("trigger_price") or 0))
        oid = self.protect.get(contract.label)
        if not oid or self.s.mode != "live":
            self.protect.pop(contract.label, None)
            self.protect_level.pop(contract.label, None)
            return False
        last = self._order_last(oid, self.protect_day.get(contract.label), self._today(now))
        status = str(last.get("status", "")).upper()
        if status == "GONE":          # a DAY stop of an earlier day: not at Zerodha, nothing to cancel
            self.log(f"Protective stop {oid} for {contract.label} is from an earlier day and no longer at Zerodha")
            self._still_held(contract)          # it may have filled: then buying back would open a long position
            self._drop_stop(contract.label)
            return False
        if status not in ("COMPLETE", "CANCELLED", "REJECTED"):
            try:
                self.kite.cancel_order(variety="regular", order_id=oid)
            except Exception as e:  # noqa: BLE001 - it may have filled meanwhile: read it again below
                self.log(f"Cancel of protective stop {oid} failed: {e}")
            for _ in range(self.CANCEL_CONFIRM_TRIES):
                try:
                    last = (self.kite.order_history(oid) or [last])[-1]
                except Exception as e:  # noqa: BLE001 - not known yet: read again
                    self.log(f"Protective stop {oid}: status not read: {e}")
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
        self._drop_stop(contract.label, keep_level=status == "CANCELLED")
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
        for label in list(self.pending_stop):          # a stop whose answer was lost: find it to cancel it
            if self.s.mode != "live":
                self.pending_stop.pop(label, None)
                continue
            try:
                found = self._resolve_pending(label)
            except Exception as e:  # noqa: BLE001
                problems.append(f"a protective stop for {label} (tag {self.pending_stop[label]['tag']}) may be at "
                                f"Zerodha ({e}): cancel it in Kite")
                continue
            if found is not None:
                self.protect[label] = str(found.get("order_id"))
        for label, oid in list(self.protect.items()):
            if self.s.mode != "live" or not oid:
                self._drop_stop(label, keep_level=False)
                continue
            try:
                last = self._order_last(oid, self.protect_day.get(label), self._today(now))
                status = str(last.get("status", "")).upper()
                filled = int(last.get("filled_quantity") or 0)
                c = self.contracts.get(label)
                sym = self.symbol_of(c) if c is not None else None
                if status == "GONE" and sym:     # ended with its day: it may have filled then, with nothing held
                    held = self._zerodha_net().get(sym, 0) - (self.others() or {}).get(sym, 0)
                    if held:
                        problems.append(f"Zerodha holds {held:+d} {sym} that this session does not (its protective "
                                        f"stop {oid} of an earlier day may have filled)")
                        self.notify("Position at Zerodha not held by the session", problems[-1])
                if status not in ("COMPLETE", "REJECTED", "CANCELLED", "GONE"):
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
            self._drop_stop(label, keep_level=False)
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
        """A protective stop that filled (fully, or in part before it was cancelled) means the market ran far: record
        it and stop the session. A stop gone with an earlier day (a leg held overnight) is placed again, once Zerodha
        is seen to still hold the leg. A read that fails is logged and tried at the next check: never raised, so the
        strategy (and its exits) keeps running. from_stream: only look at order updates already received on the price
        stream (no request)."""
        if not from_stream:
            self.stop_reads_failed = 0
        if self.s.mode != "live" or not (self.protect or self.renew or self.pending_stop):
            return
        if from_stream and self.stream is None:
            return
        day = self._today(now)
        hit, gone = [], []
        if not from_stream:
            for label in list(self.pending_stop):     # stops whose answer was lost: found ones are read below like any
                c = next((c for c in self.positions if c.label == label), None)       # other (booked if they filled)
                try:
                    found = self._resolve_pending(label)
                except Exception:  # noqa: BLE001 - not known yet: the next check
                    continue
                if found is not None and self._worth_keeping(found) and c is not None:
                    self._adopt_stop(c, self.symbol_of(c), found, float(found.get("trigger_price") or 0))
                    self._fit_found(c, self.symbol_of(c), found, abs(self.positions[c].qty))
                elif found is not None and c is None:     # no leg to protect: it must not stay live
                    msg = f"{label}: " + self._cancel_found(str(found.get("order_id")), found)
                    if int(found.get("filled_quantity") or 0):
                        msg += f" It bought {found.get('filled_quantity')}: an extra position may be open at Zerodha."
                    self.log(msg)
                    if "cancel it in Kite" in msg or "extra position" in msg:
                        self.notify("Protective stop for no leg", msg)
        for label, oid in list(self.protect.items()):
            c = next((c for c in self.positions if c.label == label), None)
            if c is not None and self._expired(c, day):
                self._drop_stop(label, keep_level=False)       # expired: settled, nothing at Zerodha to protect
                continue
            if from_stream:
                last = self.stream.order(oid)
                if not last:
                    continue
            else:
                try:
                    last = self._order_last(oid, self.protect_day.get(label), day)
                except Exception as e:  # noqa: BLE001 - a missed read must never stop the strategy (its exits)
                    self._not_read(f"Protective stop {oid} for {label} not read: {e}", "stop " + label)
                    self.stop_reads_failed += 1
                    continue
            status = str(last.get("status", "")).upper()
            filled = int(last.get("filled_quantity") or 0)
            if status == "GONE":
                gone.append((label, oid, c))
            elif status == "COMPLETE" or (status in ("REJECTED", "CANCELLED") and filled):
                if c is not None:
                    pos = self.positions[c]
                    self._record_stop(c, {**last, "filled_quantity": min(filled or abs(pos.qty), abs(pos.qty))}, now)
                self._drop_stop(label, keep_level=False)
                hit.append(label)
            elif status in ("REJECTED", "CANCELLED"):
                self._drop_stop(label, keep_level=False)
                top = self._named_ceiling(last.get("status_message") or "")
                again = top is not None and c is not None and (self._ceiling_of(label) or 1e12) > top + self.tick / 2
                if again:                              # refused for the circuit limit: the next one goes below it
                    self.stop_ceiling[label] = (self._day, top)
                    self.renew.add(label)              # it bought nothing: placed again at the next check
                if ("gone", label, oid) not in self._told:            # once per lost stop
                    self._told.add(("gone", label, oid))
                    self.notify("Protective stop gone", f"{label}: Zerodha shows it {status.lower()}"
                                + (f" ({last.get('status_message')})" if last.get("status_message") else "")
                                + (". The leg is unprotected; Algo Lab places a new one below the circuit limit at the "
                                   "next check." if again else ". The leg is unprotected at Zerodha."))
        if hit:      # first: the session squares off (each leg's stop is read again there, gone ones included)
            raise Halt("Protective stop filled at Zerodha for " + ", ".join(hit))
        if from_stream:
            return
        for label, oid, c in gone:
            if c is None:                                       # no leg: nothing to protect
                self._drop_stop(label, keep_level=False)
                continue
            try:
                if not self._still_held(c, defer=True):         # Zerodha's positions may lag: decided at the next check
                    continue
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001 - kept as it is: tried again at the next check
                self._not_read(f"Protective stop {oid} for {label} is from an earlier day; positions not read: {e}",
                               "gone " + label)
                continue
            self.log(f"Protective stop {oid} for {label} is from an earlier day and no longer at Zerodha; placing a new one")
            self._drop_stop(label)
            self.renew.add(label)
        self.renew |= {label for label in self.pending_stop
                       if label not in self._not_held and any(c.label == label for c in self.positions)}
        self._renew_stops()

    def _renew_stops(self, tell: bool = True) -> None:
        """Short legs whose stop must be placed again: try now (at each check, and at each closing-only retry). Zerodha
        must still hold the leg first (seen twice if not: then dropped, with an alert: the session's position check
        stops it). tell: alert once if it cannot be placed yet (False when the session is about to stop)."""
        if self.sync_only:
            return                                         # a sync-only run sends no new orders
        day = self._day or ist_now().date().isoformat()
        for label in sorted(self.renew):
            c = next((c for c in self.positions if c.label == label), None)
            if c is None or self.positions[c].qty >= 0 or label in self.protect or self._expired(c, day):
                self.renew.discard(label)
                continue
            sym = self.symbol_of(c)
            if not sym:
                continue
            try:
                if not self._still_held(c, defer=True):
                    continue                              # positions may lag: read again at the next try
            except Blocked as e:
                self.renew.discard(label)
                self._not_held.add(label)
                extra = ""
                if label in self.pending_stop:          # a stop whose answer was lost: kept (and named) if found
                    try:
                        found = self._resolve_pending(label)
                    except Exception:  # noqa: BLE001
                        found, extra = None, (f" A protective stop (tag {self.pending_stop[label]['tag']}) may be at "
                                              "Zerodha: cancel it in Kite (it could open a position).")
                    if found is not None:
                        self._adopt_stop(c, sym, found, float(found.get("trigger_price") or 0))
                        extra = " " + self._cancel_found(str(found.get("order_id")), found)
                raise Blocked(f"{e}{extra} No stop was placed again; the session stops without trading.") from None
            except Exception as e:  # noqa: BLE001 - positions not read: the next try
                self._not_read(f"Protective stop for {label}: positions not read ({e}); placed at the next try",
                               "renew " + label)
                continue
            try:
                self._reprotect(c, sym)
            except Halt:
                raise                                     # e.g. the old stop filled while it was resized
            except Exception as e:  # noqa: BLE001
                self._not_read(f"Protective stop for {label} not placed yet: {e}", "renew " + label)
            if label in self.protect:
                self.renew.discard(label)
            elif tell and ("renew", label, day) not in self._told:
                self._told.add(("renew", label, day))
                self.notify("Protective stop NOT renewed", f"{label}: its protective stop could not be placed yet. The "
                            "short leg is unprotected at Zerodha; Algo Lab tries again at every check (30 s) while the "
                            "session runs.")

    def _protect_sold(self, contract, sym, avg, now=None) -> None:
        """After a sale: one stop for the whole short leg. A leg that already had one (sold more of it) gets it
        replaced for the new size, never a second stop next to it (two could buy it back twice)."""
        if contract.label in self.protect:
            self._resize_protect(contract, sym, now)
        if contract.label not in self.protect and contract in self.positions and self.positions[contract].qty < 0:
            self._place_protect(contract, sym, avg, abs(self.positions[contract].qty))

    def _book_unconfirmed(self, now, contract, side, e, reason, group, sym, closing) -> None:
        if e.filled:
            cost = self.schedule.at(now).order_cost(side, e.avg * e.filled)
            self._apply(now, contract, side, e.filled, round(e.avg, 2), cost, reason, group, "live")
        if closing:
            self._reprotect(contract, sym)          # skipped while the buy may still fill
        elif side == "SELL" and e.filled:
            self._protect_sold(contract, sym, e.avg, now)
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
        self.reconcile(now)
        if closing:                        # squared off in Kite since the last check: booked, nothing sent
            self._closing_still_held(now, contract)
            pos = self.positions.get(contract)
            if pos is None:
                return next((f for f in reversed(self.fills) if f.contract == contract.label), None)
            qty = min(qty, abs(pos.qty))
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
            self._protect_sold(contract, sym, avg, now)
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
        self.reconcile(now)
        synced = {}                        # closing legs the check just synced (squared off in Kite): nothing to send
        for i, (contract, side, qty, quote, reason, group, sym, closing) in enumerate(plan):
            if closing:
                self._closing_still_held(now, contract)
                pos = self.positions.get(contract)
                if pos is None:
                    synced[i] = next((f for f in reversed(self.fills) if f.contract == contract.label), None)
                elif abs(pos.qty) < qty:
                    plan[i] = (contract, side, abs(pos.qty), quote, reason, group, sym, closing)
        day = self._today(now)
        for i, (contract, side, qty, quote, reason, group, sym, closing) in enumerate(plan):   # before any stop is taken off: a
            if i in synced:
                continue
            oid = self.protect.get(contract.label)                              # leg whose stop ended with an earlier
            if closing and side == "BUY" and oid:                               # day must still be held at Zerodha
                if str(self._order_last(oid, self.protect_day.get(contract.label), day).get("status")) == "GONE":
                    self._still_held(contract)
        fills: list = [synced.get(i) for i in range(len(plan))]
        todo, off = [], []
        try:
            for i, (contract, side, qty, quote, reason, group, sym, closing) in enumerate(plan):
                if i in synced:
                    continue
                if closing and side == "BUY":
                    if self._cancel_protect(contract, now):
                        fills[i] = self.fills[-1]   # the protective stop already bought it back
                        continue
                    off.append((contract, sym))
                    left = abs(self.positions[contract].qty)
                    if left < qty:                  # a stop that filled in part leaves less to buy
                        plan[i] = (contract, side, left, quote, reason, group, sym, closing)
                todo.append(i)
        except BaseException:
            for contract, sym in off:               # stops already taken off for this exit: put them back
                self._reprotect(contract, sym)
            raise
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
                    try:
                        self._book_unconfirmed(now, contract, side, r, reason, group, sym, closing)
                    except Halt as e:
                        first_error = first_error or e
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
                try:
                    self._protect_sold(contract, sym, avg, now)
                except Halt as e:
                    first_error = first_error or e
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
        day = self._today(now)
        if self.reconciled_on != day:   # a new day: stops gone with the old one are placed again first
            self._renew_day_stops(day, skip={contract})
        else:
            self._renew_stops()         # closing only: stops still to be placed again are tried at each retry
        self.reconciled = True          # the per-symbol check below replaces the whole-account one here
        self.reconciled_on = day
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
                used, got, avg = self._kite_closes(sym, side, gone, since=pos.opened_at)
                px = round((avg * got + self._limit(side, quote) * (gone - got)) / gone, 2)
                why = self.KITE_CLOSE + ("" if got >= gone else " (price estimated)")
                self._apply(now, contract, side, gone, px, self.schedule.at(now).order_cost(side, px * gone),
                            why, pos.group, "kite")
                for oid, q in used.items():
                    self.synced[oid] = self.synced.get(oid, 0) + q
                self.outside.append((contract, gone))
                msg = (f"{contract.label}: {gone} squared off in Kite; booked at {px:.2f}"
                       + ("" if got >= gone else " - price ESTIMATED (no Kite order found): edit the exit price on the card (Closed cycles)"))
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
