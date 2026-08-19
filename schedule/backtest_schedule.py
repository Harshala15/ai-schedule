"""
backtest_schedule.py

Reconstructs a full day's schedule the way it would have been generated
in real time, for model evaluation -- implements the "Schedule Generation
Workflow for Model Evaluation" methodology:

For each of config.CAPTURE_TIMES (06:45, 08:15, 09:45, 11:15, 12:45,
14:15, 15:45) on a given date, IN ORDER:
    1. Use the screenshots + video captured closest to that time on that
       date (already stored by the automated pipeline in
       windy_screenshots/ and windy_videos/).
    2. Truncate the day's actual-meter data to that same time T -- nothing
       "future" relative to T is ever used, matching what would genuinely
       have been known at that instant.
    3. Generate the schedule from block T through the END OF THE DAY,
       using the exact same pipeline (physics anchor -> CBR retrieval ->
       LLM adjustment -> validator) as a live run.
    4. Write it into ONE reconstructed per-day schedule file. Blocks
       before T (already finalized by an earlier capture-time's
       iteration) are left untouched -- only blocks at/after T are added
       or overwritten -- exactly matching "previously generated blocks
       stay unchanged, only future blocks update."

After all 7 capture times are processed, the single reconstructed file
represents what the schedule would have looked like in real time
throughout the day. It is then compared against the full actual-meter CSV
with error metrics (MAE/RMSE/MAPE/Bias).

Output is written to evaluation_schedules/ -- isolated from the real
production files (energy_predictions/, features_log/) and from
manual_input/output/ (one-off manual tests), since this is a distinct,
multi-step evaluation run.

Usage:
    python backtest_schedule.py --date 2026-07-26 --actuals path/to/full_day_meter_export.csv
"""

import argparse
import csv
import datetime
import re
from pathlib import Path

import config
import daily_feedback
import state_sync
import time_features
from run_pipeline import run_prediction_pipeline

# Allows an optional " (1)", " (2)" etc. suffix before ".mp4" -- browsers
# append this automatically when a file of the same name is downloaded
# more than once, so a real-world clip is often named
# "..._clean (1).mp4" instead of "..._clean.mp4".
# Hour/minute/second segments use \d{1,2} (not \d{2}) because some
# exports have shown up missing a leading zero (e.g. "9-45-09" for
# 09:45:09) -- strptime below still parses these correctly.
_VIDEO_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{1,2}-\d{1,2}-\d{1,2})_clean.*\.mp4$")

# If the nearest available screenshot/video is farther than this from a
# scheduled capture time, warn loudly instead of silently using a
# poor/stale match (can happen for dates captured before CAPTURE_TIMES
# was in effect, e.g. under the old 20-min-interval schedule).
MAX_MATCH_TOLERANCE_MINUTES = 30


def _find_nearest_screenshot_dir(date_str: str, target_dt: datetime.datetime, screenshots_dir: Path):
    """Finds the <screenshots_dir>/<date>_<HH-MM-SS>/ folder captured
    closest in time to target_dt on date_str (screenshots_dir defaults to
    config.SCREENSHOT_DIR, but can point anywhere -- e.g. a custom folder
    you dropped manually-captured screenshots into). Returns
    (folder_path, actual_capture_dt, minutes_off) or (None, None, None)."""
    candidates = []
    for d in screenshots_dir.glob(f"{date_str}_*"):
        if not d.is_dir():
            continue
        try:
            ts = datetime.datetime.strptime(d.name, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            continue
        candidates.append((abs((ts - target_dt).total_seconds()), ts, d))

    if not candidates:
        return None, None, None
    candidates.sort(key=lambda c: c[0])
    seconds_off, ts, d = candidates[0]
    return d, ts, seconds_off / 60.0


def _find_nearest_video(date_str: str, target_dt: datetime.datetime, videos_dir: Path):
    """Finds the <videos_dir>/*_clean.mp4 file captured closest in time to
    target_dt on date_str (videos_dir defaults to config.VIDEO_DIR, but
    can point anywhere -- e.g. a custom folder with manually-captured
    clips). Returns (path, actual_capture_dt, minutes_off) or
    (None, None, None)."""
    candidates = []
    for f in videos_dir.glob(f"*_{date_str}_*_clean*.mp4"):
        m = _VIDEO_TS_RE.search(f.name)
        if not m:
            continue
        try:
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            continue
        candidates.append((abs((ts - target_dt).total_seconds()), ts, f))

    if not candidates:
        return None, None, None
    candidates.sort(key=lambda c: c[0])
    seconds_off, ts, f = candidates[0]
    return f, ts, seconds_off / 60.0


def _build_image_map(screenshots_dir: Path) -> dict:
    image_map = {}
    for layer, description in config.LAYERS.items():
        path = screenshots_dir / f"{layer}.png"
        if path.exists():
            image_map[str(path)] = description
    return image_map


def _blocks_from_time_to_end_of_day(forecast_start_time: datetime.datetime) -> int:
    """How many 15-min blocks from the first block at/after
    forecast_start_time through 23:45 of that same day."""
    end_of_day = forecast_start_time.replace(hour=23, minute=45, second=0, microsecond=0)
    first_block = time_features.get_block_times(forecast_start_time, num_blocks=1)[0]
    if first_block > end_of_day:
        return 0
    minutes_remaining = (end_of_day - first_block).total_seconds() / 60.0
    return int(minutes_remaining // config.BLOCK_MINUTES) + 1


def _load_full_day_actuals(actuals_path: Path) -> dict:
    """Returns {time_label ("YYYY-MM-DD HH:MM"): actual_mw} from the raw
    meter-export CSV, for the FINAL comparison at the end (this is
    allowed to see the whole day, unlike the per-iteration truncated
    text fed to the LLM)."""
    readings = {}
    with open(actuals_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            normalized = daily_feedback._normalize_timestamp(row.get("TimeStamp"))
            if normalized is None:
                continue
            kw = daily_feedback._as_float(row.get("Active Power (kW)"))
            if kw is None:
                continue
            time_label = normalized[:16]  # "YYYY-MM-DD HH:MM"
            readings[time_label] = max(0.0, kw) / 1000.0
    return readings


def _load_enercast_predictions(xlsx_path: Path, date_str: str) -> dict:
    """
    Reads a third-party Enercast "Frozen File" export (columns: Block,
    Time (HH:MM, no date), Scheduled MW, ...) and returns
    {time_label ("YYYY-MM-DD HH:MM"): scheduled_mw} for the given
    date_str, so it can be lined up against our own schedule and the
    real actuals in the same comparison.
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active

    readings = {}
    header = None
    time_idx = mw_idx = None
    for row in ws.iter_rows(values_only=True):
        if header is None:
            header = [str(c).strip() if c is not None else "" for c in row]
            if "Time" not in header or "Scheduled MW" not in header:
                raise ValueError(f"Expected 'Time' and 'Scheduled MW' columns, found: {header}")
            time_idx = header.index("Time")
            mw_idx = header.index("Scheduled MW")
            continue
        raw_time = row[time_idx] if time_idx < len(row) else None
        raw_mw = row[mw_idx] if mw_idx < len(row) else None
        if not raw_time or raw_mw in (None, ""):
            continue
        try:
            mw = float(raw_mw)
        except (TypeError, ValueError):
            continue
        time_label = f"{date_str} {str(raw_time).strip()}"
        readings[time_label] = mw
    return readings


def run_backtest(date_str: str, actuals_path: Path, screenshots_dir: Path = None, videos_dir: Path = None,
                  enercast_path: Path = None) -> None:
    screenshots_dir = screenshots_dir or config.SCREENSHOT_DIR
    videos_dir = videos_dir or config.VIDEO_DIR

    print(f"Reconstructing schedule for {date_str} using {len(config.CAPTURE_TIMES)} "
          f"scheduled capture times: {', '.join(config.CAPTURE_TIMES)}")
    print(f"Looking for screenshots in: {screenshots_dir.resolve()}")
    print(f"Looking for videos in: {videos_dir.resolve()}")
    print(f"Output isolated to: {config.EVALUATION_OUTPUT_DIR.resolve()}\n")

    # Tracks, per block Time, which scheduling interval (T) most recently
    # generated/finalized it -- a later interval overwriting an earlier
    # one's block naturally updates this too, so it always reflects the
    # LAST interval that actually produced that block's final value.
    generated_at: dict = {}

    for time_str in config.CAPTURE_TIMES:
        hour, minute = map(int, time_str.split(":"))
        target_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour, minute=minute)

        print(f"{'=' * 60}\nScheduling interval T = {time_str}\n{'=' * 60}")

        matched_screenshots_dir, shot_ts, shot_off = _find_nearest_screenshot_dir(date_str, target_dt, screenshots_dir)
        video_path, video_ts, video_off = _find_nearest_video(date_str, target_dt, videos_dir)

        if matched_screenshots_dir is None or video_path is None:
            print(f"  [WARN] No screenshots/video found near {time_str} on {date_str} -- skipping this interval.")
            continue

        if shot_off > MAX_MATCH_TOLERANCE_MINUTES or video_off > MAX_MATCH_TOLERANCE_MINUTES:
            print(f"  [WARN] Nearest available capture is {shot_off:.0f} min (screenshots) / "
                  f"{video_off:.0f} min (video) away from the scheduled {time_str} -- "
                  f"using it anyway, but this is a poor match (likely captured under a "
                  f"different schedule than CAPTURE_TIMES).")

        print(f"  Using screenshots from {shot_ts.strftime('%H:%M:%S')} ({shot_off:.1f} min off) "
              f"and video from {video_ts.strftime('%H:%M:%S')} ({video_off:.1f} min off)")

        image_map = _build_image_map(matched_screenshots_dir)
        if not image_map:
            print(f"  [WARN] {matched_screenshots_dir} has no layer screenshots -- skipping this interval.")
            continue

        intraday_actuals_text = daily_feedback.format_intraday_actuals_for_prompt(actuals_path, target_dt)

        # Nudge 1 second past T so the forecast starts STRICTLY after T,
        # never re-predicting the exact block T already has an actual for
        # (same fix as manual_prediction.py). num_blocks MUST be computed
        # from this same nudged time, not target_dt -- when T lands
        # exactly on a block boundary (e.g. 06:45), computing it from the
        # un-nudged target_dt undercounts the shift to the next block and
        # ends up requesting one extra (spurious, past-midnight) block.
        forecast_start_time = target_dt + datetime.timedelta(seconds=1)

        num_blocks = _blocks_from_time_to_end_of_day(forecast_start_time)
        if num_blocks <= 0:
            print(f"  [INFO] {time_str} is already at/past end of day -- nothing to forecast.")
            continue

        print(f"  Forecasting {num_blocks} blocks from just after {time_str} through 23:45...")
        run_prediction_pipeline(
            image_map, video_path, reference_time=forecast_start_time, num_blocks=num_blocks,
            output_dir=config.EVALUATION_OUTPUT_DIR, intraday_actuals_text=intraday_actuals_text,
            intraday_actuals_path=actuals_path,
        )

        for block_time in time_features.get_block_times(forecast_start_time, num_blocks=num_blocks):
            generated_at[block_time.strftime("%Y-%m-%d %H:%M")] = time_str

    _compare_final_schedule(date_str, actuals_path, generated_at, enercast_path)


def _compare_final_schedule(date_str: str, actuals_path: Path, generated_at: dict, enercast_path: Path = None) -> None:
    """
    Builds ONE detailed CSV combining every scheduling interval's
    contribution: for every block in the final reconstructed schedule,
    which interval (T) generated it, OUR predicted/actual/error in BOTH
    MW and kW, the % variation -- and, if enercast_path is given, the
    third-party Enercast schedule for the same blocks alongside its own
    error vs actual and a direct our-vs-Enercast difference. A summary
    (MAE/RMSE/MAPE/Bias for us, and separately for Enercast) is written
    as commented header lines at the top of the same file.
    """
    schedule_path = config.EVALUATION_OUTPUT_DIR / f"{config.PLANT_NAME}_energy_generation_{date_str}.csv"
    if not schedule_path.exists():
        print(f"\n[WARN] No reconstructed schedule was produced at {schedule_path} -- nothing to compare.")
        return

    def _prediction_time(row):
        if row.get("Time"):
            return row["Time"]
        interval = row.get("Time Interval (15 minute interval)", "")
        if " - " in interval:
            return interval.split(" - ", 1)[0]
        return ""

    def _prediction_mw(row):
        return row.get("LLM Schedule (MW)", row.get("Predicted Generation (MW)", ""))

    with open(schedule_path, "r", newline="", encoding="utf-8") as f:
        predicted = {
            _prediction_time(row): float(_prediction_mw(row))
            for row in csv.DictReader(f)
            if _prediction_time(row) and _prediction_mw(row)
        }

    actual_readings = _load_full_day_actuals(actuals_path)
    enercast_readings = _load_enercast_predictions(enercast_path, date_str) if enercast_path else {}
    if enercast_path:
        print(f"Loaded {len(enercast_readings)} Enercast scheduled blocks for {date_str}.")

    matched_rows = []
    enercast_matched_rows = []
    detail_rows = []
    for time_label, predicted_mw in sorted(predicted.items()):
        if time_label not in actual_readings:
            continue
        actual_mw = actual_readings[time_label]
        error_mw = predicted_mw - actual_mw
        error_pct = (error_mw / actual_mw * 100.0) if actual_mw > 0.05 else None
        matched_rows.append((predicted_mw, actual_mw))

        row = {
            "Time": time_label,
            "Generated At Interval": generated_at.get(time_label, ""),
            "Our Predicted (MW)": round(predicted_mw, 3),
            "Our Predicted (kW)": round(predicted_mw * 1000, 1),
            "Actual (MW)": round(actual_mw, 3),
            "Actual (kW)": round(actual_mw * 1000, 1),
            "Our Error (MW)": round(error_mw, 3),
            "Our Error (kW)": round(error_mw * 1000, 1),
            "Our Variation (%)": round(error_pct, 1) if error_pct is not None else "",
            "Our Abs Error (MW)": round(abs(error_mw), 3),
        }

        if enercast_path:
            enercast_mw = enercast_readings.get(time_label)
            if enercast_mw is not None:
                enercast_error_mw = enercast_mw - actual_mw
                enercast_error_pct = (enercast_error_mw / actual_mw * 100.0) if actual_mw > 0.05 else None
                enercast_matched_rows.append((enercast_mw, actual_mw))
                row.update({
                    "Enercast (MW)": round(enercast_mw, 3),
                    "Enercast (kW)": round(enercast_mw * 1000, 1),
                    "Enercast Error (MW)": round(enercast_error_mw, 3),
                    "Enercast Variation (%)": round(enercast_error_pct, 1) if enercast_error_pct is not None else "",
                    "Enercast Abs Error (MW)": round(abs(enercast_error_mw), 3),
                    "Us vs Enercast (MW)": round(predicted_mw - enercast_mw, 3),
                })
            else:
                row.update({
                    "Enercast (MW)": "", "Enercast (kW)": "", "Enercast Error (MW)": "",
                    "Enercast Variation (%)": "", "Enercast Abs Error (MW)": "", "Us vs Enercast (MW)": "",
                })

        detail_rows.append(row)

    print(f"\n{'=' * 60}\nFinal reconstructed schedule vs actual meter data -- {date_str}\n{'=' * 60}")
    header = f"{'Time':<18} {'Interval':>9} {'Us(MW)':>8} {'Act(MW)':>8} {'OurErr':>8} {'OurVar%':>8}"
    if enercast_path:
        header += f" {'Enercast':>9} {'EncErr':>8} {'EncVar%':>8}"
    print(header)
    for r in detail_rows:
        our_var = f"{r['Our Variation (%)']:+.1f}" if r["Our Variation (%)"] != "" else "n/a"
        line = (f"{r['Time']:<18} {r['Generated At Interval']:>9} {r['Our Predicted (MW)']:>8.3f} "
                f"{r['Actual (MW)']:>8.3f} {r['Our Error (MW)']:>8.3f} {our_var:>8}")
        if enercast_path:
            if r.get("Enercast (MW)") != "":
                enc_var = f"{r['Enercast Variation (%)']:+.1f}" if r["Enercast Variation (%)"] != "" else "n/a"
                line += f" {r['Enercast (MW)']:>9.3f} {r['Enercast Error (MW)']:>8.3f} {enc_var:>8}"
            else:
                line += f" {'n/a':>9} {'n/a':>8} {'n/a':>8}"
        print(line)

    if not matched_rows:
        print("\n[INFO] No matching timestamps between the reconstructed schedule and actual data.")
        return

    metrics = daily_feedback._compute_error_metrics(matched_rows)
    print(f"\n--- Our model vs actual ---")
    print(f"Matched blocks: {metrics['n_matched_blocks']}")
    print(f"MAE:  {metrics['mae']} MW")
    print(f"RMSE: {metrics['rmse']} MW")
    print(f"MAPE: {metrics['mape_pct']}%")
    print(f"Bias: {metrics['bias']:+} MW ({'over-forecast' if metrics['bias'] > 0 else 'under-forecast'})")

    enercast_metrics = None
    if enercast_path:
        if enercast_matched_rows:
            enercast_metrics = daily_feedback._compute_error_metrics(enercast_matched_rows)
            print(f"\n--- Enercast vs actual ---")
            print(f"Matched blocks: {enercast_metrics['n_matched_blocks']}")
            print(f"MAE:  {enercast_metrics['mae']} MW")
            print(f"RMSE: {enercast_metrics['rmse']} MW")
            print(f"MAPE: {enercast_metrics['mape_pct']}%")
            print(f"Bias: {enercast_metrics['bias']:+} MW "
                  f"({'over-forecast' if enercast_metrics['bias'] > 0 else 'under-forecast'})")
            print(f"\n--- Head-to-head ---")
            better = "US" if metrics["mae"] < enercast_metrics["mae"] else "ENERCAST"
            print(f"Lower MAE (better): {better} (Us: {metrics['mae']} MW vs Enercast: {enercast_metrics['mae']} MW)")
        else:
            print("\n[INFO] No Enercast blocks matched our schedule's timestamps.")

    comparison_path = config.EVALUATION_OUTPUT_DIR / f"{config.PLANT_NAME}_backtest_comparison_{date_str}.csv"
    with open(comparison_path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# Schedule reconstruction backtest -- {config.PLANT_NAME} -- {date_str}\n")
        f.write(f"# --- Our model vs actual ---\n")
        f.write(f"# Matched blocks: {metrics['n_matched_blocks']}\n")
        f.write(f"# MAE (MW): {metrics['mae']}\n")
        f.write(f"# RMSE (MW): {metrics['rmse']}\n")
        f.write(f"# MAPE (%): {metrics['mape_pct']}\n")
        f.write(f"# Bias (MW): {metrics['bias']:+} ({'over-forecast' if metrics['bias'] > 0 else 'under-forecast'})\n")
        if enercast_metrics:
            f.write(f"# --- Enercast vs actual ---\n")
            f.write(f"# Matched blocks: {enercast_metrics['n_matched_blocks']}\n")
            f.write(f"# MAE (MW): {enercast_metrics['mae']}\n")
            f.write(f"# RMSE (MW): {enercast_metrics['rmse']}\n")
            f.write(f"# MAPE (%): {enercast_metrics['mape_pct']}\n")
            f.write(f"# Bias (MW): {enercast_metrics['bias']:+} "
                    f"({'over-forecast' if enercast_metrics['bias'] > 0 else 'under-forecast'})\n")
        writer = csv.DictWriter(f, fieldnames=list(detail_rows[0].keys()))
        writer.writeheader()
        writer.writerows(detail_rows)
    print(f"\nDetailed comparison CSV saved to: {comparison_path.resolve()}")

    # ---- Automatically fold this day's error analysis + actual-generation
    # pattern into the rolling day-level context
    # (prediction_context/<PLANT>_context.json), so the NEXT day's forecast
    # sees it as evidence in the LLM prompt without any manual step.
    # _add_day_to_context replaces any existing entry for this same date
    # (safe to re-run) and keeps only the most recent
    # config.CONTEXT_WINDOW_DAYS days, dropping the oldest automatically.
    context_matched = [
        {"time": r["Time"], "predicted_mw": r["Our Predicted (MW)"], "actual_mw": r["Actual (MW)"], "row": {}}
        for r in detail_rows
    ]
    context_entry = daily_feedback._analyze_day_patterns(date_str, context_matched)
    actual_rows_for_pattern = [
        (datetime.datetime.strptime(r["Time"], "%Y-%m-%d %H:%M"), r["Actual (MW)"]) for r in detail_rows
    ]
    pattern = daily_feedback._analyze_actual_pattern(date_str, actual_rows_for_pattern)
    if pattern:
        context_entry["actual_pattern"] = pattern
    daily_feedback._add_day_to_context(context_entry)
    print(f"Rolling day-level context updated: {config.PREDICTION_CONTEXT_PATH.resolve()}")

    if state_sync.is_enabled():
        try:
            pushed = state_sync.push_state_to_s3(managed_paths=(config.PREDICTION_CONTEXT_PATH,))
            if pushed.uploaded:
                print(
                    f"Synced {pushed.uploaded} context file(s) to S3 after backtest update."
                )
        except Exception as exc:
            print(f"[WARN] Could not sync backtest prediction context to S3: {exc}")
    print("(Will be used as evidence for the next day's forecast.)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help='Date to reconstruct, "YYYY-MM-DD"')
    parser.add_argument("--actuals", required=True, help="Path to the FULL DAY actual meter-export CSV")
    parser.add_argument(
        "--screenshots-dir", default=None,
        help=f"Folder containing <date>_<HH-MM-SS>/ screenshot subfolders (default: {config.SCREENSHOT_DIR}). "
             f"Point this at a custom folder instead of moving files into place.",
    )
    parser.add_argument(
        "--videos-dir", default=None,
        help=f"Folder containing the *_clean.mp4 video files (default: {config.VIDEO_DIR}). "
             f"Point this at a custom folder instead of moving files into place.",
    )
    parser.add_argument(
        "--enercast", default=None,
        help="Path to a third-party Enercast 'Frozen File' xlsx export (columns: Block, Time, "
             "Scheduled MW, ...) for the same date -- if given, it's added to the comparison CSV "
             "alongside our own schedule, and compared against the actuals too.",
    )
    args = parser.parse_args()

    actuals_path = Path(args.actuals)
    if not actuals_path.exists():
        raise SystemExit(f"--actuals file not found: {actuals_path}")

    screenshots_dir = Path(args.screenshots_dir) if args.screenshots_dir else None
    videos_dir = Path(args.videos_dir) if args.videos_dir else None
    enercast_path = Path(args.enercast) if args.enercast else None
    if screenshots_dir and not screenshots_dir.is_dir():
        raise SystemExit(f"--screenshots-dir not found: {screenshots_dir}")
    if videos_dir and not videos_dir.is_dir():
        raise SystemExit(f"--videos-dir not found: {videos_dir}")
    if enercast_path and not enercast_path.exists():
        raise SystemExit(f"--enercast file not found: {enercast_path}")

    run_backtest(args.date, actuals_path, screenshots_dir=screenshots_dir, videos_dir=videos_dir,
                 enercast_path=enercast_path)


if __name__ == "__main__":
    main()
