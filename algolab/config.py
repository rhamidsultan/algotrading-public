"""Paths, settings and the IST clock."""
from __future__ import annotations

import copy
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SPOT_DIR = DATA / "spot"
OPTION_DIR = DATA / "options"
RUNTIME = ROOT / "runtime"
IST = ZoneInfo("Asia/Kolkata")

DEFAULTS = {
    "market": {
        "underlying": "NIFTY", "lot_size": 65, "strike_step": 50, "tick_size": 0.05,
        "expiry_rules": [{"from": "2000-01-01", "weekday": 3}, {"from": "2025-09-01", "weekday": 1}],
        "lot_size_schedule": [],   # [{from: date, size: n}] by contract expiry; empty = lot_size always
        "holidays": [],            # NSE weekday holidays, used outside the range of stored data
    },
    "account": {"capital": 300000},
    "execution": {"slippage_pct": 0.5, "intrabar": "ohlc",
                  # without a bid/ask, a fill pays at least this much per side (Rs), by premium: [below, Rs]
                  "min_half_spread": [[20, 0.10], [100, 0.20]],
                  "cost_stress": 1.0,          # x the spread / slippage (2 = conservative)
                  "fill_delay_seconds": 0,     # fill at the price this long after the decision
                  "fill_max_age_minutes": 3},  # an older last trade is no price to fill at: wait for the next
    "costs": {
        "brokerage_per_order": 20, "stt_sell_pct": 0.15, "stt_exercise_pct": 0.15,
        "exchange_txn_pct": 0.03553, "sebi_pct": 0.0001, "stamp_buy_pct": 0.003, "gst_pct": 18,
    },
    "cost_history": [],            # [{until: date, <cost name>: value, ...}] earlier rates, by trade date
    "pricing": {"risk_free_rate": 0.065, "dividend_yield": 0.012, "iv_scale": 1.0, "fallback_iv": 0.13, "smile": "auto",
                "overnight_variance_share": 0.3},
    "paper": {"live_poll_seconds": 2, "sim_seconds_per_bar": 0.5},
    "alerts": {"trader_config": "/opt/trader/config.json"},   # email / Telegram settings of the portfolio dashboard
    "collect": {"strikes_each_side": 10, "puts_below": 30, "expiries": 2, "days": 7},
    "server": {"host": "", "key_file": "", "data_path": "/opt/algolab/data"},
}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Path | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = path or ROOT / "config.yaml"
    if path.exists():
        _merge(cfg, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    m = cfg.setdefault("market", {})
    m["holidays"] = sorted({str(h) for h in m.get("holidays") or []} | set(nse_holidays()))
    return cfg


def nse_holidays() -> list[str]:
    """The NSE holiday list shipped with the code (algolab/nse_holidays.txt), as 'YYYY-MM-DD' strings.
    The server keeps its own config.yaml, which updates never replace, so the list lives here too."""
    try:
        text = (Path(__file__).resolve().parent / "nse_holidays.txt").read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def ist_now() -> datetime:
    """Current Indian market time, timezone-naive (all platform times are IST)."""
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def as_of(schedule: list, when, key: str, default=None):
    """Value of `key` in the last schedule entry whose `from` date is on or before `when`."""
    d = to_date(when)
    value = default
    for row in sorted(schedule or [], key=lambda r: to_date(r["from"])):
        if to_date(row["from"]) <= d and key in row:
            value = row[key]
    return value


def to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
