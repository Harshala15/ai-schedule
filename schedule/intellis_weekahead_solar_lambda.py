"""
Master AWS Lambda Handler for Statutory Week-Ahead Solar Forecasting.
Function: 'intellis-weekhead-forcast-solar'

Trigger:
  Morning: 04:30 AM IST -> cron(0 23 * * ? *)

Active Solar Plants for Week-Ahead:
  - BHUPALPALLY
  - CME
  - ENRICH
  - KASIPET
  - KOTHAGUDEM
  - OSEPL
  - SHAHA (SIDDEHESH, PRANAV, LOKGREENB2)
  - ZTRIC

Destination:
  s3://vedanjay-schedules-test-608744602858/intellis Weekhead solar/{SITE}/{START_DATE}/{PLANT}_{START_DATE}_weekahead_WA.csv
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

# Regulatory Solar Sites active for Week-Ahead:
WA_SOLAR_SITES: List[str] = [
    "BHUPALPALLY",
    "CME",
    "ENRICH",
    "KASIPET",
    "KOTHAGUDEM",
    "OSEPL",
    "SHAHA",
    "ZTRIC",
]

SHAHA_SUB_PLANTS = ["SIDDEHESH", "PRANAV", "LOKGREENB2"]


def _resolve_start_date(event: Dict[str, Any] | None) -> tuple[str, str]:
    """Resolves today's date and next-day start date in Asia/Kolkata."""
    tz_ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    today_str = now_ist.strftime("%Y-%m-%d")

    target_override = (event or {}).get("start_date") or (event or {}).get("date") or (event or {}).get("target_date")
    if target_override:
        return today_str, str(target_override).strip()

    start_date = (now_ist.date() + timedelta(days=1)).strftime("%Y-%m-%d")
    return today_str, start_date


def lambda_handler(event: Dict[str, Any] | None, context: Any = None) -> Dict[str, Any]:
    """Master Week-Ahead Solar Lambda execution entrypoint."""
    from modules.week_ahead_forecast_solar.wa_solar_engine import generate_solar_week_ahead_schedule

    event = event or {}
    today_str, start_date_str = _resolve_start_date(event)
    bucket = event.get("bucket", S3_BUCKET)

    print("=" * 70)
    print("INTELLIS SOLAR WEEK-AHEAD FORECAST LAMBDA TRIGGERED")
    print(f"Today (IST): {today_str} | Start Date (Tomorrow): {start_date_str} (7-Day Horizon / 672 Blocks)")
    print(f"Target S3 Destination: s3://{bucket}/intellis Weekhead solar/")
    print("=" * 70)

    # Allow single or custom plant override for manual testing
    manual_plants = event.get("plant") or event.get("plants")
    if manual_plants:
        if isinstance(manual_plants, str):
            candidate_sites = [manual_plants.strip().upper()]
        else:
            candidate_sites = [str(p).strip().upper() for p in manual_plants]
    else:
        candidate_sites = list(WA_SOLAR_SITES)

    results: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for site in candidate_sites:
        print(f"\n>>> Processing Week-Ahead Solar Site: {site} starting {start_date_str}...")

        if site in ("ZTRIC", "ENRICH", "SHAHA"):
            try:
                from modules.multi_generator.multi_generator_engine import MultiGeneratorEngine
                mg_engine = MultiGeneratorEngine(s3_bucket=bucket)
                mg_res = mg_engine.generate_and_dispatch_multi_generator_schedules(
                    plant_name=site,
                    target_date_str=start_date_str,
                    today_str=today_str,
                )
                success_count += 1
                results.append({
                    "plant_name": site,
                    "site_group": site,
                    "status": "SUCCESS",
                    "s3_uri": mg_res.get("wa_s3_uri"),
                    "columns": mg_res.get("wa_columns"),
                    "total_blocks": mg_res.get("total_wa_blocks"),
                    "upload_success": mg_res.get("upload_success", True),
                })
            except Exception as exc:
                print(f"  [ERROR] Failed multi-generator WA forecast for {site}: {exc}")
                fail_count += 1
                results.append({
                    "plant_name": site,
                    "site_group": site,
                    "status": "FAILED",
                    "error": str(exc),
                    "upload_success": False,
                })
            continue

        plants_to_run = [site]

        for p_name in plants_to_run:
            try:
                res = generate_solar_week_ahead_schedule(
                    plant_name=p_name,
                    start_date_str=start_date_str,
                    today_str=today_str,
                    s3_bucket=bucket,
                )
                if res.get("upload_success"):
                    print(f"  [SUCCESS] {p_name} Week-Ahead uploaded -> {res['s3_uri']} ({res.get('total_blocks')} blocks)")
                    success_count += 1
                else:
                    print(f"  [WARN] {p_name} Week-Ahead generated locally but S3 upload failed")
                    fail_count += 1
                results.append(res)
            except Exception as exc:
                import traceback
                print(f"  [FAIL] Error generating Week-Ahead forecast for plant {p_name}: {exc}")
                traceback.print_exc()
                fail_count += 1
                results.append({
                    "plant_name": p_name,
                    "error": str(exc),
                    "upload_success": False,
                })

    summary = {
        "status": "COMPLETED" if fail_count == 0 else "PARTIAL_SUCCESS",
        "today_ist": today_str,
        "start_date_ist": start_date_str,
        "total_attempted": len(candidate_sites),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
    }

    print("\n" + "=" * 70)
    print(f"WEEK-AHEAD SOLAR RUN COMPLETED: {success_count} Succeeded | {fail_count} Failed")
    print("=" * 70)

    return summary


if __name__ == "__main__":
    test_plant = sys.argv[1] if len(sys.argv) > 1 else None
    test_event = {}
    if test_plant:
        test_event["plant"] = test_plant
    lambda_handler(test_event)
