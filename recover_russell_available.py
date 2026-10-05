"""Build useful partial Russell exports entirely offline from complete saved data."""
import gzip
import json
from pathlib import Path
from argparse import Namespace

import numpy as np
import pandas as pd

import collect_russell_research as rr

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "russell_research_20261005"
AMOUNTS = """SALES_REV_TURN EBITDA EBIT NET_INCOME IS_INC_BEF_XO_ITEM BS_TOT_ASSET BS_TOT_LIAB2
TOT_COMMON_EQY SHORT_AND_LONG_TERM_DEBT NET_DEBT BS_CASH_NEAR_CASH_ITEM CF_CASH_FROM_OPER
CAPITAL_EXPEND CF_FREE_CASH_FLOW""".split()


def recover_reference(members):
    main = {}
    forecasts = {"1BF": {}, "2BF": {}}
    for path in sorted((OUTPUT / "cache").glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            item = json.load(stream)
        req = item["request"]
        if req["kind"] != "ReferenceDataRequest":
            continue
        fields = set(req["fields"])
        period = req.get("overrides", {}).get("BEST_FPERIOD_OVERRIDE")
        is_main = fields == set(rr.REF_FIELDS)
        is_forecast = fields == set(rr.FORECAST_FIELDS) and period in forecasts
        if not is_main and not is_forecast:
            continue
        for response in item["responses"]:
            data = response.get("securityData", [])
            for row in data if isinstance(data, list) else [data]:
                security = row["security"]
                values = row.get("fieldData", {})
                if is_main:
                    main[security] = dict(security=security, **values, snapshot_date="2026-10-05",
                                          reference_source="research_cached_api", reference_retrieved_utc=item["retrieved_at_utc"])
                else:
                    forecasts[period][security] = {key + "_" + period: value for key, value in values.items()}
    for security, row in main.items():
        for period in forecasts:
            row.update(forecasts[period].get(security, {}))
    baseline = pd.read_csv(ROOT / "russell1000_liquidity_analysis" / "security_membership_and_reference.csv")
    target = set(members["security"])
    for _, row in baseline.iterrows():
        security = row["security"]
        if security not in target or security in main:
            continue
        values = {key: row[key] for key in rr.REF_FIELDS if key in row and pd.notna(row[key])}
        for original, renamed in [("BEST_PE_RATIO", "BEST_PE_RATIO_1BF"), ("BEST_SALES", "BEST_SALES_1BF"),
                                 ("BEST_EPS", "BEST_EPS_1BF"), ("best_sales_2bf", "BEST_SALES_2BF"), ("best_eps_2bf", "BEST_EPS_2BF")]:
            if original in row and pd.notna(row[original]):
                values[renamed] = row[original]
        main[security] = dict(security=security, **values, snapshot_date="2026-10-05", reference_source="earlier_russell_analysis_same_day")
    # Preserve unknown constituents explicitly; these are not claimed to have company data.
    for security in target - set(main):
        main[security] = dict(security=security, snapshot_date="2026-10-05", reference_source="membership_only_no_reference")
    result = pd.DataFrame(main.values()).sort_values("security").reset_index(drop=True)
    result["reference_available"] = result["NAME"].notna()
    for index in rr.INDICES.values():
        current = members[(members["membership_kind"] == "current") & (members["index"] == index)]
        result["in_" + index + "_source"] = result["security"].isin(current["security"])
    rr.parquet(OUTPUT / "recovered_reference_snapshot.parquet", result)
    return result


def recover_liquidity(members):
    old = pd.read_csv(ROOT / "russell1000_liquidity_analysis" / "daily_liquidity_20260701_20261002.csv")
    old["date"] = pd.to_datetime(old["date"])
    old = old[old["security"].isin(members["security"])]
    # Historical float/cap/total-return series were not collected in this earlier dataset.
    # Keep those absent rather than backfilling from today's reference snapshot.
    old = old[[column for column in ["security", "date", "PX_LAST", "PX_VOLUME", "TURNOVER", "split_adjusted_price"] if column in old]].copy()
    old["data_source"] = "earlier_russell_analysis_consolidated_us"
    rr.parquet(OUTPUT / "daily_parts" / "legacy-russell1000.parquet", old)
    monthly = rr.monthly_features(old)
    monthly["data_source"] = "earlier_russell_analysis_consolidated_us"
    rr.parquet(OUTPUT / "monthly_parts" / "legacy-russell1000.parquet", monthly)
    return len(old)


def recover_fundamentals(members, reference):
    available = {}
    allowed = set(members["security"])
    currency = reference.set_index("security").get("EQY_FUND_CRNCY", pd.Series(dtype="str"))
    for periodicity in ["annual", "quarterly"]:
        frames = []
        for path in sorted((ROOT / "data" / ("fundamentals_" + periodicity)).glob("*.parquet")):
            frame = pd.read_parquet(path)
            frame["source_ticker"] = frame["ticker"]
            frame["security"] = frame["ticker"].map(rr.composite)
            frame = frame[frame["security"].isin(allowed)].copy()
            if frame.empty:
                continue
            for field in rr.FUND_FIELDS:
                if field not in frame:
                    frame[field] = np.nan
                frame[field] = pd.to_numeric(frame[field], errors="coerce")
                if field in AMOUNTS:
                    # Checked AAPL June 2026: legacy revenue 109417 million,
                    # SCALING_FORMAT=UNT pilot revenue 109417000000 units.
                    frame[field] *= 1e6
            frame["fundamental_currency_current_reference"] = frame["security"].map(currency)
            frame["date"] = pd.to_datetime(frame["date"])
            frame["source_dataset"] = path.name
            frame["point_in_time_statement_vintage_verified"] = False
            frames.append(frame[["security", "date", "source_ticker", "source_dataset", "fundamental_currency_current_reference",
                                 "point_in_time_statement_vintage_verified", *rr.FUND_FIELDS]])
        if not frames:
            continue
        result = pd.concat(frames, ignore_index=True).drop_duplicates(["security", "date"], keep="last")
        result = result.dropna(how="all", subset=rr.FUND_FIELDS).sort_values(["security", "date"])
        rr.parquet(OUTPUT / ("fundamentals_" + periodicity + "_available.parquet"), result)
        result.to_csv(OUTPUT / ("fundamentals_" + periodicity + "_available.csv"), index=False)
        available[periodicity] = dict(securities=int(result["security"].nunique()), rows=len(result),
                                     first_date=str(result["date"].min().date()), last_date=str(result["date"].max().date()),
                                     monetary_values="native currency units converted from legacy millions; EPS and percentage ratios unchanged")
    return available


def main():
    members = pd.read_parquet(OUTPUT / "index_membership.parquet")
    reference = recover_reference(members)
    daily_rows = recover_liquidity(members)
    fundamental_coverage = recover_fundamentals(members, reference)
    args = Namespace(output=OUTPUT, snapshot_date="2026-10-05", as_of="2026-10-02", daily_start="2021-01-01", fundamental_start="2015-01-01")
    summary = rr.build_outputs(members, reference, args)
    summary["status"] = "partial_daily_capacity_reached"
    summary["reference_sources"] = reference["reference_source"].value_counts().to_dict()
    summary["current_securities_with_reference"] = {index: int((reference["reference_available"] & reference["in_" + index + "_source"]).sum()) for index in rr.INDICES.values()}
    summary["reused_consolidated_daily_rows"] = daily_rows
    summary["available_financial_history"] = fundamental_coverage
    summary["requested_long_daily_history_remaining"] = True
    rr.atomic_json(OUTPUT / "coverage_summary.json", summary)
    rr.atomic_json(OUTPUT / "recovery_status.json", {"status": "partial_exports_ready", "api_requests": 0})
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
