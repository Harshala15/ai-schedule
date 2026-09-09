"""Generic Lambda entrypoint for the Intellis AI scheduler variant.

Deploy the same image to one Lambda per site and set SITE_ID plus S3_BUCKET.
Outputs default to generated/vedanjay_ai_intellis/<SITE>/outputs/<DATE>/ so
this variant stays separate from the official Enercast scheduler outputs.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
from typing import Any

from botocore.exceptions import ClientError

from modules import schedule_utils

SITE_PACKAGES = {
    "SIRMOUR": "simour_forecast_scheduler",
    "KASIPET": "kasipet_forecast_scheduler",
    "BHUPALPALLY": "bhupalpally_forecast_scheduler",
    "KOTHAGUDEM": "kothagudem_forecast_scheduler",
    "OSEPL": "osepl_forecast_scheduler",
    "ANJANGOAN": "anjangoan_forecast_scheduler",
    "BAMKHAL": "bhupalpally_forecast_scheduler",
    "BALAKWADA": "bhupalpally_forecast_scheduler",
    "CME": "bhupalpally_forecast_scheduler",
    "ANDAD": "bhupalpally_forecast_scheduler",
    "SAWDA": "bhupalpally_forecast_scheduler",
    "GUGARIYAKHEDI": "bhupalpally_forecast_scheduler",
    "NANDGAON": "bhupalpally_forecast_scheduler",
    "GSNP": "bhupalpally_forecast_scheduler",
    "ZTRIC": "ztric_forecast_scheduler",
}

SERVICE_MODULES = {
    "SIRMOUR": "simour_forecast_scheduler.scheduler_service",
    "KASIPET": "simour_forecast_scheduler.service",
    "BHUPALPALLY": "bhupalpally_forecast_scheduler.scheduler_service",
    "KOTHAGUDEM": "kothagudem_forecast_scheduler.scheduler_service",
    "OSEPL": "osepl_forecast_scheduler.scheduler_service",
    "ANJANGOAN": "anjangoan_forecast_scheduler.scheduler_service",
    "BAMKHAL": "bhupalpally_forecast_scheduler.scheduler_service",
    "BALAKWADA": "bhupalpally_forecast_scheduler.scheduler_service",
    "CME": "bhupalpally_forecast_scheduler.scheduler_service",
    "ANDAD": "bhupalpally_forecast_scheduler.scheduler_service",
    "SAWDA": "bhupalpally_forecast_scheduler.scheduler_service",
    "GUGARIYAKHEDI": "bhupalpally_forecast_scheduler.scheduler_service",
    "NANDGAON": "bhupalpally_forecast_scheduler.scheduler_service",
    "GSNP": "bhupalpally_forecast_scheduler.scheduler_service",
    "ZTRIC": "ztric_forecast_scheduler.scheduler_service",
}

def _pick(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _event_value(event: dict[str, Any], *names: str) -> str:
    for name in names:
        value = event.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _object_exists(storage, bucket: str, key: str) -> bool:
    try:
        storage.s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _upload_lock(storage, bucket: str, key: str, payload: dict[str, Any]) -> None:
    storage.upload_json(bucket, key, payload)


def _read_lock(storage, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        response = storage.s3_client().get_object(Bucket=bucket, Key=key)
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    except Exception:
        return None


def _lock_is_active(lock: dict[str, Any] | None) -> bool:
    if not lock:
        return False
    status = str(lock.get("status", "")).lower()
    return status in {"in_progress", "completed"}

def lambda_handler(event, context):
    event = event or {}
    site_id = (_event_value(event, "site_id", "SITE_ID") or _pick("SITE_ID") or _pick("PLANT_NAME") or "SIRMOUR").upper()
    package_name = SITE_PACKAGES.get(site_id)
    if not package_name:
        raise RuntimeError(f"Unsupported Intellis AI scheduler site: {site_id}")

    os.environ["PLANT_NAME"] = site_id

    import config
    config.load_plant_profile(site_id)

    package = importlib.import_module(package_name)
    settings = importlib.import_module(f"{package_name}.settings")
    storage = importlib.import_module(f"{package_name}.storage")
    service = importlib.import_module(SERVICE_MODULES[site_id])

    bucket = _event_value(event, "bucket", "s3_bucket") or _pick("S3_BUCKET") or _pick("BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET environment variable is required.")

    raw_owner = _pick("S3_RAW_OWNER", getattr(settings, "DEFAULT_S3_RAW_OWNER", "vedanjay"))
    if site_id == "ZTRIC":
        default_raw_prefix = f"raw/{raw_owner}/multiple_generator/ZTRIC"
    else:
        default_raw_prefix = f"raw/{raw_owner}/{site_id}"
    capture_prefix = _event_value(event, "capture_prefix") or _pick("S3_CAPTURE_PREFIX", default_raw_prefix)
    meter_prefix = _event_value(event, "meter_prefix") or _pick("S3_METER_PREFIX", default_raw_prefix)

    output_root = _event_value(event, "output_prefix") or _pick("S3_OUTPUT_PREFIX", "generated/vedanjay_ai_intellis")
    if site_id == "ZTRIC":
        default_schedule_prefix = f"{output_root.rstrip('/')}/multiple_generator/ZTRIC/outputs"
    else:
        default_schedule_prefix = f"{output_root.rstrip('/')}/{site_id}/outputs"
    schedule_prefix = _event_value(event, "schedule_prefix") or _pick("S3_SCHEDULE_PREFIX", default_schedule_prefix)

    target_date, target_time, _ = schedule_utils.parse_target_datetime(event)
    run_time_slug = target_time.replace(":", "-")
    target_hour, target_minute = [int(part) for part in target_time.split(":")[:2]]
    snapshot_block = ((target_hour * 60 + target_minute) // 15) + 1
    snapshot_stamp = f"{target_date.replace('-', '')}t{target_hour:02d}{target_minute:02d}00"
    day_prefix = f"{schedule_prefix.rstrip('/')}/{target_date}"
    metadata_key = f"{day_prefix}/schedule_from_{snapshot_block}_{snapshot_stamp}.csv.meta.json"
    lock_key = f"{day_prefix}/.idempotency/{site_id}__{target_date}__{run_time_slug}.json"

    idempotency_enabled = _pick("INTELLIS_IDEMPOTENCY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    if idempotency_enabled:
        if _object_exists(storage, bucket, metadata_key):
            return {
                "status": "skipped",
                "reason": "already_completed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "metadata_key": metadata_key,
            }
        existing_lock = _read_lock(storage, bucket, lock_key)
        if _lock_is_active(existing_lock):
            return {
                "status": "skipped",
                "reason": "already_started" if existing_lock.get("status") == "in_progress" else "already_completed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "lock_key": lock_key,
            }
        _upload_lock(storage, bucket, lock_key, {
            "status": "in_progress",
            "site_id": site_id,
            "target_date": target_date,
            "target_time": target_time,
            "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    try:
        result = service.run_schedule_job(
            bucket=bucket,
            capture_prefix=capture_prefix,
            meter_prefix=meter_prefix,
            schedule_prefix=schedule_prefix,
            event=event,
        )
        if idempotency_enabled:
            _upload_lock(storage, bucket, lock_key, {
                "status": "completed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "metadata_key": result.get("snapshot_metadata_key", metadata_key),
                "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
        return result
    except Exception as exc:
        if idempotency_enabled:
            _upload_lock(storage, bucket, lock_key, {
                "status": "failed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "error": str(exc),
                "failed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
        raise
