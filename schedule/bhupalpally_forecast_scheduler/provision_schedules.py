"""Create or update the daily EventBridge Scheduler rules for Bhupalpally."""

from __future__ import annotations

import json
import os

import boto3
from botocore.exceptions import ClientError

from bhupalpally_forecast_scheduler import settings


CAPTURE_TIMES = ("05:15", "06:45", "08:15", "09:45", "11:15", "12:45", "14:15", "15:45")


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is required.")
    return value


def _schedule_name(time_str: str) -> str:
    return f"bhupalpally-forecast-{time_str.replace(':', '')}"


def _cron_expression(time_str: str) -> str:
    hour, minute = time_str.split(":")
    return f"cron({int(minute)} {int(hour)} * * ? *)"


def upsert_schedules() -> list[dict]:
    lambda_arn = _require_env("BHUPALPALLY_FORECAST_LAMBDA_ARN")
    role_arn = _require_env("BHUPALPALLY_EVENTBRIDGE_INVOKE_ROLE_ARN")
    region = os.getenv("AWS_REGION", "ap-south-1").strip() or "ap-south-1"
    client = boto3.client("scheduler", region_name=region)

    results = []
    for time_str in CAPTURE_TIMES:
        payload = json.dumps({"target_time": time_str})
        name = _schedule_name(time_str)
        kwargs = dict(
            Name=name,
            ScheduleExpression=_cron_expression(time_str),
            ScheduleExpressionTimezone=settings.DEFAULT_TIMEZONE,
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": lambda_arn,
                "RoleArn": role_arn,
                "Input": payload,
            },
            State="ENABLED",
            Description=f"Bhupalpally forecast run for {time_str}",
        )
        try:
            client.create_schedule(**kwargs)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code not in {"ConflictException", "ResourceAlreadyExistsException"}:
                raise
            client.update_schedule(**kwargs)
        results.append({"name": name, "target_time": time_str, "schedule_expression": _cron_expression(time_str)})
    return results


def main():
    results = upsert_schedules()
    for row in results:
        print(row)


if __name__ == "__main__":
    main()
