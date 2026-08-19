"""
AWS Lambda entry point for the Kasipet FTP fetcher.

Uploads the newest file from the Kasipet FTP source into:
    raw/vedanjay/KASIPET/YYYY-MM-DD/meter_data/<filename>
"""

from __future__ import annotations

import re
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

from kasipet_fetcher.fetch_latest_sftp import FTPConfig, download_latest_file


DEFAULT_S3_PREFIX_BASE = "raw/vedanjay/KASIPET"
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


def _build_sftp_config() -> FTPConfig:
    env_file = _read_env_file()

    def pick(name: str, default: str = "") -> str:
        return _read_env(name, env_file.get(name, default))

    host = pick("SFTP_HOST")
    port = int(pick("SFTP_PORT", "22"))
    username = pick("SFTP_USERNAME")
    password = pick("SFTP_PASSWORD")
    remote_dir = pick("SFTP_REMOTE_DIR", "/incoming/powerdata_realtime/")
    if not host or not username or not password:
        raise RuntimeError("Missing SFTP settings. Set SFTP_HOST, SFTP_USERNAME, and SFTP_PASSWORD.")
    return FTPConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        remote_dir=remote_dir,
        filename_prefix=pick("SFTP_FILENAME_PREFIX", "kasipet_"),
        local_dir=Path("/tmp"),
    )


def _build_s3_key(filename: str, date_str: str, prefix_base: str = DEFAULT_S3_PREFIX_BASE) -> str:
    prefix_base = prefix_base.strip("/ ")
    return f"{prefix_base}/{date_str}/meter_data/{filename}"


def _resolve_meter_date_str(filename: str, fallback_dt: datetime | None = None) -> str:
    """Use the date encoded in the meter filename when possible."""
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
