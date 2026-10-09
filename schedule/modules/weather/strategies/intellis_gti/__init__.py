"""intellis_gti strategy package.

Provides modular GTI (Global Tilted Irradiance) calculation engines:
1. MeterGTIStrategy: For metered sites with physical SCADA telemetry (4-NWP Ensemble + Kt Blending).
2. NonMeterGTIStrategy: For non-meter sites using Satellite GTI (Satellite Solar Radiation).
"""

from __future__ import annotations

from typing import Any
from modules.weather.strategies.intellis_gti.base_gti_strategy import (
    BaseGTIStrategy,
    GTIForecastResult,
)
from modules.weather.strategies.intellis_gti.meter_gti_strategy import (
    MeterGTIStrategy,
)
from modules.weather.strategies.intellis_gti.non_meter_gti_strategy import (
    NonMeterGTIStrategy,
)
from modules.weather.strategies.intellis_gti.remote_api_strategy import (
    RemoteAPIGTIStrategy,
    DEFAULT_GTI_API_URL,
)

NON_METER_SITES = {
    "ANDAD", "GUGARIYAKHEDI", "SAWDA", "BALAKWADA", "CME", "CLIMATEDETOX",
    "EMIL", "UPL", "REWASEIT", "SIDDEHESH", "PRANAV", "LOKGREENB2", "LGEPL",
    "CHANDWASA", "CHANDAWASA"
}


def get_gti_strategy(
    plant_profile: Any,
    use_api: bool | None = None,
    **kwargs: Any,
) -> BaseGTIStrategy:
    """Factory function to instantiate the correct GTI calculation strategy.
    
    Supports:
    1. Remote API Strategy with automatic local fallback (when use_api=True or USE_GTI_API=true).
    2. Non-Meter Satellite Strategy for non-meter sites.
    3. Meter NWP Ensemble Strategy for physical SCADA sites.
    """
    import os

    if isinstance(plant_profile, str):
        from modules.plant.plant_profile import load_plant_profile
        plant_profile = load_plant_profile(plant_profile)

    plant_name = getattr(plant_profile, "plant_name", "") or ""
    p_name = plant_name.upper().strip()
    meter_data = getattr(plant_profile, "meter_data", {}) or {}

    is_non_meter = (
        p_name in NON_METER_SITES
        or bool(meter_data.get("is_non_meter_site", False))
        or bool(meter_data.get("is_virtual", False))
        or bool(getattr(plant_profile, "is_non_meter_site", False))
    )

    api_url = kwargs.pop("api_url", None)
    timeout = kwargs.pop("timeout", 35)

    local_strat = (
        NonMeterGTIStrategy(plant_profile=plant_profile, **kwargs)
        if is_non_meter
        else MeterGTIStrategy(plant_profile=plant_profile, **kwargs)
    )

    should_use_api = use_api if use_api is not None else (
        os.getenv("USE_GTI_API", "").lower() in ("1", "true", "yes")
    )

    if should_use_api:
        return RemoteAPIGTIStrategy(
            plant_profile=plant_profile,
            fallback_strategy=local_strat,
            api_url=api_url,
            timeout=timeout,
            **kwargs,
        )

    return local_strat


__all__ = [
    "BaseGTIStrategy",
    "GTIForecastResult",
    "MeterGTIStrategy",
    "NonMeterGTIStrategy",
    "RemoteAPIGTIStrategy",
    "get_gti_strategy",
    "NON_METER_SITES",
    "DEFAULT_GTI_API_URL",
]
