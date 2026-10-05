"""Resumable, multi-account bulk collector for the Bloomberg Desktop API.

Several Bloomberg logins (one per machine, since the Desktop API only talks to the local
Terminal) share one data folder. Work is split into fixed batches; each account owns every
n-th batch, does its own first, then picks up unfinished batches from the others. Progress,
claims and output files are all per account, so the folder can be shared live (network drive,
OneDrive/SharePoint) or merged later by simply copying folders together - nothing conflicts.

Datasets, in priority order (mandatory ones for the credit regression come first):
  reference           ratings/outlooks/dates, identifiers, sector, country, CDS tickers (monthly round)
  fundamentals_annual statements + credit ratios, full history                      [mandatory]
  prices_daily        PX_LAST, CUR_MKT_CAP daily, full history                       [mandatory]
  fundamentals_quarterly, cds_daily, prices_daily_extra (OHLC, volume, total return)  [optional]

Setup: list the accounts once in <data>/accounts.json, e.g. {"accounts": ["alice", "bob", "carol"]}.
The account name defaults to the Windows user name; override with --account. <data> is the
folder in BBG_DATA_DIR, or data/ in the repository; it is never committed.

Usage (from the repository root):
    python bloomberg/bbg_collect.py                     # collect everything still missing
    python bloomberg/bbg_collect.py --mandatory-only    # only what the regression needs
    python bloomberg/bbg_collect.py --tiers 1 2         # restrict to priority tiers
    python bloomberg/bbg_collect.py --max-requests 200  # cap API requests this run
    python bloomberg/bbg_collect.py --status            # progress per dataset and account, no API use
    python bloomberg/bbg_collect.py --summary-only      # rebuild <data>/summary.xlsx
Exit code 3 = Bloomberg daily/monthly limit hit; the account then refuses to run for
--cooldown hours (default 12) unless --force is given.
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow.dataset as pads
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
# The data folder holds everything Bloomberg-derived plus the team's shared config, and is never
# committed. Point BBG_DATA_DIR at a shared (e.g. OneDrive) folder so all accounts see one state.
DATA = Path(os.environ.get("BBG_DATA_DIR", REPO / "data"))
LOGS = Path(os.environ.get("BBG_LOG_DIR", REPO / "logs"))
STATE = DATA / "state"
CLAIMS = STATE / "claims"
ACCOUNTS_FILE = DATA / "accounts.json"
CROSSOVER_FILE = DATA / "crossover_issuers.xlsx"
TODAY = date.today().strftime("%Y%m%d")
ROUND = date.today().strftime("%Y%m")  # reference snapshot is re-taken once per month
CLAIM_TTL = timedelta(hours=3)
# Time for a simultaneous claim by another account to become visible. Enough on a local disk or
# network drive; with OneDrive's sync delay, a rare duplicate fetch remains possible (harmless).
CLAIM_SETTLE = float(os.environ.get("BBG_CLAIM_SETTLE", 0.3))
# How long to keep coming back to batches other accounts hold before moving on (seconds)
HELD_BATCH_WAIT = 60

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
BB_1YR_DEFAULT_PROB
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
TOT_DEBT_TO_COM_EQY COM_EQY_TO_TOT_ASSET CFO_TO_TOT_DEBT
EBITDA_TO_INTEREST_EXPN INTEREST_COVERAGE_RATIO EBIT_TO_INT_EXP
CUR_RATIO QUICK_RATIO CASH_RATIO
RETURN_COM_EQY RETURN_ON_ASSET RETURN_ON_INV_CAPITAL
GROSS_MARGIN EBITDA_MARGIN OPER_MARGIN PROF_MARGIN
ALTMAN_Z_SCORE ASSET_TURNOVER INVENT_TURN ACCT_RCV_TURN DAYS_SALES_OUTSTANDING DVD_PAYOUT_RATIO
PE_RATIO PX_TO_BOOK_RATIO EV_TO_T12M_EBITDA EV_TO_T12M_SALES FREE_CASH_FLOW_YIELD ENTERPRISE_VALUE CUR_MKT_CAP
""".split()

MAX_HIST_FIELDS = 25
CDS_TENORS = {"5Y": "CDS_SPREAD_TICKER_5Y", "10Y": "CDS_SPREAD_TICKER_10Y"}

# Ordered plan: (name, kind, mandatory, batch size, config)
PLAN = [
    ("reference", "reference", True, 100, {}),
    ("fundamentals_annual", "history", True, 25,
     dict(fields=FUND_STATEMENTS + FUND_RATIOS, periodicity="YEARLY", fiscal=True, start="19500101")),
    ("prices_daily", "history", True, 5,
     dict(fields=["PX_LAST", "CUR_MKT_CAP"], periodicity="DAILY", fiscal=False, start="19500101",
          extra={"adjustmentNormal": True, "adjustmentAbnormal": True, "adjustmentSplit": True})),
    ("fundamentals_quarterly", "history", False, 10,
     dict(fields=FUND_STATEMENTS + FUND_RATIOS, periodicity="QUARTERLY", fiscal=True, start="19500101")),
    ("cds_daily", "cds", False, 10, {}),
    ("prices_daily_extra", "history", False, 5,
     dict(fields=["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_VOLUME", "TOT_RETURN_INDEX_GROSS_DVDS"],
          periodicity="DAILY", fiscal=False, start="19500101",
          extra={"adjustmentNormal": True, "adjustmentAbnormal": True, "adjustmentSplit": True})),
]

log = logging.getLogger("bbg")


class LimitReached(Exception):
    pass


class BudgetExhausted(Exception):
    pass


# --------------------------------------------------------------------------- Bloomberg session

class Bloomberg:
    def __init__(self, max_requests=None):
        import blpapi
        self.blpapi = blpapi
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
        ev_t = self.blpapi.Event
        self.session.sendRequest(request)
        t0 = time.time()
        msgs = []
        while True:
            ev = self.session.nextEvent(5000)
            et = ev.eventType()
            if et == ev_t.TIMEOUT:
                if time.time() - t0 > timeout:
                    raise TimeoutError("Bloomberg request timed out")
                continue
            for m in ev:
                if et == ev_t.REQUEST_STATUS:
                    self._check_limit(m)
                    raise RuntimeError(f"Request failed: {m}")
                if et in (ev_t.PARTIAL_RESPONSE, ev_t.RESPONSE):
                    if m.hasElement("responseError"):
                        self._check_limit(m.getElement("responseError"))
                        raise RuntimeError(f"Response error: {m.getElement('responseError')}")
                    msgs.append(m)
            if et == ev_t.RESPONSE:
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
        bulk = ("INDX_MEMBERS", "INDX_MEMBERS2", "INDX_MEMBERS3")
        for f in bulk:
            req.append("fields", f)
        out = []
        for m in self._send(req):
            for sd in m.getElement("securityData").values():
                fd = sd.getElement("fieldData")
                for f in bulk:
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


# --------------------------------------------------------------------------- shared state

def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    # Windows refuses to replace a file another account is reading at that instant: retry
    for attempt in range(40):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.25)


class State:
    """Progress of this account (written) plus all other accounts (read-only, refreshed on change)."""

    def __init__(self, account):
        self.account = account
        self.file = STATE / f"progress_{safe_name(account)}.json"
        raw = json.loads(self.file.read_text()) if self.file.exists() else {}
        self.done = {k: set(v) for k, v in raw.get("done", {}).items()}
        self.invalid = set(raw.get("invalid", []))
        self.not_historical = set(raw.get("not_historical", []))
        self._others = {}  # path -> (mtime, parsed)

    def _refresh_others(self):
        for p in STATE.glob("progress_*.json"):
            if p == self.file:
                continue
            # Windows briefly refuses reads while the owner replaces the file. Keeping the stale
            # copy would make us re-fetch batches the other account just finished, so retry.
            for attempt in range(40):
                try:
                    mtime = p.stat().st_mtime
                    if p not in self._others or self._others[p][0] != mtime:
                        raw = json.loads(p.read_text())
                        self._others[p] = (mtime, {
                            "done": {k: set(v) for k, v in raw.get("done", {}).items()},
                            "invalid": set(raw.get("invalid", [])),
                            "not_historical": set(raw.get("not_historical", [])),
                        })
                    break
                except (OSError, ValueError):
                    time.sleep(0.05)

    def all_invalid(self):
        self._refresh_others()
        out = set(self.invalid)
        for _, o in self._others.values():
            out |= o["invalid"]
        return out

    def all_not_historical(self):
        self._refresh_others()
        out = set(self.not_historical)
        for _, o in self._others.values():
            out |= o["not_historical"]
        return out

    def done_anywhere(self, key):
        self._refresh_others()
        out = set(self.done.get(key, set()))
        for _, o in self._others.values():
            out |= o["done"].get(key, set())
        return out | self.all_invalid()

    def mark(self, key, tickers):
        self.done.setdefault(key, set()).update(tickers)
        self.save()

    def save(self):
        write_json(self.file, {
            "account": self.account,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "done": {k: sorted(v) for k, v in self.done.items()},
            "invalid": sorted(self.invalid),
            "not_historical": sorted(self.not_historical),
        })


class Claims:
    """Soft locks so accounts sharing a live folder don't fetch the same batch at the same time."""

    def __init__(self, account):
        self.account = safe_name(account)
        CLAIMS.mkdir(parents=True, exist_ok=True)

    def _live_claims(self, batch_id):
        """(claim time in ns, account) for every unexpired claim on the batch, ours included."""
        out = []
        for p in CLAIMS.glob(f"{batch_id}__*.claim"):
            account = p.stem.split("__", 1)[1]
            try:
                mtime = p.stat().st_mtime
                if datetime.now() - datetime.fromtimestamp(mtime) >= CLAIM_TTL:
                    continue
                try:
                    stamp = int(p.read_text().strip())
                except (OSError, ValueError):  # being written right now
                    stamp = int(mtime * 1e9)
                out.append((stamp, account))
            except OSError:
                continue
        return out

    def claim(self, batch_id):
        """Claim a batch; True if we may fetch it. After writing our claim we wait CLAIM_SETTLE
        so that any claim written at the same moment becomes visible, then the earliest claim
        wins (ties: account name). Every account applies the same rule, so only one proceeds."""
        if any(acc != self.account for _, acc in self._live_claims(batch_id)):
            return False
        (CLAIMS / f"{batch_id}__{self.account}.claim").write_text(str(time.time_ns()))
        time.sleep(CLAIM_SETTLE)
        claims = self._live_claims(batch_id)
        if claims and min(claims)[1] != self.account:
            self.release(batch_id)
            return False
        return True

    def release(self, batch_id):
        (CLAIMS / f"{batch_id}__{self.account}.claim").unlink(missing_ok=True)


def limit_file(account):
    return STATE / f"limit_{safe_name(account)}.json"


# --------------------------------------------------------------------------- storage

def clean(df):
    """Parquet-safe and schema-stable: ints -> float64, mixed-type object columns -> str."""
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col]) or pd.api.types.is_bool_dtype(df[col]):
            df[col] = df[col].astype("float64")
        elif df[col].dtype == object:
            types = {type(v) for v in df[col].dropna()}
            if len(types) > 1:
                df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v))
    return df


def write_parquet(path, df):
    """Write under a name readers ignore, then rename: other accounts never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    df.to_parquet(tmp, index=False)
    for attempt in range(40):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # another account is reading the old file right now (Windows)
            if attempt == 39:
                raise
            time.sleep(0.25)


def write_part(dataset, df, account):
    if df.empty:
        return
    write_parquet(DATA / dataset / f"part-{safe_name(account)}-{time.time_ns()}.parquet", clean(df))


def load_dataset(name, columns=None):
    """Read every part of a dataset (parts may differ in columns), de-duplicated on ticker+date."""
    folder = DATA / name
    parts = sorted(folder.glob("*.parquet")) if folder.exists() else []
    if not parts:
        return pd.DataFrame()
    frames = []
    for p in parts:
        cols = None
        if columns:
            have = set(pq.read_schema(p).names)
            cols = [c for c in columns if c in have]
        frames.append(pd.read_parquet(p, columns=cols))
    df = pd.concat(frames, ignore_index=True)
    key = [c for c in ("ticker", "snapshot_date", "date", "tenor") if c in df.columns]
    return df.drop_duplicates(key, keep="last") if key else df


def latest_snapshot():
    snap = load_dataset("reference")
    if snap.empty:
        return snap
    return snap.sort_values("snapshot_date").drop_duplicates("ticker", keep="last")


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# --------------------------------------------------------------------------- collection

def load_accounts(account):
    if not ACCOUNTS_FILE.exists():
        write_json(ACCOUNTS_FILE, {"accounts": [account]})
        log.info("Created %s with this account only; add the other accounts to it.", ACCOUNTS_FILE.name)
    accounts = json.loads(ACCOUNTS_FILE.read_text())["accounts"]
    if account not in accounts:
        raise SystemExit(f"Account '{account}' is not listed in {ACCOUNTS_FILE.name} ({accounts}). "
                         f"Add it there (same file on every machine) or pass --account.")
    return accounts


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
    ).sort_values(["tier", "ticker"], ignore_index=True)
    write_parquet(DATA / "universe.parquet", uni)
    return uni


def get_fields(bbg):
    path = DATA / "fields.parquet"
    if path.exists():
        return set(pd.read_parquet(path)["field"])
    wanted = REFERENCE_FIELDS + FUND_STATEMENTS + FUND_RATIOS + [f for p in PLAN for f in p[4].get("fields", [])]
    info = bbg.field_info(wanted)
    dropped = sorted(set(wanted) - set(info))
    if dropped:
        log.warning("Fields unknown to Bloomberg, skipped: %s", ", ".join(dropped))
    write_parquet(path, pd.DataFrame([{"field": k, **v} for k, v in info.items()]))
    return set(info)


def state_key(name):
    return f"reference_{ROUND}" if name == "reference" else name


def fetch_batch(bbg, state, name, kind, cfg, tickers, valid, account):
    """Fetch one batch; returns the tickers that may be marked done."""
    if kind == "reference":
        fields = [f for f in REFERENCE_FIELDS if f in valid]
        df, invalid = bbg.reference(tickers, fields)
        df.insert(1, "snapshot_date", pd.Timestamp(TODAY))
        state.invalid.update(invalid)
        write_part(name, df, account)
        return tickers

    if kind == "cds":
        snap = latest_snapshot()
        snap = snap[snap["ticker"].isin(tickers)].set_index("ticker")
        targets = {}
        for t in snap.index:  # tickers without a snapshot yet are left for later
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
                write_part(name, df.rename(columns={"PX_LAST": "spread_bp"}), account)
        return list(snap.index)

    skip = state.all_not_historical()
    fields = [f for f in dict.fromkeys(cfg["fields"]) if f in valid and f not in skip]
    merged = None
    for group in chunks(fields, MAX_HIST_FIELDS):
        df, invalid, not_hist = bbg.history(tickers, group, cfg["start"], cfg["periodicity"],
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
        write_part(name, merged, account)
    return tickers


def run_plan(bbg, state, claims, uni, accounts, account, args, valid):
    me, n = accounts.index(account), len(accounts)
    wanted = set(uni.loc[uni["tier"].isin(args.tiers), "ticker"])
    all_tickers = uni["ticker"].tolist()  # fixed order -> stable batch ids across accounts
    for name, kind, mandatory, size, cfg in PLAN:
        if args.mandatory_only and not mandatory:
            continue
        key = state_key(name)
        batches = [(f"{key}-{k:05d}", b) for k, b in enumerate(chunks(all_tickers, size))]
        own = [b for k, b in enumerate(batches) if k % n == me]
        others = [] if args.no_steal else [b for k, b in enumerate(batches) if k % n != me]
        fetched = 0
        pending, waited = own + others, 0.0
        while pending:
            held = []  # batches another account had claimed: come back to them, as it may stop
            for batch_id, tickers in pending:
                done = state.done_anywhere(key)
                if not any(t in wanted and t not in done for t in tickers):
                    continue
                if not claims.claim(batch_id):
                    held.append((batch_id, tickers))
                    continue
                # re-check after claiming: another account may have finished it just before
                done = state.done_anywhere(key)
                todo = [t for t in tickers if t in wanted and t not in done]
                if not todo:
                    claims.release(batch_id)
                    continue
                try:
                    ok = fetch_batch(bbg, state, name, kind, cfg, todo, valid, account)
                    state.mark(key, ok)
                finally:
                    claims.release(batch_id)
                fetched += 1
                waited = 0.0
                remaining = len(wanted - state.done_anywhere(key))
                log.info("%s: %s, %d tickers, %d remaining overall", name, batch_id, len(todo), remaining)
            if held and waited >= HELD_BATCH_WAIT:
                log.warning("%s: %d batch(es) still claimed by other accounts (%s); a later run will pick them "
                            "up if they are left unfinished", name, len(held), ", ".join(b for b, _ in held[:5]))
                break
            if held:
                time.sleep(2)
                waited += 2
            pending = held
        if fetched == 0:
            log.info("%s: nothing left for this account", name)


# --------------------------------------------------------------------------- reporting

def status():
    uni = pd.read_parquet(DATA / "universe.parquet")
    files = sorted(STATE.glob("progress_*.json"))
    accounts = {json.loads(p.read_text()).get("account", p.stem): json.loads(p.read_text()) for p in files}
    rows = []
    for name, _, mandatory, _, _ in PLAN:
        key = state_key(name)
        union, row = set(), {"dataset": name, "mandatory": mandatory}
        for acc, raw in accounts.items():
            got = set(raw.get("done", {}).get(key, []))
            row[acc] = len(got)
            union |= got
        invalid = set().union(*(set(r.get("invalid", [])) for r in accounts.values())) if accounts else set()
        row["done"] = len((union | invalid) & set(uni["ticker"]))
        row["total"] = len(uni)
        row["pct"] = f"{row['done'] / row['total']:.0%}"
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))
    for p in STATE.glob("limit_*.json"):
        info = json.loads(p.read_text())
        print(f"limit hit by {info['account']} at {info['hit_at']}")


def coverage(dataset):
    folder = DATA / dataset
    if not folder.exists() or not any(folder.glob("*.parquet")):
        return pd.DataFrame()
    table = pads.dataset(folder, format="parquet").to_table(columns=["ticker", "date"])
    agg = table.group_by("ticker").aggregate([("date", "min"), ("date", "max"), ("date", "count")]).to_pandas()
    agg.columns = ["ticker", f"{dataset}_first", f"{dataset}_last", f"{dataset}_rows"]
    return agg.set_index("ticker")


def build_summary():
    uni = pd.read_parquet(DATA / "universe.parquet").set_index("ticker")
    cov = uni.join([c for c in (coverage(p[0]) for p in PLAN if p[0] != "reference") if not c.empty])
    out = DATA / "summary.xlsx"
    with pd.ExcelWriter(out, engine="xlsxwriter") as xl:
        snap = latest_snapshot()
        if not snap.empty:
            snap.to_excel(xl, sheet_name="Latest snapshot", index=False)
        cov.reset_index().to_excel(xl, sheet_name="Coverage", index=False)
        if (DATA / "fields.parquet").exists():
            pd.read_parquet(DATA / "fields.parquet").to_excel(xl, sheet_name="Fields", index=False)
        for ws in xl.sheets.values():
            ws.freeze_panes(1, 1)
    log.info("Summary written to %s", out)


def migrate_legacy(account):
    """Convert the single-account layout (progress.json, reference/snapshot_YYYYMMDD/) once."""
    legacy = STATE / "progress.json"
    if legacy.exists():
        raw = json.loads(legacy.read_text())
        done = {}
        for k, v in raw.get("done", {}).items():
            m = re.match(r"reference/snapshot_(\d{6})\d{2}$", k)
            done.setdefault(f"reference_{m.group(1)}" if m else k, set()).update(v)
        # legacy daily prices already contain the OHLC/volume/total-return columns
        if "prices_daily" in done:
            done.setdefault("prices_daily_extra", set()).update(done["prices_daily"])
        write_json(STATE / f"progress_{safe_name(account)}.json", {
            "account": account, "done": {k: sorted(v) for k, v in done.items()},
            "invalid": raw.get("invalid", []), "not_historical": raw.get("not_historical", []),
        })
        legacy.rename(STATE / "progress.legacy.json")
        log.info("Migrated legacy progress.json to account %s", account)
    for snap_dir in (DATA / "reference").glob("snapshot_*"):
        for p in snap_dir.glob("*.parquet"):
            p.rename(DATA / "reference" / f"part-{safe_name(account)}-{snap_dir.name}-{p.name}")
        snap_dir.rmdir()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default=os.environ.get("BBG_ACCOUNT", os.environ.get("USERNAME", "default")))
    ap.add_argument("--tiers", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--mandatory-only", action="store_true")
    ap.add_argument("--no-steal", action="store_true", help="only do this account's own batches")
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--cooldown", type=float, default=12, help="hours to wait after a limit hit")
    ap.add_argument("--force", action="store_true", help="ignore the cooldown after a limit hit")
    ap.add_argument("--rebuild-universe", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    account = args.account
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format=f"%(asctime)s [{account}] %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOGS / f"collect_{safe_name(account)}.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])

    accounts = load_accounts(account)
    migrate_legacy(account)
    if args.status:
        status()
        return
    if args.summary_only:
        build_summary()
        return

    lf = limit_file(account)
    if lf.exists() and not args.force:
        hit = datetime.fromisoformat(json.loads(lf.read_text())["hit_at"])
        wait = hit + timedelta(hours=args.cooldown) - datetime.now()
        if wait > timedelta(0):
            raise SystemExit(f"Account {account} hit the Bloomberg limit at {hit:%Y-%m-%d %H:%M}; "
                             f"retry after {hit + timedelta(hours=args.cooldown):%Y-%m-%d %H:%M} or use --force.")

    bbg = Bloomberg(args.max_requests)
    state, claims = State(account), Claims(account)
    code = 0
    try:
        valid = get_fields(bbg)
        uni_path = DATA / "universe.parquet"
        uni = pd.read_parquet(uni_path) if uni_path.exists() and not args.rebuild_universe else build_universe(bbg)
        log.info("Accounts %s, this account #%d; universe %d tickers", accounts, accounts.index(account) + 1, len(uni))
        run_plan(bbg, state, claims, uni, accounts, account, args, valid)
        lf.unlink(missing_ok=True)
        log.info("Done for this account (%d requests this run)", bbg.requests)
    except LimitReached as e:
        write_json(lf, {"account": account, "hit_at": datetime.now().isoformat(timespec="seconds"), "message": str(e)})
        log.error("Bloomberg data limit reached for %s; progress saved. Other accounts can continue.\n%s", account, e)
        code = 3
    except BudgetExhausted as e:
        log.warning("Stopping: %s. Progress saved.", e)
    except KeyboardInterrupt:
        log.warning("Interrupted, progress saved.")
        code = 130
    finally:
        state.save()
        bbg.stop()
    try:
        build_summary()
    except Exception as e:  # the summary is a convenience; never let it mask the run's outcome
        log.warning("Could not rebuild summary.xlsx (%s); run --summary-only later.", e)
    sys.exit(code)


if __name__ == "__main__":
    main()
