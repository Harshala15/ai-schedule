"""Pydantic schemas for Intellis GTI Commercial API."""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class GTIBlock(BaseModel):
    """Single 15-minute block GTI calculation result."""
    block: int = Field(..., ge=1, le=96, description="Block index from 1 to 96")
    time_interval: str = Field(..., description="15-minute boundary interval (e.g., '11:15 - 11:30')")
    gti_wm2: float = Field(..., description="Global Tilted Irradiance in W/m²")
    clearsky_poa_wm2: float = Field(..., description="Astronomical Plane-of-Array Clear-Sky benchmark in W/m²")
    temperature_c: Optional[float] = Field(None, description="2m Ambient air temperature in °C")
    wind_speed_m_s: Optional[float] = Field(None, description="10m Wind speed in m/s")


class GTIMetadata(BaseModel):
    """Metadata describing site parameters, geometry, and Open-Meteo endpoints."""
    latitude: float = Field(..., description="Site latitude in degrees")
    longitude: float = Field(..., description="Site longitude in degrees")
    tilt_deg: float = Field(..., description="Solar panel tilt angle in degrees")
    azimuth_deg: float = Field(..., description="Solar panel azimuth orientation in degrees")
    openmeteo_endpoint: str = Field(..., description="Open-Meteo endpoint used for extraction")
    plan_tier: str = Field(default="Open-Meteo Customer Premium Tier (Commercial License)", description="Active API plan tier")
    selected_models: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Top 6 ensemble members selected for each diurnal regime slot (morning, afternoon, evening, night)",
    )


class GTIResponse(BaseModel):
    """Standardized commercial response payload containing 96-block GTI values."""
    status: str = Field(default="success", description="Status string")
    plant_name: str = Field(..., description="Plant identifier or CUSTOM_SITE")
    target_date: str = Field(..., description="Target date in YYYY-MM-DD")
    units: dict[str, str] = Field(
        default_factory=lambda: {
            "gti": "W/m²",
            "clearsky_poa": "W/m²",
            "total_daylight": "kWh/m²",
        },
        description="Measurement units dictionary",
    )
    total_daylight_kwh_m2: float = Field(..., description="Integrated daily plane-of-array solar irradiation in kWh/m²")
    peak_gti_wm2: float = Field(..., description="Peak irradiance observed across the 96 blocks in W/m²")
    metadata: GTIMetadata = Field(..., description="Site configuration and forecast metadata")
    blocks_96: list[GTIBlock] = Field(..., description="Complete 96-block time series")


class HealthResponse(BaseModel):
    """Health check response schema."""
    status: str = "ok"
    service: str = "Intellis GTI Commercial Engine"
    openmeteo_plan: str = "Open-Meteo Customer Premium Plan (Commercial License)"
    version: str = "1.0.0"
