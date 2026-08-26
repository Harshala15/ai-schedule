"""
manual_prediction.py

Command-line entry point for TESTING the prediction pipeline against
MANUALLY SUPPLIED inputs, instead of the automated Windy capture (see
test_multi_image.py). Use this when you have a screenshot set + video
from some point in time (e.g. 2:15 PM), plus the actual meter data for
that day -- and want to see what the system predicts going FORWARD from
that capture time.

Simplest usage -- just drop your files in and run:
    manual_input/screenshots/   <- put satellite.png, wind.png, solarpower.png,
                                    clouds.png, rain.png here
    manual_input/video/         <- put your one video file here (any filename)
    manual_input/actuals/       <- put your one actual-meter CSV here (any filename,
                                    full-day export is fine -- see below)

    python manual_prediction.py --capture-time "2026-07-26 14:15" [--hours 1]

--capture-time  When the screenshots/video were captured, "YYYY-MM-DD
                HH:MM". The forecast covers the blocks AFTER this time --
                this is what makes "predict forward from 2:15" mean
                forward from 2:15, regardless of the real current time.
                Defaults to right now if omitted.
--hours         How many hours ahead to forecast (default: 2, i.e. the
                normal 8 blocks). Pass --hours 1 for a 1-hour-ahead
                (4-block) forecast instead.

Isolation from real production data (both directions):
  - The actuals CSV is NEVER merged into historic_cases/merged_scada_data.csv
    or the rolling prediction_context -- even if it's a full-day export,
    only rows AT OR BEFORE --capture-time are used, and only as a plain
    "here's how generation went earlier today" text block fed to the LLM.
    Rows after --capture-time are ignored, so a full-day file can never
    leak "future" actuals into a forecast that's only supposed to see the
    past. (To actually feed real actuals into the live system, use
    process_daily_actuals.py / the local manual feedback folder instead
    -- that's a separate, deliberate action.)
  - Predictions and their feature log are written to manual_input/output/
    -- never to energy_predictions/ or features_log/ -- so test runs can
    never contaminate the real per-day production files or the CBR case
    store that future automated runs read from.
"""

import argparse
import datetime
from pathlib import Path

import config
from modules.feedback import daily_feedback
from run_pipeline import run_prediction_pipeline


def _build_image_map(screenshots_dir: Path) -> dict:
    image_map = {}
    for layer, description in config.LAYERS.items():
        path = screenshots_dir / f"{layer}.png"
        if path.exists():
            image_map[str(path)] = description
        else:
            print(f"  [WARN] {path} not found -- skipping '{layer}' layer.")
    if not image_map:
        raise SystemExit(
            f"No layer screenshots found in {screenshots_dir} -- expected files named "
            f"{', '.join(f'{layer}.png' for layer in config.LAYERS)}."
        )
    return image_map


def _find_single_file(folder: Path, kind: str) -> Path:
    """Picks the one file sitting in `folder` (e.g. manual_input/video/) so
    the user doesn't have to type a full path -- errors clearly if the
    folder is empty or has more than one file (ambiguous)."""
    files = [p for p in folder.glob("*") if p.is_file()]
    if not files:
        raise SystemExit(f"No {kind} file found in {folder} -- put exactly one file there, or pass --{kind} explicitly.")
    if len(files) > 1:
        names = ", ".join(p.name for p in files)
        raise SystemExit(f"Found more than one file in {folder} ({names}) -- keep only one {kind} file there, "
                          f"or pass --{kind} explicitly to pick one.")
    return files[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshots", default=None,
                         help=f"Folder with the layer screenshots (default: {config.MANUAL_INPUT_SCREENSHOTS_DIR})")
    parser.add_argument("--video", default=None,
                         help=f"Path to the satellite/cloud animation video (default: the one file in {config.MANUAL_INPUT_VIDEO_DIR})")
    parser.add_argument("--actuals", default=None,
                         help=f"Path to the actual meter-export CSV (default: the one file in {config.MANUAL_INPUT_ACTUALS_DIR})")
    parser.add_argument(
        "--capture-time", default=None,
        help='When the screenshots/video were captured, "YYYY-MM-DD HH:MM" (default: now)',
    )
    parser.add_argument(
        "--hours", type=float, default=None,
        help="How many hours ahead to forecast (default: 2, i.e. config.NUM_FORECAST_BLOCKS blocks)",
    )
    args = parser.parse_args()

    screenshots_dir = Path(args.screenshots) if args.screenshots else config.MANUAL_INPUT_SCREENSHOTS_DIR
    video_path = Path(args.video) if args.video else _find_single_file(config.MANUAL_INPUT_VIDEO_DIR, "video")
    actuals_path = Path(args.actuals) if args.actuals else _find_single_file(config.MANUAL_INPUT_ACTUALS_DIR, "actuals")

    if not screenshots_dir.is_dir():
        raise SystemExit(f"--screenshots folder not found: {screenshots_dir}")
    if not video_path.exists():
        raise SystemExit(f"--video file not found: {video_path}")
    if not actuals_path.exists():
        raise SystemExit(f"--actuals file not found: {actuals_path}")

    reference_time = (
        datetime.datetime.strptime(args.capture_time, "%Y-%m-%d %H:%M")
        if args.capture_time else datetime.datetime.now()
    )

    num_blocks = round(args.hours * 60 / config.BLOCK_MINUTES) if args.hours else None

    # Deliberately NOT merged into historic_cases/ or the rolling context --
    # only rows at/before reference_time are read, and only as prompt text.
    # See the module docstring for why.
    intraday_actuals_text = daily_feedback.format_intraday_actuals_for_prompt(actuals_path, reference_time)
    print("Actual generation earlier today (fed to the LLM, NOT merged into real production history):")
    print(f"  {intraday_actuals_text}")

    image_map = _build_image_map(screenshots_dir)

    # Forecast must start STRICTLY AFTER the capture time, not at-or-after
    # it -- get_block_times() rounds --capture-time up to the next block
    # boundary AT OR AFTER it, so when --capture-time already lands
    # exactly on one (e.g. "14:15"), the first "predicted" block would
    # otherwise duplicate the very timestamp we already have a real
    # actual for. Nudging by 1 second pushes that case to the next
    # boundary instead, without affecting anything else.
    forecast_start_time = reference_time + datetime.timedelta(seconds=1)

    print(f"\nRunning TEST prediction using capture time: {reference_time.strftime('%Y-%m-%d %H:%M')} "
          f"({num_blocks or config.NUM_FORECAST_BLOCKS} blocks ahead) -- "
          f"output isolated to {config.MANUAL_INPUT_OUTPUT_DIR}")
    run_prediction_pipeline(
        image_map, video_path, reference_time=forecast_start_time, num_blocks=num_blocks,
        output_dir=config.MANUAL_INPUT_OUTPUT_DIR, intraday_actuals_text=intraday_actuals_text,
        intraday_actuals_path=actuals_path,
    )


if __name__ == "__main__":
    main()
