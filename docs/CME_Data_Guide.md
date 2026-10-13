# CME challenge data: what we fetch, from where, and what it costs

Everything the trading desk dashboard and the strategy research use, and the rules that keep the Databento credit ($125 on a new account) from being wasted. Commands run from the repository root; data goes to the shared folder set by `AFIB_DATA_DIR` (see [OneDrive_Setup.md](OneDrive_Setup.md)).

## Sources at a glance

| What | Source | Cost | Who runs it | How often |
|---|---|---|---|---|
| Live prices, contract to trade, trend, carry, sizes | Yahoo (`cme_challenge/live.py`) | Free | Whoever runs the dashboard | Every refresh (`refresh.py --watch 15`) |
| Commodity contract history (seasonality, backtests, model) | Databento, CME Globex daily bars | About $9 once for all 18 commodities since 2010; cents per daily update | One teammate, who holds the key | Once, then `refresh.py --databento` daily |
| Contract definitions (delivery month, expiry) | Databento | A few cents in total | Same teammate (automatic) | With each Databento run |
| Equity index, rates and FX history | Yahoo continuous series (fallback, roll gaps kept, no carry) | Free | Automatic in `continuous.py` | Each rebuild |
| US gas-demand weather outlook (heating and cooling, 26 cities) | Open-Meteo: ECMWF AIFS (AI), ECMWF IFS, NOAA GFS | Free (non-commercial) | Automatic in `refresh.py` and `cme` | Every refresh |
| Archived weather forecasts and actual weather (signal history, normals) | Open-Meteo Previous Runs and Archive APIs | Free (non-commercial) | Automatic; first download about 20 minutes | Daily in `cme` |
| Hedge fund positioning, all 18 commodities | CFTC Commitments of Traders, disaggregated (publicreporting.cftc.gov) | Free, no key | Automatic in `cme` | Weekly (Friday) |
| Natural gas storage, LNG exports, dry gas production | EIA history workbooks (eia.gov/dnav) | Free, no key | Automatic in `cme` | Weekly (Thursday) / monthly |
| European gas price (TTF) | Yahoo (`TTF=F`) | Free | Automatic in `cme` | Every run |
| Active tropical storms | NOAA National Hurricane Center | Free | Automatic in `cme` | Every run |

Bloomberg is optional: `bloomberg/futures_contracts.py --tiers 3` can replace the Yahoo fallback for financial futures on a PC with the Terminal.

## What to fetch from Databento

Fetch **daily bars for the 18 commodity markets, June 2010 to today**. The script already restricts the request to what we need:

| Sector | Markets (CME symbols) |
|---|---|
| Livestock | LE live cattle, HE lean hogs, GF feeder cattle |
| Grains | ZC corn, ZS soybeans, ZM soybean meal, ZL soybean oil, ZW Chicago wheat, KE KC wheat |
| Energy | CL WTI crude, NG natural gas, HO heating oil (ULSD), RB RBOB gasoline |
| Metals | GC gold, SI silver, HG copper, PL platinum, PA palladium |

Do **not** fetch:
- **Calendar spreads.** Asking for a whole product (`CL.FUT`) includes thousands of spreads and costs about 7 times more. The script asks only for outright contracts in the liquid months.
- **Intraday data** (trades, 1-minute or 1-second bars). Far more expensive and not needed for 15-day holds.
- **Options, or financial futures.** Yahoo is good enough for equity index, rates and FX trend.

## Rules for the credit

1. **Estimate first, every time.** `--estimate` asks Databento's price and downloads nothing. The download itself stops above $25 unless `--yes` is added.
2. **One person holds the key and downloads.** The key lives only in `cme_challenge/.env` on that PC. Never paste it in a chat, email or committed file: the repository is public.
3. **Never download the same history twice.** Databento's answer is kept in `futures/databento/raw/`. After a code change, reprocess it with `--rebuild`, which buys no bars.
4. **Keep `futures/databento/raw/` in the shared folder.** It is the paid data; everything else can be rebuilt from it.
5. **Daily updates only fetch the new days** (a few cents). Use `refresh.py --databento` once a day, not in `--watch` loops.

## Commands

First download (one teammate, once):

```powershell
Copy-Item cme_challenge\.env.example cme_challenge\.env      # paste the key from databento.com > API keys
python cme_challenge\databento_contracts.py --estimate       # price, nothing downloaded
python cme_challenge\databento_contracts.py                  # all 18 commodities, about 2-3 minutes
python cme_challenge\refresh.py --databento                  # series, model and dashboard
```

Daily:

```powershell
python cme_challenge\refresh.py --databento                  # morning: add yesterday's bars, retrain
python cme_challenge\refresh.py --watch 15                   # during trading, on one PC
```

After a fix to the collector (no bars bought):

```powershell
python cme_challenge\databento_contracts.py --rebuild
python cme_challenge\continuous.py
python cme_challenge\model.py
```

Only some markets: `--roots LE HE CL` (CME symbols).

## What has been fetched (12 October 2026)

- All 18 commodities, June 2010 to 9 October 2026: 2,475 contracts. Prices agree with Yahoo within a median 0.04-0.71% (the gap is the UTC-day close versus settlement); expiry dates come from Databento's definitions.
- Credit used: about $17.80 of $125. That is $8.75 for the history, $8.75 again after a bug that the saved raw copy now makes unnecessary, $0.14 to fill a natural-gas gap (CME writes NG with two-digit years, NGZ26, since mid-2025) and cents for definitions.
- The data sits on the Ubuntu PC that downloaded it, under `data/futures/`. Upload that folder into `AFIB-data` (OneDrive in a browser: Upload > Folder) so nobody pays for it again.

## Gas drivers not covered yet

These move natural gas but have no free daily source wired in: LNG feedgas flows (terminal by terminal, daily), European storage (GIE AGSI+, free key), US power demand including data centers (EIA-930, free key), Baker Hughes rig counts. Data centers raise demand over years and are priced in the contracts months out; for 1-15 day trades the weather, storage and LNG outages matter more.

## Known limits of the data

- Databento's daily bars for CME (`ohlcv-1d`) run from midnight to midnight UTC, so a bar's close is the last trade before about 7 pm Chicago time, close to but not exactly the official settlement.
- Databento flags a few days in June 2014 as degraded quality.
- Yahoo prices are delayed about 10 minutes; CQG shows the live price.
- Fallback series for equity index, rates and FX keep Yahoo's small roll gaps and have no carry.
- Seasonality and model results are history before commissions, not trade recommendations.
