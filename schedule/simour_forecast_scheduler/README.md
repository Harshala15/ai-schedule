# SIMOUR Forecast Scheduler

This package is the dedicated scheduler flow for the SIRMOUR forecast pipeline.

## What it does

At each scheduled time, the Lambda:

1. Loads the latest Windy screenshots/video available at or before that time.
2. Loads the meter CSV for that day, but only uses readings up to the same cutoff time.
3. Runs the existing forecasting pipeline.
4. Merges the new forecast with the latest daily schedule snapshot.
5. Writes a new timestamped schedule file plus a `latest` file under:

```text
generated/SIRMOUR/YYYY-MM-DD/
```

## Expected S3 inputs

Capture objects:

```text
sirmour/YYYY-MM-DD/...
```

Meter data:

```text
raw/vedanjay/SIRMOUR/YYYY-MM-DD/meter_data/...
```

## Output S3 keys

Each run produces:

```text
generated/SIRMOUR/YYYY-MM-DD/YYYY-MM-DD_HH-MM_schedule.csv
generated/SIRMOUR/YYYY-MM-DD/YYYY-MM-DD_HH-MM_metadata.json
generated/SIRMOUR/YYYY-MM-DD/YYYY-MM-DD_latest_schedule.csv
generated/SIRMOUR/YYYY-MM-DD/YYYY-MM-DD_latest_metadata.json
```

## Lambda environment variables

```text
S3_BUCKET
S3_CAPTURE_PREFIX   (optional, default: raw/vedanjay/SIRMOUR)
S3_METER_PREFIX     (optional, default: raw/vedanjay/SIRMOUR)
S3_SCHEDULE_PREFIX  (optional, default: generated/SIRMOUR)
S3_STATE_PREFIX     (optional, default: state/vedanjay/SIRMOUR)
ENABLE_S3_STATE_SYNC=1   (turns on persistent state mirroring)
SIMOUR_STORAGE_ROOT (recommended: /tmp/simour on Lambda)
```

## Local invocation

```bash
python -m simour_forecast_scheduler.handler
```

You can also invoke the handler with an event payload such as:

```json
{
  "target_date": "2026-08-06",
  "target_time": "08:15"
}
```

## Create EventBridge schedules automatically

This package also includes an idempotent provisioning script that creates or updates the daily EventBridge Scheduler rules for:

- `05:15`
- `06:45`
- `08:15`
- `09:45`
- `11:15`
- `12:45`
- `14:15`
- `15:45`

Set these environment variables first:

```text
SIMOUR_FORECAST_LAMBDA_ARN=arn:aws:lambda:ap-south-1:429694361053:function:simour-forecast-scheduler
SIMOUR_EVENTBRIDGE_INVOKE_ROLE_ARN=arn:aws:iam::429694361053:role/simour-eventbridge-scheduler-role
AWS_REGION=ap-south-1
```

Then run:

```bash
python -m simour_forecast_scheduler.provision
```

The script is safe to rerun. If a schedule already exists, it updates the same schedule instead of failing.

## Docker build

Build the Lambda container image from the repo root:

```bash
docker build -t simour-forecast-scheduler -f simour_forecast_scheduler/Dockerfile .
```

Push it to ECR, then create the Lambda function from that image. The Dockerfile keeps the scheduler separate from `simour_fetcher/`; it copies only the forecasting modules it needs.
