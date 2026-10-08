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

NON_METER_SITES = {
    "ANDAD", "GUGARIYAKHEDI", "SAWDA", "BALAKWADA", "CME", "CLIMATEDETOX",
    "EMIL", "UPL", "REWASEIT", "SIDDEHESH", "PRANAV", "LOKGREENB2", "LGEPL",
    "CHANDWASA", "CHANDAWASA"
}


def get_gti_strategy(
    plant_profile: Any,
    **kwargs: Any,
) -> BaseGTIStrategy:
    """Factory function to instantiate the correct GTI calculation strategy based on plant telemetry configuration."""
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

    if is_non_meter:
        return NonMeterGTIStrategy(plant_profile=plant_profile, **kwargs)
    return MeterGTIStrategy(plant_profile=plant_profile, **kwargs)


__all__ = [
    "BaseGTIStrategy",
    "GTIForecastResult",
    "MeterGTIStrategy",
    "NonMeterGTIStrategy",
    "get_gti_strategy",
    "NON_METER_SITES",
]
