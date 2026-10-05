# AFIB-M2: Bloomberg and CQG research tools

Python tools for the team's Bloomberg data collection, credit-rating and Russell liquidity research, and preparation for the CME University Trading Challenge (CQG connectivity, futures research).

The full setup walkthrough for a new teammate is in [docs/Team_Setup_Guide_Bloomberg_Python_VSCode.pdf](docs/Team_Setup_Guide_Bloomberg_Python_VSCode.pdf).

## Layout

| Folder | Contents |
|---|---|
| `bloomberg/` | `bbg_collect.py` (resumable, multi-account collector), `query.py` (quote example), `cme_probe.py` (checks Bloomberg access to CME futures data) |
| `research/credit_ratings/` | IG/HY crossover screen (`crossover.py`) and regression data checks |
| `research/russell_liquidity/` | Russell membership and liquidity research; see its [README](research/russell_liquidity/README.txt) |
| `cme_challenge/` | CME challenge research, e.g. commodity futures history (`fetch_commodities.py`) |
| `cqg/` | CQG Web API connectivity tests and the credentials template |
| `tests/` | Offline tests; no Bloomberg or CQG access needed |
| `tools/` | `runner.py` keeps the commands in `tools/programs.txt` running |
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

Nothing downloaded from Bloomberg or CQG is committed: Bloomberg data is licensed to each user, and this repository is public. The data folder holds the downloaded datasets, collector progress, the frozen ticker universe and the team account list (`accounts.json`).

- By default, scripts use `data/` in the repository (git-ignored).
- To share collection between accounts, put the data folder in the team's OneDrive/SharePoint folder and point every machine at it:

```powershell
[Environment]::SetEnvironmentVariable("BBG_DATA_DIR", "C:\Users\<you>\OneDrive - <University>\AFIB-data", "User")
```

Credentials (`cqg/credentials.json`, password and secret files), virtual environments, logs and exports are excluded by `.gitignore`.

## Bloomberg collector

```powershell
python bloomberg/bbg_collect.py --status          # progress per dataset and account, no API use
python bloomberg/bbg_collect.py                   # collect whatever is missing
python bloomberg/bbg_collect.py --mandatory-only  # only what the credit regression needs
```

Each teammate runs it with their own Bloomberg login; `accounts.json` in the data folder lists the accounts (same order everywhere, never reordered). Work is split into batches, progress is saved after each one, and an account that reaches Bloomberg's daily limit stops for 12 hours while the others continue.

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
