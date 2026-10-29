"""Writes output/YYYY-MM-DD/: portfolio_stage.json, ohlc.json, summary.md, alerts.md (+ holdings.csv at the root).

portfolio_stage.json = {"meta": {as_of, spy_price, missing_tickers: [..], ...},
                        "tickers": {T: {p, value, stage, stage_days, flag, m50, m200, rsi, hi, lo, rs_score,
                                        levels: {S1, S2, R1, TP1},
                                        quality: {growth, profitability, consistency, balance_sheet, valuation},
                                        ...details}}}
ohlc.json = {"as_of": ..., "tickers": {T: [[date, o, h, l, c, v], ...], "SPY": [...]}}   (adjusted, SPY always)
p is the last close in ohlc.json (same rounding), value = shares x p.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os

from .analysis import round_px

ALERT_ORDER = ["Stop broken", "Stage change", "Gap", "Earnings this week", "Big news this week", "Weights",
               "Support/resistance", "Missing data", "Portfolio data"]
ALERT_ICONS = {"Stop broken": "🔴", "Stage change": "🔄", "Gap": "⚡", "Earnings this week": "📅",
               "Big news this week": "📰", "Weights": "⚖️", "Support/resistance": "📍",
               "Missing data": "❓", "Portfolio data": "📝"}


def grouped_alerts(alerts: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    sections: dict[str, list[str]] = {}
    for kind, text in alerts:
        sections.setdefault(kind, []).append(text)
    return sorted(sections.items(), key=lambda kv: ALERT_ORDER.index(kv[0]) if kv[0] in ALERT_ORDER else 99)


# spec fields first (in this order), then the details
SPEC_KEYS = ["p", "value", "stage", "stage_days", "flag", "m50", "m200", "rsi", "hi", "lo", "rs_score", "levels",
             "quality"]
LEVEL_KEYS = ["S1", "S2", "R1", "TP1"]
KEYS = SPEC_KEYS + ["atr", "slope", "trend_label", "tt_score", "rs_mansfield", "ret3m_vs_spy", "support",
                    "resistance", "stop", "target", "rr", "earnings_date", "earnings_soon", "history_days", "mcap",
                    "rev_growth", "ni", "fcf", "shares_chg"]


def previous_run(out_root: str, as_of: str) -> tuple[str | None, dict | None]:
    dirs = sorted(d for d in glob.glob(os.path.join(out_root, "????-??-??"))
                  if os.path.basename(d) < as_of and os.path.exists(os.path.join(d, "portfolio_stage.json")))
    if not dirs:
        return None, None
    with open(os.path.join(dirs[-1], "portfolio_stage.json"), encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc.get("tickers"), dict):
        return os.path.basename(dirs[-1]), doc["tickers"]
    return os.path.basename(dirs[-1]), {k: v for k, v in doc.items() if k != "meta"}      # pre-v5 layout


def _m(x, d=2):
    return "—" if x is None else f"${x:,.{d}f}"


def _pct(x, d=1, signed=False):
    return "—" if x is None else (f"{x:+.{d}f}%" if signed else f"{x:.{d}f}%")


def stage_cell(r: dict) -> str:
    if r.get("stage") is None:
        return "—"
    days = r.get("stage_days")
    d = "" if days is None else f" · {'≥' if r.get('stage_since_data_start') else ''}{days}d"
    return f"S{r['stage']}{d}{' ' + r['flag'] if r.get('flag') else ''}"


def quality_cell(q: dict | None, partial: dict | None = None) -> str:
    q = q or partial
    if not q or all(v is None for v in q.values()):
        return "—"
    return "/".join("–" if q.get(k) is None else f"{q[k]:g}"
                    for k in ("growth", "profitability", "consistency", "balance_sheet", "valuation"))


CLAMP_PCT = 0.5


def ohlc_rows(df, days: int) -> list[list]:
    """[["YYYY-MM-DD", o, h, l, c, v], ...] oldest -> newest (last row = latest day), one row per date,
    same rounding as p (round_px), rows without a close dropped."""
    d = df.dropna(subset=["Close"]).sort_index()
    d = d[~d.index.normalize().duplicated(keep="last")]
    out = []
    for idx, row in d.iloc[-days:].iterrows():
        v = row.get("Volume")
        try:
            v = 0 if v is None or math.isnan(float(v)) else int(v)
        except (TypeError, ValueError):
            v = 0
        c = round_px(row["Close"])
        o, h, lo = (round_px(row.get(k)) for k in ("Open", "High", "Low"))
        o = c if o is None else o
        h = c if h is None else h
        lo = c if lo is None else lo
        # Yahoo sometimes has the close/open a hair outside high/low (adjustment rounding): widen the range by
        # at most CLAMP_PCT so the bar is consistent; anything bigger is left as is for the validator to flag.
        top, bot = max(o, c), min(o, c)
        if h < top and (top - h) / top * 100 <= CLAMP_PCT:
            h = top
        if lo > bot and (lo - bot) / bot * 100 <= CLAMP_PCT:
            lo = bot
        out.append([idx.strftime("%Y-%m-%d"), o, h, lo, c, v])
    return out


def write_ohlc(out_dir: str, as_of: str, frames: dict, days: int) -> dict:
    """frames = {ticker as in portfolio_stage.json: DataFrame}; SPY must be passed by the caller."""
    os.makedirs(out_dir, exist_ok=True)
    doc = {"as_of": as_of, "tickers": {t: ohlc_rows(df, days) for t, df in frames.items() if df is not None}}
    with open(os.path.join(out_dir, "ohlc.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    return doc


def write_holdings_csv(path: str, held: dict, meta: dict) -> None:
    """ticker, shares, group, quality (tier letter from meta.csv) — rebuilt from the ledger every run."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "shares", "group", "quality"])
        for t in sorted(held):
            m = meta.get(t, {})
            w.writerow([t, round(held[t].shares, 6), m.get("group") or "", m.get("quality") or ""])


def write_outputs(out_dir: str, meta: dict, rows: dict, alerts: list[tuple[str, str]], cfg: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    doc = {"meta": meta, "tickers": {}}
    for t, r in rows.items():
        doc["tickers"][t] = {**{k: r.get(k) for k in KEYS}, **{k: v for k, v in r.items() if k not in KEYS}}
    with open(os.path.join(out_dir, "portfolio_stage.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False, default=str)

    order = sorted(rows, key=lambda t: -(rows[t].get("value") or 0))
    L = [f"# Portfolio — {meta['as_of']}", "",
         f"Total value **{_m(meta.get('total_value'), 0)}** · {len(rows)} positions · SPY {_m(meta.get('spy_price'))}"
         + (f" · ⚠️ no data for {len(meta['missing_tickers'])}: {', '.join(meta['missing_tickers'])}"
            if meta.get("missing_tickers") else ""), "",
         "| Ticker | Weight % | Stage | Trend | RS 1-99 | Mansfield | Price | Support / Resistance | Stop | R:R | Quality G/P/C/B/V |",
         "|---|---:|---|---|---:|---:|---:|---|---:|---:|---|"]
    for t in order:
        r = rows[t]
        rs = "—" if r.get("rs_mansfield") is None else f"{r['rs_mansfield']:+.2f}"
        rr = "—" if r.get("rr") is None else f"{r['rr']:.1f}"
        hist = f" `{r['history_label']}`" if r.get("history_label") else ""
        rs99 = "—" if r.get("rs_score") is None else str(r["rs_score"])
        L.append(f"| **{t}**{hist} | {_pct(r.get('weight_pct'))} | {stage_cell(r)} | {r.get('trend_label') or '—'} | "
                 f"{rs99} | {rs} | {_m(r.get('p'))} | {_m(r.get('support'))} / {_m(r.get('resistance'))} | "
                 f"{_m(r.get('stop'))} | {rr} | {quality_cell(r.get('quality'), r.get('quality_partial'))} |")
    L += ["", "Stage: S1 basing · S2 advancing · S3 topping · S4 declining · ✅ healthy S2 · ⚠️ S2 losing steam · "
          "`≥` = stage began before the price history starts",
          "RS 1-99 = rank among your holdings of weighted return vs SPY · Quality 0-5: growth / profitability / "
          "consistency / balance sheet / valuation", ""]
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    A = [f"# Alerts — {meta['as_of']}", ""]
    if not alerts:
        A.append("Nothing to check today.")
    else:
        for kind, items in grouped_alerts(alerts):
            A += [f"## {kind}", *[f"- {x}" for x in items], ""]
    with open(os.path.join(out_dir, "alerts.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(A) + "\n")
