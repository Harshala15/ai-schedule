"""Import a Kasipet historical ZIP bundle into the S3 layout used by the repo.

This is meant for seeding Kasipet history from a local archive such as
`KASIPET.zip`. Each CSV inside the archive is uploaded to:

    raw/vedanjay/KASIPET/YYYY-MM-DD/meter_data/<filename>

The importer does not upload the ZIP itself. It expands the archive in
memory and pushes only the contained meter CSVs into the plant-specific
S3 layout expected by the scheduler and feedback pipeline.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import boto3


DEFAULT_S3_PREFIX_BASE = "raw/vedanjay/KASIPET"
DATE_RE_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})")
DATE_RE_COMPACT = re.compile(r"(\d{8})")


@dataclass(frozen=True)
class ImportedObject:
    archive_member: str
    s3_key: str


def _read_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _normalize_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _date_from_member_name(member_name: str) -> str | None:
    path = Path(member_name)
    for part in path.parts:
        if match := DATE_RE_ISO.search(part):
            return _normalize_date(match.group(1))
        if match := DATE_RE_COMPACT.search(part):
            return _normalize_date(match.group(1))

    stem = path.stem
    if match := DATE_RE_ISO.search(stem):
        return _normalize_date(match.group(1))
    if match := DATE_RE_COMPACT.search(stem):
        return _normalize_date(match.group(1))
    return None


def _build_s3_key(prefix_base: str, date_str: str, filename: str) -> str:
    prefix_base = prefix_base.strip("/ ")
    return f"{prefix_base}/{date_str}/meter_data/{Path(filename).name}"


def import_kasipet_zip(
    archive_path: Path,
    bucket: str,
    prefix_base: str = DEFAULT_S3_PREFIX_BASE,
    *,
    dry_run: bool = False,
) -> list[ImportedObject]:
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive not found: {archive_path}")
    if not archive_path.is_file():
        raise ValueError(f"Archive path is not a file: {archive_path}")

    imported: list[ImportedObject] = []
    s3 = boto3.client("s3")

    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue

            filename = Path(member.filename).name
            if not filename.lower().endswith(".csv"):
                continue

            date_str = _date_from_member_name(member.filename)
            if not date_str:
                continue

            s3_key = _build_s3_key(prefix_base, date_str, filename)
            imported.append(ImportedObject(archive_member=member.filename, s3_key=s3_key))

            if dry_run:
                continue

            with zf.open(member, "r") as source:
                data = source.read()
            s3.upload_fileobj(io.BytesIO(data), bucket, s3_key)

    return imported


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a Kasipet ZIP archive into S3.")
    parser.add_argument("--archive", required=True, help="Path to the local KASIPET ZIP archive")
    parser.add_argument("--bucket", default=None, help="S3 bucket to upload into (defaults to S3_BUCKET env var)")
    parser.add_argument("--prefix-base", default=None, help=f"S3 prefix base (default: {DEFAULT_S3_PREFIX_BASE})")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be uploaded without uploading")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    archive_path = Path(args.archive)
    bucket = args.bucket or _read_env("S3_BUCKET")
    if not bucket:
        raise SystemExit("S3 bucket not provided. Pass --bucket or set S3_BUCKET.")

    prefix_base = args.prefix_base or _read_env("S3_PREFIX_BASE", DEFAULT_S3_PREFIX_BASE)
    imported = import_kasipet_zip(archive_path, bucket, prefix_base=prefix_base, dry_run=args.dry_run)

    mode = "Would import" if args.dry_run else "Imported"
    print(f"{mode} {len(imported)} CSV file(s) from {archive_path.name}")
    for item in imported[:10]:
        print(f"  {item.archive_member} -> s3://{bucket}/{item.s3_key}")
    if len(imported) > 10:
        print(f"  ... {len(imported) - 10} more")


if __name__ == "__main__":
    main()
