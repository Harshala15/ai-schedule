"""Open-Meteo ECMWF weather helpers for Bhupalpally."""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any
import os
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from zoneinfo import ZoneInfo

import config


def _get_forecast_url() -> str:
    key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
    if key:
        return "https://customer-api.open-meteo.com/v1/forecast"
    return "https://api.open-meteo.com/v1/forecast"


URL = _get_forecast_url()
MINUTELY_15_VARIABLES = [
    "global_tilted_irradiance",
    "global_tilted_irradiance_instant",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "shortwave_radiation",
    "temperature_2m",
    "precipitation",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "wind_speed_10m",
    "relative_humidity_2m",
    "sunshine_duration",
    "is_day",
]
HOURLY_VARIABLES = MINUTELY_15_VARIABLES
CACHE_DIR = Path(tempfile.gettempdir()) / "bhupalpally_openmeteo_cache"


@dataclass(frozen=True)
class WeatherHour:
    time: dt.datetime
    global_tilted_irradiance_instant: float | None
    temperature_2m: float | None
    precipitation: float | None
    cloud_cover_low: float | None
    cloud_cover_total: float | None = None
    cloud_cover_mid: float | None = None
    cloud_cover_high: float | None = None
    effective_cloud_cover: float | None = None
    direct_normal_irradiance_instant: float | None = None
    diffuse_radiation_instant: float | None = None
    shortwave_radiation_instant: float | None = None
    wind_speed_10m: float | None = None
    relative_humidity_2m: float | None = None
    sunshine_duration: float | None = None
    is_day: float | None = None
    temp_cell_sandia_c: float | None = None
    temp_derate_factor: float | None = None


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ensure_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _request_payload(latitude: float, longitude: float, start_date: str, end_date: str, timezone: str, tilt: float, azimuth: float) -> dict:
    effective_azimuth = config.to_openmeteo_azimuth(azimuth)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "minutely_15": MINUTELY_15_VARIABLES,
        "models": "best_match",
        "timezone": timezone,
        "tilt": tilt,
        "azimuth": effective_azimuth,
        "start_date": start_date,
        "end_date": end_date,
    }

    try:
        import requests_cache
        from retry_requests import retry
        import openmeteo_requests

        cache_session = requests_cache.CachedSession(str(CACHE_DIR), expire_after=1800)
        retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
        client = openmeteo_requests.Client(session=retry_session)
        response = client.weather_api(URL, params=params)[0]
        min15 = response.Minutely15()
        interval = int(min15.Interval())
        time_start = dt.datetime.fromtimestamp(int(min15.Time()), tz=dt.timezone.utc)
        values = [min15.Variables(i).ValuesAsNumpy().tolist() for i in range(len(MINUTELY_15_VARIABLES))]
        rows = []
        num_records = len(values[0]) if values else 0
        for index in range(num_records):
            ts = time_start + dt.timedelta(seconds=interval * index)
            row_dict = {"time": ts.isoformat()}
            for v_idx, var_name in enumerate(MINUTELY_15_VARIABLES):
                row_dict[var_name] = values[v_idx][index] if v_idx < len(values) and index < len(values[v_idx]) else None
            rows.append(row_dict)
        return {
            "source": "openmeteo_requests_15min",
            "latitude": response.Latitude(),
            "longitude": response.Longitude(),
            "elevation": response.Elevation(),
            "timezone": (response.Timezone().decode() if hasattr(response.Timezone(), "decode") else str(response.Timezone())),
            "timezone_abbreviation": (response.TimezoneAbbreviation().decode() if hasattr(response.TimezoneAbbreviation(), "decode") else str(response.TimezoneAbbreviation())),
            "utc_offset_seconds": response.UtcOffsetSeconds(),
            "rows": rows,
        }
    except Exception:
        # Lightweight REST fallback for 15-min solar API
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "minutely_15": ",".join(MINUTELY_15_VARIABLES),
            "models": "best_match",
            "timezone": timezone,
            "tilt": tilt,
            "azimuth": effective_azimuth,
            "start_date": start_date,
            "end_date": end_date,
        }
        api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
        if api_key:
            params["apikey"] = api_key
        query = urlencode(params)
        req = Request(f"{_get_forecast_url()}?{query}", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as handle:
            payload = json.loads(handle.read().decode("utf-8"))
        min15_data = payload.get("minutely_15", payload.get("hourly", {}))
        time_values = min15_data.get("time", [])
        rows = []
        for index, time_value in enumerate(time_values):
            row_dict = {"time": time_value}
            for var_name in MINUTELY_15_VARIABLES:
                arr = min15_data.get(var_name, [None])
                row_dict[var_name] = arr[index] if index < len(arr) else None
            rows.append(row_dict)
        payload["rows"] = rows
        payload["source"] = "open_meteo_rest_15min"
        return payload


def _parse_row_time(raw_time: Any, timezone: str) -> dt.datetime | None:
    if raw_time is None:
        return None
    text = str(raw_time).strip()
    if not text:
        return None
    tz = _ensure_timezone(timezone)
    try:
        if "T" in text:
            parsed = dt.datetime.fromisoformat(text)
        else:
            parsed = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except ValueError:
        return None


def _format_value(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if suffix:
        return f"{value:.2f}{suffix}"
    return f"{value:.2f}"


def _summarize_rows(rows: list[WeatherHour]) -> str:
    if not rows:
        return "No ECMWF weather rows were returned for the requested horizon."

    first = rows[0]
    last = rows[-1]
    avg_ghi = sum(v.global_tilted_irradiance_instant or 0.0 for v in rows) / len(rows)
    avg_dni = sum(v.direct_normal_irradiance_instant or 0.0 for v in rows) / len(rows)
    avg_dhi = sum(v.diffuse_radiation_instant or 0.0 for v in rows) / len(rows)
    avg_cloud_tot = sum(v.cloud_cover_total if v.cloud_cover_total is not None else (v.cloud_cover_low or 0.0) for v in rows) / len(rows)
    avg_eff_cloud = sum(v.effective_cloud_cover if v.effective_cloud_cover is not None else (v.cloud_cover_low or 0.0) for v in rows) / len(rows)
    avg_wind = sum(v.wind_speed_10m or 0.0 for v in rows) / len(rows)
    avg_humidity = sum(v.relative_humidity_2m or 0.0 for v in rows) / len(rows)
    avg_t_cell = sum(v.temp_cell_sandia_c or 25.0 for v in rows) / len(rows)
    avg_derate = sum(v.temp_derate_factor or 1.0 for v in rows) / len(rows)
    max_precip = max((v.precipitation or 0.0) for v in rows)
    temp_start = first.temperature_2m
    temp_end = last.temperature_2m
    ghi_trend = (last.global_tilted_irradiance_instant or 0.0) - (first.global_tilted_irradiance_instant or 0.0)
    trend_label = "rising" if ghi_trend > 15 else ("falling" if ghi_trend < -15 else "roughly steady")
    clearness = "Direct-beam dominant (Clear sky)" if avg_dni > 450 else ("Diffuse-dominant (Cloud scattering)" if avg_dhi > avg_dni else "Mixed radiation")

    lines = [
        "ECMWF / Open-Meteo weather forecast for the next revision horizon:",
        f"- Window: {first.time.strftime('%Y-%m-%d %H:%M')} to {last.time.strftime('%Y-%m-%d %H:%M')}",
        f"- Global tilted irradiance (15m mean): start={_format_value(first.global_tilted_irradiance_instant)} W/m², "
        f"end={_format_value(last.global_tilted_irradiance_instant)} W/m², trend={trend_label}, "
        f"avg={avg_ghi:.1f} W/m²",
        f"- Direct vs Diffuse: DNI avg={avg_dni:.1f} W/m², DHI avg={avg_dhi:.1f} W/m² ({clearness})",
        f"- Optical Cloud Attenuation: effective avg={avg_eff_cloud:.1f}% (total cloud avg={avg_cloud_tot:.1f}%)",
        f"- Thermal & Wind: ambient temp avg={((temp_start or 25.0) + (temp_end or 25.0)) / 2.0:.1f}°C, wind avg={avg_wind:.1f} m/s -> Sandia PV cell temp avg={avg_t_cell:.1f}°C (thermal derate factor={avg_derate:.3f})",
        f"- Precipitation max={max_precip:.2f} mm, humidity avg={avg_humidity:.1f}%",
    ]
    return "\n".join(lines)


def fetch_ecmwf_weather_summary(
    latitude: float,
    longitude: float,
    reference_time: dt.datetime,
    hours_ahead: int,
    timezone: str = "Asia/Kolkata",
    tilt: float = 20.0,
    azimuth: float = 0.0,
) -> dict:
    """Fetch ECMWF weather data and reduce it to prompt-friendly text."""
    start_date = reference_time.date().isoformat()
    end_date = (reference_time + dt.timedelta(hours=max(1, hours_ahead))).date().isoformat()
    payload = _request_payload(latitude, longitude, start_date, end_date, timezone, tilt, azimuth)

    rows: list[WeatherHour] = []
    start = reference_time.astimezone(_ensure_timezone(timezone)) if reference_time.tzinfo else reference_time.replace(tzinfo=_ensure_timezone(timezone))
    end = start + dt.timedelta(hours=max(1, hours_ahead))
    for row in payload.get("rows", []):
        row_time = _parse_row_time(row.get("time"), timezone)
        if row_time is None or row_time < start or row_time > end:
            continue
        c_low = _coerce_float(row.get("cloud_cover_low")) or 0.0
        c_tot = _coerce_float(row.get("cloud_cover")) or c_low
        c_mid = _coerce_float(row.get("cloud_cover_mid")) or 0.0
        c_high = _coerce_float(row.get("cloud_cover_high")) or 0.0
        # Effective optical cloud cover: high cirrus is 85% transparent (0.15 weight)
        eff_cloud = round(min(100.0, c_low * 1.0 + c_mid * 0.70 + c_high * 0.15), 1)

        # 15-minute integrated mean irradiance preferred over single-second instant
        gti = _coerce_float(row.get("global_tilted_irradiance"))
        if gti is None:
            gti = _coerce_float(row.get("global_tilted_irradiance_instant"))
        dni = _coerce_float(row.get("direct_normal_irradiance"))
        if dni is None:
            dni = _coerce_float(row.get("direct_normal_irradiance_instant"))
        dhi = _coerce_float(row.get("diffuse_radiation"))
        if dhi is None:
            dhi = _coerce_float(row.get("diffuse_radiation_instant"))
        sw = _coerce_float(row.get("shortwave_radiation"))
        if sw is None:
            sw = _coerce_float(row.get("shortwave_radiation_instant"))

        temp_amb = _coerce_float(row.get("temperature_2m")) or 25.0
        wind_spd = _coerce_float(row.get("wind_speed_10m")) or 2.5
        # Sandia cell temperature model: T_cell = T_ambient + GTI * exp(-3.56 - 0.075 * wind_speed)
        thermal_coupling = math.exp(-3.56 - 0.075 * wind_spd)
        t_cell = round(temp_amb + ((gti or 0.0) * thermal_coupling), 1)
        derate_factor = round(max(0.75, 1.0 - 0.0038 * max(0.0, t_cell - 25.0)), 4)

        rows.append(
            WeatherHour(
                time=row_time,
                global_tilted_irradiance_instant=gti,
                temperature_2m=temp_amb,
                precipitation=_coerce_float(row.get("precipitation")),
                cloud_cover_low=c_low,
                cloud_cover_total=c_tot,
                cloud_cover_mid=c_mid,
                cloud_cover_high=c_high,
                effective_cloud_cover=eff_cloud,
                direct_normal_irradiance_instant=dni,
                diffuse_radiation_instant=dhi,
                shortwave_radiation_instant=sw,
                wind_speed_10m=wind_spd,
                relative_humidity_2m=_coerce_float(row.get("relative_humidity_2m")),
                sunshine_duration=_coerce_float(row.get("sunshine_duration")),
                is_day=_coerce_float(row.get("is_day")),
                temp_cell_sandia_c=t_cell,
                temp_derate_factor=derate_factor,
            )
        )

    if not rows:
        return {
            "source": payload.get("source", "openmeteo"),
            "summary": "No ECMWF weather rows were returned for the requested horizon.",
            "rows": [],
            "prompt_text": "No ECMWF weather rows were returned for the requested horizon.",
        }

    summary = _summarize_rows(rows)
    prompt_lines = [summary, "Hourly weather rows:"]
    for item in rows:
        prompt_lines.append(
            f"- {item.time.strftime('%H:%M')}: "
            f"GTI={_format_value(item.global_tilted_irradiance_instant)} W/m² (15m mean), "
            f"DNI={_format_value(item.direct_normal_irradiance_instant)} W/m², "
            f"DHI={_format_value(item.diffuse_radiation_instant)} W/m², "
            f"T_amb={_format_value(item.temperature_2m, '°C')}, "
            f"T_cell={_format_value(item.temp_cell_sandia_c, '°C')} (derate={item.temp_derate_factor}), "
            f"wind={_format_value(item.wind_speed_10m, ' m/s')}, "
            f"cloud_eff={_format_value(item.effective_cloud_cover, '%')} (tot={_format_value(item.cloud_cover_total, '%')}), "
            f"precip={_format_value(item.precipitation, ' mm')}"
        )

    return {
        "source": payload.get("source", "openmeteo"),
        "summary": summary,
        "rows": [
            {
                "time": item.time.strftime("%Y-%m-%d %H:%M"),
                "global_tilted_irradiance_instant": item.global_tilted_irradiance_instant,
                "temperature_2m": item.temperature_2m,
                "precipitation": item.precipitation,
                "cloud_cover_low": item.cloud_cover_low,
                "cloud_cover_total": item.cloud_cover_total,
                "cloud_cover_mid": item.cloud_cover_mid,
                "cloud_cover_high": item.cloud_cover_high,
                "effective_cloud_cover": item.effective_cloud_cover,
                "direct_normal_irradiance_instant": item.direct_normal_irradiance_instant,
                "diffuse_radiation_instant": item.diffuse_radiation_instant,
                "wind_speed_10m": item.wind_speed_10m,
                "relative_humidity_2m": item.relative_humidity_2m,
                "sunshine_duration": item.sunshine_duration,
                "is_day": item.is_day,
                "temp_cell_sandia_c": item.temp_cell_sandia_c,
                "temp_derate_factor": item.temp_derate_factor,
            }
            for item in rows
        ],
        "prompt_text": "\n".join(prompt_lines),
    }
