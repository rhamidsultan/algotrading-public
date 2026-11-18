"""Margin per strategy and per session from Zerodha's basket margin calculator (faked here)."""
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab import kite, margins, paper  # noqa: E402
from algolab.config import load_config  # noqa: E402

NOW = datetime(2026, 10, 7, 10, 0)
EXPIRIES = [date(2026, 10, 13), date(2026, 10, 20), date(2026, 10, 27)]
SPOT = 24010.0


def price(k, right, expiry):
    t = max((expiry - NOW.date()).days, 1) / 365
    intrinsic = max(0.0, SPOT - k) if right == "CE" else max(0.0, k - SPOT)
    return round(intrinsic + 0.4 * SPOT * 0.13 * math.sqrt(t) * math.exp(-abs(k - SPOT) / (SPOT * 0.13 * math.sqrt(t) * 2)), 2)


class ChainKite:
    def __init__(self):
        self.baskets = []
        self.sym = {}
        for e in EXPIRIES:
            for k in range(22000, 26050, 50):
                for r in ("CE", "PE"):
                    self.sym[f"NIFTY{e:%y%m%d}{k}{r}"] = (k, r, e)

    def instruments(self, exchange=None):
        if exchange == "NSE":
            return [{"tradingsymbol": "NIFTY 50", "segment": "INDICES", "instrument_token": 256265}]
        return [{"name": "NIFTY", "instrument_type": r, "expiry": e, "strike": float(k), "tradingsymbol": s,
                 "instrument_token": i + 1000, "lot_size": 65} for i, (s, (k, r, e)) in enumerate(self.sym.items())]

    def quote(self, symbols):
        out = {}
        for s in symbols:
            if s.startswith("NSE:"):
                out[s] = {"last_price": SPOT if "NIFTY" in s else 12.8, "timestamp": NOW}
            else:
                k, r, e = self.sym[s[4:]]
                p = price(k, r, e)
                out[s] = {"last_price": p, "depth": {"buy": [{"price": max(p - 0.5, 0.05)}], "sell": [{"price": p + 0.5}]}}
        return out

    def basket_order_margins(self, orders, consider_positions=True, mode=None):
        assert consider_positions is False and all(o["product"] == "NRML" for o in orders)
        self.baskets.append(orders)
        sells = sum(o["quantity"] for o in orders if o["transaction_type"] == "SELL")
        buys = sum(o["quantity"] for o in orders if o["transaction_type"] == "BUY")
        naked = sells * 1800.0
        hedged = max(sells - buys, 0) * 1800.0 + min(sells, buys) * 450.0
        return {"initial": {"total": naked}, "final": {"total": hedged, "span": hedged * 0.8, "exposure": hedged * 0.2,
                                                         "option_premium": buys * 10.0}}


def setup(tmp_path, monkeypatch):
    for mod in (kite, margins):
        monkeypatch.setattr(mod, "ist_now", lambda: NOW)
    monkeypatch.setattr(kite, "session_info", lambda: None)
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    for name in ("OUT", "REQUEST", "ATTEMPT"):
        monkeypatch.setattr(margins, name, tmp_path / Path(str(getattr(margins, name))).name)
    monkeypatch.setattr(margins, "RUNTIME", tmp_path)
    cfg = load_config()
    cfg["pricing"]["smile"] = "off"
    return cfg


def test_every_strategy_gets_zerodha_margin_for_its_entry_legs(tmp_path, monkeypatch):
    cfg = setup(tmp_path, monkeypatch)
    d = tmp_path / "sessions" / "iron_fly-2"
    d.mkdir(parents=True)
    pos = [{"contract": {"underlying": "NIFTY", "expiry": "2026-10-13", "strike": k, "right": r}, "qty": q,
            "avg_price": 50.0, "opened_at": "2026-10-07T09:30:00", "group": "g", "entry_costs": 0}
           for k, r, q in ((24000, "CE", -65), (24000, "PE", -65), (24300, "CE", 65), (23700, "PE", 65))]
    (d / "state.json").write_text(json.dumps({"broker": {"positions": pos}}))
    fk = ChainKite()
    out = margins.compute(cfg, kite=fk, log=lambda m: None)
    s = out["strategies"]
    assert set(s) >= {"short_straddle", "premium_strangle", "iron_fly", "event_iron_fly", "double_calendar",
                      "put_credit_spread", "intraday_straddle", "intraday_strangle", "blaze_butterfly"}
    assert all("margin" in v for v in s.values()), {k: v.get("error") for k, v in s.items() if "error" in v}
    assert len(s["iron_fly"]["legs"]) == 4 and s["iron_fly"]["margin"] < s["short_straddle"]["margin"]
    assert s["short_straddle"]["margin"] == 2 * 65 * 1800.0 and s["short_straddle"]["net_credit"] > 0
    b = s["blaze_butterfly"]
    assert b["unit"].startswith("1 unit") and sorted(x["text"].split()[0] for x in b["legs"]) == ["BUY", "BUY", "SELL"]
    assert [x["qty"] for x in b["legs"] if x["text"].startswith("SELL")] == [130]
    dc = s["double_calendar"]["legs"]
    assert {x["text"].rsplit(" ", 2)[-2:][0] for x in dc} == {"13", "20"}      # near and next week
    assert s["iron_fly"]["without_hedge"] > s["iron_fly"]["margin"]
    assert out["sessions"]["iron_fly-2"]["margin"] == 2 * 65 * 450.0 and out["sessions"]["iron_fly-2"]["legs"] == 4
    first = fk.baskets[0]
    assert [o["transaction_type"] for o in first] == sorted(o["transaction_type"] for o in first)   # buys first
    # the timer's entry point writes the file; a failure keeps the old figures with the error
    monkeypatch.setattr(margins, "compute", lambda: out)
    assert margins.main([]) == 0 and json.loads(margins.OUT.read_text())["strategies"]["iron_fly"]["margin"] > 0

    def boom():
        raise RuntimeError("Incorrect `api_key` or `access_token`.")
    monkeypatch.setattr(margins, "compute", boom)
    assert margins.main([]) == 1
    kept = json.loads(margins.OUT.read_text())
    assert kept["strategies"]["iron_fly"]["margin"] > 0 and "access_token" in kept["error"]


def test_when_a_refresh_is_due(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    assert margins.due(NOW) == "market hours"                       # nothing yet, market open
    margins.OUT.write_text("{}")
    assert margins.due(NOW) == ""                                   # fresh figures
    assert margins.due(datetime(2026, 10, 7, 20, 0)) == ""
    margins.REQUEST.write_text("x")
    assert margins.due(datetime(2026, 10, 7, 20, 0)) == "asked from the dashboard"
