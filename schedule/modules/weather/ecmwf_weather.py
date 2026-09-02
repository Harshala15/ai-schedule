"""Open-Meteo ECMWF weather helpers for Bhupalpally."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from zoneinfo import ZoneInfo


URL = "https://api.open-meteo.com/v1/forecast"
MINUTELY_15_VARIABLES = [
    "global_tilted_irradiance_instant",
    "shortwave_radiation_instant",
    "direct_normal_irradiance",
    "temperature_2m",
    "cloud_cover",
]
HOURLY_VARIABLES = MINUTELY_15_VARIABLES
CACHE_DIR = Path(tempfile.gettempdir()) / "bhupalpally_openmeteo_cache"


@dataclass(frozen=True)
class WeatherHour:
    time: dt.datetime
    global_tilted_irradiance_instant: float | None
    temperature_2m: float | None
    surface_temperature: float | None
    precipitation: float | None
    cloud_cover_low: float | None


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
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "minutely_15": MINUTELY_15_VARIABLES,
        "models": "best_match",
        "timezone": timezone,
        "tilt": tilt,
        "azimuth": azimuth,
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
        for index in range(len(values[0]) if values else 0):
            ts = time_start + dt.timedelta(seconds=interval * index)
            rows.append(
                {
                    "time": ts.isoformat(),
                    MINUTELY_15_VARIABLES[0]: values[0][index],
                    MINUTELY_15_VARIABLES[1]: values[1][index] if len(values) > 1 else None,
                    MINUTELY_15_VARIABLES[2]: values[2][index] if len(values) > 2 else None,
                    MINUTELY_15_VARIABLES[3]: values[3][index] if len(values) > 3 else None,
                    MINUTELY_15_VARIABLES[4]: values[4][index] if len(values) > 4 else None,
                }
            )
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
        query = urlencode({
            "latitude": latitude,
            "longitude": longitude,
            "minutely_15": ",".join(MINUTELY_15_VARIABLES),
            "models": "best_match",
            "timezone": timezone,
            "tilt": tilt,
            "azimuth": azimuth,
            "start_date": start_date,
            "end_date": end_date,
        })
        req = urllib.request.Request(f"{URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=30) as handle:
            payload = json.loads(handle.read().decode("utf-8"))
        min15_data = payload.get("minutely_15", payload.get("hourly", {}))
        time_values = min15_data.get("time", [])
        rows = []
        for index, time_value in enumerate(time_values):
            rows.append(
                {
                    "time": time_value,
                    MINUTELY_15_VARIABLES[0]: min15_data.get(MINUTELY_15_VARIABLES[0], [None])[index] if index < len(min15_data.get(MINUTELY_15_VARIABLES[0], [])) else None,
                    MINUTELY_15_VARIABLES[1]: min15_data.get(MINUTELY_15_VARIABLES[1], [None])[index] if index < len(min15_data.get(MINUTELY_15_VARIABLES[1], [])) else None,
                    MINUTELY_15_VARIABLES[2]: min15_data.get(MINUTELY_15_VARIABLES[2], [None])[index] if index < len(min15_data.get(MINUTELY_15_VARIABLES[2], [])) else None,
                    MINUTELY_15_VARIABLES[3]: min15_data.get(MINUTELY_15_VARIABLES[3], [None])[index] if index < len(min15_data.get(MINUTELY_15_VARIABLES[3], [])) else None,
                    MINUTELY_15_VARIABLES[4]: min15_data.get(MINUTELY_15_VARIABLES[4], [None])[index] if index < len(min15_data.get(MINUTELY_15_VARIABLES[4], [])) else None,
                }
            )
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
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = dt.datetime.strptime(text, fmt)
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue

    try:
        parsed = dt.datetime.fromisoformat(text)
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
    avg_cloud = sum(v.cloud_cover_low or 0.0 for v in rows) / len(rows)
    max_precip = max((v.precipitation or 0.0) for v in rows)
    temp_start = first.temperature_2m
    temp_end = last.temperature_2m
    ghi_trend = (last.global_tilted_irradiance_instant or 0.0) - (first.global_tilted_irradiance_instant or 0.0)
    trend_label = "rising" if ghi_trend > 15 else ("falling" if ghi_trend < -15 else "roughly steady")

    lines = [
        "ECMWF / Open-Meteo weather forecast for the next revision horizon:",
        f"- Window: {first.time.strftime('%Y-%m-%d %H:%M')} to {last.time.strftime('%Y-%m-%d %H:%M')}",
        f"- Global tilted irradiance: start={_format_value(first.global_tilted_irradiance_instant)} W/m², "
        f"end={_format_value(last.global_tilted_irradiance_instant)} W/m², trend={trend_label}, "
        f"avg={avg_ghi:.1f} W/m²",
        f"- Cloud cover low: avg={avg_cloud:.1f}%",
        f"- Precipitation max={max_precip:.2f} mm",
        f"- Temperature: start={_format_value(temp_start, '°C')}, end={_format_value(temp_end, '°C')}",
    ]
    return "\n".join(lines)


def fetch_ecmwf_weather_summary(
    latitude: float,
    longitude: float,
    reference_time: dt.datetime,
    hours_ahead: int,
    timezone: str = "Asia/Kolkata",
    tilt: float = 20.0,
    azimuth: float = 180.0,
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
        rows.append(
            WeatherHour(
                time=row_time,
                global_tilted_irradiance_instant=_coerce_float(row.get("global_tilted_irradiance_instant")),
                temperature_2m=_coerce_float(row.get("temperature_2m")),
                surface_temperature=_coerce_float(row.get("surface_temperature")),
                precipitation=_coerce_float(row.get("precipitation")),
                cloud_cover_low=_coerce_float(row.get("cloud_cover_low")),
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
            f"irradiance={_format_value(item.global_tilted_irradiance_instant)} W/m², "
            f"temp={_format_value(item.temperature_2m, '°C')}, "
            f"surface_temp={_format_value(item.surface_temperature, '°C')}, "
            f"precip={_format_value(item.precipitation, ' mm')}, "
            f"cloud_low={_format_value(item.cloud_cover_low, '%')}"
        )

    return {
        "source": payload.get("source", "openmeteo"),
        "summary": summary,
        "rows": [
            {
                "time": item.time.strftime("%Y-%m-%d %H:%M"),
                "global_tilted_irradiance_instant": item.global_tilted_irradiance_instant,
                "temperature_2m": item.temperature_2m,
                "surface_temperature": item.surface_temperature,
                "precipitation": item.precipitation,
                "cloud_cover_low": item.cloud_cover_low,
            }
            for item in rows
        ],
        "prompt_text": "\n".join(prompt_lines),
    }
