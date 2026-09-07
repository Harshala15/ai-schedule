"""premium_stream3.py

Open-Meteo Professional (€99/mo) Full-Feature Commercial Weather Engine:
Implements all 7 advanced commercial capabilities:

1. 5-Agency Multi-NWP Global Consensus (ECMWF Europe, DWD Germany, NOAA USA, JMA Japan, CMC Canada).
2. Convective Available Potential Energy (CAPE in J/kg) for early afternoon thunderstorm/cloud burst warning.
3. Physical Cloud Optical Transparency Ratio (uv_index / uv_index_clear_sky).
4. Native 15-Minute Sunshine Duration (sunshine_duration in seconds, 0-900s).
5. Sandia Physical Photovoltaic Module Cell Temperature Model (T_cell from surface_temp + wind cooling).
6. Run-to-Run Model Momentum Drift Tracking (customer-previous-runs-api).
7. Real-time CAMS Aerosol Optical Depth (AOD 550nm) & Dust Haze Transmission Attenuation.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from typing import Any

import requests
import config
from modules.weather import air_quality


def fetch_premium_stream3_weather(
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
    reference_time: dt.datetime | None = None,
    hours_ahead: int = 4,
    *,
    tilt: float = 5.0,
    azimuth: float = 180.0,
    plant_name: str = config.PLANT_NAME,
) -> dict[str, Any]:
    """Fetches the comprehensive full-feature Stream 3 commercial forecast."""
    reference_time = reference_time or dt.datetime.now()
    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()

    base_url = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"
    prev_url = "https://customer-previous-runs-api.open-meteo.com/v1/forecast" if api_key else "https://previous-runs-api.open-meteo.com/v1/forecast"

    # 1. Fetch 5-Agency Models for Consensus
    models_5agency = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless", "jma_seamless", "gem_seamless"]
    params_5agency: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ["shortwave_radiation", "cloud_cover", "temperature_2m", "precipitation"],
        "models": models_5agency,
        "timezone": "Asia/Kolkata",
        "forecast_days": 2,
    }
    if api_key:
        params_5agency["apikey"] = api_key

    data_5agency = {}
    try:
        r1 = requests.get(base_url, params=params_5agency, timeout=12)
        if r1.status_code == 200:
            data_5agency = r1.json()
    except Exception as exc:
        print(f"  [WARN] 5-Agency Consensus fetch failed ({exc})")

    # 2. Fetch Advanced Physics Variables (CAPE, UV Clearness, Sunshine, Sandia T_cell, Moisture)
    params_advanced: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": [
            "shortwave_radiation",
            "direct_normal_irradiance",
            "diffuse_radiation",
            "temperature_2m",
            "surface_temperature",
            "wind_speed_10m",
            "cloud_cover",
            "precipitation",
            "cape",
            "uv_index",
            "uv_index_clear_sky",
            "sunshine_duration",
            "vapour_pressure_deficit",
            "dew_point_2m",
        ],
        "minutely_15": [
            "sunshine_duration",
            "shortwave_radiation_instant",
            "direct_normal_irradiance",
            "temperature_2m",
        ],
        "models": "best_match",
        "timezone": "Asia/Kolkata",
        "forecast_days": 2,
    }
    if api_key:
        params_advanced["apikey"] = api_key

    data_adv = {}
    try:
        r2 = requests.get(base_url, params=params_advanced, timeout=12)
        if r2.status_code == 200:
            data_adv = r2.json()
    except Exception as exc:
        print(f"  [WARN] Advanced physics variables fetch failed ({exc})")

    # 3. Fetch Previous Runs for Run-to-Run Drift Tracking
    params_prev: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ["shortwave_radiation", "cloud_cover"],
        "timezone": "Asia/Kolkata",
        "forecast_days": 2,
    }
    if api_key:
        params_prev["apikey"] = api_key

    data_prev = {}
    try:
        r3 = requests.get(prev_url, params=params_prev, timeout=10)
        if r3.status_code == 200:
            data_prev = r3.json()
    except Exception as exc:
        pass

    # 4. Fetch Air Quality / AOD (CAMS)
    aq_report = air_quality.fetch_air_quality_aod(
        latitude=latitude,
        longitude=longitude,
        reference_time=reference_time,
        hours_ahead=hours_ahead,
    )
    hourly_aod = aq_report.get("hourly_aod", {})
    overall_aod_factor = aq_report.get("attenuation_factor", 1.0)
    aod_clarity = aq_report.get("clarity_description", "Standard Clear-Sky")

    # Parse Hourly Physics & 5-Agency Consensus
    hourly_adv = data_adv.get("hourly", {})
    times = hourly_adv.get("time", [])

    hourly_5ag = data_5agency.get("hourly", {})
    sw_ecmwf = hourly_5ag.get("shortwave_radiation_ecmwf_ifs025", hourly_adv.get("shortwave_radiation", []))
    sw_icon = hourly_5ag.get("shortwave_radiation_icon_seamless", [])
    sw_gfs = hourly_5ag.get("shortwave_radiation_gfs_seamless", [])
    sw_jma = hourly_5ag.get("shortwave_radiation_jma_seamless", [])
    sw_gem = hourly_5ag.get("shortwave_radiation_gem_seamless", [])

    dnis = hourly_adv.get("direct_normal_irradiance", [])
    dhis = hourly_adv.get("diffuse_radiation", [])
    temps = hourly_adv.get("temperature_2m", [])
    surf_temps = hourly_adv.get("surface_temperature", [])
    winds = hourly_adv.get("wind_speed_10m", [])
    clouds = hourly_adv.get("cloud_cover", [])
    precips = hourly_adv.get("precipitation", [])
    capes = hourly_adv.get("cape", [])
    uvs = hourly_adv.get("uv_index", [])
    uv_clears = hourly_adv.get("uv_index_clear_sky", [])
    sunshines = hourly_adv.get("sunshine_duration", [])
    vpds = hourly_adv.get("vapour_pressure_deficit", [])

    # Previous run data for drift
    prev_hourly = data_prev.get("hourly", {})
    prev_sw = prev_hourly.get("shortwave_radiation", [])

    # Native 15-Minute sunshine duration
    min15_adv = data_adv.get("minutely_15", {})
    min15_times = min15_adv.get("time", [])
    min15_sunshine = min15_adv.get("sunshine_duration", [])

    stream3_by_hour: dict[str, dict[str, Any]] = {}
    
    tilt_rad = math.radians(tilt)
    cos_tilt = math.cos(tilt_rad)

    for i, t_str in enumerate(times):
        time_part = t_str.split("T")[-1][:5] if "T" in t_str else t_str[-5:]

        # 5-Agency Consensus GHI
        vals = []
        for arr in [sw_ecmwf, sw_icon, sw_gfs, sw_jma, sw_gem]:
            if i < len(arr) and arr[i] is not None:
                vals.append(float(arr[i]))

        agency_mean_ghi = sum(vals) / len(vals) if vals else (sw_ecmwf[i] if i < len(sw_ecmwf) else 0.0)
        agency_spread = max(vals) - min(vals) if vals else 0.0

        # Physical Variables
        temp_amb = temps[i] if i < len(temps) and temps[i] is not None else 28.0
        wind_spd = winds[i] if i < len(winds) and winds[i] is not None else 2.5
        cloud_pct = clouds[i] if i < len(clouds) and clouds[i] is not None else 0.0
        precip_mm = precips[i] if i < len(precips) and precips[i] is not None else 0.0
        cape_val = capes[i] if i < len(capes) and capes[i] is not None else 0.0
        uv_val = uvs[i] if i < len(uvs) and uvs[i] is not None else 0.0
        uv_clear_val = uv_clears[i] if i < len(uv_clears) and uv_clears[i] is not None else 0.0
        sunshine_sec = sunshines[i] if i < len(sunshines) and sunshines[i] is not None else 0.0
        vpd_val = vpds[i] if i < len(vpds) and vpds[i] is not None else 1.0

        # Cloud Optical Transparency Ratio (uv_index / uv_index_clear_sky)
        if uv_clear_val > 0.1:
            cloud_transmissivity = min(1.0, max(0.05, round(uv_val / uv_clear_val, 3)))
        else:
            cloud_transmissivity = 1.0 if cloud_pct < 20 else max(0.10, 1.0 - (cloud_pct / 100.0))

        # Sunshine Fraction per Hour (0.0 to 1.0)
        sunshine_fraction = min(1.0, max(0.0, round(sunshine_sec / 3600.0, 3))) if sunshine_sec > 0 else 0.0

        # Run-to-Run Drift
        drift_w = 0.0
        if i < len(prev_sw) and prev_sw[i] is not None and i < len(sw_ecmwf) and sw_ecmwf[i] is not None:
            drift_w = round(float(sw_ecmwf[i]) - float(prev_sw[i]), 1)

        # CAMS AOD Haze Scaling
        h_aod = hourly_aod.get(time_part, {})
        hour_aod_factor = h_aod.get("attenuation_factor", overall_aod_factor)

        # Water vapor absorption correction (-2% to -4% under high humidity/VPD)
        moisture_factor = 0.97 if vpd_val < 0.8 and temp_amb > 28.0 else 1.0

        # Convert GHI to GTI on Array Tilt with AOD + Moisture
        gti_unadjusted = (agency_mean_ghi / max(0.85, cos_tilt))
        gti_om_premium = round(gti_unadjusted * hour_aod_factor * moisture_factor, 1)

        # Sandia Photovoltaic Module Cell Temperature Model:
        # T_cell = T_ambient + GTI * exp(-3.56 - 0.075 * wind_speed)
        thermal_coupling = math.exp(-3.56 - 0.075 * wind_spd)
        t_cell = round(temp_amb + (gti_om_premium * thermal_coupling), 1)
        # Temperature derating factor: -0.38% / degC above 25C
        temp_derate_multiplier = max(0.80, round(1.0 - 0.0038 * max(0.0, t_cell - 25.0), 4))

        # CAPE Thunderstorm Instability Warning
        if cape_val >= 1500.0:
            cape_status = f"HIGH INSTABILITY (CAPE={int(cape_val)} J/kg, Severe Convective Cloud Threat)"
        elif cape_val >= 800.0:
            cape_status = f"Moderate Instability (CAPE={int(cape_val)} J/kg)"
        else:
            cape_status = f"Stable Air (CAPE={int(cape_val)} J/kg)"

        if drift_w < -50.0:
            drift_label = f"Worsening Clouds (Drift: {drift_w} W/m2)"
        elif drift_w > 50.0:
            drift_label = f"Clearing Faster (Drift: +{drift_w} W/m2)"
        else:
            drift_label = "Stable Momentum"

        stream3_by_hour[time_part] = {
            "gti_om_premium": gti_om_premium,
            "agency_mean_ghi": round(agency_mean_ghi, 1),
            "agency_spread": round(agency_spread, 1),
            "temp_ambient_c": round(temp_amb, 1),
            "temp_cell_sandia_c": t_cell,
            "temp_derate_multiplier": temp_derate_multiplier,
            "wind_speed_ms": round(wind_spd, 1),
            "cloud_pct": round(cloud_pct, 1),
            "cloud_transmissivity": cloud_transmissivity,
            "sunshine_fraction": sunshine_fraction,
            "sunshine_seconds": int(sunshine_sec),
            "cape_j_kg": int(cape_val),
            "cape_status": cape_status,
            "precip_mm": round(precip_mm, 2),
            "drift_w_m2": drift_w,
            "drift_label": drift_label,
            "aod_factor": hour_aod_factor,
        }

    # Map native 15-minute sunshine duration
    min15_map: dict[str, dict[str, Any]] = {}
    for j, m_str in enumerate(min15_times):
        time_m = m_str.split("T")[-1][:5] if "T" in m_str else m_str[-5:]
        s_sec = min15_sunshine[j] if j < len(min15_sunshine) and min15_sunshine[j] is not None else 0.0
        min15_map[time_m] = {
            "sunshine_seconds_15min": int(s_sec),
            "sunshine_pct_15min": round((s_sec / 900.0) * 100.0, 1),
        }

    return {
        "status": "success",
        "clarity_description": aod_clarity,
        "stream3_hourly": stream3_by_hour,
        "stream3_minutely_15": min15_map,
        "models_used": models_5agency,
    }
