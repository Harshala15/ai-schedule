"""
time_features.py

Two jobs:
1. Compute time/solar-position features for a given timestamp + location
   (hour, day of year, approximate solar elevation) -- these matter a lot
   for solar generation independent of clouds (e.g. no sun = no
   generation, regardless of cloud cover).
2. The 15-minute BLOCK TIME helpers (previously in test_multi_image.py),
   moved here since they're time-related and now shared between the
   capture script and the prediction pipeline.

NOTE on solar elevation: this uses a standard simplified formula
(Cooper's declination equation), and assumes local clock time ~= solar
time (no timezone/longitude/equation-of-time correction). It's accurate
enough to distinguish "sun high in sky" vs "near sunrise/sunset" vs
"night", which is what the ML model needs -- for a precise
astronomically-correct value, use the `pvlib` library instead.
"""

import datetime
import math

import config


def get_block_times(start_dt, num_blocks=config.NUM_FORECAST_BLOCKS, block_minutes=config.BLOCK_MINUTES):
    """
    Returns a list of `num_blocks` datetimes, each `block_minutes` apart,
    starting from the NEXT block boundary AT OR AFTER `start_dt` -- so
    every predicted block is strictly the present moment or later.
    """
    remainder = start_dt.minute % block_minutes
    if remainder == 0 and start_dt.second == 0 and start_dt.microsecond == 0:
        base = start_dt.replace(second=0, microsecond=0)
    else:
        minutes_to_add = block_minutes - remainder
        base = (start_dt + datetime.timedelta(minutes=minutes_to_add)).replace(second=0, microsecond=0)
    return [base + datetime.timedelta(minutes=block_minutes * i) for i in range(num_blocks)]


def block_number_for_time(dt):
    """Block 1 = 00:00, Block 2 = 00:15, ... Block 96 = 23:45."""
    return dt.hour * 4 + (dt.minute // 15) + 1


def _solar_elevation_deg(dt: datetime.datetime, lat: float, lon: float) -> float:
    day_of_year = dt.timetuple().tm_yday

    # Cooper's equation for solar declination (degrees)
    declination = 23.45 * math.sin(math.radians(360.0 / 365.0 * (284 + day_of_year)))

    # Approximate solar hour angle assuming clock time ~= solar time
    solar_hour = dt.hour + dt.minute / 60.0
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


def compute_time_features(dt: datetime.datetime, lat: float = config.PLANT_LAT, lon: float = config.PLANT_LON) -> dict:
    """
    Returns a flat dict of time-based features for a single timestamp:
        hour, minute_of_day, day_of_year, month, solar_elevation_deg,
        is_daylight
    """
    elevation = _solar_elevation_deg(dt, lat, lon)
    return {
        "hour": dt.hour,
        "minute_of_day": dt.hour * 60 + dt.minute,
        "day_of_year": dt.timetuple().tm_yday,
        "month": dt.month,
        "solar_elevation_deg": round(elevation, 2),
        "is_daylight": 1 if elevation > 0 else 0,
    }
