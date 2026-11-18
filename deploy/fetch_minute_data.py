"""Runs ON THE SERVER (deploy/get-minute-data.command copies it there and starts it).

Downloads 1-minute candles from Zerodha with the server's own login, for backtests on the Mac:
  data/spot/NIFTY_minute.csv                               NIFTY 50 + India VIX, 1-minute
  data/options_1min/NIFTY/<expiry>/<strike><CE|PE>.csv     time (bar START), open, high, low, close

Zerodha only serves contracts that are still listed, so expired expiries cannot be fetched; running
this regularly (before each expiry) builds a 1-minute history over time.
Read-only on the Zerodha account: it downloads prices and places no orders.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.getcwd())
from algolab import kite as K            # noqa: E402
from algolab.config import DATA, ist_now  # noqa: E402

SPOT_FROM = date(2026, 4, 1)   # 1-minute NIFTY / VIX from this date
STRIKES_EACH_SIDE = 30         # strikes either side of today's ATM (50 points each)
EXPIRIES = 3                   # nearest listed expiries
DAYS = 60                      # days of 1-minute option candles per contract


def main():
    kc = K.client()
    today = ist_now().date()
    path, n = K.download_spot(SPOT_FROM, today, "minute", kite=kc)
    print(f"NIFTY + India VIX 1-minute: {n:,} bars -> {path}", flush=True)

    chain = K.option_chain(kc, "NIFTY")
    spot = kc.quote([K.SPOT_SYMBOL])[K.SPOT_SYMBOL]["last_price"]
    atm = round(spot / 50) * 50
    exps = sorted(e for e in chain.expiry.unique() if e >= today)[:EXPIRIES]
    want = chain[chain.expiry.isin(exps) & ((chain.strike - atm).abs() <= STRIKES_EACH_SIDE * 50)]
    root = DATA / "options_1min" / "NIFTY"
    saved = 0
    for i, r in enumerate(want.itertuples(), 1):
        df = K.fetch_candles(kc, int(r.token), today - timedelta(days=DAYS), today, "minute")
        if len(df):
            p = root / str(r.expiry) / f"{int(r.strike)}{r.right}.csv"
            p.parent.mkdir(parents=True, exist_ok=True)
            df.rename(columns={"datetime": "time"}).to_csv(p, index=False)
            saved += 1
        print(f"\r  option contracts {i}/{len(want)}", end="", flush=True)
    print(f"\nOptions 1-minute: {saved} of {len(want)} contracts saved (NIFTY {spot:,.2f}, ATM {atm}), "
          f"expiries {', '.join(str(e) for e in exps)} -> {root}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # show a plain reason (an expired login is the usual one)
        sys.exit(f"Stopped: {e}\nIf this mentions a token or login, log in to Zerodha on the server dashboard first.")
