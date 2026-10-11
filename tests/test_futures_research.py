"""Roll-correct series never book the gap between contracts; window and trend research find planted effects."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cme_challenge"))
from continuous import build, roll_dates
from futures_research import bh_qvalues, placebo_p, t_stat, window_returns


def two_contracts():
    contracts = pd.DataFrame({
        "ticker": ["XH6", "XM6"],
        "LAST_TRADEABLE_DT": pd.to_datetime(["2026-03-20", "2026-06-19"]),
        "FUT_NOTICE_FIRST": pd.to_datetime(["2026-03-02", pd.NaT]),
        "delivery": pd.to_datetime(["2026-03-01", "2026-06-01"]),
    })
    dates = pd.bdate_range("2026-01-02", "2026-06-19")
    # front grows 0.1% a day; the next contract trades 20% above it (contango) and grows 0.2% a day
    front = pd.Series(100 * 1.001 ** np.arange(len(dates)), index=dates).where(dates <= "2026-03-20")
    back = pd.Series(120 * 1.002 ** np.arange(len(dates)), index=dates)
    return contracts, pd.DataFrame({"XH6": front, "XM6": back})


class ContinuousSeries(unittest.TestCase):
    def test_roll_before_first_notice(self):
        contracts, _ = two_contracts()
        rolls = roll_dates(contracts, 5)
        self.assertEqual(rolls.iloc[0], pd.Timestamp("2026-02-23"))  # 5 business days before 2 March
        self.assertEqual(rolls.iloc[1], pd.Timestamp("2026-06-12"))  # no notice day: before last trade

    def test_returns_stay_within_one_contract(self):
        contracts, prices = two_contracts()
        s = build(contracts, prices, roll_days=5)
        self.assertEqual(s.loc["2026-02-23", "contract"], "XM6")  # switched at that close
        self.assertEqual(s.loc["2026-02-20", "contract"], "XH6")
        self.assertAlmostEqual(s.loc["2026-02-23", "ret"], 0.001)  # still XH6's own move, no +20% jump
        self.assertAlmostEqual(s.loc["2026-02-24", "ret"], 0.002)  # now XM6's own move
        self.assertLess(s["ret"].max(), 0.0021)
        self.assertLess(s.loc["2026-02-20", "carry"], 0)  # contango: rolling costs
        self.assertTrue(s.loc["2026-06-12":, "price"].isna().all())  # nothing listed after the last roll


class Research(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        dates = pd.bdate_range("2000-01-03", "2025-12-31")
        ret = pd.Series(rng.normal(0, 0.01, len(dates)), index=dates)
        planted = (dates.month == 10) & (dates.day > 9) & (dates.day <= 30)
        ret[planted] += 0.004  # about +6% over the window every year
        self.index = (1 + ret).cumprod()

    def test_planted_window_is_found(self):
        yearly = window_returns(self.index, "10-09", "10-30")
        self.assertEqual(len(yearly), 26)
        self.assertGreater(yearly.mean(), 0.04)
        t = t_stat(yearly)
        self.assertGreater(t, 3)
        self.assertLess(placebo_p(self.index, "10-09", "10-30", t), 0.05)

    def test_no_effect_elsewhere(self):
        t = t_stat(window_returns(self.index, "03-09", "03-30"))
        self.assertGreater(placebo_p(self.index, "03-09", "03-30", t), 0.05)

    def test_bh_qvalues(self):
        q = bh_qvalues([0.01, 0.04, np.nan, 0.03])
        self.assertTrue(np.isnan(q[2]))
        np.testing.assert_allclose(q.dropna().to_numpy(), [0.03, 0.04, 0.04])


if __name__ == "__main__":
    unittest.main()
