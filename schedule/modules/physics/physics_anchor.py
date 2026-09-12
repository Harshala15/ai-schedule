"""
physics_anchor.py

REPLACES ml_forecast_model.py.

Provides a deterministic, physics-based BASELINE ("anchor") estimate of
solar generation (MW) for one forecast block's feature row -- no ML
model training, no LLM call. This is pure math:

    1. Solar elevation as a rough "clear sky" proxy (0 at night, maxing
       out around solar noon).
    2. Attenuate that by how much cloud is present, blending the
       image-derived brightness/cloud stats with the video motion
       coverage stats.
    3. Scale by plant capacity and performance ratio.

WHY THIS EXISTS in the new architecture: instead of asking the LLM to
invent a number from scratch (unreliable, non-deterministic, hard to
keep physically sensible across 8 forecast horizons), the LLM's job
becomes ADJUSTING this grounded anchor value based on retrieved similar
historical cases -- e.g. "anchor says 2.3 MW, but similar past cloud
patterns show generation tends to be ~12% lower than this formula
predicts, so adjust down." That is a much more constrained, reliable
task for an LLM than free-form number generation.

This file has NO dependency on a trained model file -- there is nothing
to "load". It always produces a number, from day one, using only the
feature row.
"""

import math

import config


def calculate_anchor_mw(feature_row: dict, capacity_mw: float | None = None,
                         performance_ratio: float | None = None,
                         correction_factor: float | None = None,
                         dc_capacity_mw: float | None = None) -> float:
    """
    Main entry point: computes the physics-based anchor generation (MW)
    for one forecast block's feature row.
    """
    ac_capacity_mw = float(capacity_mw if capacity_mw is not None else getattr(config, "PLANT_CAPACITY_MW", 10.0))
    if dc_capacity_mw is None:
        if capacity_mw is not None and getattr(config, "PLANT_CAPACITY_MW", None):
            dc_ratio = getattr(config, "PLANT_DC_CAPACITY_MW", ac_capacity_mw) / max(0.1, config.PLANT_CAPACITY_MW)
            dc_capacity_mw = float(capacity_mw * dc_ratio)
        else:
            dc_capacity_mw = float(getattr(config, "PLANT_DC_CAPACITY_MW", ac_capacity_mw))
    else:
        dc_capacity_mw = float(dc_capacity_mw)
    if performance_ratio is None:
        performance_ratio = getattr(config, "PERFORMANCE_RATIO", 0.78)

    elevation = feature_row.get("solar_elevation_deg", 0.0)
    month = feature_row.get("month", 9)
    minute_of_day = feature_row.get("minute_of_day")
    if minute_of_day is None and feature_row.get("hour") is not None:
        minute_of_day = feature_row["hour"] * 60 + feature_row.get("minute", 0)

    # 1. Inverter Cut-in Threshold & Winter Morning Fog Gate:
    # String inverters require minimum cut-in DC voltage. In winter (Nov-Feb),
    # radiation fog and morning ground haze delay direct beam irradiance.
    is_winter = isinstance(month, (int, float)) and int(month) in (11, 12, 1, 2)
    cut_in_elevation = 4.5 if is_winter else 3.0
    if elevation < cut_in_elevation:
        return 0.0

    # 2. Clear-sky proxy: smooth natural bell curve scaling with solar zenith
    raw_sine = math.sin(math.radians(elevation))
    clear_sky_index = max(0.0, min(1.0, raw_sine ** 0.95))

    # 3. Cloud attenuation from image and video motion features
    cloud_signals = []
    for key in ("clouds_bright_pixel_pct", "satellite_bright_pixel_pct", "rain_bright_pixel_pct"):
        if feature_row.get(key) is not None:
            cloud_signals.append(feature_row[key] / 100.0)
    motion_cov = feature_row.get("motion_coverage_end_pct")
    if motion_cov is not None:
        cloud_signals.append(motion_cov / 100.0)

    avg_cloud_fraction = sum(cloud_signals) / len(cloud_signals) if cloud_signals else 0.0
    avg_cloud_fraction = max(0.0, min(1.0, avg_cloud_fraction))
    
    # 4. Clearness Factor: If caller passed blended live/NWP correction_factor, use it.
    # Otherwise use NWP forward clearness or image cloud attenuation.
    if correction_factor is not None:
        clearness_factor = float(correction_factor)
    elif feature_row.get("nwp_clearness") is not None:
        clearness_factor = float(feature_row["nwp_clearness"])
    else:
        clearness_factor = max(0.20, 1.0 - (0.75 * avg_cloud_fraction))

    # 5. Safe Risk-Optimized Performance Ratio & Temperature Derating
    if isinstance(month, (int, float)) and int(month) in (6, 7, 8, 9) and avg_cloud_fraction > 0.35:
        effective_pr = max(0.68, min(0.74, performance_ratio or 0.72))
    else:
        effective_pr = max(0.74, min(0.82, performance_ratio or 0.78))

    # Cell temperature derate: PV modules lose ~0.4% efficiency per deg C above 25C
    if feature_row.get("temp_derate_multiplier") is not None:
        temp_derate = float(feature_row["temp_derate_multiplier"])
    else:
        temp_amb = feature_row.get("temp_air_c", feature_row.get("temperature_2m", 30.0))
        t_cell = feature_row.get("temp_cell_sandia_c")
        if t_cell is None:
            poa_proxy = max(0.0, 1000.0 * raw_sine * clearness_factor)
            t_cell = temp_amb + ((45.0 - 20.0) / 800.0) * poa_proxy
        temp_derate = max(0.80, min(1.02, 1.0 - 0.0038 * (float(t_cell) - 25.0)))

    # Afternoon Thermal Hysteresis: Cell temperature peaks between 12:30 and 15:30 IST (750 to 930 min).
    # Accounting for thermal inertia and inverter thermal throttling derating.
    if minute_of_day is not None and (750 <= minute_of_day <= 930) and elevation >= 35.0:
        thermal_hysteresis = float(getattr(config, "AFTERNOON_THERMAL_HYSTERESIS_FACTOR", 0.94))
        temp_derate *= thermal_hysteresis

    # 6. Asymmetric P42 Clear-Sky Headroom Factor:
    # Under Indian DSM regulations (CERC/TSERC), over-generation within +15% carries zero penalty,
    # whereas under-generation > 15% incurs heavy cash penalties.
    # Calibrated to Enercast benchmark: clear-sky conditions target P42 quantile (~0.93 of peak)
    # so that real plant generation hugs slightly above the schedule (+3% to +8% band).
    clear_sky_headroom = 1.0
    if clearness_factor >= 0.85 and avg_cloud_fraction < 0.20:
        clear_sky_headroom = float(getattr(config, "CLEAR_SKY_HEADROOM_FACTOR", 0.93))

    # 7. Physics generation computation & peak clipping
    generation_mw = (
        dc_capacity_mw
        * clear_sky_index
        * clearness_factor
        * effective_pr
        * temp_derate
        * clear_sky_headroom
    )

    # Clear-sky peak ceiling: Enercast limits clear midday generation to ~85% of AC capacity
    if clearness_factor >= 0.85:
        max_clear_peak = ac_capacity_mw * float(getattr(config, "MAX_CLEAR_PEAK_AC_FRACTION", 0.85))
        generation_mw = min(generation_mw, max_clear_peak)

    # Winter morning fog suppression cap:
    if is_winter and elevation < 15.0 and minute_of_day is not None and minute_of_day < 540:
        generation_mw = min(generation_mw, ac_capacity_mw * 0.35)

    generation_mw = max(0.0, min(ac_capacity_mw, generation_mw))
    return round(generation_mw, 3)

