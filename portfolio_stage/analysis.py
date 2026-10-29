"""
Pure calculations (no network) — easy to unit-test.

Weinstein stage (weekly bars closing Friday):
  slope = % change of SMA30w over 4 weeks
  raw stage 2: close > SMA30w and slope > +SLOPE_T
  raw stage 4: close < SMA30w and slope < -SLOPE_T
  otherwise (flat slope, or price and slope disagree): previous stage 4/1 -> 1, previous 2/3 -> 3
  (no previous stage yet: close below SMA -> 1, above -> 3)
  A new stage is accepted only after the raw stage shows it for `confirm_weeks` weeks in a row.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd


def _f(x):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def r2(x, n=2):
    x = _f(x)
    return None if x is None else round(x, n)


# ============================================================================ weekly / stage
def last_complete_friday(now_et: dt.datetime) -> dt.date:
    """Friday of the most recent fully closed week (Friday counts after 16:00 ET)."""
    d = now_et.date()
    back = (d.weekday() - 4) % 7                       # days since Friday
    fri = d - dt.timedelta(days=back)
    if back == 0 and now_et.time() < dt.time(16, 0):
        fri -= dt.timedelta(days=7)
    return fri


def weekly_closes(close: pd.Series, complete_through: dt.date | None = None) -> pd.Series:
    s = pd.Series(close).dropna()
    if s.empty:
        return s
    w = s.resample("W-FRI").last().dropna()
    if complete_through is not None:
        w = w[w.index.date <= complete_through]
    return w


def _raw_stage(close, sma, slope, prev, t):
    if close > sma and slope > t:
        return 2
    if close < sma and slope < -t:
        return 4
    if prev in (4, 1):
        return 1
    if prev in (2, 3):
        return 3
    return 1 if close < sma else 3


def compute_stage(weekly: pd.Series, cfg: dict) -> dict:
    """weekly: Friday closes (oldest first). Returns stage info, or stage=None with a reason."""
    sc = cfg["stage"]
    w = pd.Series(weekly).dropna()
    out = {"stage": None, "stage_start": None, "slope": None, "sma30w": None, "sma10w": None,
           "slopes": [], "weeks": len(w), "since_data_start": False, "reason": None}
    lb, need = sc["slope_lookback_weeks"], sc["min_sma_weeks"] + sc["slope_lookback_weeks"]
    if len(w) < need:
        out["reason"] = f"only {len(w)} weekly bars (need {need})"
        return out
    sma = w.rolling(sc["sma_weeks"], min_periods=sc["min_sma_weeks"]).mean()
    sma10 = w.rolling(sc["sma_short_weeks"], min_periods=sc["sma_short_weeks"]).mean()
    slope = (sma / sma.shift(lb) - 1) * 100
    valid = slope.dropna().index
    if len(valid) == 0:
        out["reason"] = "SMA slope not computable"
        return out

    confirmed = start = pending = pending_start = None
    count = 0
    for ts in valid:
        raw = _raw_stage(w[ts], sma[ts], slope[ts], confirmed, sc["slope_t"])
        if confirmed is None:
            confirmed, start = raw, ts
            continue
        if raw == confirmed:
            pending, count = None, 0
            continue
        if raw == pending:
            count += 1
        else:
            pending, pending_start, count = raw, ts, 1
        if count >= sc["confirm_weeks"]:
            confirmed, start, pending, count = pending, pending_start, None, 0

    out.update(stage=confirmed, stage_start=start, slope=_f(slope.iloc[-1]), sma30w=_f(sma.iloc[-1]),
               sma10w=_f(sma10.iloc[-1]), slopes=[_f(x) for x in slope.dropna().iloc[-6:]],
               since_data_start=(start == valid[0]), pending=pending, pending_weeks=count)
    return out


def stage_days(daily_index: pd.DatetimeIndex, stage_start) -> int | None:
    """Trading days since the first day of the week in which the current stage began."""
    if stage_start is None:
        return None
    week_open = pd.Timestamp(stage_start) - pd.Timedelta(days=7)
    return int((pd.DatetimeIndex(daily_index) > week_open).sum())


def stage_flag(st: dict, price: float, rs_mansfield: float | None, days: int | None, cfg: dict) -> str | None:
    """✅ healthy stage 2 · ⚠️ stage 2 losing steam (⚠️ wins if both apply)."""
    if st.get("stage") != 2 or price is None:
        return None
    sc = cfg["stage"]
    m10, m30 = st.get("sma10w"), st.get("sma30w")
    slopes = [s for s in st.get("slopes", []) if s is not None]
    k = sc["warn_slope_down_weeks"]
    falling = len(slopes) > k and all(slopes[-i] < slopes[-i - 1] for i in range(1, k + 1))
    if (m10 is not None and price < m10) or falling or (days is not None and days > sc["warn_stage_days"]):
        return "⚠️"
    if m10 is not None and m30 is not None and price > m10 > m30 and rs_mansfield is not None and rs_mansfield > 0:
        return "✅"
    return None


def trend_label(price, m10, m30) -> str | None:
    if None in (price, m10, m30):
        return None
    if price > m10 > m30:
        return "Strong Bullish"
    if price > m30:
        return "Bullish"
    if price < m30 and m10 < m30:
        return "Bearish"
    return "Sideway"


# ============================================================================ RS
def mansfield_rs(stock_w: pd.Series, bench_w: pd.Series, cfg: dict) -> tuple[float | None, str | None]:
    rc = cfg["rs"]
    ratio = (pd.Series(stock_w) / pd.Series(bench_w)).dropna()
    if len(ratio) < rc["mansfield_min_weeks"]:
        return None, f"only {len(ratio)} weeks vs benchmark (need {rc['mansfield_min_weeks']})"
    base = ratio.rolling(rc["mansfield_weeks"], min_periods=rc["mansfield_min_weeks"]).mean().iloc[-1]
    note = None if len(ratio) >= rc["mansfield_weeks"] else f"partial: SMA over {len(ratio)} weeks"
    return _f(ratio.iloc[-1] / base - 1), note


def ret_vs(stock: pd.Series, bench: pd.Series, days: int) -> tuple[float | None, str | None]:
    """Return over `days` trading days minus the benchmark's, in percentage points."""
    df = pd.concat([stock, bench], axis=1, keys=["s", "b"]).dropna()
    if len(df) <= days:
        return None, f"only {len(df)} days (need {days + 1})"
    rs = df["s"].iloc[-1] / df["s"].iloc[-1 - days] - 1
    rb = df["b"].iloc[-1] / df["b"].iloc[-1 - days] - 1
    return _f((rs - rb) * 100), None


# ============================================================================ indicators
def rsi(close: pd.Series, n: int = 14) -> float | None:
    c = close.dropna()
    if len(c) <= n:
        return None
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    if _f(dn.iloc[-1]) == 0:
        return 100.0
    return _f(100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1]))


def atr(df: pd.DataFrame, n: int = 14) -> float | None:
    if df is None or len(df) <= n or not {"High", "Low", "Close"} <= set(df.columns):
        return None
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return _f(tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1])


def sma(close: pd.Series, n: int) -> float | None:
    c = close.dropna()
    return _f(c.iloc[-n:].mean()) if len(c) >= n else None


def minervini(df: pd.DataFrame, rs_mansfield: float | None, cfg: dict) -> dict:
    """Minervini Trend Template — 8 checks. Criterion 8 (RS rating >= 70) is approximated by Mansfield RS > 0."""
    mc = cfg["minervini"]
    c = df["Close"].dropna()
    p = _f(c.iloc[-1]) if len(c) else None
    m50, m150, m200 = sma(c, 50), sma(c, 150), sma(c, 200)
    m200_prev = _f(c.iloc[-200 - mc["sma200_trend_days"]:-mc["sma200_trend_days"]].mean()) \
        if len(c) >= 200 + mc["sma200_trend_days"] else None
    yr = df.iloc[-252:]
    hi52, lo52 = _f(yr["High"].max()) if "High" in yr else None, _f(yr["Low"].min()) if "Low" in yr else None

    def chk(*vals, cond):
        return None if any(v is None for v in vals) else bool(cond())

    checks = {
        "1_price_above_sma150_sma200": chk(p, m150, m200, cond=lambda: p > m150 and p > m200),
        "2_sma150_above_sma200": chk(m150, m200, cond=lambda: m150 > m200),
        "3_sma200_rising_1m": chk(m200, m200_prev, cond=lambda: m200 > m200_prev),
        "4_sma50_above_sma150_sma200": chk(m50, m150, m200, cond=lambda: m50 > m150 and m50 > m200),
        "5_price_above_sma50": chk(p, m50, cond=lambda: p > m50),
        "6_price_30pct_above_52w_low": chk(p, lo52, cond=lambda: p >= mc["low_mult"] * lo52),
        "7_price_within_25pct_of_52w_high": chk(p, hi52, cond=lambda: p >= mc["high_mult"] * hi52),
        "8_rs_positive_mansfield": chk(rs_mansfield, cond=lambda: rs_mansfield > 0),
    }
    avail = [v for v in checks.values() if v is not None]
    return {"tt_score": sum(avail), "tt_available": len(avail), "tt_checks": checks}


# ============================================================================ levels
def swing_levels(df: pd.DataFrame, lookback: int, k: int) -> tuple[list[float], list[float]]:
    """Pivot highs/lows (k bars each side) within the last `lookback` bars. The last k bars can't be confirmed."""
    d = df.iloc[-lookback:]
    hi, lo = d["High"].values, d["Low"].values
    highs, lows = [], []
    for i in range(k, len(d) - k):
        if hi[i] == hi[i - k:i + k + 1].max():
            highs.append(float(hi[i]))
        if lo[i] == lo[i - k:i + k + 1].min():
            lows.append(float(lo[i]))
    return highs, lows


def levels(df: pd.DataFrame, cfg: dict) -> dict:
    lc = cfg["levels"]
    c = df["Close"].dropna()
    p = _f(c.iloc[-1])
    a = atr(df, lc["atr_period"])
    m50, m200 = sma(c, 50), sma(c, 200)
    yr = df.iloc[-252:]
    hi52, lo52 = _f(yr["High"].max()), _f(yr["Low"].min())
    sh, sl = swing_levels(df, lc["swing_lookback_days"], lc["pivot_bars"])
    cands = {x for x in sh + sl + [m50, m200, hi52, lo52] if x is not None}
    below = [x for x in cands if x < p * (1 - 1e-6)]
    above = [x for x in cands if x > p * (1 + 1e-6)]
    support = max(below) if below else None
    resistance = min(above) if above else None
    reasons = {}
    stop = support - lc["stop_atr_mult"] * a if support is not None and a is not None else None
    if stop is None:
        reasons["stop"] = "no support level below price" if support is None else "ATR not available"
    at_high = hi52 is not None and p >= hi52 * (1 - 1e-3)
    if (at_high or resistance is None) and a is not None:
        target = p + lc["new_high_target_atr_mult"] * a
        target_basis = "new high: price + 3×ATR" if at_high else "no level above: price + 3×ATR"
    else:
        target, target_basis = resistance, "next resistance"
    if target is None:
        reasons["target"] = "no resistance and no ATR"
    rr = (target - p) / (p - stop) if None not in (target, stop) and p > stop else None
    if rr is None and "stop" not in reasons and "target" not in reasons:
        reasons["rr"] = "price at or below stop"
    return {"p": p, "atr": a, "m50": m50, "m200": m200, "hi": hi52, "lo": lo52, "rsi": rsi(c, lc["rsi_period"]),
            "support": support, "resistance": resistance, "stop": stop, "target": target,
            "target_basis": target_basis, "rr": _f(rr), "swing_highs": sh, "swing_lows": sl, "_reasons": reasons}
