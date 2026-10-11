"""Chain individual futures contracts into roll-correct daily return series.

Input: contract details and daily histories from bloomberg/futures_contracts.py (<data>/futures/) or
cme_challenge/databento_contracts.py (<data>/futures/databento/); by default Databento is used for the markets
it has and Bloomberg for the others.

The series holds the nearest liquid contract and switches to the next one on the day the next contract's volume
took over when that is known (Databento), else --roll-days business days before the earlier of its first notice
day and last trade date, i.e. before delivery can be assigned and liquidity leaves.
Every daily return is the held contract's own price change, so the price gap between two contracts, which
Yahoo's continuous series book as a return, never appears. Carry is the annualised log price gap to the next
contract: positive in backwardation, where holding the front contract and rolling it earns the gap.

Output: <data>/futures/continuous/<root>.parquet (date, contract, price, ret, index, next_contract, next_price,
carry) and coverage.csv, one row per root.

Usage (from the repository root, no Bloomberg needed):
    python cme_challenge/continuous.py
    python cme_challenge/continuous.py --roots LC --roll-days 5
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

FUT = DATA / "futures"
OUT = FUT / "continuous"
SOURCES = {"bloomberg": (FUT / "contracts.parquet", FUT / "history"),
           "databento": (FUT / "databento" / "contracts.parquet", FUT / "databento" / "history")}


def roll_dates(contracts, roll_days):
    """Business day on whose close the series moves out of each contract."""
    last = contracts["LAST_TRADEABLE_DT"]
    if "FUT_NOTICE_FIRST" in contracts:
        last = pd.concat([last, contracts["FUT_NOTICE_FIRST"]], axis=1).min(axis=1)
    days = np.busday_offset(last.to_numpy().astype("datetime64[D]"), -roll_days, roll="backward")
    rule = pd.Series(pd.to_datetime(days), index=contracts.index)
    if "ROLL_DT" in contracts:  # the day traders actually moved to the next contract, where known
        return pd.to_datetime(contracts["ROLL_DT"]).fillna(rule)
    return rule


def build(contracts, prices, roll_days=5):
    """One root: contracts (ticker, LAST_TRADEABLE_DT, FUT_NOTICE_FIRST, delivery) and prices (date x ticker)."""
    c = contracts[contracts["ticker"].isin(prices.columns)].copy()
    c["roll"] = roll_dates(c, roll_days)
    c = c.sort_values(["roll", "delivery"]).reset_index(drop=True)
    px = prices[c["ticker"]].sort_index()
    px = px.apply(lambda s: s.ffill().where(s.bfill().notna()))  # fill gaps only inside each contract's life
    dates, arr, n = px.index, px.to_numpy(dtype=float), len(c)
    rows = np.arange(len(dates))

    held = np.searchsorted(c["roll"].to_numpy(), dates.to_numpy(), side="right")  # held at each close
    padded = np.column_stack([arr, np.full(len(dates), np.nan)])  # column n = "no contract"
    price = padded[rows, np.minimum(held, n)]
    prev_held = np.r_[n, held[:-1]]
    ret = padded[rows, np.minimum(prev_held, n)] / np.r_[np.nan, price[:-1]] - 1  # same contract on both days
    nxt = np.minimum(held + 1, n)
    next_price = padded[rows, nxt]

    tickers = np.r_[c["ticker"].to_numpy(), [None]]
    delivery = np.r_[c["delivery"].to_numpy(dtype="datetime64[ns]"), [np.datetime64("NaT", "ns")]]
    years = (delivery[nxt] - delivery[np.minimum(held, n)]) / np.timedelta64(1, "D") / 365.25
    with np.errstate(divide="ignore", invalid="ignore"):
        carry = np.log(price / next_price) / years

    out = pd.DataFrame({"contract": tickers[np.minimum(held, n)], "price": price, "ret": ret,
                        "next_contract": tickers[nxt], "next_price": next_price, "carry": carry}, index=dates)
    out.index.name = "date"
    out = out.loc[out["price"].first_valid_index():] if out["price"].notna().any() else out.iloc[:0]
    out["index"] = (1 + out["ret"].fillna(0)).cumprod()
    return out


def load_root(root_dir):
    frames = [pd.read_parquet(p, columns=["ticker", "date", "PX_LAST"]) for p in sorted(root_dir.glob("*.parquet"))]
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True).dropna(subset=["PX_LAST"])
    return data.pivot_table(index="date", columns="ticker", values="PX_LAST", aggfunc="last").sort_index()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", nargs="+", help="roots to build (default: every collected root)")
    ap.add_argument("--roll-days", type=int, default=5, help="business days before first notice / last trade")
    ap.add_argument("--source", choices=["auto", "bloomberg", "databento"], default="auto",
                    help="auto: Databento for the markets it has, Bloomberg for the rest")
    args = ap.parse_args()
    tables = {name: pd.read_parquet(path) for name, (path, _) in SOURCES.items()
              if path.exists() and args.source in ("auto", name)}
    if not tables:
        ap.error("No contract details yet; run cme_challenge/databento_contracts.py or bloomberg/futures_contracts.py")
    chosen = {}  # root -> source; Databento comes last so it wins
    for name in ("bloomberg", "databento"):
        if name in tables:
            chosen.update({root: name for root in tables[name]["root"].unique()})
    wanted = {r.strip().upper() for r in args.roots} if args.roots else None
    OUT.mkdir(parents=True, exist_ok=True)
    coverage = []
    for root, source in sorted(chosen.items()):
        name = root.strip()
        if wanted and name not in wanted:
            continue
        rc = tables[source][tables[source]["root"] == root]
        prices = load_root(SOURCES[source][1] / name)
        if prices.empty:
            continue
        series = build(rc, prices, args.roll_days)
        if series.empty:
            continue
        series.to_parquet(OUT / f"{name}.parquet")
        coverage.append({
            "root": name, "name": rc["name"].iloc[0], "sector": rc["sector"].iloc[0], "source": source,
            "first": series.index[0].date(), "last": series.index[-1].date(), "days": len(series),
            "missing_return_days": int(series["ret"].isna().sum()) - 1,
            "contracts_used": series["contract"].nunique(), "contracts_with_data": prices.shape[1],
        })
    table = pd.DataFrame(coverage)
    if table.empty:
        ap.error("No contract histories found; run bloomberg/futures_contracts.py first")
    table.to_csv(OUT / "coverage.csv", index=False)
    print(table.to_string(index=False))
    print(f"\nSaved {len(table)} roll-correct series to {OUT}")


if __name__ == "__main__":
    main()
