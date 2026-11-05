"""Sector rotation (weekly, vs SPY) and the stock-detail bundle (statements, margins, insider)."""
import datetime as dt
import unittest

import numpy as np
import pandas as pd

from portfolio_stage import details as DT
from portfolio_stage import sectors as SC
from portfolio_stage.config import DEFAULTS
from tests.test_output_format import OutputFormat


def weekly(series):
    idx = pd.date_range(end="2026-10-30", periods=len(series), freq="W-FRI")
    return pd.Series(series, index=idx, dtype=float)


class Rotation(unittest.TestCase):
    sc = DEFAULTS["sectors"]

    def test_quadrants(self):
        n = 60
        spy = weekly(np.linspace(100, 130, n))
        # accelerating outperformance = Leading (a constant-pace outperformer drifts to Weakening, as in an RRG)
        leader = weekly(np.linspace(100, 130, n) * np.r_[np.ones(40), 1 + 0.0008 * np.arange(20) ** 2])
        laggard = weekly(np.linspace(100, 130, n) * np.r_[np.ones(40), np.linspace(1, 0.8, 20)])  # falling behind
        fading = weekly(np.linspace(100, 130, n) * np.r_[np.ones(40), np.linspace(1, 1.25, 20)])   # ahead, but slowing
        self.assertEqual(SC.rotation(leader, spy, self.sc)["quadrant"], "Leading")
        self.assertEqual(SC.rotation(laggard, spy, self.sc)["quadrant"], "Lagging")
        self.assertEqual(SC.rotation(fading, spy, self.sc)["quadrant"], "Weakening")
        r = SC.rotation(leader, spy, self.sc)
        self.assertGreater(r["ret_4w"], 0)
        self.assertEqual(len(r["tail"]), min(self.sc["tail_weeks"], n - self.sc["ratio_weeks"] - self.sc["momentum_weeks"] + 2))
        self.assertEqual(r["tail"][-1][1:], [r["rs_ratio"], r["rs_momentum"]])

    def test_sector_mapping(self):
        cfg = {"sectors": self.sc, "etf_qualities": ["EC", "ET"]}
        self.assertEqual(SC.sector_of({"sector": "Technology", "industry": "Semiconductors"}, "A", cfg), "SMH")
        self.assertEqual(SC.sector_of({"sector": "Healthcare", "industry": "Drug Manufacturers"}, "A", cfg), "XLV")
        self.assertEqual(SC.sector_of({"sector": "Technology", "industry": "Software"}, "EC", cfg), "other")
        self.assertEqual(SC.sector_of(None, "A", cfg), "other")

    def test_telegram(self):
        sec = {"week_ending": "2026-10-30", "rows": [
            {"etf": "XLV", "name": "Health Care", "quadrant": "Leading", "quadrant_4w_ago": "Improving",
             "ret_4w": 3.2, "ret_13w": 5.0, "portfolio_weight_pct": 6.0},
            {"etf": "XLK", "name": "Technology", "quadrant": "Lagging", "quadrant_4w_ago": "Lagging",
             "ret_4w": -2.0, "ret_13w": -4.0, "portfolio_weight_pct": 30.0}]}
        msg = SC.telegram_text(sec)
        self.assertIn("XLV</b> Health Care: Leading (was Improving)", msg)
        self.assertIn("XLK</b> Technology: Lagging · -2.0 / -4.0 · you 30%", msg)


class Details(unittest.TestCase):
    def test_statement_margins_insider_quality(self):
        q_cols = pd.to_datetime(["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
        inc = pd.DataFrame({c: {"Total Revenue": 100.0, "Operating Income": 20.0, "Gross Profit": 60.0}
                            for c in q_cols})
        cf = pd.DataFrame({c: {"Free Cash Flow": 25.0} for c in q_cols})
        a_cols = pd.to_datetime(["2025-12-31", "2024-12-31"])
        ainc = pd.DataFrame({a_cols[0]: {"Total Revenue": 400.0, "Operating Income": 80.0},
                             a_cols[1]: {"Total Revenue": 300.0, "Operating Income": 30.0}})
        fin = {"quarterly": {"income": DT.statement(inc, DT.LINES["income"]), "cashflow": DT.statement(cf, DT.LINES["cashflow"])},
               "annual": {"income": DT.statement(ainc, DT.LINES["income"])}}
        self.assertEqual(fin["quarterly"]["income"]["periods"][0], "2026-09-30")       # newest first
        self.assertIsNone(fin["quarterly"]["income"]["eps_diluted"])                  # not provided -> null
        m = DT.margins(fin)
        self.assertEqual((m["fcf_margin_pct"], m["op_margin_ttm_pct"], m["gross_margin_ttm_pct"]), (25.0, 20.0, 60.0))
        self.assertEqual((m["op_margin_fy_pct"], m["op_margin_prev_fy_pct"], m["op_margin_change_pts"]), (20.0, 10.0, 10.0))
        ins = pd.DataFrame([
            {"Start Date": "2026-10-01", "Insider": "CEO", "Position": "CEO", "Text": "Sale at price 180.00 per share.",
             "Shares": 1000, "Value": 180000},
            {"Start Date": "2026-09-01", "Insider": "CFO", "Position": "CFO", "Text": "Purchase at price 150.00 per share.",
             "Shares": 100, "Value": 15000},
            {"Start Date": "2026-08-01", "Insider": "Dir", "Position": "Director", "Text": "Stock Award(Grant)",
             "Shares": 50, "Value": 0},
            {"Start Date": "2025-01-01", "Insider": "Old", "Position": "Director", "Text": "Sale at price 50.00 per share.",
             "Shares": 9999, "Value": 1}])
        i = DT.insider(ins, dt.date(2026, 11, 4))
        self.assertEqual((i["buys_6m"], i["sells_6m"], i["net_shares_6m"], i["net_value_6m"]), (1, 1, -900, -165000))
        self.assertEqual([t["type"] for t in i["transactions"]], ["sell", "buy", "other", "sell"])
        self.assertEqual(DT.quality_pct({"growth": 5, "profitability": 5, "consistency": 5, "balance_sheet": 5,
                                         "valuation": 1}), 100.0)
        self.assertIsNone(DT.quality_pct(None))


class Integration(unittest.TestCase):
    def setUp(self):
        self.t = OutputFormat("test_json_layout_and_p_matches_ohlc")
        self.t.setUp()

    def tearDown(self):
        self.t.tearDown()

    def test_run_writes_sectors_and_details(self):
        self.t.details_by = {"NVDA": {"profile": {"long_name": "NVIDIA", "sector": "Technology",
                                                  "industry": "Semiconductors"}}}
        rc, out, doc, *_ = self.t._run(self.t._real_validator(), fundamentals=True)
        self.assertEqual(rc, 0, self.t._read(out, "validate.txt"))
        sec = doc["meta"]["sectors"]
        self.assertEqual(len(sec["rows"]), 12)
        self.assertTrue(all(r["quadrant"] in ("Leading", "Weakening", "Lagging", "Improving") for r in sec["rows"]))
        w = {r["etf"]: r["portfolio_weight_pct"] for r in sec["rows"]}
        nv = doc["tickers"]["NVDA"]
        self.assertAlmostEqual(w["SMH"], nv["weight_pct"], places=1)                 # semis -> SMH
        self.assertAlmostEqual(w["SMH"] + w["XLK"] + (sec["other_weight_pct"] or 0), 100, delta=0.1)
        self.assertEqual(nv["profile"]["long_name"], "NVIDIA")
        self.assertEqual(nv["margins"]["fcf_margin_pct"], 20.0)
        self.assertEqual(nv["quality_pct"], DT.quality_pct(nv["quality"]))


if __name__ == "__main__":
    unittest.main()
