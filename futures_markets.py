"""The futures markets the CME challenge research follows: one table for the Bloomberg and Yahoo sides.

Keyed by Bloomberg root (exactly as in its tickers, e.g. "C " for corn). Liquid months are the contract months
that actually trade (others are skipped). Point values are USD per 1.0 of the quoted price, as quoted by Yahoo
and CQG (grains and livestock in cents). Micro contracts move like the full-size ones and help size small
positions or meet the challenge's 10-contracts-a-day minimum cheaply.
"""
from collections import namedtuple

Market = namedtuple("Market", "name sector months bbg_key tier yahoo exchange point_value micro micro_point_value")
ALL_MONTHS = "FGHJKMNQUVXZ"  # Jan ... Dec

MARKETS = {
    "LC": Market("Live Cattle", "Livestock", "GJMQVZ", "Comdty", 1, "LE", "CME", 400, None, None),
    "LH": Market("Lean Hogs", "Livestock", "GJKMNQVZ", "Comdty", 2, "HE", "CME", 400, None, None),
    "FC": Market("Feeder Cattle", "Livestock", "FHJKQUVX", "Comdty", 2, "GF", "CME", 500, None, None),
    "C ": Market("Corn", "Grains", "HKNUZ", "Comdty", 2, "ZC", "CBT", 50, None, None),
    "S ": Market("Soybeans", "Grains", "FHKNQUX", "Comdty", 2, "ZS", "CBT", 50, None, None),
    "SM": Market("Soybean Meal", "Grains", "FHKNQUVZ", "Comdty", 2, "ZM", "CBT", 100, None, None),
    "BO": Market("Soybean Oil", "Grains", "FHKNQUVZ", "Comdty", 2, "ZL", "CBT", 600, None, None),
    "W ": Market("Chicago SRW Wheat", "Grains", "HKNUZ", "Comdty", 2, "ZW", "CBT", 50, None, None),
    "KW": Market("KC HRW Wheat", "Grains", "HKNUZ", "Comdty", 2, "KE", "CBT", 50, None, None),
    "CL": Market("WTI Crude Oil", "Energy", ALL_MONTHS, "Comdty", 2, "CL", "NYM", 1000, "MCL", 100),
    "NG": Market("Henry Hub Natural Gas", "Energy", ALL_MONTHS, "Comdty", 2, "NG", "NYM", 10000, None, None),
    "HO": Market("NY Harbor ULSD", "Energy", ALL_MONTHS, "Comdty", 2, "HO", "NYM", 42000, None, None),
    "XB": Market("RBOB Gasoline", "Energy", ALL_MONTHS, "Comdty", 2, "RB", "NYM", 42000, None, None),
    "GC": Market("Gold", "Metals", "GJMQVZ", "Comdty", 2, "GC", "CMX", 100, "MGC", 10),
    "SI": Market("Silver", "Metals", "HKNUZ", "Comdty", 2, "SI", "CMX", 5000, "SIL", 1000),
    "HG": Market("Copper", "Metals", "HKNUZ", "Comdty", 2, "HG", "CMX", 25000, "MHG", 2500),
    "PL": Market("Platinum", "Metals", "FJNV", "Comdty", 2, "PL", "NYM", 50, None, None),
    "PA": Market("Palladium", "Metals", "HMUZ", "Comdty", 2, "PA", "NYM", 100, None, None),
    "ES": Market("E-mini S&P 500", "Equity index", "HMUZ", "Index", 3, "ES", "CME", 50, "MES", 5),
    "NQ": Market("E-mini Nasdaq-100", "Equity index", "HMUZ", "Index", 3, "NQ", "CME", 20, "MNQ", 2),
    "RTY": Market("E-mini Russell 2000", "Equity index", "HMUZ", "Index", 3, "RTY", "CME", 50, "M2K", 5),
    "DM": Market("E-mini Dow", "Equity index", "HMUZ", "Index", 3, "YM", "CBT", 5, "MYM", 0.5),
    "TU": Market("2Y T-Note", "Rates", "HMUZ", "Comdty", 3, "ZT", "CBT", 2000, None, None),
    "FV": Market("5Y T-Note", "Rates", "HMUZ", "Comdty", 3, "ZF", "CBT", 1000, None, None),
    "TY": Market("10Y T-Note", "Rates", "HMUZ", "Comdty", 3, "ZN", "CBT", 1000, None, None),
    "US": Market("T-Bond", "Rates", "HMUZ", "Comdty", 3, "ZB", "CBT", 1000, None, None),
    "WN": Market("Ultra T-Bond", "Rates", "HMUZ", "Comdty", 3, "UB", "CBT", 1000, None, None),
    "SFR": Market("3M SOFR", "Rates", "HMUZ", "Comdty", 3, "SR3", "CME", 2500, None, None),
    "EC": Market("Euro FX", "FX", "HMUZ", "Curncy", 3, "6E", "CME", 125000, "M6E", 12500),
    "JY": Market("Japanese Yen", "FX", "HMUZ", "Curncy", 3, "6J", "CME", 12500000, None, None),
    "BP": Market("British Pound", "FX", "HMUZ", "Curncy", 3, "6B", "CME", 62500, "M6B", 6250),
    "AD": Market("Australian Dollar", "FX", "HMUZ", "Curncy", 3, "6A", "CME", 100000, "M6A", 10000),
    "CD": Market("Canadian Dollar", "FX", "HMUZ", "Curncy", 3, "6C", "CME", 100000, None, None),
    "SF": Market("Swiss Franc", "FX", "HMUZ", "Curncy", 3, "6S", "CME", 125000, None, None),
}
