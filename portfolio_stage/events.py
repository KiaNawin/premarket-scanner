"""
"This week" earnings and big news for each holding.

Earnings (Yahoo earnings calendar):
  reported  = this week's Monday .. today, with reported EPS vs estimate when Yahoo has it
  upcoming  = tomorrow .. today + N days, with before-open / after-close timing
Big news (last N days): Yahoo ticker news first, Nasdaq news as fallback. An article counts only if
  * it is about this stock (ticker or company name in the headline / Nasdaq tags it as the main symbol), and
  * its headline matches an important-event keyword, and not a "stocks to buy" style keyword
A stock that moved more than `move_pct` over the past week with no matching news is listed too.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from zoneinfo import ZoneInfo

from . import data as D

ET = ZoneInfo("America/New_York")


def week_window(as_of: dt.date, ahead_days: int) -> tuple[dt.date, dt.date]:
    monday = as_of - dt.timedelta(days=as_of.weekday())
    return monday, as_of + dt.timedelta(days=ahead_days)


# ----------------------------------------------------------------------------- earnings
def _time_label(ts) -> str:
    try:
        t = ts.tz_convert(ET) if getattr(ts, "tzinfo", None) else ts
        hm = (t.hour, t.minute)
    except Exception:
        return ""
    if hm == (0, 0):
        return "time TBD"
    if hm < (9, 30):
        return "before open"
    if hm >= (16, 0):
        return "after close"
    return f"{t:%H:%M} ET"


def _num(x):
    try:
        x = float(x)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def earnings_events(ticker: str, cfg: dict, as_of: dt.date, ticker_obj=None) -> tuple[list[dict], str | None]:
    """[{date, when, eps_est, eps_actual, surprise_pct, reported}] within the week window."""
    ec = cfg["events"]
    path = os.path.join(cfg["paths"]["cache"], "events", as_of.isoformat(), f"{ticker}_earnings.json")
    hit = D.cache_json(path)
    if hit is None:
        rows, err = [], None
        try:
            import yfinance as yf
            t = ticker_obj or yf.Ticker(ticker)
            df = t.get_earnings_dates(limit=8)
            if df is not None and len(df):
                for ts, r in df.iterrows():
                    ts = ts if getattr(ts, "tzinfo", None) else ts.tz_localize(ET)
                    rows.append({"date": ts.tz_convert(ET).date().isoformat(), "when": _time_label(ts),
                                 "eps_est": _num(r.get("EPS Estimate")), "eps_actual": _num(r.get("Reported EPS")),
                                 "surprise_pct": _num(r.get("Surprise(%)"))})
            # no rows = Yahoo simply has no dates for this stock; that is not a failure
        except Exception as e:
            err = f"earnings dates lookup failed: {type(e).__name__}: {e}"
        hit = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": rows, "error": err}
        D.save_json(path, hit)
    start, end = week_window(as_of, ec["earnings_ahead_days"])
    out = []
    for r in hit["rows"]:
        d = dt.date.fromisoformat(r["date"])
        if start <= d <= end:
            out.append({**r, "reported": d < as_of or (d == as_of and r.get("eps_actual") is not None)})
    return sorted(out, key=lambda r: r["date"]), hit.get("error")


# ----------------------------------------------------------------------------- news
def _rx(words: list[str]) -> re.Pattern:
    return re.compile(r"(?i)\b(" + "|".join(words) + r")")


def about_stock(title: str, ticker: str, name: str | None) -> bool:
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker.split('-')[0])}(?![A-Za-z])", title):
        return True
    if name:
        core = re.sub(r"(?i)[,.]|\b(inc|corp|corporation|holdings?|ltd|plc|co|company|group|class [ab])\b", " ", name)
        words = [w for w in core.split() if len(w) >= 3]
        if words and re.search(rf"(?i)\b{re.escape(words[0])}\b", title):
            return True
    return False


def is_big(title: str, cfg: dict) -> tuple[bool, str | None]:
    ec = cfg["events"]
    if _rx(ec["exclude_keywords"]).search(title):
        return False, None
    m = _rx(ec["big_news_keywords"]).search(title)
    return (True, m.group(1).lower()) if m else (False, None)


def _yahoo_news(ticker: str, ticker_obj=None) -> list[dict]:
    import yfinance as yf
    t = ticker_obj or yf.Ticker(ticker)
    out = []
    for n in (t.news or []):
        c = n.get("content", n)
        prov = c.get("provider") or {}
        url = c.get("canonicalUrl") or c.get("clickThroughUrl") or {}
        when = c.get("pubDate") or c.get("displayTime")
        if not when and n.get("providerPublishTime"):
            when = dt.datetime.fromtimestamp(n["providerPublishTime"], dt.timezone.utc).isoformat()
        out.append({"title": c.get("title") or "", "publisher": prov.get("displayName") if isinstance(prov, dict)
                    else n.get("publisher"), "url": url.get("url") if isinstance(url, dict) else n.get("link"),
                    "date": (when or "")[:10], "source": "yahoo", "main": None})
    return out


def _nasdaq_news(ticker: str) -> list[dict]:
    import premarket_scanner as PS
    r = PS.requests.get(PS.NASDAQ_NEWS_URL, params={"q": f"{ticker.replace('-', '.')}|stocks", "offset": 0,
                                                    "limit": 20, "fallback": "false"},
                        headers=PS.NASDAQ_HEADERS, timeout=10)
    r.raise_for_status()
    out = []
    low = ticker.replace("-", ".").lower()
    for a in PS._find_articles((r.json() or {}).get("data") or {}):
        try:
            d = dt.datetime.strptime(a.get("created", ""), "%b %d, %Y").date().isoformat()
        except ValueError:
            d = ""
        rel = a.get("related_symbols") or []
        url = a.get("url") or ""
        out.append({"title": a["title"], "publisher": a.get("publisher"), "date": d, "source": "nasdaq",
                    "url": "https://www.nasdaq.com" + url if url.startswith("/") else url,
                    "main": str(a.get("primarysymbol", "")).lower() == low or
                            (f"{low}|stocks" in rel and len(rel) <= 3)})
    return out


def big_news(ticker: str, name: str | None, cfg: dict, as_of: dt.date) -> tuple[list[dict], str | None]:
    ec = cfg["events"]
    path = os.path.join(cfg["paths"]["cache"], "events", as_of.isoformat(), f"{ticker}_news.json")
    hit = D.cache_json(path)
    if hit is None:
        items, errs = [], []
        for name_, fn in (("Yahoo", lambda: _yahoo_news(ticker)), ("Nasdaq", lambda: _nasdaq_news(ticker))):
            try:
                items += fn()
            except Exception as e:
                errs.append(f"{name_}: {type(e).__name__}: {e}")
        hit = {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items": items,
               "error": "; ".join(errs) if len(errs) == 2 else None}
        D.save_json(path, hit)
    since = as_of - dt.timedelta(days=ec["news_lookback_days"])
    picked, seen = [], set()
    for it in sorted(hit["items"], key=lambda x: x.get("date") or "", reverse=True):
        title = (it.get("title") or "").strip()
        try:
            d = dt.date.fromisoformat(it.get("date") or "")
        except ValueError:
            continue
        if not title or d < since or d > as_of + dt.timedelta(days=1):
            continue
        relevant = it.get("main") if it.get("source") == "nasdaq" else about_stock(title, ticker, name)
        big, tag = is_big(title, cfg)
        key = re.sub(r"\W+", "", title.lower())[:60]
        if relevant and big and key not in seen:
            seen.add(key)
            picked.append({**it, "tag": tag})
    return picked[:ec["max_news_per_ticker"]], hit.get("error")
