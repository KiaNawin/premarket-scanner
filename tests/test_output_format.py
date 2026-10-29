"""v5 output format (tickers/meta, ohlc.json with SPY, quality 0-5, rs_score) and the validator hook."""
import argparse
import csv
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from portfolio_stage import analysis as A
from portfolio_stage import cli
from portfolio_stage import data as D
from portfolio_stage import ledger as L
from portfolio_stage import quality as Q
from tests.test_ledger_slips import cfg_for

PASS_SCRIPT = "import sys\nprint('PASS all checks', sys.argv[1:])\nsys.exit(0)\n"
FAIL_SCRIPT = "import sys\nprint('[OK] spy present')\nprint('[FAIL] freshness: last bar too old')\nsys.exit(1)\n"


def fake_prices(seed, n=760, start=100.0, drift=0.0008):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    c = start * np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
    return pd.DataFrame({"Open": c * 0.998, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": rng.integers(1e5, 1e6, n)}, index=idx)


class OutputFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp)
        for k in ("quality_scores", "rs_score", "ohlc", "validator", "telegram"):
            self.cfg[k] = dict(self.cfg[k])
        self.script = os.path.join(self.tmp, "tools", "validate_output.py")
        self.cfg["validator"].update(script=self.script, holdings_csv=os.path.join(self.tmp, "holdings.csv"))
        L.append_trades(self.cfg["paths"]["trades"], [
            {"datetime": "2026-01-02T21:30:00+07:00", "ticker": t, "side": "BUY", "shares": s, "price": 10, "fee": 0}
            for t, s in (("NVDA", 3), ("CRWD", 2), ("BRK.B", 1))])
        os.makedirs(os.path.dirname(self.cfg["paths"]["meta"]), exist_ok=True)
        with open(self.cfg["paths"]["meta"], "w", encoding="utf-8") as f:
            f.write("ticker,group,quality\nNVDA,semi,A\nCRWD,cyber,B\nBRK.B,fin,A\n")
        self.prices = {"NVDA": fake_prices(1, drift=0.002), "CRWD": fake_prices(2), "BRK-B": fake_prices(3, drift=-0.0005),
                       "SPY": fake_prices(4, start=500)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    @staticmethod
    def _read(out, name):
        with open(os.path.join(out, name), encoding="utf-8") as f:
            return json.load(f) if name.endswith(".json") else f.read()

    def _run(self, script=PASS_SCRIPT, drop=()):
        if script is not None:
            os.makedirs(os.path.dirname(self.script), exist_ok=True)
            with open(self.script, "w") as f:
                f.write(script)
        prices = {k: v for k, v in self.prices.items() if k not in drop}
        missing = {k: "not found on Yahoo" for k in drop}
        args = argparse.Namespace(refresh=False, no_fundamentals=True, telegram=False)
        with mock.patch.object(D, "fetch_daily", return_value=(prices, missing)), \
                mock.patch.object(D, "fetch_quotes", return_value=({}, None)):
            rc = cli.cmd_run(args, self.cfg)
        out = os.path.join(self.cfg["paths"]["output"], cli._now_et().date().isoformat())
        doc, ohlc, alerts = (self._read(out, n) for n in ("portfolio_stage.json", "ohlc.json", "alerts.md"))
        return rc, out, doc, ohlc, alerts

    def test_json_layout_and_p_matches_ohlc(self):
        rc, out, doc, ohlc, alerts = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(set(doc), {"meta", "tickers"})
        self.assertEqual(doc["meta"]["missing_tickers"], [])
        self.assertIsInstance(doc["meta"]["spy_price"], float)
        self.assertEqual(set(doc["tickers"]), {"NVDA", "CRWD", "BRK-B"})
        for t, r in doc["tickers"].items():
            self.assertEqual(list(r)[:13], ["p", "value", "stage", "stage_days", "flag", "m50", "m200", "rsi",
                                            "hi", "lo", "rs_score", "levels", "quality"])
            self.assertIsInstance(r["stage"], int)
            self.assertIn(r["stage"], (1, 2, 3, 4))
            self.assertEqual(set(r["levels"]), {"S1", "S2", "R1", "TP1"})
            self.assertEqual(set(r["quality"]), set(Q.FIELDS))
            self.assertTrue(1 <= r["rs_score"] <= 99)
            last = ohlc["tickers"][t][-1]
            self.assertEqual(r["p"], last[4])                                   # p == last ohlc close
            self.assertAlmostEqual(r["value"], round(r["shares"] * r["p"], 2))
        self.assertGreater(doc["tickers"]["NVDA"]["rs_score"], doc["tickers"]["BRK-B"]["rs_score"])
        self.assertIn("SPY", ohlc["tickers"])
        self.assertEqual(doc["meta"]["spy_price"], ohlc["tickers"]["SPY"][-1][4])
        self.assertEqual(len(ohlc["tickers"]["SPY"]), 500)
        row = ohlc["tickers"]["NVDA"][-1]
        self.assertEqual(len(row), 6)
        self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIsInstance(row[5], int)
        self.assertNotIn("ห้ามอัปโหลด", alerts)
        with open(self.cfg["validator"]["holdings_csv"], encoding="utf-8") as f:
            h = list(csv.DictReader(f))
        self.assertEqual({x["ticker"] for x in h}, {"NVDA", "CRWD", "BRK-B"})
        self.assertEqual(list(h[0]), ["ticker", "shares", "group", "quality"])
        self.assertIn("PASS", self._read(out, "validate.txt"))

    def test_ohlc_dates_are_strings_oldest_first(self):
        df = self.prices["SPY"]
        shuffled = pd.concat([df.iloc[::-1], df.iloc[-1:]])          # reversed + a duplicated last day
        from portfolio_stage import report as R
        rows = R.ohlc_rows(shuffled, 500)
        dates = [r[0] for r in rows]
        self.assertTrue(all(isinstance(x, str) and len(x) == 10 and x[4] == x[7] == "-" for x in dates))
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(dates), len(set(dates)))
        self.assertEqual(dates[-1], df.index[-1].strftime("%Y-%m-%d"))
        self.assertEqual(len(rows), 500)

    def test_validator_fail_puts_banner_first(self):
        rc, out, doc, ohlc, alerts = self._run(FAIL_SCRIPT)
        self.assertEqual(rc, cli.VALIDATION_FAILED)
        first = alerts.splitlines()[0]
        self.assertIn("ห้ามอัปโหลด", first)
        self.assertIn("[FAIL] freshness: last bar too old", alerts)
        self.assertNotIn("[OK] spy present", alerts.split("## Validation FAIL")[1].split("\n\n")[0])

    def test_missing_validator_script_blocks_upload(self):
        rc, out, doc, ohlc, alerts = self._run(script=None)
        self.assertEqual(rc, cli.VALIDATION_FAILED)
        self.assertIn("ห้ามอัปโหลด", alerts.splitlines()[0])
        self.assertIn("not found", alerts.splitlines()[0])

    def test_missing_ticker_listed_and_spy_always_in_ohlc(self):
        rc, out, doc, ohlc, alerts = self._run(drop=("BRK-B",))
        self.assertEqual(doc["meta"]["missing_tickers"], ["BRK-B"])
        self.assertIsNone(doc["tickers"]["BRK-B"]["p"])
        self.assertIn("SPY", ohlc["tickers"])

    def test_previous_run_old_and_new_layout(self):
        from portfolio_stage import report as R
        root = os.path.join(self.tmp, "out")
        for day, doc in (("2026-10-01", {"meta": {}, "NVDA": {"stage": 2}}),
                         ("2026-10-02", {"meta": {}, "tickers": {"NVDA": {"stage": 3}}})):
            os.makedirs(os.path.join(root, day))
            with open(os.path.join(root, day, "portfolio_stage.json"), "w") as f:
                json.dump(doc, f)
        self.assertEqual(R.previous_run(root, "2026-10-02"), ("2026-10-01", {"NVDA": {"stage": 2}}))
        self.assertEqual(R.previous_run(root, "2026-10-03"), ("2026-10-02", {"NVDA": {"stage": 3}}))


class QualityAndRS(unittest.TestCase):
    cfg = {"quality_scores": {"growth_pct": [0, 5, 10, 20, 40], "net_margin_pct": [-10, 0, 5, 15, 25],
                              "debt_to_equity": [25, 50, 100, 150, 250], "pe": [15, 20, 30, 45, 70],
                              "ev_to_sales": [2, 4, 8, 15, 30]}}

    def test_scores_range_and_reasons(self):
        fu = {"rev_growth": 0.25, "ni": 30, "rev_ttm": 100, "ni_quarters": [5, 6, -1, 7, 8, 9, 3, 2],
              "total_cash": 50, "total_debt": 10, "forward_pe": 35}
        s, why = Q.scores(fu, self.cfg)
        self.assertEqual(s, {"growth": 4.0, "profitability": 5.0, "consistency": 4.5, "balance_sheet": 5.0,
                             "valuation": 2.0})
        self.assertEqual(why, {})
        s, why = Q.scores({"rev_growth": -0.1, "trailing_pe": -5, "ev_to_sales": 40}, self.cfg)
        self.assertEqual(s["growth"], 0.0)
        self.assertEqual(s["valuation"], 0.0)
        self.assertIsNone(s["consistency"])
        self.assertIn("consistency", why)
        for v in s.values():
            self.assertTrue(v is None or 0 <= v <= 5)

    def test_percentile(self):
        s = A.percentile_scores({"a": 0.5, "b": 0.1, "c": -0.2, "d": None})
        self.assertIsNone(s["d"])
        self.assertTrue(s["a"] > s["b"] > s["c"])
        self.assertTrue(all(1 <= v <= 99 for v in s.values() if v is not None))


if __name__ == "__main__":
    unittest.main()
