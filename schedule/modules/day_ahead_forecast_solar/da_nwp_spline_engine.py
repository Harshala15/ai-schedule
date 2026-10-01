"""
Module 3: 143-Member NWP Super-Ensemble Fetch & Physics-Guided Kt Spline Engine for Day-Ahead Scheduling.
"""

import numpy as np
from scipy.interpolate import CubicSpline
from typing import Dict, Any, Tuple

class DANWPSplineEngine:
    """
    Evaluates hourly clearness index Kt(H), fits natural cubic spline over 24 hourly points,
    evaluates spline at 96 15-minute midpoints Kt(b), and reconstructs GTI_m(b) = Kt_m(b) * POA_cs(b).
    """

    def __init__(self, num_members: int = 143):
        self.num_members = num_members

    def compute_physics_guided_gti_matrix(
        self,
        nwp_hourly_ghi: np.ndarray, # Shape: (143, 24) hourly GHI forecasts for Day T+1
        poa_cs: np.ndarray,         # Shape: (96,) clear-sky POA irradiance
        ghi_cs: np.ndarray          # Shape: (96,) clear-sky GHI irradiance
    ) -> np.ndarray:
        """
        Reconstructs high-resolution 15-minute Plane-of-Array GTI matrix (143, 96)
        avoiding direct linear interpolation trapezoidal step errors near sunrise/sunset.
        """
        gti_matrix = np.zeros((self.num_members, 96))
        
        # Hourly clear-sky GHI midpoints (take every 4th 15-min block midpoint)
        ghi_cs_hourly = np.array([np.mean(ghi_cs[h*4:(h+1)*4]) for h in range(24)])
        ghi_cs_hourly = np.maximum(ghi_cs_hourly, 1.0) # Avoid div-by-zero

        hours_x = np.arange(24)
        blocks_x = np.linspace(0, 23, 96)

        for m in range(self.num_members):
            # 1. Compute hourly clearness ratio Kt_m(H)
            kt_hourly = np.clip(nwp_hourly_ghi[m, :] / ghi_cs_hourly, 0.0, 1.20)
            
            # 2. Fit natural cubic spline over 24 hourly Kt points
            cs = CubicSpline(hours_x, kt_hourly, bc_type='natural')
            kt_15min = np.clip(cs(blocks_x), 0.0, 1.20)

            # 3. Physics-guided reconstruction of 15-minute GTI
            gti_15min = kt_15min * poa_cs

            # 4. Night zeroing (Blocks 1-23 and 75-96)
            gti_15min[0:23] = 0.0
            gti_15min[74:96] = 0.0

            gti_matrix[m, :] = np.maximum(0.0, gti_15min)

        return gti_matrix
