"""Live dashboard pieces, offline: contract choice, signals, weather arithmetic, model hygiene and rendering."""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cme_challenge"))
import live
import model
import refresh
import weather
from futures_markets import MARKETS

TODAY = date(2026, 10, 12)


def frame(prices, volume, end="2026-10-09"):
    idx = pd.bdate_range(end=end, periods=len(prices))
    return pd.DataFrame({"Close": prices, "Volume": volume}, index=idx)


def cattle_frames():
    up = 200 * 1.0005 ** np.arange(300)
    return {
        "LEV26.CME": frame(up - 2, 90_000),           # most volume, but expires within 5 trading days
        "LEZ26.CME": frame(up, 50_000),
        "LEG27.CME": frame(up * 1.01, 10_000),        # contango: next contract 1% higher
        "LEJ27.CME": frame(up * 1.02, 5_000, end="2026-09-01"),  # stale: no recent prices
    }


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        live.SERIES = Path(self.tmp.name)  # no Bloomberg series: seasonality stays empty

    def tearDown(self):
        self.tmp.cleanup()

    def test_listed_contracts_from_this_month(self):
        tickers = [t for t, _ in live.listed_contracts(MARKETS["LC"], TODAY)]
        self.assertEqual(tickers[:3], ["LEV26.CME", "LEZ26.CME", "LEG27.CME"])

    def test_choose_skips_expiring_and_stale_contracts(self):
        contracts = live.listed_contracts(MARKETS["LC"], TODAY)
        trade, nxt = live.choose(contracts, cattle_frames(), {"LEV26.CME": "2026-10-16"}, TODAY)
        self.assertEqual(trade[0], "LEZ26.CME")
        self.assertEqual(nxt[0], "LEG27.CME")

    def test_market_row_signals_and_size(self):
        contracts = live.listed_contracts(MARKETS["LC"], TODAY)
        coef = {"trend_21d": 0.1, "trend_63d": 0.1, "trend_126d": 0.1, "trend_252d": 0.1, "carry": 1.0, "seasonal": 0.5}
        row = live.market_row("LC", MARKETS["LC"], contracts, cattle_frames(), {"LEV26.CME": "2026-10-16"}, TODAY,
                              {"coefficients": coef}, risk=5000)
        self.assertEqual(row["contract"], "LEZ26")
        self.assertEqual(row["trend"], 1.0)
        self.assertLess(row["carry_pct_yr"], 0)  # contango
        self.assertTrue(np.isnan(row["seasonal"]))
        expected_view = 0.4 + row["carry_pct_yr"] / 100
        self.assertAlmostEqual(row["model_view"], expected_view)
        self.assertAlmostEqual(row["contracts_for_risk"] * row["daily_vol_usd"], 5000)
        self.assertEqual(row["votes"], 1)  # trend up, carry down, model up


class Sizing(unittest.TestCase):
    def test_calm_spell_cannot_more_than_double_size(self):
        rng = np.random.default_rng(5)
        ret = pd.Series(np.r_[rng.normal(0, 0.02, 300), rng.normal(0, 0.002, 21)])  # suddenly very calm
        self.assertAlmostEqual(live.sizing_vol(ret), ret.iloc[-252:].std() / 2)
        noisy = pd.Series(np.r_[rng.normal(0, 0.01, 300), rng.normal(0, 0.03, 21)])  # suddenly wild: follow it
        self.assertAlmostEqual(live.sizing_vol(noisy), noisy.iloc[-21:].std())

    def test_band_keeps_size_until_target_moves(self):
        import futures_research as fr
        ret = pd.Series(np.r_[np.full(30, 0.01) * np.tile([1, -1], 15), np.full(30, 0.0105) * np.tile([1, -1], 15)])
        w = fr.sized_weights(ret, 21, band=0.25)
        self.assertEqual(w.dropna().nunique(), 1)  # a 5% change in volatility does not trigger a trade


class Conviction(unittest.TestCase):
    def test_multiplier_follows_agreement(self):
        self.assertEqual(live.conviction(1.0, 0.2), 2.0)     # all four lookbacks and the model agree
        self.assertEqual(live.conviction(0.5, 0.2), 1.0)     # trend and model agree
        self.assertEqual(live.conviction(1.0, -0.1), 0.5)    # model disagrees
        self.assertEqual(live.conviction(0.0, 0.3), 0.5)     # no trend
        self.assertEqual(live.conviction(-1.0, np.nan), 1.0)  # no model yet: full trend agreement only


class Weather(unittest.TestCase):
    @staticmethod
    def normal(cities, hdd, cdd=0.0):
        return {"hdd": pd.DataFrame({c: hdd for c in cities}, index=range(1, 367)),
                "cdd": pd.DataFrame({c: cdd for c in cities}, index=range(1, 367))}

    def test_outlook_against_normal_and_previous_run(self):
        days = pd.date_range("2026-10-12", periods=15)
        temps = pd.DataFrame({"New York": 55.0, "Chicago": 55.0}, index=days)  # 10 HDD a day each
        previous = pd.DataFrame({"fetched_at": "2026-10-11T08:00", "model": "AI", "date": days[:10], "tdd": 12.0})
        row = weather.outlook({"AI": temps}, self.normal(["New York", "Chicago"], 8.0), previous).iloc[0]
        self.assertEqual(row["hdd_days_1_7"], 70)
        self.assertEqual(row["cdd_days_1_7"], 0)
        self.assertEqual(row["hdd_days_8_15"], 80)
        self.assertEqual(row["vs_normal_days_1_7"], 14)
        self.assertEqual(row["change_vs_previous"], -20)  # 2 degree days fewer on each of the 10 common days

    def test_heat_counts_as_gas_demand(self):
        days = pd.date_range("2026-07-01", periods=15)
        temps = pd.DataFrame({"Miami": 85.0, "Seattle": 65.0}, index=days)  # 20 CDD in Miami, none in Seattle
        row = weather.outlook({"AI": temps}, self.normal(["Miami", "Seattle"], 0.0, 10.0)).iloc[0]
        miami = weather.COOLING_WEIGHT["Miami"] / (weather.COOLING_WEIGHT["Miami"] + weather.COOLING_WEIGHT["Seattle"])
        self.assertEqual(row["hdd_days_1_7"], 0)
        self.assertAlmostEqual(row["cdd_days_1_7"], 7 * 20 * miami)
        self.assertGreater(miami, 0.9)  # Miami's power is mostly gas, Seattle's mostly hydro
        self.assertAlmostEqual(row["vs_normal_days_1_7"], 7 * (20 * miami - 10))

    def test_short_forecast_is_not_read_as_mild(self):
        temps = pd.DataFrame({"New York": 55.0}, index=pd.date_range("2026-10-12", periods=10))
        row = weather.outlook({"AI": temps}, self.normal(["New York"], 8.0)).iloc[0]
        self.assertTrue(np.isnan(row["tdd_days_8_15"]))


class Model(unittest.TestCase):
    def test_seasonal_feature_uses_earlier_years_only(self):
        days = pd.date_range("2000-01-01", "2010-12-31")
        target = pd.Series(days.year.astype(float), index=days)
        seasonal = model.seasonal_feature(target, min_years=5)
        self.assertTrue(np.isnan(seasonal["2004-06-01"]))  # only 4 earlier years
        self.assertAlmostEqual(seasonal["2008-06-01"], np.mean(range(2000, 2008)))

    def test_walk_forward_finds_planted_trend(self):
        rng = np.random.default_rng(3)
        days = pd.bdate_range("2000-01-03", "2020-12-31")
        series = {}
        for root in ["A", "B", "C"]:
            regime = np.repeat(rng.choice([-1, 1], size=len(days) // 120 + 1), 120)[:len(days)]
            ret = pd.Series(0.0008 * regime + rng.normal(0, 0.01, len(days)), index=days)
            series[root] = pd.DataFrame({"index": (1 + ret).cumprod(), "carry": 0.0})
        data = model.training_panel(series, 15)
        pred = model.walk_forward(data, 15, 2006)
        table = model.evaluate(pred, 15)
        self.assertGreater(table.loc["model", "hit_rate"], 0.5)
        self.assertGreater(table.loc["trend alone", "sharpe"], 0)


class Render(unittest.TestCase):
    def test_dashboard_without_model_or_weather(self):
        live.SERIES = Path(tempfile.mkdtemp())
        contracts = live.listed_contracts(MARKETS["LC"], TODAY)
        row = live.market_row("LC", MARKETS["LC"], contracts, cattle_frames(), {"LEV26.CME": "2026-10-16"}, TODAY,
                              None, risk=5000)
        table = pd.DataFrame([row])
        page = refresh.render(table, None, None, ["offline"], ["Model: needs the Bloomberg contract history"], 15, 5000)
        for text in ["CME Challenge Desk", "LEZ26", "Not trained yet", "Weather unavailable", 'content="900"']:
            self.assertIn(text, page)


if __name__ == "__main__":
    unittest.main()
