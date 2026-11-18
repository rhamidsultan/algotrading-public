"""Checks the Zerodha live price stream step by step. Prints no secrets.

Run in this folder:  python3 check_stream.py
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
cfg = json.loads((BASE / "config.json").read_text())
print("1. API key in config.json ends with:", cfg["api_key"][-4:])

try:
    import websocket
    print("2. websocket-client library: OK (version", getattr(websocket, "__version__", "?"), ")")
    if not hasattr(websocket, "create_connection"):
        sys.exit("   Wrong 'websocket' package installed. Run: pip3 uninstall -y websocket && pip3 install websocket-client")
except ImportError:
    sys.exit("2. websocket-client library: MISSING. Run: pip3 install websocket-client")

token_file = BASE / "token.json"
if not token_file.exists():
    sys.exit("3. No Zerodha login saved yet. Log in from the dashboard first.")
access_token = json.loads(token_file.read_text())["access_token"]

import certifi  # noqa: E402
from kiteconnect import KiteConnect  # noqa: E402

kite = KiteConnect(api_key=cfg["api_key"])
kite.set_access_token(access_token)
try:
    holdings = kite.holdings()
except Exception as e:  # noqa: BLE001
    sys.exit(f"3. Zerodha login check FAILED ({type(e).__name__}: {e}). Log in again from the dashboard.")
tokens = [h["instrument_token"] for h in holdings if h.get("instrument_token")]
print(f"3. Zerodha login: OK, {len(holdings)} holdings, {len(tokens)} with instrument tokens")

url = f"wss://ws.kite.trade?api_key={cfg['api_key']}&access_token={access_token}"
try:
    ws = websocket.create_connection(url, timeout=15, sslopt={"ca_certs": certifi.where()})
except websocket.WebSocketBadStatusException as e:
    sys.exit(f"4. Live stream REFUSED by Zerodha: HTTP {getattr(e, 'status_code', '?')} "
             f"{getattr(e, 'resp_body', b'')[:200]!r}")
except Exception as e:  # noqa: BLE001
    sys.exit(f"4. Live stream connection FAILED: {type(e).__name__}: {e}")
print("4. Live stream: connected")

ws.send(json.dumps({"a": "subscribe", "v": tokens}))
ws.send(json.dumps({"a": "mode", "v": ["ltp", tokens]}))
start, ticks = time.time(), 0
while time.time() - start < 10:
    try:
        msg = ws.recv()
    except Exception as e:  # noqa: BLE001
        print(f"5. Stream closed: {type(e).__name__}: {e}")
        break
    if isinstance(msg, bytes):
        if len(msg) > 2:
            ticks += 1
    else:
        print("5. Message from Zerodha:", msg[:300])
ws.close()
print(f"6. Price updates received in 10 seconds: {ticks}")
print("   RESULT:", "live ticks WORK" if ticks else "no prices received")
