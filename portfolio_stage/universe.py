"""
Whole-market momentum scan ("หุ้นโมเมนตัมแรง") — also the ranking universe for ret_rank_top_pct.

  1. universe   all US common stocks (NASDAQ/NYSE/AMEX listings, ETFs and test issues excluded) — the same
                list Scanner A uses (premarket_scanner.load_universe)
  2. prefilter  Yahoo batch quotes: price >= universe.min_price and 3-month average dollar volume
                >= universe.min_dollar_volume (keeps the download to liquid names)
  3. daily bars 1 year, adjusted, closed bars only -> ret_1m/3m/6m, momentum score = their average,
                ret_rank_top_pct = rank of the score among every scanned stock (1-100, 5 = top 5%)
  4. strong     any of ret_1m/3m/6m >= 30% and rank <= momentum.max_rank_top_pct (10), 20-day average
                dollar volume >= $20M, price >= $5, weekly Stage 2
  5. leaders    strong AND a setup today (base_tight.ok or first_retest) when momentum.require_setup;
                strong stocks without a setup are listed as a short watch list in momentum.md only
Outputs output/<date>/momentum.json + momentum.md. Never fails the portfolio run: errors come back as a reason.
"""
from __future__ import annotations

import copy
import datetime as dt
import glob
import json
import os
import shutil
import sys
import time

import numpy as np

from . import analysis as A
from . import data as D
from . import swing as SW

QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QUOTE_FIELDS = "symbol,quoteType,regularMarketPrice,averageDailyVolume3Month"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_universe() -> list[str]:
    import premarket_scanner as PS
    return PS.load_universe()


def quote_prefilter(symbols: list[str], min_price: float, min_dollar_volume: float, batch: int = 150) -> list[str]:
    from yfinance.data import YfData
    try:
        import yf_utils
        yf_utils._setup_cache()
    except Exception:
        pass
    data = YfData()
    keep, quoted = [], 0
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        res = None
        for attempt in range(3):
            try:
                js = data.get_raw_json(QUOTE_URL, params={"symbols": ",".join(chunk), "fields": QUOTE_FIELDS,
                                                          "formatted": "false", "lang": "en-US", "region": "US"})
                res = (js.get("quoteResponse") or {}).get("result") or []
                break
            except Exception as e:
                if attempt == 2:
                    log(f"  quote batch {i // batch + 1} failed: {e}")
                time.sleep(1 + attempt)
        for q in res or []:
            quoted += 1
            px, vol = q.get("regularMarketPrice"), q.get("averageDailyVolume3Month")
            if q.get("quoteType", "EQUITY") != "EQUITY" or not px or not vol:
                continue
            if px >= min_price and px * vol >= min_dollar_volume:
                keep.append(q["symbol"])
        time.sleep(0.2)
    if quoted < 0.5 * len(symbols):
        raise RuntimeError(f"Yahoo quotes for only {quoted}/{len(symbols)} symbols")
    return sorted(set(keep))


def _clean_old_cache(root: str, keep_days: int = 2) -> None:
    dirs = sorted(glob.glob(os.path.join(root, "prices", "????-??-??")))
    for d in dirs[:-keep_days]:
        shutil.rmtree(d, ignore_errors=True)


def analyse(t: str, df, now_et: dt.datetime, bar_complete: bool, fri: dt.date, cfg: dict) -> dict | None:
    d = SW.closed_bars(df, now_et, bar_complete)
    if len(d) < 2:
        return None
    c = d["Close"]
    rets = SW.returns(c)
    adv20 = float((d["Close"] * d["Volume"]).iloc[-20:].mean()) if len(d) >= 20 else None
    return {"ticker": t, "price": A.round_px(c.iloc[-1]), "adv20": adv20, **rets,
            "score": SW.momentum_score(rets), "_df": d}


def scan(cfg: dict, as_of: dt.date, now_et: dt.datetime, bar_complete: bool, refresh: bool = False,
         symbols: list[str] | None = None, downloader=None) -> dict:
    """Returns {"rows": {ticker: row}, "scores": [...], "leaders": [...], "funnel": {...}, "error": str|None}."""
    uc = cfg["universe"]
    out = {"rows": {}, "scores": [], "leaders": [], "funnel": {}, "error": None,
           "definition": (f"US common stocks, price >= ${uc['min_price']:g}, 3-month avg dollar volume >= "
                          f"${uc['min_dollar_volume'] / 1e6:g}M")}
    t0 = time.time()
    try:
        if symbols is None:
            allsyms = load_universe()
            out["funnel"]["universe"] = len(allsyms)
            symbols = quote_prefilter(allsyms, uc["min_price"], uc["min_dollar_volume"])
        out["funnel"]["prefilter"] = len(symbols)
        log(f"  momentum universe: {len(symbols)} liquid stocks — downloading daily bars…")
        ucfg = copy.deepcopy(cfg)
        ucfg["paths"]["cache"] = os.path.join(cfg["paths"]["cache"], "universe")
        ucfg["data"].update(history_period=uc["history_period"], retries=1, backoff_seconds=2)
        _clean_old_cache(ucfg["paths"]["cache"])
        prices, missing = {}, {}
        for i in range(0, len(symbols), uc["download_batch"]):
            got, miss = D.fetch_daily(symbols[i:i + uc["download_batch"]], ucfg, as_of, refresh=refresh,
                                      downloader=downloader)
            prices.update(got)
            missing.update(miss)
        out["funnel"]["with_prices"] = len(prices)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    fri = A.last_complete_friday(now_et)
    for t, df in prices.items():
        r = analyse(t, df, now_et, bar_complete, fri, cfg)
        if r:
            out["rows"][t] = r
    out["scores"] = [r["score"] for r in out["rows"].values() if r["score"] is not None]
    out["funnel"]["ranked"] = len(out["scores"])
    for r in out["rows"].values():
        r["ret_rank_top_pct"] = SW.top_pct(r["score"], out["scores"])

    lc = cfg["momentum"]
    leaders = []
    for t, r in out["rows"].items():
        rets = [r.get(k) for k in SW.RET_WINDOWS]
        if not (any(x is not None and x >= lc["min_ret_pct"] for x in rets)
                and r["ret_rank_top_pct"] is not None and r["ret_rank_top_pct"] <= lc["max_rank_top_pct"]
                and r["adv20"] is not None and r["adv20"] >= lc["min_adv20_usd"] and r["price"] >= lc["min_price"]):
            continue
        st = A.compute_stage(A.weekly_closes(r["_df"]["Close"], fri), cfg)
        if st["stage"] != 2:
            continue
        f, _ = SW.compute(r["_df"])
        stop_pct = (A.r2((f["prev_high"] - f["prev_low2"]) / f["prev_high"] * 100)
                    if f["prev_high"] and f["prev_low2"] is not None else None)
        leaders.append({"ticker": t, "price": r["price"], "ret_1m": r["ret_1m"], "ret_3m": r["ret_3m"],
                        "ret_6m": r["ret_6m"], "ret_rank_top_pct": r["ret_rank_top_pct"], "stage": 2,
                        "adv20_musd": A.r2(r["adv20"] / 1e6, 1), "base_tight": f["base_tight"],
                        "first_retest": f["first_retest"], "ema9": f["ema9"], "ema21": f["ema21"],
                        "prev_high": f["prev_high"], "prev_low2": f["prev_low2"], "stop_pct": stop_pct})
    setup = lambda x: bool((x["base_tight"] or {}).get("ok")) or x["first_retest"] is not None
    leaders.sort(key=lambda x: (not setup(x), x["ret_rank_top_pct"], -(x["ret_3m"] or 0)))
    out["funnel"]["strong"] = len(leaders)
    if lc.get("require_setup", True):
        out["watch"] = [x["ticker"] for x in leaders if not setup(x)]
        leaders = [x for x in leaders if setup(x)]
    out["leaders"] = leaders
    out["funnel"]["leaders"] = len(leaders)
    out["filter"] = (f"ret 1m/3m/6m any ≥ {lc['min_ret_pct']:g}% · rank top {lc['max_rank_top_pct']:g}% · "
                     f"20-day avg $ volume ≥ ${lc['min_adv20_usd'] / 1e6:g}M · price ≥ ${lc['min_price']:g} · "
                     f"weekly Stage 2" + (" · setup today (base tight or first retest)"
                                          if lc.get("require_setup", True) else ""))
    out["seconds"] = round(time.time() - t0)
    return out


# ---------------------------------------------------------------------------- report
def _yn(x) -> str:
    return "✓" if x else "·"


def write_report(out_dir: str, as_of: str, res: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    doc = {"as_of": as_of, "definition": res.get("definition"), "funnel": res.get("funnel"),
           "error": res.get("error"), "filter": res.get("filter"), "leaders": res.get("leaders", []),
           "watch": res.get("watch", [])}
    with open(os.path.join(out_dir, "momentum.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    L = [f"# Momentum leaders — {as_of}", ""]
    if res.get("error"):
        L += [f"⚠️ Scan failed: {res['error']}", ""]
    fu = res.get("funnel") or {}
    L += [f"Universe: {res.get('definition')} · scanned {fu.get('with_prices', 0)} · ranked {fu.get('ranked', 0)} "
          f"· strong {fu.get('strong', 0)} · with a setup {fu.get('leaders', 0)}", "",
          f"Filter: {res.get('filter') or '—'}",
          "", "| Ticker | 1m | 3m | 6m | Rank top % | Stage | Base tight | Retest | Buy > prev high | Stop prev low2 | Stop % |",
          "|---|---:|---:|---:|---:|---|---|---|---:|---:|---:|"]
    pc = lambda x: "—" if x is None else f"{x:+.1f}%"
    for x in res.get("leaders", []):
        bt = x["base_tight"] or {}
        bt_txt = "✅" if bt.get("ok") else f"hl{_yn(bt.get('hl'))} c{_yn(bt.get('contr'))} r{_yn(bt.get('reclaim'))}"
        rt = f"${x['first_retest']['level']:,.2f}" if x["first_retest"] else "—"
        L.append(f"| **{x['ticker']}** | {pc(x['ret_1m'])} | {pc(x['ret_3m'])} | {pc(x['ret_6m'])} | "
                 f"{x['ret_rank_top_pct']} | S2 | {bt_txt} | {rt} | ${x['prev_high']:,.2f} | "
                 f"${x['prev_low2']:,.2f} | {x['stop_pct'] if x['stop_pct'] is not None else '—'}% |")
    if res.get("watch"):
        L += ["", f"**Strong, no setup yet ({len(res['watch'])})** — wait for a tight base or a first retest:",
              ", ".join(res["watch"])]
    L += ["", "Base tight: hl = higher lows · c = range contracting · r = above rising EMA9/21. "
          "Retest = first pullback to a level broken in the last 30 days.", ""]
    with open(os.path.join(out_dir, "momentum.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def telegram_text(as_of: str, res: dict, max_rows: int = 15) -> str:
    import html
    if res.get("error"):
        return f"<b>🚀 Momentum leaders — {as_of}</b>\n⚠️ Scan failed: {html.escape(res['error'])}"
    ls = res.get("leaders", [])
    fu = res.get("funnel") or {}
    lines = [f"<b>🚀 Momentum setups — {as_of}</b>",
             f"{len(ls)} setups among {fu.get('strong', len(ls))} strong stocks out of {fu.get('ranked', 0)} scanned",
             f"<i>{html.escape(res.get('filter') or '')}</i>", ""]
    for x in ls[:max_rows]:
        tag = " ✅base" if (x["base_tight"] or {}).get("ok") else ""
        tag += f" 🔁retest ${x['first_retest']['level']:,.2f}" if x["first_retest"] else ""
        r3 = "—" if x["ret_3m"] is None else f"{x['ret_3m']:+.0f}%"
        lines.append(f"<b>{html.escape(x['ticker'])}</b> 3m {r3} · top {x['ret_rank_top_pct']}%{tag}\n"
                     f"   buy &gt; ${x['prev_high']:,.2f} · stop ${x['prev_low2']:,.2f} ({x['stop_pct']}%)")
    if len(ls) > max_rows:
        lines.append(f"\n… +{len(ls) - max_rows} more in output/{as_of}/momentum.md")
    if not ls:
        lines.append("No strong stock has a setup today.")
    if res.get("watch"):
        lines.append(f"<i>{len(res['watch'])} more strong stocks without a setup yet: momentum.md</i>")
    return "\n".join(lines)
