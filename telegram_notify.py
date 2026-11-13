#!/usr/bin/env python3
"""
Telegram reporting for both scanners.

Message format (one message per scheduled cycle, split automatically if > 4096 chars):

    [Scanner A: Pre-market Gappers]
    <list of gappers>

    [Scanner B: Trend-join Long Hits]
    <trade setups>

Env vars:
    TELEGRAM_BOT_TOKEN   token from @BotFather
    TELEGRAM_CHAT_ID     your chat id (or a group/channel id)

Quick test:
    python telegram_notify.py --test
"""
from __future__ import annotations

import argparse
import html
import os
import sys
import time

import requests

def load_dotenv(path: str | None = None) -> None:
    """Load KEY=VALUE lines from a .env file next to this script (real env vars take priority)."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):
                os.environ[k] = v


load_dotenv()

HEADER_A = "[Scanner A: หุ้น gap ก่อนตลาดเปิด]"
HEADER_B = "[Scanner B: Trend-join Long — สัญญาณทะลุ]"
HEADER_PB = "[ข้อ A: ย่อลงแนวรับ — หุ้นโมเมนตัม]"
TG_LIMIT = 4096
MAX_A_ROWS = 30

_FAIL_LABELS = {
    "price_above_yesterday_high": "< high เมื่อวาน",
    "yesterday_close_above_sma": "< SMA200",
    "price_above_premarket_high": "< high ก่อนเปิด",
    "price_above_high_of_day": "< high ของวัน",
}
_LEVEL_TH = {"ema9": "EMA9", "ema21": "EMA21", "retest": "แนวเดิม (retest)"}
_EXIT_TH = {"stop": "โดน stop", "close": "ปิดตอนตลาดปิด"}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def _money(x) -> str:
    if x is None:
        return "—"
    return f"${x:,.3f}" if abs(x) < 10 else f"${x:,.2f}"     # 3 decimals under $10 so small stops stay visible


def _vol(v) -> str:
    if v is None:
        return "n/a"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return str(v)


def _exit_th(reason: str) -> str:
    reason = str(reason)
    if reason.startswith("target"):
        return "ถึงเป้า " + reason.split(" ", 1)[-1]
    return _EXIT_TH.get(reason, reason)


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def section_a(payload: dict | None, note: str | None = None) -> str:
    lines = [HEADER_A]
    if payload is None:
        lines.append(_e(note or "วันนี้ยังไม่มีผลสแกนก่อนตลาดเปิด"))
        return "\n".join(lines)

    c = payload.get("criteria", {})
    lines.append(f"🕒 สแกน {payload.get('scan_time_et', '?')} ET · {payload.get('scan_date', '')}"
                 + (f" · {_e(note)}" if note else ""))
    lines.append(f"เกณฑ์: gap &gt;{c.get('min_gap_pct', 5):g}% · ราคา &gt;${c.get('min_price', 3):g} · "
                 f"vol ก่อนเปิด &gt;{_vol(c.get('min_premarket_volume', 50000))}")
    f = payload.get("funnel") or {}
    if f:
        steps = [f"{f.get('universe', 0):,} ตัว"]
        if f.get("prefilter") is not None:
            steps.append(f"ขึ้น≥4%: {f['prefilter']}")
        steps += [f"gap&gt;{c.get('min_gap_pct', 5):g}%: {f.get('gap', 0)}", f"ราคา&gt;${c.get('min_price', 3):g}: {f.get('gap_price', 0)}"]
        if f.get("volume_source") != "unavailable":
            steps.append(f"vol ก่อนเปิด: {f.get('passed', 0)}")
        lines.append("🔎 " + " → ".join(steps))
        if f.get("volume_source") == "unavailable":
            lines.append("⚠️ ไม่มีข้อมูล vol ก่อนเปิด — รายชื่อด้านล่างยังไม่ได้กรองด้วย volume")
        elif f.get("volume_source") == "nasdaq":
            lines.append("ℹ️ vol ก่อนเปิดจาก Nasdaq (Yahoo ไม่มีข้อมูล)")
    results = payload.get("results", [])
    if not results:
        lines.append("ไม่มีหุ้นผ่านเกณฑ์")
        return "\n".join(lines)

    lines.append("")
    for i, r in enumerate(results[:MAX_A_ROWS], 1):
        lines.append(f"{i}. <b>{_e(r['ticker'])}</b> {_money(r.get('price'))} "
                     f"<b>{r.get('gap_pct', 0):+.2f}%</b> · vol ก่อนเปิด {_vol(r.get('premarket_volume'))}")
        if r.get("catalyst"):
            lines.append(f"   📰 {_e(r['catalyst'])}")
    if len(results) > MAX_A_ROWS:
        lines.append(f"…และอีก {len(results) - MAX_A_ROWS} ตัว (ดูในไฟล์ JSON)")
    return "\n".join(lines)


def _plan_lines(p: dict) -> list[str]:
    tg = " · ".join(f"{k} {_money(v)}" for k, v in p["targets"].items())
    out = [f"   🛑 Stop {_money(p['stop'])} (−{_money(p['risk_per_share'])}, {p['stop_pct']:.1f}%) · 🎯 {tg}"]
    size = f"   📦 {p['shares']:,} หุ้น ≈ ${p['position_usd']:,.0f} · เสี่ยง ${p['risk_usd']:,.0f}"
    if p.get("size_capped"):
        size += " (จำกัดตามขนาดสูงสุด)"
    out.append(size)
    if p.get("wide_stop"):
        out.append("   ⚠️ Stop กว้าง — ควรลดขนาด")
    return out


def setup_lines(r: dict) -> list[str]:
    """Trade-setup block for one valid Trend-join Long entry (with trade plan if r['plan'] is set)."""
    sig_t = (r.get("signal_time_et") or "")[-5:]
    out = [f"✅ <b>{_e(r['ticker'])}</b> — เข้าซื้อ (ทะลุ) {_money(r.get('entry_price'))} เวลา {sig_t} ET"]
    p = r.get("plan")
    if p:
        out += _plan_lines(p)
        out.append(f"   จุดทะลุ {_money(p['breakout_level'])} ({_e(p['breakout_name'])})"
                   + (f" · ATR {_money(p['atr14'])}" if p.get("atr14") else "")
                   + f" · Gap {r.get('gap_pct') or 0:+.2f}%")
    else:
        out += [
            f"   High เมื่อวาน {_money(r.get('yesterday_high'))} · High ก่อนเปิด {_money(r.get('premarket_high'))} · "
            f"High ของวัน {_money(r.get('hod_before_signal'))} · SMA200 {_money(r.get('sma200'))}",
            f"   ล่าสุด {_money(r.get('last_price'))} · Gap {r.get('gap_pct') or 0:+.2f}%",
        ]
    sp = r.get("swing_plan")
    if sp:
        tg = " · ".join(f"{k} {_money(v)}" for k, v in sp["targets"].items())
        out.append(f"   🧪 ทดสอบเงา: stop {_money(sp['stop'])} (low 5 นาที, {sp['stop_pct']:.1f}%) · {tg} · "
                   f"{sp['shares']:,} หุ้น · ที่เหลือถือจนปิดต่ำกว่า EMA9")
    if r.get("catalyst"):
        out.append(f"   📰 {_e(r['catalyst'])}")
    return out


def pullback_lines(r: dict, header: bool = True) -> list[str]:
    """Block for one ข้อ A signal: momentum stock pulled back to support, 5-min close over the previous high."""
    p = r.get("plan") or {}
    lvl = _LEVEL_TH.get(r.get("level_name"), r.get("level_name"))
    also = [_LEVEL_TH.get(x, x) for x in r.get("also_levels") or []]
    sig_t = r.get("entry_et") or (r.get("signal_time_et") or "")[-5:]
    out = []
    if header:
        out.append(f"🎯 <b>{_e(r['ticker'])}</b> — เข้าซื้อ (ย่อลงแนวรับ) {_money(r.get('entry_price'))} เวลา {sig_t} ET")
    else:
        out.append(f"   🎯 ย่อลงแนวรับด้วย: เข้า {_money(r.get('entry_price'))} เวลา {sig_t} ET")
    out.append(f"   แนวรับ {_e(lvl)} {_money(r.get('level'))}" + (f" (+{', '.join(also)})" if also else "")
               + f" · ย่อมาแตะ {r.get('touch_et', '?')} · แท่ง 5 นาทีปิดเหนือ high แท่งก่อน {_money(r.get('prev_bar_high'))}")
    if p:
        out += _plan_lines(p)
    bits = []
    if r.get("ret_3m") is not None:
        bits.append(f"3 เดือน {r['ret_3m']:+.0f}%")
    if r.get("rank"):
        bits.append(f"top {r['rank']}%")
    bits.append({"base": "ฐานแน่น", "retest": "retest ครั้งแรก"}.get(r.get("setup"), "ยังไม่มี setup รายวัน"))
    if r.get("atr14"):
        bits.append(f"ATR {_money(r['atr14'])}")
    out.append("   " + " · ".join(bits))
    if r.get("last_price") is not None and r.get("entry_price") and abs(r["last_price"] - r["entry_price"]) > 1e-9:
        out.append(f"   ราคาล่าสุด {_money(r['last_price'])} ({r.get('last_bar_et', '')} ET)")
    return out


def section_pb(pb: dict | None) -> str:
    """Short status of ข้อ A for the silent 30-min report."""
    lines = [HEADER_PB]
    if not pb:
        return ""
    if pb.get("error"):
        lines.append(f"⚠️ {_e(pb['error'])}")
        return "\n".join(lines)
    ev = pb.get("evaluated", [])
    sig = [r for r in ev if r.get("signal")]
    near = [r for r in ev if not r.get("signal") and r.get("near_now")]
    lines.append(f"🕒 เช็ก {pb.get('check_time_et', '?')} ET · เฝ้า {pb.get('watch', 0)} ตัว "
                 f"(รายชื่อ {pb.get('as_of')}) · สัญญาณวันนี้ {len(sig)}")
    for r in sig:
        lines.append(f"🎯 {_e(r['ticker'])} {_money(r.get('entry_price'))} @ {r.get('entry_et') or (r.get('signal_time_et') or '')[-5:]} "
                     f"แนวรับ {_e(_LEVEL_TH.get(r.get('level_name'), r.get('level_name')))}")
    if near:
        parts = [f"{_e(r['ticker'])} ({_e(_LEVEL_TH.get(r['nearest']['name'], r['nearest']['name']))} "
                 f"{_money(r['nearest']['level'])})" for r in near[:15]]
        lines.append("ใกล้แนวรับตอนนี้: " + "; ".join(parts))
    return "\n".join(lines)


def _signed_money(x) -> str:
    return "—" if x is None else (f"+${x:,.0f}" if x >= 0 else f"−${-x:,.0f}")


def swing_section(sw: dict | None) -> list[str]:
    """Shadow swing variants vs the intraday plan, on the same alerts."""
    if not sw:
        return []
    out = ["", "<b>🧪 ทดสอบเงา (สวิง) — ขาย 1/3 ที่ 3R, 1/3 ที่ 5R, ที่เหลือจนปิดต่ำกว่า EMA9; ถึง 3R แล้วเลื่อน stop ไปทุน</b>"]
    seen, news = set(), []
    for r in sw.get("new", []):                                                     # one line per new alert
        if (r["date"], r["ticker"]) not in seen:
            seen.add((r["date"], r["ticker"]))
            news.append(r)
    for r in news:
        if r.get("status") == "skip":
            out.append(f"   ❔ {_e(r['ticker'])} {r['date'][5:]} — {_e(r.get('note'))}")
            continue
        out.append(f"   ➕ {_e(r['ticker'])} {r['date'][5:]} stop {_money(_f(r['stop']))} ({_f(r['stop_pct']):.1f}%)")
    for r in sw.get("events", []):
        last = (str(r.get("legs") or "").split(";") or [""])[-1].split("@")[0]
        state = (f"ปิดแล้ว ({_e(r.get('exit_reason'))}) → <b>{float(r['result_r']):+.2f}R</b>" if r.get("status") == "closed"
                 else f"ถึง {_e(last)} · ยังถือ {float(r.get('open_r') or 0):+.2f}R")
        out.append(f"   🔔 {_e(r.get('variant_name') or r.get('variant'))} {_e(r['ticker'])} ({r['date'][5:]}) {state}")
    c = sw.get("compare") or {}
    blk = lambda b: (f"{b['n']} ไม้ · ชนะ {b['win']:.0%} · เฉลี่ย {b['avg']:+.2f}R · รวม <b>{b['total']:+.2f}R</b>"
                     if b.get("n") else "ยังไม่มีไม้")
    if c.get("variants"):
        out.append(f"สัญญาณชุดเดียวกัน {c.get('alerts', 0)} ครั้ง ตั้งแต่ {c.get('since')} (ไม้ที่ยังถือคิดที่ราคาปิด):")
        for name, v in c["variants"].items():
            out.append(f"   {name} {_e(v['label'])}: {blk(v['all_marked'])} · ยังถือ {v['open']['n']}")
        out.append(f"   แผนวันเดียว (2R / ปิดตลาด): {blk(c['intraday_same'])}")
        out.append("<i>ทดสอบเงาเท่านั้น — แผน 🚨 ยังเหมือนเดิม</i>")
    return out


def _scored_lines(scored: list[dict]) -> list[str]:
    out = []
    for r in scored:
        if r.get("result_r") is None:
            out.append(f"❔ <b>{_e(r['ticker'])}</b> — {_e(r.get('exit_reason'))}")
            continue
        reason = str(r["exit_reason"])
        icon = "🎯" if reason.startswith("target") else "🛑" if reason == "stop" else "⏹"
        what = (f"{_exit_th(reason)} {r['exit_time_et']}" if reason != "close"
                else f"ปิดตลาด {_money(_f(r['exit_price']))}")
        out.append(f"{icon} <b>{_e(r['ticker'])}</b> {_money(_f(r['entry']))} → {what} · "
                   f"<b>{float(r['result_r']):+.2f}R</b> ({_signed_money(_f(r['pnl_usd']))})"
                   + (" · เคยถึง 1R" if str(r.get("hit_1r")) in ("True", "true") and not reason.startswith("target") else ""))
    return out


def _stats_lines(st: dict, title: str) -> list[str]:
    if not st.get("trades"):
        return []
    out = [f"<b>{title} ({st['trades']} ไม้ ตั้งแต่ {st['since']})</b>",
           f"ชนะ {st['win_rate']:.0%} · เฉลี่ย {st['avg_r']:+.2f}R · รวม {st['total_r']:+.2f}R ({_signed_money(st['total_pnl'])})",
           f"ไม้ชนะเฉลี่ย {st['avg_win_r']:+.2f}R · ไม้แพ้เฉลี่ย {st['avg_loss_r']:+.2f}R · "
           + (f"Profit factor {st['profit_factor']:.2f}" if st.get("profit_factor") else "Profit factor —")
           + f" · Drawdown สูงสุด {st['max_dd_r']:.2f}R",
           f"ออก: 🎯 ถึงเป้า {st['exits']['target']} · 🛑 stop {st['exits']['stop']} · ⏹ ปิดตลาด {st['exits']['close']}"]
    if st["trades"] > st["last_n"] or st["trades"] >= 5:
        l = st["last"]
        out.append(f"{st['last_n']} ไม้ล่าสุด: ชนะ {l['win_rate']:.0%} · เฉลี่ย {l['avg_r']:+.2f}R")
    if st["trades"] < 30:
        out.append(f"<i>ตัวอย่างยังน้อย ({st['trades']} ไม้) — ควรตัดสินหลัง ~30 ไม้ขึ้นไป</i>")
    return out


def pullback_eod_section(pb: dict | None) -> list[str]:
    if not pb or (not pb.get("scored") and not (pb.get("stats") or {}).get("trades")):
        return []
    out = ["", "<b>🎯 ข้อ A ย่อลงแนวรับ — วันนี้</b>"]
    scored = pb.get("scored") or []
    done = [r for r in scored if r.get("result_r") is not None]
    if scored:
        day_r = sum(float(r["result_r"]) for r in done)
        out.append(f"สัญญาณวันนี้ {len(scored)} · ผลวันนี้ <b>{day_r:+.2f}R</b>")
        out += _scored_lines(scored)
    else:
        out.append("วันนี้ไม่มีสัญญาณ")
    sl = _stats_lines(pb.get("stats") or {}, "📈 ข้อ A ทั้งหมด")
    if sl:
        out += [""] + sl
    return out


def build_eod_message(day: str, scored: list[dict], st: dict, swing: dict | None = None,
                      pullback: dict | None = None) -> str:
    """Daily summary after the close: today's alerts replayed with the plan, plus all-time stats."""
    lines = [f"<b>📊 สรุปประจำวัน {day} — Trend-join Long 🚨</b>"]
    done = [r for r in scored if r.get("result_r") is not None]
    if not scored:
        lines.append("วันนี้ไม่มีสัญญาณ")
    else:
        day_r = sum(float(r["result_r"]) for r in done)
        day_usd = sum(float(r["pnl_usd"] or 0) for r in done)
        lines.append(f"สัญญาณวันนี้ {len(scored)} · ผลวันนี้ <b>{day_r:+.2f}R</b> ({_signed_money(day_usd)})")
        lines.append("")
        lines += _scored_lines(scored)
    sl = _stats_lines(st, "📈 สถิติทั้งหมด")
    if sl:
        lines += [""] + sl
    lines.append("<i>จำลองตามกติกาสัญญาณ: ออกที่ stop / เป้า / ปิดตลาด ไม่รวม slippage และค่าธรรมเนียม</i>")
    lines += pullback_eod_section(pullback)
    lines += swing_section(swing)
    return "\n".join(lines)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


ALERT_HEADER = "🚨 Trend-join Long — สัญญาณเข้าใหม่"
PB_ALERT_HEADER = "🎯 ข้อ A ย่อลงแนวรับ — สัญญาณเข้าใหม่"


def build_alert(new_entries: list[dict], scan_time_et: str | None = None, pullbacks: list[dict] | None = None,
                paired: dict | None = None) -> str:
    """Short, loud message for signals that appeared for the first time today.
    new_entries = Scanner B rows, pullbacks = ข้อ A rows, paired = {ticker: ข้อ A row} for stocks that fired
    both in the same check (shown inside the 🚨 block so each stock gets one message)."""
    pullbacks, paired = pullbacks or [], paired or {}
    lines = []
    if new_entries:
        title = ALERT_HEADER + ("" if len(new_entries) == 1 else f" ×{len(new_entries)}")
        lines.append(f"<b>{title}</b>" + (f"  (เช็กเวลา {scan_time_et} ET)" if scan_time_et else ""))
        for r in new_entries:
            lines += [""] + setup_lines(r)
            if r["ticker"] in paired:
                lines += pullback_lines(paired[r["ticker"]], header=False)
    if pullbacks:
        if lines:
            lines.append("")
        title = PB_ALERT_HEADER + ("" if len(pullbacks) == 1 else f" ×{len(pullbacks)}")
        lines.append(f"<b>{title}</b>" + (f"  (เช็กเวลา {scan_time_et} ET)" if scan_time_et and not new_entries else ""))
        for r in pullbacks:
            lines += [""] + pullback_lines(r)
    return "\n".join(lines)


def section_b(rep: dict | None, note: str | None = None) -> str:
    lines = [HEADER_B]
    if rep is None:
        lines.append(_e(note or "สแกนกลยุทธ์ไม่ได้ทำงาน"))
        return "\n".join(lines)

    valid = [r for r in rep.get("evaluated", []) if r.get("signal")]
    others = [r for r in rep.get("evaluated", []) if not r.get("signal")]
    lines.append(f"🕒 สแกน {rep.get('scan_time_et', '?')} ET · เข้าได้ {len(valid)} / {rep.get('candidates', 0)} ตัว"
                 + (f" · {_e(note)}" if note else ""))

    if valid:
        for r in valid:
            lines += [""] + setup_lines(r)
    else:
        lines.append("ยังไม่มีสัญญาณเข้า")

    if others:
        parts = []
        for r in others:
            st = r.get("status")
            if st == "WAITING":
                why = "รอ 10:00" if "10:00" in (r.get("reason") or "") else "รอ"
            elif st == "SHORT_HISTORY":
                why = f"ข้อมูล {r.get('history_days', '?')} วัน ต้องมี 200 วันสำหรับ SMA"
            elif st == "NO_DATA":
                why = "ไม่มีข้อมูลราคา"
            else:
                fails = [lab for k, lab in _FAIL_LABELS.items() if not (r.get("checks_now") or {}).get(k, True)]
                why = ", ".join(fails) or "ยังไม่มีแท่งปิดที่ผ่าน"
            parts.append(f"{_e(r['ticker'])} ({_e(why)})")
        lines += ["", "กำลังเฝ้า: " + "; ".join(parts)]
    return "\n".join(lines)


def build_message(a_payload: dict | None, b_report: dict | None,
                  a_note: str | None = None, b_note: str | None = None, pb: dict | None = None) -> str:
    msg = section_a(a_payload, a_note) + "\n\n" + section_b(b_report, b_note)
    pbs = section_pb(pb)
    return msg + ("\n\n" + pbs if pbs else "")


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                       # pathological single long line
            chunks.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur.rstrip("\n")); cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None, retries: int = 3,
                  silent: bool = False) -> bool:
    """silent=True delivers without a notification sound (used for the routine 30-min report)."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead:\n" + text, file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chunk in split_message(text):
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                                             "disable_web_page_preview": True,
                                             "disable_notification": silent}, timeout=20)
                if r.status_code == 429:                       # rate limited
                    time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)))
                    continue
                if r.ok:
                    break
                print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"Telegram send failed (attempt {attempt}): {e}", file=sys.stderr)
            time.sleep(2 * attempt)
        else:
            ok = False
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Telegram notifier for the scanners")
    ap.add_argument("--test", action="store_true", help="Send a test message to check token/chat id")
    args = ap.parse_args()
    if args.test:
        sent = send_telegram("✅ เชื่อมต่อบอทสแกนเนอร์แล้ว\n\n" + HEADER_A + "\n(test)\n\n" + HEADER_B + "\n(test)")
        print("Sent." if sent else "Not sent — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(0 if sent else 1)
    ap.print_help()
