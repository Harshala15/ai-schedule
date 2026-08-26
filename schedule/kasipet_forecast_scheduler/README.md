# Kasipet Forecast Scheduler

Kasipet-specific Lambda scheduler package for the shared SIRMOUR forecasting engine.

## Output layout

```text
generated/KASIPET/YYYY-MM-DD/YYYY-MM-DD_HH-MM_schedule.csv
generated/KASIPET/YYYY-MM-DD/YYYY-MM-DD_HH-MM_metadata.json
generated/KASIPET/YYYY-MM-DD/YYYY-MM-DD_latest_schedule.csv
generated/KASIPET/YYYY-MM-DD/YYYY-MM-DD_latest_metadata.json
```

## Lambda env vars

```text
S3_BUCKET
S3_CAPTURE_PREFIX=raw/vedanjay/KASIPET
S3_METER_PREFIX=raw/vedanjay/KASIPET
S3_SCHEDULE_PREFIX=generated/KASIPET
S3_STATE_PREFIX=state/vedanjay/KASIPET
ENABLE_S3_STATE_SYNC=1
SIMOUR_STORAGE_ROOT=/tmp/kasipet
PLANT_NAME=KASIPET

With state sync enabled, the Kasipet Lambda mirrors:

- `historic_cases/KASIPET/merged_scada_data.csv` locally, uploaded as `historic_cases/KASIPET/KASIPET_merged_scada_data.csv` in S3
- `features_log/`
- `prediction_context/KASIPET_context.json`
HISTORIC_CASES_DIR=/tmp/kasipet/historic_cases/KASIPET
PLANT_LAT=...
PLANT_LON=...
PLANT_CAPACITY_MW=...
PERFORMANCE_RATIO=...
GEMINI_API_KEYS=key1,key2,key3
```

For local testing, copy [.env.example](./.env.example) to `.env` in the same folder and fill in the plant values and Gemini key.

The scheduler image now installs `pvlib` and its scientific Python dependencies through [requirements.txt](requirements.txt), which is required for the Step 1 pvlib summary in `modules/pvlib_utils.py`.

## Provision schedules

Run the provisioning script to create the eight daily EventBridge schedules:

```bash
python -m kasipet_forecast_scheduler.provision_schedules
```
