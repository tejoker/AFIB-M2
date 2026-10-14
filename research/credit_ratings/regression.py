"""Cross-sectional credit rating regression: today's agency rating explained by the issuer's fundamentals.

Offline: reads the reference snapshot and annual fundamentals that bloomberg/bbg_collect.py saved; no Bloomberg login.

Sample: non-financial issuers rated by S&P, Moody's or Fitch, with a fiscal year ending after mid-2024 and every
regressor available. The dependent variable is the average of the agencies' notches on the S&P scale (1 = AAA,
10 = BBB-, the last investment-grade notch, 11 = BB+, 22 = D). Bloomberg's API gives today's rating only, so this
is a cross-section, not a panel: it says which fundamentals go with a better rating, not what makes ratings move.

Regressors, from the latest fiscal year (ratios capped at their 1st and 99th percentiles, then standardised, so each
coefficient is the effect of one standard deviation):
  size          log sales in USD (converted at today's exchange rate)
  leverage      total debt / EBITDA (capped at 0-10x, 10x when EBITDA is negative), total debt / total assets
  cash flow     cash from operations / total debt
  coverage      log(1 + EBITDA / interest expense), coverage capped at 0-50x
  profitability EBITDA margin
  market        (second specification) 1-year equity volatility and log market cap in USD
plus sector (GICS) and region (US, Europe, other) fixed effects.

Models: OLS on the notch (heteroskedasticity-robust HC3 errors), ordered logit on the notch grouped into 13
classes (AA- or better and B- or worse pooled), and a logit for high yield against investment grade. Fit is
measured out of sample by 5-fold cross-validation. Fitted ratings shrink towards the average (a BB name comes out
about BBB- on average), so names are compared with peers at the same rating: an IG name whose implied notch is a
1.5 notches or more weaker than its peers' is a fallen-angel risk, a HY name 1.5 or more stronger a rising-star
candidate. A rating also reflects business risk, ownership and country that this does not see.

The collector works by tier (tier 2: S&P 500, STOXX 600 and Bloomberg Europe 500; tier 3: Russell 3000, Nikkei,
TSX, ASX, Hang Seng); --tiers chooses which ones enter. While tier 3 is incomplete, the default is tier 2 only.

Output: <data>/credit_ratings/regression_<date>.xlsx (coefficients, fit, every issuer, the IG/HY frontier).

Usage (from the repository root):
    python research/credit_ratings/regression.py
    python research/credit_ratings/regression.py --tiers 2 3
"""
import argparse
import sys
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bloomberg"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bbg_collect import latest_snapshot, load_dataset
from check_regression_data import MDY_N, SP, SP_N, notch
from data_root import DATA

OUT = DATA / "credit_ratings"
LATEST_FY_FROM = "2024-06-30"
IG_LAST = 10  # BBB- / Baa3
PEER_GAP = 1.5  # notches from peers at the same rating that flag a name (about 1.5 standard deviations)
FUNDAMENTAL = ["log_sales_usd", "debt_to_ebitda", "debt_to_assets", "cfo_to_debt", "log_coverage", "ebitda_margin"]
MARKET = ["equity_vol_1y", "log_mkt_cap_usd"]
LABELS = {
    "log_sales_usd": "size: log sales (USD)", "debt_to_ebitda": "leverage: debt / EBITDA",
    "debt_to_assets": "leverage: debt / assets", "cfo_to_debt": "cash flow: CFO / debt",
    "log_coverage": "coverage: log(1 + EBITDA / interest)", "ebitda_margin": "profitability: EBITDA margin",
    "equity_vol_1y": "market: 1-year equity volatility", "log_mkt_cap_usd": "market: log market cap (USD)",
}
REGIONS = {"US": "US", **{c: "Europe" for c in ("GB", "FR", "DE", "IT", "ES", "NL", "BE", "CH", "SE", "DK", "NO", "FI",
                                               "AT", "IE", "PT", "LU", "PL", "GR", "CZ", "JE", "GG", "IM")}}


# --------------------------------------------------------------------------- data

def fx_to_usd(currencies):
    """USD per unit of each currency, today's rate from Yahoo ("GBp" is pence)."""
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    base = {c: c.upper() for c in currencies if isinstance(c, str)}
    wanted = sorted({b for b in base.values() if b != "USD"})
    rates = {"USD": 1.0}
    if wanted:
        raw = yf.download([f"{b}USD=X" for b in wanted], period="10d", interval="1d", progress=False,
                          auto_adjust=False)["Close"]
        raw = raw.to_frame() if isinstance(raw, pd.Series) else raw
        for b in wanted:
            col = f"{b}USD=X"
            if col in raw and raw[col].notna().any():
                rates[b] = float(raw[col].dropna().iloc[-1])
    return {c: rates.get(b, np.nan) / (100 if c.endswith("p") and c[:-1].isupper() else 1) for c, b in base.items()}


def cap(x, low=0.01, high=0.99):
    return x.clip(x.quantile(low), x.quantile(high))


def build_sample(tiers):
    snap = latest_snapshot()
    universe = pd.read_parquet(DATA / "universe.parquet")
    snap = snap.merge(universe[["ticker", "tier", "indices"]], on="ticker", how="left")
    snap["notch"] = pd.concat([snap["RTG_SP_LT_LC_ISSUER_CREDIT"].map(lambda r: notch(r, SP_N)),
                               snap["RTG_MOODY_LONG_TERM"].map(lambda r: notch(r, MDY_N)),
                               snap["RTG_FITCH_LT_ISSUER_DEFAULT"].map(lambda r: notch(r, SP_N))],
                              axis=1).mean(axis=1)
    rated = snap[snap["notch"].notna() & snap["INDUSTRY_SECTOR"].ne("Financial") & snap["tier"].isin(tiers)]
    ann = load_dataset("fundamentals_annual").sort_values("date")
    latest = ann[ann["ticker"].isin(rated["ticker"])].groupby("ticker").tail(1)
    latest = latest[latest["date"] >= LATEST_FY_FROM]
    df = rated.merge(latest, on="ticker", suffixes=("", "_fy"))  # snapshot fields keep their names
    fx = fx_to_usd(set(df["EQY_FUND_CRNCY"].dropna()) | set(df["CRNCY"].dropna()))
    x = pd.DataFrame(index=df.index)
    sales_usd = df["SALES_REV_TURN"] * df["EQY_FUND_CRNCY"].map(fx)
    x["log_sales_usd"] = np.log(sales_usd.where(sales_usd > 0))
    x["debt_to_ebitda"] = df["TOT_DEBT_TO_EBITDA"].where(df["EBITDA"] > 0, 10).clip(0, 10)
    x["debt_to_assets"] = df["TOT_DEBT_TO_TOT_ASSET"].clip(0, 100)
    x["cfo_to_debt"] = cap(df["CFO_TO_TOT_DEBT"])
    x["log_coverage"] = np.log1p(df["EBITDA_TO_INTEREST_EXPN"].clip(0, 50))
    x["ebitda_margin"] = cap(df["EBITDA_MARGIN"])
    x["equity_vol_1y"] = cap(df["VOLATILITY_260D"])
    mkt_cap_usd = df["CUR_MKT_CAP"] * df["CRNCY"].map(fx)  # today's, in the trading currency
    x["log_mkt_cap_usd"] = np.log(mkt_cap_usd.where(mkt_cap_usd > 0))
    keep = ["ticker", "NAME", "GICS_SECTOR_NAME", "COUNTRY_ISO", "tier", "indices", "notch",
            "RTG_SP_LT_LC_ISSUER_CREDIT", "RTG_MOODY_LONG_TERM", "RTG_FITCH_LT_ISSUER_DEFAULT", "RTG_SP_OUTLOOK",
            "RTG_MDY_OUTLOOK", "date"]
    out = pd.concat([df[keep], x], axis=1)
    out["region"] = out["COUNTRY_ISO"].map(REGIONS).fillna("Other")
    out["sector"] = out["GICS_SECTOR_NAME"].fillna("Unknown")
    out["hy"] = (out["notch"] > IG_LAST + 0.5).astype(int)
    out["notch_class"] = out["notch"].round().clip(4, 16).astype(int)  # AA- or better ... B- or worse
    return out.rename(columns={"date": "fiscal_year_end"})


def dummies(df, levels):
    """Sector and region dummies for the given levels (dropping each variable's first, the base)."""
    out = {}
    for col, values in levels.items():
        for v in values[1:]:
            out[f"{col}: {v}"] = (df[col] == v).astype(float)
    return pd.DataFrame(out, index=df.index)


def matrix(df, regressors, stats, levels, constant=True):
    z = (df[regressors].astype(float) - stats["mean"]) / stats["sd"]
    X = pd.concat([z, dummies(df, levels)], axis=1)
    X = X.loc[:, X.std() > 0] if len(X) > 1 else X
    if constant:
        X.insert(0, "const", 1.0)
    return X


# --------------------------------------------------------------------------- models

def fit_models(df, regressors, levels):
    import statsmodels.api as sm
    from statsmodels.miscmodels.ordinal_model import OrderedModel
    stats = {"mean": df[regressors].mean(), "sd": df[regressors].std()}
    X = matrix(df, regressors, stats, levels)
    ols = sm.OLS(df["notch"], X).fit(cov_type="HC3")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        logit = sm.Logit(df["hy"], X).fit(disp=0, maxiter=200)
        ordered = OrderedModel(df["notch_class"], X.drop(columns="const"), distr="logit").fit(method="bfgs", disp=0,
                                                                                          maxiter=2000)
    return {"ols": ols, "logit": logit, "ordered": ordered, "columns": X.columns, "stats": stats}


def cross_validate(df, regressors, levels, folds=5, seed=0):
    """Out-of-sample implied notch (OLS) and probability of high yield (logit) for every issuer."""
    import statsmodels.api as sm
    order = np.random.default_rng(seed).permutation(len(df))
    fold = pd.Series(np.arange(len(df)) % folds, index=df.index[order]).reindex(df.index)
    pred = pd.DataFrame(index=df.index, columns=["implied_notch", "p_hy"], dtype=float)
    for k in range(folds):
        train, test = df[fold != k], df[fold == k]
        stats = {"mean": train[regressors].mean(), "sd": train[regressors].std()}
        Xtr = matrix(train, regressors, stats, levels)
        Xte = matrix(test, regressors, stats, levels).reindex(columns=Xtr.columns, fill_value=0.0)
        pred.loc[test.index, "implied_notch"] = sm.OLS(train["notch"], Xtr).fit().predict(Xte)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pred.loc[test.index, "p_hy"] = sm.Logit(train["hy"], Xtr).fit(disp=0, maxiter=200).predict(Xte)
    return pred


def auc(y, p):
    """Probability that a random high-yield issuer gets a higher P(HY) than a random investment-grade one."""
    ranks = pd.Series(p).rank()
    pos = np.asarray(y) == 1
    n1, n0 = pos.sum(), (~pos).sum()
    return (ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else np.nan


def fit_table(df, pred, models):
    err = pred["implied_notch"] - df["notch"]
    rounded = pred["implied_notch"].round().clip(1, 22)
    return pd.Series({
        "issuers": len(df), "high yield share": df["hy"].mean(),
        "OLS R² (in sample)": models["ols"].rsquared, "OLS adjusted R²": models["ols"].rsquared_adj,
        "logit pseudo R²": models["logit"].prsquared,
        "ordered logit pseudo R²": 1 - models["ordered"].llf / models["ordered"].llnull
        if hasattr(models["ordered"], "llnull") else np.nan,
        "out of sample: mean absolute error (notches)": err.abs().mean(),
        "out of sample: exact notch": (rounded == df["notch"].round()).mean(),
        "out of sample: within 1 notch": (err.abs() <= 1).mean(),
        "out of sample: within 2 notches": (err.abs() <= 2).mean(),
        "out of sample: IG/HY correctly classified": ((pred["p_hy"] > 0.5).astype(int) == df["hy"]).mean(),
        "  (calling every issuer IG would get)": 1 - df["hy"].mean(),
        "out of sample: IG/HY AUC": auc(df["hy"], pred["p_hy"]),
    })


def coefficient_table(models, regressors):
    ols, logit, ordered = models["ols"], models["logit"], models["ordered"]
    rows = []
    for name in regressors:
        row = {"driver": LABELS.get(name, name), "variable": name,
               "OLS notches per 1 sd": ols.params[name], "OLS t (HC3)": ols.tvalues[name],
               "logit log-odds of HY per 1 sd": logit.params[name], "logit t": logit.tvalues[name]}
        if name in ordered.params:
            row.update({"ordered logit per 1 sd": ordered.params[name], "ordered logit t": ordered.tvalues[name]})
        rows.append(row)
    return pd.DataFrame(rows)


def rating_label(n):
    return SP[int(round(min(max(n, 1), len(SP)))) - 1] if np.isfinite(n) else ""


# --------------------------------------------------------------------------- report

def run(tiers):
    df = build_sample(tiers)
    results = {}
    for spec, regressors in (("fundamentals", FUNDAMENTAL), ("fundamentals + market", FUNDAMENTAL + MARKET)):
        data = df.dropna(subset=regressors + ["notch"]).copy()
        levels = {c: list(data[c].value_counts().index) for c in ("sector", "region")}
        models = fit_models(data, regressors, levels)
        pred = cross_validate(data, regressors, levels)
        results[spec] = {"data": data, "models": models, "pred": pred, "regressors": regressors,
                         "fit": fit_table(data, pred, models), "coef": coefficient_table(models, regressors)}
    return df, results


def issuer_table(res):
    d, pred = res["data"], res["pred"]
    out = d[["ticker", "NAME", "sector", "region", "tier", "RTG_SP_LT_LC_ISSUER_CREDIT", "RTG_MOODY_LONG_TERM",
             "RTG_FITCH_LT_ISSUER_DEFAULT", "RTG_SP_OUTLOOK", "RTG_MDY_OUTLOOK", "notch", "fiscal_year_end"]].copy()
    out["implied_notch"] = pred["implied_notch"]
    out["implied_rating"] = out["implied_notch"].map(rating_label)
    out["gap_notches"] = out["notch"] - out["implied_notch"]  # > 0: rated worse than its fundamentals
    # Fitted values shrink towards the average rating, so every HY name looks better than its rating and every AA
    # name worse. Against peers: the implied notch minus what the model typically implies at that actual rating.
    slope, intercept = np.polyfit(out["notch"], out["implied_notch"], 1)
    out["vs_peers_notches"] = out["implied_notch"] - (intercept + slope * out["notch"])  # < 0: stronger than peers
    out["p_hy"] = pred["p_hy"]
    out["reading"] = np.where(out["vs_peers_notches"] <= -PEER_GAP, "fundamentals stronger than its rating's peers",
                              np.where(out["vs_peers_notches"] >= PEER_GAP,
                                       "fundamentals weaker than its rating's peers", ""))
    for r in res["regressors"]:
        out[r] = d[r]
    return out.sort_values("gap_notches")


def frontier(issuers):
    """Issuers rated BBB to BB, flagged when their fundamentals sit at least PEER_GAP notches away from what the
    model typically implies at their rating: weaker for an IG name, stronger for a HY name."""
    f = issuers[issuers["notch"].between(IG_LAST - 1, IG_LAST + 2)].copy()
    f["side"] = np.where(f["notch"] <= IG_LAST + 0.5, "IG", "HY")
    f["watch"] = np.where((f["side"] == "IG") & (f["vs_peers_notches"] >= PEER_GAP), "fallen-angel risk",
                          np.where((f["side"] == "HY") & (f["vs_peers_notches"] <= -PEER_GAP),
                                   "rising-star candidate", ""))
    return f.sort_values("vs_peers_notches", ascending=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiers", type=int, nargs="+", default=[2], help="collector tiers in the sample (default 2)")
    args = ap.parse_args()
    df, results = run(args.tiers)
    pd.set_option("display.width", 200)
    print(f"Rated non-financial issuers, tiers {args.tiers}, fiscal year after {LATEST_FY_FROM}: {len(df)}\n")
    for spec, res in results.items():
        print(f"== {spec}: {len(res['data'])} issuers with every regressor")
        print(res["coef"].drop(columns="variable").round(3).to_string(index=False))
        print()
        print(res["fit"].round(3).to_string())
        print()
    main_res = results["fundamentals + market"]
    issuers = issuer_table(main_res)
    front = frontier(issuers)
    cols = ["NAME", "RTG_SP_LT_LC_ISSUER_CREDIT", "RTG_MOODY_LONG_TERM", "implied_rating", "vs_peers_notches",
            "p_hy", "watch"]
    print(f"IG/HY frontier (rated BBB to BB) whose fundamentals + market sit {PEER_GAP:g}+ notch from their rating's "
          "peers (out of sample):")
    print(front[front["watch"] != ""][cols].round(2).to_string(index=False) if (front["watch"] != "").any()
          else "  no issuer on the wrong side of its fundamentals")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"regression_{date.today():%Y%m%d}.xlsx"
    with pd.ExcelWriter(path) as xl:
        for spec, res in results.items():
            name = "market" if "market" in spec else "fundamentals"
            res["coef"].to_excel(xl, sheet_name=f"coef {name}", index=False)
            res["fit"].rename("value").to_frame().to_excel(xl, sheet_name=f"fit {name}")
        issuers.to_excel(xl, sheet_name="issuers", index=False)
        front.to_excel(xl, sheet_name="IG-HY frontier", index=False)
        pd.DataFrame({"note": [
            f"Tiers {args.tiers}; collected fundamentals cover {df['ticker'].nunique()} rated non-financial issuers.",
            "Cross-section of today's ratings: associations, not causes; ratings also weigh business risk and "
            "ownership that the regressors do not see.",
            "implied_notch and p_hy are out of sample (5-fold cross-validation); gap > 0 = rated worse than "
            "fundamentals; vs_peers_notches < 0 = fundamentals stronger than issuers with the same rating "
            "(the gap itself is biased by shrinkage towards the average rating).",
        ]}).to_excel(xl, sheet_name="notes", index=False)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
