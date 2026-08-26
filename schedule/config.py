"""
config.py

Single source of truth for plant details, file paths, and pipeline
settings -- imported by every other file in this project. Centralizing
these here avoids circular imports between test_multi_image.py and the
new feature/ML pipeline files, and means you only ever update plant
details in ONE place.
"""

import json
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


def _read_env_setting(name: str) -> str:
    return os.getenv(name, _read_env_value(name)).strip()

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


def _load_json_profile(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _profile_str(profile: dict, key: str, default: str) -> str:
    value = profile.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _profile_float(profile: dict, key: str, default: float) -> float:
    value = profile.get(key)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_profile_mw_setting(env_name: str, profile_key: str, default_mw: float) -> float:
    env_value = _read_env_setting(env_name)
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            return default_mw
    profile_kw = _profile_float(PLANT_PROFILE, profile_key, default_mw * 1000.0)
    return profile_kw / 1000.0

# ---- Plant profile / details ----
_DEFAULT_PLANT_NAME = _read_env_str("PLANT_NAME", "SIRMOUR")
_PLANT_FALLBACKS = {
    "SIRMOUR": {
        "latitude": 24.56253056,
        "longitude": 75.09140278,
        "capacity_mw": 5.1,
        "dc_capacity_mw": 5.48,
        "max_feed_in_mw": 5.1,
    },
    "KASIPET": {
        "latitude": 19.03943918,
        "longitude": 79.43691745,
        "capacity_mw": 15.0,
        "dc_capacity_mw": 16.5,
        "max_feed_in_mw": 15.0,
    },
    "BHUPALPALLY": {
        "latitude": 18.447931,
        "longitude": 79.877263,
        "capacity_mw": 10.0,
        "dc_capacity_mw": 11.005,
        "max_feed_in_mw": 10.0,
    },
}
_fallback = _PLANT_FALLBACKS.get(_DEFAULT_PLANT_NAME.upper(), _PLANT_FALLBACKS["SIRMOUR"])
_DEFAULT_PLANT_PROFILE_PATH = Path(
    os.getenv(
        "PLANT_PROFILE_PATH",
        _read_env_value("PLANT_PROFILE_PATH") or str(Path(__file__).resolve().with_name("plant_profiles") / f"{_DEFAULT_PLANT_NAME}.json"),
    )
).expanduser()
PLANT_PROFILE_PATH = _DEFAULT_PLANT_PROFILE_PATH
PLANT_PROFILE = _load_json_profile(_DEFAULT_PLANT_PROFILE_PATH)

def _read_profile_setting(env_name: str, profile_key: str, default: str) -> str:
    env_value = _read_env_setting(env_name)
    if env_value:
        return env_value
    return _profile_str(PLANT_PROFILE, profile_key, default)


def _read_profile_float_setting(env_name: str, profile_key: str, default: float) -> float:
    env_value = _read_env_setting(env_name)
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            return default
    return _profile_float(PLANT_PROFILE, profile_key, default)


PLANT_NAME = _read_profile_setting("PLANT_NAME", "plant_name", _DEFAULT_PLANT_NAME)
PLANT_LAT = _read_profile_float_setting("PLANT_LAT", "latitude", _fallback["latitude"])
PLANT_LON = _read_profile_float_setting("PLANT_LON", "longitude", _fallback["longitude"])

# Rated (nameplate) capacity in MW. For Bhupalpally we prefer the AC
# feed-in cap because that is the practical schedule ceiling.
PLANT_CAPACITY_MW = _read_profile_mw_setting(
    "PLANT_CAPACITY_MW",
    "maximum_feed_in_ac_kw",
    _fallback["capacity_mw"],
)

# Separate DC-capacity metadata when available in the plant profile.
PLANT_DC_CAPACITY_MW = _read_profile_mw_setting(
    "PLANT_DC_CAPACITY_MW",
    "dc_capacity_kw",
    _fallback["dc_capacity_mw"],
)

# Hard AC feed-in cap for the final schedule and validator clamp.
PLANT_MAX_FEED_IN_MW = _read_profile_mw_setting(
    "PLANT_MAX_FEED_IN_MW",
    "maximum_feed_in_ac_kw",
    _fallback["max_feed_in_mw"],
)

PLANT_TILT_DEG = _read_profile_float_setting("PLANT_TILT_DEG", "tilt_deg", 20.0)
PLANT_ORIENTATION_FROM_SOUTH_DEG = _read_profile_float_setting(
    "PLANT_ORIENTATION_FROM_SOUTH_DEG",
    "orientation_deg_from_south",
    0.0,
)
PLANT_TRACKER_TYPE = _read_profile_setting("PLANT_TRACKER_TYPE", "tracker_type", "None")
PLANT_AVAILABILITY_PLANNED_PCT = _read_profile_float_setting(
    "PLANT_AVAILABILITY_PLANNED_PCT",
    "availability_planned_pct",
    100.0,
)
PLANT_PPA_RATE_INR_PER_KWH = _read_profile_float_setting(
    "PLANT_PPA_RATE_INR_PER_KWH",
    "ppa_rate_inr_per_kwh",
    0.0,
)
PLANT_EEG_ID = _read_profile_setting("PLANT_EEG_ID", "eeg_id", "")
PLANT_KEY = _read_profile_setting("PLANT_KEY", "plant_key", "")

# Performance Ratio -- accounts for real-world losses (panel temperature,
# inverter, wiring, soiling, shading, mismatch etc.). 0.75-0.85 is typical
# for a well-maintained plant. Update this once you have your plant's
# actual historical PR.
PERFORMANCE_RATIO = _read_profile_float_setting("PERFORMANCE_RATIO", "performance_ratio", 0.78)

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
NUM_FORECAST_BLOCKS = 12      # 12 x 15 min = next 3 hours
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
FEEDBACK_ANALYSIS_DIR = _storage_path("feedback_analysis")
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
METER_HISTORY_DIR = _storage_path("meter_history")
PVLIB_SUMMARY_DIR = _storage_path("pvlib_summary")
PLANT_PERFORMANCE_DIR = _storage_path("plant_performance")
ECMWF_WEATHER_DIR = _storage_path("ecmwf_weather")
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
             HISTORIC_CASES_DIR, ACTUALS_INBOX_DIR, ACTUALS_INBOX_PROCESSED_DIR, PREDICTION_CONTEXT_PATH.parent, METER_HISTORY_DIR, PVLIB_SUMMARY_DIR, PLANT_PERFORMANCE_DIR, ECMWF_WEATHER_DIR,
             MANUAL_INPUT_SCREENSHOTS_DIR, MANUAL_INPUT_VIDEO_DIR, MANUAL_INPUT_ACTUALS_DIR, MANUAL_INPUT_OUTPUT_DIR,
             EVALUATION_OUTPUT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

MODEL_PATH = MODELS_DIR / "generation_model.pkl"
