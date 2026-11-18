"""Shared helper: load today's Kite access token and return a ready KiteConnect client.

Use from your own scripts:

    from kite_session import get_kite
    kite = get_kite()
    print(kite.holdings())
"""
import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from kiteconnect import KiteConnect

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

IST = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = BASE / "data" / "access_token.json"
API_KEY = os.environ.get("KITE_API_KEY", "")


class LoginRequired(Exception):
    """No valid token for today - open the login page and sign in."""


def _last_expiry(now=None):
    """Kite tokens are flushed around 6:00 AM IST every day."""
    now = now or datetime.now(IST)
    cutoff = datetime.combine(now.date(), time(6, 0), tzinfo=IST)
    return cutoff if now >= cutoff else cutoff - timedelta(days=1)


def read_token():
    """Return today's access token, or None if missing/expired."""
    try:
        data = json.loads(TOKEN_FILE.read_text())
        created = datetime.fromisoformat(data["created"])
    except (OSError, ValueError, KeyError):
        return None
    return data.get("access_token") if created >= _last_expiry() else None


def save_token(access_token):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "access_token": access_token,
        "created": datetime.now(IST).isoformat(),
    }))
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_FILE)


def get_kite():
    token = read_token()
    if not token:
        raise LoginRequired("No valid Kite token for today. Do the morning login first.")
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(token)
    return kite
