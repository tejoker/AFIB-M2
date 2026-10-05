"""Finish the active research collection's exports with the latest feature code.

This is a finite helper for the currently running collector, not a scheduled task.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd

from collect_russell_research import atomic_json


def audit(root):
    members = pd.read_parquet(root / "index_membership.parquet")
    company = pd.read_parquet(root / "company_reference_snapshot.parquet")
    panel = pd.read_parquet(root / "monthly_liquidity_panel.parquet")
    cross = pd.read_parquet(root / "regression_cross_section.parquet")
    assert company["company_id"].is_unique, "Duplicate company snapshot identifiers"
    assert not panel.duplicated(["security", "month"]).any(), "Duplicate security/month outcomes"
    observed = panel["membership_observation_date"].notna()
    assert (panel.loc[observed, "membership_observation_date"] < panel.loc[observed, "month"].dt.to_period("M").dt.to_timestamp()).all(), "Membership lookahead"
    eligible = panel["regression_liquidity_eligible"]
    assert (panel.loc[eligible, "trading_days"] >= 15).all()
    assert panel.loc[eligible, "calendar_month_complete"].all()
    positive = panel["adtv_usd"] > 0
    assert np.allclose(panel.loc[positive, "log_adtv_usd"], np.log(panel.loc[positive, "adtv_usd"]), equal_nan=True)
    assert cross["company_id"].is_unique
    result = {"status": "passed", "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "company_snapshot_rows": len(company), "monthly_panel_rows": len(panel),
              "cross_section_rows": len(cross), "eligible_monthly_rows": int(eligible.sum()),
              "membership_rows": len(members),
              "checks": ["company deduplication", "security-month uniqueness", "membership timing",
                         "completed-month eligibility", "minimum trading days", "log transformation"]}
    atomic_json(root / "data_audit.json", result)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--timeout-hours", type=float, default=3)
    args = ap.parse_args()
    deadline = time.monotonic() + args.timeout_hours * 3600
    root = args.output.resolve()
    while time.monotonic() < deadline:
        path = root / "run_status.json"
        status = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if status.get("status") == "complete":
            command = [sys.executable, str(Path(__file__).with_name("collect_russell_research.py")), "--output", str(root), "--build-only"]
            subprocess.run(command, check=True, cwd=Path(__file__).parent)
            result = audit(root)
            status["latest_exports_verified"] = True
            status["audit"] = result
            atomic_json(path, status)
            print(json.dumps(result, indent=2), flush=True)
            return
        if status.get("status") == "incomplete_resumable":
            print("Collector stopped with retained cache: " + str(status.get("reason")), flush=True)
            return
        time.sleep(15)
    atomic_json(root / "postprocess_status.json", {"status": "not_run", "reason": "Waiting deadline reached; collection status remains in run_status.json"})


if __name__ == "__main__":
    main()
