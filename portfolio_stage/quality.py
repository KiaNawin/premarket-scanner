"""
Quality scores 0–5 (half points) from Yahoo fundamentals — only for what Yahoo provides; a score
whose inputs are missing stays null with the reason. Thresholds live in config.yaml (quality_scores).

  growth         revenue growth TTM (falls back to latest-quarter YoY when 8 quarters aren't available)
  profitability  net margin TTM (net income / revenue, last 4 quarters)
  consistency    share of the last (up to 8) quarters with positive net income
  balance_sheet  net cash, else debt-to-equity; current ratio < 1 costs half a point
  valuation      forward P/E, else trailing P/E, else EV/Sales (cheaper = higher)
"""
from __future__ import annotations

FIELDS = ("growth", "profitability", "consistency", "balance_sheet", "valuation")


def _ladder(x: float, steps: list[float], higher_is_better: bool = True) -> float:
    """steps = 5 ascending cut-offs -> score 0..5."""
    n = sum(1 for s in steps if x >= s) if higher_is_better else sum(1 for s in steps if x <= s)
    return float(n)


def scores(fu: dict | None, cfg: dict) -> tuple[dict, dict]:
    q = cfg["quality_scores"]
    out = {k: None for k in FIELDS}
    why: dict[str, str] = {}
    if not fu:
        return out, {k: "no fundamentals" for k in FIELDS}

    g = fu.get("rev_growth") if fu.get("rev_growth") is not None else fu.get("rev_growth_q_yoy")
    if g is not None:
        out["growth"] = _ladder(g * 100, q["growth_pct"])
    else:
        why["growth"] = "no revenue growth from Yahoo"

    if fu.get("ni") is not None and fu.get("rev_ttm"):
        out["profitability"] = _ladder(fu["ni"] / fu["rev_ttm"] * 100, q["net_margin_pct"])
    else:
        why["profitability"] = "needs 4 quarters of revenue and net income"

    nq = fu.get("ni_quarters") or []
    if len(nq) >= 4:
        out["consistency"] = round(sum(1 for x in nq if x > 0) / len(nq) * 10) / 2      # nearest 0.5
    else:
        why["consistency"] = f"needs >= 4 quarters of net income; Yahoo has {len(nq)}"

    cash, debt, de, cr = fu.get("total_cash"), fu.get("total_debt"), fu.get("debt_to_equity"), fu.get("current_ratio")
    bs = None
    if cash is not None and debt is not None and cash >= debt:
        bs = 5.0 if debt == 0 or cash >= 2 * debt else 4.0
    elif de is not None:
        bs = _ladder(de, q["debt_to_equity"], higher_is_better=False)
    if bs is not None:
        if cr is not None and cr < 1:
            bs = max(0.0, bs - 0.5)
        out["balance_sheet"] = bs
    else:
        why["balance_sheet"] = "no cash/debt or debt-to-equity from Yahoo"

    pe = fu.get("forward_pe") if (fu.get("forward_pe") or 0) > 0 else (
        fu.get("trailing_pe") if (fu.get("trailing_pe") or 0) > 0 else None)
    if pe is not None:
        out["valuation"] = _ladder(pe, q["pe"], higher_is_better=False)
    elif fu.get("ev_to_sales") is not None and fu["ev_to_sales"] > 0:
        out["valuation"] = _ladder(fu["ev_to_sales"], q["ev_to_sales"], higher_is_better=False)
    else:
        why["valuation"] = "no positive P/E or EV/Sales (loss-making with no sales multiple?)"
    return out, why
