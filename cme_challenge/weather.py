"""US heating demand outlook for natural gas: population-weighted heating degree days from three weather models.

Natural gas reacts to changes in the 1-2 week temperature outlook. This fetches temperatures for large US
cities from ECMWF's AI model (AIFS), ECMWF's physics model (IFS) and NOAA's GFS through Open-Meteo (free for
non-commercial use, no key), converts them to heating degree days (65 F minus the daily mean temperature, when
positive), weights cities by population and compares them with the 2016-2025 normal and with the previous
fetch. Colder than normal, or colder than the previous run, means more heating demand: supportive for natural
gas and heating oil. It is context for the trader, not a backtested signal.

Usage (from the repository root):
    python cme_challenge/weather.py
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

WEATHER = DATA / "futures" / "weather"
CITIES = {  # latitude, longitude, metro population in millions (the weight)
    "New York": (40.71, -74.01, 19.5), "Chicago": (41.88, -87.63, 9.4), "Philadelphia": (39.95, -75.17, 6.2),
    "Washington": (38.90, -77.04, 6.3), "Boston": (42.36, -71.06, 4.9), "Detroit": (42.33, -83.05, 4.3),
    "Minneapolis": (44.98, -93.27, 3.7), "Pittsburgh": (40.44, -80.00, 2.4), "Cleveland": (41.50, -81.69, 2.1),
    "Atlanta": (33.75, -84.39, 6.3), "Dallas": (32.78, -96.80, 8.1), "Houston": (29.76, -95.37, 7.5),
    "Denver": (39.74, -104.99, 3.0), "Seattle": (47.61, -122.33, 4.0), "Los Angeles": (34.05, -118.24, 12.8),
}
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


def weighted(frame):
    """Population-weighted average across the city columns present (cities missing a value are left out)."""
    w = pd.Series({c: CITIES[c][2] for c in frame.columns})
    return frame.mul(w).sum(axis=1, min_count=1) / frame.notna().mul(w).sum(axis=1)


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


def normals():
    """Normal heating degree days by city and day of year, from the Open-Meteo archive (cached once)."""
    path = WEATHER / "normals.parquet"
    table = pd.read_parquet(path) if path.exists() else pd.DataFrame(index=range(1, 367))
    for city, (lat, lon, _) in CITIES.items():
        if city in table.columns:
            continue
        if len(table.columns):
            time.sleep(15)  # ten years of archive weigh like hundreds of calls in Open-Meteo's per-minute limit
        try:
            data = get_json("https://archive-api.open-meteo.com/v1/archive", {
                "latitude": lat, "longitude": lon, "start_date": f"{NORMAL_YEARS[0]}-01-01",
                "end_date": f"{NORMAL_YEARS[1]}-12-31", "daily": "temperature_2m_mean",
                "temperature_unit": "fahrenheit", "timezone": "auto"})
        except urllib.error.URLError as e:  # includes HTTP 429: keep what we have, finish on a later run
            print(f"Weather normals: stopped at {city} ({e}); the next refresh continues", file=sys.stderr)
            break
        t = pd.Series(data["daily"]["temperature_2m_mean"], index=pd.to_datetime(data["daily"]["time"]), dtype=float)
        by_doy = hdd(t).groupby(t.index.dayofyear).mean().reindex(range(1, 367))
        wrap = pd.concat([by_doy.iloc[-7:], by_doy, by_doy.iloc[:7]])  # smooth +-7 days across the year end
        table[city] = wrap.rolling(15, center=True, min_periods=5).mean().iloc[7:-7].to_numpy()
        WEATHER.mkdir(parents=True, exist_ok=True)
        table.to_parquet(path)
    return table


def outlook(forecasts, normal, previous=None):
    """Summary per model: weighted HDD for days 1-7 and 8-15, vs normal and vs the previous fetch."""
    rows = []
    for model, temps in forecasts.items():
        daily = weighted(hdd(temps)).dropna()
        with_normal = [c for c in temps.columns if c in normal.columns]  # compare like with like
        if with_normal:
            cmp = weighted(hdd(temps[with_normal])).reindex(daily.index)
            norm = weighted(normal.loc[daily.index.dayofyear, with_normal].set_axis(daily.index))
        row = {"model": model, "first_day": daily.index.min().date() if len(daily) else None,
               "cities_with_normal": len(with_normal)}
        for label, part, days in (("days_1_7", daily.iloc[:7], 7), ("days_8_15", daily.iloc[7:15], 8)):
            complete = len(part) == days  # a shorter forecast would look warmer
            row[f"hdd_{label}"] = part.sum() if complete else np.nan
            row[f"vs_normal_{label}"] = (cmp.loc[part.index].sum() - norm.loc[part.index].sum()
                                         if with_normal and complete else np.nan)
        if previous is not None:
            prev = previous[previous["model"] == model].set_index("date")["hdd"]
            common = daily.index.intersection(pd.to_datetime(prev.index))
            row["change_vs_previous"] = (daily.loc[common] - prev.loc[common].to_numpy()).sum() if len(common) else np.nan
            row["previous_fetch"] = previous["fetched_at"].iloc[0] if len(common) else None
        rows.append(row)
    return pd.DataFrame(rows)


def refresh():
    """Fetch every model, save the daily history, and return the outlook table (raises if nothing worked)."""
    WEATHER.mkdir(parents=True, exist_ok=True)
    normal = normals()
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
    now = datetime.now().isoformat(timespec="minutes")
    previous = None
    if not history.empty:  # last fetch at least 5 hours old, so a new model run had time to arrive
        old = history[pd.to_datetime(history["fetched_at"]) <= pd.Timestamp(now) - pd.Timedelta(hours=5)]
        if not old.empty:
            previous = old[old["fetched_at"] == old["fetched_at"].max()]
    table = outlook(forecasts, normal, previous).assign(fetched_at=now)
    rows = [pd.DataFrame({"fetched_at": now, "model": m, "date": weighted(hdd(t)).dropna().index,
                          "hdd": weighted(hdd(t)).dropna().to_numpy()}) for m, t in forecasts.items()]
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
    print("Population-weighted US heating degree days (positive vs_normal / change = colder = more gas demand)\n")
    print(table.round(1).to_string(index=False))
    for e in errors:
        print("Could not fetch", e)


if __name__ == "__main__":
    main()
