# AFIB-M2: trading challenge toolkit

The team's tools for two competitions:

| Competition | Dates | What we trade | Main tools |
|---|---|---|---|
| **CME University Trading Challenge** | until **Fri 30 Oct, 16:00 CT** | CME futures, through CQG | `cme` signal database, trading dashboard |
| **Bloomberg Global Trading Challenge** | **16 Oct to 13 Nov** | long-only single stocks in the WLS Index, at most 20% per name | stock screen, Russell/credit event strategy |

Nothing here places orders. Every output is research to discuss before trading, and returns are before commissions.

## Start here (Windows, about 10 minutes)

**1. Install.** Paste this into PowerShell (no administrator rights needed):

```powershell
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; irm https://raw.githubusercontent.com/tejoker/AFIB-M2/main/tools/setup_windows.ps1 | iex
```

It installs Python 3.13 for your user, puts the code in `%USERPROFILE%\AFIB-M2` (not in Documents, which OneDrive may sync), creates `.venv` with the pinned packages and Bloomberg's `blpapi`, puts the `cme` command on your PATH and checks everything with `verify_setup.py`. Run the same line again later to update.

**2. Point at the shared data folder.** Team data lives in the school OneDrive folder `AFIB-data`, not in the repository. Follow [docs/OneDrive_Setup.md](docs/OneDrive_Setup.md) once, then set the path to your synced copy:

```powershell
[Environment]::SetEnvironmentVariable("AFIB_DATA_DIR", "C:\Users\<you>\OneDrive - <University>\AFIB-data", "User")
```

Without it, scripts use `data\` inside the repository, which only you see.

**3. Add keys (only for what you use).** These files are git-ignored and never leave your PC.

| For | Do |
|---|---|
| CME futures history (`cme`, dashboard) | `Copy-Item cme_challenge\.env.example cme_challenge\.env`, then paste the team's Databento API key into it |
| CQG connection tests | `Copy-Item cqg\credentials.example.json cqg\credentials.json`, then fill in your own CQG login |
| Bloomberg scripts | nothing: they use the Bloomberg Terminal logged in on the same PC |

Open a **new** terminal in the repository folder. Commands below use `python`; in VS Code the terminal activates `.venv` for you, elsewhere run `.\.venv\Scripts\Activate.ps1` first. Always run from the repository root.

## CME Trading Challenge: before each trade

```powershell
cme                                            # signal database: what each strategy says today and its track record
python cme_challenge\refresh.py --watch 15     # trading dashboard, rebuilt every 15 minutes (Ctrl+C to stop)
```

**`cme`** covers the 18 commodities. For each one and each horizon (1, 2, 5 and 15 trading days) it gives every strategy's signal today and how well that signal predicted past moves: momentum, carry, seasonality, hedge-fund positioning (CFTC), a ridge regression combining them, and for natural gas and heating oil the weather, EIA storage and Europe's TTF price. Each signal gets a verdict:

- **reliable**: survives the correction for testing hundreds of combinations (q < 0.10) and kept its sign over the last 3 years;
- **suggestive**: |t| ≥ 2 but not corrected;
- **noise**: everything else, which is most rows. The page says so rather than hiding it.

Prices are current every time it runs. During the session each market gets a provisional bar for today from Yahoo's delayed price (about 10 minutes behind) of the contract it holds, and the output names the markets concerned. After 19:00 CT the settled day is downloaded from Databento (a few cents) and replaces it. Markets that have not opened yet keep yesterday's close. Other options: `cme --offline` (download nothing), `cme --root NG` (every row for one market). Output: `futures\cme\cme.html` in the data folder.

**The dashboard** (`futures\dashboard.html`) covers all 34 markets, including equity index, rates and FX. It shows:

- the top 8 ideas where trend, carry, seasonality and the model agree, with the contract to trade and a size for $5,000 of daily risk per position;
- the natural-gas weather outlook from three forecast models;
- the days left, the next Fed decision and the challenge rules: **be flat before Fri 30 Oct 16:00 CT, and trade at least 10 contracts every day**.

One PC running `--watch` on the shared folder keeps the page current for everyone. `refresh.py` without `--watch` refreshes once. Add `--databento` to also pull new contract history and retrain the model.

Check the live price in CQG before every order: Yahoo is delayed.

## Bloomberg Global Trading Challenge: picking stocks

```powershell
python bloomberg\bgtc_screen.py               # Bloomberg PC: stock screen (resumes after a Bloomberg limit)
python research\bgtc_events\strategy.py       # any PC: Russell 1000/2000 migration and credit avoid list
python research\credit_ratings\regression.py  # any PC: implied credit ratings and IG/HY frontier names
```

| Output (in the data folder) | What it gives |
|---|---|
| `bgtc\screen_<date>.xlsx` | Ranked candidates (12-month momentum, reaction to the last earnings, analyst upside), names reporting during the challenge, a credit-danger list, and only WLS members |
| `bgtc\events_<date>.xlsx` | Stocks expected to move from the Russell 1000 to the 2000 at the December reconstitution (index buying: candidates), moves the other way (selling: avoid), names near the cut-off to watch, and the credit-outlook evidence |
| `credit_ratings\regression_<date>.xlsx` | Ratings explained by fundamentals; IG names weaker than their rating (fallen-angel risk) and HY names stronger (rising-star candidates) |

Russell dates: membership is set on **Fri 30 Oct** (rank day), preliminary lists come out **Fri 13 Nov** and changes take effect after the close on **Fri 11 Dec**. Re-run `strategy.py` after 30 Oct with that day's prices. `strategy.py --credit-veto` also drops candidates with a negative outlook at BBB-/Baa3 or below. Before buying, check each pick is in the WLS Index (`MEMB` on the Terminal) and look at its total market cap across share classes.

## Collecting data

Bloomberg data comes from each teammate's own Terminal login and is shared through the data folder:

```powershell
python bloomberg\bbg_collect.py --status          # progress per dataset and account, no API use
python bloomberg\bbg_collect.py                   # collect whatever is missing
python bloomberg\bbg_collect.py --mandatory-only  # only what the credit regression needs
```

`accounts.json` in the data folder lists the team's Bloomberg accounts (same order everywhere, never reordered). Work is split into batches saved one by one, so any account can carry on where another stopped. When an account hits Bloomberg's daily limit it stops (exit code 3) and waits out a cooldown, and another account can take over. With OneDrive, run one collector at a time and let the folder sync before the next account starts.

CME contract history comes from [Databento](https://databento.com/): CME Globex since June 2010, expired contracts included. What it costs and what to fetch is in [docs/CME_Data_Guide.md](docs/CME_Data_Guide.md).

```powershell
python cme_challenge\databento_contracts.py --estimate   # price of the download, nothing fetched
python cme_challenge\databento_contracts.py              # commodities; stops above $25 unless --yes
python bloomberg\futures_contracts.py --tiers 3          # Bloomberg PC: equity index, rates and FX contracts
python cme_challenge\continuous.py                       # roll-correct series from the contracts
```

Later runs add only the new days; `cme` and `refresh.py --databento` do this for you.

## Research scripts

These are the studies behind the tools. Each prints its results and saves a file in the data folder.

```powershell
python cme_challenge\futures_research.py window --start 10-16 --end 11-13   # seasonal window, every market
python cme_challenge\futures_research.py backtest --horizon 15              # trend and carry portfolios
python cme_challenge\futures_research.py signals --risk 5000                # today's signals and sizes
python cme_challenge\futures_research.py sizing                             # constant size vs volatility targeting
python cme_challenge\model.py                                               # walk-forward model for the dashboard
python cme_challenge\event_strategies.py                                    # pre-Fed drift, weekly reversal
python cme_challenge\weather_strategy.py                                    # natural gas weather strategies
python research\credit_ratings\check_regression_data.py                     # is the credit data complete enough?
python research\credit_ratings\crossover.py                                 # Bloomberg PC: BBB-/BB+ issuers today
```

Russell liquidity research (Bloomberg PC; collection is incomplete, see [its README](research/russell_liquidity/README.txt)):

```powershell
python research\russell_liquidity\collect_russell_research.py               # collect, resuming from its cache
python research\russell_liquidity\collect_russell_research.py --build-only  # rebuild exports from the cache
python research\russell_liquidity\recover_russell_available.py              # partial exports from saved data
```

## Repository layout

| Folder | Contents |
|---|---|
| `cme_challenge/` | CME challenge: Databento collection, roll-correct series, research, model, dashboard (`refresh.py`, `live.py`), signal database (`signal_db.py`) with positioning (`cot.py`), gas (`gas.py`) and weather (`weather.py`). No Bloomberg needed |
| `bloomberg/` | Needs a Terminal: data collector (`bbg_collect.py`), stock screen (`bgtc_screen.py`), futures contracts (`futures_contracts.py`), access checks (`query.py`, `cme_probe.py`) |
| `research/bgtc_events/` | Russell 1000/2000 migration and credit-outlook strategy for the Bloomberg challenge, from saved data |
| `research/credit_ratings/` | IG/HY crossover screen, rating regression and data checks |
| `research/russell_liquidity/` | Russell membership and liquidity research |
| `cqg/` | CQG Web API connection tests and the credentials template |
| `tools/` | Windows setup script, shared-folder check (`check_shared_data.py`), a runner that keeps the commands in `programs.txt` alive |
| `docs/` | [OneDrive setup](docs/OneDrive_Setup.md), [CME data guide](docs/CME_Data_Guide.md), the original [setup guide PDF](docs/Team_Setup_Guide_Bloomberg_Python_VSCode.pdf) (the OneDrive guide replaces its shared-folder part) |
| `tests/` | Offline tests: no Bloomberg, CQG or network needed |
| `vendor/cqg_proto/` | CQG's generated protocol files, under `vendor/cqg_proto/LICENSE.md` |
| `cme`, `cme.bat` | The `cme` command (Linux/macOS and Windows) |
| `data_root.py`, `futures_markets.py` | Where the data folder is; the futures markets and their contract details |

## Rules for the repository

- **The repository is public. Never commit data or secrets.** Bloomberg data is licensed to each user. `.gitignore` already keeps out `data/`, `.env`, credentials, spreadsheets, parquet files, logs and `.venv`.
- Run `python -m unittest discover -s tests` before pushing; it takes about 30 seconds.
- Without Windows: create `.venv` with `python3 -m venv .venv` and install `requirements.txt` (`blpapi` only matters on a Bloomberg PC). Then use `./cme` and `.venv/bin/python` in place of `cme` and `python`.
