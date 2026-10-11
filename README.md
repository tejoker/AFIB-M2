# AFIB-M2: Bloomberg and CQG research tools

Python tools for the team's Bloomberg data collection, credit-rating and Russell liquidity research, and preparation for the CME University Trading Challenge (CQG connectivity, futures research).

The full Python/Bloomberg setup walkthrough is in [docs/Team_Setup_Guide_Bloomberg_Python_VSCode.pdf](docs/Team_Setup_Guide_Bloomberg_Python_VSCode.pdf). For shared CME and Bloomberg research data on school OneDrive, follow [docs/OneDrive_Setup.md](docs/OneDrive_Setup.md); it supersedes the PDF's shared-folder instructions.

## Layout

| Folder | Contents |
|---|---|
| `bloomberg/` | `bbg_collect.py` (resumable, multi-account collector), `futures_contracts.py` (futures contract histories from Bloomberg), `query.py` (quote example), `cme_probe.py` (checks Bloomberg access to CME futures data) |
| `research/credit_ratings/` | IG/HY crossover screen (`crossover.py`) and regression data checks |
| `research/russell_liquidity/` | Russell membership and liquidity research; see its [README](research/russell_liquidity/README.txt) |
| `cme_challenge/` | CME challenge research: contract histories from Databento, roll-correct series, strategy research (seasonal windows, trend, carry), the model and the trading desk dashboard; no Bloomberg login needed |
| `cqg/` | CQG Web API connectivity tests and the credentials template |
| `tests/` | Offline tests; no Bloomberg or CQG access needed |
| `tools/` | `check_shared_data.py` verifies the team folder; `runner.py` keeps the commands in `tools/programs.txt` running |
| `vendor/cqg_proto/` | CQG's generated protocol modules, under the terms in `vendor/cqg_proto/LICENSE.md` |
| `docs/` | Setup guide and its generator |

Run every script from the repository root, e.g. `python bloomberg/bbg_collect.py --status`.

## Setup on Windows

Python 3.13, 64-bit. The versions are those of the working environment; `verify_setup.py` checks them.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-bloomberg.txt
.\.venv\Scripts\python.exe verify_setup.py
```

`blpapi` is not on PyPI; `requirements-bloomberg.txt` installs it from Bloomberg's package index. Bloomberg Desktop API scripts need a Bloomberg Terminal logged in on the same PC (`localhost:8194`).

For CQG, copy the template and fill in your own account details locally:

```powershell
Copy-Item cqg\credentials.example.json cqg\credentials.json
```

## Data stays out of the repository

Nothing downloaded from Bloomberg or CQG is committed: Bloomberg data is licensed to each user, and this repository is public. The main Bloomberg collector's data folder holds its downloaded datasets, progress, frozen ticker universe and team account list (`accounts.json`).

- By default, the scripts use `data/` in the repository (git-ignored); `data_root.py` decides the folder for all of them. The Russell research scripts keep their own `--output` folder.
- To share data between teammates, use the [OneDrive setup guide](docs/OneDrive_Setup.md). Each machine points `AFIB_DATA_DIR` at its own synced copy of the same school folder. Existing `BBG_DATA_DIR` setups remain supported:

```powershell
[Environment]::SetEnvironmentVariable("AFIB_DATA_DIR", "C:\Users\<you>\OneDrive - <University>\AFIB-data", "User")
```

Credentials (`cqg/credentials.json`, password and secret files), virtual environments, logs and exports are excluded by `.gitignore`.

## Bloomberg collector

```powershell
python bloomberg/bbg_collect.py --status          # progress per dataset and account, no API use
python bloomberg/bbg_collect.py                   # collect whatever is missing
python bloomberg/bbg_collect.py --mandatory-only  # only what the credit regression needs
```

Each teammate runs it with their own Bloomberg login; `accounts.json` in the data folder lists the accounts (same order everywhere, never reordered). Work is split into batches and progress is saved after each one. With OneDrive, use one active collector at a time and wait for sync before another account resumes. An account that reaches Bloomberg's limit stops for the configured cooldown; another account can continue after the handoff.

## CME research without Bloomberg

```powershell
python cme_challenge/fetch_commodities.py
python cme_challenge/summarize_seasonality.py
```

These commands save Yahoo continuous front-month commodity proxies and a descriptive calendar-month summary under `commodities/` in `AFIB_DATA_DIR` or the local `data/` fallback. They do not connect to Bloomberg or place trades. Roll discontinuities prevent the summary from being treated as tradable futures P&L.

## Futures strategy research (contract-level)

Yahoo's continuous series jump whenever the front contract changes, which fakes seasonal signals (October lean hogs is one). These scripts work from individual contracts instead, saved under `futures/` in the data folder.

Commodity contract history comes from [Databento](https://databento.com/) (CME Globex since June 2010, expired contracts included; new accounts get free credit). One teammate creates an account and stores the API key:

```powershell
[Environment]::SetEnvironmentVariable("DATABENTO_API_KEY", "db-...", "User")   # once; reopen the terminal
python cme_challenge/databento_contracts.py --estimate    # Databento's price for the download; nothing fetched
python cme_challenge/databento_contracts.py               # all commodities; stops above $25 unless --yes
```

Later runs add only the new days. Financial futures can come from Databento too (`--sectors all`) or from Bloomberg, on a PC with the Terminal (resumable; exit code 3 at a Bloomberg limit):

```powershell
python bloomberg/futures_contracts.py --tiers 3                      # equity index, rates and FX
python bloomberg/futures_contracts.py --status                       # progress and requests left, no API use
```

Then, on any PC:

```powershell
python cme_challenge/continuous.py                                   # roll-correct series (Databento where available)
python cme_challenge/futures_research.py window --start 10-09 --end 10-30 --detail LC
python cme_challenge/futures_research.py backtest --horizon 15       # trend and carry portfolios
python cme_challenge/futures_research.py signals --risk 5000         # current signals and position sizes
python cme_challenge/model.py                                        # walk-forward model for the dashboard
```

Returns are within-contract price changes before commissions. They describe history and are not trade recommendations.

## Trading desk: latest data and model view before each trade

```powershell
python cme_challenge/refresh.py --watch 15      # refresh every 15 minutes during trading; Ctrl+C to stop
python cme_challenge/refresh.py                 # refresh once and open the dashboard
python cme_challenge/refresh.py --databento     # also add the latest Databento history and retrain the model
```

Each refresh:
- pulls the latest prices for all 34 markets from Yahoo (delayed about 10 minutes) and picks the contract to trade;
- recomputes trend, carry, seasonality and the model's view, with position sizes;
- fetches the US heating-demand outlook from ECMWF's AI model (AIFS), ECMWF IFS and NOAA GFS through [Open-Meteo](https://open-meteo.com/), which is free for non-commercial use;
- writes `futures/dashboard.html` in the data folder.

With the shared OneDrive folder, one PC running `--watch` keeps the dashboard current for the whole team; the page reloads itself in the browser. Seasonality and the model view appear once the contract history has been collected (previous section). The first refresh also downloads ten years of weather normals (about four minutes, once). To refresh without a terminal open, schedule `refresh.py --no-browser` with Windows Task Scheduler.

## Research workflow (Russell liquidity)

```powershell
python research/russell_liquidity/collect_russell_research.py
python research/russell_liquidity/collect_russell_research.py --build-only
python research/russell_liquidity/recover_russell_available.py
```

The first command collects data and resumes using completed cached responses. `--build-only` rebuilds exports from the collector's cache; `recover_russell_available.py` also combines available data from earlier collections.

Collection is currently incomplete following a Bloomberg daily capacity response. Exports must be checked for missing data and coverage before regressions. The scripts do not establish that exclusion from the S&P 500 causes lower liquidity or creates an arbitrage opportunity.

## Tests

```powershell
python -m unittest discover -s tests
```
