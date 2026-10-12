"""Research on the roll-correct futures series: seasonal windows, trend and carry, and current signals.

Input: <data>/futures/continuous/ from cme_challenge/continuous.py, plus contracts.parquet for point values.
Returns are within-contract price changes, so they are tradable before commissions. They are still history:
a pattern that held for 20 years can fail in the next three weeks.

Usage (from the repository root, no Bloomberg needed):
    python cme_challenge/futures_research.py window --start 10-09 --end 10-30          # every root
    python cme_challenge/futures_research.py window --start 10-09 --end 10-30 --detail LC
    python cme_challenge/futures_research.py backtest --horizon 15                     # trend and carry
    python cme_challenge/futures_research.py signals --risk 5000                       # today's signals and sizes
    python cme_challenge/futures_research.py sizing                                    # constant vs vol-targeted

window   Return over the same calendar window each year. The placebo p-value compares the window's t-statistic
         with same-length windows starting on every other date of the year, so a root that simply trends all
         year does not look seasonal; q-values correct for testing every root at once (Benjamini-Hochberg).
backtest Equal-risk portfolios rebalanced every --horizon trading days: time-series trend over several
         lookbacks, carry (sign, and long top third / short bottom third), and trend plus carry.
signals  Latest trend and carry per root, volatility, contract value and the number of contracts that carry
         --risk dollars of daily volatility.
sizing   Constant size against volatility targeting (size inverse to recent volatility, rebalanced daily from
         the previous day's estimate) for long positions by sector, 12-month trend and combined trend. Every
         rule is scaled to the same average risk, so the Sharpe ratio, drawdown and worst 15 days compare like
         with like; turnover is the average daily change in size relative to the average size.

Outputs are saved under <data>/futures/research/.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_markets import MARKETS

FUT = DATA / "futures"
SERIES = FUT / "continuous"
RESEARCH = FUT / "research"
LOOKBACKS = (21, 63, 126, 252)  # about 1, 3, 6 and 12 months of trading days
VOL_DAYS = 63


def load_series(roots=None):
    wanted = {r.strip().upper() for r in roots} if roots else None
    series = {p.stem: pd.read_parquet(p) for p in sorted(SERIES.glob("*.parquet"))
              if not wanted or p.stem in wanted}
    if not series:
        raise SystemExit(f"No series in {SERIES}; run cme_challenge/continuous.py first")
    return series


def root_info():
    """Name, sector and point value per root: from the contract details, else from futures_markets.py."""
    table = pd.DataFrame({"name": [m.name for m in MARKETS.values()], "sector": [m.sector for m in MARKETS.values()],
                          "point_value": [float(m.point_value) for m in MARKETS.values()]},
                         index=[r.strip() for r in MARKETS])
    paths = [p for p in (FUT / "contracts.parquet", FUT / "databento" / "contracts.parquet") if p.exists()]
    if not paths:
        return table
    c = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)  # Databento last: it wins below
    databento = c["source"].eq("databento") if "source" in c else False
    c = c.assign(root=c["root"].str.strip(), _databento=databento).sort_values(["_databento", "LAST_TRADEABLE_DT"])
    last = c.groupby("root").last()
    details = pd.DataFrame({"name": last["name"], "sector": last["sector"],
                            "point_value": last.get("FUT_VAL_PT", pd.Series(np.nan, index=last.index))})
    return pd.concat([details, table.drop(details.index, errors="ignore")])


# --------------------------------------------------------------------------- seasonal windows

def window_returns(index, start_md, end_md, tolerance=5):
    """Yearly returns from the last close on or before start_md to the last close on or before end_md."""
    dates = index.index
    out = {}
    for year in range(dates[0].year, dates[-1].year + 1):
        a = pd.Timestamp(f"{year}-{start_md}")
        b = pd.Timestamp(f"{year}-{end_md}")
        if b <= a:
            b += pd.DateOffset(years=1)  # window across the year end
        if a < dates[0] or b > dates[-1]:
            continue
        i0, i1 = dates.searchsorted(a, side="right") - 1, dates.searchsorted(b, side="right") - 1
        if i1 <= i0 or (a - dates[i0]).days > tolerance or (b - dates[i1]).days > tolerance:
            continue
        out[year] = index.iloc[i1] / index.iloc[i0] - 1
    return pd.Series(out, dtype=float)


def t_stat(values):
    values = values.dropna()
    if len(values) < 3 or values.std(ddof=1) == 0:
        return np.nan
    return values.mean() / (values.std(ddof=1) / np.sqrt(len(values)))


def placebo_p(index, start_md, end_md, observed_t, step=3):
    """Share of same-length windows starting elsewhere in the year with a |t| at least as large."""
    a, b = pd.Timestamp(f"2001-{start_md}"), pd.Timestamp(f"2001-{end_md}")
    length = (b - a).days % 365
    placebo = []
    for offset in range(0, 365, step):
        start = pd.Timestamp("2001-01-01") + pd.Timedelta(days=offset)
        gap = abs((start - a).days)
        if min(gap, 365 - gap) < length:  # overlaps the window being tested
            continue
        end = start + pd.Timedelta(days=length)
        placebo.append(abs(t_stat(window_returns(index, start.strftime("%m-%d"), end.strftime("%m-%d")))))
    placebo = np.array([t for t in placebo if np.isfinite(t)])
    if not len(placebo) or not np.isfinite(observed_t):
        return np.nan
    return (1 + (placebo >= abs(observed_t)).sum()) / (1 + len(placebo))


def bh_qvalues(p):
    """Benjamini-Hochberg q-values, NaN kept in place."""
    p = pd.Series(p, dtype=float)
    valid = p.dropna().sort_values()
    if valid.empty:
        return p
    n = len(valid)
    q = (valid * n / np.arange(1, n + 1)).iloc[::-1].cummin().iloc[::-1].clip(upper=1)
    return q.reindex(p.index)


def run_window(series, info, start_md, end_md, min_years, detail):
    rows = []
    for root, s in series.items():
        yearly = window_returns(s["index"], start_md, end_md)
        if len(yearly) < min_years:
            continue
        t = t_stat(yearly)
        notional = s["price"].iloc[-1] * info["point_value"].get(root, np.nan)
        rows.append({"root": root, "name": info["name"].get(root, root), "years": len(yearly),
                     "first": yearly.index.min(), "mean_pct": 100 * yearly.mean(),
                     "median_pct": 100 * yearly.median(), "up_years": (yearly > 0).mean(), "t": t,
                     "placebo_p": placebo_p(s["index"], start_md, end_md, t),
                     "mean_usd_per_contract": yearly.mean() * notional})
    table = pd.DataFrame(rows)
    if table.empty:
        raise SystemExit(f"No root has {min_years} years of data for this window")
    table["q"] = bh_qvalues(table["placebo_p"]).to_numpy()
    table = table.sort_values("t").reset_index(drop=True)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    name = f"window_{start_md}_{end_md}".replace("-", "")
    table.to_csv(RESEARCH / f"{name}.csv", index=False)
    print(f"Window {start_md} -> {end_md}, roll-correct returns (placebo p: share of other windows this strong; "
          f"q: corrected for testing {len(table)} roots)\n")
    print(table.round({"mean_pct": 2, "median_pct": 2, "up_years": 2, "t": 2, "placebo_p": 3, "q": 3,
                       "mean_usd_per_contract": 0}).to_string(index=False))
    if detail:
        root = detail.strip().upper()
        s = series.get(root)
        if s is None:
            raise SystemExit(f"No series for {root}")
        rows = []
        for year, ret in window_returns(s["index"], start_md, end_md).items():
            a, b = pd.Timestamp(f"{year}-{start_md}"), pd.Timestamp(f"{year}-{end_md}")
            seg = s.loc[s.index[s.index.searchsorted(a, side="right") - 1]:b]
            rows.append({"year": year, "entry": seg.index[0].date(), "exit": seg.index[-1].date(),
                         "held": " -> ".join(dict.fromkeys(seg["contract"].iloc[:-1].dropna())),
                         "return_pct": round(100 * ret, 2)})
        print(f"\n{root} by year (contracts held between entry and exit):\n")
        print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nSaved {RESEARCH / (name + '.csv')}")


# --------------------------------------------------------------------------- trend and carry

def panels(series):
    calendar = pd.DatetimeIndex(sorted(set().union(*(s.index for s in series.values()))))
    index = pd.DataFrame({r: s["index"] for r, s in series.items()}).reindex(calendar).ffill(limit=5)
    ret = pd.DataFrame({r: s["ret"] for r, s in series.items()}).reindex(calendar)
    carry = pd.DataFrame({r: s["carry"] for r, s in series.items()}).reindex(calendar).ffill(limit=5)
    return index, ret, carry


def signals_at(index, carry, i):
    """Signals known at the close of calendar row i (no later data used)."""
    out = {}
    for lb in LOOKBACKS:
        if i - lb >= 0:
            out[f"trend_{lb}d"] = np.sign(index.iloc[i] / index.iloc[i - lb] - 1)
    trend = [out[k] for k in out if k.startswith("trend_")]
    if trend:
        out["trend_combo"] = pd.concat(trend, axis=1).mean(axis=1)
    c = carry.iloc[i]
    out["carry_sign"] = np.sign(c)
    ranks = c.rank(pct=True)
    out["carry_xs"] = pd.Series(np.where(ranks > 2 / 3, 1.0, np.where(ranks <= 1 / 3, -1.0, 0.0)),
                                index=c.index).where(c.notna())
    if "trend_combo" in out:
        out["trend_plus_carry"] = (out["trend_combo"] + out["carry_sign"]) / 2
    return out


def run_backtest(series, info, horizon, start):
    index, ret, carry = panels(series)
    vol = ret.rolling(VOL_DAYS, min_periods=40).std()
    first = max(index.index.searchsorted(pd.Timestamp(start)), max(LOOKBACKS))
    periods = []
    for i in range(first, len(index) - horizon, horizon):
        forward = index.iloc[i + horizon] / index.iloc[i] - 1
        risk = vol.iloc[i] * np.sqrt(horizon)
        row = {"date": index.index[i]}
        for name, signal in signals_at(index, carry, i).items():
            scaled = (signal * forward / risk).replace([np.inf, -np.inf], np.nan).dropna()
            row[name] = scaled.mean() if len(scaled) else np.nan
            if name == "trend_combo":
                sectors = info["sector"].reindex(scaled.index).fillna("Other")
                for sector, values in scaled.groupby(sectors):
                    row[f"trend_combo | {sector}"] = values.mean()
        periods.append(row)
    p = pd.DataFrame(periods).set_index("date")
    if p.empty:
        raise SystemExit("Not enough history for a backtest")

    def stats(x):
        x = x.dropna()
        sd = x.std(ddof=1)
        return {"periods": len(x), "mean": x.mean(), "t": t_stat(x),
                "sharpe": x.mean() / sd * np.sqrt(252 / horizon) if sd > 0 else np.nan,
                "hit_rate": (x > 0).mean(), "worst": x.min()}

    recent = p.index >= p.index[-1] - pd.DateOffset(years=5)
    table = pd.DataFrame({name: {**stats(p[name]), "sharpe_last_5y": stats(p.loc[recent, name])["sharpe"]}
                          for name in p.columns}).T
    RESEARCH.mkdir(parents=True, exist_ok=True)
    p.to_csv(RESEARCH / f"backtest_{horizon}d_periods.csv")
    table.to_csv(RESEARCH / f"backtest_{horizon}d.csv")
    print(f"Equal-risk portfolios, {horizon}-trading-day holds from {p.index[0].date()} to {p.index[-1].date()}.\n"
          "mean/worst: per-period return in units of each root's expected volatility; sharpe: annualised, "
          "before costs.\n")
    print(table.astype(float).round(3).to_string())
    print(f"\nSaved {RESEARCH / f'backtest_{horizon}d.csv'} and the per-period returns")


# --------------------------------------------------------------------------- position sizing

SIZING_RULES = {  # name: (volatility lookback in days or None for constant size, cap, rebalancing band)
    "constant size": (None, None, 0.0),
    "vol target, 63 days": (63, None, 0.0),
    "vol target, 21 days": (21, None, 0.0),
    "vol target, 21 days, capped, 25% band": (21, 2.0, 0.25),
}


def sized_weights(ret, lookback, cap=None, band=0.0):
    """Daily size per unit of risk: 1 / yesterday's volatility, at most `cap` times the 1-year-volatility size,
    changed only when the target moves by more than `band` (fewer trades). None lookback = constant size."""
    if lookback is None:
        return pd.Series(1.0, index=ret.index)
    target = 1 / ret.rolling(lookback, min_periods=max(10, lookback // 2)).std().shift(1)
    if cap is not None:
        target = target.clip(upper=cap / ret.rolling(252, min_periods=126).std().shift(1))
    if band <= 0:
        return target
    held, out = np.nan, []
    for t in target.to_numpy():
        if np.isfinite(t) and (not np.isfinite(held) or abs(t / held - 1) > band):
            held = t
        out.append(held)
    return pd.Series(out, index=ret.index)


def sizing_stats(daily, weights):
    """Sharpe, drawdown and worst 15 days at 15%/year average risk, plus turnover."""
    daily = daily.dropna()
    if daily.empty or daily.std() == 0:
        return {}
    scaled = daily * (0.15 / np.sqrt(252)) / daily.std()  # same average risk for every rule
    equity = (1 + scaled).cumprod()
    w = weights.reindex(daily.index)
    return {"sharpe": daily.mean() / daily.std() * np.sqrt(252), "max_drawdown": (equity / equity.cummax() - 1).min(),
            "worst_15_days": scaled.rolling(15).sum().min(),
            "turnover": (w.diff().abs().mean() / w.abs().mean()) if w.abs().mean() > 0 else np.nan,
            "from": daily.index[0].date()}


def run_sizing(series, info, start):
    index, ret, _ = panels(series)
    ret = ret.loc[start:]
    index = index.loc[ret.index]
    trend12 = np.sign(index / index.shift(252) - 1).shift(1)  # known at yesterday's close
    combo = pd.concat([np.sign(index / index.shift(lb) - 1) for lb in LOOKBACKS]).groupby(level=0).mean().shift(1)
    strategies = {f"long {s.lower()}": (pd.DataFrame(1.0, index=ret.index, columns=ret.columns),
                                        [r for r in ret.columns if info["sector"].get(r) == s])
                  for s in sorted(info["sector"].reindex(ret.columns).dropna().unique())}
    strategies["12-month trend, all markets"] = (trend12, list(ret.columns))
    strategies["trend combo, all markets"] = (combo, list(ret.columns))
    rows = []
    for name, (signal, roots) in strategies.items():
        for rule, (lookback, cap, band) in SIZING_RULES.items():
            legs, weights = [], []
            for r in roots:
                x = ret[r].dropna()
                w = sized_weights(x, lookback, cap, band)
                if lookback is None:
                    w = w / x.std()  # constant size, same risk per market as the others on average
                legs.append(signal[r].reindex(x.index) * w * x)
                weights.append(w)
            port = pd.concat(legs, axis=1).mean(axis=1)
            rows.append({"strategy": name, "sizing": rule, "markets": len(roots),
                         **sizing_stats(port, pd.concat(weights, axis=1).mean(axis=1))})
    table = pd.DataFrame(rows)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "sizing.csv", index=False)
    print("Constant size vs volatility targeting, daily, before costs; every rule scaled to 15%/year average risk.\n")
    print(table.round({"sharpe": 2, "max_drawdown": 3, "worst_15_days": 3, "turnover": 3}).to_string(index=False))
    print(f"\nSaved {RESEARCH / 'sizing.csv'}")


# --------------------------------------------------------------------------- current signals

def run_signals(series, info, risk):
    index, ret, carry = panels(series)
    i = len(index) - 1
    current = pd.DataFrame(signals_at(index, carry, i))
    daily_vol = ret.rolling(VOL_DAYS, min_periods=40).std().iloc[i]
    rows = []
    for root, s in series.items():
        last = s.iloc[-1]
        notional = last["price"] * info["point_value"].get(root, np.nan)
        usd_vol = notional * daily_vol.get(root, np.nan)
        rows.append({"root": root, "name": info["name"].get(root, root), "sector": info["sector"].get(root, ""),
                     "as_of": s.index[-1].date(), "contract": last["contract"], "price": last["price"],
                     **{k: current.at[root, k] for k in current.columns if k.startswith("trend_")},
                     "carry_pct_yr": 100 * last["carry"], "vol_pct_yr": 100 * daily_vol.get(root, np.nan)
                     * np.sqrt(252), "notional_usd": notional, "daily_vol_usd": usd_vol,
                     "contracts_for_risk": risk / usd_vol if usd_vol and np.isfinite(usd_vol) else np.nan})
    table = pd.DataFrame(rows).sort_values(["sector", "root"])
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "signals_latest.csv", index=False)
    print(f"Signals at each root's latest close; contracts_for_risk = contracts with ${risk:,.0f} daily volatility.\n")
    print(table.round(2).to_string(index=False))
    stale = table[pd.to_datetime(table["as_of"]) < index.index[-1] - pd.Timedelta(days=5)]
    if not stale.empty:
        print(f"\nStale data (older than 5 days): {', '.join(stale['root'])}; rerun the collector.")
    print(f"\nSaved {RESEARCH / 'signals_latest.csv'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    w = sub.add_parser("window", help="same calendar window every year")
    w.add_argument("--start", default="10-09", help="MM-DD; entry at the last close on or before it")
    w.add_argument("--end", default="10-30", help="MM-DD; exit at the last close on or before it")
    w.add_argument("--min-years", type=int, default=8)
    w.add_argument("--detail", help="root to show year by year, e.g. LC")
    b = sub.add_parser("backtest", help="trend and carry portfolios")
    b.add_argument("--horizon", type=int, default=15, help="trading days per holding period")
    b.add_argument("--start", default="2006-01-01")
    g = sub.add_parser("signals", help="current trend, carry and position sizes")
    g.add_argument("--risk", type=float, default=5000, help="daily volatility in USD per position")
    z = sub.add_parser("sizing", help="constant size vs volatility targeting")
    z.add_argument("--start", default="2006-01-01")
    for p in (w, b, g, z):
        p.add_argument("--roots", nargs="+")
    args = ap.parse_args()
    series, info = load_series(args.roots), root_info()
    if args.command == "window":
        run_window(series, info, args.start, args.end, args.min_years, args.detail)
    elif args.command == "backtest":
        run_backtest(series, info, args.horizon, args.start)
    elif args.command == "sizing":
        run_sizing(series, info, args.start)
    else:
        run_signals(series, info, args.risk)


if __name__ == "__main__":
    main()
