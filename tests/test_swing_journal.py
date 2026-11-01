"""Shadow swing plan: 5-min-low stop, 1/3 at 3R, 1/3 at 5R, rest until a close below EMA9."""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

import pandas as pd

import swing_journal as SJ
import telegram_notify as TG
import trade_plan as TP

ET = "America/New_York"


def minutes(day, rows):
    """rows: [(HH:MM, high, low, close)] -> 1-minute DataFrame (tz-aware ET)."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz=ET) for t, *_ in rows])
    return pd.DataFrame({"Open": [c for *_, c in rows], "High": [h for _, h, _, _ in rows],
                         "Low": [l for _, _, l, _ in rows], "Close": [c for *_, c in rows]}, index=idx)


def daily(rows):
    """rows: [(YYYY-MM-DD, o, h, l, c)]"""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows])
    return pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows], "Low": [r[3] for r in rows],
                         "Close": [r[4] for r in rows], "Volume": 1}, index=idx)


SIG = {"date": "2026-11-02", "ticker": "AAA", "signal_time_et": "2026-11-02 10:12", "entry": 100.0}


class Plan(unittest.TestCase):
    def setUp(self):
        self.cfg = TP.load_config()

    def test_stop_from_last_two_5min_candles(self):
        m = minutes("2026-11-02", [("10:03", 101, 97.0, 100), ("10:06", 101, 99.0, 100), ("10:11", 101, 99.5, 100),
                                   ("10:12", 101, 99.6, 100), ("10:13", 101, 90.0, 100)])
        self.assertEqual(TP.low_before_signal(m, "2026-11-02 10:12"), 99.0)   # 10:05-10:12 only, nothing after

    def test_min_stop_and_targets(self):
        p = TP.make_swing_plan({"entry_price": 100, "low_5m": 99.9}, self.cfg)
        self.assertEqual(p["stop"], 99.5)                                      # at least 0.5% room
        self.assertEqual(p["targets"], {"3R": 101.5, "5R": 102.5})
        self.assertEqual((p["shares"], p["size_capped"]), (100, True))      # $100/$0.50 = 200, capped at $10k

    def test_alert_shows_test_line(self):
        r = {"ticker": "AAA", "entry_price": 100, "signal_time_et": "2026-11-02 10:12",
             "swing_plan": TP.make_swing_plan({"entry_price": 100, "low_5m": 98}, self.cfg)}
        self.assertIn("🧪 Swing test: stop $98.00", "\n".join(TG.setup_lines(r)))


class Replay(unittest.TestCase):
    def setUp(self):
        self.cfg = TP.load_config()

    def test_stopped_on_signal_day(self):
        m = minutes("2026-11-02", [("10:12", 100, 99, 100), ("10:20", 100.5, 97.9, 98)])
        r = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg)
        self.assertEqual((r["status"], r["result_r"]), ("closed", -1.0))

    def test_partials_then_ema9_exit(self):
        # entry 100, stop 98 -> 1R = 2 ; 3R = 106, 5R = 110
        m = minutes("2026-11-02", [("10:12", 100, 99, 100), ("11:00", 106.5, 100, 106)])
        r = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg)
        self.assertEqual(r["status"], "open")
        self.assertAlmostEqual(float(r["realized_r"]), 0.3333 * 3, places=3)
        hist = [(f"2026-10-{d:02d}", 100, 101, 99, 100) for d in range(1, 31)]
        d = daily(hist + [("2026-11-02", 100, 106.5, 99, 106), ("2026-11-03", 107, 111, 106, 110),
                          ("2026-11-04", 104, 105, 100.5, 101)])
        SJ.advance(r, d, self.cfg, dt.date(2026, 11, 3))
        self.assertEqual(r["status"], "open")                                  # 5R hit, still above EMA9
        SJ.advance(r, d, self.cfg, dt.date(2026, 11, 4))
        ema = d["Close"].ewm(span=9, adjust=False).mean().iloc[-1]
        self.assertGreater(ema, 101)
        self.assertEqual(r["exit_reason"], "close < EMA9")
        want = 0.3333 * 3 + 0.3333 * 5 + 0.3334 * (101 - 100) / 2
        self.assertAlmostEqual(r["result_r"], round(want, 2), places=2)

    def test_gap_below_stop_fills_at_open(self):
        m = minutes("2026-11-02", [("10:12", 100, 99, 100), ("15:59", 101, 99, 100)])
        r = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg)
        SJ.advance(r, daily([("2026-11-02", 100, 101, 99, 100), ("2026-11-03", 96, 97, 95, 96)]), self.cfg,
                   dt.date(2026, 11, 3))
        self.assertEqual((r["exit_reason"], r["result_r"]), ("stop (gap)", -2.0))

    def test_variant_b_moves_stop_to_entry_after_3r(self):
        m = minutes("2026-11-02", [("10:12", 100, 99, 100), ("11:00", 106.5, 100, 106), ("11:01", 106, 101, 102)])
        a = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg, "orig")             # retired: stop never moves
        b = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg, "be3r")
        self.assertEqual(b["active_stop"], 100.0)
        hist = [(f"2026-10-{d:02d}", 95, 96, 94, 95) for d in range(1, 31)]          # EMA9 well below price
        nxt = daily(hist + [("2026-11-02", 100, 106.5, 99, 102), ("2026-11-03", 101, 102, 99.5, 101)])
        SJ.advance(a, nxt, self.cfg, dt.date(2026, 11, 3))
        SJ.advance(b, nxt, self.cfg, dt.date(2026, 11, 3))
        self.assertEqual(a["status"], "open")                                  # A: stop still 98
        self.assertEqual((b["exit_reason"], b["result_r"]), ("stop (BE)", 1.0))   # 1/3 x 3R, rest at entry

    def test_variant_b_gap_below_entry(self):
        m = minutes("2026-11-02", [("10:12", 100, 99, 100), ("11:00", 106.5, 100, 106)])
        b = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg, "be3r")
        SJ.advance(b, daily([("2026-11-03", 99, 99.5, 97, 98)]), self.cfg, dt.date(2026, 11, 3))
        want = 0.3333 * 3 + 0.6667 * (99 - 100) / 2
        self.assertEqual(b["exit_reason"], "stop (BE, gap)")
        self.assertAlmostEqual(b["result_r"], round(want, 2), places=2)

    def test_b_holds_overnight_only_after_3r(self):
        m = minutes("2026-11-02", [("10:12", 100, 99, 100), ("11:00", 106.5, 100, 106), ("15:59", 106, 105, 105.5)])
        b = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg, "be3r_d1")
        self.assertEqual((b["status"], b["active_stop"]), ("open", 100.0))      # 3R reached -> rest held

    def test_time_limit(self):
        self.cfg["swing"]["max_days"] = 2
        m = minutes("2026-11-02", [("10:12", 100, 99, 100)])
        r = SJ.open_trade({**SIG, "low_5m": 98}, m, self.cfg)
        SJ.advance(r, daily([("2026-11-03", 100, 101, 99, 100.5), ("2026-11-04", 100, 101, 99, 101)]), self.cfg,
                   dt.date(2026, 11, 4))
        self.assertEqual((r["exit_reason"], r["result_r"]), ("time", 0.5))


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sig_dir = os.path.join(self.tmp, "signals")
        os.makedirs(self.sig_dir)
        with open(os.path.join(self.sig_dir, "2026-11-02.jsonl"), "w") as f:
            f.write(json.dumps(SIG) + "\n")                                    # old alert: no low_5m stored
            f.write(json.dumps({**SIG, "ticker": "BBB"}) + "\n")
        self.csv = os.path.join(self.tmp, "swing.csv")
        self.cfg = TP.load_config()
        self.cfg["swing"]["start_date"] = "2026-11-01"
        self.mins = {"AAA": minutes("2026-11-02", [("10:05", 101, 98, 100), ("10:12", 100.5, 99, 100),
                                                   ("13:00", 101, 99, 100.5)])}       # BBB: no data
        self.day = {"AAA": daily([("2026-11-02", 100, 101, 98, 100.5), ("2026-11-03", 100, 106.2, 99.5, 105)])}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sync(self, day):
        return SJ.sync(day, lambda t, d: {k: v for k, v in self.mins.items() if k in t},
                       lambda t: {k: v for k, v in self.day.items() if k in t}, self.cfg, self.sig_dir, self.csv)

    def test_backfill_advance_and_rerun(self):
        out = self.run_sync(dt.date(2026, 11, 2))
        self.assertEqual({(r["ticker"], r["variant"]): r["status"] for r in out["new"]},
                         {("AAA", "be3r"): "open", ("AAA", "be3r_d1"): "closed",       # B: no 3R on day 1 -> out
                          ("BBB", "be3r"): "skip", ("BBB", "be3r_d1"): "skip"})
        out = self.run_sync(dt.date(2026, 11, 3))
        self.assertEqual(sorted(r["ticker"] for r in out["new"]), ["BBB", "BBB"])   # skips retried, AAA kept
        self.assertEqual([(r["ticker"], r["variant_name"]) for r in out["events"]], [("AAA", "A")])
        rows = {(r["ticker"], r["variant"]): r for r in SJ.read_rows(self.csv)}
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[("AAA", "be3r")]["legs"].startswith("3R@106"))
        self.assertEqual(float(rows[("AAA", "be3r")]["active_stop"]), 100.0)
        self.assertEqual(rows[("AAA", "be3r_d1")]["exit_reason"], "day-1 close (no 3R)")
        self.assertEqual(float(rows[("AAA", "be3r_d1")]["result_r"]), 0.25)          # (100.5 - 100) / 2
        again = self.run_sync(dt.date(2026, 11, 3))
        self.assertEqual(again["events"], [])                                   # same evening twice = no change
        c = SJ.compare(self.csv, os.path.join(self.tmp, "none.csv"), self.cfg)
        self.assertEqual(c["alerts"], 1)                                        # BBB has no data -> not compared
        self.assertEqual(c["variants"]["A"]["open"]["n"], 1)
        self.assertEqual(c["variants"]["B"]["closed"]["n"], 1)
        msg = TG.build_eod_message("2026-11-03", [], {"trades": 0}, {**out, "compare": c})
        self.assertIn("🧪 Swing test", msg)
        self.assertIn("A AAA (11-02) 3R hit", msg)
        self.assertIn("B A + out at day-1 close unless 3R", msg)

    def test_legacy_letters_map_to_rule_ids(self):
        with open(self.csv, "w", encoding="utf-8") as f:
            f.write("date,ticker,variant,status,entry,stop\n2026-11-02,AAA,,closed,100,98\n"
                    "2026-11-02,AAA,A,closed,100,98\n2026-11-02,AAA,B,closed,100,98\n")
        self.assertEqual([r["variant"] for r in SJ.read_rows(self.csv)], ["orig", "orig", "be3r"])
        out = self.run_sync(dt.date(2026, 11, 2))
        self.assertEqual(sorted((r["ticker"], r["variant"]) for r in out["new"]),
                         [("AAA", "be3r_d1"), ("BBB", "be3r"), ("BBB", "be3r_d1")])   # old B row is today's A


if __name__ == "__main__":
    unittest.main()
