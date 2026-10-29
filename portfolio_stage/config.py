from __future__ import annotations

import copy
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "benchmark": "SPY",
    "paths": {"trades": "portfolio/trades.csv", "meta": "portfolio/meta.csv", "output": "output",
              "cache": "cache", "slips": "slips"},
    "groups": ["semi", "tech", "cyber", "power", "space", "health", "fin", "def"],
    "qualities": ["A", "B", "C", "EC", "ET"],
    "etf_qualities": ["EC", "ET"],
    "stage": {"sma_weeks": 30, "sma_short_weeks": 10, "slope_lookback_weeks": 4, "slope_t": 1.0,
              "confirm_weeks": 2, "min_sma_weeks": 10, "warn_stage_days": 100, "warn_slope_down_weeks": 3},
    "rs": {"mansfield_weeks": 52, "mansfield_min_weeks": 26, "ret_3m_days": 63, "ret_6m_days": 126},
    "minervini": {"low_mult": 1.30, "high_mult": 0.75, "sma200_trend_days": 22},
    "levels": {"swing_lookback_days": 120, "pivot_bars": 5, "atr_period": 14, "rsi_period": 14,
               "stop_atr_mult": 1.0, "new_high_target_atr_mult": 3.0, "near_level_pct": 3.0},
    "alerts": {"gap_pct": 5.0, "max_weight_pct": 5.0, "max_quality_c_pct": 7.0, "earnings_days": 5},
    "data": {"history_period": "2y", "retries": 3, "backoff_seconds": 2, "fundamentals_refresh_days": 7,
             "stale_days": 5},
    "ingest": {"model": "claude-sonnet-5-5", "slip_timezone": "Asia/Bangkok", "amount_tolerance_pct": 1.0,
               "check_price_vs_day_range": True, "day_range_tolerance_pct": 2.0},
    "reconcile": {"tolerance_pct": 2.0},
    "telegram": {"silent": False, "send_when_no_alerts": True},
    "events": {"earnings_ahead_days": 7, "news_lookback_days": 7, "max_news_per_ticker": 2, "move_pct": 5.0,
               "include_etfs": False,
               "big_news_keywords": ["earnings", "results", "guidance", "outlook", "FDA", "acquir", "merger",
                                     "offering", "lawsuit", "downgrad", "upgrad", "CEO", "contract", "buyback"],
               "exclude_keywords": ["stocks? to (buy|watch|own)", "should you (buy|sell)"]},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | None = None) -> dict:
    path = path or os.path.join(ROOT, "config.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except FileNotFoundError:
        user = {}
    cfg = _merge(DEFAULTS, user)
    cfg["paths"] = {k: v if os.path.isabs(v) else os.path.join(ROOT, v) for k, v in cfg["paths"].items()}
    return cfg


def load_dotenv(path: str | None = None) -> None:
    """KEY=VALUE lines from .env in the repo root (real environment variables win)."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):
                os.environ[k] = v
