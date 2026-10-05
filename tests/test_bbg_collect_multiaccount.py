"""Three accounts collect in parallel against a fake Bloomberg; one hits its daily limit.

Checks that every ticker is collected exactly once, that the other accounts take over the
limited account's work, and that a limited account refuses to run during its cooldown.
Uses a temporary data folder, so it never touches real data or the Bloomberg API.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "tests" / "fake_collect.py"
DATASETS = ["reference", "fundamentals_annual", "prices_daily", "fundamentals_quarterly", "cds_daily",
            "prices_daily_extra"]
N_TICKERS = 150


class MultiAccountCollection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name) / "data"
        cls.data.mkdir()
        (cls.data / "accounts.json").write_text(json.dumps({"accounts": ["alice", "bob", "carol"]}))
        tickers = [f"T{i:03d} US Equity" for i in range(N_TICKERS)]
        pd.DataFrame({"ticker": tickers, "tier": [1] * 10 + [2] * 40 + [3] * 100, "indices": "X"}).to_parquet(
            cls.data / "universe.parquet", index=False)
        cls.env = {**os.environ, "BBG_DATA_DIR": str(cls.data), "BBG_LOG_DIR": str(Path(cls.tmp.name) / "logs"),
                   "BBG_CLAIM_SETTLE": "0.05"}  # local disk: claims are visible at once
        procs = [subprocess.Popen([sys.executable, str(HELPER), name, str(limit)], env=cls.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                 for name, limit in [("alice", 10**6), ("bob", 5), ("carol", 10**6)]]
        cls.results = []
        for p in procs:
            _, err = p.communicate(timeout=600)
            cls.results.append((p.returncode, err))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_codes(self):
        codes = [code for code, _ in self.results]
        self.assertEqual(codes, [0, 3, 0], [err[-2000:] for _, err in self.results])

    def test_status_after_collection(self):
        out = subprocess.run([sys.executable, str(REPO / "bloomberg" / "bbg_collect.py"), "--account", "alice",
                              "--status"], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.count("100%"), len(DATASETS), out.stdout)

    def test_every_ticker_collected_once(self):
        for dataset in DATASETS:
            parts = list((self.data / dataset).glob("*.parquet"))
            tickers = pd.concat([pd.read_parquet(p, columns=["ticker"]) for p in parts])["ticker"]
            expected = N_TICKERS
            self.assertEqual(tickers.nunique(), expected, dataset)
            key = ["ticker", "tenor"] if dataset == "cds_daily" else ["ticker"]
            frame = pd.concat([pd.read_parquet(p, columns=key) for p in parts])
            self.assertFalse(frame.duplicated().any(), f"{dataset}: fetched more than once")

    def test_limited_account_waits_for_cooldown(self):
        self.assertTrue((self.data / "state" / "limit_bob.json").exists())
        retry = subprocess.run([sys.executable, str(HELPER), "bob", str(10**6)], env=self.env,
                               capture_output=True, text=True, timeout=120)
        self.assertNotEqual(retry.returncode, 0)
        self.assertIn("hit the Bloomberg limit", retry.stderr)


if __name__ == "__main__":
    unittest.main()
