#!/usr/bin/env python3
"""
Trend-join Long — Strategy Scanner
==================================
Takes the stocks produced by premarket_scanner.py and, after 10:00 AM New York time,
checks the Trend-join Long entry criteria on 1-minute bars (same logic as the
TradingView script tradingview/trend_join_long.pine):

  1. Price > yesterday's daily high
  2. Yesterday's close > daily 200 SMA
  3. Price > today's pre-market high (04:00–09:30 ET)
  4. Price > current high of day (closes at a new regular-session high)

The first bar at/after 10:00 ET where all four pass = VALID ENTRY signal.

Outputs (ready for automated reporting)
  signals/trend_join_long_YYYY-MM-DD.json   full machine-readable result
  signals/trend_join_long_YYYY-MM-DD.md     human-readable report
  watchlists/tradingview_YYYY-MM-DD.txt      watchlist to import into TradingView
  Optional: POST the report to WEBHOOK_URL (Slack / Discord / any JSON endpoint)

Usage
  python strategy_scanner.py                          # uses latest scans/premarket_scan_*.json
  python strategy_scanner.py --input scans/premarket_scan_2026-10-09.json
  python strategy_scanner.py --watchlist-only         # just build the TradingView watchlist
  python strategy_scanner.py --force                  # run even before 10:00 ET (testing)
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
PM_START, RTH_OPEN, RTH_CLOSE = dt.time(4, 0), dt.time(9, 30), dt.time(16, 0)
STRATEGY = "Trend-join Long"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
def latest_premarket_file(scan_dir: str) -> str | None:
    files = sorted(glob.glob(os.path.join(scan_dir, "premarket_scan_*.json")))
    return files[-1] if files else None


def load_candidates(path: str) -> tuple[dt.date, list[dict]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return dt.date.fromisoformat(data["scan_date"]), data.get("results", [])


def write_watchlist(tickers: list[str], day: dt.date, outdir: str) -> str:
    """TradingView: Watchlist menu → 'Import list…' accepts a comma-separated .txt."""
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"tradingview_{day.isoformat()}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(t.replace("-", ".") for t in tickers))
    return path


# --------------------------------------------------------------------------- #
# Core evaluation (pure function — easy to test)
# --------------------------------------------------------------------------- #
def _to_et(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = df.index.tz_convert(ET) if df.index.tz is not None else df.index.tz_localize(ET)
    return df


def _r(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 4)


def evaluate(ticker: str, daily: pd.DataFrame | None, minute: pd.DataFrame | None, today: dt.date,
             sma_len: int = 200, signal_start: dt.time = dt.time(10, 0)) -> dict:
    res = {"ticker": ticker, "status": "NO_DATA", "signal": False}
    if daily is None or minute is None or daily.empty or minute.empty:
        res["reason"] = "No price data"
        return res

    # Daily: yesterday's high / close and 200 SMA as of yesterday
    d = _to_et(daily.dropna(subset=["Close"]))
    d = d[d.index.date < today]
    if len(d) < sma_len:
        res["reason"] = f"Only {len(d)} daily bars (< {sma_len} needed for SMA)"
        return res
    y_high = float(d["High"].iloc[-1])
    y_close = float(d["Close"].iloc[-1])
    sma = float(d["Close"].rolling(sma_len).mean().iloc[-1])

    # Intraday
    m = _to_et(minute.dropna(subset=["Close"]))
    m = m[m.index.date == today]
    t = m.index.time
    pm = m[(t >= PM_START) & (t < RTH_OPEN)]
    rth = m[(t >= RTH_OPEN) & (t < RTH_CLOSE)].copy()
    pm_high = float(pm["High"].max()) if not pm.empty else float("nan")

    res.update({"yesterday_high": _r(y_high), "yesterday_close": _r(y_close),
                f"sma{sma_len}": _r(sma), "premarket_high": _r(pm_high)})

    if rth.empty:
        res.update(status="WAITING", reason="Market opens 09:30 ET; signals start 10:00 ET")
        return res

    rth["prior_hod"] = rth["High"].cummax().shift(1)          # high of day BEFORE each bar
    rth["c1"] = rth["Close"] > y_high
    c2 = y_close > sma
    rth["c3"] = rth["Close"] > pm_high if not math.isnan(pm_high) else False
    rth["c4"] = rth["Close"] > rth["prior_hod"]
    rth["all"] = rth["c1"] & c2 & rth["c3"] & rth["c4"]
    rth["window"] = rth.index.time >= signal_start

    last = rth.iloc[-1]
    checks_now = {
        "price_above_yesterday_high": bool(last["c1"]),
        "yesterday_close_above_sma": bool(c2),
        "price_above_premarket_high": bool(last["c3"]),
        "price_above_high_of_day": bool(last["c4"]),
    }
    res.update({
        "last_price": _r(last["Close"]),
        "last_bar_et": rth.index[-1].strftime("%H:%M"),
        "high_of_day": _r(rth["High"].max()),
        "checks_now": checks_now,
    })

    hits = rth[rth["all"] & rth["window"]]
    if not hits.empty:
        first = hits.iloc[0]
        res.update({
            "status": "VALID_ENTRY",
            "signal": True,
            "signal_time_et": hits.index[0].strftime("%Y-%m-%d %H:%M"),
            "entry_price": _r(first["Close"]),
            "hod_before_signal": _r(first["prior_hod"]),
        })
    else:
        failed = [k for k, v in checks_now.items() if not v]
        if not c2:
            res.update(status="FAIL", reason="Yesterday's close is below the 200 SMA (not in an uptrend)")
        elif not rth["window"].any():
            res.update(status="WAITING", reason="Before 10:00 ET signal window")
        else:
            res.update(status="NO_SIGNAL", reason="Failing now: " + ", ".join(failed) if failed
                       else "Criteria pass now but not yet on a closed bar after 10:00")
    return res


# --------------------------------------------------------------------------- #
# Data fetch (Yahoo Finance)
# --------------------------------------------------------------------------- #
def fetch(tickers: list[str]):
    import yf_utils
    daily = yf_utils.download(tickers, period="2y", interval="1d")
    minute = yf_utils.download(tickers, period="2d", interval="1m", prepost=True)
    return {t: (daily.get(t), minute.get(t)) for t in tickers}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def build_report(day: dt.date, results: list[dict], source: str, sma_len: int) -> dict:
    valid = [r for r in results if r["signal"]]
    return {
        "strategy": STRATEGY,
        "scan_date": day.isoformat(),
        "generated_at_et": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source_premarket_file": os.path.basename(source),
        "criteria": [
            "Price > yesterday's daily high",
            f"Yesterday's close > daily {sma_len} SMA",
            "Price > today's pre-market high",
            "Price > current high of day (new HOD)",
            "Signal only after 10:00 AM ET",
        ],
        "candidates": len(results),
        "valid_count": len(valid),
        "valid_entries": [
            {k: r.get(k) for k in ("ticker", "signal_time_et", "entry_price", "last_price", "gap_pct",
                                    "yesterday_high", "yesterday_close", f"sma{sma_len}", "premarket_high",
                                    "hod_before_signal", "catalyst")}
            for r in valid
        ],
        "evaluated": results,
    }


def to_markdown(rep: dict) -> str:
    lines = [f"**{rep['strategy']} — {rep['scan_date']}**",
             f"{rep['valid_count']} valid entr{'y' if rep['valid_count'] == 1 else 'ies'} "
             f"out of {rep['candidates']} pre-market gappers (generated {rep['generated_at_et']})", ""]
    if rep["valid_entries"]:
        lines += ["| Ticker | Signal (ET) | Entry | Gap % | Catalyst |", "|---|---|---|---|---|"]
        for v in rep["valid_entries"]:
            lines.append(f"| {v['ticker']} | {(v['signal_time_et'] or '')[-5:]} | ${v['entry_price']:.2f} | "
                         f"{v.get('gap_pct') or 0:+.2f}% | {v.get('catalyst') or ''} |")
    else:
        lines.append("_No valid entries yet._")
    others = [r for r in rep["evaluated"] if not r["signal"]]
    if others:
        lines += ["", "Not triggered:"]
        lines += [f"- {r['ticker']}: {r['status']} — {r.get('reason', '')}" for r in others]
    return "\n".join(lines)


def post_webhook(url: str, rep: dict, md: str) -> None:
    import requests
    body = {"text": md, "content": md[:1900], "report": rep}   # Slack uses text, Discord uses content
    r = requests.post(url, json=body, timeout=20)
    print(f"Webhook → HTTP {r.status_code}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run_strategy(src: str, outdir: str = "signals", sma_len: int = 200, fetcher=None) -> tuple[dict, str]:
    """Evaluate every ticker in a pre-market scan file; save JSON + Markdown. Returns (report, base_path)."""
    day, candidates = load_candidates(src)
    tickers = [c["ticker"] for c in candidates]
    results = []
    if tickers:
        data = (fetcher or fetch)(tickers)
        info = {c["ticker"]: c for c in candidates}
        for t in tickers:
            r = evaluate(t, *data.get(t, (None, None)), today=day, sma_len=sma_len)
            r["gap_pct"] = info[t].get("gap_pct")
            r["catalyst"] = info[t].get("catalyst")
            results.append(r)
    results.sort(key=lambda r: (not r["signal"], r.get("signal_time_et") or "", r["ticker"]))

    rep = build_report(day, results, src, sma_len)
    rep["scan_time_et"] = dt.datetime.now(ET).strftime("%H:%M")
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, f"trend_join_long_{day.isoformat()}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, ensure_ascii=False)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(to_markdown(rep) + "\n")
    return rep, base


def main() -> int:
    ap = argparse.ArgumentParser(description="Trend-join Long strategy scanner")
    ap.add_argument("--input", help="premarket_scan_YYYY-MM-DD.json (default: latest in --scan-dir)")
    ap.add_argument("--scan-dir", default="scans")
    ap.add_argument("--outdir", default="signals")
    ap.add_argument("--watchlist-dir", default="watchlists")
    ap.add_argument("--sma", type=int, default=200)
    ap.add_argument("--watchlist-only", action="store_true", help="Only write the TradingView watchlist")
    ap.add_argument("--force", action="store_true", help="Run even outside 10:00–16:00 ET or on an old scan")
    ap.add_argument("--only-et-hour", type=int, help="Exit quietly unless the current ET hour equals this "
                                                       "(lets one UTC cron work in both EDT and EST)")
    args = ap.parse_args()

    now = dt.datetime.now(ET)
    if args.only_et_hour is not None and now.hour != args.only_et_hour:
        print(f"Skipping: it is {now:%H:%M} ET, not hour {args.only_et_hour}.")
        return 0

    src = args.input or latest_premarket_file(args.scan_dir)
    if not src:
        print("No pre-market scan found — run premarket_scanner.py first.", file=sys.stderr)
        return 1
    day, candidates = load_candidates(src)
    tickers = [c["ticker"] for c in candidates]

    wl = write_watchlist(tickers, day, args.watchlist_dir)
    print(f"TradingView watchlist → {wl}")
    if args.watchlist_only:
        return 0

    if not args.force:
        if day != now.date():
            print(f"Pre-market scan is from {day}, not today ({now.date()}). Use --force to run anyway.", file=sys.stderr)
            return 1
        if not (dt.time(8, 0) <= now.time() < RTH_CLOSE):
            print(f"It is {now:%H:%M} ET — the strategy runs between 08:00 and 16:00 ET "
                  f"(signals only after 10:00). Use --force to override.", file=sys.stderr)
            return 1

    rep, base = run_strategy(src, args.outdir, args.sma)
    md = to_markdown(rep)
    print(md)
    print(f"\nSaved → {base}.json and {base}.md")
    if os.environ.get("WEBHOOK_URL"):
        try:
            post_webhook(os.environ["WEBHOOK_URL"], rep, md)
        except Exception as e:
            print(f"Webhook failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
