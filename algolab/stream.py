"""Zerodha's live price stream (Kite WebSocket), shared by every live session on the server.

One connection carries ticks for the index, India VIX and every option in use (full mode: last price
plus the best bid and ask), and Zerodha's order updates (fills, rejections) as they happen.

  stream = KiteStream(api_key, access_token, log=print)
  stream.start()                         # background thread; reconnects by itself
  stream.want({256265, 264969, ...})     # instrument tokens to keep subscribed
  stream.tick(256265)                    # {"last", "bid", "ask", "ts", "rx"} or None
  stream.healthy()                       # connected and heard from in the last few seconds
  stream.wait_order(order_id, timeout)   # latest order update for that order, or None

Prices are only trusted while the connection is healthy: Zerodha sends a heartbeat every second and
a tick whenever a price changes, plus a snapshot of each instrument as soon as it is subscribed. The
live feed (kite.KiteFeed) falls back to polling quotes whenever the stream is not healthy.

Binary format (Kite Connect v3 docs): a message starts with the number of packets (int16); each
packet is its length (int16) followed by the packet. Integers are big-endian int32; prices are in
paise (divide by 100) except currency derivatives. Packet sizes: 8 = LTP, 28/32 = index quote/full,
44 = quote, 184 = full with 5 levels of market depth.
"""
from __future__ import annotations

import json
import struct
import threading
import time
from datetime import datetime

WS_URL = "wss://ws.kite.trade"
MAX_TOKENS = 3000            # Zerodha's limit per connection


def _divisor(token: int) -> float:
    seg = token & 0xFF
    return 10_000_000.0 if seg == 3 else 10_000.0 if seg == 6 else 100.0


def _ts(sec: int):
    return datetime.fromtimestamp(sec) if sec else None


def parse_binary(data: bytes) -> list[dict]:
    """Decode one binary message into ticks: {"token", "last", "bid", "ask", "ts", ...}."""
    if len(data) < 2:
        return []           # heartbeat
    out = []
    n = struct.unpack(">H", data[0:2])[0]
    pos = 2
    for _ in range(n):
        if pos + 2 > len(data):
            break
        size = struct.unpack(">H", data[pos:pos + 2])[0]
        pkt = data[pos + 2:pos + 2 + size]
        pos += 2 + size
        if len(pkt) < 8:
            continue
        token = struct.unpack(">I", pkt[0:4])[0]
        div = _divisor(token)
        t = {"token": token, "last": struct.unpack(">i", pkt[4:8])[0] / div, "bid": None, "ask": None, "ts": None}
        if size in (28, 32) and (token & 0xFF) == 9:            # index
            hi, lo, op, cl = struct.unpack(">iiii", pkt[8:24])
            t.update(high=hi / div, low=lo / div, open=op / div, close=cl / div)
            if size == 32:
                t["ts"] = _ts(struct.unpack(">I", pkt[28:32])[0])
        elif size >= 44:
            (lq, avg, vol, bq, sq, op, hi, lo, cl) = struct.unpack(">iiiiiiiii", pkt[8:44])
            t.update(volume=vol, open=op / div, high=hi / div, low=lo / div, close=cl / div)
            if size >= 184:
                t["ts"] = _ts(struct.unpack(">I", pkt[60:64])[0])
                t["oi"] = struct.unpack(">i", pkt[48:52])[0]
                levels = []
                for i in range(10):
                    q, p, o = struct.unpack(">iih", pkt[64 + 12 * i:64 + 12 * i + 10])
                    levels.append((q, p / div, o))
                bids, asks = levels[:5], levels[5:]
                t["bid"] = next((p for q, p, o in bids if q > 0 and p > 0), None)
                t["ask"] = next((p for q, p, o in asks if q > 0 and p > 0), None)
        out.append(t)
    return out


class KiteStream:
    def __init__(self, api_key: str, access_token: str, log=print, url: str = WS_URL, stale_after: float = 5.0,
                 connect=None, clock=time.time):
        self.api_key, self.access_token, self.log, self.url = api_key, access_token, log, url
        self.stale_after = stale_after
        self._connect = connect or self._default_connect
        self.clock = clock
        self.ticks: dict[int, dict] = {}          # token -> latest tick (this connection only)
        self._want: set[int] = set()
        self._subscribed: set[int] = set()
        self._lock = threading.Lock()
        self._orders: dict[str, dict] = {}
        self._order_cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._ws = None
        self.connected = False
        self.last_msg = 0.0
        self.connected_at = 0.0
        self.reconnects = 0
        self.messages = 0
        self.error = ""
        self.on_ticks = None                      # optional callback(list of ticks), e.g. the 1-second recorder

    # ---- public -------------------------------------------------------------------------
    def start(self) -> "KiteStream":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="kite-stream", daemon=True)
            self._thread.start()
        return self

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def set_token(self, access_token: str) -> None:
        """A new day's login: reconnect with it."""
        if access_token and access_token != self.access_token:
            self.access_token = access_token
            self.error = ""
            self._close()

    def want(self, tokens) -> None:
        tokens = {int(t) for t in tokens if t}
        if len(tokens) > MAX_TOKENS:
            tokens = set(sorted(tokens)[:MAX_TOKENS])
        with self._lock:
            self._want = tokens

    def healthy(self) -> bool:
        return self.connected and self.clock() - self.last_msg <= self.stale_after

    def tick(self, token) -> dict | None:
        """The latest tick for a subscribed instrument, while the stream is healthy."""
        if not self.healthy() or token is None:
            return None
        return self.ticks.get(int(token))

    def wait_order(self, order_id: str, timeout: float) -> dict | None:
        """Wait up to `timeout` seconds for a new update of this order; the latest one (or None)."""
        end = self.clock() + timeout
        with self._order_cv:
            seen = self._orders.get(str(order_id))
            while True:
                cur = self._orders.get(str(order_id))
                if cur is not None and cur is not seen:
                    return cur
                left = end - self.clock()
                if left <= 0:
                    return cur
                self._order_cv.wait(min(left, 0.5))

    def order(self, order_id: str) -> dict | None:
        return self._orders.get(str(order_id))

    def status(self) -> dict:
        return {"connected": self.connected, "healthy": self.healthy(),
                "last_message_age": round(self.clock() - self.last_msg, 1) if self.last_msg else None,
                "subscribed": len(self._subscribed), "wanted": len(self._want), "with_ticks": len(self.ticks),
                "reconnects": self.reconnects, "messages": self.messages, "error": self.error}

    # ---- connection ------------------------------------------------------------------------
    def _default_connect(self, url: str):
        import ssl
        import websocket   # websocket-client
        sslopt = {}
        try:
            import certifi
            sslopt = {"ca_certs": certifi.where(), "cert_reqs": ssl.CERT_REQUIRED}
        except ImportError:
            pass
        ws = websocket.create_connection(url, timeout=15, sslopt=sslopt,
                                         header=["X-Kite-Version: 3"], enable_multithread=True)
        ws.settimeout(1.0)
        return ws

    def _close(self) -> None:
        ws, self._ws = self._ws, None
        self.connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _send(self, ws, msg: dict) -> None:
        ws.send(json.dumps(msg))

    def _sync_subscriptions(self, ws) -> None:
        with self._lock:
            want = set(self._want)
        drop, add = self._subscribed - want, want - self._subscribed
        if drop:
            self._send(ws, {"a": "unsubscribe", "v": sorted(drop)})
            for t in drop:
                self.ticks.pop(t, None)
        if add:
            self._send(ws, {"a": "subscribe", "v": sorted(add)})
            self._send(ws, {"a": "mode", "v": ["full", sorted(add)]})
        self._subscribed = want

    def _handle_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "order" and isinstance(msg.get("data"), dict):
            d = msg["data"]
            with self._order_cv:
                self._orders[str(d.get("order_id"))] = d
                if len(self._orders) > 500:
                    for k in list(self._orders)[:100]:
                        self._orders.pop(k, None)
                self._order_cv.notify_all()
        elif kind == "error":
            self.error = str(msg.get("data"))
            self.log(f"Price stream error from Zerodha: {self.error}")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = f"{self.url}?api_key={self.api_key}&access_token={self.access_token}"
                ws = self._connect(url)
                self._ws = ws
                self.ticks, self._subscribed = {}, set()
                self.connected, self.connected_at = True, self.clock()
                self.last_msg = self.clock()
                if self.reconnects:
                    self.log("Price stream reconnected")
                backoff = 1.0
                self._loop(ws)
            except Exception as e:  # noqa: BLE001 - network, expired login, closed socket...
                if not self._stop.is_set():
                    msg = f"{type(e).__name__}: {e}"[:200]
                    if msg != self.error:
                        self.log(f"Price stream down ({msg}); the feed polls quotes meanwhile")
                    self.error = msg
            finally:
                self._close()
            if self._stop.is_set():
                break
            self.reconnects += 1
            # an invalid/expired login cannot be fixed by retrying fast: wait for the next login
            wait = 60.0 if "403" in self.error or "Forbidden" in self.error else backoff
            self._stop.wait(wait)
            backoff = min(backoff * 2, 30.0)

    def _loop(self, ws) -> None:
        while not self._stop.is_set() and self._ws is ws:
            self._sync_subscriptions(ws)
            try:
                opcode, data = ws.recv_data()
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ in ("WebSocketTimeoutException", "TimeoutError", "timeout"):
                    if self.clock() - self.last_msg > max(10.0, 2 * self.stale_after):
                        raise TimeoutError("no data from Zerodha for 10 s") from None
                    continue
                raise
            self.last_msg = self.clock()
            self.messages += 1
            if opcode == 2:
                batch = parse_binary(data)
                for t in batch:
                    t["rx"] = self.last_msg
                    self.ticks[t["token"]] = t
                if self.on_ticks is not None and batch:
                    try:
                        self.on_ticks(batch)
                    except Exception:  # noqa: BLE001 - a listener must never break the price stream
                        self.on_ticks = None
            elif opcode == 1:
                self._handle_text(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
            elif opcode == 8:
                raise ConnectionError("closed by Zerodha")
