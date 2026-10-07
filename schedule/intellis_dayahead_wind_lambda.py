"""
Master AWS Lambda Handler for Statutory Day-Ahead Wind Forecasting.
Function: 'intellis-dayhead-forcast-wind'

Triggers:
  1. Morning: 04:30 AM IST -> {"run_type": "da0"} (CHANDAWASA, JEWLI, JGBPL)
  2. Night:   10:30 PM IST -> {"run_type": "da1"} (JEWLI, JGBPL)

Destination:
  s3://vedanjay-schedules-test-608744602858/intellis Dayhead wind/{SITE}/{TARGET_DATE}/{PLANT}_{TARGET_DATE}_{run_tag}.csv
"""

from __future__ import annotations
import os
import sys
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List
import pytz

# Add schedule root to sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

S3_BUCKET = os.getenv("SCHEDULE_BUCKET", "vedanjay-schedules-test-608744602858")

# Regulatory Wind Plant Matrix: (Run Morning da0?, Run Night da1?)
WIND_DISPATCH_RULES: Dict[str, tuple[bool, bool]] = {
    "CHANDAWASA": (True, False),  # Morning only
    "JEWLI": (True, True),        # Morning & Night
    "JGBPL": (True, True),        # Morning & Night
}


def _resolve_target_date(event: Dict[str, Any] | None) -> tuple[str, str]:
    """Resolves today's date and next-day target date in Asia/Kolkata."""
    tz_ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    today_str = now_ist.strftime("%Y-%m-%d")

    target_override = (event or {}).get("target_date") or (event or {}).get("date")
    if target_override:
        return today_str, str(target_override).strip()

    target_date = (now_ist.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    return today_str, target_date


def _resolve_run_type(event: Dict[str, Any] | None) -> str:
    """Detects whether this execution is morning ('da0') or night ('da1')."""
    if event and "run_type" in event:
        val = str(event["run_type"]).strip().lower()
        if val in ("da0", "morning"):
            return "da0"
        if val in ("da1", "night"):
            return "da1"

    # Infer from current IST hour if not explicitly supplied
    tz_ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    return "da0" if now_ist.hour < 12 else "da1"


def lambda_handler(event: Dict[str, Any] | None, context: Any = None) -> Dict[str, Any]:
    """Master Wind Day-Ahead Lambda execution entrypoint."""
    from modules.day_ahead_forecast_wind.wind_da_engine import generate_wind_day_ahead_schedule

    event = event or {}
    today_str, target_date_str = _resolve_target_date(event)
    run_tag = _resolve_run_type(event)
    bucket = event.get("bucket", S3_BUCKET)

    print("=" * 70)
    print("INTELLIS WIND DAY-AHEAD FORECAST LAMBDA TRIGGERED")
    print(f"Today (IST): {today_str} | Target Date (Tomorrow): {target_date_str} | Run Tag: {run_tag.upper()}")
    print(f"Target S3 Destination: s3://{bucket}/intellis Dayhead wind/")
    print("=" * 70)

    # Allow single or custom plant override for manual testing
    manual_plants = event.get("plant") or event.get("plants")
    if manual_plants:
        if isinstance(manual_plants, str):
            candidate_sites = [manual_plants.strip().upper()]
        else:
            candidate_sites = [str(p).strip().upper() for p in manual_plants]
    else:
        candidate_sites = []
        is_morning = (run_tag == "da0")
        for site, (run_morn, run_night) in WIND_DISPATCH_RULES.items():
            if is_morning and run_morn:
                candidate_sites.append(site)
            elif not is_morning and run_night:
                candidate_sites.append(site)

    results: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for site in candidate_sites:
        print(f"\n>>> Processing Wind Site: {site} ({run_tag.upper()}) for {target_date_str}...")
        try:
            res = generate_wind_day_ahead_schedule(
                plant_name=site,
                target_date_str=target_date_str,
                run_tag=run_tag,
                s3_bucket=bucket,
            )
            if res.get("upload_success"):
                print(f"  [SUCCESS] {site} Wind Day-Ahead uploaded -> {res['s3_uri']}")
                success_count += 1
            else:
                print(f"  [WARN] {site} Wind Day-Ahead generated locally but S3 upload failed")
                fail_count += 1
            results.append(res)
        except Exception as exc:
            import traceback
            print(f"  [FAIL] Error generating Day-Ahead forecast for wind plant {site}: {exc}")
            traceback.print_exc()
            fail_count += 1
            results.append({
                "plant_name": site,
                "error": str(exc),
                "upload_success": False,
            })

    summary = {
        "status": "COMPLETED" if fail_count == 0 else "PARTIAL_SUCCESS",
        "run_type": run_tag,
        "today_ist": today_str,
        "target_date_ist": target_date_str,
        "total_attempted": len(candidate_sites),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
    }

    print("\n" + "=" * 70)
    print(f"WIND DAY-AHEAD RUN COMPLETED: {success_count}/{len(candidate_sites)} Succeeded | {fail_count} Failed")
    print("=" * 70)

    return summary


if __name__ == "__main__":
    test_run_tag = sys.argv[1] if len(sys.argv) > 1 else "da0"
    test_plant = sys.argv[2] if len(sys.argv) > 2 else None
    test_event = {"run_type": test_run_tag}
    if test_plant:
        test_event["plant"] = test_plant
    lambda_handler(test_event)
