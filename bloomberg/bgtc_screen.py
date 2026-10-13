"""Stock screen for the Bloomberg Global Trading Challenge (long only, single stocks, at most 20% per name).

Universe: S&P 500, Russell 1000, Russell 2000 and STOXX 600 members. From one year of daily prices it computes
the signals with the best published evidence over a month: 12-month momentum excluding the last month, the
return around the latest earnings announcement (stocks that jump on results tend to keep drifting for weeks),
distance to the 52-week high and volatility. Reference data adds the next report date, analyst target and
rating, and S&P/Moody's ratings with outlook, which feed a credit-danger list (negative outlook at BBB-/Baa3 or
below, or a crossover issuer on negative watch) to avoid.

Output: <data>/bgtc/screen_<date>.xlsx with the ranked candidates, recent strong reporters, names reporting
during the challenge, the credit-danger list and everything screened. It saves after each request and resumes
after Ctrl+C or a Bloomberg limit (exit code 3). Reference fields the Terminal does not know are skipped and
listed, not fatal.

Usage (from the repository root, Bloomberg Terminal logged in):
    python bloomberg/bgtc_screen.py                    # whole universe
    python bloomberg/bgtc_screen.py --indices SPX      # S&P 500 only (quick test)
    python bloomberg/bgtc_screen.py --max-requests 20  # stop after 20 requests (resume later)
"""
import argparse
import logging
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

from bbg_collect import DATA, LOGS, Bloomberg, BudgetExhausted, LimitReached, chunks, write_parquet

INDICES = {"SPX": "SPX Index", "R1000": "RIY Index", "R2000": "RTY Index", "STOXX600": "SXXP Index"}
REFERENCE_FIELDS = [
    "NAME", "GICS_SECTOR_NAME", "COUNTRY_ISO", "CRNCY", "CUR_MKT_CAP", "PX_LAST", "VOLUME_AVG_20D",
    "LATEST_ANNOUNCEMENT_DT", "EXPECTED_REPORT_DT", "BEST_TARGET_PRICE", "BEST_ANALYST_RATING",
    "RTG_SP_LT_LC_ISSUER_CREDIT", "RTG_SP_OUTLOOK", "RTG_MDY_LONG_TERM", "RTG_MDY_ISSUER",
    "RTG_MDY_SEN_UNSECURED_DEBT", "RTG_MDY_OUTLOOK",
]
MOODYS = ["RTG_MDY_LONG_TERM", "RTG_MDY_ISSUER", "RTG_MDY_SEN_UNSECURED_DEBT"]  # first one the Terminal knows
OUT = DATA / "bgtc"
CHALLENGE = (date(2026, 10, 16), date(2026, 11, 13))
LOW_IG_OR_WORSE = ["BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
MDY_LOW = ["Baa3", "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]

log = logging.getLogger("bgtc")


def universe(bbg, indices, today):
    path = OUT / f"universe_{today}.parquet"  # `today` carries the index selection too (see main)
    if path.exists():
        return pd.read_parquet(path)
    rows = []
    for name in indices:
        for t in bbg.index_members(INDICES[name]):
            rows.append({"ticker": t, "index": name})
    uni = pd.DataFrame(rows).groupby("ticker")["index"].agg(lambda s: ",".join(sorted(set(s)))).reset_index()
    write_parquet(path, uni)
    log.info("Universe: %d stocks from %s", len(uni), ", ".join(indices))
    return uni


def reference(bbg, tickers, today):
    path = OUT / f"reference_{today}.parquet"
    if path.exists():
        return pd.read_parquet(path)
    known = bbg.field_info(REFERENCE_FIELDS)
    fields = [f for f in REFERENCE_FIELDS if f in known]
    skipped = sorted(set(REFERENCE_FIELDS) - set(fields))
    if skipped:
        log.warning("Fields this Terminal does not know, skipped: %s", ", ".join(skipped))
    frames = [bbg.reference(batch, fields)[0] for batch in chunks(tickers, 100)]
    ref = pd.concat(frames, ignore_index=True)
    write_parquet(path, ref)
    return ref


def prices(bbg, tickers, today, days=400):
    """Daily PX_LAST (split and dividend adjusted) for the last `days` calendar days, cached per batch."""
    folder = OUT / f"prices_{today}"
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    extra = {"adjustmentNormal": True, "adjustmentAbnormal": True, "adjustmentSplit": True}
    frames = []
    for i, batch in enumerate(chunks(tickers, 50)):
        path = folder / f"batch-{i:04}.parquet"
        if not path.exists():
            df, _, _ = bbg.history(batch, ["PX_LAST"], start, "DAILY", extra=extra)
            write_parquet(path, df if not df.empty else pd.DataFrame({"ticker": [], "date": [], "PX_LAST": []}))
            log.info("Prices %d/%d (%d requests this run)", i + 1, -(-len(tickers) // 50), bbg.requests)
        frames.append(pd.read_parquet(path))
    px = pd.concat(frames, ignore_index=True)
    px["date"] = pd.to_datetime(px["date"])
    return px.pivot_table(index="date", columns="ticker", values="PX_LAST").sort_index()


def signals(px, ref, today):
    """Per stock: momentum, earnings-announcement return, 52-week high distance, volatility, catalysts."""
    ret = px.pct_change()
    out = pd.DataFrame(index=px.columns)
    last = px.ffill().iloc[-1]
    out["mom_12_1"] = px.ffill().shift(21).iloc[-1] / px.ffill().iloc[-min(len(px), 253)] - 1
    out["ret_1m"] = last / px.ffill().iloc[-min(len(px), 22)] - 1
    out["vol_60d"] = ret.iloc[-60:].std() * np.sqrt(252)
    out["from_52w_high"] = last / px.iloc[-min(len(px), 252):].max() - 1
    ref = ref.set_index("ticker")
    ann = pd.to_datetime(ref.get("LATEST_ANNOUNCEMENT_DT"), errors="coerce") if "LATEST_ANNOUNCEMENT_DT" in ref \
        else pd.Series(pd.NaT, index=ref.index)
    ear, ear_date = {}, {}
    dates = px.index
    for t in out.index:
        a = ann.get(t, pd.NaT)
        if pd.isna(a) or a < pd.Timestamp(date.today()) - pd.Timedelta(days=75):
            continue
        i = dates.searchsorted(a)  # first trading day on or after the announcement
        if i < 1 or i + 1 >= len(dates):
            continue
        # announcements before or after the session: the move shows up between the day before and the day after
        before, after = px[t].iloc[i - 1], px[t].iloc[i + 1]
        if np.isfinite(before) and np.isfinite(after) and before > 0:
            ear[t], ear_date[t] = after / before - 1, a.date()
    out["earnings_reaction"] = pd.Series(ear)
    out["last_report"] = pd.Series(ear_date)
    out = out.join(ref, how="left")
    if "BEST_TARGET_PRICE" in out and "PX_LAST" in out:
        out["target_upside"] = out["BEST_TARGET_PRICE"] / out["PX_LAST"] - 1
    if "EXPECTED_REPORT_DT" in out:
        nxt = pd.to_datetime(out["EXPECTED_REPORT_DT"], errors="coerce")
        out["reports_in_challenge"] = nxt.between(pd.Timestamp(CHALLENGE[0]), pd.Timestamp(CHALLENGE[1]))
    out["credit_danger"] = credit_danger(out)
    return out


def credit_danger(df):
    """Negative outlook or watch at BBB-/Baa3 or below: the fallen-angel and downgrade-drift zone."""
    def negative(col):
        return df[col].astype(str).str.upper().str.contains("NEG|\\*-", regex=True) if col in df else False
    sp = df["RTG_SP_LT_LC_ISSUER_CREDIT"].astype(str) if "RTG_SP_LT_LC_ISSUER_CREDIT" in df else pd.Series("", index=df.index)
    mdy = pd.Series("", index=df.index)
    for col in MOODYS:  # fill from whichever Moody's fields this Terminal returned
        if col in df:
            mdy = mdy.where(mdy.str.len() > 0, df[col].fillna("").astype(str))
    low = sp.str.replace(r"\s.*$", "", regex=True).isin(LOW_IG_OR_WORSE) | \
        mdy.str.replace(r"\(P\)|\s.*$", "", regex=True).isin(MDY_LOW)
    watch_neg = sp.str.contains(r"\*-", regex=True) | mdy.str.contains(r"\*-", regex=True)
    return (low & (negative("RTG_SP_OUTLOOK") | negative("RTG_MDY_OUTLOOK") | watch_neg)).fillna(False)


def rank(sig):
    """Composite score: momentum and earnings reaction first, analyst upside second; liquid, no credit danger."""
    def z(s):
        s = s.astype(float).clip(s.quantile(0.02), s.quantile(0.98))
        return (s - s.mean()) / s.std()
    if "VOLUME_AVG_20D" in sig and "PX_LAST" in sig:  # drop the least traded fifth (in local currency)
        liquid = (sig["VOLUME_AVG_20D"] * sig["PX_LAST"]).rank(pct=True) > 0.2
    else:
        liquid = pd.Series(True, index=sig.index)
    score = 0.4 * z(sig["mom_12_1"]).fillna(0) + 0.4 * z(sig["earnings_reaction"]).fillna(0)
    if "target_upside" in sig:
        score += 0.2 * z(sig["target_upside"]).fillna(0)
    out = sig.assign(score=score, liquid=liquid)
    keep = out["liquid"] & ~out["credit_danger"] & out["mom_12_1"].notna()
    return out[keep].sort_values("score", ascending=False), out


def write_report(ranked, everything, today):
    cols = [c for c in ["NAME", "index", "GICS_SECTOR_NAME", "COUNTRY_ISO", "score", "mom_12_1", "earnings_reaction",
                        "last_report", "EXPECTED_REPORT_DT", "reports_in_challenge", "target_upside",
                        "BEST_ANALYST_RATING", "from_52w_high", "ret_1m", "vol_60d", "RTG_SP_LT_LC_ISSUER_CREDIT",
                        "RTG_SP_OUTLOOK", *MOODYS, "RTG_MDY_OUTLOOK", "CUR_MKT_CAP", "CRNCY"]
            if c in everything]
    path = OUT / f"screen_{today}.xlsx"
    recent = ranked[ranked["earnings_reaction"].notna()].sort_values("earnings_reaction", ascending=False)
    upcoming = ranked[ranked["reports_in_challenge"].fillna(False).astype(bool)] if "reports_in_challenge" in ranked \
        else ranked.iloc[:0]
    with pd.ExcelWriter(path) as xl:
        ranked[cols].head(60).to_excel(xl, sheet_name="Top candidates")
        recent[recent["mom_12_1"] > 0][cols].head(40).to_excel(xl, sheet_name="Strong recent reporters")
        upcoming[cols].head(60).to_excel(xl, sheet_name="Reporting in challenge")
        everything[everything["credit_danger"]][cols].to_excel(xl, sheet_name="Credit danger - avoid")
        everything[cols].sort_values("score", ascending=False).to_excel(xl, sheet_name="All screened")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--indices", nargs="+", default=list(INDICES), choices=list(INDICES))
    ap.add_argument("--max-requests", type=int)
    args = ap.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOGS / "bgtc_screen.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    # saved progress is per day and per index selection: a quick SPX test must not stand in for the full run
    today = f"{date.today().isoformat()}_{'-'.join(sorted(args.indices))}"
    bbg = Bloomberg(args.max_requests)
    code = 0
    try:
        uni = universe(bbg, args.indices, today)
        tickers = sorted(uni["ticker"])
        ref = reference(bbg, tickers, today).merge(uni, on="ticker", how="left")
        px = prices(bbg, tickers, today)
        ranked, everything = rank(signals(px, ref, today))
        path = write_report(ranked, everything, today)
        log.info("Screen saved: %s (%d candidates after filters, %d in credit danger)", path, len(ranked),
                 int(everything["credit_danger"].sum()))
        top = ranked.head(15)
        print(top[[c for c in ["NAME", "score", "mom_12_1", "earnings_reaction", "target_upside"] if c in top]]
              .round(3).to_string())
    except LimitReached as e:
        log.error("Bloomberg data limit reached; progress saved, rerun later or from another login.\n%s", e)
        code = 3
    except BudgetExhausted as e:
        log.warning("Stopping: %s. Progress saved; rerun to continue.", e)
    except KeyboardInterrupt:
        log.warning("Interrupted, progress saved.")
        code = 130
    finally:
        bbg.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
