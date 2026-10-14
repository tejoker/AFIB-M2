"""Intraday momentum day table, short-term momentum book and the GPT news request, on synthetic data."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cme_challenge"))
import intraday_momentum as im
import news_request as nr
import short_term_momentum as stm


def hourly(days, instrument=None):
    """Hourly UTC bars over `days` business days; prices rise 1 per hour; instrument id per day."""
    idx = pd.date_range("2026-03-02", periods=24 * days, freq="h", tz=im.CT).tz_convert("UTC")
    price = 100 + np.arange(len(idx), dtype=float)
    day = idx.tz_convert(im.CT).tz_localize(None).normalize()
    ids = [instrument(d) if instrument else 1 for d in day]
    return pd.DataFrame({"instrument_id": ids, "open": price, "high": price + 0.5, "low": price - 0.5,
                         "close": price + 0.5, "volume": 10}, index=idx)


class Intraday(unittest.TestCase):
    def test_returns_use_the_close_hour_in_chicago_time(self):
        d = im.daily_table(hourly(5), first_hour=8, close_hour=13)
        first = d.iloc[0]
        self.assertEqual(first["open"], 100 + 24 + 13)          # second day, 13:00 CT bar
        self.assertEqual(first["prev_close"], 100 + 13 + 0.5)   # first day's 13:00 CT bar close
        self.assertAlmostEqual(first["last"], 0.5 / (100 + 24 + 13))
        self.assertTrue((d["rod"] > 0).all())

    def test_roll_days_are_skipped(self):
        roll = pd.Timestamp("2026-03-04")
        d = im.daily_table(hourly(5, lambda day: 2 if day >= roll else 1), 8, 13)
        self.assertNotIn(roll, d.index)
        self.assertIn(roll + pd.Timedelta(days=1), d.index)


class ShortTerm(unittest.TestCase):
    def test_momentum_book_earns_on_persistent_trends_without_look_ahead(self):
        days = pd.bdate_range("2015-01-01", periods=400)
        rng = np.random.default_rng(0)
        drift = np.linspace(-0.5, 0.5, 8)  # markets keep their own direction
        scaled = pd.DataFrame(rng.normal(drift, 1.0, (400, 8)), index=days, columns=list("ABCDEFGH"))
        for kind in ("xs", "ts"):
            self.assertGreater(stm.run(scaled, kind, 5, 5).mean(), 0.1)
        noise = pd.DataFrame(rng.normal(0, 1.0, (400, 8)), index=days, columns=list("ABCDEFGH"))
        self.assertLess(abs(stm.run(noise, "ts", 1, 1).mean()), 0.1)  # no edge from noise


class News(unittest.TestCase):
    def test_template_is_strict_and_complete(self):
        req = json.loads(nr.TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(req["temperature"], 0)
        spec = req["response_format"]["json_schema"]
        self.assertTrue(spec["strict"])

        def check(node):  # strict mode: every object closed, every property required
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for v in node.values():
                    check(v)
            elif isinstance(node, list):
                for v in node:
                    check(v)
        check(spec["schema"])

    def test_headlines_parsed_with_and_without_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "h.txt"
            f.write_text("07:05 | Reuters | EIA crude draw\n\n# skip\nGold steady\n", encoding="utf-8")
            rows = nr.read_headlines(f, "2026-10-14")
        self.assertEqual([r["id"] for r in rows], ["h1", "h2"])
        self.assertEqual(rows[0]["time"], "2026-10-14 07:05 CT")
        self.assertEqual(rows[1]["source"], "unknown")


if __name__ == "__main__":
    unittest.main()
