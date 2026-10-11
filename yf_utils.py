"""
Robust Yahoo Finance downloads shared by both scanners.

* Keeps yfinance's timezone cache in a project-local folder (.yf_cache) so two runs, or a
  OneDrive/antivirus lock on the default cache, don't cause "database is locked" errors.
* Retries any ticker that came back empty, one at a time without threads.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import pandas as pd
import yfinance as yf

_CACHE_SET = False


def _setup_cache() -> None:
    global _CACHE_SET
    if _CACHE_SET:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".yf_cache")
    os.makedirs(path, exist_ok=True)
    try:
        yf.set_tz_cache_location(path)
    except Exception as e:  # older/newer yfinance without this API — not fatal
        print(f"(yfinance cache location not set: {e})", file=sys.stderr)
    _CACHE_SET = True


def _split(df: pd.DataFrame | None, tickers: list[str]) -> dict[str, pd.DataFrame | None]:
    out: dict[str, pd.DataFrame | None] = {t: None for t in tickers}
    if df is None or df.empty:
        return out
    if not isinstance(df.columns, pd.MultiIndex):
        if len(tickers) == 1:
            out[tickers[0]] = df
        return out
    level0 = set(df.columns.get_level_values(0))
    for t in tickers:
        if t in level0:
            sub = df[t]
            if isinstance(sub, pd.Series):
                sub = sub.to_frame()
            sub = sub.dropna(how="all")
            out[t] = sub if not sub.empty else None
    return out


def download(tickers: list[str], retries: int = 2, **kw: Any) -> dict[str, pd.DataFrame | None]:
    """yf.download wrapper → {ticker: DataFrame or None}, retrying empty tickers serially."""
    _setup_cache()
    base: dict[str, Any] = {
        "group_by": "ticker",
        "auto_adjust": False,
        "threads": True,
        "progress": False,
    }
    base.update(kw)
    try:
        result: dict[str, pd.DataFrame | None] = _split(yf.download(tickers, **base), tickers)
    except Exception as e:
        print(f"    download failed: {e}", file=sys.stderr)
        result = {t: None for t in tickers}

    for attempt in range(retries):
        missing = [t for t, v in result.items() if v is None]
        if not missing or len(missing) > 50:   # many missing = genuinely no data, not a glitch
            break
        time.sleep(1 + attempt)
        for t in missing:
            try:
                df = yf.download(t, **{**base, "threads": False})
                result.update(_split(df, [t]))
            except Exception:
                pass
    return result
