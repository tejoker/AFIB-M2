# Bloomberg and CQG research scripts

Python scripts for Bloomberg data collection, Russell 1000 / Russell 2000 / S&P 500 membership and liquidity research, and CQG connectivity checks.

## Setup on Windows

The recorded dependency versions come from the working Python 3.13 environment.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-bloomberg.txt
```

Bloomberg Desktop API scripts require a logged-in Bloomberg Terminal and access to the requested datasets. The default connection uses `localhost:8194`. Bloomberg's package installation instructions are available on its [API library page](https://professional.bloomberg.com/support/api-library/).

For CQG, copy the template and fill in your own account details locally:

```powershell
Copy-Item cqg_credentials.example.json cqg_credentials.json
```

`cqg_credentials.json`, password files, environment secrets, virtual environments, logs, and generated data exports are excluded by `.gitignore`. Keep account values in those local files. The example contains placeholders only.

## Research workflow

```powershell
python collect_russell_research.py
python collect_russell_research.py --build-only
python recover_russell_available.py
python -m unittest test_russell_research -v
```

The first command collects data and resumes using completed cached responses. `--build-only` rebuilds exports from the collector's cache; `recover_russell_available.py` also combines available data from earlier collections. Read [russell_research_README.txt](russell_research_README.txt) for coverage, provenance, field definitions, and regression precautions.

Collection is currently incomplete following a Bloomberg daily capacity response. Exports must be checked for missing data and coverage before regressions. The scripts do not establish that exclusion from the S&P 500 causes lower liquidity or creates an arbitrage opportunity.

Generated datasets stay local under `data/` and are not included in this source repository. The generated CQG protocol modules under `cqg_proto/` are needed by `cqg_logon_test.py`; their upstream terms are in `cqg_proto/LICENSE.md`.
