"""Resumable bulk collector for Bloomberg Desktop API data.

Collects, for every company in the configured indices:
  - reference snapshot (identifiers, classification, all agency ratings/outlooks/dates, CDS tickers,
    current market and credit metrics), stored per run date so rating history accumulates over time
  - annual and quarterly fundamentals (statements + ratios) for the longest history available
  - daily prices / market cap / total return index for the longest history available
  - daily 5Y and 10Y CDS spreads where Bloomberg has a CDS curve

Work is done tier by tier (1 = crossover names, 2 = SPX + SXXP, 3 = everything else) and every
batch is saved immediately, so the run can be interrupted and resumed. If Bloomberg reports a
daily/monthly data limit the script saves its progress and exits with code 3; rerun it later.

Usage:
    python bbg_collect.py                    # collect everything not yet collected
    python bbg_collect.py --tiers 1 2        # only the highest-priority names
    python bbg_collect.py --max-requests 200 # cap the number of API requests this run
    python bbg_collect.py --summary-only     # rebuild data/summary.xlsx from what is stored
"""
import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import blpapi
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state" / "progress.json"
LOG_FILE = ROOT / "logs" / "collect.log"
CROSSOVER_FILE = ROOT / "crossover_issuers.xlsx"
TODAY = date.today().strftime("%Y%m%d")

# index -> tier (lower tier = collected first)
INDICES = {
    "SPX Index": 2, "SXXP Index": 2,
    "RAY Index": 3, "BE500 Index": 3, "SPTSX Index": 3, "NKY Index": 3, "AS51 Index": 3, "HSI Index": 3,
}

REFERENCE_FIELDS = """
NAME LONG_COMP_NAME TICKER EXCH_CODE ID_ISIN ID_BB_GLOBAL ID_BB_COMPANY ID_BB_ULTIMATE_PARENT_CO
ULT_PARENT_TICKER_EXCHANGE GICS_SECTOR_NAME GICS_INDUSTRY_GROUP_NAME GICS_INDUSTRY_NAME GICS_SUB_INDUSTRY_NAME
INDUSTRY_SECTOR INDUSTRY_GROUP INDUSTRY_SUBGROUP COUNTRY_ISO CNTRY_OF_DOMICILE CNTRY_OF_RISK CRNCY EQY_FUND_CRNCY
EQY_FISCAL_YR_END MARKET_STATUS
RTG_SP_LT_LC_ISSUER_CREDIT RTG_SP_LT_LC_ISS_CRED_RTG_DT RTG_SP_LT_FC_ISSUER_CREDIT RTG_SP_LT_FC_ISS_CRED_RTG_DT
RTG_SP_OUTLOOK RTG_SP_OUTLOOK_DT RTG_SP_LT_OUTLOOK RTG_SP_LT_OUTLOOK_DT RTG_SP_ST_LC_ISSUER_CREDIT
RTG_MOODY_LONG_TERM RTG_MOODY_LONG_TERM_DATE RTG_MDY_ISSUER RTG_MDY_ISSUER_RTG_DT RTG_MDY_SEN_UNSECURED_DEBT
RTG_MDY_LT_CORP_FAMILY RTG_MDY_OUTLOOK RTG_MDY_OUTLOOK_DT
RTG_FITCH_LT_ISSUER_DEFAULT RTG_FITCH_LT_ISSUER_DFLT_RTG_DT RTG_FITCH_SEN_UNSECURED RTG_FITCH_OUTLOOK RTG_FITCH_OUTLOOK_DT
RTG_DBRS_LT_ISSUER_RATING RTG_DBRS_OUTLOOK RTG_SCOPE_ISSUER_CREDIT_STR RTG_KBRA RTG_KBRA_OUTLOOK
RTG_ISSUER_CUR_REVISION IS_RTG_SENSITIVE
CDS_SPREAD_TICKER_1Y CDS_SPREAD_TICKER_3Y CDS_SPREAD_TICKER_5Y CDS_SPREAD_TICKER_7Y CDS_SPREAD_TICKER_10Y
BB_1YR_DEFAULT_PROB DEFAULT_PROB_1Y BB_DEFAULT_RISK_RATING
PX_LAST CUR_MKT_CAP EQY_SH_OUT ENTERPRISE_VALUE VOLATILITY_30D VOLATILITY_260D EQY_BETA EQY_DVD_YLD_12M
""".split()

FUND_STATEMENTS = """
SALES_REV_TURN GROSS_PROFIT EBITDA EBIT IS_OPER_INC NET_INCOME IS_INT_EXPENSE IS_EPS IS_DILUTED_EPS
BS_TOT_ASSET BS_TOT_LIAB2 TOTAL_EQUITY TOT_COMMON_EQY BS_CASH_NEAR_CASH_ITEM SHORT_AND_LONG_TERM_DEBT NET_DEBT
BS_ST_BORROW BS_LT_BORROW BS_CUR_ASSET_REPORT BS_CUR_LIAB BS_INVENTORIES BS_ACCT_NOTE_RCV BS_ACCT_PAYABLE
BS_GOODWILL BS_NET_FIX_ASSET BS_RETAIN_EARN WORKING_CAPITAL BS_SH_OUT
IS_COG_AND_SERVICES_SOLD IS_SGA_EXPENSE IS_INC_TAX_EXP IS_INC_BEF_XO_ITEM
CF_CASH_FROM_OPER CAPITAL_EXPEND CF_FREE_CASH_FLOW CF_DVD_PAID CF_DEPR_AMORT CF_NET_CHNG_CASH
CF_INCR_LT_BORROW CF_REIMB_LT_BORROW CF_INCR_CAP_STOCK CF_DECR_CAP_STOCK
NUM_OF_EMPLOYEES EQY_DPS
""".split()

FUND_RATIOS = """
NET_DEBT_TO_EBITDA TOT_DEBT_TO_EBITDA TOT_DEBT_TO_TOT_EQY TOT_DEBT_TO_TOT_CAP TOT_DEBT_TO_TOT_ASSET
TOT_DEBT_TO_COM_EQY NET_DEBT_TO_SHRHLDR_EQY COM_EQY_TO_TOT_ASSET CFO_TO_TOT_DEBT
EBITDA_TO_INTEREST_EXPN INTEREST_COVERAGE_RATIO EBIT_TO_INT_EXP
CUR_RATIO QUICK_RATIO CASH_RATIO
RETURN_COM_EQY RETURN_ON_ASSET RETURN_ON_INV_CAPITAL
GROSS_MARGIN EBITDA_MARGIN OPER_MARGIN PROF_MARGIN
ALTMAN_Z_SCORE ASSET_TURNOVER INVENT_TURN ACCT_RCV_TURN DAYS_SALES_OUTSTANDING DVD_PAYOUT_RATIO
PE_RATIO PX_TO_BOOK_RATIO EV_TO_T12M_EBITDA EV_TO_T12M_SALES FREE_CASH_FLOW_YIELD ENTERPRISE_VALUE CUR_MKT_CAP
""".split()

PRICE_FIELDS = "PX_OPEN PX_HIGH PX_LOW PX_LAST PX_VOLUME CUR_MKT_CAP TOT_RETURN_INDEX_GROSS_DVDS".split()
CDS_TENORS = {"5Y": "CDS_SPREAD_TICKER_5Y", "10Y": "CDS_SPREAD_TICKER_10Y"}
MAX_HIST_FIELDS = 25

HISTORY_DATASETS = {
    "fundamentals_annual": dict(fields=FUND_STATEMENTS + FUND_RATIOS, periodicity="YEARLY", fiscal=True,
                                start="19500101", batch=25),
    "fundamentals_quarterly": dict(fields=FUND_STATEMENTS + FUND_RATIOS, periodicity="QUARTERLY", fiscal=True,
                                   start="19500101", batch=10),
    "prices_daily": dict(fields=PRICE_FIELDS, periodicity="DAILY", fiscal=False, start="19500101", batch=5,
                         extra={"adjustmentNormal": True, "adjustmentAbnormal": True, "adjustmentSplit": True}),
}

log = logging.getLogger("bbg")


class LimitReached(Exception):
    pass


class BudgetExhausted(Exception):
    pass


class Bloomberg:
    def __init__(self, max_requests=None):
        opts = blpapi.SessionOptions()
        opts.setServerHost("localhost")
        opts.setServerPort(8194)
        self.session = blpapi.Session(opts)
        if not self.session.start():
            raise SystemExit("Cannot start Bloomberg session (is the Terminal running and logged in?)")
        for svc in ("//blp/refdata", "//blp/apiflds"):
            if not self.session.openService(svc):
                raise SystemExit(f"Cannot open {svc}")
        self.refdata = self.session.getService("//blp/refdata")
        self.apiflds = self.session.getService("//blp/apiflds")
        self.max_requests = max_requests
        self.requests = 0

    def stop(self):
        self.session.stop()

    @staticmethod
    def _check_limit(element):
        txt = str(element)
        if 'category = "LIMIT"' in txt or "CAPACITY" in txt.upper():
            raise LimitReached(txt[:800])

    def _send(self, request, timeout=900):
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise BudgetExhausted(f"request budget of {self.max_requests} used")
        self.requests += 1
        self.session.sendRequest(request)
        t0 = time.time()
        msgs = []
        while True:
            ev = self.session.nextEvent(5000)
            et = ev.eventType()
            if et == blpapi.Event.TIMEOUT:
                if time.time() - t0 > timeout:
                    raise TimeoutError("Bloomberg request timed out")
                continue
            for m in ev:
                if et == blpapi.Event.REQUEST_STATUS or m.messageType() == blpapi.Name("RequestFailure"):
                    self._check_limit(m)
                    raise RuntimeError(f"Request failed: {m}")
                if et in (blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE):
                    if m.hasElement("responseError"):
                        self._check_limit(m.getElement("responseError"))
                        raise RuntimeError(f"Response error: {m.getElement('responseError')}")
                    msgs.append(m)
            if et == blpapi.Event.RESPONSE:
                return msgs

    def field_info(self, fields):
        req = self.apiflds.createRequest("FieldInfoRequest")
        for f in dict.fromkeys(fields):
            req.append("id", f)
        req.set("returnFieldDocumentation", False)
        info = {}
        for m in self._send(req):
            for fd in m.getElement("fieldData").values():
                if fd.hasElement("fieldError"):
                    continue
                fi = fd.getElement("fieldInfo")
                info[fi.getElementAsString("mnemonic")] = {
                    "description": fi.getElementAsString("description"),
                    "ftype": fi.getElementAsString("ftype"),
                }
        return info

    def index_members(self, index):
        req = self.refdata.createRequest("ReferenceDataRequest")
        req.append("securities", index)
        for f in ("INDX_MEMBERS", "INDX_MEMBERS2", "INDX_MEMBERS3"):
            req.append("fields", f)
        out = []
        for m in self._send(req):
            for sd in m.getElement("securityData").values():
                fd = sd.getElement("fieldData")
                for f in ("INDX_MEMBERS", "INDX_MEMBERS2", "INDX_MEMBERS3"):
                    if fd.hasElement(f):
                        out += [row.getElement(0).getValueAsString() + " Equity" for row in fd.getElement(f).values()]
        return out

    def reference(self, securities, fields):
        req = self.refdata.createRequest("ReferenceDataRequest")
        for s in securities:
            req.append("securities", s)
        for f in fields:
            req.append("fields", f)
        rows, invalid = [], []
        for m in self._send(req):
            for sd in m.getElement("securityData").values():
                sec = sd.getElementAsString("security")
                if sd.hasElement("securityError"):
                    self._check_limit(sd.getElement("securityError"))
                    invalid.append(sec)
                    continue
                if sd.getElement("fieldExceptions").numValues():
                    self._check_limit(sd.getElement("fieldExceptions"))
                fd = sd.getElement("fieldData")
                row = {"ticker": sec}
                for f in fields:
                    if fd.hasElement(f) and not fd.getElement(f).isArray():
                        row[f] = fd.getElement(f).getValue()
                rows.append(row)
        return pd.DataFrame(rows), invalid

    def history(self, securities, fields, start, periodicity, fiscal=False, extra=None):
        req = self.refdata.createRequest("HistoricalDataRequest")
        for s in securities:
            req.append("securities", s)
        for f in fields:
            req.append("fields", f)
        req.set("startDate", start)
        req.set("endDate", TODAY)
        req.set("periodicitySelection", periodicity)
        if fiscal:
            req.set("periodicityAdjustment", "FISCAL")
        for k, v in (extra or {}).items():
            req.set(k, v)
        rows, invalid, not_historical = [], [], set()
        for m in self._send(req):
            sd = m.getElement("securityData")
            sec = sd.getElementAsString("security")
            if sd.hasElement("securityError"):
                self._check_limit(sd.getElement("securityError"))
                invalid.append(sec)
                continue
            for fe in sd.getElement("fieldExceptions").values():
                info = fe.getElement("errorInfo")
                self._check_limit(info)
                if "historical" in info.getElementAsString("message").lower():
                    not_historical.add(fe.getElementAsString("fieldId"))
            data = sd.getElement("fieldData")
            for i in range(data.numValues()):
                point = data.getValueAsElement(i)
                row = {"ticker": sec}
                for j in range(point.numElements()):
                    e = point.getElement(j)
                    row[str(e.name())] = e.getValue()
                rows.append(row)
        return pd.DataFrame(rows), invalid, not_historical


class State:
    def __init__(self):
        raw = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        self.done = {k: set(v) for k, v in raw.get("done", {}).items()}
        self.invalid = set(raw.get("invalid", []))
        self.not_historical = set(raw.get("not_historical", []))

    def is_done(self, dataset, ticker):
        return ticker in self.invalid or ticker in self.done.get(dataset, set())

    def mark(self, dataset, tickers):
        self.done.setdefault(dataset, set()).update(tickers)
        self.save()

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "done": {k: sorted(v) for k, v in self.done.items()},
            "invalid": sorted(self.invalid),
            "not_historical": sorted(self.not_historical),
        }, indent=1))
        tmp.replace(STATE_FILE)


def clean(df):
    """Make object columns Parquet-safe: a column mixing types is stored as strings."""
    for col in df.columns[df.dtypes == object]:
        types = {type(v) for v in df[col].dropna()}
        if len(types) > 1:
            df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v))
    return df


def write_part(dataset, df):
    if df.empty:
        return
    folder = DATA / dataset
    folder.mkdir(parents=True, exist_ok=True)
    clean(df).to_parquet(folder / f"part-{time.time_ns()}.parquet", index=False)


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def build_universe(bbg):
    members = {}
    if CROSSOVER_FILE.exists():
        for t in pd.read_excel(CROSSOVER_FILE, sheet_name="All crossover")["Ticker"]:
            members.setdefault(t, {"tier": 1, "indices": set()})["indices"].add("CROSSOVER")
    for index, tier in INDICES.items():
        tickers = bbg.index_members(index)
        log.info("%s: %d members", index, len(tickers))
        for t in tickers:
            entry = members.setdefault(t, {"tier": tier, "indices": set()})
            entry["tier"] = min(entry["tier"], tier)
            entry["indices"].add(index.replace(" Index", ""))
    uni = pd.DataFrame(
        [{"ticker": t, "tier": v["tier"], "indices": ",".join(sorted(v["indices"]))} for t, v in members.items()]
    ).sort_values(["tier", "ticker"])
    DATA.mkdir(parents=True, exist_ok=True)
    uni.to_parquet(DATA / "universe.parquet", index=False)
    log.info("Universe: %d unique tickers (%s)", len(uni), uni["tier"].value_counts().sort_index().to_dict())
    return uni


def validate_fields(bbg):
    wanted = REFERENCE_FIELDS + FUND_STATEMENTS + FUND_RATIOS + PRICE_FIELDS + ["PX_LAST"]
    info = bbg.field_info(wanted)
    dropped = sorted(set(wanted) - set(info))
    if dropped:
        log.warning("Fields unknown to Bloomberg, skipped: %s", ", ".join(dropped))
    pd.DataFrame([{"field": k, **v} for k, v in info.items()]).to_parquet(DATA / "fields.parquet", index=False)
    return set(info)


def collect_reference(bbg, state, tickers, fields):
    dataset = f"reference/snapshot_{TODAY}"
    todo = [t for t in tickers if not state.is_done(dataset, t)]
    for batch in chunks(todo, 100):
        df, invalid = bbg.reference(batch, fields)
        df.insert(1, "snapshot_date", pd.Timestamp(TODAY))
        write_part(dataset, df)
        state.invalid.update(invalid)
        state.mark(dataset, batch)
        log.info("reference: +%d (invalid %d)", len(batch), len(invalid))


def collect_history(bbg, state, tickers, name, cfg, valid):
    fields = [f for f in dict.fromkeys(cfg["fields"]) if f in valid and f not in state.not_historical]
    todo = [t for t in tickers if not state.is_done(name, t)]
    for n, batch in enumerate(chunks(todo, cfg["batch"]), 1):
        merged = None
        for group in chunks(fields, MAX_HIST_FIELDS):
            df, invalid, not_hist = bbg.history(batch, group, cfg["start"], cfg["periodicity"],
                                                cfg["fiscal"], cfg.get("extra"))
            state.invalid.update(invalid)
            if not_hist:
                log.warning("%s: not available historically, dropped: %s", name, ", ".join(sorted(not_hist)))
                state.not_historical.update(not_hist)
            if not df.empty:
                merged = df if merged is None else merged.merge(df, on=["ticker", "date"], how="outer")
        if merged is not None:
            # Bloomberg pads periods before the first report with date-only rows
            merged = merged.dropna(how="all", subset=[c for c in merged.columns if c not in ("ticker", "date")])
            merged["date"] = pd.to_datetime(merged["date"])
            write_part(name, merged)
        state.mark(name, batch)
        fields = [f for f in fields if f not in state.not_historical]
        log.info("%s: batch %d/%d, %d rows", name, n, -(-len(todo) // cfg["batch"]),
                 0 if merged is None else len(merged))


def latest_snapshot():
    snaps = sorted((DATA / "reference").glob("snapshot_*"))
    return pd.read_parquet(snaps[-1]) if snaps else pd.DataFrame()


def collect_cds(bbg, state, tickers):
    snap = latest_snapshot()
    if snap.empty:
        return
    snap = snap[snap["ticker"].isin(tickers)]
    todo = [t for t in snap["ticker"] if not state.is_done("cds_daily", t)]
    snap = snap.set_index("ticker")
    for batch in chunks(todo, 10):
        targets = {}
        for t in batch:
            for tenor, col in CDS_TENORS.items():
                if col in snap.columns and isinstance(snap.at[t, col], str):
                    targets[snap.at[t, col] + " Corp"] = (t, tenor)
        if targets:
            df, _, _ = bbg.history(list(targets), ["PX_LAST"], "19900101", "DAILY")
            if not df.empty:
                df["cds_ticker"] = df["ticker"]
                df["tenor"] = df["cds_ticker"].map(lambda c: targets[c][1])
                df["ticker"] = df["cds_ticker"].map(lambda c: targets[c][0])
                df["date"] = pd.to_datetime(df["date"])
                write_part("cds_daily", df.rename(columns={"PX_LAST": "spread_bp"}))
        state.mark("cds_daily", batch)
        log.info("cds_daily: +%d issuers, %d curves", len(batch), len(targets))


def coverage(dataset):
    folder = DATA / dataset
    if not folder.exists():
        return pd.DataFrame()
    table = pads.dataset(folder, format="parquet").to_table(columns=["ticker", "date"])
    agg = table.group_by("ticker").aggregate([("date", "min"), ("date", "max"), ("date", "count")]).to_pandas()
    agg.columns = ["ticker", f"{dataset}_first", f"{dataset}_last", f"{dataset}_rows"]
    return agg.set_index("ticker")


def build_summary():
    uni = pd.read_parquet(DATA / "universe.parquet").set_index("ticker")
    snap = latest_snapshot()
    cov = uni.join([coverage(d) for d in [*HISTORY_DATASETS, "cds_daily"] if (DATA / d).exists()])
    out = DATA / "summary.xlsx"
    with pd.ExcelWriter(out, engine="xlsxwriter") as xl:
        if not snap.empty:
            snap.to_excel(xl, sheet_name="Latest snapshot", index=False)
        cov.reset_index().to_excel(xl, sheet_name="Coverage", index=False)
        if (DATA / "fields.parquet").exists():
            pd.read_parquet(DATA / "fields.parquet").to_excel(xl, sheet_name="Fields", index=False)
        for ws in xl.sheets.values():
            ws.freeze_panes(1, 1)
    log.info("Summary written to %s", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tiers", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    if args.summary_only:
        build_summary()
        return

    bbg = Bloomberg(args.max_requests)
    state = State()
    code = 0
    try:
        valid = validate_fields(bbg)
        uni = build_universe(bbg)
        ref_fields = [f for f in REFERENCE_FIELDS if f in valid]
        for tier in sorted(args.tiers):
            tickers = uni.loc[uni["tier"] == tier, "ticker"].tolist()
            log.info("=== Tier %d: %d tickers ===", tier, len(tickers))
            collect_reference(bbg, state, tickers, ref_fields)
            for name, cfg in HISTORY_DATASETS.items():
                collect_history(bbg, state, tickers, name, cfg, valid)
            collect_cds(bbg, state, tickers)
        log.info("Collection complete (%d requests this run)", bbg.requests)
    except LimitReached as e:
        log.error("Bloomberg data limit reached, progress saved; rerun later to resume.\n%s", e)
        code = 3
    except BudgetExhausted as e:
        log.warning("Stopping: %s. Progress saved; rerun to continue.", e)
    except KeyboardInterrupt:
        log.warning("Interrupted, progress saved.")
        code = 130
    finally:
        state.save()
        bbg.stop()
    build_summary()
    sys.exit(code)


if __name__ == "__main__":
    main()
