"""Helpers for mirroring persistent forecasting state to and from S3.

This module keeps the long-lived state that the local workflow relies on
in sync with the Lambda workflow:

- historic_cases/
- features_log/
- prediction_context/
- meter_history/
- pvlib_summary/
- plant_performance/
- ecmwf_weather/

The Lambda scheduler can download that state before a forecast run and
upload the updated files after the run completes. Local feedback scripts
can also push the same state when ENABLE_S3_STATE_SYNC is set.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import boto3
from botocore.exceptions import ClientError

import config


DEFAULT_STATE_OWNER = "vedanjay"
DEFAULT_STATE_PREFIX = f"state/{DEFAULT_STATE_OWNER}/{config.PLANT_NAME}"
MERGED_SCADA_FILENAME = f"{config.PLANT_NAME}_merged_scada_data.csv"


@dataclass(frozen=True)
class StateSyncResult:
    downloaded: int = 0
    uploaded: int = 0
    deleted_remote: int = 0


def is_enabled() -> bool:
    return os.getenv("ENABLE_S3_STATE_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_bucket(bucket: str | None = None) -> str:
    if bucket and bucket.strip():
        return bucket.strip()
    return os.getenv("S3_BUCKET", "").strip()


def resolve_prefix(prefix: str | None = None) -> str:
    if prefix and prefix.strip():
        return prefix.strip().strip("/ ")
    env_prefix = os.getenv("S3_STATE_PREFIX", "").strip()
    if env_prefix:
        return env_prefix.strip("/ ")
    return DEFAULT_STATE_PREFIX.strip("/ ")


def managed_state_paths() -> tuple[Path, ...]:
    """Directories that hold persistent state worth mirroring."""
    return (
        config.HISTORIC_CASES_DIR / "merged_scada_data.csv",
        config.FEATURES_LOG_DIR,
        config.PREDICTION_CONTEXT_PATH,
        config.METER_HISTORY_DIR,
        config.FEEDBACK_ANALYSIS_DIR,
        config.PVLIB_SUMMARY_DIR,
        config.PLANT_PERFORMANCE_DIR,
        config.ECMWF_WEATHER_DIR,
    )


def _s3_client():
    return boto3.client("s3")


def _storage_root() -> Path:
    return config.STORAGE_ROOT.resolve()


def _relative_path(path: Path) -> Path | None:
    try:
        return path.resolve().relative_to(_storage_root())
    except ValueError:
        return None


def _iter_local_files(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for candidate in sorted(path.rglob("*")):
            if candidate.is_file():
                files.append(candidate)
    files.sort()
    return files


def _clear_local_paths(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _prefix_for_key(prefix: str, relative_path: Path) -> str:
    return f"{prefix}/{relative_path.as_posix()}"


def _chunked(items: list[dict], size: int = 1000) -> Iterable[list[dict]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _state_relative_path_for_upload(path: Path) -> Path | None:
    relative = _relative_path(path)
    if relative is None:
        return None

    # Store the merged SCADA file with the plant name in the S3 filename.
    if path.name == "merged_scada_data.csv":
        return Path(relative.parent.as_posix()) / MERGED_SCADA_FILENAME

    return relative


def _local_destination_for_download(relative_path: Path) -> Path:
    if relative_path.name == MERGED_SCADA_FILENAME:
        return _storage_root() / relative_path.parent / "merged_scada_data.csv"
    return _storage_root() / relative_path


def download_state(
    bucket: str | None = None,
    prefix: str | None = None,
    managed_paths: Iterable[Path] | None = None,
    *,
    clear_local: bool = True,
) -> StateSyncResult:
    """Download persistent state from S3 into the local storage root."""
    if not is_enabled():
        return StateSyncResult()

    bucket = resolve_bucket(bucket)
    prefix = resolve_prefix(prefix)
    if not bucket or not prefix:
        return StateSyncResult()

    paths = tuple(managed_paths or managed_state_paths())
    if clear_local:
        _clear_local_paths(paths)

    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    downloaded = 0

    allowed_roots = {
        rel.parts[0]
        for rel in (_relative_path(path) for path in paths)
        if rel and rel.parts
    }

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key or key.endswith("/"):
                continue
            if key == prefix:
                continue

            relative = key[len(prefix):].lstrip("/")
            if not relative:
                continue

            relative_path = Path(relative)
            if allowed_roots and relative_path.parts and relative_path.parts[0] not in allowed_roots:
                continue

            destination = _local_destination_for_download(relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(destination))
            downloaded += 1
            if destination.resolve() == config.PREDICTION_CONTEXT_PATH.resolve():
                print(f"  [STATE] Downloaded prediction context from s3://{bucket}/{key} -> {destination}")

    return StateSyncResult(downloaded=downloaded)


def upload_state(
    bucket: str | None = None,
    prefix: str | None = None,
    managed_paths: Iterable[Path] | None = None,
) -> StateSyncResult:
    """Upload local persistent state to S3 and remove stale remote files."""
    if not is_enabled():
        return StateSyncResult()

    bucket = resolve_bucket(bucket)
    prefix = resolve_prefix(prefix)
    if not bucket or not prefix:
        return StateSyncResult()

    client = _s3_client()
    paths = tuple(managed_paths or managed_state_paths())
    local_files = _iter_local_files(paths)

    if not local_files:
        return StateSyncResult()

    local_keys: set[str] = set()
    uploaded = 0
    for file_path in local_files:
        relative = _state_relative_path_for_upload(file_path)
        if relative is None:
            continue
        key = _prefix_for_key(prefix, relative)
        client.upload_file(str(file_path), bucket, key)
        local_keys.add(key)
        uploaded += 1
        if file_path.resolve() == config.PREDICTION_CONTEXT_PATH.resolve():
            print(f"  [STATE] Uploaded prediction context to s3://{bucket}/{key} from {file_path}")

    deleted_remote = 0
    stale_keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key or key.endswith("/"):
                continue
            if key not in local_keys:
                stale_keys.add(key)

    if stale_keys:
        for chunk in _chunked([{"Key": key} for key in sorted(stale_keys)]):
            client.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
            deleted_remote += len(chunk)

    # If bucket versioning is enabled, remove older versions so the
    # cloud snapshot mirrors the latest local 3-day state instead of
    # retaining prior object versions.
    version_entries: list[dict] = []
    try:
        version_resp = client.list_object_versions(Bucket=bucket, Prefix=prefix)
    except ClientError as exc:
        error_code = (exc.response or {}).get("Error", {}).get("Code", "")
        if error_code in {"AccessDenied", "AllAccessDisabled"}:
            print(
                f"  [STATE] Skipping version cleanup for s3://{bucket}/{prefix} "
                f"because the Lambda role lacks list-version permission ({error_code})."
            )
            version_resp = {}
        else:
            raise

    for version in version_resp.get("Versions", []):
        key = version.get("Key", "")
        version_id = version.get("VersionId")
        is_latest = bool(version.get("IsLatest"))
        if not key or not version_id:
            continue
        if key in stale_keys or (key in local_keys and not is_latest):
            version_entries.append({"Key": key, "VersionId": version_id})
    for delete_marker in version_resp.get("DeleteMarkers", []):
        key = delete_marker.get("Key", "")
        version_id = delete_marker.get("VersionId")
        if key and version_id and key in stale_keys:
            version_entries.append({"Key": key, "VersionId": version_id})

    if version_entries:
        try:
            for chunk in _chunked(version_entries):
                client.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
                deleted_remote += len(chunk)
        except ClientError as exc:
            error_code = (exc.response or {}).get("Error", {}).get("Code", "")
            if error_code in {"AccessDenied", "AllAccessDisabled"}:
                print(
                    f"  [STATE] Skipping version deletion for s3://{bucket}/{prefix} "
                    f"because the Lambda role lacks delete-version permission ({error_code})."
                )
            else:
                raise

    return StateSyncResult(downloaded=0, uploaded=uploaded, deleted_remote=deleted_remote)


def refresh_state_from_s3(
    bucket: str | None = None,
    prefix: str | None = None,
    managed_paths: Iterable[Path] | None = None,
) -> StateSyncResult:
    """Convenience wrapper used by Lambda before a forecast run."""
    return download_state(bucket=bucket, prefix=prefix, managed_paths=managed_paths, clear_local=True)


def push_state_to_s3(
    bucket: str | None = None,
    prefix: str | None = None,
    managed_paths: Iterable[Path] | None = None,
) -> StateSyncResult:
    """Convenience wrapper used after local feedback updates or forecast runs."""
    return upload_state(bucket=bucket, prefix=prefix, managed_paths=managed_paths)
