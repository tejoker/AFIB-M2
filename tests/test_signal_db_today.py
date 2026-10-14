"""Today's provisional bar in the cme signal database, from fake Yahoo quotes (no network)."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cme_challenge"))
import signal_db as sdb

TODAY = pd.Timestamp.now(tz=sdb.CT).tz_localize(None).normalize()
YESTERDAY = TODAY - pd.Timedelta(days=1)


def series(contract, price):
    days = pd.DatetimeIndex([YESTERDAY - pd.Timedelta(days=1), YESTERDAY])
    return pd.DataFrame({"contract": contract, "price": [price * 0.99, price], "ret": [0.0, 0.01],
                         "next_contract": "NEXT", "next_price": price * 0.98, "carry": 0.1,
                         "index": [1.0, 1.01]}, index=days)


def quotes(prev, now, with_today=True):
    days = [YESTERDAY, TODAY] if with_today else [YESTERDAY]
    return pd.DataFrame({"Close": [prev, now][:len(days)], "Volume": 1}, index=pd.DatetimeIndex(days))


class ProvisionalBar(unittest.TestCase):
    def test_today_added_against_the_series_last_price(self):
        s = {"CL": series("CLZ26", 100.0), "LH": series("HEZ26", 70.0), "HO": series("HOX26", 4.0),
             "C": series("ZCZ26", 480.0)}
        q = {"CLZ26.NYM": quotes(99.5, 102.0),                 # Yahoo's close differs a little: fine
             "HEZ26.CME": quotes(70.0, 70.0, with_today=False),  # not trading yet today
             "HOX26.NYM": quotes(400.0, 404.0),                 # other units: left alone
             "ZCZ26.CBT": quotes(480.0, 470.4)}                 # root "C " in the market table
        notes = []
        out = sdb.add_today(s, notes, quotes=q)
        self.assertEqual(out["CL"].index[-1], TODAY)
        self.assertAlmostEqual(out["CL"]["ret"].iloc[-1], 0.02)
        self.assertAlmostEqual(out["CL"]["index"].iloc[-1], 1.01 * 1.02)
        self.assertEqual(out["CL"]["contract"].iloc[-1], "CLZ26")
        self.assertTrue(np.isnan(out["CL"]["next_price"].iloc[-1]))
        self.assertAlmostEqual(out["C"]["ret"].iloc[-1], -0.02)
        self.assertEqual(out["LH"].index[-1], YESTERDAY)
        self.assertEqual(out["HO"].index[-1], YESTERDAY)
        self.assertTrue(any("provisional for CL, C" in n for n in notes))
        self.assertTrue(any("HO" in n and "disagrees" in n for n in notes))

    def test_nothing_to_do_when_series_are_current(self):
        s = {"CL": series("CLZ26", 100.0).rename(index={YESTERDAY: TODAY})}
        self.assertIs(sdb.add_today(s, [], quotes={})["CL"], s["CL"])


if __name__ == "__main__":
    unittest.main()
