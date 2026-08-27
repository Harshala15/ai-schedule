"""
daily_feedback.py

Run this manually at the end of each day, once your plant's actual
SCADA/meter export for that day is available:

    python daily_feedback.py <path_to_actual_meter_csv>

It does TWO things:
    1. Adds an "Actual Generation (MW)" column to features_log.csv
       (the case store) for every matching timestamp -- this is what
       lets similarity_retrieval.py eventually show the LLM "in similar
       situations, actual generation was X" instead of just predictions.
    2. Computes and prints error metrics (MAE, RMSE, MAPE, Bias) comparing
       predicted vs actual generation, and appends a row to a running
       accuracy log CSV so you can track accuracy over time.

No LLM, no ML training -- pure deterministic comparison and file update.

Expected actual-meter CSV format: at least a timestamp column and a
power column, matching TIMESTAMP_COLUMN / POWER_COLUMN_MW below (adjust
to your SCADA export's real column names). Negative power readings are
treated as 0 (no generation), consistent with earlier data-cleaning
decisions in this project.
"""

import csv
import json
import re
import sys
import datetime
from pathlib import Path

import config
from modules.storage import state_sync

# ---- Adjust these to match your actual SCADA export's column names ----
TIMESTAMP_COLUMN = "TimeStamp"
POWER_COLUMN_MW = "Active Power (MW)"   # expects MW already -- convert first if your export is in kW
# -------------------------------------------------------------------

ACTUAL_COLUMN_NAME = "Actual Generation (MW)"
# Keep the accuracy log under the writable storage root so Lambda can append to it safely.
ACCURACY_LOG_PATH = config.ACCURACY_REPORTS_DIR / f"{config.PLANT_NAME}_daily_accuracy.csv"
CONTEXT_SCHEMA_VERSION = 2

# ---- Raw company meter-export columns (used by the raw-meter learning flow) ----
# Same shape as historic_cases/*_SOLAR_INV.csv: TimeStamp + Active Power in
# kW (not MW yet) + raw sensor columns. Update this if the company ever
# changes their export's column names/order.
RAW_METER_COLUMNS = [
    "TimeStamp", "Active Power (kW)", "POA (W/m2)", "GHI (W/m2)",
    "Wind Speed (m/s)", "Wind Direction (DEG.)", "AMB TEMP", "MOD TEMP", "Humidity",
]
RAW_METER_TIMESTAMP_COLUMNS = ("TimeStamp", "Timestamp")
RAW_METER_POWER_COLUMNS = ("Active Power (kW)", "Active Power-Avg MFM-OUT (KW)", "Active Power (MW)")
MERGED_STORE_PATH = config.HISTORIC_CASES_DIR / "merged_scada_data.csv"
RAW_METER_MANIFEST_PATH = config.ACTUALS_INBOX_PROCESSED_DIR / f"{config.PLANT_NAME}_raw_meter_manifest.json"

PLANT_ACTUAL_METER_COLUMNS = {
    "SIRMOUR": {
        "timestamp": ("TimeStamp", "Timestamp"),
        "power": ("Active Power (kW)", "Active Power (MW)", "Active Power-Avg MFM-OUT (KW)", "GSPPL - Meter data (live) (kW)"),
    },
    "KASIPET": {
        "timestamp": ("Timestamp", "TimeStamp"),
        "power": ("Active Power-Avg MFM-OUT (KW)", "Active Power (kW)", "Active Power (MW)"),
    },
    "BHUPALPALLY": {
        "timestamp": ("TimeStamp", "Timestamp"),
        "power": (
            "Active Power (kW)",
            "Active Power (MW)",
            "Active Power-Avg MFM-OUT (KW)",
            "Active Power-Avg MFM-OUT (kW)",
        ),
    },
}


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_header_name(name: str) -> str:
    """Normalize a CSV header so BOMs, wrapping quotes, and odd spacing
    from spreadsheet exports do not break column matching."""
    cleaned = (name or "").replace("\ufeff", "").strip()
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) >= 2:
        cleaned = cleaned[1:-1].strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _pick_first_existing_column(fieldnames: list[str] | None, candidates: tuple[str, ...]) -> str | None:
    fieldnames = fieldnames or []
    normalized_fields = { _normalize_header_name(field): field for field in fieldnames if field }
    for candidate in candidates:
        normalized_candidate = _normalize_header_name(candidate)
        if candidate in fieldnames:
            return candidate
        if normalized_candidate in normalized_fields:
            return normalized_fields[normalized_candidate]
    return None


def _power_value_to_mw(raw_value, power_column_name: str) -> float | None:
    """Normalizes a power reading to MW regardless of whether the source
    column stores kW or MW."""
    value = _as_float(raw_value)
    if value is None:
        return None
    if "kw" in power_column_name.lower() and "(mw)" not in power_column_name.lower():
        return max(0.0, value) / 1000.0
    return max(0.0, value)


def _load_actual_readings(actual_csv_path: str) -> dict:
    """
    Reads the actual meter CSV and returns {time_label: actual_mw}, with
    time_label formatted as "%Y-%m-%d %H:%M" to match features_log.csv's
    Time column, and negative readings clipped to 0.
    """
    readings = {}
    plant = (config.PLANT_NAME or "").strip().upper()
    column_profile = PLANT_ACTUAL_METER_COLUMNS.get(plant, {})
    timestamp_candidates = column_profile.get("timestamp", RAW_METER_TIMESTAMP_COLUMNS)
    power_candidates = column_profile.get("power", RAW_METER_POWER_COLUMNS)
    with open(actual_csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        timestamp_column = _pick_first_existing_column(
            reader.fieldnames,
            tuple(dict.fromkeys((TIMESTAMP_COLUMN, *timestamp_candidates, "DateTime", "Datetime", "Start (Asia/Calcutta)", "Start (Asia/Kolkata)", "Start"))),
        )
        power_column = _pick_first_existing_column(
            reader.fieldnames,
            tuple(dict.fromkeys((POWER_COLUMN_MW, *power_candidates))),
        )
        if timestamp_column is None or power_column is None:
            raise SystemExit(
                f"Expected meter columns for plant {plant or 'UNKNOWN'} were not found. "
                f"Timestamp candidates: {timestamp_candidates}; power candidates: {power_candidates}. "
                f"Available columns: {reader.fieldnames}\n"
                f"Update PLANT_ACTUAL_METER_COLUMNS at the top of this script to match the plant export."
            )
        for row in reader:
            raw_ts = (row.get(timestamp_column) or "").strip()
            normalized = _normalize_timestamp(raw_ts)
            if normalized is None:
                continue
            time_label = normalized[:16]
            mw = _power_value_to_mw(row.get(power_column), power_column)
            if mw is None:
                continue
            readings[time_label] = mw
    return readings


def _extract_schedule_time(row: dict) -> str:
    """Return the canonical forecast timestamp label for a schedule row."""
    if row.get("Time"):
        return (row["Time"] or "").strip()[:16]
    interval = (row.get("Time Interval (15 minute interval)") or "").strip()
    if " - " in interval:
        return interval.split(" - ", 1)[0].strip()[:16]
    return ""


def _extract_schedule_mw(row: dict) -> float | None:
    """Return the schedule MW value from a forecast row, if present."""
    # Prefer the validated final value when it is available so revision
    # feedback is based on the actual accepted forecast, not the raw LLM output.
    for key in ("Schedule MW", "Final Validated MW", "LLM Schedule (MW)", "Predicted Generation (MW)", "Forecast (MW)"):
        if key not in row:
            continue
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def _extract_named_mw(row: dict, *keys: str) -> float | None:
    """Return the first usable MW value from a row across a set of column names."""
    for key in keys:
        if key not in row:
            continue
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def _load_schedule_rows(schedule_csv_path: str) -> dict:
    """Read a forecast CSV and return {time_label: forecast_mw}."""
    schedule = {}
    with open(schedule_csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            time_label = _extract_schedule_time(row)
            predicted_mw = _extract_schedule_mw(row)
            if not time_label or predicted_mw is None:
                continue
            schedule[time_label] = {
                "time": time_label,
                "predicted_mw": predicted_mw,
                "row": row,
            }
    return schedule


def _case_store_paths():
    """Every per-day features_log file (features_log/<PLANT>_features_log_<date>.csv)."""
    return sorted(config.FEATURES_LOG_DIR.glob(f"{config.PLANT_NAME}_features_log_*.csv"))


def _update_case_store_with_actuals(actual_readings: dict) -> tuple:
    """
    Fills in ACTUAL_COLUMN_NAME for every row (across ALL per-day
    features_log files) whose Time matches an actual reading, and
    rewrites each touched file using the same schema-safe (dict-keyed,
    DictWriter) approach as prediction_store.py -- so adding this new
    column can never shift or corrupt existing values, even for rows
    written before this column existed.

    Returns (updated_count, matched_rows) where matched_rows is a list of
    (predicted_mw, actual_mw) pairs for the error-metric calculation below.
    """
    case_store_paths = _case_store_paths()
    if not case_store_paths:
        raise SystemExit(f"No case store files found in {config.FEATURES_LOG_DIR} -- run the main pipeline first.")

    updated_count = 0
    matched_rows = []
    for csv_path in case_store_paths:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames)
            rows = list(reader)

        if ACTUAL_COLUMN_NAME not in fieldnames:
            fieldnames.append(ACTUAL_COLUMN_NAME)

        file_touched = False
        for row in rows:
            time_label = row.get("Time")
            if time_label in actual_readings:
                actual_mw = actual_readings[time_label]
                row[ACTUAL_COLUMN_NAME] = str(actual_mw)
                updated_count += 1
                file_touched = True

                predicted_raw = row.get("Predicted Generation (MW)")
                try:
                    predicted_mw = float(predicted_raw)
                    matched_rows.append((predicted_mw, actual_mw))
                except (TypeError, ValueError):
                    pass

        if file_touched:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, restval="", extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

    return updated_count, matched_rows


def _compute_error_metrics(matched_rows: list) -> dict:
    """Computes MAE, RMSE, MAPE (skipping zero-actual rows to avoid
    divide-by-zero), and Bias (mean signed error, +ve = over-predicting)."""
    if not matched_rows:
        return {}

    n = len(matched_rows)
    abs_errors = [abs(pred - actual) for pred, actual in matched_rows]
    signed_errors = [pred - actual for pred, actual in matched_rows]
    squared_errors = [(pred - actual) ** 2 for pred, actual in matched_rows]

    mae = sum(abs_errors) / n
    rmse = (sum(squared_errors) / n) ** 0.5
    bias = sum(signed_errors) / n

    pct_errors = [
        abs(pred - actual) / actual for pred, actual in matched_rows if actual > 0
    ]
    mape = (sum(pct_errors) / len(pct_errors) * 100) if pct_errors else None

    return {
        "n_matched_blocks": n,
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "mape_pct": round(mape, 2) if mape is not None else None,
        "bias": round(bias, 4),
    }


def _compute_metrics_from_schedule_vs_actual(schedule_rows: list[dict]) -> dict:
    """Compute metrics using schedule-vs-actual signed error convention.

    The schedule CSV stores forecast MW values, while the meter file stores
    actual plant output. We use forecast - actual as the signed error so
    positive bias means over-forecasting.
    """
    if not schedule_rows:
        return {}

    pairs = [(row["predicted_mw"], row["actual_mw"]) for row in schedule_rows]
    metrics = _compute_error_metrics(pairs)
    if not metrics:
        return {}

    # Preserve the schedule-vs-actual interpretation for callers that read
    # the context JSON later.
    metrics["bias"] = round(metrics["bias"], 4)
    return metrics


def _log_accuracy(metrics: dict, date_str: str = None) -> None:
    """Appends a metrics row to the running accuracy log, so accuracy over
    time can be reviewed later. Defaults to today's date; pass date_str
    explicitly when logging metrics for a day other than today (e.g. a
    company export dropped a day late)."""
    ACCURACY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    row = {"date": date_str or datetime.date.today().isoformat(), **metrics}
    file_exists = ACCURACY_LOG_PATH.exists()

    with open(ACCURACY_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _push_state_to_s3_if_enabled() -> None:
    """Mirror the local persistent state to S3 when state sync is enabled."""
    if not state_sync.is_enabled():
        return

    try:
        result = state_sync.push_state_to_s3()
        if result.uploaded or result.deleted_remote:
            print(
                f"  Synced persistent state to S3: uploaded {result.uploaded} file(s) "
                f"and removed {result.deleted_remote} stale object(s)."
            )
    except Exception as exc:
        print(f"  [WARN] Could not sync persistent state to S3: {exc}")


def _daily_revision_feedback_path(date_str: str) -> Path:
    return config.FEEDBACK_ANALYSIS_DIR / config.PLANT_NAME / f"{config.PLANT_NAME}_{date_str}.json"


def _legacy_daily_revision_feedback_path(date_str: str) -> Path:
    """Backward-compatible path for older date-only filenames."""
    return config.FEEDBACK_ANALYSIS_DIR / config.PLANT_NAME / f"{date_str}.json"


def _empty_daily_revision_feedback(date_str: str) -> dict:
    return {
        "schema_version": 1,
        "type": "daily_revision_error_analysis",
        "plant_name": config.PLANT_NAME,
        "date": date_str,
        "revision_count": 0,
        "revisions": [],
        "daily_summary": {
            "revision_count": 0,
            "latest_revision_id": None,
            "latest_revision_time": None,
            "latest_summary": None,
            "latest_step_metrics": {},
            "best_step": None,
            "worst_step": None,
            "revision_times": [],
            "average_metrics": {},
        },
    }


def _load_daily_revision_feedback(date_str: str) -> dict:
    path = _daily_revision_feedback_path(date_str)
    if not path.exists():
        legacy_path = _legacy_daily_revision_feedback_path(date_str)
        path = legacy_path if legacy_path.exists() else path
    if not path.exists():
        return _empty_daily_revision_feedback(date_str)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_daily_revision_feedback(date_str)
    if not isinstance(payload, dict):
        return _empty_daily_revision_feedback(date_str)
    payload.setdefault("schema_version", 1)
    payload.setdefault("type", "daily_revision_error_analysis")
    payload.setdefault("plant_name", config.PLANT_NAME)
    payload.setdefault("date", date_str)
    payload.setdefault("revisions", [])
    payload.setdefault("daily_summary", {})
    return payload


def _extract_revision_metadata(schedule_csv_path: Path, source_label: str, entry_date: str) -> tuple[str, str]:
    name = schedule_csv_path.name
    match = re.search(r"(\d{4}-\d{2}-\d{2})[_-](\d{2}-\d{2})", name)
    if match:
        revision_time = f"{match.group(1)} {match.group(2).replace('-', ':')}"
        return revision_time, revision_time
    try:
        stamp = datetime.datetime.fromtimestamp(schedule_csv_path.stat().st_mtime)
        revision_time = stamp.strftime("%Y-%m-%d %H:%M:%S")
        return revision_time, revision_time
    except OSError:
        pass
    if source_label.strip():
        revision_id = f"{entry_date} {source_label.strip()}"
        return revision_id, entry_date
    return entry_date, entry_date


def _build_revision_blocks(matched: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for item in matched:
        actual_mw = float(item["actual_mw"])
        schedule_row = item.get("row", {}) if isinstance(item.get("row"), dict) else {}
        step1_mw = _extract_named_mw(schedule_row, "Step 1 Meter Base Forecast MW", "Step 1 LLM MW", "anchor_mw")
        step2_mw = _extract_named_mw(schedule_row, "Step 2 Weather + Video Adjusted MW", "Step 2 Weather + Video MW")
        step3_mw = _extract_named_mw(schedule_row, "Step 3 Plant Performance MW")
        step4_mw = _extract_named_mw(
            schedule_row,
            "Step 4 Revision Feedback MW",
            "Step 4 Revision Feedback Adjusted MW",
            "Step 4 Context Adjusted MW",
            "Step 4 Context MW",
            "Step 4 Context MW ",
        )
        llm_schedule_mw = _extract_named_mw(schedule_row, "LLM Schedule (MW)", "LLM MW")
        schedule_mw = _extract_named_mw(schedule_row, "Schedule MW", "Final Validated MW")
        if schedule_mw is None:
            schedule_mw = llm_schedule_mw
        if llm_schedule_mw is None:
            llm_schedule_mw = schedule_mw
        if step4_mw is None:
            step4_mw = llm_schedule_mw
        if step3_mw is None:
            step3_mw = step4_mw
        if step2_mw is None:
            step2_mw = step3_mw
        if step1_mw is None:
            step1_mw = step2_mw
        step_values = {
            "step1_mw": step1_mw,
            "step2_mw": step2_mw,
            "step3_mw": step3_mw,
            "step4_mw": step4_mw,
            "llm_schedule_mw": llm_schedule_mw,
            "schedule_mw": schedule_mw,
        }
        error_values = {
            key: round(value - actual_mw, 4) if value is not None else None
            for key, value in step_values.items()
        }
        final_validated_mw = schedule_mw if schedule_mw is not None else llm_schedule_mw
        if final_validated_mw is None:
            final_validated_mw = step4_mw
        blocks.append({
            "time": item["time"],
            "step1_mw": round(step1_mw, 4) if step1_mw is not None else None,
            "step2_mw": round(step2_mw, 4) if step2_mw is not None else None,
            "step3_mw": round(step3_mw, 4) if step3_mw is not None else None,
            "step4_mw": round(step4_mw, 4) if step4_mw is not None else None,
            "llm_schedule_mw": round(llm_schedule_mw, 4) if llm_schedule_mw is not None else None,
            "schedule_mw": round(schedule_mw, 4) if schedule_mw is not None else None,
            "final_validated_mw": round(final_validated_mw, 4) if final_validated_mw is not None else None,
            "actual_mw": round(actual_mw, 4),
            "errors": error_values,
            "error_mw": error_values.get("schedule_mw"),
            "abs_error_mw": round(abs(error_values.get("schedule_mw")), 4) if error_values.get("schedule_mw") is not None else None,
            "pct_error": round(abs(error_values.get("schedule_mw")) / actual_mw * 100.0, 2) if actual_mw > 0 and error_values.get("schedule_mw") is not None else None,
        })
    return blocks


def _compute_step_metrics(blocks: list[dict]) -> dict:
    """Compute error metrics for each forecast step against actual MW."""
    step_keys = (
        "step1_mw",
        "step2_mw",
        "step3_mw",
        "step4_mw",
        "llm_schedule_mw",
        "schedule_mw",
    )
    metrics = {}
    for step_key in step_keys:
        pairs = []
        for block in blocks:
            actual_mw = block.get("actual_mw")
            forecast_mw = block.get(step_key)
            if isinstance(actual_mw, (int, float)) and isinstance(forecast_mw, (int, float)):
                pairs.append((forecast_mw, actual_mw))
        metrics[step_key] = _compute_error_metrics(pairs)
    return metrics


def _best_and_worst_steps(step_metrics: dict) -> tuple[str | None, str | None]:
    """Return the lowest-MAE and highest-MAE steps, ignoring empty metrics."""
    scored = []
    for step_name, metrics in step_metrics.items():
        if not isinstance(metrics, dict):
            continue
        mae = metrics.get("mae")
        if isinstance(mae, (int, float)):
            scored.append((float(mae), step_name))
    if not scored:
        return None, None
    scored.sort()
    return scored[0][1], scored[-1][1]


def _update_daily_revision_summary(payload: dict) -> dict:
    revisions = payload.get("revisions", [])
    if not isinstance(revisions, list):
        revisions = []

    revision_times = []
    metric_totals = {
        "mae": [],
        "rmse": [],
        "mape_pct": [],
        "bias": [],
    }

    for revision in revisions:
        if not isinstance(revision, dict):
            continue
        revision_time = revision.get("revision_time")
        if revision_time:
            revision_times.append(revision_time)
        analysis = revision.get("analysis") if isinstance(revision.get("analysis"), dict) else {}
        metrics = analysis.get("metrics") if isinstance(analysis.get("metrics"), dict) else {}
        for key in metric_totals:
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                metric_totals[key].append(float(value))

    average_metrics = {
        key: round(sum(values) / len(values), 4) if values else None
        for key, values in metric_totals.items()
    }

    latest_revision = revisions[-1] if revisions else {}
    latest_analysis = latest_revision.get("analysis") if isinstance(latest_revision.get("analysis"), dict) else {}
    latest_metrics = latest_analysis.get("metrics") if isinstance(latest_analysis.get("metrics"), dict) else {}
    latest_step_metrics = latest_analysis.get("step_metrics") if isinstance(latest_analysis.get("step_metrics"), dict) else {}
    best_step, worst_step = _best_and_worst_steps(latest_step_metrics)

    payload["revision_count"] = len(revisions)
    payload["daily_summary"] = {
        "revision_count": len(revisions),
        "latest_revision_id": latest_revision.get("revision_id"),
        "latest_revision_time": latest_revision.get("revision_time"),
        "latest_summary": latest_analysis.get("summary"),
        "latest_metrics": latest_metrics,
        "latest_step_metrics": latest_step_metrics,
        "best_step": best_step,
        "worst_step": worst_step,
        "revision_times": revision_times,
        "average_metrics": average_metrics,
    }
    return payload


def _derive_revision_lessons(payload: dict) -> list[str]:
    """Turn the latest revision entry into compact, next-run lessons."""
    revisions = payload.get("revisions", [])
    if not isinstance(revisions, list) or not revisions:
        return []

    latest_revision = revisions[-1] if isinstance(revisions[-1], dict) else {}
    latest_analysis = latest_revision.get("analysis") if isinstance(latest_revision.get("analysis"), dict) else {}
    latest_metrics = latest_analysis.get("metrics") if isinstance(latest_analysis.get("metrics"), dict) else {}

    lesson_entry = {
        "date": payload.get("date", ""),
        "bias": latest_metrics.get("bias"),
        "mae": latest_metrics.get("mae"),
        "time_of_day_bias": latest_analysis.get("time_of_day_bias") if isinstance(latest_analysis.get("time_of_day_bias"), dict) else {},
        "actual_pattern": latest_analysis.get("actual_pattern") if isinstance(latest_analysis.get("actual_pattern"), dict) else {},
        "worst_block": latest_analysis.get("worst_block") if isinstance(latest_analysis.get("worst_block"), dict) else {},
    }

    previous_entry = None
    if len(revisions) >= 2 and isinstance(revisions[-2], dict):
        previous_analysis = revisions[-2].get("analysis") if isinstance(revisions[-2].get("analysis"), dict) else {}
        previous_metrics = previous_analysis.get("metrics") if isinstance(previous_analysis.get("metrics"), dict) else {}
        previous_entry = {
            "date": revisions[-2].get("revision_time", revisions[-2].get("revision_id", "previous revision")),
            "mae": previous_metrics.get("mae"),
        }

    return _derive_entry_lessons(lesson_entry, previous_entry=previous_entry)


def _save_daily_revision_feedback(payload: dict) -> Path:
    date_str = payload.get("date") or datetime.date.today().isoformat()
    path = _daily_revision_feedback_path(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def format_daily_revision_feedback_for_prompt(date_value=None) -> str:
    """Render the day-level revision feedback JSON as compact prompt text."""
    if date_value is None:
        date_str = datetime.date.today().isoformat()
    elif isinstance(date_value, datetime.datetime):
        date_str = date_value.date().isoformat()
    elif isinstance(date_value, datetime.date):
        date_str = date_value.isoformat()
    else:
        date_str = str(date_value).strip()[:10]

    payload = _load_daily_revision_feedback(date_str)
    revisions = payload.get("revisions", []) if isinstance(payload.get("revisions", []), list) else []
    daily_summary = payload.get("daily_summary", {}) if isinstance(payload.get("daily_summary"), dict) else {}
    if not revisions:
        return (
            f"No revision-level feedback JSON is available yet for "
            f"{payload.get('plant_name', config.PLANT_NAME)} on {payload.get('date', date_str)}."
        )

    lines = [
        f"Daily revision feedback JSON for {payload.get('plant_name', config.PLANT_NAME)} on {payload.get('date', date_str)}:",
    ]
    if daily_summary.get("revision_count") is not None:
        lines.append(f"- Revision count so far: {daily_summary.get('revision_count')}")
    if daily_summary.get("latest_summary"):
        lines.append(f"- Latest revision summary: {daily_summary.get('latest_summary')}")

    lessons = _derive_revision_lessons(payload)
    if lessons:
        lines.append("- Lessons to apply next revision:")
        lines.extend(f"  - {lesson}" for lesson in lessons)

    latest_metrics = daily_summary.get("latest_metrics") if isinstance(daily_summary.get("latest_metrics"), dict) else {}
    if latest_metrics:
        metrics_text = ", ".join(
            f"{key}={latest_metrics[key]}"
            for key in ("n_matched_blocks", "mae", "rmse", "mape_pct", "bias")
            if latest_metrics.get(key) is not None
        )
        if metrics_text:
            lines.append(f"- Latest metrics: {metrics_text}")
    latest_step_metrics = daily_summary.get("latest_step_metrics") if isinstance(daily_summary.get("latest_step_metrics"), dict) else {}
    if latest_step_metrics:
        step_lines = []
        for step_name in ("step1_mw", "step2_mw", "step3_mw", "step4_mw", "llm_schedule_mw", "schedule_mw"):
            step_metric = latest_step_metrics.get(step_name) if isinstance(latest_step_metrics.get(step_name), dict) else {}
            if step_metric and step_metric.get("mae") is not None:
                step_lines.append(f"{step_name} MAE={step_metric.get('mae')}")
        if step_lines:
            lines.append("- Latest step metrics: " + "; ".join(step_lines))
    best_step = daily_summary.get("best_step")
    worst_step = daily_summary.get("worst_step")
    if best_step or worst_step:
        lines.append(
            "- Step ranking: "
            + ", ".join(
                part for part in (
                    f"best={best_step}" if best_step else "",
                    f"worst={worst_step}" if worst_step else "",
                )
                if part
            )
        )
    average_metrics = daily_summary.get("average_metrics") if isinstance(daily_summary.get("average_metrics"), dict) else {}
    if average_metrics:
        avg_text = ", ".join(
            f"{key}={average_metrics[key]}"
            for key in ("mae", "rmse", "mape_pct", "bias")
            if average_metrics.get(key) is not None
        )
        if avg_text:
            lines.append(f"- Average revision metrics: {avg_text}")
    revision_times = daily_summary.get("revision_times") if isinstance(daily_summary.get("revision_times"), list) else []
    if revision_times:
        lines.append("- Revision times: " + ", ".join(str(item) for item in revision_times))

    latest_revision = revisions[-1] if revisions and isinstance(revisions[-1], dict) else {}
    analysis = latest_revision.get("analysis") if isinstance(latest_revision.get("analysis"), dict) else {}
    if analysis.get("summary"):
        lines.append(f"- Latest block feedback: {analysis.get('summary')}")
    time_of_day_bias = analysis.get("time_of_day_bias") if isinstance(analysis.get("time_of_day_bias"), dict) else {}
    if time_of_day_bias:
        lines.append(
            "- Latest time-of-day bias: "
            + "; ".join(f"{bucket}: {value:+.3f} MW" for bucket, value in time_of_day_bias.items() if isinstance(value, (int, float)))
        )
    worst_block = analysis.get("worst_block") if isinstance(analysis.get("worst_block"), dict) else {}
    if worst_block.get("time"):
        lines.append(
            f"- Latest worst block: {worst_block.get('time')} "
            f"(error {worst_block.get('error_mw')})"
        )
    blocks = analysis.get("blocks") if isinstance(analysis.get("blocks"), list) else []
    if blocks:
        preview = []
        for item in blocks[:5]:
            if not isinstance(item, dict):
                continue
            preview.append(
                f"{item.get('time')}: final_validated_mw={item.get('final_validated_mw')}, "
                f"actual_mw={item.get('actual_mw')}, error_mw={item.get('error_mw')}"
            )
        if preview:
            lines.append("- Block feedback preview:")
            lines.extend(f"  {line}" for line in preview)

    return "\n".join(lines)


def _record_daily_revision_feedback(
    date_str: str,
    *,
    revision_id: str,
    revision_time: str,
    source_label: str,
    schedule_csv_path: Path,
    actual_meter_csv_path: Path,
    entry: dict,
    matched: list[dict],
    skipped_missing: int,
    actual_pattern: dict | None,
) -> dict:
    payload = _load_daily_revision_feedback(date_str)
    revision_entry = {
        "revision_id": revision_id,
        "revision_time": revision_time,
        "source": source_label,
        "schedule_csv": schedule_csv_path.name,
        "actual_meter_csv": actual_meter_csv_path.name,
        "matched_blocks": len(matched),
        "skipped_missing_blocks": skipped_missing,
        "analysis": {
            "metrics": {
                "n_matched_blocks": entry.get("n_matched_blocks"),
                "mae": entry.get("mae"),
                "rmse": entry.get("rmse"),
                "mape_pct": entry.get("mape_pct"),
                "bias": entry.get("bias"),
            },
            "step_metrics": _compute_step_metrics(_build_revision_blocks(matched)),
            "summary": entry.get("summary"),
            "bias_direction": entry.get("bias_direction"),
            "time_of_day_bias": entry.get("time_of_day_bias", {}),
            "worst_block": entry.get("worst_block", {}),
            "actual_pattern": actual_pattern,
            "blocks": _build_revision_blocks(matched),
        },
    }

    revisions = payload.get("revisions", [])
    if not isinstance(revisions, list):
        revisions = []
    revisions = [rev for rev in revisions if not (isinstance(rev, dict) and rev.get("revision_id") == revision_id)]
    revisions.append(revision_entry)
    payload["revisions"] = revisions
    payload = _update_daily_revision_summary(payload)
    saved_path = _save_daily_revision_feedback(payload)
    print(f"  [FEEDBACK] Updated daily revision feedback JSON: {saved_path.resolve()}")
    return revision_entry


def _load_processed_raw_meter_names() -> set[str]:
    if not RAW_METER_MANIFEST_PATH.exists():
        return set()
    try:
        data = json.loads(RAW_METER_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, list):
        return {str(item) for item in data if str(item).strip()}
    return set()


def _save_processed_raw_meter_names(names: set[str]) -> None:
    RAW_METER_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_METER_MANIFEST_PATH.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")


def sync_historic_case_actuals() -> int:
    """Join every compatible SCADA CSV in ``historic_cases`` to the case store.

    This is safe to call before every forecast: it only fills actual values
    for timestamps already captured by the pipeline and does not create
    duplicate rows or accuracy-log entries.
    """
    if not _case_store_paths():
        return 0

    readings = {}
    for csv_path in config.HISTORIC_CASES_DIR.glob("*.csv"):
        try:
            readings.update(_load_actual_readings(str(csv_path)))
        except (OSError, SystemExit):
            # A folder can contain auxiliary CSVs; only files matching the
            # configured SCADA timestamp/power columns are relevant here.
            continue
    if not readings:
        return 0
    updated_count, _ = _update_case_store_with_actuals(readings)
    return updated_count


# Timestamp formats accepted from a company export, tried in this order.
# Real exports have shown up in more than one of these -- e.g. after
# someone opens the CSV in Excel and saves it, which silently reorders
# the date (DD-MM-YYYY instead of YYYY-MM-DD) and can drop the seconds.
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M")


def _normalize_timestamp(raw_ts: str) -> str:
    """Parses raw_ts against every format in _TIMESTAMP_FORMATS and
    returns it re-formatted as "%Y-%m-%d %H:%M:%S" (the format the rest of
    the pipeline expects) -- or None if raw_ts matches none of them."""
    raw_ts = (raw_ts or "").strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.datetime.strptime(raw_ts, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _merge_meter_csv_into_store(csv_path: Path) -> set:
    """
    Appends one raw company meter-export CSV (TimeStamp + Active Power in
    kW + sensor columns -- no MW column yet, the same shape the plant
    sends every evening) into historic_cases/merged_scada_data.csv, keyed
    by timestamp so re-dropping the same file twice updates rows instead
    of duplicating them.

    Negative Active Power (kW) readings are clipped to 0 (no generation --
    the same convention already used everywhere else in this project), and
    Active Power (MW) is derived from the clipped kW value.

    Blank rows and rows with an unparseable timestamp or power value are
    skipped individually (with a count printed) instead of failing the
    whole file -- real exports sometimes have padding rows or a
    reformatted date (see _normalize_timestamp) after being opened in a
    spreadsheet tool, and one bad row shouldn't discard an entire day's
    otherwise-good data.

    Returns the set of calendar-date strings ("YYYY-MM-DD") the file
    touched, so the caller knows which day(s) to analyze.
    """
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        input_fieldnames = list(reader.fieldnames or [])
        timestamp_column = _pick_first_existing_column(input_fieldnames, RAW_METER_TIMESTAMP_COLUMNS)
        power_column = _pick_first_existing_column(input_fieldnames, RAW_METER_POWER_COLUMNS)
        if timestamp_column is None or power_column is None:
            raise ValueError(
                f"unexpected columns {reader.fieldnames} -- missing required timestamp/power columns "
                f"from accepted sets {RAW_METER_TIMESTAMP_COLUMNS} / {RAW_METER_POWER_COLUMNS}"
            )
        new_rows = list(reader)

    merged_header = ["TimeStamp", "Active Power (kW)", "Active Power (MW)"]
    existing_by_time = {}
    existing_fieldnames = []
    if MERGED_STORE_PATH.exists():
        with open(MERGED_STORE_PATH, "r", newline="", encoding="utf-8") as f:
            existing_reader = csv.DictReader(f)
            existing_fieldnames = list(existing_reader.fieldnames or [])
            for row in existing_reader:
                row_timestamp = row.get("TimeStamp") or row.get("Timestamp")
                if row_timestamp:
                    # Keep only the canonical timestamp key in the merged store.
                    row.pop("Timestamp", None)
                    existing_by_time[row_timestamp] = row

    for column in existing_fieldnames + input_fieldnames:
        if column and column not in merged_header and column != "Timestamp":
            merged_header.append(column)

    touched_dates = set()
    skipped_rows = 0
    for row in new_rows:
        timestamp = _normalize_timestamp(row.get(timestamp_column))
        kw = _as_float(row.get(power_column))
        if timestamp is None or kw is None:
            skipped_rows += 1
            continue

        kw = max(0.0, kw)
        row = dict(row)
        row["TimeStamp"] = timestamp
        row.pop("Timestamp", None)
        row["Active Power (kW)"] = str(kw)
        row["Active Power (MW)"] = str(kw / 1000.0)
        existing_by_time[timestamp] = row
        touched_dates.add(timestamp[:10])

    if skipped_rows:
        print(f"  [INFO] {csv_path.name}: skipped {skipped_rows} row(s) with a blank/unparseable "
              f"timestamp or power value.")

    sorted_times = sorted(
        existing_by_time.keys(),
        key=lambda t: datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
    )
    with open(MERGED_STORE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=merged_header)
        writer.writeheader()
        for t in sorted_times:
            writer.writerow(existing_by_time[t])

    return touched_dates


def _load_features_log_rows_for_date(date_str: str) -> list:
    """Returns every row from that date's features_log file that has both
    a predicted and a (now-synced) actual generation value."""
    path = config.FEATURES_LOG_DIR / f"{config.PLANT_NAME}_features_log_{date_str}.csv"
    if not path.exists():
        return []

    matched = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                predicted_mw = float(row.get("Predicted Generation (MW)"))
                actual_mw = float(row.get(ACTUAL_COLUMN_NAME))
            except (TypeError, ValueError):
                continue
            matched.append({
                "time": row["Time"], "predicted_mw": predicted_mw,
                "actual_mw": actual_mw, "row": row,
            })
    return matched


def _time_of_day_bucket(time_label: str) -> str:
    hour = int(time_label[11:13])
    if hour < 10:
        return "morning (before 10:00)"
    if hour < 14:
        return "midday (10:00-14:00)"
    return "afternoon (14:00+)"


def bucket_name_for_time(value) -> str:
    """Return the stable time-of-day bucket used for revision memory."""
    if isinstance(value, datetime.datetime):
        hour = value.hour
    elif isinstance(value, datetime.date):
        hour = 0
    else:
        text = str(value or "").strip()
        if len(text) >= 13 and text[10] == " ":
            try:
                hour = int(text[11:13])
            except ValueError:
                hour = 0
        else:
            try:
                hour = int(text[:2])
            except ValueError:
                hour = 0
    if hour < 10:
        return "morning (before 10:00)"
    if hour < 14:
        return "midday (10:00-14:00)"
    return "afternoon (14:00+)"


def get_recent_time_of_day_biases() -> dict:
    """Return the rolling time-of-day bias map from the prediction context."""
    context = _load_context()
    recent = context.get("recent_summary", {}) if isinstance(context.get("recent_summary"), dict) else {}
    bias = recent.get("time_of_day_bias", {}) if isinstance(recent.get("time_of_day_bias"), dict) else {}
    return {
        str(key): float(value)
        for key, value in bias.items()
        if isinstance(value, (int, float))
    }


def recommend_final_clamp_factor(block_time, live_state: dict | None = None) -> tuple[float, str]:
    """Suggest a conservative final-stage clamp factor for a forecast block."""
    factor = 1.0
    bucket = bucket_name_for_time(block_time)
    bucket_bias = get_recent_time_of_day_biases().get(bucket)

    if live_state and isinstance(live_state.get("live_residual_factor"), (int, float)):
        live_factor = float(live_state["live_residual_factor"])
        if live_factor < 1.0:
            factor = min(factor, max(0.55, live_factor))

    if isinstance(bucket_bias, (int, float)) and bucket_bias > 0.01:
        scale = min(0.22, abs(bucket_bias) / max(config.PLANT_CAPACITY_MW * 0.25, 0.25))
        factor = min(factor, max(0.68, 1.0 - scale))

    return round(factor, 3), bucket


def recommend_stepwise_correction_factors(block_time, live_state: dict | None = None) -> dict:
    """Return progressively stronger correction factors for step 1 through step 4.

    The factors are intentionally conservative when the current day is
    already underperforming the physics ramp, and they get stronger from
    Step 1 through Step 4 so the model can correct in stages instead of
    only at the final commit.
    """
    bucket = bucket_name_for_time(block_time)
    bucket_bias = get_recent_time_of_day_biases().get(bucket)
    live_factor = 1.0
    fluctuation_flag = False
    if live_state:
        live_factor = float(live_state.get("live_residual_factor", 1.0) or 1.0)
        fluctuation_flag = bool(live_state.get("fluctuation_flag"))

    base_factor = max(0.55, min(1.15, live_factor))
    bucket_adjustment = 0.0
    if isinstance(bucket_bias, (int, float)) and bucket_bias > 0.01:
        bucket_adjustment = -min(0.24, abs(bucket_bias) / max(config.PLANT_CAPACITY_MW * 0.20, 0.20))
    elif isinstance(bucket_bias, (int, float)) and bucket_bias < -0.01:
        bucket_adjustment = min(0.15, abs(bucket_bias) / max(config.PLANT_CAPACITY_MW * 0.25, 0.25))

    if fluctuation_flag:
        base_factor = max(0.55, min(1.12, base_factor * 0.97))
        if bucket_adjustment < 0:
            bucket_adjustment -= 0.02

    def _stage_factor(strength: float) -> float:
        return round(max(0.55, min(1.15, base_factor + (bucket_adjustment * strength))), 3)

    return {
        "bucket": bucket,
        "bucket_bias": round(float(bucket_bias), 3) if isinstance(bucket_bias, (int, float)) else None,
        "base_factor": round(base_factor, 3),
        "step1_factor": _stage_factor(0.25),
        "step2_factor": _stage_factor(0.55),
        "step3_factor": _stage_factor(0.80),
        "step4_factor": _stage_factor(1.00),
    }


def _analyze_day_patterns(date_str: str, matched: list) -> dict:
    """
    Deterministic (no LLM) day-level error + pattern analysis: overall
    MAE/RMSE/MAPE/Bias, bias broken down by time-of-day bucket, and the
    single worst-forecast block with the conditions (engineered features)
    present at that time -- packaged as a compact human-readable summary
    that later gets fed into the LLM prompt as evidence.
    """
    pairs = [(m["predicted_mw"], m["actual_mw"]) for m in matched]
    metrics = _compute_error_metrics(pairs)

    buckets = {}
    for m in matched:
        buckets.setdefault(_time_of_day_bucket(m["time"]), []).append(m["predicted_mw"] - m["actual_mw"])
    bucket_bias = {bucket: round(sum(errs) / len(errs), 3) for bucket, errs in buckets.items()}

    worst = max(matched, key=lambda m: abs(m["predicted_mw"] - m["actual_mw"]))
    notable_bits = []
    for key, label in (
        ("clouds_bright_pixel_pct", "cloud-layer bright-pixel %"),
        ("satellite_bright_pixel_pct", "satellite bright-pixel %"),
        ("motion_coverage_end_pct", "video cloud coverage %"),
        ("motion_direction_deg", "cloud motion direction (deg, -1=stationary)"),
        ("solar_elevation_deg", "solar elevation (deg)"),
    ):
        if worst["row"].get(key):
            notable_bits.append(f"{label}={worst['row'][key]}")

    bias = metrics["bias"]
    bias_direction = "over-forecast" if bias > 0.01 else ("under-forecast" if bias < -0.01 else "roughly balanced")
    mape_str = f"{metrics['mape_pct']}%" if metrics.get("mape_pct") is not None else "n/a"
    bucket_text = "; ".join(
        f"{bucket}: {bucket_bias[bucket]:+} MW" for bucket in sorted(bucket_bias)
    )
    worst_error = worst["predicted_mw"] - worst["actual_mw"]

    summary = (
        f"{date_str}: MAE={metrics['mae']} MW, RMSE={metrics['rmse']} MW, MAPE={mape_str}, "
        f"Bias={bias:+.3f} MW ({bias_direction}). By time of day -- {bucket_text}. "
        f"Worst block at {worst['time']}: predicted {worst['predicted_mw']} MW vs actual "
        f"{worst['actual_mw']} MW (error {worst_error:+.3f} MW)"
        + (f", conditions: {', '.join(notable_bits)}." if notable_bits else ".")
    )

    return {
        "date": date_str,
        "n_matched_blocks": metrics["n_matched_blocks"],
        "mae": metrics["mae"],
        "rmse": metrics["rmse"],
        "mape_pct": metrics["mape_pct"],
        "bias": bias,
        "bias_direction": bias_direction,
        "time_of_day_bias": bucket_bias,
        "worst_block": {
            "time": worst["time"], "predicted_mw": worst["predicted_mw"],
            "actual_mw": worst["actual_mw"], "error_mw": round(worst_error, 3),
        },
        "summary": summary,
    }


def _load_merged_scada_readings_for_date(date_str: str) -> list:
    """Returns [(datetime, actual_mw), ...] sorted by time, read directly
    from historic_cases/merged_scada_data.csv for one calendar date --
    independent of features_log/predictions, so this describes what the
    plant actually did that day regardless of forecast accuracy."""
    if not MERGED_STORE_PATH.exists():
        return []

    rows = []
    with open(MERGED_STORE_PATH, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts_raw = row.get("TimeStamp", "")
            if not ts_raw.startswith(date_str):
                continue
            mw = _as_float(row.get("Active Power (MW)"))
            if mw is None:
                continue
            try:
                ts = datetime.datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            rows.append((ts, mw))

    rows.sort(key=lambda pair: pair[0])
    return rows


def _analyze_actual_pattern(date_str: str, rows: list) -> dict:
    """
    Deterministic (no LLM) pattern analysis of that day's ACTUAL meter
    data ALONE -- distinct from _analyze_day_patterns, which measures how
    well OUR PREDICTION did. This captures what kind of day it actually
    was (heavy/light cloud attenuation, choppy vs steady, peak output),
    which stays useful context for future days even on a day our forecast
    happened to nail (or badly miss).

    Uses the same elevation-only clear-sky ceiling and choppiness
    threshold as format_intraday_actuals_for_prompt, but computed over
    the WHOLE day (this runs after the day is over) rather than a
    same-day rolling window.
    """
    import math
    import time_features

    if len(rows) < 2:
        return None

    ratios = []
    bucket_ratios = {}
    for ts, mw in rows:
        elevation = time_features.compute_time_features(ts)["solar_elevation_deg"]
        if elevation <= 0:
            continue
        clear_sky_mw = config.PLANT_CAPACITY_MW * math.sin(math.radians(elevation)) * config.PERFORMANCE_RATIO
        if clear_sky_mw > 0.05:
            ratio = mw / clear_sky_mw
            ratios.append(ratio)
            bucket_ratios.setdefault(_time_of_day_bucket(ts.strftime("%Y-%m-%d %H:%M")), []).append(ratio)

    diffs = [abs(rows[i][1] - rows[i - 1][1]) for i in range(1, len(rows))]
    avg_abs_step = sum(diffs) / len(diffs) if diffs else 0.0
    choppy = avg_abs_step > CHOPPY_AVG_STEP_MW

    peak_ts, peak_mw = max(rows, key=lambda pair: pair[1])

    avg_clear_sky_pct = (sum(ratios) / len(ratios) * 100.0) if ratios else None
    bucket_pct = {b: round(sum(vals) / len(vals) * 100.0, 1) for b, vals in bucket_ratios.items()}

    clear_sky_str = f"{avg_clear_sky_pct:.0f}%" if avg_clear_sky_pct is not None else "n/a"
    bucket_text = "; ".join(f"{b}: {pct}%" for b, pct in sorted(bucket_pct.items())) or "n/a"
    choppiness_str = "CHOPPY (patchy/intermittent cloud)" if choppy else "smooth/steady"

    summary = (
        f"{date_str} actual generation pattern: averaged {clear_sky_str} of clear-sky ceiling "
        f"across the day (by time of day -- {bucket_text}). Block-to-block variability: "
        f"avg |change|={avg_abs_step:.3f} MW -- {choppiness_str}. Peak output {peak_mw:.3f} MW at "
        f"{peak_ts.strftime('%H:%M')}."
    )

    return {
        "n_readings": len(rows),
        "avg_clear_sky_pct": round(avg_clear_sky_pct, 1) if avg_clear_sky_pct is not None else None,
        "time_of_day_clear_sky_pct": bucket_pct,
        "avg_abs_step_mw": round(avg_abs_step, 3),
        "choppy": choppy,
        "peak_mw": round(peak_mw, 3),
        "peak_time": peak_ts.strftime("%H:%M"),
        "summary": summary,
    }


def _load_context() -> dict:
    ensure_prediction_context_exists()
    if not config.PREDICTION_CONTEXT_PATH.exists():
        return _empty_prediction_context()
    try:
        payload = json.loads(config.PREDICTION_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_prediction_context()
    return _normalize_prediction_context(payload)


def ensure_prediction_context_exists() -> None:
    """Create an empty rolling context file on first run if it is missing."""
    if config.PREDICTION_CONTEXT_PATH.exists():
        return
    config.PREDICTION_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PREDICTION_CONTEXT_PATH.write_text(
        json.dumps(_empty_prediction_context(), indent=2),
        encoding="utf-8",
    )
    print(
        f"  [CTX] Created fresh prediction context at {config.PREDICTION_CONTEXT_PATH.resolve()} "
        f"(initialized to structured empty context)"
    )


def _add_day_to_context(entry: dict) -> None:
    """Adds/replaces today's entry inside the structured rolling context."""
    context = _load_context()
    normalized_entry = _normalize_context_entry(entry)
    entries = [e for e in context["entries"] if e.get("date") != normalized_entry["date"]]
    entries.append(normalized_entry)
    entries = sorted(entries, key=lambda e: e["date"])[-config.CONTEXT_WINDOW_DAYS:]
    context["entries"] = entries
    context["recent_summary"] = _build_recent_summary(entries)
    context["llm_context_summary"] = _build_llm_context_summary(context)

    config.PREDICTION_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PREDICTION_CONTEXT_PATH.write_text(json.dumps(context, indent=2), encoding="utf-8")
    print(f"  [CTX] Updated prediction context for {normalized_entry['date']} -> {config.PREDICTION_CONTEXT_PATH.resolve()}")


def format_context_for_prompt() -> str:
    """Renders the rolling day-level context as compact text for
    llm_predictor.py's prompt. Called every run regardless of whether the
    local feedback flow had new files this run -- it always reflects the last
    CONTEXT_WINDOW_DAYS days of accumulated learnings."""
    context = _load_context()
    entries = context["entries"]
    if not entries:
        return "No recent day-level accuracy history is available yet."

    lines = [
        f"Plant context for {context['plant_profile']['plant_name']} "
        f"({context['plant_profile']['capacity_mw']} MW, {context['plant_profile']['block_minutes']}-min blocks):",
    ]
    for line in context.get("llm_context_summary", []):
        lines.append(f"- {line}")
    lines.append(
        f"Recent day-level forecast accuracy and patterns (last {len(entries)} day(s), oldest first):"
    )
    for e in entries:
        lines.append(f"- {e['summary']}")
        if e.get("lessons"):
            lines.append("  Lessons:")
            for lesson in e["lessons"]:
                lines.append(f"    - {lesson}")
        if e.get("actual_pattern"):
            lines.append(f"  Also, {e['actual_pattern']['summary']}")
    lines.append(
        "If a bias direction or time-of-day tendency repeats across these days, factor it into "
        "your adjustment; a pattern seen on only one day is weaker evidence than one repeated "
        "across multiple days."
    )
    return "\n".join(lines)


def _empty_prediction_context() -> dict:
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "plant_profile": _build_plant_profile(),
        "recent_summary": {},
        "llm_context_summary": [],
        "entries": [],
    }


def _build_plant_profile() -> dict:
    return {
        "plant_name": config.PLANT_NAME,
        "capacity_mw": config.PLANT_CAPACITY_MW,
        "latitude": config.PLANT_LAT,
        "longitude": config.PLANT_LON,
        "timezone": "Asia/Kolkata",
        "block_minutes": config.BLOCK_MINUTES,
        "forecast_horizon_blocks": config.NUM_FORECAST_BLOCKS,
        "context_window_days": config.CONTEXT_WINDOW_DAYS,
    }


def _normalize_context_entry(entry: dict) -> dict:
    normalized = dict(entry)
    metrics = normalized.get("metrics") if isinstance(normalized.get("metrics"), dict) else {}
    metrics = {
        "mae_mw": metrics.get("mae_mw", normalized.get("mae")),
        "rmse_mw": metrics.get("rmse_mw", normalized.get("rmse")),
        "mape_pct": metrics.get("mape_pct", normalized.get("mape_pct")),
        "bias_mw": metrics.get("bias_mw", normalized.get("bias")),
    }
    normalized["metrics"] = metrics
    normalized.setdefault("time_of_day_bias", {})
    if not normalized.get("lessons"):
        normalized["lessons"] = []
    return normalized


def _normalize_prediction_context(payload) -> dict:
    if isinstance(payload, list):
        entries = [_normalize_context_entry(entry) for entry in payload if isinstance(entry, dict)]
        entries = sorted(entries, key=lambda e: e.get("date", ""))[-config.CONTEXT_WINDOW_DAYS:]
        for index, entry in enumerate(entries):
            previous_entry = entries[index - 1] if index > 0 else None
            entry["lessons"] = _derive_entry_lessons(entry, previous_entry=previous_entry)
        context = _empty_prediction_context()
        context["entries"] = entries
        context["recent_summary"] = _build_recent_summary(entries)
        context["llm_context_summary"] = _build_llm_context_summary(context)
        return context

    if not isinstance(payload, dict):
        return _empty_prediction_context()

    context = _empty_prediction_context()
    context.update(payload)

    entries = context.get("entries", [])
    if not isinstance(entries, list):
        entries = []
    entries = [_normalize_context_entry(entry) for entry in entries if isinstance(entry, dict)]
    entries = sorted(entries, key=lambda e: e.get("date", ""))[-config.CONTEXT_WINDOW_DAYS:]
    for index, entry in enumerate(entries):
        previous_entry = entries[index - 1] if index > 0 else None
        entry["lessons"] = _derive_entry_lessons(entry, previous_entry=previous_entry)
    context["entries"] = entries
    context["plant_profile"] = {**_build_plant_profile(), **(context.get("plant_profile") or {})}
    context["recent_summary"] = _build_recent_summary(entries)
    context["llm_context_summary"] = _build_llm_context_summary(context)
    context["schema_version"] = max(int(context.get("schema_version", CONTEXT_SCHEMA_VERSION) or CONTEXT_SCHEMA_VERSION), CONTEXT_SCHEMA_VERSION)
    return context


def _derive_entry_lessons(entry: dict, previous_entry: dict | None = None) -> list[str]:
    lessons: list[str] = []
    bias = entry.get("bias")
    bucket_bias = entry.get("time_of_day_bias") if isinstance(entry.get("time_of_day_bias"), dict) else {}

    if isinstance(bias, (int, float)):
        if bias > 0.01:
            lessons.append("Forecast ran high; nudge future blocks downward when conditions are similar.")
        elif bias < -0.01:
            lessons.append("Forecast ran low; nudge future blocks upward when conditions are similar.")

    if bucket_bias:
        strongest_bucket, strongest_value = max(bucket_bias.items(), key=lambda item: abs(item[1]) if isinstance(item[1], (int, float)) else -1)
        if isinstance(strongest_value, (int, float)):
            if strongest_value > 0.01:
                lessons.append(
                    f"Strongest bias came from {strongest_bucket}; that bucket ran high by {strongest_value:+.3f} MW."
                )
            elif strongest_value < -0.01:
                lessons.append(
                    f"Strongest bias came from {strongest_bucket}; that bucket ran low by {strongest_value:+.3f} MW."
                )

        for bucket_name in ("morning (before 10:00)", "midday (10:00-14:00)", "afternoon (14:00+)"):
            bucket_value = bucket_bias.get(bucket_name)
            if not isinstance(bucket_value, (int, float)):
                continue
            if bucket_value > 0.01:
                lessons.append(
                    f"{bucket_name} was over-forecast by {bucket_value:+.3f} MW; trim future blocks in that bucket more aggressively."
                )
            elif bucket_value < -0.01:
                lessons.append(
                    f"{bucket_name} was under-forecast by {bucket_value:+.3f} MW; allow a modest upward correction in that bucket."
                )

    afternoon_bias = bucket_bias.get("afternoon (14:00+)")
    if isinstance(afternoon_bias, (int, float)):
        if afternoon_bias > 0.01:
            lessons.append("Afternoon blocks specifically were over-forecast; trim late-day output a bit more.")
        elif afternoon_bias < -0.01:
            lessons.append("Afternoon blocks specifically were under-forecast; lift late-day output a bit more.")

    if previous_entry:
        prev_mae = previous_entry.get("mae")
        curr_mae = entry.get("mae")
        prev_date = previous_entry.get("date", "previous day")
        if isinstance(prev_mae, (int, float)) and isinstance(curr_mae, (int, float)):
            delta = curr_mae - prev_mae
            if delta < -0.01:
                lessons.append(
                    f"Compared with {prev_date}, MAE improved from {prev_mae:.3f} MW to {curr_mae:.3f} MW."
                )
            elif delta > 0.01:
                lessons.append(
                    f"Compared with {prev_date}, MAE worsened from {prev_mae:.3f} MW to {curr_mae:.3f} MW."
                )

    actual_pattern = entry.get("actual_pattern") if isinstance(entry.get("actual_pattern"), dict) else {}
    if actual_pattern.get("choppy"):
        lessons.append("Actual generation was choppy; use stronger correction when clouds are unstable.")
    else:
        lessons.append("Actual generation was smooth; only mild correction was needed.")

    worst_block = entry.get("worst_block") if isinstance(entry.get("worst_block"), dict) else {}
    if worst_block:
        worst_time = worst_block.get("time")
        predicted_mw = worst_block.get("predicted_mw")
        actual_mw = worst_block.get("actual_mw")
        error_mw = worst_block.get("error_mw")
        if worst_time and isinstance(predicted_mw, (int, float)) and isinstance(actual_mw, (int, float)) and isinstance(error_mw, (int, float)):
            lessons.append(
                f"Worst miss was at {worst_time}: predicted {predicted_mw:.3f} MW vs actual {actual_mw:.3f} MW "
                f"(error {error_mw:+.3f} MW)."
            )

    # Deduplicate while keeping the generated order, then cap to a compact set.
    deduped: list[str] = []
    seen: set[str] = set()
    for lesson in lessons:
        if lesson not in seen:
            deduped.append(lesson)
            seen.add(lesson)
    if not deduped:
        deduped.append("Use this day as a similar-case reference for the plant's behavior.")
    return deduped[:5]


def _extract_metric(entry: dict, key: str, fallback: str | None = None):
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    if key in metrics and metrics[key] is not None:
        return metrics[key]
    if fallback and fallback in entry:
        return entry.get(fallback)
    return entry.get(key)


def _build_recent_summary(entries: list[dict]) -> dict:
    if not entries:
        return {
            "window_days": config.CONTEXT_WINDOW_DAYS,
            "entry_count": 0,
            "rolling_mae_mw": None,
            "rolling_rmse_mw": None,
            "rolling_mape_pct": None,
            "rolling_bias_mw": None,
            "bias_direction": "no data",
            "time_of_day_bias": {},
            "regime_summary": "no recent entries",
            "correction_hint": "No recent context is available yet.",
        }

    maes = [v for v in (_extract_metric(e, "mae_mw", "mae") for e in entries) if isinstance(v, (int, float))]
    rmses = [v for v in (_extract_metric(e, "rmse_mw", "rmse") for e in entries) if isinstance(v, (int, float))]
    mapes = [v for v in (_extract_metric(e, "mape_pct") for e in entries) if isinstance(v, (int, float))]
    biases = [v for v in (_extract_metric(e, "bias_mw", "bias") for e in entries) if isinstance(v, (int, float))]

    bucket_totals: dict[str, list[float]] = {}
    choppy_days = 0
    smooth_days = 0
    for entry in entries:
        for bucket, value in (entry.get("time_of_day_bias") or {}).items():
            if isinstance(value, (int, float)):
                bucket_totals.setdefault(bucket, []).append(float(value))
        pattern = entry.get("actual_pattern") if isinstance(entry.get("actual_pattern"), dict) else {}
        if pattern.get("choppy") is True:
            choppy_days += 1
        elif pattern:
            smooth_days += 1

    time_of_day_bias = {
        bucket: round(sum(values) / len(values), 3)
        for bucket, values in sorted(bucket_totals.items())
        if values
    }
    avg_bias = sum(biases) / len(biases) if biases else 0.0
    bias_direction = "over-forecast" if avg_bias > 0.01 else ("under-forecast" if avg_bias < -0.01 else "roughly balanced")
    if choppy_days and choppy_days >= smooth_days:
        regime_summary = f"mostly choppy ({choppy_days}/{len(entries)} day(s))"
    elif smooth_days:
        regime_summary = f"mostly smooth ({smooth_days}/{len(entries)} day(s))"
    else:
        regime_summary = "mixed"

    dominant_bucket = None
    if time_of_day_bias:
        dominant_bucket = max(time_of_day_bias.items(), key=lambda item: abs(item[1]))[0]

    if avg_bias > 0.01:
        correction_hint = "Bias is positive overall, so future forecasts should lean slightly lower, especially in the strongest positive-bias time bucket."
    elif avg_bias < -0.01:
        correction_hint = "Bias is negative overall, so future forecasts should lean slightly higher, especially in the strongest negative-bias time bucket."
    else:
        correction_hint = "Overall bias is close to neutral, so use only small corrections unless the time-of-day pattern is repeating."

    if dominant_bucket and dominant_bucket in time_of_day_bias:
        correction_hint += f" The strongest time-of-day signal is {dominant_bucket} ({time_of_day_bias[dominant_bucket]:+} MW)."

    return {
        "window_days": config.CONTEXT_WINDOW_DAYS,
        "entry_count": len(entries),
        "rolling_mae_mw": round(sum(maes) / len(maes), 4) if maes else None,
        "rolling_rmse_mw": round(sum(rmses) / len(rmses), 4) if rmses else None,
        "rolling_mape_pct": round(sum(mapes) / len(mapes), 2) if mapes else None,
        "rolling_bias_mw": round(avg_bias, 4),
        "bias_direction": bias_direction,
        "time_of_day_bias": time_of_day_bias,
        "regime_summary": regime_summary,
        "correction_hint": correction_hint,
    }


def _build_llm_context_summary(context: dict) -> list[str]:
    plant = context.get("plant_profile", {}) if isinstance(context.get("plant_profile"), dict) else {}
    recent = context.get("recent_summary", {}) if isinstance(context.get("recent_summary"), dict) else {}
    entries = context.get("entries", []) if isinstance(context.get("entries"), list) else []

    lines = [
        f"{plant.get('plant_name', config.PLANT_NAME)}: {plant.get('capacity_mw', config.PLANT_CAPACITY_MW)} MW plant with "
        f"{plant.get('block_minutes', config.BLOCK_MINUTES)}-minute blocks in {plant.get('timezone', 'Asia/Kolkata')}.",
    ]
    if recent.get("rolling_bias_mw") is not None:
        lines.append(
            f"Rolling {recent.get('window_days', config.CONTEXT_WINDOW_DAYS)}-day bias: "
            f"{recent['rolling_bias_mw']:+.3f} MW ({recent.get('bias_direction', 'unknown')})."
        )
    if recent.get("rolling_mae_mw") is not None or recent.get("rolling_rmse_mw") is not None:
        lines.append(
            f"Recent accuracy: MAE={recent.get('rolling_mae_mw')}, RMSE={recent.get('rolling_rmse_mw')}, "
            f"MAPE={recent.get('rolling_mape_pct')}%."
        )
    if recent.get("time_of_day_bias"):
        time_bias = "; ".join(f"{bucket}: {value:+.3f} MW" for bucket, value in recent["time_of_day_bias"].items())
        lines.append(f"Time-of-day bias: {time_bias}.")
    if recent.get("regime_summary"):
        lines.append(f"Regime summary: {recent['regime_summary']}.")
    if recent.get("correction_hint"):
        lines.append(f"Correction hint: {recent['correction_hint']}")

    for entry in entries[-config.CONTEXT_WINDOW_DAYS:]:
        lines.append(
            f"{entry.get('date')}: {entry.get('summary')}"
        )
    return lines


CHOPPY_AVG_STEP_MW = config.PLANT_CAPACITY_MW * 0.10


def _load_intraday_rows(actuals_csv_path, reference_time: datetime.datetime) -> list:
    """Return today's meter rows up to reference_time as [(ts, mw), ...]."""
    with open(actuals_csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        timestamp_column = _pick_first_existing_column(
            reader.fieldnames,
            RAW_METER_TIMESTAMP_COLUMNS,
        )
        power_column = _pick_first_existing_column(
            reader.fieldnames,
            RAW_METER_POWER_COLUMNS,
        )
        if timestamp_column is None or power_column is None:
            return []
        rows = []
        for row in reader:
            normalized = _normalize_timestamp(row.get(timestamp_column))
            if normalized is None:
                continue
            ts = datetime.datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
            if ts > reference_time:
                continue
            mw = _power_value_to_mw(row.get(power_column), power_column)
            if mw is None:
                continue
            rows.append((ts, mw))

    rows.sort(key=lambda pair: pair[0])
    return rows


def _load_intraday_meter_rows(actuals_csv_path, reference_time: datetime.datetime) -> list[dict]:
    """Return today's meter rows up to reference_time with raw sensor fields preserved."""
    with open(actuals_csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        timestamp_column = _pick_first_existing_column(
            reader.fieldnames,
            RAW_METER_TIMESTAMP_COLUMNS,
        )
        power_column = _pick_first_existing_column(
            reader.fieldnames,
            RAW_METER_POWER_COLUMNS,
        )
        ghi_column = _pick_first_existing_column(
            reader.fieldnames,
            ("GHI (W/m2)", "GHI_W (W/m2)", "GHI_W", "GHI"),
        )
        poa_column = _pick_first_existing_column(
            reader.fieldnames,
            ("POA (W/m2)", "POA", "Plane of Array (W/m2)"),
        )
        ambient_temp_column = _pick_first_existing_column(
            reader.fieldnames,
            ("AMB TEMP", "Ambient Temperature (C)", "Ambient Temp", "Ambient Temperature"),
        )
        module_temp_column = _pick_first_existing_column(
            reader.fieldnames,
            ("MOD TEMP", "Module Temperature (C)", "Module Temp", "Module Temperature"),
        )
        wind_speed_column = _pick_first_existing_column(
            reader.fieldnames,
            ("Wind Speed (m/s)", "Wind Speed", "WindSpeed"),
        )
        wind_direction_column = _pick_first_existing_column(
            reader.fieldnames,
            ("Wind Direction (DEG.)", "Wind Direction (Deg.)", "Wind Direction", "Wind Direction (DEG)"),
        )
        humidity_column = _pick_first_existing_column(
            reader.fieldnames,
            ("Humidity", "RH", "Relative Humidity"),
        )
        rows = []
        for row in reader:
            if timestamp_column is None or power_column is None:
                return []
            normalized = _normalize_timestamp(row.get(timestamp_column))
            if normalized is None:
                continue
            ts = datetime.datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
            if ts > reference_time:
                continue
            rows.append(
                {
                    "timestamp": ts,
                    "active_power_mw": _power_value_to_mw(row.get(power_column), power_column) or 0.0,
                    "poa": _as_float(row.get(poa_column)) if poa_column else None,
                    "ghi": _as_float(row.get(ghi_column)) if ghi_column else None,
                    "wind_speed": _as_float(row.get(wind_speed_column)) if wind_speed_column else None,
                    "wind_direction": _as_float(row.get(wind_direction_column)) if wind_direction_column else None,
                    "ambient_temp": _as_float(row.get(ambient_temp_column)) if ambient_temp_column else None,
                    "module_temp": _as_float(row.get(module_temp_column)) if module_temp_column else None,
                    "humidity": _as_float(row.get(humidity_column)) if humidity_column else None,
                }
            )

    rows.sort(key=lambda item: item["timestamp"])
    return rows


def _circular_mean_degrees(values: list[float | None]) -> float | None:
    """Return the circular mean of compass-like degrees."""
    import math

    filtered = [value for value in values if value is not None]
    if not filtered:
        return None

    sin_sum = sum(math.sin(math.radians(value)) for value in filtered)
    cos_sum = sum(math.cos(math.radians(value)) for value in filtered)
    if abs(sin_sum) < 1e-9 and abs(cos_sum) < 1e-9:
        return None
    return round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0, 1)


def _format_sensor_value(value, unit: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}{(' ' + unit) if unit else ''}"


def summarize_intraday_state(actuals_csv_path, reference_time: datetime.datetime) -> dict | None:
    """
    Compact live-state summary for the current forecast run.

    This turns today's actual generation up to reference_time into a
    small deterministic state object that downstream steps can use for:
      - regime detection
      - residual correction
      - early fluctuation flags
      - prompt context
    """
    import math
    import time_features

    rows = _load_intraday_meter_rows(actuals_csv_path, reference_time)
    if not rows:
        return None

    recent = rows[-8:]
    latest_ts = recent[-1]["timestamp"]
    latest_mw = recent[-1]["active_power_mw"]
    recent_mw_values = [item["active_power_mw"] for item in recent]
    recent_avg_mw = sum(recent_mw_values) / len(recent_mw_values)
    recent_delta_mw = recent_mw_values[-1] - recent_mw_values[0]

    if len(recent) >= 3:
        diffs = [abs(recent[i]["active_power_mw"] - recent[i - 1]["active_power_mw"]) for i in range(1, len(recent))]
        avg_abs_step = sum(diffs) / len(diffs)
    else:
        avg_abs_step = 0.0

    recent_ghi_values = [item.get("ghi") for item in recent if item.get("ghi") is not None]
    recent_poa_values = [item.get("poa") for item in recent if item.get("poa") is not None]
    recent_wind_dir = _circular_mean_degrees([item.get("wind_direction") for item in recent])
    recent_wind_speed_values = [item.get("wind_speed") for item in recent if item.get("wind_speed") is not None]
    recent_ambient_temp_values = [item.get("ambient_temp") for item in recent if item.get("ambient_temp") is not None]
    recent_module_temp_values = [item.get("module_temp") for item in recent if item.get("module_temp") is not None]
    recent_humidity_values = [item.get("humidity") for item in recent if item.get("humidity") is not None]

    ratios = []
    for item in rows:
        ts = item["timestamp"]
        mw = item["active_power_mw"]
        elevation = time_features.compute_time_features(ts)["solar_elevation_deg"]
        if elevation <= 0:
            continue
        clear_sky_mw = config.PLANT_CAPACITY_MW * math.sin(math.radians(elevation)) * config.PERFORMANCE_RATIO
        if clear_sky_mw > 0.05:
            ratios.append(mw / clear_sky_mw)

    whole_day_ratio = sum(ratios) / len(ratios) if ratios else None
    recent_ratio = sum(ratios[-4:]) / len(ratios[-4:]) if ratios else None
    ratio_for_state = recent_ratio if recent_ratio is not None else whole_day_ratio

    trend_label = "rising" if recent_delta_mw > 0.05 else ("falling" if recent_delta_mw < -0.05 else "roughly stable")
    choppy = avg_abs_step > CHOPPY_AVG_STEP_MW
    fluctuation_flag = choppy or abs(recent_delta_mw) >= config.PLANT_CAPACITY_MW * 0.12

    if ratio_for_state is None:
        regime = "insufficient live data"
        live_residual_factor = 1.0
    else:
        if choppy and ratio_for_state < 0.55:
            regime = "unstable cloudy"
        elif choppy and ratio_for_state < 0.80:
            regime = "broken-cloud transition"
        elif ratio_for_state >= 0.85 and trend_label == "rising":
            regime = "clear / strengthening"
        elif ratio_for_state >= 0.70:
            regime = "steady moderate"
        else:
            regime = "weak cloudy"

        base_factor = 0.55 + (0.45 * ratio_for_state)
        trend_adjustment = max(-0.08, min(0.08, recent_delta_mw / max(config.PLANT_CAPACITY_MW * 0.20, 0.25) * 0.05))
        live_residual_factor = max(0.50, min(1.10, base_factor + trend_adjustment))
        if choppy:
            live_residual_factor = max(0.50, min(1.10, live_residual_factor * 0.97))

    summary = (
        f"Live same-day state up to {latest_ts.strftime('%Y-%m-%d %H:%M')}: "
        f"latest={latest_mw:.3f} MW, recent_avg={recent_avg_mw:.3f} MW, "
        f"trend={trend_label}, regime={regime}, "
        f"avg_step={avg_abs_step:.3f} MW, fluctuation_flag={fluctuation_flag}, "
        f"live_residual_factor={live_residual_factor:.3f}."
    )
    sensor_bits = []
    if recent_ghi_values:
        sensor_bits.append(f"GHI avg={sum(recent_ghi_values) / len(recent_ghi_values):.1f} W/m2")
    if recent_poa_values:
        sensor_bits.append(f"POA avg={sum(recent_poa_values) / len(recent_poa_values):.1f} W/m2")
    if recent_wind_dir is not None:
        sensor_bits.append(f"wind dir mean={recent_wind_dir:.1f} deg")
    if recent_wind_speed_values:
        sensor_bits.append(f"wind speed avg={sum(recent_wind_speed_values) / len(recent_wind_speed_values):.1f} m/s")
    if recent_ambient_temp_values:
        sensor_bits.append(f"ambient temp avg={sum(recent_ambient_temp_values) / len(recent_ambient_temp_values):.1f} C")
    if recent_module_temp_values:
        sensor_bits.append(f"module temp avg={sum(recent_module_temp_values) / len(recent_module_temp_values):.1f} C")
    if recent_humidity_values:
        sensor_bits.append(f"humidity avg={sum(recent_humidity_values) / len(recent_humidity_values):.1f}%")
    if sensor_bits:
        summary += " Sensor context: " + ", ".join(sensor_bits) + "."

    return {
        "reference_time": reference_time.strftime("%Y-%m-%d %H:%M"),
        "latest_time": latest_ts.strftime("%Y-%m-%d %H:%M"),
        "latest_mw": round(latest_mw, 3),
        "recent_avg_mw": round(recent_avg_mw, 3),
        "recent_delta_mw": round(recent_delta_mw, 3),
        "trend_label": trend_label,
        "avg_abs_step_mw": round(avg_abs_step, 3),
        "choppy": choppy,
        "fluctuation_flag": fluctuation_flag,
        "whole_day_clear_sky_ratio": round(whole_day_ratio, 3) if whole_day_ratio is not None else None,
        "recent_clear_sky_ratio": round(recent_ratio, 3) if recent_ratio is not None else None,
        "regime": regime,
        "live_residual_factor": round(live_residual_factor, 3),
        "summary": summary,
        "recent_ghi_avg": round(sum(recent_ghi_values) / len(recent_ghi_values), 1) if recent_ghi_values else None,
        "recent_poa_avg": round(sum(recent_poa_values) / len(recent_poa_values), 1) if recent_poa_values else None,
        "recent_wind_direction_mean": recent_wind_dir,
        "recent_wind_speed_avg": round(sum(recent_wind_speed_values) / len(recent_wind_speed_values), 1) if recent_wind_speed_values else None,
        "recent_ambient_temp_avg": round(sum(recent_ambient_temp_values) / len(recent_ambient_temp_values), 1) if recent_ambient_temp_values else None,
        "recent_module_temp_avg": round(sum(recent_module_temp_values) / len(recent_module_temp_values), 1) if recent_module_temp_values else None,
        "recent_humidity_avg": round(sum(recent_humidity_values) / len(recent_humidity_values), 1) if recent_humidity_values else None,
    }


def format_intraday_state_for_prompt(state: dict | None) -> str:
    if not state:
        return "No live same-day state summary is available yet."
    lines = [
        "Live same-day regime and fluctuation summary:",
        f"- {state['summary']}",
    ]
    if state.get("recent_clear_sky_ratio") is not None:
        lines.append(
            f"- Recent actual-vs-clear-sky ratio: {state['recent_clear_sky_ratio'] * 100:.0f}%"
        )
    if state.get("recent_ghi_avg") is not None:
        lines.append(f"- Recent GHI average: {state['recent_ghi_avg']:.1f} W/m2")
    if state.get("recent_poa_avg") is not None:
        lines.append(f"- Recent POA average: {state['recent_poa_avg']:.1f} W/m2")
    if state.get("recent_wind_direction_mean") is not None:
        lines.append(f"- Recent wind direction mean: {state['recent_wind_direction_mean']:.1f} deg")
    return "\n".join(lines)


def summarize_meter_rows_for_prompt(rows: list[dict], label: str = "") -> str:
    """Convert raw meter rows into a compact behavioral-memory summary."""
    summary = summarize_meter_rows_for_json(rows, label=label)
    if not summary:
        prefix = f"{label}: " if label else ""
        return f"{prefix}no meter rows available."

    bucket_summary = summary.get("time_of_day", {})
    bucket_bits = [
        f"{name} avg={data['avg_mw']:.3f} MW"
        for name, data in bucket_summary.items()
        if data.get("count", 0) > 0
    ]
    label_text = f"{label}: " if label else ""
    return (
        f"{label_text}{summary['first_ts']} to {summary['last_ts']}: "
        f"avg={summary['avg_mw']:.3f} MW, peak={summary['peak_mw']:.3f} MW at {summary['peak_time']}, "
        f"start={summary['start_mw']:.3f} MW, end={summary['end_mw']:.3f} MW, trend={summary['trend']}, "
        f"avg_step={summary['avg_abs_step']:.3f} MW, {'choppy' if summary['choppy'] else 'smooth'}. "
        + ("Time-of-day: " + "; ".join(bucket_bits) + "." if bucket_bits else "")
    ).strip()


def summarize_meter_rows_for_json(rows: list[dict], label: str = "") -> dict:
    """Convert raw meter rows into a structured JSON-friendly summary."""
    if not rows:
        return {}

    ordered = sorted(rows, key=lambda item: item["timestamp"])
    timestamps = [item["timestamp"] for item in ordered]
    mw_values = [float(item.get("active_power_mw") or 0.0) for item in ordered]
    first_ts, last_ts = timestamps[0], timestamps[-1]
    first_mw, last_mw = mw_values[0], mw_values[-1]
    peak_index = max(range(len(ordered)), key=lambda idx: mw_values[idx])
    peak_ts = timestamps[peak_index]
    peak_mw = mw_values[peak_index]
    avg_mw = sum(mw_values) / len(mw_values)
    diffs = [abs(mw_values[i] - mw_values[i - 1]) for i in range(1, len(mw_values))]
    avg_abs_step = sum(diffs) / len(diffs) if diffs else 0.0
    trend = "rising" if last_mw > first_mw + 0.05 else ("falling" if last_mw < first_mw - 0.05 else "flat")
    choppy = avg_abs_step > CHOPPY_AVG_STEP_MW

    def _bucket(hour: int) -> str:
        if hour < 10:
            return "morning"
        if hour < 14:
            return "midday"
        return "afternoon"

    bucket_values: dict[str, list[float]] = {"morning": [], "midday": [], "afternoon": []}
    for item in ordered:
        bucket_values[_bucket(item["timestamp"].hour)].append(float(item.get("active_power_mw") or 0.0))

    bucket_summary = {}
    for bucket_name in ("morning", "midday", "afternoon"):
        values = bucket_values[bucket_name]
        if values:
            bucket_summary[bucket_name] = {
                "count": len(values),
                "avg_mw": round(sum(values) / len(values), 3),
            }

    return {
        "label": label or "",
        "first_ts": first_ts.strftime("%Y-%m-%d %H:%M"),
        "last_ts": last_ts.strftime("%Y-%m-%d %H:%M"),
        "start_mw": round(first_mw, 3),
        "end_mw": round(last_mw, 3),
        "peak_time": peak_ts.strftime("%H:%M"),
        "peak_mw": round(peak_mw, 3),
        "avg_mw": round(avg_mw, 3),
        "avg_abs_step": round(avg_abs_step, 3),
        "trend": trend,
        "choppy": choppy,
        "time_of_day": bucket_summary,
    }


def _meter_row_readings_for_json(rows: list[dict]) -> list[dict]:
    """Return the raw MW readings in prompt-friendly JSON form."""
    readings = []
    for row in sorted(rows, key=lambda item: item["timestamp"]):
        readings.append(
            {
                "timestamp": row["timestamp"].strftime("%Y-%m-%d %H:%M"),
                "mw": round(float(row.get("active_power_mw") or 0.0), 3),
            }
        )
    return readings


def build_recent_meter_history_json(
    recent_paths: list[Path],
    *,
    current_meter_path: Path | None = None,
    reference_time: datetime.datetime | None = None,
    target_date: str | None = None,
    plant_name: str | None = None,
    existing_payload: dict | None = None,
) -> dict:
    """Build or refresh the cached recent-meter JSON for prompt use.

    `recent_paths` should contain the previous completed-day meter files
    in chronological order. `current_meter_path` can point at the current
    revision's clipped meter file; when provided, it is treated as the
    most recent day and is loaded only up to `reference_time`.
    """
    payload = dict(existing_payload or {})
    plant = (plant_name or config.PLANT_NAME or "").strip().upper()
    current_date = target_date or payload.get("target_date") or ""
    reference_time = reference_time or datetime.datetime.max

    days_by_date: dict[str, dict] = {}
    ordered_paths: list[tuple[Path, datetime.datetime]] = []

    for path in recent_paths:
        ordered_paths.append((path, datetime.datetime.max))

    if current_meter_path is not None:
        ordered_paths.append((current_meter_path, reference_time))

    for path, cutoff in ordered_paths:
        if not path:
            continue
        day_label = path.name[:10] if len(path.name) >= 10 else path.stem
        try:
            rows = _load_intraday_meter_rows(path, cutoff)
        except Exception:
            rows = []
        if not rows:
            continue
        day_entry = {
            "date": day_label,
            "source_file": path.name,
            "summary": summarize_meter_rows_for_json(rows, label=day_label),
            "readings": _meter_row_readings_for_json(rows),
        }
        days_by_date[day_label] = day_entry

    days_data = [days_by_date[key] for key in sorted(days_by_date.keys())]
    payload.update({
        "type": "recent_meter_history_raw",
        "plant_name": plant,
        "target_date": current_date,
        "days": 3,
        "recent_files": [path.name for path, _ in ordered_paths if path is not None],
        "days_data": days_data,
    })
    return payload


def derive_recent_meter_history_step1(
    history_payload: dict | None,
    block_time: datetime.datetime,
    fallback: float = 0.0,
) -> float:
    """Turn the cached 3-day meter JSON into a Step 1 baseline for one block."""
    if not isinstance(history_payload, dict):
        return round(float(fallback or 0.0), 3)

    days_data = history_payload.get("days_data")
    if not isinstance(days_data, list) or not days_data:
        return round(float(fallback or 0.0), 3)

    target_clock = block_time.strftime("%H:%M")
    bucket = "morning" if block_time.hour < 10 else "midday" if block_time.hour < 14 else "afternoon"
    per_day_values: list[float] = []

    for day in days_data:
        if not isinstance(day, dict):
            continue
        readings = day.get("readings")
        if isinstance(readings, list):
            matching_values = []
            for reading in readings:
                if not isinstance(reading, dict):
                    continue
                timestamp = str(reading.get("timestamp", "")).strip()
                if len(timestamp) >= 16 and timestamp[11:16] == target_clock:
                    try:
                        matching_values.append(float(reading.get("mw", 0.0)))
                    except (TypeError, ValueError):
                        continue
            if matching_values:
                per_day_values.append(sum(matching_values) / len(matching_values))
                continue

        summary = day.get("summary")
        if isinstance(summary, dict):
            time_of_day = summary.get("time_of_day")
            if isinstance(time_of_day, dict):
                bucket_summary = time_of_day.get(bucket)
                if isinstance(bucket_summary, dict) and bucket_summary.get("avg_mw") is not None:
                    try:
                        per_day_values.append(float(bucket_summary["avg_mw"]))
                        continue
                    except (TypeError, ValueError):
                        pass
            if summary.get("avg_mw") is not None:
                try:
                    per_day_values.append(float(summary["avg_mw"]))
                except (TypeError, ValueError):
                    pass

    if not per_day_values:
        return round(float(fallback or 0.0), 3)

    return round(sum(per_day_values) / len(per_day_values), 3)


def summarize_meter_csv_for_prompt(actuals_csv_path, reference_time: datetime.datetime | None = None, label: str = "") -> str:
    """Summarize a single meter CSV into a short prompt-friendly description."""
    reference_time = reference_time or datetime.datetime.max
    rows = _load_intraday_meter_rows(actuals_csv_path, reference_time)
    return summarize_meter_rows_for_prompt(rows, label=label or Path(actuals_csv_path).stem)


def format_intraday_actuals_for_prompt(actuals_csv_path, reference_time: datetime.datetime) -> str:
    """
    Reads a raw actual-meter CSV (RAW_METER_COLUMNS format) and turns
    generation up to reference_time into PATTERNS useful for the forward
    forecast, not just a raw readings dump:
      - the recent readings themselves, and the ramp trend across them
      - block-to-block volatility ("choppy" = patchy/intermittent cloud,
        which tends to persist into the next hour; "smooth" = steady
        conditions) -- this is a today-specific signal CBR/day-context
        can't give, since those come from OTHER days
      - today's OWN actual-vs-clear-sky ratio: actual generation compared
        against the elevation-only clear-sky ceiling (no cloud data, just
        "sun this high in the sky gives this much on a clear day") for
        those same past timestamps -- a same-day, directly comparable
        cloud-attenuation reading, again distinct from other days' cases

    Rows AFTER reference_time are ignored even if present in the file, so
    a full-day export can safely be passed in while simulating an earlier
    point in the day (e.g. testing "predict forward from 2:15 PM" using a
    file that also has 3 PM-6 PM rows) without leaking those "future"
    actuals into a forecast that's only supposed to see the past.

    Used by manual_prediction.py, which deliberately does NOT merge this
    file into historic_cases/merged_scada_data.csv or the rolling context
    -- this keeps a manual/test run's input isolated from the real
    production history, feeding the LLM this same-day trend directly
    instead.
    """
    import math
    import time_features

    rows = _load_intraday_meter_rows(actuals_csv_path, reference_time)

    if not rows:
        return "No actual generation data is available for earlier today yet."

    recent = rows[-8:]  # last ~2 hours at 15-min spacing
    lines = [
        f"Actual generation recorded earlier TODAY, up to {reference_time.strftime('%Y-%m-%d %H:%M')} "
        f"(most recent readings, do not treat anything after this time as known):"
    ]
    lines += [
        "- {ts}: power={mw:.3f} MW, GHI={ghi}, POA={poa}, wind_dir={wind_dir}".format(
            ts=item["timestamp"].strftime("%H:%M"),
            mw=item["active_power_mw"],
            ghi=_format_sensor_value(item.get("ghi"), "W/m2"),
            poa=_format_sensor_value(item.get("poa"), "W/m2"),
            wind_dir=_format_sensor_value(item.get("wind_direction"), "deg"),
        )
        for item in recent
    ]

    # ---- Trend / ramp rate across the recent window ----
    if len(recent) >= 2:
        delta = recent[-1]["active_power_mw"] - recent[0]["active_power_mw"]
        trend = "rising" if delta > 0.05 else ("falling" if delta < -0.05 else "roughly stable")
        span_minutes = (recent[-1]["timestamp"] - recent[0]["timestamp"]).total_seconds() / 60.0
        ramp_per_15min = (delta / span_minutes * 15.0) if span_minutes > 0 else 0.0
        lines.append(f"Trend over this window: {trend} ({delta:+.3f} MW; ~{ramp_per_15min:+.3f} MW per 15-min block).")

    # ---- Volatility / choppiness over the RECENT window (not the whole
    # day -- a calm morning ramp would otherwise dilute/mask genuine
    # volatility happening right now, which is what matters for the next
    # hour's forecast) ----
    if len(recent) >= 3:
        diffs = [abs(recent[i]["active_power_mw"] - recent[i - 1]["active_power_mw"]) for i in range(1, len(recent))]
        avg_abs_step = sum(diffs) / len(diffs)
        choppiness = "CHOPPY (patchy/intermittent cloud -- likely to continue into the next hour)" \
            if avg_abs_step > CHOPPY_AVG_STEP_MW else "smooth/steady (stable conditions so far)"
        lines.append(f"Block-to-block variability today: avg |change|={avg_abs_step:.3f} MW -- {choppiness}.")

    recent_ghi_values = [item.get("ghi") for item in recent if item.get("ghi") is not None]
    recent_poa_values = [item.get("poa") for item in recent if item.get("poa") is not None]
    recent_wind_dir = _circular_mean_degrees([item.get("wind_direction") for item in recent])
    if recent_ghi_values or recent_poa_values or recent_wind_dir is not None:
        sensor_bits = []
        if recent_ghi_values:
            sensor_bits.append(f"avg GHI={sum(recent_ghi_values) / len(recent_ghi_values):.1f} W/m2")
        if recent_poa_values:
            sensor_bits.append(f"avg POA={sum(recent_poa_values) / len(recent_poa_values):.1f} W/m2")
        if recent_wind_dir is not None:
            sensor_bits.append(f"mean wind direction={recent_wind_dir:.1f} deg")
        lines.append("Recent raw sensor context: " + ", ".join(sensor_bits) + ".")

    # ---- Today's own actual-vs-clear-sky ratio (elevation-only, no cloud data) ----
    ratios = []
    for item in rows:
        ts = item["timestamp"]
        mw = item["active_power_mw"]
        elevation = time_features.compute_time_features(ts)["solar_elevation_deg"]
        if elevation <= 0:
            continue
        clear_sky_mw = config.PLANT_CAPACITY_MW * math.sin(math.radians(elevation)) * config.PERFORMANCE_RATIO
        if clear_sky_mw > 0.05:
            ratios.append(mw / clear_sky_mw)

    if ratios:
        whole_day_pct = sum(ratios) / len(ratios) * 100.0
        recent_ratios = ratios[-4:]
        recent_pct = sum(recent_ratios) / len(recent_ratios) * 100.0
        lines.append(
            f"Today's actual generation has averaged {whole_day_pct:.0f}% of the clear-sky ceiling "
            f"(elevation-only baseline, ignoring cloud) over the whole day so far, and {recent_pct:.0f}% "
            f"over the most recent readings -- treat this as TODAY'S OWN real-time cloud-attenuation "
            f"reading, more directly relevant than other days' historical cases."
        )

    return "\n".join(lines)


MIN_BLOCKS_FOR_ANCHOR_CORRECTION = 20
ANCHOR_CORRECTION_BOUNDS = (0.5, 1.5)


def compute_anchor_correction_factor() -> float:
    """
    Empirically calibrates the physics anchor against every real SCADA
    actual synced so far, across ALL per-day case-store files (not just
    the recent rolling context) -- this is a slow, statistically-stable
    correction, separate from the LLM's per-run adjustment.

    Recomputes the anchor's raw output (physics_anchor.calculate_anchor_mw,
    BEFORE any LLM adjustment) for every historical feature row that has a
    matched actual, and returns actual_total / anchor_total as a
    multiplicative correction factor -- e.g. 0.7 means the anchor formula
    has been running 30% too high, on average, across all data collected
    so far.

    Requires at least MIN_BLOCKS_FOR_ANCHOR_CORRECTION matched blocks
    before applying any correction (returns 1.0 / no-op below that) -- a
    day or two of data is too little to safely recalibrate a formula that
    affects every future prediction. Bounded to ANCHOR_CORRECTION_BOUNDS
    so no amount of accumulated history can swing the anchor by more than
    that range in one direction.
    """
    import physics_anchor  # local import: avoids a module-load-order dependency

    anchor_total, actual_total, n = 0.0, 0.0, 0

    for csv_path in _case_store_paths():
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                actual_mw = _as_float(row.get(ACTUAL_COLUMN_NAME))
                if actual_mw is None:
                    continue

                feature_row = {}
                for key, value in row.items():
                    if key in ("Block", "Time", "Predicted Generation (MW)", ACTUAL_COLUMN_NAME):
                        continue
                    parsed = _as_float(value)
                    feature_row[key] = parsed if parsed is not None else value

                anchor_total += physics_anchor.calculate_anchor_mw(feature_row)
                actual_total += actual_mw
                n += 1

    if n < MIN_BLOCKS_FOR_ANCHOR_CORRECTION or anchor_total <= 0:
        return 1.0

    low, high = ANCHOR_CORRECTION_BOUNDS
    return max(low, min(high, actual_total / anchor_total))


def suggested_max_deviation_fraction(default_fraction: float = None) -> float:
    """
    Looks at the rolling day-level context (last up to
    config.CONTEXT_WINDOW_DAYS days). If MULTIPLE days show the SAME
    bias direction (all over-forecast or all under-forecast), that's
    repeated real-world evidence the anchor is off by more than a
    single-day fluke -- so validator.py is allowed a bigger correction
    window than its hardcoded default for this run. A single day, or
    days that disagree, isn't strong enough evidence, so the default
    (validator.MAX_DEVIATION_FRACTION) stays in place.
    """
    import validator  # local import: avoids a module-load-order dependency

    if default_fraction is None:
        default_fraction = validator.MAX_DEVIATION_FRACTION

    context = _load_context()
    entries = context.get("entries", [])
    if len(entries) < 2:
        return default_fraction

    directions = {e.get("bias_direction") for e in entries}
    if len(directions) != 1 or "roughly balanced" in directions:
        return default_fraction

    return 0.80 if len(entries) >= 3 else 0.60


def process_actuals_inbox() -> list:
    """
    Local/manual compatibility path. Scans config.ACTUALS_INBOX_DIR for
    new meter-export CSVs, merges them into the case store, re-syncs
    actuals into the feature-log case store, runs error/pattern analysis
    for every day the file touched, folds each day's analysis into the
    rolling prediction-context file (capped at
    config.CONTEXT_WINDOW_DAYS days), and archives the file into
    config.ACTUALS_INBOX_PROCESSED_DIR so it is never re-processed.

    Returns the list of date strings analyzed this call (for logging).
    """
    inbox_files = sorted(config.ACTUALS_INBOX_DIR.glob("*.csv"))
    return process_actual_meter_files(inbox_files, source_label="daily_actuals_inbox")


def process_actual_meter_files(csv_paths: list[Path], source_label: str = "raw meter files") -> list:
    """
    Process meter-export CSVs already available locally.

    This is the common learning path used by the raw-meter flow and the
    optional local/manual inbox flow. Each source file is processed once
    by filename; a small manifest prevents duplicate re-learning on
    later runs.
    """
    if not csv_paths:
        return []

    processed_names = _load_processed_raw_meter_names()
    analyzed_dates: list[str] = []
    touched_any = False

    for csv_path in sorted(csv_paths):
        if csv_path.name in processed_names:
            continue
        try:
            touched_dates = _merge_meter_csv_into_store(csv_path)
        except (OSError, ValueError, KeyError) as e:
            print(f"  [WARN] Skipping {csv_path.name} from {source_label} ({e}).")
            continue

        touched_any = True
        sync_historic_case_actuals()

        for date_str in sorted(touched_dates):
            matched = _load_features_log_rows_for_date(date_str)
            if not matched:
                print(f"  [INFO] {csv_path.name}: no predicted+actual matches for {date_str} yet "
                      f"-- skipping pattern analysis for this date.")
                continue

            entry = _analyze_day_patterns(date_str, matched)

            actual_rows = _load_merged_scada_readings_for_date(date_str)
            pattern = _analyze_actual_pattern(date_str, actual_rows)
            if pattern:
                entry["actual_pattern"] = pattern

            _add_day_to_context(entry)
            _log_accuracy(
                {k: entry[k] for k in ("n_matched_blocks", "mae", "rmse", "mape_pct", "bias")},
                date_str=date_str,
            )
            print(f"  [OK] Analyzed {date_str}: {entry['summary']}")
            analyzed_dates.append(date_str)

        processed_names.add(csv_path.name)
        print(f"  Marked {csv_path.name} as processed from {source_label}.")

    if touched_any:
        _save_processed_raw_meter_names(processed_names)
        _push_state_to_s3_if_enabled()

    return analyzed_dates


def process_schedule_feedback(
    schedule_csv_path: str | Path,
    actual_meter_csv_path: str | Path,
    *,
    source_label: str = "daily schedule feedback",
    entry_date: str | None = None,
) -> dict | None:
    """Build rolling day-level context directly from a forecast schedule CSV
    and the day's meter export.

    This is the schedule-vs-meter feedback path:
      - schedule forecast values come from LLM Schedule (MW)
      - actual values come from Active Power (kW), converted to MW
      - blocks with missing/blank/unparseable values are skipped
      - the resulting day summary is appended to prediction_context/<PLANT>_context.json

    Returns the context entry if one was written, otherwise None.
    """
    schedule_csv_path = Path(schedule_csv_path)
    actual_meter_csv_path = Path(actual_meter_csv_path)

    if not schedule_csv_path.exists():
        raise FileNotFoundError(f"Schedule CSV not found: {schedule_csv_path}")
    if not actual_meter_csv_path.exists():
        raise FileNotFoundError(f"Actual meter CSV not found: {actual_meter_csv_path}")

    date_str = entry_date
    if not date_str:
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", schedule_csv_path.name)
        date_str = date_match.group(1) if date_match else None
    if not date_str:
        actual_date_match = re.search(r"(\d{4}-\d{2}-\d{2})", actual_meter_csv_path.name)
        date_str = actual_date_match.group(1) if actual_date_match else datetime.date.today().isoformat()

    schedule_rows = _load_schedule_rows(str(schedule_csv_path))
    actual_readings = _load_actual_readings(str(actual_meter_csv_path))
    if not schedule_rows:
        print(f"  [INFO] No usable schedule rows found in {schedule_csv_path.name}.")
        return None
    if not actual_readings:
        print(f"  [INFO] No usable actual readings found in {actual_meter_csv_path.name}.")
        return None

    matched = []
    skipped_missing = 0
    for time_label, sched in schedule_rows.items():
        actual_mw = actual_readings.get(time_label)
        if actual_mw is None:
            skipped_missing += 1
            continue
        predicted_mw = sched.get("predicted_mw")
        if predicted_mw is None:
            skipped_missing += 1
            continue
        matched.append({
            "time": time_label,
            "predicted_mw": predicted_mw,
            "actual_mw": actual_mw,
            "row": sched.get("row", {}),
        })

    if not matched:
        print(
            f"  [INFO] No matched schedule+actual blocks for {date_str} "
            f"from {schedule_csv_path.name} and {actual_meter_csv_path.name}."
        )
        return None

    if skipped_missing:
        print(f"  [INFO] Skipped {skipped_missing} block(s) with missing schedule or actual values.")

    entry = _analyze_day_patterns(date_str, matched)
    entry["source"] = source_label
    entry["schedule_csv"] = schedule_csv_path.name
    entry["actual_meter_csv"] = actual_meter_csv_path.name
    entry["feedback_type"] = "schedule_vs_meter"

    actual_rows = _load_merged_scada_readings_for_date(date_str)
    pattern = _analyze_actual_pattern(date_str, actual_rows)
    if pattern:
        entry["actual_pattern"] = pattern

    _add_day_to_context(entry)
    _log_accuracy(
        {k: entry[k] for k in ("n_matched_blocks", "mae", "rmse", "mape_pct", "bias")},
        date_str=date_str,
    )
    revision_id, revision_time = _extract_revision_metadata(schedule_csv_path, source_label, date_str)
    _record_daily_revision_feedback(
        date_str,
        revision_id=revision_id,
        revision_time=revision_time,
        source_label=source_label,
        schedule_csv_path=schedule_csv_path,
        actual_meter_csv_path=actual_meter_csv_path,
        entry=entry,
        matched=matched,
        skipped_missing=skipped_missing,
        actual_pattern=pattern,
    )
    _push_state_to_s3_if_enabled()
    return entry


def run_daily_feedback(actual_csv_path: str) -> None:
    print(f"Loading actual meter readings from: {actual_csv_path}")
    actual_readings = _load_actual_readings(actual_csv_path)
    print(f"  Found {len(actual_readings)} usable actual readings.")

    print("\nUpdating case store with actual generation values...")
    updated_count, matched_rows = _update_case_store_with_actuals(actual_readings)
    print(f"  Updated {updated_count} rows in the case store with actual generation.")

    if not matched_rows:
        print("\n[INFO] No predicted+actual pairs matched by timestamp -- "
              "no error metrics to compute. Check that your pipeline was "
              "running (and logging predictions) at these times.")
        return

    print("\nComputing error metrics...")
    metrics = _compute_error_metrics(matched_rows)
    for key, value in metrics.items():
        print(f"  {key}: {value}")

    _log_accuracy(metrics)
    print(f"\nAccuracy log updated: {ACCURACY_LOG_PATH.resolve()}")
    _push_state_to_s3_if_enabled()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python daily_feedback.py <path_to_actual_meter_csv>")
        sys.exit(1)
    run_daily_feedback(sys.argv[1])
