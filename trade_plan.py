"""
Trade plan for a Trend-join Long entry: stop loss, R targets and position size.

Rules (mechanical, all settings in trade_config.json):
  breakout level = highest of yesterday's high, today's pre-market high and the high of day
                   before the signal bar — the level the stock just broke
  stop           = breakout level − stop_atr_buffer × ATR(14, daily)
                   (if that leaves less than min_stop_pct % of room, the stop is widened to it)
  1R             = entry − stop  (risk per share)
  targets        = entry + n × 1R  for n in targets_r
  shares         = floor(risk_per_trade_usd ÷ 1R), capped so shares × entry ≤ max_position_usd

Swing plan (shadow test, settings under "swing" in trade_config.json) — from The Investmin's 4 steps:
  stop           = lowest Low of the last `stop_candles` 5-minute candles up to the signal minute
                   (at least min_stop_pct below entry)
  risk           = account_usd × risk_pct / 100 when account_usd is set, else risk_per_trade_usd
  targets        = 3R and 5R: sell 1/3 at each; the last 1/3 exits on the first daily close below EMA9
"""
from __future__ import annotations

import json
import math
import os

DEFAULTS = {
    "risk_per_trade_usd": 100.0,
    "max_position_usd": 10000.0,
    "stop_atr_buffer": 0.25,
    "min_stop_pct": 0.5,
    "wide_stop_pct": 8.0,
    "targets_r": [1, 2],
    "exit_target_r": 2,
    "swing": {"enabled": True, "stop_candles": 2, "candle_minutes": 5, "min_stop_pct": 0.5,
              "account_usd": None, "risk_pct": 0.5, "partials": [[3, 0.3333], [5, 0.3333]],
              "trail_ema": 9, "max_days": 20, "start_date": "2026-10-26",
              "variants": [{"name": "A", "id": "be3r", "label": "stop to entry after 3R", "be_after": "3R"},
                           {"name": "B", "id": "be3r_d1", "label": "A + out at day-1 close unless 3R",
                            "be_after": "3R", "day1_exit_unless": "3R"}]},
}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_config.json")


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    cfg["swing"] = dict(DEFAULTS["swing"])
    try:
        with open(path, encoding="utf-8") as f:
            user = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        sw = user.pop("swing", None)
        cfg.update(user)
        if isinstance(sw, dict):
            cfg["swing"].update({k: v for k, v in sw.items() if not k.startswith("_")})
    except (OSError, ValueError):
        pass
    return cfg


def low_before_signal(minute, signal_time_et: str, candles: int = 2, minutes: int = 5) -> float | None:
    """Lowest Low of the last `candles` N-minute candles (regular session, aligned to 09:30) up to and
    including the signal minute. minute: 1-minute DataFrame, tz-aware or ET-naive index."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    if minute is None or getattr(minute, "empty", True) or not signal_time_et:
        return None
    try:
        sig = dt.datetime.strptime(signal_time_et, "%Y-%m-%d %H:%M").replace(tzinfo=et)
    except ValueError:
        return None
    m = minute.copy()
    m.index = m.index.tz_convert(et) if m.index.tz is not None else m.index.tz_localize(et)
    open_ = sig.replace(hour=9, minute=30)
    bucket = open_ + dt.timedelta(minutes=((sig - open_).seconds // 60) // minutes * minutes)
    start = max(open_, bucket - dt.timedelta(minutes=minutes * (candles - 1)))
    w = m[(m.index >= start) & (m.index <= sig)]["Low"].dropna()
    return _num(w.min()) if len(w) else None


def make_swing_plan(r: dict, cfg: dict | None = None) -> dict | None:
    """Shadow 'swing' plan for one VALID_ENTRY row (needs r['low_5m'] from Scanner B)."""
    cfg = cfg or load_config()
    sc = cfg["swing"]
    entry, low = _num(r.get("entry_price") or r.get("entry")), _num(r.get("low_5m"))
    if not sc.get("enabled") or not entry or entry <= 0 or low is None:
        return None
    stop = min(low, entry * (1 - sc["min_stop_pct"] / 100))
    risk_ps = entry - stop
    if risk_ps <= 0:
        return None
    risk_usd = (sc["account_usd"] * sc["risk_pct"] / 100) if sc.get("account_usd") else cfg["risk_per_trade_usd"]
    shares = int(risk_usd // risk_ps)
    cap = int(cfg["max_position_usd"] // entry) if cfg.get("max_position_usd") else shares
    capped = shares > cap
    shares = max(min(shares, cap), 0)
    return {"entry": round(entry, 4), "stop": round(stop, 4), "risk_per_share": round(risk_ps, 4),
            "stop_pct": round(risk_ps / entry * 100, 2), "low_5m": round(low, 4),
            "targets": {f"{n:g}R": round(entry + n * risk_ps, 4) for n, _ in sc["partials"]},
            "shares": shares, "position_usd": round(shares * entry, 2), "risk_usd": round(shares * risk_ps, 2),
            "size_capped": capped}


def _num(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def make_plan(r: dict, cfg: dict | None = None) -> dict | None:
    """r = one VALID_ENTRY row from Scanner B. Returns the plan dict, or None if entry is missing."""
    cfg = cfg or load_config()
    entry = _num(r.get("entry_price"))
    if not entry or entry <= 0:
        return None
    levels = {"Y-High": _num(r.get("yesterday_high")), "PM-High": _num(r.get("premarket_high")),
              "HOD": _num(r.get("hod_before_signal"))}
    levels = {k: v for k, v in levels.items() if v and v < entry}
    if levels:
        level_name, level = max(levels.items(), key=lambda kv: kv[1])
    else:
        level_name, level = "entry", entry
    atr = _num(r.get("atr14"))
    stop = level - cfg["stop_atr_buffer"] * atr if atr else level
    max_stop = entry * (1 - cfg["min_stop_pct"] / 100)          # at least min_stop_pct of room
    stop = min(stop, max_stop)
    risk_ps = entry - stop
    if risk_ps <= 0:
        return None
    shares = int(cfg["risk_per_trade_usd"] // risk_ps)
    cap = int(cfg["max_position_usd"] // entry) if cfg.get("max_position_usd") else shares
    capped = shares > cap
    shares = max(min(shares, cap), 0)
    stop_pct = risk_ps / entry * 100
    return {
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "risk_per_share": round(risk_ps, 4),
        "stop_pct": round(stop_pct, 2),
        "breakout_level": round(level, 4),
        "breakout_name": level_name,
        "atr14": round(atr, 4) if atr else None,
        "targets": {f"{n:g}R": round(entry + n * risk_ps, 4) for n in cfg["targets_r"]},
        "exit_target_r": cfg["exit_target_r"],
        "shares": shares,
        "position_usd": round(shares * entry, 2),
        "risk_usd": round(shares * risk_ps, 2),
        "size_capped": capped,
        "wide_stop": stop_pct > cfg["wide_stop_pct"],
    }
