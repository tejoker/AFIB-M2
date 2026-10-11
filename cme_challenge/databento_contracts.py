"""Collect daily prices of every futures contract from Databento, expired ones included (no Bloomberg needed).

Saves the same layout as bloomberg/futures_contracts.py under <data>/futures/databento/, which
cme_challenge/continuous.py prefers for the markets it covers. CME Globex history starts in June 2010. The first
run downloads everything; later runs add only the days since the last one. Databento's price for the request
is shown first and the script stops above --max-cost unless --yes is given.

Each contract is identified by its CME symbol and a two-digit year (LEZ15, LEZ25), worked out from its trading
dates. Prices are checked against Yahoo's quote for a live contract and rescaled if Databento uses another unit.
The roll date stored for each contract is the day the next contract's volume overtook it, so the roll-correct
series switches when traders actually did.

Setup: create an account at databento.com (new accounts get free credit) and store the API key once:
    [Environment]::SetEnvironmentVariable("DATABENTO_API_KEY", "db-...", "User")

Usage (from the repository root):
    python cme_challenge/databento_contracts.py --estimate        # price of what is missing, nothing downloaded
    python cme_challenge/databento_contracts.py                   # commodities (default)
    python cme_challenge/databento_contracts.py --sectors all     # every market in futures_markets.py
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import DATA
from futures_markets import MARKETS

DATASET = "GLBX.MDP3"
FIRST_DAY = date(2010, 6, 6)
SCHEMAS = ("ohlcv-eod", "ohlcv-1d")  # session-aligned bars if offered, else UTC-day bars
COMMODITIES = ("Livestock", "Grains", "Energy", "Metals")
MONTH_CODES = "FGHJKMNQUVXZ"
OUT = DATA / "futures" / "databento"
STATE_FILE = OUT / "state.json"


def connect():
    import databento as db
    return db.Historical()  # reads DATABENTO_API_KEY


def client_error():
    import databento as db
    return db.BentoClientError


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"through": {}, "schema": None}


def save_state(state):
    OUT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def pick_schema(client, state, parents, start, end):
    if state.get("schema"):
        return state["schema"]
    for schema in SCHEMAS:
        try:
            client.metadata.get_cost(dataset=DATASET, symbols=parents[:1], stype_in="parent", schema=schema,
                                     start=start, end=end)
            return schema
        except client_error():
            continue
    raise SystemExit(f"Databento offers none of {SCHEMAS} for {DATASET}")


def raw_symbols(client, bars, start, end):
    """Contract symbol of each bar: Databento's own column when it holds contract symbols, else via symbology."""
    if bars["symbol"].astype(str).str.contains(r"\d$").all():
        return bars["symbol"].astype(str)
    ids = sorted(bars["instrument_id"].astype(int).unique())
    intervals = []
    for i in range(0, len(ids), 2000):
        res = client.symbology.resolve(dataset=DATASET, symbols=[str(x) for x in ids[i:i + 2000]],
                                       stype_in="instrument_id", stype_out="raw_symbol",
                                       start_date=str(start), end_date=str(end))
        for iid, spans in res.get("result", {}).items():
            intervals += [(int(iid), pd.Timestamp(s["d0"]), pd.Timestamp(s["d1"]), s["s"]) for s in spans]
    table = pd.DataFrame(intervals, columns=["instrument_id", "d0", "d1", "raw"])
    merged = bars[["instrument_id", "date"]].reset_index().merge(table, on="instrument_id", how="left")
    merged = merged[(merged["date"] >= merged["d0"]) & (merged["date"] < merged["d1"])]
    return merged.drop_duplicates("index").set_index("index")["raw"].reindex(bars.index)


def contract_year(digits, month, first, last, live):
    """Delivery year from the symbol's year digits and the dates the contract traded."""
    if len(digits) == 2:
        return 2000 + int(digits)
    d = int(digits)
    if live:  # still trading: the first matching year from the current one
        return last.year + (d - last.year % 10) % 10
    year = last.year + (1 if month < last.month - 2 else 0)  # expired: last trade sits in or just before delivery
    return year - (year % 10 - d) % 10


def split_contracts(root, market, bars, data_end):
    """One frame per contract; the same symbol ten years apart (LEZ5) becomes two contracts."""
    pattern = re.compile(rf"^{re.escape(market.yahoo)}([{MONTH_CODES}])(\d{{1,2}})$")
    out = {}
    for symbol, rows in bars.groupby("raw"):
        m = pattern.match(str(symbol))
        if not m or m.group(1) not in market.months:
            continue  # spreads, other products, thin months
        rows = rows.sort_values("date")
        segment = (rows["date"].diff() > pd.Timedelta(days=300)).cumsum()
        for _, seg in rows.groupby(segment):
            live = seg["date"].iloc[-1] >= pd.Timestamp(data_end) - pd.Timedelta(days=5)
            month = MONTH_CODES.index(m.group(1)) + 1
            year = contract_year(m.group(2), month, seg["date"].iloc[0], seg["date"].iloc[-1], live)
            ticker = f"{market.yahoo}{m.group(1)}{year % 100:02d}"
            out[ticker] = (seg, pd.Timestamp(year=year, month=month, day=1), live)
    return out


def price_scale(root, market, contracts):
    """Power of ten between Databento's and Yahoo's quote for the most recent live contract (1 if unknown)."""
    live = [(t, seg) for t, (seg, _, is_live) in contracts.items() if is_live]
    if not live:
        return 1.0
    ticker, seg = max(live, key=lambda c: c[1]["volume"].iloc[-5:].mean())
    try:
        import yfinance as yf
        quote = yf.Ticker(f"{ticker}.{market.exchange}").history(period="10d")["Close"]
        return scale_from(seg["close"].iloc[-1] / quote.iloc[-1])
    except Exception:  # no Yahoo: keep Databento's unit
        return 1.0


def scale_from(ratio):
    """Nearest power of ten to a price ratio (100 for cents vs dollars), 1 if the ratio is unusable."""
    return float(10 ** round(np.log10(ratio))) if np.isfinite(ratio) and ratio > 0 else 1.0


def roll_dates(table, histories):
    """Day the next contract's 3-day volume first beat the current one's, within 90 days before its last trade."""
    table = table.sort_values("delivery").reset_index(drop=True)
    rolls = []
    for i, c in table.iterrows():
        nxt = table.iloc[i + 1]["ticker"] if i + 1 < len(table) else None
        roll = pd.NaT
        if nxt is not None and c["ticker"] in histories and nxt in histories:
            vol = pd.concat([histories[c["ticker"]].set_index("date")["PX_VOLUME"],
                             histories[nxt].set_index("date")["PX_VOLUME"]], axis=1, sort=True).fillna(0)
            vol = vol.rolling(3, min_periods=1).mean()
            window = vol.loc[c["LAST_TRADEABLE_DT"] - pd.Timedelta(days=90):c["LAST_TRADEABLE_DT"]]
            over = window.index[window.iloc[:, 1] > window.iloc[:, 0]]
            roll = over[0] if len(over) else pd.NaT
        rolls.append(roll)
    return table.assign(ROLL_DT=rolls)


def yahoo_expiries():
    path = DATA / "futures" / "live" / "expiries.json"
    return json.loads(path.read_text()) if path.exists() else {}


def update_root(client, root, market, schema, start, end):
    bars = client.timeseries.get_range(dataset=DATASET, symbols=[f"{market.yahoo}.FUT"], stype_in="parent",
                                       schema=schema, start=str(start), end=str(end)).to_df()
    folder = OUT / "history" / root.strip()
    folder.mkdir(parents=True, exist_ok=True)
    if not bars.empty:
        bars = bars.reset_index()
        bars["date"] = pd.to_datetime(bars["ts_event"]).dt.tz_localize(None).dt.normalize()
        bars["raw"] = raw_symbols(client, bars, start, end)
        contracts = split_contracts(root, market, bars, end)
        scale = price_scale(root, market, contracts)
        for ticker, (seg, _, _) in contracts.items():
            new = pd.DataFrame({"ticker": ticker, "date": seg["date"].to_numpy(),
                                "PX_OPEN": seg["open"].to_numpy() / scale, "PX_HIGH": seg["high"].to_numpy() / scale,
                                "PX_LOW": seg["low"].to_numpy() / scale, "PX_LAST": seg["close"].to_numpy() / scale,
                                "PX_VOLUME": seg["volume"].to_numpy(dtype=float)})
            path = folder / f"{ticker}.parquet"
            if path.exists():
                new = pd.concat([pd.read_parquet(path), new]).drop_duplicates("date", keep="last")
            new.sort_values("date").to_parquet(path, index=False)
    return contract_table(root, market, folder, end)


def contract_table(root, market, folder, data_end):
    """Contract details for every saved contract of the root, with last trade and roll dates."""
    histories = {p.stem: pd.read_parquet(p) for p in sorted(folder.glob("*.parquet"))}
    expiry = yahoo_expiries()
    rows = []
    for ticker, h in histories.items():
        code, yy = ticker[len(market.yahoo)], int(ticker[len(market.yahoo) + 1:])
        delivery = pd.Timestamp(year=2000 + yy, month=MONTH_CODES.index(code) + 1, day=1)
        last = h["date"].max()
        live = last >= pd.Timestamp(data_end) - pd.Timedelta(days=5)
        known = expiry.get(f"{ticker[:-2]}{yy:02d}.{market.exchange}")
        ltd = pd.Timestamp(known) if live and known else (delivery + pd.Timedelta(days=27) if live else last)
        rows.append({"ticker": ticker, "root": root, "name": market.name, "sector": market.sector,
                     "tier": market.tier, "month_code": code, "delivery": delivery, "LAST_TRADEABLE_DT": ltd,
                     "FUT_NOTICE_FIRST": pd.NaT, "FUT_VAL_PT": float(market.point_value), "source": "databento"})
    if not rows:
        return pd.DataFrame()
    return roll_dates(pd.DataFrame(rows), histories)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sectors", nargs="+", default=list(COMMODITIES), help="sectors to collect, or 'all'")
    ap.add_argument("--estimate", action="store_true", help="print Databento's price and stop")
    ap.add_argument("--max-cost", type=float, default=25.0, help="stop if the request costs more (USD)")
    ap.add_argument("--yes", action="store_true", help="download even above --max-cost")
    args = ap.parse_args()
    sectors = {m.sector for m in MARKETS.values()} if args.sectors == ["all"] else set(args.sectors)
    roots = [r for r, m in MARKETS.items() if m.sector in sectors]
    if not roots:
        ap.error(f"No markets in sectors {sorted(sectors)}")

    client, state = connect(), load_state()
    available = client.metadata.get_dataset_range(dataset=DATASET)
    end = min(pd.Timestamp(available["end"]).tz_localize(None).date(), date.today())
    starts = {r: max(FIRST_DAY, date.fromisoformat(state["through"][r.strip()]) - timedelta(days=7))
              if r.strip() in state["through"] else FIRST_DAY for r in roots}
    parents = [f"{MARKETS[r].yahoo}.FUT" for r in roots]
    schema = pick_schema(client, state, parents, min(starts.values()), end)
    costs = {r: client.metadata.get_cost(dataset=DATASET, symbols=[f"{MARKETS[r].yahoo}.FUT"], stype_in="parent",
                                         schema=schema, start=str(starts[r]), end=str(end)) for r in roots}
    total = sum(costs.values())
    print(f"{schema} from {min(starts.values())} to {end} for {len(roots)} markets: Databento price ${total:,.2f}")
    if args.estimate:
        for r in roots:
            print(f"  {r.strip():4s} {MARKETS[r].name:24s} from {starts[r]}  ${costs[r]:,.2f}")
        return
    if total > args.max_cost and not args.yes:
        raise SystemExit(f"Above --max-cost ${args.max_cost:,.2f}; rerun with --yes or a higher --max-cost.")

    state["schema"] = schema
    tables = []
    for r in roots:
        table = update_root(client, r, MARKETS[r], schema, starts[r], end)
        state["through"][r.strip()] = str(end)
        save_state(state)
        if not table.empty:
            tables.append(table)
        print(f"  {r.strip():4s} {MARKETS[r].name:24s} {len(table):4d} contracts")
    path = OUT / "contracts.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    if tables:
        new = pd.concat(tables, ignore_index=True)
        if not old.empty:
            old = old[~old["root"].isin(new["root"])]
        pd.concat([old, new], ignore_index=True).to_parquet(path, index=False)
    print(f"Saved under {OUT}; next: python cme_challenge/continuous.py")


if __name__ == "__main__":
    main()
