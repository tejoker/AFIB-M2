"""Collect daily prices of individual futures contracts, expired ones included, for roll-correct research.

Yahoo's continuous "=F" series jump whenever the front contract changes, which fakes or hides seasonal and trend
signals (lean hogs in October is the textbook case). This script downloads every contract separately, so
cme_challenge/continuous.py can chain returns without ever crossing a roll.

For each root, in priority order (live cattle, then other commodities, then financials):
  1. FUT_CHAIN including expired contracts, cached for a week in <data>/futures/chains/
  2. contract details (last trade date, first notice day, point value, tick) -> <data>/futures/contracts.parquet
  3. daily history over the months before each contract's expiry -> <data>/futures/history/<root>/<contract>.parquet

Expired contracts are fetched once; contracts still trading are refreshed on every run. Progress is saved after
each request, so the script resumes after Ctrl+C, a crash or a Bloomberg limit (exit code 3: rerun later or from
another account). <data> is the same folder as the main collector's (see data_root.py); with OneDrive, run one
collector at a time.

Usage (from the repository root, with the Bloomberg Terminal logged in):
    python bloomberg/futures_contracts.py --status        # progress and remaining requests, no API use
    python bloomberg/futures_contracts.py --roots LC      # live cattle only (start here)
    python bloomberg/futures_contracts.py                 # everything, in priority order
    python bloomberg/futures_contracts.py --max-requests 50
"""
import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from bbg_collect import (DATA, LOGS, Bloomberg, BudgetExhausted, LimitReached, chunks, clean, safe_name, write_json,
                         write_parquet)

FUT = DATA / "futures"
CHAINS = FUT / "chains"
HISTORY = FUT / "history"
CONTRACTS = FUT / "contracts.parquet"
STATE_FILE = FUT / "state.json"
CHAIN_MAX_AGE = timedelta(days=7)
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan ... Dec
ALL_MONTHS = MONTH_CODES

# Bloomberg root (exactly as in its tickers, e.g. "C " for corn): yellow key, liquid contract months (other listed
# months are thin and skipped), name, sector, collection tier. The challenge allows agriculture, energy, equity
# index, FX, interest rate and metals futures; micro contracts move like their full-size versions.
ROOTS = {
    "LC": ("Comdty", "GJMQVZ", "Live Cattle", "Livestock", 1),
    "LH": ("Comdty", "GJKMNQVZ", "Lean Hogs", "Livestock", 2),
    "FC": ("Comdty", "FHJKQUVX", "Feeder Cattle", "Livestock", 2),
    "C ": ("Comdty", "HKNUZ", "Corn", "Grains", 2),
    "S ": ("Comdty", "FHKNQUX", "Soybeans", "Grains", 2),
    "SM": ("Comdty", "FHKNQUVZ", "Soybean Meal", "Grains", 2),
    "BO": ("Comdty", "FHKNQUVZ", "Soybean Oil", "Grains", 2),
    "W ": ("Comdty", "HKNUZ", "Chicago SRW Wheat", "Grains", 2),
    "KW": ("Comdty", "HKNUZ", "KC HRW Wheat", "Grains", 2),
    "CL": ("Comdty", ALL_MONTHS, "WTI Crude Oil", "Energy", 2),
    "NG": ("Comdty", ALL_MONTHS, "Henry Hub Natural Gas", "Energy", 2),
    "HO": ("Comdty", ALL_MONTHS, "NY Harbor ULSD", "Energy", 2),
    "XB": ("Comdty", ALL_MONTHS, "RBOB Gasoline", "Energy", 2),
    "GC": ("Comdty", "GJMQVZ", "Gold", "Metals", 2),
    "SI": ("Comdty", "HKNUZ", "Silver", "Metals", 2),
    "HG": ("Comdty", "HKNUZ", "Copper", "Metals", 2),
    "PL": ("Comdty", "FJNV", "Platinum", "Metals", 2),
    "PA": ("Comdty", "HMUZ", "Palladium", "Metals", 2),
    "ES": ("Index", "HMUZ", "E-mini S&P 500", "Equity index", 3),
    "NQ": ("Index", "HMUZ", "E-mini Nasdaq-100", "Equity index", 3),
    "RTY": ("Index", "HMUZ", "E-mini Russell 2000", "Equity index", 3),
    "DM": ("Index", "HMUZ", "E-mini Dow", "Equity index", 3),
    "TU": ("Comdty", "HMUZ", "2Y T-Note", "Rates", 3),
    "FV": ("Comdty", "HMUZ", "5Y T-Note", "Rates", 3),
    "TY": ("Comdty", "HMUZ", "10Y T-Note", "Rates", 3),
    "US": ("Comdty", "HMUZ", "T-Bond", "Rates", 3),
    "WN": ("Comdty", "HMUZ", "Ultra T-Bond", "Rates", 3),
    "SFR": ("Comdty", "HMUZ", "3M SOFR", "Rates", 3),
    "EC": ("Curncy", "HMUZ", "Euro FX", "FX", 3),
    "JY": ("Curncy", "HMUZ", "Japanese Yen", "FX", 3),
    "BP": ("Curncy", "HMUZ", "British Pound", "FX", 3),
    "AD": ("Curncy", "HMUZ", "Australian Dollar", "FX", 3),
    "CD": ("Curncy", "HMUZ", "Canadian Dollar", "FX", 3),
    "SF": ("Curncy", "HMUZ", "Swiss Franc", "FX", 3),
}
META_FIELDS = ["LAST_TRADEABLE_DT", "FUT_NOTICE_FIRST", "FUT_VAL_PT", "FUT_TICK_SIZE", "FUT_TICK_VAL", "CRNCY"]
HISTORY_BATCH = 10  # contracts per history request

log = logging.getLogger("futures")


class FuturesBloomberg(Bloomberg):
    def chain(self, generic):
        """Every contract ticker of a root, expired ones included."""
        req = self.refdata.createRequest("ReferenceDataRequest")
        req.append("securities", generic)
        req.append("fields", "FUT_CHAIN")
        override = req.getElement("overrides").appendElement()
        override.setElement("fieldId", "INCLUDE_EXPIRED_CONTRACTS")
        override.setElement("value", "Y")
        tickers = []
        for m in self._send(req):
            for sd in m.getElement("securityData").values():
                if sd.hasElement("securityError"):
                    self._check_limit(sd.getElement("securityError"))
                    raise RuntimeError(f"{generic}: {sd.getElement('securityError')}")
                if sd.getElement("fieldExceptions").numValues():
                    self._check_limit(sd.getElement("fieldExceptions"))
                fd = sd.getElement("fieldData")
                if fd.hasElement("FUT_CHAIN"):
                    tickers += [row.getElement(0).getValueAsString() for row in fd.getElement("FUT_CHAIN").values()]
        return tickers


def resolve_roots(names):
    by_stripped = {root.strip(): root for root in ROOTS}
    unknown = [n for n in names if n.strip().upper() not in by_stripped]
    if unknown:
        raise SystemExit(f"Unknown roots {unknown}; choose from {sorted(by_stripped)}")
    return [by_stripped[n.strip().upper()] for n in names]


def chain_path(root):
    return CHAINS / f"{safe_name(root.strip())}.json"


def history_path(root, ticker):
    return HISTORY / safe_name(root.strip()) / f"{safe_name(ticker)}.parquet"


def candidate_tickers(root, tickers, start_year):
    """Liquid contract months of this root, dropping contracts whose two-digit year is before start_year.

    Bloomberg writes expired contracts with a two-digit year (LCZ05) and live ones with one digit (LCZ6); the exact
    expiry comes from the contract details, which are filtered again afterwards.
    """
    key, months = ROOTS[root][0], ROOTS[root][1]
    pattern = re.compile(rf"^{re.escape(root)}([{MONTH_CODES}])(\d{{1,2}}) {key}$")
    keep = []
    for ticker in tickers:
        m = pattern.match(ticker)
        if not m or m.group(1) not in months:
            continue
        if len(m.group(2)) == 2:
            yy = int(m.group(2))
            if (1900 + yy if yy >= 50 else 2000 + yy) < start_year:
                continue
        keep.append(ticker)
    return keep


def delivery_month(month_code, last_trade):
    """Contract (delivery) month as a Timestamp; January crude oil, for example, stops trading in December."""
    month = MONTH_CODES.index(month_code) + 1
    year = last_trade.year + (1 if month < last_trade.month - 2 else 0)
    return pd.Timestamp(year=year, month=month, day=1)


def load_state():
    if STATE_FILE.exists():
        raw = json.loads(STATE_FILE.read_text())
        return {"complete": set(raw.get("complete", [])), "empty": set(raw.get("empty", []))}
    return {"complete": set(), "empty": set()}


def save_state(state):
    write_json(STATE_FILE, {k: sorted(v) for k, v in state.items()})


def load_contracts():
    return pd.read_parquet(CONTRACTS) if CONTRACTS.exists() else pd.DataFrame()


def update_chain(bbg, root, refresh):
    path = chain_path(root)
    if path.exists() and not refresh:
        cached = json.loads(path.read_text())
        if datetime.now() - datetime.fromisoformat(cached["fetched_at"]) < CHAIN_MAX_AGE:
            return cached["tickers"]
    tickers = bbg.chain(f"{root}1 {ROOTS[root][0]}")
    if not tickers:  # not cached, so the next run asks again
        log.warning("%s: Bloomberg returned no contracts for %s1 %s", root.strip(), root, ROOTS[root][0])
        return []
    write_json(path, {"root": root, "fetched_at": datetime.now().isoformat(timespec="seconds"), "tickers": tickers})
    log.info("%s: chain has %d contracts", root.strip(), len(tickers))
    return tickers


def update_contracts(bbg, root, tickers, contracts):
    """Fetch details for contracts not seen before; returns the updated contracts table."""
    known = set(contracts["ticker"]) if not contracts.empty else set()
    missing = [t for t in tickers if t not in known]
    if not missing:
        return contracts
    key, _, name, sector, tier = ROOTS[root]
    frames = []
    for batch in chunks(missing, 100):
        df, invalid = bbg.reference(batch, META_FIELDS)
        if invalid:
            log.info("%s: Bloomberg does not know %d contracts, e.g. %s", root.strip(), len(invalid), invalid[:3])
        frames.append(df)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty or "LAST_TRADEABLE_DT" not in df:
        return contracts
    df = df.dropna(subset=["LAST_TRADEABLE_DT"]).copy()
    df["LAST_TRADEABLE_DT"] = pd.to_datetime(df["LAST_TRADEABLE_DT"])
    if "FUT_NOTICE_FIRST" in df:
        df["FUT_NOTICE_FIRST"] = pd.to_datetime(df["FUT_NOTICE_FIRST"], errors="coerce")
    df["root"], df["name"], df["sector"], df["tier"] = root, name, sector, tier
    df["month_code"] = [re.match(rf"^{re.escape(root)}([{MONTH_CODES}])", t).group(1) for t in df["ticker"]]
    df["delivery"] = [delivery_month(m, ltd) for m, ltd in zip(df["month_code"], df["LAST_TRADEABLE_DT"])]
    contracts = pd.concat([contracts, clean(df)], ignore_index=True) if not contracts.empty else clean(df)
    write_parquet(CONTRACTS, contracts)
    return contracts


def pending(contracts, roots, state, start_year, lookback, today):
    """Contracts still to fetch: in range, already trading inside their window, and not complete or empty."""
    if contracts.empty:
        return contracts
    c = contracts[contracts["root"].isin(roots)].copy()
    c = c[c["LAST_TRADEABLE_DT"] >= pd.Timestamp(year=start_year, month=1, day=1)]
    c = c[c["LAST_TRADEABLE_DT"] - pd.Timedelta(days=lookback) <= pd.Timestamp(today)]
    done = state["complete"] | state["empty"]
    return c[~c["ticker"].isin(done)]


def request_groups(todo, roots):
    """Batches of contracts expiring in the same month (similar windows, little wasted data), in tier order."""
    order = {root: i for i, root in enumerate(roots)}
    todo = todo.assign(_tier=todo["tier"], _order=todo["root"].map(order),
                       _month=todo["LAST_TRADEABLE_DT"].dt.to_period("M"))
    groups = []
    for _, tier_rows in todo.sort_values(["_tier", "_month", "_order"]).groupby("_tier", sort=True):
        for _, month_rows in tier_rows.groupby("_month", sort=True):
            groups += list(chunks(month_rows.to_dict("records"), HISTORY_BATCH))
    return groups


def fetch_group(bbg, group, fields, lookback, today, state):
    start = min(c["LAST_TRADEABLE_DT"] for c in group) - timedelta(days=lookback)
    end = min(max(c["LAST_TRADEABLE_DT"] for c in group).date(), today)
    tickers = [c["ticker"] for c in group]
    df, invalid, _ = bbg.history(tickers, fields, start.strftime("%Y%m%d"), "DAILY",
                                 extra={"endDate": end.strftime("%Y%m%d")})
    rows = dict(tuple(df.groupby("ticker"))) if not df.empty else {}
    for c in group:
        ticker, expired = c["ticker"], c["LAST_TRADEABLE_DT"].date() <= today
        data = rows.get(ticker)
        if data is not None and not data.empty:
            data = data.assign(date=pd.to_datetime(data["date"])).sort_values("date")
            write_parquet(history_path(c["root"], ticker), clean(data.reset_index(drop=True)))
            if expired:
                state["complete"].add(ticker)
        elif ticker in invalid or c["LAST_TRADEABLE_DT"].date() < today - timedelta(days=7):
            state["empty"].add(ticker)
    save_state(state)
    log.info("%s..%s: %d contracts, %d rows (%d requests this run)", start.date(), end, len(group), len(df),
             bbg.requests)


def status(roots, start_year, lookback, today):
    contracts, state = load_contracts(), load_state()
    rows = []
    for root in roots:
        c = pending(contracts, [root], {"complete": set(), "empty": set()}, start_year, lookback, today)
        todo = pending(contracts, [root], state, start_year, lookback, today)
        live = [t for t in todo["ticker"] if history_path(root, t).exists()] if not todo.empty else []
        rows.append({
            "root": root.strip(), "name": ROOTS[root][2], "tier": ROOTS[root][4],
            "chain": "yes" if chain_path(root).exists() else "not yet",
            "contracts": len(c), "complete": int(c["ticker"].isin(state["complete"]).sum()) if len(c) else 0,
            "no data": int(c["ticker"].isin(state["empty"]).sum()) if len(c) else 0,
            "still trading": len(live), "to fetch": len(todo) - len(live),
        })
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    todo = pending(contracts, roots, state, start_year, lookback, today)
    unchained = int((table["chain"] == "not yet").sum())
    requests = len(request_groups(todo, roots)) if not todo.empty else 0
    print(f"\nAbout {requests} history requests left for roots with known contracts"
          + (f"; {unchained} roots still need their chain and details (2+ requests each)." if unchained else "."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", nargs="+", help="Bloomberg roots, e.g. LC LH CL (default: all, in priority order)")
    ap.add_argument("--tiers", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--start-year", type=int, default=2005, help="first contract expiry year to collect")
    ap.add_argument("--lookback-days", type=int, default=240,
                    help="calendar days of history before each expiry (covers holding it and the one before)")
    ap.add_argument("--fields", nargs="+", default=["PX_LAST", "OPEN_INT"])
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--refresh-chains", action="store_true", help="refetch chains even if cached this week")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    roots = resolve_roots(args.roots) if args.roots else list(ROOTS)
    roots = sorted((r for r in roots if ROOTS[r][4] in args.tiers), key=lambda r: ROOTS[r][4])
    today = date.today()
    if args.status:
        status(roots, args.start_year, args.lookback_days, today)
        return

    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOGS / "futures_contracts.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    bbg = FuturesBloomberg(args.max_requests)
    state, contracts = load_state(), load_contracts()
    code = 0
    try:
        for root in roots:
            tickers = candidate_tickers(root, update_chain(bbg, root, args.refresh_chains), args.start_year)
            contracts = update_contracts(bbg, root, tickers, contracts)
        groups = request_groups(pending(contracts, roots, state, args.start_year, args.lookback_days, today), roots)
        log.info("%d history requests to make for %d roots", len(groups), len(roots))
        for group in groups:
            fetch_group(bbg, group, args.fields, args.lookback_days, today, state)
        log.info("Done (%d requests this run)", bbg.requests)
    except LimitReached as e:
        log.error("Bloomberg data limit reached; progress saved. Rerun later or from another account.\n%s", e)
        code = 3
    except BudgetExhausted as e:
        log.warning("Stopping: %s. Progress saved.", e)
    except KeyboardInterrupt:
        log.warning("Interrupted, progress saved.")
        code = 130
    finally:
        bbg.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
