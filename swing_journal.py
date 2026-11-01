"""
Shadow test of a swing plan on the same Trend-join Long alerts (The Investmin's 4 steps), next to the
regular intraday plan in history/trades.csv — so the two can be compared on identical signals.

Per alert (settings: "swing" in trade_config.json):
  entry   the alert's entry price
  stop    lowest Low of the last 2 five-minute candles up to the signal (>= 0.5 % below entry)
  sell    1/3 at 3R, 1/3 at 5R; the last 1/3 exits on the first daily close below EMA9
          (EMA9 exit applies once the 3R partial is taken; before that only the stop or the time limit)
  time    still open after max_days trading days -> exit at that day's close
Replay
  signal day      1-minute bars after the signal (stop first when stop and target touch in the same minute)
  following days  daily bars: open below the stop -> filled at the open; otherwise stop first, then targets
                  (filled at the open on a gap above the target), then the EMA9 close and time checks
Rows live in history/swing_trades.csv (one per alert, updated every evening until closed).
Back-test of rules on Yahoo prices: no slippage or fees.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import trade_journal as TJ
import trade_plan as TP

ET = ZoneInfo("America/New_York")
BASE = os.path.dirname(os.path.abspath(__file__))
SWING_CSV = os.path.join(BASE, "history", "swing_trades.csv")
COLUMNS = ["date", "ticker", "signal_time_et", "entry", "stop", "risk_per_share", "stop_pct", "low_5m",
           "target_3r", "target_5r", "shares", "risk_usd", "status", "remaining", "legs", "realized_r",
           "exit_reason", "exit_date", "days_held", "last_checked", "last_close", "result_r", "pnl_usd",
           "open_r", "mfe_r", "note"]


def _num(x):
    try:
        x = float(x)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- csv
def read_rows(path: str = SWING_CSV) -> list[dict]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def write_rows(rows: list[dict], path: str = SWING_CSV) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.get("date", ""), r.get("signal_time_et") or "", r.get("ticker", "")))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


# ----------------------------------------------------------------------------- state helpers
def _legs(r: dict) -> list[str]:
    return [x for x in str(r.get("legs") or "").split(";") if x]


def _sell(r: dict, frac: float, price: float, label: str, when: str) -> None:
    """Sell `frac` of the original position at `price`; R is added to realized_r."""
    frac = min(frac, float(r["remaining"]))
    if frac <= 1e-9:
        return
    risk = float(r["risk_per_share"])
    r["realized_r"] = round(float(r.get("realized_r") or 0) + frac * (price - float(r["entry"])) / risk, 4)
    r["remaining"] = round(float(r["remaining"]) - frac, 4)
    r["legs"] = ";".join(_legs(r) + [f"{label}@{price:.4g}:{when}"])


def _close_out(r: dict, price: float, reason: str, day: str) -> None:
    _sell(r, float(r["remaining"]), price, reason, day)
    r.update(status="closed", exit_reason=reason, exit_date=day, remaining=0.0)
    r["result_r"] = round(float(r["realized_r"]), 2)
    r["pnl_usd"] = round(float(r["result_r"]) * float(r["risk_per_share"]) * float(r["shares"] or 0), 2)
    r["open_r"] = None


def _partial_targets(r: dict, cfg: dict) -> list[tuple[str, float, float]]:
    risk, entry = float(r["risk_per_share"]), float(r["entry"])
    done = {x.split("@")[0] for x in _legs(r)}
    return [(f"{n:g}R", entry + n * risk, f) for n, f in cfg["swing"]["partials"] if f"{n:g}R" not in done]


def _partial_taken(r: dict) -> bool:
    return any(x.split("@")[0].endswith("R") for x in _legs(r))


def _mark(r: dict, close: float, high: float | None = None) -> None:
    risk, entry = float(r["risk_per_share"]), float(r["entry"])
    r["last_close"] = round(close, 4)
    if r["status"] == "open":
        r["open_r"] = round(float(r["realized_r"] or 0) + float(r["remaining"]) * (close - entry) / risk, 2)
    if high is not None:
        prev = _num(r.get("mfe_r"))
        r["mfe_r"] = round(max(prev if prev is not None else -99.0, (high - entry) / risk), 2)


# ----------------------------------------------------------------------------- replay
def open_trade(sig: dict, minute, cfg: dict) -> dict:
    """New row from a frozen alert + that day's 1-minute bars (replays the signal day)."""
    sc = cfg["swing"]
    low = _num(sig.get("low_5m"))
    if low is None:
        low = TP.low_before_signal(minute, sig.get("signal_time_et"), sc["stop_candles"], sc["candle_minutes"])
    r = {"date": sig["date"], "ticker": sig["ticker"], "signal_time_et": sig.get("signal_time_et"), "status": "skip"}
    plan = TP.make_swing_plan({"entry": sig.get("entry"), "low_5m": low}, cfg)
    if not plan:
        r["note"] = "no 1-minute data around the signal (stop not computable)"
        return r
    r.update(entry=plan["entry"], stop=plan["stop"], risk_per_share=plan["risk_per_share"], stop_pct=plan["stop_pct"],
             low_5m=plan["low_5m"], target_3r=plan["targets"].get("3R"), target_5r=plan["targets"].get("5R"),
             shares=plan["shares"], risk_usd=plan["risk_usd"], status="open", remaining=1.0, legs="",
             realized_r=0.0, days_held=0, last_checked=sig["date"])
    if minute is None or getattr(minute, "empty", True):
        r.update(status="skip", note="no 1-minute bars for the signal day (retried next evening)")
        return r
    m = minute.copy()
    m.index = m.index.tz_convert(ET) if m.index.tz is not None else m.index.tz_localize(ET)
    day = dt.date.fromisoformat(sig["date"])
    m = m[(m.index.date == day) & (m.index.time >= TJ.RTH_OPEN) & (m.index.time < TJ.RTH_CLOSE)].dropna(subset=["Close"])
    sig_t = dt.datetime.strptime(sig["signal_time_et"], "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    after = m[m.index > sig_t]
    stop = float(r["stop"])
    for ts, bar in after.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        if lo <= stop:
            _close_out(r, stop, "stop", f"{sig['date']} {ts:%H:%M}")
            break
        for label, px, frac in _partial_targets(r, cfg):
            if hi >= px:
                _sell(r, frac, px, label, f"{sig['date']} {ts:%H:%M}")
    if r["status"] == "closed":
        r["exit_date"] = sig["date"]
    if len(after):
        _mark(r, float(after["Close"].iloc[-1]), float(after["High"].max()))
    return r


def step_day(r: dict, d: str, o: float, h: float, l: float, c: float, ema: float | None, cfg: dict) -> None:
    """Apply one later daily bar to an open row."""
    if r["status"] != "open":
        return
    stop = float(r["stop"])
    r["days_held"] = int(r.get("days_held") or 0) + 1
    if o <= stop:
        _close_out(r, o, "stop (gap)", d)
    elif l <= stop:
        _close_out(r, stop, "stop", d)
    else:
        for label, px, frac in _partial_targets(r, cfg):
            if o >= px:
                _sell(r, frac, o, label, d)
            elif h >= px:
                _sell(r, frac, px, label, d)
        if r["status"] == "open" and float(r["remaining"]) > 1e-9:
            if _partial_taken(r) and ema is not None and c < ema:
                _close_out(r, c, f"close < EMA{cfg['swing']['trail_ema']}", d)
            elif r["days_held"] >= cfg["swing"]["max_days"]:
                _close_out(r, c, "time", d)
    r["last_checked"] = d
    _mark(r, c, h)


def advance(r: dict, daily, cfg: dict, through: dt.date) -> None:
    """Feed every daily bar after last_checked up to `through` (catches up after missed evenings)."""
    if r.get("status") != "open" or daily is None or getattr(daily, "empty", True):
        return
    d = daily.dropna(subset=["Close"]).copy()
    idx = d.index.tz_convert(ET) if getattr(d.index, "tz", None) is not None else d.index
    d.index = [x.date() for x in idx]
    ema = d["Close"].ewm(span=cfg["swing"]["trail_ema"], adjust=False).mean()
    last = dt.date.fromisoformat(str(r["last_checked"])[:10])
    for day in d.index:
        if last < day <= through:
            b = d.loc[day]
            step_day(r, day.isoformat(), float(b["Open"]), float(b["High"]), float(b["Low"]), float(b["Close"]),
                     float(ema.loc[day]), cfg)
            if r["status"] != "open":
                break


# ----------------------------------------------------------------------------- daily sync
def sync(day: dt.date, fetch_minutes_for_day, fetch_daily, cfg: dict | None = None,
         signals_dir: str = TJ.SIGNALS_DIR, path: str = SWING_CSV) -> dict:
    """Open rows for every alert since swing.start_date that has none yet (back-fill), then advance all open
    rows through `day`. fetch_minutes_for_day(tickers, date) / fetch_daily(tickers) -> {ticker: DataFrame}.
    Returns {"new": [...], "events": [...], "open": n}."""
    cfg = cfg or TP.load_config()
    sc = cfg["swing"]
    start = sc.get("start_date") or "0000"
    retry_from = (day - dt.timedelta(days=28)).isoformat()          # Yahoo keeps 1-minute bars ~30 days
    rows = [r for r in read_rows(path) if not (r.get("status") == "skip" and r.get("date", "") >= retry_from)]
    have = {(r["date"], r["ticker"]) for r in rows}
    new = []
    for f in sorted(glob.glob(os.path.join(signals_dir, "*.jsonl"))):
        d = os.path.basename(f)[:10]
        if d < start or d > day.isoformat():
            continue
        sigs = [s for s in TJ.load_signals(dt.date.fromisoformat(d), signals_dir) if (d, s["ticker"]) not in have]
        if not sigs:
            continue
        mins = fetch_minutes_for_day([s["ticker"] for s in sigs], dt.date.fromisoformat(d))
        for s in sigs:
            r = open_trade(s, mins.get(s["ticker"]), cfg)
            rows.append(r)
            new.append(r)
    before = {(r["date"], r["ticker"]): (r.get("status"), r.get("legs")) for r in rows}
    open_rows = [r for r in rows if r.get("status") == "open"]
    if open_rows:
        daily = fetch_daily(sorted({r["ticker"] for r in open_rows}))
        for r in open_rows:
            advance(r, daily.get(r["ticker"]), cfg, day)
    events = [r for r in rows if r not in new and before[(r["date"], r["ticker"])] != (r.get("status"), r.get("legs"))]
    write_rows(rows, path)
    return {"new": new, "events": events, "open": sum(1 for r in rows if r.get("status") == "open")}


# ----------------------------------------------------------------------------- comparison
def compare(path: str = SWING_CSV, trades_csv: str = TJ.TRADES_CSV) -> dict:
    """Both plans on the same alerts: closed swing trades vs the intraday plan's results for those alerts."""
    rows = [r for r in read_rows(path) if r.get("status") in ("open", "closed")]
    closed = [r for r in rows if r["status"] == "closed"]
    opened = [r for r in rows if r["status"] == "open"]
    keys = {(r["date"], r["ticker"]) for r in rows}
    intra = [r for r in TJ._read_trades(trades_csv) if (r.get("date"), r.get("ticker")) in keys
             and _num(r.get("result_r")) is not None]

    def block(vals):
        if not vals:
            return {"n": 0}
        return {"n": len(vals), "win": sum(v > 0 for v in vals) / len(vals), "avg": sum(vals) / len(vals),
                "total": sum(vals)}
    return {"swing_closed": block([float(r["result_r"]) for r in closed]),
            "swing_open": {"n": len(opened), "open_r": sum(_num(r.get("open_r")) or 0 for r in opened)},
            "swing_all_marked": block([float(r["result_r"]) if r["status"] == "closed" else (_num(r.get("open_r")) or 0)
                                       for r in rows]),
            "intraday_same": block([float(r["result_r"]) for r in intra]),
            "since": min((r["date"] for r in rows), default=None)}


# ----------------------------------------------------------------------------- data (Yahoo, raw prices)
def yahoo_minutes_for_day(tickers: list[str], day: dt.date) -> dict:
    import yf_utils
    return yf_utils.download(tickers, start=day.isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                             interval="1m", prepost=False)


def yahoo_daily(tickers: list[str]) -> dict:
    import yf_utils
    return yf_utils.download(tickers, period="6mo", interval="1d")
