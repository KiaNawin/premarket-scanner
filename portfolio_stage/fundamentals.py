"""
Next earnings date (refreshed daily) and fundamentals (refreshed weekly): market cap, revenue
growth TTM, net income TTM, free cash flow TTM, share count change YoY. Values that Yahoo does not
provide stay null, with the reason in `null_reasons` — nothing is estimated.
"""
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

from . import data as D

try:
    from run_cycle import NYSE_HOLIDAYS          # same holiday list as the scanner
except Exception:
    NYSE_HOLIDAYS = set()


def trading_days_between(a: dt.date, b: dt.date) -> int:
    return int(np.busday_count(a, b, holidays=sorted(NYSE_HOLIDAYS)))


def _first_date(x):
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        x = [v for v in x if v is not None]
        x = min(x) if x else None
    try:
        return pd.Timestamp(x).date()
    except Exception:
        return None


def earnings(ticker: str, cfg: dict, as_of: dt.date, ticker_obj=None) -> dict:
    path = os.path.join(cfg["paths"]["cache"], "earnings", as_of.isoformat(), f"{ticker}.json")
    hit = D.cache_json(path)
    if hit is None:
        hit = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "date": None, "reason": None}
        try:
            import yfinance as yf
            t = ticker_obj or yf.Ticker(ticker)
            cal = t.calendar
            d = _first_date((cal or {}).get("Earnings Date")) if isinstance(cal, dict) else None
            if d is None and isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                d = _first_date(list(cal.loc["Earnings Date"].values))
            hit["date"] = d.isoformat() if d else None
            if d is None:
                hit["reason"] = "Yahoo has no upcoming earnings date"
        except Exception as e:
            hit["reason"] = f"earnings lookup failed: {type(e).__name__}: {e}"
        D.save_json(path, hit)
    d = dt.date.fromisoformat(hit["date"]) if hit.get("date") else None
    soon = None
    if d is not None:
        soon = d >= as_of and trading_days_between(as_of, d) <= cfg["alerts"]["earnings_days"]
    return {"earnings_date": hit.get("date"), "earnings_soon": soon, "reason": hit.get("reason")}


def _row(df: pd.DataFrame | None, names: list[str]) -> pd.Series | None:
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index)
            return s.sort_index(ascending=False).dropna()
    return None


def fundamentals(ticker: str, cfg: dict, ticker_obj=None) -> dict:
    path = os.path.join(cfg["paths"]["cache"], "fundamentals", f"{ticker}.json")
    hit = D.cache_json(path, max_age_days=cfg["data"]["fundamentals_refresh_days"])
    if hit is not None:
        return hit
    out = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "name": None, "mcap": None, "rev_growth": None,
           "rev_growth_q_yoy": None, "ni": None, "fcf": None, "shares_chg": None, "reasons": {}}
    R = out["reasons"]
    try:
        import yfinance as yf
        t = ticker_obj or yf.Ticker(ticker)
    except Exception as e:
        for k in ("mcap", "rev_growth", "ni", "fcf", "shares_chg"):
            R[k] = f"yfinance unavailable: {e}"
        return out

    def safe(fn, key):
        try:
            return fn()
        except Exception as e:
            R.setdefault(key, f"{type(e).__name__}: {e}")
            return None

    info = safe(lambda: t.info or {}, "info") or {}
    out["name"] = info.get("shortName") or info.get("longName")
    fi = safe(lambda: t.fast_info, "fast_info")
    mcap = info.get("marketCap") or (safe(lambda: fi["market_cap"], "mcap") if fi is not None else None)
    out["mcap"] = float(mcap) if mcap else None
    if out["mcap"] is None:
        R.setdefault("mcap", "market cap not provided by Yahoo")
    if info.get("revenueGrowth") is not None:
        out["rev_growth_q_yoy"] = float(info["revenueGrowth"])

    inc = safe(lambda: t.quarterly_income_stmt, "rev_growth")
    cf = safe(lambda: t.quarterly_cashflow, "fcf")
    bs = safe(lambda: t.quarterly_balance_sheet, "shares_chg")

    rev = _row(inc, ["Total Revenue", "Operating Revenue"])
    if rev is not None and len(rev) >= 8:
        out["rev_growth"] = float(rev.iloc[:4].sum() / rev.iloc[4:8].sum() - 1)
    else:
        R.setdefault("rev_growth", f"TTM growth needs 8 quarters of revenue; Yahoo has {0 if rev is None else len(rev)}"
                     + (" (see rev_growth_q_yoy for latest-quarter YoY)" if out["rev_growth_q_yoy"] is not None else ""))
    ni = _row(inc, ["Net Income", "Net Income Common Stockholders"])
    if ni is not None and len(ni) >= 4:
        out["ni"] = float(ni.iloc[:4].sum())
    else:
        R.setdefault("ni", f"needs 4 quarters of net income; Yahoo has {0 if ni is None else len(ni)}")
    fcf = _row(cf, ["Free Cash Flow"])
    if fcf is not None and len(fcf) >= 4:
        out["fcf"] = float(fcf.iloc[:4].sum())
    else:
        R.setdefault("fcf", f"needs 4 quarters of free cash flow; Yahoo has {0 if fcf is None else len(fcf)}")
    sh = _row(bs, ["Ordinary Shares Number", "Share Issued"])
    if sh is not None and len(sh) >= 5 and sh.iloc[4]:
        out["shares_chg"] = float(sh.iloc[0] / sh.iloc[4] - 1)
    else:
        R.setdefault("shares_chg", f"needs 5 quarterly share counts; Yahoo has {0 if sh is None else len(sh)}")
    D.save_json(path, out)
    return out
