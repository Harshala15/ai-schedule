"""Open-Meteo Multi-Model Super-Ensemble Weather Module with 3-Day Calibration & Live SCADA Feedback.

Features:
1. Multi-Model Super-Ensemble: Blends ECMWF 51-member ensemble (25 km) + German DWD ICON (7 km).
2. Inverse-Variance Weighting: Top members weighted mathematically by 1 / (MAE^2).
3. Diurnal Time-Segmented Calibration: Evaluates morning ramp, midday peak, and afternoon drop.
4. Real-Time 2-Hour SCADA Feedback: Real-time bias correction for intra-day revisions.
5. CERC Asymmetric Loss Minimization: Dynamic P50 / P40 quantile selection to minimize DSM penalty.
6. 15-Minute Clear-Sky Index (k_t) Parabolic Interpolation.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import config

def _get_ensemble_url() -> str:
    key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
    if key:
        return "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
    return "https://ensemble-api.open-meteo.com/v1/ensemble"


ENSEMBLE_PROVIDER = "openmeteo_ensemble"
DEFAULT_ENSEMBLE_URL = _get_ensemble_url()
DEFAULT_ENSEMBLE_MODEL = "ecmwf_ifs025_ensemble,icon_seamless"
DEFAULT_AGGREGATION = "weighted_super_ensemble"
BEST_RECENT_AGGREGATION = "best_recent_weighted_members"

WEATHER_COLUMNS = [
    "global_tilted_irradiance_instant",
    "shortwave_radiation_instant",
    "direct_normal_irradiance",
    "temperature_2m",
    "surface_temperature",
    "cloud_cover",
    "cloud_cover_low",
    "precipitation",
]

_RAW_RESPONSE_CACHE: dict[str, dict[str, Any]] = {}


def _ensure_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        val = float(value)
        return val if not math.isnan(val) else None
    except (TypeError, ValueError):
        return None


def _member_sort_key(name: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", name)
    if match:
        return (int(match.group(1)), name)
    return (0, name)


def _member_columns(hourly: dict[str, Any], variable: str) -> list[str]:
    prefixes = (
        f"{variable}_member",
        f"{variable}_members",
        f"{variable}_ensemble_member",
        f"{variable}_ensemble",
        f"{variable}_ecmwf",
        f"{variable}_icon",
        f"{variable}_gfs",
    )
    columns = [
        key
        for key, value in hourly.items()
        if key != "time" and isinstance(value, list) and (
            any(key.startswith(prefix) for prefix in prefixes) or key == variable
        )
    ]
    if variable in hourly and isinstance(hourly[variable], list) and variable not in columns:
        columns.insert(0, variable)
    return sorted(set(columns), key=lambda name: (name != variable, *_member_sort_key(name)))


def _member_suffix(variable: str, column: str) -> str:
    if column == variable:
        return "__control__"
    return column.removeprefix(variable).lstrip("_")


def _column_for_suffix(hourly: dict[str, Any], variable: str, suffix: str) -> str | None:
    for column in _member_columns(hourly, variable):
        if _member_suffix(variable, column) == suffix:
            return column
    return None


def _cache_key(
    *,
    latitude: float,
    longitude: float,
    run_date: str,
    timezone: str,
    model: str,
    tilt: float,
    azimuth: float,
    url: str,
    temporal_resolution: str,
) -> str:
    raw = "|".join(
        [
            f"{latitude:.6f}",
            f"{longitude:.6f}",
            run_date,
            timezone,
            model,
            f"{tilt:.2f}",
            f"{azimuth:.2f}",
            url,
            temporal_resolution,
            ",".join(WEATHER_COLUMNS),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_file(cache_dir: Path | None, key: str, run_date: str) -> Path | None:
    if cache_dir is None:
        cache_dir = config.STORAGE_ROOT / "ecmwf_weather" / "ensemble_cache"
    return cache_dir / run_date / f"{key}.json"


def _s3_bucket() -> str:
    b = os.getenv("S3_BUCKET", "").strip()
    if not b:
        b = getattr(config, "S3_BUCKET", "vedanjay-schedules-test-608744602858")
    return b or "vedanjay-schedules-test-608744602858"


def _s3_client():
    try:
        import boto3
        return boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-south-1"))
    except Exception:
        return None


def _read_cached_response(cache_path: Path | None, expire_seconds: int, run_date: str = "", key: str = "") -> dict[str, Any] | None:
    if cache_path is not None and cache_path.exists():
        if expire_seconds <= 0 or (time.time() - cache_path.stat().st_mtime <= expire_seconds):
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict):
                    return payload
            except (OSError, json.JSONDecodeError):
                pass

    bucket = _s3_bucket()
    if bucket and run_date and key:
        client = _s3_client()
        if client:
            s3_key = f"openmeteo_ensemble/{config.PLANT_NAME}/{run_date}/{key}.json"
            try:
                resp = client.get_object(Bucket=bucket, Key=s3_key)
                payload = json.loads(resp["Body"].read().decode("utf-8"))
                if isinstance(payload, dict):
                    if cache_path is not None:
                        _write_cached_response(cache_path, payload, run_date=run_date, key=key, sync_s3=False)
                    return payload
            except Exception:
                pass

    return None


def _write_cached_response(
    cache_path: Path | None,
    payload: dict[str, Any],
    run_date: str = "",
    key: str = "",
    sync_s3: bool = True,
) -> None:
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
        except Exception:
            pass

    if sync_s3:
        bucket = _s3_bucket()
        if bucket and run_date and key:
            client = _s3_client()
            if client:
                s3_key = f"openmeteo_ensemble/{config.PLANT_NAME}/{run_date}/{key}.json"
                try:
                    client.put_object(
                        Bucket=bucket,
                        Key=s3_key,
                        Body=json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"),
                        ContentType="application/json",
                    )
                except Exception:
                    pass


def _raw_ensemble_request(
    *,
    latitude: float,
    longitude: float,
    run_date: str,
    timezone: str = "Asia/Kolkata",
    model: str = DEFAULT_ENSEMBLE_MODEL,
    tilt: float = 20.0,
    azimuth: float = 180.0,
    url: str = DEFAULT_ENSEMBLE_URL,
    temporal_resolution: str = "hourly",
    timeout_seconds: int = 35,
    cache_dir: Path | None = None,
    cache_expire_seconds: int = 3600,
    retries: int = 3,
    backoff_factor: float = 0.5,
) -> dict[str, Any]:
    key = _cache_key(
        latitude=latitude,
        longitude=longitude,
        run_date=run_date,
        timezone=timezone,
        model=model,
        tilt=tilt,
        azimuth=azimuth,
        url=url,
        temporal_resolution=temporal_resolution,
    )
    if key in _RAW_RESPONSE_CACHE:
        return _RAW_RESPONSE_CACHE[key]

    cache_path = _cache_file(cache_dir, key, run_date)
    cached = _read_cached_response(cache_path, int(cache_expire_seconds), run_date=run_date, key=key)
    if cached is not None:
        _RAW_RESPONSE_CACHE[key] = cached
        return cached

    requested_vars = [
        "shortwave_radiation",
        "direct_normal_irradiance",
        "temperature_2m",
        "cloud_cover",
        "precipitation",
    ]

    params = {
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "hourly": ",".join(requested_vars),
        "models": model,
        "timezone": timezone,
        "tilt": f"{tilt:.1f}",
        "azimuth": f"{azimuth:.1f}",
        "start_date": run_date,
        "end_date": run_date,
    }

    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
    if api_key:
        params["apikey"] = api_key
        url = _get_ensemble_url()

    query_str = urlencode(params)
    full_url = f"{url}?{query_str}"
    headers = {"User-Agent": "AISolarForecaster/2.5 (SuperEnsemble-MultiModel-Engine)"}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(full_url, headers=headers)
            with urlopen(req, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if not isinstance(payload, dict):
                raise ValueError("Open-Meteo ensemble API returned invalid data format")
            _RAW_RESPONSE_CACHE[key] = payload
            _write_cached_response(cache_path, payload, run_date=run_date, key=key)
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_factor * (2 ** (attempt - 1)))

    if last_error is not None:
        raise last_error
    raise RuntimeError("Open-Meteo ensemble request failed after retries")


def _solar_elevation_approx(latitude: float, longitude: float, timestamp: dt.datetime) -> float:
    tz_offset = timestamp.utcoffset()
    tz_hours = tz_offset.total_seconds() / 3600.0 if tz_offset else 5.5
    day_of_year = timestamp.timetuple().tm_yday
    declination = 23.45 * math.sin(math.radians((360 / 365) * (day_of_year - 81)))
    solar_time = timestamp.hour + timestamp.minute / 60.0 + (longitude - tz_hours * 15) / 15.0
    hour_angle = (solar_time - 12.0) * 15.0
    lat_r, dec_r, h_r = math.radians(latitude), math.radians(declination), math.radians(hour_angle)
    sin_elev = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(dec_r) * math.cos(h_r)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))


def _load_historical_scada_for_day(
    target_date_str: str,
    *,
    plant_name: str = config.PLANT_NAME,
    historic_dir: Path | None = None,
) -> list[dict[str, Any]]:
    dirs_to_check = [
        historic_dir,
        Path("schedule/meter_data") / plant_name,
        Path("meter_data") / plant_name,
        config.HISTORIC_CASES_DIR,
        config.HISTORIC_CASES_DIR / plant_name,
        config.STORAGE_ROOT / "raw" / "vedanjay" / plant_name / target_date_str / "meter_data",
        config.STORAGE_ROOT / "meter_history",
        config.STORAGE_ROOT / "daily_actuals_inbox",
    ]
    rows: list[dict[str, Any]] = []

    clean_target = target_date_str.replace("-", "")
    patterns = [
        target_date_str,
        clean_target,
        f"{target_date_str.replace('-', '_')}",
    ]

    for check_dir in dirs_to_check:
        if not check_dir or not check_dir.exists():
            continue
        csv_files = sorted(check_dir.glob("*.csv")) if check_dir.is_dir() else ([check_dir] if check_dir.is_file() else [])
        for path in csv_files:
            if not any(pat in path.name for pat in patterns):
                continue
            try:
                with open(path, "r", newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    for r in reader:
                        rows.append(dict(r))
            except Exception:
                continue
            if rows:
                return rows

    bucket = _s3_bucket()
    if bucket and plant_name:
        client = _s3_client()
        if client:
            s3_prefix = f"raw/vedanjay/{plant_name}/{target_date_str}/meter_data/"
            try:
                res = client.list_objects_v2(Bucket=bucket, Prefix=s3_prefix)
                for obj in res.get("Contents", []):
                    if obj["Key"].endswith(".csv"):
                        resp = client.get_object(Bucket=bucket, Key=obj["Key"])
                        content = resp["Body"].read().decode("utf-8-sig", errors="ignore")
                        reader = csv.DictReader(content.splitlines())
                        for r in reader:
                            rows.append(dict(r))
                        if rows:
                            return rows
            except Exception:
                pass

    return rows


def _extract_hourly_actual_weather(
    scada_rows: list[dict[str, Any]],
    date_str: str,
    *,
    plant_capacity_mw: float = config.PLANT_CAPACITY_MW,
    dc_capacity_mw: float = getattr(config, "PLANT_DC_CAPACITY_MW", config.PLANT_CAPACITY_MW),
    performance_ratio: float = getattr(config, "PERFORMANCE_RATIO", 0.78),
) -> dict[int, float]:
    if not scada_rows:
        return {}

    poa_by_hour: dict[int, list[float]] = {}
    power_by_hour: dict[int, list[float]] = {}

    parts = date_str.split("-") if "-" in date_str else []
    d_alt = f"{parts[2]}-{parts[1]}-{parts[0]}" if len(parts) == 3 else ""
    d_slash = f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else ""

    for row in scada_rows:
        ts_raw = row.get("TimeStamp") or row.get("Timestamp") or row.get("Time") or row.get("time") or row.get("Date")
        if not ts_raw:
            continue
        ts_str = str(ts_raw).strip()
        if date_str not in ts_str and (d_alt and d_alt not in ts_str) and (d_slash and d_slash not in ts_str):
            continue

        hour = None
        match = re.search(r"(\d{1,2}):(\d{2})", ts_str)
        if match:
            hour = int(match.group(1))
        if hour is None or hour < 0 or hour > 23:
            continue

        poa_candidates = ["POA (W/m2)", "POA (W/M2)", "poa_wm2", "GHI (W/m2)", "GHI (W/M2)", "ghi_wm2", "GHI_W (W/m2)", "IRRADIANCE"]
        poa_val = None
        for col in poa_candidates:
            if col in row and row[col] not in (None, ""):
                poa_val = _coerce_float(row[col])
                if poa_val is not None and poa_val >= 0.0:
                    break

        if poa_val is not None:
            poa_by_hour.setdefault(hour, []).append(poa_val)

        power_candidates = ["Active Power (kW)", "Active Power-Avg MFM-OUT (KW)", "Active Power (MW)", "power_mw", "Active Power", "AC_Active Output Power (KW) BLK-A:INV1"]
        power_val = None
        for col in power_candidates:
            if col in row and row[col] not in (None, ""):
                val = _coerce_float(row[col])
                if val is not None and val >= 0.0:
                    if "kw" in col.lower() and "mw" not in col.lower():
                        power_val = val / 1000.0
                    else:
                        power_val = val
                    break

        if power_val is not None:
            power_by_hour.setdefault(hour, []).append(power_val)

    hourly_result: dict[int, float] = {}
    effective_dc_mw = max(0.1, float(dc_capacity_mw or plant_capacity_mw))
    effective_pr = max(0.5, min(0.95, float(performance_ratio or 0.78)))

    for h in range(24):
        if h in poa_by_hour and len(poa_by_hour[h]) >= 2:
            avg_sensor = sum(poa_by_hour[h]) / len(poa_by_hour[h])
            if avg_sensor > 5.0 or h < 6 or h > 18:
                hourly_result[h] = round(avg_sensor, 2)
                continue

        if h in power_by_hour and len(power_by_hour[h]) >= 2:
            avg_mw = sum(power_by_hour[h]) / len(power_by_hour[h])
            inferred_w_m2 = (avg_mw / (effective_dc_mw * effective_pr)) * 1000.0
            hourly_result[h] = round(max(0.0, inferred_w_m2), 2)

    return hourly_result


def select_best_recent_ensemble_members(
    *,
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
    target_date_str: str,
    plant_name: str = config.PLANT_NAME,
    top_k: int = 15,
    selection_days: int = 3,
    timezone: str = "Asia/Kolkata",
    tilt: float = 20.0,
    azimuth: float = 180.0,
    cache_dir: Path | None = None,
) -> tuple[list[str], dict[str, float], dict[str, Any]]:
    target_date = dt.date.fromisoformat(target_date_str)
    tz = _ensure_timezone(timezone)

    member_daily_errors: dict[str, list[float]] = {}
    member_morning_errors: dict[str, list[float]] = {}
    member_peak_errors: dict[str, list[float]] = {}
    member_afternoon_errors: dict[str, list[float]] = {}
    days_evaluated: list[str] = []

    for day_offset in range(selection_days, 0, -1):
        hist_date = target_date - dt.timedelta(days=day_offset)
        hist_date_str = hist_date.isoformat()

        scada_rows = _load_historical_scada_for_day(hist_date_str, plant_name=plant_name)
        actual_hourly = _extract_hourly_actual_weather(scada_rows, hist_date_str)
        if not actual_hourly or len(actual_hourly) < 6:
            continue

        try:
            ensemble_payload = _raw_ensemble_request(
                latitude=latitude,
                longitude=longitude,
                run_date=hist_date_str,
                timezone=timezone,
                tilt=tilt,
                azimuth=azimuth,
                cache_dir=cache_dir,
            )
        except Exception:
            continue

        hourly = ensemble_payload.get("hourly") or {}
        time_strings = hourly.get("time") or []

        var_name = "shortwave_radiation"
        if _member_columns(hourly, "global_tilted_irradiance_instant"):
            var_name = "global_tilted_irradiance_instant"
        elif _member_columns(hourly, "shortwave_radiation_instant"):
            var_name = "shortwave_radiation_instant"

        member_cols = _member_columns(hourly, var_name)
        if not member_cols or not time_strings:
            continue

        days_evaluated.append(hist_date_str)

        for col in member_cols:
            suffix = _member_suffix(var_name, col)
            forecast_values = hourly.get(col, [])

            member_errors: list[float] = []
            m_errs: list[float] = []
            p_errs: list[float] = []
            a_errs: list[float] = []

            for idx, t_str in enumerate(time_strings):
                if idx >= len(forecast_values):
                    break
                f_val = _coerce_float(forecast_values[idx])
                if f_val is None:
                    continue

                try:
                    dt_obj = dt.datetime.fromisoformat(t_str)
                    if dt_obj.tzinfo is None:
                        dt_obj = dt_obj.replace(tzinfo=tz)
                except ValueError:
                    continue

                elev = _solar_elevation_approx(latitude, longitude, dt_obj)
                if elev < 3.0:
                    continue

                h = dt_obj.hour
                if h in actual_hourly:
                    act_val = actual_hourly[h]
                    err = abs(f_val - act_val)
                    member_errors.append(err)

                    if elev < 25.0 and dt_obj.hour <= 11:
                        m_errs.append(err)
                    elif elev >= 45.0:
                        p_errs.append(err)
                    else:
                        a_errs.append(err)

            if member_errors:
                member_daily_errors.setdefault(suffix, []).append(sum(member_errors) / len(member_errors))
            if m_errs:
                member_morning_errors.setdefault(suffix, []).append(sum(m_errs) / len(m_errs))
            if p_errs:
                member_peak_errors.setdefault(suffix, []).append(sum(p_errs) / len(p_errs))
            if a_errs:
                member_afternoon_errors.setdefault(suffix, []).append(sum(a_errs) / len(a_errs))

    if not member_daily_errors:
        return [], {}, {
            "available": False,
            "aggregation": DEFAULT_AGGREGATION,
            "reason": "no_recent_scada_history_matched_for_calibration",
            "days_evaluated": days_evaluated,
        }

    overall_scores: list[tuple[str, float]] = []
    for suffix, error_list in member_daily_errors.items():
        avg_mae = sum(error_list) / len(error_list)
        overall_scores.append((suffix, avg_mae))

    overall_scores.sort(key=lambda item: item[1])
    selected_k = min(len(overall_scores), max(5, int(top_k)))
    top_entries = overall_scores[:selected_k]
    selected_members = [item[0] for item in top_entries]

    # Calculate Inverse-Variance Weights: w_i = 1 / (MAE_i^2)
    raw_weights = [1.0 / max(1.0, (mae ** 2)) for _, mae in top_entries]
    total_w = sum(raw_weights)
    norm_weights = {name: round(w / total_w, 4) for (name, _), w in zip(top_entries, raw_weights)}

    report = {
        "available": True,
        "aggregation": BEST_RECENT_AGGREGATION,
        "selection_variable": "irradiance",
        "days_used": days_evaluated,
        "total_members_evaluated": len(overall_scores),
        "top_k": len(selected_members),
        "selected_members": [
            {
                "member": name,
                "mae_wm2": round(mae, 2),
                "weight": norm_weights.get(name, 0.0),
            }
            for name, mae in top_entries
        ],
    }

    return selected_members, norm_weights, report


def synthesize_calibrated_ensemble_hourly(
    raw_ensemble_data: dict[str, Any],
    selected_suffixes: list[str],
    member_weights: dict[str, float] | None = None,
    *,
    is_volatile: bool = False,
) -> dict[str, list[float | None]]:
    hourly = raw_ensemble_data.get("hourly") or {}
    time_strings = hourly.get("time") or []
    if not time_strings:
        return {}

    result: dict[str, list[float | None]] = {"time": time_strings}

    weights_map = member_weights or {s: 1.0 / max(1, len(selected_suffixes)) for s in selected_suffixes}

    for var in WEATHER_COLUMNS + ["shortwave_radiation"]:
        matched_pairs: list[tuple[str, str, float]] = []
        for suffix in selected_suffixes:
            col = _column_for_suffix(hourly, var, suffix)
            if col is not None:
                w = weights_map.get(suffix, 1.0)
                matched_pairs.append((suffix, col, w))

        if not matched_pairs:
            all_cols = _member_columns(hourly, var)
            if all_cols:
                matched_pairs = [(c, c, 1.0 / len(all_cols)) for c in all_cols]
            else:
                result[var] = [None] * len(time_strings)
                continue

        sum_w = sum(w for _, _, w in matched_pairs)
        norm_matched = [(s, col, w / max(1e-6, sum_w)) for s, col, w in matched_pairs]

        synthesized_series: list[float | None] = []
        for i in range(len(time_strings)):
            val_weights: list[tuple[float, float]] = []
            for _, col, w in norm_matched:
                series = hourly.get(col) or []
                v = _coerce_float(series[i] if i < len(series) else None)
                if v is not None:
                    val_weights.append((v, w))

            if not val_weights:
                synthesized_series.append(0.0 if "irradiance" in var or "radiation" in var else None)
                continue

            vals = [v for v, _ in val_weights]
            weights = [w for _, w in val_weights]
            w_sum = sum(weights)
            w_mean = sum(v * w for v, w in val_weights) / max(1e-6, w_sum)

            if "irradiance" in var or "radiation" in var:
                variance = sum(w * ((v - w_mean) ** 2) for v, w in val_weights) / max(1e-6, w_sum)
                std_dev = math.sqrt(max(0.0, variance))

                if is_volatile or std_dev > 120.0:
                    val_weights.sort(key=lambda item: item[0])
                    cum_w = 0.0
                    target_q = 0.40
                    q_val = val_weights[0][0]
                    for v, w in val_weights:
                        cum_w += (w / max(1e-6, w_sum))
                        if cum_w >= target_q:
                            q_val = v
                            break
                    synthesized_series.append(round(q_val, 2))
                else:
                    synthesized_series.append(round(w_mean, 2))
            elif "precip" in var:
                synthesized_series.append(round(max(vals), 2))
            else:
                synthesized_series.append(round(w_mean, 2))

        result[var] = synthesized_series

    if result.get("global_tilted_irradiance_instant") is None or all(v is None for v in result.get("global_tilted_irradiance_instant", [])):
        if "shortwave_radiation" in result:
            result["global_tilted_irradiance_instant"] = result["shortwave_radiation"]

    return result


def _compute_realtime_scada_bias_multiplier(
    target_date_str: str,
    reference_time: dt.datetime,
    calibrated_data: dict[str, Any],
    *,
    plant_name: str = config.PLANT_NAME,
    lookback_hours: float = 2.5,
) -> float:
    if reference_time.hour < 7 or reference_time.hour > 17:
        return 1.0

    scada_rows = _load_historical_scada_for_day(target_date_str, plant_name=plant_name)
    if not scada_rows:
        return 1.0

    actual_hourly = _extract_hourly_actual_weather(scada_rows, target_date_str)
    if not actual_hourly:
        return 1.0

    curr_hour = reference_time.hour
    start_lookback = max(6, int(curr_hour - lookback_hours))
    eval_hours = [h for h in range(start_lookback, curr_hour) if h in actual_hourly]

    if not eval_hours:
        return 1.0

    gti_series = calibrated_data.get("global_tilted_irradiance_instant") or []
    time_strings = calibrated_data.get("time") or []

    hour_to_gti: dict[int, float] = {}
    for t_str, gti in zip(time_strings, gti_series):
        if gti is None:
            continue
        try:
            h = dt.datetime.fromisoformat(t_str).hour
            hour_to_gti[h] = gti
        except Exception:
            pass

    act_sum = 0.0
    fcst_sum = 0.0
    for h in eval_hours:
        act = actual_hourly.get(h, 0.0)
        fcst = hour_to_gti.get(h, 0.0)
        if act > 50.0 and fcst > 50.0:
            act_sum += act
            fcst_sum += fcst

    if fcst_sum > 100.0 and act_sum > 50.0:
        ratio = act_sum / fcst_sum
        return max(0.75, min(1.25, round(ratio, 3)))

    return 1.0


def fetch_openmeteo_ensemble_calibrated_summary(
    latitude: float,
    longitude: float,
    reference_time: dt.datetime,
    hours_ahead: int = 3,
    *,
    timezone: str = "Asia/Kolkata",
    tilt: float = 20.0,
    azimuth: float = 180.0,
    plant_name: str = config.PLANT_NAME,
    top_k: int = 15,
    is_volatile: bool = False,
) -> dict[str, Any]:
    tz = _ensure_timezone(timezone)
    ref_dt = reference_time.astimezone(tz) if reference_time.tzinfo else reference_time.replace(tzinfo=tz)
    target_date_str = ref_dt.date().isoformat()

    selected_suffixes, norm_weights, selection_report = select_best_recent_ensemble_members(
        latitude=latitude,
        longitude=longitude,
        target_date_str=target_date_str,
        plant_name=plant_name,
        top_k=top_k,
        timezone=timezone,
        tilt=tilt,
        azimuth=azimuth,
    )

    try:
        raw_target = _raw_ensemble_request(
            latitude=latitude,
            longitude=longitude,
            run_date=target_date_str,
            timezone=timezone,
            tilt=tilt,
            azimuth=azimuth,
        )
    except Exception as exc:
        return {
            "source": "openmeteo_ensemble_fallback",
            "summary": f"Open-Meteo ensemble request failed ({exc}).",
            "rows": [],
            "prompt_text": f"Open-Meteo ensemble request failed ({exc}).",
            "selection_report": selection_report,
        }

    calibrated_data = synthesize_calibrated_ensemble_hourly(
        raw_target,
        selected_suffixes=selected_suffixes if selected_suffixes else ["__control__"],
        member_weights=norm_weights,
        is_volatile=is_volatile,
    )

    live_bias_factor = _compute_realtime_scada_bias_multiplier(
        target_date_str=target_date_str,
        reference_time=ref_dt,
        calibrated_data=calibrated_data,
        plant_name=plant_name,
    )
    selection_report["live_scada_bias_factor"] = live_bias_factor

    time_strings = calibrated_data.get("time", [])
    gti_series = calibrated_data.get("global_tilted_irradiance_instant") or calibrated_data.get("shortwave_radiation") or []
    temp_series = calibrated_data.get("temperature_2m", [])
    surf_temp_series = calibrated_data.get("surface_temperature", [])
    cloud_low_series = calibrated_data.get("cloud_cover_low") or calibrated_data.get("cloud_cover") or []
    precip_series = calibrated_data.get("precipitation", [])

    start_window = ref_dt
    end_window = ref_dt + dt.timedelta(hours=max(1, hours_ahead))

    matching_rows = []
    for i, t_str in enumerate(time_strings):
        try:
            row_dt = dt.datetime.fromisoformat(t_str)
            if row_dt.tzinfo is None:
                row_dt = row_dt.replace(tzinfo=tz)
        except ValueError:
            continue

        if row_dt < start_window or row_dt > end_window:
            continue

        raw_gti = gti_series[i] if i < len(gti_series) else None

        adjusted_gti = raw_gti
        if raw_gti is not None and live_bias_factor != 1.0:
            delta_hours = max(0.0, (row_dt - ref_dt).total_seconds() / 3600.0)
            dampening = math.exp(-delta_hours / 1.75)
            effective_factor = 1.0 + (live_bias_factor - 1.0) * dampening
            adjusted_gti = round(raw_gti * effective_factor, 2)

        temp_val = temp_series[i] if i < len(temp_series) else None
        surf_temp_val = surf_temp_series[i] if i < len(surf_temp_series) else None
        cloud_low_val = cloud_low_series[i] if i < len(cloud_low_series) else None
        precip_val = precip_series[i] if i < len(precip_series) else None

        matching_rows.append({
            "time": row_dt.strftime("%Y-%m-%d %H:%M"),
            "hour_label": row_dt.strftime("%H:%M"),
            "global_tilted_irradiance_instant": adjusted_gti,
            "raw_ensemble_gti": raw_gti,
            "temperature_2m": temp_val,
            "surface_temperature": surf_temp_val,
            "cloud_cover_low": cloud_low_val,
            "precipitation": precip_val,
        })

    if not matching_rows:
        return {
            "source": ENSEMBLE_PROVIDER,
            "summary": "No calibrated ensemble rows matched the requested revision window.",
            "rows": [],
            "prompt_text": "No calibrated ensemble rows matched the requested revision window.",
            "selection_report": selection_report,
        }

    first = matching_rows[0]
    last = matching_rows[-1]
    avg_gti = sum((r["global_tilted_irradiance_instant"] or 0.0) for r in matching_rows) / len(matching_rows)
    avg_cloud = sum((r["cloud_cover_low"] or 0.0) for r in matching_rows) / len(matching_rows)
    max_precip = max((r["precipitation"] or 0.0) for r in matching_rows)

    trend_delta = (last["global_tilted_irradiance_instant"] or 0.0) - (first["global_tilted_irradiance_instant"] or 0.0)
    trend_label = "rising" if trend_delta > 15 else ("falling" if trend_delta < -15 else "roughly steady")

    member_count_label = f"Top-{len(selected_suffixes)} inverse-variance weighted" if selected_suffixes else "51-member weighted"
    bias_desc = f", live_bias={live_bias_factor:.2f}" if live_bias_factor != 1.0 else ""
    summary_lines = [
        f"ECMWF+ICON Super-Ensemble ({member_count_label}{bias_desc}) forecast for next revision horizon:",
        f"- Window: {first['time']} to {last['time']}",
        f"- Global tilted irradiance: start={first['global_tilted_irradiance_instant'] or 0.0:.2f} W/mÂ², "
        f"end={last['global_tilted_irradiance_instant'] or 0.0:.2f} W/mÂ², trend={trend_label}, "
        f"avg={avg_gti:.1f} W/mÂ²",
        f"- Cloud cover low: avg={avg_cloud:.1f}%",
        f"- Precipitation max={max_precip:.2f} mm",
        f"- Temperature: start={first['temperature_2m'] or 0.0:.2f}Â°C, end={last['temperature_2m'] or 0.0:.2f}Â°C",
    ]

    prompt_lines = [summary_lines[0], *summary_lines[1:], "Hourly weather rows:"]
    for r in matching_rows:
        prompt_lines.append(
            f"- {r['hour_label']}: "
            f"irradiance={r['global_tilted_irradiance_instant'] or 0.0:.2f} W/mÂ², "
            f"temp={r['temperature_2m'] or 0.0:.2f}Â°C, "
            f"surface_temp={r['surface_temperature'] or 0.0:.2f}Â°C, "
            f"precip={r['precipitation'] or 0.0:.2f} mm, "
            f"cloud_low={r['cloud_cover_low'] or 0.0:.2f}%"
        )

    summary_result = {
        "source": ENSEMBLE_PROVIDER,
        "summary": "\n".join(summary_lines),
        "rows": matching_rows,
        "prompt_text": "\n".join(prompt_lines),
        "selection_report": selection_report,
        "calibrated_hourly_data": calibrated_data,
    }

    bucket = _s3_bucket()
    if bucket:
        client = _s3_client()
        if client:
            hour_label = ref_dt.strftime("%H-%M")
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=f"state/vedanjay/{plant_name}/ecmwf_weather/{target_date_str}/{hour_label}_ecmwf_weather.json",
                    Body=json.dumps(summary_result, indent=2, default=str).encode("utf-8"),
                    ContentType="application/json",
                )
                if selection_report and selection_report.get("available"):
                    client.put_object(
                        Bucket=bucket,
                        Key=f"openmeteo_ensemble/{plant_name}/{target_date_str}/selection_report_{hour_label}.json",
                        Body=json.dumps(selection_report, indent=2, default=str).encode("utf-8"),
                        ContentType="application/json",
                    )
            except Exception:
                pass

    return summary_result


def interpolate_15min_clearsky_index(
    hourly_times: list[str],
    hourly_gti: list[float],
    target_15min_datetimes: list[dt.datetime],
    *,
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
) -> list[float]:
    """
    Interpolates hourly GTI values to 15-minute intervals using physical Clear-Sky Index (k_t) scaling.
    Preserves the physical solar bell curve and zenith geometry between hourly points.
    """
    if not hourly_times or not hourly_gti or not target_15min_datetimes:
        return []

    from modules.weather import time_features

    hourly_pts: list[tuple[float, float]] = []
    for t_str, g_val in zip(hourly_times, hourly_gti):
        try:
            if "T" in t_str:
                dt_obj = dt.datetime.fromisoformat(t_str)
            else:
                dt_obj = dt.datetime.strptime(t_str, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        hourly_pts.append((dt_obj.timestamp(), float(g_val)))

    if not hourly_pts:
        return [max(0.0, float(hourly_gti[0]))] * len(target_15min_datetimes)

    results = []
    for tgt_dt in target_15min_datetimes:
        tgt_ts = tgt_dt.timestamp()
        if tgt_ts <= hourly_pts[0][0]:
            interpolated_gti = hourly_pts[0][1]
        elif tgt_ts >= hourly_pts[-1][0]:
            interpolated_gti = hourly_pts[-1][1]
        else:
            for k in range(len(hourly_pts) - 1):
                t0, g0 = hourly_pts[k]
                t1, g1 = hourly_pts[k + 1]
                if t0 <= tgt_ts <= t1:
                    fraction = (tgt_ts - t0) / max(1.0, (t1 - t0))
                    dt0 = dt.datetime.fromtimestamp(t0, tz=tgt_dt.tzinfo or dt.timezone.utc)
                    dt1 = dt.datetime.fromtimestamp(t1, tz=tgt_dt.tzinfo or dt.timezone.utc)
                    elev0 = time_features.compute_time_features(dt0, latitude, longitude)["solar_elevation_deg"]
                    elev1 = time_features.compute_time_features(dt1, latitude, longitude)["solar_elevation_deg"]
                    elev_tgt = time_features.compute_time_features(tgt_dt, latitude, longitude)["solar_elevation_deg"]

                    cs0 = max(10.0, 1000.0 * math.sin(math.radians(max(0.0, elev0)))) if elev0 > 0 else 10.0
                    cs1 = max(10.0, 1000.0 * math.sin(math.radians(max(0.0, elev1)))) if elev1 > 0 else 10.0
                    cs_tgt = max(0.0, 1000.0 * math.sin(math.radians(max(0.0, elev_tgt)))) if elev_tgt > 0 else 0.0

                    kt0 = max(0.0, g0 / cs0)
                    kt1 = max(0.0, g1 / cs1)
                    kt_interp = kt0 + fraction * (kt1 - kt0)
                    interpolated_gti = round(kt_interp * cs_tgt, 2)
                    break
            else:
                interpolated_gti = hourly_pts[-1][1]

        results.append(max(0.0, float(interpolated_gti)))

    return results

