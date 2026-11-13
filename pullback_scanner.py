#!/usr/bin/env python3
"""
ข้อ A — Pullback-to-support entry (intraday, runs on GitHub with the scanners)

Watch list = the whole-market momentum scan (portfolio_stage momentum, check #6): every "strong" stock
(top momentum, weekly Stage 2), with or without a daily setup. GitHub builds it before the open
(.github/workflows/momentum.yml -> watchlists/momentum_YYYY-MM-DD.json).

For each stock, support levels come from closed daily bars (yesterday and before):
  EMA9, EMA21, and the "first retest" level from the momentum scan (a swing high broken in the last
  30 days that the stock has come back to). A level only counts if yesterday's close was above it.

Signal on today's 5-minute bars (regular session, closed bars only):
  1. touch    a bar's Low comes within  near_atr x ATR14 above the level (or dips at most below_atr x ATR
              under it) — the stock has pulled back to support
  2. hold     no bar closes more than below_atr x ATR under the level (that would be a broken support)
  3. trigger  within max_bars_after_touch bars of the touch, a bar CLOSES above the previous bar's High,
              and above the level, at/after start_et
  4. not chasing  entry <= level + max_entry_atr x ATR
Entry = trigger bar's close · stop = lowest Low since the touch (at least min_stop_pct below) ·
targets n x R (trade_config.json "pullback").
Results are a back-test of mechanical rules (history/pullback_trades.csv), not advice.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
BASE = os.path.dirname(os.path.abspath(__file__))
WATCH_DIR = os.path.join(BASE, "watchlists")
SIGNALS_DIR = os.path.join(BASE, "history", "pullback_signals")
TRADES_CSV = os.path.join(BASE, "history", "pullback_trades.csv")
RTH_OPEN, RTH_CLOSE = dt.time(9, 30), dt.time(16, 0)
BAR = dt.timedelta(minutes=5)

DEFAULTS = {"enabled": True, "near_atr": 0.3, "below_atr": 0.5, "max_bars_after_touch": 6,
            "max_entry_atr": 1.0, "start_et": "09:45", "end_et": "15:30", "min_stop_pct": 0.5,
            "wide_stop_pct": 6.0, "targets_r": [1, 2, 3], "exit_target_r": 2, "max_list_age_days": 4,
            "include_no_setup": True}
LEVEL_NAMES = {"ema9": "EMA9", "ema21": "EMA21", "retest": "แนวเดิม"}


def load_config() -> dict:
    import trade_plan
    cfg = trade_plan.load_config()
    pb = dict(DEFAULTS)
    pb.update({k: v for k, v in (cfg.get("pullback") or {}).items() if not k.startswith("_")})
    pb["risk_per_trade_usd"] = cfg["risk_per_trade_usd"]
    pb["max_position_usd"] = cfg["max_position_usd"]
    return pb


def _r(x, n=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else round(x, n)


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def _to_et(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = df.index.tz_convert(ET) if df.index.tz is not None else df.index.tz_localize(ET)
    return df


# ----------------------------------------------------------------------------- watch list
def watchlist_path(day: dt.date, watch_dir: str = WATCH_DIR) -> str:
    return os.path.join(watch_dir, f"momentum_{day.isoformat()}.json")


def load_watchlist(day: dt.date, cfg: dict | None = None, watch_dir: str = WATCH_DIR) -> tuple[list[dict], str | None]:
    """Latest momentum list dated <= day and not older than max_list_age_days. Returns (stocks, as_of)."""
    cfg = cfg or load_config()
    files = sorted(f for f in glob.glob(os.path.join(watch_dir, "momentum_????-??-??.json"))
                   if os.path.basename(f)[9:19] <= day.isoformat())
    if not files:
        return [], None
    as_of = os.path.basename(files[-1])[9:19]
    if (day - dt.date.fromisoformat(as_of)).days > cfg["max_list_age_days"]:
        return [], as_of
    try:
        with open(files[-1], encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return [], as_of
    rows = doc.get("strong") or doc.get("leaders") or []
    if not cfg.get("include_no_setup", True):
        rows = [r for r in rows if (r.get("base_tight") or {}).get("ok") or r.get("first_retest")]
    return rows, as_of


# ----------------------------------------------------------------------------- core (pure)
def daily_levels(daily: pd.DataFrame, today: dt.date, retest: float | None = None) -> dict | None:
    """Support levels + ATR14 from daily bars closed before `today`."""
    d = _to_et(daily.dropna(subset=["Close"]))
    d = d[d.index.date < today]
    if len(d) < 22:
        return None
    c = d["Close"]
    prev_c = c.shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - prev_c).abs(), (d["Low"] - prev_c).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    if not atr or math.isnan(atr):
        return None
    y_close = float(c.iloc[-1])
    lv = {"ema9": float(c.ewm(span=9, adjust=False).mean().iloc[-1]),
          "ema21": float(c.ewm(span=21, adjust=False).mean().iloc[-1])}
    if retest:
        lv["retest"] = float(retest)
    lv = {k: v for k, v in lv.items() if v < y_close}            # support = below yesterday's close
    return {"atr14": atr, "y_close": y_close, "levels": lv}


def closed_5m(minute: pd.DataFrame, today: dt.date, now: dt.datetime | None) -> pd.DataFrame:
    """Regular-session 5-minute bars of `today` that have finished by `now` (index = bar start)."""
    m = _to_et(minute.dropna(subset=["Close"]))
    m = m[(m.index.date == today) & (m.index.time >= RTH_OPEN) & (m.index.time < RTH_CLOSE)]
    if len(m) >= 2 and (m.index[1] - m.index[0]) < BAR:           # 1-minute input -> resample
        m = m.resample("5min", origin="start_day", offset="30min", label="left", closed="left").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    if now is not None:
        m = m[m.index + BAR <= now]
    return m


def find_trigger(bars: pd.DataFrame, level: float, atr: float, cfg: dict) -> dict | None:
    """First pullback-to-`level` + 5-min breakout of the previous bar's high. None if not (yet)."""
    near, below = cfg["near_atr"] * atr, cfg["below_atr"] * atr
    start, end = _hm(cfg["start_et"]), _hm(cfg["end_et"])
    hi, lo, cl = bars["High"].to_numpy(float), bars["Low"].to_numpy(float), bars["Close"].to_numpy(float)
    touch = None
    for j in range(len(bars)):
        if cl[j] < level - below:                                  # support broken: done for the day
            return None
        if touch is not None and j - touch > cfg["max_bars_after_touch"]:
            touch = None                                           # bounce came to nothing; wait for a new touch
        if lo[j] <= level + near and touch is None:
            touch = j
        if touch is None or j < 1:
            continue
        t_end = (bars.index[j] + BAR).time()
        if not (start <= t_end <= end):
            continue
        if cl[j] > hi[j - 1] and cl[j] > level and cl[j] <= level + cfg["max_entry_atr"] * atr:
            low = float(lo[touch:j + 1].min())
            return {"touch_et": bars.index[touch].strftime("%H:%M"), "bar_et": bars.index[j].strftime("%H:%M"),
                    "entry_et": (bars.index[j] + BAR).strftime("%H:%M"),
                    "signal_time_et": (bars.index[j] + BAR - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"),
                    "entry": float(cl[j]), "prev_bar_high": float(hi[j - 1]), "pullback_low": low}
    return None


def make_plan(entry: float, low: float, cfg: dict) -> dict | None:
    stop = min(low, entry * (1 - cfg["min_stop_pct"] / 100))
    risk = entry - stop
    if risk <= 0:
        return None
    shares = int(cfg["risk_per_trade_usd"] // risk)
    cap = int(cfg["max_position_usd"] // entry) if cfg.get("max_position_usd") else shares
    capped = shares > cap
    shares = max(min(shares, cap), 0)
    pct = risk / entry * 100
    return {"entry": round(entry, 4), "stop": round(stop, 4), "risk_per_share": round(risk, 4),
            "stop_pct": round(pct, 2), "targets": {f"{n:g}R": round(entry + n * risk, 4) for n in cfg["targets_r"]},
            "exit_target_r": cfg["exit_target_r"], "shares": shares, "position_usd": round(shares * entry, 2),
            "risk_usd": round(shares * risk, 2), "size_capped": capped, "wide_stop": pct > cfg["wide_stop_pct"]}


def evaluate(info: dict, daily: pd.DataFrame | None, minute: pd.DataFrame | None, today: dt.date,
             now: dt.datetime | None, cfg: dict) -> dict:
    t = info["ticker"]
    res = {"ticker": t, "signal": False, "status": "NO_DATA",
           "setup": "base" if (info.get("base_tight") or {}).get("ok") else "retest" if info.get("first_retest") else None,
           "ret_3m": info.get("ret_3m"), "rank": info.get("ret_rank_top_pct")}
    if daily is None or getattr(daily, "empty", True):
        return res
    dl = daily_levels(daily, today, (info.get("first_retest") or {}).get("level"))
    if not dl:
        res["status"] = "SHORT_HISTORY"
        return res
    atr = dl["atr14"]
    res.update(atr14=_r(atr), y_close=_r(dl["y_close"]), levels={k: _r(v) for k, v in dl["levels"].items()})
    if not dl["levels"]:
        res["status"] = "NO_SUPPORT"                               # closed below EMA9/21 yesterday
        return res
    bars = closed_5m(minute, today, now) if minute is not None and not minute.empty else pd.DataFrame()
    if bars.empty:
        res["status"] = "WAITING"
        return res
    last = float(bars["Close"].iloc[-1])
    res["last_price"], res["last_bar_et"] = _r(last), bars.index[-1].strftime("%H:%M")
    nearest = min(dl["levels"].items(), key=lambda kv: abs(last - kv[1]))
    res["nearest"] = {"name": nearest[0], "level": _r(nearest[1]), "dist_atr": _r((last - nearest[1]) / atr, 2)}
    hits = []
    for name, lvl in dl["levels"].items():
        trig = find_trigger(bars, lvl, atr, cfg)
        if trig:
            hits.append((trig["signal_time_et"], -lvl, name, lvl, trig))
    if not hits:
        broken = [k for k, v in dl["levels"].items() if (bars["Close"] < v - cfg["below_atr"] * atr).any()]
        res["status"] = "BROKEN" if broken and len(broken) == len(dl["levels"]) else "NO_SIGNAL"
        res["near_now"] = abs(last - nearest[1]) <= cfg["near_atr"] * atr or last < nearest[1]
        return res
    hits.sort()
    _, _, name, lvl, trig = hits[0]
    same = [n for _, _, n, v, tg in hits if n != name and tg["signal_time_et"] == trig["signal_time_et"]]
    plan = make_plan(trig["entry"], trig["pullback_low"], cfg)
    if not plan:
        res["status"] = "NO_SIGNAL"
        return res
    res.update(signal=True, status="VALID_ENTRY", level_name=name, level=_r(lvl), also_levels=same, **trig,
               entry_price=_r(trig["entry"]), plan=plan)
    plan.update(breakout_level=_r(lvl), breakout_name=LEVEL_NAMES.get(name, name), atr14=_r(atr))
    if last <= plan["stop"]:
        res.update(signal=False, status="STOPPED")               # already back under the stop: don't alert
    return res


# ----------------------------------------------------------------------------- data + run
def fetch(tickers: list[str]):
    import yf_utils
    daily = yf_utils.download(tickers, period="6mo", interval="1d")
    minute = yf_utils.download(tickers, period="1d", interval="5m", prepost=False)
    return {t: (daily.get(t), minute.get(t)) for t in tickers}


def run(day: dt.date, now: dt.datetime | None = None, fetcher=None, watch_dir: str = WATCH_DIR) -> dict:
    """{"as_of": momentum list date, "watch": n, "evaluated": [...], "error": str|None}"""
    cfg = load_config()
    out = {"as_of": None, "watch": 0, "evaluated": [], "error": None, "check_time_et": None}
    if not cfg.get("enabled", True):
        out["error"] = "disabled"
        return out
    rows, as_of = load_watchlist(day, cfg, watch_dir)
    out["as_of"], out["watch"] = as_of, len(rows)
    if not rows:
        if as_of is None:
            out["error"] = "ยังไม่มีรายชื่อหุ้นโมเมนตัม (watchlists/momentum_*.json)"
        elif (day - dt.date.fromisoformat(as_of)).days > cfg["max_list_age_days"]:
            out["error"] = f"รายชื่อหุ้นโมเมนตัมเก่าเกินไป ({as_of})"
        else:
            out["error"] = f"รายชื่อหุ้นโมเมนตัม {as_of} ว่าง (ไม่มีหุ้นผ่านเกณฑ์ หรือสแกนไม่สำเร็จ)"
        return out
    now = now or dt.datetime.now(ET)
    out["check_time_et"] = now.strftime("%H:%M")
    try:
        data = (fetcher or fetch)([r["ticker"] for r in rows])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for r in rows:
        out["evaluated"].append(evaluate(r, *data.get(r["ticker"], (None, None)), today=day, now=now, cfg=cfg))
    out["evaluated"].sort(key=lambda x: (not x["signal"], x.get("signal_time_et") or "", x["ticker"]))
    return out


def export_watchlist(src_json: str, day: dt.date, watch_dir: str = WATCH_DIR) -> str:
    """Copy output/<date>/momentum.json to watchlists/momentum_<date>.json (public market data only)."""
    with open(src_json, encoding="utf-8") as f:
        doc = json.load(f)
    keep = ("ticker", "price", "ret_1m", "ret_3m", "ret_6m", "ret_rank_top_pct", "base_tight", "first_retest",
            "ema9", "ema21", "prev_high", "prev_low2")
    slim = lambda rows: [{k: r.get(k) for k in keep} for r in rows or []]
    out = {"as_of": doc.get("as_of"), "filter": doc.get("filter"), "funnel": doc.get("funnel"),
           "error": doc.get("error"), "leaders": slim(doc.get("leaders")), "strong": slim(doc.get("strong"))}
    os.makedirs(watch_dir, exist_ok=True)
    path = watchlist_path(day, watch_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    return path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ข้อ A — pullback-to-support check")
    ap.add_argument("--export", metavar="MOMENTUM_JSON", help="copy a momentum.json into watchlists/")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today ET)")
    a = ap.parse_args()
    d = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(ET).date()
    if a.export:
        print(export_watchlist(a.export, d))
        sys.exit(0)
    res = run(d)
    print(json.dumps({k: v for k, v in res.items() if k != "evaluated"}, ensure_ascii=False))
    for r in res["evaluated"]:
        print(r["ticker"], r["status"], r.get("level_name") or "", r.get("entry_price") or "")
