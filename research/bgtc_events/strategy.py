"""Event strategies for the Bloomberg Global Trading Challenge: Russell 1000/2000 migration and IG/HY credit events.

Offline: reads what bloomberg/bgtc_screen.py and bloomberg/bbg_collect.py saved in the data folder; no Bloomberg login.

1. Russell reconstitution (the long signal). The December 2026 reconstitution ranks companies on Fri 30 Oct (rank
   day), publishes preliminary lists on Fri 13 Nov and takes effect after the close on Fri 11 Dec. A Russell 1000
   member that falls below the lower edge of the band around the 1000th company moves to the Russell 2000, where it
   becomes one of the largest members: funds tracking the Russell 2000 hold a much larger share of it than funds
   tracking the Russell 1000 did, so it meets net buying (Chang, Hong and Liskovich, RFS 2015). Moves the other way
   meet net selling. Here the band is rebuilt from current market caps: rank 1000 is the breakpoint, and existing
   members keep their index within +/-2.5% of cumulative market cap around it (FTSE Russell's banding rule).
2. IG/HY credit (a filter). Rating outlooks are tested on the year of saved prices: the market-adjusted return after
   the latest S&P and Moody's outlook dates. Only the latest outlook per issuer is visible, so the sample is small
   and skewed; it is evidence, not a backtest. Issuers at BBB-/Baa3 or below with a negative outlook or watch are
   flagged to avoid.

Output: <data>/bgtc/events_<date>.xlsx (Russell migrations, watch list, credit events, avoid list, proposed sleeve).

Usage (from the repository root):
    python research/bgtc_events/strategy.py
    python research/bgtc_events/strategy.py --sleeve 0.4 --max-names 5
"""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as pads

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repository root
from data_root import DATA

BGTC = DATA / "bgtc"
RANK_DAY = pd.Timestamp("2026-10-30")
CHALLENGE = (pd.Timestamp("2026-10-16"), pd.Timestamp("2026-11-13"))
BREAK_RANK = 1000
BAND = 0.025      # cumulative market-cap share either side of the breakpoint
WATCH = 0.15      # members within 15% of a band edge can still cross before rank day
MIN_PRICE = 1.0   # Russell drops members whose 30-day average price is below $1

SP = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
      "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
MDY = ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3",
       "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]
SP_N = {r: i + 1 for i, r in enumerate(SP)}
MDY_N = {r: i + 1 for i, r in enumerate(MDY)}
LAST_IG = 10  # BBB- / Baa3


def notch(rating, scale):
    if not isinstance(rating, str):
        return np.nan
    return scale.get(re.sub(r"\(P\)|\*[+-]?|\s.*$|u$", "", rating.strip()), np.nan)


def latest(pattern):
    """Most recent file or folder in the bgtc folder matching the pattern (names carry the date)."""
    found = sorted(BGTC.glob(pattern))
    if not found:
        raise SystemExit(f"Nothing matches {BGTC / pattern}: run bloomberg/bgtc_screen.py first")
    return found[-1]


def load():
    uni = pd.read_parquet(latest("universe_*R1000*R2000*.parquet"))
    ref = pd.read_parquet(latest("reference_*R1000*R2000*.parquet")).merge(uni, on="ticker", how="left")
    px = pads.dataset(latest("prices_*R1000*R2000*")).to_table().to_pandas()
    px["date"] = pd.to_datetime(px["date"])
    px = px.pivot_table(index="date", columns="ticker", values="PX_LAST").sort_index()
    credit = pads.dataset(DATA / "reference").to_table().to_pandas()
    credit = credit.sort_values("snapshot_date").groupby("ticker").tail(1).set_index("ticker")
    return ref, px, credit


# --------------------------------------------------------------------------- Russell migration

def russell_bands(caps, break_rank=BREAK_RANK, band=BAND):
    """Breakpoint and band edges from a Series of market caps (Russell 1000 and 2000 members)."""
    s = caps.dropna().sort_values(ascending=False)
    cum = s.cumsum() / s.sum()
    at = cum.iloc[break_rank - 1]
    upper = s.iloc[int(np.argmin(np.abs(cum.values - (at - band))))]
    lower = s.iloc[int(np.argmin(np.abs(cum.values - (at + band))))]
    return s.iloc[break_rank - 1], lower, upper


def russell_migration(ref, px):
    rus = ref[ref["index"].astype(str).str.contains("R1000|R2000")].copy()
    rus["current"] = np.where(rus["index"].str.contains("R1000"), "R1000", "R2000")
    breakpoint, lower, upper = russell_bands(rus.set_index("ticker")["CUR_MKT_CAP"])
    cap = rus["CUR_MKT_CAP"]
    r1, r2 = rus["current"].eq("R1000"), rus["current"].eq("R2000")
    rus["predicted"] = np.select([r1 & (cap < lower), r2 & (cap > upper)], ["R2000", "R1000"], rus["current"])
    # price move needed by rank day to change the prediction (negative = must fall)
    rus["move_to_flip"] = np.where(r1, lower / cap - 1, upper / cap - 1)
    rus["watch"] = rus["predicted"].eq(rus["current"]) & (rus["move_to_flip"].abs() < WATCH)
    rus["rank"] = cap.rank(ascending=False, method="first").astype(int)
    avg30 = px.iloc[-21:].mean()  # about 30 calendar days
    rus["avg_price_30d"] = rus["ticker"].map(avg30)
    rus["price_risk"] = rus["avg_price_30d"] < MIN_PRICE * 1.2  # near the $1 floor: could be deleted outright
    adv_usd = rus["VOLUME_AVG_20D"] * rus["PX_LAST"]
    rus["adv_usd_m"] = adv_usd / 1e6
    rus["days_volume_per_1pct_cap"] = 0.01 * cap / adv_usd  # higher = index demand moves the price more
    return rus, dict(breakpoint=breakpoint, lower=lower, upper=upper)


# --------------------------------------------------------------------------- price signals

def price_signals(px, tickers):
    p = px.reindex(columns=tickers).ffill()
    ret = px.reindex(columns=tickers).pct_change(fill_method=None)
    out = pd.DataFrame(index=tickers)
    out["ret_1m"] = p.iloc[-1] / p.iloc[-22] - 1
    out["ret_3m"] = p.iloc[-1] / p.iloc[-64] - 1
    out["mom_12_1"] = p.iloc[-22] / p.iloc[-min(len(p), 253)] - 1
    out["vol_60d"] = ret.iloc[-60:].std() * np.sqrt(252)
    return out


# --------------------------------------------------------------------------- credit

def abnormal_returns(px, region):
    """Daily return minus the equal-weighted return of the stock's region (US or Europe)."""
    ret = px.pct_change(fill_method=None).clip(-0.5, 1.0)
    ab = ret.copy()
    for g in region.unique():
        cols = region.index[region.eq(g)].intersection(ret.columns)
        ab[cols] = ret[cols].sub(ret[cols].mean(axis=1), axis=0)
    return ab


def car(ab, ticker, when, start, end):
    """Cumulative abnormal return from `start` to `end` trading days after the first session on/after `when`."""
    i = ab.index.searchsorted(when)
    if ticker not in ab or i + start < 0 or i + end >= len(ab):
        return np.nan
    s = ab[ticker].iloc[i + start:i + end + 1]
    return s.sum() if s.notna().mean() >= 0.8 else np.nan


def outlook_events(credit, ab):
    rows = []
    for agency, outlook, dated in [("S&P", "RTG_SP_OUTLOOK", "RTG_SP_OUTLOOK_DT"),
                                   ("Moody's", "RTG_MDY_OUTLOOK", "RTG_MDY_OUTLOOK_DT")]:
        when = pd.to_datetime(credit[dated], errors="coerce")
        text = credit[outlook].astype(str).str.upper()
        for t in credit.index[when.notna() & (when > ab.index[0] + pd.Timedelta(days=15))]:
            rows.append({"agency": agency, "ticker": t, "date": when[t],
                         "outlook": "POS" if "POS" in text[t] else "NEG" if "NEG" in text[t] else "STABLE",
                         "car_pre_-10_-2": car(ab, t, when[t], -10, -2), "car_event_-1_+1": car(ab, t, when[t], -1, 1),
                         "car_post_+2_+21": car(ab, t, when[t], 2, 21)})
    return pd.DataFrame(rows)


def summarize_events(ev):
    def stats(s):
        s = s.dropna()
        t = s.mean() / s.std() * np.sqrt(len(s)) if len(s) > 2 else np.nan
        return pd.Series({"events": len(s), "mean_pct": 100 * s.mean(), "t_stat": t, "positive_pct": 100 * (s > 0).mean()})
    windows = [c for c in ev if c.startswith("car_")]
    return pd.concat({w: ev.groupby(["agency", "outlook"])[w].apply(stats).unstack() for w in windows}, axis=1).round(2)


def credit_flags(credit):
    out = pd.DataFrame(index=credit.index)
    out["sp_notch"] = credit["RTG_SP_LT_LC_ISSUER_CREDIT"].map(lambda r: notch(r, SP_N))
    out["mdy_notch"] = credit["RTG_MOODY_LONG_TERM"].map(lambda r: notch(r, MDY_N))
    out["fitch_notch"] = credit["RTG_FITCH_LT_ISSUER_DEFAULT"].map(lambda r: notch(r, SP_N))
    n = out[["sp_notch", "mdy_notch", "fitch_notch"]]
    out["worst_notch"] = n.max(axis=1)
    out["bucket"] = np.select([n.isna().all(axis=1), (n <= LAST_IG).any(axis=1) & (n > LAST_IG).any(axis=1),
                               out["worst_notch"] <= LAST_IG], ["unrated", "split IG/HY", "IG"], "HY")
    negative = pd.Series(False, index=credit.index)
    for col in ["RTG_SP_OUTLOOK", "RTG_MDY_OUTLOOK", "RTG_FITCH_OUTLOOK",
                "RTG_SP_LT_LC_ISSUER_CREDIT", "RTG_MOODY_LONG_TERM"]:
        if col in credit:
            negative |= credit[col].astype(str).str.upper().str.contains(r"NEG|\*-", regex=True)
    out["negative"] = negative
    out["avoid_credit"] = negative & (out["worst_notch"] >= LAST_IG)  # fallen-angel / downgrade zone
    out["drsk_1y_pd_pct"] = 100 * credit.get("BB_1YR_DEFAULT_PROB")
    for c in ["RTG_SP_LT_LC_ISSUER_CREDIT", "RTG_SP_OUTLOOK", "RTG_MOODY_LONG_TERM", "RTG_MDY_OUTLOOK",
              "RTG_FITCH_LT_ISSUER_DEFAULT"]:
        out[c] = credit.get(c)
    return out


# --------------------------------------------------------------------------- book

def sleeve(rus, flags, signals, weight, max_names, credit_veto=False):
    """Equal-weighted long sleeve of predicted Russell 1000 -> 2000 movers, skipping price-floor risks.

    Most of these names fell into the credit-avoid zone on the way down; with credit_veto they are skipped too.
    """
    c = rus[rus["current"].eq("R1000") & rus["predicted"].eq("R2000")].set_index("ticker")
    c = c.join(flags[["bucket", "avoid_credit", "RTG_MOODY_LONG_TERM", "RTG_FITCH_LT_ISSUER_DEFAULT"]], how="left")
    c = c.join(signals, how="left")
    reason = pd.Series("", index=c.index)
    reason[c["price_risk"]] += "price near $1 (outright deletion risk); "
    c["avoid_credit"] = c["avoid_credit"].fillna(False).astype(bool)
    if credit_veto:
        reason[c["avoid_credit"]] += "negative outlook at BBB-/Baa3 or below; "
    c["excluded_because"] = reason.str.rstrip("; ")
    # deepest below the band first: least likely to climb back above it by rank day
    picks = c[c["excluded_because"].eq("")].sort_values("move_to_flip", ascending=False).head(max_names)
    c["weight"] = 0.0
    if len(picks):
        c.loc[picks.index, "weight"] = min(weight / len(picks), 0.20)
    reports = pd.to_datetime(c["EXPECTED_REPORT_DT"], errors="coerce")
    c["reports_in_challenge"] = reports.between(*CHALLENGE)
    return c.sort_values(["weight", "move_to_flip"], ascending=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sleeve", type=float, default=0.4, help="share of the book in the Russell sleeve (default 0.4)")
    ap.add_argument("--max-names", type=int, default=5)
    ap.add_argument("--credit-veto", action="store_true",
                    help="also skip movers with a negative outlook at BBB-/Baa3 or below (flagged either way)")
    args = ap.parse_args()

    ref, px, credit = load()
    asof = px.index[-1].date()
    rus, bands = russell_migration(ref, px)
    signals = price_signals(px, rus["ticker"].tolist())
    flags = credit_flags(credit)
    region = ref.set_index("ticker")["index"].map(lambda s: "EU" if s == "STOXX600" else "US")
    events = outlook_events(credit[credit.index.isin(px.columns)], abnormal_returns(px, region))
    event_table = summarize_events(events)
    book = sleeve(rus, flags, signals, args.sleeve, args.max_names, args.credit_veto)

    cols = ["NAME", "current", "predicted", "rank", "CUR_MKT_CAP", "move_to_flip", "avg_price_30d", "adv_usd_m",
            "days_volume_per_1pct_cap", "GICS_SECTOR_NAME", "EXPECTED_REPORT_DT"]
    moving = rus[rus["current"] != rus["predicted"]].set_index("ticker").join(signals)
    watch = rus[rus["watch"]].set_index("ticker").join(signals).sort_values("move_to_flip", key=abs)
    avoid = flags[flags["avoid_credit"] & flags.index.isin(ref["ticker"])].join(ref.set_index("ticker")[["NAME", "index"]])

    path = BGTC / f"events_{asof}.xlsx"
    with pd.ExcelWriter(path) as xl:
        book.to_excel(xl, sheet_name="Russell sleeve")
        moving[cols + list(signals)].sort_values("predicted").to_excel(xl, sheet_name="Predicted migrations")
        watch[cols + list(signals)].to_excel(xl, sheet_name="Watch until rank day")
        event_table.to_excel(xl, sheet_name="Credit outlook evidence")
        events.to_excel(xl, sheet_name="Credit outlook events", index=False)
        avoid.to_excel(xl, sheet_name="Credit avoid")
        pd.Series({"prices as of": str(asof), "breakpoint (rank 1000) $bn": bands["breakpoint"] / 1e9,
                   "band lower $bn": bands["lower"] / 1e9, "band upper $bn": bands["upper"] / 1e9,
                   "rank day": str(RANK_DAY.date()), "challenge": f"{CHALLENGE[0].date()} to {CHALLENGE[1].date()}"},
                  name="value").to_excel(xl, sheet_name="Assumptions")

    print(f"Prices to {asof}. Russell breakpoint ${bands['breakpoint'] / 1e9:.2f}bn, band "
          f"${bands['lower'] / 1e9:.2f}bn to ${bands['upper'] / 1e9:.2f}bn (June 2026 published: $5.7bn, $2.7-9.6bn)")
    show = ["NAME", "CUR_MKT_CAP", "move_to_flip", "ret_3m", "vol_60d", "avoid_credit", "weight", "excluded_because"]
    with pd.option_context("display.width", 220, "display.max_colwidth", 45):
        print("\nRussell 1000 -> 2000 (buy candidates):")
        print(book[show].assign(CUR_MKT_CAP=book["CUR_MKT_CAP"] / 1e9).round(3).to_string())
        print("\nRussell 2000 -> 1000 (avoid): " + ", ".join(moving[moving["predicted"].eq("R1000")]["NAME"]))
        print(f"Watch until rank day ({len(watch)}): " + ", ".join(watch["NAME"].head(15)))
        print("\nReturns after the latest outlook date, market-adjusted (%):")
        print(event_table.xs("mean_pct", axis=1, level=1).join(
            event_table["car_post_+2_+21"][["events", "t_stat"]].add_prefix("post_")).to_string())
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
