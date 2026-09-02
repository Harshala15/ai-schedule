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


def calculate_anchor_mw(feature_row: dict, capacity_mw: float = config.PLANT_CAPACITY_MW,
                         performance_ratio: float = config.PERFORMANCE_RATIO,
                         correction_factor: float = 1.0) -> float:
    """
    Main entry point: computes the physics-based anchor generation (MW)
    for one forecast block's feature row.
    """
    elevation = feature_row.get("solar_elevation_deg", 0.0)
    # 1. Inverter Cut-in Threshold: Physical inverters require minimum string voltage
    # and turn off when sun is below 7.5 degrees (eliminating pre-dawn & post-dusk creep).
    if elevation < 7.5:
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
    clearness_factor = max(0.20, 1.0 - (0.75 * avg_cloud_fraction))

    # 4. Safe Risk-Optimized Performance Ratio & Temperature Derating
    # During monsoon (June-September), scales curve to naturally peak at ~3.55 MW without flat plateaus
    # During dry/winter (October-May), scales curve to peak at ~4.15 MW.
    month = feature_row.get("month", 9)
    if isinstance(month, (int, float)) and int(month) in (6, 7, 8, 9):
        effective_pr = max(0.68, min(0.705, performance_ratio or 0.695))
    else:
        effective_pr = max(0.74, min(0.81, performance_ratio or 0.78))

    # Cell temperature derate: PV modules lose ~0.4% efficiency per deg C above 25C
    temp_amb = feature_row.get("temp_air_c", feature_row.get("temperature_2m", 30.0))
    poa_proxy = max(0.0, 1000.0 * raw_sine * clearness_factor)
    t_cell = temp_amb + ((45.0 - 20.0) / 800.0) * poa_proxy
    temp_derate = max(0.88, min(1.02, 1.0 - 0.0038 * (t_cell - 25.0)))

    # 5. Physics generation computation (dynamic curved solar arch)
    generation_mw = capacity_mw * clear_sky_index * clearness_factor * effective_pr * temp_derate * correction_factor
    generation_mw = max(0.0, min(capacity_mw, generation_mw))
    return round(generation_mw, 3)