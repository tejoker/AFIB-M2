"""The futures contract collector skips thin months, resumes without refetching, and survives a Bloomberg limit.

Uses a fake Bloomberg and a temporary data folder, so it never touches real data or the Bloomberg API.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "tests" / "fake_futures_collect.py"


def run(env, limit, *args):
    out = subprocess.run([sys.executable, str(HELPER), str(limit), *args], env=env, capture_output=True,
                         text=True, timeout=300)
    requests = re.findall(r"\((\d+) requests this run\)", out.stdout)
    return out.returncode, int(requests[-1]) if requests else None, out.stdout + out.stderr


class FuturesCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.env = {**os.environ, "AFIB_DATA_DIR": str(self.data), "BBG_LOG_DIR": str(Path(self.tmp.name) / "logs")}

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return json.loads((self.data / "futures" / "state.json").read_text())

    def test_collects_liquid_months_and_resumes(self):
        code, first, log = run(self.env, 10**6, "--roots", "LC", "--start-year", "2010")
        self.assertEqual(code, 0, log)
        contracts = pd.read_parquet(self.data / "futures" / "contracts.parquet")
        self.assertTrue(set(contracts["month_code"]) <= set("GJMQVZ"), "thin months must be skipped")
        self.assertGreaterEqual(contracts["LAST_TRADEABLE_DT"].min(), pd.Timestamp("2010-01-01"))
        files = list((self.data / "futures" / "history" / "LC").glob("*.parquet"))
        expired = contracts[contracts["LAST_TRADEABLE_DT"].dt.date <= date.today()]
        self.assertEqual(set(self.state()["complete"]), set(expired["ticker"]))
        self.assertGreater(len(files), len(expired), "contracts still trading are saved too")
        dec = contracts[contracts["ticker"] == "LCZ15 Comdty"].iloc[0]
        self.assertEqual(dec["delivery"], pd.Timestamp("2015-12-01"))

        code, second, log = run(self.env, 10**6, "--roots", "LC", "--start-year", "2010")
        self.assertEqual(code, 0, log)
        live_months = {t.to_period("M") for t in contracts["LAST_TRADEABLE_DT"]
                       if t.date() > date.today() and (t - pd.Timedelta(days=240)).date() <= date.today()}
        self.assertEqual(second, len(live_months), "a rerun only refreshes contracts still trading")
        self.assertLess(second, first)

    def test_limit_then_resume_matches_uninterrupted_run(self):
        code, _, log = run(self.env, 4, "--roots", "LC", "--start-year", "2010")
        self.assertEqual(code, 3, log)
        partial = set(self.state()["complete"])
        code, _, log = run(self.env, 10**6, "--roots", "LC", "--start-year", "2010")
        self.assertEqual(code, 0, log)
        contracts = pd.read_parquet(self.data / "futures" / "contracts.parquet")
        expired = set(contracts.loc[contracts["LAST_TRADEABLE_DT"].dt.date <= date.today(), "ticker"])
        self.assertLess(len(partial), len(expired))
        self.assertEqual(set(self.state()["complete"]), expired)

    def test_status_needs_no_bloomberg(self):
        run(self.env, 10**6, "--roots", "LC", "--start-year", "2010")
        out = subprocess.run([sys.executable, str(REPO / "bloomberg" / "futures_contracts.py"), "--status",
                              "--start-year", "2010"], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Live Cattle", out.stdout)
        self.assertIn("not yet", out.stdout)  # other roots have no chain yet


if __name__ == "__main__":
    unittest.main()
