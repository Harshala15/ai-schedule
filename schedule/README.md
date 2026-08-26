# Windy Solar Forecast Pipeline

Automated short-term (next 2 hours, 15-minute blocks) solar generation forecasting for a solar plant, built from live Windy.com weather imagery instead of a paid weather API.

A headless browser scrapes several Windy map layers (satellite clouds, cloud cover, rain, solar irradiance, wind) around the plant's coordinates, turns them into numeric features with classical computer vision (color/brightness stats + optical flow), grounds a forecast in a deterministic physics formula, then asks an LLM to adjust that forecast using the most similar historical situations on record. A validator keeps the LLM's adjustment physically sane before anything is saved.

## Why this design

Asking an LLM to "look at a weather map and predict megawatts" is unreliable and non-deterministic. This pipeline instead gives the LLM a narrow, constrained job:

1. **Physics anchor** (`modules/physics/physics_anchor.py`) computes a baseline MW estimate from solar elevation and cloud attenuation — pure math, always available, never wildly wrong.
2. **Case-based retrieval** (`modules/retrieval/similarity_retrieval.py`) finds the most similar past situations (by weighted feature distance) that already have a real SCADA outcome.
3. **The LLM** (`modules/llm/predictor.py`) only *adjusts* the anchor using that retrieved evidence and explains why — it never invents a number from scratch, and one LLM call covers all 8 blocks at once.
4. **The validator** (`validator.py`) clips the result to plant capacity, caps how far the LLM may deviate from the anchor, and smooths unrealistic block-to-block jumps.

If the LLM is unavailable, missing an API key, or returns something unparseable, the pipeline automatically falls back to the physics anchor for every block — it never produces no output.

## Pipeline architecture

```
Windy screenshots (5 layers)  ---->  modules/opencv/image_feature_extraction.py  --\
                                                                                     >-- modules/features/feature_builder.py -> modules/physics/physics_anchor.py
Windy satellite animation      ---->  modules/opencv/video_motion_features.py    --/                                |
(optical flow)                                                                                        v
                                                                                    modules/retrieval/similarity_retrieval.py
                                                                             (top-K similar past cases, from
                                                                              features_log.csv case store)
                                                                                                        |
                                                                                                        v
                                                                                            modules/llm/predictor.py
                                                                                (Gemini adjusts the anchor
                                                                                 using retrieved evidence)
                                                                                                        |
                                                                                                        v
                                                                                             validator.py
                                                                             (range clip / deviation limit /
                                                                                    smoothness check)
                                                                                                        |
                                                                                                        v
                                                                                       modules/storage/prediction_store.py
                                                                              (saves predictions + updates
                                                                               the features_log case store)
```

Orchestrated end-to-end by [run_pipeline.py](run_pipeline.py), triggered every run by [test_multi_image.py](test_multi_image.py).

## Modules

| File | Role |
|---|---|
| [test_multi_image.py](test_multi_image.py) | Entry point. Drives Playwright to log into Windy Premium, capture 5 map layers as screenshots, record + trim a satellite animation, then calls the prediction pipeline. Loops forever on an interval. |
| [config.py](config.py) | Single source of truth: plant details (name/lat/lon/capacity/performance ratio), Windy capture settings, forecast block settings, file paths, and CBR retrieval weights. |
| [run_pipeline.py](run_pipeline.py) | Orchestrates one end-to-end prediction run (the 5 phases in the diagram above). |
| [modules/opencv/image_feature_extraction.py](modules/opencv/image_feature_extraction.py) | Computes brightness/saturation/hue/bright-pixel-% stats over a plant-centered region of interest in each layer screenshot. |
| [modules/opencv/video_motion_features.py](modules/opencv/video_motion_features.py) | Runs Farneback optical flow on the recorded satellite animation to get cloud motion direction, a relative motion score, directional consistency, and cloud-coverage trend. |
| [modules/weather/time_features.py](modules/weather/time_features.py) | Computes solar elevation (Cooper's equation) and calendar features for a timestamp; also generates the 8 upcoming 15-minute forecast-block timestamps. |
| [modules/features/feature_builder.py](modules/features/feature_builder.py) | Merges image, motion, and time features into one flat row per forecast block; encodes categorical values numerically. |
| [modules/physics/physics_anchor.py](modules/physics/physics_anchor.py) | Deterministic clear-sky × cloud-attenuation × capacity × performance-ratio formula — the baseline MW estimate, no ML or LLM involved. |
| [modules/retrieval/similarity_retrieval.py](modules/retrieval/similarity_retrieval.py) | Case-based reasoning: finds the top-K nearest past feature rows (weighted, z-score-normalized Euclidean distance) that have a matched SCADA actual, and formats them as evidence text. |
| [modules/llm/predictor.py](modules/llm/predictor.py) | The only module that calls an LLM (Google Gemini). Builds the prompt, parses the JSON response, and falls back to the anchor per-block on any failure. |
| [validator.py](validator.py) | Safety net: range clip, max-deviation-from-anchor limit, and block-to-block smoothness cap. |
| [modules/storage/prediction_store.py](modules/storage/prediction_store.py) | Writes/updates the two output CSVs (predictions + feature case store), keyed by timestamp so reruns update rather than duplicate rows. |
| [modules/feedback/daily_feedback.py](modules/feedback/daily_feedback.py) | Run manually once real SCADA/meter data is available: joins actuals into the case store by timestamp and logs MAE/RMSE/MAPE/Bias. Also auto-syncs any CSV dropped into `historic_cases/` before every pipeline run. |
| [accuracy_tracker.py](accuracy_tracker.py) | Standalone script comparing a predictions CSV against an actual-meter CSV and writing a plain-text accuracy report; flags when MAPE exceeds a retrain threshold. |
| [simour_forecast_scheduler/](simour_forecast_scheduler/) | Separate Lambda-oriented scheduler package. At each scheduled time, it loads the latest S3 screenshots/video and meter CSV up to that cutoff, runs the pipeline, and writes timestamped daily schedule snapshots under `generated/{PLANT}/{YYYY-MM-DD}/`. |

## Code Layout

The reusable logic now lives under `modules/` so it is easy to find by domain:

| Folder | What belongs here |
|---|---|
| `modules/llm/` | Gemini / LLM prompt building, parsing, and stepwise forecast adjustment. |
| `modules/weather/` | Weather helpers, ECMWF/Open-Meteo fetch and summary code, and time/solar feature helpers. |
| `modules/opencv/` | OpenCV-based image and video feature extraction. |
| `modules/features/` | Feature assembly and encoding logic that combines weather, image, video, and time features. |
| `modules/physics/` | Deterministic physics anchor logic. |
| `modules/retrieval/` | Similarity search / case-based reasoning over historical feature rows. |
| `modules/feedback/` | Daily feedback, actuals matching, context building, and accuracy logging. |
| `modules/storage/` | Prediction CSV storage and persistent state sync helpers. |

The old top-level files remain as thin compatibility wrappers so existing scripts and Lambda entrypoints continue to work while the real code stays in the module folders.

## Setup

**Requirements:** Python 3.11+, [Playwright](https://playwright.dev/python/), OpenCV, NumPy, the [`google-genai`](https://pypi.org/project/google-genai/) SDK, and (optional but recommended) [ffmpeg](https://ffmpeg.org/) on your `PATH` for trimming the recorded video.

```bash
pip install playwright opencv-python numpy google-genai
playwright install chromium
```

1. Update the plant details in [config.py](config.py) — `PLANT_NAME`, `PLANT_LAT`, `PLANT_LON`, `PLANT_CAPACITY_MW`, `PERFORMANCE_RATIO`.
2. Create a `.env` file in the project root with your Gemini API key:
   ```
   GEMINI_API_KEYS=key1,key2,key3
   # or use numbered fallbacks:
   # GEMINI_API_KEY_1=key1
   # GEMINI_API_KEY_2=key2
   ```
   (Without this, the pipeline still runs — every block simply falls back to the physics anchor with "Low" confidence.)
3. You need a **Windy Premium** account (the animated satellite nowcast layer requires it).

## Running

```bash
python test_multi_image.py
```

On the very first run, a visible browser window opens so you can log in to Windy — your session is then saved to `windy_login.json` and reused for all future (headless) runs. After that, the script loops forever: capture screenshots → record animation → run the prediction pipeline → wait `RUN_INTERVAL_SECONDS` (default 20 min) → repeat.

Each run prints its progress (physics anchors per block, retrieved similar cases, LLM-adjusted values, any validator corrections) and writes:

- `energy_predictions/<PLANT>_energy_generation.csv` — human-facing output: Block, Time, Predicted Generation (MW/kW).
- `features_log/<PLANT>_features_log.csv` — every engineered feature per block plus the prediction. This is the case store that `modules/retrieval/similarity_retrieval.py` searches, and that `modules/feedback/daily_feedback.py` enriches with real outcomes.
- `windy_screenshots/<lat>_<lon>/<timestamp>/` — the 5 raw layer screenshots for that run (for debugging).
- `windy_videos/` — the raw and ffmpeg-trimmed satellite animation clips.

### Closing the feedback loop

Drop any SCADA/meter export CSV into `historic_cases/` (columns matching `TIMESTAMP_COLUMN` / `POWER_COLUMN_MW` in [modules/feedback/daily_feedback.py](modules/feedback/daily_feedback.py), defaulting to `TimeStamp` / `Active Power (MW)`). It's automatically joined into the case store — by matching timestamp only — at the start of every pipeline run, so future forecasts can cite real outcomes ("in similar cloud conditions, actual generation was X% lower than the anchor formula") without a manual step.

To make AWS Lambda use the same persistent history as your local runs, enable `ENABLE_S3_STATE_SYNC=1`. The scheduler will mirror these folders under `S3_STATE_PREFIX`:

- `historic_cases/`
- `features_log/`
- `prediction_context/<PLANT>_context.json`

To also get an error-metrics report and update the running accuracy log, run it directly:

```bash
python modules/feedback/daily_feedback.py path/to/actual_meter.csv
```

## Configuration knobs worth knowing

- `CBR_TOP_K` / `CBR_FEATURE_WEIGHTS` in [config.py](config.py) — how many similar cases are retrieved and how much each feature counts toward "similarity."
- `MAX_DEVIATION_FRACTION` / `MAX_STEP_CHANGE_MW` in [validator.py](validator.py) — how far the LLM is allowed to move the forecast away from the physics anchor.
- `NUM_FORECAST_BLOCKS` / `BLOCK_MINUTES` / `RUN_INTERVAL_SECONDS` in [config.py](config.py) — forecast horizon and how often the pipeline runs.
- `LAYERS` in [config.py](config.py) — which Windy map layers get captured and fed into feature extraction.
- `S3_STATE_PREFIX` / `ENABLE_S3_STATE_SYNC` — S3 mirror location for persistent state when running the Lambda scheduler.

## Notes

- `.venv/requirements.txt` in this repo is a stale, unrelated dependency list left over from the virtual environment's origin — it does not reflect what this project actually imports. Use the `pip install` command above instead.
- Solar elevation uses a simplified formula (assumes local clock time ≈ solar time, no timezone/equation-of-time correction) — accurate enough to distinguish day/night/near-horizon, not astronomically precise.
- The recorded animation's playback speed is a Windy UI artifact, not real time — motion features are intentionally relative/dimensionless (not km/h) for this reason.

## Scheduler package

The new [simour_forecast_scheduler/](simour_forecast_scheduler/) package is meant for the Lambda-driven day schedule flow you described:

- EventBridge triggers the Lambda at `05:15`, `06:45`, `08:15`, `09:45`, `11:15`, `12:45`, `14:15`, and `15:45`.
- The Lambda selects the latest capture bundle available up to that time.
- It uses meter data only up to the same cutoff.
- It reads raw inputs from `raw/vedanjay/SIRMOUR/{YYYY-MM-DD}/windy/` and `raw/vedanjay/SIRMOUR/{YYYY-MM-DD}/meter_data/`.
- It writes a timestamped schedule snapshot plus a `latest` file under `generated/SIRMOUR/{YYYY-MM-DD}/`.
- The package also includes `python -m simour_forecast_scheduler.provision`, which creates or updates all eight EventBridge schedules automatically so you do not need to run the AWS Scheduler CLI by hand.

## Kasipet deployment

Kasipet now has matching deployment packages:

- [kasipet_fetcher/](kasipet_fetcher/) for the Kasipet SFTP fetch Lambda
- [kasipet_forecast_scheduler/](kasipet_forecast_scheduler/) for the Kasipet forecast scheduler Lambda

The shared forecasting engine in [config.py](config.py) is now plant-configurable through environment variables such as:

```text
PLANT_NAME
PLANT_LAT
PLANT_LON
PLANT_CAPACITY_MW
PERFORMANCE_RATIO
```

That means SIMOUR and Kasipet can use the same codebase but separate Lambda deployments, S3 prefixes, and EventBridge schedules.
