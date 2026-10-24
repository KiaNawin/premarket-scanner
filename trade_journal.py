"""
Trade journal for Trend-join Long alerts.

1. When an alert is sent, its trade plan is frozen in history/signals/YYYY-MM-DD.jsonl
   (one JSON line per alert, so concurrent jobs merge cleanly in git).
2. After the close, score_day() replays 1-minute bars from the signal to 16:00 ET and records
   what a mechanical plan would have done:
      exit at the stop (−1R), at the exit target (default +2R), or at the 16:00 close.
   If the stop and the target are both touched inside the same minute, the stop is assumed first
   (conservative). Results go to history/trades.csv (open it in Excel).
3. stats() summarises every trade in trades.csv: win rate, average R, total R, profit factor,
   max drawdown — the numbers that show whether the strategy really works.
Results are a back-test of the alert rules, not live fills: slippage and commissions are ignored.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_DIR = os.path.join(BASE, "history", "signals")
TRADES_CSV = os.path.join(BASE, "history", "trades.csv")
RTH_OPEN, RTH_CLOSE = dt.time(9, 30), dt.time(16, 0)

COLUMNS = ["date", "ticker", "signal_time_et", "alerted_at_et", "entry", "stop", "risk_per_share", "stop_pct",
           "target_1r", "target_2r", "exit_target_r", "shares", "position_usd", "risk_usd", "breakout_level",
           "breakout_name", "atr14", "gap_pct", "catalyst", "exit_reason", "exit_time_et", "exit_price",
           "result_r", "pnl_usd", "hit_1r", "mfe_r", "mae_r", "close_price"]


# ----------------------------------------------------------------------------- recording
def record_alerts(day: dt.date, entries: list[dict], alerted_at: str, signals_dir: str = SIGNALS_DIR) -> None:
    os.makedirs(signals_dir, exist_ok=True)
    with open(os.path.join(signals_dir, f"{day.isoformat()}.jsonl"), "a", encoding="utf-8") as f:
        for r in entries:
            p = r.get("plan") or {}
            row = {
                "date": day.isoformat(), "ticker": r["ticker"], "signal_time_et": r.get("signal_time_et"),
                "alerted_at_et": alerted_at, "gap_pct": r.get("gap_pct"), "catalyst": r.get("catalyst"),
                **{k: p.get(k) for k in ("entry", "stop", "risk_per_share", "stop_pct", "exit_target_r", "shares",
                                         "position_usd", "risk_usd", "breakout_level", "breakout_name", "atr14")},
                "target_1r": (p.get("targets") or {}).get("1R"), "target_2r": (p.get("targets") or {}).get("2R"),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_signals(day: dt.date, signals_dir: str = SIGNALS_DIR) -> list[dict]:
    path = os.path.join(signals_dir, f"{day.isoformat()}.jsonl")
    rows, seen = [], set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row["ticker"] in seen:          # first alert of the day wins
                    continue
                seen.add(row["ticker"])
                rows.append(row)
    except OSError:
        pass
    return rows


# ----------------------------------------------------------------------------- scoring
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def simulate(row: dict, minute) -> dict:
    """Replay 1-minute bars after the signal bar. minute: DataFrame with High/Low/Close, ET or tz index."""
    out = {"exit_reason": "no data", "exit_time_et": None, "exit_price": None, "result_r": None, "pnl_usd": None,
           "hit_1r": None, "mfe_r": None, "mae_r": None, "close_price": None}
    entry, stop, risk = _num(row.get("entry")), _num(row.get("stop")), _num(row.get("risk_per_share"))
    if minute is None or getattr(minute, "empty", True) or not entry or not stop or not risk or risk <= 0:
        return out
    m = minute.copy()
    m.index = m.index.tz_convert(ET) if m.index.tz is not None else m.index.tz_localize(ET)
    day = dt.date.fromisoformat(row["date"])
    m = m[(m.index.date == day) & (m.index.time >= RTH_OPEN) & (m.index.time < RTH_CLOSE)].dropna(subset=["Close"])
    try:
        sig = dt.datetime.strptime(row["signal_time_et"], "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    except (TypeError, ValueError, KeyError):
        return out
    after = m[m.index > sig]
    if after.empty:
        out.update(exit_reason="no bars after signal", close_price=float(m["Close"].iloc[-1]) if len(m) else None)
        return out

    tgt_r = _num(row.get("exit_target_r")) or 2.0
    target, one_r = entry + tgt_r * risk, entry + risk
    exit_reason = exit_time = exit_price = None
    hit_1r = False
    for ts, bar in after.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        if lo <= stop:                                   # stop first when both touch (conservative)
            exit_reason, exit_time, exit_price = "stop", ts, stop
            break
        if hi >= one_r:
            hit_1r = True
        if hi >= target:
            exit_reason, exit_time, exit_price = f"target {tgt_r:g}R", ts, target
            break
    close_price = float(after["Close"].iloc[-1])
    if exit_reason is None:
        exit_reason, exit_time, exit_price = "close", after.index[-1], close_price
    held = after[after.index <= exit_time]
    res_r = (exit_price - entry) / risk
    shares = _num(row.get("shares")) or 0
    out.update(
        exit_reason=exit_reason, exit_time_et=exit_time.strftime("%H:%M"), exit_price=round(exit_price, 4),
        result_r=round(res_r, 2), pnl_usd=round(res_r * risk * shares, 2), hit_1r=hit_1r or res_r >= 1,
        mfe_r=round((float(after["High"].max()) - entry) / risk, 2),
        mae_r=round((float(held["Low"].min()) - entry) / risk, 2), close_price=round(close_price, 4),
    )
    return out


def _read_trades(path: str = TRADES_CSV) -> list[dict]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _write_trades(rows: list[dict], path: str = TRADES_CSV) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.get("date", ""), r.get("signal_time_et") or "", r.get("ticker", "")))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:     # utf-8-sig so Excel shows Thai/emoji correctly
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


def score_day(day: dt.date, fetch_minutes, signals_dir: str = SIGNALS_DIR, trades_csv: str = TRADES_CSV) -> list[dict]:
    """Score today's alerts and (re)write their rows in trades.csv. fetch_minutes(tickers) -> {t: DataFrame}."""
    sigs = load_signals(day, signals_dir)
    data = fetch_minutes([s["ticker"] for s in sigs]) if sigs else {}
    scored = [{**s, **simulate(s, data.get(s["ticker"]))} for s in sigs]
    others = [r for r in _read_trades(trades_csv) if r.get("date") != day.isoformat()]   # re-run safe
    _write_trades(others + scored, trades_csv)
    return scored


# ----------------------------------------------------------------------------- statistics
def stats(trades_csv: str = TRADES_CSV, last_n: int = 20) -> dict:
    rows = [r for r in _read_trades(trades_csv) if _num(r.get("result_r")) is not None]
    rs = [float(r["result_r"]) for r in rows]
    if not rs:
        return {"trades": 0}

    def block(vals):
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v <= 0]
        neg = -sum(v for v in vals if v < 0)
        return {"trades": len(vals), "win_rate": len(wins) / len(vals), "avg_r": sum(vals) / len(vals),
                "total_r": sum(vals), "avg_win_r": sum(wins) / len(wins) if wins else 0.0,
                "avg_loss_r": sum(losses) / len(losses) if losses else 0.0,
                "profit_factor": (sum(v for v in vals if v > 0) / neg) if neg else None}

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for v in rs:
        equity += v
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    out = block(rs)
    out.update(since=rows[0]["date"], total_pnl=sum(_num(r.get("pnl_usd")) or 0 for r in rows), max_dd_r=max_dd,
               best=max(rows, key=lambda r: float(r["result_r"])), worst=min(rows, key=lambda r: float(r["result_r"])),
               last=block(rs[-last_n:]), last_n=min(last_n, len(rs)),
               exits={k: sum(1 for r in rows if r["exit_reason"].startswith(k)) for k in ("target", "stop", "close")})
    return out
