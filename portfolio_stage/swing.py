"""
Swing-entry fields per ticker (spec: scanner_prompt_swing.md). Daily bars, closed bars only.

  ema9, ema21          EMA of the close (span n, adjust=False — same recursion as TradingView/Yahoo)
  avwap                {value, anchor_date}: VWAP of (H+L+C)/3 x volume from the lowest Low of the last 126 bars
  prev_high            High of the latest bar ("break yesterday's high" entry for the next day)
  prev_low2            lowest Low of the last 2 bars (stop for group C)
  gaps_open            up to 2 unfilled up-gaps of the last 120 bars below the price: [{bot, top, date}], top desc
  base_tight           {hl, contr, reclaim, ok}
  first_retest         {level, date} | None — first pullback to a just-broken swing high (date = breakout day)
  ret_1m/3m/6m         % return over 21 / 63 / 126 bars
Every value that can't be computed is None, with the reason in the returned `reasons` dict.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .analysis import _f, r2, round_px

FIELDS = ("ema9", "ema21", "avwap", "prev_high", "prev_low2", "gaps_open", "base_tight", "first_retest",
          "ret_1m", "ret_3m", "ret_6m")
RET_WINDOWS = {"ret_1m": 21, "ret_3m": 63, "ret_6m": 126}


def closed_bars(df: pd.DataFrame, now_et: dt.datetime, bar_complete: bool) -> pd.DataFrame:
    """Drop today's bar while it is still forming (run before 16:00 ET on a trading day)."""
    d = df.dropna(subset=["Close"])
    if not bar_complete and len(d) and d.index[-1].date() == now_et.date():
        d = d.iloc[:-1]
    return d


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def returns(close: pd.Series) -> dict:
    c = close.dropna()
    return {k: (r2((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100) if len(c) > n and c.iloc[-1 - n] > 0 else None)
            for k, n in RET_WINDOWS.items()}


def avwap(df: pd.DataFrame, lookback: int = 126) -> dict | None:
    d = df.iloc[-lookback:]
    if d.empty or d["Low"].isna().all():
        return None
    anchor = d["Low"].idxmin()
    a = df.loc[anchor:]
    tp = (a["High"] + a["Low"] + a["Close"]) / 3
    v = a["Volume"].fillna(0)
    if v.sum() <= 0:
        return None
    return {"value": round_px((tp * v).sum() / v.sum()), "anchor_date": anchor.strftime("%Y-%m-%d")}


def gaps_open(df: pd.DataFrame, price: float, lookback: int = 120, keep: int = 2) -> list[dict]:
    h, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    n = len(df)
    out = []
    for i in range(max(1, n - lookback), n):
        if not (lo[i] > h[i - 1]):
            continue
        bot, top = h[i - 1], lo[i]
        later = lo[i + 1:]
        if later.size:
            m = float(np.nanmin(later))
            if m <= bot:
                continue                                    # filled
            top = min(top, m)                               # partially filled
        bot, top = round_px(bot), round_px(top)
        if bot < top < price:
            out.append({"bot": bot, "top": top, "date": df.index[i].strftime("%Y-%m-%d")})
    return sorted(out, key=lambda g: -g["top"])[:keep]


def _swing_lows(low: np.ndarray, start: int, k: int) -> list[int]:
    n = len(low)
    return [j for j in range(max(start, k), n - k)
            if low[j] == np.nanmin(low[j - k:j + k + 1])]


def base_tight(df: pd.DataFrame, e9: pd.Series, e21: pd.Series) -> tuple[dict | None, str | None]:
    n = len(df)
    if n < 40:
        return None, f"needs 40 bars, have {n}"
    lo = df["Low"].to_numpy(float)
    sl = _swing_lows(lo, n - 63, 3)
    pts = [lo[j] for j in sl][-3:]
    hl = len(pts) >= 2 and all(b > a for a, b in zip(pts, pts[1:]))
    rng = ((df["High"] - df["Low"]) / df["Close"]).to_numpy(float)
    recent, before = np.nanmean(rng[-10:]), np.nanmean(rng[-40:-10])
    contr = bool(before > 0 and recent < 0.75 * before)
    c = float(df["Close"].iloc[-1])
    reclaim = bool(c > e9.iloc[-1] and c > e21.iloc[-1] and e9.iloc[-1] >= e9.iloc[-4] and e21.iloc[-1] >= e21.iloc[-6])
    hl = bool(hl)
    return {"hl": hl, "contr": contr, "reclaim": reclaim, "ok": hl and contr and reclaim}, None


def first_retest(df: pd.DataFrame) -> dict | None:
    h, lo, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    n = len(df)
    if n < 25:
        return None
    best = None                                             # (breakout index, level)
    for j in range(max(5, n - 1 - 150), n - 1 - 10 + 1):     # swing highs 150..10 bars back
        if h[j] != np.nanmax(h[j - 5:j + 6]):
            continue
        lvl = h[j]
        crosses = [b for b in range(max(j + 1, n - 30), n - 1) if c[b - 1] < lvl < c[b]]
        if not crosses:
            continue
        b = crosses[-1]
        if np.any(lo[b + 1:n - 1] <= lvl * 1.03):           # already came back near it before today
            continue
        if lo[-1] <= lvl * 1.03 and c[-1] >= lvl * 0.98:
            if best is None or b > best[0]:
                best = (b, lvl)
    if best is None:
        return None
    return {"level": round_px(best[1]), "date": df.index[best[0]].strftime("%Y-%m-%d")}


def compute(df: pd.DataFrame) -> tuple[dict, dict]:
    """df = closed daily bars (oldest first). Returns (fields, null_reasons)."""
    out = {k: None for k in FIELDS}
    out["gaps_open"] = []
    why: dict[str, str] = {}
    d = df.dropna(subset=["Close"])
    n = len(d)
    if n < 2:
        return out, {k: f"only {n} closed bars" for k in FIELDS}
    c = d["Close"]
    e9, e21 = ema(c, 9), ema(c, 21)
    price = float(c.iloc[-1])
    if n >= 9:
        out["ema9"] = round_px(e9.iloc[-1])
    else:
        why["ema9"] = f"needs 9 bars, have {n}"
    if n >= 21:
        out["ema21"] = round_px(e21.iloc[-1])
    else:
        why["ema21"] = f"needs 21 bars, have {n}"
    out["avwap"] = avwap(d)
    if out["avwap"] is None:
        why["avwap"] = "no volume since the 126-day low"
    out["prev_high"] = round_px(d["High"].iloc[-1])
    out["prev_low2"] = round_px(d["Low"].iloc[-2:].min())
    out["gaps_open"] = gaps_open(d, round_px(price))
    if n >= 21:
        out["base_tight"], r = base_tight(d, e9, e21)
        if r:
            why["base_tight"] = r
    else:
        why["base_tight"] = f"needs 40 bars, have {n}"
    out["first_retest"] = first_retest(d)
    rets = returns(c)
    out.update(rets)
    for k, v in rets.items():
        if v is None:
            why[k] = f"needs {RET_WINDOWS[k] + 1} bars, have {n}"
    return out, why


def momentum_score(rets: dict) -> float | None:
    """Average of ret_1m/3m/6m — all three required."""
    vals = [rets.get(k) for k in RET_WINDOWS]
    return None if any(v is None for v in vals) else float(np.mean(vals))


def top_pct(score: float | None, universe_scores: list[float]) -> int | None:
    """1-100: 5 = in the top 5% of the scanned universe (score is ranked among universe + itself)."""
    if score is None or not universe_scores:
        return None
    better = sum(1 for s in universe_scores if s > score)
    n = len(universe_scores) + (0 if score in universe_scores else 1)
    return int(min(100, max(1, np.ceil((better + 1) / n * 100))))
