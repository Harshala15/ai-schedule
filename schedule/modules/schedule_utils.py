"""Shared scheduler helpers used by plant-specific Lambda wrappers."""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

import config


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
        "Step 1 Meter Base Forecast MW",
        "Step 2 Weather Adjustment MW",
        "LLM Reasoning",
    ]
    ordered = [column for column in preferred if column in existing_fieldnames or column in preferred]
    for column in existing_fieldnames:
        if column not in ordered and column not in (
            "Step 2 Weather + Video Adjusted MW",
            "Step 3 Plant Performance MW",
            "Step 4 Revision Feedback MW",
            "Step 4 Revision Feedback Adjusted MW",
            "LLM Schedule (MW)",
            "Schedule MW",
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

    def _sort_key(row: dict) -> dt.datetime:
        key = row_time_key(row)
        try:
            return dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return dt.datetime.max

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
            return None
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

    def _sort_key(row: dict) -> dt.datetime:
        key = row_time_key(row)
        try:
            return dt.datetime.strptime(key, "%Y-%m-%d %H:%M")
        except ValueError:
            return dt.datetime.max

    frozen_rows = sorted(merged_by_time.values(), key=_sort_key)
    current_final_fieldnames = [
        "Block",
        "Time Interval (15 minute interval)",
        "Step 1 Meter Base Forecast MW",
        "Step 2 Weather Adjustment MW",
        "LLM Reasoning",
    ]

    for row in frozen_rows:
        if "Step 2 Weather Adjustment MW" not in row or not str(row.get("Step 2 Weather Adjustment MW", "")).strip():
            row["Step 2 Weather Adjustment MW"] = str(
                row.get("Step 2 Weather + Video Adjusted MW")
                or row.get("Schedule MW")
                or row.get("LLM Schedule (MW)")
                or row.get("Final Validated MW")
                or row.get("step2_mw")
                or "0.0"
            )

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

    mw_columns = [column for column in current_final_fieldnames if "MW" in column.upper()]
    for row in frozen_rows:
        block_number = _row_block_number(row)
        should_zero = (block_number is not None and block_number < 28) or _is_night_time(row)
        if not should_zero:
            continue
        for column in mw_columns:
            if column in row:
                row[column] = "0"

    # ---- Stitch boundary seam between past frozen blocks and new forward blocks ----
    cap_mw = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
    mw_main_col = None
    for candidate in (
        "Step 2 Weather Adjustment MW",
        "Step 2 Weather + Video Adjusted MW",
        "Final Validated MW",
        "Schedule MW",
        "schedule_mw",
        "LLM Schedule (MW)",
    ):
        if candidate in current_final_fieldnames:
            mw_main_col = candidate
            break

    if mw_main_col and len(frozen_rows) > 1:
        for i in range(1, len(frozen_rows)):
            r_prev_dt = _row_dt(frozen_rows[i - 1])
            r_curr_dt = _row_dt(frozen_rows[i])
            if r_prev_dt is not None and r_curr_dt is not None:
                if r_prev_dt < freeze_from <= r_curr_dt:
                    # Dynamic physical ramp limits based on solar geometry & time-of-day
                    block_time = r_curr_dt.time()
                    if dt.time(7, 45) <= block_time <= dt.time(10, 45):
                        # Morning rapid solar geometric ramp-up (allow up to 20% plant capacity per 15 min)
                        max_step = cap_mw * 0.20
                    elif dt.time(16, 0) <= block_time <= dt.time(18, 15):
                        # Late afternoon rapid sunset ramp-down
                        max_step = cap_mw * 0.18
                    elif dt.time(11, 0) <= block_time < dt.time(16, 0):
                        # Midday solar arch
                        max_step = cap_mw * 0.12
                    else:
                        # Pre-dawn / post-dusk
                        max_step = cap_mw * 0.08

                    try:
                        prev_mw = float(frozen_rows[i - 1].get(mw_main_col, 0.0) or 0.0)
                        curr_mw = float(frozen_rows[i].get(mw_main_col, 0.0) or 0.0)
                        diff = curr_mw - prev_mw
                        if abs(diff) > max_step:
                            smooth_mw = round(prev_mw + max_step * (1 if diff > 0 else -1), 3)
                            frozen_rows[i][mw_main_col] = str(max(0.0, smooth_mw))
                            if i + 1 < len(frozen_rows):
                                next_mw = float(frozen_rows[i + 1].get(mw_main_col, 0.0) or 0.0)
                                diff_next = next_mw - smooth_mw
                                if abs(diff_next) > max_step:
                                    smooth_next = round(smooth_mw + max_step * (1 if diff_next > 0 else -1), 3)
                                    frozen_rows[i + 1][mw_main_col] = str(max(0.0, smooth_next))
                    except (ValueError, TypeError):
                        pass
                    break


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
) -> dict:
    """Write a full block schedule with missing blocks filled with zero.

    Implements Approach 3 for un-forecasted afternoon daylight blocks:
    1. Astronomical Solar Geometry (PVLib / time_features elevation envelope).
    2. Tri-Stream Weather Consensus (ECMWF 9 km, 91-Member Ensemble, 5-Agency Consensus + CAPE).
    3. Live Ground MOS Bias Calibration (evaluated strictly up to revision time,
       clamped to [0.85, 1.15] and exponentially damped into late afternoon).
    4. Physical Safety Guardrails:
       - Physical diffuse floor >= 25% capacity during high daylight (elev >= 45 deg).
       - Convective storm attenuation when CAPE >= 1500 J/kg or rain cells detected.
       - Smooth diurnal descent towards dusk (no unphysical sawtooth spikes).
       - Seam boundary ramp limit from the last block of the active AI window.
    """
    import math
    schedule_by_block: dict[int, float] = {}

    # Extract target date if not explicitly passed
    target_date_str = str(target_date) if target_date else None
    date_match = None
    if not target_date_str:
        for p_cand in (input_csv_path, fallback_csv_path):
            if p_cand:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", str(p_cand.name))
                if m:
                    target_date_str = m.group(1)
                    break
    if not target_date_str:
        target_date_str = dt.datetime.now(IST).strftime("%Y-%m-%d")

    # 1. Read fallback / latest schedule first (full diurnal curve)
    if fallback_csv_path and fallback_csv_path.exists():
        with open(fallback_csv_path, "r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                raw_block = str(row.get("Block", "")).strip()
                try:
                    b = int(raw_block)
                    mw = float(
                        row.get("Step 2 Weather Adjustment MW")
                        or row.get("Step 2 Weather + Video Adjusted MW")
                        or row.get("Final Validated MW")
                        or row.get("Schedule MW")
                        or row.get("schedule_mw")
                        or row.get("LLM Schedule (MW)")
                        or 0.0
                    )
                    schedule_by_block[b] = mw
                except (ValueError, TypeError):
                    continue

    # 2. Overlay frozen current-final schedule (authoritative for past frozen blocks + active AI window)
    if input_csv_path.exists():
        with open(input_csv_path, "r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                raw_block = str(row.get("Block", "")).strip()
                try:
                    b = int(raw_block)
                    mw = float(
                        row.get("Step 2 Weather Adjustment MW")
                        or row.get("Step 2 Weather + Video Adjusted MW")
                        or row.get("Final Validated MW")
                        or row.get("Schedule MW")
                        or row.get("schedule_mw")
                        or row.get("LLM Schedule (MW)")
                        or 0.0
                    )
                    schedule_by_block[b] = mw
                except (ValueError, TypeError):
                    continue

    dc_cap = float(getattr(config, "PLANT_DC_CAPACITY_MW", getattr(config, "PLANT_CAPACITY_MW", 10.0)))
    ac_cap = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
    pr = float(getattr(config, "PERFORMANCE_RATIO", 0.78))

    # 3. APPROACH 3: Combined Physics + Tri-Stream NWP + Live Ground MOS Calibration
    max_populated_daylight_block = max(
        [b for b, mw in schedule_by_block.items() if 28 <= b <= 72 and mw > 0.05],
        default=27
    )

    # A. Calculate Live Ground Clearness from morning blocks up to revision time (t <= T_rev)
    clearness_ratios = []
    for b, mw in schedule_by_block.items():
        if 28 <= b <= max_populated_daylight_block and mw > 0.01:
            b_hour = (b - 1) * 0.25
            h_from_noon = abs(b_hour - 12.25)
            if h_from_noon < 6.0:
                elev_sin = math.cos((h_from_noon / 6.0) * (math.pi / 2.0))
                clear_theoretical = dc_cap * pr * (elev_sin ** 1.05)
                if clear_theoretical > 0.1:
                    clearness_ratios.append(min(1.0, mw / clear_theoretical))

    implied_clearness = max(0.20, min(1.0, sum(clearness_ratios) / len(clearness_ratios))) if clearness_ratios else 0.85

    # Guardrail 1: Safety-clamp raw MOS bias to [0.85, 1.15] (prevents morning micro-clouds from over-slashing afternoon)
    clamped_mos = max(0.85, min(1.15, implied_clearness))

    # B. Load / Fetch Tri-Stream Weather Fusion if not already provided
    if weather_fusion_map is None:
        try:
            from modules.weather import weather_fusion
            ref_dt = dt.datetime.strptime(f"{target_date_str} 06:00", "%Y-%m-%d %H:%M")
            lat = float(getattr(config, "PLANT_LAT", 24.56))
            lon = float(getattr(config, "PLANT_LON", 75.09))
            p_name = getattr(config, "PLANT_NAME", "Solar Plant")
            w_res = weather_fusion.fetch_dual_stream_weather_fusion(
                latitude=lat,
                longitude=lon,
                reference_time=ref_dt,
                hours_ahead=14,
                plant_name=p_name,
            )
            weather_rows = w_res.get("fused_rows", [])
            weather_fusion_map = {r.get("hour_label"): r for r in weather_rows if isinstance(r, dict) and r.get("hour_label")}
        except Exception:
            weather_fusion_map = {}

    # C. Block-by-block Combined Physics + Tri-Stream Weather Synthesis
    for block in range(max_populated_daylight_block + 1, 74):
        block_hour = (block - 1) * 0.25
        b_hour_int = int(block_hour)
        b_min_int = int(round((block_hour - b_hour_int) * 60))
        b_time_str = f"{b_hour_int:02d}:{b_min_int:02d}"

        try:
            from modules.weather import time_features
            block_dt = dt.datetime.strptime(f"{target_date_str} {b_time_str}", "%Y-%m-%d %H:%M")
            elev = time_features.compute_time_features(block_dt)["solar_elevation_deg"]
        except Exception:
            h_from_noon = abs(block_hour - 12.25)
            elev = max(0.0, 90.0 - (h_from_noon * 15.0)) if h_from_noon < 6.0 else 0.0

        if elev < 3.0:
            schedule_by_block[block] = 0.0
            continue
        elif elev < 7.5:
            # Low dusk diffuse generation
            schedule_by_block[block] = round(min(0.20, ac_cap * 0.04), 3)
            continue

        raw_sine = math.sin(math.radians(max(0.0, elev)))
        clearsky_mw = min(ac_cap, dc_cap * pr * (raw_sine ** 1.05))
        clearsky_gti = max(10.0, 1000.0 * (raw_sine ** 0.95))

        # Tri-stream consensus lookup
        w_entry = weather_fusion_map.get(b_time_str) if weather_fusion_map else None
        if not w_entry and weather_fusion_map:
            w_entry = weather_fusion_map.get(f"{b_hour_int:02d}:00")

        if w_entry:
            gti_fused = float(w_entry.get("gti_fused") or w_entry.get("gti_stream1", 0.0))
            cloud_pct = float(w_entry.get("cloud_pct", 0.0))
            precip_mm = float(w_entry.get("precip_mm", 0.0))
            cape_val = float(w_entry.get("cape_j_kg", 0.0))
            temp_derate = float(w_entry.get("temp_derate_multiplier", 1.0))
            if gti_fused > 20.0 and clearsky_gti > 20.0:
                nwp_clearness = max(0.15, min(1.02, gti_fused / clearsky_gti))
            else:
                nwp_clearness = max(0.20, min(1.0, 1.0 - (0.75 * cloud_pct / 100.0)))
        else:
            nwp_clearness = implied_clearness
            precip_mm = 0.0
            cape_val = 0.0
            temp_derate = 1.0

        # Guardrail 2: Exponential MOS Damping into late afternoon
        blocks_ahead = block - max_populated_daylight_block
        damping_factor = math.exp(-0.12 * blocks_ahead)
        damped_mos = 1.0 + (clamped_mos - 1.0) * damping_factor

        effective_clearness = max(0.15, min(1.05, nwp_clearness * damped_mos))
        synth_mw = clearsky_mw * effective_clearness * temp_derate

        # Physical Guardrails:
        # 1. Physical Diffuse Floor (elevation >= 45 deg)
        if elev >= 45.0:
            diffuse_floor = round(ac_cap * 0.25, 3)
            synth_mw = max(diffuse_floor, synth_mw)

        # 2. Convective Storm Attenuation (High CAPE + Rain)
        if cape_val >= 1500 and (precip_mm >= 0.15 or (w_entry and w_entry.get("cloud_pct", 0.0) >= 60.0)):
            synth_mw = min(synth_mw, clearsky_mw * 0.35)

        # 3. Monotonic Afternoon Descent after solar noon (block >= 50)
        prev_block_mw = schedule_by_block.get(block - 1)
        if block >= 50 and prev_block_mw is not None:
            max_allowed_step = prev_block_mw + (ac_cap * 0.05)
            synth_mw = min(synth_mw, max_allowed_step)

        final_block_mw = round(max(0.0, min(ac_cap, synth_mw)), 3)
        schedule_by_block[block] = final_block_mw if final_block_mw > 0.02 else 0.0

    # Seam Boundary Smoothing: smooth transition from last AI block
    if max_populated_daylight_block in schedule_by_block and (max_populated_daylight_block + 1) in schedule_by_block:
        last_ai_mw = schedule_by_block[max_populated_daylight_block]
        first_synth_mw = schedule_by_block[max_populated_daylight_block + 1]
        max_step = ac_cap * 0.15
        if abs(first_synth_mw - last_ai_mw) > max_step:
            smoothed = round(last_ai_mw + max_step * (1 if first_synth_mw > last_ai_mw else -1), 3)
            schedule_by_block[max_populated_daylight_block + 1] = max(0.0, smoothed)

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["block", "schedule_mw"])
        writer.writeheader()
        for block in range(1, total_blocks + 1):
            val = schedule_by_block.get(block, 0.0)
            if block < 24 or block >= 75:
                val = 0.0
            writer.writerow({
                "block": block,
                "schedule_mw": max(0.0, round(val, 3)),
            })

    return {
        "input_csv": str(input_csv_path),
        "output_csv": str(output_csv_path),
        "total_blocks": total_blocks,
        "blocks_present": len(schedule_by_block),
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





