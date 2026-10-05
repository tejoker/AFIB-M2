"""Check which CME Group futures data this Bloomberg login can access.

For each contract (Bloomberg generic front month) reports:
  - contract specs from reference data
  - daily history depth
  - intraday bar history depth
  - live streaming: real-time or delayed
Results are printed and saved to cme_access.csv.
"""
import time
from datetime import datetime, timedelta

import blpapi
import pandas as pd

CONTRACTS = {
    # Equity index
    "ES1 Index": "E-mini S&P 500", "NQ1 Index": "E-mini Nasdaq-100", "RTY1 Index": "E-mini Russell 2000",
    "DM1 Index": "E-mini Dow", "MES1 Index": "Micro E-mini S&P 500", "MNQ1 Index": "Micro E-mini Nasdaq",
    # Interest rates
    "TU1 Comdty": "2Y T-Note", "FV1 Comdty": "5Y T-Note", "TY1 Comdty": "10Y T-Note", "US1 Comdty": "T-Bond",
    "WN1 Comdty": "Ultra T-Bond", "SFR1 Comdty": "3M SOFR", "FF1 Comdty": "30D Fed Funds",
    # FX
    "EC1 Curncy": "Euro FX", "JY1 Curncy": "Japanese Yen", "BP1 Curncy": "British Pound",
    "AD1 Curncy": "Australian Dollar", "CD1 Curncy": "Canadian Dollar", "SF1 Curncy": "Swiss Franc",
    # Energy
    "CL1 Comdty": "WTI Crude", "NG1 Comdty": "Henry Hub Nat Gas", "HO1 Comdty": "NY Harbor ULSD",
    "XB1 Comdty": "RBOB Gasoline",
    # Metals
    "GC1 Comdty": "Gold", "SI1 Comdty": "Silver", "HG1 Comdty": "Copper", "PL1 Comdty": "Platinum",
    # Agriculture
    "C 1 Comdty": "Corn", "S 1 Comdty": "Soybeans", "W 1 Comdty": "Chicago Wheat", "SM1 Comdty": "Soybean Meal",
    "BO1 Comdty": "Soybean Oil", "LC1 Comdty": "Live Cattle", "LH1 Comdty": "Lean Hogs", "FC1 Comdty": "Feeder Cattle",
    # Crypto
    "BTC1 Curncy": "Bitcoin", "DCR1 Curncy": "Ether",
}
REF_FIELDS = ["NAME", "FUT_CUR_GEN_TICKER", "EXCH_CODE", "CRNCY", "PX_LAST", "LAST_UPDATE", "VOLUME", "OPEN_INT",
              "FUT_CONT_SIZE", "FUT_TICK_SIZE", "FUT_TICK_VAL", "FUT_INIT_SPEC_ML", "LAST_TRADEABLE_DT"]


def main():
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    s = blpapi.Session(opts)
    if not s.start() or not s.openService("//blp/refdata") or not s.openService("//blp/mktdata"):
        raise SystemExit("Cannot connect to Bloomberg")
    ref = s.getService("//blp/refdata")

    def send(req):
        s.sendRequest(req)
        msgs = []
        while True:
            ev = s.nextEvent(30000)
            if ev.eventType() in (blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE):
                msgs += list(ev)
            if ev.eventType() == blpapi.Event.RESPONSE:
                return msgs

    rows = {t: {"contract": n} for t, n in CONTRACTS.items()}

    # 1. reference data
    req = ref.createRequest("ReferenceDataRequest")
    for t in CONTRACTS:
        req.append("securities", t)
    for f in REF_FIELDS:
        req.append("fields", f)
    for m in send(req):
        for sd in m.getElement("securityData").values():
            t = sd.getElementAsString("security")
            if sd.hasElement("securityError"):
                rows[t]["error"] = sd.getElement("securityError").getElementAsString("message")
                continue
            fd = sd.getElement("fieldData")
            for f in REF_FIELDS:
                if fd.hasElement(f):
                    rows[t][f] = fd.getElement(f).getValue()

    # 2. daily history depth (one field, so this is cheap)
    req = ref.createRequest("HistoricalDataRequest")
    for t in CONTRACTS:
        req.append("securities", t)
    req.append("fields", "PX_LAST")
    req.set("startDate", "19500101")
    req.set("endDate", datetime.now().strftime("%Y%m%d"))
    req.set("periodicitySelection", "MONTHLY")
    for m in send(req):
        sd = m.getElement("securityData")
        t = sd.getElementAsString("security")
        data = sd.getElement("fieldData")
        if data.numValues():
            rows[t]["daily_history_from"] = data.getValueAsElement(0).getElementAsDatetime("date")

    # 3. intraday bars: how far back 1-hour bars go
    end = datetime.utcnow()
    for t in CONTRACTS:
        if "error" in rows[t]:
            continue
        req = ref.createRequest("IntradayBarRequest")
        req.set("security", t)
        req.set("eventType", "TRADE")
        req.set("interval", 60)
        req.set("startDateTime", end - timedelta(days=365 * 2))
        req.set("endDateTime", end)
        for m in send(req):
            if m.hasElement("responseError"):
                rows[t]["intraday"] = m.getElement("responseError").getElementAsString("message")
                break
            bars = m.getElement("barData").getElement("barTickData")
            if bars.numValues():
                rows[t]["intraday_bars_from"] = bars.getValueAsElement(0).getElementAsDatetime("time")
                rows[t]["intraday_bars_count"] = bars.numValues()

    # 4. live streaming: real-time vs delayed
    subs = blpapi.SubscriptionList()
    cids = {}
    for i, t in enumerate(CONTRACTS):
        if "error" not in rows[t]:
            cids[i] = t
            subs.add(t, "LAST_PRICE,BID,ASK,IS_DELAYED_STREAM", "", blpapi.CorrelationId(i))
    s.subscribe(subs)
    deadline = time.time() + 15
    while time.time() < deadline:
        ev = s.nextEvent(1000)
        for m in ev:
            if not m.correlationIds():
                continue
            t = cids.get(m.correlationIds()[0].value())
            if t is None:
                continue
            if m.messageType() == blpapi.Name("SubscriptionFailure"):
                rows[t]["stream"] = "FAILED: " + str(m.getElement("reason").getElementAsString("description"))
            elif m.messageType() == blpapi.Name("MarketDataEvents"):
                if m.hasElement("IS_DELAYED_STREAM"):
                    rows[t]["stream"] = "DELAYED" if m.getElementAsBool("IS_DELAYED_STREAM") else "REAL-TIME"
                for f in ("BID", "ASK"):
                    if m.hasElement(f) and not m.getElement(f).isNull():
                        rows[t][f"live_{f.lower()}"] = m.getElementAsFloat(f)
    s.stop()

    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "ticker"
    df.to_csv("cme_access.csv")
    cols = ["contract", "FUT_CUR_GEN_TICKER", "PX_LAST", "OPEN_INT", "FUT_TICK_VAL", "FUT_INIT_SPEC_ML",
            "daily_history_from", "intraday_bars_from", "stream", "error"]
    with pd.option_context("display.width", 250, "display.max_columns", 20, "display.max_colwidth", 25):
        print(df[[c for c in cols if c in df]].to_string())


if __name__ == "__main__":
    main()
