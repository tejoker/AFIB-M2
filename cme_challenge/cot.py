"""Weekly hedge fund positioning in each commodity: CFTC Commitments of Traders, managed money.

Source: the CFTC disaggregated futures-only report through publicreporting.cftc.gov (free, no key), from 2009.
Positions are as of Tuesday and published Friday at 3:30 pm ET (a day or two later after a federal holiday);
a report is used only from the following Monday's close, so no signal uses it before it was public.

Signals per market, known at each close:
  cot_z       managed money long minus short as a share of open interest, against its last 3 years (z-score):
              above 0 = funds more long than usual (crowded long)
  cot_change  4-week change of that share, in percentage points: above 0 = funds have been buying

Usage (from the repository root):
    python cme_challenge/cot.py
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA

COT = DATA / "futures" / "cot" / "managed_money.parquet"
URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
CODES = {  # Bloomberg root (as in futures_markets.py, stripped): CFTC contract market code
    "LC": "057642", "LH": "054642", "FC": "061641", "C": "002602", "S": "005602", "SM": "026603", "BO": "007601",
    "W": "001602", "KW": "001612", "CL": "067651", "NG": "023651", "HO": "022651", "XB": "111659", "GC": "088691",
    "SI": "084691", "HG": "085692", "PL": "076651", "PA": "075651",
}
PUBLIC_AFTER = pd.Timedelta(days=6)  # Tuesday's positions, used from the following Monday's close
Z_WEEKS, MIN_WEEKS, CHANGE_WEEKS = 156, 52, 4


def fetch(start="2009-01-01"):
    query = urllib.parse.urlencode({
        "$select": "cftc_contract_market_code,report_date_as_yyyy_mm_dd,open_interest_all,"
                   "m_money_positions_long_all,m_money_positions_short_all",
        "$where": f"report_date_as_yyyy_mm_dd >= '{start}' and cftc_contract_market_code in("
                  + ",".join(f"'{c}'" for c in CODES.values()) + ")",
        "$order": "report_date_as_yyyy_mm_dd", "$limit": 50000})
    with urllib.request.urlopen(f"{URL}?{query}", timeout=120) as response:
        rows = json.load(response)
    root_of = {code: root for root, code in CODES.items()}
    df = pd.DataFrame(rows)
    return pd.DataFrame({
        "root": df["cftc_contract_market_code"].map(root_of),
        "report_date": pd.to_datetime(df["report_date_as_yyyy_mm_dd"]).dt.tz_localize(None).dt.normalize(),
        "open_interest": pd.to_numeric(df["open_interest_all"]),
        "mm_long": pd.to_numeric(df["m_money_positions_long_all"]),
        "mm_short": pd.to_numeric(df["m_money_positions_short_all"])}).drop_duplicates(["root", "report_date"])


def load(update=True, max_age_hours=12):
    """The cached reports, refreshed when older than max_age_hours; the cache is used if CFTC is unreachable."""
    fresh = COT.exists() and pd.Timestamp.now().timestamp() - COT.stat().st_mtime < max_age_hours * 3600
    if update and not fresh:
        try:
            table = fetch()
            COT.parent.mkdir(parents=True, exist_ok=True)
            table.to_parquet(COT, index=False)
            return table
        except OSError as e:
            if not COT.exists():
                raise
            print(f"CFTC unreachable ({e}); using the cached reports", file=sys.stderr)
    return pd.read_parquet(COT) if COT.exists() else pd.DataFrame()


def signals(table, root, dates):
    """cot_z and cot_change for one root on the given trading dates, each from the latest public report."""
    rows = table[table["root"] == root].sort_values("report_date")
    out = pd.DataFrame(index=dates, columns=["cot_z", "cot_change"], dtype=float)
    if rows.empty:
        return out
    net = (rows["mm_long"] - rows["mm_short"]) / rows["open_interest"].where(rows["open_interest"] > 0)
    mean = net.rolling(Z_WEEKS, min_periods=MIN_WEEKS).mean()
    sd = net.rolling(Z_WEEKS, min_periods=MIN_WEEKS).std()
    weekly = pd.DataFrame({"public": (rows["report_date"] + PUBLIC_AFTER).astype("datetime64[ns]"),
                           "cot_z": ((net - mean) / sd.where(sd > 0)).to_numpy(),
                           "cot_change": 100 * net.diff(CHANGE_WEEKS).to_numpy()})
    left = pd.DataFrame({"date": pd.DatetimeIndex(dates).astype("datetime64[ns]")})  # same resolution as `public`
    merged = pd.merge_asof(left, weekly.sort_values("public"), left_on="date", right_on="public",
                           tolerance=pd.Timedelta(days=21))  # a report missing for 3 weeks is not reused
    return merged.set_index("date")[["cot_z", "cot_change"]].set_axis(dates)


def main():
    table = load()
    latest = table.sort_values("report_date").groupby("root").tail(1).set_index("root")
    net = 100 * (latest["mm_long"] - latest["mm_short"]) / latest["open_interest"]
    print(f"Managed money net position, % of open interest, report of {latest['report_date'].max().date()}\n")
    print(net.round(1).sort_values().to_string())
    print(f"\nSaved {COT}")


if __name__ == "__main__":
    main()
