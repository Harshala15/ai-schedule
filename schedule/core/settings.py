"""Centralized S3 path configuration and environment resolvers."""

from __future__ import annotations

import os
from pathlib import Path


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DEFAULT_S3_RAW_OWNER = _env_str("S3_RAW_OWNER", "vedanjay")
DEFAULT_TIMEZONE = _env_str("DEFAULT_TIMEZONE", "Asia/Kolkata")


def resolve_s3_prefixes(site_id: str, raw_owner: str | None = None) -> tuple[str, str, str]:
    """Resolve default raw capture, meter, and output schedule prefixes."""
    owner = raw_owner or DEFAULT_S3_RAW_OWNER
    site_clean = site_id.strip().upper()

    if site_clean == "ZTRIC":
        raw_prefix = f"raw/{owner}/multiple_generator/ZTRIC"
        schedule_prefix = "generated/vedanjay_ai_intellis/multiple_generator/ZTRIC/outputs"
    else:
        raw_prefix = f"raw/{owner}/{site_clean}"
        schedule_prefix = f"generated/vedanjay_ai_intellis/{site_clean}/outputs"

    return raw_prefix, raw_prefix, schedule_prefix
