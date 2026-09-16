"""Shared scheduler helpers used by plant-specific Lambda wrappers."""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

import config

stabilize_curve = None
validate_schedule_curve = None


from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _capture_times_for_site() -> list[str]:
    site = (getattr(config, "PLANT_NAME", "") or "").strip().upper()
    if site in {"BHUPALPALLY", "KASIPET", "KOTHAGUDEM", "MANDAMARRI", "BALAKWADA", "ANDAD", "SAWDA", "CME", "ANJANGAON", "ANJANGOAN", "BAMKHAL", "GUGARIYAKHEDI", "NANDGAON", "OSEPL", "GSNP", "GSPPL"}:
        return ["06:00", "06:45", "08:15", "09:45", "11:15", "12:45", "14:15", "15:45"]
    return list(getattr(config, "CAPTURE_TIMES", []) or [])


def _nearest_configured_capture_time(now: dt.datetime, max_drift_minutes: int = 10) -> str:
    """Snap automatic EventBridge runs to configured revision times."""
    capture_times = _capture_times_for_site()
    now_minutes = now.hour * 60 + now.minute
    best_time = now.strftime("%H:%M")
    best_delta = max_drift_minutes + 1
    for item in capture_times:
        try:
            hour, minute = [int(part) for part in str(item).split(":")[:2]]
        except Exception:
            continue
        delta = abs((hour * 60 + minute) - now_minutes)
        if delta < best_delta:
            best_delta = delta
            best_time = f"{hour:02d}:{minute:02d}"
    return best_time if best_delta <= max_drift_minutes else now.strftime("%H:%M")


def parse_target_datetime(event: dict | None) -> tuple[str, str, dt.datetime]:
    now = dt.datetime.now(IST)
    event = event or {}
    target_date = event.get("target_date") or now.strftime("%Y-%m-%d")
    target_time = event.get("target_time") or _nearest_configured_capture_time(now)
    target_dt = dt.datetime.strptime(f"{target_date} {target_time}", "%Y-%m-%d %H:%M")
    return target_date, target_time, target_dt


def storage_subpath(*parts: str) -> Path:
    return config.STORAGE_ROOT.joinpath(*parts)


def prefix_to_local_dir(prefix: str, *parts: str) -> Path:
    cleaned = [part for part in prefix.strip("/ ").split("/") if part]
    return config.STORAGE_ROOT.joinpath(*cleaned, *parts)


def read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict]]:
    with open(csv_path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def row_time_key(row: dict) -> str:
    if row.get("Time"):
        return row["Time"]
    interval = row.get("Time Interval (15 minute interval)", "")
    if " - " in interval:
        return interval.split(" - ", 1)[0]
    return ""


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _current_final_fieldnames(existing_fieldnames: list[str]) -> list[str]:
    """Return the frozen current-final header order."""
    preferred = [
        "Block",
        "Time Interval (15 minute interval)",
        "intellis_gti",
        "intellis_mw",
        "schedule_mw",
    ]
    ordered = [column for column in preferred if column in existing_fieldnames or column in preferred]
    for column in existing_fieldnames:
        if column not in ordered and column not in (
            "LLM Reasoning",
            "Step 1 Meter Base Forecast MW",
            "Step 2 Weather Adjustment MW",
            "Step 2 Weather + Video Adjusted MW",
            "Step 3 Plant Performance MW",
            "Step 4 Revision Feedback MW",
            "Step 4 Revision Feedback Adjusted MW",
            "LLM Schedule (MW)",
            "Final Validated MW",
        ):
            ordered.append(column)
    return ordered


def _meter_filename_hints(plant_name: str | None = None) -> list[str]:
    plant = (plant_name or config.PLANT_NAME or "").strip().upper()
    hints = {
        "BHUPALPALLY": ["bhupalpally"],
        "KASIPET": ["kasipet"],
        "KOTHAGUDEM": ["kothagudem"],
        "MANDAMARRI": ["mandamarri"],
        "SIRMOUR": ["sirmour", "solar_inv", "solarinv"],
        "BAMKHAL": ["bamkhal"],
        "BALAKWADA": ["balakwada"],
        "ANDAD": ["andad"],
        "SAWDA": ["sawda"],
        "ANJANGAON": ["anjangaon", "anjangoan"],
        "ANJANGOAN": ["anjangaon", "anjangoan"],
        "GUGARIYAKHEDI": ["gugariyakhedi"],
        "NANDGAON": ["nandgaon"],
        "CME": ["cme"],
        "OSEPL": ["osepl"],
        "GSNP": ["gsnp", "gsppl"],
        "GSPPL": ["gsnp", "gsppl"],
    }
    return hints.get(plant, [plant.lower()] if plant else [])


def _meter_object_sort_key(obj, plant_name: str | None = None) -> tuple[int, dt.datetime, str]:
    name = Path(getattr(obj, "key", "")).name.lower()
    score = 0
    for hint in _meter_filename_hints(plant_name):
        if hint and hint.lower() in name:
            score += 100
    if "solar_inv" in name or "solarinv" in name:
        score += 50
    if re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})", name):
        score += 10
    modified = getattr(obj, "last_modified", None) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return score, modified, getattr(obj, "key", "")


def select_preferred_meter_object(meter_objects: list, plant_name: str | None = None):
    if not meter_objects:
        return None
    return sorted(meter_objects, key=lambda obj: _meter_object_sort_key(obj, plant_name))[-1]


def meter_day_prefixes(meter_prefix: str, date_str: str) -> list[str]:
    base = meter_prefix.rstrip("/")
    return [
        f"{base}/{date_str}/metered_data",
        f"{base}/{date_str}/meter_data",
    ]


def list_meter_objects_for_day(storage_module, bucket: str, meter_prefix: str, date_str: str) -> list:
    for day_prefix in meter_day_prefixes(meter_prefix, date_str):
        objects = storage_module.list_objects(bucket, day_prefix)
        if objects:
            return objects
    return []

def merge_latest_schedule(snapshot_csv: Path, latest_csv: Path) -> tuple[int, int, int]:
    snapshot_fields, snapshot_rows = read_csv_rows(snapshot_csv)
    if not snapshot_rows:
        raise ValueError(f"No schedule rows were produced in {snapshot_csv}")

    if latest_csv.exists():
        latest_fields, latest_rows = read_csv_rows(latest_csv)
    else:
        latest_fields, latest_rows = snapshot_fields, []

    fieldnames = snapshot_fields or latest_fields
    if not fieldnames:
        raise ValueError("Schedule CSV did not contain any headers.")

    merged_by_time: dict[str, dict] = {}
    for row in latest_rows:
        key = row_time_key(row)
        if key:
            merged_by_time[key] = dict(row)

    snapshot_times: set[str] = set()
    for row in snapshot_rows:
        key = row_time_key(row)
        if not key:
            continue
        snapshot_times.add(key)
        merged_by_time[key] = dict(row)

    preserved_rows = sum(1 for row in latest_rows if row_time_key(row) and row_time_key(row) not in snapshot_times)

    def _sort_key(row: dict) -> int:
        raw_b = str(row.get("Block", "") or row.get("block", "")).strip()
        try:
            return int(raw_b)
        except (ValueError, TypeError):
            pass
        key = row_time_key(row)
        if len(key) == 5 and ":" in key:
            parts = key.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        return 999999

    merged_rows = sorted(merged_by_time.values(), key=_sort_key)
    write_csv(latest_csv, fieldnames, merged_rows)
    return len(snapshot_rows), preserved_rows, len(merged_rows)


def freeze_from_datetime(target_date: str, target_time: str, block_minutes: int | None = None) -> dt.datetime:
    freeze_lag_minutes_by_plant = {
        "SIRMOUR": 90,
        "ANJANGOAN": 90,
        "ANJANGAON": 90,
        "ANDAD": 90,
        "BALAKWADA": 90,
        "BAMKHAL": 90,
        "CHANDAWASA": 90,
        "CHANDWASA": 90,
        "GSNP": 90,
        "GUGARIYAKHEDI": 90,
        "NANDGAON": 90,
        "SAWDA": 90,
        "BHUPALPALLY": 45,
        "KASIPET": 45,
        "KOTHAGUDEM": 45,
        "MANDAMARRI": 45,
        "CME": 45,
        "OSEPL": 45,
        "ZTRIC": 45,
    }
    freeze_lag_minutes = freeze_lag_minutes_by_plant.get(config.PLANT_NAME.upper(), 45)
    return freeze_from_datetime_with_lag(
        target_date,
        target_time,
        block_minutes=block_minutes,
        freeze_lag_minutes=freeze_lag_minutes,
    )


def freeze_from_datetime_with_lag(
    target_date: str,
    target_time: str,
    *,
    block_minutes: int | None = None,
    freeze_lag_minutes: int = 45,
) -> dt.datetime:
    freeze_from = dt.datetime.strptime(f"{target_date} {target_time}", "%Y-%m-%d %H:%M")
    freeze_from += dt.timedelta(minutes=max(0, int(freeze_lag_minutes)))
    block_minutes = block_minutes or config.BLOCK_MINUTES
    remainder = freeze_from.minute % block_minutes
    if remainder or freeze_from.second or freeze_from.microsecond:
        freeze_from += dt.timedelta(minutes=block_minutes - remainder)
    return freeze_from.replace(second=0, microsecond=0)


def write_current_final_schedule(
    latest_csv: Path,
    current_final_csv: Path,
    target_date: str,
    target_time: str,
    block_minutes: int | None = None,
    freeze_lag_minutes: int | None = None,
) -> int:
    latest_fields, latest_rows = read_csv_rows(latest_csv)
    previous_exists = current_final_csv.exists()
    previous_fields, previous_rows = read_csv_rows(current_final_csv) if previous_exists else ([], [])

    fieldnames = latest_fields or previous_fields
    if not fieldnames:
        raise ValueError("Schedule CSV did not contain any headers.")

    if freeze_lag_minutes is None:
        freeze_lag_minutes = {
        "SIRMOUR": 90,
        "ANJANGOAN": 90,
        "ANJANGAON": 90,
        "ANDAD": 90,
        "BALAKWADA": 90,
        "BAMKHAL": 90,
        "CHANDAWASA": 90,
        "CHANDWASA": 90,
        "GSNP": 90,
        "GUGARIYAKHEDI": 90,
        "NANDGAON": 90,
        "SAWDA": 90,
        "BHUPALPALLY": 45,
        "KASIPET": 45,
        "KOTHAGUDEM": 45,
        "MANDAMARRI": 45,
        "CME": 45,
        "OSEPL": 45,
        "ZTRIC": 45,
    }.get(config.PLANT_NAME.upper(), 45)
    freeze_from = freeze_from_datetime_with_lag(
        target_date,
        target_time,
        block_minutes=block_minutes,
        freeze_lag_minutes=freeze_lag_minutes,
    )

    def _row_dt(row: dict) -> dt.datetime | None:
        key = row_time_key(row)
        if not key:
            raw_b = str(row.get("Block", "") or row.get("block", "")).strip()
            try:
                b = int(raw_b)
                end_min = b * 15
                s_hr, s_min = divmod(end_min - 15, 60)
                key = f"{s_hr:02d}:{s_min:02d}"
            except Exception:
                return None
        if len(key) == 5 and ":" in key:
            key = f"{target_date} {key}"
        try:
            return dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    merged_by_time: dict[str, dict] = {}

    # Normal path: keep the previously frozen rows from the existing
    # current-final file and only replace blocks at/after the current
    # freeze point with the new latest forecast.
    #
    # Fallback path: if the previous current-final file is missing (for
    # example after a cold start or an S3 download problem), seed the
    # frozen portion from the cumulative latest file instead of losing
    # the earlier values entirely.
    past_source_rows = previous_rows if previous_rows else latest_rows
    if not previous_exists and latest_rows:
        print(
            f"[WARN] Previous current-final schedule was not available at {current_final_csv}; "
            "seeding frozen rows from the cumulative latest schedule."
        )

    for row in past_source_rows:
        row_dt = _row_dt(row)
        key = row_time_key(row)
        if key and row_dt is not None and row_dt < freeze_from:
            merged_by_time[key] = dict(row)

    for row in latest_rows:
        row_dt = _row_dt(row)
        key = row_time_key(row)
        if key and row_dt is not None and row_dt >= freeze_from:
            merged_by_time[key] = dict(row)

    def _sort_key(row: dict) -> int:
        raw_b = str(row.get("Block", "") or row.get("block", "")).strip()
        try:
            return int(raw_b)
        except (ValueError, TypeError):
            pass
        dt_val = _row_dt(row)
        if dt_val is not None:
            return dt_val.hour * 60 + dt_val.minute
        return 999999

    frozen_rows = sorted(merged_by_time.values(), key=_sort_key)
    current_final_fieldnames = [
        "Block",
        "Time Interval (15 minute interval)",
        "intellis_gti",
        "intellis_mw",
        "schedule_mw",
    ]

    for row in frozen_rows:
        if "intellis_mw" not in row or not str(row.get("intellis_mw", "")).strip():
            row["intellis_mw"] = str(
                row.get("schedule_mw")
                or row.get("Step 2 Weather Adjustment MW")
                or row.get("Schedule MW")
                or row.get("Final Validated MW")
                or row.get("step2_mw")
                or "0.0"
            )
        row["schedule_mw"] = row["intellis_mw"]
        if "intellis_gti" not in row or not str(row.get("intellis_gti", "")).strip():
            row["intellis_gti"] = "0.0"

    def _row_block_number(row: dict) -> int | None:
        raw = row.get("Block", "")
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None

    def _is_night_time(row: dict) -> bool:
        key = row_time_key(row)
        if not key:
            return False
        try:
            row_dt = dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        return row_dt.time() >= dt.time(19, 0)

    is_wind = getattr(config, "is_wind_plant", lambda: False)()
    for row in frozen_rows:
        block_number = _row_block_number(row)
        should_zero = not is_wind and ((block_number is not None and (block_number < 24 or block_number > 76)) or _is_night_time(row))
        if should_zero:
            row["intellis_mw"] = "0.0"
            row["schedule_mw"] = "0.0"
            row["intellis_gti"] = "0.0"

    write_csv(current_final_csv, current_final_fieldnames, frozen_rows)
    return len(frozen_rows)


def current_final_schedule_name(target_date: str) -> str:
    return f"{config.PLANT_NAME}_{target_date}_current_final_schedule.csv"


def penalty_schedule_name(target_date: str) -> str:
    return f"{config.PLANT_NAME}_{target_date}_penalty_schedule.csv"


def download_previous_latest_schedule(
    storage_module,
    bucket: str,
    schedule_prefix: str,
    target_date: str,
    target_time: str,
    latest_csv: Path,
) -> None:
    day_prefix = f"{schedule_prefix.rstrip('/')}/{target_date}"
    schedule_objects = storage_module.list_objects(bucket, day_prefix)
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
        storage_module.download_file(bucket, selected_key, latest_csv)
    except Exception:
        return


def download_previous_current_final_schedule(
    storage_module,
    bucket: str,
    schedule_prefix: str,
    target_date: str,
    current_final_csv: Path,
) -> None:
    current_final_key = f"{schedule_prefix.rstrip('/')}/{target_date}/{current_final_schedule_name(target_date)}"
    try:
        storage_module.download_file(bucket, current_final_key, current_final_csv)
        return
    except Exception:
        legacy_key = f"{schedule_prefix.rstrip('/')}/{target_date}/{target_date}_current_final_schedule.csv"
        try:
            storage_module.download_file(bucket, legacy_key, current_final_csv)
        except Exception:
            return


def blocks_from_time_to_end_of_day(
    forecast_start_time: dt.datetime,
    block_minutes: int | None = None,
) -> int:
    block_minutes = block_minutes or config.BLOCK_MINUTES
    end_of_day = forecast_start_time.replace(hour=18, minute=45, second=0, microsecond=0)
    first_block = forecast_start_time if forecast_start_time.minute % block_minutes == 0 and forecast_start_time.second == 0 else None
    if first_block is None:
        minute_offset = block_minutes - (forecast_start_time.minute % block_minutes)
        first_block = (forecast_start_time + dt.timedelta(minutes=minute_offset)).replace(second=0, microsecond=0)
    if first_block > end_of_day:
        return 0
    minutes_remaining = (end_of_day - first_block).total_seconds() / 60.0
    return int(minutes_remaining // block_minutes) + 1


def write_full_block_schedule_from_llm_schedule(
    input_csv_path: Path,
    output_csv_path: Path,
    *,
    fallback_csv_path: Path | None = None,
    total_blocks: int = 96,
    target_date: str | dt.date | None = None,
    weather_fusion_map: dict | None = None,
    meter_csv_path: Path | str | None = None,
) -> dict:
    """Write full 96-block penalty schedule directly from Intellis GTI and MW.

    Derives intellis_gti and intellis_mw directly from the current final schedule
    and evaluates against meter actuals without PVLib physics or curve stability heuristics.
    """
    schedule_by_block: dict[int, dict] = {}

    target_date_str = str(target_date) if target_date else None
    if not target_date_str:
        for p_cand in (input_csv_path, fallback_csv_path):
            if p_cand:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", str(p_cand.name))
                if m:
                    target_date_str = m.group(1)
                    break
    if not target_date_str:
        target_date_str = dt.datetime.now(IST).strftime("%Y-%m-%d")

    # Read fallback latest schedule first
    if fallback_csv_path and fallback_csv_path.exists():
        with open(fallback_csv_path, "r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                raw_b = str(row.get("Block", "") or row.get("block", "")).strip()
                try:
                    b = int(raw_b)
                    mw = float(
                        row.get("intellis_mw")
                        or row.get("Step 2 Weather Adjustment MW")
                        or row.get("Schedule MW")
                        or row.get("Final Validated MW")
                        or 0.0
                    )
                    gti = float(row.get("intellis_gti", 0.0) or 0.0)
                    schedule_by_block[b] = {
                        "intellis_gti": gti,
                        "intellis_mw": mw,
                        "time_interval": row.get("Time Interval (15 minute interval)", ""),
                    }
                except (ValueError, TypeError):
                    continue

    # Overlay current-final schedule
    if input_csv_path.exists():
        with open(input_csv_path, "r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                raw_b = str(row.get("Block", "") or row.get("block", "")).strip()
                try:
                    b = int(raw_b)
                    mw = float(
                        row.get("intellis_mw")
                        or row.get("Step 2 Weather Adjustment MW")
                        or row.get("Schedule MW")
                        or row.get("Final Validated MW")
                        or 0.0
                    )
                    gti = float(row.get("intellis_gti", 0.0) or 0.0)
                    schedule_by_block[b] = {
                        "intellis_gti": gti,
                        "intellis_mw": mw,
                        "time_interval": row.get("Time Interval (15 minute interval)", ""),
                    }
                except (ValueError, TypeError):
                    continue

    # Plant regulatory parameters
    cap_mw = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
    ppa = float(getattr(config, "PPA_RATE_INR_PER_KWH", 6.97))
    reg = str(getattr(config, "PENALTY_REGULATION", "Madhya Pradesh")).lower()
    band_pct = 0.15 if any(s in reg for s in ["maharashtra", "merc", "karnataka", "telangana"]) else 0.10
    tol_mw = round(cap_mw * band_pct, 3)

    # Attempt to load meter actuals if present
    meter_by_block: dict[int, float] = {}
    if meter_csv_path and Path(meter_csv_path).exists():
        try:
            from modules.meter.meter_normalizer import load_and_normalize_meter_csv
            from modules.weather.intellis_ensemble_gti_ai import load_plant_profile
            plant_name = getattr(config, "PLANT_NAME", "SIRMOUR")
            profile = load_plant_profile(plant_name)
            norm_df = load_and_normalize_meter_csv(Path(meter_csv_path), meter_config=profile.meter_data)
            if not norm_df.empty:
                for _, r in norm_df.iterrows():
                    b_num = int(r["block"])
                    if 1 <= b_num <= 96:
                        meter_by_block[b_num] = max(0.0, float(r["metered_mw"]))
        except Exception as e:
            print(f"[WARN] Failed to load meter actuals from meter_csv_path: {e}")

    if not meter_by_block:
        try:
            from modules.weather.intellis_ensemble_gti_ai import IntellisEnsembleGTIAI, load_plant_profile
            plant_profile = load_plant_profile(getattr(config, "PLANT_NAME", "SIRMOUR"))
            ai_engine = IntellisEnsembleGTIAI(plant_profile=plant_profile)
            meter_actuals = ai_engine.load_meter_actuals(target_date_str)
            if meter_actuals is not None and len(meter_actuals) == 96:
                for b_i in range(96):
                    meter_by_block[b_i + 1] = float(meter_actuals[b_i])
        except Exception:
            meter_by_block = {}

    rows = []
    tot_pen = 0.0
    safe_count = 0
    has_actuals = any(v > 0.05 for v in meter_by_block.values())
    max_actual_block = total_blocks
    if has_actuals:
        reported_blocks = [b for b, v in meter_by_block.items() if v > 0.005]
        if reported_blocks and max(reported_blocks) < 70:
            max_actual_block = max(reported_blocks)

    for block in range(1, total_blocks + 1):
        end_min = block * 15
        start_min = end_min - 15
        s_hr, s_min = divmod(start_min, 60)
        e_hr, e_min = divmod(end_min, 60)
        t_str = "00:00" if e_hr == 24 else f"{e_hr:02d}:{e_min:02d}"

        b_data = schedule_by_block.get(block, {})
        gti_val = float(b_data.get("intellis_gti", 0.0))
        mw_val = float(b_data.get("intellis_mw", 0.0))
        if not getattr(config, "is_wind_plant", lambda: False)() and (block < 24 or block > 76):
            mw_val = 0.0
            gti_val = 0.0

        m_val = float(meter_by_block.get(block, 0.0))
        dev = round(m_val - mw_val, 2)
        abs_dev = abs(dev)

        slab = "0% Safe"
        blk_pen = 0.0
        if has_actuals and block <= max_actual_block:
            if abs_dev <= tol_mw:
                safe_count += 1
            elif abs_dev <= (tol_mw * 2.0):
                slab = "10% Slab"
                blk_pen = (abs_dev - tol_mw) * 250.0 * 0.10 * ppa
            elif abs_dev <= (tol_mw * 3.0):
                slab = "20% Slab"
                blk_pen = (tol_mw * 250.0 * 0.10 * ppa) + ((abs_dev - tol_mw * 2.0) * 250.0 * 0.20 * ppa)
            else:
                slab = ">30% Slab"
                blk_pen = (tol_mw * 250.0 * 0.10 * ppa) + (tol_mw * 250.0 * 0.20 * ppa) + ((abs_dev - tol_mw * 3.0) * 250.0 * 0.30 * ppa)
        else:
            dev = 0.0
            abs_dev = 0.0
            slab = "0% Safe"
            blk_pen = 0.0
            safe_count += 1

        tot_pen += blk_pen

        rows.append({
            "block": block,
            "time": t_str,
            "intellis_gti": round(gti_val, 1),
            "intellis_mw": round(mw_val, 2),
            "schedule_mw": round(mw_val, 2),
            "dev_mw": dev,
            "dsm_slab": slab,
            "block_penalty_inr": round(blk_pen, 2),
            "cumulative_penalty_inr": round(tot_pen, 2),
        })

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "block",
        "time",
        "intellis_gti",
        "intellis_mw",
        "schedule_mw",
        "dev_mw",
        "dsm_slab",
        "block_penalty_inr",
        "cumulative_penalty_inr",
    ]
    with open(output_csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    return {
        "input_csv": str(input_csv_path),
        "output_csv": str(output_csv_path),
        "total_blocks": total_blocks,
        "blocks_present": len(rows),
        "total_penalty_inr": round(tot_pen, 2),
        "safe_blocks": safe_count,
    }


def download_recent_meter_history_files(

    storage_module,
    bucket: str,
    meter_prefix: str,
    target_date: str,
    destination_dir: Path,
    days: int = 3,
) -> list[Path]:
    """Download the previous N completed day meter files into a local folder.

    Returns the downloaded file paths in chronological order, oldest first.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    target_dt = dt.datetime.strptime(target_date, "%Y-%m-%d").date()
    downloaded: list[Path] = []

    for offset in range(days, 0, -1):
        day = target_dt - dt.timedelta(days=offset)
        date_str = day.strftime("%Y-%m-%d")
        meter_objects = list_meter_objects_for_day(storage_module, bucket, meter_prefix, date_str)
        if not meter_objects:
            continue

        selected = select_preferred_meter_object(meter_objects)
        local_path = destination_dir / f"{date_str}_{Path(selected.key).name}"
        try:
            storage_module.download_file(bucket, selected.key, local_path)
        except Exception:
            continue
        downloaded.append(local_path)

    return downloaded





