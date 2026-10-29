"""Writes output/YYYY-MM-DD/: portfolio_stage.json, summary.md, alerts.md."""
from __future__ import annotations

import glob
import json
import os

ALERT_ORDER = ["Stop broken", "Stage change", "Gap", "Earnings this week", "Big news this week", "Weights",
               "Near support/resistance", "Missing data", "Portfolio data"]
ALERT_ICONS = {"Stop broken": "🔴", "Stage change": "🔄", "Gap": "⚡", "Earnings this week": "📅",
               "Big news this week": "📰", "Weights": "⚖️", "Near support/resistance": "📍",
               "Missing data": "❓", "Portfolio data": "📝"}


def grouped_alerts(alerts: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    sections: dict[str, list[str]] = {}
    for kind, text in alerts:
        sections.setdefault(kind, []).append(text)
    return sorted(sections.items(), key=lambda kv: ALERT_ORDER.index(kv[0]) if kv[0] in ALERT_ORDER else 99)


KEYS = ["p", "value", "hi", "lo", "m50", "m200", "rsi", "atr", "stage", "stage_days", "slope", "flag", "trend_label",
        "tt_score", "rs_mansfield", "ret3m_vs_spy", "support", "resistance", "stop", "target", "rr",
        "earnings_date", "earnings_soon", "history_days", "mcap", "rev_growth", "ni", "fcf", "shares_chg"]


def previous_run(out_root: str, as_of: str) -> tuple[str | None, dict | None]:
    dirs = sorted(d for d in glob.glob(os.path.join(out_root, "????-??-??"))
                  if os.path.basename(d) < as_of and os.path.exists(os.path.join(d, "portfolio_stage.json")))
    if not dirs:
        return None, None
    with open(os.path.join(dirs[-1], "portfolio_stage.json"), encoding="utf-8") as f:
        return os.path.basename(dirs[-1]), json.load(f)


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


def write_outputs(out_dir: str, meta: dict, rows: dict, alerts: list[tuple[str, str]], cfg: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    doc = {"meta": meta}
    for t, r in rows.items():
        doc[t] = {**{k: r.get(k) for k in KEYS}, **{k: v for k, v in r.items() if k not in KEYS}}
    with open(os.path.join(out_dir, "portfolio_stage.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False, default=str)

    order = sorted(rows, key=lambda t: -(rows[t].get("value") or 0))
    L = [f"# Portfolio — {meta['as_of']}", "",
         f"Total value **{_m(meta.get('total_value'), 0)}** · {len(rows)} positions · SPY {_m(meta.get('spy_price'))}"
         + (f" · ⚠️ no data for {len(meta['missing_tickers'])}: {', '.join(meta['missing_tickers'])}"
            if meta.get("missing_tickers") else ""), "",
         "| Ticker | Weight % | Stage | Trend | RS | Price | Support / Resistance | Stop | R:R |",
         "|---|---:|---|---|---:|---:|---|---:|---:|"]
    for t in order:
        r = rows[t]
        rs = "—" if r.get("rs_mansfield") is None else f"{r['rs_mansfield']:+.2f}"
        rr = "—" if r.get("rr") is None else f"{r['rr']:.1f}"
        hist = f" `{r['history_label']}`" if r.get("history_label") else ""
        L.append(f"| **{t}**{hist} | {_pct(r.get('weight_pct'))} | {stage_cell(r)} | {r.get('trend_label') or '—'} | "
                 f"{rs} | {_m(r.get('p'))} | {_m(r.get('support'))} / {_m(r.get('resistance'))} | "
                 f"{_m(r.get('stop'))} | {rr} |")
    L += ["", "Stage: S1 basing · S2 advancing · S3 topping · S4 declining · ✅ healthy S2 · ⚠️ S2 losing steam · "
          "`≥` = stage began before the price history starts", ""]
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
