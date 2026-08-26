"""Case-Based Reasoning retrieval for past visual-weather situations.

Each row in ``features_log`` is one case: the engineered Windy features,
the forecast made at that time, and (once SCADA data is available) its
actual generation.  A query always returns the nearest top-K cases; it
never switches to an unstructured "summarize all history" mode.
"""

import csv
import math

import config


NON_FEATURE_COLUMNS = {
    "Block", "Time", "Predicted Generation (MW)", "Actual Generation (MW)",
}


def _case_store_paths():
    """Every per-day features_log file (features_log/<PLANT>_features_log_<date>.csv),
    sorted chronologically by filename (which sorts correctly since dates
    are YYYY-MM-DD)."""
    return sorted(config.FEATURES_LOG_DIR.glob(f"{config.PLANT_NAME}_features_log_*.csv"))


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_case_store():
    """Reads and concatenates every per-day case-store file, so retrieval
    still searches the plant's FULL history even though it's split across
    many files -- one per day, not one ever-growing file."""
    rows = []
    for path in _case_store_paths():
        with open(path, "r", newline="", encoding="utf-8") as file:
            rows.extend(csv.DictReader(file))
    return rows


def _shared_feature_columns(current_row, history_rows):
    """Use only numeric, engineered fields present in both query and cases."""
    columns = []
    for column, value in current_row.items():
        if column in NON_FEATURE_COLUMNS or _as_float(value) is None:
            continue
        if any(_as_float(row.get(column)) is not None for row in history_rows):
            columns.append(column)
    return sorted(columns)


def _normalization_stats(rows, columns):
    means, stds = {}, {}
    for column in columns:
        values = [_as_float(row.get(column)) for row in rows]
        values = [value for value in values if value is not None]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        means[column] = mean
        stds[column] = max(math.sqrt(variance), 1e-6)
    return means, stds


def _circular_difference_degrees(first, second):
    """Smallest angle gap, so 359° and 1° are correctly close."""
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _weighted_distance(current, candidate, columns, means, stds):
    total_weight, squared_distance = 0.0, 0.0
    for column in columns:
        query_value = _as_float(current.get(column))
        case_value = _as_float(candidate.get(column))
        if query_value is None or case_value is None:
            continue
        if column == "motion_direction_deg" and query_value >= 0 and case_value >= 0:
            difference = _circular_difference_degrees(query_value, case_value) / stds[column]
        else:
            difference = (query_value - case_value) / stds[column]
        weight = config.CBR_FEATURE_WEIGHTS.get(column, 0.5)
        squared_distance += weight * difference * difference
        total_weight += weight
    return math.sqrt(squared_distance / total_weight) if total_weight else float("inf")


def get_top_k_similar_cases(current_feature_row, k=None, exclude_time=None):
    """Return nearest feature-matched past cases, sorted closest first.

    Cases with SCADA actuals include signed prediction error and percentage
    error, giving the LLM concrete outcome evidence rather than only text.
    """
    k = config.CBR_TOP_K if k is None else max(1, int(k))
    rows = [row for row in _load_case_store() if row.get("Time") != exclude_time]
    if not rows:
        return []

    # Ground-truth outcomes are more useful than unfinished forecasts.
    # Once feedback exists, retrieve only completed cases; during cold
    # start, unfinished feature-matched forecasts still provide limited
    # structural context and are explicitly labelled as such in the prompt.
    completed_rows = [row for row in rows if _as_float(row.get("Actual Generation (MW)")) is not None]
    rows = completed_rows or rows

    columns = _shared_feature_columns(current_feature_row, rows)
    if not columns:
        return []
    means, stds = _normalization_stats(rows, columns)

    scored = []
    for row in rows:
        distance = _weighted_distance(current_feature_row, row, columns, means, stds)
        if math.isfinite(distance):
            scored.append((distance, row))
    scored.sort(key=lambda pair: pair[0])

    results = []
    for distance, row in scored[:k]:
        result = {
            "time": row.get("Time", "unknown"),
            "distance": round(distance, 4),
            "predicted_mw": row.get("Predicted Generation (MW)", ""),
            "features_compared": len(columns),
        }
        actual = _as_float(row.get("Actual Generation (MW)"))
        predicted = _as_float(row.get("Predicted Generation (MW)"))
        if actual is not None:
            result["actual_mw"] = round(actual, 3)
        if actual is not None and predicted is not None:
            error = predicted - actual
            result["error_mw"] = round(error, 3)
            if actual > 0.05:
                result["error_pct"] = round(error / actual * 100.0, 1)
        results.append(result)
    return results


def format_cases_for_prompt(cases):
    """Create compact, outcome-focused CBR evidence for the LLM prompt."""
    if not cases:
        return (
            "No feature-matched historical cases are available yet. Keep the "
            "physics anchors unchanged and use Low confidence."
        )

    lines = ["Top feature-matched historical cases (nearest first):"]
    errors = []
    for case in cases:
        line = (
            f"- {case['time']}: prior forecast={case['predicted_mw']} MW"
        )
        if "actual_mw" in case:
            line += f", actual={case['actual_mw']} MW"
        if "error_mw" in case:
            line += f", forecast error={case['error_mw']:+.3f} MW"
            errors.append(case["error_mw"])
        line += f", distance={case['distance']}"
        lines.append(line)

    if errors:
        mean_error = sum(errors) / len(errors)
        lines.append(
            f"Outcome summary across {len(errors)} cases with SCADA actuals: "
            f"mean forecast bias={mean_error:+.3f} MW (positive means over-forecast)."
        )
    else:
        lines.append("None of these cases has a matched SCADA actual yet; do not infer an outcome bias.")
    return "\n".join(lines)
