"""
Stock detail page data (refreshed weekly, like fundamentals): company profile, margins, the three financial
statements (quarterly + annual, selected lines) and insider transactions — all from Yahoo via yfinance.
Values Yahoo doesn't provide stay null; nothing is estimated.

  profile     {long_name, sector, industry, exchange, country, website}
  margins     fcf_margin_pct (FCF TTM / revenue TTM), op_margin_ttm_pct, op_margin_fy_pct, op_margin_prev_fy_pct,
              op_margin_change_pts (latest fiscal year vs the one before), gross_margin_ttm_pct
  financials  {quarterly|annual: {income|balance|cashflow: {periods: [YYYY-MM-DD newest first], LINE: [values]}}}
  insider     {transactions: [{date, insider, position, type: buy|sell|other, shares, value, text}],
               buys_6m, sells_6m, net_shares_6m, net_value_6m}
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from . import data as D

LINES = {
    "income": {"revenue": ["Total Revenue", "Operating Revenue"], "gross_profit": ["Gross Profit"],
               "operating_income": ["Operating Income"], "net_income": ["Net Income", "Net Income Common Stockholders"],
               "eps_diluted": ["Diluted EPS"], "ebitda": ["EBITDA", "Normalized EBITDA"]},
    "balance": {"total_assets": ["Total Assets"], "total_liabilities": ["Total Liabilities Net Minority Interest"],
                "equity": ["Stockholders Equity", "Common Stock Equity"],
                "cash": ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"],
                "total_debt": ["Total Debt"], "current_assets": ["Current Assets"],
                "current_liabilities": ["Current Liabilities"], "shares": ["Ordinary Shares Number", "Share Issued"]},
    "cashflow": {"operating_cf": ["Operating Cash Flow"], "capex": ["Capital Expenditure"], "fcf": ["Free Cash Flow"],
                 "buybacks": ["Repurchase Of Capital Stock"], "dividends": ["Cash Dividends Paid"],
                 "sbc": ["Stock Based Compensation"]},
}
ATTRS = {("quarterly", "income"): "quarterly_income_stmt", ("quarterly", "balance"): "quarterly_balance_sheet",
         ("quarterly", "cashflow"): "quarterly_cashflow", ("annual", "income"): "income_stmt",
         ("annual", "balance"): "balance_sheet", ("annual", "cashflow"): "cashflow"}


def _num(x):
    try:
        x = float(x)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def statement(df, lines: dict) -> dict:
    """DataFrame (rows = line items, columns = period ends) -> {periods, line: [values newest first]}."""
    if df is None or getattr(df, "empty", True):
        return {"periods": []}
    cols = sorted(df.columns, reverse=True)
    out = {"periods": [pd.Timestamp(c).strftime("%Y-%m-%d") for c in cols]}
    for key, names in lines.items():
        name = next((n for n in names if n in df.index), None)
        out[key] = [_num(df.at[name, c]) for c in cols] if name else None
    return out


def _sum4(vals):
    v = [x for x in (vals or [])[:4] if x is not None]
    return sum(v) if len(v) == 4 else None


def margins(fin: dict) -> dict:
    q, a = fin.get("quarterly", {}), fin.get("annual", {})
    rev_ttm = _sum4((q.get("income") or {}).get("revenue"))
    pct = lambda n, d: round(n / d * 100, 2) if n is not None and d else None
    op_fy = (a.get("income") or {}).get("operating_income") or []
    rev_fy = (a.get("income") or {}).get("revenue") or []
    fy = pct(op_fy[0], rev_fy[0]) if op_fy and rev_fy else None
    fy_prev = pct(op_fy[1], rev_fy[1]) if len(op_fy) > 1 and len(rev_fy) > 1 else None
    return {"fcf_margin_pct": pct(_sum4((q.get("cashflow") or {}).get("fcf")), rev_ttm),
            "op_margin_ttm_pct": pct(_sum4((q.get("income") or {}).get("operating_income")), rev_ttm),
            "gross_margin_ttm_pct": pct(_sum4((q.get("income") or {}).get("gross_profit")), rev_ttm),
            "op_margin_fy_pct": fy, "op_margin_prev_fy_pct": fy_prev,
            "op_margin_change_pts": round(fy - fy_prev, 2) if fy is not None and fy_prev is not None else None}


def _kind(text: str) -> str:
    t = (text or "").lower()
    if t.startswith("sale") or " sale " in f" {t} ":
        return "sell"
    if t.startswith("purchase") or t.startswith("buy"):
        return "buy"
    return "other"


def insider(df, as_of: dt.date, keep: int = 15, months: int = 6) -> dict:
    out = {"transactions": [], "buys_6m": 0, "sells_6m": 0, "net_shares_6m": 0.0, "net_value_6m": 0.0}
    if df is None or getattr(df, "empty", True):
        return out
    since = pd.Timestamp(as_of) - pd.DateOffset(months=months)
    d = df.copy()
    d["Start Date"] = pd.to_datetime(d.get("Start Date"), errors="coerce")
    d = d.sort_values("Start Date", ascending=False)
    for _, r in d.iterrows():
        kind = _kind(str(r.get("Text") or ""))
        shares, value = _num(r.get("Shares")), _num(r.get("Value"))
        when = r.get("Start Date")
        if pd.notna(when) and when >= since and kind in ("buy", "sell"):
            sign = 1 if kind == "buy" else -1
            out["buys_6m" if kind == "buy" else "sells_6m"] += 1
            out["net_shares_6m"] += sign * (shares or 0)
            out["net_value_6m"] += sign * (value or 0)
        if len(out["transactions"]) < keep:
            out["transactions"].append({"date": when.strftime("%Y-%m-%d") if pd.notna(when) else None,
                                        "insider": r.get("Insider"), "position": r.get("Position"), "type": kind,
                                        "shares": shares, "value": value, "text": r.get("Text") or None})
    out["net_shares_6m"], out["net_value_6m"] = round(out["net_shares_6m"]), round(out["net_value_6m"])
    return out


def details(ticker: str, cfg: dict, as_of: dt.date, ticker_obj=None) -> dict:
    """Weekly-cached detail bundle for one stock."""
    path = os.path.join(cfg["paths"]["cache"], "details", f"{ticker}.json")
    hit = D.cache_json(path, max_age_days=cfg["data"]["fundamentals_refresh_days"])
    if hit is not None and hit.get("v") == 1:
        return hit
    out = {"v": 1, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "profile": {}, "margins": {},
           "financials": {"quarterly": {}, "annual": {}}, "insider": None, "errors": {}}
    try:
        import yfinance as yf
        t = ticker_obj or yf.Ticker(ticker)
    except Exception as e:
        out["errors"]["all"] = f"yfinance unavailable: {e}"
        return out

    def safe(key, fn):
        try:
            return fn()
        except Exception as e:
            out["errors"][key] = f"{type(e).__name__}: {e}"
            return None
    info = safe("profile", lambda: t.info or {}) or {}
    out["profile"] = {"long_name": info.get("longName") or info.get("shortName"), "sector": info.get("sector"),
                      "industry": info.get("industry"), "exchange": info.get("fullExchangeName") or info.get("exchange"),
                      "country": info.get("country"), "website": info.get("website")}
    for (period, kind), attr in ATTRS.items():
        out["financials"][period][kind] = statement(safe(f"{period}.{kind}", lambda a=attr: getattr(t, a)), LINES[kind])
    out["margins"] = margins(out["financials"])
    out["insider"] = insider(safe("insider", lambda: t.insider_transactions), as_of)
    D.save_json(path, out)
    return out


def quality_pct(q: dict | None) -> float | None:
    """'Business quality' % = average of the four business scores (valuation excluded) / 5."""
    keys = ("growth", "profitability", "consistency", "balance_sheet")
    if not q or any(q.get(k) is None for k in keys):
        return None
    return round(sum(q[k] for k in keys) / 20 * 100, 1)
