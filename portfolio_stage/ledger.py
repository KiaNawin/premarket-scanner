"""
Append-only trade ledger (portfolio/trades.csv) and holdings derived from it.

Rows are only ever appended — never edited or deleted. Holdings and average cost are recomputed
from every row on each run. Sides:
  OPENING  starting position from the bootstrap (dime_values.csv), priced at the last close
  BUY      adds shares, cost += shares x price + fee
  SELL     removes shares at the average cost (realised P&L = proceeds - fee - avg cost x shares)
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
from dataclasses import dataclass, field

COLUMNS = ["trade_id", "datetime", "ticker", "side", "shares", "price", "fee", "source_file"]
SIDES = ("OPENING", "BUY", "SELL")
EPS = 1e-6


def normalize_ticker(t: str) -> str:
    return str(t).strip().upper().replace(".", "-").replace("/", "-")


def trade_key(datetime_iso: str, ticker: str, side: str, shares: float, price: float) -> str:
    """Duplicate-detection hash of (datetime, ticker, side, shares, price)."""
    ts = dt.datetime.fromisoformat(datetime_iso)
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.timezone.utc)
    raw = f"{ts.strftime('%Y-%m-%dT%H:%M:%S')}|{normalize_ticker(ticker)}|{side.upper()}|{float(shares):.6f}|{float(price):.4f}"
    return hashlib.sha256(raw.encode()).hexdigest()


def read_trades(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def existing_ids(path: str) -> set[str]:
    return {r["trade_id"] for r in read_trades(path)}


def append_trades(path: str, trades: list[dict]) -> list[dict]:
    """Append rows (dicts with datetime/ticker/side/shares/price/fee/source_file). Skips duplicates.
    Returns the rows actually written."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    seen = existing_ids(path)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    written = []
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        for t in trades:
            side = t["side"].upper()
            if side not in SIDES:
                raise ValueError(f"bad side {side!r}")
            h = trade_key(t["datetime"], t["ticker"], side, t["shares"], t["price"])
            tid = "T" + h[:12]
            if tid in seen:
                continue
            row = {"trade_id": tid, "datetime": t["datetime"], "ticker": normalize_ticker(t["ticker"]),
                   "side": side, "shares": f"{float(t['shares']):.6f}".rstrip("0").rstrip("."),
                   "price": f"{float(t['price']):.4f}", "fee": f"{float(t.get('fee') or 0):.2f}",
                   "source_file": t.get("source_file", "")}
            w.writerow(row)
            seen.add(tid)
            written.append(row)
    return written


@dataclass
class Position:
    ticker: str
    shares: float = 0.0
    cost: float = 0.0             # total cost of the shares still held (incl. buy fees)
    realized: float = 0.0
    fees: float = 0.0
    first_trade: str | None = None
    opening_datetime: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def avg_cost(self) -> float | None:
        return self.cost / self.shares if self.shares > EPS else None


def holdings(trades: list[dict], include_closed: bool = False) -> dict[str, Position]:
    pos: dict[str, Position] = {}
    for r in sorted(trades, key=lambda r: r["datetime"]):
        t = normalize_ticker(r["ticker"])
        p = pos.setdefault(t, Position(t))
        side = r["side"].upper()
        sh, px, fee = float(r["shares"]), float(r["price"]), float(r.get("fee") or 0)
        p.first_trade = p.first_trade or r["datetime"]
        p.fees += fee
        if side in ("OPENING", "BUY"):
            if side == "OPENING":
                p.opening_datetime = r["datetime"]
            p.shares += sh
            p.cost += sh * px + fee
        elif side == "SELL":
            if sh > p.shares + EPS:
                p.warnings.append(f"SELL {sh} on {r['datetime'][:10]} exceeds held {p.shares:.6f} — missing slip?")
            avg = p.cost / p.shares if p.shares > EPS else px
            sold = min(sh, p.shares) if p.shares > EPS else sh
            p.realized += sh * px - fee - avg * sold
            p.cost -= avg * sold
            p.shares -= sh
            if abs(p.shares) < EPS:
                p.shares, p.cost = 0.0, 0.0
    return {t: p for t, p in pos.items() if include_closed or p.shares > EPS or p.warnings}


def opening_datetimes(trades: list[dict]) -> dict[str, str]:
    return {normalize_ticker(r["ticker"]): r["datetime"] for r in trades if r["side"].upper() == "OPENING"}


# ----------------------------------------------------------------------------- meta (group / quality)
META_COLUMNS = ["ticker", "group", "quality"]


def read_meta(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {normalize_ticker(r["ticker"]): {"group": (r.get("group") or "").strip(),
                                                "quality": (r.get("quality") or "").strip().upper()}
                for r in csv.DictReader(f) if r.get("ticker")}


def ensure_meta(path: str, tickers: list[str]) -> list[str]:
    """Add blank rows for tickers missing from meta.csv (existing rows are left untouched)."""
    meta = read_meta(path)
    missing = [t for t in tickers if normalize_ticker(t) not in meta]
    if not missing:
        return []
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(META_COLUMNS)
        for t in missing:
            w.writerow([normalize_ticker(t), "", ""])
    return missing


def read_values_csv(path: str) -> dict[str, float]:
    """dime_values.csv: ticker, value_usd (extra columns ignored; '$' and ',' allowed)."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            keys = {k.strip().lower(): v for k, v in r.items() if k}
            t, v = keys.get("ticker"), keys.get("value_usd")
            if not t or v in (None, ""):
                continue
            out[normalize_ticker(t)] = float(str(v).replace("$", "").replace(",", "").strip())
    return out
