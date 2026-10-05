"""Check whether the collected data supports a credit-rating regression, using local data only."""
import re

import numpy as np
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bloomberg"))
from bbg_collect import latest_snapshot, load_dataset

SP = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
      "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
MDY = ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3",
       "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]
SP_N = {r: i + 1 for i, r in enumerate(SP)}
MDY_N = {r: i + 1 for i, r in enumerate(MDY)}

# Candidate regressors grouped by the credit driver they measure
DRIVERS = {
    "size": ["SALES_REV_TURN", "BS_TOT_ASSET", "CUR_MKT_CAP"],
    "leverage": ["TOT_DEBT_TO_EBITDA", "NET_DEBT_TO_EBITDA", "TOT_DEBT_TO_TOT_CAP", "TOT_DEBT_TO_TOT_ASSET"],
    "coverage": ["EBITDA_TO_INTEREST_EXPN", "INTEREST_COVERAGE_RATIO", "EBIT_TO_INT_EXP"],
    "profitability": ["EBITDA_MARGIN", "OPER_MARGIN", "RETURN_ON_ASSET", "RETURN_ON_INV_CAPITAL"],
    "cash flow": ["CFO_TO_TOT_DEBT", "CF_FREE_CASH_FLOW", "CF_CASH_FROM_OPER"],
    "liquidity": ["CUR_RATIO", "CASH_RATIO", "BS_CASH_NEAR_CASH_ITEM"],
    "composite": ["ALTMAN_Z_SCORE"],
}


def notch(r, scale):
    if not isinstance(r, str):
        return np.nan
    return scale.get(re.sub(r"\(P\)|\*[+-]?|\s.*$|u$", "", r.strip()), np.nan)


def main():
    snap = latest_snapshot()
    ann = load_dataset("fundamentals_annual").sort_values("date")

    # Dependent variable: average agency notch, available today only
    snap["notch"] = pd.concat([
        snap["RTG_SP_LT_LC_ISSUER_CREDIT"].map(lambda r: notch(r, SP_N)),
        snap["RTG_MOODY_LONG_TERM"].map(lambda r: notch(r, MDY_N)),
        snap["RTG_FITCH_LT_ISSUER_DEFAULT"].map(lambda r: notch(r, SP_N)),
    ], axis=1).mean(axis=1)
    rated = snap[snap["notch"].notna()]
    fin = rated["INDUSTRY_SECTOR"].eq("Financial")
    print(f"Snapshot: {len(snap)} issuers, rated by >=1 agency: {len(rated)} ({len(rated) / len(snap):.0%})")
    print(f"  of which financials (no EBITDA/leverage concept): {fin.sum()}; non-financial rated: {(~fin).sum()}")
    print(f"  IG: {(rated['notch'] <= 10.5).sum()}  HY: {(rated['notch'] > 10.5).sum()}")
    print(f"  rating action dates available (S&P): {snap['RTG_SP_LT_LC_ISS_CRED_RTG_DT'].notna().sum()}")
    print("  rating history (time series): NOT available via API -> dependent variable is cross-sectional\n")

    # Latest fiscal year per non-financial rated issuer
    sample = rated.loc[~fin, "ticker"]
    latest = ann[ann["ticker"].isin(sample)].groupby("ticker").tail(1).set_index("ticker")
    recent = latest[latest["date"] >= "2024-06-30"]
    print(f"Non-financial rated issuers with a fiscal year ending after mid-2024: {len(recent)} / {len(sample)}\n")

    print(f"{'driver':<14}{'field':<26}{'% filled latest FY':>20}{'median yrs history':>20}")
    years = ann[ann["ticker"].isin(sample)].groupby("ticker").count()
    for driver, fields in DRIVERS.items():
        for f in fields:
            if f not in ann:
                print(f"{driver:<14}{f:<26}{'missing':>20}")
                continue
            print(f"{driver:<14}{f:<26}{recent[f].notna().mean():>19.0%}{years[f].median():>20.0f}")

    core = ["SALES_REV_TURN", "TOT_DEBT_TO_EBITDA", "EBITDA_TO_INTEREST_EXPN", "EBITDA_MARGIN", "CFO_TO_TOT_DEBT"]
    core = [c for c in core if c in recent]
    complete = recent[core].notna().all(axis=1)
    print(f"\nIssuers with ALL core regressors {core} filled: {complete.sum()} / {len(recent)}")
    xs = recent[complete].join(rated.set_index("ticker")[["notch"]])
    print(f"Usable cross-section for rating regression: {len(xs)} issuers, "
          f"{xs['notch'].between(9.5, 11.5).sum()} of them within one notch of the IG/HY line")

    panel = ann[ann["ticker"].isin(sample)].dropna(subset=core)
    print(f"Panel (issuer-years) with all core regressors: {len(panel)}, from {panel['date'].min():%Y}")


if __name__ == "__main__":
    main()
