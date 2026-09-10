"""weather_fusion.py

Tri-Stream Commercial Weather Architecture:
Provides 3 explicit, independent weather streams side-by-side for LLM analysis:

1. Stream 1: ECMWF 9 km High-Resolution Deterministic Model (microclimate rain cells & peak shape)
2. Stream 2: 91-Member Multi-Model Mega-Ensemble (51 ECMWF + 40 DWD ICON, probabilistic P40 cloud bounds)
3. Stream 3: Open-Meteo Premium Atmospheric & Multi-Agency Engine:
   - 5-Agency Global Consensus (ECMWF Europe, DWD Germany, NOAA GFS, JMA Japan, CMC Canada)
   - CAPE Convective Atmospheric Instability (J/kg) for early thunderstorm warning
   - Physical Cloud Optical Transmissivity Ratio (UV / UV_clear)
   - Native 15-Minute Sunshine Duration (seconds / fraction)
   - Sandia Photovoltaic Cell Temperature (T_cell) & Dynamic Silicon Thermal Derating
   - Run-to-Run Momentum Drift Tracking (Previous Model Runs API)
   - Real-time CAMS Aerosol Optical Depth (AOD 550nm) & Dust Haze Attenuation
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from dataclasses import dataclass
from typing import Any

import config
from modules.weather import air_quality, ecmwf_weather, openmeteo_ensemble, premium_stream3


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
    """Fetches 3 explicit standalone weather streams and formats them for LLM 3-stream arbitration."""
    reference_time = reference_time or dt.datetime.now()
    effective_tilt = tilt if tilt is not None else getattr(config, "PLANT_TILT_DEG", 15.0)
    raw_az = azimuth if azimuth is not None else getattr(config, "PLANT_ORIENTATION_FROM_SOUTH_DEG", getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 0.0))
    effective_azimuth = config.to_openmeteo_azimuth(raw_az)

    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
    is_commercial = bool(api_key)

    # 1. Fetch Stream 1: ECMWF 9 km Best-Match (Deterministic NWP)
    bm_report = {}
    try:
        bm_report = ecmwf_weather.fetch_ecmwf_weather_summary(
            latitude=latitude,
            longitude=longitude,
            reference_time=reference_time,
            hours_ahead=hours_ahead,
            tilt=effective_tilt,
            azimuth=effective_azimuth,
        )
    except Exception as exc:
        print(f"  [WARN] Stream 1 (ECMWF Best-Match) fetch error: {exc}")

    # 2. Fetch Stream 2: 91-Member Multi-Model Super-Ensemble (Probabilistic NWP)
    ens_report = {}
    try:
        ens_report = openmeteo_ensemble.fetch_openmeteo_ensemble_calibrated_summary(
            latitude=latitude,
            longitude=longitude,
            reference_time=reference_time,
            hours_ahead=hours_ahead,
            plant_name=plant_name,
            top_k=15,
            tilt=effective_tilt,
            azimuth=effective_azimuth,
            is_volatile=is_volatile,
        )
    except Exception as exc:
        print(f"  [WARN] Stream 2 (Super-Ensemble) fetch error: {exc}")

    # 3. Fetch Stream 3: Open-Meteo Premium Full-Feature Commercial Engine
    prem_report = {}
    try:
        prem_report = premium_stream3.fetch_premium_stream3_weather(
            latitude=latitude,
            longitude=longitude,
            reference_time=reference_time,
            hours_ahead=hours_ahead,
            tilt=effective_tilt,
            azimuth=effective_azimuth,
            plant_name=plant_name,
        )
    except Exception as exc:
        print(f"  [WARN] Stream 3 (Open-Meteo Premium) fetch error: {exc}")

    bm_rows = bm_report.get("rows", [])
    ens_rows = ens_report.get("rows", [])
    prem_hourly = prem_report.get("stream3_hourly", {})
    aod_clarity = prem_report.get("clarity_description", "Standard Clear-Sky")

    # Map Stream 1 (ECMWF Best-Match) by hour
    bm_by_hour: dict[str, dict[str, Any]] = {}
    for r in bm_rows:
        t_label = r.time.strftime("%H:%M") if hasattr(r, "time") else r.get("hour_label") or r.get("time", "")[-5:]
        gti = getattr(r, "global_tilted_irradiance_instant", None) if hasattr(r, "global_tilted_irradiance_instant") else r.get("global_tilted_irradiance_instant")
        temp = getattr(r, "temperature_2m", None) if hasattr(r, "temperature_2m") else r.get("temperature_2m")
        precip = getattr(r, "precipitation", None) if hasattr(r, "precipitation") else r.get("precipitation", 0.0)
        cloud = getattr(r, "effective_cloud_cover", None) if hasattr(r, "effective_cloud_cover") else None
        if cloud is None:
            cloud = getattr(r, "cloud_cover_low", None) if hasattr(r, "cloud_cover_low") else r.get("cloud_cover_low", 0.0)
        t_cell = getattr(r, "temp_cell_sandia_c", None) if hasattr(r, "temp_cell_sandia_c") else r.get("temp_cell_sandia_c")
        derate = getattr(r, "temp_derate_factor", None) if hasattr(r, "temp_derate_factor") else r.get("temp_derate_factor")
        bm_by_hour[t_label] = {
            "gti": gti or 0.0,
            "temp": temp or 25.0,
            "precip": precip or 0.0,
            "cloud": cloud or 0.0,
            "temp_cell": t_cell,
            "derate": derate,
        }

    # Map Stream 2 (Super-Ensemble) by hour
    ens_by_hour: dict[str, dict[str, Any]] = {}
    for r in ens_rows:
        t_label = r.get("hour_label") or r.get("time", "")[-5:]
        cloud = r.get("effective_cloud_cover")
        if cloud is None:
            cloud = r.get("cloud_cover_low") or 0.0
        ens_by_hour[t_label] = {
            "gti": r.get("global_tilted_irradiance_instant") or r.get("global_tilted_irradiance") or 0.0,
            "temp": r.get("temperature_2m") or 25.0,
            "precip": r.get("precipitation") or 0.0,
            "cloud": cloud,
            "temp_cell": r.get("temp_cell_sandia_c"),
            "derate": r.get("temp_derate_factor"),
        }

    def _get_hourly_interpolated(hourly_map: dict[str, dict[str, Any]], t_label: str) -> dict[str, Any]:
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
                if isinstance(v, (int, float)) and k in d_ceil and isinstance(d_ceil[k], (int, float)):
                    interpolated[k] = round(v + fraction * (d_ceil[k] - v), 3)
            return interpolated
        except Exception:
            h_floor = f"{t_label[:2]}:00"
            return hourly_map.get(h_floor, {})

    all_hours = sorted(set(list(bm_by_hour.keys()) + list(ens_by_hour.keys())))
    if not all_hours:
        fallback_text = bm_report.get("prompt_text") or ens_report.get("prompt_text") or "No weather data available."
        return {
            "source": "weather_fusion_fallback",
            "prompt_text": fallback_text,
            "fused_rows": [],
            "bm_report": bm_report,
            "ens_report": ens_report,
            "prem_report": prem_report,
        }

    fused_rows = []
    gateway_label = "Open-Meteo Professional Commercial Gateway (EUR 99 Plan Active)" if is_commercial else "Open-Meteo Standard Gateway"
    
    prompt_lines = [
        f"THREE SEPARATE WEATHER FORECAST STREAMS FOR AI ANALYSIS ({gateway_label}):",
        f"- Target Horizon: {all_hours[0]} to {all_hours[-1]}",
        f"- Atmospheric Clarity / AOD: {aod_clarity}",
        "- STREAM 1: ECMWF 9 km High-Resolution Deterministic Model (local microclimate rain cells & peak shape)",
        "- STREAM 2: 91-Member Multi-Model Super-Ensemble (51 ECMWF + 40 DWD ICON, probabilistic P40 cloud bounds)",
        "- STREAM 3: Open-Meteo Premium 5-Agency Consensus Engine (ECMWF, DWD, GFS, JMA, GEM + CAPE Instability + Sandia T_cell + Run Drift)",
        "",
        "BLOCK-BY-BLOCK 3-STREAM COMPARISON & ADVANCED PHYSICS TABLE (15-Minute Continuous Resolution):",
    ]

    for h in all_hours:
        b = _get_hourly_interpolated(bm_by_hour, h)
        e = _get_hourly_interpolated(ens_by_hour, h)
        p = _get_hourly_interpolated(prem_hourly, h)

        gti_stream1 = round(b.get("gti", 0.0), 1)
        gti_stream2 = round(e.get("gti", 0.0), 1)
        gti_stream3 = round(p.get("gti_om_premium", gti_stream1), 1)

        temp_avg = round((b.get("temp", 25.0) + e.get("temp", 25.0)) / 2.0, 1)
        precip_max = max(b.get("precip", 0.0), e.get("precip", 0.0), p.get("precip_mm", 0.0))
        cloud_avg = round((b.get("cloud", 0.0) + e.get("cloud", 0.0)) / 2.0, 1)

        drift_w = p.get("drift_w_m2", 0.0)
        drift_lbl = p.get("drift_label", "Stable")
        cell_t = p.get("temp_cell_sandia_c", b.get("temp_cell") or round(temp_avg + 10.0, 1))
        temp_derate = p.get("temp_derate_multiplier", b.get("derate") or 0.95)
        cape_val = int(p.get("cape_j_kg", 0))
        trans_val = p.get("cloud_transmissivity", 1.0)
        sun_sec = p.get("sunshine_seconds", 3600)

        delta_spread = abs(gti_stream1 - gti_stream2)

        # Robust consensus target:
        is_cloudy_or_rain = (precip_max >= 0.15) or (cloud_avg >= 40.0) or (trans_val < 0.80)
        is_convective_storm = (cape_val >= 1500 and (drift_w < -30.0 or cloud_avg >= 40.0 or precip_max >= 0.10))

        if is_cloudy_or_rain or is_convective_storm:
            gti_fused = min(gti_stream1, gti_stream2, gti_stream3)
            regime = "RAIN / CLOUD DAMPENED (P40 Guardrail)"
            conf = "Convective Storm Threat" if is_convective_storm else "Cloud / Rain Attenuation"
        elif delta_spread <= 60.0:
            gti_fused = round(((gti_stream1 + gti_stream2 + gti_stream3) / 3.0), 1)
            regime = "HIGH AGREEMENT"
            conf = "High Confidence"
        elif delta_spread > 120.0 or drift_w < -50.0:
            if cloud_avg >= 35.0 or drift_w < -50.0:
                gti_fused = round(min(gti_stream1, gti_stream2, gti_stream3) * 0.95, 1)
                regime = "HIGH DIVERGENCE (Cloud Penalty Shield)"
                conf = "Cloud Momentum Shift" if drift_w < -50.0 else "Cloud Uncertainty"
            else:
                # Clear sky divergence between models (e.g. transposition or aerosol differences)
                sorted_gtis = sorted([gti_stream1, gti_stream2, gti_stream3])
                gti_fused = round(sorted_gtis[1], 1)  # Robust median
                regime = "MODERATE SPREAD (Clear-Sky Robust Median)"
                conf = "Clear Sky Transposition Spread"
        else:
            gti_fused = round((0.4 * gti_stream1 + 0.3 * gti_stream2 + 0.3 * gti_stream3), 1)
            regime = "MODERATE SPREAD"
            conf = "Moderate Confidence"

        fused_rows.append({
            "hour_label": h,
            "gti_stream1": gti_stream1,
            "gti_stream2": gti_stream2,
            "gti_stream3": gti_stream3,
            "gti_fused": gti_fused,
            "delta_spread": round(delta_spread, 1),
            "temp_c": temp_avg,
            "temp_cell_sandia_c": cell_t,
            "temp_derate_multiplier": temp_derate,
            "cape_j_kg": cape_val,
            "cloud_transmissivity": trans_val,
            "sunshine_seconds": sun_sec,
            "drift_w_m2": drift_w,
            "precip_mm": precip_max,
            "cloud_pct": cloud_avg,
            "regime": regime,
            "confidence": conf,
        })

        prompt_lines.append(
            f"  * {h} | Stream 1: {gti_stream1:>5.1f} W/m2 | Stream 2: {gti_stream2:>5.1f} W/m2 | Stream 3 (OM-Prem 5-Agency): {gti_stream3:>5.1f} W/m2 | "
            f"[CAPE: {cape_val:>4d} J/kg | Transmissivity: {trans_val:.2f} | Sandia T_cell: {cell_t}C (Derate: {temp_derate}) | Drift: {drift_w:>+5.1f} W/m2 ({drift_lbl}) | Rain: {precip_max:.2f}mm | {conf}]"
        )

    prompt_lines.extend([
        "",
        "LLM 3-STREAM SYNTHESIS & ARBITRATION INSTRUCTIONS:",
        "1. Evaluate all 3 streams side-by-side across the continuous 15-minute trajectory:",
        "   - When Stream 1, Stream 2, and Stream 3 agree (Delta <= 60 W/m2), follow the high-confidence solar curve.",
        "   - When CAPE > 1500 J/kg, rain cells, or Drift < -50 W/m2 occur: Maintain a continuous, smoothly attenuated lower envelope across the entire convective window. DO NOT drop output on a single 15-min block and immediately spike back up.",
        "   - Factor Sandia Module Cell Temperature (T_cell) for physical silicon thermal derating during peak noon hours.",
        "   - Factor Cloud Transmissivity (UV/UV_clear) to differentiate thin cirrus from thick rain-bearing clouds.",
        "   - When Stream 3 indicates high atmospheric haze/AOD, maintain the AOD-attenuated generation target.",
        "2. Strict CERC Penalty Mandate: Your goal is 0 INR DSM deviation penalty (error must stay within +-15% of plant capacity).",
    ])

    fused_prompt_text = "\n".join(prompt_lines)

    return {
        "source": "tri_stream_premium_fusion",
        "prompt_text": fused_prompt_text,
        "fused_rows": fused_rows,
        "bm_report": bm_report,
        "ens_report": ens_report,
        "prem_report": prem_report,
    }
