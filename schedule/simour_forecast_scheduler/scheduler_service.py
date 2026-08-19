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
    base_prefix = f"{capture_prefix.rstrip('/')}/{date_str}"
    screenshot_prefix = f"{base_prefix}/windy/screenshots"
    video_prefix = f"{base_prefix}/windy/videos"
    screenshot_objects = _list_capture_objects(bucket, screenshot_prefix)
    if not screenshot_objects:
        raise FileNotFoundError(f"No screenshot objects found under {screenshot_prefix}/")

    bundles: dict[dt.datetime, list[storage.S3ObjectRef]] = {}
    for obj in screenshot_objects:
        rel = _relative_key(obj.key, screenshot_prefix)
        parts = Path(rel).parts
        if len(parts) < 2:
            continue
        capture_time = _extract_timestamp(parts[0])
        if capture_time is None or capture_time > target_dt:
            continue
        bundles.setdefault(capture_time, []).append(obj)

    if not bundles:
        raise FileNotFoundError(
            f"No screenshot bundle at or before {target_dt.strftime('%Y-%m-%d %H:%M')} under {screenshot_prefix}/"
        )

    capture_time = max(bundles)
    selected_screenshots = bundles[capture_time]
    selected_screenshots.sort(key=lambda item: item.key)

    video_objects = _list_capture_objects(bucket, video_prefix)
    if not video_objects:
        raise FileNotFoundError(f"No video objects found under {video_prefix}/")

    selected_video = None
    matching_videos = []
    for obj in video_objects:
        ts = _extract_timestamp(Path(obj.key).name)
        if ts is None or ts > target_dt:
            continue
        matching_videos.append((ts, obj))
    if matching_videos:
        matching_videos.sort(key=lambda pair: pair[0])
        same_ts = [obj for ts, obj in matching_videos if ts == capture_time]
        selected_video = same_ts[-1] if same_ts else matching_videos[-1][1]
    if selected_video is None:
        raise FileNotFoundError(
            f"No satellite video found at or before {target_dt.strftime('%Y-%m-%d %H:%M')} under {video_prefix}/"
        )

    meter_base_prefix = f"{meter_prefix.rstrip('/')}/{date_str}/meter_data"
    meter_objects = _list_capture_objects(bucket, meter_base_prefix)
    if not meter_objects:
        raise FileNotFoundError(f"No meter objects found under {meter_base_prefix}/")
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

    for obj in selected_screenshots:
        rel = _relative_key(obj.key, screenshot_prefix)
        parts = Path(rel).parts
        if len(parts) < 2:
            continue
        local_name = parts[-1]
        storage.download_file(bucket, obj.key, screenshot_dir / local_name)

    storage.download_file(bucket, selected_video.key, video_dir / Path(selected_video.key).name)
    storage.download_file(bucket, selected_meter.key, meter_dir / Path(selected_meter.key).name)

    return CaptureSelection(
        target_date=date_str,
        target_time=target_dt.strftime("%H:%M"),
        target_dt=target_dt,
        capture_time=capture_time,
        screenshot_dir=screenshot_dir,
        video_path=video_dir / Path(selected_video.key).name,
        meter_path=meter_dir / Path(selected_meter.key).name,
        screenshot_key_prefix=screenshot_prefix,
        video_key=selected_video.key,
        meter_key=selected_meter.key,
    )


def _build_image_map(screenshot_dir: Path) -> dict[str, str]:
    image_map: dict[str, str] = {}
    for layer, description in config.LAYERS.items():
        path = screenshot_dir / f"{layer}.png"
        if path.exists():
            image_map[str(path)] = description
    if not image_map:
        raise FileNotFoundError(f"No layer screenshots found in {screenshot_dir}")
    return image_map


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


def _freeze_boundary_time(target_time: str) -> str:
    """Return the next revision boundary after target_time."""
    try:
        current_time = dt.datetime.strptime(target_time, "%H:%M").time()
    except ValueError:
        return target_time

    revision_times = []
    for candidate in config.CAPTURE_TIMES:
        try:
            revision_times.append(dt.datetime.strptime(candidate, "%H:%M").time())
        except ValueError:
            continue

    for revision_time in revision_times:
        if revision_time > current_time:
            return revision_time.strftime("%H:%M")
    return target_time


def _write_current_final_schedule(latest_csv: Path, current_final_csv: Path, target_date: str, target_time: str) -> int:
    latest_fields, latest_rows = _read_csv_rows(latest_csv)
    previous_fields, previous_rows = _read_csv_rows(current_final_csv) if current_final_csv.exists() else ([], [])

    fieldnames = latest_fields or previous_fields
    if not fieldnames:
        raise ValueError("Schedule CSV did not contain any headers.")

    freeze_from = dt.datetime.strptime(
        f"{target_date} {_freeze_boundary_time(target_time)}",
        "%Y-%m-%d %H:%M",
    )

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

    # Start exactly on the revision time so the first run of the day can
    # keep the 06:45 block instead of skipping straight to 07:00.
    forecast_start_dt = target_dt
    forecast_end_dt = dt.datetime.strptime(f"{target_date} 19:00", "%Y-%m-%d %H:%M")
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
        output_dir=work_output_dir,
        intraday_actuals_path=selection.meter_path,
    )

    snapshot_source = work_output_dir / f"{config.PLANT_NAME}_energy_generation_{target_date}.csv"
    if not snapshot_source.exists():
        raise FileNotFoundError(f"Expected schedule output was not produced: {snapshot_source}")

    snapshot_csv = generated_root / f"{target_date}_{target_time.replace(':', '-')}_schedule.csv"
    snapshot_metadata = generated_root / f"{target_date}_{target_time.replace(':', '-')}_metadata.json"
    latest_csv = generated_root / f"{target_date}_latest_schedule.csv"
    current_final_csv = generated_root / "current_final_schedule.csv"
    latest_metadata = generated_root / f"{target_date}_latest_metadata.json"

    shutil.copyfile(snapshot_source, snapshot_csv)
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
    snapshot_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_file(bucket, f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}", current_final_csv, content_type="text/csv")
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.push_state_to_s3(bucket=bucket)

    return metadata
