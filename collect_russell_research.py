"""Cache-backed Russell 1000/2000 research data collection and panel construction.

Uses its own output directory; does not modify bbg_collect.py or its progress.
Run with the workspace .venv Python. Cached complete responses make reruns resumable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import logging
from pathlib import Path
import time

import blpapi
import numpy as np
import pandas as pd

INDICES = {"RIY Index": "russell1000", "RTY Index": "russell2000", "SPX Index": "sp500"}
REF_FIELDS = """NAME LONG_COMP_NAME TICKER ID_ISIN ID_BB_GLOBAL ID_BB_GLOBAL_COMPANY
GICS_SECTOR GICS_INDUSTRY GICS_SUB_INDUSTRY COUNTRY_ISO CNTRY_OF_DOMICILE CNTRY_OF_RISK
CRNCY EQY_FUND_CRNCY EQY_INIT_PO_DT EQY_FISCAL_YR_END EQY_PRIM_SECURITY_COMP_EXCH
PX_LAST CUR_MKT_CAP EQY_SH_OUT EQY_FLOAT PX_BID PX_ASK LAST_UPDATE_DT VOLUME_AVG_20D VOLUME_AVG_3M
PE_RATIO CURRENT_EV_TO_T12M_EBITDA PX_TO_BOOK_RATIO FREE_CASH_FLOW_YIELD EQY_DVD_YLD_IND
RETURN_ON_ASSET RETURN_COM_EQY RETURN_ON_INV_CAPITAL GROSS_MARGIN EBITDA_MARGIN OPER_MARGIN PROF_MARGIN
SALES_GROWTH NET_DEBT_TO_EBITDA TOT_DEBT_TO_TOT_ASSET CUR_RATIO QUICK_RATIO
TRAIL_12M_NET_INC TRAIL_12M_EBITDA CURR_ENTP_VAL SALES_REV_TURN BS_TOT_ASSET BS_TOT_LIAB2
SHORT_AND_LONG_TERM_DEBT NET_DEBT BS_CASH_NEAR_CASH_ITEM TOT_COMMON_EQY
CF_CASH_FROM_OPER CAPITAL_EXPEND CF_FREE_CASH_FLOW BETA_RAW_OVERRIDABLE VOLATILITY_90D VOLATILITY_260D
RTG_SP_LT_LC_ISSUER_CREDIT RTG_MDY_ISSUER RTG_FITCH_LT_ISSUER_DEFAULT BB_1YR_DEFAULT_PROB""".split()
FORECAST_FIELDS = "BEST_PE_RATIO BEST_EPS BEST_SALES BEST_EBITDA BEST_ROE".split()
FUND_FIELDS = """SALES_REV_TURN EBITDA EBIT NET_INCOME IS_INC_BEF_XO_ITEM IS_DILUTED_EPS
BS_TOT_ASSET BS_TOT_LIAB2 TOT_COMMON_EQY SHORT_AND_LONG_TERM_DEBT NET_DEBT BS_CASH_NEAR_CASH_ITEM
CF_CASH_FROM_OPER CAPITAL_EXPEND CF_FREE_CASH_FLOW RETURN_ON_ASSET RETURN_COM_EQY RETURN_ON_INV_CAPITAL
PROF_MARGIN SALES_GROWTH NET_DEBT_TO_EBITDA TOT_DEBT_TO_TOT_ASSET CUR_RATIO EBITDA_MARGIN""".split()
DAILY_FIELDS = "PX_LAST PX_VOLUME TURNOVER CUR_MKT_CAP EQY_SH_OUT EQY_FLOAT TOT_RETURN_INDEX_GROSS_DVDS".split()
HISTORICAL_SNAPSHOT_FIELDS = "CUR_MKT_CAP PE_RATIO PX_TO_BOOK_RATIO CURRENT_EV_TO_T12M_EBITDA FREE_CASH_FLOW_YIELD".split()
SECTORS = {10: "Energy", 15: "Materials", 20: "Industrials", 25: "Consumer discretionary",
           30: "Consumer staples", 35: "Health care", 40: "Financials", 45: "Information technology",
           50: "Communication services", 55: "Utilities", 60: "Real estate"}


def native(el):
    if el.isArray():
        return [native(v) if isinstance(v, blpapi.Element) else v for v in el.values()]
    if el.isComplexType():
        return {str(el.getElement(i).name()): native(el.getElement(i)) for i in range(el.numElements())}
    if el.isNull():
        return None
    v = el.getValue()
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime, dt.time)) else v


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def parquet(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    for col in frame.columns:
        if not pd.api.types.is_object_dtype(frame[col].dtype):
            continue
        types = {type(x) for x in frame[col].dropna()}
        if len(types) > 1:
            frame[col] = frame[col].map(lambda v: None if pd.isna(v) else str(v))
    temp = path.with_suffix(".tmp.parquet")
    frame.to_parquet(temp, index=False)
    temp.replace(path)


class BloombergLimit(RuntimeError):
    pass


class Client:
    def __init__(self, root, max_requests=None):
        self.root = root
        self.max_requests = max_requests
        self.requests = 0
        self.cache_hits = 0
        options = blpapi.SessionOptions()
        options.setServerHost("localhost")
        options.setServerPort(8194)
        self.session = blpapi.Session(options)
        if not self.session.start():
            raise RuntimeError("Cannot connect to the logged-in Bloomberg Terminal")
        for service in ["//blp/refdata", "//blp/apiflds"]:
            if not self.session.openService(service):
                self.session.stop()
                raise RuntimeError(f"Cannot open {service}")
        self.errors = []

    def query(self, securities, fields, kind="ReferenceDataRequest", params=None, overrides=None, service="//blp/refdata"):
        spec = dict(service=service, kind=kind, securities=securities, fields=fields,
                    params=params or {}, overrides=overrides or {})
        signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        path = self.root / "cache" / (signature + ".json.gz")
        if path.exists():
            self.cache_hits += 1
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                return json.load(stream)["responses"]
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise BloombergLimit(f"Configured request budget ({self.max_requests}) reached; resume from cache")
        req = self.session.getService(service).createRequest(kind)
        if kind == "FieldInfoRequest":
            for field in fields:
                req.append("id", field)
            req.set("returnFieldDocumentation", True)
        else:
            for security in securities:
                req.append("securities", security)
            for field in fields:
                req.append("fields", field)
            for key, value in (params or {}).items():
                req.set(key, value)
            for key, value in (overrides or {}).items():
                ov = req.getElement("overrides").appendElement()
                ov.setElement("fieldId", key)
                ov.setElement("value", value)
        self.requests += 1
        self.session.sendRequest(req)
        responses = []
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            event = self.session.nextEvent(1000)
            et = event.eventType()
            for message in event:
                if et not in [blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE, blpapi.Event.REQUEST_STATUS]:
                    continue
                obj = native(message.asElement())
                serialized = json.dumps(obj, default=str).upper()
                if any(token in serialized for token in ['"CATEGORY": "LIMIT"', "DAILY DATA LIMIT", "MONTHLY DATA LIMIT", '"CATEGORY": "CAPACITY"']):
                    raise BloombergLimit(serialized[:1000])
                if et == blpapi.Event.REQUEST_STATUS or "responseError" in obj:
                    raise RuntimeError(f"Bloomberg request failed: {str(obj)[:1500]}")
                responses.append(obj)
            if et == blpapi.Event.RESPONSE:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp.gz")
                with gzip.open(temporary, "wt", encoding="utf-8") as stream:
                    json.dump({"request": spec, "retrieved_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "responses": responses}, stream, default=str)
                temporary.replace(path)
                return responses
        raise TimeoutError("Bloomberg request exceeded 240 seconds; incomplete response was not cached")

    def security_data(self, securities, fields, **kwargs):
        responses = self.query(securities, fields, **kwargs)
        result = []
        for obj in responses:
            data = obj.get("securityData", [])
            result.extend(data if isinstance(data, list) else [data])
        for item in result:
            if item.get("securityError") or item.get("fieldExceptions"):
                self.errors.append({"security": item.get("security"), "fields": fields,
                                    "securityError": item.get("securityError"),
                                    "fieldExceptions": item.get("fieldExceptions", [])})
        return result


def composite(member):
    value = str(member).strip().removesuffix(" Equity")
    ticker, exchange = value.rsplit(" ", 1)
    # Preserve delisted tickers; consolidate U.S. trading venues, without guessing foreign tickers.
    return ticker + " US Equity" if exchange in {"US", "UN", "UW", "UQ", "UR", "UF", "UA", "UP"} else value + " Equity"


def membership(client, args):
    records = []
    current = client.security_data(list(INDICES), ["NAME", "INDX_MEMBERS"])
    for data in current:
        rows = data.get("fieldData", {}).get("INDX_MEMBERS", [])
        if not rows:
            raise RuntimeError(f"Missing current constituents for {data.get('security')}")
        logging.info("%s: %d current security lines", data["security"], len(rows))
        for row in rows:
            raw = row["Member Ticker and Exchange Code"]
            records.append(dict(index=INDICES[data["security"]], membership_date=args.snapshot_date,
                                membership_kind="current", raw_member=raw, security=composite(raw), weight_pct=None))
    dates = pd.date_range(args.daily_start, args.as_of, freq="QE")
    # A year-end baseline lets January outcomes use the preceding observed membership.
    baseline = (pd.Timestamp(args.daily_start) - pd.offsets.QuarterEnd()).strftime("%Y-%m-%d")
    for date in [baseline, *dates.strftime("%Y-%m-%d")]:
        result = client.security_data(list(INDICES), ["INDX_MWEIGHT_HIST"], overrides={"END_DATE_OVERRIDE": date.replace("-", "")})
        for data in result:
            rows = data.get("fieldData", {}).get("INDX_MWEIGHT_HIST", [])
            for row in rows:
                raw = row["Index Member"]
                records.append(dict(index=INDICES[data["security"]], membership_date=date, membership_kind="historical_quarter_end",
                                    raw_member=raw, security=composite(raw), weight_pct=row.get("Percent Weight")))
            logging.info("Membership %s %s: %d", date, data["security"], len(rows))
    frame = pd.DataFrame(records).drop_duplicates(["index", "membership_date", "membership_kind", "security"])
    parquet(args.output / "index_membership.parquet", frame)
    frame.to_csv(args.output / "index_membership.csv", index=False)
    return frame


def validate_fields(client, args):
    all_fields = sorted(set(REF_FIELDS + FORECAST_FIELDS + FUND_FIELDS + DAILY_FIELDS + HISTORICAL_SNAPSHOT_FIELDS))
    response = client.query([], all_fields, kind="FieldInfoRequest", service="//blp/apiflds")
    info = {}
    for message in response:
        for item in message.get("fieldData", []):
            if "fieldInfo" in item:
                doc = item["fieldInfo"]
                info[doc["mnemonic"]] = doc
    atomic_json(args.output / "field_documentation.json", info)
    pd.DataFrame([dict(field=k, description=v.get("description"), datatype=v.get("datatype"),
                       documentation=v.get("documentation")) for k, v in info.items()]).to_csv(args.output / "field_dictionary.csv", index=False)
    invalid = sorted(set(all_fields) - set(info))
    atomic_json(args.output / "unknown_fields.json", invalid)
    if invalid:
        logging.warning("Unknown fields omitted: %s", invalid)
    return set(info)


def snapshots(client, securities, valid, args):
    frames = []
    fields = [f for f in REF_FIELDS if f in valid]
    for n, batch in enumerate(chunks(securities, args.batch_size), 1):
        rows = client.security_data(batch, fields, overrides={"SCALING_FORMAT": "UNT"})
        frame = pd.DataFrame([dict(security=row["security"], **row.get("fieldData", {})) for row in rows])
        frame["snapshot_date"] = args.snapshot_date
        for period in ["1BF", "2BF"]:
            estimates = client.security_data(batch, [f for f in FORECAST_FIELDS if f in valid],
                                            overrides={"SCALING_FORMAT": "UNT", "BEST_FPERIOD_OVERRIDE": period})
            values = pd.DataFrame([dict(security=row["security"], **{k + "_" + period: v for k, v in row.get("fieldData", {}).items()}) for row in estimates])
            frame = frame.merge(values, on="security", how="left", validate="one_to_one")
        parquet(args.output / "snapshot_parts" / f"batch-{n:04}.parquet", frame)
        frames.append(frame)
        logging.info("Reference and forecasts: %d/%d securities", min(n * args.batch_size, len(securities)), len(securities))
    return pd.concat(frames, ignore_index=True)


def history_frame(result):
    rows = [dict(security=item["security"], **row) for item in result for row in item.get("fieldData", [])]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
        for col in frame.columns.difference(["security", "date"]):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.sort_values(["security", "date"]).drop_duplicates(["security", "date"])
    return frame


def history(client, securities, fields, start, periodicity, args, split=False, fiscal=False):
    params = dict(startDate=start.replace("-", ""), endDate=args.as_of.replace("-", ""),
                  periodicitySelection=periodicity, adjustmentNormal=False, adjustmentAbnormal=False,
                  adjustmentSplit=split, adjustmentFollowDPDF=False)
    if fiscal:
        params["periodicityAdjustment"] = "FISCAL"
    return history_frame(client.security_data(securities, fields, kind="HistoricalDataRequest", params=params,
                                               overrides={"SCALING_FORMAT": "UNT"}))


def monthly_features(daily):
    daily = daily.sort_values(["security", "date"]).copy()
    for field in DAILY_FIELDS + ["split_adjusted_price"]:
        if field not in daily:
            daily[field] = np.nan
    daily["price_return"] = daily.groupby("security")["split_adjusted_price"].pct_change(fill_method=None)
    traded = daily["TURNOVER"] > 0
    daily["amihud_per_million"] = (daily["price_return"].abs() / (daily["TURNOVER"] / 1e6)).where(traded)
    daily["float_turnover_pct"] = (daily["PX_VOLUME"] / (daily["EQY_FLOAT"] * 1e6) * 100).where(daily["EQY_FLOAT"] > 0)
    # Historical EQY_SH_OUT responds to SCALING_FORMAT=UNT and is already in units;
    # EQY_FLOAT remains in millions. Verified against AAPL/AA price * shares = cap.
    daily["shares_turnover_pct"] = (daily["PX_VOLUME"] / daily["EQY_SH_OUT"] * 100).where(daily["EQY_SH_OUT"] > 0)
    daily["month"] = daily["date"].dt.to_period("M").dt.to_timestamp("M")
    daily["active"] = traded & (daily["PX_VOLUME"] > 0)
    daily["active_turnover"] = daily["TURNOVER"].where(daily["active"])
    daily["active_volume"] = daily["PX_VOLUME"].where(daily["active"])
    frame = daily.groupby(["security", "month"], sort=True).agg(
        observed_through=("date", "max"), trading_days=("active", "sum"),
        adtv_usd=("active_turnover", "mean"), adv_shares=("active_volume", "mean"),
        dollar_volume_sum=("active_turnover", "sum"), amihud_per_million=("amihud_per_million", "mean"),
        float_turnover_pct=("float_turnover_pct", "mean"), shares_turnover_pct=("shares_turnover_pct", "mean"),
        price_volatility_annualized_pct=("price_return", "std"), market_cap_usd=("CUR_MKT_CAP", "last"),
        float_shares=("EQY_FLOAT", "last"), shares_out=("EQY_SH_OUT", "last"), last_price=("PX_LAST", "last"),
        split_adjusted_last_price=("split_adjusted_price", "last"), total_return_index=("TOT_RETURN_INDEX_GROSS_DVDS", "last")
    ).reset_index()
    frame.loc[frame["trading_days"] == 0, "dollar_volume_sum"] = np.nan
    frame["float_shares"] *= 1e6
    frame["price_volatility_annualized_pct"] *= np.sqrt(252) * 100
    frame["total_return_monthly"] = frame.groupby("security")["total_return_index"].pct_change(fill_method=None)
    frame["price_return_monthly"] = frame.groupby("security")["split_adjusted_last_price"].pct_change(fill_method=None)
    consecutive = frame.groupby("security")["month"].transform(lambda x: x.dt.to_period("M").astype("int64").diff().eq(1))
    frame.loc[~consecutive, ["total_return_monthly", "price_return_monthly"]] = np.nan
    quality = daily.assign(tri_matches_price=np.isclose(daily["TOT_RETURN_INDEX_GROSS_DVDS"], daily["PX_LAST"], equal_nan=False)).groupby("security")["tri_matches_price"].all()
    frame["total_return_series_matches_raw_price"] = frame["security"].map(quality)
    # October is incomplete on this collection date; keep it explicitly flagged.
    frame["calendar_month_complete"] = frame["month"] <= pd.Timestamp(daily["date"].max())
    return frame


def collect_history(client, securities, valid, args):
    count = 0
    for n, batch in enumerate(chunks(securities, args.history_batch_size), 1):
        daily_path = args.output / "daily_parts" / f"batch-{n:04}.parquet"
        # Cache prevents API duplication; parquet files are rebuilt to reflect parser improvements.
        start = (pd.Timestamp(args.daily_start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        daily = history(client, batch, [f for f in DAILY_FIELDS if f in valid], start, "DAILY", args)
        adjusted = history(client, batch, ["PX_LAST"], start, "DAILY", args, split=True)
        if not daily.empty:
            if not adjusted.empty:
                daily = daily.merge(adjusted.rename(columns={"PX_LAST": "split_adjusted_price"}), on=["security", "date"],
                                    how="left", validate="one_to_one")
            parquet(daily_path, daily)
            monthly = monthly_features(daily)
            monthly = monthly[monthly["month"] >= pd.Timestamp(args.daily_start)]
            parquet(args.output / "monthly_parts" / f"batch-{n:04}.parquet", monthly)
            count += len(daily)
        fundamentals = history(client, batch, [f for f in FUND_FIELDS if f in valid], args.fundamental_start, "QUARTERLY", args, fiscal=True)
        if not fundamentals.empty:
            columns = fundamentals.columns.difference(["security", "date"])
            fundamentals = fundamentals.dropna(how="all", subset=columns)
            parquet(args.output / "fundamental_parts" / f"batch-{n:04}.parquet", fundamentals)
        valuation = history(client, batch, [f for f in HISTORICAL_SNAPSHOT_FIELDS if f in valid], args.daily_start, "MONTHLY", args)
        if not valuation.empty:
            parquet(args.output / "valuation_parts" / f"batch-{n:04}.parquet", valuation)
        logging.info("Market/fundamental/valuation history: %d/%d securities; %d daily rows so far", min(n * args.history_batch_size, len(securities)), len(securities), count)


def merge_membership(panel, members):
    dates = sorted(members.loc[members["membership_kind"] == "historical_quarter_end", "membership_date"].unique())
    if not dates:
        for label in INDICES.values():
            panel["in_" + label + "_observed"] = pd.NA
        return panel
    # Map every outcome month to the most recent available snapshot STRICTLY BEFORE that month starts.
    reference = pd.DataFrame({"membership_observation_date": pd.to_datetime(dates)})
    months = panel[["month"]].drop_duplicates().sort_values("month")
    months["month_start"] = months["month"].dt.to_period("M").dt.to_timestamp()
    months = pd.merge_asof(months, reference, left_on="month_start", right_on="membership_observation_date",
                           direction="backward", allow_exact_matches=False)
    panel = panel.merge(months[["month", "membership_observation_date"]], on="month", how="left", validate="many_to_one")
    historical = members[members["membership_kind"] == "historical_quarter_end"].copy()
    historical["membership_observation_date"] = pd.to_datetime(historical["membership_date"])
    for label in INDICES.values():
        rows = historical[historical["index"] == label][["security", "membership_observation_date"]].drop_duplicates()
        name = "in_" + label + "_observed"
        rows[name] = True
        available_dates = set(rows["membership_observation_date"])
        panel = panel.merge(rows, on=["security", "membership_observation_date"], how="left", validate="many_to_one")
        # A completely missing index/date query gives unknown membership, never an automatic false.
        available = panel["membership_observation_date"].isin(available_dates)
        panel[name] = panel[name].astype("boolean")
        panel.loc[available, name] = panel.loc[available, name].fillna(False)
    return panel


def build_outputs(members, reference, args):
    for field in REF_FIELDS + [f + "_" + p for p in ["1BF", "2BF"] for f in FORECAST_FIELDS]:
        if field not in reference:
            reference[field] = np.nan
    numeric = ["CUR_MKT_CAP", "EQY_FLOAT", "EQY_SH_OUT", "PX_BID", "PX_ASK", "VOLUME_AVG_3M", "BEST_SALES_1BF", "BEST_SALES_2BF"]
    for col in numeric:
        reference[col] = pd.to_numeric(reference[col], errors="coerce")
    reference["company_id_missing"] = reference["ID_BB_GLOBAL_COMPANY"].isna()
    reference["company_id"] = reference["ID_BB_GLOBAL_COMPANY"].fillna("unresolved:" + reference["security"])
    reference["sector_current"] = reference["GICS_SECTOR"].map(SECTORS)
    reference["market_cap_usd"] = reference["CUR_MKT_CAP"].where(reference["CRNCY"] == "USD")
    reference["float_shares_current"] = reference["EQY_FLOAT"] * 1e6
    reference["shares_out_current"] = reference["EQY_SH_OUT"] * 1e6
    reference["forward_sales_growth_pct"] = ((reference["BEST_SALES_2BF"] / reference["BEST_SALES_1BF"] - 1) * 100).where(reference["BEST_SALES_1BF"] > 0)
    reference["spread_bps_snapshot"] = ((reference["PX_ASK"] - reference["PX_BID"]) / ((reference["PX_ASK"] + reference["PX_BID"]) / 2) * 1e4).where((reference["PX_ASK"] >= reference["PX_BID"]) & (reference["PX_BID"] > 0))
    current = members[members["membership_kind"] == "current"]
    for label in INDICES.values():
        reference["in_" + label + "_current"] = reference["security"].isin(current.loc[current["index"] == label, "security"])
    flags = ["in_" + label + "_current" for label in INDICES.values()]
    company_flags = reference.groupby("company_id")[flags].max()
    company = reference.sort_values(["in_sp500_current", "VOLUME_AVG_3M"], ascending=[False, False]).drop_duplicates("company_id")
    company = company.drop(columns=flags).merge(company_flags, on="company_id", validate="one_to_one")
    company["russell1000_not_sp500_current"] = company["in_russell1000_current"] & ~company["in_sp500_current"]
    parquet(args.output / "security_reference_snapshot.parquet", reference)
    parquet(args.output / "company_reference_snapshot.parquet", company)
    company.to_csv(args.output / "company_reference_snapshot.csv", index=False)
    monthly_files = sorted((args.output / "monthly_parts").glob("*.parquet"))
    panels = [pd.read_parquet(path) for path in monthly_files]
    panel = pd.concat(panels, ignore_index=True) if panels else pd.DataFrame()
    if not panel.empty:
        panel = panel.drop_duplicates(["security", "month"]).sort_values(["security", "month"])
        consecutive = panel.groupby("security")["month"].transform(lambda x: x.dt.to_period("M").astype("int64").diff().eq(1))
        panel.loc[~consecutive, ["total_return_monthly", "price_return_monthly"]] = np.nan
        identity = reference[["security", "company_id", "company_id_missing", "NAME", "sector_current", "GICS_INDUSTRY", "CRNCY"]].rename(columns={"GICS_INDUSTRY": "gics_industry_current"})
        panel = panel.merge(identity, on="security", how="left", validate="many_to_one")
        panel = merge_membership(panel, members)
        panel["regression_liquidity_eligible"] = (panel["trading_days"] >= 15) & panel["calendar_month_complete"] & (panel["CRNCY"] == "USD")
        for col in ["market_cap_usd", "adtv_usd", "amihud_per_million", "float_turnover_pct"]:
            panel["log_" + col] = np.log(panel[col].where(panel[col] > 0))
        parquet(args.output / "monthly_liquidity_panel.parquet", panel)
        panel.to_csv(args.output / "monthly_liquidity_panel.csv", index=False)
        # This is a current-constituent cross-section with recent completed-month liquidity.
        recent = panel[(panel["month"] >= pd.Timestamp(args.as_of) - pd.DateOffset(months=3)) & panel["calendar_month_complete"]]
        recent = recent.groupby("security").agg(adtv_recent_usd=("adtv_usd", "mean"), amihud_recent_per_million=("amihud_per_million", "mean"),
                                                  float_turnover_recent_pct=("float_turnover_pct", "mean"), recent_months=("month", "nunique")).reset_index()
        cross = company.merge(recent, on="security", how="left", validate="one_to_one")
        cross = cross[cross["in_russell1000_current"] | cross["in_russell2000_current"] | cross["in_sp500_current"]]
        aliases = {"BEST_PE_RATIO_1BF": "forward_pe", "CURRENT_EV_TO_T12M_EBITDA": "ev_ebitda", "PX_TO_BOOK_RATIO": "price_book",
                   "RETURN_ON_ASSET": "roa_pct", "PROF_MARGIN": "profit_margin_pct", "SALES_GROWTH": "sales_growth_pct",
                   "NET_DEBT_TO_EBITDA": "net_debt_ebitda", "BETA_RAW_OVERRIDABLE": "beta", "VOLATILITY_90D": "volatility_90d_pct"}
        for source, name in aliases.items():
            cross[name] = pd.to_numeric(cross[source], errors="coerce")
        cross["avg_share_volume_3m"] = pd.to_numeric(cross["VOLUME_AVG_3M"], errors="coerce")
        cross["float_turnover_snapshot_3m_pct"] = (cross["avg_share_volume_3m"] / cross["float_shares_current"] * 100).where(cross["float_shares_current"] > 0)
        cross["dollar_volume_proxy_current_price_3m_usd"] = cross["avg_share_volume_3m"] * pd.to_numeric(cross["PX_LAST"], errors="coerce")
        cross["regression_snapshot_turnover_eligible"] = (~cross["company_id_missing"]) & (cross["CRNCY"] == "USD") & (cross["market_cap_usd"] > 0) & (cross["float_turnover_snapshot_3m_pct"] > 0)
        for col in ["market_cap_usd", "adtv_recent_usd", "amihud_recent_per_million", "forward_pe", "ev_ebitda", "price_book"]:
            cross["log_" + col] = np.log(cross[col].where(cross[col] > 0))
        cross["regression_liquidity_eligible"] = (cross["CRNCY"] == "USD") & (cross["recent_months"] >= 3) & (cross["market_cap_usd"] > 0) & (cross["adtv_recent_usd"] > 0)
        parquet(args.output / "regression_cross_section.parquet", cross)
        cross.to_csv(args.output / "regression_cross_section.csv", index=False)
        available_cross = cross[(~cross["company_id_missing"]) & cross["NAME"].notna()].copy()
        parquet(args.output / "regression_cross_section_available.parquet", available_cross)
        available_cross.to_csv(args.output / "regression_cross_section_available.csv", index=False)
    coverage = []
    for dataset in ["daily", "fundamental", "valuation"]:
        for path in sorted((args.output / (dataset + "_parts")).glob("*.parquet")):
            frame = pd.read_parquet(path)
            for security, group in frame.groupby("security"):
                coverage.append(dict(dataset=dataset, security=security, rows=len(group), first_date=group["date"].min(), last_date=group["date"].max()))
    coverage = pd.DataFrame(coverage, columns=["dataset", "security", "rows", "first_date", "last_date"])
    expected = pd.MultiIndex.from_product([["daily", "fundamental", "valuation"], reference["security"].unique()], names=["dataset", "security"]).to_frame(index=False)
    if not coverage.empty:
        coverage = coverage.groupby(["dataset", "security"], as_index=False).agg(rows=("rows", "sum"), first_date=("first_date", "min"), last_date=("last_date", "max"))
    coverage = expected.merge(coverage, on=["dataset", "security"], how="left", validate="one_to_one")
    coverage["rows"] = coverage["rows"].fillna(0).astype(int)
    coverage["has_data"] = coverage["rows"] > 0
    coverage.to_csv(args.output / "coverage.csv", index=False)
    missing = []
    for column in reference:
        missing.append(dict(dataset="reference", field=column, rows=len(reference), non_null=int(reference[column].notna().sum())))
    if not panel.empty:
        for column in panel:
            missing.append(dict(dataset="monthly_panel", field=column, rows=len(panel), non_null=int(panel[column].notna().sum())))
    pd.DataFrame(missing).to_csv(args.output / "field_coverage.csv", index=False)
    historical_weights = pd.to_numeric(members.loc[members["membership_kind"] == "historical_quarter_end", "weight_pct"], errors="coerce")
    summary = dict(snapshot_date=args.snapshot_date, history_end=args.as_of, daily_start=args.daily_start, fundamental_start=args.fundamental_start,
                   securities_requested=len(reference), companies=len(company), historical_membership_rows=len(members),
                   target_securities=int(members["security"].nunique()), reference_rows_with_name=int(reference["NAME"].notna().sum()),
                   companies_with_resolved_ids=int((~company["company_id_missing"]).sum()),
                   current_security_counts=current.groupby("index")["security"].nunique().to_dict(),
                   current_company_counts={label: int((company["in_" + label + "_current"] & ~company["company_id_missing"]).sum()) for label in INDICES.values()},
                   unresolved_current_securities={label: int((reference["in_" + label + "_current"] & reference["company_id_missing"]).sum()) for label in INDICES.values()},
                   historical_only_securities=int((~reference[flags].any(axis=1)).sum()), unresolved_company_ids=int(reference["company_id_missing"].sum()),
                   historical_weights_positive=int((historical_weights > 0).sum()), historical_weights_nonpositive=int((historical_weights <= 0).sum()),
                   monthly_rows=len(panel), monthly_securities=0 if panel.empty else int(panel["security"].nunique()),
                   history_coverage={} if coverage.empty else coverage.groupby("dataset").agg(securities_with_data=("has_data", "sum"), securities_requested=("security", "nunique"), rows=("rows", "sum")).to_dict("index"))
    atomic_json(args.output / "coverage_summary.json", summary)
    with pd.ExcelWriter(args.output / "research_overview.xlsx", engine="xlsxwriter") as writer:
        company.to_excel(writer, sheet_name="Company snapshot", index=False)
        coverage.to_excel(writer, sheet_name="History coverage", index=False)
        pd.DataFrame(missing).to_excel(writer, sheet_name="Field coverage", index=False)
        current.to_excel(writer, sheet_name="Current membership", index=False)
        for sheet in writer.sheets.values():
            sheet.freeze_panes(1, 1)
            sheet.autofilter(0, 0, sheet.dim_rowmax, sheet.dim_colmax)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "data" / "russell_research_20261005")
    parser.add_argument("--snapshot-date", default="2026-10-05")
    parser.add_argument("--as-of", default="2026-10-02")
    parser.add_argument("--daily-start", default="2021-01-01")
    parser.add_argument("--fundamental-start", default="2015-01-01")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--history-batch-size", type=int, default=50)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(args.output / "collect.log", encoding="utf-8"), logging.StreamHandler()])
    config = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    config_path = args.output / "configuration.json"
    if config_path.exists():
        old = json.loads(config_path.read_text(encoding="utf-8"))
        for key in ["snapshot_date", "as_of", "daily_start", "fundamental_start", "batch_size", "history_batch_size"]:
            if old[key] != config[key]:
                raise ValueError(f"Use a new output directory when changing {key}; existing partitions must not mix configurations")
    atomic_json(config_path, config)
    client = None
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        if args.build_only:
            members = pd.read_parquet(args.output / "index_membership.parquet")
            reference = pd.concat([pd.read_parquet(path) for path in sorted((args.output / "snapshot_parts").glob("*.parquet"))], ignore_index=True)
        else:
            client = Client(args.output, args.max_requests)
            valid = validate_fields(client, args)
            members = membership(client, args)
            securities = sorted(members["security"].unique())
            logging.info("Union of current and historical securities: %d", len(securities))
            reference = snapshots(client, securities, valid, args)
            collect_history(client, securities, valid, args)
            benchmarks = history(client, list(INDICES), ["PX_LAST", "TOT_RETURN_INDEX_GROSS_DVDS"], args.daily_start, "DAILY", args)
            if not benchmarks.empty:
                parquet(args.output / "index_benchmarks_daily.parquet", benchmarks)
        summary = build_outputs(members, reference, args)
        atomic_json(args.output / ("export_status.json" if args.build_only else "run_status.json"), dict(status="exports_rebuilt" if args.build_only else "complete", started_at_utc=started,
                    completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(), requests=0 if client is None else client.requests,
                    cache_hits=0 if client is None else client.cache_hits))
        print(json.dumps(summary, indent=2, default=str), flush=True)
    except (BloombergLimit, KeyboardInterrupt, RuntimeError, TimeoutError) as exc:
        atomic_json(args.output / "run_status.json", dict(status="incomplete_resumable", started_at_utc=started, reason=str(exc),
                    requests=0 if client is None else client.requests, cache_hits=0 if client is None else client.cache_hits))
        logging.error("Collection stopped; complete responses retained for resume: %s", exc)
        raise
    finally:
        if client is not None:
            atomic_json(args.output / "api_errors.json", client.errors)
            client.session.stop()


if __name__ == "__main__":
    main()
