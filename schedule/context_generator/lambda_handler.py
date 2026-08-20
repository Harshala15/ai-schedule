from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

import daily_feedback
import state_sync


IST = ZoneInfo("Asia/Kolkata")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _today_str() -> str:
    return _dt.datetime.now(IST).strftime("%Y-%m-%d")


def _s3_client():
    return boto3.client("s3", region_name=_env("AWS_REGION", "ap-south-1") or "ap-south-1")


def _download_s3_object(bucket: str, key: str, target_path: Path) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _s3_client().download_file(bucket, key, str(target_path))
    return target_path


def _pick_latest_key(bucket: str, prefix: str, suffixes: tuple[str, ...]) -> str | None:
    prefix = prefix.rstrip("/") + "/"
    resp = _s3_client().list_objects_v2(Bucket=bucket, Prefix=prefix)
    contents = resp.get("Contents", [])
    if not contents:
        return None
    candidates = []
    for obj in contents:
        key = obj.get("Key", "")
        if not key or key.endswith("/"):
            continue
        name = key.rsplit("/", 1)[-1]
        if suffixes and not any(name.endswith(sfx) for sfx in suffixes):
            continue
        candidates.append(obj)
    if not candidates:
        return None
    candidates.sort(key=lambda o: o.get("LastModified") or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
    return candidates[-1]["Key"]


def _discover_inputs_for_today(plant: str, date_str: str) -> tuple[Path, Path]:
    bucket = _env("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET environment variable is required.")

    schedule_prefix = _env("S3_SCHEDULE_PREFIX", f"generated/{plant}")
    meter_prefix = _env("S3_METER_PREFIX", f"raw/vedanjay/{plant}")

    schedule_key = _pick_latest_key(
        bucket,
        f"{schedule_prefix}/{date_str}",
        (
            f"{date_str}_current_final_schedule.csv",
            "current_final_schedule.csv",
            f"{date_str}_latest_schedule.csv",
            "_latest_schedule.csv",
            "_schedule.csv",
        ),
    )
    if not schedule_key:
        raise RuntimeError(f"No schedule CSV found in s3://{bucket}/{schedule_prefix}/{date_str}/")

    meter_key = _pick_latest_key(
        bucket,
        f"{meter_prefix}/{date_str}/meter_data",
        (".csv",),
    )
    if not meter_key:
        raise RuntimeError(f"No meter CSV found in s3://{bucket}/{meter_prefix}/{date_str}/meter_data/")

    workdir = Path(_env("SIMOUR_STORAGE_ROOT", "/tmp")) / "context_generator" / plant.lower() / date_str
    schedule_path = _download_s3_object(bucket, schedule_key, workdir / Path(schedule_key).name)
    meter_path = _download_s3_object(bucket, meter_key, workdir / Path(meter_key).name)
    return schedule_path, meter_path


def lambda_handler(event, context):
    plant = _env("PLANT_NAME", "SIRMOUR").upper()
    date_str = (event or {}).get("target_date") or _today_str()
    bucket = _env("S3_BUCKET")

    if state_sync.is_enabled():
        state_sync.refresh_state_from_s3(bucket=bucket)

    schedule_csv, actual_meter_csv = _discover_inputs_for_today(plant, date_str)
    entry = daily_feedback.process_schedule_feedback(
        schedule_csv,
        actual_meter_csv,
        source_label=f"{plant.lower()} nightly context generation",
    )

    if state_sync.is_enabled():
        state_sync.push_state_to_s3(bucket=bucket)

    return {
        "plant": plant,
        "date": date_str,
        "schedule_csv": str(schedule_csv),
        "actual_meter_csv": str(actual_meter_csv),
        "context_created": bool(entry),
        "summary": entry["summary"] if entry else None,
    }

