"""Benchmark natural-gas weather strategies on archived forecasts (what the models said at the time).

Data: Open-Meteo's Previous Runs API keeps, for each day, what a model forecast 1 to 7 days earlier. Archives
start around mid-2021 for NOAA GFS, March 2024 for ECMWF IFS and February 2025 for ECMWF's AI model (AIFS),
so only GFS spans several winters. Temperatures for the 26 US cities of weather.py become gas-weighted degree
days, heating plus cooling (TDD, as in weather.py), and are cached under <data>/futures/weather/previous_runs/.

Signals, from forecasts issued at day t for days t+1..t+7:
  revision  the 7-day degree-day forecast against the one issued the day before, on the same days (more heating
            or cooling demand = long natural gas): what weather traders react to
  anomaly   the 7-day forecast against the 2016-2025 normal (more demand than normal = long)

Timing: a day's position uses only forecasts issued up to the previous day and is taken at that day's close
("next day"), so no forecast published after the close is used. "same day" uses that day's forecasts at that
day's close, which includes runs published after the 2:30 pm ET settlement (optimistic). "reaction" is the
correlation between a revision and the same day's price move, which happens before you can trade it.

Returns: the roll-correct natural gas and heating oil series (cme_challenge/continuous.py, Databento), daily,
before costs, by season: heating (November to March), cooling (June to September), shoulder (April, May, October).

Ceiling (--ceiling): the same trade with the weather that actually happened (Open-Meteo archive, 2010 on)
instead of a forecast, as perfect foresight, with the direction wrong 5% of the time, and with noise of the
size GFS forecasts really had (measured on the archive above). This is hindsight: it shows how much knowing
next week's weather is worth at most, not what a forecast everyone else also has can earn.

Usage (from the repository root):
    python cme_challenge/weather_strategy.py              # download (first run about 15 min) and benchmark
    python cme_challenge/weather_strategy.py --no-fetch   # benchmark the cached forecasts only
    python cme_challenge/weather_strategy.py --ceiling    # hindsight ceiling since 2010 (first run about 15 min)
"""
import argparse
import sys
import time
import urllib.error
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

import weather

CACHE = DATA / "futures" / "weather" / "previous_runs"
RESEARCH = DATA / "futures" / "research"
SERIES = DATA / "futures" / "continuous"
MODELS = {"NOAA GFS": ("gfs_seamless", "2021-04-01"), "ECMWF IFS": ("ecmwf_ifs025", "2024-02-01"),
          "ECMWF AIFS (AI)": ("ecmwf_aifs025_single", "2025-02-01")}
LEADS = range(1, 8)
SEASONS = {"heating": (11, 12, 1, 2, 3), "cooling": (6, 7, 8, 9), "shoulder": (4, 5, 10)}


def by_season(index):
    """(season, mask) pairs for a DatetimeIndex."""
    return [(name, index.month.isin(months)) for name, months in SEASONS.items()]


def fetch_city(model, city, start, end):
    """Daily mean temperature by target day (rows) and lead in days (columns 1..7)."""
    lat, lon, *_ = weather.CITIES[city]
    data = weather.get_json("https://previous-runs-api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "models": model, "start_date": str(start), "end_date": str(end),
        "hourly": ",".join(f"temperature_2m_previous_day{k}" for k in LEADS), "temperature_unit": "fahrenheit",
        "timezone": "auto"})
    hourly = pd.DataFrame(data["hourly"]).set_index("time")
    hourly.index = pd.to_datetime(hourly.index)
    out = {}
    for k in LEADS:
        h = pd.to_numeric(hourly[f"temperature_2m_previous_day{k}"], errors="coerce")
        by_day = h.groupby(h.index.normalize())
        out[k] = by_day.mean().where(by_day.count() >= 18)
    return pd.DataFrame(out).dropna(how="all")


def load_model(label, fetch=True):
    model, first = MODELS[label]
    CACHE.mkdir(parents=True, exist_ok=True)
    end = date.today() + pd.Timedelta(days=8)  # targets up to a week ahead carry the forecasts issued today
    temps = {}
    for city in weather.CITIES:
        path = CACHE / f"{model}_{city.replace(' ', '_')}.parquet"
        cached = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        cached.columns = [int(c) for c in cached.columns]
        start = (cached.index.max() - pd.Timedelta(days=10)).date() if len(cached) else date.fromisoformat(first)
        if fetch and start < end:
            try:
                new = fetch_city(model, city, start, end)
                cached = pd.concat([cached, new])
                cached = cached[~cached.index.duplicated(keep="last")].sort_index()
                cached.set_axis([str(c) for c in cached.columns], axis=1).to_parquet(path)
            except (urllib.error.URLError, KeyError, ValueError) as e:
                print(f"  {label} {city}: fetch failed ({e}); using what is cached", file=sys.stderr)
            # long ranges weigh like many calls in Open-Meteo's per-minute limit; a daily update is light
            time.sleep(12 if (end - start).days > 60 else 1)
        if len(cached):
            temps[city] = cached
    return temps


def degree_days_by_lead(temps, kind="tdd"):
    """Gas-weighted degree days (weather.degree_days, column `kind`): target day x lead."""
    out = {}
    for k in LEADS:
        frame = pd.DataFrame({city: t[k] for city, t in temps.items() if k in t})
        out[k] = weather.degree_days(frame)[kind]
    return pd.DataFrame(out).sort_index()


def signals(dd, normal_by_day):
    """Per issue day t: 7-day forecast, its revision since t-1 (same target days) and its anomaly."""
    days = pd.date_range(dd.index.min(), dd.index.max())
    dd = dd.reindex(days)
    made = {k: dd[k].shift(-k) for k in LEADS}                        # forecast for t+k issued at t
    forecast = pd.concat(made.values(), axis=1).sum(axis=1, min_count=len(LEADS))
    revision = pd.concat([made[k] - dd[k + 1].shift(-k) for k in LEADS if k + 1 in LEADS],  # vs issued at t-1
                         axis=1).sum(axis=1, min_count=len(LEADS) - 1)
    normal = sum(normal_by_day.reindex(days).shift(-k) for k in LEADS)
    return pd.DataFrame({"forecast": forecast, "revision": revision, "anomaly": forecast - normal})


def forecast_signals(label="NOAA GFS", fetch=True, normal=None):
    """Revision and anomaly of one model's 7-day TDD forecast per issue day, over the cities with a normal."""
    normal = weather.normals(fetch=False) if normal is None else normal
    temps = {c: t for c, t in load_model(label, fetch).items() if c in normal["hdd"].columns}
    if not temps:
        return pd.DataFrame(columns=["forecast", "revision", "anomaly"])
    dd = degree_days_by_lead(temps)
    days = pd.date_range(dd.index.min(), dd.index.max() + pd.Timedelta(days=8))
    normal_day = weather.normal_degree_days(normal, days, list(temps))["tdd"]
    return signals(dd, normal_day).dropna(how="all")


def daily_pnl(signal, ret, hold, lag):
    """Position = average sign of the last `hold` daily signals known `lag` days before the close."""
    sig = np.sign(signal).reindex(pd.date_range(signal.index.min(), ret.index.max())).ffill(limit=3)
    known = sig.shift(lag).reindex(ret.index)                        # issued by the end of the previous day
    position = known.rolling(hold, min_periods=1).mean()
    return (position.shift(1) * ret).dropna(), position              # held from yesterday's close


def stats(pnl):
    pnl = pnl[pnl != 0].dropna()
    if len(pnl) < 40 or pnl.std() == 0:
        return {"days": len(pnl)}
    return {"days": len(pnl), "sharpe": pnl.mean() / pnl.std() * np.sqrt(252),
            "t": pnl.mean() / pnl.std() * np.sqrt(len(pnl)), "hit_rate": (pnl > 0).mean(),
            "pnl_pct_yr": 100 * pnl.mean() * 252}


def benchmark(fetch):
    normal = weather.normals(fetch=fetch, max_new=None)
    rows, reactions = [], []
    for label in MODELS:
        print(f"{label}: {'updating' if fetch else 'loading'} archived forecasts...")
        sig = forecast_signals(label, fetch, normal)
        if sig.empty:
            continue
        for root, market in (("NG", "natural gas"), ("HO", "heating oil")):
            path = SERIES / f"{root}.parquet"
            if not path.exists():
                continue
            ret = pd.read_parquet(path)["ret"].loc[sig.index.min():]
            same_day = sig["revision"].reindex(ret.index)
            reactions.append({"model": label, "market": market,
                              **{f"reaction_corr_{season}": ret[mask].corr(same_day[mask])
                                 for season, mask in by_season(ret.index)}})
            for name in ("revision", "anomaly"):
                for hold in (1, 5):
                    for timing, lag in (("next day", 1), ("same day (optimistic)", 0)):
                        pnl, _ = daily_pnl(sig[name], ret, hold, lag)
                        for season, mask in by_season(pnl.index):
                            rows.append({"model": label, "market": market, "signal": name, "hold_days": hold,
                                         "timing": timing, "season": season, "from": pnl.index.min().date(),
                                         **stats(pnl[mask])})
            for season, mask in by_season(ret.index):
                rows.append({"model": label, "market": market, "signal": "buy and hold", "hold_days": None,
                             "timing": "", "season": season, "from": ret.index.min().date(), **stats(ret[mask])})
    table, reaction = pd.DataFrame(rows), pd.DataFrame(reactions)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "weather_strategy.csv", index=False)
    pd.set_option("display.width", 220)
    print("\nSame-day reaction: correlation of the forecast revision with that day's return "
          "(the move you cannot catch)\n")
    print(reaction.round(3).to_string(index=False))
    print("\nStrategies, daily, before costs (Sharpe annualised; t > 2 is the usual bar)\n")
    cols = ["model", "market", "signal", "hold_days", "timing", "season", "from", "days", "sharpe", "t", "hit_rate",
            "pnl_pct_yr"]
    print(table[cols].round(2).to_string(index=False))
    print(f"\nSaved {RESEARCH / 'weather_strategy.csv'}")


# --------------------------------------------------------------------------- hindsight ceiling

def demand_anomaly(temps):
    """Next-7-day population-weighted degree days against the same calendar days' average, per day t."""
    dd = weather.degree_days(temps)["tdd"].dropna()
    by_doy = dd.groupby(np.minimum(dd.index.dayofyear, 365)).mean()
    smooth = pd.concat([by_doy.iloc[-7:], by_doy, by_doy.iloc[:7]]).rolling(15, center=True).mean().iloc[7:-7]
    anomaly_day = dd - smooth.reindex(np.minimum(dd.index.dayofyear, 365)).to_numpy()
    return sum(anomaly_day.shift(-k) for k in LEADS)  # days t+1..t+7, known only in hindsight


def gfs_error_ratio():
    """Std of GFS's 7-day degree-day forecast error relative to the std of the actual 7-day anomaly."""
    temps = {c: t for c, t in load_model("NOAA GFS", fetch=False).items()}
    actual = weather.actual_temps()
    if not temps or actual.empty:
        return np.nan
    cities = [c for c in temps if c in actual]
    forecast = {}
    for k in LEADS:
        frame = pd.DataFrame({c: temps[c][k] for c in cities})
        forecast[k] = weather.degree_days(frame)["tdd"].shift(-k)  # issued at t for t+k
    fsum = pd.concat(forecast.values(), axis=1).sum(axis=1, min_count=len(LEADS))
    asum = sum(weather.degree_days(actual[cities])["tdd"].shift(-k) for k in LEADS)
    both = pd.concat([fsum, asum, demand_anomaly(actual[cities])], axis=1, keys=["f", "a", "anom"]).dropna()
    return (both["f"] - both["a"]).std() / both["anom"].std()


def ceiling(fetch=True):
    actual = weather.actual_temps() if fetch else (pd.read_parquet(weather.ACTUALS) if weather.ACTUALS.exists()
                                                   else pd.DataFrame())
    if actual.empty:
        raise SystemExit("No actual temperatures yet; run --ceiling without --no-fetch")
    anomaly = demand_anomaly(actual).dropna()
    ratio = gfs_error_ratio()
    rng = np.random.default_rng(0)
    versions = {
        "perfect foresight": anomaly,
        "direction wrong 5% of days": anomaly.where(rng.random(len(anomaly)) >= 0.05, -anomaly),
    }
    if np.isfinite(ratio):
        noise = pd.Series(rng.normal(0, ratio * anomaly.std(), len(anomaly)), index=anomaly.index)
        versions[f"GFS-sized error ({ratio:.2f} x anomaly std)"] = anomaly + noise
    rows = []
    for root, market in (("NG", "natural gas"), ("HO", "heating oil")):
        path = SERIES / f"{root}.parquet"
        if not path.exists():
            continue
        ret = pd.read_parquet(path)["ret"].loc[anomaly.index.min():anomaly.index.max()]
        for name, signal in versions.items():
            for hold in (1, 5):
                pnl, _ = daily_pnl(signal, ret, hold, lag=0)  # at the close of day t, about days t+1..t+7
                for season, mask in by_season(pnl.index):
                    rows.append({"market": market, "weather known": name, "hold_days": hold, "season": season,
                                 "from": pnl.index.min().date(), **stats(pnl[mask])})
    table = pd.DataFrame(rows)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "weather_ceiling.csv", index=False)
    pd.set_option("display.width", 220)
    print("Hindsight ceiling: trading on next week's ACTUAL weather (long when more gas-weighted degree days than "
          "normal).\nNot tradable: it shows the most any forecast could add.\n")
    print(table.round(2).to_string(index=False))
    print(f"\nSaved {RESEARCH / 'weather_ceiling.csv'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fetch", action="store_true", help="use cached data only")
    ap.add_argument("--ceiling", action="store_true", help="hindsight ceiling with actual weather since 2010")
    args = ap.parse_args()
    if args.ceiling:
        ceiling(fetch=not args.no_fetch)
    else:
        benchmark(fetch=not args.no_fetch)


if __name__ == "__main__":
    main()
