"""Scheduler job runner for the Bhupalpally forecast Lambda."""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import config
from modules import schedule_utils as shared_schedule_utils
from modules.meter import meter_normalizer
from core import settings, storage


@dataclass(frozen=True)
class CaptureSelection:
    target_date: str
    target_time: str
    target_dt: dt.datetime
    capture_time: dt.datetime
    screenshot_dir: Path | None = None
    video_path: Path | None = None
    meter_path: Path | None = None
    screenshot_key_prefix: str = ""
    video_key: str = ""
    meter_key: str = ""
    meter_rows_available: int = 0
    meter_rows_used: int = 0
    weather_summary: str = ""
    context_summary: str = ""
    context_payload: dict | None = None
    enercast_path: Path | None = None


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

def _pick_latest_capture_bundle(
    bucket: str,
    capture_prefix: str,
    meter_prefix: str,
    target_dt: dt.datetime,
) -> CaptureSelection:
    date_str = target_dt.strftime("%Y-%m-%d")

    meter_objects = shared_schedule_utils.list_meter_objects_for_day(storage, bucket, meter_prefix, date_str)
    if not meter_objects:
        print(
            f"  [WARN] No meter file found under metered_data or meter_data for {meter_prefix.rstrip('/')}/{date_str}/; "
            "continuing without intraday actuals."
        )

    selected_meter = None
    if meter_objects:
        selected_meter = shared_schedule_utils.select_preferred_meter_object(meter_objects, config.PLANT_NAME)

    work_root = _storage_subpath("_scheduler_work", date_str, target_dt.strftime("%H-%M"))
    meter_dir = work_root / "meter"
    meter_dir.mkdir(parents=True, exist_ok=True)

    # Clear any stale files from an earlier run for the same cutoff.
    for child in meter_dir.glob("*"):
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
        try:
            clipped_meter_path, meter_rows_available, meter_rows_used = _clip_meter_to_cutoff(
                raw_meter_path,
                meter_dir / f"{Path(selected_meter.key).stem}_upto_{target_dt.strftime('%H-%M')}.csv",
                target_dt,
            )
        except Exception as e:
            if config.is_wind_plant():
                print(f"  [INFO] Wind plant meter data clipping bypassed ({e}); using virtual telemetry.")
                clipped_meter_path = None
                meter_rows_available = 0
                meter_rows_used = 0
            else:
                raise

    return CaptureSelection(
        target_date=date_str,
        target_time=target_dt.strftime("%H:%M"),
        target_dt=target_dt,
        capture_time=target_dt,
        meter_path=clipped_meter_path,
        meter_key=selected_meter.key if selected_meter is not None else "",
        meter_rows_available=meter_rows_available,
        meter_rows_used=meter_rows_used,
    )




def _clip_meter_to_cutoff(source_csv: Path, destination_csv: Path, cutoff_dt: dt.datetime) -> tuple[Path, int, int]:
    """Write a meter CSV trimmed to the revision cutoff."""
    with open(source_csv, "r", newline="", encoding="utf-8", errors="ignore") as handle:
        lines = []
        for line in handle:
            if not lines and not line.strip():
                continue
            lines.append(line)
        reader = csv.DictReader(lines)
        fieldnames = list(reader.fieldnames or [])
        timestamp_column = meter_normalizer.find_timestamp_column(fieldnames)
        rows = list(reader)

    if timestamp_column is None:
        raise ValueError(
            f"Could not locate a timestamp column in {source_csv.name}; "
            "unable to safely trim meter data to the revision cutoff."
        )

    kept_rows = []
    for row in rows:
        normalized = meter_normalizer.normalize_timestamp_str(row.get(timestamp_column))
        if normalized is None:
            continue
        try:
            row_dt = dt.datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
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
    plant_name = (
        (event or {}).get("plant_name")
        or (event or {}).get("plant")
        or os.getenv("PLANT_NAME")
        or getattr(config, "PLANT_NAME", "")
        or getattr(settings, "PLANT_NAME", "BHUPALPALLY")
    )
    config.load_plant_profile(plant_name)
    target_date, target_time, target_dt = _parse_target_datetime(event)
    selection = _pick_latest_capture_bundle(bucket, capture_prefix, meter_prefix, target_dt)

    forecast_start_dt = target_dt
    forecast_end_dt = target_dt + dt.timedelta(hours=settings.FORECAST_HORIZON_HOURS)
    generated_root = _prefix_to_local_dir(schedule_prefix, target_date)
    generated_root.mkdir(parents=True, exist_ok=True)

    work_output_dir = _storage_subpath("_scheduler_work", target_date, target_dt.strftime("%H-%M"), "pipeline_output")
    if work_output_dir.exists():
        shutil.rmtree(work_output_dir)
    work_output_dir.mkdir(parents=True, exist_ok=True)

    is_wind_site = config.is_wind_plant()
    snapshot_source = work_output_dir / f"{config.PLANT_NAME}_energy_generation_{target_date}.csv"
    solar_sched_result = None
    wind_sched_result = None
    if is_wind_site:
        from modules.weather.strategies.intellis_wind import calculate_wind_schedule_96block, WindTurbineProfile
        wind_prof = WindTurbineProfile.from_plant_profile(getattr(config, "PLANT_PROFILE", {}) or config.PLANT_NAME)
        snapshot_block = ((target_dt.hour * 60 + target_dt.minute) // config.BLOCK_MINUTES) + 1
        wind_sched = calculate_wind_schedule_96block(
            config.PLANT_LAT,
            config.PLANT_LON,
            target_date,
            profile=wind_prof,
            scada_actuals=selection.meter_path,
            current_block=snapshot_block,
            enable_slot_selection=True,
            enable_bias_correction=True,
            enable_telemetry_blending=True,
        )

        # -------------------------------------------------------------
        # LLM Strategic Arbiter Integration for Wind Power Plants
        # -------------------------------------------------------------
        wind_sched_result = {"llm_strategy": {}}
        try:
            import numpy as np
            from modules.llm.strategic_arbiter import LLMStrategicArbiter
            arbiter = LLMStrategicArbiter(plant_profile=wind_prof)
            lag_mins = int(getattr(config, "PLANT_PROFILE", {}).get("freeze_lag_minutes", 90) or 90)
            lag_blocks = lag_mins // config.BLOCK_MINUTES
            actionable_block = min(96, snapshot_block + lag_blocks)
            next_12_blocks = [
                b for b in wind_sched["blocks"]
                if actionable_block <= b["block"] < actionable_block + 12
            ]
            wind_indicators = {
                "mean_wind_speed": round(float(np.mean([b.get("wind_speed_hub_m_s", 0.0) for b in next_12_blocks])), 2) if next_12_blocks else 0.0,
                "mean_air_density": round(float(np.mean([b.get("air_density_kg_m3", 1.15) for b in next_12_blocks])), 3) if next_12_blocks else 1.15,
                "temp_c": 26.0,
                "turbulence_risk": "LOW",
            }
            advice = arbiter.get_wind_strategic_guidance(
                target_date_str=target_date,
                target_time_str=target_time,
                wind_indicators=wind_indicators,
                next_12_blocks=next_12_blocks,
            )
            print(f"  [WIND LLM STRATEGY] Regime: {advice.regime} | Risk Quantile: {advice.quantile_bias_factor:.3f} | Agency: {advice.preferred_agency}")
            print(f"                     Reasoning: {advice.reasoning}")
            wind_sched_result["llm_strategy"] = {
                "regime": advice.regime,
                "quantile_bias_factor": advice.quantile_bias_factor,
                "preferred_agency": advice.preferred_agency,
                "reasoning": advice.reasoning,
                "source": advice.source,
            }

            if advice.block_predictions:
                print(f"  [WIND LLM PREDICTIONS] Applying direct LLM predictions to {len(advice.block_predictions)} blocks...")
                for b in wind_sched["blocks"]:
                    b_str = str(b["block"])
                    if b["block"] >= actionable_block and b_str in advice.block_predictions:
                        pred_mw = float(advice.block_predictions[b_str])
                        pred_mw = max(0.0, min(wind_prof.rated_capacity_mw, pred_mw))
                        b["schedule_mw"] = round(pred_mw, 2)
                        b["intellis_mw"] = round(pred_mw, 2)
            elif advice.quantile_bias_factor != 1.0:
                print(f"  [WIND LLM BIAS] Scaling actionable blocks by quantile_bias_factor: {advice.quantile_bias_factor:.3f}")
                for b in wind_sched["blocks"]:
                    if b["block"] >= actionable_block:
                        scaled_mw = float(b["schedule_mw"]) * advice.quantile_bias_factor
                        scaled_mw = max(0.0, min(wind_prof.rated_capacity_mw, scaled_mw))
                        b["schedule_mw"] = round(scaled_mw, 2)
                        b["intellis_mw"] = round(scaled_mw, 2)
        except Exception as wind_llm_err:
            print(f"  [WARN] Wind LLM Strategic Arbiter invocation skipped: {wind_llm_err}")

        fieldnames = [
            "Block",
            "Time Interval (15 minute interval)",
            "wind_speed_hub_m_s",
            "air_density_kg_m3",
            "intellis_gti",
            "intellis_mw",
            "schedule_mw",
        ]
        rows_to_write = []
        for b in wind_sched["blocks"]:
            rows_to_write.append({
                "Block": b["block"],
                "Time Interval (15 minute interval)": b["time_interval"],
                "wind_speed_hub_m_s": b.get("wind_speed_hub_m_s"),
                "air_density_kg_m3": b.get("air_density_kg_m3"),
                "intellis_gti": b["intellis_gti"],
                "intellis_mw": b["intellis_mw"],
                "schedule_mw": b["schedule_mw"],
            })
        shared_schedule_utils.write_csv(snapshot_source, fieldnames, rows_to_write)
    else:
        from modules.solar_schedule.solar_scheduler import SolarScheduleEngine, load_plant_profile
        prof = load_plant_profile(config.PLANT_NAME)
        ai_engine = SolarScheduleEngine(plant_profile=prof)
        solar_sched_result = ai_engine.generate_revision_schedule_csv(
            target_date_str=target_date,
            target_time_str=target_time,
            output_csv_path=snapshot_source,
            live_meter_csv_path=selection.meter_path,
            enercast_intraday_csv_path=selection.enercast_path,
        )

    if not snapshot_source.exists():
        raise FileNotFoundError(f"Expected schedule output was not produced: {snapshot_source}")

    # --- Active Plant Control Windows Guardrail (DynamoDB) ---
    control_summary: dict[str, Any] = {}
    try:
        from modules.control_windows import PlantControlWindowEngine
        cw_engine = PlantControlWindowEngine()
        freeze_from = _freeze_from_datetime(target_date, target_time)
        freeze_end_block = ((freeze_from.hour * 60 + freeze_from.minute) // config.BLOCK_MINUTES)

        ss_fields, ss_rows = _read_csv_rows(snapshot_source)
        if ss_rows:
            raw_sched_mw = []
            for r in ss_rows:
                v = r.get("schedule_mw") or r.get("intellis_mw") or 0.0
                try:
                    raw_sched_mw.append(float(v))
                except Exception:
                    raw_sched_mw.append(0.0)

            ac_cap = float(getattr(config, "PLANT_CAPACITY_MW", 0.0) or (prof.ac_capacity_mw if "prof" in locals() else 37.0))
            dc_cap = float(getattr(config, "PLANT_DC_CAPACITY_MW", 0.0) or (getattr(prof, "dc_capacity_mw", ac_cap) if "prof" in locals() else ac_cap))

            if len(raw_sched_mw) == 96:
                controlled_mw, block_audit, control_summary = cw_engine.apply_to_blocks(
                    raw_forecast_mw_96=raw_sched_mw,
                    site_id=config.PLANT_NAME,
                    target_date_str=target_date,
                    site_ac_capacity_mw=ac_cap,
                    site_dc_capacity_mw=dc_cap,
                    freeze_end_block=freeze_end_block,
                )

                new_fields = list(ss_fields)
                audit_cols = [
                    "raw_forecast_mw",
                    "block_control_status",
                    "block_control_mode",
                    "block_control_type",
                    "effective_control_capacity_ac_mw",
                    "control_applied",
                ]
                for col in audit_cols:
                    if col not in new_fields:
                        new_fields.append(col)

                for i, r in enumerate(ss_rows):
                    audit_info = block_audit[i]
                    r["raw_forecast_mw"] = audit_info["raw_forecast_mw"]
                    r["block_control_status"] = audit_info["block_control_status"]
                    r["block_control_mode"] = audit_info["block_control_mode"]
                    r["block_control_type"] = audit_info["block_control_type"]
                    r["effective_control_capacity_ac_mw"] = audit_info["effective_control_capacity_ac_mw"]
                    r["control_applied"] = audit_info["control_applied"]
                    r["schedule_mw"] = round(float(controlled_mw[i]), 2)
                    if "intellis_mw" in r and not audit_info.get("frozen", False):
                        r["intellis_mw"] = round(float(controlled_mw[i]), 2)

                _write_csv(snapshot_source, new_fields, ss_rows)
                print(f"  [CONTROL_WINDOWS] Applied control windows: {control_summary.get('windows_applied', 0)} active window(s) applied for {config.PLANT_NAME}.")
    except Exception as cw_err:
        print(f"  [WARN] PlantControlWindowEngine execution failed; continuing without control windows: {cw_err}")

    snapshot_block = ((target_dt.hour * 60 + target_dt.minute) // config.BLOCK_MINUTES) + 1
    snapshot_stamp = f"{target_date.replace('-', '')}t{target_dt.strftime('%H%M%S')}"
    snapshot_csv = generated_root / f"schedule_from_{snapshot_block}_{snapshot_stamp}.csv"
    snapshot_metadata = generated_root / f"{snapshot_csv.name}.meta.json"
    latest_csv = generated_root / f"{target_date}_latest_schedule.csv"
    current_final_csv = generated_root / _current_final_schedule_name(target_date)
    penalty_csv = generated_root / _penalty_schedule_name(target_date)
    latest_metadata = generated_root / f"{target_date}_latest_metadata.json"

    shutil.copyfile(snapshot_source, snapshot_csv)
    force_all = event and (str(event.get("force", "")).lower() in ("1", "true", "yes") or str(event.get("clean_seed", "")).lower() in ("1", "true", "yes"))
    if force_all or (target_time in ("00:00", "01:15") and event and str(event.get("force", "")).lower() in ("1", "true", "yes")):
        if current_final_csv.exists():
            current_final_csv.unlink(missing_ok=True)
        if latest_csv.exists():
            latest_csv.unlink(missing_ok=True)
    else:
        _download_previous_current_final_schedule(bucket, schedule_prefix, target_date, current_final_csv)
    snapshot_rows, preserved_rows, merged_rows = _merge_latest_schedule(snapshot_source, latest_csv)
    shutil.copyfile(latest_csv, snapshot_csv)
    current_final_rows = _write_current_final_schedule(latest_csv, current_final_csv, target_date, target_time)
    if int((control_summary or {}).get("windows_applied") or 0) > 0:
        # Publish already-applied plant-control output into current-final so
        # downstream IP/UI and penalty outputs match the effective schedule.
        shutil.copyfile(latest_csv, current_final_csv)
        _, current_final_rows_data = _read_csv_rows(current_final_csv)
        current_final_rows = len(current_final_rows_data)
    penalty_summary = shared_schedule_utils.write_full_block_schedule_from_llm_schedule(
        current_final_csv,
        penalty_csv,
        fallback_csv_path=latest_csv,
        target_date=target_date,
    )

    forecast_start_label = forecast_start_dt.strftime("%Y-%m-%d %H:%M")
    forecast_end_label = forecast_end_dt.strftime("%Y-%m-%d %H:%M")
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
        generated_rows=merged_rows,
        preserved_rows=preserved_rows,
        current_final_rows=current_final_rows,
        penalty_rows=penalty_summary["total_blocks"],
    )
    metadata["snapshot_rows"] = snapshot_rows
    metadata["plant_type"] = getattr(config, "PLANT_TYPE", "")
    metadata["is_wind_plant"] = bool(is_wind_site)
    metadata["control_windows"] = control_summary
    if is_wind_site:
        metadata["weather_summary_type"] = "wind_ensemble"
        if isinstance(wind_sched_result, dict) and "llm_strategy" in wind_sched_result:
            metadata["llm_strategy"] = wind_sched_result["llm_strategy"]
    elif isinstance(solar_sched_result, dict) and "llm_strategy" in solar_sched_result:
        metadata["llm_strategy"] = solar_sched_result["llm_strategy"]
    snapshot_metadata.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_file(bucket, f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_csv.name}", current_final_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["penalty_csv_key"], penalty_csv, content_type="text/csv")

    # Specialized revision penalty replica exclusively for LGEPL
    if str(getattr(config, "PLANT_NAME", "")).strip().upper() == "LGEPL":
        try:
            # 1. Resolve block number at revision run
            run_block = (event or {}).get("block") or (event or {}).get("block_no")
            if run_block is None:
                run_block = (target_dt.hour * 60 + target_dt.minute) // 15
                if run_block == 0:
                    run_block = 96

            # 2. Resolve revision tag (R1 to R8)
            rev_tag = (event or {}).get("rev") or (event or {}).get("revision_no") or (event or {}).get("revision")
            if not rev_tag:
                lgepl_rev_map = {
                    "06:30": "R1", "06:45": "R1",
                    "08:00": "R2", "08:15": "R2",
                    "09:30": "R3", "09:45": "R3",
                    "11:00": "R4", "11:15": "R4",
                    "12:30": "R5", "12:45": "R5",
                    "14:00": "R6", "14:15": "R6",
                    "15:30": "R7", "15:45": "R7",
                }
                t_clean = target_time.strip()
                rev_tag = lgepl_rev_map.get(t_clean)
                if not rev_tag:
                    t_mins = target_dt.hour * 60 + target_dt.minute
                    if t_mins <= 420:
                        rev_tag = "R1"
                    elif t_mins <= 510:
                        rev_tag = "R2"
                    elif t_mins <= 600:
                        rev_tag = "R3"
                    elif t_mins <= 690:
                        rev_tag = "R4"
                    elif t_mins <= 780:
                        rev_tag = "R5"
                    elif t_mins <= 870:
                        rev_tag = "R6"
                    else:
                        rev_tag = "R7"

            else:
                rev_tag = str(rev_tag).strip().upper()
                if not rev_tag.startswith("R"):
                    rev_tag = f"R{rev_tag}"

            # Format: LGEPL_DD-MM-YYYY_<BLOCK>_ID_<REV>.csv
            dd_mm_yyyy = target_dt.strftime("%d-%m-%Y")
            lgepl_replica_filename = f"LGEPL_{dd_mm_yyyy}_{run_block}_ID_{rev_tag}.csv"
            lgepl_replica_csv = generated_root / lgepl_replica_filename
            shutil.copyfile(penalty_csv, lgepl_replica_csv)

            lgepl_replica_key = f"{schedule_prefix.rstrip('/')}/{target_date}/{lgepl_replica_csv.name}"
            storage.upload_file(bucket, lgepl_replica_key, lgepl_replica_csv, content_type="text/csv")
            metadata["lgepl_revision_penalty_csv_key"] = lgepl_replica_key
            print(f"  [LGEPL] Created & uploaded revision penalty replica: {lgepl_replica_key}")
        except Exception as lgepl_err:
            print(f"  [WARN] Failed to write LGEPL revision penalty replica: {lgepl_err}")

    legacy_current_final_csv = generated_root / "current_final_schedule.csv"
    if legacy_current_final_csv != current_final_csv:
        shutil.copyfile(current_final_csv, legacy_current_final_csv)
    legacy_penalty_csv = generated_root / f"{target_date}_penalty_schedule.csv"
    if legacy_penalty_csv != penalty_csv:
        shutil.copyfile(penalty_csv, legacy_penalty_csv)
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)

    return json.loads(json.dumps(metadata, default=str))

