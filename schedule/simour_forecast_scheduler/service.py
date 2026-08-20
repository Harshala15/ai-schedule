"""Scheduler job runner for the SIMOUR forecast Lambda."""

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
from simour_forecast_scheduler import settings, storage


_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")


@dataclass(frozen=True)
class CaptureSelection:
    target_date: str
    target_time: str
    target_dt: dt.datetime
    capture_time: dt.datetime
    screenshot_dir: Path
    video_path: Path
    meter_path: Path
    screenshot_key_prefix: str
    video_key: str
    meter_key: str


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
    match = _TIMESTAMP_RE.search(value or "")
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _extract_filename_date(value: str) -> dt.date | None:
    """Try to infer the calendar date encoded in an S3 object name.

    Supports common plant file name patterns such as:
    - 2026_08_18_SOLAR_INV.csv
    - bhupalpally_20260818.csv
    - 2026-08-18_meter_data.csv
    """
    match = _DATE_RE.search(value or "")
    if not match:
        return None
    try:
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _relative_key(key: str, prefix: str) -> str:
    prefix = prefix.strip("/ ")
    if key.startswith(prefix):
        return key[len(prefix):].lstrip("/")
    return key.lstrip("/")


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
        raise FileNotFoundError(f"No video objects found under {day_video_prefix}/")

    matching_videos = []
    for obj in video_objects:
        ts = _extract_timestamp(Path(obj.key).name)
        if ts is None or ts > target_dt:
            continue
        matching_videos.append((ts, obj))
    matching_videos.sort(key=lambda pair: pair[0])
    selected_video = matching_videos[-1][1] if matching_videos else None

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
        target_date_only = target_dt.date()
        ranked = []
        for obj in meter_objects:
            file_date = _extract_filename_date(Path(obj.key).name)
            if file_date is None or file_date > target_date_only:
                continue
            ranked.append((file_date, obj.last_modified or dt.datetime.min.replace(tzinfo=dt.timezone.utc), obj.key, obj))
        if ranked:
            ranked.sort()
            selected_meter = ranked[-1][3]
        else:
            meter_objects.sort(key=lambda obj: (obj.last_modified or dt.datetime.min.replace(tzinfo=dt.timezone.utc), obj.key))
            selected_meter = meter_objects[-1]

    work_root = _storage_subpath("_scheduler_work", date_str, target_dt.strftime("%H-%M"))
    screenshot_dir = work_root / "screenshots"
    video_dir = work_root / "video"
    meter_dir = work_root / "meter"
    for folder in (screenshot_dir, video_dir, meter_dir):
        folder.mkdir(parents=True, exist_ok=True)

    for folder in (screenshot_dir, video_dir, meter_dir):
        for child in folder.glob("*"):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

    if selected_video is not None:
        storage.download_file(bucket, selected_video.key, video_dir / Path(selected_video.key).name)
    if selected_meter is not None:
        storage.download_file(bucket, selected_meter.key, meter_dir / Path(selected_meter.key).name)

    return CaptureSelection(
        target_date=date_str,
        target_time=target_dt.strftime("%H:%M"),
        target_dt=target_dt,
        capture_time=_extract_timestamp(Path(selected_video.key).name) if selected_video is not None else target_dt,
        screenshot_dir=screenshot_dir,
        video_path=video_dir / Path(selected_video.key).name if selected_video is not None else None,
        meter_path=meter_dir / Path(selected_meter.key).name if selected_meter is not None else None,
        screenshot_key_prefix="",
        video_key=selected_video.key if selected_video is not None else "",
        meter_key=selected_meter.key if selected_meter is not None else "",
    )


def _build_image_map(screenshot_dir: Path) -> dict[str, str]:
    image_map: dict[str, str] = {}
    for layer, description in config.LAYERS.items():
        path = screenshot_dir / f"{layer}.png"
        if path.exists():
            image_map[str(path)] = description
    return image_map


def _layer_from_screenshot_name(name: str) -> str | None:
    stem = Path(name).stem.lower()
    for layer in config.LAYERS:
        if stem == layer.lower() or stem.endswith(f"_{layer.lower()}"):
            return layer
    return None


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
    """Return the frozen cutoff for current_final_schedule.csv.

    Kasipet and Bhupalpally use a 45-minute delay, while SIRMOUR keeps
    the longer 90-minute delay. The cutoff is rounded up to the next
    15-minute schedule boundary.
    """
    freeze_from = dt.datetime.strptime(f"{target_date} {target_time}", "%Y-%m-%d %H:%M")
    freeze_from += dt.timedelta(minutes=settings.EFFECTIVE_DELAY_MINUTES)
    remainder = freeze_from.minute % config.BLOCK_MINUTES
    if remainder or freeze_from.second or freeze_from.microsecond:
        freeze_from += dt.timedelta(minutes=config.BLOCK_MINUTES - remainder)
    return freeze_from.replace(second=0, microsecond=0)


def _write_current_final_schedule(latest_csv: Path, current_final_csv: Path, target_date: str, target_time: str) -> int:
    """Build the frozen cumulative file used as current_final_schedule.csv."""
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


def _download_previous_latest_schedule(
    bucket: str,
    schedule_prefix: str,
    target_date: str,
    target_time: str,
    latest_csv: Path,
) -> None:
    """Best-effort fetch of the prior daily schedule from S3.

    The Lambda only generates the current forecast slice for the chosen
    capture time. To preserve earlier blocks from the same day, we pull
    the most recent prior schedule into local storage before merging.
    """
    day_prefix = f"{schedule_prefix.rstrip('/')}/{target_date}"
    schedule_objects = storage.list_objects(bucket, day_prefix)
    if not schedule_objects:
        return

    target_minutes = int(target_time[:2]) * 60 + int(target_time[3:5])
    candidates: list[tuple[int, str]] = []
    for obj in schedule_objects:
        name = Path(obj.key).name
        if not name.endswith("_schedule.csv"):
            continue
        if name.endswith("_latest_schedule.csv"):
            name_time = target_minutes
        else:
            match = re.match(rf"{re.escape(target_date)}_(\d{{2}})-(\d{{2}})_schedule\.csv$", name)
            if not match:
                continue
            name_time = int(match.group(1)) * 60 + int(match.group(2))
        if name_time >= target_minutes:
            continue
        candidates.append((name_time, obj.key))

    if not candidates:
        return

    _, selected_key = max(candidates, key=lambda item: item[0])
    try:
        storage.download_file(bucket, selected_key, latest_csv)
    except Exception:
        return


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


def _blocks_from_time_to_end_of_day(forecast_start_time: dt.datetime) -> int:
    """Count 15-minute blocks from the first block after the capture
    time through the last daylight block ending at 19:00."""
    end_of_day = forecast_start_time.replace(hour=18, minute=45, second=0, microsecond=0)
    first_block = forecast_start_time if forecast_start_time.minute % 15 == 0 and forecast_start_time.second == 0 else None
    if first_block is None:
        minute_offset = 15 - (forecast_start_time.minute % 15)
        first_block = (forecast_start_time + dt.timedelta(minutes=minute_offset)).replace(second=0, microsecond=0)
    if first_block > end_of_day:
        return 0
    minutes_remaining = (end_of_day - first_block).total_seconds() / 60.0
    return int(minutes_remaining // 15) + 1


def _snapshot_metadata(
    selection: CaptureSelection,
    forecast_start: str,
    forecast_end: str,
    snapshot_csv_key: str,
    latest_csv_key: str,
    current_final_csv_key: str,
    trace_csv_key: str,
    current_trace_csv_key: str,
    snapshot_metadata_key: str,
    latest_metadata_key: str,
    generated_rows: int,
    preserved_rows: int,
    current_final_rows: int,
) -> dict:
    return {
        "status": "ok",
        "date": selection.target_date,
        "run_time": selection.target_time.replace(":", "-"),
        "forecast_start": forecast_start,
        "forecast_end": forecast_end,
        "capture_time": selection.capture_time.strftime("%Y-%m-%d %H:%M:%S"),
        "video_key": selection.video_key,
        "meter_key": selection.meter_key,
        "snapshot_csv_key": snapshot_csv_key,
        "snapshot_metadata_key": snapshot_metadata_key,
        "latest_csv_key": latest_csv_key,
        "current_final_csv_key": current_final_csv_key,
        "trace_csv_key": trace_csv_key,
        "current_trace_csv_key": current_trace_csv_key,
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
    image_map = _build_image_map(selection.screenshot_dir)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.refresh_state_from_s3(bucket=bucket)

    # Start the schedule exactly at the revision time so the first run
    # of the day can include the 06:45 block instead of skipping to the
    # next quarter-hour.
    forecast_start_dt = target_dt
    forecast_end_dt = dt.datetime.strptime(f"{target_date} 19:00", "%Y-%m-%d %H:%M")
    num_blocks = _blocks_from_time_to_end_of_day(forecast_start_dt)
    generated_root = _prefix_to_local_dir(schedule_prefix, target_date)
    generated_root.mkdir(parents=True, exist_ok=True)

    work_output_dir = _storage_subpath("_scheduler_work", target_date, target_dt.strftime("%H-%M"), "pipeline_output")
    if work_output_dir.exists():
        shutil.rmtree(work_output_dir)
    work_output_dir.mkdir(parents=True, exist_ok=True)

    run_pipeline.run_prediction_pipeline(
        image_map=image_map,
        video_path=selection.video_path,
        reference_time=forecast_start_dt,
        num_blocks=num_blocks,
        output_dir=work_output_dir,
        intraday_actuals_path=selection.meter_path,
    )

    snapshot_source = work_output_dir / f"{config.PLANT_NAME}_energy_generation_{target_date}.csv"
    if not snapshot_source.exists():
        raise FileNotFoundError(f"Expected schedule output was not produced: {snapshot_source}")

    features_log_source = work_output_dir / f"{config.PLANT_NAME}_features_log_{target_date}.csv"
    snapshot_csv = generated_root / f"{target_date}_{target_time.replace(':', '-')}_schedule.csv"
    snapshot_metadata = generated_root / f"{target_date}_{target_time.replace(':', '-')}_metadata.json"
    latest_csv = generated_root / f"{target_date}_latest_schedule.csv"
    current_final_csv = generated_root / _current_final_schedule_name(target_date)
    trace_source = work_output_dir / f"{config.PLANT_NAME}_forecast_trace_{target_date}.csv"
    trace_csv = generated_root / "forecast_trace.csv"
    current_trace_csv = generated_root / "current_forecast_trace.csv"
    latest_metadata = generated_root / f"{target_date}_latest_metadata.json"

    shutil.copyfile(snapshot_source, snapshot_csv)
    _download_previous_latest_schedule(bucket, schedule_prefix, target_date, target_time, latest_csv)
    _download_previous_current_final_schedule(bucket, schedule_prefix, target_date, current_final_csv)
    snapshot_rows, preserved_rows, merged_rows = _merge_latest_schedule(snapshot_source, latest_csv)
    # The dated revision file should be cumulative too, not just the
    # separate "latest" file.
    shutil.copyfile(latest_csv, snapshot_csv)
    current_final_rows = _write_current_final_schedule(latest_csv, current_final_csv, target_date, target_time)

    if not trace_source.exists():
        trace_candidates = sorted(work_output_dir.glob(f"{config.PLANT_NAME}_forecast_trace_*.csv"))
        if trace_candidates:
            trace_source = trace_candidates[-1]
    if trace_source.exists():
        shutil.copyfile(trace_source, trace_csv)
        shutil.copyfile(trace_source, current_trace_csv)
    else:
        raise FileNotFoundError(f"Expected forecast trace output was not produced: {trace_source}")

    if features_log_source.exists():
        features_log_target = config.FEATURES_LOG_DIR / features_log_source.name
        features_log_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(features_log_source, features_log_target)
    else:
        fallback_features = sorted(work_output_dir.glob(f"{config.PLANT_NAME}_features_log_*.csv"))
        if fallback_features:
            features_log_target = config.FEATURES_LOG_DIR / fallback_features[-1].name
            features_log_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fallback_features[-1], features_log_target)
        else:
            raise FileNotFoundError(f"Expected features log output was not produced: {features_log_source}")

    forecast_start_label = forecast_start_dt.strftime("%Y-%m-%d %H:%M")
    forecast_end_label = forecast_end_dt.strftime("%Y-%m-%d %H:%M")
    metadata = _snapshot_metadata(
        selection=selection,
        forecast_start=forecast_start_label,
        forecast_end=forecast_end_label,
        snapshot_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_csv.name}",
        latest_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_csv.name}",
        current_final_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}",
        trace_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{trace_csv.name}",
        current_trace_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{current_trace_csv.name}",
        snapshot_metadata_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_metadata.name}",
        latest_metadata_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_metadata.name}",
        generated_rows=merged_rows,
        preserved_rows=preserved_rows,
        current_final_rows=current_final_rows,
    )
    snapshot_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["current_final_csv_key"], current_final_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["current_trace_csv_key"], current_trace_csv, content_type="text/csv")
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)

    legacy_current_final_csv = generated_root / "current_final_schedule.csv"
    if legacy_current_final_csv != current_final_csv:
        shutil.copyfile(current_final_csv, legacy_current_final_csv)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.push_state_to_s3(bucket=bucket)

    return metadata
