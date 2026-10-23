#!/usr/bin/env python3
"""
Scheduler for both scanners + Telegram reporting.

Schedule (New York time, US trading days only):
  Scanner A  (pre-market gappers)   08:30, 09:00, 09:30 … 14:00   — every 30 min
  Scanner B  (Trend-join Long)      08:35, 09:05, 09:35 … 14:35   — 5 min after each A run,
                                                                     last run before 15:00
  After every Scanner B run, one Telegram message with both sections:
      [Scanner A: Pre-market Gappers] …  [Scanner B: Trend-join Long Hits] …

Modes
  python run_cycle.py --loop        # keep running; fires every slot on time (always-on PC / VPS)
  python run_cycle.py --once        # run the slot that matches "now" (for cron / GitHub Actions)
  python run_cycle.py --slot 10:00  # run a given slot right now (testing)
  add --dry-run to print the Telegram message instead of sending it

Times are New York time, so US daylight-saving changes are handled automatically.
In Bangkok: 08:30 ET = 19:30 (Mar–Nov) / 20:30 (Nov–Mar); 15:00 ET = 02:00 / 03:00.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from zoneinfo import ZoneInfo

import strategy_scanner as B
import telegram_notify as tg

ET = ZoneInfo("America/New_York")

A_START, A_END = dt.time(8, 30), dt.time(14, 0)   # Scanner A window
B_END = dt.time(15, 0)                              # Scanner B runs until 15:00
B_DELAY = dt.timedelta(minutes=5)
ALERT_START, ALERT_END = dt.time(10, 0), dt.time(11, 30)   # fast 5-min Scanner B checks (--alert)
STEP = dt.timedelta(minutes=30)

# NYSE full-day closures (update yearly: nyse.com/markets/hours-calendars)
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def slots_for(day: dt.date) -> list[tuple[dt.datetime, str]]:
    """[(slot_start_ET, kind)] where kind 'A+B' = run A then B 5 min later, 'B' = strategy only."""
    out = []
    t = dt.datetime.combine(day, A_START, ET)
    while True:
        if t.time() <= A_END:
            out.append((t, "A+B"))
        elif (t + B_DELAY).time() < B_END:
            out.append((t, "B"))
        else:
            break
        t += STEP
    return out


def slot_for_now(now: dt.datetime) -> tuple[dt.datetime, str] | None:
    """Slot whose 30-min window contains `now` (tolerates late cron starts)."""
    for start, kind in slots_for(now.date()):
        if start <= now < start + STEP:
            return start, kind
    return None


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
def _sleep_until(target: dt.datetime) -> None:
    while (left := (target - dt.datetime.now(ET)).total_seconds()) > 0:
        time.sleep(min(left, 60))


def _today_scan_file(scan_dir: str, day: dt.date) -> str | None:
    p = os.path.join(scan_dir, f"premarket_scan_{day.isoformat()}.json")
    return p if os.path.exists(p) else None


def _alerted_path(state_dir: str, day: dt.date) -> str:
    return os.path.join(state_dir, f"alerted_{day.isoformat()}.txt")


def read_alerted(state_dir: str, day: dt.date) -> set[str]:
    try:
        with open(_alerted_path(state_dir, day), encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def add_alerted(state_dir: str, day: dt.date, tickers: list[str]) -> None:
    os.makedirs(state_dir, exist_ok=True)
    with open(_alerted_path(state_dir, day), "a", encoding="utf-8") as f:   # one ticker per line (git union-merge)
        for t in tickers:
            f.write(t + "\n")


def send_new_alerts(rep: dict | None, day: dt.date, args) -> list[str]:
    """Send one loud Telegram alert for valid entries not alerted before today. Returns tickers alerted."""
    if not rep:
        return []
    done = read_alerted(args.state_dir, day)
    new = [r for r in rep.get("evaluated", []) if r.get("signal") and r["ticker"] not in done]
    if not new:
        return []
    msg = tg.build_alert(new, rep.get("scan_time_et"))
    if args.dry_run:
        print("----- ALERT (sound on) -----\n" + msg)
    elif not tg.send_telegram(msg, silent=False):
        return []                                  # not delivered → try again next run
    tickers = [r["ticker"] for r in new]
    add_alerted(args.state_dir, day, tickers)
    print(f"Alerted new entries: {', '.join(tickers)}", file=sys.stderr)
    return tickers


def run_alert(now: dt.datetime, args) -> int:
    """Fast check: re-run Scanner B on today's gappers, alert only on brand-new signals."""
    day = now.date()
    a_path = _today_scan_file(args.scan_dir, day)
    if not a_path:
        print("No Scanner A results yet today — nothing to check.")
        return 0
    rep, _ = B.run_strategy(a_path, outdir=args.signal_dir)
    sent = send_new_alerts(rep, day, args)
    print(f"Alert check {now:%H:%M} ET: {rep.get('valid_count', 0)} valid, {len(sent)} new.")
    return 0


def run_cycle(start: dt.datetime, kind: str, args) -> str:
    day = start.date()
    a_payload = a_note = b_report = b_note = None
    a_path = None
    print(f"\n=== {day} slot {start:%H:%M} ET ({kind}) ===", file=sys.stderr)

    # ---- Scanner A
    if kind == "A+B":
        try:
            import premarket_scanner as A
            tickers = args.tickers.split(",") if args.tickers else None
            a_path, a_payload = A.run_scan(tickers=tickers, outdir=args.scan_dir)
        except Exception as e:
            traceback.print_exc()
            a_note = f"Scanner A failed: {e}"
    if a_payload is None:                                   # B-only slot, or A failed
        a_path = _today_scan_file(args.scan_dir, day)
        if a_path:
            with open(a_path, encoding="utf-8") as f:
                a_payload = json.load(f)
            a_note = (a_note + " — showing last successful scan") if a_note else "latest scan (Scanner A finished at 14:00)"

    # ---- Scanner B, 5 minutes after the A slot (no wait for manual --slot test runs)
    if not getattr(args, "no_wait", False):
        print(f"Waiting until {start + B_DELAY:%H:%M} ET to run Scanner B…", file=sys.stderr)
        _sleep_until(start + B_DELAY)
    if a_path:
        try:
            b_report, _ = B.run_strategy(a_path, outdir=args.signal_dir)
            B.write_watchlist([r["ticker"] for r in a_payload.get("results", [])], day, args.watchlist_dir)
        except Exception as e:
            traceback.print_exc()
            b_note = f"Scanner B failed: {e}"
    else:
        b_note = "No Scanner A results today, nothing to check."

    try:
        send_new_alerts(b_report, day, args)
    except Exception:
        traceback.print_exc()
    msg = tg.build_message(a_payload, b_report, a_note, b_note)
    if args.dry_run:
        print("----- REPORT (silent) -----\n" + msg)
    else:
        tg.send_telegram(msg, silent=True)
    return msg


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def read_last_slot(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_last_slot(path: str, key: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key + "\n")


def parse_slot(text: str) -> tuple[int, int]:
    """Accept '10:00', '10', '1000', '10.00', '9:30', ' 10 : 30 '."""
    s = text.strip().replace(" ", "").replace(".", ":")
    if ":" in s:
        h, m = s.split(":", 1)
    elif len(s) in (3, 4) and s.isdigit():
        h, m = s[:-2], s[-2:]
    else:
        h, m = s, "0"
    h, m = int(h), int(m or 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(text)
    return h, m


def next_slot(after: dt.datetime) -> tuple[dt.datetime, str]:
    d = after.date()
    while True:
        if is_trading_day(d):
            for start, kind in slots_for(d):
                if start >= after:
                    return start, kind
        d += dt.timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run both scanners on schedule and report to Telegram")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--loop", action="store_true", help="Run forever, firing every slot")
    mode.add_argument("--once", action="store_true", help="Run the slot matching the current time, then exit")
    mode.add_argument("--slot", help="Run this slot now, e.g. 10:00 (testing)")
    mode.add_argument("--show-schedule", action="store_true")
    mode.add_argument("--alert", action="store_true",
                      help="Fast Scanner B check (10:00–11:30 ET); Telegram only for NEW valid entries")
    ap.add_argument("--dry-run", action="store_true", help="Print the Telegram message instead of sending")
    ap.add_argument("--tickers", help="Limit Scanner A to these tickers (testing)")
    ap.add_argument("--scan-dir", default="scans")
    ap.add_argument("--signal-dir", default="signals")
    ap.add_argument("--watchlist-dir", default="watchlists")
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--force", action="store_true", help="--alert: run even outside 10:00–11:30 ET / non-trading days")
    ap.add_argument("--state-file", default=os.path.join("state", "last_slot.txt"),
                    help="Remembers the last completed slot so repeated triggers don't send twice")
    args = ap.parse_args()

    now = dt.datetime.now(ET)

    if args.show_schedule:
        bkk = ZoneInfo("Asia/Bangkok")
        for s, k in slots_for(now.date()):
            b_at = s + B_DELAY
            a_txt = f"A {s:%H:%M}" if k == "A+B" else "       "
            print(f"{a_txt}  B {b_at:%H:%M} ET   (Bangkok: B {b_at.astimezone(bkk):%H:%M})")
        return 0

    if args.slot:
        try:
            h, m = parse_slot(args.slot)
        except ValueError:
            print(f"Invalid --slot '{args.slot}'. Use HH:MM, e.g. 10:00", file=sys.stderr)
            return 2
        start = dt.datetime.combine(now.date(), dt.time(h, m), ET)
        kind = dict((s.time(), k) for s, k in slots_for(now.date())).get(start.time(), "A+B")
        if not is_trading_day(now.date()):
            print(f"⚠ {now:%A %Y-%m-%d} is not a US trading day (New York time {now:%H:%M}) — "
                  f"there are no bars for today, so expect 0 results. Telegram delivery is still tested.",
                  file=sys.stderr)
        args.no_wait = True
        run_cycle(min(start, now), kind, args)
        return 0

    if args.alert:
        if not args.force:
            if not is_trading_day(now.date()):
                print(f"{now.date()} is not a US trading day — nothing to do.")
                return 0
            if not (ALERT_START <= now.time() <= ALERT_END):
                print(f"{now:%H:%M} ET is outside the {ALERT_START:%H:%M}–{ALERT_END:%H:%M} alert window — nothing to do.")
                return 0
        return run_alert(now, args)

    if args.once:
        if not is_trading_day(now.date()):
            print(f"{now.date()} is not a US trading day — nothing to do.")
            return 0
        slot = slot_for_now(now)
        if not slot:
            print(f"{now:%H:%M} ET is outside the 08:30–15:00 schedule — nothing to do.")
            return 0
        key = slot[0].strftime("%Y-%m-%d %H:%M")
        if read_last_slot(args.state_file) == key:
            print(f"Slot {key} ET already done — skipping duplicate trigger.")
            return 0
        run_cycle(*slot, args)
        write_last_slot(args.state_file, key)
        return 0

    # --loop
    print("Scheduler started (New York time). Ctrl+C to stop.", file=sys.stderr)
    while True:
        start, kind = next_slot(dt.datetime.now(ET) - dt.timedelta(seconds=30))
        print(f"Next run: {start:%Y-%m-%d %H:%M} ET ({kind})", file=sys.stderr)
        _sleep_until(start)
        try:
            run_cycle(start, kind, args)
        except Exception:
            traceback.print_exc()
        time.sleep(31)   # don't re-pick the same slot


if __name__ == "__main__":
    sys.exit(main())
