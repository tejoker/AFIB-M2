# Shared challenge data on school OneDrive

The same synced folder can hold CME research data and Bloomberg research data. The CME Yahoo downloader works without a Bloomberg Terminal. Bloomberg API scripts still connect to a logged-in Terminal on the same PC at `localhost:8194`; OneDrive shares saved files, not API sessions. The two challenges have different datasets and rules, so keep their analyses separate.

## One-time upload from a computer without the sync app

Microsoft's OneDrive sync app runs on Windows and macOS only. On Linux, or any computer without it, you can still put data in the shared folder once through the browser:

1. Sign in at [Microsoft 365](https://www.office.com/) with your school Microsoft account and open OneDrive. Complete the school's sign-in and multifactor prompts yourself; never store your password in this repository or in a file.
2. Under **My files**, create `AFIB-data`, or open it under **Shared** if a teammate already created it. Choose **Upload > Folder** (the browser may label it **Add new > Folder upload**) and select the `data/commodities` folder of your checkout. Wait for the upload to finish, then confirm that `commodities/universe.csv` and `commodities/seasonality_summary.csv` appear online.
3. If you created `AFIB-data`, share it with the named teammates using **Can edit** access. Each teammate can find it under **Shared** and choose **Add shortcut to My files**.

A browser upload is a one-off copy; it does not keep a local folder in sync. To read and write the shared folder automatically from Linux, first configure a school-approved Linux sync or mount client that uses Microsoft browser sign-in, then follow [From a Linux checkout](#from-a-linux-checkout). Windows and macOS teammates use Microsoft's OneDrive app as described next.

## Set up automatic sync on a Windows PC

1. Use the same `AFIB-data` folder created above, or create one in a school team SharePoint library or school OneDrive if it does not exist yet. Give named teammates edit access and keep it separate from the public Git repository. Check with your Terminal administrator that the planned team access to Bloomberg-derived data is permitted.
2. Sign in to the OneDrive app with your school Microsoft account. Each teammate adds that shared folder to their OneDrive or syncs the SharePoint library to their PC. In File Explorer, select **Always keep on this device** for `AFIB-data` and wait for OneDrive to show **Up to date**. Copy the folder's actual local path from File Explorer; the example path below is only a placeholder.
3. One teammate initializes the synced folder from the repository root. Replace the example path with the real local path:

```powershell
.\.venv\Scripts\python.exe tools\check_shared_data.py --path 'C:\Users\YOU\OneDrive - SCHOOL\AFIB-data' --init
```

This creates `team_storage.json` in the shared folder. Do this once only. The command prints a **Storage ID**; share that ID with the team so everyone can compare it. Bloomberg's `accounts.json` can be added later, before the Bloomberg collector runs.

## Configure each PC

Set the path to the synced copy on **that PC**; the paths do not need to be identical across machines.

```powershell
[Environment]::SetEnvironmentVariable('AFIB_DATA_DIR', 'C:\Users\YOU\OneDrive - SCHOOL\AFIB-data', 'User')
```

Close and reopen the terminal so it receives the new environment variable. Then run:

```powershell
.\.venv\Scripts\python.exe tools\check_shared_data.py
```

The check confirms that this local folder is writable and its Storage ID matches the team's ID. It cannot prove that OneDrive has finished uploading; check the OneDrive **Up to date** indicator too. `AFIB_DATA_DIR` is the preferred shared root for both workflows; the old `BBG_DATA_DIR` name still works if `AFIB_DATA_DIR` is unset.

## CME research without Bloomberg

After installing `requirements.txt`, run these from the repository root:

```powershell
.\.venv\Scripts\python.exe cme_challenge\fetch_commodities.py
.\.venv\Scripts\python.exe cme_challenge\summarize_seasonality.py
```

The downloader saves daily Yahoo continuous front-month proxy series in `AFIB-data\commodities\` as Parquet, plus close-price CSV files for the seasonality summary. `universe.csv` records each symbol's coverage and retrieval time. The summary reports historical within-month price changes by calendar month, sample years and positive-year frequency. Months with a very large daily price ratio or nonpositive close are retained in `seasonality_months.csv` but excluded from the aggregate, with an exclusion count. It is descriptive research: roll jumps and data errors can still distort these prices, so the output is not a futures backtest or a trade recommendation. Do not run the downloader simultaneously on two synced PCs.

If the OneDrive folder is not synced yet, these commands write to the repo's ignored `data\commodities\` folder. Once it is synced and `AFIB_DATA_DIR` is set, copy the staged data if the destination has no `commodities` folder yet:

```powershell
if (Test-Path "$env:AFIB_DATA_DIR\commodities") { throw 'Destination already exists; inspect it before copying.' }
Copy-Item -Path .\data\commodities -Destination $env:AFIB_DATA_DIR -Recurse
```

Wait for OneDrive to say **Up to date**, then run `summarize_seasonality.py` again against the shared copy.

### From a Linux checkout

A Windows OneDrive path is not available to a Linux shell. First make the synced `AFIB-data` folder accessible on the Linux machine and use its **Linux** path below. Run from the repository root:

```bash
export AFIB_DATA_DIR='/absolute/path/to/synced/AFIB-data'
python3 tools/check_shared_data.py --init  # only the first teammate, once
if test -e "$AFIB_DATA_DIR/commodities"; then
    echo 'Destination exists; inspect it before copying.' >&2
else
    cp -a data/commodities "$AFIB_DATA_DIR/"
fi
python3 cme_challenge/summarize_seasonality.py
```

For later shells, set `AFIB_DATA_DIR` again or add the export to your shell configuration. If the synced folder is only on another computer, copy the staged `data/commodities/` folder to that computer first, or use the [one-time browser upload](#one-time-upload-from-a-computer-without-the-sync-app).

## Bloomberg collector and handoff

Before Bloomberg collection, agree on the exact collector account names. They identify each teammate's progress and are separate from Microsoft email addresses. Use each person's Windows user name (`$env:USERNAME`; quote names with spaces), or consistently pass `--account NAME`. One teammate adds the ordered list to the shared folder:

```powershell
.\.venv\Scripts\python.exe tools\check_shared_data.py --set-accounts --accounts alice bob carol
.\.venv\Scripts\python.exe bloomberg\bbg_collect.py --account alice --status
```

Keep that account list and order consistent. Before the first collection, `--status` reports that there is no universe yet.

Run **one collector at a time** when using OneDrive. Start with any listed account:

```powershell
.\.venv\Scripts\python.exe bloomberg\bbg_collect.py --account alice
```

The collector writes Parquet parts and saves progress after each completed batch. When it stops, run `--status` and wait for OneDrive to say **Up to date** on that PC. The next teammate waits for their PC to reach **Up to date**, runs `check_shared_data.py` to confirm the Storage ID, and runs `--status` to confirm the same completed counts before collecting. The collector reads all accounts’ progress and fetches only unfinished tickers. A user whose Bloomberg limit was reached remains on cooldown; another user can continue after the handoff.

If two machines collect simultaneously through OneDrive, its delayed sync can hide recent progress and claims, causing duplicate API requests or conflicted files. A normal network drive with immediate visibility is needed for the collector’s concurrent claim mechanism. Also avoid running the same account on two machines at once.

The Bloomberg collector, CME probe, credit crossover, CME downloader, seasonality summary and `check_shared_data.py` all take the folder from `data_root.py`: `AFIB_DATA_DIR`, else `BBG_DATA_DIR` as a compatibility fallback for existing Bloomberg setups. Russell research scripts keep their own `--output` location, so their existing caches are still found.

## Where files appear

| Shared folder entry | Purpose |
|---|---|
| `team_storage.json` | Folder identity shared across PCs |
| `accounts.json` | Ordered Bloomberg collector account list, added before Bloomberg collection |
| `commodities/` | CME Yahoo history and descriptive seasonality outputs |
| `universe.parquet`, `fields.parquet` | Frozen ticker universe and known fields |
| `state/progress_ACCOUNT.json` | Completed tickers for each account |
| `state/limit_ACCOUNT.json` | Account cooldown after a Bloomberg limit |
| `reference/`, `prices_daily/`, `fundamentals_annual/`, etc. | Downloaded Parquet parts |
| `summary.xlsx` | Convenience summary rebuilt after a run |

Collector logs stay in the repository's ignored `logs/` folder on each PC. The data folder is ignored by Git; OneDrive is the shared copy when configured as above.
