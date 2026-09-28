"""Generic Lambda entrypoint for the Intellis AI scheduler variant.

Deploy the same image to one Lambda per site and set SITE_ID plus S3_BUCKET.
Outputs default to generated/vedanjay_ai_intellis/<SITE>/outputs/<DATE>/ so
this variant stays separate from the official Enercast scheduler outputs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from botocore.exceptions import ClientError

from core import scheduler_service, settings, storage
from modules import schedule_utils


def _pick(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _event_value(event: dict[str, Any], *names: str) -> str:
    for name in names:
        value = event.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _object_exists(bucket: str, key: str) -> bool:
    try:
        storage.s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _upload_lock(bucket: str, key: str, payload: dict[str, Any]) -> None:
    storage.upload_json(bucket, key, payload)


def _read_lock(bucket: str, key: str) -> dict[str, Any] | None:
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

    os.environ["PLANT_NAME"] = site_id

    import config
    config.load_plant_profile(site_id)

    if site_id in ("JEWLI", "JGBPL"):
        os.environ.setdefault("USE_LLM_FOR_WIND", "false")
        os.environ.setdefault("USE_LLM_JEWLI", "false")

    bucket = _event_value(event, "bucket", "s3_bucket") or _pick("S3_BUCKET") or _pick("BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET environment variable is required.")

    raw_owner = _pick("S3_RAW_OWNER", getattr(settings, "DEFAULT_S3_RAW_OWNER", "vedanjay"))
    default_raw, _, default_sched = settings.resolve_s3_prefixes(site_id, raw_owner=raw_owner)

    capture_prefix = _event_value(event, "capture_prefix") or _pick("S3_CAPTURE_PREFIX", default_raw)
    meter_prefix = _event_value(event, "meter_prefix") or _pick("S3_METER_PREFIX", default_raw)
    schedule_prefix = _event_value(event, "schedule_prefix") or _pick("S3_SCHEDULE_PREFIX", default_sched)

    target_date, target_time, _ = schedule_utils.parse_target_datetime(event)
    run_time_slug = target_time.replace(":", "-")
    target_hour, target_minute = [int(part) for part in target_time.split(":")[:2]]
    snapshot_block = ((target_hour * 60 + target_minute) // 15) + 1
    snapshot_stamp = f"{target_date.replace('-', '')}t{target_hour:02d}{target_minute:02d}00"
    day_prefix = f"{schedule_prefix.rstrip('/')}/{target_date}"
    metadata_key = f"{day_prefix}/schedule_from_{snapshot_block}_{snapshot_stamp}.csv.meta.json"
    lock_key = f"{day_prefix}/.idempotency/{site_id}__{target_date}__{run_time_slug}.json"

    force = str(event.get("force", "")).lower() in {"1", "true", "yes"} or str(event.get("recompute", "")).lower() in {"1", "true", "yes"}
    idempotency_val = _event_value(event, "idempotency_enabled", "INTELLIS_IDEMPOTENCY_ENABLED") or _pick("INTELLIS_IDEMPOTENCY_ENABLED", "true")
    idempotency_enabled = not force and (idempotency_val.lower() in {"1", "true", "yes", "on"})
    if idempotency_enabled:
        if _object_exists(bucket, metadata_key):
            return {
                "status": "skipped",
                "reason": "already_completed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "metadata_key": metadata_key,
            }
        existing_lock = _read_lock(bucket, lock_key)
        if _lock_is_active(existing_lock):
            return {
                "status": "skipped",
                "reason": "already_started" if existing_lock.get("status") == "in_progress" else "already_completed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "lock_key": lock_key,
            }
        _upload_lock(bucket, lock_key, {
            "status": "in_progress",
            "site_id": site_id,
            "target_date": target_date,
            "target_time": target_time,
            "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    try:
        result = scheduler_service.run_schedule_job(
            bucket=bucket,
            capture_prefix=capture_prefix,
            meter_prefix=meter_prefix,
            schedule_prefix=schedule_prefix,
            event=event,
        )
        if idempotency_enabled:
            _upload_lock(bucket, lock_key, {
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
            _upload_lock(bucket, lock_key, {
                "status": "failed",
                "site_id": site_id,
                "target_date": target_date,
                "target_time": target_time,
                "error": str(exc),
                "failed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
        raise
