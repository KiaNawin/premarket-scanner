"""
Accumulation tracker for stocks that have been falling (weekly; idea from a Thai investor's RKLB post):
"last support = weekly EMA200; wait until selling is exhausted (weekly RSI < 30), no new low, a dull sideways
base = Stage 1 -> accumulate; weekly breakout then a pullback that holds (higher low) -> add; Stage 2 -> full".

Weekly OHLC through the last complete Friday. Per holding (`accumulate` in portfolio_stage.json):
  ema200w, dist_ema200w_pct, near_ema200w      weekly EMA200 = the last support (needs 200 weeks)
  rsi_w, rsi_w_min                             weekly RSI14 now and its low over `oversold_lookback_weeks`
  peak, peak_week                              highest weekly high of the last `low_lookback_weeks` (decline start)
  low, low_week, weeks_since_low, drawdown_pct lowest weekly low since that peak
  base_high, base_low, base_width_pct          range of the weeks after that low (the Stage 1 base)
  breakout_week, higher_low                    first weekly close above base_high, and a pullback low that held
  state  falling   still making lows, or selling not exhausted yet (no weekly RSI < 30)      -> wait
         base      exhausted + no new low for >= min_base_weeks + base no wider than max   -> start accumulating
         breakout  weekly close above the base, pullback held above the base low, and price still
                   within breakout_hold_pct of the base high (else: failed breakout, back to base)  -> add
         stage2    weekly Stage 2 (our Weinstein engine)                                   -> full position
         shallow   fell less than min_drawdown_pct from the 52-week peak (not a deep decline)
Rules, not advice; every threshold is in config.yaml (`accumulate:`).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from . import analysis as A

LABELS = {"falling": "Falling — wait (selling not exhausted / new lows)",
          "base": "Base (Stage 1) — start accumulating",
          "breakout": "Breakout + higher low — add",
          "stage2": "Stage 2 — full position",
          "shallow": "Shallow pullback — not a deep decline, nothing to rebuild"}


def weekly_ohlc(df: pd.DataFrame, through: dt.date) -> pd.DataFrame:
    d = df.dropna(subset=["Close"])
    d = d[d.index.date <= through]
    if d.empty:
        return d
    w = d.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return w[w.index.date <= through]


def _rsi_series(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def evaluate(df: pd.DataFrame, fri: dt.date, stage: int | None, cfg: dict) -> tuple[dict, str | None]:
    ac = cfg["accumulate"]
    w = weekly_ohlc(df, fri)
    n = len(w)
    if n < ac["min_weeks"]:
        return {"state": None}, f"needs {ac['min_weeks']} weekly bars, have {n}"
    c, h, lo = w["Close"], w["High"], w["Low"]
    out = {"weeks": n, "price_w": A.round_px(c.iloc[-1])}
    if n >= 200:
        ema = c.ewm(span=200, adjust=False).mean().iloc[-1]
        out["ema200w"] = A.round_px(ema)
        out["dist_ema200w_pct"] = A.r2((c.iloc[-1] / ema - 1) * 100)
        out["near_ema200w"] = abs(out["dist_ema200w_pct"]) <= ac["near_ema_pct"]
    else:
        out.update(ema200w=None, dist_ema200w_pct=None, near_ema200w=None)
    rs = _rsi_series(c)
    out["rsi_w"] = A.r2(rs.iloc[-1], 1) if pd.notna(rs.iloc[-1]) else None
    recent_rsi = rs.iloc[-ac["oversold_lookback_weeks"]:]
    out["rsi_w_min"] = A.r2(recent_rsi.min(), 1) if recent_rsi.notna().any() else None
    ov = recent_rsi[recent_rsi < ac["oversold_rsi"]]
    out["oversold_week"] = ov.index[-1].strftime("%Y-%m-%d") if len(ov) else None

    win = w.iloc[-ac["low_lookback_weeks"]:]
    pk = win["High"].idxmax()                                           # the current decline starts at the peak
    leg = w.loc[pk:]
    li = leg["Low"].idxmin()                                            # ... and its lowest week so far
    pos = w.index.get_loc(li)
    peak = float(w.at[pk, "High"])
    out["peak"], out["peak_week"] = A.round_px(peak), pk.strftime("%Y-%m-%d")
    out["low"], out["low_week"] = A.round_px(w["Low"].iloc[pos]), li.strftime("%Y-%m-%d")
    out["weeks_since_low"] = n - 1 - pos
    drop = (1 - w["Low"].iloc[pos] / peak) * 100                        # how deep the fall from the peak is
    out["drawdown_pct"] = A.r2(-drop)

    base = w.iloc[pos + 1:]                                             # the weeks after the low
    bo_week, higher_low = None, None
    if len(base) >= ac["min_base_weeks"]:
        core = base.iloc[:ac["min_base_weeks"]]                         # first weeks after the low define the box
        bh, bl = float(core["High"].max()), float(min(core["Low"].min(), w["Low"].iloc[pos]))
        out["base_high"], out["base_low"] = A.round_px(bh), A.round_px(bl)
        out["base_width_pct"] = A.r2((bh / bl - 1) * 100)
        after = base.iloc[ac["min_base_weeks"]:]
        brk = after[after["Close"] > bh]
        if len(brk):
            bo_week = brk.index[0]
            post = w.loc[bo_week:].iloc[1:]
            if len(post):                                # retest: dipped back under the breakout week's high
                pull = float(post["Low"].min())          # without losing the base low
                higher_low = bool(pull > bl and pull < float(w.loc[bo_week, "High"]))
                out["pullback_low"] = A.round_px(pull)
    else:
        out.update(base_high=None, base_low=None, base_width_pct=None)
    out["breakout_week"] = bo_week.strftime("%Y-%m-%d") if bo_week is not None else None
    out["higher_low"] = higher_low

    exhausted = out["oversold_week"] is not None or (ac["ema_counts_as_exhausted"] and bool(out.get("near_ema200w")))
    fell = drop >= ac["min_drawdown_pct"]
    if stage == 2:
        state = "stage2"
    elif not fell:
        state = "shallow"
    elif (bo_week is not None and higher_low
          and c.iloc[-1] >= out["base_high"] * (1 - ac["breakout_hold_pct"] / 100)):
        state = "breakout"
    elif (exhausted and out["weeks_since_low"] >= ac["min_base_weeks"]
          and (out.get("base_width_pct") or 999) <= ac["max_base_width_pct"]):
        state = "base"
    else:
        state = "falling"
    out["state"], out["label"] = state, LABELS[state]
    why = []
    if bo_week is not None and state != "breakout" and state != "stage2":
        why.append(f"breakout of {out['breakout_week']} failed: back inside the base")
    if state == "falling":
        if out["weeks_since_low"] < ac["min_base_weeks"]:
            why.append(f"new low {out['weeks_since_low']} week(s) ago")
        if not exhausted:
            why.append(f"weekly RSI hasn't been below {ac['oversold_rsi']} (min {out['rsi_w_min']})")
        if (out.get("base_width_pct") or 0) > ac["max_base_width_pct"]:
            why.append(f"range {out['base_width_pct']:.0f}% still too wide")
    out["why"] = "; ".join(why) or None
    return out, None


def alert_text(t: str, new: dict, old: dict | None) -> str | None:
    """One line when the state changes (none on the first run or when unchanged)."""
    if not old or not old.get("state") or old.get("state") == new.get("state") or not new.get("state"):
        return None
    extra = []
    if new.get("ema200w"):
        extra.append(f"{new['dist_ema200w_pct']:+.0f}% vs weekly EMA200 ${new['ema200w']:,.2f}")
    if new.get("rsi_w") is not None:
        extra.append(f"weekly RSI {new['rsi_w']:.0f}")
    return f"**{t}** {old['state']} → **{new['state']}**: {LABELS[new['state']]}" + (f" ({', '.join(extra)})" if extra else "")
