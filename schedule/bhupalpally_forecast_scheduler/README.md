# Bhupalpally Forecast Scheduler

Bhupalpally-specific Lambda scheduler package for the shared forecasting engine.

## What it does

At each revision time, the Lambda:

1. Loads the latest Windy video available at or before that time.
2. Loads the meter CSV for that day and trims it to readings up to the revision cutoff.
3. Runs the shared forecasting pipeline with a 3-hour horizon from the revision time.
4. Uses the Bhupalpally plant profile JSON to supply capacity, tilt, orientation, and other asset metadata.
5. Produces a Step 1 LLM forecast from meter data up to the revision time.
6. Adjusts Step 1 with Windy video + ECMWF weather as Step 2.
7. Adjusts Step 2 with the context JSON as Step 3.
8. Falls back to the physics anchor only when the LLM is unavailable or exhausted.
9. Merges the new forecast into the rolling latest/current-final schedule snapshots.

## Output layout

```text
generated/BHUPALPALLY/YYYY-MM-DD/YYYY-MM-DD_HH-MM_schedule.csv
generated/BHUPALPALLY/YYYY-MM-DD/YYYY-MM-DD_HH-MM_metadata.json
generated/BHUPALPALLY/YYYY-MM-DD/YYYY-MM-DD_latest_schedule.csv
generated/BHUPALPALLY/YYYY-MM-DD/YYYY-MM-DD_latest_metadata.json
generated/BHUPALPALLY/YYYY-MM-DD/YYYY-MM-DD_current_final_schedule.csv
```

## Plant profile

The Bhupalpally scheduler reads plant metadata from:

```text
plant_profiles/BHUPALPALLY.json
```

This profile is used to populate:

- latitude / longitude
- DC capacity
- maximum feed-in AC cap
- tracker type
- tilt
- orientation
- PPA rate
- EEG ID

## Lambda env vars

```text
S3_BUCKET
S3_CAPTURE_PREFIX=raw/vedanjay/BHUPALPALLY
S3_METER_PREFIX=raw/vedanjay/BHUPALPALLY
S3_SCHEDULE_PREFIX=generated/BHUPALPALLY
S3_STATE_PREFIX=state/vedanjay/BHUPALPALLY
ENABLE_S3_STATE_SYNC=1
SIMOUR_STORAGE_ROOT=/tmp/bhupalpally
PLANT_NAME=BHUPALPALLY
PLANT_PROFILE_PATH=plant_profiles/BHUPALPALLY.json
BHUPALPALLY_FORECAST_HORIZON_HOURS=3
```

## Local invocation

```bash
python -m bhupalpally_forecast_scheduler.lambda_handler
```

## Provision schedules

Create or update the eight daily EventBridge schedules with:

```bash
python -m bhupalpally_forecast_scheduler.provision_schedules
```
