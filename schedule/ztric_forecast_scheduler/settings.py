"""Settings for the ZTRIC Intellis multiple-generator scheduler."""

from __future__ import annotations

import os

PLANT_NAME = os.getenv("PLANT_NAME", os.getenv("SITE_ID", "ZTRIC")).strip().upper()
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
DEFAULT_S3_RAW_OWNER = os.getenv("S3_RAW_OWNER", "vedanjay")
MULTI_GENERATOR_PLANT_TABLE = os.getenv("MULTI_GENERATOR_PLANT_TABLE", "multi_generator_plant")
MULTI_GENERATOR_PLANT_ID = os.getenv("MULTI_GENERATOR_PLANT_ID", "ZETRIC_SOLAR_PARK")
ROUND_DECIMALS = int(os.getenv("ZTRIC_ROUND_DECIMALS", "2"))
