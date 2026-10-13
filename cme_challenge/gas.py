"""Natural gas fundamentals besides the weather: storage, Europe, LNG exports, production and hurricanes.

Sources (free, no key):
  EIA weekly storage   Lower 48 working gas in underground storage (Bcf), published Thursday 10:30 am ET for the
                       week to the previous Friday; history from 2010
  TTF                  Dutch gas, Europe's benchmark price (Yahoo "TTF=F", EUR/MWh), from late 2017. US LNG
                       terminals run near capacity, so Europe moves Henry Hub mostly through sentiment and
                       export outages, not through extra volumes
  EIA monthly          LNG exports and dry gas production (published about two months late): context only
  NHC                  active tropical storms; a Gulf storm can shut LNG terminals (less demand, bearish) or
                       offshore production (bullish): context only

Signals for natural gas, positive = bullish for the reasoning shown, known at each close:
  storage_surprise  5-year average change for that week minus the reported change, Bcf: a smaller injection or
                    larger withdrawal than usual is positive (from the Thursday of the report)
  storage_deficit   5-year average level minus the current level, as % of the average: below normal is positive
  ttf_5d            TTF's return over 5 trading days: Europe paying more is positive

Not covered, for lack of a free daily source: LNG feedgas flows, European storage (GIE AGSI+ needs a free key),
power demand including data centers (EIA-930 needs a free key). Data center demand grows over years; it is in
the price of the contracts months out, not a 1-15 day signal.

Usage (from the repository root):
    python cme_challenge/gas.py
"""
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

GAS = DATA / "futures" / "gas"
EIA = "https://www.eia.gov/dnav/ng/hist_xls/{}.xls"
STORAGE_KEY, LNG_KEY, PRODUCTION_KEY = "NW2_EPG0_SWO_R48_BCFw", "N9133US2m", "N9070US2m"
REPORT_AFTER = pd.Timedelta(days=6)  # week ending Friday, published the next Thursday
STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
GULF = {"lat": (18, 31), "lon": (-98, -80)}


def download(url, path, max_age_hours):
    """Cached download; a failed one falls back to the cached file."""
    fresh = path.exists() and pd.Timestamp.now().timestamp() - path.stat().st_mtime < max_age_hours * 3600
    if not fresh:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:
            if not path.exists():
                raise
            print(f"{url}: {e}; using the cached copy", file=sys.stderr)
    return path


def eia_series(key, max_age_hours=12):
    """One EIA history workbook as a Series indexed by period."""
    path = download(EIA.format(key), GAS / f"{key}.xls", max_age_hours)
    raw = pd.read_excel(path, sheet_name="Data 1", header=None, skiprows=3)
    return pd.Series(pd.to_numeric(raw[1], errors="coerce").to_numpy(), index=pd.to_datetime(raw[0]),
                     name=key).dropna()


def five_year_average(series, dates):
    """Average of the 5 previous years' values on the nearest date to the same day (at least 3 years)."""
    years = []
    for k in range(1, 6):
        target = dates - pd.DateOffset(years=k)
        years.append(series.reindex(target, method="nearest", tolerance=pd.Timedelta(days=4)).to_numpy())
    stacked = np.vstack(years)
    count = np.isfinite(stacked).sum(axis=0)
    total = np.where(np.isfinite(stacked), stacked, 0).sum(axis=0)
    return pd.Series(np.where(count >= 3, total / np.maximum(count, 1), np.nan), index=dates)


def storage(update=True):
    """Weekly storage: level, change, 5-year averages, surprise and deficit, and the report date."""
    level = eia_series(STORAGE_KEY, 12 if update else 1e9).sort_index()
    change = level.diff().where(level.index.to_series().diff().dt.days.eq(7))
    table = pd.DataFrame({"level": level, "change": change})
    table["avg_change_5y"] = five_year_average(change, table.index)
    table["avg_level_5y"] = five_year_average(level, table.index)
    table["storage_surprise"] = table["avg_change_5y"] - table["change"]
    table["storage_deficit"] = 100 * (table["avg_level_5y"] - table["level"]) / table["avg_level_5y"]
    table["report_date"] = table.index + REPORT_AFTER
    return table.rename_axis("week_ending")


def ttf(update=True):
    """Daily TTF settlement prices (EUR/MWh), cached."""
    path = GAS / "ttf.parquet"
    fresh = path.exists() and pd.Timestamp.now().timestamp() - path.stat().st_mtime < 6 * 3600
    if update and not fresh:
        try:
            import logging
            import yfinance as yf
            logging.getLogger("yfinance").setLevel(logging.CRITICAL)
            raw = yf.download("TTF=F", period="max", interval="1d", auto_adjust=False, progress=False)
            close = raw["Close"].iloc[:, 0] if isinstance(raw.columns, pd.MultiIndex) else raw["Close"]
            if len(close.dropna()):
                GAS.mkdir(parents=True, exist_ok=True)
                close.dropna().rename("ttf").to_frame().to_parquet(path)
        except Exception as e:  # Yahoo hiccup: keep the cached prices
            print(f"TTF from Yahoo failed ({e}); using the cached prices", file=sys.stderr)
    return pd.read_parquet(path)["ttf"] if path.exists() else pd.Series(dtype=float, name="ttf")


def monthly(update=True):
    """Latest LNG exports and dry production in Bcf/d, with the change on a year earlier."""
    out = {}
    for name, key in (("LNG exports", LNG_KEY), ("Dry gas production", PRODUCTION_KEY)):
        s = eia_series(key, 24 if update else 1e9)
        bcfd = s / s.index.days_in_month / 1000  # MMcf per month -> Bcf per day
        last = bcfd.index[-1]
        year_ago = bcfd.get(last - pd.DateOffset(years=1), np.nan)
        out[name] = {"month": last.strftime("%b %Y"), "bcf_per_day": bcfd.iloc[-1],
                     "change_vs_year_ago_pct": 100 * (bcfd.iloc[-1] / year_ago - 1)}
    return out


def storms():
    """Active tropical storms from the National Hurricane Center, Atlantic basin and whether in the Gulf."""
    request = urllib.request.Request(STORMS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        active = json.load(response).get("activeStorms", [])
    out = []
    for s in active:
        lat, lon = s.get("latitudeNumeric"), s.get("longitudeNumeric")
        in_gulf = (lat is not None and lon is not None and GULF["lat"][0] <= lat <= GULF["lat"][1]
                   and GULF["lon"][0] <= lon <= GULF["lon"][1])
        out.append({"name": s.get("name"), "class": s.get("classification"), "wind_kt": s.get("intensity"),
                    "atlantic": str(s.get("id", "")).lower().startswith("al"), "gulf": bool(in_gulf),
                    "lat": lat, "lon": lon})
    return out


def signals(dates, store, ttf_prices):
    """Daily gas signals on the given trading dates (see the module docstring)."""
    weekly = store.dropna(subset=["storage_surprise"]).sort_values("report_date")
    left = pd.DataFrame({"date": dates.astype("datetime64[ns]")})  # merge keys must share a resolution
    weekly = weekly.assign(report_date=weekly["report_date"].astype("datetime64[ns]"))
    merged = pd.merge_asof(left, weekly[["report_date", "storage_surprise", "storage_deficit"]], left_on="date",
                           right_on="report_date", tolerance=pd.Timedelta(days=14)).set_index("date")
    prices = ttf_prices.reindex(dates.union(ttf_prices.index)).ffill(limit=3).reindex(dates)
    out = merged[["storage_surprise", "storage_deficit"]].set_axis(dates)
    out["ttf_5d"] = np.log(prices / prices.shift(5))
    return out


def context(update=True):
    """Everything the desk shows about gas besides the weather, each part independent of the others' failures."""
    out, errors = {}, []
    for name, fn in (("storage", lambda: storage(update)), ("ttf", lambda: ttf(update)),
                     ("monthly", lambda: monthly(update)), ("storms", storms)):
        try:
            out[name] = fn()
        except Exception as e:  # context: one missing source must not hide the others
            errors.append(f"{name}: {e}")
    return out, errors


def main():
    ctx, errors = context()
    if "storage" in ctx:
        s = ctx["storage"].dropna(subset=["change"]).iloc[-1]
        print(f"Storage, week to {s.name.date()} (reported {s['report_date'].date()}): {s['level']:,.0f} Bcf, "
              f"change {s['change']:+,.0f} vs 5-year average {s['avg_change_5y']:+,.0f}; level "
              f"{-s['storage_deficit']:+.1f}% vs 5-year average")
    if "ttf" in ctx and len(ctx["ttf"]):
        t = ctx["ttf"]
        print(f"TTF {t.index[-1].date()}: {t.iloc[-1]:.2f} EUR/MWh, 5 days {100 * (t.iloc[-1] / t.iloc[-6] - 1):+.1f}%")
    for name, m in ctx.get("monthly", {}).items():
        print(f"{name}, {m['month']}: {m['bcf_per_day']:.1f} Bcf/d ({m['change_vs_year_ago_pct']:+.1f}% on a year)")
    for s in ctx.get("storms", []):
        print(f"Storm {s['name']} ({s['class']}, {s['wind_kt']} kt){' in the Gulf' if s['gulf'] else ''}")
    for e in errors:
        print("Could not fetch", e)


if __name__ == "__main__":
    main()
