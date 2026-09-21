"""weather_fusion.py (DEPRECATED & SUPERSEDED BY IntellisEnsembleGTIAI)

The legacy 3-weather-stream logic (Stream 1: ECMWF, Stream 2: Open-Meteo Ensemble, Stream 3: Premium)
has been completely removed and superseded by the 143-member Open-Meteo Super-Ensemble
GTI AI engine (`modules.weather.intellis_ensemble_gti_ai.IntellisEnsembleGTIAI`).

This module provides clean delegation to IntellisEnsembleGTIAI for backward compatibility.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import config
from modules.weather import time_features


def _get_hourly_interpolated(
    hourly_map: dict[str, dict[str, Any]],
    t_label: str,
    target_dt: dt.datetime | None = None,
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
) -> dict[str, Any]:
    """Interpolates hourly weather rows to 15-minute resolution using clear-sky solar geometry."""
    if t_label in hourly_map:
        return hourly_map[t_label]
    try:
        hr = int(t_label.split(":")[0])
        mn = int(t_label.split(":")[1])
        h_floor = f"{hr:02d}:00"
        h_ceil = f"{(hr + 1):02d}:00"
        fraction = mn / 60.0

        d_floor = hourly_map.get(h_floor, {})
        d_ceil = hourly_map.get(h_ceil, d_floor)

        if not d_floor and not d_ceil:
            return {}
        if not d_floor:
            return d_ceil
        if not d_ceil:
            return d_floor

        interpolated = dict(d_floor)
        for k, v in d_floor.items():
            if isinstance(v, bool):
                interpolated[k] = v
            elif isinstance(v, (int, float)) and k in d_ceil and isinstance(d_ceil[k], (int, float)):
                interpolated[k] = round(v + fraction * (d_ceil[k] - v), 3)

        if target_dt is not None:
            elev_t = time_features.compute_time_features(target_dt, latitude, longitude)["solar_elevation_deg"]
            if elev_t < 3.0:
                interpolated["gti"] = 0.0
            else:
                dt_f = target_dt.replace(minute=0, second=0, microsecond=0)
                dt_c = dt_f + dt.timedelta(hours=1)
                elev_f = time_features.compute_time_features(dt_f, latitude, longitude)["solar_elevation_deg"]
                elev_c = time_features.compute_time_features(dt_c, latitude, longitude)["solar_elevation_deg"]

                cs_f = max(0.1, 1000.0 * max(0.0, elev_f / 90.0) ** 1.2)
                cs_c = max(0.1, 1000.0 * max(0.0, elev_c / 90.0) ** 1.2)
                cs_t = max(0.1, 1000.0 * max(0.0, elev_t / 90.0) ** 1.2)

                gti_f = float(d_floor.get("gti", 0.0))
                gti_c = float(d_ceil.get("gti", gti_f))

                kt_f = max(0.0, min(1.25, gti_f / cs_f))
                kt_c = max(0.0, min(1.25, gti_c / cs_c))
                kt_t = kt_f + fraction * (kt_c - kt_f)

                interpolated_gti = round(kt_t * cs_t, 1)
                interpolated["gti"] = interpolated_gti

        return interpolated
    except Exception:
        h_floor = f"{t_label[:2]}:00"
        return hourly_map.get(h_floor, {})


def fetch_dual_stream_weather_fusion(
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
    reference_time: dt.datetime = None,
    hours_ahead: int = 4,
    *,
    tilt: float | None = None,
    azimuth: float | None = None,
    plant_name: str = config.PLANT_NAME,
    is_volatile: bool = False,
) -> dict[str, Any]:
    """Replaced 3-weather-stream with unified IntellisEnsembleGTIAI engine."""
    reference_time = reference_time or dt.datetime.now()
    target_date_str = reference_time.strftime("%Y-%m-%d")

    try:
        from modules.weather.intellis_ensemble_gti_ai import IntellisEnsembleGTIAI, load_plant_profile
        prof = load_plant_profile(plant_name)
        ai_engine = IntellisEnsembleGTIAI(plant_profile=prof)
        sched = ai_engine.predict_96block_schedule(target_date_str)

        fused_rows = []
        for b in sched.get("blocks", []):
            fused_rows.append({
                "hour_label": b.get("time", ""),
                "gti_fused": float(b.get("intellis_gti", b.get("predicted_gti_wm2", 0.0))),
                "predicted_mw": float(b.get("intellis_mw", b.get("predicted_mw", 0.0))),
                "temp_c": 28.0,
                "precip_mm": 0.0,
                "cloud_pct": 0.0,
            })

        return {
            "source": "intellis_ensemble_gti_ai",
            "prompt_text": f"Unified Intellis 143-Member Super-Ensemble GTI AI Active for {plant_name}.",
            "fused_rows": fused_rows,
        }
    except Exception as exc:
        print(f"  [WARN] Intellis GTI engine delegation error: {exc}")
        return {
            "source": "intellis_fallback",
            "prompt_text": "Intellis GTI fallback.",
            "fused_rows": [],
        }
