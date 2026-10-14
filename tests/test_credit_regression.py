"""Credit regression: shrinkage of fitted ratings towards the average does not flag names by itself."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research" / "credit_ratings"))
import regression as rg


def fake_result(implied):
    notch = pd.Series(np.repeat([6.0, 8.0, 9.0, 10.0, 11.0, 12.0], 20))
    data = pd.DataFrame({"ticker": [f"T{i}" for i in range(len(notch))], "NAME": "x", "sector": "Industrials",
                         "region": "US", "tier": 2, "RTG_SP_LT_LC_ISSUER_CREDIT": "", "RTG_MOODY_LONG_TERM": "",
                         "RTG_FITCH_LT_ISSUER_DEFAULT": "", "RTG_SP_OUTLOOK": "", "RTG_MDY_OUTLOOK": "",
                         "notch": notch, "fiscal_year_end": pd.Timestamp("2025-12-31"), "log_sales_usd": 1.0})
    pred = pd.DataFrame({"implied_notch": implied(notch), "p_hy": 0.1}, index=data.index)
    return {"data": data, "pred": pred, "regressors": ["log_sales_usd"]}


class Frontier(unittest.TestCase):
    def test_shrinkage_alone_flags_nobody(self):
        # every BB name is implied a notch better than its rating, every A name a notch worse: pure shrinkage
        res = fake_result(lambda n: 9.0 + 0.5 * (n - 9.0))
        issuers = rg.issuer_table(res)
        self.assertGreater(issuers.loc[issuers["notch"] == 12, "gap_notches"].min(), 1)  # the raw gap is fooled
        self.assertTrue((rg.frontier(issuers)["watch"] == "").all())

    def test_weak_investment_grade_name_is_flagged(self):
        def implied(n):
            out = 9.0 + 0.5 * (n - 9.0)
            out.iloc[60] += 3  # a BBB- name whose fundamentals look like BB-
            return out
        front = rg.frontier(rg.issuer_table(fake_result(implied)))
        flagged = front[front["watch"] != ""]
        self.assertEqual(list(flagged["ticker"]), ["T60"])
        self.assertEqual(flagged["watch"].iloc[0], "fallen-angel risk")


if __name__ == "__main__":
    unittest.main()
