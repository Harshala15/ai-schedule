"""
feature_builder.py

Combines the three feature sources into ONE flat, feature-ready row per
forecast block:
  - video motion features   (from video_motion_features.analyze_video)
  - image features           (from image_feature_extraction.extract_image_features)
  - time / solar features    (from time_features.compute_time_features, differs
                               per block since each block is a different future time)

Categorical values (compass direction, coverage trend) are encoded to
numbers here, since both the physics anchor and the similarity-based
retrieval need numeric input, not text labels.

NEW: build_feature_vector() converts a feature row into a plain ordered
list of numbers -- this is what similarity_retrieval.py uses to compare
"how similar is today's situation to a past case" via distance
calculations (e.g. Euclidean/cosine), replacing the old
get_feature_columns() use in train_model.py (which is being removed).
"""

# Compass direction -> degrees (0 = North, clockwise), so downstream
# numeric code gets a continuous value instead of a text label.
_DIRECTION_TO_DEGREES = {
    "North": 0, "Northeast": 45, "East": 90, "Southeast": 135,
    "South": 180, "Southwest": 225, "West": 270, "Northwest": 315,
    "negligible / stationary": -1,  # sentinel: "no meaningful motion"
}

_TREND_TO_NUMBER = {
    "increasing": 1,
    "stable": 0,
    "decreasing": -1,
}


def _default_motion_features() -> dict:
    """Used when no video was recorded/processed, so the pipeline can
    still run (with motion features neutrally filled in) instead of
    crashing."""
    return {
        "avg_direction": "negligible / stationary",
        "avg_motion_score": 0.0,
        "directional_consistency": 0.0,
        "coverage_start_pct": 0.0,
        "coverage_end_pct": 0.0,
        "coverage_trend": "stable",
        "frame_count": 0,
    }


def encode_motion_features(motion_features: dict | None) -> dict:
    mf = motion_features or _default_motion_features()
    return {
        "motion_direction_deg": _DIRECTION_TO_DEGREES.get(mf.get("avg_direction"), -1),
        # A Windy animation is time-lapse, so this is deliberately a
        # relative image-motion score rather than a false physical km/h.
        "motion_score": mf.get("avg_motion_score", mf.get("avg_speed_kmh", 0.0)) or 0.0,
        "motion_directional_consistency": mf.get("directional_consistency", 0.0) or 0.0,
        "motion_coverage_start_pct": mf.get("coverage_start_pct", 0.0) or 0.0,
        "motion_coverage_end_pct": mf.get("coverage_end_pct", 0.0) or 0.0,
        "motion_coverage_trend": _TREND_TO_NUMBER.get(mf.get("coverage_trend"), 0),
        "motion_frame_count": mf.get("frame_count", 0) or 0,
    }


def combine_features(motion_features: dict | None, image_features: dict, time_features: dict) -> dict:
    """
    Returns one flat dict combining all three sources for a SINGLE
    forecast block. `image_features` and `motion_features` are the same
    for every block in a run (they only change once you re-capture),
    `time_features` differs per block (since each block is a different
    future timestamp).
    """
    row = {}
    row.update(encode_motion_features(motion_features))
    row.update(image_features)
    row.update(time_features)
    return row


def get_feature_columns(sample_row: dict) -> list:
    """
    Returns a SORTED, stable list of feature column names from a sample
    row. Sorting alphabetically means the column order is always the
    same regardless of dict insertion order -- important so any code
    reading these rows later (similarity_retrieval.py, analysis, etc.)
    always sees features in the same order.
    """
    return sorted(sample_row.keys())


def build_feature_vector(feature_row: dict, feature_columns: list) -> list:
    """
    Converts a feature row (dict) into a plain ordered list of numbers,
    using `feature_columns` (from get_feature_columns) to fix the order.
    This is what similarity_retrieval.py compares between today's
    situation and past cases -- e.g. via Euclidean distance -- to find
    the top-K most similar historical blocks.

    Any missing or non-numeric value (None, empty string, a stray text
    value) becomes 0.0 instead of raising an error, so a single messy
    column never breaks the whole similarity comparison.
    """
    vector = []
    for col in feature_columns:
        value = feature_row.get(col)
        if value is None or value == "":
            vector.append(0.0)
            continue
        try:
            vector.append(float(value))
        except (TypeError, ValueError):
            vector.append(0.0)
    return vector
