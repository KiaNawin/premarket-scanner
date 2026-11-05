"""
Portfolio summary for meta.summary / meta.history in portfolio_stage.json (the rebalance page's header card).

  cash        portfolio/cash.csv — you edit it: account, currency (USD/THB), amount, as_of, note
  fx          USD/THB close from Yahoo (THB=X, baht per dollar)
  summary     stocks / cash / total in USD and THB, cost basis, unrealized and realized P/L,
              change today (held shares x (last close - previous close)) and over ~1 month
  history     actual    [date, stocks_usd, cost_usd] from the trade ledger (starts at the first trade)
              backcast  [date, value_usd, coverage_pct] = today's shares x past closes — NOT the account's
                        real past; only for longer chart ranges, coverage = share of today's value priced
              snapshots [date, stocks_usd, cash_usd, total_usd, fx] — one row per run (portfolio/history.csv)
Every value that can't be computed is null with the reason in summary.null_reasons.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd

from . import analysis as A
from . import data as D
from . import ledger as L

ET = ZoneInfo("America/New_York")
CASH_COLUMNS = ["account", "currency", "amount", "as_of", "note"]
HIST_COLUMNS = ["date", "stocks_usd", "cash_usd", "total_usd", "fx_usdthb"]


# ---------------------------------------------------------------------------- cash
def read_cash(path: str) -> tuple[list[dict], str | None]:
    if not os.path.exists(path):
        return [], f"{os.path.basename(path)} not found — add your cash balances there"
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                amt = float(str(r.get("amount") or "").replace(",", ""))
            except ValueError:
                continue
            out.append({"account": (r.get("account") or "").strip(), "currency": (r.get("currency") or "").strip().upper(),
                        "amount": amt, "as_of": (r.get("as_of") or "").strip() or None,
                        "note": (r.get("note") or "").strip() or None})
    return out, None


def cash_usd(accounts: list[dict], fx: float | None) -> tuple[float | None, list[dict], list[str]]:
    total, rows, problems = 0.0, [], []
    for a in accounts:
        usd = None
        if a["currency"] == "USD":
            usd = a["amount"]
        elif a["currency"] == "THB" and fx:
            usd = a["amount"] / fx
        else:
            problems.append(f"{a['account'] or '?'}: can't convert {a['currency']}"
                            + ("" if a["currency"] != "THB" else " (no USD/THB rate)"))
        rows.append({**a, "usd": A.r2(usd)})
        total += usd or 0
    return (A.r2(total) if accounts else None), rows, problems


# ---------------------------------------------------------------------------- history
def _trade_date(iso: str) -> dt.date:
    t = dt.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=ET)
    return t.astimezone(ET).date()


def _closes(prices: dict, tickers) -> pd.DataFrame:
    cols = {t: prices[D.to_yahoo(t)]["Close"] for t in tickers if prices.get(D.to_yahoo(t)) is not None}
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols).sort_index()
    df.index = pd.DatetimeIndex(df.index).normalize()
    return df[~df.index.duplicated(keep="last")]


def _v(shares: float, close) -> float:
    """Same rounding as a row's `value` (shares x round_px(close), to the cent) so the series ends on it."""
    return A.r2(shares * A.round_px(close))


def actual_history(trades: list[dict], prices: dict, as_of: dt.date) -> list[list]:
    """Account stock value and cost basis at each close since the first trade (ledger positions that day)."""
    if not trades:
        return []
    by_day = sorted(trades, key=lambda r: r["datetime"])
    start = _trade_date(by_day[0]["datetime"])
    closes = _closes(prices, {L.normalize_ticker(r["ticker"]) for r in trades})
    if closes.empty:
        return []
    closes = closes[closes.index.date >= start].ffill()          # same bars as p (last row = latest close)
    out = []
    for ts in closes.index:
        d = ts.date()
        upto = [r for r in by_day if _trade_date(r["datetime"]) <= d]
        pos = L.holdings(upto)
        val = sum(_v(p.shares, closes.at[ts, t]) for t, p in pos.items()
                  if p.shares > L.EPS and t in closes.columns and pd.notna(closes.at[ts, t]))
        cost = sum(p.cost for p in pos.values() if p.shares > L.EPS)
        out.append([d.isoformat(), A.r2(val), A.r2(cost)])
    return out


def backcast(held: dict, prices: dict, days: int) -> list[list]:
    """Today's shares x each past close; coverage = % of today's value whose ticker has a price that day."""
    closes = _closes(prices, held)
    if closes.empty:
        return []
    closes = closes.iloc[-days:]
    last = closes.ffill().iloc[-1]
    weights = {t: held[t].shares * float(last[t]) for t in closes.columns if pd.notna(last[t])}
    total_now = sum(weights.values()) or 1.0
    out = []
    for ts, row in closes.iterrows():
        have = [t for t in weights if pd.notna(row[t])]
        val = sum(_v(held[t].shares, row[t]) for t in have)
        out.append([ts.date().isoformat(), A.r2(val), A.r2(sum(weights[t] for t in have) / total_now * 100, 1)])
    return out


def save_snapshot(path: str, row: dict) -> list[list]:
    """Replace today's row in portfolio/history.csv (one per date) and return all rows."""
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != row["date"]]
    rows.append({k: row.get(k) for k in HIST_COLUMNS})
    rows.sort(key=lambda r: r["date"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_COLUMNS)
        w.writeheader()
        w.writerows({k: ("" if r.get(k) is None else r.get(k)) for k in HIST_COLUMNS} for r in rows)

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return [[r["date"]] + [num(r.get(k)) for k in HIST_COLUMNS[1:]] for r in rows]


# ---------------------------------------------------------------------------- summary
def build(rows: dict, held: dict, trades: list[dict], prices: dict, fx_df, cfg: dict, as_of: dt.date) -> tuple[dict, dict]:
    """Returns (summary, history) for meta."""
    why: dict[str, str] = {}
    fx = A.round_px(fx_df["Close"].dropna().iloc[-1]) if fx_df is not None and len(fx_df) else None
    fx_date = fx_df["Close"].dropna().index[-1].date().isoformat() if fx else None
    if fx is None:
        why["fx_usdthb"] = f"no {cfg['portfolio']['fx_ticker']} price from Yahoo"

    stocks = A.r2(sum(r.get("value") or 0 for r in rows.values()))
    cost = A.r2(sum(p.cost for t, p in held.items() if (rows.get(t) or {}).get("value") is not None))
    unreal = A.r2(stocks - cost) if cost else None
    all_pos = L.holdings(trades, include_closed=True)
    realized = A.r2(sum(p.realized for p in all_pos.values()))

    day = 0.0
    day_ok = False
    for t, p in held.items():
        df = prices.get(D.to_yahoo(t))
        if df is not None and len(df) > 1:
            c = df["Close"].dropna()
            day += p.shares * float(c.iloc[-1] - c.iloc[-2])
            day_ok = True
    day = A.r2(day) if day_ok else None
    if day is None:
        why["day_change_usd"] = "no price data"

    accounts, cash_err = read_cash(cfg["paths"]["cash"])
    cash, cash_rows, problems = cash_usd(accounts, fx)
    if cash_err:
        why["cash_usd"] = cash_err
    if problems:
        why["cash"] = "; ".join(problems)
    total = A.r2(stocks + (cash or 0))

    hist_actual = actual_history(trades, prices, as_of)
    hist_back = backcast(held, prices, cfg["portfolio"]["backcast_days"])
    month, basis = None, None
    if len(hist_actual) > 21:
        month, basis = hist_actual[-1][1] - hist_actual[-22][1], "actual"
    elif len(hist_back) > 21 and hist_back[-22][2] >= 95:
        month, basis = hist_back[-1][1] - hist_back[-22][1], "backcast (today's holdings at past prices)"
        why["month_change_usd"] = (f"ledger history starts {hist_actual[0][0] if hist_actual else '—'}; "
                                   "this uses today's holdings at the prices of 21 trading days ago")
    else:
        why["month_change_usd"] = "not enough history"
    prev_month_val = (stocks - month) if month is not None else None

    thb = lambda x: A.r2(x * fx) if x is not None and fx else None
    summary = {
        "currency": "USD", "fx_usdthb": fx, "fx_date": fx_date,
        "stocks_usd": stocks, "cash_usd": cash, "total_usd": total,
        "stocks_thb": thb(stocks), "cash_thb": thb(cash), "total_thb": thb(total),
        "cost_usd": cost, "unrealized_usd": unreal,
        "unrealized_pct": A.r2(unreal / cost * 100) if unreal is not None and cost else None,
        "realized_usd": realized,
        "day_change_usd": day, "day_change_pct": A.r2(day / (stocks - day) * 100) if day is not None and stocks - day else None,
        "month_change_usd": A.r2(month), "month_change_pct": A.r2(month / prev_month_val * 100) if month is not None and prev_month_val else None,
        "month_basis": basis,
        "cash": {"accounts": cash_rows, "as_of": max((a["as_of"] for a in accounts if a.get("as_of")), default=None)},
        "history_start": hist_actual[0][0] if hist_actual else None,
        "null_reasons": why,
    }
    snaps = save_snapshot(cfg["paths"]["history"], {"date": as_of.isoformat(), "stocks_usd": stocks, "cash_usd": cash,
                                                    "total_usd": total, "fx_usdthb": fx})
    return summary, {"actual": hist_actual, "backcast": hist_back, "snapshots": snaps}
