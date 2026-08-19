"""Configuration for the SIMOUR forecast scheduler package."""

import os
from datetime import time

import config


DEFAULT_TIMEZONE = "Asia/Kolkata"

# Forecast schedules are finalized through 19:00 local time.
SCHEDULE_END_TIME = time(19, 0)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

# S3 layout used by the scheduler package.
DEFAULT_S3_RAW_OWNER = "vedanjay"
DEFAULT_S3_PLANT_FOLDER = "SIRMOUR"
DEFAULT_S3_CAPTURE_PREFIX = f"raw/{DEFAULT_S3_RAW_OWNER}/{DEFAULT_S3_PLANT_FOLDER}"
DEFAULT_S3_METER_PREFIX = f"raw/{DEFAULT_S3_RAW_OWNER}/{DEFAULT_S3_PLANT_FOLDER}"
DEFAULT_S3_SCHEDULE_PREFIX = f"generated/{DEFAULT_S3_PLANT_FOLDER}"
DEFAULT_S3_STATE_PREFIX = f"state/{DEFAULT_S3_RAW_OWNER}/{DEFAULT_S3_PLANT_FOLDER}"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}

# Keep the scheduler package aligned with the plant being forecast.
PLANT_NAME = config.PLANT_NAME
PLANT_LAT = config.PLANT_LAT
PLANT_LON = config.PLANT_LON
BLOCK_MINUTES = config.BLOCK_MINUTES
LAYERS = config.LAYERS
ENABLE_S3_STATE_SYNC = _env_bool("ENABLE_S3_STATE_SYNC", False)
_DEFAULT_EFFECTIVE_DELAY_MINUTES = 45 if PLANT_NAME.upper() in {"KASIPET", "BHUPALPALLY"} else 90
EFFECTIVE_DELAY_MINUTES = _env_int("EFFECTIVE_DELAY_MINUTES", _DEFAULT_EFFECTIVE_DELAY_MINUTES)
