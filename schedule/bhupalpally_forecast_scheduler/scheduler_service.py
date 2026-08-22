"""Scheduler job runner for the Bhupalpally forecast Lambda."""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import config
import daily_feedback
import prediction_store
import run_pipeline
import state_sync
from bhupalpally_forecast_scheduler import ecmwf_weather, settings, storage


@dataclass(frozen=True)
class CaptureSelection:
    target_date: str
    target_time: str
    target_dt: dt.datetime
    capture_time: dt.datetime
    screenshot_dir: Path
    video_path: Path | None
    meter_path: Path | None
    screenshot_key_prefix: str
    video_key: str
    meter_key: str
    meter_rows_available: int = 0
    meter_rows_used: int = 0
    weather_summary: str = ""
    context_summary: str = ""
    context_payload: dict | None = None


def _parse_target_datetime(event: dict | None) -> tuple[str, str, dt.datetime]:
    now = dt.datetime.now()
    target_date = (event or {}).get("target_date") or now.strftime("%Y-%m-%d")
    target_time = (event or {}).get("target_time") or now.strftime("%H:%M")
    target_dt = dt.datetime.strptime(f"{target_date} {target_time}", "%Y-%m-%d %H:%M")
    return target_date, target_time, target_dt


def _storage_subpath(*parts: str) -> Path:
    return config.STORAGE_ROOT.joinpath(*parts)


def _prefix_to_local_dir(prefix: str, *parts: str) -> Path:
    cleaned = [part for part in prefix.strip("/ ").split("/") if part]
    return config.STORAGE_ROOT.joinpath(*cleaned, *parts)


def _extract_timestamp(value: str) -> dt.datetime | None:
    matches = [
        r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})",
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
    ]
    for pattern in matches:
        match = re.search(pattern, value)
        if not match:
            continue
        text = match.group(1)
        for fmt in ("%Y-%m-%d_%H-%M-%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
    for fmt in ("%Y-%m-%d_%H-%M-%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _list_capture_objects(bucket: str, prefix: str) -> list[storage.S3ObjectRef]:
    return storage.list_objects(bucket, prefix.rstrip("/") + "/")


def _pick_latest_capture_bundle(
    bucket: str,
    capture_prefix: str,
    meter_prefix: str,
    target_dt: dt.datetime,
) -> CaptureSelection:
    date_str = target_dt.strftime("%Y-%m-%d")
    day_prefix = f"{capture_prefix.rstrip('/')}/{date_str}"
    day_video_prefix = f"{day_prefix}/windy/videos"

    video_objects = _list_capture_objects(bucket, day_video_prefix)
    if not video_objects:
        video_objects = [
            obj for obj in _list_capture_objects(bucket, capture_prefix)
            if "/windy/videos/" in obj.key
        ]

    selected_video = None
    matching_videos = []
    for obj in video_objects:
        ts = _extract_timestamp(Path(obj.key).name)
        if ts is None or ts > target_dt:
            continue
        matching_videos.append((ts, obj))
    if matching_videos:
        def _video_preference(obj: storage.S3ObjectRef) -> int:
            filename = Path(obj.key).name.lower()
            if filename.endswith("_full.webm") or "_full.webm" in filename:
                return 3
            if filename.endswith(".webm"):
                return 2
            if filename.endswith(".mp4") and "_clean.mp4" not in filename:
                return 1
            if filename.endswith("_clean.mp4") or filename.endswith(".mp4"):
                return 0
            return -1

        matching_videos.sort(key=lambda pair: (pair[0], _video_preference(pair[1]), pair[1].size, pair[1].key))
        selected_video = matching_videos[-1][1]

    meter_day_prefix = f"{meter_prefix.rstrip('/')}/{date_str}/meter_data"
    meter_objects = _list_capture_objects(bucket, meter_day_prefix)
    if not meter_objects:
        print(
            f"  [WARN] No meter file found under {meter_day_prefix}/; "
            "continuing without intraday actuals."
        )
        meter_objects = _list_capture_objects(bucket, meter_prefix)

    selected_meter = None
    if meter_objects:
        meter_objects.sort(key=lambda obj: (obj.last_modified or dt.datetime.min.replace(tzinfo=dt.timezone.utc), obj.key))
        selected_meter = meter_objects[-1]

    work_root = _storage_subpath("_scheduler_work", date_str, target_dt.strftime("%H-%M"))
    screenshot_dir = work_root / "screenshots"
    video_dir = work_root / "video"
    meter_dir = work_root / "meter"
    for folder in (screenshot_dir, video_dir, meter_dir):
        folder.mkdir(parents=True, exist_ok=True)

    # Clear any stale files from an earlier run for the same cutoff.
    for folder in (screenshot_dir, video_dir, meter_dir):
        for child in folder.glob("*"):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

    meter_rows_available = 0
    meter_rows_used = 0
    clipped_meter_path = None
    if selected_meter is not None:
        raw_meter_path = meter_dir / Path(selected_meter.key).name
        storage.download_file(bucket, selected_meter.key, raw_meter_path)
        clipped_meter_path, meter_rows_available, meter_rows_used = _clip_meter_to_cutoff(
            raw_meter_path,
            meter_dir / f"{Path(selected_meter.key).stem}_upto_{target_dt.strftime('%H-%M')}.csv",
            target_dt,
        )

    if selected_video is not None:
        storage.download_file(bucket, selected_video.key, video_dir / Path(selected_video.key).name)

    weather_report = ecmwf_weather.fetch_ecmwf_weather_summary(
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
        reference_time=target_dt,
        hours_ahead=settings.FORECAST_HORIZON_HOURS,
        timezone=settings.DEFAULT_TIMEZONE,
        tilt=settings.ECMWF_TILT_DEGREES,
        azimuth=settings.ECMWF_AZIMUTH_DEGREES,
    )
    context_payload = _load_prediction_context_payload()
    context_summary = daily_feedback.format_context_for_prompt()

    return CaptureSelection(
        target_date=date_str,
        target_time=target_dt.strftime("%H:%M"),
        target_dt=target_dt,
        capture_time=_extract_timestamp(Path(selected_video.key).name) if selected_video is not None else target_dt,
        screenshot_dir=screenshot_dir,
        video_path=video_dir / Path(selected_video.key).name if selected_video is not None else None,
        meter_path=clipped_meter_path,
        screenshot_key_prefix="",
        video_key=selected_video.key if selected_video is not None else "",
        meter_key=selected_meter.key if selected_meter is not None else "",
        meter_rows_available=meter_rows_available,
        meter_rows_used=meter_rows_used,
        weather_summary=weather_report.get("prompt_text", ""),
        context_summary=context_summary,
        context_payload=context_payload,
    )


def _load_prediction_context_payload() -> dict:
    daily_feedback.ensure_prediction_context_exists()
    try:
        return json.loads(config.PREDICTION_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _clip_meter_to_cutoff(source_csv: Path, destination_csv: Path, cutoff_dt: dt.datetime) -> tuple[Path, int, int]:
    """Write a meter CSV trimmed to the revision cutoff."""
    with open(source_csv, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        timestamp_column = daily_feedback._pick_first_existing_column(  # type: ignore[attr-defined]
            fieldnames,
            daily_feedback.RAW_METER_TIMESTAMP_COLUMNS,  # type: ignore[attr-defined]
        )
        rows = list(reader)

    if timestamp_column is None:
        raise ValueError(
            f"Could not locate a timestamp column in {source_csv.name}; "
            "unable to safely trim meter data to the revision cutoff."
        )

    kept_rows = []
    for row in rows:
        normalized = daily_feedback._normalize_timestamp(row.get(timestamp_column))  # type: ignore[attr-defined]
        if normalized is None:
            continue
        row_dt = dt.datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        if row_dt > cutoff_dt:
            continue
        kept_rows.append(row)

    if not kept_rows:
        destination_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(destination_csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
        return destination_csv, len(rows), 0

    destination_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(destination_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in kept_rows:
            writer.writerow(row)
    return destination_csv, len(rows), len(kept_rows)


def _read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    with open(csv_path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _row_time_key(row: dict) -> str:
    if row.get("Time"):
        return row["Time"]
    interval = row.get("Time Interval (15 minute interval)", "")
    if " - " in interval:
        return interval.split(" - ", 1)[0]
    return ""


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _merge_latest_schedule(snapshot_csv: Path, latest_csv: Path) -> tuple[int, int, int]:
    snapshot_fields, snapshot_rows = _read_csv_rows(snapshot_csv)
    if not snapshot_rows:
        raise ValueError(f"No schedule rows were produced in {snapshot_csv}")

    if latest_csv.exists():
        latest_fields, latest_rows = _read_csv_rows(latest_csv)
    else:
        latest_fields, latest_rows = snapshot_fields, []

    fieldnames = snapshot_fields or latest_fields
    if not fieldnames:
        raise ValueError("Schedule CSV did not contain any headers.")

    merged_by_time: dict[str, dict] = {}
    for row in latest_rows:
        key = _row_time_key(row)
        if key:
            merged_by_time[key] = dict(row)

    snapshot_times: set[str] = set()
    for row in snapshot_rows:
        key = _row_time_key(row)
        if not key:
            continue
        snapshot_times.add(key)
        merged_by_time[key] = dict(row)

    preserved_rows = sum(1 for row in latest_rows if _row_time_key(row) and _row_time_key(row) not in snapshot_times)

    def _sort_key(row: dict) -> dt.datetime:
        key = _row_time_key(row)
        try:
            return dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return dt.datetime.max

    merged_rows = sorted(merged_by_time.values(), key=_sort_key)
    _write_csv(latest_csv, fieldnames, merged_rows)
    return len(snapshot_rows), preserved_rows, len(merged_rows)


def _freeze_from_datetime(target_date: str, target_time: str) -> dt.datetime:
    """Return the frozen cutoff for current_final_schedule.csv."""
    freeze_from = dt.datetime.strptime(f"{target_date} {target_time}", "%Y-%m-%d %H:%M")
    freeze_from += dt.timedelta(minutes=settings.EFFECTIVE_DELAY_MINUTES)
    remainder = freeze_from.minute % config.BLOCK_MINUTES
    if remainder or freeze_from.second or freeze_from.microsecond:
        freeze_from += dt.timedelta(minutes=config.BLOCK_MINUTES - remainder)
    return freeze_from.replace(second=0, microsecond=0)


def _write_current_final_schedule(latest_csv: Path, current_final_csv: Path, target_date: str, target_time: str) -> int:
    latest_fields, latest_rows = _read_csv_rows(latest_csv)
    previous_fields, previous_rows = _read_csv_rows(current_final_csv) if current_final_csv.exists() else ([], [])

    fieldnames = latest_fields or previous_fields
    if not fieldnames:
        raise ValueError("Schedule CSV did not contain any headers.")

    freeze_from = _freeze_from_datetime(target_date, target_time)

    def _row_dt(row: dict) -> dt.datetime | None:
        key = _row_time_key(row)
        if not key:
            return None
        try:
            return dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    merged_by_time: dict[str, dict] = {}
    for row in previous_rows:
        row_dt = _row_dt(row)
        key = _row_time_key(row)
        if key and row_dt is not None and row_dt < freeze_from:
            merged_by_time[key] = dict(row)

    for row in latest_rows:
        row_dt = _row_dt(row)
        key = _row_time_key(row)
        if key and row_dt is not None and row_dt >= freeze_from:
            merged_by_time[key] = dict(row)

    def _sort_key(row: dict) -> dt.datetime:
        key = _row_time_key(row)
        try:
            return dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return dt.datetime.max

    frozen_rows = sorted(merged_by_time.values(), key=_sort_key)
    _write_csv(current_final_csv, fieldnames, frozen_rows)
    return len(frozen_rows)


def _current_final_schedule_name(target_date: str) -> str:
    return f"{target_date}_current_final_schedule.csv"


def _download_previous_current_final_schedule(
    bucket: str,
    schedule_prefix: str,
    target_date: str,
    current_final_csv: Path,
) -> None:
    """Restore the prior cumulative final schedule from S3 if present."""
    current_final_key = f"{schedule_prefix.rstrip('/')}/{target_date}/{_current_final_schedule_name(target_date)}"
    try:
        storage.download_file(bucket, current_final_key, current_final_csv)
    except Exception:
        return


def _snapshot_metadata(
    selection: CaptureSelection,
    forecast_start: str,
    forecast_end: str,
    snapshot_csv_key: str,
    latest_csv_key: str,
    current_final_csv_key: str,
    snapshot_metadata_key: str,
    latest_metadata_key: str,
    generated_rows: int,
    preserved_rows: int,
    current_final_rows: int,
) -> dict:
    context_entries = []
    context_summary = selection.context_summary
    if isinstance(selection.context_payload, dict):
        entries = selection.context_payload.get("entries", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    context_entries.append({
                        "date": entry.get("date"),
                        "summary": entry.get("summary"),
                        "bias": entry.get("bias"),
                    })
    return {
        "status": "ok",
        "date": selection.target_date,
        "run_time": selection.target_time.replace(":", "-"),
        "forecast_start": forecast_start,
        "forecast_end": forecast_end,
        "forecast_horizon_hours": settings.FORECAST_HORIZON_HOURS,
        "capture_time": selection.capture_time.strftime("%Y-%m-%d %H:%M:%S"),
        "video_key": selection.video_key,
        "meter_key": selection.meter_key,
        "meter_rows_available": selection.meter_rows_available,
        "meter_rows_used": selection.meter_rows_used,
        "weather_summary": selection.weather_summary,
        "context_summary": context_summary,
        "context_entries": context_entries,
        "plant_name": config.PLANT_NAME,
        "plant_profile_path": str(getattr(config, "PLANT_PROFILE_PATH", "")),
        "plant_lat": config.PLANT_LAT,
        "plant_lon": config.PLANT_LON,
        "plant_capacity_mw": config.PLANT_CAPACITY_MW,
        "plant_dc_capacity_mw": getattr(config, "PLANT_DC_CAPACITY_MW", None),
        "plant_max_feed_in_mw": getattr(config, "PLANT_MAX_FEED_IN_MW", None),
        "plant_tilt_deg": getattr(config, "PLANT_TILT_DEG", None),
        "plant_orientation_from_south_deg": getattr(config, "PLANT_ORIENTATION_FROM_SOUTH_DEG", None),
        "plant_tracker_type": getattr(config, "PLANT_TRACKER_TYPE", None),
        "plant_availability_planned_pct": getattr(config, "PLANT_AVAILABILITY_PLANNED_PCT", None),
        "plant_ppa_rate_inr_per_kwh": getattr(config, "PLANT_PPA_RATE_INR_PER_KWH", None),
        "plant_eeg_id": getattr(config, "PLANT_EEG_ID", ""),
        "plant_key": getattr(config, "PLANT_KEY", ""),
        "snapshot_csv_key": snapshot_csv_key,
        "snapshot_metadata_key": snapshot_metadata_key,
        "latest_csv_key": latest_csv_key,
        "current_final_csv_key": current_final_csv_key,
        "latest_metadata_key": latest_metadata_key,
        "generated_rows": generated_rows,
        "preserved_rows": preserved_rows,
        "current_final_rows": current_final_rows,
    }


def run_schedule_job(
    bucket: str,
    capture_prefix: str,
    meter_prefix: str,
    schedule_prefix: str,
    event: dict | None = None,
) -> dict:
    target_date, target_time, target_dt = _parse_target_datetime(event)
    selection = _pick_latest_capture_bundle(bucket, capture_prefix, meter_prefix, target_dt)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.refresh_state_from_s3(bucket=bucket)

    forecast_start_dt = target_dt
    forecast_end_dt = target_dt + dt.timedelta(hours=settings.FORECAST_HORIZON_HOURS)
    generated_root = _prefix_to_local_dir(schedule_prefix, target_date)
    generated_root.mkdir(parents=True, exist_ok=True)

    work_output_dir = _storage_subpath("_scheduler_work", target_date, target_dt.strftime("%H-%M"), "pipeline_output")
    if work_output_dir.exists():
        shutil.rmtree(work_output_dir)
    work_output_dir.mkdir(parents=True, exist_ok=True)

    run_pipeline.run_prediction_pipeline(
        image_map={},
        video_path=selection.video_path,
        reference_time=forecast_start_dt,
        num_blocks=settings.FORECAST_BLOCKS,
        output_dir=work_output_dir,
        intraday_actuals_path=selection.meter_path,
        weather_text=selection.weather_summary,
        context_text=selection.context_summary,
    )

    snapshot_source = work_output_dir / f"{config.PLANT_NAME}_energy_generation_{target_date}.csv"
    if not snapshot_source.exists():
        raise FileNotFoundError(f"Expected schedule output was not produced: {snapshot_source}")

    snapshot_csv = generated_root / f"{target_date}_{target_time.replace(':', '-')}_schedule.csv"
    snapshot_metadata = generated_root / f"{target_date}_{target_time.replace(':', '-')}_metadata.json"
    latest_csv = generated_root / f"{target_date}_latest_schedule.csv"
    current_final_csv = generated_root / _current_final_schedule_name(target_date)
    latest_metadata = generated_root / f"{target_date}_latest_metadata.json"

    shutil.copyfile(snapshot_source, snapshot_csv)
    _download_previous_current_final_schedule(bucket, schedule_prefix, target_date, current_final_csv)
    snapshot_rows, preserved_rows, merged_rows = _merge_latest_schedule(snapshot_source, latest_csv)
    shutil.copyfile(latest_csv, snapshot_csv)
    current_final_rows = _write_current_final_schedule(latest_csv, current_final_csv, target_date, target_time)

    forecast_start_label = forecast_start_dt.strftime("%Y-%m-%d %H:%M")
    forecast_end_label = forecast_end_dt.strftime("%Y-%m-%d %H:%M")
    metadata = _snapshot_metadata(
        selection=selection,
        forecast_start=forecast_start_label,
        forecast_end=forecast_end_label,
        snapshot_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_csv.name}",
        latest_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_csv.name}",
        current_final_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}",
        snapshot_metadata_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_metadata.name}",
        latest_metadata_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_metadata.name}",
        generated_rows=merged_rows,
        preserved_rows=preserved_rows,
        current_final_rows=current_final_rows,
    )
    metadata["snapshot_rows"] = snapshot_rows
    snapshot_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_file(bucket, f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}", current_final_csv, content_type="text/csv")
    legacy_current_final_csv = generated_root / "current_final_schedule.csv"
    if legacy_current_final_csv != current_final_csv:
        shutil.copyfile(current_final_csv, legacy_current_final_csv)
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.push_state_to_s3(bucket=bucket)

    return metadata
