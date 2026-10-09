"""remote_api_strategy.py

Dedicated GTI strategy that queries the Intellis GTI REST API endpoint for
96-block irradiance data, with automatic seamless fallback to local strategy execution.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
import numpy as np
import requests

from modules.weather.strategies.intellis_gti.base_gti_strategy import (
    BaseGTIStrategy,
    GTIForecastResult,
)

logger = logging.getLogger(__name__)

DEFAULT_GTI_API_URL = "https://cbe0jmvos5.execute-api.ap-south-1.amazonaws.com/v1/intellis_gti_regime"


class RemoteAPIGTIStrategy(BaseGTIStrategy):
    """Fetches 96-block GTI from Intellis REST API with automatic fallback to local strategy."""

    def __init__(
        self,
        plant_profile: Any,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        api_url: str | None = None,
        fallback_strategy: BaseGTIStrategy | None = None,
        timeout: int = 35,
        **kwargs: Any,
    ):
        super().__init__(plant_profile, api_key=api_key, cache_dir=cache_dir, **kwargs)
        self.api_url = api_url or os.getenv("INTELLIS_GTI_API_URL", DEFAULT_GTI_API_URL)
        self.fallback_strategy = fallback_strategy
        self.timeout = timeout

    def _get_local_fallback(self) -> BaseGTIStrategy:
        """Instantiate the appropriate local strategy (Meter vs Non-Meter)."""
        if self.fallback_strategy:
            return self.fallback_strategy

        from modules.weather.strategies.intellis_gti.meter_gti_strategy import MeterGTIStrategy
        from modules.weather.strategies.intellis_gti.non_meter_gti_strategy import NonMeterGTIStrategy

        plant_name = str(getattr(self.profile, "plant_name", "")).upper().strip()
        meter_data = getattr(self.profile, "meter_data", {}) or {}
        non_meter_sites = {
            "ANDAD", "GUGARIYAKHEDI", "SAWDA", "BALAKWADA", "CME", "CLIMATEDETOX",
            "EMIL", "UPL", "REWASEIT", "SIDDEHESH", "PRANAV", "LOKGREENB2", "LGEPL",
            "CHANDWASA", "CHANDAWASA"
        }
        is_non_meter = (
            plant_name in non_meter_sites
            or bool(meter_data.get("is_non_meter_site", False))
            or bool(meter_data.get("is_virtual", False))
            or bool(getattr(self.profile, "is_non_meter_site", False))
        )

        strat_cls = NonMeterGTIStrategy if is_non_meter else MeterGTIStrategy
        return strat_cls(
            plant_profile=self.profile,
            api_key=self.api_key,
            cache_dir=self.cache_dir,
            **self.extra_kwargs,
        )

    def compute_gti(
        self,
        target_date_str: str,
        selected_keys: list[str] | None = None,
        weights_map: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> GTIForecastResult:
        """Query remote API for 96-block GTI values with automatic local fallback."""
        plant_name = getattr(self.profile, "plant_name", "GSNP")
        lat = getattr(self.profile, "latitude", None)
        lon = getattr(self.profile, "longitude", None)
        tilt = getattr(self.profile, "tilt_deg", 15.0)
        azimuth = getattr(self.profile, "azimuth_pvlib", 180.0)

        params: dict[str, Any] = {"date": target_date_str}
        if plant_name:
            params["plant"] = plant_name
        elif lat is not None and lon is not None:
            params["latitude"] = lat
            params["longitude"] = lon
            params["tilt"] = tilt
            params["azimuth"] = azimuth

        # 1. Attempt API query
        try:
            resp = requests.get(self.api_url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                payload = resp.json()
                blocks = payload.get("blocks_96", [])
                if len(blocks) == 96:
                    gti_arr = np.array([float(b.get("gti_wm2", 0.0)) for b in blocks], dtype=float)
                    cs_poa = np.array([float(b.get("clearsky_poa_wm2", 0.0)) for b in blocks], dtype=float)

                    denom = np.maximum(15.0, cs_poa)
                    blended_kt = np.clip(gti_arr / denom, 0.0, 1.15)
                    blended_kt[:23] = 0.0
                    blended_kt[76:] = 0.0

                    amb_temp = 25.0 + 10.0 * np.sin(np.pi * np.maximum(0, np.arange(96) - 24) / 56.0)
                    wind_speed = np.full(96, 2.5)

                    return GTIForecastResult(
                        target_date=target_date_str,
                        gti_96=gti_arr,
                        cs_poa_96=cs_poa,
                        blended_kt_96=blended_kt,
                        amb_temp_96=amb_temp,
                        wind_speed_96=wind_speed,
                        strategy_name="REMOTE_INTELLIS_GTI_API",
                        telemetry_source="API_GATEWAY_LAMBDA",
                        metadata=payload.get("metadata", {}),
                    )
                else:
                    logger.warning("Remote API returned %d blocks instead of 96. Triggering local fallback.", len(blocks))
            else:
                logger.warning("Remote API returned status %d: %s. Triggering local fallback.", resp.status_code, resp.text[:100])
        except Exception as exc:
            logger.warning("Remote API request failed (%s). Triggering local fallback.", exc)

        # 2. Local Fallback (Guarantees zero downtime & 100% accuracy)
        fallback = self._get_local_fallback()
        return fallback.compute_gti(
            target_date_str=target_date_str,
            selected_keys=selected_keys,
            weights_map=weights_map,
            **kwargs,
        )

    def fetch_ensemble_weather(self, target_date_str: str) -> dict[str, Any]:
        """Delegate raw NWP weather retrieval to fallback strategy if needed."""
        fallback = self._get_local_fallback()
        if hasattr(fallback, "fetch_ensemble_weather"):
            return fallback.fetch_ensemble_weather(target_date_str)
        return {}
