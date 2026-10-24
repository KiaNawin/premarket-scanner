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

HEADER_A = "[Scanner A: Pre-market Gappers]"
HEADER_B = "[Scanner B: Trend-join Long Hits]"
TG_LIMIT = 4096
MAX_A_ROWS = 30

_FAIL_LABELS = {
    "price_above_yesterday_high": "< Y-High",
    "yesterday_close_above_sma": "< SMA200",
    "price_above_premarket_high": "< PM-High",
    "price_above_high_of_day": "< HOD",
}


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


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def section_a(payload: dict | None, note: str | None = None) -> str:
    lines = [HEADER_A]
    if payload is None:
        lines.append(_e(note or "No pre-market scan available today."))
        return "\n".join(lines)

    c = payload.get("criteria", {})
    lines.append(f"🕒 Scan {payload.get('scan_time_et', '?')} ET · {payload.get('scan_date', '')}"
                 + (f" · {_e(note)}" if note else ""))
    lines.append(f"Filters: gap &gt;{c.get('min_gap_pct', 5):g}% · price &gt;${c.get('min_price', 3):g} · "
                 f"PM vol &gt;{_vol(c.get('min_premarket_volume', 50000))}")
    f = payload.get("funnel") or {}
    if f:
        steps = [f"{f.get('universe', 0):,} stocks"]
        if f.get("prefilter") is not None:
            steps.append(f"up≥4%: {f['prefilter']}")
        steps += [f"gap&gt;{c.get('min_gap_pct', 5):g}%: {f.get('gap', 0)}", f"price&gt;${c.get('min_price', 3):g}: {f.get('gap_price', 0)}"]
        if f.get("volume_source") != "unavailable":
            steps.append(f"PM vol: {f.get('passed', 0)}")
        lines.append("🔎 " + " → ".join(steps))
        if f.get("volume_source") == "unavailable":
            lines.append("⚠️ Pre-market volume unavailable — list below is NOT volume-filtered")
        elif f.get("volume_source") == "nasdaq":
            lines.append("ℹ️ PM volume from Nasdaq (Yahoo had none)")
    results = payload.get("results", [])
    if not results:
        lines.append("No stocks passed the filters.")
        return "\n".join(lines)

    lines.append("")
    for i, r in enumerate(results[:MAX_A_ROWS], 1):
        lines.append(f"{i}. <b>{_e(r['ticker'])}</b> {_money(r.get('price'))} "
                     f"<b>{r.get('gap_pct', 0):+.2f}%</b> · PM vol {_vol(r.get('premarket_volume'))}")
        if r.get("catalyst"):
            lines.append(f"   📰 {_e(r['catalyst'])}")
    if len(results) > MAX_A_ROWS:
        lines.append(f"…and {len(results) - MAX_A_ROWS} more (see JSON file)")
    return "\n".join(lines)


def setup_lines(r: dict) -> list[str]:
    """Trade-setup block for one valid Trend-join Long entry (with trade plan if r['plan'] is set)."""
    sig_t = (r.get("signal_time_et") or "")[-5:]
    out = [f"✅ <b>{_e(r['ticker'])}</b> — LONG entry {_money(r.get('entry_price'))} @ {sig_t} ET"]
    p = r.get("plan")
    if p:
        tg = " · ".join(f"{k} {_money(v)}" for k, v in p["targets"].items())
        out.append(f"   🛑 Stop {_money(p['stop'])} (−{_money(p['risk_per_share'])}, {p['stop_pct']:.1f}%) · 🎯 {tg}")
        size = f"   📦 {p['shares']:,} sh ≈ ${p['position_usd']:,.0f} · risk ${p['risk_usd']:,.0f}"
        if p.get("size_capped"):
            size += " (capped by max position)"
        out.append(size)
        if p.get("wide_stop"):
            out.append("   ⚠️ Wide stop — consider a smaller size")
        out.append(f"   Breakout {_money(p['breakout_level'])} ({_e(p['breakout_name'])})"
                   + (f" · ATR {_money(p['atr14'])}" if p.get("atr14") else "")
                   + f" · Gap {r.get('gap_pct') or 0:+.2f}%")
    else:
        out += [
            f"   Y-High {_money(r.get('yesterday_high'))} · PM-High {_money(r.get('premarket_high'))} · "
            f"HOD {_money(r.get('hod_before_signal'))} · SMA200 {_money(r.get('sma200'))}",
            f"   Last {_money(r.get('last_price'))} · Gap {r.get('gap_pct') or 0:+.2f}%",
        ]
    if r.get("catalyst"):
        out.append(f"   📰 {_e(r['catalyst'])}")
    return out


def _signed_money(x) -> str:
    return "—" if x is None else (f"+${x:,.0f}" if x >= 0 else f"−${-x:,.0f}")


def build_eod_message(day: str, scored: list[dict], st: dict) -> str:
    """Daily summary after the close: today's alerts replayed with the plan, plus all-time stats."""
    lines = [f"<b>📊 Trend-join Long — Daily Summary {day}</b>"]
    done = [r for r in scored if r.get("result_r") is not None]
    if not scored:
        lines.append("No alerts today.")
    else:
        day_r = sum(float(r["result_r"]) for r in done)
        day_usd = sum(float(r["pnl_usd"] or 0) for r in done)
        lines.append(f"Alerts today: {len(scored)} · Day result: <b>{day_r:+.2f}R</b> ({_signed_money(day_usd)})")
        lines.append("")
        for r in scored:
            if r.get("result_r") is None:
                lines.append(f"❔ <b>{_e(r['ticker'])}</b> — {_e(r.get('exit_reason'))}")
                continue
            reason = str(r["exit_reason"])
            icon = "🎯" if reason.startswith("target") else "🛑" if reason == "stop" else "⏹"
            what = (f"{reason} {r['exit_time_et']}" if reason != "close" else f"close {_money(_f(r['exit_price']))}")
            lines.append(f"{icon} <b>{_e(r['ticker'])}</b> {_money(_f(r['entry']))} → {what} · "
                         f"<b>{float(r['result_r']):+.2f}R</b> ({_signed_money(_f(r['pnl_usd']))})"
                         + (" · hit 1R" if str(r.get("hit_1r")) in ("True", "true") and not reason.startswith("target") else ""))
    if st.get("trades"):
        lines += ["", f"<b>📈 All-time ({st['trades']} trades since {st['since']})</b>",
                  f"Win rate {st['win_rate']:.0%} · Avg {st['avg_r']:+.2f}R · Total {st['total_r']:+.2f}R ({_signed_money(st['total_pnl'])})",
                  f"Avg win {st['avg_win_r']:+.2f}R · Avg loss {st['avg_loss_r']:+.2f}R · "
                  + (f"Profit factor {st['profit_factor']:.2f}" if st.get("profit_factor") else "Profit factor —")
                  + f" · Max drawdown {st['max_dd_r']:.2f}R",
                  f"Exits: 🎯 {st['exits']['target']} · 🛑 {st['exits']['stop']} · ⏹ {st['exits']['close']}"]
        if st["trades"] > st["last_n"] or st["trades"] >= 5:
            l = st["last"]
            lines.append(f"Last {st['last_n']}: {l['win_rate']:.0%} win · {l['avg_r']:+.2f}R avg")
        if st["trades"] < 30:
            lines.append(f"<i>Small sample ({st['trades']} trades) — judge the strategy after ~30+.</i>")
    lines.append("<i>Back-test of the alert rules: stop/target/close exits, no slippage or fees.</i>")
    return "\n".join(lines)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


ALERT_HEADER = "🚨 Trend-join Long — NEW ENTRY"


def build_alert(new_entries: list[dict], scan_time_et: str | None = None) -> str:
    """Short, loud message for signals that appeared for the first time today."""
    title = ALERT_HEADER + ("" if len(new_entries) == 1 else f" ×{len(new_entries)}")
    lines = [f"<b>{title}</b>" + (f"  (checked {scan_time_et} ET)" if scan_time_et else "")]
    for r in new_entries:
        lines += [""] + setup_lines(r)
    return "\n".join(lines)


def section_b(rep: dict | None, note: str | None = None) -> str:
    lines = [HEADER_B]
    if rep is None:
        lines.append(_e(note or "Strategy scan did not run."))
        return "\n".join(lines)

    valid = [r for r in rep.get("evaluated", []) if r.get("signal")]
    others = [r for r in rep.get("evaluated", []) if not r.get("signal")]
    lines.append(f"🕒 Scan {rep.get('scan_time_et', '?')} ET · {len(valid)} valid entr"
                 f"{'y' if len(valid) == 1 else 'ies'} / {rep.get('candidates', 0)} tickers"
                 + (f" · {_e(note)}" if note else ""))

    if valid:
        for r in valid:
            lines += [""] + setup_lines(r)
    else:
        lines.append("No valid entries yet.")

    if others:
        parts = []
        for r in others:
            st = r.get("status")
            if st == "WAITING":
                why = "waiting 10:00" if "10:00" in (r.get("reason") or "") else "waiting"
            elif st == "SHORT_HISTORY":
                why = f"{r.get('history_days', '?')}d history, need 200 for SMA"
            elif st == "NO_DATA":
                why = "no price data"
            else:
                fails = [lab for k, lab in _FAIL_LABELS.items() if not (r.get("checks_now") or {}).get(k, True)]
                why = ", ".join(fails) or "no closed-bar signal yet"
            parts.append(f"{_e(r['ticker'])} ({_e(why)})")
        lines += ["", "Watching: " + "; ".join(parts)]
    return "\n".join(lines)


def build_message(a_payload: dict | None, b_report: dict | None,
                  a_note: str | None = None, b_note: str | None = None) -> str:
    return section_a(a_payload, a_note) + "\n\n" + section_b(b_report, b_note)


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
        sent = send_telegram("✅ Scanner bot connected.\n\n" + HEADER_A + "\n(test)\n\n" + HEADER_B + "\n(test)")
        print("Sent." if sent else "Not sent — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        sys.exit(0 if sent else 1)
    ap.print_help()
