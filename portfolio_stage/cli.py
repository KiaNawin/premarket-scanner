"""
python -m portfolio_stage run                         analyse every holding -> output/YYYY-MM-DD/
python -m portfolio_stage momentum                    whole-market momentum scan only (momentum.md)
python -m portfolio_stage bootstrap dime_values.csv   first time only: OPENING rows from Dime values
python -m portfolio_stage ingest slips/               read Dime slips -> append to portfolio/trades.csv
python -m portfolio_stage reconcile dime_values.csv   compare ledger value vs the Dime app (>2% -> alert)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

from . import analysis as A
from . import data as D
from . import ledger as L
from .config import load_config, load_dotenv

ET = ZoneInfo("America/New_York")


def _now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def is_trading_day(d: dt.date) -> bool:
    try:
        from run_cycle import NYSE_HOLIDAYS             # same holiday list as the scanner
    except Exception:
        NYSE_HOLIDAYS = set()
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def last_bar_complete(now_et: dt.datetime) -> bool:
    """False whenever the run happens before 16:00 ET on a US trading day (that day's bar isn't final yet)."""
    return not (is_trading_day(now_et.date()) and now_et.time() < dt.time(16, 0))


def _last_completed_close(df, now_et: dt.datetime) -> tuple[float, dt.date]:
    """Latest *finished* daily close (ignores today's bar while the market is still open)."""
    last = df.index[-1].date()
    if last == now_et.date() and now_et.time() < dt.time(16, 0) and len(df) > 1:
        return float(df["Close"].iloc[-2]), df.index[-2].date()
    return float(df["Close"].iloc[-1]), last


# ============================================================================ bootstrap
def cmd_bootstrap(args, cfg) -> int:
    values = L.read_values_csv(args.values_csv)
    if not values:
        print(f"ไม่พบข้อมูลใน {args.values_csv} (ต้องมีคอลัมน์ ticker, value_usd)")
        return 1
    trades = L.read_trades(cfg["paths"]["trades"])
    opened = set(L.opening_datetimes(trades))
    todo = {t: v for t, v in values.items() if t not in opened}
    if not todo:
        print("ทุก ticker มีรายการ OPENING แล้ว — bootstrap ทำได้ครั้งเดียว (trades.csv แก้ย้อนหลังไม่ได้)")
        return 0
    now = _now_et()
    prices, missing = D.fetch_daily(list(todo), cfg, now.date(), refresh=args.refresh)
    rows, report = [], []
    for t, v in todo.items():
        df = prices.get(D.to_yahoo(t))
        if df is None:
            report.append(f"  ✗ {t:<6} ${v:>12,.2f}  ข้าม — {missing.get(D.to_yahoo(t), 'no data')}")
            continue
        px, d = _last_completed_close(df, now)
        shares = v / px
        rows.append({"datetime": dt.datetime.combine(d, dt.time(16, 0), ET).isoformat(timespec="seconds"),
                     "ticker": t, "side": "OPENING", "shares": round(shares, 6), "price": px, "fee": 0,
                     "source_file": os.path.basename(args.values_csv)})
        report.append(f"  ✓ {t:<6} ${v:>12,.2f} ÷ ${px:,.2f} (close {d}) = {shares:,.6f} sh")
    written = L.append_trades(cfg["paths"]["trades"], rows)
    new_meta = L.ensure_meta(cfg["paths"]["meta"], [r["ticker"] for r in rows])
    print("\n".join(report))
    print(f"\nบันทึก OPENING {len(written)} รายการ → {cfg['paths']['trades']}")
    if len(written) < len(todo):
        print(f"ดึงราคาไม่ได้ {len(todo) - len(written)} ตัว — แก้ ticker ใน {args.values_csv} แล้วรัน bootstrap ซ้ำได้ "
              "(ตัวที่บันทึกแล้วจะไม่ถูกซ้ำ)")
    if new_meta:
        print(f"เพิ่ม {len(new_meta)} ticker ใน {cfg['paths']['meta']} — กรอกคอลัมน์ group และ quality ด้วย")
    print("หมายเหตุ: ต้นทุนของรายการ OPENING = ราคาปิดวันนั้น (Dime ไม่ให้ต้นทุนจริง) กำไร/ขาดทุนจึงนับจากวันนี้")
    return 0


# ============================================================================ ingest
def cmd_ingest(args, cfg) -> int:
    from . import slips as S
    if not os.path.isdir(args.folder):
        print(f"ไม่พบโฟลเดอร์ {args.folder}")
        return 1
    s = S.ingest(args.folder, cfg, dry_run=args.dry_run)
    S.print_summary(s, args.dry_run)
    return 0


# ============================================================================ reconcile
def cmd_reconcile(args, cfg) -> int:
    app = L.read_values_csv(args.values_csv)
    pos = L.holdings(L.read_trades(cfg["paths"]["trades"]))
    now = _now_et()
    tickers = sorted(set(app) | set(pos))
    prices, missing = D.fetch_daily(tickers, cfg, now.date(), refresh=args.refresh)
    tol = cfg["reconcile"]["tolerance_pct"]
    lines, flagged = [], 0
    for t in tickers:
        df = prices.get(D.to_yahoo(t))
        px = float(df["Close"].iloc[-1]) if df is not None else None
        sh = pos[t].shares if t in pos else 0.0
        calc = sh * px if px is not None else None
        appv = app.get(t)
        if px is None:
            status, diff = f"⚠️ no price ({missing.get(D.to_yahoo(t), '')})", None
        elif appv is None:
            status, diff = "⚠️ in trades.csv but not in the app (sold without a SELL slip?)", None
        elif sh <= L.EPS:
            status, diff = "⚠️ in the app but not in trades.csv (missing BUY slip?)", None
        else:
            diff = (calc - appv) / appv * 100
            status = "✅" if abs(diff) <= tol else (
                "⚠️ off by more than {:g}% — ".format(tol) + ("app is higher: missing BUY slip / stock dividend / split?"
                                                          if diff < 0 else "app is lower: missing SELL slip / reverse split?"))
        flagged += status != "✅"
        lines.append((t, sh, px, calc, appv, diff, status))
    out_dir = os.path.join(cfg["paths"]["output"], now.date().isoformat())
    os.makedirs(out_dir, exist_ok=True)
    md = [f"# Reconcile — {now:%Y-%m-%d %H:%M} ET", "",
          f"Shares from trades.csv × latest Yahoo price vs values in {os.path.basename(args.values_csv)} "
          f"(tolerance ±{tol:g}%)", "", "| Ticker | Shares | Price | Calculated | In app | Diff | Status |",
          "|---|---:|---:|---:|---:|---:|---|"]
    for t, sh, px, calc, appv, diff, status in lines:
        f = lambda x, d=2: "—" if x is None else f"${x:,.{d}f}"
        md.append(f"| {t} | {sh:,.6f} | {f(px)} | {f(calc)} | {f(appv)} | "
                  f"{'—' if diff is None else f'{diff:+.2f}%'} | {status} |")
    md += ["", "Yahoo's price and the time you copied the app values can differ slightly — copy values after the close."]
    path = os.path.join(out_dir, "reconcile.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    print("\n".join(md[4:]))
    print(f"\n{'ตรงกันทุกตัว' if not flagged else f'ต้องตรวจ {flagged} ตัว'} · บันทึกที่ {path}")
    return 0


# ============================================================================ run
def cmd_run(args, cfg) -> int:
    from . import fundamentals as F
    from . import quality as Q
    from . import report as R
    from . import swing as SW
    from . import universe as U
    from . import validate as V
    now = _now_et()
    as_of = now.date()
    trades = L.read_trades(cfg["paths"]["trades"])
    if not trades:
        print(f"ยังไม่มี {cfg['paths']['trades']} — รัน: python -m portfolio_stage bootstrap dime_values.csv")
        return 1
    pos = L.holdings(trades)
    held = {t: p for t, p in pos.items() if p.shares > L.EPS}
    L.ensure_meta(cfg["paths"]["meta"], list(held))
    meta = L.read_meta(cfg["paths"]["meta"])
    bench = cfg["benchmark"]
    prices, missing = D.fetch_daily(list(held) + [bench], cfg, as_of, refresh=args.refresh)
    spy = prices.get(bench)
    fri = A.last_complete_friday(now)
    spy_w = A.weekly_closes(spy["Close"], fri) if spy is not None else None
    quotes, q_err = D.fetch_quotes(list(held))
    etf_q = set(cfg["etf_qualities"])
    rsc = cfg["rs_score"]
    bar_complete = last_bar_complete(now)
    ures = None
    if getattr(args, "no_universe", False) or not cfg["universe"]["enabled"]:
        rank_why = "universe scan skipped (--no-universe)"
    else:
        print("สแกนหุ้นทั้งตลาดหาโมเมนตัม (ใช้เวลาสักครู่)…")
        ures = U.scan(cfg, as_of, now, bar_complete, refresh=args.refresh)
        rank_why = f"universe scan failed: {ures['error']}" if ures["error"] else None

    rows, rel_ret = {}, {}
    for t, p in held.items():
        yt = D.to_yahoo(t)
        m = meta.get(t, {})
        tier = m.get("quality") or None                    # letter A/B/C/EC/ET from meta.csv
        r = {"shares": round(p.shares, 6), "avg_cost": A.r2(p.avg_cost, 4), "group": m.get("group") or None,
             "tier": tier, "levels": {k: None for k in R.LEVEL_KEYS},
             "quality": None, **{k: None for k in SW.FIELDS}, "ret_rank_top_pct": None,
             "valuation": None, "pe": None, "forward_pe": None, "ps": None, "null_reasons": {}}
        nr = r["null_reasons"]
        df = prices.get(yt)
        if df is None:
            nr["all"] = missing.get(yt, "no price data")
            rows[t] = r
            continue
        lv = A.levels(df, cfg)
        px = A.round_px(lv["p"])                            # = last close in ohlc.json
        n = len(df)
        r["history_days"] = n
        r["history_label"] = f"history {n}d" if n < 200 else None
        lag = int(__import__("numpy").busday_count(df.index[-1].date(), as_of))
        if lag > cfg["data"]["stale_days"]:
            r["stale"] = f"last bar {df.index[-1].date()} ({lag} trading days old)"
        w = A.weekly_closes(df["Close"], fri)
        st = A.compute_stage(w, cfg)
        rs, rs_note = A.mansfield_rs(w, spy_w, cfg) if spy_w is not None else (None, f"benchmark {bench} missing")
        days = A.stage_days(df.index, st["stage_start"])
        r3, r3n = A.ret_vs(df["Close"], spy["Close"], cfg["rs"]["ret_3m_days"]) if spy is not None else (None, "no SPY")
        r6, r6n = A.ret_vs(df["Close"], spy["Close"], cfg["rs"]["ret_6m_days"]) if spy is not None else (None, "no SPY")
        rel_ret[t] = A.weighted_rel_return(df["Close"], spy["Close"], rsc["windows_days"], rsc["weights"]) \
            if spy is not None else None
        tt = A.minervini(df, rs, cfg)
        pc = D.prev_close(df, as_of)
        day_chg = None
        if df.index[-1].date() == as_of and pc:          # session started: the gap is today's open vs prior close
            o = A._f(df["Open"].iloc[-1])
            gap, live_err = ((o / pc - 1) * 100 if o else None), (None if o else "no open price")
            r["gap_basis"] = "open"
            day_chg = (float(df["Close"].iloc[-1]) / pc - 1) * 100
        else:                                             # before the open: latest pre-market trade
            live, live_err = D.latest_trade_today(quotes.get(yt), as_of) if not q_err else (None, q_err)
            gap = (live / pc - 1) * 100 if live and pc else None
            r["gap_basis"] = "pre-market"
        r["day_change_pct"] = A.r2(day_chg)
        sw, sw_why = SW.compute(SW.closed_bars(df, now, bar_complete))
        r.update(sw)
        nr.update(sw_why)
        r["ret_rank_top_pct"] = SW.top_pct(SW.momentum_score(sw), ures["scores"]) if ures else None
        if r["ret_rank_top_pct"] is None:
            if not ures or ures["error"] or not ures["scores"]:
                nr["ret_rank_top_pct"] = rank_why if not ures or ures["error"] else "universe scan returned no stocks"
            else:
                nr["ret_rank_top_pct"] = "needs ret_1m, ret_3m and ret_6m — " + (
                    sw_why.get("ret_6m") or sw_why.get("ret_3m") or sw_why.get("ret_1m") or "missing")
        sup, res = lv["supports"], lv["resistances"]
        r["levels"] = {"S1": A.r2(sup[0]) if sup else None, "S2": A.r2(sup[1]) if len(sup) > 1 else None,
                       "R1": A.r2(res[0]) if res else None, "TP1": A.r2(lv["target"])}
        for k, why in (("S1", "no support level below price"), ("S2", "only one support level below price"),
                       ("R1", "no resistance above price (at/near highs)"), ("TP1", lv["_reasons"].get("target"))):
            if r["levels"][k] is None:
                nr[f"levels.{k}"] = why or "not available"
        r.update({
            "p": px, "value": A.r2(p.shares * px), "hi": A.r2(lv["hi"], 4), "lo": A.r2(lv["lo"], 4),
            "m50": A.r2(lv["m50"], 4), "m200": A.r2(lv["m200"], 4), "rsi": A.r2(lv["rsi"], 1),
            "atr": A.r2(lv["atr"], 4), "stage": None if st["stage"] is None else int(st["stage"]),
            "stage_days": days,
            "stage_since_data_start": st.get("since_data_start"), "stage_pending": st.get("pending"),
            "slope": A.r2(st["slope"]), "sma10w": A.r2(st["sma10w"], 4), "sma30w": A.r2(st["sma30w"], 4),
            "flag": A.stage_flag(st, px, rs, days, cfg), "trend_label": A.trend_label(px, st["sma10w"], st["sma30w"]),
            "tt_score": tt["tt_score"], "tt_available": tt["tt_available"], "tt_checks": tt["tt_checks"],
            "rs_mansfield": A.r2(rs, 4), "ret3m_vs_spy": A.r2(r3), "ret6m_vs_spy": A.r2(r6),
            "support": A.r2(lv["support"], 4), "resistance": A.r2(lv["resistance"], 4), "stop": A.r2(lv["stop"], 4),
            "target": A.r2(lv["target"], 4), "target_basis": lv["target_basis"], "rr": A.r2(lv["rr"]),
            "gap_pct": A.r2(gap), "unrealized_pct": A.r2((px / p.avg_cost - 1) * 100) if p.avg_cost else None,
        })
        nr.update(lv["_reasons"])
        if st["stage"] is None:
            nr["stage"] = st["reason"]
        if rs is None or rs_note:
            nr["rs_mansfield"] = rs_note
        if r3 is None:
            nr["ret3m_vs_spy"] = r3n
        if r6 is None:
            nr["ret6m_vs_spy"] = r6n
        if rel_ret[t] is None:
            nr["rs_score"] = "no SPY data" if spy is None else f"needs > {rsc['windows_days'][0]} days of history"
        if gap is None:
            nr["gap_pct"] = live_err or "no previous close"

        fkeys = ("earnings_date", "mcap", "rev_growth", "ni", "fcf", "shares_chg")
        fu = None
        vkeys = ("valuation", "pe", "forward_pe", "ps")
        if args.no_fundamentals:
            for k in fkeys + ("quality",) + vkeys:
                nr[k] = "skipped (--no-fundamentals)"
        elif tier in etf_q:
            for k in fkeys + ("quality",) + vkeys:
                nr[k] = f"ETF (tier {tier}) — no company fundamentals"
        else:
            e = F.earnings(yt, cfg, as_of)
            r.update(earnings_date=e["earnings_date"], earnings_soon=e["earnings_soon"])
            if e["reason"]:
                nr["earnings_date"] = e["reason"]
            fu = F.fundamentals(yt, cfg)
            r.update({k: fu.get(k) for k in ("mcap", "rev_growth", "rev_growth_q_yoy", "ni", "fcf", "shares_chg")})
            nr.update({k: v for k, v in fu.get("reasons", {}).items() if r.get(k) is None})
            an = F.analyst(yt, cfg, as_of)
            up = A.r2((an["target_mean"] / px - 1) * 100) if an.get("target_mean") and px else None
            label, lwhy = F.value_label(up, an.get("analysts"), cfg)
            r["valuation"] = {"target_mean": A.r2(an.get("target_mean")), "target_low": A.r2(an.get("target_low")),
                              "target_high": A.r2(an.get("target_high")),
                              "analysts": int(an["analysts"]) if an.get("analysts") else None,
                              "recommendation": an.get("recommendation"), "upside_pct": up, "label": label}
            if lwhy:
                nr["valuation.label"] = lwhy if not an.get("error") else f"Yahoo info failed: {an['error']}"
            r.update(pe=A.r2(an.get("pe"), 1), forward_pe=A.r2(an.get("forward_pe"), 1), ps=A.r2(an.get("ps"), 1))
            for k in ("pe", "forward_pe", "ps"):
                if r[k] is None:
                    nr[k] = {"pe": "no trailing P/E (loss-making or not provided)", "forward_pe": "no forward P/E",
                             "ps": "no price/sales from Yahoo"}[k]
            qs, qwhy = Q.scores(fu, cfg)
            nr.update({f"quality.{k}": v for k, v in qwhy.items()})
            if all(v is not None for v in qs.values()):
                r["quality"] = qs
            else:                 # quality is all 5 numbers or null — partial scores kept separately
                r["quality_partial"] = qs
                nr["quality"] = "incomplete: " + ", ".join(k for k, v in qs.items() if v is None)
        # ---- this week's earnings & big news
        r["events"] = {"earnings": [], "news": [], "move_1w": None, "errors": []}
        ev = r["events"]
        if len(df) > 5:
            ev["move_1w"] = A.r2((px / float(df["Close"].iloc[-6]) - 1) * 100)
        skip_etf = tier in etf_q and not cfg["events"]["include_etfs"]
        if not args.no_fundamentals and not skip_etf:
            from . import events as EV
            ev["earnings"], e_err = EV.earnings_events(yt, cfg, as_of)
            if not ev["earnings"] and r.get("earnings_date"):           # fall back to the calendar date
                d = dt.date.fromisoformat(r["earnings_date"])
                lo, hi = EV.week_window(as_of, cfg["events"]["earnings_ahead_days"])
                if lo <= d <= hi:
                    ev["earnings"] = [{"date": r["earnings_date"], "when": "", "eps_est": None, "eps_actual": None,
                                       "surprise_pct": None, "reported": d < as_of}]
            name = (fu or {}).get("name")
            ev["news"], n_err = EV.big_news(yt, name, cfg, as_of)
            ev["errors"] = [e for e in (e_err if not ev["earnings"] else None, n_err) if e]
        rows[t] = r

    scores = A.percentile_scores(rel_ret)
    for t, r in rows.items():
        r["rs_score"] = scores.get(t)
        nr = r["null_reasons"]
        for k in R.KEYS:                          # any remaining null gets a reason
            if k in ("levels", "quality", "earnings_soon", "gaps_open", "first_retest", "valuation") or "all" in nr:
                continue
            if r.get(k) is None and k not in nr:
                n = r.get("history_days") or 0
                nr[k] = f"not computable with {n} days of history" if n < 252 else "not available"

    total = sum(r.get("value") or 0 for r in rows.values())
    for r in rows.values():
        r["weight_pct"] = A.r2(r["value"] / total * 100) if r.get("value") and total else None

    spy_gap = None
    if spy is not None and len(spy) > 1 and spy.index[-1].date() == as_of:
        spy_gap = A.r2((float(spy["Open"].iloc[-1]) / float(spy["Close"].iloc[-2]) - 1) * 100)
    alerts = build_alerts(rows, held, meta, missing, cfg, R.previous_run(cfg["paths"]["output"], as_of.isoformat()),
                          as_of, spy_gap)
    out_dir = os.path.join(cfg["paths"]["output"], as_of.isoformat())
    miss_reasons = {t: missing[D.to_yahoo(t)] for t in held if D.to_yahoo(t) in missing}
    if bench in missing:
        miss_reasons[bench] = missing[bench]
    meta_out = {"as_of": as_of.isoformat(),
                "spy_price": A.round_px(spy["Close"].iloc[-1]) if spy is not None else None,
                "missing_tickers": sorted(miss_reasons),
                "missing_reasons": miss_reasons,
                "generated_at_et": now.isoformat(timespec="seconds"),
                "weekly_bars_through": fri.isoformat(), "benchmark": bench,
                "total_value": A.r2(total), "positions": len(rows)}
    if q_err:
        meta_out["quote_error"] = q_err
    try:                                                   # header card: totals, cash, FX, value history
        from . import summary as SM
        closed = [t for t in L.holdings(trades, include_closed=True) if t not in held]
        hist_prices = dict(prices)
        if closed:
            hist_prices.update(D.fetch_daily(closed, cfg, as_of, refresh=args.refresh)[0])
        fx_t = cfg["portfolio"]["fx_ticker"]
        fx_df = D.fetch_daily([fx_t], cfg, as_of, refresh=args.refresh)[0].get(D.to_yahoo(fx_t))
        meta_out["summary"], meta_out["history"] = SM.build(rows, held, trades, hist_prices, fx_df, cfg, as_of)
    except Exception as e:
        import traceback
        traceback.print_exc()
        meta_out["summary"] = {"null_reasons": {"all": f"summary failed: {type(e).__name__}: {e}"}}
    meta_out["last_bar_complete"] = bar_complete
    if ures is not None:
        meta_out["universe"] = {"definition": ures["definition"], "funnel": ures["funnel"], "error": ures["error"],
                                "seconds": ures.get("seconds")}
    intraday = not meta_out["last_bar_complete"]
    R.write_outputs(out_dir, meta_out, rows, alerts, cfg)
    frames = {t: prices.get(D.to_yahoo(t)) for t in held}
    frames[bench] = spy                                      # SPY always (missing -> validator fails, as it should)
    R.write_ohlc(out_dir, as_of.isoformat(), frames, cfg["ohlc"]["days"])
    R.write_holdings_csv(V._abs(cfg["validator"]["holdings_csv"]), held, meta)
    if ures is not None:
        U.write_report(out_dir, as_of.isoformat(), ures)
        print(f"✓ โมเมนตัม: {len(ures['leaders'])} ตัวผ่านเกณฑ์จาก {ures['funnel'].get('ranked', 0)} หุ้น "
              f"→ momentum.md" if not ures["error"] else f"⚠️ สแกนทั้งตลาดไม่สำเร็จ: {ures['error']}")
    print(f"✓ {len(rows)} positions · total ${total:,.0f} · {len(alerts)} alerts → {out_dir}")
    if intraday:
        print(f"⚠️ last_bar_complete: false — รันก่อน 16:00 ET ของวันทำการ ({now:%Y-%m-%d %H:%M} ET) "
              "แท่งวันนี้ยังไม่จบ ราคาอาจเป็นราคาระหว่างวัน · ห้ามอัปโหลด ให้รันใหม่หลังตลาดปิด (หลัง 05:00 น. เวลาไทย)")
    if miss_reasons:
        print(f"⚠️ ดึงข้อมูลไม่ได้: " + "; ".join(f"{t} ({why})" for t, why in miss_reasons.items()))

    # ---- validate (criteria live in tools/validate_output.py — not changed here)
    vres = V.run(out_dir, cfg)
    if not vres["ok"]:
        V.mark_alerts(out_dir, vres)
    V.print_result(vres)

    if getattr(args, "telegram", False):
        from . import notify as N
        tcfg = cfg.get("telegram", {})
        if alerts or not vres["ok"] or tcfg.get("send_when_no_alerts", True):
            sent = N.send(N.build_message(meta_out, alerts, rows, vres), silent=tcfg.get("silent", False))
            print("✓ ส่ง Telegram แล้ว" if sent else "✗ ส่ง Telegram ไม่สำเร็จ — เช็ก TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ใน .env")
            if ures is not None:
                N.send(U.telegram_text(as_of.isoformat(), ures, cfg["momentum"]["telegram_rows"]),
                       silent=tcfg.get("silent", False))
    return 0 if vres["ok"] else VALIDATION_FAILED


VALIDATION_FAILED = 3


def cmd_momentum(args, cfg) -> int:
    """Whole-market momentum scan only -> output/<date>/momentum.md (+ Telegram with --telegram)."""
    from . import universe as U
    now = _now_et()
    res = U.scan(cfg, now.date(), now, last_bar_complete(now), refresh=args.refresh)
    out_dir = os.path.join(cfg["paths"]["output"], now.date().isoformat())
    U.write_report(out_dir, now.date().isoformat(), res)
    if res["error"]:
        print(f"⚠️ สแกนไม่สำเร็จ: {res['error']}")
    else:
        print(f"✓ {len(res['leaders'])} ตัวผ่านเกณฑ์จาก {res['funnel'].get('ranked', 0)} หุ้น "
              f"({res.get('seconds')} วินาที) → {os.path.join(out_dir, 'momentum.md')}")
    if args.telegram:
        from . import notify as N
        N.send(U.telegram_text(now.date().isoformat(), res, cfg["momentum"]["telegram_rows"]))
    return 1 if res["error"] else 0


def short_date(iso: str | None) -> str:
    try:
        d = dt.date.fromisoformat(iso)
        return f"{d:%b} {d.day}"
    except (TypeError, ValueError):
        return ""


def _px(x) -> str:
    return f"${x:,.2f}"


def news_key(n: dict) -> str:
    import re
    return n.get("url") or re.sub(r"\W+", "", (n.get("title") or "").lower())[:60]


def new_earnings_event(e: dict, old_up: set, old_done: set, as_of: dt.date | None) -> bool:
    """Earnings lines only when something changed: newly in the window, newly reported, or reporting next session."""
    d = e.get("date")
    if e.get("reported"):
        return d not in old_done
    if d not in old_up:
        return True
    if as_of and d:
        nxt = as_of + dt.timedelta(days=1)
        while not is_trading_day(nxt):
            nxt += dt.timedelta(days=1)
        return dt.date.fromisoformat(d) <= nxt              # reminder: reports in the coming session
    return False


def level_events(r: dict, old: dict, cfg: dict) -> list[str]:
    """Support/resistance alerts per level, only for what changed since the previous run:
      broke S1 (vs last run's S1)  ·  broke out over R1  ·  reached TP1
      newly within near_atr_mult x ATR of today's S1 / R1 (wasn't near last run)."""
    k = cfg["levels"]["near_atr_mult"]
    p, a = r.get("p"), r.get("atr")
    lv, olv = r.get("levels") or {}, old.get("levels") or {}
    op, oa = old.get("p"), old.get("atr")
    if p is None:
        return []
    out, crossed = [], set()
    if op is not None:
        s1, r1, tp = olv.get("S1"), olv.get("R1"), olv.get("TP1")
        if s1 is not None and op >= s1 > p:
            nxt = f" → next support S1 {_px(lv['S1'])}" if lv.get("S1") else " → no support below"
            out.append(f"broke support S1 {_px(s1)} (now {_px(p)}){nxt}")
            crossed.add("S1")
        if r1 is not None and op <= r1 < p:
            nxt = lv.get("R1") or lv.get("TP1")
            hit_tp = tp is not None and tp == r1
            out.append(f"broke out above R1 {_px(r1)}{' = TP1' if hit_tp else ''} (now {_px(p)})"
                       + (f" → next {_px(nxt)}" if nxt else ""))
            crossed.add("R1")
            if hit_tp:
                crossed.add("TP1")
        if tp is not None and "TP1" not in crossed and op < tp <= p:
            out.append(f"reached target TP1 {_px(tp)} (now {_px(p)}) — consider taking profit / raising the stop")
            crossed.add("TP1")

    def near(px, lvl, atr):
        return None not in (px, lvl, atr) and atr > 0 and abs(px - lvl) <= k * atr

    for key, word in (("S1", "support"), ("R1", "resistance")):
        lvl = lv.get(key)
        if key in crossed or not near(p, lvl, a) or near(op, olv.get(key), oa):
            continue
        out.append(f"approaching {word} {key} {_px(lvl)} — {abs(p - lvl) / p * 100:.1f}% away "
                   f"({abs(p - lvl) / a:.1f}×ATR)")
    return out


def build_alerts(rows, held, meta, missing, cfg, prev, as_of: dt.date | None = None,
                 spy_gap: float | None = None) -> list[tuple[str, str]]:
    """Alerts only for what is new since the previous run (news, earnings, levels); gaps measured vs SPY's gap."""
    ac = cfg["alerts"]
    prev_date, prev_rows = prev
    out = []
    for t, why in missing.items():
        out.append(("Missing data", f"**{t}** no price data — {why}"))
    for t, p in held.items():
        for w in p.warnings:
            out.append(("Portfolio data", f"**{t}** {w}"))
        m = meta.get(t, {})
        if not m.get("group") or m.get("quality") not in cfg["qualities"] or m.get("group") not in cfg["groups"]:
            out.append(("Portfolio data", f"**{t}** group/quality missing or invalid in meta.csv "
                                          f"(group={m.get('group') or '-'}, quality={m.get('quality') or '-'})"))
    for t, r in rows.items():
        p = r.get("p")
        if r.get("stale"):
            out.append(("Missing data", f"**{t}** stale data: {r['stale']}"))
        old = (prev_rows or {}).get(t) or {}
        if old.get("stage") is not None and r.get("stage") is not None and old["stage"] != r["stage"]:
            out.append(("Stage change", f"**{t}** S{old['stage']} → S{r['stage']} (vs {prev_date})"))
        stop_hit = p is not None and old.get("stop") is not None and p < old["stop"]
        if stop_hit:
            out.append(("Stop broken", f"**{t}** ${p:,.2f} below last stop ${old['stop']:,.2f}"))
        out += [("Support/resistance", f"**{t}** {x}") for x in level_events(r, old, cfg)
                if not (stop_hit and x.startswith("broke support"))]          # the stop line already says it
        mkt = spy_gap or 0.0
        if r.get("gap_pct") is not None and abs(r["gap_pct"] - mkt) >= ac["gap_pct"]:
            if r.get("gap_basis") == "open":
                day = f", closed {r['day_change_pct']:+.1f}%" if r.get("day_change_pct") is not None else ""
                out.append(("Gap", f"**{t}** gapped {r['gap_pct']:+.1f}% at the open (SPY {mkt:+.1f}%{day})"))
            else:
                out.append(("Gap", f"**{t}** {r['gap_pct']:+.1f}% pre-market vs previous close"))
        ev = r.get("events") or {}
        old_ev = old.get("events") or {}
        old_up = {e.get("date") for e in old_ev.get("earnings", []) if not e.get("reported")}
        old_done = {e.get("date") for e in old_ev.get("earnings", []) if e.get("reported")}
        for e in ev.get("earnings", []):
            when = f" ({e['when']})" if e.get("when") else ""
            if not new_earnings_event(e, old_up, old_done, as_of):
                continue
            if e.get("reported"):
                eps = ""
                if e.get("eps_actual") is not None:
                    eps = f" · EPS {e['eps_actual']:g}" + (f" vs {e['eps_est']:g} est" if e.get("eps_est") is not None else "") \
                          + (f" ({e['surprise_pct']:+.1f}%)" if e.get("surprise_pct") is not None else "")
                out.append(("Earnings this week", f"**{t}** reported {short_date(e['date'])}{when}{eps}"))
            else:
                est = f" · est EPS {e['eps_est']:g}" if e.get("eps_est") is not None else ""
                out.append(("Earnings this week", f"**{t}** reports {short_date(e['date'])}{when}{est}"))
        seen = {news_key(n) for n in old_ev.get("news", [])}
        for n in ev.get("news", []):
            if news_key(n) in seen:                       # already sent in the previous run
                continue
            title = n["title"].replace("[", "(").replace("]", ")")
            head = f"[{title}]({n['url']})" if n.get("url") else title
            src = ", ".join(x for x in (n.get("publisher"), short_date(n.get("date"))) if x)
            out.append(("Big news this week", f"**{t}** {head}" + (f" — {src}" if src else "")))
        mv, omv = ev.get("move_1w"), old_ev.get("move_1w")
        flagged_before = omv is not None and abs(omv) >= cfg["events"]["move_pct"] and not old_ev.get("news") \
            and (omv > 0) == ((mv or 0) > 0)
        if mv is not None and abs(mv) >= cfg["events"]["move_pct"] and not ev.get("news") and not flagged_before:
            out.append(("Big news this week", f"**{t}** {mv:+.1f}% in 5 days — no matching news found"))
        for e in ev.get("errors", []):
            out.append(("Missing data", f"**{t}** news/earnings: {e}"))
        if r.get("weight_pct") and r["weight_pct"] > ac["max_weight_pct"]:
            out.append(("Weights", f"**{t}** {r['weight_pct']:.1f}% (over {ac['max_weight_pct']:g}%)"))
    c_total = sum(r.get("weight_pct") or 0 for r in rows.values() if r.get("tier") == "C")
    if c_total > ac["max_quality_c_pct"]:
        names = ", ".join(t for t, r in rows.items() if r.get("tier") == "C")
        out.append(("Weights", f"Quality C total {c_total:.1f}% (over {ac['max_quality_c_pct']:g}%): {names}"))
    return out


# ============================================================================ main
def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):     # emoji/Thai safe even when Task Scheduler redirects to a log
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m portfolio_stage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="analyse holdings")
    p.add_argument("--refresh", action="store_true", help="ignore today's price cache")
    p.add_argument("--no-fundamentals", action="store_true", help="skip earnings/fundamentals lookups")
    p.add_argument("--telegram", action="store_true", help="also send the alerts to Telegram")
    p.add_argument("--no-universe", action="store_true", help="skip the whole-market momentum scan")
    p = sub.add_parser("momentum", help="whole-market momentum scan only")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--telegram", action="store_true")
    p = sub.add_parser("bootstrap", help="first-time OPENING rows from dime_values.csv")
    p.add_argument("values_csv")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("ingest", help="read Dime slips into trades.csv")
    p.add_argument("folder")
    p.add_argument("--dry-run", action="store_true", help="read and validate only; write/move nothing")
    p = sub.add_parser("reconcile", help="compare ledger value with the Dime app")
    p.add_argument("values_csv")
    p.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    fn = {"run": cmd_run, "momentum": cmd_momentum, "bootstrap": cmd_bootstrap, "ingest": cmd_ingest, "reconcile": cmd_reconcile}[args.cmd]
    if args.cmd == "run" and args.telegram:
        try:
            rc = fn(args, cfg)
        except Exception:
            import traceback
            err = traceback.format_exc()
            print(err, file=sys.stderr)
            from . import notify as N
            N.send_failure(err)
            return 1
        if rc and rc != VALIDATION_FAILED:          # validation failures were already sent with the ⛔ banner
            from . import notify as N
            N.send_failure(f"run exited with code {rc} (e.g. portfolio/trades.csv does not exist yet)")
        return rc
    return fn(args, cfg)
