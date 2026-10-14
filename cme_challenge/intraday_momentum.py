"""Intraday momentum on CME commodities: does the day's move so far predict the last hour into settlement?

Published evidence: the first half-hour of the S&P 500 predicts its last half-hour (Gao, Han, Li and Zhou, JFE
2018), and across 60+ futures the rest of the day predicts the last half-hour (Baltussen, Da, Lammers and Martens,
JFE 2021), driven by option dealers' hedging and late rebalancing. This tests it on our 18 commodities with hourly
bars of the front contract (Databento continuous, volume roll; one-minute bars cost 15 times more).

Per market and day, in Chicago time:
  close hour  the hourly bar holding the settlement (energy 13:00-14:00, grains 13:00-14:00, livestock 12:00-13:00,
              gold, silver and platinum 12:00-13:00, copper and palladium 11:00-12:00)
  trade       enter at the close hour's first trade, exit at its last: flat every night
  signals     rest of day: last close-hour close (yesterday) to today's close-hour open
              first hour: last close-hour close to the end of the first hour of the day session
              (energy and grains 08:00-09:00, livestock 08:00-09:00, metals 07:00-08:00)
Days where the front contract changed between yesterday and today are skipped (the roll is not a return).
Costs: one tick plus $4 a contract per round trip.

Usage (from the repository root):
    python cme_challenge/intraday_momentum.py fetch --estimate     # Databento's price, nothing fetched
    python cme_challenge/intraday_momentum.py fetch                # hourly bars since June 2010 (stops above $25)
    python cme_challenge/intraday_momentum.py                      # the benchmark
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_markets import MARKETS

import databento_contracts as dc

HOURLY = DATA / "futures" / "databento" / "hourly"
RESEARCH = DATA / "futures" / "research"
START, MAX_COST = "2010-06-06", 25.0
CT = "America/Chicago"
COST_USD = 4.0  # commission and fees per contract, round trip
# Yahoo/CME root: (first hour start, close hour start, tick size) in Chicago time
SESSIONS = {
    "CL": (8, 13, 0.01), "NG": (8, 13, 0.001), "HO": (8, 13, 0.0001), "RB": (8, 13, 0.0001),
    "ZC": (8, 13, 0.25), "ZS": (8, 13, 0.25), "ZM": (8, 13, 0.1), "ZL": (8, 13, 0.01), "ZW": (8, 13, 0.25),
    "KE": (8, 13, 0.25),
    "LE": (8, 12, 0.025), "GF": (8, 12, 0.025), "HE": (8, 12, 0.025),
    "GC": (7, 12, 0.1), "SI": (7, 12, 0.005), "PL": (7, 12, 0.1), "HG": (7, 11, 0.0005), "PA": (7, 11, 0.5),
}
SUBPERIODS = [("2010-2017", "2010", "2017"), ("2018-2026", "2018", "2026"), ("last 3 years", None, None)]


def market(root):
    return next(m for m in MARKETS.values() if m.yahoo == root)


# --------------------------------------------------------------------------- data

def retry(call, attempts=4):
    """Databento's gateway times out now and then (HTTP 504): try again after 10, 20, 40 seconds."""
    import time
    for i in range(attempts):
        try:
            return call()
        except Exception as e:
            if i == attempts - 1 or "504" not in str(e) and "timed out" not in str(e).lower():
                raise
            time.sleep(10 * 2 ** i)


def fetch(estimate, yes):
    client = dc.connect()
    end = pd.Timestamp.now(tz="UTC").normalize().date()
    todo = {r: f"{r}.v.0" for r in SESSIONS}
    have = {r: pd.read_parquet(HOURLY / f"{r}.parquet") for r in todo if (HOURLY / f"{r}.parquet").exists()}
    starts = {r: (have[r].index.max() + pd.Timedelta(hours=1)).date() if r in have else START for r in todo}
    todo = {r: s for r, s in todo.items() if str(starts[r]) < str(end)}  # markets already up to date
    if not todo:
        print("Hourly bars are up to date")
        return
    costs = {r: retry(lambda: client.metadata.get_cost(dataset=dc.DATASET, symbols=[s], stype_in="continuous",
                                                       schema="ohlcv-1h", start=str(starts[r]), end=str(end)))
             for r, s in todo.items()}
    total = sum(costs.values())
    print(f"ohlcv-1h, front contracts of {len(todo)} markets, to {end}: Databento price ${total:,.2f}")
    if estimate:
        return
    if total > MAX_COST and not yes:
        raise SystemExit(f"Above ${MAX_COST:.0f}: rerun with --yes to accept")
    HOURLY.mkdir(parents=True, exist_ok=True)

    def one(r):  # saved per market: a failure keeps what is already paid for
        bars = retry(lambda: client.timeseries.get_range(dataset=dc.DATASET, symbols=[todo[r]], stype_in="continuous",
                                                         schema="ohlcv-1h", start=str(starts[r]), end=str(end)).to_df())
        bars = bars[["instrument_id", "open", "high", "low", "close", "volume"]]
        if r in have:
            bars = pd.concat([have[r], bars])
            bars = bars[~bars.index.duplicated(keep="last")]
        bars.sort_index().to_parquet(HOURLY / f"{r}.parquet")
        return f"  {r}: {len(bars):,} hourly bars, {bars.index.min():%Y-%m-%d} to {bars.index.max():%Y-%m-%d}"

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=6) as pool:  # a 16-year market takes Databento about 10 minutes
        for done in as_completed([pool.submit(one, r) for r in todo]):
            print(done.result(), flush=True)


def daily_table(bars, first_hour, close_hour):
    """One row per trading day: the close-hour bar, the first-hour bar and yesterday's close-hour bar."""
    b = bars.copy()
    b.index = b.index.tz_convert(CT)
    b["day"] = b.index.tz_localize(None).normalize()
    b["hour"] = b.index.hour
    close = b[b["hour"] == close_hour].set_index("day")[["instrument_id", "open", "close", "volume"]]
    first = b[b["hour"] == first_hour].set_index("day")["close"].rename("first_close")
    d = close.join(first, how="left")
    d = d[d.index.dayofweek < 5]
    d["prev_close"] = d["close"].shift(1)
    d["prev_id"] = d["instrument_id"].shift(1)
    d = d[d["instrument_id"] == d["prev_id"]]  # no roll inside the window
    d["rod"] = d["open"] / d["prev_close"] - 1
    d["first"] = d["first_close"] / d["prev_close"] - 1
    d["last"] = d["close"] / d["open"] - 1
    return d.replace([np.inf, -np.inf], np.nan).dropna(subset=["rod", "last"])


# --------------------------------------------------------------------------- statistics

def stats(x):
    x = x.dropna()
    if len(x) < 50:
        return {"days": len(x)}
    yearly = x.groupby(x.index.year).mean()
    t_years = yearly.mean() / yearly.std(ddof=1) * np.sqrt(len(yearly)) if len(yearly) > 2 else np.nan
    return {"days": len(x), "mean_bp": 1e4 * x.mean(), "t": x.mean() / x.std() * np.sqrt(len(x)),
            "t_years": t_years, "hit": (x > 0).mean(), "sharpe": x.mean() / x.std() * np.sqrt(252)}


def vol_stats(x):
    """stats() for P&L in units of a normal last-hour move: the mean as a % of that move."""
    out = stats(x)
    if "mean_bp" in out:
        out["mean_pct_of_move"] = out.pop("mean_bp") / 100
    return out


def period(x, name, start, end):
    if start is None:
        return x[x.index >= x.index.max() - pd.DateOffset(years=3)]
    return x.loc[start:end]


def benchmark():
    rows, pooled = [], {}
    for root, (first_hour, close_hour, tick) in SESSIONS.items():
        path = HOURLY / f"{root}.parquet"
        if not path.exists():
            continue
        m = market(root)
        d = daily_table(pd.read_parquet(path), first_hour, close_hour)
        cost = (tick * m.point_value + COST_USD) / (d["open"] * m.point_value)  # return units, per round trip
        vol = d["last"].rolling(63, min_periods=40).std().shift(1)  # known before the trade
        for signal in ("rod", "first"):
            side = np.sign(d[signal])
            gross = side * d["last"]
            net = gross - cost
            # equal-risk version: each market sized to 1 unit of its own last-hour volatility
            pooled.setdefault((signal, "before costs"), {})[root] = (gross / vol).rename(root)
            pooled.setdefault((signal, "after costs"), {})[root] = (net / vol).rename(root)
            rows.append({"root": root, "name": m.name, "sector": m.sector, "signal": signal,
                         "cost_bp": 1e4 * cost.median(), "slope_t": slope_t(d[signal], d["last"]),
                         **{f"gross_{k}": v for k, v in stats(gross).items() if k in ("mean_bp", "t", "hit")},
                         **{f"net_{k}": v for k, v in stats(net).items()}})
    table = pd.DataFrame(rows)
    portfolios = []
    for (signal, costs), parts in pooled.items():
        p = pd.concat(parts.values(), axis=1, sort=True)
        port = p.mean(axis=1, skipna=True)[p.notna().sum(axis=1) >= 6]
        rows_for = lambda name, x: {"signal": signal, "costs": costs, "period": name, **vol_stats(x)}
        for name, start, end in [("all", "2010", "2026"), *SUBPERIODS]:
            portfolios.append(rows_for(name, period(port, name, start, end)))
        for sector in ("Energy", "Grains", "Livestock", "Metals"):
            cols = [r for r in p if market(r).sector == sector]
            portfolios.append(rows_for(f"{sector} only", p[cols].mean(axis=1, skipna=True).dropna()))
        strong = pd.concat({r: pooled_strength(r, signal) for r in parts}, axis=1, sort=True)
        portfolios.append(rows_for("days with |signal| in its top third",
                                   p.where(strong).mean(axis=1, skipna=True).dropna()))
    return table, pd.DataFrame(portfolios), pooled


def pooled_strength(root, signal, cache={}):
    """True on days the signal's size is in the top third of its own past (expanding), else False."""
    key = (root, signal)
    if key not in cache:
        first_hour, close_hour, _ = SESSIONS[root]
        d = daily_table(pd.read_parquet(HOURLY / f"{root}.parquet"), first_hour, close_hour)
        size = d[signal].abs()
        cache[key] = size > size.expanding(250).quantile(2 / 3).shift(1)
    return cache[key]


def slope_t(x, y):
    df = pd.concat([x, y], axis=1).dropna()
    if len(df) < 50:
        return np.nan
    x, y = df.iloc[:, 0], df.iloc[:, 1]
    beta = np.cov(x, y)[0, 1] / x.var()
    resid = y - beta * x
    return beta / (resid.std() / (x.std() * np.sqrt(len(x))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="benchmark", choices=["benchmark", "fetch"])
    ap.add_argument("--estimate", action="store_true", help="fetch: show Databento's price only")
    ap.add_argument("--yes", action="store_true", help=f"fetch: accept a price above ${MAX_COST:.0f}")
    args = ap.parse_args()
    if args.command == "fetch":
        return fetch(args.estimate, args.yes)
    table, portfolios, _ = benchmark()
    if table.empty:
        raise SystemExit(f"No hourly bars in {HOURLY}: run `python cme_challenge/intraday_momentum.py fetch` first")
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "intraday_momentum_markets.csv", index=False)
    portfolios.to_csv(RESEARCH / "intraday_momentum.csv", index=False)
    with pd.option_context("display.width", 220, "display.max_rows", 200, "display.float_format", "{:.2f}".format):
        print("Equal-risk portfolio of the 18 commodities, last hour into settlement, after costs "
              "(sharpe annualised, t_years = t of the yearly means):\n")
        print(portfolios.to_string(index=False))
        print("\nPer market, after costs, sorted by t:\n")
        cols = ["root", "name", "signal", "cost_bp", "gross_mean_bp", "net_mean_bp", "net_t", "net_hit",
                "net_sharpe", "slope_t", "net_days"]
        print(table.sort_values(["signal", "net_t"], ascending=[True, False])[cols].to_string(index=False))
    print(f"\nSaved {RESEARCH / 'intraday_momentum.csv'} and intraday_momentum_markets.csv")


if __name__ == "__main__":
    main()
