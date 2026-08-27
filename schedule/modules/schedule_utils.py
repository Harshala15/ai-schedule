"""Shared scheduler helpers used by plant-specific Lambda wrappers."""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

import config


def parse_target_datetime(event: dict | None) -> tuple[str, str, dt.datetime]:
    now = dt.datetime.now()
    target_date = (event or {}).get("target_date") or now.strftime("%Y-%m-%d")
    target_time = (event or {}).get("target_time") or now.strftime("%H:%M")
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
        "Step 2 Weather + Video Adjusted MW",
        "Step 3 Plant Performance MW",
        "Step 4 Revision Feedback MW",
        "LLM Schedule (MW)",
        "Schedule MW",
        "LLM Reasoning",
    ]
    ordered = [column for column in preferred if column in existing_fieldnames or column in preferred]
    for column in existing_fieldnames:
        if column not in ordered:
            ordered.append(column)
    return ordered


def _meter_filename_hints(plant_name: str | None = None) -> list[str]:
    plant = (plant_name or config.PLANT_NAME or "").strip().upper()
    hints = {
        "BHUPALPALLY": ["bhupalpally"],
        "KASIPET": ["kasipet"],
        "SIRMOUR": ["sirmour", "solar_inv", "solarinv"],
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
        "BHUPALPALLY": 45,
        "KASIPET": 45,
        "SIRMOUR": 90,
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
            "BHUPALPALLY": 45,
            "KASIPET": 45,
            "SIRMOUR": 90,
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
    current_final_fieldnames = _current_final_fieldnames(fieldnames)

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
    total_blocks: int = 96,
) -> dict:
    """Write a full block schedule with missing blocks filled with zero.

    The input is expected to contain a `Block` column and an
    `LLM Schedule (MW)` column. The output always contains exactly
    `total_blocks` rows with the schema:

        block,schedule_mw
    """
    schedule_by_block: dict[int, float] = {}
    with open(input_csv_path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_block = str(row.get("Block", "")).strip()
            try:
                block = int(raw_block)
            except (TypeError, ValueError):
                continue

            raw_mw = row.get("LLM Schedule (MW)", "")
            try:
                schedule_mw = float(raw_mw or 0.0)
            except (TypeError, ValueError):
                schedule_mw = 0.0

            schedule_by_block[block] = schedule_mw

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["block", "schedule_mw"])
        writer.writeheader()
        for block in range(1, total_blocks + 1):
            writer.writerow({
                "block": block,
                "schedule_mw": schedule_by_block.get(block, 0.0),
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
        day_prefix = f"{meter_prefix.rstrip('/')}/{date_str}/meter_data"
        meter_objects = storage_module.list_objects(bucket, day_prefix)
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
