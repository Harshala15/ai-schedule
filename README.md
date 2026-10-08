# Intellis AI Renewable Forecasting & Dispatch Engine (Production 2.0)

Welcome to the **Intellis AI** renewable energy forecasting and scheduling repository. This system autonomously generates 96-block (15-minute interval) intraday dispatch schedules for **23 utility-scale solar and wind power plants** across India, optimizing for state grid regulatory compliance (MERC, TG SLDC, MP SLDC, and CERC).

For complete documentation, see the official [schedule/README.md](schedule/README.md).

---

## 🧭 Master Project Flow (Read This First)

If you are a new engineer or developer onboarding to this repository, here is how the entire system works from trigger to final schedule:

```
                  EventBridge (Every 15 mins) / CLI run_local.py
                                       │
                                       ▼
                     [Step 1: Lambda & Site Orchestration]
                      File: schedule/intellis_ai_lambda.py
                      - Parses plant name, target date, and current block (1-96).
                      - Acquires S3 lock to prevent duplicate runs.
                      - Pulls latest raw ground SCADA telemetry from S3.
                                       │
                                       ▼
                        [Step 2: Plant Type Dispatcher]
                                       ├── If Wind (JEWLI, CHANDAWASA) ──► Go to Step 3B
                                       └── If Solar (All other 21 plants) ──► Go to Step 3A
                                       │
            ┌──────────────────────────┴──────────────────────────┐
            ▼                                                     ▼
 [Step 3A: Solar Physics Engine]                       [Step 3B: Wind Physics Engine]
 File: schedule/modules/solar_schedule/                File: schedule/modules/weather/strategies/
       solar_scheduler.py                                    intellis_wind/wind_ensemble_strategy.py
 - Queries dedicated GTI weather strategies.           - Blends ECMWF IFS + DWD ICON 100m wind speeds.
 - Computes PVLib Clear-Sky POA envelope.              - Computes air density adjustment at hub height.
 - Calculates ground Clearness Index (k_t).            - Evaluates turbine power curve + wake losses.
            │                                                     │
            └──────────────────────────┬──────────────────────────┘
                                       │
                                       ▼
               [Step 4: Real-Time SCADA Decoupled Handover Bridge]
                - Calculates elapsed blocks from actual meter timestamp.
                - 0 to 30 min: 100% Meter-decay adjusted (nowcasting).
                - 30 to 45 min: 50% Meter + 50% NWP weather blend.
                - > 45 min: 100% Pure NWP forecast (eliminates "zombie clouds").
                                       │
                                       ▼
                     [Step 5: AI Strategic Risk Arbiter]
                      File: schedule/modules/llm/strategic_arbiter.py
                      - Evaluates the next 12 blocks against cloud cover & CAPE index.
                      - Assigns asymmetric regulatory bias (conservative vs. baseline).
                                       │
                                       ▼
                     [Step 6: Four Deterministic Guardrails]
                      - G1: Night Zero Enforcement (0.0 MW before 06:00 & after 18:30).
                      - G2: Clear-Sky Negative Cut Lockout (AI cannot cut if sky is clear).
                      - G3: Overcast Cloud Ceiling (Clamps unrealistic monsoon spikes).
                      - G4: 10% AC Capacity Ramp Limiter (Smooths inter-block steps).
                                       │
                                       ▼
                     [Step 7: Statutory Gate Closure & S3 Publishing]
                      - Merges with previous final schedule respecting gate closure:
                        * 45-min freeze (3 blocks) for TG & MH.
                        * 90-min freeze (6 blocks) for MP & CERC.
                      - Writes outputs to AWS S3 & FTP for SLDC submission.
```

---

## 🎯 Which File Contains Which Logic? (Quick Developer Guide)

New team members should refer to this table to know **where active production logic lives** and what files can be ignored:

| Category | File Path | What Logic Lives Here? |
| :--- | :--- | :--- |
| **System Entrypoint** | [`schedule/intellis_ai_lambda.py`](schedule/intellis_ai_lambda.py) | AWS Lambda entrypoint (`lambda_handler`), S3 idempotency locks, run orchestration. |
| **Local CLI Runner** | [`run_local.py`](run_local.py) & [`schedule/run_local.py`](schedule/run_local.py) | Unified command-line interface to run any plant locally without AWS or cloud dependencies. |
| **Solar Engine** | [`schedule/modules/solar_schedule/solar_scheduler.py`](schedule/modules/solar_schedule/solar_scheduler.py) | **Primary Solar Forecast Engine**: PVLib clear sky, NWP blending, SCADA handover, guardrails. |
| **Wind Engine** | [`schedule/modules/weather/strategies/intellis_wind/wind_ensemble_strategy.py`](schedule/modules/weather/strategies/intellis_wind/wind_ensemble_strategy.py) | **Primary Wind Forecast Engine**: 100m hub wind, air density correction, power curve mapping. |
| **AI Risk Arbiter** | [`schedule/modules/llm/strategic_arbiter.py`](schedule/modules/llm/strategic_arbiter.py) | **LLM Strategic Agent**: Analyzes atmospheric risk, satellite cover, and regulatory bias. |
| **LLM Transport** | [`schedule/modules/llm/predictor.py`](schedule/modules/llm/predictor.py) | OpenRouter API client, model fallback chain, retry handler. |
| **Plant Profiles** | [`schedule/plant_profiles/`](schedule/plant_profiles/) | Master JSON configs for each of the 23 plants (capacities, coordinates, tilt, azimuth, SLDC rules). |
| **Global Config** | [`schedule/config.py`](schedule/config.py) | Fallback plant definitions, API credentials, and regulatory constants. |
| **Deployer Script** | [`schedule/build_and_deploy_all_lambdas.py`](schedule/build_and_deploy_all_lambdas.py) | Automated script to build AMD64 Docker image, push to ECR, and update 23 Lambdas. |
| **Offline Sandbox** | [`schedule/scratch/`](schedule/scratch/) | **DO NOT TOUCH FOR PRODUCTION**: Isolated local scratchpad for backtests and research. |

---

## 🚀 Quickstart: Running a Schedule Locally

You do not need AWS Lambda to run or test forecasts. Use the unified local runner:

```bash
# 1. Run GSNP Solar Plant for 10:00 AM:
python run_local.py --plant GSNP --time 10:00

# 2. Run Kothagudem Solar Plant (37 MW) for a specific date:
python run_local.py --plant KOTHAGUDEM --date 2026-09-26 --time 14:15

# 3. Run Jewli Wind Farm (100.8 MW):
python run_local.py --plant JEWLI --time 11:30

# 4. Save to custom output CSV:
python run_local.py --plant SIRMOUR --time 09:45 --output my_test_schedule.csv
```

---

## 🚢 Building & Deploying to AWS Lambda

Deploying updates to all 23 production Lambdas is completely automated:

```bash
# Must have AWS CLI configured with profile 'intellis-608'
python schedule/build_and_deploy_all_lambdas.py
```
This builds the Docker image with `--platform linux/amd64`, pushes it to Amazon ECR (`intellis-ai-scheduler:intellis-ai-20260926-v1`), and sequentially updates and verifies all 23 Lambda functions.
