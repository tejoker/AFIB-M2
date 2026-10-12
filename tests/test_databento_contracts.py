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


def bars_for(iid, raw, start, end, volume):
    days = pd.bdate_range(start, end, tz="UTC")
    vol = volume(days) if callable(volume) else np.full(len(days), volume)
    close = 200 + np.arange(len(days)) * 0.1
    return pd.DataFrame({"instrument_id": iid, "open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": vol, "symbol": raw}, index=pd.Index(days, name="ts_event"))


ROLL = pd.Timestamp("2015-11-20")  # volumes cross here; the 3-day averages cross one trading day later
ROLL_DT = ROLL + pd.offsets.BDay(1)
INSTRUMENTS = {  # instrument id: (raw symbol, first day, last day, volume)
    101: ("LEZ5", "2015-06-01", "2015-12-31", lambda d: np.where(d.tz_localize(None) < ROLL, 9000, 100)),
    102: ("LEG6", "2015-08-03", "2016-02-29", lambda d: np.where(d.tz_localize(None) < ROLL, 1000, 8000)),
    201: ("LEZ5", "2025-06-02", "2025-12-31", 5000),  # the same symbol ten years later
    211: ("LEZ5", "2017-03-01", "2017-03-03", 1),     # ... which already traded a little in 2017 (same contract)
    202: ("LEG6", "2025-08-01", "2026-02-27", 4000),
    301: ("LEZ6", "2026-06-01", "2026-10-09", 6000),  # still trading
    400: ("LEF6", "2025-12-01", "2026-01-30", 50),    # not a liquid live cattle month
    900: ("LEZ5-LEG6", "2015-09-01", "2015-10-01", 300),  # calendar spread
    501: ("LEJ8", "2016-05-02", "2016-05-06", 2),      # listed 12 years ahead (like natural gas): April 2028
}
DELIVERY = {101: (2015, 12), 102: (2016, 2), 201: (2025, 12), 211: (2025, 12), 202: (2026, 2), 301: (2026, 12),
            400: (2026, 1), 501: (2028, 4)}
EXPIRY = {501: "2028-04-28"}  # a contract that has not expired; the others expire on their last bar


class FakeClient:
    def __init__(self, cost=1.0, eod=True, label_with_product=False):
        self.cost, self.eod, self.ranges = cost, eod, []
        self.frame = pd.concat([bars_for(i, r, a, b, v) for i, (r, a, b, v) in INSTRUMENTS.items()]).sort_index()
        self.label_with_product = label_with_product  # some answers label rows LE.FUT: then symbology is used
        self.metadata = SimpleNamespace(get_cost=self.get_cost, get_dataset_range=lambda dataset: {
            "start": "2010-06-06T00:00:00.000000000Z", "end": "2026-10-10T00:00:00.000000000Z"})
        self.timeseries = SimpleNamespace(get_range=self.get_range)
        self.symbology = SimpleNamespace(resolve=self.resolve)

    def get_cost(self, dataset, symbols, stype_in, schema, start, end):
        if schema == "ohlcv-eod" and not self.eod:
            raise FakeError("schema not available")
        return self.cost

    def get_range(self, dataset, symbols, stype_in, schema, start, end):
        if schema == "definition":
            return SimpleNamespace(to_df=lambda: self.definitions(symbols, pd.Timestamp(start)))
        self.ranges.append((symbols[0][:2], str(start), str(end), schema))
        f = self.frame[self.frame["symbol"].isin(symbols)]  # only what was asked for: no spreads, no thin months
        sel = f[(f.index >= pd.Timestamp(start, tz="UTC")) & (f.index < pd.Timestamp(end, tz="UTC"))].copy()
        if self.label_with_product:
            sel["symbol"] = "LE.FUT"
        return SimpleNamespace(to_df=lambda: sel.copy())

    def definitions(self, symbols, day):
        rows = []
        for iid, (raw, first, last, _) in INSTRUMENTS.items():
            expiry = pd.Timestamp(EXPIRY.get(iid, last))
            if iid in DELIVERY and raw in symbols and pd.Timestamp(first) - pd.Timedelta(days=60) <= day <= expiry:
                rows.append({"instrument_id": iid, "raw_symbol": raw, "expiration": expiry.tz_localize("UTC"),
                             "maturity_year": DELIVERY[iid][0], "maturity_month": DELIVERY[iid][1]})
        return pd.DataFrame(rows, index=pd.Index([day.tz_localize("UTC")] * len(rows), name="ts_recv"))

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
        self.assertEqual(set(le.index), {"LEZ15", "LEG16", "LEZ25", "LEG26", "LEZ26", "LEJ28"})
        self.assertEqual(le.loc["LEJ28", "LAST_TRADEABLE_DT"], pd.Timestamp("2028-04-28"))  # definition, not a guess
        self.assertEqual(le.loc["LEZ15", "delivery"], pd.Timestamp("2015-12-01"))
        self.assertEqual(le.loc["LEZ15", "LAST_TRADEABLE_DT"], pd.Timestamp("2015-12-31"))
        self.assertEqual(le.loc["LEZ15", "ROLL_DT"], ROLL_DT)  # next contract's 3-day volume took over
        history = pd.read_parquet(dc.OUT / "history" / "LC" / "LEZ25.parquet")
        self.assertEqual(history["date"].min(), pd.Timestamp("2017-03-01"))  # early quiet trading kept with it
        self.assertEqual(len(pd.read_parquet(dc.OUT / "history" / "LC" / "LEZ15.parquet")),
                         len(pd.bdate_range("2015-06-01", "2015-12-31")))  # not overwritten by the 2025 contract
        self.assertEqual(client.ranges[0][3], "ohlcv-eod")

        second = FakeClient()
        self.run_main(second)
        le_ranges = [r for r in second.ranges if r[0] == "LE"]
        self.assertEqual(le_ranges[0][1], "2026-10-03")  # a week before the last run's end, not 2010

    def test_product_labels_resolved_through_symbology(self):
        self.run_main(FakeClient(label_with_product=True))
        contracts = pd.read_parquet(dc.OUT / "contracts.parquet")
        self.assertEqual(set(contracts["ticker"]), {"LEZ15", "LEG16", "LEZ25", "LEG26", "LEZ26", "LEJ28"})

    def test_rebuild_from_saved_bars_buys_no_bars(self):
        self.run_main(FakeClient())
        client = FakeClient()
        self.run_main(client, "--rebuild")
        self.assertEqual(client.ranges, [])  # only definitions (already cached) were looked at
        contracts = pd.read_parquet(dc.OUT / "contracts.parquet")
        self.assertIn("LEZ15", set(contracts["ticker"]))

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

    def test_roots_by_cme_symbol(self):
        client = FakeClient()
        dc.connect = lambda: client
        sys.argv = ["databento_contracts.py", "--roots", "LE", "ZM", "--estimate"]
        dc.main()
        sys.argv = ["databento_contracts.py", "--roots", "LE", "--yes"]
        dc.main()
        self.assertEqual({r[0] for r in client.ranges}, {"LE"})  # nothing else downloaded
        sys.argv = ["databento_contracts.py", "--roots", "XX"]
        with self.assertRaises(SystemExit):
            dc.main()

    def test_env_file_key(self):
        env = Path(self.tmp.name) / ".env"
        env.write_text('# my key\nOTHER=1\nDATABENTO_API_KEY="db-abc123"  # personal\n', encoding="utf-8")
        self.assertEqual(dc.env_file_key(env), "db-abc123")
        self.assertIsNone(dc.env_file_key(Path(self.tmp.name) / "missing.env"))

    def test_roll_waits_until_contract_is_front(self):
        days = pd.bdate_range("2026-08-03", "2026-12-31")
        def hist(t, vol):
            return pd.DataFrame({"ticker": t, "date": days, "PX_VOLUME": vol})
        front = np.where(days < "2026-10-20", 900, 10)    # October contract, expires 28 Oct
        dec = np.where(days < "2026-10-20", 100, 800)      # December takes over from 20 Oct
        jan = np.where(days == pd.Timestamp("2026-09-18"), 5000, 50)  # one-day burst while still third in line
        table = pd.DataFrame({"ticker": ["X", "Z", "F"], "delivery": pd.to_datetime(["2026-11-01", "2026-12-01", "2027-01-01"]),
                              "LAST_TRADEABLE_DT": pd.to_datetime(["2026-10-28", "2026-11-25", "2026-12-29"])})
        rolls = dc.roll_dates(table, {"X": hist("X", front), "Z": hist("Z", dec), "F": hist("F", jan)}).set_index("ticker")
        self.assertEqual(rolls.loc["X", "ROLL_DT"], pd.Timestamp("2026-10-21"))
        self.assertTrue(pd.isna(rolls.loc["Z", "ROLL_DT"]))  # the September burst is ignored: calendar rule applies

    def test_scale_from(self):
        self.assertEqual(dc.scale_from(227.05 / 2.2705), 100.0)
        self.assertEqual(dc.scale_from(1.004), 1.0)
        self.assertEqual(dc.scale_from(float("nan")), 1.0)


if __name__ == "__main__":
    unittest.main()
