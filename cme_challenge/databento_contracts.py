"""Collect daily prices of every futures contract from Databento, expired ones included (no Bloomberg needed).

Saves the same layout as bloomberg/futures_contracts.py under <data>/futures/databento/, which
cme_challenge/continuous.py prefers for the markets it covers. CME Globex history starts in June 2010. The first
run downloads everything; later runs add only the days since the last one. Databento's price for the request
is shown first and the script stops above --max-cost unless --yes is given.

Each contract is identified by its CME symbol and a two-digit year (LEZ15, LEZ25), taken from Databento's
instrument definitions (one sample a month, a few cents in all), which also give its exact expiry. Prices are checked against Yahoo's quote for a live contract and rescaled if Databento uses another unit.
The roll date stored for each contract is the day the next contract's volume overtook it, so the roll-correct
series switches when traders actually did.

Setup: create an account at databento.com (new accounts get free credit), copy cme_challenge/.env.example to
cme_challenge/.env and put the API key in it (.env is never committed). A DATABENTO_API_KEY environment
variable, if set, takes precedence.

Usage (from the repository root):
    python cme_challenge/databento_contracts.py --estimate        # price of what is missing, nothing downloaded
    python cme_challenge/databento_contracts.py                   # commodities (default)
    python cme_challenge/databento_contracts.py --roots LE HE CL  # only these markets (CME symbols)
    python cme_challenge/databento_contracts.py --sectors all     # every market in futures_markets.py
    python cme_challenge/databento_contracts.py --rebuild         # reprocess the saved answer, no bars bought
"""
import argparse
import json
import os
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


def env_file_key(path=Path(__file__).with_name(".env")):
    """DATABENTO_API_KEY from cme_challenge/.env (KEY=value lines; # comments and quotes allowed)."""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == "DATABENTO_API_KEY":
            key = value.split(" #")[0].strip().strip("'\"")
            return key.removeprefix("db-") if key.startswith("db-db-") else key or None  # template prefix pasted twice
    return None


def connect():
    import databento as db
    key = os.environ.get("DATABENTO_API_KEY") or env_file_key()
    if not key:
        raise SystemExit("No Databento API key: copy cme_challenge/.env.example to cme_challenge/.env and fill it in")
    return db.Historical(key)


def client_error():
    import databento as db
    return db.BentoClientError


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"through": {}, "schema": None}


def save_state(state):
    OUT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def outrights(market):
    """CME symbols of the liquid contract months: one-digit years (LEZ0..LEZ9) and the two-digit form CME moved
    some products to (natural gas writes NGZ26 since 2025). Asking for these instead of the whole product
    (LE.FUT) leaves out the calendar spreads, which made requests about 7 times dearer."""
    return ([f"{market.yahoo}{code}{digit}" for code in market.months for digit in range(10)]
            + [f"{market.yahoo}{code}{year % 100:02d}" for code in market.months for year in range(2020, 2041)])


def pick_schema(client, state, symbols, start, end):
    if state.get("schema"):
        return state["schema"]
    for schema in SCHEMAS:
        try:
            client.metadata.get_cost(dataset=DATASET, symbols=symbols, stype_in="raw_symbol", schema=schema,
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


def contract_year(digits, month, first):
    """Delivery year from the symbol's year digit(s) and the contract's first trade: a contract trades only before
    its delivery and is never listed ten years ahead, so it is the first matching year from that trade on."""
    if len(digits) == 2:
        return 2000 + int(digits)
    year = first.year + (int(digits) - first.year % 10) % 10
    if year == first.year and month < first.month - 1:  # that year's contract had already expired
        year += 10
    return year


def sample_days(start, end):
    """Second business day of every month from start to end, plus the last day before end."""
    months = pd.date_range(pd.Timestamp(start).replace(day=1), pd.Timestamp(end), freq="MS")
    days = [pd.bdate_range(m, periods=2)[1] for m in months]
    return sorted({d.date() for d in days if pd.Timestamp(start) <= d < pd.Timestamp(end)}
                  | {(pd.Timestamp(end) - pd.offsets.BDay(1)).date()})


def definitions_on(client, symbols, day):
    try:
        return client.timeseries.get_range(dataset=DATASET, symbols=symbols, stype_in="raw_symbol",
                                           schema="definition", start=str(day),
                                           end=str(day + timedelta(days=1))).to_df()
    except client_error() as e:
        if getattr(e, "http_status", None) == 422:  # none of the symbols existed that day (market not listed yet)
            return pd.DataFrame()
        raise


def update_definitions(client, roots, end):
    """Instrument definitions (id, symbol, delivery month, expiry) sampled monthly, cached in raw/definitions."""
    path = OUT / "raw" / "definitions.parquet"
    defs = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["sample_day", "root", "instrument_id"])
    # A sample is done if it found contracts, or came back empty for the same symbol list (a market not trading
    # yet); empty samples are asked again when the symbol list changes (cents)
    asked = defs["n_symbols"] if "n_symbols" in defs else pd.Series(np.nan, index=defs.index)
    current = {r: len(outrights(MARKETS[r])) for r in MARKETS}
    keep = defs["instrument_id"].notna() | (asked == defs["root"].map(current))
    defs = defs[keep]
    done = set(zip(defs["sample_day"].astype(str), defs["root"]))
    for day in sample_days(FIRST_DAY, end):
        missing = [r for r in roots if (str(day), r) not in done]
        if not missing:
            continue
        symbols = [s for r in missing for s in outrights(MARKETS[r])]
        frames = [definitions_on(client, symbols[i:i + 2000], day)
                  for i in range(0, len(symbols), 2000)]  # Databento takes 2,000 symbols per request
        frame = pd.concat([f for f in frames if not f.empty]) if any(not f.empty for f in frames) else frames[0]
        rows = [pd.DataFrame({"sample_day": str(day), "root": r, "n_symbols": current[r]}, index=[0])
                for r in missing]  # marks the sample; dropped once real definitions exist for it
        if not frame.empty:
            frame = frame.reset_index()[["instrument_id", "raw_symbol", "expiration", "maturity_year", "maturity_month"]]
            prefix = {MARKETS[r].yahoo: r for r in missing}
            frame["root"] = [next((prefix[p] for p in sorted(prefix, key=len, reverse=True) if str(s).startswith(p)),
                                  None) for s in frame["raw_symbol"]]
            rows.append(frame.assign(sample_day=str(day)))
        defs = pd.concat([defs, *rows], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    defs.to_parquet(path, index=False)
    return defs


def split_contracts(root, market, bars, data_end, defs=None):
    """One frame per contract. Databento's instrument id tells apart the same symbol ten years apart (LEZ5 for
    2015 and 2025); its definition gives the delivery month and expiry. Without a definition the year is
    estimated from the first trade."""
    pattern = re.compile(rf"^{re.escape(market.yahoo)}([{MONTH_CODES}])(\d{{1,2}})$")
    known = {}
    if defs is not None and len(defs) and "instrument_id" in defs:
        d = defs.dropna(subset=["instrument_id", "maturity_year"])
        known = {(int(i), str(s)): (int(y), int(mo), e) for i, s, y, mo, e in
                 zip(d["instrument_id"], d["raw_symbol"], d["maturity_year"], d["maturity_month"], d["expiration"])}
    out = {}
    for (iid, symbol), rows in bars.groupby(["instrument_id", "raw"]):
        m = pattern.match(str(symbol))
        if not m or m.group(1) not in market.months:
            continue  # spreads, other products, thin months
        rows = rows.sort_values("date")
        month = MONTH_CODES.index(m.group(1)) + 1
        expiry = pd.NaT
        if (int(iid), str(symbol)) in known:
            year, month, expiry = known[(int(iid), str(symbol))]
            expiry = pd.Timestamp(expiry).tz_localize(None).normalize() if pd.notna(expiry) else pd.NaT
        else:
            year = contract_year(m.group(2), month, rows["date"].iloc[0])
        ticker = f"{market.yahoo}{m.group(1)}{year % 100:02d}"
        delivery = pd.Timestamp(year=year, month=month, day=1)
        live = (expiry if pd.notna(expiry) else delivery + pd.offsets.MonthEnd(1)) >= pd.Timestamp(data_end)
        if ticker in out:  # the exchange gave the same contract a new id: one history
            rows = pd.concat([out[ticker][0], rows]).drop_duplicates("date", keep="last").sort_values("date")
            expiry = expiry if pd.notna(expiry) else out[ticker][3]
        out[ticker] = (rows, delivery, live, expiry)
    return out


def price_scale(root, market, contracts):
    """Power of ten between Databento's and Yahoo's quote for the most recent live contract (1 if unknown)."""
    live = [(t, seg) for t, (seg, _, is_live, _) in contracts.items() if is_live]
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


def roll_dates(table, histories, roll_days=5):
    """Day the next contract's 3-day volume first beat the current one's, searched only once the current one is
    the front contract (after the previous roll) and within 90 days of its last trade. Far contracts can swap
    volume leads long before, which would otherwise skip a contract altogether."""
    table = table.sort_values("delivery").reset_index(drop=True)
    rolls, previous = [], pd.Timestamp.min
    for i, c in table.iterrows():
        nxt = table.iloc[i + 1]["ticker"] if i + 1 < len(table) else None
        roll = pd.NaT
        if nxt is not None and c["ticker"] in histories and nxt in histories:
            vol = pd.concat([histories[c["ticker"]].set_index("date")["PX_VOLUME"],
                             histories[nxt].set_index("date")["PX_VOLUME"]], axis=1, sort=True).fillna(0)
            vol = vol.rolling(3, min_periods=1).mean()
            start = max(c["LAST_TRADEABLE_DT"] - pd.Timedelta(days=90), previous + pd.Timedelta(days=1))
            window = vol.loc[start:c["LAST_TRADEABLE_DT"]]
            over = window.index[window.iloc[:, 1] > window.iloc[:, 0]]
            roll = over[0] if len(over) else pd.NaT
        rolls.append(roll)
        # where the series leaves this contract: its volume roll, else the calendar rule continuous.py applies
        previous = roll if pd.notna(roll) else c["LAST_TRADEABLE_DT"] - pd.offsets.BDay(roll_days)
    return table.assign(ROLL_DT=rolls)


def yahoo_expiries():
    path = DATA / "futures" / "live" / "expiries.json"
    return json.loads(path.read_text()) if path.exists() else {}


RAW_COLUMNS = ["instrument_id", "raw", "date", "open", "high", "low", "close", "volume"]


def update_root(client, root, market, schema, start, end, defs=None):
    """Add the new bars to the root's copy of Databento's answer, then rebuild its contract files from that copy
    (so reprocessing never needs a paid download again)."""
    bars = client.timeseries.get_range(dataset=DATASET, symbols=outrights(market), stype_in="raw_symbol",
                                       schema=schema, start=str(start), end=str(end)).to_df()
    raw_path = OUT / "raw" / f"{root.strip()}.parquet"
    raw = pd.read_parquet(raw_path) if raw_path.exists() else pd.DataFrame(columns=RAW_COLUMNS)
    if not bars.empty:
        bars = bars.reset_index()
        bars["date"] = pd.to_datetime(bars["ts_event"]).dt.tz_localize(None).dt.normalize()
        bars["raw"] = raw_symbols(client, bars, start, end)
        raw = pd.concat([raw, bars[RAW_COLUMNS]] if len(raw) else [bars[RAW_COLUMNS]], ignore_index=True)
        raw = raw.drop_duplicates(["instrument_id", "date"], keep="last")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw.to_parquet(raw_path, index=False)
    return rebuild_root(root, market, raw, end, defs)


def rebuild_root(root, market, raw, end, defs=None):
    folder = OUT / "history" / root.strip()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.parquet"):
        old.unlink()
    expiries = {}
    if len(raw):
        contracts = split_contracts(root, market, raw, end, defs)
        scale = price_scale(root, market, contracts)
        expiries = {t: c[3] for t, c in contracts.items()}
        for ticker, (seg, _, _, _) in contracts.items():
            pd.DataFrame({"ticker": ticker, "date": seg["date"].to_numpy(),
                          "PX_OPEN": seg["open"].to_numpy() / scale, "PX_HIGH": seg["high"].to_numpy() / scale,
                          "PX_LOW": seg["low"].to_numpy() / scale, "PX_LAST": seg["close"].to_numpy() / scale,
                          "PX_VOLUME": seg["volume"].to_numpy(dtype=float)}).to_parquet(folder / f"{ticker}.parquet",
                                                                                         index=False)
    return contract_table(root, market, folder, end, expiries)


def contract_table(root, market, folder, data_end, expiries=None):
    """Contract details for every saved contract of the root, with last trade and roll dates."""
    histories = {p.stem: pd.read_parquet(p) for p in sorted(folder.glob("*.parquet"))}
    expiry = yahoo_expiries()
    rows = []
    for ticker, h in histories.items():
        code, yy = ticker[len(market.yahoo)], int(ticker[len(market.yahoo) + 1:])
        delivery = pd.Timestamp(year=2000 + yy, month=MONTH_CODES.index(code) + 1, day=1)
        last = h["date"].max()
        live = delivery + pd.offsets.MonthEnd(1) >= pd.Timestamp(data_end)  # far contracts can be quiet for months
        exact = (expiries or {}).get(ticker, pd.NaT)
        known = expiry.get(f"{ticker[:-2]}{yy:02d}.{market.exchange}")
        if pd.notna(exact):  # from Databento's definition
            ltd = exact
            live = exact >= pd.Timestamp(data_end)
        else:
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
    ap.add_argument("--roots", nargs="+", help="only these markets, by CME symbol (LE, CL, ZM) or Bloomberg root")
    ap.add_argument("--estimate", action="store_true", help="print Databento's price and stop")
    ap.add_argument("--max-cost", type=float, default=25.0, help="stop if the request costs more (USD)")
    ap.add_argument("--yes", action="store_true", help="download even above --max-cost")
    ap.add_argument("--since", type=date.fromisoformat,
                    help="download from this date (YYYY-MM-DD) instead of the last run, e.g. to fill a gap")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild contract files from the saved raw bars (only definitions are fetched, cents)")
    args = ap.parse_args()
    sectors = {m.sector for m in MARKETS.values()} if args.sectors == ["all"] else set(args.sectors)
    roots = [r for r, m in MARKETS.items() if m.sector in sectors]
    if args.roots:
        names = {n.strip().upper() for n in args.roots}
        roots = [r for r, m in MARKETS.items() if r.strip() in names or m.yahoo in names]
        unknown = names - {r.strip() for r in roots} - {MARKETS[r].yahoo for r in roots}
        if unknown:
            ap.error(f"Unknown markets {sorted(unknown)}; use CME symbols such as "
                     f"{', '.join(m.yahoo for m in list(MARKETS.values())[:6])}")
    if not roots:
        ap.error(f"No markets in sectors {sorted(sectors)}")

    client, state = connect(), load_state()
    if args.rebuild:
        rebuild(client, roots, state)
        return
    available = client.metadata.get_dataset_range(dataset=DATASET)
    end = min(pd.Timestamp(available["end"]).tz_localize(None).date(), date.today())
    starts = {r: max(FIRST_DAY, date.fromisoformat(state["through"][r.strip()]) - timedelta(days=7))
              if r.strip() in state["through"] else FIRST_DAY for r in roots}
    if args.since:
        starts = {r: max(FIRST_DAY, args.since) for r in roots}
    schema = pick_schema(client, state, outrights(MARKETS[roots[0]]), min(starts.values()), end)
    costs = {r: client.metadata.get_cost(dataset=DATASET, symbols=outrights(MARKETS[r]), stype_in="raw_symbol",
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
    print("Contract definitions (monthly samples, a few cents)...")
    defs = update_definitions(client, roots, end)
    tables = []
    for r in roots:
        table = update_root(client, r, MARKETS[r], schema, starts[r], end, defs[defs["root"] == r])
        state["through"][r.strip()] = str(end)
        save_state(state)
        if not table.empty:
            tables.append(table)
        print(f"  {r.strip():4s} {MARKETS[r].name:24s} {len(table):4d} contracts")
    save_contracts(tables)


def rebuild(client, roots, state):
    roots = [r for r in roots if (OUT / "raw" / f"{r.strip()}.parquet").exists()]
    if not roots:
        raise SystemExit(f"No saved raw bars under {OUT / 'raw'}; run without --rebuild first")
    end = max(date.fromisoformat(v) for v in state["through"].values()) if state["through"] else date.today()
    defs = update_definitions(client, roots, end)
    tables = []
    for r in roots:
        raw = pd.read_parquet(OUT / "raw" / f"{r.strip()}.parquet")
        table = rebuild_root(r, MARKETS[r], raw, end, defs[defs["root"] == r])
        tables.append(table)
        print(f"  {r.strip():4s} {MARKETS[r].name:24s} {len(table):4d} contracts (rebuilt)")
    save_contracts(tables)


def save_contracts(tables):
    tables = [t for t in tables if not t.empty]
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
