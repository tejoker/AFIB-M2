"""US gas demand outlook from the weather: heating and cooling degree days from three weather models.

Natural gas reacts to changes in the 1-2 week temperature outlook, in winter through heating and in summer through
air conditioning, since about 40% of US electricity comes from gas-fired plants. This fetches temperatures for 26
large US cities from ECMWF's AI model (AIFS), ECMWF's physics model (IFS) and NOAA's GFS through Open-Meteo (free
for non-commercial use, no key) and converts them to degree days (base 65 F):

  HDD  heating degree days, 65 F minus the daily mean when positive; cities weighted by population times the share
       of homes heated with gas in their census region
  CDD  cooling degree days, the daily mean minus 65 F when positive; cities weighted by population times the share
       of gas in their region's electricity generation (Florida burns far more gas per degree of heat than Seattle)
  TDD  HDD + CDD: gas demand from the weather, compared with the 2016-2025 normal and with the previous fetch

The weights are rounded approximations (EIA and Census figures, mid-2020s); they rank regions, they are not a
demand model. More degree days than normal, or than the previous run, means more gas demand: supportive for
natural gas (and, in winter, heating oil).

Usage (from the repository root):
    python cme_challenge/weather.py
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

WEATHER = DATA / "futures" / "weather"
ACTUALS = WEATHER / "actuals.parquet"
# latitude, longitude, metro population in millions, census region, gas share of the region's power generation
CITIES = {
    "New York": (40.71, -74.01, 19.5, "NE", 0.45), "Philadelphia": (39.95, -75.17, 6.2, "NE", 0.50),
    "Boston": (42.36, -71.06, 4.9, "NE", 0.55), "Pittsburgh": (40.44, -80.00, 2.4, "NE", 0.50),
    "Chicago": (41.88, -87.63, 9.4, "MW", 0.20), "Detroit": (42.33, -83.05, 4.3, "MW", 0.30),
    "Minneapolis": (44.98, -93.27, 3.7, "MW", 0.25), "Cleveland": (41.50, -81.69, 2.1, "MW", 0.45),
    "St. Louis": (38.63, -90.20, 2.8, "MW", 0.15),
    "Washington": (38.90, -77.04, 6.3, "S", 0.45), "Atlanta": (33.75, -84.39, 6.3, "S", 0.45),
    "Charlotte": (35.23, -80.84, 2.8, "S", 0.35), "Nashville": (36.16, -86.78, 2.1, "S", 0.25),
    "Miami": (25.76, -80.19, 6.2, "S", 0.75), "Tampa": (27.95, -82.46, 3.4, "S", 0.75),
    "Orlando": (28.54, -81.38, 2.8, "S", 0.75), "Dallas": (32.78, -96.80, 8.1, "S", 0.45),
    "Houston": (29.76, -95.37, 7.5, "S", 0.45), "San Antonio": (29.42, -98.49, 2.7, "S", 0.45),
    "Austin": (30.27, -97.74, 2.5, "S", 0.45),
    "Denver": (39.74, -104.99, 3.0, "W", 0.35), "Phoenix": (33.45, -112.07, 5.1, "W", 0.45),
    "Las Vegas": (36.17, -115.14, 2.4, "W", 0.70), "Los Angeles": (34.05, -118.24, 12.8, "W", 0.45),
    "San Francisco": (37.77, -122.42, 4.6, "W", 0.45), "Seattle": (47.61, -122.33, 4.0, "W", 0.10),
}
GAS_HEATING_SHARE = {"NE": 0.50, "MW": 0.64, "S": 0.33, "W": 0.52}  # homes heated with utility gas, by region
POPULATION = {c: v[2] for c, v in CITIES.items()}
HEATING_WEIGHT = {c: v[2] * GAS_HEATING_SHARE[v[3]] for c, v in CITIES.items()}
COOLING_WEIGHT = {c: v[2] * v[4] for c, v in CITIES.items()}
MODELS = {"ECMWF AIFS (AI)": "ecmwf_aifs025_single", "ECMWF IFS": "ecmwf_ifs025", "NOAA GFS": "gfs_seamless"}
NORMAL_YEARS = (2016, 2025)
BASE_F = 65.0


def get_json(url, params, retries=3):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(full, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or e.code >= 500) or attempt == retries - 1:
                raise
            time.sleep(20)  # rate limited or a passing server error


def hdd(temp_f):
    return np.maximum(BASE_F - temp_f, 0)


def cdd(temp_f):
    return np.maximum(temp_f - BASE_F, 0)


def weighted(frame, weights=None):
    """Weighted average across the city columns present (cities missing a value are left out); population
    weights by default."""
    weights = POPULATION if weights is None else weights
    w = pd.Series({c: weights[c] for c in frame.columns})
    return frame.mul(w).sum(axis=1, min_count=1) / frame.notna().mul(w).sum(axis=1)


def degree_days(temps):
    """Gas-weighted HDD and CDD per day from mean temperatures (days x cities, F), and their sum TDD."""
    h, c = weighted(hdd(temps), HEATING_WEIGHT), weighted(cdd(temps), COOLING_WEIGHT)
    return pd.DataFrame({"hdd": h, "cdd": c, "tdd": h + c})


def fetch_forecast(model):
    """Daily mean temperature (F) by city for the next 15 days from one model."""
    names = list(CITIES)
    data = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": ",".join(str(CITIES[c][0]) for c in names), "longitude": ",".join(str(CITIES[c][1]) for c in names),
        "hourly": "temperature_2m", "models": model, "temperature_unit": "fahrenheit", "forecast_days": 15,
        "timezone": "auto"})
    data = data if isinstance(data, list) else [data]
    daily = {}
    for city, loc in zip(names, data):
        h = pd.Series(loc["hourly"]["temperature_2m"], index=pd.to_datetime(loc["hourly"]["time"]), dtype=float)
        by_day = h.groupby(h.index.normalize())
        daily[city] = by_day.mean().where(by_day.count() >= 18)  # whole days only
    return pd.DataFrame(daily)


def actual_temps(start="2010-01-01", max_new=None):
    """Daily mean temperature (F) per city from the Open-Meteo archive, cached. Resumes after a rate limit;
    max_new caps how many cities without any history are downloaded in this call (each takes about a minute)."""
    table = pd.read_parquet(ACTUALS) if ACTUALS.exists() else pd.DataFrame()
    end = (date.today() - pd.Timedelta(days=6)).isoformat()  # the archive lags a few days
    new = 0
    for city, (lat, lon, *_) in CITIES.items():
        have = table[city].dropna() if city in table else pd.Series(dtype=float)
        first = (have.index.max() + pd.Timedelta(days=1)).date().isoformat() if len(have) else start
        if first >= end or (not len(have) and max_new is not None and new >= max_new):
            continue
        new += not len(have)
        try:
            data = get_json("https://archive-api.open-meteo.com/v1/archive", {
                "latitude": lat, "longitude": lon, "start_date": first, "end_date": end,
                "daily": "temperature_2m_mean", "temperature_unit": "fahrenheit", "timezone": "auto"})
        except urllib.error.URLError as e:
            print(f"  weather archive stopped at {city} ({e}); the next run continues", file=sys.stderr)
            break
        got = pd.Series(data["daily"]["temperature_2m_mean"], index=pd.to_datetime(data["daily"]["time"]),
                        dtype=float, name=city)
        table = got.to_frame() if table.empty else table.combine_first(got.to_frame())
        ACTUALS.parent.mkdir(parents=True, exist_ok=True)
        table.to_parquet(ACTUALS)
        if (pd.Timestamp(end) - pd.Timestamp(first)).days > 400:
            time.sleep(45)  # years of archive weigh like hundreds of calls in Open-Meteo's per-minute limit
    return table


def normals(fetch=True, max_new=2):
    """Normal HDD and CDD by day of year (rows 1-366) and city, 2016-2025, from the archive of actual weather."""
    actual = pd.read_parquet(ACTUALS) if ACTUALS.exists() else pd.DataFrame()
    if fetch and any(c not in actual or actual[c].count() < 3000 for c in CITIES):  # only cities still missing
        actual = actual_temps(max_new=max_new)
    years = actual.loc[f"{NORMAL_YEARS[0]}-01-01":f"{NORMAL_YEARS[1]}-12-31"]
    cities = [c for c in years.columns if c in CITIES and years[c].count() >= 3000]
    out = {}
    for name, fn in (("hdd", hdd), ("cdd", cdd)):
        table = pd.DataFrame(index=range(1, 367))
        for city in cities:
            t = years[city].dropna()
            by_doy = fn(t).groupby(t.index.dayofyear).mean().reindex(range(1, 367))
            wrap = pd.concat([by_doy.iloc[-7:], by_doy, by_doy.iloc[:7]])  # smooth +-7 days across the year end
            table[city] = wrap.rolling(15, center=True, min_periods=5).mean().iloc[7:-7].to_numpy()
        out[name] = table
    return out


def normal_degree_days(normal, days, cities):
    """Normal weighted HDD, CDD and TDD on the given days, over the given cities."""
    doy = np.minimum(days.dayofyear, 366)
    h = weighted(normal["hdd"].loc[doy, cities].set_axis(days), HEATING_WEIGHT)
    c = weighted(normal["cdd"].loc[doy, cities].set_axis(days), COOLING_WEIGHT)
    return pd.DataFrame({"hdd": h, "cdd": c, "tdd": h + c})


def outlook(forecasts, normal, previous=None):
    """Summary per model: gas-weighted HDD, CDD and their total for days 1-7 and 8-15, the total against normal
    and against the previous fetch."""
    rows = []
    for model, temps in forecasts.items():
        daily = degree_days(temps).dropna()
        with_normal = [c for c in temps.columns if c in normal["hdd"].columns]  # compare like with like
        if with_normal:
            cmp = degree_days(temps[with_normal]).reindex(daily.index)
            norm = normal_degree_days(normal, daily.index, with_normal)
        row = {"model": model, "first_day": daily.index.min().date() if len(daily) else None,
               "cities": temps.notna().any().sum(), "cities_with_normal": len(with_normal)}
        for label, part, days in (("days_1_7", daily.iloc[:7], 7), ("days_8_15", daily.iloc[7:15], 8)):
            complete = len(part) == days  # a shorter forecast would look mild
            for kind in ("hdd", "cdd", "tdd"):
                row[f"{kind}_{label}"] = part[kind].sum() if complete else np.nan
            row[f"vs_normal_{label}"] = (cmp.loc[part.index, "tdd"].sum() - norm.loc[part.index, "tdd"].sum()
                                         if with_normal and complete else np.nan)
        if previous is not None:
            prev = previous[previous["model"] == model].set_index("date")["tdd"]
            common = daily.index.intersection(pd.to_datetime(prev.index))
            row["change_vs_previous"] = ((daily.loc[common, "tdd"] - prev.loc[common].to_numpy()).sum()
                                         if len(common) else np.nan)
            row["previous_fetch"] = previous["fetched_at"].iloc[0] if len(common) else None
        rows.append(row)
    return pd.DataFrame(rows)


def refresh():
    """Fetch every model, save the daily history, and return the outlook table (raises if nothing worked)."""
    WEATHER.mkdir(parents=True, exist_ok=True)
    try:
        normal = normals()
    except (urllib.error.URLError, OSError, ValueError):  # no archive reachable: compare with what is cached
        normal = normals(fetch=False)
    forecasts, errors = {}, []
    for label, model in MODELS.items():
        try:
            forecasts[label] = fetch_forecast(model)
        except (urllib.error.URLError, KeyError, ValueError) as e:
            errors.append(f"{label}: {e}")
    if not forecasts:
        raise RuntimeError("no weather model could be fetched: " + "; ".join(errors))
    history_path = WEATHER / "history.parquet"
    history = pd.read_parquet(history_path) if history_path.exists() else pd.DataFrame()
    if "tdd" not in history:  # saved before cooling demand was added: not comparable
        history = pd.DataFrame()
    now = datetime.now().isoformat(timespec="minutes")
    previous = None
    if not history.empty:  # last fetch at least 5 hours old, so a new model run had time to arrive
        old = history[pd.to_datetime(history["fetched_at"]) <= pd.Timestamp(now) - pd.Timedelta(hours=5)]
        if not old.empty:
            previous = old[old["fetched_at"] == old["fetched_at"].max()]
    table = outlook(forecasts, normal, previous).assign(fetched_at=now)
    rows = []
    for m, t in forecasts.items():
        dd = degree_days(t).dropna()
        rows.append(dd.rename_axis("date").reset_index().assign(fetched_at=now, model=m))
    history = pd.concat([history, *rows], ignore_index=True)
    history = history[pd.to_datetime(history["fetched_at"]) >= pd.Timestamp(now) - pd.Timedelta(days=14)]
    history.to_parquet(history_path, index=False)
    table.to_csv(WEATHER / "outlook.csv", index=False)
    return table, errors


def last_outlook():
    """The last saved outlook, shown when a fetch fails (its fetched_at column says how old it is)."""
    path = WEATHER / "outlook.csv"
    return pd.read_csv(path) if path.exists() else None


def main():
    table, errors = refresh()
    print("Gas-weighted US degree days (positive vs_normal / change = more heating or cooling = more gas demand)\n")
    print(table.round(1).to_string(index=False))
    for e in errors:
        print("Could not fetch", e)


if __name__ == "__main__":
    main()
