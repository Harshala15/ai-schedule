# Intellis GTI Forecasting & Commercial API Package

A unified, production-grade 96-block Global Tilted Irradiance (GTI) solar forecasting engine and REST API service.

---

## 1. Directory Structure

```
modules/weather/strategies/intellis_gti/
│
├── __init__.py                     # Unified Factory: get_gti_strategy(...)
├── base_gti_strategy.py            # Astronomical geometry & GTIForecastResult dataclass
├── meter_gti_strategy.py           # 143-NWP ensemble + ground telemetry calibration (Meter sites)
├── non_meter_gti_strategy.py       # Satellite-observed POA irradiance & Kt modeling (Non-meter sites)
├── remote_api_strategy.py          # Remote client caller with automatic seamless local fallback
│
├── api/                            # REST API Microservice (FastAPI + Pydantic)
│   ├── __init__.py                 # API exports
│   ├── app.py                      # FastAPI application & route endpoints
│   ├── schemas.py                  # Standardized commercial JSON response models
│   ├── auth.py                     # API key authentication & client validation
│   ├── lambda_handler.py           # Mangum adapter for serverless AWS Lambda
│   └── client_demo.py              # Developer test client & demonstration script
│
├── deployment/                     # Cloud Deployment & Packaging
│   ├── Dockerfile                  # Linux/amd64 container definition for AWS Lambda
│   └── deploy_aws.py               # Automated ECR build & API Gateway deployment script
│
└── README.md                       # Complete package documentation
```

---

## 2. Calculation Strategies

The package provides three primary strategy implementations deriving from `BaseGTIStrategy`:

### A. `MeterGTIStrategy` (For Metered SCADA Sites)
- Downloads up to 143 numerical weather prediction (NWP) ensemble members from ECMWF IFS, DWD ICON, NCEP GEFS, and GEM Global via Open-Meteo Customer Premium endpoints.
- Evaluates past 5–7 days of physical pyranometer telemetry from S3 bucket `vedanjay-schedules-test`.
- Ranks models by RMSE, bias, and correlation independently across 3 diurnal regimes:
  - Morning (06:00 – 10:00)
  - Midday (10:00 – 14:00)
  - Afternoon (14:00 – 18:45)
- Blends the winning models with smooth cosine spline transitions and physical cloud optical ceiling capping.

### B. `NonMeterGTIStrategy` (For Non-Meter & Virtual Sites)
- Operates on sites without on-site telemetry (e.g., UPL, EMIL, SAWDA, BALAKWADA).
- Queries satellite-observed solar irradiance and cloud optical depth.
- Interpolates into 96 15-minute time steps.
- Applies calibrated performance ratios and astronomical Plane-of-Array clear-sky benchmarks.

### C. `RemoteAPIGTIStrategy` (Cloud REST API Caller)
- Connects directly to the deployed AWS API Gateway endpoint:
  `https://cbe0jmvos5.execute-api.ap-south-1.amazonaws.com/v1/intellis_gti_regime?plant={plant}&date={date}`
- Retrieves pre-computed 96-block irradiance data in ~4 seconds.
- **Fail-Safe Mechanism**: If an HTTP timeout, status 5xx, or network failure occurs, it automatically routes computation to the local strategy without interrupting the scheduling pipeline.

---

## 3. Factory Usage in Python

```python
from modules.weather.strategies.intellis_gti import get_gti_strategy

# 1. Standard Local Strategy (auto-detects Meter vs Non-Meter from profile)
strategy = get_gti_strategy("GSNP")
result = strategy.compute_gti("2026-10-09")
print(result.strategy_name)  # METER_NWP_ENSEMBLE_GTI

# 2. Remote API Strategy with Automatic Local Fallback
api_strategy = get_gti_strategy("GSNP", use_api=True)
result = api_strategy.compute_gti("2026-10-09")
print(result.strategy_name)  # REMOTE_INTELLIS_GTI_API
```

---

## 4. REST API Service (`api/`)

The REST API exposes the GTI forecasting capabilities over standard HTTP endpoints:

### Endpoints
* **Base URL**: `https://cbe0jmvos5.execute-api.ap-south-1.amazonaws.com`
* **Swagger Documentation**: `/docs`
* **Health Check**: `GET /v1/health`
* **GTI Forecast**: `GET /v1/intellis_gti_regime?plant={plant}&date={date}`
* **GTI Alias**: `GET /v1/gti?plant={plant}&date={date}`

### Run the API Locally
From the `schedule/` directory:
```bash
uvicorn modules.weather.strategies.intellis_gti.api.app:app --host 0.0.0.0 --port 8000 --reload
```

---

## 5. AWS Deployment (`deployment/`)

To build and deploy the container image to Amazon ECR and AWS Lambda:
```bash
python modules/weather/strategies/intellis_gti/deployment/deploy_aws.py
```
This script handles ECR authentication, Docker build, image push, Lambda function code update, and HTTP API Gateway linkage.
