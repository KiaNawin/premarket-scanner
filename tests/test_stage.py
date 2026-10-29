"""Weinstein stage decisions. Run: python -m unittest discover -s tests -v"""
import datetime as dt
import unittest

import numpy as np
import pandas as pd

from portfolio_stage import analysis as A
from portfolio_stage.config import DEFAULTS

CFG = DEFAULTS


def weekly(values, end="2026-10-23"):
    idx = pd.date_range(end=end, periods=len(values), freq="W-FRI")
    return pd.Series(values, index=idx, dtype=float)


def trend(start, pct_per_week, n):
    return list(start * (1 + pct_per_week / 100) ** np.arange(n))


class StageCases(unittest.TestCase):
    def test_clear_uptrend_is_stage_2(self):
        st = A.compute_stage(weekly(trend(50, 1.5, 60)), CFG)
        self.assertEqual(st["stage"], 2)
        self.assertGreater(st["slope"], CFG["stage"]["slope_t"])

    def test_clear_downtrend_is_stage_4(self):
        st = A.compute_stage(weekly(trend(100, -1.5, 60)), CFG)
        self.assertEqual(st["stage"], 4)
        self.assertLess(st["slope"], -CFG["stage"]["slope_t"])

    def test_reversal_from_4_to_1(self):
        down = trend(100, -1.5, 40)
        flat = [down[-1] * (1 + 0.01 * np.sin(i)) for i in range(40)]       # base building, SMA flattens
        st = A.compute_stage(weekly(down + flat), CFG)
        self.assertEqual(st["stage"], 1)
        self.assertLessEqual(abs(st["slope"]), CFG["stage"]["slope_t"])

    def test_uptrend_flattening_becomes_stage_3(self):
        up = trend(50, 1.5, 40)
        flat = [up[-1] * (1 + 0.01 * np.sin(i)) for i in range(40)]
        st = A.compute_stage(weekly(up + flat), CFG)
        self.assertEqual(st["stage"], 3)

    def test_new_ipo_uses_available_history(self):
        st = A.compute_stage(weekly(trend(20, 2.0, 16)), CFG)            # 16 weeks: SMA from what exists
        self.assertEqual(st["stage"], 2)
        self.assertEqual(st["weeks"], 16)
        short = A.compute_stage(weekly(trend(20, 2.0, 8)), CFG)          # too short: no guess
        self.assertIsNone(short["stage"])
        self.assertIn("weekly bars", short["reason"])

    def test_missing_data(self):
        vals = trend(50, 1.5, 60)
        for i in (5, 6, 20, 41, 55):
            vals[i] = np.nan                                              # gaps inside the series
        st = A.compute_stage(weekly(vals), CFG)
        self.assertEqual(st["stage"], 2)
        self.assertEqual(st["weeks"], 55)
        empty = A.compute_stage(pd.Series(dtype=float), CFG)
        self.assertIsNone(empty["stage"])
        self.assertTrue(empty["reason"])

    def test_one_week_dip_does_not_flip_stage(self):
        vals = trend(50, 1.5, 50)
        dip = vals[:-1] + [vals[-1] * 0.80]                               # one close under SMA30w
        self.assertEqual(A.compute_stage(weekly(dip), CFG)["stage"], 2)
        two = vals[:-2] + [vals[-2] * 0.80, vals[-2] * 0.80]               # confirmed after 2 weeks
        self.assertEqual(A.compute_stage(weekly(two), CFG)["stage"], 3)


class StageHelpers(unittest.TestCase):
    def test_last_complete_friday(self):
        ET = A.dt.timezone(dt.timedelta(hours=-4))
        self.assertEqual(A.last_complete_friday(dt.datetime(2026, 10, 28, 10, 0, tzinfo=ET)), dt.date(2026, 10, 23))
        self.assertEqual(A.last_complete_friday(dt.datetime(2026, 10, 30, 15, 0, tzinfo=ET)), dt.date(2026, 10, 23))
        self.assertEqual(A.last_complete_friday(dt.datetime(2026, 10, 30, 16, 5, tzinfo=ET)), dt.date(2026, 10, 30))
        self.assertEqual(A.last_complete_friday(dt.datetime(2026, 11, 1, 9, 0, tzinfo=ET)), dt.date(2026, 10, 30))

    def test_incomplete_week_is_dropped(self):
        daily = pd.Series(1.0, index=pd.bdate_range("2026-10-01", "2026-10-28"))
        w = A.weekly_closes(daily, dt.date(2026, 10, 23))
        self.assertEqual(w.index[-1].date(), dt.date(2026, 10, 23))

    def test_stage_days_counts_trading_days(self):
        idx = pd.bdate_range("2026-09-01", "2026-10-28")
        self.assertEqual(A.stage_days(idx, pd.Timestamp("2026-10-23")), len(pd.bdate_range("2026-10-19", "2026-10-28")))

    def test_flag_and_trend(self):
        st = {"stage": 2, "sma10w": 95.0, "sma30w": 90.0, "slopes": [3, 3.2, 3.4, 3.5, 3.6]}
        self.assertEqual(A.stage_flag(st, 100.0, 0.1, 40, CFG), "✅")
        self.assertEqual(A.stage_flag(st, 94.0, 0.1, 40, CFG), "⚠️")             # below SMA10w
        self.assertEqual(A.stage_flag(st, 100.0, 0.1, 120, CFG), "⚠️")           # stage too old
        falling = dict(st, slopes=[4.0, 3.5, 3.0, 2.5])
        self.assertEqual(A.stage_flag(falling, 100.0, 0.1, 40, CFG), "⚠️")      # slope down 3 weeks
        self.assertEqual(A.trend_label(100, 95, 90), "Strong Bullish")
        self.assertEqual(A.trend_label(80, 85, 90), "Bearish")


if __name__ == "__main__":
    unittest.main()
