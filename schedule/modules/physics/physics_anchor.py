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
    for one forecast block's feature row. This is the "before LLM
    adjustment" baseline that gets passed into llm_predictor.py.

    correction_factor: optional empirical multiplier -- see
    daily_feedback.compute_anchor_correction_factor(), which derives this
    from real SCADA history once enough of it has accumulated. Lets the
    anchor self-correct against real outcomes without hand-tuning the
    formula's constants (PERFORMANCE_RATIO, cloud attenuation) blind.
    Defaults to 1.0 (no change) for any caller that doesn't pass one.
    """
    elevation = feature_row.get("solar_elevation_deg", 0.0)
    if elevation <= 0:
        return 0.0  # sun below horizon -> no generation

    # Clear-sky proxy: rises with sin(elevation), roughly matching how
    # actual clear-sky irradiance behaves through the day.
    clear_sky_index = math.sin(math.radians(elevation))
    clear_sky_index = max(0.0, min(1.0, clear_sky_index))

    # Cloud attenuation: average a few cloud-ish signals into one 0-1
    # "how clear is it" factor. Higher bright_pixel_pct / coverage = more
    # cloud = less clear.
    cloud_signals = []
    for key in ("clouds_bright_pixel_pct", "satellite_bright_pixel_pct", "rain_bright_pixel_pct"):
        if feature_row.get(key) is not None:
            cloud_signals.append(feature_row[key] / 100.0)
    motion_cov = feature_row.get("motion_coverage_end_pct")
    if motion_cov is not None:
        cloud_signals.append(motion_cov / 100.0)

    avg_cloud_fraction = sum(cloud_signals) / len(cloud_signals) if cloud_signals else 0.3
    avg_cloud_fraction = max(0.0, min(1.0, avg_cloud_fraction))
    clearness_factor = 1.0 - (0.8 * avg_cloud_fraction)  # heavy cloud can cut ~80%

    generation_mw = capacity_mw * clear_sky_index * clearness_factor * performance_ratio * correction_factor
    generation_mw = max(0.0, min(capacity_mw, generation_mw))
    return round(generation_mw, 3)