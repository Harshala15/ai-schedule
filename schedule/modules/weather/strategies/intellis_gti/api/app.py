"""Intellis GTI Commercial REST API.

FastAPI application serving 96-block Global Tilted Irradiance (GTI) solar predictions
powered by Open-Meteo Customer Premium Commercial Tier.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Ensure schedule directory is on python path
_SCHEDULE_DIR = Path(__file__).resolve().parents[5]
if str(_SCHEDULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SCHEDULE_DIR))

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
import numpy as np

from modules.plant.plant_profile import PlantProfile, load_plant_profile
from modules.weather.strategies.intellis_gti import get_gti_strategy, BaseGTIStrategy
from .schemas import GTIBlock, GTIMetadata, GTIResponse, HealthResponse

DEFAULT_PREMIUM_KEY = "jbThkFlLZSXZE3CU"


def _resolve_api_key(query_key: Optional[str] = None) -> str:
    """Resolve Open-Meteo API key: query param -> env var -> config -> default premium key."""
    if query_key and query_key.strip():
        return query_key.strip()
    env_key = os.getenv("OPENMETEO_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        import config
        cfg_key = getattr(config, "OPENMETEO_API_KEY", "").strip()
        if cfg_key:
            return cfg_key
    except Exception:
        pass
    return DEFAULT_PREMIUM_KEY


app = FastAPI(
    title="Intellis GTI Commercial Engine",
    description=(
        "Production-grade 96-block Global Tilted Irradiance (GTI) solar forecasting engine. "
        "Powered by multi-agency ensemble blending and astronomical solar geometry "
        "using Open-Meteo Customer Premium Commercial Tier."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for web and client frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/v1/health", response_model=HealthResponse, tags=["Monitoring"])
def health_check() -> HealthResponse:
    """Service health and plan tier verification probe."""
    active_key = _resolve_api_key()
    plan_tier = (
        f"Open-Meteo Customer Premium Tier (Active: {active_key[:4]}...{active_key[-3:]})"
        if active_key
        else "Open-Meteo Free Public Tier"
    )
    return HealthResponse(openmeteo_plan=plan_tier)


@app.get(
    "/v1/intellis_gti_regime",
    response_model=GTIResponse,
    tags=["Solar GTI Forecast"],
    summary="Generate 96-Block GTI Solar Forecast (Intellis GTI Regime)",
)
@app.get(
    "/v1/gti",
    response_model=GTIResponse,
    tags=["Solar GTI Forecast"],
    summary="Generate 96-Block GTI Solar Forecast (Alias)",
)
def get_gti_forecast(
    plant: Optional[str] = Query(
        None,
        description="Registered plant identifier (e.g., GSNP, REWASPRNG, ANJANGOAN, KASIPET, SIRMOUR)",
    ),
    latitude: Optional[float] = Query(
        None,
        ge=-90.0,
        le=90.0,
        description="Site latitude in decimal degrees (for custom sites)",
    ),
    longitude: Optional[float] = Query(
        None,
        ge=-180.0,
        le=180.0,
        description="Site longitude in decimal degrees (for custom sites)",
    ),
    tilt: Optional[float] = Query(
        None,
        ge=0.0,
        le=90.0,
        description="Solar panel tilt angle in degrees (default: 15.0)",
    ),
    azimuth: Optional[float] = Query(
        None,
        ge=-180.0,
        le=360.0,
        description="Solar panel azimuth in degrees from North (default: 180.0 South)",
    ),
    date: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Target forecast date in YYYY-MM-DD format (defaults to current IST date)",
    ),
    api_key: Optional[str] = Query(
        None,
        description="Optional API key (currently not required, open access)",
    ),
) -> GTIResponse:
    """Computes high-accuracy 96-block GTI values across 15-minute time intervals."""
    # 1. Resolve target date
    if not date:
        date = dt.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

    # 2. Resolve plant profile or custom site coordinates
    if plant:
        try:
            profile = load_plant_profile(plant.strip().upper())
        except Exception as err:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Plant '{plant}' not found in registered plant profiles: {err}",
            )
        plant_id = profile.plant_name
        lat_val = profile.latitude
        lon_val = profile.longitude
        tilt_val = profile.tilt_deg
        azimuth_val = profile.azimuth_pvlib
    elif latitude is not None and longitude is not None:
        plant_id = "CUSTOM_SITE"
        lat_val = float(latitude)
        lon_val = float(longitude)
        tilt_val = float(tilt if tilt is not None else 15.0)
        azimuth_val = float(azimuth if azimuth is not None else 180.0)
        profile = PlantProfile(
            plant_name=plant_id,
            latitude=lat_val,
            longitude=lon_val,
            ac_capacity_mw=10.0,
            dc_capacity_mw=12.0,
            tilt_deg=tilt_val,
            azimuth_pvlib=azimuth_val,
            transfer_ratio=0.010,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either 'plant' (e.g., GSNP) or both 'latitude' and 'longitude'.",
        )

    # 3. Instantiate GTI Strategy with Open-Meteo Premium Key
    effective_key = _resolve_api_key(api_key)
    try:
        strategy: BaseGTIStrategy = get_gti_strategy(
            plant_profile=profile,
            api_key=effective_key,
            use_api=False,  # Enforce direct local computation inside the endpoint handler
        )
        gti_result = strategy.compute_gti(target_date_str=date)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"GTI Calculation failed: {exc}",
        )

    gti_arr = gti_result.gti_96
    cs_poa_arr = gti_result.cs_poa_96

    # 4. Construct 96 blocks
    blocks_list: list[GTIBlock] = []
    for b in range(96):
        start_min = b * 15
        end_min = (b + 1) * 15
        s_hr, s_m = divmod(start_min, 60)
        e_hr, e_m = divmod(end_min, 60)
        t_interval = f"{s_hr:02d}:{s_m:02d} - {'24:00' if e_hr == 24 else f'{e_hr:02d}:{e_m:02d}'}"

        blocks_list.append(
            GTIBlock(
                block=b + 1,
                time_interval=t_interval,
                gti_wm2=round(float(gti_arr[b]), 2),
                clearsky_poa_wm2=round(float(cs_poa_arr[b]), 2),
            )
        )

    # 5. Integrated solar metrics
    total_kwh_m2 = round(float(np.sum(gti_arr) * 0.25 / 1000.0), 3)
    peak_gti = round(float(np.max(gti_arr)), 2)

    openmeteo_endpoint_url = getattr(strategy, "get_api_url", lambda: "https://customer-ensemble-api.open-meteo.com/v1/ensemble")()

    # Extract top 6 models for each diurnal regime slot
    slot_selections = gti_result.metadata.get("slot_selections", {})
    if slot_selections:
        morning_models = list(slot_selections.get("morning", ([],))[0][:6])
        afternoon_models = list(slot_selections.get("midday", ([],))[0][:6])
        evening_models = list(slot_selections.get("afternoon", ([],))[0][:6])
    else:
        raw_keys = list(gti_result.metadata.get("selected_keys", []))
        morning_models = raw_keys[:6]
        afternoon_models = raw_keys[:6]
        evening_models = raw_keys[:6]

    selected_models_by_regime = {
        "morning": morning_models,
        "afternoon": afternoon_models,
        "evening": evening_models,
        "night": [],
    }

    tier_label = (
        f"Open-Meteo Customer Premium Tier (Commercial Key: {effective_key[:4]}...{effective_key[-3:]})"
        if effective_key
        else "Open-Meteo Public Free Tier"
    )

    return GTIResponse(
        status="success",
        plant_name=plant_id,
        target_date=date,
        total_daylight_kwh_m2=total_kwh_m2,
        peak_gti_wm2=peak_gti,
        metadata=GTIMetadata(
            latitude=round(lat_val, 4),
            longitude=round(lon_val, 4),
            tilt_deg=round(tilt_val, 2),
            azimuth_deg=round(azimuth_val, 2),
            openmeteo_endpoint=openmeteo_endpoint_url,
            plan_tier=tier_label,
            selected_models=selected_models_by_regime,
        ),
        blocks_96=blocks_list,
    )
