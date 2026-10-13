"""Signal database: the IC has no look-ahead or small-sample bias, and every outside source is used only once public."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cme_challenge"))
import cot
import gas
import signal_db as sd

DAYS = pd.bdate_range("2010-01-01", "2026-09-30")
RECENT = DAYS[-1] - pd.DateOffset(years=3)


def random_walk(seed):
    rng = np.random.default_rng(seed)
    return pd.Series(np.exp(np.cumsum(rng.normal(0, 0.015, len(DAYS)))), index=DAYS)


def momentum_record(index, lookback, horizon):
    vol = sd.volatility(index)
    signal = (index / index.shift(lookback) - 1) / (vol * np.sqrt(lookback))
    target = (index.shift(-horizon) / index - 1) / (vol * np.sqrt(horizon))
    return sd.ic_stats(sd.standardise(signal), target, RECENT)


class InformationCoefficient(unittest.TestCase):
    def test_slow_momentum_on_a_random_walk_is_not_read_as_reversal(self):
        # a sample correlation within each year gave t of about -5 here (Stambaugh bias); the record must not
        t = [momentum_record(random_walk(seed), 252, 5)["t"] for seed in range(12)]
        self.assertLess(abs(np.mean(t)), 0.8)

    def test_finds_a_planted_edge(self):
        index = random_walk(1)
        target = (index.shift(-5) / index - 1) / (sd.volatility(index) * np.sqrt(5))
        noise = pd.Series(np.random.default_rng(2).normal(0, 1, len(DAYS)), index=DAYS)
        signal = 0.1 * target.fillna(0) + noise  # knows a little of the next 5 days
        stats = sd.ic_stats(sd.standardise(signal), target, RECENT)
        self.assertGreater(stats["ic"], 0.05)
        self.assertGreater(stats["t"], 3)

    def test_standardising_uses_the_past_only(self):
        x = random_walk(3)
        changed = x.copy()
        changed.iloc[3000:] *= 10
        pd.testing.assert_series_equal(sd.standardise(x).iloc[:3000], sd.standardise(changed).iloc[:3000])

    def test_pooled_q_values_are_their_own_family(self):
        p = pd.Series([0.001, 0.5, 0.9])
        self.assertAlmostEqual(sd.bh_q(p).iloc[0], 0.003)


class Availability(unittest.TestCase):
    def test_positioning_is_used_from_the_monday_after_the_report(self):
        reports = pd.DataFrame({"root": "NG", "report_date": pd.date_range("2020-01-07", periods=80, freq="7D"),
                                "open_interest": 1000.0, "mm_long": np.arange(80) * 2.0 + 100, "mm_short": 100.0})
        days = pd.bdate_range("2021-06-01", "2021-06-30")
        out = cot.signals(reports, "NG", days)
        tuesday = pd.Timestamp("2021-06-15")  # a report date in the table
        self.assertIn(tuesday, set(reports["report_date"]))
        before, after = out.loc["2021-06-18", "cot_change"], out.loc["2021-06-21", "cot_change"]  # Friday, Monday
        net = lambda d: (reports.set_index("report_date").loc[d, "mm_long"] - 100) / 1000
        self.assertAlmostEqual(after, 100 * (net(tuesday) - net(tuesday - pd.Timedelta(weeks=4))))
        self.assertNotEqual(before, after)  # Friday still shows the previous report

    def test_storage_report_counts_from_the_thursday(self):
        weeks = pd.date_range("2015-01-02", "2021-12-31", freq="W-FRI")
        level = pd.Series(2000 + 50 * np.sin(np.arange(len(weeks)) / 52 * 2 * np.pi), index=weeks)
        level.iloc[-1] = level.iloc[-2] + 1  # last week: almost no injection
        store = level.to_frame("level").assign(change=level.diff())
        store["avg_change_5y"] = gas.five_year_average(store["change"], store.index)
        store["avg_level_5y"] = gas.five_year_average(level, store.index)
        store["storage_surprise"] = store["avg_change_5y"] - store["change"]
        store["storage_deficit"] = 100 * (store["avg_level_5y"] - store["level"]) / store["avg_level_5y"]
        store["report_date"] = store.index + gas.REPORT_AFTER
        week_end = weeks[-1]
        days = pd.bdate_range(week_end, week_end + pd.Timedelta(days=10))
        out = gas.signals(days, store, pd.Series(dtype=float))
        thursday = week_end + pd.Timedelta(days=6)
        self.assertNotEqual(out.loc[thursday - pd.Timedelta(days=1), "storage_surprise"],
                            out.loc[thursday, "storage_surprise"])
        self.assertAlmostEqual(out.loc[thursday, "storage_surprise"], store["storage_surprise"].iloc[-1])


if __name__ == "__main__":
    unittest.main()
