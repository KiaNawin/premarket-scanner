"""
Weekly sector rotation — where money is flowing, from price alone (no paid data needed).

For each sector ETF (config `sectors.etfs`) on weekly closes through the last complete Friday:
  ret_1w / ret_4w / ret_13w   ETF return minus SPY return over 1, 4, 13 weeks (percentage points)
  rs_ratio                    100 x RS / SMA(RS, ratio_weeks)          RS = ETF / SPY
  rs_momentum                 100 x rs_ratio / SMA(rs_ratio, momentum_weeks)
  quadrant                    Leading (ratio >= 100, momentum >= 100) · Weakening (>= 100, < 100)
                              Lagging (< 100, < 100) · Improving (< 100, >= 100)
  tail                        [[week, rs_ratio, rs_momentum], ...] for the last `tail_weeks` weeks
  portfolio_weight_pct        share of your stock value in that sector (Yahoo sector / industry of each holding)
This is a simplified relative-rotation view (same idea as an RRG chart, not the licensed JdK formula).
"""
from __future__ import annotations

import pandas as pd

from . import analysis as A

QUADRANTS = {(True, True): "Leading", (True, False): "Weakening", (False, False): "Lagging", (False, True): "Improving"}


def quadrant(ratio: float | None, mom: float | None) -> str | None:
    if ratio is None or mom is None:
        return None
    return QUADRANTS[(ratio >= 100, mom >= 100)]


def _ret(w: pd.Series, n: int) -> float | None:
    return (float(w.iloc[-1]) / float(w.iloc[-1 - n]) - 1) * 100 if len(w) > n and w.iloc[-1 - n] else None


def rotation(etf_w: pd.Series, spy_w: pd.Series, sc: dict) -> dict:
    df = pd.concat([etf_w, spy_w], axis=1, keys=["e", "s"], sort=True).dropna()
    out = {"ret_1w": None, "ret_4w": None, "ret_13w": None, "rs_ratio": None, "rs_momentum": None,
           "quadrant": None, "quadrant_4w_ago": None, "tail": [], "weeks": len(df)}
    if len(df) < 2:
        return out
    for k, n in (("ret_1w", 1), ("ret_4w", 4), ("ret_13w", 13)):
        e, s = _ret(df["e"], n), _ret(df["s"], n)
        out[k] = A.r2(e - s) if e is not None and s is not None else None
    rs = df["e"] / df["s"]
    ratio = 100 * rs / rs.rolling(sc["ratio_weeks"]).mean()
    mom = 100 * ratio / ratio.rolling(sc["momentum_weeks"]).mean()
    both = pd.concat([ratio, mom], axis=1, keys=["r", "m"]).dropna()
    if both.empty:
        return out
    out["rs_ratio"], out["rs_momentum"] = A.r2(both["r"].iloc[-1]), A.r2(both["m"].iloc[-1])
    out["quadrant"] = quadrant(out["rs_ratio"], out["rs_momentum"])
    if len(both) > 4:
        out["quadrant_4w_ago"] = quadrant(float(both["r"].iloc[-5]), float(both["m"].iloc[-5]))
    tail = both.iloc[-sc["tail_weeks"]:]
    out["tail"] = [[ts.strftime("%Y-%m-%d"), A.r2(r), A.r2(m)] for ts, (r, m) in tail.iterrows()]
    return out


def sector_of(fu: dict | None, tier: str | None, cfg: dict) -> str:
    """ETF key a holding belongs to: semis -> SMH, else Yahoo sector -> sector ETF, ETFs/unknown -> 'other'."""
    sc = cfg["sectors"]
    if tier in cfg["etf_qualities"]:
        return "other"
    ind = ((fu or {}).get("industry") or "").lower()
    if any(k in ind for k in sc["semis_industry_keywords"]):
        return sc["semis_etf"]
    return sc["yahoo_sector_to_etf"].get((fu or {}).get("sector") or "", "other")


def build(prices: dict, spy_daily, fri, rows: dict, sector_by_ticker: dict, cfg: dict) -> dict:
    sc = cfg["sectors"]
    spy_w = A.weekly_closes(spy_daily["Close"], fri) if spy_daily is not None else None
    total = sum(r.get("value") or 0 for r in rows.values())
    weights: dict[str, float] = {}
    for t, r in rows.items():
        if r.get("value"):
            k = sector_by_ticker.get(t, "other")
            weights[k] = weights.get(k, 0) + r["value"]
    out = []
    for etf, name in sc["etfs"].items():
        df = prices.get(etf)
        row = {"etf": etf, "name": name, "portfolio_weight_pct": A.r2(weights.get(etf, 0) / total * 100) if total else None}
        if df is None or spy_w is None:
            row.update(quadrant=None, reason="no price data")
        else:
            row.update(rotation(A.weekly_closes(df["Close"], fri), spy_w, sc))
        out.append(row)
    order = {"Leading": 0, "Improving": 1, "Weakening": 2, "Lagging": 3, None: 4}
    out.sort(key=lambda x: (order[x.get("quadrant")], -(x.get("ret_4w") or -99)))
    return {"week_ending": fri.isoformat(), "benchmark": cfg["benchmark"],
            "method": f"RS = ETF/SPY weekly; rs_ratio = 100*RS/SMA{sc['ratio_weeks']}; "
                      f"rs_momentum = 100*rs_ratio/SMA{sc['momentum_weeks']}(rs_ratio)",
            "rows": out,
            "other_weight_pct": A.r2(weights.get("other", 0) / total * 100) if total else None}


def telegram_text(sec: dict) -> str:
    import html
    icons = {"Leading": "🟢", "Improving": "🔵", "Weakening": "🟡", "Lagging": "🔴", None: "⚪"}
    lines = [f"<b>🧭 Sector rotation — week ending {sec['week_ending']}</b>",
             "<i>vs SPY · 4w / 13w excess return · your weight</i>", ""]
    for r in sec["rows"]:
        moved = (f" (was {r['quadrant_4w_ago']})" if r.get("quadrant_4w_ago") and r["quadrant_4w_ago"] != r.get("quadrant")
                 else "")
        f = lambda x: "—" if x is None else f"{x:+.1f}"
        w = f" · you {r['portfolio_weight_pct']:.0f}%" if r.get("portfolio_weight_pct") else ""
        lines.append(f"{icons.get(r.get('quadrant'))} <b>{html.escape(r['etf'])}</b> {html.escape(r['name'])}: "
                     f"{r.get('quadrant') or '—'}{moved} · {f(r.get('ret_4w'))} / {f(r.get('ret_13w'))}{w}")
    return "\n".join(lines)
