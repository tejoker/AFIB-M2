"""Run bloomberg/bbg_collect.py for one account against a fake Bloomberg (used by the tests).

Usage: python tests/fake_collect.py <account> <requests before a simulated limit>
"""
import random
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bloomberg"))
import bbg_collect as bc

account, limit_after = sys.argv[1], int(sys.argv[2])


class FakeBloomberg:
    def __init__(self, max_requests=None):
        self.requests = 0

    def stop(self):
        pass

    def _tick(self):
        self.requests += 1
        if self.requests > limit_after:
            raise bc.LimitReached('category = "LIMIT" DAILY_CAPACITY_REACHED (simulated)')
        time.sleep(random.uniform(0.005, 0.03))

    def field_info(self, fields):
        return {f: {"description": f, "ftype": "Real"} for f in fields}

    def reference(self, secs, fields):
        self._tick()
        return pd.DataFrame([{"ticker": s, "NAME": s, "CDS_SPREAD_TICKER_5Y": "C" + s} for s in secs]), []

    def history(self, secs, fields, start, per, fiscal=False, extra=None):
        self._tick()
        rows = [{"ticker": s, "date": pd.Timestamp("2020-12-31"), **{f: 1.0 for f in fields}} for s in secs]
        return pd.DataFrame(rows), [], set()


bc.Bloomberg = FakeBloomberg
sys.argv = ["bbg_collect.py", "--account", account]
bc.main()
