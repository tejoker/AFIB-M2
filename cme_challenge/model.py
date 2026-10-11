"""Walk-forward model: one view per market on the next 15 trading days, from trend, carry and seasonality.

Trained on the roll-correct Bloomberg series from cme_challenge/continuous.py. It is a ridge regression pooled
across markets, whose target is the next --horizon-day return divided by the market's expected volatility over
that period: an output of +0.3 means "up by about a third of a normal 15-day move". Each year is predicted by
a model fitted only on earlier years; the hit rate and Sharpe ratio reported are those out-of-sample predictions,
next to trend alone and carry alone. The final model, fitted on every year, is saved for the live dashboard.

Usage (from the repository root, no Bloomberg needed once the series exist):
    python cme_challenge/model.py
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_research import LOOKBACKS, VOL_DAYS, load_series

MODEL_DIR = DATA / "futures" / "model"
FEATURES = [f"trend_{lb}d" for lb in LOOKBACKS] + ["carry", "seasonal"]
HORIZON = 15
ALPHA = 0.01  # ridge penalty per observation on standardised features


def features(index, carry):
    """Inputs known at each close, from a price index of within-contract returns and the carry series."""
    f = pd.DataFrame(index=index.index)
    for lb in LOOKBACKS:
        f[f"trend_{lb}d"] = np.sign(index / index.shift(lb) - 1)
    f["carry"] = carry.reindex(index.index).clip(-1, 1)  # annualised; capped at +-100% (near-expiry outliers)
    f["vol"] = index.pct_change().rolling(VOL_DAYS, min_periods=40).std()
    return f


def forward_scaled(index, vol, horizon):
    return (index.shift(-horizon) / index - 1) / (vol * np.sqrt(horizon))


def seasonal_table(target, half_width=3):
    """Day of year x year: scaled forward return, averaged over neighbouring days (not across the year end)."""
    df = pd.DataFrame({"y": target.to_numpy(), "year": target.index.year,
                       "doy": np.minimum(target.index.dayofyear, 365)})
    table = df.pivot_table(index="doy", columns="year", values="y", aggfunc="mean").reindex(range(1, 366))
    return table.rolling(2 * half_width + 1, center=True, min_periods=1).mean()


def seasonal_feature(target, min_years=5):
    """For each date, the mean of the same calendar window in earlier years only."""
    table = seasonal_table(target)
    counts = table.notna().T.cumsum().shift(1).T
    sums = table.fillna(0).T.cumsum().shift(1).T
    prior = (sums / counts).where(counts >= min_years)
    years = {year: i for i, year in enumerate(prior.columns)}
    doy = np.minimum(target.index.dayofyear, 365) - 1
    col = np.array([years.get(y, -1) for y in target.index.year])
    values = np.where(col >= 0, prior.to_numpy()[doy, np.maximum(col, 0)], np.nan)
    return pd.Series(values, index=target.index)


def seasonal_now(index, carry, today, horizon=HORIZON, min_years=5):
    """Mean scaled return of the window starting today's calendar date, over all earlier years."""
    target = forward_scaled(index, features(index, carry)["vol"], horizon)
    table = seasonal_table(target)
    row = table.loc[min(pd.Timestamp(today).dayofyear, 365), [y for y in table.columns if y < pd.Timestamp(today).year]]
    return row.mean() if row.notna().sum() >= min_years else np.nan


def training_panel(series, horizon):
    frames = []
    for root, s in series.items():
        f = features(s["index"], s["carry"])
        f["target"] = forward_scaled(s["index"], f["vol"], horizon)
        f["seasonal"] = seasonal_feature(f["target"])
        f["root"] = root
        frames.append(f)
    data = pd.concat(frames).rename_axis("date").reset_index()
    data[["carry", "seasonal"]] = data[["carry", "seasonal"]].fillna(0)  # missing = no view
    return data.dropna(subset=[f"trend_{lb}d" for lb in LOOKBACKS] + ["target"])


def fit(X, y, alpha=ALPHA):
    scale = X.std(axis=0)
    scale[scale == 0] = 1
    Z = X / scale
    beta = np.linalg.solve(Z.T @ Z + alpha * len(Z) * np.eye(Z.shape[1]), Z.T @ y)
    return beta / scale


def walk_forward(data, horizon, first_year):
    out = []
    for year in range(first_year, data["date"].dt.year.max() + 1):
        cutoff = pd.Timestamp(year=year, month=1, day=1) - pd.offsets.BDay(horizon + 1)  # targets end before Jan 1
        train, test = data[data["date"] < cutoff], data[data["date"].dt.year == year]
        if len(train) < 2000 or test.empty:
            continue
        beta = fit(train[FEATURES].to_numpy(), train["target"].to_numpy())
        out.append(test.assign(pred=test[FEATURES].to_numpy() @ beta))
    return pd.concat(out) if out else pd.DataFrame()


def sharpe(period_returns, horizon):
    sd = period_returns.std()
    return period_returns.mean() / sd * np.sqrt(252 / horizon) if sd > 0 else np.nan


def evaluate(pred, horizon):
    """Hit rate and annualised Sharpe of equal-risk portfolios rebalanced every `horizon` trading days."""
    dates = np.sort(pred["date"].unique())[::horizon]
    sample = pred[pred["date"].isin(dates)]
    trend = sample[[f"trend_{lb}d" for lb in LOOKBACKS]].mean(axis=1)
    signals = {"model": sample["pred"], "trend alone": trend, "carry alone": sample["carry"]}
    recent = sample["date"] >= sample["date"].max() - pd.DateOffset(years=5)
    rows = {}
    for name, signal in signals.items():
        side = np.sign(signal)
        period = (side * sample["target"]).groupby(sample["date"]).mean()
        hit = (side * sample["target"] > 0)[side != 0].mean()
        last = (side * sample["target"])[recent].groupby(sample["date"][recent]).mean()
        rows[name] = {"hit_rate": hit, "sharpe": sharpe(period, horizon), "sharpe_last_5y": sharpe(last, horizon),
                      "periods": len(period)}
    return pd.DataFrame(rows).T


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=HORIZON, help="trading days ahead")
    ap.add_argument("--first-test-year", type=int, default=2012)
    args = ap.parse_args()
    data = training_panel(load_series(), args.horizon)
    pred = walk_forward(data, args.horizon, args.first_test_year)
    if pred.empty:
        raise SystemExit("Not enough history to evaluate the model")
    table = evaluate(pred, args.horizon)
    beta = fit(data[FEATURES].to_numpy(), data["target"].to_numpy())
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model = {"features": FEATURES, "coefficients": dict(zip(FEATURES, beta.round(6).tolist())),
             "horizon": args.horizon, "trained_at": datetime.now().isoformat(timespec="seconds"),
             "trained_through": str(data["date"].max().date()), "markets": int(data["root"].nunique()),
             "out_of_sample": json.loads(table.to_json(orient="index"))}
    (MODEL_DIR / "model.json").write_text(json.dumps(model, indent=2), encoding="utf-8")
    print(f"Out-of-sample {pred['date'].dt.year.min()}-{pred['date'].dt.year.max()}, "
          f"{args.horizon}-day holds, before costs:\n")
    print(table.astype(float).round(3).to_string())
    print("\nCoefficients (all years):", {k: round(v, 3) for k, v in model["coefficients"].items()})
    print(f"Saved {MODEL_DIR / 'model.json'}")


if __name__ == "__main__":
    main()
