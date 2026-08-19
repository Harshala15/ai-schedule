"""
AWS Lambda entry point for the SIMOUR SFTP fetcher.

Flow:
1. Read SFTP credentials and settings from environment variables.
2. Download the newest file from the remote SFTP directory into /tmp.
3. Upload that file to S3 under:
       raw/vedanjay/SIRMOUR/YYYY-MM-DD/meter_data/<filename>

Expected environment variables:
    SFTP_HOST
    SFTP_PORT
    SFTP_USERNAME
    SFTP_PASSWORD
    SFTP_REMOTE_DIR
    S3_BUCKET
    S3_PREFIX_BASE   (optional, default: raw/vedanjay/SIRMOUR)

Optional local testing:
    python -m simour_fetcher.lambda_handler
"""

from __future__ import annotations

import re
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

from simour_fetcher.fetch_latest_sftp import SFTPConfig, download_latest_file


DEFAULT_S3_PREFIX_BASE = "raw/vedanjay/SIRMOUR"
IST = ZoneInfo("Asia/Kolkata")
ENV_FILE_PATH = Path(__file__).resolve().with_name(".env")
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")


def _read_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _read_env_file() -> dict:
    values = {}
    try:
        for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                continue
            values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def _build_sftp_config() -> SFTPConfig:
    env_file = _read_env_file()

    def pick(name: str, default: str = "") -> str:
        return _read_env(name, env_file.get(name, default))

    host = pick("SFTP_HOST")
    port = int(pick("SFTP_PORT", "22"))
    username = pick("SFTP_USERNAME")
    password = pick("SFTP_PASSWORD")
    remote_dir = pick("SFTP_REMOTE_DIR", "/incoming/powerdata_realtime/SIRMOUR/")
    if not host or not username or not password:
        raise RuntimeError(
            "Missing SFTP settings. Set SFTP_HOST, SFTP_USERNAME, and SFTP_PASSWORD."
        )
    return SFTPConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        remote_dir=remote_dir,
        local_dir=Path("/tmp"),  # Lambda path always stages downloads here
    )


def _build_s3_key(filename: str, date_str: str, prefix_base: str = DEFAULT_S3_PREFIX_BASE) -> str:
    prefix_base = prefix_base.strip("/ ")
    return f"{prefix_base}/{date_str}/meter_data/{filename}"


def _resolve_meter_date_str(filename: str, fallback_dt: datetime | None = None) -> str:
    """Use the date encoded in the meter filename when possible.

    This keeps files in the correct S3 day-folder even if the fetcher
    runs a little after midnight or the source file was delayed.
    """
    match = _DATE_RE.search(filename or "")
    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return (fallback_dt or datetime.now(IST)).strftime("%Y-%m-%d")


def _build_history_key(filename: str, date_str: str, run_label: str,
                       prefix_base: str = DEFAULT_S3_PREFIX_BASE) -> str:
    prefix_base = prefix_base.strip("/ ")
    return f"{prefix_base}/{date_str}/{run_label}/{filename}"


def lambda_handler(event, context):
    env_file = _read_env_file()

    def pick(name: str, default: str = "") -> str:
        return _read_env(name, env_file.get(name, default))

    bucket = pick("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET environment variable is required.")

    s3_prefix_base = pick("S3_PREFIX_BASE", DEFAULT_S3_PREFIX_BASE)
    config = _build_sftp_config()

    local_path, remote_path, mtime = download_latest_file(config, local_dir=Path("/tmp"))

    filename = local_path.name
    date_str = _resolve_meter_date_str(filename, fallback_dt=mtime)
    s3_key = _build_s3_key(filename, date_str, prefix_base=s3_prefix_base)

    s3 = boto3.client("s3")
    s3.upload_file(str(local_path), bucket, s3_key)

    result = {
        "status": "ok",
        "remote_file": remote_path,
        "remote_mtime": mtime,
        "local_path": str(local_path),
        "bucket": bucket,
        "s3_key": s3_key,
    }
    print(result)
    return result


def main():
    lambda_handler({}, None)


if __name__ == "__main__":
    main()
