"""Screen S&P 500 and STOXX 600 issuers sitting at the IG/HY frontier (BBB-/BB+)."""
import re

import blpapi
import pandas as pd

INDICES = ["SPX Index", "SXXP Index"]
FIELDS = {
    "NAME": "Name",
    "GICS_SECTOR_NAME": "Sector",
    "COUNTRY_ISO": "Country",
    "RTG_SP_LT_LC_ISSUER_CREDIT": "S&P",
    "RTG_SP_OUTLOOK": "S&P outlook",
    "RTG_MOODY_LONG_TERM": "Moody's",
    "RTG_MDY_OUTLOOK": "Moody's outlook",
    "RTG_FITCH_LT_ISSUER_DEFAULT": "Fitch",
    "RTG_FITCH_OUTLOOK": "Fitch outlook",
    "NET_DEBT_TO_EBITDA": "Net debt/EBITDA",
    "CDS_SPREAD_TICKER_5Y": "CDS ticker",
}
OUTPUT = "crossover_issuers.xlsx"

# Notch scale: AAA = 1 ... BBB- = 10 (last IG notch), BB+ = 11 (first HY notch)
SP_SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
            "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"]
MDY_SCALE = ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3",
             "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]
SP_NOTCH = {r: i + 1 for i, r in enumerate(SP_SCALE)}
MDY_NOTCH = {r: i + 1 for i, r in enumerate(MDY_SCALE)}
IG_FLOOR, HY_CEIL = 10, 11


def notch(rating, scale):
    """Convert a rating string to a notch number, ignoring watch/provisional markers."""
    if not isinstance(rating, str):
        return None
    clean = re.sub(r"\(P\)|\*[+-]?|\s.*$|u$", "", rating.strip())
    return scale.get(clean)


class Bbg:
    def __init__(self):
        opts = blpapi.SessionOptions()
        opts.setServerHost("localhost")
        opts.setServerPort(8194)
        self.session = blpapi.Session(opts)
        if not self.session.start() or not self.session.openService("//blp/refdata"):
            raise SystemExit("Cannot connect to Bloomberg (is the Terminal logged in?)")
        self.svc = self.session.getService("//blp/refdata")

    def _send(self, securities, fields):
        req = self.svc.createRequest("ReferenceDataRequest")
        for s in securities:
            req.append("securities", s)
        for f in fields:
            req.append("fields", f)
        self.session.sendRequest(req)
        while True:
            ev = self.session.nextEvent(30000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    yield from msg.getElement("securityData").values()
            if ev.eventType() == blpapi.Event.RESPONSE:
                return

    def members(self, index):
        out = []
        for sd in self._send([index], ["INDX_MEMBERS"]):
            fd = sd.getElement("fieldData")
            if fd.hasElement("INDX_MEMBERS"):
                for row in fd.getElement("INDX_MEMBERS").values():
                    out.append(row.getElement(0).getValueAsString() + " Equity")
        return out

    def ref(self, securities, fields, chunk=100):
        rows = {}
        for i in range(0, len(securities), chunk):
            for sd in self._send(securities[i:i + chunk], fields):
                sec = sd.getElementAsString("security")
                row = {}
                if not sd.hasElement("securityError"):
                    fd = sd.getElement("fieldData")
                    for f in fields:
                        if fd.hasElement(f):
                            row[f] = fd.getElement(f).getValue()
                rows[sec] = row
        return pd.DataFrame.from_dict(rows, orient="index").reindex(columns=fields)

    def stop(self):
        self.session.stop()


def classify(row):
    notches = [n for n in (row["sp_n"], row["mdy_n"], row["fitch_n"]) if n is not None]
    if not notches:
        return pd.Series({"Index rating notch": None, "Bucket": None})
    s = sorted(notches)
    # Bloomberg index rule: middle of 3, lower of 2, else the single rating
    idx = s[1] if len(s) == 3 else s[-1]
    has_ig = any(n <= IG_FLOOR for n in s)
    has_hy = any(n >= HY_CEIL for n in s)
    if has_ig and has_hy:
        bucket = "Split IG/HY"
    elif has_ig:
        bucket = "Low IG (BBB-/Baa3)"
    else:
        bucket = "High HY (BB+/Ba1)"
    return pd.Series({"Index rating notch": idx, "Bucket": bucket})


def main():
    bbg = Bbg()
    tickers = []
    for ix in INDICES:
        m = bbg.members(ix)
        print(f"{ix}: {len(m)} members")
        tickers += m
    tickers = list(dict.fromkeys(tickers))

    df = bbg.ref(tickers, list(FIELDS))
    df["sp_n"] = df["RTG_SP_LT_LC_ISSUER_CREDIT"].map(lambda r: notch(r, SP_NOTCH))
    df["mdy_n"] = df["RTG_MOODY_LONG_TERM"].map(lambda r: notch(r, MDY_NOTCH))
    df["fitch_n"] = df["RTG_FITCH_LT_ISSUER_DEFAULT"].map(lambda r: notch(r, SP_NOTCH))
    df = df.astype({c: "object" for c in ("sp_n", "mdy_n", "fitch_n")})
    df = df.where(df.notna(), None)

    # Keep issuers with at least one agency on the BBB-/BB+ frontier
    at_frontier = df[["sp_n", "mdy_n", "fitch_n"]].isin([IG_FLOOR, HY_CEIL]).any(axis=1)
    xo = df[at_frontier].copy()
    xo = xo.join(xo.apply(classify, axis=1))
    xo["Index rating"] = xo["Index rating notch"].map(lambda n: SP_SCALE[int(n) - 1] if n else None)

    # 5Y CDS spread where a CDS ticker exists
    cds = xo["CDS_SPREAD_TICKER_5Y"].dropna()
    if not cds.empty:
        cds_secs = (cds + " Corp").tolist()
        px = bbg.ref(cds_secs, ["PX_LAST"])["PX_LAST"]
        xo["5Y CDS (bp)"] = (xo["CDS_SPREAD_TICKER_5Y"] + " Corp").map(px)
    bbg.stop()

    xo = xo.drop(columns=["sp_n", "mdy_n", "fitch_n", "Index rating notch"]).rename(columns=FIELDS)
    xo.index.name = "Ticker"
    xo = xo.sort_values(["Bucket", "5Y CDS (bp)" if "5Y CDS (bp)" in xo else "Name"])

    with pd.ExcelWriter(OUTPUT, engine="xlsxwriter") as xl:
        xo.to_excel(xl, sheet_name="All crossover")
        for bucket, grp in xo.groupby("Bucket"):
            grp.to_excel(xl, sheet_name=bucket.split(" (")[0].replace("/", "-")[:31])
        for ws in xl.sheets.values():
            ws.autofit()
            ws.freeze_panes(1, 1)

    print(f"\n{len(xo)} crossover issuers out of {len(df)} screened -> {OUTPUT}\n")
    print(xo["Bucket"].value_counts().to_string())
    cols = ["Name", "S&P", "Moody's", "Fitch", "Index rating", "Bucket", "Net debt/EBITDA", "5Y CDS (bp)"]
    with pd.option_context("display.width", 250, "display.max_rows", 500, "display.max_colwidth", 30):
        print(xo[[c for c in cols if c in xo]].to_string())


if __name__ == "__main__":
    main()
