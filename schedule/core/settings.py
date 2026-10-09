"""Centralized S3 path configuration and environment resolvers."""

from __future__ import annotations

import os
from pathlib import Path


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


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


DEFAULT_S3_RAW_OWNER = _env_str("S3_RAW_OWNER", "vedanjay")
DEFAULT_TIMEZONE = _env_str("DEFAULT_TIMEZONE", "Asia/Kolkata")
FORECAST_HORIZON_HOURS = _env_int("FORECAST_HORIZON_HOURS", 3)
FORECAST_BLOCKS = _env_int("FORECAST_BLOCKS", 12)
ENABLE_S3_STATE_SYNC = _env_bool("ENABLE_S3_STATE_SYNC", False)


def resolve_s3_prefixes(site_id: str, raw_owner: str | None = None) -> tuple[str, str, str]:
    """Resolve default raw capture, meter, and output schedule prefixes."""
    owner = raw_owner or DEFAULT_S3_RAW_OWNER
    site_clean = site_id.strip().upper()

    if site_clean in ("ZTRIC", "ENRICH", "SHAHA"):
        raw_prefix = f"raw/{owner}/multiple_generator/{site_clean}"
        schedule_prefix = f"generated/vedanjay_ai_intellis/multiple_generator/{site_clean}/outputs"
    else:
        raw_prefix = f"raw/{owner}/{site_clean}"
        schedule_prefix = f"generated/vedanjay_ai_intellis/{site_clean}/outputs"

    return raw_prefix, raw_prefix, schedule_prefix
