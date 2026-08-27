"""
config.py

Single source of truth for the Windy capture workflow.
"""

import os
import re
from pathlib import Path


ENV_FILE_PATH = Path(__file__).resolve().with_name(".env")
IS_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
OUTPUT_ROOT = Path("/tmp") if IS_LAMBDA else Path(".")


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


def _get_env_value(name: str, default: str = "") -> str:
    """Read from the process environment first, then fall back to .env."""
    return os.getenv(name, _read_env_value(name) or default).strip()


def _normalize_s3_bucket_name(value: str) -> str:
    """S3 bucket names must be lowercase and use only valid characters."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9.-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip(".-")
    return value


# ---- Plant details ----
SITES = (
    {
        "name": "SIRMOUR",
        "lat": 24.56253056,
        "lon": 75.09140278,
        "s3_prefix": "sirmour",
        "capacity_mw": None,
    },
    {
        "name": "KASIPET",
        "lat": 19.03943918,
        "lon": 79.43691745,
        "s3_prefix": "kasipet",
        "capacity_mw": None,
    },
    {
        "name": "BHUPALPALLY",
        "lat": 18.447931,
        "lon": 79.877263,
        "s3_prefix": "bhupalpally",
        "capacity_mw": 10,
    },
    {
        "name": "OSEPL",
        "lat": 17.9068,
        "lon": 76.3229,
        "s3_prefix": "osepl",
        "capacity_mw": None,
    },
)

PLANT_NAME = SITES[0]["name"]
PLANT_LAT = SITES[0]["lat"]
PLANT_LON = SITES[0]["lon"]
PLANT_CAPACITY_MW = SITES[0].get("capacity_mw")

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

# ---- Run timing ----
RUN_INTERVAL_SECONDS = 20 * 60
LAMBDA_GATED_VIDEO_SITES = ("SIRMOUR", "KASIPET", "BHUPALPALLY")
REVISION_TIMES = ("05:55", "06:45", "08:15", "09:45", "11:15", "14:15", "15:45")
LAMBDA_CAPTURE_OFFSET_MINUTES = 5
LAMBDA_CAPTURE_WINDOW_MINUTES = int(_get_env_value("LAMBDA_CAPTURE_WINDOW_MINUTES", "5"))

# ---- S3 upload ----
S3_BUCKET_NAME = _normalize_s3_bucket_name(
    _get_env_value("S3_BUCKET_NAME", _get_env_value("S3_BUCKET", "ai-forecasting-storage"))
)
S3_REGION = _get_env_value("S3_REGION", "ap-south-1")
S3_PREFIX = _get_env_value("S3_PREFIX", SITES[0]["s3_prefix"]).strip().strip("/")
AUTO_CREATE_S3_BUCKET = _get_env_value("AUTO_CREATE_S3_BUCKET", "true").lower() not in {"0", "false", "no"}
PLANT_ID = _get_env_value("PLANT_ID", "vedanjay")
SITE_ID = _get_env_value("SITE_ID", "")

# ---- Paths ----
STORAGE_STATE_PATH = Path("windy_login.json")
SCREENSHOT_DIR = OUTPUT_ROOT / "windy_screenshots" / f"{PLANT_LAT}_{PLANT_LON}"
VIDEO_DIR = OUTPUT_ROOT / "windy_videos"

for _dir in (SCREENSHOT_DIR, VIDEO_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
