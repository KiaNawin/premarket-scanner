#!/usr/bin/env python3
"""
🧾 Options shadow test — would buying a call have beaten buying the stock?

Nothing is traded. For every new 🚨 (Scanner B) or 🎯 (ข้อ A) alert, right after the alert is sent:
  * pick one call: the first expiry at least min_dte days out, strike = highest strike at or below the
    stock entry (slightly in the money, delta ~0.55-0.65), and save its bid / ask / IV / open interest
    -> history/options_signals/<date>.jsonl
At the end-of-day run, for each recorded call:
  * est   = Black-Scholes value at the stock plan's exit price and time, with the entry IV
            (sold at mid − half the entry spread). This is a model: it ignores IV changes.
  * close = the real bid of the same contract after the close ("held to the close"; includes IV crush).
  * Both are % of the premium paid (bought at the ask). Buying calls for exactly 1R of premium makes
    the result in R = % / 100 (−100 % = −1R), so it lines up with the stock plan's R.
-> history/options_trades.csv, plus a block in the Thai EOD summary.
Caveats: Yahoo option quotes can be delayed ~15 min and thin after the close; no commissions.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_DIR = os.path.join(BASE, "history", "options_signals")
TRADES_CSV = os.path.join(BASE, "history", "options_trades.csv")
DEFAULTS = {"enabled": True, "min_dte": 7, "max_dte": 60, "max_spread_pct": 15.0, "min_open_interest": 100,
            "rate": 0.04}
FIELDS = ["date", "ticker", "kind", "signal_time_et", "contract", "expiry", "strike", "dte", "stock_entry",
          "ask", "bid", "mid", "spread_pct", "iv", "open_interest", "liquid", "stock_exit", "stock_exit_time_et",
          "stock_exit_reason", "stock_r", "stock_pct", "est_value", "est_pct", "close_bid", "close_iv",
          "close_pct", "stock_close", "note"]


def load_config() -> dict:
    import trade_plan
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (trade_plan.load_config().get("options") or {}).items() if not k.startswith("_")})
    return c


# ----------------------------------------------------------------------------- math
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S: float, K: float, T: float, r: float, iv: float) -> float:
    if T <= 0 or iv <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + iv * iv / 2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def bs_delta(S: float, K: float, T: float, r: float, iv: float) -> float:
    if T <= 0 or iv <= 0:
        return 1.0 if S > K else 0.0
    return _ncdf((math.log(S / K) + (r + iv * iv / 2) * T) / (iv * math.sqrt(T)))


def years_to_expiry(expiry: str, at: dt.datetime) -> float:
    exp = dt.datetime.combine(dt.date.fromisoformat(expiry), dt.time(16, 0), ET)
    return max((exp - at).total_seconds(), 0) / (365 * 86400)


# ----------------------------------------------------------------------------- contract choice (pure)
def pick_expiry(expiries: list[str], today: dt.date, cfg: dict) -> str | None:
    ok = [e for e in sorted(expiries) if cfg["min_dte"] <= (dt.date.fromisoformat(e) - today).days <= cfg["max_dte"]]
    return ok[0] if ok else None


def pick_strike(calls: list[dict], spot: float) -> dict | None:
    """Highest strike at or below spot (slightly ITM); else the lowest strike above."""
    calls = sorted((c for c in calls if c.get("strike")), key=lambda c: c["strike"])
    below = [c for c in calls if c["strike"] <= spot]
    return below[-1] if below else (calls[0] if calls else None)


def quote_fields(c: dict, cfg: dict) -> dict:
    bid, ask = float(c.get("bid") or 0), float(c.get("ask") or 0)
    mid = (bid + ask) / 2 if bid and ask else float(c.get("lastPrice") or 0)
    spread = (ask - bid) / mid * 100 if bid and ask and mid else None
    oi = int(c.get("openInterest") or 0)
    liquid = bool(ask) and spread is not None and spread <= cfg["max_spread_pct"] and oi >= cfg["min_open_interest"]
    return {"contract": c.get("contractSymbol"), "strike": float(c["strike"]), "ask": round(ask, 4),
            "bid": round(bid, 4), "mid": round(mid, 4), "spread_pct": round(spread, 1) if spread is not None else None,
            "iv": round(float(c.get("impliedVolatility") or 0), 4), "open_interest": oi, "liquid": liquid}


# ----------------------------------------------------------------------------- data
def yahoo_chain(ticker: str):
    """(expiries, get_calls(expiry) -> list[dict])"""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    return list(tk.options or []), lambda e: tk.option_chain(e).calls.to_dict("records")


def record_alerts(day: dt.date, rows: list[dict], kind: str, chain=yahoo_chain, out_dir: str = SIGNALS_DIR,
                  now: dt.datetime | None = None) -> list[dict]:
    """Pick and save one call per new alert. kind = "B" (🚨) or "A" (🎯)."""
    cfg = load_config()
    if not cfg.get("enabled", True) or not rows:
        return []
    now = now or dt.datetime.now(ET)
    saved = []
    for r in rows:
        entry = (r.get("plan") or {}).get("entry") or r.get("entry_price")
        rec = {"date": day.isoformat(), "ticker": r["ticker"], "kind": kind, "signal_time_et": r.get("signal_time_et"),
               "stock_entry": entry, "quote_time_et": now.strftime("%H:%M")}
        try:
            expiries, get_calls = chain(r["ticker"])
            exp = pick_expiry(expiries, day, cfg)
            if not exp:
                rec["note"] = "ไม่มีออปชันที่อายุเหมาะ" if expiries else "หุ้นนี้ไม่มีออปชัน"
            else:
                c = pick_strike(get_calls(exp), float(entry))
                if c is None:
                    rec["note"] = "ไม่มี strike"
                else:
                    rec.update(expiry=exp, dte=(dt.date.fromisoformat(exp) - day).days, **quote_fields(c, cfg))
                    if rec["iv"]:
                        rec["delta"] = round(bs_delta(float(entry), rec["strike"], years_to_expiry(exp, now),
                                                      cfg["rate"], rec["iv"]), 2)
        except Exception as e:
            rec["note"] = f"ดึงออปชันไม่ได้: {type(e).__name__}"
        saved.append(rec)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{day.isoformat()}.jsonl"), "a", encoding="utf-8") as f:
        for rec in saved:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return saved


def _load(day: dt.date, out_dir: str) -> list[dict]:
    try:
        with open(os.path.join(out_dir, f"{day.isoformat()}.jsonl"), encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except OSError:
        return []


def _read_csv(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def score_row(sig: dict, stock: dict | None, close_quote: dict | None, day: dt.date, cfg: dict) -> dict:
    """One scored line (pure)."""
    row = {k: sig.get(k) for k in FIELDS if k in sig}
    row.update(date=sig["date"], ticker=sig["ticker"], kind=sig["kind"])
    if not sig.get("ask"):
        row["note"] = sig.get("note") or "ไม่มีราคา ask"
        return row
    ask, spread = float(sig["ask"]), float(sig["ask"]) - float(sig.get("bid") or sig["ask"])
    if stock and _f(stock.get("exit_price")) is not None:
        S, K = float(stock["exit_price"]), float(sig["strike"])
        t = (stock.get("exit_time_et") or "16:00")[-5:]
        at = dt.datetime.combine(day, dt.time.fromisoformat(t), ET)
        est = bs_call(S, K, years_to_expiry(sig["expiry"], at), cfg["rate"], float(sig["iv"] or 0)) - spread / 2
        entry = _f(stock.get("entry")) or _f(sig.get("stock_entry"))
        row.update(stock_exit=S, stock_exit_time_et=t, stock_exit_reason=stock.get("exit_reason"),
                   stock_r=_f(stock.get("result_r")), stock_close=_f(stock.get("close_price")),
                   stock_pct=round((S / entry - 1) * 100, 2) if entry else None,
                   est_value=round(max(est, 0), 4), est_pct=round((max(est, 0) / ask - 1) * 100, 1))
    else:
        row["note"] = "ไม่มีผลของแผนหุ้น"
    if close_quote:
        px = float(close_quote.get("bid") or 0) or float(close_quote.get("lastPrice") or 0)
        if px:
            row.update(close_bid=round(px, 4), close_iv=_f(close_quote.get("impliedVolatility")),
                       close_pct=round((px / ask - 1) * 100, 1))
    return row


def score_day(day: dt.date, stock_rows: dict[str, list[dict]], chain=yahoo_chain, out_dir: str = SIGNALS_DIR,
              trades_csv: str = TRADES_CSV) -> list[dict]:
    """stock_rows = {"B": scored 🚨 rows, "A": scored 🎯 rows} from trade_journal.score_day."""
    cfg = load_config()
    sigs = _load(day, out_dir)
    have = {(r["date"], r["ticker"], r["kind"]) for r in _read_csv(trades_csv)}
    new = []
    for s in sigs:
        if (s["date"], s["ticker"], s["kind"]) in have:
            continue
        stock = next((r for r in stock_rows.get(s["kind"], []) if r.get("ticker") == s["ticker"]), None)
        q = None
        if s.get("contract") and s.get("expiry"):
            try:
                _, get_calls = chain(s["ticker"])
                q = next((c for c in get_calls(s["expiry"]) if c.get("contractSymbol") == s["contract"]), None)
            except Exception:
                q = None
        new.append(score_row(s, stock, q, day, cfg))
    if new:
        exists = os.path.exists(trades_csv)
        os.makedirs(os.path.dirname(trades_csv), exist_ok=True)
        with open(trades_csv, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerows(new)
    return new


def stats(trades_csv: str = TRADES_CSV) -> dict:
    rows = _read_csv(trades_csv)
    liq = [r for r in rows if r.get("liquid") in ("True", "true") and _f(r.get("est_pct")) is not None]

    def blk(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"n": 0}
        return {"n": len(vals), "win": sum(v > 0 for v in vals) / len(vals), "avg": sum(vals) / len(vals),
                "total": sum(vals)}
    return {"signals": len(rows), "liquid": len(liq), "since": rows[0]["date"] if rows else None,
            "est_r": blk([_f(r["est_pct"]) / 100 for r in liq]),
            "close_r": blk([_f(r.get("close_pct")) / 100 if _f(r.get("close_pct")) is not None else None for r in liq]),
            "stock_r": blk([_f(r.get("stock_r")) for r in liq])}
