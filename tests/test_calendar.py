"""NYSE early closes (13:00 ET) are respected by the scanner schedule, EOD guard and portfolio_stage."""
import datetime as dt
import io
import contextlib
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import run_cycle as R
from portfolio_stage import analysis as A
from portfolio_stage import cli

ET = ZoneInfo("America/New_York")
at = lambda y, m, d, h, mi: dt.datetime(y, m, d, h, mi, tzinfo=ET)


class Calendar(unittest.TestCase):
    def test_market_close(self):
        self.assertEqual(R.market_close(dt.date(2026, 11, 27)), dt.time(13, 0))     # day after Thanksgiving
        self.assertEqual(R.market_close(dt.date(2026, 12, 24)), dt.time(13, 0))     # Christmas Eve
        self.assertEqual(R.market_close(dt.date(2026, 11, 25)), dt.time(16, 0))
        self.assertFalse(R.is_trading_day(dt.date(2026, 11, 26)))                   # Thanksgiving

    def test_schedule(self):
        normal = R.slots_for(dt.date(2026, 11, 25))
        self.assertEqual((normal[-1][0].strftime("%H:%M"), normal[-1][1]), ("14:30", "B"))
        self.assertEqual(max(t for t, k in normal if k == "A+B").strftime("%H:%M"), "14:00")
        early = R.slots_for(dt.date(2026, 11, 27))
        self.assertEqual(max(t for t, k in early if k == "A+B").strftime("%H:%M"), "12:00")
        self.assertTrue(all((t + R.B_DELAY).time() < dt.time(13, 0) for t, _ in early))
        self.assertIsNone(R.slot_for_now(at(2026, 11, 27, 13, 40)))

    def test_eod_runs_after_an_early_close(self):
        out = io.StringIO()
        with mock.patch.object(R, "run_eod", return_value=0) as eod, \
                mock.patch.object(R.dt, "datetime", wraps=dt.datetime) as fake, \
                mock.patch.object(sys, "argv", ["run_cycle.py", "--eod", "--dry-run"]), contextlib.redirect_stdout(out):
            fake.now = lambda tz=None: at(2026, 11, 27, 13, 20)
            R.main()
        eod.assert_called_once()

    def test_portfolio_stage(self):
        self.assertTrue(cli.last_bar_complete(at(2026, 11, 27, 13, 30), dt.date(2026, 11, 27)))
        self.assertFalse(cli.last_bar_complete(at(2026, 11, 27, 12, 30), dt.date(2026, 11, 27)))
        self.assertEqual(cli.expected_session(at(2026, 11, 27, 13, 30)), dt.date(2026, 11, 27))
        self.assertEqual(A.last_complete_friday(at(2026, 11, 27, 14, 0)), dt.date(2026, 11, 27))
        self.assertEqual(A.last_complete_friday(at(2026, 11, 27, 12, 0)), dt.date(2026, 11, 20))


if __name__ == "__main__":
    unittest.main()
