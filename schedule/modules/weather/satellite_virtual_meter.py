"""satellite_virtual_meter.py

Provides a high-reliability Satellite Solar Radiation Virtual Meter fallback
for utility-scale solar plants when physical SCADA meter telemetry is missing,
delayed, or interrupted (e.g. RTU outage, logger reboot, SFTP delay).

Calculates synthetic plant generation (P_virtual) from satellite-derived
Global Tilted Irradiance (GTI/POA), applies Sandia thermal derating,
and enforces conservative CERC/DSM safety floors to protect against
unobserved passing cloud dips during monsoon regimes.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from typing import Any

import requests
import config
from modules.weather import time_features


_DAY_CACHE: dict[tuple, dict[str, list]] = {}


def fetch_satellite_day_profile(
    target_date: dt.date,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> dict[str, list]:
    """Fetch and cache a full day of hourly satellite irradiance from Open-Meteo."""
    lat = float(latitude if latitude is not None else config.PLANT_LAT)
    lon = float(longitude if longitude is not None else config.PLANT_LON)
    t = float(tilt if tilt is not None else getattr(config, "PLANT_TILT_DEG", 20.0))
    az = float(azimuth if azimuth is not None else getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 0.0))
    om_azimuth = az if abs(az) <= 90 else (az - 180.0)

    cache_key = (target_date.isoformat(), round(lat, 4), round(lon, 4), round(t, 1), round(om_azimuth, 1))
    if cache_key in _DAY_CACHE:
        return _DAY_CACHE[cache_key]

    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
    base_url = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"

    now = dt.datetime.now()
    days_diff = (now.date() - target_date).days

    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "hourly": [
            "shortwave_radiation",
            "direct_normal_irradiance",
            "global_tilted_irradiance",
            "temperature_2m",
            "cloud_cover",
        ],
        "tilt": t,
        "azimuth": om_azimuth,
        "timezone": "Asia/Kolkata",
    }
    if api_key:
        params["apikey"] = api_key

    if 0 <= days_diff <= 7:
        params["past_days"] = min(7, max(1, days_diff + 1))
        params["forecast_days"] = 1
        query_url = base_url
    elif days_diff > 7:
        date_str = target_date.strftime("%Y-%m-%d")
        params["start_date"] = date_str
        params["end_date"] = date_str
        query_url = "https://archive-api.open-meteo.com/v1/archive"
    else:
        params["forecast_days"] = 2
        query_url = base_url

    try:
        resp = requests.get(query_url, params=params, timeout=12)
        if resp.status_code == 200:
            payload = resp.json()
            hourly = payload.get("hourly", {})
            _DAY_CACHE[cache_key] = hourly
            return hourly
    except Exception as exc:
        print(f"  [WARN] Satellite solar radiation query failed ({exc})")

    empty_hourly: dict[str, list] = {"time": [], "global_tilted_irradiance": [], "shortwave_radiation": [], "direct_normal_irradiance": [], "temperature_2m": [], "cloud_cover": []}
    return empty_hourly


def get_satellite_irradiance_for_timestamp(
    reference_time: dt.datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> dict[str, Any]:
    """Fetch satellite solar radiation interpolated for any reference timestamp."""
    hourly = fetch_satellite_day_profile(
        target_date=reference_time.date(),
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
    )

    times = hourly.get("time", [])
    gtis = hourly.get("global_tilted_irradiance", [])
    ghis = hourly.get("shortwave_radiation", [])
    dnis = hourly.get("direct_normal_irradiance", [])
    temps = hourly.get("temperature_2m", [])
    clouds = hourly.get("cloud_cover", [])

    ref_target_str = reference_time.strftime("%Y-%m-%dT%H")
    best_idx = None
    for idx, t_str in enumerate(times):
        if t_str.startswith(ref_target_str):
            best_idx = idx
            break

    if best_idx is not None and best_idx < len(gtis):
        minute_fraction = reference_time.minute / 60.0
        curr_gti = float(gtis[best_idx] if gtis[best_idx] is not None else 0.0)
        next_gti = float(gtis[min(len(gtis) - 1, best_idx + 1)] if best_idx + 1 < len(gtis) and gtis[best_idx + 1] is not None else curr_gti)
        interp_gti = curr_gti + minute_fraction * (next_gti - curr_gti)

        curr_temp = float(temps[best_idx] if best_idx < len(temps) and temps[best_idx] is not None else 25.0)
        next_temp = float(temps[min(len(temps) - 1, best_idx + 1)] if best_idx + 1 < len(temps) and temps[best_idx + 1] is not None else curr_temp)
        interp_temp = curr_temp + minute_fraction * (next_temp - curr_temp)

        curr_cloud = float(clouds[best_idx] if best_idx < len(clouds) and clouds[best_idx] is not None else 0.0)
        curr_ghi = float(ghis[best_idx] if best_idx < len(ghis) and ghis[best_idx] is not None else 0.0)
        curr_dni = float(dnis[best_idx] if best_idx < len(dnis) and dnis[best_idx] is not None else 0.0)

        return {
            "status": "ok",
            "gti": round(max(0.0, interp_gti), 2),
            "ghi": round(max(0.0, curr_ghi), 2),
            "dni": round(max(0.0, curr_dni), 2),
            "temperature": round(interp_temp, 1),
            "cloud_cover": round(curr_cloud, 1),
            "source": "open-meteo-satellite",
        }

    return {
        "status": "unavailable",
        "gti": 0.0,
        "ghi": 0.0,
        "dni": 0.0,
        "temperature": 25.0,
        "cloud_cover": 0.0,
        "source": "none",
    }


def fetch_satellite_irradiance_at_cutoff(
    reference_time: dt.datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> dict[str, Any]:
    """Fetch satellite solar radiation for the reference cutoff time."""
    return get_satellite_irradiance_for_timestamp(
        reference_time=reference_time,
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
    )


def calculate_virtual_generation_mw(
    gti_w_per_m2: float,
    temperature_c: float = 25.0,
    module_temp_c: float | None = None,
    plant_capacity_mw: float | None = None,
    dc_capacity_mw: float | None = None,
    performance_ratio: float | None = None,
) -> float:
    """Calculate synthetic plant power (MW) from Plane-of-Array irradiance."""
    cap_mw = float(plant_capacity_mw if plant_capacity_mw is not None else config.PLANT_CAPACITY_MW)
    dc_mw = float(dc_capacity_mw if dc_capacity_mw is not None else getattr(config, "PLANT_DC_CAPACITY_MW", cap_mw * 1.074))
    pr = float(performance_ratio if performance_ratio is not None else getattr(config, "PERFORMANCE_RATIO", 0.78))

    if gti_w_per_m2 <= 5.0:
        return 0.0

    # Cell temperature derating:
    # If explicit module temperature is available from sensor, use it.
    # Otherwise apply Sandia PV model: T_cell = T_amb + (POA * 0.03)
    if module_temp_c is not None and module_temp_c > -40.0:
        t_cell = module_temp_c
    else:
        t_cell = temperature_c + (gti_w_per_m2 * 0.03)

    # Silicon monocrystalline thermal coefficient: -0.38% / deg C
    thermal_derate = 1.0 - (0.0038 * (t_cell - 25.0))
    p_virtual = dc_mw * (gti_w_per_m2 / 1000.0) * thermal_derate * pr
    return round(max(0.0, min(cap_mw, p_virtual)), 3)


def impute_missing_generation_from_radiation(
    timestamp: dt.datetime,
    ground_poa: float | None = None,
    ground_ghi: float | None = None,
    ambient_temp: float | None = None,
    module_temp: float | None = None,
    plant_capacity_mw: float | None = None,
    dc_capacity_mw: float | None = None,
    performance_ratio: float | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> tuple[float, str]:
    """
    Impute active power generation (MW) for a missing or NA SCADA block.

    Priority cascade:
      1. On-site pyranometer POA (W/m2) if available and >= 0.0.
      2. On-site pyranometer GHI (W/m2) if available and >= 0.0.
      3. Satellite Global Tilted Irradiance (GTI/POA) from Open-Meteo.

    Returns:
      (virtual_mw, source_name)
      source_name in {"ground_poa", "ground_ghi", "satellite_solar_radiation"}
    """
    # 1. On-site Pyranometer POA
    if ground_poa is not None and ground_poa >= 0.0:
        mw = calculate_virtual_generation_mw(
            gti_w_per_m2=ground_poa,
            temperature_c=ambient_temp if ambient_temp is not None else 25.0,
            module_temp_c=module_temp,
            plant_capacity_mw=plant_capacity_mw,
            dc_capacity_mw=dc_capacity_mw,
            performance_ratio=performance_ratio,
        )
        return mw, "ground_poa"

    # 2. On-site Pyranometer GHI
    if ground_ghi is not None and ground_ghi >= 0.0:
        mw = calculate_virtual_generation_mw(
            gti_w_per_m2=ground_ghi,
            temperature_c=ambient_temp if ambient_temp is not None else 25.0,
            module_temp_c=module_temp,
            plant_capacity_mw=plant_capacity_mw,
            dc_capacity_mw=dc_capacity_mw,
            performance_ratio=performance_ratio,
        )
        return mw, "ground_ghi"

    # 3. Satellite Solar Radiation GTI Fallback
    sat_info = get_satellite_irradiance_for_timestamp(
        reference_time=timestamp,
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
    )
    sat_gti = sat_info.get("gti", 0.0)
    sat_temp = sat_info.get("temperature", ambient_temp if ambient_temp is not None else 25.0)
    mw = calculate_virtual_generation_mw(
        gti_w_per_m2=sat_gti,
        temperature_c=sat_temp,
        module_temp_c=module_temp,
        plant_capacity_mw=plant_capacity_mw,
        dc_capacity_mw=dc_capacity_mw,
        performance_ratio=performance_ratio,
    )
    return mw, "satellite_solar_radiation"


def build_satellite_virtual_intraday_state(
    reference_time: dt.datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
    plant_capacity_mw: float | None = None,
    dc_capacity_mw: float | None = None,
    performance_ratio: float | None = None,
) -> dict[str, Any]:
    """Assemble a synthetic intraday_state dictionary matching the physical meter schema."""
    sat_info = fetch_satellite_irradiance_at_cutoff(
        reference_time=reference_time,
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
    )

    cap_mw = float(plant_capacity_mw if plant_capacity_mw is not None else config.PLANT_CAPACITY_MW)
    dc_mw = float(dc_capacity_mw if dc_capacity_mw is not None else getattr(config, "PLANT_DC_CAPACITY_MW", cap_mw * 1.074))
    pr = float(performance_ratio if performance_ratio is not None else getattr(config, "PERFORMANCE_RATIO", 0.78))

    gti = sat_info.get("gti", 0.0)
    temp = sat_info.get("temperature", 25.0)
    cloud = sat_info.get("cloud_cover", 0.0)
    p_virtual = calculate_virtual_generation_mw(
        gti,
        temp,
        plant_capacity_mw=cap_mw,
        dc_capacity_mw=dc_mw,
        performance_ratio=pr,
    )

    time_feats = time_features.compute_time_features(reference_time)
    elev = time_feats["solar_elevation_deg"]

    # Calculate clear-sky theoretical envelope at this solar elevation
    if elev > 3.0:
        clear_sky_envelope = cap_mw * math.sin(math.radians(elev)) * pr
        clear_sky_envelope = max(0.05, clear_sky_envelope)
        raw_ratio = p_virtual / clear_sky_envelope
    else:
        raw_ratio = 1.0

    # Safety Guardrails:
    # 1. Dawn Exemption Rule: low sun angles (< 20 deg) have low wake-up ratios
    if elev < 20.0:
        live_residual_factor = 1.0
        regime = "satellite virtual meter (morning dawn ascent)"
    else:
        # 2. Base Clearness Factor from Satellite Virtual Power
        base_factor = max(0.20, min(1.05, raw_ratio))

        # 3. Afternoon Monsoon Cloud Damping:
        # During Indian monsoon (June - September) after 13:00 IST, satellite models
        # can under-estimate rapid localized convective cloud bursts.
        # Apply a 0.85 conservative factor to protect against CERC over-forecast penalties.
        if 13 <= reference_time.hour <= 16 and reference_time.month in (6, 7, 8, 9):
            base_factor = round(min(base_factor, base_factor * 0.88), 3)

        live_residual_factor = max(0.20, min(1.05, base_factor))

        if cloud >= 70 or live_residual_factor < 0.50:
            regime = "satellite virtual meter (overcast / heavy cloud attenuation)"
        elif cloud >= 40 or live_residual_factor < 0.75:
            regime = "satellite virtual meter (broken clouds / moderate irradiance)"
        else:
            regime = "satellite virtual meter (clear / strong irradiance)"

    summary = (
        f"Physical SCADA meter telemetry is offline up to {reference_time.strftime('%Y-%m-%d %H:%M')}. "
        f"Activated Satellite Solar Radiation Virtual Meter: "
        f"Satellite GTI={gti:.1f} W/m2, Cloud Cover={cloud:.0f}%, "
        f"Synthetic P_virtual={p_virtual:.3f} MW, Regime='{regime}', "
        f"Live Residual Factor={live_residual_factor:.3f}."
    )

    return {
        "regime": regime,
        "live_residual_factor": round(live_residual_factor, 3),
        "fluctuation_flag": bool(cloud > 50),
        "latest_mw": p_virtual,
        "last_frozen_mw": p_virtual,
        "summary": summary,
        "is_virtual_meter": True,
        "telemetry_source": "SATELLITE_VIRTUAL_METER",
        "satellite_gti": gti,
        "satellite_cloud_cover": cloud,
    }
