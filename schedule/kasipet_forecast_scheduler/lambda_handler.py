"""AWS Lambda entry point for the Kasipet forecast scheduler."""

from __future__ import annotations

import os

from kasipet_forecast_scheduler import settings
from simour_forecast_scheduler.service import run_schedule_job


def _pick(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def lambda_handler(event, context):
    bucket = _pick("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET environment variable is required.")

    capture_prefix = _pick("S3_CAPTURE_PREFIX", settings.DEFAULT_S3_CAPTURE_PREFIX)
    meter_prefix = _pick("S3_METER_PREFIX", settings.DEFAULT_S3_METER_PREFIX)
    schedule_prefix = _pick("S3_SCHEDULE_PREFIX", settings.DEFAULT_S3_SCHEDULE_PREFIX)

    result = run_schedule_job(
        bucket=bucket,
        capture_prefix=capture_prefix,
        meter_prefix=meter_prefix,
        schedule_prefix=schedule_prefix,
        event=event or {},
    )
    print(result)
    return result


def main():
    lambda_handler({}, None)


if __name__ == "__main__":
    main()
