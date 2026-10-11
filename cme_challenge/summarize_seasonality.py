"""Summarize historical calendar-month patterns in saved Yahoo futures proxies.

This is descriptive research. Continuous front-month prices can jump at rolls,
so the returns below are not tradable backtest returns or trade recommendations.
"""

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

DEFAULT_INPUT = DATA / "commodities"
MAX_DAILY_RATIO = 5.0  # conservative diagnostic for scale errors and discontinuities


def monthly_observations(symbol, rows, as_of, min_days=15):
    """Return completed historical months with enough observed days and positive endpoints."""
    by_month = defaultdict(dict)
    for row in rows:
        try:
            day = date.fromisoformat(row["date"][:10])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if day >= as_of.replace(day=1) or not math.isfinite(close):
            continue
        by_month[(day.year, day.month)][day] = close

    output = []
    for (year, month), points in sorted(by_month.items()):
        if len(points) < min_days:
            continue
        first, last = min(points), max(points)
        start_close, end_close = points[first], points[last]
        if start_close <= 0 or end_close <= 0:
            continue
        prices = [points[day] for day in sorted(points)]
        suspicious_jump = any(before <= 0 or after <= 0 or
                              after / before > MAX_DAILY_RATIO or after / before < 1 / MAX_DAILY_RATIO
                              for before, after in zip(prices, prices[1:]))
        output.append({
            "symbol": symbol, "year": year, "month": month, "observations": len(points),
            "first_date": first.isoformat(), "last_date": last.isoformat(),
            "first_close": start_close, "last_close": end_close,
            "within_month_return_pct": 100 * (end_close / start_close - 1),
            "suspicious_jump": suspicious_jump,
        })
    return output


def summarize(observations, min_years=8):
    grouped = defaultdict(list)
    for row in observations:
        grouped[(row["symbol"], row["month"])].append(row)
    result = []
    for (symbol, month), rows in sorted(grouped.items()):
        valid = [row for row in rows if not row["suspicious_jump"]]
        returns = [row["within_month_return_pct"] for row in valid]
        result.append({
            "symbol": symbol, "month": month, "years": len(returns),
            "excluded_suspicious_months": len(rows) - len(valid),
            "first_year": min((row["year"] for row in valid), default=None),
            "last_year": max((row["year"] for row in valid), default=None),
            "mean_return_pct": statistics.mean(returns) if returns else None,
            "median_return_pct": statistics.median(returns) if returns else None,
            "positive_year_fraction": sum(value > 0 for value in returns) / len(returns) if returns else None,
            "min_years_met": len(returns) >= min_years,
        })
    return result


def write_csv(path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="folder created by fetch_commodities.py")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(), help="exclude this month and later (YYYY-MM-DD)")
    parser.add_argument("--min-days", type=int, default=15, help="minimum observed trading days in a month")
    parser.add_argument("--min-years", type=int, default=8, help="minimum historical years to flag a month as research-ready")
    args = parser.parse_args()
    if args.min_days < 2 or args.min_years < 2:
        parser.error("--min-days and --min-years must both be at least 2")
    universe = args.input / "universe.csv"
    if not universe.is_file():
        parser.error(f"No {universe}; run fetch_commodities.py first")

    with universe.open(newline="", encoding="utf-8") as stream:
        symbols = [row["symbol"] for row in csv.DictReader(stream) if int(row["rows"]) > 0]
    observations = []
    for symbol in symbols:
        path = args.input / f"{symbol}.csv"
        if not path.is_file():
            parser.error(f"Missing {path}; rerun fetch_commodities.py to write close-price CSV files")
        with path.open(newline="", encoding="utf-8") as stream:
            observations.extend(monthly_observations(symbol, csv.DictReader(stream), args.as_of, args.min_days))
    summary = summarize(observations, args.min_years)
    write_csv(args.input / "seasonality_months.csv", observations,
              ["symbol", "year", "month", "observations", "first_date", "last_date", "first_close", "last_close", "within_month_return_pct", "suspicious_jump"])
    write_csv(args.input / "seasonality_summary.csv", summary,
              ["symbol", "month", "years", "excluded_suspicious_months", "first_year", "last_year", "mean_return_pct", "median_return_pct", "positive_year_fraction", "min_years_met"])
    (args.input / "seasonality_method.json").write_text(json.dumps({
        "as_of": args.as_of.isoformat(), "min_days": args.min_days, "min_years": args.min_years,
        "method": "First to last available close within each completed calendar month; aggregate across years.",
        "suspicious_daily_ratio": f"exclude month from summary if adjacent positive closes differ by more than {MAX_DAILY_RATIO}x or either is nonpositive",
        "warning": "Yahoo continuous front-month series can jump at rolls or change units. Flagging large jumps does not make these tradable returns or signals.",
    }, indent=2), encoding="utf-8")
    print(f"Saved {len(observations)} historical symbol-months and {len(summary)} summaries to {args.input}")


if __name__ == "__main__":
    main()
