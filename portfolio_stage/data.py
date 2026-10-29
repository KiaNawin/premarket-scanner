"""
Price data from Yahoo (same source as the scanner) with retry + exponential backoff and a daily
disk cache. Every ticker that cannot be loaded is returned in `missing` with the reason — nothing
is dropped silently.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
PRICE_COLS = ["Open", "High", "Low", "Close", "Volume"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def to_yahoo(t: str) -> str:
    return t.replace(".", "-").upper()


def _clean(df) -> pd.DataFrame | None:
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(0, axis=1) if df.columns.nlevels > 1 else df
    cols = [c for c in PRICE_COLS if c in df.columns]
    if "Close" not in cols:
        return None
    df = df[cols].dropna(subset=["Close"]).copy()
    if df.empty:
        return None
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(ET).tz_localize(None)
    df.index = pd.DatetimeIndex(idx).normalize()
    return df[~df.index.duplicated(keep="last")].sort_index()


def _download(tickers: list[str], period: str) -> dict[str, pd.DataFrame | None]:
    import yfinance as yf
    try:
        import yf_utils                      # reuse the scanner's cache-location fix ("database is locked")
        yf_utils._setup_cache()
    except Exception:
        pass
    raw = yf.download(tickers, period=period, interval="1d", auto_adjust=True, group_by="ticker",
                      threads=True, progress=False)
    out = {}
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else (raw if len(tickers) == 1 else None)
        except KeyError:
            sub = None
        out[t] = _clean(sub)
    return out


def fetch_daily(tickers: list[str], cfg: dict, as_of: dt.date, refresh: bool = False,
                downloader=None) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Returns ({ticker: DataFrame}, {ticker: reason}) — every requested ticker lands in one of the two."""
    downloader = downloader or _download
    dcfg = cfg["data"]
    cache_dir = os.path.join(cfg["paths"]["cache"], "prices", as_of.isoformat())
    os.makedirs(cache_dir, exist_ok=True)
    got: dict[str, pd.DataFrame] = {}
    todo = []
    for t in dict.fromkeys(to_yahoo(x) for x in tickers):
        p = os.path.join(cache_dir, f"{t}.pkl")
        if not refresh and os.path.exists(p):
            try:
                got[t] = pd.read_pickle(p)
                continue
            except Exception:
                pass
        todo.append(t)

    last_err: dict[str, str] = {}
    for attempt in range(dcfg["retries"] + 1):
        if not todo:
            break
        if attempt:
            wait = dcfg["backoff_seconds"] * 2 ** (attempt - 1)
            log(f"  retry {attempt}/{dcfg['retries']} for {len(todo)} tickers in {wait}s: {', '.join(todo[:10])}")
            time.sleep(wait)
        batch = todo if attempt == 0 else None
        results: dict[str, pd.DataFrame | None] = {}
        try:
            if batch and len(batch) > 1:
                results = downloader(batch, dcfg["history_period"])
            else:                                     # retries go one ticker at a time
                for t in todo:
                    try:
                        results.update(downloader([t], dcfg["history_period"]))
                    except Exception as e:
                        last_err[t] = f"{type(e).__name__}: {e}"
        except Exception as e:
            for t in todo:
                last_err[t] = f"{type(e).__name__}: {e}"
        still = []
        for t in todo:
            df = results.get(t)
            if df is not None and not df.empty:
                got[t] = df
                df.to_pickle(os.path.join(cache_dir, f"{t}.pkl"))
            else:
                last_err.setdefault(t, "Yahoo returned no rows (delisted, wrong ticker or rate-limited)")
                still.append(t)
        todo = still

    missing = {t: f"no price data after {dcfg['retries'] + 1} attempts — {last_err.get(t, 'unknown error')}"
               for t in todo}
    return got, missing


def prev_close(df: pd.DataFrame, today: dt.date) -> float | None:
    d = df[df.index.date < today]
    return float(d["Close"].iloc[-1]) if len(d) else None


def fetch_quotes(tickers: list[str]) -> tuple[dict[str, dict], str | None]:
    """Latest pre-market / regular quote per ticker via Yahoo's batch quote endpoint (as the scanner does)."""
    try:
        import premarket_scanner as PS
        from yfinance.data import YfData
        PS.yf_utils._setup_cache()
        data = YfData()
        out = {}
        for i in range(0, len(tickers), 150):
            batch = [to_yahoo(t) for t in tickers[i:i + 150]]
            js = data.get_raw_json(PS.QUOTE_URL, params={"symbols": ",".join(batch), "fields": PS.QUOTE_FIELDS,
                                                         "formatted": "false", "lang": "en-US", "region": "US"})
            for q in (js.get("quoteResponse") or {}).get("result") or []:
                out[q["symbol"]] = q
        return out, None
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"


def latest_trade_today(q: dict | None, today: dt.date) -> tuple[float | None, str | None]:
    """Freshest price traded today (pre-market or regular), else (None, reason)."""
    if not q:
        return None, "no quote from Yahoo"

    def fresh(ts):
        try:
            return dt.datetime.fromtimestamp(int(ts), ET).date() == today
        except (TypeError, ValueError, OSError):
            return False

    cands = []
    if q.get("regularMarketPrice") and fresh(q.get("regularMarketTime")):
        cands.append((int(q["regularMarketTime"]), float(q["regularMarketPrice"])))
    if q.get("preMarketPrice") and fresh(q.get("preMarketTime")):
        cands.append((int(q["preMarketTime"]), float(q["preMarketPrice"])))
    if not cands:
        return None, "no trade yet today"
    return max(cands)[1], None


def cache_json(path: str, max_age_days: float | None = None):
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        if max_age_days is not None:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(obj["fetched_at"])).days
            if age >= max_age_days:
                return None
        return obj
    except Exception:
        return None


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
