"""Accumulation tracker: falling -> base -> breakout -> (stage 2), on synthetic weekly paths."""
import datetime as dt
import unittest

import numpy as np
import pandas as pd

from portfolio_stage import accumulate as AC
from portfolio_stage.config import DEFAULTS

CFG = {"accumulate": dict(DEFAULTS["accumulate"])}


def daily_from_weekly(closes, wiggle=0.02):
    """Weekly closes -> 5 daily bars per week ending Fridays (last Friday 2026-10-30)."""
    weeks = pd.date_range(end="2026-10-30", periods=len(closes), freq="W-FRI")
    rows = []
    for wk, c in zip(weeks, closes):
        for k in range(5):
            day = wk - pd.Timedelta(days=4 - k)
            rows.append((day, c, c * (1 + wiggle), c * (1 - wiggle), c))
    df = pd.DataFrame(rows, columns=["d", "Open", "High", "Low", "Close"]).set_index("d")
    return df


def path(*legs):
    out = []
    for a, b, n in legs:
        out += list(np.linspace(a, b, n))
    return out


class States(unittest.TestCase):
    fri = dt.date(2026, 10, 30)

    def run_(self, closes, stage=4):
        a, why = AC.evaluate(daily_from_weekly(closes), self.fri, stage, CFG)
        self.assertIsNone(why)
        return a

    def test_falling_while_making_new_lows(self):
        a = self.run_(path((100, 100, 40), (100, 30, 30)))
        self.assertEqual(a["state"], "falling")
        self.assertIn("new low", a["why"])

    def test_base_after_exhaustion_and_no_new_low(self):
        a = self.run_(path((100, 100, 40), (100, 30, 20)) + [31, 32] * 5)          # crash (RSI<30), then dull range
        self.assertLess(a["rsi_w_min"], 30)
        self.assertGreaterEqual(a["weeks_since_low"], 6)
        self.assertEqual(a["state"], "base")

    def test_breakout_with_higher_low_then_failed(self):
        base = path((100, 100, 40), (100, 30, 20)) + [31, 32] * 4
        good = self.run_(base + [35, 38, 34, 36, 39, 41])                         # breaks out, retests, holds
        self.assertEqual(good["state"], "breakout")
        self.assertTrue(good["higher_low"])
        bad = self.run_(base + [35, 38, 34, 31, 30.5, 30.5])                      # falls back into the base
        self.assertNotEqual(bad["state"], "breakout")
        self.assertIn("failed", bad["why"])

    def test_stage2_and_shallow(self):
        self.assertEqual(self.run_(path((50, 100, 80)), stage=2)["state"], "stage2")
        self.assertEqual(self.run_(path((100, 100, 60), (100, 90, 20)), stage=3)["state"], "shallow")

    def test_ema200w_needs_200_weeks(self):
        self.assertIsNone(self.run_(path((100, 100, 80), (100, 30, 20)))["ema200w"])
        a = self.run_(path((100, 100, 200), (100, 50, 20), (50, 52, 10)))
        self.assertIsNotNone(a["ema200w"])
        self.assertAlmostEqual(a["dist_ema200w_pct"], (a["price_w"] / a["ema200w"] - 1) * 100, delta=0.01)

    def test_alert_only_on_change(self):
        self.assertIsNone(AC.alert_text("X", {"state": "base"}, None))
        self.assertIsNone(AC.alert_text("X", {"state": "base"}, {"state": "base"}))
        self.assertIn("falling → **base**", AC.alert_text("X", {"state": "base", "rsi_w": 35}, {"state": "falling"}))


if __name__ == "__main__":
    unittest.main()
