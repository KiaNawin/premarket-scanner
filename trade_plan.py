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
}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_config.json")


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    except (OSError, ValueError):
        pass
    return cfg


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
