#!/usr/bin/env python3
"""
Pre-market Gap Scanner
======================
Scans US-listed stocks before the opening bell for:
  * gap up  > 5%   vs. previous regular-session close
  * price   > $3   (latest pre-market trade)
  * pre-market volume > 50,000 shares

Data sources
  * Prices / % change / pre-market volume : Yahoo Finance (via yfinance, 1-minute bars incl. pre-market)
  * News catalyst                         : Benzinga (News API if BENZINGA_API_KEY is set,
                                            otherwise Benzinga stories surfaced in Yahoo's news feed)

Output
  * Printed table: Ticker | Price | Gap % | Catalyst
  * JSON file:     <outdir>/premarket_scan_YYYY-MM-DD.json   (one new file per trading day)

Usage
  python premarket_scanner.py                    # full scan of NASDAQ/NYSE/AMEX common stocks
  python premarket_scanner.py --tickers AAPL,TSLA,PLTR   # scan only these
  python premarket_scanner.py --min-gap 8 --min-volume 100000 --outdir ./scans
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

import yf_utils

ET = ZoneInfo("America/New_York")
PREMARKET_START = dt.time(4, 0)
REGULAR_OPEN = dt.time(9, 30)
REGULAR_CLOSE = dt.time(16, 0)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (premarket-scanner)"}


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
def load_universe() -> list[str]:
    """All non-ETF, non-test common listings on NASDAQ, NYSE, NYSE American, etc."""
    symbols: set[str] = set()

    def read(url: str) -> pd.DataFrame:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        r.raise_for_status()
        lines = [ln for ln in r.text.splitlines() if not ln.startswith("File Creation Time")]
        return pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str)

    nas = read(NASDAQ_LISTED_URL)
    nas = nas[(nas["Test Issue"] == "N") & (nas["ETF"] == "N")]
    symbols.update(nas["Symbol"].dropna())

    oth = read(OTHER_LISTED_URL)
    oth = oth[(oth["Test Issue"] == "N") & (oth["ETF"] == "N")]
    symbols.update(oth["ACT Symbol"].dropna())

    clean = []
    for s in symbols:
        s = s.strip().upper()
        if not s or any(c in s for c in "$=^ "):  # preferreds, when-issued, etc.
            continue
        clean.append(s.replace(".", "-"))  # Yahoo uses BRK-B, not BRK.B
    return sorted(clean)


# --------------------------------------------------------------------------- #
# Yahoo Finance: gap %, price, pre-market volume
# --------------------------------------------------------------------------- #
def _frame_for(data: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame | None:
    if data is None or data.empty:
        return None
    if single and not isinstance(data.columns, pd.MultiIndex):
        return data
    try:
        return data[ticker]
    except KeyError:
        return None


def compute_premarket_stats(
    ticker: str, daily: pd.DataFrame | None, minute: pd.DataFrame | None, today: dt.date
) -> dict | None:
    """Return {'ticker','price','prev_close','gap_pct','premarket_volume','session'} or None.

    price / gap_pct use the latest trade today (pre-market before 09:30, regular session after),
    so the scan stays meaningful when it re-runs during the trading day.
    premarket_volume is always the 04:00–09:30 ET volume.
    """
    if daily is None or minute is None or daily.empty or minute.empty:
        return None

    # Previous regular-session close = last daily close dated before today (ET)
    d = daily.dropna(subset=["Close"]).copy()
    d_idx = d.index.tz_convert(ET) if d.index.tz is not None else d.index.tz_localize(ET)
    d = d[d_idx.date < today]
    if d.empty:
        return None
    prev_close = float(d["Close"].iloc[-1])

    # Today's bars: pre-market (04:00–09:29) and regular session (09:30–15:59) ET
    m = minute.copy()
    m.index = m.index.tz_convert(ET) if m.index.tz is not None else m.index.tz_localize(ET)
    t = m.index.time
    day = m[(m.index.date == today) & (t >= PREMARKET_START) & (t < REGULAR_CLOSE)]
    pm = day[day.index.time < REGULAR_OPEN]
    if pm.empty or prev_close <= 0:
        return None
    closes = day["Close"].dropna()
    if closes.empty:
        return None

    price = float(closes.iloc[-1])
    session = "regular" if closes.index[-1].time() >= REGULAR_OPEN else "pre-market"
    volume = int(pm["Volume"].fillna(0).sum())
    gap_pct = (price / prev_close - 1.0) * 100.0
    return {
        "ticker": ticker,
        "price": round(price, 2),
        "prev_close": round(prev_close, 2),
        "gap_pct": round(gap_pct, 2),
        "premarket_volume": volume,
        "session": session,
    }


def scan_yahoo(tickers: list[str], today: dt.date, batch_size: int = 200, pause: float = 1.0) -> list[dict]:
    rows: list[dict] = []
    total = len(tickers)
    for i in range(0, total, batch_size):
        batch = tickers[i : i + batch_size]
        print(f"  Yahoo batch {i // batch_size + 1}/{(total - 1) // batch_size + 1} ({len(batch)} symbols)", file=sys.stderr)
        daily = yf_utils.download(batch, period="5d", interval="1d")
        minute = yf_utils.download(batch, period="2d", interval="1m", prepost=True)
        for t in batch:
            stats = compute_premarket_stats(t, daily.get(t), minute.get(t), today)
            if stats:
                rows.append(stats)
        time.sleep(pause)
    return rows


# --------------------------------------------------------------------------- #
# Fast pre-filter (Yahoo batch quotes) — narrows ~7,000 symbols to the few that
# are actually up, so only those need 1-minute bars. Deliberately generous:
# the exact gap / price / pre-market volume checks still run on 1-minute bars.
# --------------------------------------------------------------------------- #
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QUOTE_FIELDS = ("symbol,marketState,regularMarketPrice,regularMarketPreviousClose,regularMarketChangePercent,"
                "regularMarketTime,preMarketPrice,preMarketChangePercent,preMarketTime")
QUOTE_BATCH = 150
PREFILTER_MARGIN = 1.0          # keep anything up ≥ (min_gap - 1)% to be safe
PREFILTER_MIN_UNIVERSE = 300    # small ticker lists are scanned directly
PREFILTER_MIN_COVERAGE = 0.5    # need quotes for ≥50% of symbols, else fall back


def _quote_candidate(q: dict, gap_floor: float, price_floor: float, today: dt.date) -> bool:
    """Generous check: is this quote plausibly up ≥ gap_floor% today?"""
    def fresh(ts) -> bool:
        try:
            return dt.datetime.fromtimestamp(int(ts), ET).date() == today
        except (TypeError, ValueError, OSError):
            return False

    prices, changes = [], []
    if q.get("preMarketPrice") and fresh(q.get("preMarketTime")):
        prices.append(q["preMarketPrice"])
        if q.get("preMarketChangePercent") is not None:
            changes.append(q["preMarketChangePercent"])
    if q.get("regularMarketPrice") and fresh(q.get("regularMarketTime")):
        prices.append(q["regularMarketPrice"])
        if q.get("regularMarketChangePercent") is not None:
            changes.append(q["regularMarketChangePercent"])
    if not prices:
        return False
    latest = max(prices)
    if latest < price_floor:
        return False
    # reference = smallest plausible "yesterday close" (avoids missing a gap when Yahoo's
    # previous-close field hasn't rolled over yet in pre-market)
    refs = [x for x in (q.get("regularMarketPreviousClose"), q.get("regularMarketPrice")) if x]
    if any(c >= gap_floor for c in changes):
        return True
    return any(latest / r - 1 >= gap_floor / 100 for r in refs if r > 0)


def prefilter_quotes(universe: list[str], min_gap: float, min_price: float, today: dt.date) -> list[str] | None:
    """Return candidate tickers, or None if the quote endpoint isn't usable (→ full scan)."""
    try:
        from yfinance.data import YfData
        yf_utils._setup_cache()
        data = YfData()
    except Exception as e:
        print(f"  pre-filter init failed: {e}", file=sys.stderr)
        return None

    gap_floor, price_floor = min_gap - PREFILTER_MARGIN, min_price * 0.9
    quoted, cands, failures = 0, [], 0
    for i in range(0, len(universe), QUOTE_BATCH):
        batch = universe[i : i + QUOTE_BATCH]
        result = None
        for attempt in range(3):
            try:
                js = data.get_raw_json(QUOTE_URL, params={"symbols": ",".join(batch), "fields": QUOTE_FIELDS,
                                                          "formatted": "false", "lang": "en-US", "region": "US"})
                result = (js.get("quoteResponse") or {}).get("result") or []
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  quote batch {i // QUOTE_BATCH + 1} failed: {e}", file=sys.stderr)
                time.sleep(1 + attempt)
        if result is None:
            failures += 1
            if failures >= 5 and quoted == 0:      # endpoint clearly not working
                return None
            continue
        quoted += len(result)
        cands += [q["symbol"] for q in result if q.get("symbol") and _quote_candidate(q, gap_floor, price_floor, today)]
        time.sleep(0.2)

    if quoted < PREFILTER_MIN_COVERAGE * len(universe):
        print(f"  pre-filter got quotes for only {quoted}/{len(universe)} symbols", file=sys.stderr)
        return None
    return sorted(set(cands))


def apply_filters(rows: list[dict], min_gap: float, min_price: float, min_volume: int) -> list[dict]:
    hits = [
        r for r in rows
        if r["gap_pct"] > min_gap and r["price"] > min_price and r["premarket_volume"] > min_volume
    ]
    return sorted(hits, key=lambda r: r["gap_pct"], reverse=True)


# --------------------------------------------------------------------------- #
# Benzinga: one-sentence news catalyst
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def one_sentence(text: str, max_len: int = 220) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    first = _SENT_RE.split(text, maxsplit=1)[0].strip()
    if len(first) > max_len:
        first = first[: max_len - 1].rsplit(" ", 1)[0] + "…"
    if first[-1] not in ".!?…":
        first += "."
    return first


def benzinga_api_catalyst(ticker: str, api_key: str, today: dt.date) -> dict | None:
    params = {
        "token": api_key,
        "tickers": ticker,
        "pageSize": 5,
        "displayOutput": "abstract",
        "dateFrom": (today - dt.timedelta(days=3)).isoformat(),
        "sort": "created:desc",
    }
    r = requests.get(BENZINGA_NEWS_URL, params=params, headers={**HTTP_HEADERS, "accept": "application/json"}, timeout=20)
    r.raise_for_status()
    items = r.json() or []
    for it in items:
        sentence = one_sentence(it.get("teaser") or "") or one_sentence(it.get("title") or "")
        if sentence:
            return {"catalyst": sentence, "catalyst_source": "Benzinga", "catalyst_url": it.get("url"),
                    "catalyst_headline": it.get("title")}
    return None


def yahoo_feed_catalyst(ticker: str) -> dict | None:
    """Prefer Benzinga stories in Yahoo's ticker news feed; fall back to latest headline."""
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        return None
    parsed = []
    for n in news:
        c = n.get("content", n)  # yfinance >=0.2.50 nests under 'content'
        provider = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
        url = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")
        parsed.append({
            "provider": provider or "",
            "title": c.get("title") or "",
            "summary": c.get("summary") or "",
            "url": url,
        })
    if not parsed:
        return None
    bz = [p for p in parsed if "benzinga" in p["provider"].lower()]
    pick = (bz or parsed)[0]
    sentence = one_sentence(pick["summary"]) or one_sentence(pick["title"])
    if not sentence:
        return None
    return {"catalyst": sentence, "catalyst_source": pick["provider"] or "Yahoo Finance",
            "catalyst_url": pick["url"], "catalyst_headline": pick["title"]}


def get_catalyst(ticker: str, today: dt.date, api_key: str | None) -> dict:
    if api_key:
        try:
            res = benzinga_api_catalyst(ticker, api_key, today)
            if res:
                return res
        except Exception as e:
            print(f"    Benzinga API error for {ticker}: {e}", file=sys.stderr)
    res = yahoo_feed_catalyst(ticker)
    if res:
        return res
    return {"catalyst": "No recent news catalyst found.", "catalyst_source": None,
            "catalyst_url": None, "catalyst_headline": None}


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_table(results: list[dict]) -> None:
    if not results:
        print("No stocks matched the criteria.")
        return
    print(f"\n{'Ticker':<8}{'Price':>10}{'Gap %':>9}   Catalyst")
    print("-" * 100)
    for r in results:
        print(f"{r['ticker']:<8}{'$' + format(r['price'], ',.2f'):>10}{r['gap_pct']:>+8.2f}%   {r['catalyst']}")
    print()


def save_json(results: list[dict], today: dt.date, outdir: str, criteria: dict, scanned: int) -> tuple[str, dict]:
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"premarket_scan_{today.isoformat()}.json")
    now = dt.datetime.now(ET)
    payload = {
        "scan_date": today.isoformat(),
        "generated_at_et": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "scan_time_et": now.strftime("%H:%M"),
        "criteria": criteria,
        "symbols_scanned": scanned,
        "count": len(results),
        "results": [
            {
                "ticker": r["ticker"],
                "price": r["price"],
                "gap_pct": r["gap_pct"],
                "catalyst": r["catalyst"],
                "prev_close": r["prev_close"],
                "premarket_volume": r["premarket_volume"],
                "session": r.get("session"),
                "catalyst_source": r.get("catalyst_source"),
                "catalyst_headline": r.get("catalyst_headline"),
                "catalyst_url": r.get("catalyst_url"),
            }
            for r in results
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path, payload


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run_scan(tickers: list[str] | None = None, min_gap: float = 5.0, min_price: float = 3.0,
             min_volume: int = 50_000, outdir: str = "scans", batch_size: int = 200) -> tuple[str, dict]:
    """Run one full scan and save the day's JSON. Returns (path, payload)."""
    today = dt.datetime.now(ET).date()
    if tickers:
        universe = [t.strip().upper().replace(".", "-") for t in tickers if t.strip()]
    else:
        print("Loading US stock universe…", file=sys.stderr)
        universe = load_universe()
    to_scan = universe
    mode = "full"
    if len(universe) > PREFILTER_MIN_UNIVERSE:
        t0 = time.time()
        cands = prefilter_quotes(universe, min_gap, min_price, today)
        if cands is not None:
            to_scan, mode = cands, "fast"
            print(f"Fast pre-filter: {len(cands)} of {len(universe)} symbols are up ≥{min_gap - PREFILTER_MARGIN:g}% "
                  f"({time.time() - t0:.0f}s)", file=sys.stderr)
        else:
            print("Fast pre-filter unavailable — falling back to full scan.", file=sys.stderr)
    print(f"Scanning {len(to_scan)} symbols from Yahoo Finance ({mode} mode)…", file=sys.stderr)

    rows = scan_yahoo(to_scan, today, batch_size=batch_size) if to_scan else []
    hits = apply_filters(rows, min_gap, min_price, min_volume)
    print(f"{len(hits)} stocks passed the filters. Fetching Benzinga catalysts…", file=sys.stderr)

    api_key = os.environ.get("BENZINGA_API_KEY")
    for r in hits:
        r.update(get_catalyst(r["ticker"], today, api_key))

    print_table(hits)
    criteria = {"min_gap_pct": min_gap, "min_price": min_price, "min_premarket_volume": min_volume}
    path, payload = save_json(hits, today, outdir, criteria, scanned=len(universe))
    print(f"Saved → {path}")
    return path, payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-market gap scanner (Yahoo Finance + Benzinga)")
    ap.add_argument("--tickers", help="Comma-separated list to scan instead of the full US universe")
    ap.add_argument("--min-gap", type=float, default=5.0, help="Minimum gap up %% (default 5)")
    ap.add_argument("--min-price", type=float, default=3.0, help="Minimum price $ (default 3)")
    ap.add_argument("--min-volume", type=int, default=50_000, help="Minimum pre-market volume (default 50,000)")
    ap.add_argument("--outdir", default="scans", help="Folder for daily JSON files (default ./scans)")
    ap.add_argument("--batch-size", type=int, default=200)
    args = ap.parse_args()

    now_et = dt.datetime.now(ET)
    today = now_et.date()
    if now_et.weekday() >= 5:
        print("Weekend – US market closed, nothing to scan.")
        return 0
    if not (PREMARKET_START <= now_et.time() < REGULAR_CLOSE):
        print(f"Warning: it is {now_et:%H:%M} ET, outside 04:00–16:00 ET.", file=sys.stderr)

    tickers = args.tickers.split(",") if args.tickers else None
    run_scan(tickers, args.min_gap, args.min_price, args.min_volume, args.outdir, args.batch_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
