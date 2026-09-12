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

import concurrent.futures
import datetime as dt
import json
import math
import os
from dataclasses import dataclass
from typing import Any

import config
from modules.weather import air_quality, ecmwf_weather, openmeteo_ensemble, premium_stream3, time_features


def _get_hourly_interpolated(
    hourly_map: dict[str, dict[str, Any]],
    t_label: str,
    target_dt: dt.datetime | None = None,
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
) -> dict[str, Any]:
    """
    Interpolates hourly weather rows to 15-minute resolution.
    Uses parabolic clear-sky solar geometry (Kt) for global tilted irradiance (GTI)
    to eliminate pre-dawn radiation creep and preserve natural trigonometric arcs.
    """
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

        # Solar Geometry Parabolic Clearness Index (Kt) Interpolation for Irradiance:
        if target_dt is not None:
            elev_t = time_features.compute_time_features(target_dt, latitude, longitude)["solar_elevation_deg"]
            if elev_t < 3.0:
                interpolated["gti"] = 0.0
                if "gti_om_premium" in interpolated:
                    interpolated["gti_om_premium"] = 0.0
            else:
                dt_floor = target_dt.replace(minute=0, second=0, microsecond=0)
                dt_ceil = dt_floor + dt.timedelta(hours=1)
                elev_f = time_features.compute_time_features(dt_floor, latitude, longitude)["solar_elevation_deg"]
                elev_c = time_features.compute_time_features(dt_ceil, latitude, longitude)["solar_elevation_deg"]

                cs_f = max(15.0, 1000.0 * (math.sin(math.radians(max(0.0, elev_f))) ** 0.95))
                cs_c = max(15.0, 1000.0 * (math.sin(math.radians(max(0.0, elev_c))) ** 0.95))
                cs_t = max(0.0, 1000.0 * (math.sin(math.radians(max(0.0, elev_t))) ** 0.95))

                gti_f = float(d_floor.get("gti", d_floor.get("gti_om_premium", 0.0)) or 0.0)
                gti_c = float(d_ceil.get("gti", d_ceil.get("gti_om_premium", 0.0)) or 0.0)

                kt_f = max(0.0, min(1.25, gti_f / cs_f))
                kt_c = max(0.0, min(1.25, gti_c / cs_c))
                kt_t = kt_f + fraction * (kt_c - kt_f)

                interpolated_gti = round(kt_t * cs_t, 1)
                interpolated["gti"] = interpolated_gti
                if "gti_om_premium" in interpolated:
                    interpolated["gti_om_premium"] = interpolated_gti

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
    """Fetches 3 explicit standalone weather streams and formats them for LLM 3-stream arbitration."""
    reference_time = reference_time or dt.datetime.now()
    effective_tilt = tilt if tilt is not None else getattr(config, "PLANT_TILT_DEG", 15.0)
    raw_az = azimuth if azimuth is not None else getattr(config, "PLANT_ORIENTATION_FROM_SOUTH_DEG", getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 0.0))
    effective_azimuth = config.to_openmeteo_azimuth(raw_az)

    api_key = getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "").strip()
    is_commercial = bool(api_key)

    # 1. Fetch 3 Independent Weather Streams Concurrently (ThreadPoolExecutor)
    # Reduces total network latency from ~30s down to ~8s
    bm_report = {}
    ens_report = {}
    prem_report = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f_bm = executor.submit(
            ecmwf_weather.fetch_ecmwf_weather_summary,
            latitude=latitude,
            longitude=longitude,
            reference_time=reference_time,
            hours_ahead=hours_ahead,
            tilt=effective_tilt,
            azimuth=effective_azimuth,
        )
        f_ens = executor.submit(
            openmeteo_ensemble.fetch_openmeteo_ensemble_calibrated_summary,
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
        f_prem = executor.submit(
            premium_stream3.fetch_premium_stream3_weather,
            latitude=latitude,
            longitude=longitude,
            reference_time=reference_time,
            hours_ahead=hours_ahead,
            tilt=effective_tilt,
            azimuth=effective_azimuth,
            plant_name=plant_name,
        )

        try:
            bm_report = f_bm.result(timeout=25)
        except Exception as exc:
            print(f"  [WARN] Stream 1 (ECMWF Best-Match) fetch error: {exc}")

        try:
            ens_report = f_ens.result(timeout=25)
        except Exception as exc:
            print(f"  [WARN] Stream 2 (Super-Ensemble) fetch error: {exc}")

        try:
            prem_report = f_prem.result(timeout=25)
        except Exception as exc:
            print(f"  [WARN] Stream 3 (Open-Meteo Premium) fetch error: {exc}")

    bm_rows = bm_report.get("rows", [])
    ens_rows = ens_report.get("rows", [])
    prem_hourly = prem_report.get("stream3_hourly", {})
    aod_clarity = prem_report.get("clarity_description", "Standard Clear-Sky")

    # Map Stream 1 (ECMWF Best-Match) by time
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

    # Map Stream 2 (Super-Ensemble) by time
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

    # 2. Build continuous chronological 15-minute horizon
    total_blocks = int(hours_ahead * 4) + 1
    horizon_dts = [reference_time + dt.timedelta(minutes=15 * k) for k in range(total_blocks)]
    horizon_labels = [d.strftime("%H:%M") for d in horizon_dts]
    horizon_dt_by_label = {d.strftime("%H:%M"): d for d in horizon_dts}

    stream_keys = set(list(bm_by_hour.keys()) + list(ens_by_hour.keys()) + list(prem_hourly.keys()))
    if not stream_keys:
        fallback_text = bm_report.get("prompt_text") or ens_report.get("prompt_text") or "No weather data available."
        return {
            "source": "weather_fusion_fallback",
            "prompt_text": fallback_text,
            "fused_rows": [],
            "bm_report": bm_report,
            "ens_report": ens_report,
            "prem_report": prem_report,
        }

    all_hours = [lbl for lbl in horizon_labels if lbl in stream_keys or any(lbl[:2] == k[:2] for k in stream_keys)]
    if not all_hours:
        all_hours = sorted(stream_keys)

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
        target_dt = horizon_dt_by_label.get(h)
        b = _get_hourly_interpolated(bm_by_hour, h, target_dt, latitude, longitude)
        e = _get_hourly_interpolated(ens_by_hour, h, target_dt, latitude, longitude)
        p = _get_hourly_interpolated(prem_hourly, h, target_dt, latitude, longitude)

        raw_s1 = b.get("gti")
        raw_s2 = e.get("gti")
        raw_s3 = p.get("gti_om_premium", p.get("gti"))

        # Calculate sun elevation for this block
        elev_target = 0.0
        if target_dt:
            elev_target = time_features.compute_time_features(target_dt, latitude, longitude)["solar_elevation_deg"]

        # Night / below inverter cut-in threshold:
        if elev_target < 3.0 and (raw_s1 is None or raw_s1 < 10.0) and (raw_s2 is None or raw_s2 < 10.0):
            gti_stream1 = 0.0
            gti_stream2 = 0.0
            gti_stream3 = 0.0
            gti_fused = 0.0
            delta_spread = 0.0
            regime = "NIGHT / PRE-SUNRISE"
            conf = "High Confidence"
        else:
            # Filter active streams: discard failed streams (0.0 during daytime when others are active)
            streams_dict = {}
            if raw_s1 is not None and not (elev_target >= 10.0 and raw_s1 <= 0.0 and max(raw_s2 or 0.0, raw_s3 or 0.0) > 50.0):
                streams_dict["S1"] = float(raw_s1)
            if raw_s2 is not None and not (elev_target >= 10.0 and raw_s2 <= 0.0 and max(raw_s1 or 0.0, raw_s3 or 0.0) > 50.0):
                streams_dict["S2"] = float(raw_s2)
            if raw_s3 is not None and not (elev_target >= 10.0 and raw_s3 <= 0.0 and max(raw_s1 or 0.0, raw_s2 or 0.0) > 50.0):
                streams_dict["S3"] = float(raw_s3)

            if not streams_dict:
                streams_dict["S1"] = float(raw_s1 or 0.0)

            active_vals = list(streams_dict.values())
            gti_stream1 = round(raw_s1 if raw_s1 is not None else active_vals[0], 1)
            gti_stream2 = round(raw_s2 if raw_s2 is not None else active_vals[0], 1)
            gti_stream3 = round(raw_s3 if raw_s3 is not None else active_vals[0], 1)

            sorted_gtis = sorted(active_vals)
            med_val = sorted_gtis[len(sorted_gtis) // 2]
            delta_spread = round(max(active_vals) - min(active_vals), 1)

            # Check for rain / cloud consensus: require at least 2 streams to confirm rain / thick clouds
            p1 = float(b.get("precip", 0.0) or 0.0)
            p2 = float(e.get("precip", 0.0) or 0.0)
            p3 = float(p.get("precip_mm", 0.0) or 0.0)
            c1 = float(b.get("cloud", 0.0) or 0.0)
            c2 = float(e.get("cloud", 0.0) or 0.0)
            c3 = float(p.get("cloud_pct", 0.0) or 0.0)

            rain_votes = sum(1 for p_val in (p1, p2, p3) if p_val >= 0.50)
            cloud_votes = sum(1 for c_val in (c1, c2, c3) if c_val >= 50.0)

            is_confirmed_rain = (rain_votes >= 2) or (rain_votes >= 1 and cloud_votes >= 2)
            is_confirmed_heavy_cloud = (cloud_votes >= 2)

            if len(sorted_gtis) == 3:
                d_low_mid = sorted_gtis[1] - sorted_gtis[0]
                d_mid_high = sorted_gtis[2] - sorted_gtis[1]

                if is_confirmed_rain:
                    # Confirmed storm/rain by multiple streams: take conservative lower envelope
                    gti_fused = round((sorted_gtis[0] * 0.60 + sorted_gtis[1] * 0.40), 1)
                    regime = "RAIN / CLOUD CONFIRMED (Multi-Stream Consensus)"
                    conf = "Confirmed Rain Attenuation"
                elif is_confirmed_heavy_cloud and sorted_gtis[0] < sorted_gtis[1] - 100.0:
                    # Heavy cloud confirmed: reject high ungrounded spike
                    gti_fused = round((sorted_gtis[0] * 0.50 + sorted_gtis[1] * 0.50), 1)
                    regime = "OVERCAST CONSENSUS (High Outlier Rejected)"
                    conf = "Heavy Cloud Attenuation"
                elif d_mid_high <= 85.0 and sorted_gtis[1] > sorted_gtis[0] + 85.0:
                    # Upper pair agrees (e.g. 640 and 675 W/m2 vs 417 W/m2): discard the low outlier!
                    gti_fused = round((sorted_gtis[1] + sorted_gtis[2]) / 2.0, 1)
                    regime = "UPPER CONSENSUS (Pessimistic Outlier Rejected)"
                    conf = "High Confidence (Upper Pair Agreement)"
                elif d_low_mid <= 85.0 and sorted_gtis[2] > sorted_gtis[1] + 85.0:
                    # Lower pair agrees (e.g. 350 and 380 W/m2 vs 650 W/m2): discard the high outlier!
                    gti_fused = round((sorted_gtis[0] + sorted_gtis[1]) / 2.0, 1)
                    regime = "LOWER CONSENSUS (Spurious High Outlier Rejected)"
                    conf = "Moderate Confidence (Lower Pair Agreement)"
                elif delta_spread <= 65.0:
                    # All three streams agree closely
                    gti_fused = round(sum(sorted_gtis) / 3.0, 1)
                    regime = "HIGH AGREEMENT (Tri-Stream Mean)"
                    conf = "High Confidence"
                else:
                    # Dispersed models: use robust statistical median
                    gti_fused = round(med_val, 1)
                    regime = "ROBUST MEDIAN CONSENSUS"
                    conf = "Model Spread (Robust Median)"
            elif len(sorted_gtis) == 2:
                if is_confirmed_rain:
                    gti_fused = round(min(sorted_gtis), 1)
                    regime = "DUAL STREAM RAIN DAMPENED"
                    conf = "Rain Attenuation"
                else:
                    gti_fused = round((sorted_gtis[0] + sorted_gtis[1]) / 2.0, 1)
                    regime = "DUAL STREAM MEAN"
                    conf = "Dual Stream Consensus"
            else:
                gti_fused = round(sorted_gtis[0], 1)
                regime = "SINGLE ACTIVE STREAM"
                conf = "Single Stream Fallback"

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

        fused_rows.append({
            "hour_label": h,
            "gti_stream1": gti_stream1,
            "gti_stream2": gti_stream2,
            "gti_stream3": gti_stream3,
            "gti_fused": gti_fused,
            "delta_spread": delta_spread,
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
            f"Fused: {gti_fused:>5.1f} W/m2 ({regime}) | [CAPE: {cape_val:>4d} J/kg | Transmissivity: {trans_val:.2f} | Sandia T_cell: {cell_t}C | Rain: {precip_max:.2f}mm | {conf}]"
        )

    prompt_lines.extend([
        "",
        "LLM 3-STREAM SYNTHESIS & ARBITRATION INSTRUCTIONS:",
        "1. Evaluate all 3 streams side-by-side across the continuous 15-minute trajectory:",
        "   - When Stream 1, Stream 2, and Stream 3 agree (Delta <= 65 W/m2), follow the high-confidence solar curve.",
        "   - When at least 2 streams confirm rain cells or thick clouds: Follow the robust consensus lower envelope.",
        "   - An isolated low or high outlier is automatically rejected by the robust consensus engine.",
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
