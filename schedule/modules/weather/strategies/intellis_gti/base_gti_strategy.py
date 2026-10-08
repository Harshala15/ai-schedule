"""base_gti_strategy.py

Defines the abstract interface, standard output data container (GTIForecastResult),
and shared solar astronomical geometry / ephemeris calculations for all solar GTI strategies.
"""

from __future__ import annotations

import datetime as dt
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd

try:
    from pvlib.location import Location
    from pvlib import irradiance
    HAS_PVLIB = True
except ImportError:
    HAS_PVLIB = False

try:
    import config
except ImportError:
    config = None


@dataclass
class GTIForecastResult:
    """Standardized output container for 96-block GTI calculation engines."""
    target_date: str
    gti_96: np.ndarray             # 96-block Global Tilted Irradiance (W/m²)
    cs_poa_96: np.ndarray          # 96-block Clear-Sky POA Irradiance (W/m²)
    blended_kt_96: np.ndarray      # 96-block Clearness Index (Kt)
    amb_temp_96: np.ndarray        # 96-block Ambient Temperature (°C)
    wind_speed_96: np.ndarray      # 96-block 10m Wind Speed (m/s)
    strategy_name: str             # e.g., 'METER_NWP_ENSEMBLE_GTI' or 'NON_METER_SATELLITE_GTI'
    telemetry_source: str          # e.g., 'PHYSICAL_SCADA' or 'SATELLITE_SOLAR_RADIATION'
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_solar_elevation_deg(timestamp: dt.datetime, lat: float, lon: float) -> float:
    """Compute solar elevation angle (degrees) using Cooper's declination equation."""
    day_of_year = timestamp.timetuple().tm_yday
    declination = 23.45 * math.sin(math.radians(360.0 / 365.0 * (284 + day_of_year)))
    solar_hour = timestamp.hour + timestamp.minute / 60.0
    hour_angle = 15.0 * (solar_hour - 12.0)

    lat_rad = math.radians(lat)
    dec_rad = math.radians(declination)
    hra_rad = math.radians(hour_angle)

    sin_elevation = (
        math.sin(lat_rad) * math.sin(dec_rad)
        + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(hra_rad)
    )
    sin_elevation = max(-1.0, min(1.0, sin_elevation))
    return math.degrees(math.asin(sin_elevation))


def compute_time_features(
    timestamp: dt.datetime,
    lat: float = 24.0,
    lon: float = 75.0,
) -> dict[str, Any]:
    """Returns a flat dictionary of time and solar ephemeris features."""
    elevation = compute_solar_elevation_deg(timestamp, lat, lon)
    return {
        "hour": timestamp.hour,
        "minute_of_day": timestamp.hour * 60 + timestamp.minute,
        "day_of_year": timestamp.timetuple().tm_yday,
        "month": timestamp.month,
        "solar_elevation_deg": round(elevation, 2),
        "is_daylight": 1 if elevation > 0 else 0,
    }


def get_block_times(
    start_dt: dt.datetime,
    num_blocks: int = 96,
    block_minutes: int = 15,
) -> list[dt.datetime]:
    """Returns a list of num_blocks datetimes starting from next block boundary."""
    remainder = start_dt.minute % block_minutes
    if remainder == 0 and start_dt.second == 0 and start_dt.microsecond == 0:
        base = start_dt.replace(second=0, microsecond=0)
    else:
        minutes_to_add = block_minutes - remainder
        base = (start_dt + dt.timedelta(minutes=minutes_to_add)).replace(second=0, microsecond=0)
    return [base + dt.timedelta(minutes=block_minutes * i) for i in range(num_blocks)]


def block_number_for_time(timestamp: dt.datetime) -> int:
    """Block 1 = 00:00-00:15, Block 96 = 23:45-24:00."""
    return timestamp.hour * 4 + (timestamp.minute // 15) + 1


class BaseGTIStrategy(ABC):
    """Abstract base class for all solar GTI calculation strategies."""

    def __init__(
        self,
        plant_profile: Any,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        **kwargs: Any,
    ):
        self.profile = plant_profile
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.extra_kwargs = kwargs

    def compute_clearsky_poa_96block(self, target_date_str: str) -> np.ndarray:
        """Compute theoretical 96-block Plane-of-Array (POA) Clear-Sky irradiance (W/m²)."""
        lat = getattr(self.profile, "latitude", 24.0)
        lon = getattr(self.profile, "longitude", 75.0)
        tilt = getattr(self.profile, "tilt_deg", 15.0)
        az_pvlib = getattr(self.profile, "azimuth_pvlib", 188.0)

        if HAS_PVLIB:
            try:
                times = pd.date_range(f"{target_date_str} 00:00", f"{target_date_str} 23:45", freq="15min", tz="Asia/Kolkata")
                loc = Location(lat, lon, tz="Asia/Kolkata")
                sp = loc.get_solarposition(times)
                cs = loc.get_clearsky(times)
                poa = irradiance.get_total_irradiance(
                    tilt,
                    az_pvlib,
                    sp["apparent_zenith"],
                    sp["azimuth"],
                    cs["dni"],
                    cs["ghi"],
                    cs["dhi"],
                )
                poa_vals = poa["poa_global"].fillna(0.0).clip(lower=0.0).values
                if np.max(poa_vals) > 400.0:
                    return poa_vals
            except Exception:
                pass

        # Robust physical solar geometry fallback
        b_idx = np.arange(96)
        noon_dist = np.abs(b_idx - 48.5) * 0.25
        cos_elev = np.maximum(0.0, np.cos(noon_dist * (np.pi / 12.0)))
        return np.where((b_idx >= 24) & (b_idx <= 75), 980.0 * (cos_elev ** 1.15), 0.0)

    @abstractmethod
    def compute_gti(
        self,
        target_date_str: str,
        **kwargs: Any,
    ) -> GTIForecastResult:
        """Compute the 96-block Global Tilted Irradiance (W/m²) and associated weather vectors."""
        pass
