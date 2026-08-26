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
import run_pipeline
from modules.feedback import daily_feedback
from modules.storage import prediction_store
from modules.storage import state_sync
from modules import plant_performance_utils as shared_performance_utils
from modules import pvlib_utils as shared_pvlib_utils
from modules import schedule_utils as shared_schedule_utils
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
    return shared_schedule_utils.parse_target_datetime(event)


def _storage_subpath(*parts: str) -> Path:
    return shared_schedule_utils.storage_subpath(*parts)


def _prefix_to_local_dir(prefix: str, *parts: str) -> Path:
    return shared_schedule_utils.prefix_to_local_dir(prefix, *parts)


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
        matching_videos.sort(key=lambda pair: pair[0])
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
        selected_meter = shared_schedule_utils.select_preferred_meter_object(meter_objects)

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

    if selected_video is not None:
        storage.download_file(bucket, selected_video.key, video_dir / Path(selected_video.key).name)
    if selected_meter is not None:
        storage.download_file(bucket, selected_meter.key, meter_dir / Path(selected_meter.key).name)
        clipped_meter_path, _, _ = _clip_meter_to_cutoff(
            meter_dir / Path(selected_meter.key).name,
            meter_dir / f"{Path(selected_meter.key).stem}_upto_{target_dt.strftime('%H-%M')}.csv",
            target_dt,
        )
    
    return CaptureSelection(
        target_date=date_str,
        target_time=target_dt.strftime("%H:%M"),
        target_dt=target_dt,
        capture_time=_extract_timestamp(Path(selected_video.key).name) if selected_video is not None else target_dt,
        screenshot_dir=screenshot_dir,
        video_path=video_dir / Path(selected_video.key).name if selected_video is not None else None,
        meter_path=clipped_meter_path if selected_meter is not None else None,
        screenshot_key_prefix="",
        video_key=selected_video.key if selected_video is not None else "",
        meter_key=selected_meter.key if selected_meter is not None else "",
    )


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

    destination_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(destination_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in kept_rows:
            writer.writerow(row)
    return destination_csv, len(rows), len(kept_rows)


def _generate_pre_revision_feedback(
    bucket: str,
    schedule_prefix: str,
    target_date: str,
    target_time: str,
    meter_path: Path | None,
    work_root: Path,
) -> None:
    if meter_path is None or not meter_path.exists():
        return

    pre_feedback_schedule_csv = work_root / f"{target_date}_{target_time.replace(':', '-')}_pre_revision_current_final.csv"
    _download_previous_current_final_schedule(bucket, schedule_prefix, target_date, pre_feedback_schedule_csv)
    if not pre_feedback_schedule_csv.exists():
        print("  [INFO] No previous current-final schedule available yet; skipping pre-revision feedback JSON.")
        return

    try:
        entry = daily_feedback.process_schedule_feedback(
            pre_feedback_schedule_csv,
            meter_path,
            source_label=f"{config.PLANT_NAME.lower()} pre-revision feedback",
            entry_date=target_date,
        )
        if entry:
            print(
                f"  [FEEDBACK] Created pre-revision stepwise analysis JSON "
                f"before schedule generation for {target_date} {target_time}."
            )
    finally:
        try:
            pre_feedback_schedule_csv.unlink()
        except FileNotFoundError:
            pass


def _mirror_features_log_to_persistent_store(work_output_dir: Path) -> list[Path]:
    """Copy generated case-store CSVs from the run folder into the persistent store."""
    mirrored_paths: list[Path] = []
    for source_path in sorted(work_output_dir.glob(f"{config.PLANT_NAME}_features_log_*.csv")):
        destination_path = config.FEATURES_LOG_DIR / source_path.name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)
        mirrored_paths.append(destination_path)
    return mirrored_paths


def _build_image_map(screenshot_dir: Path) -> dict[str, str]:
    image_map: dict[str, str] = {}
    for layer, description in config.LAYERS.items():
        path = screenshot_dir / f"{layer}.png"
        if path.exists():
            image_map[str(path)] = description
    return image_map


def _read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    return shared_schedule_utils.read_csv_rows(csv_path)


def _row_time_key(row: dict) -> str:
    return shared_schedule_utils.row_time_key(row)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    shared_schedule_utils.write_csv(path, fieldnames, rows)


def _merge_latest_schedule(snapshot_csv: Path, latest_csv: Path) -> tuple[int, int, int]:
    return shared_schedule_utils.merge_latest_schedule(snapshot_csv, latest_csv)


def _freeze_from_datetime(target_date: str, target_time: str) -> dt.datetime:
    return shared_schedule_utils.freeze_from_datetime(target_date, target_time, block_minutes=config.BLOCK_MINUTES)


def _write_current_final_schedule(latest_csv: Path, current_final_csv: Path, target_date: str, target_time: str) -> int:
    return shared_schedule_utils.write_current_final_schedule(
        latest_csv,
        current_final_csv,
        target_date,
        target_time,
        block_minutes=config.BLOCK_MINUTES,
    )


def _current_final_schedule_name(target_date: str) -> str:
    return shared_schedule_utils.current_final_schedule_name(target_date)


def _download_previous_current_final_schedule(
    bucket: str,
    schedule_prefix: str,
    target_date: str,
    current_final_csv: Path,
) -> None:
    return shared_schedule_utils.download_previous_current_final_schedule(
        storage,
        bucket,
        schedule_prefix,
        target_date,
        current_final_csv,
    )


def _build_recent_meter_history_text(
    bucket: str,
    meter_prefix: str,
    target_date: str,
    work_root: Path,
) -> str:
    cache_dir = config.METER_HISTORY_DIR / config.PLANT_NAME / target_date
    cache_path = cache_dir / "recent_meter_history.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("target_date") == target_date and cached.get("days_data"):
                return json.dumps(cached, indent=2, default=str)
        except (OSError, json.JSONDecodeError):
            pass

    recent_dir = work_root / "meter_history"
    if recent_dir.exists():
        shutil.rmtree(recent_dir)
    recent_paths = shared_schedule_utils.download_recent_meter_history_files(
        storage,
        bucket,
        meter_prefix,
        target_date,
        recent_dir,
        days=3,
    )
    if not recent_paths:
        return "No prior 3-day meter history is available yet."

    summary_payload = daily_feedback.build_recent_meter_history_json(
        recent_paths,
        target_date=target_date,
        plant_name=config.PLANT_NAME,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(summary_payload, indent=2, default=str), encoding="utf-8")
    return json.dumps(summary_payload, indent=2, default=str)


def _build_recent_plant_performance_text(
    bucket: str,
    meter_prefix: str,
    target_date: str,
    work_root: Path,
) -> str:
    return shared_performance_utils.build_recent_plant_performance_text(
        storage,
        bucket,
        meter_prefix,
        target_date,
        work_root,
        days=8,
    )


def _blocks_from_time_to_end_of_day(forecast_start_time: dt.datetime) -> int:
    return shared_schedule_utils.blocks_from_time_to_end_of_day(forecast_start_time, block_minutes=config.BLOCK_MINUTES)


def _build_pvlib_text(reference_time: dt.datetime, num_blocks: int) -> str:
    pvlib_dir = config.PVLIB_SUMMARY_DIR / config.PLANT_NAME / reference_time.strftime("%Y-%m-%d")
    pvlib_dir.mkdir(parents=True, exist_ok=True)
    summary_text = shared_pvlib_utils.build_pvlib_block_summary(
        reference_time,
        num_blocks,
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
        timezone=settings.DEFAULT_TIMEZONE,
        tilt_deg=getattr(config, "PLANT_TILT_DEG", None),
        azimuth_deg=int(round(180 + float(getattr(config, "PLANT_ORIENTATION_FROM_SOUTH_DEG", 0.0)))),
        capacity_mw=getattr(config, "PLANT_CAPACITY_MW", None),
        performance_ratio=getattr(config, "PERFORMANCE_RATIO", None),
        block_minutes=config.BLOCK_MINUTES,
    )
    summary_payload = {
        "type": "pvlib_summary",
        "plant_name": config.PLANT_NAME,
        "reference_time": reference_time.strftime("%Y-%m-%d %H:%M"),
        "num_blocks": num_blocks,
        "timezone": settings.DEFAULT_TIMEZONE,
        "summary_text": summary_text,
    }
    (pvlib_dir / f"{reference_time.strftime('%H-%M')}_summary.json").write_text(
        json.dumps(summary_payload, indent=2, default=str),
        encoding="utf-8",
    )
    return summary_text


def _snapshot_metadata(
    selection: CaptureSelection,
    forecast_start: str,
    forecast_end: str,
    pvlib_summary: str,
    plant_performance_summary: str,
    snapshot_csv_key: str,
    latest_csv_key: str,
    current_final_csv_key: str,
    penalty_csv_key: str,
    snapshot_metadata_key: str,
    latest_metadata_key: str,
    generated_rows: int,
    preserved_rows: int,
    current_final_rows: int,
    penalty_rows: int,
) -> dict:
    capture_time = getattr(selection, "capture_time", None)
    if hasattr(capture_time, "strftime"):
        capture_time_text = capture_time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        capture_time_text = str(capture_time or "")

    run_time = getattr(selection, "target_time", "")
    run_time_text = str(run_time).replace(":", "-")

    return {
        "status": "ok",
        "date": selection.target_date,
        "run_time": run_time_text,
        "forecast_start": forecast_start,
        "forecast_end": forecast_end,
        "capture_time": capture_time_text,
        "video_key": selection.video_key,
        "meter_key": selection.meter_key,
        "pvlib_summary": pvlib_summary,
        "plant_performance_summary": plant_performance_summary,
        "snapshot_csv_key": snapshot_csv_key,
        "snapshot_metadata_key": snapshot_metadata_key,
        "latest_csv_key": latest_csv_key,
        "current_final_csv_key": current_final_csv_key,
        "penalty_csv_key": penalty_csv_key,
        "latest_metadata_key": latest_metadata_key,
        "generated_rows": generated_rows,
        "preserved_rows": preserved_rows,
        "current_final_rows": current_final_rows,
        "penalty_rows": penalty_rows,
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
    num_blocks = _blocks_from_time_to_end_of_day(forecast_start_dt)
    generated_root = _prefix_to_local_dir(schedule_prefix, target_date)
    generated_root.mkdir(parents=True, exist_ok=True)

    work_output_dir = _storage_subpath("_scheduler_work", target_date, target_dt.strftime("%H-%M"), "pipeline_output")
    if work_output_dir.exists():
        shutil.rmtree(work_output_dir)
    work_output_dir.mkdir(parents=True, exist_ok=True)
    meter_history_text = _build_recent_meter_history_text(bucket, meter_prefix, target_date, work_output_dir.parent)
    pvlib_text = _build_pvlib_text(forecast_start_dt, num_blocks)
    plant_performance_text = _build_recent_plant_performance_text(bucket, meter_prefix, target_date, work_output_dir.parent)
    _generate_pre_revision_feedback(
        bucket,
        schedule_prefix,
        target_date,
        target_time,
        selection.meter_path,
        work_output_dir.parent,
    )

    run_pipeline.run_prediction_pipeline(
        image_map=image_map,
        video_path=selection.video_path,
        reference_time=forecast_start_dt,
        output_dir=work_output_dir,
        intraday_actuals_path=selection.meter_path,
        meter_history_text=meter_history_text,
        pvlib_text=pvlib_text,
        plant_performance_text=plant_performance_text,
    )
    mirrored_features = _mirror_features_log_to_persistent_store(work_output_dir)
    if mirrored_features:
        print(
            "  [STATE] Mirrored features_log file(s) into the persistent case store: "
            + ", ".join(str(path.resolve()) for path in mirrored_features)
        )

    snapshot_source = work_output_dir / f"{config.PLANT_NAME}_energy_generation_{target_date}.csv"
    if not snapshot_source.exists():
        raise FileNotFoundError(f"Expected schedule output was not produced: {snapshot_source}")

    snapshot_csv = generated_root / f"{target_date}_{target_time.replace(':', '-')}_schedule.csv"
    snapshot_metadata = generated_root / f"{target_date}_{target_time.replace(':', '-')}_metadata.json"
    latest_csv = generated_root / f"{target_date}_latest_schedule.csv"
    current_final_csv = generated_root / _current_final_schedule_name(target_date)
    penalty_csv = generated_root / f"{target_date}_penalty_schedule.csv"
    latest_metadata = generated_root / f"{target_date}_latest_metadata.json"

    shutil.copyfile(snapshot_source, snapshot_csv)
    _download_previous_current_final_schedule(bucket, schedule_prefix, target_date, current_final_csv)
    snapshot_rows, preserved_rows, merged_rows = _merge_latest_schedule(snapshot_source, latest_csv)
    shutil.copyfile(latest_csv, snapshot_csv)
    current_final_rows = _write_current_final_schedule(latest_csv, current_final_csv, target_date, target_time)
    penalty_summary = shared_schedule_utils.write_full_block_schedule_from_llm_schedule(
        current_final_csv,
        penalty_csv,
    )
    penalty_total_blocks = int(penalty_summary.get("total_blocks", 96))

    forecast_start_label = forecast_start_dt.strftime("%Y-%m-%d %H:%M")
    forecast_end_label = forecast_end_dt.strftime("%Y-%m-%d %H:%M")
    try:
        metadata = _snapshot_metadata(
            selection=selection,
            forecast_start=forecast_start_label,
            forecast_end=forecast_end_label,
            snapshot_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_csv.name}",
            latest_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_csv.name}",
            current_final_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}",
            penalty_csv_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{penalty_csv.name}",
            snapshot_metadata_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_metadata.name}",
            latest_metadata_key=f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_metadata.name}",
            pvlib_summary=pvlib_text,
            plant_performance_summary=plant_performance_text,
            generated_rows=merged_rows,
            preserved_rows=preserved_rows,
            current_final_rows=current_final_rows,
            penalty_rows=penalty_total_blocks,
        )
    except Exception as exc:
        print(f"  [WARN] Metadata assembly failed for {config.PLANT_NAME}; using fallback metadata: {exc!r}")
        metadata = {
            "status": "ok",
            "date": target_date,
            "run_time": target_time.replace(":", "-"),
            "forecast_start": forecast_start_label,
            "forecast_end": forecast_end_label,
            "capture_time": getattr(selection.capture_time, "strftime", lambda *_: str(selection.capture_time))("%Y-%m-%d %H:%M:%S"),
            "video_key": selection.video_key,
            "meter_key": selection.meter_key,
            "pvlib_summary": pvlib_text,
            "plant_performance_summary": plant_performance_text,
            "snapshot_csv_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_csv.name}",
            "snapshot_metadata_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_metadata.name}",
            "latest_csv_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_csv.name}",
            "current_final_csv_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}",
            "penalty_csv_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{penalty_csv.name}",
            "latest_metadata_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_metadata.name}",
            "generated_rows": merged_rows,
            "preserved_rows": preserved_rows,
            "current_final_rows": current_final_rows,
            "penalty_rows": penalty_total_blocks,
        }
    snapshot_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_file(bucket, f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}", current_final_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["penalty_csv_key"], penalty_csv, content_type="text/csv")
    legacy_current_final_csv = generated_root / "current_final_schedule.csv"
    if legacy_current_final_csv != current_final_csv:
        shutil.copyfile(current_final_csv, legacy_current_final_csv)
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.push_state_to_s3(bucket=bucket)

    return metadata
