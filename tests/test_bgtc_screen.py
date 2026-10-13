"""Bloomberg challenge screen against a fake Bloomberg: ranking, credit-danger list, unknown fields, resume."""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bloomberg"))
import bgtc_screen as bs

TODAY = date.today()
STOCKS = {  # ticker: (daily drift, announcement jump, rating, outlook, volume)
    "WIN US Equity": (0.002, 0.08, "A", "STABLE", 1e6),        # strong trend, jumped on results
    "MEH US Equity": (0.0, 0.0, "A", "STABLE", 1e6),
    "LOSE US Equity": (-0.001, -0.06, "A", "STABLE", 1e6),
    "RISK US Equity": (0.002, 0.05, "BBB-", "NEG", 1e6),      # looks good, but a fallen angel in waiting
    "THIN US Equity": (0.003, 0.10, "A", "STABLE", 10),      # great signals, cannot be traded in size
}
ANNOUNCED = pd.Timestamp(TODAY - timedelta(days=20))


class FakeBloomberg:
    def __init__(self, max_requests=None):
        self.requests = 0

    def stop(self):
        pass

    def index_members(self, index):
        self.requests += 1
        if index == "WLS Index":  # a full-size benchmark list that leaves out one screened stock
            return [t for t in STOCKS if t != "MEH US Equity"] + [f"X{i} US Equity" for i in range(4000)]
        return list(STOCKS) if index == "SPX Index" else []

    def field_info(self, fields):
        self.requests += 1
        return {f: {} for f in fields if f != "BEST_ANALYST_RATING"}  # one field this "Terminal" lacks

    def reference(self, securities, fields):
        self.requests += 1
        assert "BEST_ANALYST_RATING" not in fields
        rows = []
        for t in securities:
            _, _, rating, outlook, volume = STOCKS[t]
            rows.append({"ticker": t, "NAME": t.split()[0], "PX_LAST": 100.0, "VOLUME_AVG_20D": volume,
                         "LATEST_ANNOUNCEMENT_DT": ANNOUNCED.date(), "EXPECTED_REPORT_DT": date(2026, 11, 3),
                         "BEST_TARGET_PRICE": 110.0, "RTG_SP_LT_LC_ISSUER_CREDIT": rating, "RTG_SP_OUTLOOK": outlook})
        return pd.DataFrame(rows), []

    def history(self, securities, fields, start, periodicity, fiscal=False, extra=None):
        self.requests += 1
        days = pd.bdate_range(start, TODAY)
        rows = []
        for t in securities:
            drift, jump, *_ = STOCKS[t]
            r = np.full(len(days), drift)
            r[days.searchsorted(ANNOUNCED)] += jump
            for d, p in zip(days, 100 * np.cumprod(1 + r)):
                rows.append({"ticker": t, "date": d, "PX_LAST": p})
        return pd.DataFrame(rows), [], set()


class Screen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = bs.OUT, bs.Bloomberg
        bs.OUT, bs.Bloomberg = Path(self.tmp.name), FakeBloomberg

    def tearDown(self):
        bs.OUT, bs.Bloomberg = self.saved
        self.tmp.cleanup()

    def run_screen(self):
        sys.argv = ["bgtc_screen.py", "--indices", "SPX"]
        with self.assertRaises(SystemExit) as done:
            bs.main()
        self.assertEqual(done.exception.code, 0)
        return pd.ExcelFile(bs.OUT / f"screen_{TODAY.isoformat()}_SPX.xlsx")

    def test_ranking_filters_and_report(self):
        xl = self.run_screen()
        top = xl.parse("Top candidates", index_col=0)
        self.assertEqual(top.index[0], "WIN US Equity")
        self.assertNotIn("RISK US Equity", top.index)       # credit danger
        self.assertNotIn("THIN US Equity", top.index)       # illiquid
        self.assertIn("RISK US Equity", xl.parse("Credit danger - avoid", index_col=0).index)
        self.assertAlmostEqual(top.loc["WIN US Equity", "earnings_reaction"], 1.08 * 1.002 ** 2 - 1, places=3)
        self.assertTrue(top["reports_in_challenge"].all())
        self.assertNotIn("MEH US Equity", top.index)        # not in the WLS benchmark: cannot be traded
        self.assertIn("Top large and mid caps", xl.sheet_names)

    def test_other_index_selection_does_not_reuse_saved_universe(self):
        self.run_screen()
        sys.argv = ["bgtc_screen.py", "--indices", "SPX", "STOXX600"]
        with self.assertRaises(SystemExit):
            bs.main()
        self.assertTrue((bs.OUT / f"universe_{TODAY.isoformat()}_SPX-STOXX600.parquet").exists())

    def test_rerun_reuses_saved_data(self):
        self.run_screen()
        calls = []
        original = FakeBloomberg.history
        FakeBloomberg.history = lambda self, *a, **k: calls.append(1) or original(self, *a, **k)
        try:
            self.run_screen()
        finally:
            FakeBloomberg.history = original
        self.assertEqual(calls, [])  # prices of the day are cached


if __name__ == "__main__":
    unittest.main()
