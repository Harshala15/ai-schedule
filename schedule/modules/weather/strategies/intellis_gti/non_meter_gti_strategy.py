"""non_meter_gti_strategy.py

Dedicated Global Tilted Irradiance (GTI) Calculation Engine for Non-Meter Solar Sites
(e.g., UPL, ANDAD, SIDDEHESH, GUGARIYAKHEDI, SAWDA, BALAKWADA, CME, EMIL).

When physical boundary SCADA telemetry is absent, this strategy computes the 96-block
GTI curve strictly from satellite-derived solar radiation (Satellite GTI), capturing
real observed cloud attenuation and atmospheric conditions.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    import config
except ImportError:
    config = None

from modules.weather.strategies.intellis_gti.base_gti_strategy import (
    BaseGTIStrategy,
    GTIForecastResult,
    compute_time_features,
)

_DAY_CACHE: dict[tuple, dict[str, list]] = {}


def fetch_satellite_day_profile(
    target_date: dt.date,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
) -> dict[str, list]:
    """Fetch and cache a full day of hourly satellite irradiance from Open-Meteo."""
    lat = float(latitude if latitude is not None else getattr(config, "PLANT_LAT", 24.0))
    lon = float(longitude if longitude is not None else getattr(config, "PLANT_LON", 75.0))
    t = float(tilt if tilt is not None else getattr(config, "PLANT_TILT_DEG", 20.0))
    az = float(azimuth if azimuth is not None else getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 0.0))
    om_azimuth = az if abs(az) <= 90 else (az - 180.0)

    cache_key = (target_date.isoformat(), round(lat, 4), round(lon, 4), round(t, 1), round(om_azimuth, 1))
    if cache_key in _DAY_CACHE:
        return _DAY_CACHE[cache_key]

    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip() or "jbThkFlLZSXZE3CU"
    base_url = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"

    now = dt.datetime.now()
    days_diff = (now.date() - target_date).days
    date_str = target_date.strftime("%Y-%m-%d")

    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": [
            "shortwave_radiation",
            "direct_normal_irradiance",
            "global_tilted_irradiance",
            "temperature_2m",
            "wind_speed_10m",
            "cloud_cover",
        ],
        "tilt": t,
        "azimuth": om_azimuth,
        "timezone": "Asia/Kolkata",
    }
    if api_key:
        params["apikey"] = api_key

    if days_diff > 30:
        query_url = "https://archive-api.open-meteo.com/v1/archive"
    else:
        query_url = base_url

    try:
        resp = requests.get(query_url, params=params, timeout=25)
        if resp.status_code == 200:
            payload = resp.json()
            hourly = payload.get("hourly", {})
            _DAY_CACHE[cache_key] = hourly
            return hourly
    except Exception as exc:
        print(f"  [WARN] Satellite solar radiation query failed ({exc})")

    empty_hourly: dict[str, list] = {
        "time": [],
        "global_tilted_irradiance": [],
        "shortwave_radiation": [],
        "direct_normal_irradiance": [],
        "temperature_2m": [],
        "wind_speed_10m": [],
        "cloud_cover": [],
    }
    return empty_hourly


def get_satellite_irradiance_for_timestamp(
    reference_time: dt.datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
    cutoff_time: dt.datetime | None = None,
) -> dict[str, Any]:
    """Retrieve interpolated satellite irradiance for a specific timestamp."""
    if cutoff_time is not None and reference_time > cutoff_time:
        return {"gti": 0.0, "source": "none", "status": "excluded_future_block", "temperature": 25.0, "cloud_cover": 0.0}

    hourly = fetch_satellite_day_profile(
        target_date=reference_time.date(),
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
    )

    times = hourly.get("time", [])
    gtis = hourly.get("global_tilted_irradiance", [])
    sws = hourly.get("shortwave_radiation", [])
    temps = hourly.get("temperature_2m", [])
    clouds = hourly.get("cloud_cover", [])
    winds = hourly.get("wind_speed_10m", [])

    time_str = reference_time.strftime("%Y-%m-%dT%H:00")
    for i, t in enumerate(times):
        if t == time_str:
            g = float(gtis[i]) if (i < len(gtis) and gtis[i] is not None) else 0.0
            sw = float(sws[i]) if (i < len(sws) and sws[i] is not None) else 0.0
            temp = float(temps[i]) if (i < len(temps) and temps[i] is not None) else 25.0
            cld = float(clouds[i]) if (i < len(clouds) and clouds[i] is not None) else 0.0
            w = float(winds[i]) if (i < len(winds) and winds[i] is not None) else 10.0
            effective_gti = max(g, sw)
            return {
                "gti": effective_gti,
                "temperature": temp,
                "cloud_cover": cld,
                "wind_speed": w,
                "source": "openmeteo_satellite",
            }

    return {"gti": 0.0, "temperature": 25.0, "cloud_cover": 0.0, "wind_speed": 10.0, "source": "none"}


def fetch_satellite_irradiance_at_cutoff(
    reference_time: dt.datetime,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
    cutoff_time: dt.datetime | None = None,
) -> dict[str, Any]:
    """Fetch satellite solar radiation for the reference cutoff time."""
    effective_cutoff = cutoff_time or reference_time
    return get_satellite_irradiance_for_timestamp(
        reference_time=reference_time,
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
        cutoff_time=effective_cutoff,
    )


def calculate_virtual_generation_mw(
    gti_w_per_m2: float,
    temperature_c: float = 25.0,
    module_temp_c: float | None = None,
    wind_speed_kmh: float = 10.0,
    plant_capacity_mw: float | None = None,
    dc_capacity_mw: float | None = None,
    performance_ratio: float | None = None,
    is_non_meter: bool = False,
) -> float:
    """Calculate synthetic plant power (MW) from Plane-of-Array irradiance."""
    cap_mw = float(plant_capacity_mw if plant_capacity_mw is not None else getattr(config, "PLANT_CAPACITY_MW", 10.0))
    dc_mw = float(dc_capacity_mw if dc_capacity_mw is not None else getattr(config, "PLANT_DC_CAPACITY_MW", cap_mw * 1.074))

    if gti_w_per_m2 <= 5.0:
        return 0.0

    if is_non_meter:
        wind_sp = float(wind_speed_kmh if wind_speed_kmh is not None else 10.0)
        t_cell = temperature_c + gti_w_per_m2 * math.exp(-3.47 - 0.0594 * wind_sp) + (gti_w_per_m2 / 1000.0) * 3.0
        thermal_derate = max(0.70, min(1.05, 1.0 - 0.0038 * (t_cell - 25.0)))

        target_pr = float(performance_ratio) if performance_ratio is not None and performance_ratio > 0.5 else 0.8300
        base_bop = target_pr / 0.905
        dynamic_pr = round(base_bop * thermal_derate, 4)
        p_virtual = dc_mw * (gti_w_per_m2 / 1000.0) * dynamic_pr
        return round(max(0.0, min(cap_mw, p_virtual)), 3)

    pr = float(performance_ratio if performance_ratio is not None else getattr(config, "PERFORMANCE_RATIO", 0.78))
    if module_temp_c is not None and module_temp_c > -40.0:
        t_cell = module_temp_c
    else:
        t_cell = temperature_c + (gti_w_per_m2 * 0.03)

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
    cutoff_time: dt.datetime | None = None,
    is_non_meter: bool = False,
) -> tuple[float, str]:
    """Impute generation when physical telemetry is missing."""
    if cutoff_time is not None and timestamp > cutoff_time:
        return 0.0, "future_block_excluded"

    if ground_poa is not None and ground_poa > 5.0:
        mw = calculate_virtual_generation_mw(
            gti_w_per_m2=ground_poa,
            temperature_c=ambient_temp or 25.0,
            module_temp_c=module_temp,
            plant_capacity_mw=plant_capacity_mw,
            dc_capacity_mw=dc_capacity_mw,
            performance_ratio=performance_ratio,
            is_non_meter=is_non_meter,
        )
        return mw, "ground_poa"

    sat_data = get_satellite_irradiance_for_timestamp(
        reference_time=timestamp,
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
        cutoff_time=cutoff_time,
    )
    sat_gti = sat_data.get("gti", 0.0)
    if sat_gti > 5.0:
        mw = calculate_virtual_generation_mw(
            gti_w_per_m2=sat_gti,
            temperature_c=sat_data.get("temperature", 25.0),
            module_temp_c=None,
            wind_speed_kmh=sat_data.get("wind_speed", 10.0),
            plant_capacity_mw=plant_capacity_mw,
            dc_capacity_mw=dc_capacity_mw,
            performance_ratio=performance_ratio,
            is_non_meter=is_non_meter,
        )
        return mw, "satellite_gti"

    return 0.0, "zero_floor"


def fetch_satellite_96block_profile(
    target_date: dt.date | str,
    latitude: float | None = None,
    longitude: float | None = None,
    tilt: float | None = None,
    azimuth: float | None = None,
    plant_capacity_mw: float | None = None,
    dc_capacity_mw: float | None = None,
    performance_ratio: float | None = None,
    cutoff_time: dt.datetime | None = None,
    is_non_meter: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate 96-block synthetic (meter_mw, meter_poa) arrays from satellite solar radiation API."""
    if isinstance(target_date, str):
        target_date = dt.datetime.strptime(target_date, "%Y-%m-%d").date()

    if cutoff_time is None and target_date == dt.date.today():
        cutoff_time = dt.datetime.now()

    hourly = fetch_satellite_day_profile(
        target_date=target_date,
        latitude=latitude,
        longitude=longitude,
        tilt=tilt,
        azimuth=azimuth,
    )

    times = hourly.get("time", [])
    gtis = hourly.get("global_tilted_irradiance", [])
    temps = hourly.get("temperature_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    clouds = hourly.get("cloud_cover", [])

    target_str = target_date.strftime("%Y-%m-%d")
    hour_data: dict[int, dict[str, float]] = {}
    for t_str, g_val, tmp_val, w_val, cld_val in zip(times, gtis, temps, winds if winds else [10.0] * len(times), clouds):
        if t_str.startswith(target_str):
            try:
                h = int(t_str.split("T")[1].split(":")[0])
                hour_data[h] = {
                    "gti": float(g_val if g_val is not None else 0.0),
                    "temp": float(tmp_val if tmp_val is not None else 25.0),
                    "wind": float(w_val if w_val is not None else 10.0),
                    "cloud": float(cld_val if cld_val is not None else 0.0),
                }
            except Exception:
                continue

    poa_96 = np.zeros(96, dtype=float)
    mw_96 = np.zeros(96, dtype=float)

    cap_mw = float(plant_capacity_mw if plant_capacity_mw is not None else getattr(config, "PLANT_CAPACITY_MW", 10.0))
    dc_mw = float(dc_capacity_mw if dc_capacity_mw is not None else getattr(config, "PLANT_DC_CAPACITY_MW", cap_mw * 1.074))
    pr = float(performance_ratio if performance_ratio is not None else getattr(config, "PERFORMANCE_RATIO", 0.78))

    for b in range(1, 97):
        end_min = b * 15
        end_hr, end_m = divmod(end_min, 60)
        if end_hr == 24:
            blk_end_dt = dt.datetime.combine(target_date + dt.timedelta(days=1), dt.time(0, 0))
        else:
            blk_end_dt = dt.datetime.combine(target_date, dt.time(end_hr, end_m))

        if cutoff_time is not None and blk_end_dt > cutoff_time:
            continue

        if b < 22 or b > 75:
            poa_96[b - 1] = 0.0
            mw_96[b - 1] = 0.0
            continue

        mid_min = (b - 0.5) * 15
        h = int(mid_min // 60)
        m = mid_min % 60
        frac = m / 60.0

        curr = hour_data.get(h, {"gti": 0.0, "temp": 25.0, "wind": 10.0, "cloud": 0.0})
        nxt = hour_data.get(min(23, h + 1), curr)

        gti_interp = curr["gti"] + frac * (nxt["gti"] - curr["gti"])
        temp_interp = curr["temp"] + frac * (nxt["temp"] - curr["temp"])
        wind_interp = curr["wind"] + frac * (nxt["wind"] - curr["wind"])

        p_virt = calculate_virtual_generation_mw(
            gti_w_per_m2=gti_interp,
            temperature_c=temp_interp,
            wind_speed_kmh=wind_interp,
            plant_capacity_mw=cap_mw,
            dc_capacity_mw=dc_mw,
            performance_ratio=pr,
            is_non_meter=is_non_meter,
        )

        poa_96[b - 1] = round(max(0.0, gti_interp), 2)
        mw_96[b - 1] = round(max(0.0, p_virt), 3)

    return mw_96, poa_96


class NonMeterGTIStrategy(BaseGTIStrategy):
    """Satellite-Derived GTI calculation strategy for sites lacking physical meter telemetry."""

    def __init__(
        self,
        plant_profile: Any,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        **kwargs: Any,
    ):
        super().__init__(plant_profile, api_key=api_key, cache_dir=cache_dir, **kwargs)
        self.tz = ZoneInfo("Asia/Kolkata")

    def compute_gti(
        self,
        target_date_str: str,
        cutoff_time: dt.datetime | None = None,
        **kwargs: Any,
    ) -> GTIForecastResult:
        """Compute 96-block Satellite GTI (W/m²), clear-sky POA, ambient temperature, and wind speed."""
        target_date = dt.datetime.strptime(target_date_str, "%Y-%m-%d").date()

        lat = getattr(self.profile, "latitude", None)
        lon = getattr(self.profile, "longitude", None)
        tilt = getattr(self.profile, "tilt_deg", None)
        az_om = getattr(self.profile, "azimuth_openmeteo", None)
        ac_mw = getattr(self.profile, "ac_capacity_mw", 10.0)
        dc_mw = getattr(self.profile, "dc_capacity_mw", ac_mw)
        pr = getattr(self.profile, "calibrated_pr", 0.8300) or 0.8300

        hourly = fetch_satellite_day_profile(
            target_date=target_date,
            latitude=lat,
            longitude=lon,
            tilt=tilt,
            azimuth=az_om,
        )

        times = hourly.get("time", [])
        gtis = hourly.get("global_tilted_irradiance", [])
        temps = hourly.get("temperature_2m", [])
        winds = hourly.get("wind_speed_10m", [])
        clouds = hourly.get("cloud_cover", [])

        hourly_idx = np.arange(0, 24, 1.0)
        b_idx = np.arange(0, 24, 0.25)

        target_str = target_date.strftime("%Y-%m-%d")
        day_gti = [0.0] * 24
        day_temp = [25.0] * 24
        day_wind = [2.5] * 24
        day_cloud = [0.0] * 24

        for t_str, g_val, tmp_val, w_val, cld_val in zip(
            times,
            gtis,
            temps,
            winds if winds else [2.5] * len(times),
            clouds if clouds else [0.0] * len(times),
        ):
            if t_str.startswith(target_str):
                try:
                    h = int(t_str.split("T")[1].split(":")[0])
                    if 0 <= h < 24:
                        day_gti[h] = float(g_val if g_val is not None else 0.0)
                        day_temp[h] = float(tmp_val if tmp_val is not None else 25.0)
                        day_wind[h] = float(w_val if w_val is not None else 2.5)
                        day_cloud[h] = float(cld_val if cld_val is not None else 0.0)
                except Exception:
                    continue

        gti_96 = np.interp(b_idx, hourly_idx, day_gti)
        amb_temp_96 = np.interp(b_idx, hourly_idx, day_temp)
        wind_speed_96 = np.interp(b_idx, hourly_idx, day_wind)

        gti_96[:23] = 0.0
        gti_96[76:] = 0.0
        gti_96 = np.maximum(0.0, np.round(gti_96, 1))

        cs_poa_96 = self.compute_clearsky_poa_96block(target_date_str)

        with np.errstate(divide="ignore", invalid="ignore"):
            blended_kt_96 = np.where(cs_poa_96 > 10.0, np.clip(gti_96 / cs_poa_96, 0.0, 1.25), 1.0)
        blended_kt_96 = np.nan_to_num(blended_kt_96, nan=1.0)

        mw_virt, poa_virt = fetch_satellite_96block_profile(
            target_date=target_date,
            latitude=lat,
            longitude=lon,
            tilt=tilt,
            azimuth=az_om,
            plant_capacity_mw=ac_mw,
            dc_capacity_mw=dc_mw,
            performance_ratio=pr,
            cutoff_time=cutoff_time,
            is_non_meter=True,
        )

        if np.max(poa_virt) > 50.0:
            gti_96 = np.round(poa_virt, 1)

        return GTIForecastResult(
            target_date=target_date_str,
            gti_96=gti_96,
            cs_poa_96=cs_poa_96,
            blended_kt_96=blended_kt_96,
            amb_temp_96=amb_temp_96,
            wind_speed_96=wind_speed_96,
            strategy_name="NON_METER_SATELLITE_GTI",
            telemetry_source="SATELLITE_SOLAR_RADIATION",
            metadata={
                "is_non_meter": True,
                "satellite_provider": "Open-Meteo Satellite Solar API",
                "selected_keys": ["SATELLITE_GTI"],
                "weights_map": {"SATELLITE_GTI": 1.0},
                "peak_satellite_gti_wm2": float(np.max(gti_96)),
                "cloud_cover_daily_avg": float(np.mean(day_cloud)),
                "virtual_mw_profile": mw_virt.tolist(),
            },
        )
