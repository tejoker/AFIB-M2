"""Initialize or check the team's synced research data folder without using an API.

One teammate runs --init once. Everyone then runs this check against their own
synced copy and compares the storage ID before collecting.
"""

import argparse
import json
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root
from data_root import CONFIGURED

MARKER = "team_storage.json"


def data_folder(path_argument):
    configured = path_argument or CONFIGURED
    if not configured:
        raise ValueError("Set AFIB_DATA_DIR (or BBG_DATA_DIR) or pass --path to the synced team folder")
    folder = Path(configured).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    return folder


def account_list(accounts):
    if not isinstance(accounts, list) or not accounts or not all(isinstance(a, str) and a.strip() for a in accounts):
        raise ValueError("The account list must hold at least one name")
    # bbg_collect.py names each account's files after it, with characters other than these replaced by _
    # (Windows user names may contain spaces or accents); the resulting file names must still differ
    file_names = [re.sub(r"[^A-Za-z0-9_.-]", "_", account) for account in accounts]
    if len(set(file_names)) != len(file_names):
        raise ValueError("Account names must be unique, even with characters other than letters, numbers, "
                         "_, . and - replaced by _")
    return accounts


def configure_accounts(folder, accounts):
    accounts = account_list(accounts)
    account_file = folder / "accounts.json"
    if account_file.exists():
        existing = json.loads(account_file.read_text(encoding="utf-8"))["accounts"]
        if existing != accounts:
            raise ValueError(f"Existing {account_file} has a different account list; resolve it before changing accounts")
    else:
        with account_file.open("x", encoding="utf-8") as stream:
            json.dump({"accounts": accounts}, stream, indent=2)
            stream.write("\n")


def initialize(folder, accounts):
    marker = folder / MARKER
    if marker.exists():
        raise ValueError(f"Already initialized: {marker}")
    if accounts:
        configure_accounts(folder, accounts)
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"storage_id": str(uuid.uuid4()),
                   "created_at_utc": datetime.now(timezone.utc).isoformat()}, stream, indent=2)
        stream.write("\n")


def check(folder, account=None):
    marker = folder / MARKER
    account_file = folder / "accounts.json"
    if not marker.is_file():
        raise ValueError(f"Missing {MARKER} in {folder}; wait for sync or initialize once")
    storage_id = json.loads(marker.read_text(encoding="utf-8"))["storage_id"]
    uuid.UUID(storage_id)
    accounts = account_list(json.loads(account_file.read_text(encoding="utf-8"))["accounts"]) if account_file.is_file() else []
    if account and not accounts:
        raise ValueError(f"No accounts.json in {folder}; configure Bloomberg collector accounts first")
    if account and account not in accounts:
        raise ValueError(f"Account {account!r} is not listed in {account_file}")
    with tempfile.TemporaryFile(dir=folder) as stream:
        stream.write(b"AFIB storage check")
        stream.seek(0)
        if stream.read() != b"AFIB storage check":
            raise ValueError(f"Could not read the test file in {folder}")
    print(f"Data folder: {folder}")
    print(f"Storage ID: {storage_id}")
    print(f"Bloomberg collector accounts: {', '.join(accounts) if accounts else 'not configured'}")
    print("Local read/write: OK")
    print("Compare the storage ID on every machine and wait until OneDrive says Up to date before handoff.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="synced folder; defaults to AFIB_DATA_DIR or BBG_DATA_DIR")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--init", action="store_true", help="create the team marker once")
    actions.add_argument("--set-accounts", action="store_true", help="add the Bloomberg collector account list once")
    parser.add_argument("--accounts", nargs="+", help="ordered Bloomberg collector account names for --init or --set-accounts")
    parser.add_argument("--account", help="this user's Bloomberg collector account name for the membership check")
    args = parser.parse_args()
    try:
        folder = data_folder(args.path)
        if args.init:
            if args.account and not args.accounts:
                raise ValueError("--account requires --accounts during initialization")
            if args.account and args.account not in args.accounts:
                raise ValueError(f"Account {args.account!r} is not in the proposed account list")
            initialize(folder, args.accounts)
        elif args.set_accounts:
            if not args.accounts:
                parser.error("--set-accounts requires --accounts")
            if not (folder / MARKER).is_file():
                raise ValueError(f"Initialize {MARKER} before setting accounts")
            configure_accounts(folder, args.accounts)
        elif args.accounts:
            parser.error("--accounts is only used with --init or --set-accounts")
        check(folder, args.account)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Shared data check failed: {exc}\n")


if __name__ == "__main__":
    main()
