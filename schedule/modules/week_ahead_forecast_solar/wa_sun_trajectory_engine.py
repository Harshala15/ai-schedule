"""
Module 1: 7-Day High-Precision Astronomical Sun Trajectory & Clear-Sky POA Irradiance Engine.
Computes solar position, GHI_cs, and POA_cs for all 672 15-minute intervals across the 7-day forecast horizon.
"""

from __future__ import annotations
import datetime as dt
from typing import Dict, Any, Tuple
import numpy as np
import pandas as pd
import pvlib
from zoneinfo import ZoneInfo


class WASunTrajectoryEngine:
    """
    Evaluates Ineichen clear-sky model and Perez transposition over all 7 days (672 blocks).
    """

    def __init__(
        self,
        lat: float,
        lon: float,
        elevation_m: float = 450.0,
        surface_tilt: float = 15.0,
        surface_azimuth: float = 180.0, # South facing
        albedo: float = 0.20,
    ):
        self.location = pvlib.location.Location(
            latitude=lat,
            longitude=lon,
            altitude=elevation_m,
            tz="Asia/Kolkata",
        )
        self.surface_tilt = surface_tilt
        self.surface_azimuth = surface_azimuth
        self.albedo = albedo

    def compute_7day_clearsky_trajectory(
        self,
        start_date_str: str,
    ) -> Tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, np.ndarray]:
        """
        Generates 7-day (672 15-minute blocks) midpoints, clear-sky POA, GHI, and solar zenith.
        
        Returns:
            times: DatetimeIndex of length 672 (15-min midpoints)
            poa_cs: Array of length 672 (W/m2)
            ghi_cs: Array of length 672 (W/m2)
            solar_zenith: Array of length 672 (degrees)
        """
        start_dt = dt.datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        end_dt = start_dt + dt.timedelta(days=7)

        # 15-min intervals: 7 days * 96 blocks = 672 blocks
        # We evaluate at block midpoints (+7.5 minutes) for astronomical accuracy
        intervals_start = pd.date_range(start=start_dt, end=end_dt, freq="15min", inclusive="left")
        midpoints = intervals_start + pd.Timedelta(minutes=7.5)

        # Solar position
        solar_pos = self.location.get_solarposition(midpoints)
        solar_zenith = solar_pos["zenith"].values
        solar_azimuth = solar_pos["azimuth"].values

        # Ineichen clear-sky irradiance
        cs = self.location.get_clearsky(midpoints, model="ineichen")
        ghi_cs = np.array(cs["ghi"].values, copy=True)
        dni_cs = np.array(cs["dni"].values, copy=True)
        dhi_cs = np.array(cs["dhi"].values, copy=True)
        dni_extra = pvlib.irradiance.get_extra_radiation(midpoints).values

        # Transposition to plane of array (POA)
        total_irrad = pvlib.irradiance.get_total_irradiance(
            surface_tilt=self.surface_tilt,
            surface_azimuth=self.surface_azimuth,
            solar_zenith=solar_zenith,
            solar_azimuth=solar_azimuth,
            dni=dni_cs,
            ghi=ghi_cs,
            dhi=dhi_cs,
            dni_extra=dni_extra,
            albedo=self.albedo,
            model="perez",
        )

        poa_raw = total_irrad["poa_global"]
        poa_cs = np.maximum(np.nan_to_num(poa_raw, nan=0.0), 0.0)
        poa_cs = np.array(poa_cs, copy=True)

        # Nighttime zeroing (zenith >= 90 deg)
        night_mask = solar_zenith >= 89.5
        poa_cs[night_mask] = 0.0
        ghi_cs[night_mask] = 0.0

        return midpoints, poa_cs, ghi_cs, solar_zenith
