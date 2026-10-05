"""Download daily history for the CME Group commodity futures tradable in the CME University Trading Challenge.

Yahoo's "=F" tickers are continuous front-month series: they jump on roll dates, so returns across a
roll are not real P&L. Fine for seasonality / mean-reversion research, not for exact backtest fills.
"""
from pathlib import Path

import pandas as pd
import yfinance as yf

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "commodities"

# (yahoo ticker, CME symbol, name, exchange, sector)
UNIVERSE = [
    # Energy - NYMEX
    ("CL=F", "CL", "WTI Crude Oil", "NYMEX", "Energy"),
    ("BZ=F", "BZ", "Brent Crude Oil (last day financial)", "NYMEX", "Energy"),
    ("NG=F", "NG", "Henry Hub Natural Gas", "NYMEX", "Energy"),
    ("HO=F", "HO", "NY Harbor ULSD (Heating Oil)", "NYMEX", "Energy"),
    ("RB=F", "RB", "RBOB Gasoline", "NYMEX", "Energy"),
    # Micro WTI (MCL) tracks CL; Yahoo only serves 5 days for MCL=F, so research it with CL.
    # Metals - COMEX / NYMEX
    ("GC=F", "GC", "Gold", "COMEX", "Metals"),
    ("MGC=F", "MGC", "Micro Gold", "COMEX", "Metals"),
    ("SI=F", "SI", "Silver", "COMEX", "Metals"),
    ("SIL=F", "SIL", "Micro Silver (1,000 oz)", "COMEX", "Metals"),
    ("HG=F", "HG", "Copper", "COMEX", "Metals"),
    ("ALI=F", "ALI", "Aluminum", "COMEX", "Metals"),
    ("PL=F", "PL", "Platinum", "NYMEX", "Metals"),
    ("PA=F", "PA", "Palladium", "NYMEX", "Metals"),
    # Grains & oilseeds - CBOT
    ("ZC=F", "ZC", "Corn", "CBOT", "Grains"),
    ("ZW=F", "ZW", "Chicago SRW Wheat", "CBOT", "Grains"),
    ("KE=F", "KE", "KC HRW Wheat", "CBOT", "Grains"),
    ("ZS=F", "ZS", "Soybeans", "CBOT", "Grains"),
    ("ZM=F", "ZM", "Soybean Meal", "CBOT", "Grains"),
    ("ZL=F", "ZL", "Soybean Oil", "CBOT", "Grains"),
    ("ZO=F", "ZO", "Oats", "CBOT", "Grains"),
    ("ZR=F", "ZR", "Rough Rice", "CBOT", "Grains"),
    # Livestock & dairy - CME
    ("LE=F", "LE", "Live Cattle", "CME", "Livestock"),
    ("GF=F", "GF", "Feeder Cattle", "CME", "Livestock"),
    ("HE=F", "HE", "Lean Hogs", "CME", "Livestock"),
    ("DC=F", "DC", "Class III Milk", "CME", "Dairy"),
    # Forest
    ("LBR=F", "LBR", "Lumber", "CME", "Forest"),
]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tickers = [u[0] for u in UNIVERSE]
    raw = yf.download(tickers, period="max", interval="1d", auto_adjust=False,
                      group_by="ticker", progress=False, threads=True)

    rows, closes = [], {}
    for ticker, symbol, name, exchange, sector in UNIVERSE:
        df = raw[ticker].dropna(how="all") if ticker in raw.columns.get_level_values(0) else pd.DataFrame()
        ok = not df.empty
        if ok:
            df.to_parquet(OUT_DIR / f"{symbol}.parquet")
            closes[symbol] = df["Close"]
        rows.append({
            "symbol": symbol, "yahoo": ticker, "name": name, "exchange": exchange, "sector": sector,
            "rows": len(df), "start": df.index.min().date() if ok else None,
            "end": df.index.max().date() if ok else None,
            "last_close": round(float(df["Close"].iloc[-1]), 4) if ok else None,
        })

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "universe.csv", index=False)
    pd.DataFrame(closes).sort_index().to_parquet(OUT_DIR / "closes.parquet")

    pd.set_option("display.width", 200)
    print(summary.to_string(index=False))
    missing = summary[summary["rows"] == 0]["symbol"].tolist()
    print(f"\nSaved {len(closes)}/{len(UNIVERSE)} series to {OUT_DIR}" + (f" | no data: {missing}" if missing else ""))


if __name__ == "__main__":
    main()
