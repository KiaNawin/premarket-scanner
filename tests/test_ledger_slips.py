"""Ledger (append-only, holdings) and slip validation."""
import datetime as dt
import os
import shutil
import tempfile
import unittest

from portfolio_stage import ledger as L
from portfolio_stage import slips as S
from portfolio_stage.config import DEFAULTS


def cfg_for(tmp):
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    c["paths"] = {"trades": os.path.join(tmp, "portfolio", "trades.csv"),
                  "meta": os.path.join(tmp, "portfolio", "meta.csv"),
                  "output": os.path.join(tmp, "output"), "cache": os.path.join(tmp, "cache"),
                  "slips": os.path.join(tmp, "slips")}
    return c


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp)
        self.path = self.cfg["paths"]["trades"]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_append_only_and_duplicates(self):
        t1 = {"datetime": "2026-10-20T21:35:00+07:00", "ticker": "NVDA", "side": "BUY", "shares": 2, "price": 180, "fee": 1}
        self.assertEqual(len(L.append_trades(self.path, [t1])), 1)
        before = open(self.path, encoding="utf-8").read()
        self.assertEqual(L.append_trades(self.path, [t1]), [])                     # duplicate ignored
        t2 = dict(t1, datetime="2026-10-21T21:40:00+07:00", side="SELL", shares=1, price=190)
        L.append_trades(self.path, [t2])
        after = open(self.path, encoding="utf-8").read()
        self.assertTrue(after.startswith(before))                                   # old lines untouched

    def test_holdings_and_average_cost(self):
        L.append_trades(self.path, [
            {"datetime": "2026-10-01T16:00:00-04:00", "ticker": "AAPL", "side": "OPENING", "shares": 10, "price": 100, "fee": 0},
            {"datetime": "2026-10-05T10:00:00-04:00", "ticker": "AAPL", "side": "BUY", "shares": 10, "price": 120, "fee": 2},
            {"datetime": "2026-10-06T10:00:00-04:00", "ticker": "AAPL", "side": "SELL", "shares": 5, "price": 130, "fee": 1},
        ])
        p = L.holdings(L.read_trades(self.path))["AAPL"]
        self.assertAlmostEqual(p.shares, 15)
        self.assertAlmostEqual(p.avg_cost, (1000 + 1202) / 20)                       # sells don't change avg cost
        self.assertAlmostEqual(p.realized, 5 * 130 - 1 - 5 * (2202 / 20))

    def test_oversell_is_flagged(self):
        L.append_trades(self.path, [
            {"datetime": "2026-10-05T10:00:00-04:00", "ticker": "TSLA", "side": "BUY", "shares": 1, "price": 400, "fee": 0},
            {"datetime": "2026-10-06T10:00:00-04:00", "ticker": "TSLA", "side": "SELL", "shares": 3, "price": 410, "fee": 0}])
        self.assertTrue(L.holdings(L.read_trades(self.path))["TSLA"].warnings)


class Slips(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = cfg_for(self.tmp)
        self.folder = self.cfg["paths"]["slips"]
        os.makedirs(self.folder)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def slip(self, name, **trade):
        open(os.path.join(self.folder, name), "wb").write(b"img")
        base = {"ticker": "NVDA", "side": "BUY", "shares": 0.5, "price": 180.0, "fee": 0.15, "amount": 90.15,
                "datetime": "2026-10-27T21:35:10", "status": "filled", "status_raw": "สำเร็จ", "unclear_fields": []}
        base.update(trade)
        return name, {"is_trade_slip": True, "trades": [base]}

    def run_ingest(self, slips, day_range=lambda t, w: (170.0, 190.0)):
        table = dict(slips)
        return S.ingest(self.folder, self.cfg, extract=lambda p: table[os.path.basename(p)],
                        day_range=day_range, now=self.NOW)

    def test_rules(self):
        L.append_trades(self.cfg["paths"]["trades"], [{"datetime": "2026-10-20T16:00:00-04:00", "ticker": "AMD",
                                                       "side": "OPENING", "shares": 3, "price": 230, "fee": 0}])
        s = self.run_ingest([
            self.slip("a_ok.png"),
            self.slip("b_dup.png"),                                                   # same trade again
            self.slip("pending.png", status="pending", status_raw="รอจับคู่"),
            self.slip("blurry.jpg", price=None, unclear_fields=["price"]),
            self.slip("mismatch.png", amount=900.15),                                 # 10x -> misread digit
            self.slip("range.png", price=18.0, amount=9.15),                          # outside day's range
            self.slip("old.png", ticker="AMD", datetime="2026-10-15T21:35:00", price=185, amount=92.65),
        ])
        self.assertEqual([n for n, _ in s["added"]], ["a_ok.png"])
        skipped = dict(s["skipped"])
        self.assertIn("duplicate", " ".join(skipped["b_dup.png"]))
        self.assertIn("not Filled", " ".join(skipped["pending.png"]))
        self.assertIn("OPENING", " ".join(skipped["old.png"]))
        review = dict(s["review"])
        self.assertIn("unclear", " ".join(review["blurry.jpg"]))
        self.assertIn("amount", " ".join(review["mismatch.png"]))
        self.assertIn("range", " ".join(review["range.png"]))
        self.assertTrue(os.path.exists(os.path.join(self.folder, "done", "a_ok.png")))
        self.assertTrue(os.path.exists(os.path.join(self.folder, "review", "blurry.jpg.reason.txt")))
        self.assertEqual(sum(1 for _ in open(self.cfg["paths"]["trades"], encoding="utf-8")), 3)  # header + 2

    def test_api_error_leaves_file(self):
        open(os.path.join(self.folder, "x.png"), "wb").write(b"img")

        def boom(p):
            raise RuntimeError("timeout")
        s = S.ingest(self.folder, self.cfg, extract=boom, day_range=lambda t, w: None, now=self.NOW)
        self.assertEqual(len(s["errors"]), 1)
        self.assertTrue(os.path.exists(os.path.join(self.folder, "x.png")))


if __name__ == "__main__":
    unittest.main()


class TelegramMessage(unittest.TestCase):
    def test_format_and_escaping(self):
        import re
        from portfolio_stage import notify as N
        meta = {"as_of": "2026-10-29", "total_value": 12345.6, "positions": 3, "weekly_bars_through": "2026-10-23"}
        rows = {"NVDA": {"stage": 2, "flag": "✅"}, "INTC": {"stage": 4}, "AT&T": {"stage": 2, "flag": "⚠️"}}
        alerts = [("Near support/resistance", "**NVDA** 0.5% above support $116.24"),
                  ("Stop broken", "**INTC** $30.78 < stop <b>"), ("Stage change", "**AT&T** S2 → S3")]
        msg = N.build_message(meta, alerts, rows)
        self.assertLess(msg.index("Stop broken"), msg.index("Stage change"))           # most important first
        self.assertLess(msg.index("Stage change"), msg.index("Near support"))
        self.assertIn("<b>AT&amp;T</b>", msg)
        self.assertIn("&lt;b&gt;", msg)                                                 # raw text escaped
        for tag in re.findall(r"<[^>]*>", msg):
            self.assertTrue(tag in ("<b>", "</b>", "<i>", "</i>", "</a>") or tag.startswith('<a href="https://'), tag)
        self.assertIn("Nothing to check today", N.build_message(meta, [], rows))


class Events(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        from portfolio_stage.config import load_config
        self.cfg = load_config()
        self.cfg["paths"] = dict(self.cfg["paths"], cache=os.path.join(self.tmp, "cache"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_earnings_window(self):
        import pandas as pd
        from portfolio_stage import events as E
        ET = E.ET
        idx = pd.DatetimeIndex([pd.Timestamp("2026-11-04 16:05", tz=ET), pd.Timestamp("2026-10-27 07:00", tz=ET),
                                pd.Timestamp("2026-07-28 16:05", tz=ET)], name="Earnings Date")
        df = pd.DataFrame({"EPS Estimate": [0.92, 0.75, 0.6], "Reported EPS": [float("nan"), 0.81, 0.62],
                           "Surprise(%)": [float("nan"), 8.0, 3.3]}, index=idx)
        fake = type("T", (), {"get_earnings_dates": lambda self, limit=8: df})()
        rows, err = E.earnings_events("NVDA", self.cfg, dt.date(2026, 10, 29), ticker_obj=fake)
        self.assertIsNone(err)
        self.assertEqual([(r["date"], r["reported"], r["when"]) for r in rows],
                         [("2026-10-27", True, "before open"), ("2026-11-04", False, "after close")])

    def test_big_news_filtering(self):
        from portfolio_stage import events as E
        E._yahoo_news = lambda t, ticker_obj=None: [
            {"title": "NVIDIA Beats Q3 Estimates, Raises Guidance", "publisher": "Reuters", "url": "https://x/1",
             "date": "2026-10-28", "source": "yahoo", "main": None},
            {"title": "3 AI Stocks to Buy Before 2027 (NVDA)", "publisher": "Fool", "url": "https://x/2",
             "date": "2026-10-28", "source": "yahoo", "main": None},
            {"title": "Intel CEO steps down", "publisher": "Reuters", "url": "https://x/3",
             "date": "2026-10-28", "source": "yahoo", "main": None},
            {"title": "NVIDIA wins $2B contract", "publisher": "Bloomberg", "url": "https://x/4",
             "date": "2026-10-10", "source": "yahoo", "main": None}]
        E._nasdaq_news = lambda t: [
            {"title": "Stocks Tumble as Chipmakers Slide", "publisher": "Barchart", "url": "https://x/5",
             "date": "2026-10-28", "source": "nasdaq", "main": False},
            {"title": "Nvidia Faces DOJ Probe Over Sales", "publisher": "RTTNews", "url": "https://x/6",
             "date": "2026-10-27", "source": "nasdaq", "main": True}]
        news, err = E.big_news("NVDA", "NVIDIA Corporation", self.cfg, dt.date(2026, 10, 29))
        self.assertEqual([n["url"] for n in news], ["https://x/1", "https://x/6"])
