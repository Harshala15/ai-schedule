"""
Module 4: Faiman/Sandia Convective Thermal Derating & Inverter Power Transformation Engine for Day-Ahead Scheduling.
"""

import numpy as np
from typing import Dict, Any

class DAPhysicsBaseEngine:
    """
    Evaluates Sandia module cell temperature, thermal power derating, DC simulation,
    inverter AC conversion, site transfer multipliers M[b], and clear-sky envelope ceiling clipping.
    """

    def __init__(self, site_config: Dict[str, Any]):
        self.p_cap_ac = site_config.get("P_CAP_AC", 10.0)
        self.p_cap_dc = site_config.get("P_CAP_DC", 12.5)
        self.transfer_ratio = site_config.get("eta_transfer", self.p_cap_ac / self.p_cap_dc)
        self.gamma_Pmp = site_config.get("gamma_Pmp", -0.37) # -0.37% / °C
        self.u0 = site_config.get("u0", 29.0)
        self.u1 = site_config.get("u1", 0.0)
        self.transfer_multipliers = site_config.get("M_multipliers", np.ones(96))

    def compute_physics_base_power_matrix(
        self,
        gti_matrix: np.ndarray,      # (143, 96)
        t_air_matrix: np.ndarray,    # (143, 96) ambient temperature in °C
        wind_matrix: np.ndarray,     # (143, 96) 10m wind speed in m/s
        p_clearsky: np.ndarray       # (96,) clear-sky AC power envelope
    ) -> np.ndarray:
        """
        Computes 143 x 96 thermally derated AC base power matrix for Day T+1.
        """
        num_members, num_blocks = gti_matrix.shape
        p_base_matrix = np.zeros((num_members, num_blocks))

        for m in range(num_members):
            gti = gti_matrix[m, :]
            t_air = t_air_matrix[m, :]
            w_10m = wind_matrix[m, :]

            # 1. Sandia Faiman Module Cell Temperature
            t_cell = t_air + gti * np.exp(-self.u0 - self.u1 * w_10m)

            # 2. Temperature Derating Factor
            derate_temp = 1.0 + (self.gamma_Pmp / 100.0) * (t_cell - 25.0)

            # 3. DC Power Simulation
            p_dc = self.p_cap_dc * (gti / 1000.0) * derate_temp

            # 4. Inverter AC Conversion & Interconnect Clipping
            p_ac = np.minimum(p_dc * self.transfer_ratio, self.p_cap_ac)

            # 5. Site Empirical Array Transfer Multipliers
            p_base = np.round(p_ac * self.transfer_multipliers, 2)

            # 6. Clear-Sky Envelope Ceiling Clipping
            p_base = np.minimum(p_base, p_clearsky)

            p_base_matrix[m, :] = np.maximum(0.0, p_base)

        return p_base_matrix
