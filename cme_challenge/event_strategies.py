"""Benchmark two short-horizon strategies with published evidence, on the roll-correct series.

pre-FOMC drift  Equity index futures tend to rise in the 24 hours before scheduled Fed announcements (Lucca &
                Moench, 2015). Tested here with daily closes: long from the close before the announcement day
                to its close, which also includes the 2 pm ET reaction (a cruder window than the paper's).
                Announcement dates: last day of each scheduled FOMC meeting, from federalreserve.gov
                (historical pages for 2000-2020, cached; the calendar page for 2021-2026, listed below).
reversal        Short-term reversal across futures: each week, long the markets with the worst volatility-
                scaled return over the past 5 days and short the best (top and bottom third), held 5 days.

Usage (from the repository root):
    python cme_challenge/event_strategies.py
"""
import re
import sys
import time
import urllib.request
from html import unescape
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

from futures_research import load_series, root_info, t_stat

RESEARCH = DATA / "futures" / "research"
FOMC_CACHE = RESEARCH / "fomc_announcements.csv"
# Scheduled meetings from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (fetched 12 Oct 2026);
# the announcement is on the last day
RECENT_MEETINGS = {
    2021: ["Jan 26-27", "Mar 16-17", "Apr 27-28", "Jun 15-16", "Jul 27-28", "Sep 21-22", "Nov 2-3", "Dec 14-15"],
    2022: ["Jan 25-26", "Mar 15-16", "May 3-4", "Jun 14-15", "Jul 26-27", "Sep 20-21", "Nov 1-2", "Dec 13-14"],
    2023: ["Feb 1", "Mar 21-22", "May 2-3", "Jun 13-14", "Jul 25-26", "Sep 19-20", "Nov 1", "Dec 12-13"],
    2024: ["Jan 30-31", "Mar 19-20", "May 1", "Jun 11-12", "Jul 30-31", "Sep 17-18", "Nov 6-7", "Dec 17-18"],
    2025: ["Jan 28-29", "Mar 18-19", "May 6-7", "Jun 17-18", "Jul 29-30", "Sep 16-17", "Oct 28-29", "Dec 9-10"],
    2026: ["Jan 27-28", "Mar 17-18", "Apr 28-29", "Jun 16-17", "Jul 28-29", "Sep 15-16", "Oct 27-28", "Dec 8-9"],
}


def last_day(year, text):
    """'Jan 26-27' -> date of the 27th; 'Feb 1' -> 1 Feb."""
    month, days = text.split(" ", 1)
    return pd.Timestamp(f"{year}-{month}-{days.split('-')[-1]}")


def fomc_announcements():
    if FOMC_CACHE.exists():
        return pd.to_datetime(pd.read_csv(FOMC_CACHE)["date"])
    dates = []
    for year in range(2000, 2021):
        req = urllib.request.Request(f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = unescape(r.read().decode("utf-8", "replace"))
        for head in re.findall(r"<h5[^>]*>([^<]*Meeting[^<]*)</h5>", page):
            if "unscheduled" in head.lower() or "cancel" in head.lower() or "conference" in head.lower():
                continue
            m = re.match(r"\s*(\w+)(?:/(\w+))?\s+([\d\-]+)\s+Meeting", head)
            if not m:
                continue
            month = m.group(2) or m.group(1)  # "Jan/Feb 31-1" ends in February
            dates.append(last_day(year, f"{month[:3]} {m.group(3)}"))
        time.sleep(1)
    dates += [last_day(y, t) for y, meetings in RECENT_MEETINGS.items() for t in meetings]
    out = pd.Series(sorted(set(dates)), name="date")
    RESEARCH.mkdir(parents=True, exist_ok=True)
    out.to_frame().to_csv(FOMC_CACHE, index=False)
    return out


def pre_fomc(series, info, announcements):
    rows = []
    past = announcements[announcements <= pd.Timestamp.today()]
    for root in [r for r in series if info["sector"].get(r) in ("Equity index", "Rates", "FX")]:
        ret = series[root]["ret"].dropna()
        days = ret.index.intersection(past)
        event, other = ret.loc[days], ret.drop(days)
        recent = event[event.index >= event.index.max() - pd.DateOffset(years=10)] if len(event) else event
        rows.append({"root": root, "name": info["name"].get(root, root), "events": len(event),
                     "mean_event_pct": 100 * event.mean(), "mean_other_day_pct": 100 * other.mean(),
                     "t": t_stat(event), "up_share": (event > 0).mean(), "mean_last_10y_pct": 100 * recent.mean(),
                     "t_last_10y": t_stat(recent)})
    return pd.DataFrame(rows).sort_values("t", ascending=False)


def reversal(series, info, sectors=None, lookback=5, hold=5):
    rets = pd.DataFrame({r: s["ret"] for r, s in series.items()
                         if sectors is None or info["sector"].get(r) in sectors}).sort_index()
    rets = rets.loc["2006":]
    vol = rets.rolling(63, min_periods=40).std()
    past = rets.rolling(lookback).sum() / (vol * np.sqrt(lookback))
    fwd = rets[::-1].rolling(hold).sum()[::-1].shift(-1) / (vol * np.sqrt(hold))  # next `hold` days, scaled
    periods = []
    for d in rets.index[63::hold]:
        score = past.loc[d].dropna()
        if len(score) < 6:
            continue
        rank = score.rank(pct=True)
        side = pd.Series(np.where(rank <= 1 / 3, 1.0, np.where(rank > 2 / 3, -1.0, 0.0)), index=score.index)
        periods.append((d, (side * fwd.loc[d, score.index]).mean()))
    p = pd.Series(dict(periods)).dropna()
    recent = p[p.index >= p.index.max() - pd.DateOffset(years=5)]
    sharpe = lambda x: x.mean() / x.std() * np.sqrt(252 / hold) if len(x) > 10 else np.nan
    return {"markets": rets.shape[1], "periods": len(p), "sharpe": sharpe(p), "t": t_stat(p),
            "hit_rate": (p > 0).mean(), "sharpe_last_5y": sharpe(recent)}


def main():
    series, info = load_series(), root_info()
    announcements = fomc_announcements()
    upcoming = announcements[announcements > pd.Timestamp.today()]
    pd.set_option("display.width", 220)
    print(f"Pre-FOMC drift: {len(announcements[announcements <= pd.Timestamp.today()])} scheduled announcements since "
          f"{announcements.min().date()}; next: {upcoming.min().date() if len(upcoming) else 'none listed'}\n")
    table = pre_fomc(series, info, announcements)
    print(table.round(3).to_string(index=False))
    rows = [{"universe": "all markets", **reversal(series, info)},
            {"universe": "commodities", **reversal(series, info, ("Energy", "Grains", "Livestock", "Metals"))},
            {"universe": "financials", **reversal(series, info, ("Equity index", "Rates", "FX"))}]
    rev = pd.DataFrame(rows)
    print("\nWeekly short-term reversal (long last week's losers, short winners), before costs:\n")
    print(rev.round(3).to_string(index=False))
    RESEARCH.mkdir(parents=True, exist_ok=True)
    table.to_csv(RESEARCH / "pre_fomc.csv", index=False)
    rev.to_csv(RESEARCH / "reversal.csv", index=False)
    print(f"\nSaved {RESEARCH / 'pre_fomc.csv'} and reversal.csv")


if __name__ == "__main__":
    main()
