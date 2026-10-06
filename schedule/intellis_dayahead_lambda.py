"""
intellis_dayahead_lambda.py

Master AWS Lambda Handler for 'intellis-dayhead-forcast-solar'.
Generates statutory 96-block Day-Ahead forecast for all solar & wind plants
according to the regulatory schedule matrix, and uploads directly to AWS S3:
  s3://vedanjay-schedules-test-608744602858/intellis Dayhead solar/{SITE_NAME}/{TARGET_DATE}/{PLANT_NAME}_{TARGET_DATE}_{run_tag}.csv

Run Types:
  - 'da0': Morning run (Triggered at 04:30 AM IST)
  - 'da1': Night run (Triggered at 10:30 PM IST)
"""

import os
import json
import logging
from datetime import datetime, timedelta
import pytz
from typing import Dict, Any, List

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Default S3 Bucket
S3_BUCKET = os.getenv("SCHEDULE_BUCKET", "vedanjay-schedules-test-608744602858")

# Regulatory Solar Plant Matrix: (Run Morning da0?, Run Afternoon da1?, Run Night da2?)
SITE_DISPATCH_RULES: Dict[str, tuple[bool, bool, bool]] = {
    "ANDAD": (True, False, False),
    "ANJANGOAN": (True, False, False),
    "BALAKWADA": (True, False, False),
    "BAMKHAL": (True, False, False),
    "BHUPALPALLY": (True, False, True),
    "CME": (True, False, False),
    "ENRICH": (True, False, True),
    "GSNP": (True, False, False),
    "GUGARIYAKHEDI": (True, False, False),
    "KASIPET": (True, False, True),
    "KOTHAGUDEM": (True, False, True),
    "LGEPL": (True, True, True),
    "NANDGAON": (True, False, False),
    "OSEPL": (True, False, False),
    "REWASEIT": (False, False, False),
    "REWASPRNG": (False, False, False),
    "SAWDA": (True, False, False),
    "SHAHA": (True, False, True),
    "SIRMOUR": (True, False, False),
    "ZTRIC": (True, False, True),
}

SHAHA_SUB_PLANTS = ["SIDDEHESH", "PRANAV", "LOKGREENB2"]


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
    """Detects whether this execution is morning ('da0'), afternoon ('da_afternoon'), or night ('da_night')."""
    if event and "run_type" in event:
        val = str(event["run_type"]).strip().lower()
        if val in ("da0", "morning"):
            return "da0"
        if val in ("afternoon", "da_afternoon", "da1_afternoon"):
            return "da_afternoon"
        if val in ("da1", "da2", "night", "da_night"):
            return "da_night"

    # Infer from current IST hour if not explicitly supplied
    tz_ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(tz_ist)
    if now_ist.hour < 11:
        return "da0"
    elif now_ist.hour < 17:
        return "da_afternoon"
    else:
        return "da_night"


def lambda_handler(event: Dict[str, Any] | None, context: Any = None) -> Dict[str, Any]:
    """Master Solar Day-Ahead Lambda execution entrypoint."""
    from modules.day_ahead_forecast_solar.solar_da_engine import generate_solar_day_ahead_schedule

    event = event or {}
    today_str, target_date_str = _resolve_target_date(event)
    run_tag = _resolve_run_type(event)
    bucket = event.get("bucket", S3_BUCKET)

    print("=" * 70)
    print(f"INTELLIS SOLAR DAY-AHEAD FORECAST LAMBDA TRIGGERED")
    print(f"Today (IST): {today_str} | Target Date (Tomorrow): {target_date_str} | Run Tag: {run_tag.upper()}")
    print(f"Target S3 Bucket: {bucket}")
    print("=" * 70)

    # Allow single or custom plant override for manual testing
    manual_plants = event.get("plant") or event.get("plants")
    if manual_plants:
        if isinstance(manual_plants, str):
            candidate_sites = [manual_plants.strip().upper()]
        else:
            candidate_sites = [str(p).strip().upper() for p in manual_plants]
    else:
        # Filter candidate sites matching regulatory schedule table
        candidate_sites = []
        is_morning = (run_tag == "da0")
        is_afternoon = (run_tag == "da_afternoon")
        is_night = (run_tag in ("da1", "da2", "da_night", "night"))

        for site, rules in SITE_DISPATCH_RULES.items():
            run_morn = rules[0]
            run_afternoon = rules[1] if len(rules) > 2 else False
            run_night = rules[2] if len(rules) > 2 else rules[1]

            if is_morning and run_morn:
                candidate_sites.append(site)
            elif is_afternoon and run_afternoon:
                candidate_sites.append(site)
            elif is_night and run_night:
                candidate_sites.append(site)

    results: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for site in candidate_sites:
        print(f"\n>>> Processing Solar Site: {site} ({run_tag.upper()}) for {target_date_str}...")

        if site in ("ZTRIC", "ENRICH", "SHAHA"):
            try:
                from modules.multi_generator.multi_generator_engine import MultiGeneratorEngine
                mg_engine = MultiGeneratorEngine(s3_bucket=bucket)
                mg_res = mg_engine.generate_and_dispatch_multi_generator_schedules(
                    plant_name=site,
                    target_date_str=target_date_str,
                    today_str=today_str,
                    run_tag=run_tag,
                )
                success_count += 1
                results.append({
                    "plant_name": site,
                    "site_group": site,
                    "status": "SUCCESS",
                    "s3_uri": mg_res.get("da_s3_uri"),
                    "columns": mg_res.get("da_columns"),
                })
            except Exception as exc:
                print(f"  [ERROR] Failed multi-generator DA forecast for {site}: {exc}")
                fail_count += 1
                results.append({
                    "plant_name": site,
                    "site_group": site,
                    "status": "FAILED",
                    "error": str(exc),
                })
            continue

        # Standard standalone plants
        plants_to_run = [site]

        for p_name in plants_to_run:
            if p_name == "LGEPL":
                if run_tag == "da0":
                    plant_run_tag = "da0"
                elif run_tag == "da_afternoon":
                    plant_run_tag = "da1"
                else:
                    plant_run_tag = "da2"
            else:
                plant_run_tag = "da0" if run_tag == "da0" else "da1"

            block_override = (event or {}).get("block") or (event or {}).get("block_no")
            try:
                res = generate_solar_day_ahead_schedule(
                    plant_name=p_name,
                    target_date_str=target_date_str,
                    run_tag=plant_run_tag,
                    s3_bucket=bucket,
                    block_no=block_override,
                )

                if res.get("upload_success"):
                    print(f"  [SUCCESS] {p_name} Day-Ahead uploaded -> {res['s3_uri']}")
                    success_count += 1
                else:
                    print(f"  [WARN] {p_name} generated locally but S3 upload failed -> {res.get('s3_uri')}")
                    success_count += 1

                results.append({
                    "plant_name": p_name,
                    "site_group": site,
                    "status": "SUCCESS",
                    "s3_uri": res.get("s3_uri"),
                    "regime": res.get("synoptic_regime", "SOLAR_PHYSICS"),
                })
            except Exception as exc:
                print(f"  [ERROR] Failed to generate Day-Ahead forecast for {p_name}: {exc}")
                fail_count += 1
                results.append({
                    "plant_name": p_name,
                    "site_group": site,
                    "status": "FAILED",
                    "error": str(exc),
                })

    summary = {
        "status": "OK" if fail_count == 0 else "PARTIAL_FAILURE",
        "today_ist": today_str,
        "target_date_ist": target_date_str,
        "run_tag": run_tag,
        "total_attempted": len(results),
        "successful": success_count,
        "failed": fail_count,
        "results": results,
    }

    print("\n" + "=" * 70)
    print(f"DAY-AHEAD RUN COMPLETED: {success_count}/{len(results)} Succeeded | {fail_count} Failed")
    print("=" * 70)
    return summary


if __name__ == "__main__":
    import sys
    test_run_tag = sys.argv[1] if len(sys.argv) > 1 else "da0"
    test_plant = sys.argv[2] if len(sys.argv) > 2 else "ANJANGOAN"
    test_event = {"run_type": test_run_tag, "plant": test_plant}
    res = lambda_handler(test_event)
    print("\nLocal Execution Test Output:")
    print(json.dumps(res, indent=2))
