"""Verify financial units, calendar gaps, splits and membership observation timing."""
import unittest

import numpy as np
import pandas as pd

from collect_russell_research import monthly_features, merge_membership


class ResearchFeatures(unittest.TestCase):
    def test_turnover_units_and_split(self):
        daily = pd.DataFrame({
            "security": ["ABC US Equity"] * 3,
            "date": pd.to_datetime(["2026-09-28", "2026-09-29", "2026-09-30"]),
            "PX_LAST": [100., 50., 55.],
            "split_adjusted_price": [50., 50., 55.],
            "PX_VOLUME": [1000., 2000., 3000.],
            "TURNOVER": [100_000., 100_000., 165_000.],
            "CUR_MKT_CAP": [100_000_000., 100_000_000., 110_000_000.],
            "EQY_SH_OUT": [1_000_000., 2_000_000., 2_000_000.],
            "EQY_FLOAT": [.8, 1.6, 1.6],
        })
        row = monthly_features(daily).iloc[0]
        self.assertAlmostEqual(row.shares_out, 2_000_000)
        self.assertAlmostEqual(row.float_shares, 1_600_000)
        self.assertAlmostEqual(row.shares_turnover_pct, (.1 + .1 + .15) / 3)
        self.assertAlmostEqual(row.amihud_per_million, (0. + .1 / .165) / 2)
        self.assertAlmostEqual(row.adtv_usd, 365_000 / 3)

    def test_partial_month_and_missing_month_return(self):
        daily = pd.DataFrame({
            "security": ["ABC US Equity"] * 3,
            "date": pd.to_datetime(["2026-06-30", "2026-08-31", "2026-09-02"]),
            "PX_LAST": [10., 12., 13.], "split_adjusted_price": [10., 12., 13.],
            "PX_VOLUME": [100., 100., 100.], "TURNOVER": [1000., 1200., 1300.],
            "TOT_RETURN_INDEX_GROSS_DVDS": [100., 125., 140.],
        })
        monthly = monthly_features(daily)
        self.assertTrue(np.isnan(monthly.iloc[1].total_return_monthly))
        self.assertAlmostEqual(monthly.iloc[2].total_return_monthly, .12)
        self.assertFalse(monthly.iloc[2].calendar_month_complete)

    def test_membership_never_uses_later_snapshot(self):
        panel = pd.DataFrame({"security": ["ABC US Equity"] * 2,
                              "month": pd.to_datetime(["2026-03-31", "2026-04-30"])})
        members = pd.DataFrame([
            {"security": "ABC US Equity", "index": "russell1000", "membership_date": "2025-12-31", "membership_kind": "historical_quarter_end"},
            {"security": "XYZ US Equity", "index": "russell2000", "membership_date": "2025-12-31", "membership_kind": "historical_quarter_end"},
            {"security": "XYZ US Equity", "index": "russell1000", "membership_date": "2026-03-31", "membership_kind": "historical_quarter_end"},
            {"security": "ABC US Equity", "index": "russell2000", "membership_date": "2026-03-31", "membership_kind": "historical_quarter_end"},
        ])
        out = merge_membership(panel, members)
        self.assertTrue(out.iloc[0].in_russell1000_observed)
        self.assertFalse(out.iloc[0].in_russell2000_observed)
        self.assertFalse(out.iloc[1].in_russell1000_observed)
        self.assertTrue(out.iloc[1].in_russell2000_observed)
        self.assertTrue(out.in_sp500_observed.isna().all())


if __name__ == "__main__":
    unittest.main()
