"""Header-card data: valuation labels, cash + FX totals, actual/backcast history, snapshots."""
import csv
import os
import unittest

from portfolio_stage import fundamentals as F
from tests.test_output_format import OutputFormat


class Summary(unittest.TestCase):
    def setUp(self):
        self.t = OutputFormat("test_json_layout_and_p_matches_ohlc")
        self.t.setUp()
        with open(self.t.cfg["paths"]["cash"], "w", encoding="utf-8") as f:
            f.write("account,currency,amount,as_of,note\nDime! Save,THB,\"3,300\",2026-11-02,\nDime! USD,USD,100,2026-11-02,\n")

    def tearDown(self):
        self.t.tearDown()

    def test_totals_cash_fx_and_history(self):
        rc, out, doc, ohlc, alerts = self.t._run(self.t._real_validator())
        self.assertEqual(rc, 0, self.t._read(out, "validate.txt"))
        sm, hist = doc["meta"]["summary"], doc["meta"]["history"]
        fx = sm["fx_usdthb"]
        self.assertAlmostEqual(sm["cash_usd"], round(3300 / fx + 100, 2), places=1)
        self.assertAlmostEqual(sm["total_usd"], round(sm["stocks_usd"] + sm["cash_usd"], 2), places=1)
        self.assertAlmostEqual(sm["total_thb"], round(sm["total_usd"] * fx, 2), delta=1)
        self.assertEqual(sm["stocks_usd"], doc["meta"]["total_value"])
        self.assertAlmostEqual(sm["unrealized_usd"], sm["stocks_usd"] - sm["cost_usd"], places=1)
        self.assertIsNotNone(sm["day_change_usd"])
        # ledger starts 2026-01-02 in the fixture -> actual history from that day, cost = 3*10+2*10+1*10
        self.assertEqual(sm["history_start"], hist["actual"][0][0])
        self.assertTrue(hist["actual"][0][0] >= "2026-01-02")
        self.assertEqual(hist["actual"][-1][2], 60.0)
        self.assertEqual(hist["actual"][-1][1], sm["stocks_usd"])
        self.assertEqual(sm["month_basis"], "actual")
        self.assertEqual(hist["backcast"][-1][1], sm["stocks_usd"])
        self.assertEqual(hist["backcast"][-1][2], 100.0)
        self.assertEqual(hist["snapshots"][-1][0], doc["meta"]["as_of"])
        # second run the same day replaces the snapshot instead of adding one
        self.t._run(self.t._real_validator())
        with open(self.t.cfg["paths"]["history"], encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_missing_cash_file_is_a_reason_not_an_error(self):
        os.remove(self.t.cfg["paths"]["cash"])
        rc, out, doc, *_ = self.t._run()
        sm = doc["meta"]["summary"]
        self.assertIsNone(sm["cash_usd"])
        self.assertIn("cash.csv not found", sm["null_reasons"]["cash_usd"])
        self.assertEqual(sm["total_usd"], sm["stocks_usd"])

    def test_valuation_label_and_multiples(self):
        self.t.analyst = {"NVDA": {"target_mean": 1000.0, "analysts": 40, "recommendation": "buy", "pe": 30.0,
                                   "forward_pe": 25.0, "ps": 15.0, "error": None},
                          "CRWD": {"target_mean": None, "analysts": None, "error": None}}
        rc, out, doc, *_ = self.t._run(self.t._real_validator(), fundamentals=True)
        self.assertEqual(rc, 0, self.t._read(out, "validate.txt"))
        n, c = doc["tickers"]["NVDA"], doc["tickers"]["CRWD"]
        self.assertAlmostEqual(n["valuation"]["upside_pct"], round((1000 / n["p"] - 1) * 100, 2), places=1)
        self.assertEqual((n["pe"], n["forward_pe"], n["ps"]), (30.0, 25.0, 15.0))
        self.assertIsNotNone(n["valuation"]["label"])
        self.assertIsNone(c["valuation"]["label"])
        self.assertIn("no analyst price target", c["null_reasons"]["valuation.label"])

    def test_label_thresholds(self):
        cfg = self.t.cfg
        cases = [(45, "Bargain"), (30, "Bargain"), (12, "Undervalued"), (0, "Fair"), (-10, "Fair"),
                 (-20, "Overvalued"), (-40, "Premium")]
        for up, want in cases:
            self.assertEqual(F.value_label(up, 10, cfg)[0], want, up)
        self.assertIsNone(F.value_label(50, 2, cfg)[0])                     # too few analysts


if __name__ == "__main__":
    unittest.main()
