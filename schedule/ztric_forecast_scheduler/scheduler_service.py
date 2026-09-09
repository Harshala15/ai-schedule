from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3

from modules import schedule_utils
from modules.weather import openmeteo_ensemble
from ztric_forecast_scheduler import settings, storage

METER_POWER_COLUMNS = [
    "metered_mw",
    "Metered MW",
    "MW",
    "TVM Active Power",
    "Active Power-avg MFM-OUT(Meter Power) (kW)",
    "Active Power-Avg MFM-OUT (KW)",
    "Active Power (kW)",
    "Active Power (MW)",
]
METER_TIMESTAMP_COLUMNS = [
    "block_end",
    "block_start",
    "Block End",
    "Block Start",
    "TimeStamp",
    "Timestamp",
    "datetime",
    "date_time",
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _safe_id(value: str) -> str:
    return str(value or "").strip().upper().replace(" ", "_").replace("/", "_").replace("-", "_").replace(".", "")


def _asset_id(value: str) -> str:
    return _safe_id(value)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _unique_id(base_id: str, raw: dict[str, Any], seen_ids: set[str]) -> str:
    item_id = base_id or "ITEM"
    if item_id not in seen_ids:
        seen_ids.add(item_id)
        return item_id
    for key in ("contract_id", "approval_number"):
        suffix = _safe_id(str(raw.get(key) or ""))
        if suffix:
            candidate = f"{item_id}_{suffix}"
            if candidate not in seen_ids:
                seen_ids.add(candidate)
                return candidate
    index = 2
    while f"{item_id}_{index}" in seen_ids:
        index += 1
    candidate = f"{item_id}_{index}"
    seen_ids.add(candidate)
    return candidate


def _load_contract_config() -> dict[str, Any]:
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
    table_name = os.getenv("MULTI_GENERATOR_PLANT_TABLE", settings.MULTI_GENERATOR_PLANT_TABLE).strip()
    plant_id = os.getenv("MULTI_GENERATOR_PLANT_ID", settings.MULTI_GENERATOR_PLANT_ID).strip()
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    response = table.get_item(Key={"plant_id": plant_id})
    item = _jsonable(response.get("Item") or {})
    if not item:
        raise RuntimeError(f"No ZTRIC contract config found in {table_name} for plant_id={plant_id}")

    buyers: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    seen_buyer_ids: set[str] = set()
    seen_asset_ids: set[str] = set()

    for raw_buyer in item.get("buyers") or []:
        if not isinstance(raw_buyer, dict):
            continue
        buyer_name = str(raw_buyer.get("buyer_name") or "").strip()
        buyer_capacity = _float_value(raw_buyer.get("schedule_capacity_mw"))
        if not buyer_name or buyer_capacity <= 0:
            continue
        buyer_id = _unique_id(_safe_id(buyer_name), raw_buyer, seen_buyer_ids)
        buyers.append(
            {
                "buyer_id": buyer_id,
                "buyer_name": buyer_name,
                "capacity_mw": buyer_capacity,
                "contract_id": str(raw_buyer.get("contract_id") or "").strip(),
                "approval_number": str(raw_buyer.get("approval_number") or "").strip(),
            }
        )
        for raw_asset in raw_buyer.get("assets") or []:
            if not isinstance(raw_asset, dict):
                continue
            asset_name = str(raw_asset.get("asset_name") or raw_asset.get("assetName") or "").strip()
            asset_capacity = _float_value(raw_asset.get("capacity_ac_mw") or raw_asset.get("acCapacityMw"))
            if not asset_name or asset_capacity <= 0:
                continue
            asset_code = _asset_id(asset_name)
            if asset_code in seen_asset_ids:
                continue
            seen_asset_ids.add(asset_code)
            assets.append(
                {
                    "asset_id": asset_code,
                    "asset_name": asset_name,
                    "buyer_id": buyer_id,
                    "capacity_ac_mw": asset_capacity,
                    "capacity_dc_mw": _float_value(raw_asset.get("capacity_dc_mw") or raw_asset.get("dcCapacityMw"), asset_capacity),
                    "meter_data_available": bool(raw_asset.get("meter_data_available") or raw_asset.get("meterAvailable")),
                }
            )

    if not buyers:
        raise RuntimeError("ZTRIC contract config has no active buyers with schedule_capacity_mw")
    if not assets:
        raise RuntimeError("ZTRIC contract config has no active assets to aggregate")

    current_capacity = item.get("currently_scheduling_capacity") if isinstance(item.get("currently_scheduling_capacity"), dict) else {}
    reference_capacity = _float_value(current_capacity.get("ac_mw"))
    if reference_capacity <= 0:
        reference_capacity = sum(_float_value(buyer.get("capacity_mw")) for buyer in buyers)
    if reference_capacity <= 0:
        raise RuntimeError("ZTRIC reference capacity could not be derived from contract config")

    return {
        "config_source": "dynamodb_multi_generator_plant",
        "table_name": table_name,
        "plant_id": plant_id,
        "plant_name": item.get("plant_name") or "ZETRIC",
        "latitude": _float_value(item.get("latitude"), 18.557968),
        "longitude": _float_value(item.get("longitude"), 76.859083),
        "reference_capacity_mw": reference_capacity,
        "buyers": buyers,
        "assets": assets,
        "updated_at": item.get("updated_at"),
        "updated_by": item.get("updated_by"),
    }


def _block_times(block: int) -> tuple[str, str]:
    start_min = (block - 1) * 15
    end_min = block * 15
    return f"{start_min // 60:02d}:{start_min % 60:02d}", f"{(end_min // 60) % 24:02d}:{end_min % 60:02d}"


def _pick_field(fieldnames: list[str], candidates: list[str]) -> str | None:
    lower_map = {str(field).lower().strip(): field for field in fieldnames if field}
    for candidate in candidates:
        hit = lower_map.get(candidate.lower().strip())
        if hit:
            return hit
    return None


def _parse_time(value: str, target_date: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]
    for fmt in formats:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            date_part = dt.datetime.strptime(target_date, "%Y-%m-%d")
            return date_part.replace(hour=parsed.hour, minute=parsed.minute, second=parsed.second)
        except ValueError:
            pass
    return None


def _block_from_time(value: dt.datetime) -> int:
    return max(1, min(96, ((value.hour * 60 + value.minute) // 15) + 1))


def _power_to_mw(raw_value: Any, column: str) -> float | None:
    value = _float_value(raw_value, default=math.nan)
    if math.isnan(value):
        return None
    normalized = column.lower()
    if "kw" in normalized and "mw" not in normalized:
        value = value / 1000.0
    return max(value, 0.0)


def _latest_meter_key(bucket: str, raw_day_prefix: str, asset: dict[str, Any]) -> str | None:
    if not asset.get("meter_data_available"):
        return None
    prefixes = [
        f"{raw_day_prefix.rstrip('/')}/metered_data/{asset['asset_id']}",
        f"{raw_day_prefix.rstrip('/')}/metered_data/{asset['asset_name']}",
    ]
    for prefix in prefixes:
        objects = [obj for obj in storage.list_objects(bucket, prefix.rstrip("/") + "/") if obj.key.lower().endswith(".csv")]
        if objects:
            return max(objects, key=lambda obj: (obj.last_modified or dt.datetime.min.replace(tzinfo=dt.timezone.utc), obj.key)).key
    return None


def _load_meter_values(path: Path, target_date: str, cutoff_block: int, asset_capacity_mw: float) -> dict[int, float]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(2048)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        handle.seek(0)
        reader = csv.DictReader(handle, delimiter=delimiter)
        fieldnames = list(reader.fieldnames or [])
        block_col = _pick_field(fieldnames, ["block", "block_no", "Block", "Block No"])
        time_col = _pick_field(fieldnames, METER_TIMESTAMP_COLUMNS)
        power_col = _pick_field(fieldnames, METER_POWER_COLUMNS)
        if power_col is None:
            raise RuntimeError(f"Could not find meter power column in {path.name}")
        values: dict[int, float] = {}
        for row in reader:
            block = None
            if block_col:
                try:
                    block = int(float(str(row.get(block_col, "")).strip()))
                except Exception:
                    block = None
            if block is None and time_col:
                parsed = _parse_time(str(row.get(time_col, "")), target_date)
                if parsed is not None:
                    block = _block_from_time(parsed)
            if block is None or block < 1 or block > cutoff_block:
                continue
            mw = _power_to_mw(row.get(power_col), power_col)
            if mw is None:
                continue
            values[block] = min(mw, asset_capacity_mw)
    return values


def _clear_sky_curve(
    *,
    latitude: float,
    longitude: float,
    target_date: str,
    capacity_ac_mw: float,
    capacity_dc_mw: float,
    timezone: str,
) -> tuple[dict[int, float], dict[int, float]]:
    try:
        import pandas as pd
        from pvlib import irradiance
        from pvlib.location import Location

        tz = ZoneInfo(timezone)
        day_start = dt.datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=tz)
        times = pd.date_range(start=day_start + dt.timedelta(minutes=15), periods=96, freq="15min", tz=timezone)
        location = Location(latitude, longitude, tz=timezone)
        solar_position = location.get_solarposition(times)
        clearsky = location.get_clearsky(times, model="ineichen")
        poa = irradiance.get_total_irradiance(
            surface_tilt=20.0,
            surface_azimuth=180.0,
            solar_zenith=solar_position["apparent_zenith"],
            solar_azimuth=solar_position["azimuth"],
            dni=clearsky["dni"],
            ghi=clearsky["ghi"],
            dhi=clearsky["dhi"],
        )["poa_global"].fillna(0.0).clip(lower=0.0)
        clear_mw: dict[int, float] = {}
        clear_poa: dict[int, float] = {}
        for block, poa_w in enumerate(poa, start=1):
            clear_poa[block] = float(poa_w)
            clear_mw[block] = min(max((float(poa_w) / 1000.0) * capacity_dc_mw * 0.78, 0.0), capacity_ac_mw)
        return clear_mw, clear_poa
    except Exception:
        clear_mw = {}
        clear_poa = {}
        for block in range(1, 97):
            hour = ((block - 1) * 15) / 60.0
            daylight = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
            clear_poa[block] = daylight * 900.0
            clear_mw[block] = min(capacity_ac_mw, capacity_ac_mw * daylight * 0.85)
        return clear_mw, clear_poa


def _weather_report(contract: dict[str, Any], target_dt: dt.datetime) -> dict[str, Any]:
    try:
        return openmeteo_ensemble.fetch_openmeteo_ensemble_calibrated_summary(
            latitude=float(contract.get("latitude") or 18.557968),
            longitude=float(contract.get("longitude") or 76.859083),
            reference_time=target_dt,
            hours_ahead=12,
            timezone=settings.DEFAULT_TIMEZONE,
            plant_name="ZTRIC",
        )
    except Exception as exc:
        return {"source": "openmeteo_ensemble_error", "prompt_text": f"Open-Meteo ensemble unavailable: {exc}", "rows": []}


def _weather_factor_by_block(weather: dict[str, Any], clear_poa: dict[int, float]) -> dict[int, float]:
    factors: dict[int, float] = {}
    for row in weather.get("rows") or []:
        hour_label = str(row.get("hour_label") or "")
        try:
            hour, minute = [int(part) for part in hour_label.split(":")[:2]]
        except Exception:
            continue
        gti = _float_value(row.get("global_tilted_irradiance_instant"), default=math.nan)
        if math.isnan(gti):
            continue
        for offset in range(4):
            block = ((hour * 60 + minute + offset * 15) // 15) + 1
            if 1 <= block <= 96:
                poa = clear_poa.get(block, 0.0)
                factors[block] = max(0.15, min(1.15, gti / poa)) if poa > 30 else 0.0
    return factors


def _build_asset_schedule(
    *,
    asset: dict[str, Any],
    meter_values: dict[int, float],
    clear_mw: dict[int, float],
    weather_factors: dict[int, float],
    cutoff_block: int,
) -> dict[int, float]:
    capacity = float(asset["capacity_ac_mw"])
    schedule: dict[int, float] = {}
    recent_blocks = [block for block in sorted(meter_values) if block <= cutoff_block and clear_mw.get(block, 0.0) > 0.05]
    if recent_blocks:
        ratios = [meter_values[block] / max(clear_mw.get(block, 0.0), 0.05) for block in recent_blocks[-4:]]
        meter_factor = max(0.2, min(1.2, sum(ratios) / len(ratios)))
    else:
        meter_factor = 1.0

    for block in range(1, 97):
        if block <= cutoff_block and block in meter_values:
            value = meter_values[block]
        else:
            weather_factor = weather_factors.get(block, 1.0)
            value = clear_mw.get(block, 0.0) * weather_factor * meter_factor
        schedule[block] = min(max(value, 0.0), capacity)
    return schedule


def _generate_asset_schedules(
    *,
    bucket: str,
    contract: dict[str, Any],
    raw_day_prefix: str,
    target_date: str,
    target_dt: dt.datetime,
    weather: dict[str, Any],
    work_root: Path,
) -> tuple[dict[str, dict[int, float]], list[dict[str, Any]]]:
    cutoff_block = ((target_dt.hour * 60 + target_dt.minute) // 15) + 1
    asset_schedules: dict[str, dict[int, float]] = {}
    asset_inputs: list[dict[str, Any]] = []
    for asset in contract["assets"]:
        capacity_ac = float(asset["capacity_ac_mw"])
        capacity_dc = float(asset.get("capacity_dc_mw") or capacity_ac)
        clear_mw, clear_poa = _clear_sky_curve(
            latitude=float(contract.get("latitude") or 18.557968),
            longitude=float(contract.get("longitude") or 76.859083),
            target_date=target_date,
            capacity_ac_mw=capacity_ac,
            capacity_dc_mw=capacity_dc,
            timezone=settings.DEFAULT_TIMEZONE,
        )
        weather_factors = _weather_factor_by_block(weather, clear_poa)
        meter_key = _latest_meter_key(bucket, raw_day_prefix, asset)
        meter_values: dict[int, float] = {}
        meter_error = ""
        if meter_key:
            local_meter = work_root / "metered_data" / asset["asset_id"] / Path(meter_key).name
            storage.download_file(bucket, meter_key, local_meter)
            try:
                meter_values = _load_meter_values(local_meter, target_date, cutoff_block, capacity_ac)
            except Exception as exc:
                meter_error = str(exc)
        schedule = _build_asset_schedule(
            asset=asset,
            meter_values=meter_values,
            clear_mw=clear_mw,
            weather_factors=weather_factors,
            cutoff_block=cutoff_block,
        )
        asset_schedules[asset["asset_id"]] = schedule
        asset_inputs.append(
            {
                "asset_id": asset["asset_id"],
                "asset_name": asset["asset_name"],
                "buyer_id": asset["buyer_id"],
                "capacity_ac_mw": capacity_ac,
                "meter_data_available": asset["meter_data_available"],
                "meter_key": meter_key or "",
                "meter_rows_used": len(meter_values),
                "fallback_used": not bool(meter_values),
                "meter_error": meter_error,
            }
        )
    return asset_schedules, asset_inputs


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _round_capacity_safe(value: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor((max(float(value), 0.0) + 1e-9) * factor) / factor


def _build_rows(
    asset_schedules: dict[str, dict[int, float]],
    contract: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    buyers = contract["buyers"]
    assets = contract["assets"]
    decimals = settings.ROUND_DECIMALS
    asset_ids = [asset["asset_id"] for asset in assets]
    buyer_ids = [buyer["buyer_id"] for buyer in buyers]
    buyer_capacity = {buyer["buyer_id"]: float(buyer["capacity_mw"]) for buyer in buyers}
    total_capacity = float(contract["reference_capacity_mw"])
    fieldnames = ["block", "start_time", "end_time"] + asset_ids + buyer_ids + ["total_ai_schedule_mw"]
    rows: list[dict[str, Any]] = []

    for block in range(1, 97):
        start_time, end_time = _block_times(block)
        row: dict[str, Any] = {"block": block, "start_time": start_time, "end_time": end_time}
        buyer_totals = {buyer_id: 0.0 for buyer_id in buyer_ids}
        for asset in assets:
            asset_id = asset["asset_id"]
            asset_capacity = float(asset["capacity_ac_mw"])
            value = min(max(float(asset_schedules.get(asset_id, {}).get(block, 0.0)), 0.0), asset_capacity)
            row[asset_id] = _round_capacity_safe(value, decimals)
            buyer_totals[asset["buyer_id"]] = buyer_totals.get(asset["buyer_id"], 0.0) + value

        capped_buyer_total = 0.0
        for buyer_id in buyer_ids:
            capped = min(max(buyer_totals.get(buyer_id, 0.0), 0.0), buyer_capacity.get(buyer_id, 0.0))
            row[buyer_id] = _round_capacity_safe(capped, decimals)
            capped_buyer_total += capped
        row["total_ai_schedule_mw"] = _round_capacity_safe(min(capped_buyer_total, total_capacity), decimals)
        rows.append(row)
    return fieldnames, rows


def run_schedule_job(
    bucket: str,
    capture_prefix: str,
    meter_prefix: str,
    schedule_prefix: str,
    event: dict | None = None,
) -> dict[str, Any]:
    target_date, target_time, target_dt = schedule_utils.parse_target_datetime(event)
    effective_dt = schedule_utils.freeze_from_datetime(target_date, target_time, block_minutes=15)
    snapshot_block = max(1, min(96, ((effective_dt.hour * 60 + effective_dt.minute) // 15)))
    snapshot_stamp = f"{target_date.replace('-', '')}t{target_dt.strftime('%H%M%S')}"

    contract = _load_contract_config()
    raw_day_prefix = f"{capture_prefix.rstrip('/')}/{target_date}"
    work_root = schedule_utils.storage_subpath("_scheduler_work", "ZTRIC", target_date, target_time.replace(":", "-"))
    weather = _weather_report(contract, target_dt)
    asset_schedules, asset_inputs = _generate_asset_schedules(
        bucket=bucket,
        contract=contract,
        raw_day_prefix=raw_day_prefix,
        target_date=target_date,
        target_dt=target_dt,
        weather=weather,
        work_root=work_root,
    )
    fieldnames, rows = _build_rows(asset_schedules, contract)

    generated_root = schedule_utils.prefix_to_local_dir(schedule_prefix, target_date)
    snapshot_csv = generated_root / f"schedule_from_{snapshot_block}_{snapshot_stamp}.csv"
    snapshot_metadata = generated_root / f"{snapshot_csv.name}.meta.json"
    latest_csv = generated_root / f"{target_date}_latest_schedule.csv"
    latest_metadata = generated_root / f"{target_date}_latest_metadata.json"
    summary_json = generated_root / "multiple_generator_formula_summary.json"

    _write_csv(snapshot_csv, fieldnames, rows)
    _write_csv(latest_csv, fieldnames, rows)

    metadata = {
        "status": "ok",
        "site_id": "ZTRIC",
        "site_type": "multiple_generator",
        "mode": "asset_ai_schedule_contract_aggregation",
        "date": target_date,
        "run_time": target_time.replace(":", "-"),
        "snapshot_block": snapshot_block,
        "effective_time": effective_dt.strftime("%H:%M"),
        "effective_delay_minutes": int((effective_dt - target_dt).total_seconds() // 60),
        "raw_prefix": raw_day_prefix,
        "snapshot_csv_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_csv.name}",
        "snapshot_metadata_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{snapshot_metadata.name}",
        "latest_csv_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_csv.name}",
        "latest_metadata_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{latest_metadata.name}",
        "summary_key": f"{schedule_prefix.rstrip('/')}/{target_date}/{summary_json.name}",
        "rows": len(rows),
        "contract": contract,
        "asset_inputs": asset_inputs,
        "weather_summary": weather.get("prompt_text") or weather.get("summary") or "",
        "capacity_guards": {
            "asset_schedule_mw_lte_asset_ac_capacity_mw": True,
            "buyer_schedule_mw_lte_buyer_schedule_capacity_mw": True,
            "total_schedule_mw_lte_currently_scheduling_capacity_mw": True,
        },
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    summary = {
        "ok": True,
        "site": "ZTRIC",
        "mode": "asset_ai_schedule_contract_aggregation",
        "target_date": target_date,
        "target_time": target_time,
        "asset_inputs": asset_inputs,
        "contract": contract,
        "uploaded_files": 5,
    }
    snapshot_metadata.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    latest_metadata.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    summary_json.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    storage.upload_file(bucket, metadata["snapshot_csv_key"], snapshot_csv, content_type="text/csv")
    storage.upload_file(bucket, metadata["latest_csv_key"], latest_csv, content_type="text/csv")
    storage.upload_json(bucket, metadata["snapshot_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["latest_metadata_key"], metadata)
    storage.upload_json(bucket, metadata["summary_key"], summary)
    return metadata



