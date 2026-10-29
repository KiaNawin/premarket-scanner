"""
python -m portfolio_stage ingest slips/

Reads Dime trade slips (png/jpg) with Claude vision and appends confirmed fills to the ledger.
  * only status Filled / สำเร็จ / จับคู่สำเร็จ is recorded (others -> slips/skipped/)
  * anything unreadable or inconsistent is NOT guessed -> slips/review/ with a .reason.txt
  * duplicates (same datetime, ticker, side, shares, price) are skipped
  * a slip dated before the OPENING snapshot is skipped (already inside the opening position)
  * recorded slips -> slips/done/
A slip is all-or-nothing: if any trade on it needs review, nothing from that file is recorded.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shutil
from zoneinfo import ZoneInfo

import requests

from . import ledger as L

API_URL = "https://api.anthropic.com/v1/messages"
IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

PROMPT = """You are reading a trade confirmation slip (screenshot) from the Dime! investing app (Thailand) for US stocks.
Extract every executed or attempted trade shown. Return ONLY JSON, no prose:

{"is_trade_slip": true|false,
 "trades": [
   {"ticker": "AAPL",
    "side": "BUY" | "SELL",
    "shares": 1.234567,
    "price": 123.45,
    "fee": 0.15,
    "amount": 152.48,
    "datetime": "YYYY-MM-DDTHH:MM:SS",
    "status_raw": "text exactly as shown",
    "status": "filled" | "partially_filled" | "pending" | "cancelled" | "rejected" | "unknown",
    "unclear_fields": ["price"],
    "notes": "short"}
 ]}

Rules — follow strictly:
- NEVER guess. If any digit/character of a field is blurry, cut off, ambiguous, or not shown, set that field to null
  AND list its name in unclear_fields.
- side: ซื้อ/Buy -> BUY, ขาย/Sell -> SELL.
- shares: the executed quantity (may be fractional). price: the executed (average) price per share in USD.
- fee: total fees in USD for this trade (commission + SEC/TAF/VAT etc. summed). 0 only if the slip explicitly shows 0.
  If no fee is shown at all, null and add "fee" to unclear_fields.
- amount: total amount shown for the trade (USD), or null if not shown (do not compute it).
- datetime: exactly the date/time printed on the slip, in the slip's local time. Convert Thai Buddhist years
  (e.g. 2569) to Gregorian (2569 - 543 = 2026). Thai month names -> numbers. No timezone suffix.
- status: "filled" only for a completed/matched order (e.g. Filled, สำเร็จ, จับคู่สำเร็จ, Executed).
- If the image is not a trade slip, return {"is_trade_slip": false, "trades": []}.
"""

REQUIRED = ("ticker", "side", "shares", "price", "fee", "datetime", "status")


# ----------------------------------------------------------------------------- vision call
def claude_extract(image_path: str, cfg: dict, api_key: str | None = None, timeout: int = 90) -> dict:
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (add it to .env)")
    ext = os.path.splitext(image_path)[1].lower()
    with open(image_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode()
    body = {"model": cfg["ingest"]["model"], "max_tokens": 1500, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": IMG_TYPES[ext], "data": b64}},
                {"type": "text", "text": PROMPT}]}]}
    r = requests.post(API_URL, json=body, timeout=timeout, headers={
        "x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Claude API HTTP {r.status_code}: {r.text[:300]}")
    text = "".join(c.get("text", "") for c in r.json().get("content", []) if c.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model reply: {text[:200]}")
    return json.loads(m.group(0))


# ----------------------------------------------------------------------------- validation
def _num(x):
    if isinstance(x, bool):
        return None
    try:
        return float(str(x).replace(",", "").replace("$", "").strip()) if x is not None else None
    except ValueError:
        return None


def validate_trade(t: dict, cfg: dict, now: dt.datetime, day_range=None) -> tuple[dict | None, list[str], str | None]:
    """Returns (clean_trade, review_reasons, skip_reason)."""
    reasons = []
    unclear = [u for u in (t.get("unclear_fields") or []) if u]
    if unclear:
        reasons.append(f"unclear on slip: {', '.join(unclear)}")
    status = str(t.get("status") or "").lower()
    if status and status != "filled" and not unclear:
        return None, [], f"status '{t.get('status_raw') or status}' is not Filled"
    missing = [k for k in REQUIRED if t.get(k) in (None, "")]
    if missing:
        reasons.append(f"missing: {', '.join(missing)}")
    ticker = str(t.get("ticker") or "").strip().upper()
    if ticker and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        reasons.append(f"ticker looks wrong: {ticker!r}")
    side = str(t.get("side") or "").upper()
    if side and side not in ("BUY", "SELL"):
        reasons.append(f"side not BUY/SELL: {side!r}")
    shares, price, fee, amount = _num(t.get("shares")), _num(t.get("price")), _num(t.get("fee")), _num(t.get("amount"))
    if t.get("shares") is not None and (shares is None or shares <= 0):
        reasons.append(f"shares invalid: {t.get('shares')!r}")
    if t.get("price") is not None and (price is None or price <= 0):
        reasons.append(f"price invalid: {t.get('price')!r}")
    if t.get("fee") is not None and (fee is None or fee < 0):
        reasons.append(f"fee invalid: {t.get('fee')!r}")
    when = None
    if t.get("datetime"):
        try:
            when = dt.datetime.fromisoformat(str(t["datetime"]).replace("Z", ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=ZoneInfo(cfg["ingest"]["slip_timezone"]))
            if when > now + dt.timedelta(hours=1):
                reasons.append(f"datetime is in the future: {t['datetime']}")
            if when.year < 2000:
                reasons.append(f"datetime year looks wrong: {t['datetime']}")
        except ValueError:
            reasons.append(f"datetime not readable: {t['datetime']!r}")
    if amount and shares and price:
        tol = cfg["ingest"]["amount_tolerance_pct"] / 100
        gross = shares * price
        options = [gross] + ([gross + fee, gross - fee] if fee else [])
        if min(abs(o - amount) / amount for o in options) > tol:
            reasons.append(f"amount {amount} ≠ shares×price {gross:.2f} (±fee) — a digit may be misread")
    if day_range and price and not reasons:
        lo, hi = day_range
        tol = cfg["ingest"]["day_range_tolerance_pct"] / 100
        if not (lo * (1 - tol) <= price <= hi * (1 + tol)):
            reasons.append(f"price {price} outside {ticker}'s range that day ({lo:.2f}–{hi:.2f})")
    if reasons:
        return None, reasons, None
    clean = {"datetime": when.isoformat(timespec="seconds"), "ticker": L.normalize_ticker(ticker), "side": side,
             "shares": shares, "price": price, "fee": fee}
    return clean, [], None


def yahoo_day_range(ticker: str, when: dt.datetime) -> tuple[float, float] | None:
    """That day's (low, high) including extended hours, unadjusted. None if unavailable (check is skipped)."""
    try:
        import yfinance as yf
        et = when.astimezone(ZoneInfo("America/New_York")).date()
        df = yf.Ticker(ticker.replace(".", "-")).history(start=et, end=et + dt.timedelta(days=1), interval="1h",
                                                          prepost=True, auto_adjust=False)
        if df is None or df.empty:
            df = yf.Ticker(ticker.replace(".", "-")).history(start=et, end=et + dt.timedelta(days=1),
                                                              auto_adjust=False)
        if df is None or df.empty:
            return None
        return float(df["Low"].min()), float(df["High"].max())
    except Exception:
        return None


# ----------------------------------------------------------------------------- ingest
def _move(src: str, dest_dir: str, reason_lines: list[str] | None = None, extracted=None) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(src)
    dst = os.path.join(dest_dir, base)
    n = 1
    while os.path.exists(dst):
        stem, ext = os.path.splitext(base)
        dst = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        n += 1
    shutil.move(src, dst)
    if reason_lines:
        with open(dst + ".reason.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(reason_lines) + "\n")
            if extracted is not None:
                f.write("\nWhat was read from the slip:\n" + json.dumps(extracted, indent=2, ensure_ascii=False) + "\n")
    return dst


def ingest(folder: str, cfg: dict, extract=None, day_range=None, dry_run: bool = False,
           now: dt.datetime | None = None) -> dict:
    extract = extract or (lambda p: claude_extract(p, cfg))
    day_range = day_range if day_range is not None else (
        yahoo_day_range if cfg["ingest"]["check_price_vs_day_range"] else (lambda t, w: None))
    now = now or dt.datetime.now(dt.timezone.utc)
    trades_path = cfg["paths"]["trades"]
    existing = L.read_trades(trades_path)
    ids = {r["trade_id"] for r in existing}
    openings = L.opening_datetimes(existing)
    summary = {"added": [], "skipped": [], "review": [], "errors": []}

    files = sorted(f for f in os.listdir(folder)
                   if os.path.isfile(os.path.join(folder, f)) and os.path.splitext(f)[1].lower() in IMG_TYPES)
    for name in files:
        path = os.path.join(folder, name)
        try:
            ex = extract(path)
        except Exception as e:                      # network/API problem: leave the file, retry next run
            summary["errors"].append((name, f"{type(e).__name__}: {e}"))
            continue
        if not ex.get("is_trade_slip") or not ex.get("trades"):
            reasons = ["not recognised as a Dime trade slip (or no trade found)"]
            summary["review"].append((name, reasons))
            if not dry_run:
                _move(path, os.path.join(folder, "review"), reasons, ex)
            continue

        good, review, skips = [], [], []
        for i, t in enumerate(ex["trades"], 1):
            tag = f"trade {i}" if len(ex["trades"]) > 1 else "trade"
            clean, reasons, skip = validate_trade(t, cfg, now)
            if clean and day_range:
                rng = day_range(clean["ticker"], dt.datetime.fromisoformat(clean["datetime"]))
                if rng:
                    clean, reasons, skip = validate_trade(t, cfg, now, rng)
            if skip:
                skips.append(f"{tag}: {skip}")
                continue
            if reasons:
                review += [f"{tag}: {r}" for r in reasons]
                continue
            op = openings.get(clean["ticker"])
            if op and dt.datetime.fromisoformat(clean["datetime"]) <= dt.datetime.fromisoformat(op):
                skips.append(f"{tag}: {clean['ticker']} {clean['datetime']} is before the OPENING snapshot "
                             f"({op}) — already included")
                continue
            tid = "T" + L.trade_key(clean["datetime"], clean["ticker"], clean["side"], clean["shares"],
                                    clean["price"])[:12]
            if tid in ids or tid in {g.get("_id") for g in good}:
                skips.append(f"{tag}: duplicate of {tid} already in trades.csv")
                continue
            clean["_id"] = tid
            clean["source_file"] = name
            good.append(clean)

        if review:
            summary["review"].append((name, review))
            if not dry_run:
                _move(path, os.path.join(folder, "review"), review, ex)
            continue
        if good:
            if not dry_run:
                L.append_trades(trades_path, [{k: v for k, v in g.items() if k != "_id"} for g in good])
                L.ensure_meta(cfg["paths"]["meta"], [g["ticker"] for g in good])
            ids |= {g["_id"] for g in good}
            summary["added"] += [(name, g) for g in good]
            if skips:
                summary["skipped"].append((name, skips))
            if not dry_run:
                _move(path, os.path.join(folder, "done"), skips or None)
        else:
            summary["skipped"].append((name, skips))
            if not dry_run:
                _move(path, os.path.join(folder, "skipped"), skips, ex)
    return summary


def print_summary(s: dict, dry_run: bool = False) -> None:
    head = "DRY RUN — nothing written or moved\n" if dry_run else ""
    print(f"\n{head}เพิ่ม {len(s['added'])} รายการ · ข้าม {sum(len(x[1]) for x in s['skipped'])} รายการ · "
          f"ต้องตรวจเอง {len(s['review'])} ใบ" + (f" · error {len(s['errors'])} ใบ (ไฟล์ยังอยู่ที่เดิม จะลองใหม่รอบหน้า)"
                                                  if s["errors"] else ""))
    for name, g in s["added"]:
        print(f"  + {g['datetime']} {g['side']:4} {g['ticker']:<6} {g['shares']:g} @ {g['price']} fee {g['fee']}  ({name})")
    for name, reasons in s["skipped"]:
        for r in reasons:
            print(f"  - ข้าม  {name}: {r}")
    for name, reasons in s["review"]:
        print(f"  ? ตรวจ  {name}: " + " | ".join(reasons))
    for name, err in s["errors"]:
        print(f"  ! error {name}: {err}")
