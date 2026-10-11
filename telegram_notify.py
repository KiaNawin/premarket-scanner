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
    return "—" if x is None else f"${x:,.2f}"


def _vol(v) -> str:
    if v is None:
        return "—"
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
            sig_t = (r.get("signal_time_et") or "")[-5:]
            lines += [
                "",
                f"✅ <b>{_e(r['ticker'])}</b> — LONG entry {_money(r.get('entry_price'))} @ {sig_t} ET",
                f"   Y-High {_money(r.get('yesterday_high'))} · PM-High {_money(r.get('premarket_high'))} · "
                f"HOD {_money(r.get('hod_before_signal'))} · SMA200 {_money(r.get('sma200'))}",
                f"   Last {_money(r.get('last_price'))} · Gap {r.get('gap_pct') or 0:+.2f}%",
            ]
            if r.get("catalyst"):
                lines.append(f"   📰 {_e(r['catalyst'])}")
    else:
        lines.append("No valid entries yet.")

    if others:
        parts = []
        for r in others:
            st = r.get("status")
            if st == "WAITING":
                why = "waiting 10:00" if "10:00" in (r.get("reason") or "") else "waiting"
            elif st == "NO_DATA":
                why = "no data"
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


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None, retries: int = 3) -> bool:
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
                                             "disable_web_page_preview": True}, timeout=20)
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
