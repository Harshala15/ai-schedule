"""Small S3 helper layer used by the Kasipet forecast scheduler."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import boto3


@dataclass(frozen=True)
class S3ObjectRef:
    key: str
    last_modified: object
    size: int


def s3_client():
    return boto3.client("s3")


def list_objects(bucket: str, prefix: str) -> list[S3ObjectRef]:
    client = s3_client()
    paginator = client.get_paginator("list_objects_v2")
    items: list[S3ObjectRef] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            items.append(
                S3ObjectRef(
                    key=obj["Key"],
                    last_modified=obj.get("LastModified"),
                    size=int(obj.get("Size", 0)),
                )
            )
    return items


def download_file(bucket: str, key: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    s3_client().download_file(bucket, key, str(destination))
    return destination


def upload_file(bucket: str, key: str, source: Path, content_type: str | None = None) -> str:
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    if extra_args:
        s3_client().upload_file(str(source), bucket, key, ExtraArgs=extra_args)
    else:
        s3_client().upload_file(str(source), bucket, key)
    return key


def upload_json(bucket: str, key: str, payload: dict) -> str:
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    s3_client().put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return key
