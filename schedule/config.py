"""
config.py

Single source of truth for plant details, file paths, and pipeline
settings -- imported by every other file in this project. Centralizing
these here avoids circular imports between test_multi_image.py and the
new feature/ML pipeline files, and means you only ever update plant
details in ONE place.
"""

import os
from pathlib import Path


# ---- API credentials ----
# Keep secrets in the project .env file (which is excluded from version
# control). An environment variable takes precedence, which also supports
# deployments that inject credentials externally.
ENV_FILE_PATH = Path(__file__).resolve().with_name(".env")


def _read_env_value(name: str) -> str:
    """Return a value from .env without requiring an extra dependency."""
    try:
        for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", _read_env_value("GEMINI_API_KEY")).strip()


def _read_env_str(name: str, default: str) -> str:
    value = os.getenv(name, _read_env_value(name))
    return value.strip() if value.strip() else default


def _read_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, _read_env_value(name)).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default

# ---- Local/runtime storage root ----
# Default to the current working directory for local development.
# In Lambda/container deployments, set SIMOUR_STORAGE_ROOT to /tmp/... so
# the shared pipeline modules can create their working folders on writable
# storage instead of the read-only package directory.
STORAGE_ROOT = Path(os.getenv("SIMOUR_STORAGE_ROOT", ".")).expanduser().resolve()


def _storage_path(*parts: str) -> Path:
    return STORAGE_ROOT.joinpath(*parts)


def _read_env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default

# ---- Plant details ----
PLANT_NAME = _read_env_str("PLANT_NAME", "SIRMOUR")
PLANT_LAT = _read_env_float("PLANT_LAT", 24.56253056)
PLANT_LON = _read_env_float("PLANT_LON", 75.09140278)

# Rated (nameplate) capacity in MW.
PLANT_CAPACITY_MW = _read_env_float("PLANT_CAPACITY_MW", 5.1)

# Performance Ratio -- accounts for real-world losses (panel temperature,
# inverter, wiring, soiling, shading, mismatch etc.). 0.75-0.85 is typical
# for a well-maintained plant. Update this once you have your plant's
# actual historical PR.
PERFORMANCE_RATIO = _read_env_float("PERFORMANCE_RATIO", 0.78)

# ---- Windy capture settings ----
ZOOM_LEVEL = 11  # calibrated so the screenshot covers ~100km x 100km
VIEWPORT_WIDTH = 1600
VIEWPORT_HEIGHT = 1000

LAYERS = {
    "satellite": "Satellite cloud imagery -- cloud position, density, and movement around the plant",
    "wind": "Wind speed, direction, and gusts",
    "solarpower": "Solar power / solar irradiance layer -- expected solar radiation intensity reaching the ground around the plant",
    "clouds": "Cloud cover layer -- overall cloud coverage and thickness around the plant",
    "rain": "Rain / precipitation layer -- rainfall intensity and coverage around the plant",
}

# ---- Animation video settings ----
RECORD_ANIMATION_VIDEO = True
ANIMATION_LAYER = "satellite"
# Short enough to avoid Windy's animation loop/reversal contaminating
# optical-flow features. This measures motion; it is not the forecast horizon.
ANIMATION_RECORD_SECONDS = 8

# ---- Forecast settings ----
NUM_FORECAST_BLOCKS = 8       # 8 x 15 min = next 2 hours
BLOCK_MINUTES = 15

# ---- Capture schedule ----
# Capture only at these fixed times each day (24h "HH:MM") -- required by
# the schedule-generation evaluation workflow, which reconstructs the
# day's schedule using exactly the forecast captured at each of these
# times (see backtest_schedule.py).
CAPTURE_TIMES = ["05:15", "06:45", "08:15", "09:45", "11:15", "12:45", "14:15", "15:45"]

# ---- Paths ----
STORAGE_STATE_PATH = _storage_path("windy_login.json")
SCREENSHOT_DIR = _storage_path("windy_screenshots") / f"{PLANT_LAT}_{PLANT_LON}"
VIDEO_DIR = _storage_path("windy_videos")
PREDICTIONS_DIR = _storage_path("energy_predictions")
FEATURES_LOG_DIR = _storage_path("features_log")
MODELS_DIR = _storage_path("models")
ACCURACY_REPORTS_DIR = _storage_path("accuracy_reports")
_DEFAULT_HISTORIC_CASES_DIR = _storage_path("historic_cases") if PLANT_NAME.upper() == "SIRMOUR" else _storage_path("historic_cases") / PLANT_NAME
HISTORIC_CASES_DIR = _read_env_path("HISTORIC_CASES_DIR", _DEFAULT_HISTORIC_CASES_DIR)

# ---- Daily actuals feedback automation ----
# Local/manual feedback drop folder for when you want to process a raw
# meter-export CSV outside the Lambda raw-meter flow.
# The actual learning logic lives in daily_feedback.py and can also be
# driven directly from the plant's raw meter files.
ACTUALS_INBOX_DIR = _storage_path("daily_actuals_inbox")
ACTUALS_INBOX_PROCESSED_DIR = ACTUALS_INBOX_DIR / "processed"

# Rolling day-level accuracy/pattern context fed into the LLM prompt (see
# llm_predictor.py) -- keeps only the most recent CONTEXT_WINDOW_DAYS days,
# dropping the oldest each time a new day is added.
PREDICTION_CONTEXT_PATH = _storage_path("prediction_context") / f"{PLANT_NAME}_context.json"
CONTEXT_WINDOW_DAYS = 3

# ---- Manual prediction input (see manual_prediction.py) ----
# Drop a manually-captured screenshot set, video, and actual-meter CSV
# here to run the pipeline without the automated Windy capture.
MANUAL_INPUT_DIR = _storage_path("manual_input")
MANUAL_INPUT_SCREENSHOTS_DIR = MANUAL_INPUT_DIR / "screenshots"
MANUAL_INPUT_VIDEO_DIR = MANUAL_INPUT_DIR / "video"
MANUAL_INPUT_ACTUALS_DIR = MANUAL_INPUT_DIR / "actuals"
# Test-run predictions and their feature log always land here -- kept
# completely separate from energy_predictions/ and features_log/ (the
# real per-day production files and CBR case store), so testing never
# mixes into or pollutes them.
MANUAL_INPUT_OUTPUT_DIR = MANUAL_INPUT_DIR / "output"

# ---- Schedule-generation evaluation (see backtest_schedule.py) ----
# Reconstructed full-day schedules (one real-time-accurate schedule per
# evaluated date) land here -- isolated from energy_predictions/ and
# features_log/ (real production + CBR case store) and from
# manual_input/output/ (one-off manual tests), since this is a distinct,
# multi-step evaluation run comparing a whole reconstructed day against
# the real meter data.
EVALUATION_OUTPUT_DIR = _storage_path("evaluation_schedules")

# ---- Case-Based Reasoning retrieval ----
# These weights express the relative importance of visual conditions and
# solar position when comparing a new situation with past feature rows.
# Values are applied after per-column z-score normalization.
CBR_TOP_K = 8
CBR_FEATURE_WEIGHTS = {
    "solar_elevation_deg": 2.5,
    "minute_of_day": 1.5,
    "clouds_bright_pixel_pct": 2.0,
    "satellite_bright_pixel_pct": 2.0,
    "motion_coverage_end_pct": 1.8,
    "motion_score": 1.3,
    "motion_directional_consistency": 0.8,
    "motion_direction_deg": 1.2,
    "rain_bright_pixel_pct": 1.0,
    "solarpower_bright_pixel_pct": 1.0,
    "clouds_brightness_std": 1.8,
    "satellite_brightness_std": 1.8,
}

for _dir in (SCREENSHOT_DIR, VIDEO_DIR, PREDICTIONS_DIR, FEATURES_LOG_DIR, MODELS_DIR, ACCURACY_REPORTS_DIR,
             HISTORIC_CASES_DIR, ACTUALS_INBOX_DIR, ACTUALS_INBOX_PROCESSED_DIR, PREDICTION_CONTEXT_PATH.parent,
             MANUAL_INPUT_SCREENSHOTS_DIR, MANUAL_INPUT_VIDEO_DIR, MANUAL_INPUT_ACTUALS_DIR, MANUAL_INPUT_OUTPUT_DIR,
             EVALUATION_OUTPUT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODELS_DIR / "generation_model.pkl"
