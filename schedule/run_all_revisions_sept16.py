"""Run all 17 revisions for Chandawasa on 2026-09-16 (06:00 to 14:00 every 30 mins) with force=True."""

import os
import sys
import datetime as dt
from pathlib import Path

os.environ["AWS_PROFILE"] = "intellis-608"
os.environ["AWS_DEFAULT_REGION"] = "ap-south-1"

sys.path.insert(0, r"d:\14 sept intellis\schedule")
from intellis_ai_lambda import lambda_handler

target_date = "2026-09-16"
bucket = "vedanjay-schedules-test-608744602858"

# 17 revisions from 06:00 to 14:00 every 30 min
revision_times = [
    f"{h:02d}:{m:02d}"
    for h in range(6, 15)
    for m in (0, 30)
    if not (h == 14 and m == 30)
]

print(f"Total revisions to run: {len(revision_times)} -> {revision_times}")

results = []
for t in revision_times:
    print(f"\n---> Running revision for {target_date} at {t} IST (force=True) ...")
    event = {
        "site_id": "CHANDAWASA",
        "target_date": target_date,
        "target_time": t,
        "bucket": bucket,
        "force": True,
    }
    res = lambda_handler(event, None)
    results.append({"time": t, "status": res.get("status"), "snapshot_key": res.get("snapshot_csv_key")})
    print(f"     Status: {res.get('status')} | Key: {res.get('snapshot_csv_key')}")

print("\n=== Summary of All Revisions Executed ===")
for r in results:
    print(f"Revision {r['time']}: {r['status']} -> {r['snapshot_key']}")
