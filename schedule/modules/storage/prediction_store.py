"""
prediction_store.py

Saves two kinds of CSVs, each split into a SEPARATE FILE PER CALENDAR DAY
(the date embedded in each row's Time), instead of one ever-growing file.
Within each day's file, rows are still UPDATE-OR-APPEND keyed on "Time":
if a row for that time already exists, it's updated with the latest
prediction; if not, it's appended. Each file stays sorted chronologically.

1. energy_predictions/<PLANT>_energy_generation_<YYYY-MM-DD>.csv
   -> the final, human-facing schedule output: block, 15-minute interval,
      physics anchor MW, final LLM/validated MW, and LLM reasoning.

2. features_log/<PLANT>_features_log_<YYYY-MM-DD>.csv
   -> Block, Time, every raw feature value used, AND the predicted MW.
   This is the case store -- similarity_retrieval.py reads across ALL of
   these per-day files (not just one), so CBR retrieval still searches
   the plant's full history even though it's split across many files.
   daily_feedback.py enriches the matching day's file with actual
   generation each evening.
"""

import csv
import datetime

import config


def _update_or_append(csv_path, header, rows_by_time, preserve_existing_extra_columns: bool = True):
    """Shared schema-safe read-merge-sort-write logic for both CSV files."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing_by_time = {}
    existing_header = []
    if csv_path.exists():
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_header = list(reader.fieldnames or [])
            for row in reader:
                time_key = _row_time_key(row)
                if time_key:
                    existing_by_time[time_key] = row

    final_header = list(header)
    if preserve_existing_extra_columns:
        # Preserve columns added by feedback (especially Actual Generation)
        # when a later prediction run updates the same feature-log file.
        for column in existing_header:
            if column not in final_header:
                final_header.append(column)

    for time_label, values in rows_by_time.items():
        new_row = dict(zip(header, values))
        existing_row = existing_by_time.get(time_label, {})
        existing_row.update(new_row)
        existing_by_time[time_label] = existing_row

    def _parse_time(time_label):
        return datetime.datetime.strptime(time_label, "%Y-%m-%d %H:%M")

    sorted_times = sorted(existing_by_time.keys(), key=_parse_time)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(final_header)
        for t in sorted_times:
            writer.writerow([existing_by_time[t].get(column, "") for column in final_header])


def _group_by_date(rows_by_time: dict) -> dict:
    """Splits a {time_label: row} dict into {date_str: {time_label: row}},
    keyed on the date portion of each time_label ("YYYY-MM-DD HH:MM" ->
    "YYYY-MM-DD") -- almost always a single date, except when a run's
    forecast blocks span midnight into the next day."""
    by_date = {}
    for time_label, row in rows_by_time.items():
        date_str = time_label[:10]
        by_date.setdefault(date_str, {})[time_label] = row
    return by_date


def _row_time_key(row: dict) -> str:
    if row.get("Time"):
        return row["Time"]
    interval = row.get("Time Interval (15 minute interval)", "")
    if " - " in interval:
        return interval.split(" - ", 1)[0]
    return ""


def _format_time_interval(time_label: str) -> str:
    start = datetime.datetime.strptime(time_label, "%Y-%m-%d %H:%M")
    end = start + datetime.timedelta(minutes=config.BLOCK_MINUTES)
    return f"{time_label} - {end.strftime('%H:%M')}"


def save_generation_csv(rows, output_dir=None) -> list:
    """
    `rows`: list of dicts or tuples describing the forecast sequence.
    `output_dir`: overrides config.PREDICTIONS_DIR -- pass this to write
        somewhere other than the real per-day production files (see
        manual_prediction.py, which always redirects test runs here so
        they never mix into real data).

    Returns the list of file paths written to (one per calendar date
    touched by `rows` -- usually just one).
    """
    output_dir = output_dir or config.PREDICTIONS_DIR
    header = [
        "Block",
        "Time Interval (15 minute interval)",
        "Step 1 Meter Base Forecast MW",
        "Step 2 Weather + Video Adjusted MW",
        "Step 3 Plant Performance MW",
        "Step 4 Revision Feedback MW",
        "LLM Schedule (MW)",
        "Schedule MW",
        "LLM Reasoning",
    ]

    hard_cap_mw = getattr(config, "PLANT_MAX_FEED_IN_MW", config.PLANT_CAPACITY_MW)

    def _clip_display_mw(value):
        try:
            return round(max(0.0, min(hard_cap_mw, float(value))), 3)
        except (TypeError, ValueError):
            return value

    rows_by_time = {}
    for row in rows:
        if isinstance(row, dict):
            block_number = row.get("block_number", row.get("Block", ""))
            time_label = row.get("time", row.get("Time", ""))
            step1_mw = _clip_display_mw(row.get("step1_mw", row.get("Step 1 Meter Base Forecast MW", row.get("anchor_mw", ""))))
            step2_mw = _clip_display_mw(row.get("step2_mw", row.get("Step 2 Weather + Video Adjusted MW", row.get("llm_mw", ""))))
            step3_mw = _clip_display_mw(row.get("step3_mw", row.get("Step 3 Plant Performance MW", row.get("llm_mw", ""))))
            step4_mw = _clip_display_mw(
                row.get(
                    "step4_mw",
                    row.get(
                        "Step 4 Revision Feedback MW",
                        row.get("Step 4 Revision Feedback Adjusted MW", row.get("Step 4 Context Adjusted MW", row.get("llm_mw", step3_mw))),
                    ),
                )
            )
            llm_mw = _clip_display_mw(row.get("llm_mw", row.get("LLM Schedule (MW)", step4_mw)))
            final_mw = _clip_display_mw(row.get("final_mw", row.get("Schedule MW", row.get("Final Validated MW", llm_mw))))
            reasoning = row.get("reasoning", row.get("LLM Reasoning", ""))
        else:
            if len(row) == 5:
                block_number, time_label, anchor_mw, final_mw, reasoning = row
                step1_mw = _clip_display_mw(anchor_mw)
                step2_mw = _clip_display_mw(final_mw)
                step3_mw = _clip_display_mw(final_mw)
                step4_mw = _clip_display_mw(final_mw)
                llm_mw = _clip_display_mw(final_mw)
            elif len(row) == 8:
                block_number, time_label, step1_mw, step2_mw, step3_mw, llm_mw, final_mw, reasoning = row
                step1_mw = _clip_display_mw(step1_mw)
                step2_mw = _clip_display_mw(step2_mw)
                step3_mw = _clip_display_mw(step3_mw)
                step4_mw = _clip_display_mw(step3_mw)
                llm_mw = _clip_display_mw(llm_mw)
                final_mw = _clip_display_mw(final_mw)
            else:
                block_number, time_label, step1_mw, step2_mw, step3_mw, step4_mw, llm_mw, final_mw, reasoning = row
                step1_mw = _clip_display_mw(step1_mw)
                step2_mw = _clip_display_mw(step2_mw)
                step3_mw = _clip_display_mw(step3_mw)
                step4_mw = _clip_display_mw(step4_mw)
                llm_mw = _clip_display_mw(llm_mw)
                final_mw = _clip_display_mw(final_mw)

        rows_by_time[time_label] = [
            str(block_number),
            _format_time_interval(time_label),
            str(step1_mw),
            str(step2_mw),
            str(step3_mw),
            str(step4_mw),
            str(llm_mw),
            str(final_mw),
            reasoning,
        ]

    written_paths = []
    for date_str, date_rows_by_time in _group_by_date(rows_by_time).items():
        csv_path = output_dir / f"{config.PLANT_NAME}_energy_generation_{date_str}.csv"
        _update_or_append(csv_path, header, date_rows_by_time, preserve_existing_extra_columns=False)
        written_paths.append(csv_path)
    return written_paths


def save_features_log(rows, feature_columns, output_dir=None) -> list:
    """
    `rows`: list of (block_number, time_label, feature_row_dict, generation_mw)
    `feature_columns`: sorted list of feature names (for a stable column
    order across every call -- see feature_builder.get_feature_columns).
    `output_dir`: overrides config.FEATURES_LOG_DIR -- pass this to write
        somewhere other than the real per-day case store (see
        manual_prediction.py). A file written here is NOT picked up by
        similarity_retrieval.py's glob, so test runs never contaminate
        CBR retrieval either.

    Returns the list of file paths written to (one per calendar date
    touched by `rows` -- usually just one).
    """
    output_dir = output_dir or config.FEATURES_LOG_DIR
    header = ["Block", "Time"] + feature_columns + ["Predicted Generation (MW)"]

    rows_by_time = {}
    for block_number, time_label, feature_row, mw in rows:
        row = [str(block_number), time_label]
        row += [str(feature_row.get(col, "")) for col in feature_columns]
        row.append(str(mw))
        rows_by_time[time_label] = row

    written_paths = []
    for date_str, date_rows_by_time in _group_by_date(rows_by_time).items():
        csv_path = output_dir / f"{config.PLANT_NAME}_features_log_{date_str}.csv"
        _update_or_append(csv_path, header, date_rows_by_time)
        written_paths.append(csv_path)
    return written_paths


def save_forecast_trace_csv(rows, output_dir=None) -> list:
    """
    `rows`: list of dicts describing how each forecast block was produced.

    This is a human-readable debug companion to the normal schedule CSV:
    it keeps the forecast output stable while adding a block-wise audit
    trail for anchor, LLM, validation, retrieval, and context evidence.
    """
    output_dir = output_dir or config.PREDICTIONS_DIR
    header = [
        "Block",
        "Time",
        "Physics Anchor MW",
        "Base Physics Anchor MW",
        "Raw Step 1 MW",
        "Raw Step 2 MW",
        "Raw Step 3 MW",
        "Raw Step 4 MW",
        "Raw LLM MW",
        "Step 1 Meter Base MW",
        "Step 2 Weather + Video MW",
        "Step 3 Plant Performance MW",
        "Step 4 Revision Feedback MW",
        "Stepwise Base Factor",
        "Step 1 Factor",
        "Step 2 Factor",
        "Step 3 Factor",
        "Step 4 Factor",
        "Final Stage Cap MW",
        "Revision Clamp Factor",
        "Time of Day Bucket",
        "Correction Note",
        "Live Residual Factor",
        "Regime",
        "Fluctuation Flag",
        "Step 2 Confidence",
        "Step 2 Reasoning",
        "LLM MW",
        "Validated MW",
        "Confidence",
        "Reasoning",
        "Retrieved Cases Count",
        "Top Retrieved Case",
        "Context Summary",
        "Live State Summary",
        "Weather Summary",
        "Feature Snapshot",
    ]

    rows_by_time = {}
    for row in rows:
        time_label = row.get("Time", "")
        if not time_label:
            continue
        rows_by_time[time_label] = [
            str(row.get("Block", "")),
            time_label,
            str(row.get("Physics Anchor MW", "")),
            str(row.get("Base Physics Anchor MW", "")),
            str(row.get("Raw Step 1 MW", "")),
            str(row.get("Raw Step 2 MW", "")),
            str(row.get("Raw Step 3 MW", "")),
            str(row.get("Raw Step 4 MW", "")),
            str(row.get("Raw LLM MW", "")),
            str(row.get("Step 1 Meter Base MW", "")),
            str(row.get("Step 2 Weather + Video MW", "")),
            str(row.get("Step 3 Plant Performance MW", "")),
            str(row.get("Step 4 Revision Feedback MW", row.get("Step 4 Context MW", ""))),
            str(row.get("Stepwise Base Factor", "")),
            str(row.get("Step 1 Factor", "")),
            str(row.get("Step 2 Factor", "")),
            str(row.get("Step 3 Factor", "")),
            str(row.get("Step 4 Factor", "")),
            str(row.get("Final Stage Cap MW", "")),
            str(row.get("Revision Clamp Factor", "")),
            str(row.get("Time of Day Bucket", "")),
            str(row.get("Correction Note", "")),
            str(row.get("Live Residual Factor", "")),
            str(row.get("Regime", "")),
            str(row.get("Fluctuation Flag", "")),
            str(row.get("Step 2 Confidence", "")),
            str(row.get("Step 2 Reasoning", "")),
            str(row.get("LLM MW", "")),
            str(row.get("Validated MW", "")),
            str(row.get("Confidence", "")),
            str(row.get("Reasoning", "")),
            str(row.get("Retrieved Cases Count", "")),
            str(row.get("Top Retrieved Case", "")),
            str(row.get("Context Summary", "")),
            str(row.get("Live State Summary", "")),
            str(row.get("Weather Summary", "")),
            str(row.get("Feature Snapshot", "")),
        ]

    written_paths = []
    for date_str, date_rows_by_time in _group_by_date(rows_by_time).items():
        csv_path = output_dir / f"{config.PLANT_NAME}_forecast_trace_{date_str}.csv"
        _update_or_append(csv_path, header, date_rows_by_time)
        written_paths.append(csv_path)
    return written_paths
