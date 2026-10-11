"""Run bloomberg/futures_contracts.py against a fake Bloomberg (used by the tests).

Usage: python tests/fake_futures_collect.py <requests before a simulated limit> [collector arguments...]
Contracts expire on the 20th of their month, from 2003 to next year; expired ones use two-digit years
like Bloomberg (LCZ05), live ones one digit (LCZ6). A thin January contract must be skipped.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bloomberg"))
import futures_contracts as fc

limit_after = int(sys.argv[1])
TODAY = date.today()


def chain_for(root):
    key, months = fc.ROOTS[root][0], fc.ROOTS[root][1]
    out = {}
    for year in range(2003, TODAY.year + 2):
        for code in fc.MONTH_CODES:
            if code not in months and not (code == "F" and year == 2012):
                continue
            ltd = date(year, fc.MONTH_CODES.index(code) + 1, 20)
            digits = f"{year % 100:02d}" if ltd < TODAY else f"{year % 10}"
            out[f"{root}{code}{digits} {key}"] = ltd
    return out


CONTRACTS = {t: ltd for root in fc.ROOTS for t, ltd in chain_for(root).items()}


class FakeBloomberg:
    def __init__(self, max_requests=None):
        self.max_requests, self.requests = max_requests, 0

    def stop(self):
        pass

    def _tick(self):
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise fc.BudgetExhausted(f"request budget of {self.max_requests} used")
        self.requests += 1
        if self.requests > limit_after:
            raise fc.LimitReached('category = "LIMIT" DAILY_CAPACITY_REACHED (simulated)')

    def chain(self, generic):
        self._tick()
        root = generic.rsplit(" ", 1)[0][:-1]
        return list(chain_for(root))

    def reference(self, securities, fields):
        self._tick()
        rows = [{"ticker": s, "LAST_TRADEABLE_DT": CONTRACTS[s], "FUT_NOTICE_FIRST": CONTRACTS[s] - timedelta(days=10),
                 "FUT_VAL_PT": 400.0, "FUT_TICK_SIZE": 0.025, "FUT_TICK_VAL": 10.0, "CRNCY": "USD"}
                for s in securities if s in CONTRACTS]
        return pd.DataFrame(rows), [s for s in securities if s not in CONTRACTS]

    def history(self, securities, fields, start, periodicity, fiscal=False, extra=None):
        self._tick()
        start = datetime.strptime(start, "%Y%m%d").date()
        end = datetime.strptime(extra["endDate"], "%Y%m%d").date()
        rows = []
        for s in securities:
            ltd = CONTRACTS[s]
            for day in pd.bdate_range(max(start, ltd - timedelta(days=400)), min(end, ltd)):
                rows.append({"ticker": s, "date": day.date(), "PX_LAST": 100 + day.dayofyear / 10, "OPEN_INT": 1000.0})
        return pd.DataFrame(rows), [], set()


fc.FuturesBloomberg = FakeBloomberg
sys.argv = ["futures_contracts.py", *sys.argv[2:]]
fc.main()
