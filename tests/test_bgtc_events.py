"""Russell migration bands, event returns and credit flags on synthetic data; no Bloomberg needed."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research" / "bgtc_events"))
import strategy as st


def members(n1=1000, n2=2000):
    """Russell 1000 caps falling from $50bn, Russell 2000 caps from $8bn, plus a demotion and a promotion."""
    r1 = pd.DataFrame({"ticker": [f"L{i}" for i in range(n1)], "index": "R1000",
                       "CUR_MKT_CAP": np.geomspace(5e10, 6e9, n1)})
    r2 = pd.DataFrame({"ticker": [f"S{i}" for i in range(n2)], "index": "R2000",
                       "CUR_MKT_CAP": np.geomspace(5e9, 5e7, n2)})
    df = pd.concat([r1, r2], ignore_index=True)
    df.loc[df["ticker"].eq("L500"), "CUR_MKT_CAP"] = 1e8   # collapsed: far below the band
    df.loc[df["ticker"].eq("S10"), "CUR_MKT_CAP"] = 4e10   # soared: far above it
    df["VOLUME_AVG_20D"], df["PX_LAST"] = 1e6, 20.0
    return df


class Russell(unittest.TestCase):
    def test_band_brackets_breakpoint(self):
        caps = members().set_index("ticker")["CUR_MKT_CAP"]
        bp, lower, upper = st.russell_bands(caps)
        self.assertEqual(bp, caps.sort_values(ascending=False).iloc[999])
        self.assertLess(lower, bp)
        self.assertGreater(upper, bp)

    def test_only_members_outside_the_band_move(self):
        df = members()
        px = pd.DataFrame(20.0, index=pd.bdate_range("2026-08-01", periods=40), columns=df["ticker"])
        px["L500"] = 0.9  # also under the $1 floor
        rus, _ = st.russell_migration(df, px)
        moved = rus[rus["current"] != rus["predicted"]].set_index("ticker")["predicted"]
        self.assertEqual(moved.to_dict(), {"L500": "R2000", "S10": "R1000"})
        self.assertTrue(rus.set_index("ticker").loc["L500", "price_risk"])

    def test_sleeve_skips_price_risk_and_caps_weight(self):
        df = members()
        px = pd.DataFrame(20.0, index=pd.bdate_range("2026-08-01", periods=40), columns=df["ticker"])
        df.loc[df["ticker"].eq("L501"), "CUR_MKT_CAP"] = 1.2e8
        px["L500"] = 0.9
        rus, _ = st.russell_migration(df, px)
        flags = pd.DataFrame({"bucket": "IG", "avoid_credit": False, "RTG_MOODY_LONG_TERM": None,
                              "RTG_FITCH_LT_ISSUER_DEFAULT": None}, index=df["ticker"])
        flags.loc["L501", "avoid_credit"] = True
        book = st.sleeve(rus.assign(EXPECTED_REPORT_DT=None), flags, pd.DataFrame(index=df["ticker"]), 0.4, 5)
        self.assertEqual(book.loc["L500", "weight"], 0.0)
        self.assertEqual(book.loc["L501", "weight"], 0.20)  # one pick: capped at 20%, not the whole 40% sleeve
        vetoed = st.sleeve(rus.assign(EXPECTED_REPORT_DT=None), flags, pd.DataFrame(index=df["ticker"]), 0.4, 5,
                           credit_veto=True)
        self.assertEqual(vetoed["weight"].sum(), 0.0)


class Credit(unittest.TestCase):
    def test_car_sums_abnormal_returns_after_event(self):
        days = pd.bdate_range("2026-01-01", periods=60)
        ab = pd.DataFrame({"X": 0.0}, index=days)
        ab.iloc[25:45, 0] = 0.01
        self.assertAlmostEqual(st.car(ab, "X", days[23], 2, 21), 0.20)
        self.assertTrue(np.isnan(st.car(ab, "X", days[55], 2, 21)))  # window runs past the data

    def test_avoid_only_negative_at_or_below_bbb_minus(self):
        credit = pd.DataFrame({
            "RTG_SP_LT_LC_ISSUER_CREDIT": ["BBB-", "A", "BB+ *-", "BB", None],
            "RTG_SP_OUTLOOK": ["NEG", "NEG", "STABLE", "POS", None],
            "RTG_MOODY_LONG_TERM": ["Baa3", "A2", "Ba1", "Ba2", None],
            "RTG_MDY_OUTLOOK": ["STABLE"] * 4 + [None],
            "RTG_FITCH_LT_ISSUER_DEFAULT": [None] * 5,
            "BB_1YR_DEFAULT_PROB": [0.001] * 5,
        }, index=["FALL", "SAFE", "WATCH", "RISE", "NR"])
        f = st.credit_flags(credit)
        self.assertEqual(f.index[f["avoid_credit"]].tolist(), ["FALL", "WATCH"])
        self.assertEqual(f.loc["NR", "bucket"], "unrated")
        self.assertEqual(f.loc["RISE", "bucket"], "HY")


if __name__ == "__main__":
    unittest.main()
