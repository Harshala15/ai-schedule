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
import state_sync

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
    with open(actual_csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        timestamp_column = _pick_first_existing_column(
            reader.fieldnames,
            (TIMESTAMP_COLUMN, "Timestamp", "DateTime", "Datetime", "Start (Asia/Calcutta)", "Start (Asia/Kolkata)", "Start"),
        )
        power_column = _pick_first_existing_column(
            reader.fieldnames,
            (
                POWER_COLUMN_MW,
                "Active Power (kW)",
                "Active Power-Avg MFM-OUT (KW)",
                "Active Power (MW)",
                "GSPPL - Meter data (live) (kW)",
            ),
        )
        if timestamp_column is None or power_column is None:
            raise SystemExit(
                f"Expected columns '{TIMESTAMP_COLUMN}' and '{POWER_COLUMN_MW}' not found. "
                f"Available columns: {reader.fieldnames}\n"
                f"Update TIMESTAMP_COLUMN / POWER_COLUMN_MW at the top of this script to match."
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
    for key in ("LLM Schedule (MW)", "Predicted Generation (MW)", "Forecast (MW)"):
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
        normalized["lessons"] = _derive_entry_lessons(normalized)
    return normalized


def _normalize_prediction_context(payload) -> dict:
    if isinstance(payload, list):
        entries = [_normalize_context_entry(entry) for entry in payload if isinstance(entry, dict)]
        entries = sorted(entries, key=lambda e: e.get("date", ""))[-config.CONTEXT_WINDOW_DAYS:]
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
    context["entries"] = entries
    context["plant_profile"] = {**_build_plant_profile(), **(context.get("plant_profile") or {})}
    context["recent_summary"] = _build_recent_summary(entries)
    context["llm_context_summary"] = _build_llm_context_summary(context)
    context["schema_version"] = max(int(context.get("schema_version", CONTEXT_SCHEMA_VERSION) or CONTEXT_SCHEMA_VERSION), CONTEXT_SCHEMA_VERSION)
    return context


def _derive_entry_lessons(entry: dict) -> list[str]:
    lessons: list[str] = []
    bias = entry.get("bias")
    if isinstance(bias, (int, float)):
        if bias > 0.01:
            lessons.append("Forecast ran high; nudge future blocks downward when conditions are similar.")
        elif bias < -0.01:
            lessons.append("Forecast ran low; nudge future blocks upward when conditions are similar.")
    actual_pattern = entry.get("actual_pattern") if isinstance(entry.get("actual_pattern"), dict) else {}
    if actual_pattern.get("choppy"):
        lessons.append("Actual generation was choppy; use stronger correction when clouds are unstable.")
    else:
        lessons.append("Actual generation was smooth; only mild correction was needed.")
    if not lessons:
        lessons.append("Use this day as a similar-case reference for the plant's behavior.")
    return lessons


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
    import time_features

    with open(actuals_csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            normalized = _normalize_timestamp(row.get("TimeStamp"))
            if normalized is None:
                continue
            ts = datetime.datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
            if ts > reference_time:
                continue
            kw = _as_float(row.get("Active Power (kW)"))
            if kw is None:
                continue
            rows.append((ts, max(0.0, kw) / 1000.0))

    rows.sort(key=lambda pair: pair[0])
    return rows


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

    rows = _load_intraday_rows(actuals_csv_path, reference_time)
    if not rows:
        return None

    recent = rows[-8:]
    latest_ts, latest_mw = recent[-1]
    recent_mw_values = [mw for _, mw in recent]
    recent_avg_mw = sum(recent_mw_values) / len(recent_mw_values)
    recent_delta_mw = recent_mw_values[-1] - recent_mw_values[0]

    if len(recent) >= 3:
        diffs = [abs(recent[i][1] - recent[i - 1][1]) for i in range(1, len(recent))]
        avg_abs_step = sum(diffs) / len(diffs)
    else:
        avg_abs_step = 0.0

    ratios = []
    for ts, mw in rows:
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
    return "\n".join(lines)


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

    rows = _load_intraday_rows(actuals_csv_path, reference_time)

    if not rows:
        return "No actual generation data is available for earlier today yet."

    rows.sort(key=lambda pair: pair[0])
    recent = rows[-8:]  # last ~2 hours at 15-min spacing
    lines = [
        f"Actual generation recorded earlier TODAY, up to {reference_time.strftime('%Y-%m-%d %H:%M')} "
        f"(most recent readings, do not treat anything after this time as known):"
    ]
    lines += [f"- {ts.strftime('%H:%M')}: {mw:.3f} MW" for ts, mw in recent]

    # ---- Trend / ramp rate across the recent window ----
    if len(recent) >= 2:
        delta = recent[-1][1] - recent[0][1]
        trend = "rising" if delta > 0.05 else ("falling" if delta < -0.05 else "roughly stable")
        span_minutes = (recent[-1][0] - recent[0][0]).total_seconds() / 60.0
        ramp_per_15min = (delta / span_minutes * 15.0) if span_minutes > 0 else 0.0
        lines.append(f"Trend over this window: {trend} ({delta:+.3f} MW; ~{ramp_per_15min:+.3f} MW per 15-min block).")

    # ---- Volatility / choppiness over the RECENT window (not the whole
    # day -- a calm morning ramp would otherwise dilute/mask genuine
    # volatility happening right now, which is what matters for the next
    # hour's forecast) ----
    if len(recent) >= 3:
        diffs = [abs(recent[i][1] - recent[i - 1][1]) for i in range(1, len(recent))]
        avg_abs_step = sum(diffs) / len(diffs)
        choppiness = "CHOPPY (patchy/intermittent cloud -- likely to continue into the next hour)" \
            if avg_abs_step > CHOPPY_AVG_STEP_MW else "smooth/steady (stable conditions so far)"
        lines.append(f"Block-to-block variability today: avg |change|={avg_abs_step:.3f} MW -- {choppiness}.")

    # ---- Today's own actual-vs-clear-sky ratio (elevation-only, no cloud data) ----
    ratios = []
    for ts, mw in rows:
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
