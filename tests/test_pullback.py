"""ข้อ A — pullback-to-support signal, dedup with Scanner B, Thai alert text."""
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pullback_scanner as PB  # noqa: E402
import run_cycle  # noqa: E402
import telegram_notify as TG  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = dt.date(2026, 11, 13)


def daily_uptrend(n=80, start=50.0, step=0.4):
    idx = pd.bdate_range(end=DAY - dt.timedelta(days=1), periods=n)
    c = start + step * np.arange(n)
    return pd.DataFrame({"Open": c - 0.2, "High": c + 0.6, "Low": c - 0.6, "Close": c, "Volume": 1e6}, index=idx)


def bars_5m(path, start="09:30"):
    """path = list of (high, low, close) per 5-min bar from 09:30."""
    t0 = dt.datetime.combine(DAY, dt.time.fromisoformat(start), ET)
    idx = [t0 + dt.timedelta(minutes=5 * i) for i in range(len(path))]
    h, l, c = zip(*path)
    o = [path[0][2]] + list(c[:-1])
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1e5}, index=pd.DatetimeIndex(idx))


def cfg():
    c = dict(PB.DEFAULTS)
    c.update(risk_per_trade_usd=100, max_position_usd=10000)
    return c


class Levels(unittest.TestCase):
    def test_levels_below_close(self):
        lv = PB.daily_levels(daily_uptrend(), DAY, retest=70.0)
        self.assertLess(lv["levels"]["ema9"], lv["y_close"])
        self.assertLess(lv["levels"]["ema21"], lv["levels"]["ema9"])
        self.assertEqual(lv["levels"]["retest"], 70.0)
        self.assertNotIn("retest", PB.daily_levels(daily_uptrend(), DAY, retest=500)["levels"])


class Trigger(unittest.TestCase):
    def setUp(self):
        self.d = daily_uptrend()
        self.lv = PB.daily_levels(self.d, DAY)
        self.e9, self.atr = self.lv["levels"]["ema9"], self.lv["atr14"]

    def path(self, dip_close_below=0.0):
        e = self.e9
        return [(e + 2.0, e + 1.2, e + 1.5),             # 09:30 open above support
                (e + 1.6, e + 0.6, e + 0.8),             # 09:35 pulling back
                (e + 0.9, e + 0.05, e + 0.3),            # 09:40 touches EMA9
                (e + 0.5, e - 0.1, e + 0.2 - dip_close_below),   # 09:45 holds
                (e + 0.9, e + 0.2, e + 0.8),             # 09:50 closes above 09:45 high -> trigger (ends 09:55)
                (e + 1.2, e + 0.7, e + 1.0)]

    def test_signal(self):
        trig = PB.find_trigger(bars_5m(self.path()), self.e9, self.atr, cfg())
        self.assertIsNotNone(trig)
        self.assertEqual(trig["bar_et"], "09:50")
        self.assertEqual(trig["signal_time_et"], "2026-11-13 09:54")
        self.assertAlmostEqual(trig["pullback_low"], self.e9 - 0.1, places=6)

    def test_broken_support_no_signal(self):
        trig = PB.find_trigger(bars_5m(self.path(dip_close_below=0.6 * self.atr + 0.3)), self.e9, self.atr, cfg())
        self.assertIsNone(trig)

    def test_no_touch_no_signal(self):
        e = self.e9 + 3 * self.atr
        p = [(e + 1, e, e + .5), (e + 1.5, e + .4, e + 1.4), (e + 2, e + 1.2, e + 1.9)]
        self.assertIsNone(PB.find_trigger(bars_5m(p), self.e9, self.atr, cfg()))

    def test_forming_bar_ignored(self):
        now = dt.datetime.combine(DAY, dt.time(9, 52), ET)        # 09:50 bar still forming
        self.assertEqual(len(PB.closed_5m(bars_5m(self.path()), DAY, now)), 4)

    def test_evaluate_plan_and_alert_text(self):
        info = {"ticker": "AAA", "ret_3m": 45.0, "ret_rank_top_pct": 3, "base_tight": {"ok": True}}
        now = dt.datetime.combine(DAY, dt.time(10, 5), ET)
        r = PB.evaluate(info, self.d, bars_5m(self.path()), DAY, now, cfg())
        self.assertTrue(r["signal"], r)
        self.assertEqual(r["level_name"], "ema9")
        p = r["plan"]
        self.assertAlmostEqual(p["stop"], round(self.e9 - 0.1, 4), places=3)
        self.assertEqual(set(p["targets"]), {"1R", "2R", "3R"})
        msg = TG.build_alert([], "10:05", pullbacks=[r])
        self.assertIn("🎯 ข้อ A ย่อลงแนวรับ", msg)
        self.assertIn("แนวรับ EMA9", msg)
        self.assertIn("ฐานแน่น", msg)

    def test_stopped_out_not_alerted(self):
        p = self.path() + [(self.e9 + 0.3, self.e9 - 0.4, self.e9 - 0.3)]
        info = {"ticker": "AAA"}
        now = dt.datetime.combine(DAY, dt.time(10, 30), ET)
        r = PB.evaluate(info, self.d, bars_5m(p), DAY, now, cfg())
        self.assertFalse(r["signal"])
        self.assertEqual(r["status"], "STOPPED")


class Watchlist(unittest.TestCase):
    def test_load_and_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "momentum.json")
            with open(src, "w") as f:
                json.dump({"as_of": "2026-11-12", "leaders": [{"ticker": "AAA"}],
                           "strong": [{"ticker": "AAA"}, {"ticker": "BBB", "secret": 1}]}, f)
            PB.export_watchlist(src, dt.date(2026, 11, 12), tmp)
            rows, as_of = PB.load_watchlist(DAY, cfg(), tmp)
            self.assertEqual([r["ticker"] for r in rows], ["AAA", "BBB"])
            self.assertNotIn("secret", rows[1])
            self.assertEqual(as_of, "2026-11-12")
            rows, _ = PB.load_watchlist(dt.date(2026, 11, 20), cfg(), tmp)          # too old
            self.assertEqual(rows, [])


class Dedup(unittest.TestCase):
    """One message per stock per day across Scanner B and ข้อ A."""

    def run_send(self, tmp, rep, pb):
        args = types.SimpleNamespace(state_dir=tmp, dry_run=False)
        sent = []
        with mock.patch.object(run_cycle.tg, "send_telegram", side_effect=lambda m, silent: sent.append(m) or True), \
             mock.patch.object(run_cycle.trade_journal, "record_alerts"):
            run_cycle.send_new_alerts(rep, DAY, args, pb)
        return sent

    def b(self, t):
        return {"ticker": t, "signal": True, "entry_price": 10.0, "signal_time_et": "2026-11-13 10:10",
                "yesterday_high": 9.8, "premarket_high": 9.9, "hod_before_signal": 9.95, "atr14": 0.5}

    def a(self, t):
        return {"ticker": t, "signal": True, "entry_price": 20.0, "signal_time_et": "2026-11-13 10:14",
                "level_name": "ema21", "level": 19.5, "touch_et": "10:00", "prev_bar_high": 19.9,
                "plan": PB.make_plan(20.0, 19.4, cfg())}

    def test_same_run_one_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = self.run_send(tmp, {"evaluated": [self.b("XYZ")]}, {"evaluated": [self.a("XYZ"), self.a("QQQ")]})
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].count("<b>XYZ</b>"), 1)
            self.assertIn("ย่อลงแนวรับด้วย", sent[0])
            self.assertIn("<b>QQQ</b>", sent[0])

    def test_later_signal_recorded_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_send(tmp, {"evaluated": [self.b("XYZ")]}, None)
            sent = self.run_send(tmp, None, {"evaluated": [self.a("XYZ")]})
            self.assertEqual(sent, [])
            self.assertIn("XYZ", run_cycle.read_pb_alerted(tmp, DAY))
            sent = self.run_send(tmp, None, {"evaluated": [self.a("XYZ")]})       # no repeat either
            self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
