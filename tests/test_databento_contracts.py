"""Databento collector against a fake client: contract identity, roll dates, increments and the cost guard."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cme_challenge"))
import continuous
import databento_contracts as dc


class FakeError(Exception):
    pass


def bars_for(iid, start, end, volume):
    days = pd.bdate_range(start, end, tz="UTC")
    vol = volume(days) if callable(volume) else np.full(len(days), volume)
    close = 200 + np.arange(len(days)) * 0.1
    return pd.DataFrame({"instrument_id": iid, "open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": vol, "symbol": "LE.FUT"}, index=pd.Index(days, name="ts_event"))


ROLL = pd.Timestamp("2015-11-20")  # volumes cross here; the 3-day averages cross one trading day later
ROLL_DT = ROLL + pd.offsets.BDay(1)
INSTRUMENTS = {  # instrument id: (raw symbol, first day, last day, volume)
    101: ("LEZ5", "2015-06-01", "2015-12-31", lambda d: np.where(d.tz_localize(None) < ROLL, 9000, 100)),
    102: ("LEG6", "2015-08-03", "2016-02-29", lambda d: np.where(d.tz_localize(None) < ROLL, 1000, 8000)),
    201: ("LEZ5", "2025-06-02", "2025-12-31", 5000),  # the same symbol ten years later
    202: ("LEG6", "2025-08-01", "2026-02-27", 4000),
    301: ("LEZ6", "2026-06-01", "2026-10-09", 6000),  # still trading
    400: ("LEF6", "2025-12-01", "2026-01-30", 50),    # not a liquid live cattle month
    900: ("LEZ5-LEG6", "2015-09-01", "2015-10-01", 300),  # calendar spread
}


class FakeClient:
    def __init__(self, cost=1.0, eod=True):
        self.cost, self.eod, self.ranges = cost, eod, []
        self.frame = pd.concat([bars_for(i, a, b, v) for i, (_, a, b, v) in INSTRUMENTS.items()]).sort_index()
        self.metadata = SimpleNamespace(get_cost=self.get_cost, get_dataset_range=lambda dataset: {
            "start": "2010-06-06T00:00:00.000000000Z", "end": "2026-10-10T00:00:00.000000000Z"})
        self.timeseries = SimpleNamespace(get_range=self.get_range)
        self.symbology = SimpleNamespace(resolve=self.resolve)

    def get_cost(self, dataset, symbols, stype_in, schema, start, end):
        if schema == "ohlcv-eod" and not self.eod:
            raise FakeError("schema not available")
        return self.cost

    def get_range(self, dataset, symbols, stype_in, schema, start, end):
        self.ranges.append((symbols[0], str(start), str(end), schema))
        f = self.frame if symbols == ["LE.FUT"] else self.frame.iloc[:0]
        sel = f[(f.index >= pd.Timestamp(start, tz="UTC")) & (f.index < pd.Timestamp(end, tz="UTC"))]
        return SimpleNamespace(to_df=lambda: sel.copy())

    def resolve(self, dataset, symbols, stype_in, stype_out, start_date, end_date):
        spans = {}
        for s in symbols:
            raw, a, b, _ = INSTRUMENTS[int(s)]
            spans[s] = [{"d0": a, "d1": str((pd.Timestamp(b) + pd.Timedelta(days=1)).date()), "s": raw}]
        return {"result": spans}


class DatabentoCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        out = Path(self.tmp.name) / "databento"
        self.saved = (dc.OUT, dc.STATE_FILE, dc.connect, dc.client_error, dc.price_scale, dc.yahoo_expiries)
        dc.OUT, dc.STATE_FILE = out, out / "state.json"
        dc.client_error = lambda: FakeError
        dc.price_scale = lambda *a: 1.0
        dc.yahoo_expiries = lambda: {}

    def tearDown(self):
        dc.OUT, dc.STATE_FILE, dc.connect, dc.client_error, dc.price_scale, dc.yahoo_expiries = self.saved
        self.tmp.cleanup()

    def run_main(self, client, *args):
        dc.connect = lambda: client
        sys.argv = ["databento_contracts.py", "--sectors", "Livestock", *args]
        dc.main()

    def test_contracts_rolls_and_increment(self):
        client = FakeClient()
        self.run_main(client)
        contracts = pd.read_parquet(dc.OUT / "contracts.parquet")
        le = contracts[contracts["root"] == "LC"].set_index("ticker")
        self.assertEqual(set(le.index), {"LEZ15", "LEG16", "LEZ25", "LEG26", "LEZ26"})
        self.assertEqual(le.loc["LEZ15", "delivery"], pd.Timestamp("2015-12-01"))
        self.assertEqual(le.loc["LEZ15", "LAST_TRADEABLE_DT"], pd.Timestamp("2015-12-31"))
        self.assertEqual(le.loc["LEZ15", "ROLL_DT"], ROLL_DT)  # next contract's 3-day volume took over
        history = pd.read_parquet(dc.OUT / "history" / "LC" / "LEZ25.parquet")
        self.assertEqual(history["date"].min(), pd.Timestamp("2025-06-02"))
        self.assertEqual(client.ranges[0][3], "ohlcv-eod")

        second = FakeClient()
        self.run_main(second)
        le_ranges = [r for r in second.ranges if r[0] == "LE.FUT"]
        self.assertEqual(le_ranges[0][1], "2026-10-03")  # a week before the last run's end, not 2010

    def test_series_switch_on_roll_date(self):
        self.run_main(FakeClient())
        contracts = pd.read_parquet(dc.OUT / "contracts.parquet")
        le = contracts[contracts["root"] == "LC"]
        prices = continuous.load_root(dc.OUT / "history" / "LC")
        s = continuous.build(le, prices)
        self.assertEqual(s.loc[ROLL_DT, "contract"], "LEG16")  # held from that close on
        self.assertEqual(s.loc[ROLL_DT - pd.offsets.BDay(1), "contract"], "LEZ15")

    def test_cost_guard_and_schema_fallback(self):
        with self.assertRaises(SystemExit):
            self.run_main(FakeClient(cost=100.0))
        self.assertFalse((dc.OUT / "contracts.parquet").exists())
        client = FakeClient(eod=False)
        self.run_main(client)
        self.assertEqual(client.ranges[0][3], "ohlcv-1d")

    def test_scale_from(self):
        self.assertEqual(dc.scale_from(227.05 / 2.2705), 100.0)
        self.assertEqual(dc.scale_from(1.004), 1.0)
        self.assertEqual(dc.scale_from(float("nan")), 1.0)


if __name__ == "__main__":
    unittest.main()
