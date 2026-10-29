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

    def _run(self, script=PASS_SCRIPT, drop=(), fundamentals=False):
        if script is not None:
            os.makedirs(os.path.dirname(self.script), exist_ok=True)
            with open(self.script, "w") as f:
                f.write(script)
        prices = {k: v for k, v in self.prices.items() if k not in drop}
        missing = {k: "not found on Yahoo" for k in drop}
        args = argparse.Namespace(refresh=False, no_fundamentals=not fundamentals, telegram=False)
        full = {"rev_growth": 0.3, "ni": 20, "rev_ttm": 100, "ni_quarters": [1, 2, 3, 4], "total_cash": 9,
                "total_debt": 1, "forward_pe": 40, "reasons": {}}
        partial = {"rev_growth": 0.1, "reasons": {}}                    # Yahoo gave almost nothing
        from portfolio_stage import events as EV
        from portfolio_stage import fundamentals as F
        with mock.patch.object(D, "fetch_daily", return_value=(prices, missing)), \
                mock.patch.object(D, "fetch_quotes", return_value=({}, None)), \
                mock.patch.object(F, "earnings", return_value={"earnings_date": None, "earnings_soon": False,
                                                               "reason": "none"}), \
                mock.patch.object(F, "fundamentals", side_effect=lambda yt, cfg: partial if yt == "CRWD" else full), \
                mock.patch.object(EV, "earnings_events", return_value=([], None)), \
                mock.patch.object(EV, "big_news", return_value=([], None)):
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
            self.assertIsNone(r["quality"])                                   # --no-fundamentals
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

    def _real_validator(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "validate_output.py")
        if not os.path.exists(path):
            self.skipTest("tools/validate_output.py not present")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_real_validator_passes_no_fundamentals(self):
        rc, out, doc, ohlc, alerts = self._run(self._real_validator())
        self.assertEqual(rc, 0, self._read(out, "validate.txt"))
        self.assertIn("อัปโหลดได้", self._read(out, "validate.txt"))

    def test_real_validator_passes_with_quality_and_etf(self):
        with open(self.cfg["paths"]["meta"], "a", encoding="utf-8") as f:
            f.write("SPYM,def,EC\n")                                    # an ETF holding
        L.append_trades(self.cfg["paths"]["trades"], [{"datetime": "2026-01-02T21:31:00+07:00", "ticker": "SPYM",
                                                        "side": "BUY", "shares": 1, "price": 10, "fee": 0}])
        self.prices["SPYM"] = fake_prices(9, start=70)
        rc, out, doc, ohlc, alerts = self._run(self._real_validator(), fundamentals=True)
        self.assertEqual(rc, 0, self._read(out, "validate.txt"))
        tk = doc["tickers"]
        self.assertEqual(set(tk["NVDA"]["quality"]), set(Q.FIELDS))
        self.assertTrue(all(0 <= v <= 5 for v in tk["NVDA"]["quality"].values()))
        self.assertIsNone(tk["CRWD"]["quality"])                             # incomplete -> null, never partial nulls
        self.assertEqual(tk["CRWD"]["quality_partial"]["growth"], 3.0)
        self.assertIn("incomplete", tk["CRWD"]["null_reasons"]["quality"])
        self.assertIsNone(tk["SPYM"]["quality"])                             # ETF

    def test_real_validator_fails_when_ticker_missing(self):
        rc, out, doc, ohlc, alerts = self._run(self._real_validator(), drop=("BRK-B",))
        self.assertEqual(rc, cli.VALIDATION_FAILED)
        first = alerts.splitlines()[0]
        self.assertIn("ห้ามอัปโหลด", first)
        self.assertIn("FAIL ขาด ['BRK-B']", alerts)

    def test_tiny_ohlc_glitch_clamped_big_one_kept(self):
        from portfolio_stage import report as R
        idx = pd.to_datetime(["2026-10-27", "2026-10-28"])
        df = pd.DataFrame({"Open": [10.0, 10.0], "High": [10.02, 10.0], "Low": [9.9, 9.0],
                           "Close": [10.03, 12.0], "Volume": [1, 1]}, index=idx)
        rows = R.ohlc_rows(df, 500)
        self.assertEqual(rows[0][2], 10.03)            # high raised to the close (0.1% glitch)
        self.assertEqual(rows[1][2], 10.0)             # 20% inconsistency left for the validator

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


class LevelAlerts(unittest.TestCase):
    cfg = {"levels": {"near_atr_mult": 0.5}}

    def ev(self, p, levels, old=None, atr=2.0):
        return cli.level_events({"p": p, "atr": atr, "levels": levels}, old or {}, self.cfg)

    def test_broke_support_names_next_level(self):
        old = {"p": 101, "atr": 2, "levels": {"S1": 100, "S2": 95, "R1": 110, "TP1": 110}}
        out = self.ev(98, {"S1": 95, "S2": 90, "R1": 100, "TP1": 100}, old)
        self.assertEqual(len(out), 1)
        self.assertIn("broke support S1 $100.00", out[0])
        self.assertIn("next support S1 $95.00", out[0])

    def test_breakout_equal_to_tp1_is_one_alert(self):
        old = {"p": 108, "atr": 2, "levels": {"S1": 100, "S2": 95, "R1": 110, "TP1": 110}}
        out = self.ev(112, {"S1": 110, "S2": 100, "R1": 120, "TP1": 120}, old)
        self.assertEqual(len(out), 1)
        self.assertIn("broke out above R1 $110.00 = TP1", out[0])

    def test_tp1_above_r1(self):
        old = {"p": 118, "atr": 2, "levels": {"S1": 110, "S2": 100, "R1": None, "TP1": 120}}
        out = self.ev(121, {"S1": 110, "S2": 100, "R1": None, "TP1": 127}, old)
        self.assertTrue(any("reached target TP1 $120.00" in x for x in out))

    def test_approach_only_when_new(self):
        lv = {"S1": 99.5, "S2": 95, "R1": 120, "TP1": 120}
        self.assertTrue(any("approaching support S1" in x for x in self.ev(100, lv)))          # first run
        old = {"p": 100.2, "atr": 2, "levels": lv}
        self.assertEqual(self.ev(100, lv, old), [])                                            # already near
        far = {"p": 105, "atr": 2, "levels": lv}
        self.assertTrue(any("approaching support S1" in x for x in self.ev(100, lv, far)))     # newly near

    def test_quiet_in_the_middle(self):
        lv = {"S1": 90, "S2": 85, "R1": 110, "TP1": 110}
        self.assertEqual(self.ev(100, lv, {"p": 101, "atr": 2, "levels": lv}), [])


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
