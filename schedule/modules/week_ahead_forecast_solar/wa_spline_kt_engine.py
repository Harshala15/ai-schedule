"""
Module 3: 7-Day Multi-Member Clearness Index (Kt) Cubic Spline & Transposition Engine.
Splines hourly clearness ratios to 96 15-minute intervals for each of the 7 days (672 blocks total).
"""

from __future__ import annotations
import numpy as np
from scipy.interpolate import CubicSpline


class WASplineKtEngine:
    """
    Transforms hourly 7-day NWP GHI forecasts into 672-block 15-minute Plane-of-Array (POA) irradiance.
    Operates per day on clearness ratios to prevent step discontinuities near sunrise and sunset.
    """

    def compute_7day_poa_matrix(
        self,
        nwp_hourly_ghi: np.ndarray, # (M, 168)
        poa_cs_672: np.ndarray,     # (672,)
        ghi_cs_672: np.ndarray,     # (672,)
    ) -> np.ndarray:
        """
        Reconstructs high-resolution 672-block POA irradiance matrix (M, 672).
        """
        num_members = nwp_hourly_ghi.shape[0]
        poa_matrix = np.zeros((num_members, 672))

        # Process day-by-day (7 days, 24 hours / 96 blocks each)
        for day in range(7):
            h_start = day * 24
            h_end = (day + 1) * 24

            b_start = day * 96
            b_end = (day + 1) * 96

            day_poa_cs = poa_cs_672[b_start:b_end]
            day_ghi_cs = ghi_cs_672[b_start:b_end]

            # Hourly clear-sky GHI midpoints
            ghi_cs_hourly = np.array([np.mean(day_ghi_cs[h*4:(h+1)*4]) for h in range(24)])
            ghi_cs_hourly = np.maximum(ghi_cs_hourly, 1.0) # Avoid division by zero

            hours_x = np.arange(24)
            blocks_x = np.linspace(0, 23, 96)

            for m in range(num_members):
                member_day_ghi = nwp_hourly_ghi[m, h_start:h_end]
                # Clearness index Kt clipped between 0.0 and 1.20
                kt_hourly = np.clip(member_day_ghi / ghi_cs_hourly, 0.0, 1.20)

                # Fit natural cubic spline across the 24 hours of the day
                cs_kt = CubicSpline(hours_x, kt_hourly, bc_type="natural")
                kt_blocks = np.clip(cs_kt(blocks_x), 0.0, 1.25)

                # Reconstruct Plane-of-Array irradiance
                poa_matrix[m, b_start:b_end] = kt_blocks * day_poa_cs

        return poa_matrix
