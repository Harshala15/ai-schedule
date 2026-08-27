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
import run_pipeline
from modules.feedback import daily_feedback
from modules.storage import prediction_store
from modules.storage import state_sync
from modules import plant_performance_utils as shared_performance_utils
from modules import pvlib_utils as shared_pvlib_utils
from modules import schedule_utils as shared_schedule_utils
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
    return shared_schedule_utils.parse_target_datetime(event)


def _storage_subpath(*parts: str) -> Path:
    return shared_schedule_utils.storage_subpath(*parts)


def _prefix_to_local_dir(prefix: str, *parts: str) -> Path:
    return shared_schedule_utils.prefix_to_local_dir(prefix, *parts)


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
    weather_artifact = _store_ecmwf_weather_report(date_str, target_dt.strftime("%H-%M"), weather_report)
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
        # Keep the raw ECMWF payload on the capture record so metadata can point to it.
        context_summary=context_summary,
        context_payload=context_payload,
    )


def _load_prediction_context_payload() -> dict:
    daily_feedback.ensure_prediction_context_exists()
    try:
        return json.loads(config.PREDICTION_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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


def _store_ecmwf_weather_report(target_date: str, target_time: str, weather_report: dict) -> Path:
    weather_dir = config.ECMWF_WEATHER_DIR / config.PLANT_NAME / target_date
    weather_dir.mkdir(parents=True, exist_ok=True)
    weather_path = weather_dir / f"{target_time}_ecmwf_weather.json"
    weather_path.write_text(json.dumps(weather_report, indent=2, default=str), encoding="utf-8")
    return weather_path


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


def _read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    with open(csv_path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


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


def _penalty_schedule_name(target_date: str) -> str:
    return shared_schedule_utils.penalty_schedule_name(target_date)


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
        return
    except Exception:
        legacy_key = f"{schedule_prefix.rstrip('/')}/{target_date}/{target_date}_current_final_schedule.csv"
        try:
            storage.download_file(bucket, legacy_key, current_final_csv)
        except Exception:
            return


def _snapshot_metadata(
    selection: CaptureSelection,
    forecast_start: str,
    forecast_end: str,
    ecmwf_weather_key: str,
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
        "ecmwf_weather_key": ecmwf_weather_key,
        "pvlib_summary": pvlib_summary,
        "plant_performance_summary": plant_performance_summary,
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
    meter_history_text = _build_recent_meter_history_text(bucket, meter_prefix, target_date, work_output_dir.parent)
    pvlib_text = _build_pvlib_text(forecast_start_dt, settings.FORECAST_BLOCKS)
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
        image_map={},
        video_path=selection.video_path,
        reference_time=forecast_start_dt,
        num_blocks=settings.FORECAST_BLOCKS,
        output_dir=work_output_dir,
        intraday_actuals_path=selection.meter_path,
        weather_text=selection.weather_summary,
        context_text=selection.context_summary,
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
    penalty_csv = generated_root / _penalty_schedule_name(target_date)
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

    forecast_start_label = forecast_start_dt.strftime("%Y-%m-%d %H:%M")
    forecast_end_label = forecast_end_dt.strftime("%Y-%m-%d %H:%M")
    metadata = _snapshot_metadata(
        selection=selection,
        forecast_start=forecast_start_label,
        forecast_end=forecast_end_label,
        ecmwf_weather_key=f"state/vedanjay/{config.PLANT_NAME}/ecmwf_weather/{target_date}/{target_time.replace(':', '-')}_ecmwf_weather.json",
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
        penalty_rows=penalty_summary["total_blocks"],
    )
    metadata["snapshot_rows"] = snapshot_rows
    snapshot_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_file(bucket, f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}", current_final_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["penalty_csv_key"], penalty_csv, content_type="text/csv")
    legacy_current_final_csv = generated_root / "current_final_schedule.csv"
    if legacy_current_final_csv != current_final_csv:
        shutil.copyfile(current_final_csv, legacy_current_final_csv)
    legacy_penalty_csv = generated_root / f"{target_date}_penalty_schedule.csv"
    if legacy_penalty_csv != penalty_csv:
        shutil.copyfile(penalty_csv, legacy_penalty_csv)
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)

    if settings.ENABLE_S3_STATE_SYNC:
        state_sync.push_state_to_s3(bucket=bucket)

    return metadata
