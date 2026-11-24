"""Market-cap size filter + 🧾 options shadow test."""
import datetime as dt
import os
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import options_shadow as O  # noqa: E402
import premarket_scanner as A  # noqa: E402
import pullback_scanner as PB  # noqa: E402
import telegram_notify as TG  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = dt.date(2026, 11, 24)


class SizeFilter(unittest.TestCase):
    def test_drops_small_and_unknown(self):
        rows = [{"ticker": "BIG"}, {"ticker": "MID"}, {"ticker": "SML"}, {"ticker": "ETF"}]
        kept = A.size_filter(rows, 2e9, {"BIG": 50e9, "MID": 2.5e9, "SML": 400e6, "ETF": None})
        self.assertEqual([r["ticker"] for r in kept], ["BIG", "MID"])
        self.assertEqual(rows[2]["market_cap"], 400e6)
        self.assertEqual(len(A.size_filter(rows, 0, {})), 4)         # 0 = off

    def test_defaults(self):
        self.assertEqual(A.DEFAULT_MIN_GAP, 3.0)
        self.assertEqual(A.DEFAULT_MIN_MCAP, 2e9)

    def test_section_a_shows_cap(self):
        p = {"scan_time_et": "09:00", "scan_date": "2026-11-24",
             "criteria": {"min_gap_pct": 3, "min_price": 3, "min_premarket_volume": 50000, "min_market_cap": 2e9},
             "funnel": {"universe": 7000, "gap": 40, "gap_price": 30, "mcap": 6, "passed": 4},
             "results": [{"ticker": "NVDA", "price": 190.0, "gap_pct": 3.4, "premarket_volume": 900000,
                          "market_cap": 4.6e12}]}
        s = TG.section_a(p)
        self.assertIn("มูลค่าตลาด ≥$2.0B", s)
        self.assertIn("≥$2.0B: 6", s)
        self.assertIn("$4.60T", s)

    def test_pullback_drops_small(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "momentum_2026-11-24.json"), "w") as f:
                json.dump({"strong": [{"ticker": "BIG"}, {"ticker": "SML"}]}, f)
            res = PB.run(DAY, dt.datetime.combine(DAY, dt.time(10, 30), ET), fetcher=lambda ts: {},
                         watch_dir=tmp, caps_fetcher=lambda ts: {"BIG": 9e9, "SML": 3e8})
            self.assertEqual([r["ticker"] for r in res["evaluated"]], ["BIG"])
            self.assertEqual(res["dropped_small"], 1)
            res = PB.run(DAY, dt.datetime.combine(DAY, dt.time(10, 30), ET), fetcher=lambda ts: {},
                         watch_dir=tmp, caps_fetcher=lambda ts: {})            # lookup failed: keep all
            self.assertEqual(len(res["evaluated"]), 2)


CALLS = [{"contractSymbol": f"XYZ261205C{k:08d}", "strike": k / 1000, "bid": b, "ask": a, "lastPrice": (a + b) / 2,
          "openInterest": 500, "impliedVolatility": 0.5}
         for k, b, a in [(95000, 7.9, 8.1), (100000, 4.9, 5.1), (105000, 2.6, 2.8)]]


def chain(t):
    return ["2026-11-28", "2026-12-05", "2027-03-19"], lambda e: CALLS


class Options(unittest.TestCase):
    def test_pick(self):
        cfg = dict(O.DEFAULTS)
        self.assertEqual(O.pick_expiry(["2026-11-28", "2026-12-05"], DAY, cfg), "2026-12-05")  # 4 days too short
        self.assertEqual(O.pick_strike(CALLS, 102.0)["strike"], 100.0)
        self.assertEqual(O.pick_strike(CALLS, 90.0)["strike"], 95.0)

    def test_bs_sane(self):
        v = O.bs_call(100, 100, 30 / 365, 0.04, 0.5)
        self.assertTrue(5 < v < 7)
        self.assertGreater(O.bs_call(110, 100, 30 / 365, 0.04, 0.5), v)

    def test_record_and_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig_dir, csv_path = os.path.join(tmp, "sig"), os.path.join(tmp, "opt.csv")
            now = dt.datetime.combine(DAY, dt.time(10, 15), ET)
            rec = O.record_alerts(DAY, [{"ticker": "XYZ", "signal_time_et": "2026-11-24 10:12",
                                         "plan": {"entry": 101.0}}], "B", chain=chain, out_dir=sig_dir, now=now)
            self.assertEqual((rec[0]["expiry"], rec[0]["strike"], rec[0]["ask"]), ("2026-12-05", 100.0, 5.1))
            self.assertTrue(rec[0]["liquid"])
            stock = {"ticker": "XYZ", "entry": 101.0, "exit_price": 105.0, "exit_time_et": "11:00",
                     "exit_reason": "target 2R", "result_r": 2.0, "close_price": 104.0}
            out = O.score_day(DAY, {"B": [stock]}, chain=chain, out_dir=sig_dir, trades_csv=csv_path)
            self.assertEqual(len(out), 1)
            self.assertGreater(out[0]["est_pct"], 20)                       # +4% stock -> call well up
            self.assertAlmostEqual(out[0]["close_pct"], (4.9 / 5.1 - 1) * 100, places=0)
            self.assertEqual(O.score_day(DAY, {"B": [stock]}, chain=chain, out_dir=sig_dir, trades_csv=csv_path), [])
            st = O.stats(csv_path)
            self.assertEqual(st["liquid"], 1)
            msg = "\n".join(TG.options_eod_section({"scored": out, "stats": st}))
            self.assertIn("🧾 ออปชันเงา", msg)
            self.assertIn("XYZ call 100", msg)

    def test_no_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = O.record_alerts(DAY, [{"ticker": "NOP", "entry_price": 10}], "A",
                                  chain=lambda t: ([], None), out_dir=tmp)
            self.assertEqual(rec[0]["note"], "หุ้นนี้ไม่มีออปชัน")


if __name__ == "__main__":
    unittest.main()
