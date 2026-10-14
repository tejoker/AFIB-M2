"""The desk's signal database: every strategy's view on every commodity over 1, 2, 5 and 15 trading days, with
its track record measured as an information coefficient (IC).

For each of the 18 commodities (Databento contract history, roll-correct) and each horizon h, every strategy
gives today's signal and how well that signal ranked the following h-day returns in the past.

Strategies (signals known at each close; positive = the strategy's bullish reading):
  mom_1d ... mom_252d  return over the last 1, 5, 21, 63 and 252 trading days in units of normal volatility.
                       A positive IC means moves tend to continue (momentum), a negative one that they reverse.
  carry                annualised gap to the next contract, positive in backwardation
  season               average move over the next h days at this calendar date, earlier years only
  cot_z, cot_change    hedge fund positioning against its last 3 years, and its 4-week change (cot.py)
  weather_revision     natural gas and heating oil: change in NOAA GFS's 7-day gas-weighted degree days (heating
  weather_anomaly      plus cooling) since the previous day's forecast, and against normal (weather_strategy.py);
                       the forecast issued the day before, so model runs published after the close are not used
  storage_surprise,    natural gas: weekly storage change against its 5-year average, storage level against its
  storage_deficit,     5-year average, Europe's TTF over 5 days (gas.py)
  ttf_5d
  ridge                the common signals (momentum, carry, season, positioning) combined by a ridge regression
                       per horizon, pooled across the 18 markets, each year predicted by a fit on earlier years only

Columns of the database (<data>/futures/cme/signals.csv and the page cme.html):
  ic          average of z x r over the whole history: z is the signal standardised with its own past only, r the
              next h-day return in units of a normal move (63-day volatility, capped at 4). It reads like a
              correlation and is the average gain of holding z units of risk. A sample correlation would subtract
              each sample's mean return, which biases slow signals such as 12-month momentum towards reversal
              (Stambaugh); this measure has no such bias, as the tests check on random walks.
  ic_3y       the same over the last 3 years
  t           mean of the yearly averages over their standard error (years are close to independent, so
              overlapping h-day returns do not inflate it)
  q           Benjamini-Hochberg q-value: the share of false discoveries expected at that cut-off, among the
              market rows, and separately among the pooled rows
  hit         share of days where the signal, read in the direction of its IC, called the move's direction
  expected_pct  IC x today's z x a normal h-day move: the move its record implies (ridge: its own forecast)
  verdict     reliable (q < 0.10 and the last 3 years have the same sign), suggestive (|t| >= 2), else noise

Pooled rows (market ALL) test each strategy on the 18 commodities together: far more data than one market, so
they can find an edge too small to prove market by market. Each market row repeats its strategy's pooled record
(pooled_ic, pooled_verdict) and the move it implies there today (expected_pooled_pct = pooled IC x the market's
z x its normal move): the more robust reading when the market's own record is short.

About 750 market rows are tested: dozens pass |t| >= 2 by chance alone, hence q. A reliable IC of 0.03 is useful
over many trades and markets; it is not certainty on any one. Returns are before commissions.

Usage (from the repository root, or `cme` / `cme.bat` there):
    python cme_challenge/signal_db.py                  # update every source, compute, open the page
    python cme_challenge/signal_db.py --offline        # use what is saved, download nothing
    python cme_challenge/signal_db.py --root NG        # also print every row for natural gas
"""
import argparse
import html
import json
import math
import os
import sys
import time
import traceback
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_markets import MARKETS

import cot
import gas
import model
import weather
import weather_strategy

FUT = DATA / "futures"
SERIES = FUT / "continuous"
OUT = FUT / "cme"
HORIZONS = (1, 2, 5, 15)
MOMENTUM = (1, 5, 21, 63, 252)
COMMON = [f"mom_{lb}d" for lb in MOMENTUM] + ["carry", "season", "cot_z", "cot_change"]
SECTORS = ("Energy", "Metals", "Grains", "Livestock")
VOL_DAYS, MIN_OBS, RECENT_YEARS, FIRST_RIDGE_YEAR = 63, 250, 3, 2014
Z_CAP, RETURN_CAP = 3.0, 4.0
WEATHER_ROOTS, GAS_ROOT = ("NG", "HO"), "NG"
CT = "America/Chicago"
SETTLED_CT = 19  # Databento's daily bars end at midnight UTC: 19:00 CT in summer, 18:00 in winter
PROVISIONAL_TOLERANCE = 0.03  # Yahoo and the series may differ a little on the close, not by a unit change
DESCRIPTION = {
    "mom_1d": "yesterday's move", "mom_5d": "last week's move", "mom_21d": "last month's move",
    "mom_63d": "last 3 months' move", "mom_252d": "last 12 months' move", "carry": "backwardation",
    "season": "same dates in past years", "cot_z": "funds more long than usual", "cot_change": "funds buying",
    "weather_revision": "forecast turned more gas-hungry", "weather_anomaly": "forecast more gas-hungry than normal",
    "storage_surprise": "storage build below normal", "storage_deficit": "storage below 5-year average",
    "ttf_5d": "European gas rising", "ridge": "all common signals combined",
}


# --------------------------------------------------------------------------- inputs

def commodity_roots():
    return [r.strip() for r, m in MARKETS.items()
            if m.sector in SECTORS and (SERIES / f"{r.strip()}.parquet").exists()]


def market_info(root):
    m = MARKETS.get(root) or MARKETS.get(f"{root} ")
    return {"name": m.name, "sector": m.sector, "symbol": m.yahoo}


def volatility(index):
    return index.pct_change().rolling(VOL_DAYS, min_periods=40).std()


def signal_frame(s, root, cot_table, extra):
    """Every signal for one market on its own trading dates, plus volatility and forward returns."""
    index = s["index"]
    vol = volatility(index)
    f = pd.DataFrame(index=s.index)
    for lb in MOMENTUM:
        f[f"mom_{lb}d"] = ((index / index.shift(lb) - 1) / (vol * np.sqrt(lb))).clip(-5, 5)
    f["carry"] = s["carry"].clip(-1, 1)
    positioning = cot.signals(cot_table, root, s.index) if len(cot_table) else pd.DataFrame(index=s.index)
    for name in ("cot_z", "cot_change"):
        f[name] = positioning.get(name, np.nan)
    for name, series in extra.get(root, {}).items():
        f[name] = series.reindex(s.index)
    f["vol"] = vol
    for h in HORIZONS:
        f[f"fwd_{h}"] = index.shift(-h) / index - 1
        target = f[f"fwd_{h}"] / (vol * np.sqrt(h))
        f[f"target_{h}"] = target
        f[f"season_{h}"] = model.seasonal_feature(target)
    return f


def weather_signals(update, notes):
    """GFS revision and anomaly as known at each close (issued the day before), by calendar day."""
    yesterday = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    try:
        normal = weather.normals(False)
        sig = weather_strategy.forecast_signals("NOAA GFS", fetch=False, normal=normal)
        last = sig["revision"].last_valid_index() if len(sig) else None
        if update and (last is None or last < yesterday):  # about a minute: 26 cities, recent days only
            sig = weather_strategy.forecast_signals("NOAA GFS", fetch=True, normal=normal)
    except Exception as e:  # weather is one signal among many
        notes.append(f"Weather signals unavailable: {e}")
        return pd.DataFrame()
    if sig.empty:
        notes.append("Weather signals: no GFS archive yet (python cme_challenge/weather_strategy.py)")
        return sig
    days = pd.date_range(sig.index.min(), pd.Timestamp.today().normalize())
    return sig.reindex(days).shift(1)  # forecast issued yesterday, known at today's close


def market_specific(series, update, notes, gas_ctx):
    extra = {}
    wx = weather_signals(update, notes)
    if not wx.empty:
        for root in WEATHER_ROOTS:
            if root in series:
                extra.setdefault(root, {}).update({"weather_revision": wx["revision"], "weather_anomaly": wx["anomaly"]})
    if GAS_ROOT in series and "storage" in gas_ctx:
        sig = gas.signals(series[GAS_ROOT].index, gas_ctx["storage"], gas_ctx.get("ttf", pd.Series(dtype=float)))
        extra.setdefault(GAS_ROOT, {}).update({c: sig[c] for c in sig.columns})
    return extra


# --------------------------------------------------------------------------- statistics

def standardise(x, min_obs=MIN_OBS):
    """The signal against its own past only (expanding mean and standard deviation), capped at +-3."""
    x = x.astype(float)
    past = x.dropna()
    mean = past.expanding(min_obs).mean().reindex(x.index).ffill()
    sd = past.expanding(min_obs).std().reindex(x.index).ffill()
    return ((x - mean) / sd.where(sd > 0)).clip(-Z_CAP, Z_CAP)


def t_to_p(t, df):
    """Two-sided p-value of a Student t, through a normal approximation good enough for a few degrees of freedom."""
    if not np.isfinite(t) or df < 1:
        return np.nan
    z = abs(t) * (1 - 1 / (4 * df)) / math.sqrt(1 + t * t / (2 * df))
    return math.erfc(z / math.sqrt(2))


def bh_q(p):
    p = pd.Series(p, dtype=float)
    valid = p.dropna().sort_values()
    if valid.empty:
        return p
    n = len(valid)
    q = (valid * n / np.arange(1, n + 1)).iloc[::-1].cummin().iloc[::-1].clip(upper=1)
    return q.reindex(p.index)


def pairs(z, r):
    """Standardised signal and capped forward return on the dates both exist."""
    return pd.concat([z, r.clip(-RETURN_CAP, RETURN_CAP)], axis=1, keys=["z", "r"]).dropna()


def ic_stats(z, r, recent_from):
    """Record of a standardised signal z against forward returns r in units of a normal move (aligned Series)."""
    return record(pairs(z, r), recent_from)


def record(df, recent_from):
    """IC, t and hit rate from z and r columns; the index may repeat a date (several markets pooled)."""
    if len(df) < MIN_OBS or df["z"].nunique() < 3:
        return None
    gain = df["z"] * df["r"]
    ic = gain.mean()
    recent = gain[gain.index >= recent_from]
    by_year = gain.groupby(gain.index.year)
    yearly = by_year.mean()[by_year.count() >= 40]
    t = (yearly.mean() / (yearly.std(ddof=1) / np.sqrt(len(yearly)))
         if len(yearly) >= 3 and yearly.std(ddof=1) > 0 else np.nan)
    called, moved = np.sign(df["z"]) * np.sign(ic), np.sign(df["r"])
    active = (called != 0) & (moved != 0)
    return {"ic": ic, "ic_3y": recent.mean() if len(recent) >= 120 else np.nan, "t": t,
            "years": len(yearly), "years_same_sign": (np.sign(yearly) == np.sign(ic)).mean(),
            "hit": (called[active] == moved[active]).mean(), "n": len(df), "from": df.index.min().date()}


# --------------------------------------------------------------------------- ridge

def ridge(frames, h):
    """Walk-forward predictions (out of sample) and today's prediction per market for horizon h."""
    features = [c for c in COMMON if c != "season"] + [f"season_{h}"]
    rows = []
    for root, f in frames.items():
        part = f[features + [f"target_{h}", f"fwd_{h}"]].copy()
        part[features] = part[features].fillna(0)  # no positioning or seasonal history yet = no view
        part = part[f["mom_252d"].notna()].assign(root=root)
        rows.append(part)
    data = pd.concat(rows).rename_axis("date").reset_index()
    known = data.dropna(subset=[f"target_{h}"])
    known = known.assign(**{f"target_{h}": known[f"target_{h}"].clip(-RETURN_CAP, RETURN_CAP)})  # as it is scored
    preds = []
    for year in range(FIRST_RIDGE_YEAR, data["date"].dt.year.max() + 1):
        cutoff = pd.Timestamp(year=year, month=1, day=1) - pd.offsets.BDay(h + 1)  # targets end before Jan 1
        train, test = known[known["date"] < cutoff], data[data["date"].dt.year == year]
        if len(train) < 2000 or test.empty:
            continue
        beta = model.fit(train[features].to_numpy(), train[f"target_{h}"].to_numpy())
        preds.append(test.assign(pred=test[features].to_numpy() @ beta))
    oos = pd.concat(preds) if preds else pd.DataFrame(columns=["root", "date", "pred"])
    beta = model.fit(known[features].to_numpy(), known[f"target_{h}"].to_numpy())
    latest = data.sort_values("date").groupby("root").tail(1).set_index("root")
    today = pd.Series(latest[features].to_numpy() @ beta, index=latest.index)
    return oos, today, dict(zip(features, beta))


# --------------------------------------------------------------------------- the database

def build(series, extra, cot_table):
    frames = {root: signal_frame(s, root, cot_table, extra) for root, s in series.items()}
    last_date = max(f.index[-1] for f in frames.values())
    recent_from = last_date - pd.DateOffset(years=RECENT_YEARS)
    rows, coefficients, pooled = [], {}, {}
    for h in HORIZONS:
        oos, today_pred, coefficients[h] = ridge(frames, h)
        for root, f in frames.items():
            vol_now = f["vol"].dropna().iloc[-1] if f["vol"].notna().any() else np.nan
            move = vol_now * np.sqrt(h)
            for name in [c for c in f.columns if c in DESCRIPTION] + [f"season_{h}"]:
                z = standardise(f[name])
                strategy = "season" if name.startswith("season_") else name
                df = pairs(z, f[f"target_{h}"])
                stats = record(df, recent_from)
                if stats is None:
                    continue
                pooled.setdefault((strategy, h), []).append(df)
                rows.append({"root": root, "horizon": h, "strategy": strategy, "normal_move_pct": 100 * move,
                             "signal_now": f[name].iloc[-1], "z_now": z.iloc[-1], **stats,
                             "expected_pct": 100 * stats["ic"] * z.iloc[-1] * move, "as_of": f.index[-1].date()})
            r = oos[oos["root"] == root].set_index("date")
            pred = today_pred.get(root, np.nan)
            if len(r):  # the walk-forward predictions with today's appended, standardised on their own past
                z = standardise(pd.concat([r["pred"], pd.Series([pred], index=[f.index[-1]])])
                                .groupby(level=0).last(), min_obs=120)
                df = pairs(z, r[f"target_{h}"])
                stats = record(df, recent_from)
                if stats is not None:
                    pooled.setdefault(("ridge", h), []).append(df)
                    rows.append({"root": root, "horizon": h, "strategy": "ridge", "normal_move_pct": 100 * move,
                                 "signal_now": pred,
                                 "z_now": z.iloc[-1], **stats, "expected_pct": 100 * pred * move,
                                 "as_of": f.index[-1].date()})
    for (strategy, h), dfs in pooled.items():
        if len(dfs) >= 3:  # the markets' (z, r) pairs together, years as blocks
            stats = record(pd.concat(dfs), recent_from)
            if stats is not None:
                rows.append({"root": "ALL", "horizon": h, "strategy": strategy, **stats,
                             "markets": len(dfs), "as_of": last_date.date()})
    db = pd.DataFrame(rows)
    db["p"] = [t_to_p(t, y - 1) for t, y in zip(db["t"], db["years"])]
    is_pooled = db["root"] == "ALL"
    db["q"] = pd.concat([bh_q(db.loc[is_pooled, "p"]), bh_q(db.loc[~is_pooled, "p"])])
    same_recent = np.sign(db["ic_3y"]) == np.sign(db["ic"])
    db["verdict"] = np.where((db["q"] < 0.10) & same_recent, "reliable",
                             np.where(db["t"].abs() >= 2, "suggestive", "noise"))
    record_of = db[is_pooled].set_index(["strategy", "horizon"])[["ic", "verdict"]]
    record_of.columns = ["pooled_ic", "pooled_verdict"]
    db = db.join(record_of, on=["strategy", "horizon"])
    db["expected_pooled_pct"] = db["pooled_ic"] * db["z_now"] * db["normal_move_pct"]
    db.loc[is_pooled | (db["strategy"] == "ridge"), "expected_pooled_pct"] = np.nan
    db["side"] = np.where(db["expected_pct"] > 0, "LONG", np.where(db["expected_pct"] < 0, "SHORT", ""))
    info = pd.DataFrame({r: market_info(r) for r in db["root"].unique() if r != "ALL"}).T
    db = db.join(info, on="root")
    db.loc[is_pooled, ["symbol", "name", "sector"]] = ["ALL", "All 18 commodities", "Pooled"]
    db["description"] = db["strategy"].map(DESCRIPTION)
    order = ["root", "symbol", "name", "sector", "horizon", "strategy", "description", "verdict", "side",
             "expected_pct", "pooled_verdict", "pooled_ic", "expected_pooled_pct", "signal_now", "z_now", "ic",
             "ic_3y", "t", "q", "hit", "years", "years_same_sign", "n", "markets", "normal_move_pct", "from",
             "as_of"]
    return db[order].sort_values(["sector", "root", "horizon", "strategy"]).reset_index(drop=True), coefficients


def summary(db):
    """Per market and horizon: the ridge view, the strategies proven across markets (pooled reliable) and how
    many of the market's own reliable or suggestive signals point each way."""
    rows = []
    for (root, h), g in db[db["root"] != "ALL"].groupby(["root", "horizon"]):
        r = g[g["strategy"] == "ridge"]
        proven = g[(g["pooled_verdict"] == "reliable") & (g["strategy"] != "ridge")]
        others = g[(g["strategy"] != "ridge") & (g["verdict"] != "noise")]
        rows.append({"root": root, "symbol": g["symbol"].iloc[0], "name": g["name"].iloc[0],
                     "sector": g["sector"].iloc[0], "horizon": h,
                     "ridge_pct": r["expected_pct"].iloc[0] if len(r) else np.nan,
                     "ridge_verdict": r["verdict"].iloc[0] if len(r) else "",
                     "ridge_ic": r["ic"].iloc[0] if len(r) else np.nan,
                     "proven_pct": proven["expected_pooled_pct"].sum() if len(proven) else np.nan,
                     "proven": ", ".join(f"{s} {v:+.2f}%" for s, v in zip(proven["strategy"],
                                                                         proven["expected_pooled_pct"])),
                     "long": ", ".join(others.loc[others["side"] == "LONG", "strategy"]),
                     "short": ", ".join(others.loc[others["side"] == "SHORT", "strategy"]),
                     "reliable_long": int(((others["side"] == "LONG") & (others["verdict"] == "reliable")).sum()),
                     "reliable_short": int(((others["side"] == "SHORT") & (others["verdict"] == "reliable")).sum())})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- updates

def update_history(notes):
    """Add the missing Databento days (cents) when the series miss the previous trading day, or today's session
    once it has settled (after SETTLED_CT)."""
    import databento_contracts
    import refresh
    newest = max((pd.read_parquet(SERIES / f"{r}.parquet", columns=["ret"]).index.max() for r in commodity_roots()),
                 default=None)
    now = pd.Timestamp.now(tz=CT).tz_localize(None)
    today = now.normalize()
    settled = now.hour >= SETTLED_CT and today.dayofweek < 5
    if newest is not None and newest >= (today if settled else today - pd.offsets.BDay(1)):
        return
    if not (os.environ.get("DATABENTO_API_KEY") or databento_contracts.env_file_key()):
        notes.append("Contract history not updated: no Databento key in cme_challenge/.env")
        return
    refresh.update_history(notes, databento=True, bloomberg=False)
    refresh.update_model(notes)


def add_today(series, notes, quotes=None):
    """Append today's provisional bar to each series that ends before today: Yahoo's delayed price of the very
    contract the series holds, against the series' own last price, so the signals read the market as it is now.
    The next Databento update replaces it with the settled day. Markets not trading yet keep yesterday's close;
    a market whose Yahoo price disagrees with the series on the last common day (other units) is left alone."""
    now = pd.Timestamp.now(tz=CT).tz_localize(None)
    today = now.normalize()
    stale = {r: s for r, s in series.items() if s.index[-1] < today}
    if not stale:
        return series
    tickers = {r: f"{s['contract'].iloc[-1]}.{MARKETS.get(r, MARKETS.get(f'{r} ')).exchange}" for r, s in stale.items()}
    if quotes is None:
        import live
        quotes = live.download(sorted(set(tickers.values())), period="5d")
    added, skipped = [], []
    for root, s in stale.items():
        q = quotes.get(tickers[root])
        if q is None or q.index[-1].normalize() != today:
            continue  # no trade yet today
        last_day, last = s.index[-1], s.iloc[-1]
        before = q["Close"][q.index.normalize() == last_day]
        if before.empty or abs(before.iloc[-1] / last["price"] - 1) > PROVISIONAL_TOLERANCE:
            skipped.append(root)
            continue
        ret = q["Close"].iloc[-1] / last["price"] - 1
        row = last.copy()
        row[["price", "ret", "next_price", "index"]] = [last["price"] * (1 + ret), ret, np.nan, last["index"] * (1 + ret)]
        series[root] = pd.concat([s, row.to_frame(today).T.astype(s.dtypes)])
        added.append(root)
    if added:
        notes.append(f"{today.date()} is provisional for {', '.join(added)}: Yahoo's delayed price at {now:%H:%M} CT "
                     f"until the session settles")
    if skipped:
        notes.append(f"No provisional bar for {', '.join(skipped)}: Yahoo's price disagrees with the contract history")
    return series


def run(update, notes):
    if update:
        try:
            update_history(notes)
        except Exception as e:
            notes.append(f"Contract history update failed: {e}")
    series = {r: pd.read_parquet(SERIES / f"{r}.parquet") for r in commodity_roots()}
    if not series:
        raise SystemExit(f"No commodity series in {SERIES}: run cme_challenge/databento_contracts.py and continuous.py")
    if update:
        try:
            series = add_today(series, notes)
        except Exception as e:  # the settled history is still there
            notes.append(f"Today's provisional prices unavailable: {e}")
    try:
        cot_table = cot.load(update)
    except Exception as e:
        notes.append(f"Positioning (CFTC) unavailable: {e}")
        cot_table = pd.DataFrame()
    gas_ctx, gas_errors = gas.context(update)
    notes.extend(f"Gas data, {e}" for e in gas_errors)
    outlook, weather_errors = None, []
    if update:
        try:
            outlook, weather_errors = weather.refresh()
        except Exception as e:
            weather_errors = [str(e)]
    if outlook is None:
        outlook = weather.last_outlook()
    notes.extend(f"Weather outlook, {e}" for e in weather_errors)
    extra = market_specific(series, update, notes, gas_ctx)
    db, coefficients = build(series, extra, cot_table)
    return db, summary(db), coefficients, gas_ctx, outlook, cot_table


# --------------------------------------------------------------------------- output

def fmt(value, digits=2, signed=False):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def print_terminal(db, summ, gas_ctx, outlook, notes, root):
    pd.set_option("display.width", 200)
    print(f"CME commodity signals, prices to {db['as_of'].max()} (before commissions; history, not certainty)\n")
    cells = summ.assign(cell=[f"{fmt(p, 2, True)}% {v[:3]}" if np.isfinite(p) else "—"
                              for p, v in zip(summ["ridge_pct"], summ["ridge_verdict"])])
    table = cells.pivot_table(index=["sector", "symbol", "name"], columns="horizon", values="cell", aggfunc="first")
    table.columns = [f"ridge {h}d" for h in table.columns]
    print("Ridge view: expected move over the horizon and its verdict (rel = reliable, sug = suggestive, noi = noise)")
    print(table.reset_index().drop(columns="sector").to_string(index=False))
    pooled = db[db["root"] == "ALL"]
    print("\nEach strategy on the 18 commodities together: IC (t) and verdict")
    grid = pooled.assign(cell=[f"{ic:+.3f} ({t:+.1f}) {v[:3]}" for ic, t, v in zip(pooled["ic"], pooled["t"],
                                                                                   pooled["verdict"])])
    grid = grid.pivot_table(index="strategy", columns="horizon", values="cell", aggfunc="first")
    grid.columns = [f"{h}d" for h in grid.columns]
    print(grid.fillna("—").to_string())
    proven = db[(db["root"] != "ALL") & (db["pooled_verdict"] == "reliable") & (db["strategy"] != "ridge")
                & db["expected_pooled_pct"].notna()]
    proven = proven.reindex(proven["expected_pooled_pct"].abs().sort_values(ascending=False).index).head(15)
    print("\nLargest moves implied today by strategies proven across markets (pooled reliable)")
    if proven.empty:
        print("  none")
    else:
        print(proven[["symbol", "horizon", "strategy", "expected_pooled_pct", "z_now", "pooled_ic", "ic", "verdict"]]
              .round({"expected_pooled_pct": 2, "z_now": 2, "pooled_ic": 3, "ic": 3}).to_string(index=False))
    own = db[(db["root"] != "ALL") & (db["verdict"] == "reliable")]
    print(f"\nMarket-specific reliable signals (q < 0.10 among {int((db['root'] != 'ALL').sum())} market tests): "
          + (", ".join(f"{s} {st} {h}d" for s, st, h in zip(own["symbol"], own["strategy"], own["horizon"]))
             if len(own) else "none"))
    print("\nNatural gas")
    for line in gas_lines(gas_ctx, outlook):
        print("  " + line)
    if root:
        rows = db[(db["root"] == root.upper()) | (db["symbol"] == root.upper())]
        print(f"\nEvery row for {root.upper()}")
        print(rows[["horizon", "strategy", "verdict", "side", "expected_pct", "pooled_verdict", "expected_pooled_pct",
                    "z_now", "ic", "ic_3y", "t", "q", "hit", "n"]].round(3).to_string(index=False))
    for n in notes:
        print("Note:", n)


def gas_lines(ctx, outlook):
    lines = []
    if outlook is not None and len(outlook) and "tdd_days_1_7" in outlook:
        for _, r in outlook.iterrows():
            lines.append(f"Weather {r['model']}: days 1-7 {fmt(r['hdd_days_1_7'], 0)} HDD + {fmt(r['cdd_days_1_7'], 0)} "
                         f"CDD, {fmt(r['vs_normal_days_1_7'], 0, True)} vs normal; days 8-15 "
                         f"{fmt(r['vs_normal_days_8_15'], 0, True)} vs normal; "
                         f"{fmt(r.get('change_vs_previous', np.nan), 1, True)} since the previous run")
    if "storage" in ctx:
        s = ctx["storage"].dropna(subset=["change"]).iloc[-1]
        lines.append(f"Storage week to {s.name.date()}: {s['level']:,.0f} Bcf, {s['change']:+,.0f} vs 5-year average "
                     f"{s['avg_change_5y']:+,.0f}; {-s['storage_deficit']:+.1f}% vs 5-year average level")
    t = ctx.get("ttf")
    if t is not None and len(t) > 6:
        lines.append(f"Europe TTF {t.index[-1].date()}: {t.iloc[-1]:.2f} EUR/MWh, {100 * (t.iloc[-1] / t.iloc[-6] - 1):+.1f}% "
                     "over 5 days")
    for name, m in ctx.get("monthly", {}).items():
        lines.append(f"{name} {m['month']}: {m['bcf_per_day']:.1f} Bcf/d, {m['change_vs_year_ago_pct']:+.1f}% on a year")
    gulf = [s for s in ctx.get("storms", []) if s["atlantic"]]
    lines.append("Atlantic storms: " + (", ".join(f"{s['name']} ({s['class']}, {s['wind_kt']} kt"
                                                    f"{', in the Gulf' if s['gulf'] else ''})" for s in gulf)
                                         if gulf else "none active"))
    return lines


CSS = """:root { --bg:#f7f8fa; --card:#fff; --ink:#1f2933; --muted:#5b6770; --rule:#e2e6ea; --up:#15803d;
  --down:#c2410c; --accent:#1d4ed8; --chip:#eef2f7; }
@media (prefers-color-scheme: dark) { :root { --bg:#111418; --card:#1a1f25; --ink:#e6e9ec; --muted:#9aa5ae;
  --rule:#2c333b; --up:#4ade80; --down:#fb923c; --accent:#93b4ff; --chip:#232a32; } }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1400px; margin:0 auto; padding:16px; }
h1 { font-size:22px; margin:4px 0; } h2 { font-size:16px; margin:0 0 8px; }
.card { background:var(--card); border:1px solid var(--rule); border-radius:8px; padding:14px; margin:12px 0; overflow-x:auto; }
table { border-collapse:collapse; width:100%; } th, td { padding:5px 8px; border-bottom:1px solid var(--rule);
  text-align:left; white-space:nowrap; }
th { font-weight:600; color:var(--muted); font-size:12px; } th[data-k] { cursor:pointer; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.up { color:var(--up); } .down { color:var(--down); } .muted { color:var(--muted); font-size:12px; }
.reliable { font-weight:600; } .noise { color:var(--muted); }
.filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
select, input { font:inherit; padding:4px 6px; border:1px solid var(--rule); border-radius:6px; background:var(--card); color:var(--ink); }
ul { margin:4px 0; padding-left:18px; } a { color:var(--accent); }"""


def pooled_table(db):
    pooled = db[db["root"] == "ALL"]
    if pooled.empty:
        return "<p class=muted>Not enough markets.</p>"
    head = "".join(f"<th>{h} day{'s' if h > 1 else ''}</th>" for h in HORIZONS)
    body = []
    for strategy, g in pooled.groupby("strategy"):
        cells = []
        for h in HORIZONS:
            r = g[g["horizon"] == h]
            if r.empty:
                cells.append("<td>—</td>")
                continue
            r = r.iloc[0]
            cls = ("up" if r["ic"] > 0 else "down") if r["verdict"] != "noise" else "noise"
            cells.append(f'<td class="num"><span class="{cls} {r["verdict"]}">{r["ic"]:+.3f}</span> '
                         f'<span class=muted>({r["t"]:+.1f}) {r["verdict"]}</span></td>')
        body.append(f"<tr><td title=\"{html.escape(DESCRIPTION.get(strategy, ''))}\">{html.escape(strategy)}</td>"
                    f"{''.join(cells)}</tr>")
    return f"<table><thead><tr><th>Strategy</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render(db, summ, gas_ctx, outlook, notes):
    data = db.assign(**{c: db[c].astype(str) for c in ("from", "as_of")}).astype(object)
    data = data.where(data.notna(), None)
    payload = json.dumps(data.to_dict(orient="records")).replace("</", "<\\/")
    rows = []
    for (sector, root), g in summ.groupby(["sector", "root"], sort=True):
        cells = []
        for h in HORIZONS:
            r = g[g["horizon"] == h]
            if r.empty or not np.isfinite(r["ridge_pct"].iloc[0]):
                cells.append("<td>—</td>")
                continue
            r = r.iloc[0]
            cls = "up" if r["ridge_pct"] > 0 else "down"
            support = f"{r['reliable_long']}▲ {r['reliable_short']}▼" if r["reliable_long"] + r["reliable_short"] else ""
            proven = (f'<br><span class="muted" title="{html.escape(r["proven"])}">proven {r["proven_pct"]:+.2f}%</span>'
                      if np.isfinite(r["proven_pct"]) else "")
            cells.append(f'<td class="num"><span class="{cls} {r["ridge_verdict"]}">{r["ridge_pct"]:+.2f}%</span> '
                         f'<span class=muted>{html.escape(r["ridge_verdict"])} {support}</span>{proven}</td>')
        rows.append(f"<tr><td>{html.escape(sector)}</td><td><b>{html.escape(g['symbol'].iloc[0])}</b> "
                    f"{html.escape(g['name'].iloc[0])}</td>{''.join(cells)}</tr>")
    head = "".join(f"<th>{h} day{'s' if h > 1 else ''}</th>" for h in HORIZONS)
    gas_html = "".join(f"<li>{html.escape(line)}</li>" for line in gas_lines(gas_ctx, outlook))
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes) or "<li>Every source updated.</li>"
    strategies = sorted(db["strategy"].unique())
    options = lambda values: "".join(f'<option value="{html.escape(str(v))}">{html.escape(str(v))}</option>'
                                     for v in values)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CME Signal Database</title>
<style>{CSS}</style></head><body><main>
<h1>CME Signal Database</h1>
<div class=muted>Built {pd.Timestamp.now():%a %d %b %H:%M} · prices to {db['as_of'].max()} · 18 CME commodities,
horizons of {', '.join(map(str, HORIZONS))} trading days · before commissions. A reliable signal is a small, repeated
edge, not certainty.</div>
<div class="card"><h2>Ridge view per market</h2><table><thead><tr><th>Sector</th><th>Market</th>{head}</tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class=muted>Ridge: expected move over the horizon from all common signals combined (momentum, carry, season,
positioning), with its own verdict out of sample. Proven: the sum of the moves implied by the strategies that are
reliable across markets. ▲/▼ count the market's own reliable signals pointing up or down.</p></div>
<div class="card"><h2>Which strategies work across the 18 commodities</h2>{pooled_table(db)}
<p class=muted>IC of each strategy on all markets together (t in brackets). Reliable = q &lt; 0.10 among these pooled
tests and the same sign over the last 3 years. Negative IC on a momentum row means moves tend to reverse.</p></div>
<div class="card"><h2>Natural gas</h2><ul>{gas_html}</ul>
<p class=muted>Weather: gas-weighted heating plus cooling degree days in 26 US cities (positive = more gas demand).
LNG terminals run near capacity, so Europe (TTF) moves Henry Hub mostly through sentiment and outages. A Gulf
storm can cut LNG demand (bearish) or offshore supply (bullish). Not covered: LNG feedgas, European storage,
power demand including data centers (no free daily source).</p></div>
<div class="card"><h2>Every strategy, market and horizon</h2>
<div class=filters>
<select id=f-root><option value="">All markets</option>{options(sorted(db['symbol'].unique()))}</select>
<select id=f-h><option value="">All horizons</option>{options(HORIZONS)}</select>
<select id=f-s><option value="">All strategies</option>{options(strategies)}</select>
<select id=f-v><option value="">All verdicts</option>{options(['reliable', 'suggestive', 'noise'])}</select>
<input id=f-q placeholder="search" size=12><span id=count class=muted></span></div>
<table><thead><tr>
<th data-k=symbol>Market</th><th data-k=horizon>Days</th><th data-k=strategy>Strategy</th><th data-k=verdict>Verdict</th>
<th data-k=side>Side</th><th data-k=expected_pct>Expected %</th><th data-k=pooled_verdict>Pooled</th>
<th data-k=expected_pooled_pct>Expected % (pooled IC)</th><th data-k=z_now>Signal z</th><th data-k=ic>IC</th>
<th data-k=ic_3y>IC 3y</th><th data-k=t>t</th><th data-k=q>q</th><th data-k=hit>Hit</th><th data-k=years_same_sign>Years same sign</th>
<th data-k=n>Days tested</th><th data-k=from>From</th></tr></thead><tbody id=rows></tbody></table>
<p class=muted>IC: rank correlation of the signal with the next h-day return. t: from the yearly ICs. q: false-discovery
rate over all {len(db)} rows. Reliable: q &lt; 0.10 and the last 3 years agree. Expected %: IC × today's z-score × a normal
h-day move. Click a column to sort.</p></div>
<div class="card"><h2>Pipeline</h2><ul class=muted>{notes_html}</ul></div>
</main>
<script>
const DATA = {payload};
let key = "expected_pct", dir = -1;
const $ = id => document.getElementById(id);
const num = (v, d, s) => v === null ? "—" : (s && v > 0 ? "+" : "") + v.toFixed(d);
function draw() {{
  const f = {{root: $("f-root").value, h: $("f-h").value, s: $("f-s").value, v: $("f-v").value, q: $("f-q").value.toLowerCase()}};
  let rows = DATA.filter(r => (!f.root || r.symbol === f.root) && (!f.h || String(r.horizon) === f.h)
    && (!f.s || r.strategy === f.s) && (!f.v || r.verdict === f.v)
    && (!f.q || (r.name + " " + r.strategy + " " + (r.description || "")).toLowerCase().includes(f.q)));
  const abs = ["expected_pct", "expected_pooled_pct", "ic", "t"].includes(key);
  rows.sort((a, b) => {{ let x = a[key], y = b[key]; if (x === null) return 1; if (y === null) return -1;
    if (abs) {{ x = Math.abs(x); y = Math.abs(y); }} return (x > y ? 1 : x < y ? -1 : 0) * dir; }});
  $("count").textContent = rows.length + " rows";
  $("rows").innerHTML = rows.slice(0, 1500).map(r => `<tr class="${{r.verdict}}"><td><b>${{r.symbol}}</b> ${{r.name}}</td>
    <td class=num>${{r.horizon}}</td><td title="${{r.description || ""}}">${{r.strategy}}</td><td>${{r.verdict}}</td>
    <td class="${{r.side === "LONG" ? "up" : "down"}}">${{r.side}}</td><td class=num>${{num(r.expected_pct, 2, true)}}</td>
    <td>${{r.pooled_verdict || ""}}</td><td class=num>${{num(r.expected_pooled_pct, 2, true)}}</td>
    <td class=num>${{num(r.z_now, 2, true)}}</td><td class=num>${{num(r.ic, 3, true)}}</td><td class=num>${{num(r.ic_3y, 3, true)}}</td>
    <td class=num>${{num(r.t, 1, true)}}</td><td class=num>${{num(r.q, 2)}}</td><td class=num>${{num(r.hit === null ? null : 100 * r.hit, 0)}}%</td>
    <td class=num>${{num(r.years_same_sign === null ? null : 100 * r.years_same_sign, 0)}}% of ${{r.years}}</td>
    <td class=num>${{r.n}}</td><td>${{r.from}}</td></tr>`).join("");
}}
document.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {{
  dir = key === th.dataset.k ? -dir : -1; key = th.dataset.k; draw(); }});
["f-root", "f-h", "f-s", "f-v", "f-q"].forEach(id => $(id).oninput = draw);
draw();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="use saved data only, download nothing")
    ap.add_argument("--root", help="also print every row for this market (e.g. NG)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    args = ap.parse_args()
    notes = []
    db, summ, coefficients, gas_ctx, outlook, _ = run(not args.offline, notes)
    OUT.mkdir(parents=True, exist_ok=True)
    db.to_csv(OUT / "signals.csv", index=False)
    summ.to_csv(OUT / "summary.csv", index=False)
    (OUT / "ridge.json").write_text(json.dumps({str(h): {k: round(float(v), 6) for k, v in c.items()}
                                                for h, c in coefficients.items()}, indent=1))
    page = OUT / "cme.html"
    page.write_text(render(db, summ, gas_ctx, outlook, notes), encoding="utf-8")
    print_terminal(db, summ, gas_ctx, outlook, notes, args.root)
    print(f"\nSaved {OUT / 'signals.csv'} and {page}")
    if not args.no_browser:
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
