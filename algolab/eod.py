"""NSE end-of-day option prices (the F&O "bhavcopy") and the volatility smile built from them.

Zerodha keeps no history for expired options, but NSE publishes, for every trading day, the closing
price of every NIFTY option. Intraday prices are not in it, so it cannot replace recorded prices;
what it gives is the *shape* of real prices across strikes: how much richer far out-of-the-money
puts are than India VIX implies (the skew), and how weekly at-the-money options compare with VIX.

  import_bhavcopy()  downloads the daily files (or reads ones you saved in data/eod/raw/) and keeps
                     the NIFTY option rows: data/eod/days/<date>.csv.gz, one small file per day.
  build_smile()      turns those closes into implied-volatility ratios (option IV / India VIX) by
                     standardised moneyness, per day and expiry: data/eod/NIFTY_smile.csv.gz
  Smile.ratio()      what the backtest uses: for an option at a moment, the ratio seen at the
                     PREVIOUS trading day's close (never the same day, so no look-ahead), at the same
                     standardised distance from spot. Modelled price = Black-Scholes with
                     VIX x ratio instead of plain VIX.
  validate()         how far plain-VIX and smile-corrected estimates are from the real next-day closes.
"""
from __future__ import annotations

import bisect
import io
import math
import time as _time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA, SPOT_DIR, to_date
from .pricing import year_fraction

EOD_DIR = DATA / "eod"
DAYS_DIR = EOD_DIR / "days"
RAW_DIR = EOD_DIR / "raw"          # put manually downloaded bhavcopy zips/csvs here
SMILE_FILE = EOD_DIR / "NIFTY_smile.csv.gz"

URLS = [  # NSE has used both; the newer (UDiFF) one first
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip",
    "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{mon}/fo{d:%d}{mon}{d:%Y}bhav.csv.zip",
    "https://archives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{mon}/fo{d:%d}{mon}{d:%Y}bhav.csv.zip",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/",
}
# column names in the new (UDiFF) and old bhavcopy layouts
COLS = {
    "symbol": ("TckrSymb", "SYMBOL"), "instrument": ("FinInstrmTp", "INSTRUMENT"),
    "expiry": ("XpryDt", "EXPIRY_DT"), "strike": ("StrkPric", "STRIKE_PR"), "right": ("OptnTp", "OPTION_TYP"),
    "close": ("ClsPric", "CLOSE"), "settle": ("SttlmPric", "SETTLE_PR"), "volume": ("TtlTradgVol", "CONTRACTS"),
    "oi": ("OpnIntrst", "OPEN_INT"), "underlying": ("UndrlygPric",), "date": ("TradDt", "TIMESTAMP"),
}
KEEP = ["date", "expiry", "strike", "right", "close", "settle", "volume", "oi", "underlying"]


class EodError(RuntimeError):
    pass


# ---- reading one bhavcopy --------------------------------------------------------------
def _dates(col: pd.Series) -> pd.Series:
    """2026-10-09 (new layout) or 09-OCT-2026 / 09-Oct-2026 (old layout)."""
    text = col.astype(str).str.strip()
    iso = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    rest = iso.isna()
    if rest.any():
        iso[rest] = pd.to_datetime(text[rest].str.title(), format="%d-%b-%Y", errors="coerce")
    return iso.dt.date


def parse_bhavcopy(raw: bytes, underlying: str = "NIFTY") -> pd.DataFrame:
    """NIFTY option rows of one day's F&O bhavcopy (zip or csv bytes, either NSE layout)."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
            if name is None:
                raise EodError("zip holds no csv")
            raw = z.read(name)
    df = pd.read_csv(io.BytesIO(raw), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    pick = {}
    for key, names in COLS.items():
        col = next((n for n in names if n in df.columns), None)
        if col is not None:
            pick[key] = col
    missing = [k for k in ("symbol", "expiry", "strike", "right", "close") if k not in pick]
    if missing:
        raise EodError(f"unrecognised bhavcopy layout (no {', '.join(missing)})")
    out = pd.DataFrame({k: df[c] for k, c in pick.items()})
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out["right"] = out["right"].astype(str).str.strip().str.upper()
    out = out[(out.symbol == underlying) & out.right.isin(["CE", "PE"])]
    if "instrument" in out:
        inst = out["instrument"].astype(str).str.strip().str.upper()
        out = out[inst.isin(["IDO", "OPTIDX"])]
    out = out.copy()
    out["expiry"] = _dates(out["expiry"])
    if "date" in out:
        out["date"] = _dates(out["date"])
    for k in ("strike", "close", "settle", "volume", "oi", "underlying"):
        if k in out:
            out[k] = pd.to_numeric(out[k], errors="coerce")
        else:
            out[k] = np.nan
    out["strike"] = out["strike"].round().astype("Int64")
    out = out.dropna(subset=["strike", "close"])
    return out[[k for k in KEEP if k in out]].reset_index(drop=True)


def _fetch(session, d: date) -> bytes | None:
    mon = d.strftime("%b").upper()
    for pattern in URLS:
        url = pattern.format(d=d, mon=mon)
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content
    return None


def _day_file(d: date) -> Path:
    return DAYS_DIR / f"{d.isoformat()}.csv.gz"


def trading_days(start: date, end: date) -> list[date]:
    """Days to fetch: those in the stored NIFTY data when available, else weekdays."""
    days = set()
    for f in SPOT_DIR.glob("NIFTY_*.csv"):
        s = pd.read_csv(f, usecols=["datetime"], parse_dates=["datetime"])["datetime"].dt.date
        days |= set(s)
    if days:
        known_end = max(days)
        out = sorted(d for d in days if start <= d <= end)
        d = known_end + timedelta(days=1)
        while d <= end:  # beyond the stored data: weekdays
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out
    return [d.date() for d in pd.bdate_range(start, end)]


def import_bhavcopy(start, end=None, log=print, pause: float = 0.6, session=None) -> dict:
    """Download (or read from data/eod/raw/) the F&O bhavcopy for each trading day and keep the NIFTY
    option rows. Days already imported are skipped, so it can be stopped and run again."""
    import requests
    start, end = to_date(start), to_date(end or date.today())
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # manually saved files first
    added = 0
    for f in sorted(RAW_DIR.iterdir()):
        if f.suffix.lower() not in (".zip", ".csv"):
            continue
        try:
            df = parse_bhavcopy(f.read_bytes())
        except Exception as e:  # noqa: BLE001
            log(f"Skipped {f.name}: {e}")
            continue
        for d, g in df.groupby("date") if "date" in df else []:
            g.drop(columns="date").to_csv(_day_file(d), index=False)
            added += 1
    days = [d for d in trading_days(start, end) if not _day_file(d).exists()]
    session = session or requests.Session()
    try:
        session.get("https://www.nseindia.com/", headers=HEADERS, timeout=15)  # cookies, if it wants them
    except Exception:  # noqa: BLE001
        pass
    got, missing, fails_in_row = 0, [], 0
    for i, d in enumerate(days, 1):
        raw = _fetch(session, d)
        if raw is None:
            missing.append(d)
            fails_in_row += 1
            if fails_in_row >= 15 and got == 0:
                raise EodError(
                    "NSE did not return any file. Their site may be blocking automated downloads from this "
                    "network. You can download the F&O bhavcopy files in a browser from "
                    "nseindia.com/all-reports-derivatives and put them in data/eod/raw/, then run this again.")
        else:
            fails_in_row = 0
            try:
                df = parse_bhavcopy(raw)
                df.drop(columns=[c for c in ("date",) if c in df]).to_csv(_day_file(d), index=False)
                got += 1
            except EodError as e:
                missing.append(d)
                log(f"{d}: {e}")
        if i % 20 == 0 or i == len(days):
            log(f"{i}/{len(days)} days checked, {got} saved")
        _time.sleep(pause)
    return {"saved": got + added, "missing": missing, "checked": len(days)}


# ---- the smile ----------------------------------------------------------------------------
def _daily_spot_vix() -> pd.DataFrame:
    frames = []
    for f in sorted(SPOT_DIR.glob("NIFTY_*.csv")):
        df = pd.read_csv(f, parse_dates=["datetime"])
        if "vix" not in df:
            continue
        df["date"] = df["datetime"].dt.date
        frames.append(df.sort_values("datetime").groupby("date").agg(spot=("close", "last"), vix=("vix", "last")))
    if not frames:
        raise EodError("No NIFTY price data yet: download it first (Data & broker tab).")
    out = pd.concat(frames)
    return out[~out.index.duplicated(keep="first")].sort_index()


def _ncdf(x):
    """Standard normal CDF for numpy arrays (Abramowitz-Stegun 7.1.26, error < 1e-7)."""
    z = np.abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-z * z)
    return 0.5 * (1.0 + np.sign(x) * y)


def bs_vec(spot, strike, t, sigma, is_call, r, q):
    vt = sigma * np.sqrt(t)
    d1 = (np.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / vt
    d2 = d1 - vt
    call = spot * np.exp(-q * t) * _ncdf(d1) - strike * np.exp(-r * t) * _ncdf(d2)
    put = strike * np.exp(-r * t) * _ncdf(-d2) - spot * np.exp(-q * t) * _ncdf(-d1)
    return np.where(is_call, call, put)


def iv_vec(price, spot, strike, t, is_call, r, q, iters: int = 50):
    """Implied volatility by bisection, for arrays. NaN where the price is outside the 2%-300% range."""
    lo, hi = np.full(price.shape, 0.02), np.full(price.shape, 3.0)
    ok = (bs_vec(spot, strike, t, lo, is_call, r, q) <= price) & (price <= bs_vec(spot, strike, t, hi, is_call, r, q))
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        below = bs_vec(spot, strike, t, mid, is_call, r, q) < price
        lo, hi = np.where(below, mid, lo), np.where(below, hi, mid)
    return np.where(ok, 0.5 * (lo + hi), np.nan)


def build_smile(cfg: dict, min_price: float = 0.5, log=print) -> pd.DataFrame:
    """Implied-volatility ratio (option IV / India VIX) for every out-of-the-money NIFTY option that
    traded, by day and expiry, against standardised moneyness z = ln(K/S) / (VIX x sqrt(t))."""
    p = cfg["pricing"]
    r, q, share = p["risk_free_rate"], p["dividend_yield"], p.get("overnight_variance_share", 0.3)
    sv = _daily_spot_vix()
    files = sorted(DAYS_DIR.glob("*.csv.gz"))
    if not files:
        raise EodError("No NSE closing prices imported yet.")
    parts = []
    for f in files:
        d = date.fromisoformat(f.name[:10])
        if d not in sv.index:
            continue
        vix = sv.at[d, "vix"]
        if not vix or vix != vix:
            continue
        sigma = vix / 100.0
        df = pd.read_csv(f, parse_dates=["expiry"])
        df["expiry"] = df["expiry"].dt.date
        und = df["underlying"].dropna() if "underlying" in df else pd.Series(dtype=float)
        spot = float(und.iloc[0]) if len(und) and und.iloc[0] > 0 else float(sv.at[d, "spot"])
        df = df[(df.close >= min_price) & (df.volume.fillna(0) > 0) & (df.expiry > d)]
        df = df[((df.right == "PE") & (df.strike <= spot)) | ((df.right == "CE") & (df.strike >= spot))]
        if df.empty:
            continue
        when = datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30)
        tmap = {e: year_fraction(when, e, share) for e in df.expiry.unique()}
        t = df.expiry.map(tmap).to_numpy(float)
        k = df.strike.to_numpy(float)
        iv = iv_vec(df.close.to_numpy(float), spot, k, t, (df.right == "CE").to_numpy(), r, q)
        parts.append(pd.DataFrame({"date": d, "expiry": df.expiry.to_numpy(), "t": t,
                                   "z": np.log(k / spot) / (sigma * np.sqrt(t)), "ratio": iv / sigma,
                                   "strike": k.astype(int), "right": df.right.to_numpy(),
                                   "close": df.close.to_numpy(float), "spot": spot, "vix": vix}).dropna(subset=["ratio"]))
    if not parts:
        raise EodError("No usable option closes (check that the NIFTY price data covers the imported days).")
    smile = pd.concat(parts, ignore_index=True)
    EOD_DIR.mkdir(parents=True, exist_ok=True)
    smile.round({"t": 6, "z": 4, "ratio": 4}).to_csv(SMILE_FILE, index=False)
    log(f"Smile built from {smile.date.nunique()} days, {len(smile):,} option closes")
    return smile


class Smile:
    """Lookup used by the pricing model. Uses only days strictly before the moment priced."""

    def __init__(self, table: pd.DataFrame, lo: float = 0.5, hi: float = 4.0):
        self.lo, self.hi = lo, hi
        self.curves: dict = {}
        by_day: dict = {}
        for (d, e), g in table.groupby(["date", "expiry"]):
            g = g.sort_values("z")
            if len(g) >= 3:
                self.curves[(d, e)] = (g.z.to_numpy(float), g.ratio.to_numpy(float))
                by_day.setdefault(d, []).append(e)
        self.days = sorted(by_day)
        self.expiries = {d: sorted(v) for d, v in by_day.items()}

    @classmethod
    def load(cls, path: Path = SMILE_FILE) -> "Smile | None":
        if not path.exists():
            return None
        t = pd.read_csv(path, parse_dates=["date", "expiry"])
        t["date"], t["expiry"] = t.date.dt.date, t.expiry.dt.date
        return cls(t)

    @property
    def span(self) -> tuple[date, date] | None:
        return (self.days[0], self.days[-1]) if self.days else None

    def ratio(self, now: datetime, expiry: date, strike: float, spot: float, sigma: float, t: float,
              max_age_days: int = 6) -> float | None:
        i = bisect.bisect_left(self.days, now.date()) - 1   # the last day BEFORE today
        if i < 0 or t <= 0 or sigma <= 0:
            return None
        d = self.days[i]
        if (now.date() - d).days > max_age_days:
            return None
        key = (d, expiry)
        if key not in self.curves:  # not listed / not traded that day: the nearest expiry that was
            exps = self.expiries[d]
            e2 = min(exps, key=lambda e: abs((e - expiry).days))
            key = (d, e2)
        zs, rs = self.curves[key]
        z = math.log(strike / spot) / (sigma * math.sqrt(t))
        return float(min(max(np.interp(z, zs, rs), self.lo), self.hi))


# ---- how good is it? ------------------------------------------------------------------------
def validate(cfg: dict, smile_table: pd.DataFrame | None = None) -> pd.DataFrame:
    """Price each day's out-of-the-money closes with plain VIX and with the PREVIOUS day's smile, and
    compare with the real closes: median error by distance from spot (out of sample)."""
    t_all = smile_table if smile_table is not None else pd.read_csv(SMILE_FILE)
    t_all = t_all.copy()
    t_all["date"] = pd.to_datetime(t_all["date"]).dt.date
    t_all["expiry"] = pd.to_datetime(t_all["expiry"]).dt.date
    sm = Smile(t_all)
    p = cfg["pricing"]
    r, q = p["risk_free_rate"], p["dividend_yield"]
    ratios = []
    for row in t_all.itertuples():
        when = datetime.combine(row.date, datetime.min.time()).replace(hour=15, minute=30)
        ratios.append(sm.ratio(when, row.expiry, row.strike, row.spot, row.vix / 100.0, row.t))
    df = t_all.assign(prev_ratio=pd.array(ratios, dtype="float")).dropna(subset=["prev_ratio"])
    if df.empty:
        return pd.DataFrame()
    sig = df.vix.to_numpy(float) / 100.0
    args = (df.spot.to_numpy(float), df.strike.to_numpy(float), df.t.to_numpy(float))
    call = (df.right == "CE").to_numpy()
    df["flat"] = bs_vec(*args, sig, call, r, q)
    df["smile"] = bs_vec(*args, sig * df.prev_ratio.to_numpy(float), call, r, q)
    df["band"] = pd.cut(df.z, [-99, -3, -2, -1, 1, 2, 99],
                        labels=["far puts (z < -3)", "puts (z -3 to -2)", "puts (z -2 to -1)", "near the money",
                                "calls (z 1 to 2)", "far calls (z > 2)"])
    df["flat_err"] = (df.flat - df.close).abs() / df.close * 100
    df["flat_bias"] = (df.flat - df.close) / df.close * 100
    df["smile_err"] = (df.smile - df.close).abs() / df.close * 100
    return df.groupby("band", observed=True).agg(
        options=("close", "size"), plain_vix_error_pct=("flat_err", "median"),
        plain_vix_bias_pct=("flat_bias", "median"), with_smile_error_pct=("smile_err", "median")).round(1)
