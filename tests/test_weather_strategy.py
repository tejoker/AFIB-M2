"""Weather strategy timing: signals use forecasts issued by day t only, positions earn the following returns."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cme_challenge"))
import weather_strategy as ws


class Signals(unittest.TestCase):
    def test_revision_compares_two_issues_of_the_same_days(self):
        days = pd.date_range("2025-01-01", periods=30)
        # each forecast says 10 HDD, except that the issue of 10 January turned 1 HDD colder for every day ahead
        hdd = pd.DataFrame({k: 10.0 for k in ws.LEADS}, index=days)
        jan10 = pd.Timestamp("2025-01-10")
        for k in ws.LEADS:
            hdd.loc[jan10 + pd.Timedelta(days=k), k] = 11.0  # forecast for jan10+k issued on jan10 (lead k)
        sig = ws.signals(hdd, pd.Series(10.0, index=pd.date_range("2025-01-01", periods=40)))
        self.assertEqual(sig.loc[jan10, "revision"], 6)       # 6 shared target days, each 1 HDD colder
        self.assertEqual(sig.loc[jan10, "anomaly"], 7)
        self.assertEqual(sig.loc[jan10 - pd.Timedelta(days=1), "revision"], 0)

    def test_next_day_timing_has_no_hindsight(self):
        days = pd.bdate_range("2020-01-01", periods=2000)
        ret = pd.Series(np.random.default_rng(7).normal(0, 0.01, len(days)), index=days)
        # a signal that "knows" each day's return on the day it happens
        cheat = pd.Series(np.sign(ret.to_numpy()), index=days)
        hindsight = (np.sign(ret) * ret).mean()
        for lag in (0, 1):  # even the optimistic same-day timing trades at the close, after the move
            pnl, _ = ws.daily_pnl(cheat, ret, hold=1, lag=lag)
            self.assertLess(abs(pnl.mean()), hindsight / 10)


if __name__ == "__main__":
    unittest.main()
