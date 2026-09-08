"""air_quality.py

Open-Meteo Commercial Air Quality and Aerosol Optical Depth (AOD) Engine:
Queries customer-air-quality-api.open-meteo.com for:
- Aerosol Optical Depth (AOD 550nm)
- Dust concentration (ug/m3)
- PM2.5 and PM10 particulates (ug/m3)

Calculates the real-time atmospheric transmission factor to prevent chronic
over-forecasting penalties in Indian dry-season / hazy conditions.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from typing import Any

import requests

import config


def fetch_air_quality_aod(
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
    reference_time: dt.datetime | None = None,
    hours_ahead: int = 4,
) -> dict[str, Any]:
    """Fetches AOD, dust, and particulate metrics from Open-Meteo Air Quality API."""
    reference_time = reference_time or dt.datetime.now()
    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()

    base_url = "https://customer-air-quality-api.open-meteo.com/v1/air-quality" if api_key else "https://air-quality-api.open-meteo.com/v1/air-quality"

    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ["aerosol_optical_depth", "dust", "pm10", "pm2_5"],
        "timezone": "auto",
        "forecast_days": 2,
    }
    if api_key:
        params["apikey"] = api_key

    try:
        resp = requests.get(base_url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [WARN] Air Quality API fetch failed ({exc}), applying default standard atmospheric clarity.")
        return {
            "status": "fallback",
            "aod_avg": 0.25,
            "attenuation_factor": 1.0,
            "clarity_description": "Standard Clear-Sky (No AOD Data)",
            "hourly_aod": {},
        }

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    aod_vals = hourly.get("aerosol_optical_depth", [])
    dust_vals = hourly.get("dust", [])
    pm25_vals = hourly.get("pm2_5", [])
    pm10_vals = hourly.get("pm10", [])

    hourly_map: dict[str, dict[str, Any]] = {}
    valid_aod = []

    for i, t_str in enumerate(times):
        time_part = t_str.split("T")[-1][:5] if "T" in t_str else t_str[-5:]
        aod = aod_vals[i] if i < len(aod_vals) and aod_vals[i] is not None else 0.25
        dust = dust_vals[i] if i < len(dust_vals) and dust_vals[i] is not None else 0.0
        pm25 = pm25_vals[i] if i < len(pm25_vals) and pm25_vals[i] is not None else 0.0
        pm10 = pm10_vals[i] if i < len(pm10_vals) and pm10_vals[i] is not None else 0.0

        excess_aod = max(0.0, aod - 0.15)
        dust_factor = min(0.05, (dust / 500.0) * 0.05) if dust > 20.0 else 0.0
        aod_factor = max(0.82, round(1.0 - (excess_aod * 0.12 + dust_factor), 3))

        hourly_map[time_part] = {
            "aod": round(aod, 2),
            "dust_ug_m3": round(dust, 1),
            "pm25_ug_m3": round(pm25, 1),
            "pm10_ug_m3": round(pm10, 1),
            "attenuation_factor": aod_factor,
        }
        valid_aod.append(aod)

    avg_aod = round(sum(valid_aod) / len(valid_aod), 2) if valid_aod else 0.25
    avg_excess = max(0.0, avg_aod - 0.15)
    overall_attenuation = max(0.82, round(1.0 - (avg_excess * 0.12), 3))

    if avg_aod < 0.20:
        clarity = "Pristine Clear Sky (Minimal Particulate Attenuation)"
    elif avg_aod < 0.45:
        loss_pct = round((1.0 - overall_attenuation) * 100, 1)
        clarity = f"Moderate Haze (AOD={avg_aod:.2f}, -{loss_pct}% GHI Attenuation)"
    else:
        loss_pct = round((1.0 - overall_attenuation) * 100, 1)
        clarity = f"Heavy Particulate / Dust Haze (AOD={avg_aod:.2f}, -{loss_pct}% GHI Attenuation)"

    return {
        "status": "success",
        "aod_avg": avg_aod,
        "attenuation_factor": overall_attenuation,
        "clarity_description": clarity,
        "hourly_aod": hourly_map,
    }
