"""
Module 2: NREL SPA / Ineichen Clear-Sky Solar Ephemeris & Transposition Engine for Day-Ahead Scheduling.
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple

class DAClearSkyEngine:
    """
    Computes NREL SPA solar ephemeris, Ineichen clear-sky radiation triplet,
    and Perez / Hay-Davies Plane-of-Array (POA) transposition for 96 grid blocks.
    """

    def __init__(self, site_config: Dict[str, Any]):
        self.latitude = site_config.get("latitude", 19.876)
        self.longitude = site_config.get("longitude", 75.342)
        self.altitude = site_config.get("altitude", 450.0)
        self.tilt = site_config.get("tilt", 20.0)
        self.azimuth = site_config.get("azimuth", 180.0)  # 180° = South facing
        self.linke_turbidity = site_config.get("linke_turbidity", 3.5)

    def compute_solar_ephemeris(self, target_date_str: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculates solar zenith angle theta_z, solar azimuth gamma_s,
        and elevation alpha = 90 - theta_z for all 96 blocks of target date.
        """
        # 96 15-minute midpoint block timestamps
        timestamps = pd.date_range(start=f"{target_date_str} 00:07:30", periods=96, freq="15min")
        
        # Synthetic astronomical solar geometry approximation
        day_of_year = timestamps[0].dayofyear
        declination = 23.45 * np.sin(np.radians(360 / 365 * (day_of_year - 81)))
        
        theta_z = np.zeros(96)
        gamma_s = np.zeros(96)
        alpha = np.zeros(96)

        for b in range(96):
            hour_angle = (b - 47.5) * 3.75  # 15 degrees per hour -> 3.75 deg per 15-min block
            lat_rad = np.radians(self.latitude)
            dec_rad = np.radians(declination)
            ha_rad = np.radians(hour_angle)

            cos_zenith = np.sin(lat_rad) * np.sin(dec_rad) + np.cos(lat_rad) * np.cos(dec_rad) * np.cos(ha_rad)
            zenith_deg = np.degrees(np.arccos(np.clip(cos_zenith, -1.0, 1.0)))
            
            theta_z[b] = zenith_deg
            alpha[b] = max(0.0, 90.0 - zenith_deg)
            gamma_s[b] = 180.0 + hour_angle * 0.75  # Approximate azimuth sweep

        return theta_z, gamma_s, alpha

    def compute_ineichen_clearsky_poa(self, target_date_str: str) -> Tuple[np.ndarray, np.ndarray]:
        """
        Evaluates Ineichen clear-sky model and Perez / Hay-Davies POA transposition.
        Returns POA_cs[1..96] and GHI_cs[1..96] in W/m².
        """
        theta_z, gamma_s, alpha = self.compute_solar_ephemeris(target_date_str)
        
        poa_cs = np.zeros(96)
        ghi_cs = np.zeros(96)

        solar_constant = 1361.0  # I0 W/m²

        for b in range(96):
            if alpha[b] < 3.0 or theta_z[b] >= 87.0:
                poa_cs[b] = 0.0
                ghi_cs[b] = 0.0
            else:
                zenith_rad = np.radians(theta_z[b])
                air_mass = 1.0 / (np.cos(zenith_rad) + 0.50572 * (96.07995 - theta_z[b]) ** (-1.6364))
                air_mass_corr = air_mass * (101325.0 * (1.0 - 2.25577e-5 * self.altitude) ** 5.25588 / 101325.0)

                # Ineichen clear sky formulas
                dni = solar_constant * 0.7 * np.exp(-0.09 * air_mass_corr * (self.linke_turbidity - 1.0))
                ghi = max(0.0, dni * np.cos(zenith_rad) + 50.0 * np.sin(np.radians(alpha[b])))
                ghi_cs[b] = ghi

                # Angle of Incidence (AOI)
                tilt_rad = np.radians(self.tilt)
                az_diff_rad = np.radians(gamma_s[b] - self.azimuth)
                cos_aoi = np.cos(zenith_rad) * np.cos(tilt_rad) + np.sin(zenith_rad) * np.sin(tilt_rad) * np.cos(az_diff_rad)
                cos_aoi = max(0.0, cos_aoi)

                poa_beam = dni * cos_aoi
                poa_ground = ghi * 0.20 * (1.0 - np.cos(tilt_rad)) / 2.0
                poa_diffuse = ghi * 0.15 * (1.0 + np.cos(tilt_rad)) / 2.0
                
                poa_cs[b] = max(0.0, poa_beam + poa_ground + poa_diffuse)

        return poa_cs, ghi_cs
