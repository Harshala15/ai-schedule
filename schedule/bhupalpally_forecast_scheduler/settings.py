"""Configuration for the Bhupalpally forecast scheduler package."""

from __future__ import annotations

import os

import config


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


DEFAULT_TIMEZONE = "Asia/Kolkata"

# Bhupalpally runs a short rolling horizon from each revision time.
FORECAST_HORIZON_HOURS = _env_int("BHUPALPALLY_FORECAST_HORIZON_HOURS", 3)
FORECAST_BLOCKS = _env_int(
    "BHUPALPALLY_FORECAST_BLOCKS",
    max(1, FORECAST_HORIZON_HOURS * 60 // config.BLOCK_MINUTES),
)

# S3 layout used by the scheduler package.
DEFAULT_S3_RAW_OWNER = "vedanjay"
DEFAULT_S3_PLANT_FOLDER = "BHUPALPALLY"
DEFAULT_S3_CAPTURE_PREFIX = f"raw/{DEFAULT_S3_RAW_OWNER}/{DEFAULT_S3_PLANT_FOLDER}"
DEFAULT_S3_METER_PREFIX = f"raw/{DEFAULT_S3_RAW_OWNER}/{DEFAULT_S3_PLANT_FOLDER}"
DEFAULT_S3_SCHEDULE_PREFIX = f"generated/{DEFAULT_S3_PLANT_FOLDER}"
DEFAULT_S3_STATE_PREFIX = f"state/{DEFAULT_S3_RAW_OWNER}/{DEFAULT_S3_PLANT_FOLDER}"

PLANT_NAME = config.PLANT_NAME
PLANT_LAT = config.PLANT_LAT
PLANT_LON = config.PLANT_LON
BLOCK_MINUTES = config.BLOCK_MINUTES
LAYERS = config.LAYERS
ECMWF_TILT_DEGREES = _env_int("BHUPALPALLY_ECMWF_TILT_DEGREES", int(round(config.PLANT_TILT_DEG)))
ECMWF_AZIMUTH_DEGREES = _env_int(
    "BHUPALPALLY_ECMWF_AZIMUTH_DEGREES",
    int(round(180 + config.PLANT_ORIENTATION_FROM_SOUTH_DEG)),
)
ENABLE_S3_STATE_SYNC = _env_bool("ENABLE_S3_STATE_SYNC", False)
_DEFAULT_EFFECTIVE_DELAY_MINUTES = 45
EFFECTIVE_DELAY_MINUTES = _env_int("EFFECTIVE_DELAY_MINUTES", _DEFAULT_EFFECTIVE_DELAY_MINUTES)
