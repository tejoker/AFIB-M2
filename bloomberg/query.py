import blpapi

SECURITIES = ["AAPL US Equity", "MSFT US Equity", "SPX Index", "EURUSD Curncy"]
FIELDS = ["NAME", "PX_LAST", "CHG_PCT_1D", "CRNCY"]


def main():
    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)

    session = blpapi.Session(options)
    if not session.start():
        raise SystemExit("Failed to start session (is the Bloomberg Terminal logged in?)")
    if not session.openService("//blp/refdata"):
        raise SystemExit("Failed to open //blp/refdata")

    service = session.getService("//blp/refdata")
    request = service.createRequest("ReferenceDataRequest")
    for sec in SECURITIES:
        request.append("securities", sec)
    for fld in FIELDS:
        request.append("fields", fld)

    session.sendRequest(request)

    print(f"{'Security':<18}" + "".join(f"{f:<28}" for f in FIELDS))
    while True:
        event = session.nextEvent(5000)
        for msg in event:
            if not msg.hasElement("securityData"):
                continue
            for sd in msg.getElement("securityData").values():
                ticker = sd.getElementAsString("security")
                if sd.hasElement("securityError"):
                    print(f"{ticker:<18}ERROR: {sd.getElement('securityError')}")
                    continue
                data = sd.getElement("fieldData")
                row = [
                    str(data.getElementValue(f)) if data.hasElement(f) else "N/A"
                    for f in FIELDS
                ]
                print(f"{ticker:<18}" + "".join(f"{v[:26]:<28}" for v in row))
        if event.eventType() == blpapi.Event.RESPONSE:
            break

    session.stop()


if __name__ == "__main__":
    main()
