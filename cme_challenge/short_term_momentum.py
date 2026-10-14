"""Short-term reversal, flipped: do commodities that moved most over the last days keep going?

event_strategies.py found weekly reversal (long last week's losers) LOST money on commodities (Sharpe about
-0.4), so the flipped bet is short-term momentum. A direction picked after seeing a backtest is how false
discoveries are made, so this checks whether the result holds across lookbacks, holding periods, both halves of
the sample and the last 3 years, instead of trusting the one setting that looked good.

Equity reversal is paid for supplying liquidity, and pays most when the VIX is high (Nagel, RFS 2012); each
variant is also split by VIX regime (above its own trailing 1-year 2/3 quantile, known the day before).

Variants (18 commodities, roll-correct daily series, returns in units of each market's 63-day volatility):
  xs  cross-section: long the top third by vol-scaled lookback return, short the bottom third
  ts  time series: each market long if its lookback return is positive, short if negative, equal risk
Costs: one tick plus $4 a contract per round trip, charged on each position change.

Usage (from the repository root):
    python cme_challenge/short_term_momentum.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from futures_research import RESEARCH, load_series, root_info

COMMODITIES = ("Energy", "Grains", "Livestock", "Metals")
LOOKBACKS, HOLDS = (1, 2, 5, 10), (1, 5)
VOL_DAYS = 63
COST_VOL = 0.03  # round-trip cost in units of daily volatility (about 1 tick + $4 against a 1.5-2% daily move)
PERIODS = [("2010-2017", "2010", "2017"), ("2018-2026", "2018", "2026")]


def vix():
    import yfinance as yf
    v = yf.download("^VIX", start="2005-01-01", progress=False, auto_adjust=False)["Close"].squeeze()
    v.index = pd.to_datetime(v.index).tz_localize(None)
    high = v > v.rolling(252, min_periods=120).quantile(2 / 3)
    return high.shift(1).astype("boolean"), float(v.iloc[-1]), bool(high.iloc[-1])


def positions(past, kind):
    if kind == "ts":
        return np.sign(past)
    rank = past.rank(axis=1, pct=True)
    enough = past.notna().sum(axis=1) >= 6
    side = pd.DataFrame(np.where(rank > 2 / 3, 1.0, np.where(rank <= 1 / 3, -1.0, 0.0)), index=past.index,
                        columns=past.columns)
    return side.where(past.notna()).where(enough, axis=0)


def run(scaled, kind, lookback, hold):
    """Daily P&L in volatility units of a book rebalanced every `hold` days, after costs."""
    past = scaled.rolling(lookback).sum()
    pos = positions(past, kind)
    if hold > 1:  # set at the close, held `hold` days
        pos = pos.iloc[::hold].reindex(scaled.index).ffill(limit=hold - 1)
    held = pos.shift(1)  # earns from the next day
    trades = held.fillna(0).diff().abs()
    pnl = (held * scaled - trades * COST_VOL / 2).sum(axis=1) / held.abs().sum(axis=1).replace(0, np.nan)
    return pnl.dropna()


def stats(x):
    x = x.dropna()
    if len(x) < 100:
        return {"days": len(x)}
    yearly = x.groupby(x.index.year).mean()
    return {"days": len(x), "sharpe": x.mean() / x.std() * np.sqrt(252),
            "t_years": yearly.mean() / yearly.std(ddof=1) * np.sqrt(len(yearly)),
            "years_positive": (yearly > 0).mean()}


def main():
    info = root_info()
    series = {r: s for r, s in load_series().items() if info["sector"].get(r) in COMMODITIES}
    rets = pd.DataFrame({r: s["ret"] for r, s in series.items()}).sort_index().loc["2010-06":]
    vol = rets.rolling(VOL_DAYS, min_periods=40).std().shift(1)
    scaled = (rets / vol).clip(-5, 5)
    try:
        high_vix, vix_now, high_now = vix()
    except Exception as e:  # the VIX split is a bonus
        print(f"VIX unavailable ({e}); no regime split")
        high_vix, vix_now, high_now = None, np.nan, None
    rows = []
    for kind in ("xs", "ts"):
        for lookback in LOOKBACKS:
            for hold in HOLDS:
                pnl = run(scaled, kind, lookback, hold)
                row = {"kind": kind, "lookback": lookback, "hold": hold, **stats(pnl)}
                for name, a, b in PERIODS:
                    row[f"sharpe_{name}"] = stats(pnl.loc[a:b]).get("sharpe")
                row["sharpe_last_3y"] = stats(pnl[pnl.index >= pnl.index.max() - pd.DateOffset(years=3)]).get("sharpe")
                if high_vix is not None:
                    regime = high_vix.reindex(pnl.index)
                    row["sharpe_high_vix"] = stats(pnl[regime.fillna(False).astype(bool)]).get("sharpe")
                    row["sharpe_normal_vix"] = stats(pnl[~regime.fillna(True).astype(bool)]).get("sharpe")
                rows.append(row)
    table = pd.DataFrame(rows)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "short_term_momentum.csv", index=False)
    with pd.option_context("display.width", 220, "display.float_format", "{:.2f}".format):
        print(f"Short-term momentum on {rets.shape[1]} commodities, {rets.index[0]:%Y-%m} to {rets.index[-1]:%Y-%m}, "
              f"after costs (lookback and hold in trading days):\n")
        print(table.to_string(index=False))
    if high_now is not None:
        print(f"\nVIX now {vix_now:.1f}: {'HIGH' if high_now else 'normal'} regime against its last year")
    print(f"\nSaved {RESEARCH / 'short_term_momentum.csv'}")


if __name__ == "__main__":
    main()
