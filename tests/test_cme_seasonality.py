"""Check that seasonality excludes incomplete months and reports sample depth."""

import csv
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cme_challenge"))
from summarize_seasonality import monthly_observations, summarize


class SeasonalitySummary(unittest.TestCase):
    def test_completed_historical_months_and_sample_depth(self):
        rows = []
        for year in range(2015, 2023):
            rows += [{"date": f"{year}-04-{day:02d}", "close": 100 if day == 1 else 110}
                     for day in range(1, 16)]
        rows += [{"date": f"2023-04-{day:02d}", "close": 100} for day in range(1, 4)]
        rows += [{"date": f"2026-10-{day:02d}", "close": 100} for day in range(1, 16)]
        months = monthly_observations("CL", rows, date(2026, 10, 5))
        self.assertEqual(len(months), 8)
        summary = summarize(months, min_years=8)
        self.assertEqual(summary[0]["years"], 8)
        self.assertAlmostEqual(summary[0]["mean_return_pct"], 10)
        self.assertTrue(summary[0]["min_years_met"])
        self.assertFalse(summarize(months, min_years=9)[0]["min_years_met"])

    def test_scale_jump_is_retained_but_excluded_from_summary(self):
        rows = [{"date": f"2012-04-{day:02d}", "close": 167 if day < 10 else 1667}
                for day in range(1, 20)]
        months = monthly_observations("MGC", rows, date(2013, 1, 1))
        self.assertTrue(months[0]["suspicious_jump"])
        result = summarize(months)[0]
        self.assertEqual(result["years"], 0)
        self.assertEqual(result["excluded_suspicious_months"], 1)

    def test_cli_writes_readable_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with (folder / "universe.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["symbol", "rows"])
                writer.writeheader()
                writer.writerow({"symbol": "GC", "rows": 4})
            with (folder / "GC.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["date", "close"])
                writer.writeheader()
                for day in range(1, 5):
                    writer.writerow({"date": f"2020-05-{day:02d}", "close": 100 + day})
            run = subprocess.run([sys.executable, str(REPO / "cme_challenge" / "summarize_seasonality.py"),
                                  "--input", str(folder), "--as-of", "2020-06-01", "--min-days", "2"],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            with (folder / "seasonality_summary.csv").open(newline="", encoding="utf-8") as stream:
                result = list(csv.DictReader(stream))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["symbol"], "GC")
            self.assertEqual(result[0]["min_years_met"], "False")


if __name__ == "__main__":
    unittest.main()
