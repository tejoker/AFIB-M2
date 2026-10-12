"""Latest prices and signals for every market from Yahoo, for trading by hand (no Bloomberg needed).

For each market it downloads two years of daily prices for the contracts listed over the next 15 months, picks
the contract to trade (most volume over the last 5 days, not within 5 business days of expiry) and the next one,
and computes the same inputs the model was trained on: trend over 1, 3, 6 and 12 months on the traded
contract's own prices (no roll inside), carry against the next contract, and volatility. Seasonality comes from
the Bloomberg series when they have been built; the model view needs <data>/futures/model/model.json.

Yahoo's futures quotes are delayed (about 10 minutes); CQG shows the live price.

Usage (from the repository root):
    python cme_challenge/live.py --risk 5000
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_markets import MARKETS
from futures_research import LOOKBACKS, VOL_DAYS
from model import FEATURES, features, seasonal_now

LIVE = DATA / "futures" / "live"
SERIES = DATA / "futures" / "continuous"
MODEL = DATA / "futures" / "model" / "model.json"
MONTH_CODES = "FGHJKMNQUVXZ"
MIN_DAYS_TO_EXPIRY = 5
SIZING_DAYS, SIZING_CAP = 21, 2.0  # volatility targeting as benchmarked: 21-day volatility, at most 2x normal size


def listed_contracts(market, today, months_ahead=15):
    """(Yahoo ticker, delivery month) for the market's liquid months from this month on."""
    out = []
    for k in range(months_ahead + 1):
        year, month = today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1
        code = MONTH_CODES[month - 1]
        if code in market.months:
            out.append((f"{market.yahoo}{code}{year % 100:02d}.{market.exchange}",
                        pd.Timestamp(year=year, month=month, day=1)))
    return out


def download(tickers):
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # contracts not listed yet are expected
    frames = {}
    for i in range(0, len(tickers), 60):
        batch = tickers[i:i + 60]
        raw = yf.download(batch, period="2y", interval="1d", group_by="ticker", auto_adjust=False,
                          progress=False, threads=True)
        if raw.empty:
            continue
        for t in batch:
            if isinstance(raw.columns, pd.MultiIndex) and t in raw.columns.get_level_values(0):
                df = raw[t].dropna(subset=["Close"])
                if not df.empty:
                    frames[t] = df
    return frames


def expiries(tickers):
    """Last trade dates from Yahoo, cached for good (they never change)."""
    import yfinance as yf
    path = LIVE / "expiries.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    for t in tickers:
        if t in cache:
            continue
        try:
            iso = yf.Ticker(t).info.get("expireIsoDate")
        except Exception:  # Yahoo hiccup: treat as unknown, ask again next time
            iso = None
        if iso:
            cache[t] = iso[:10]
    LIVE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return cache


def choose(contracts, frames, expiry, today):
    """The contract to trade and the next one, from contracts with fresh prices."""
    fresh = [(t, d) for t, d in contracts if t in frames]
    if not fresh:
        return None, None
    latest = max(frames[t].index[-1] for t, _ in fresh)
    usable = [(t, d) for t, d in fresh if frames[t].index[-1] >= latest - pd.Timedelta(days=5)
              and not (t in expiry and np.busday_count(today, date.fromisoformat(expiry[t])) <= MIN_DAYS_TO_EXPIRY)]
    if not usable:
        return None, None
    trade = max(usable, key=lambda c: frames[c[0]]["Volume"].iloc[-5:].mean())
    later = [c for c in usable if c[1] > trade[1]]
    return trade, (min(later, key=lambda c: c[1]) if later else None)


def market_row(root, market, contracts, frames, expiry, today, model, risk):
    trade, nxt = choose(contracts, frames, expiry, today)
    if trade is None:
        return None
    close = frames[trade[0]]["Close"]
    f = features(close, pd.Series(dtype=float)).iloc[-1]
    if nxt is not None:  # long lookbacks the traded contract is too young for: use the next one's own history
        older = features(frames[nxt[0]]["Close"], pd.Series(dtype=float)).iloc[-1]
        for lb in LOOKBACKS:
            if np.isnan(f[f"trend_{lb}d"]):
                f[f"trend_{lb}d"] = older[f"trend_{lb}d"]
    carry = np.nan
    if nxt is not None:
        both = pd.concat([close, frames[nxt[0]]["Close"]], axis=1, sort=True).dropna()
        if len(both):
            years = (nxt[1] - trade[1]).days / 365.25
            carry = float(np.log(both.iloc[-1, 0] / both.iloc[-1, 1]) / years)
    seasonal = np.nan
    series_path = SERIES / f"{root.strip()}.parquet"
    if series_path.exists():
        s = pd.read_parquet(series_path)
        seasonal = seasonal_now(s["index"], s["carry"], today)
    inputs = {**{k: f[k] for k in f.index if k.startswith("trend_")}, "carry": carry, "seasonal": seasonal}
    view = np.nan
    if model:
        view = sum(model["coefficients"][k] * (0.0 if np.isnan(inputs[k]) else
                                               (np.clip(inputs[k], -1, 1) if k == "carry" else inputs[k]))
                   for k in FEATURES)
    trends = [inputs[f"trend_{lb}d"] for lb in LOOKBACKS if np.isfinite(inputs[f"trend_{lb}d"])]
    trend = float(np.mean(trends)) if trends else np.nan
    votes = int(sum(np.sign(v) for v in (trend, carry, seasonal, view) if np.isfinite(v)))
    size_x = conviction(trend, view)
    last, previous = close.iloc[-1], close.iloc[-2] if len(close) > 1 else np.nan
    vol = sizing_vol(close.pct_change())
    notional = last * market.point_value
    usd_vol = notional * vol
    exp = expiry.get(trade[0])
    return {
        "root": root.strip(), "name": market.name, "sector": market.sector, "contract": trade[0].split(".")[0],
        "next": nxt[0].split(".")[0] if nxt else None, "as_of": close.index[-1].date(), "last": last,
        "change_pct": 100 * (last / previous - 1), **{k: inputs[k] for k in inputs if k.startswith("trend_")},
        "trend": trend, "carry_pct_yr": 100 * carry, "seasonal": seasonal, "model_view": view, "votes": votes,
        "vol_pct_yr": 100 * vol * np.sqrt(252), "notional_usd": notional, "daily_vol_usd": usd_vol,
        "contracts_for_risk": risk / usd_vol if usd_vol > 0 else np.nan, "size_x": size_x,
        "micro": market.micro, "micros_for_risk": (risk / (usd_vol * market.micro_point_value / market.point_value)
                                                  if market.micro and usd_vol > 0 else np.nan),
        "expiry": exp, "volume_5d": frames[trade[0]]["Volume"].iloc[-5:].mean(),
        "note": note(len(close), exp, today),
    }


def conviction(trend, view):
    """Size multiplier on the equal-risk base: 0.5 when signals are mixed, 1 when trend and the model agree,
    2 when all four trend lookbacks and the model agree (without a model: 1 when the four lookbacks agree).
    In the backtest, full trend agreement won 51.6% of 15-day periods against 48.8% for partial agreement;
    the model's own strength did not help, so it only confirms the direction here."""
    if not np.isfinite(trend) or trend == 0:
        return 0.5
    if not np.isfinite(view):
        return 1.0 if abs(trend) == 1 else 0.5
    if np.sign(view) != np.sign(trend):
        return 0.5
    return 2.0 if abs(trend) == 1 else 1.0


def sizing_vol(ret, lookback=SIZING_DAYS, cap=SIZING_CAP):
    """Daily volatility used for sizing: the last `lookback` days, but never below 1/cap of the 1-year figure,
    so a calm spell cannot push a position above cap times its normal size (futures_research.py sizing)."""
    recent = ret.iloc[-lookback:].std()
    year = ret.iloc[-252:].std() if ret.count() >= 126 else np.nan
    return max(recent, year / cap) if np.isfinite(year) else recent


def note(days_of_history, expiry, today):
    if days_of_history <= VOL_DAYS:
        return f"Yahoo has only {days_of_history} day{'s' if days_of_history != 1 else ''} of history"
    if expiry:
        left = int(np.busday_count(today, date.fromisoformat(expiry)))
        if left <= 10:
            return f"expires in {left} trading days; the dashboard moves to the next contract 5 days before"
    return ""


def refresh(risk, today=None):
    today = today or date.today()
    listed = {root: listed_contracts(m, today) for root, m in MARKETS.items()}
    frames = download([t for contracts in listed.values() for t, _ in contracts])
    # expiry only matters for the nearest contracts, which could be about to stop trading
    near = [t for contracts in listed.values() for t, _ in [c for c in contracts if c[0] in frames][:2]]
    expiry = expiries(near)
    model = json.loads(MODEL.read_text()) if MODEL.exists() else None
    rows = [market_row(root, MARKETS[root], listed[root], frames, expiry, today, model, risk) for root in MARKETS]
    table = pd.DataFrame([r for r in rows if r])
    LIVE.mkdir(parents=True, exist_ok=True)
    table.to_csv(LIVE / "latest.csv", index=False)
    return table, model


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--risk", type=float, default=5000, help="daily volatility in USD per position, for sizing")
    args = ap.parse_args()
    table, model = refresh(args.risk)
    pd.set_option("display.width", 250)
    cols = ["root", "contract", "last", "change_pct", "trend", "carry_pct_yr", "seasonal", "model_view", "votes",
            "contracts_for_risk", "micros_for_risk"]
    print(table[cols].round(2).to_string(index=False))
    if model is None:
        print("\nNo model yet: run cme_challenge/model.py once the Bloomberg series exist.")
    print(f"\nSaved {LIVE / 'latest.csv'}")


if __name__ == "__main__":
    main()
