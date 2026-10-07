"""
Module 4: 7-Day PV Cell Temperature Derating & Physical Guardrails Engine.
Transforms POA irradiance (W/m2) to AC generation (MW) with temperature derating,
night zeroing, inverter saturation, and physical clear-sky ceiling caps.
"""

from __future__ import annotations
import numpy as np


class WAPowerDerateEngine:
    """
    Computes statutory 672-block generation from consensus POA irradiance.
    """

    def __init__(
        self,
        capacity_ac_mw: float,
        capacity_dc_mw: float | None = None,
        temp_coeff: float = -0.0038, # -0.38%/deg C
        noct_c: float = 45.0,
        inverter_efficiency: float = 0.985,
    ):
        self.capacity_ac_mw = capacity_ac_mw
        self.capacity_dc_mw = capacity_dc_mw or (capacity_ac_mw * 1.30)
        self.temp_coeff = temp_coeff
        self.noct_c = noct_c
        self.inverter_efficiency = inverter_efficiency

    def convert_poa_to_power(
        self,
        poa_matrix_672: np.ndarray,      # (M, 672)
        poa_cs_672: np.ndarray,          # (672,)
        ambient_temp_168: np.ndarray,    # (168,) hourly
        solar_zenith_672: np.ndarray,    # (672,)
    ) -> np.ndarray:
        """
        Converts multi-member POA to final consensus 672-block schedule (MW).
        """
        # Interpolate 168 hourly temperatures to 672 15-minute intervals
        h_idx = np.arange(0, 168, 1.0)
        b_idx = np.arange(0, 168, 0.25)
        temp_672 = np.interp(b_idx, h_idx, ambient_temp_168)

        # 1. Take median consensus across NWP ensemble members
        poa_consensus_672 = np.median(poa_matrix_672, axis=0)

        # 2. PV Cell Temperature & Thermal Derate
        t_cell = temp_672 + (poa_consensus_672 / 800.0) * (self.noct_c - 20.0)
        thermal_derate = 1.0 + self.temp_coeff * (t_cell - 25.0)
        thermal_derate = np.clip(thermal_derate, 0.70, 1.10)

        # 3. DC Power Generation
        p_dc = self.capacity_dc_mw * (poa_consensus_672 / 1000.0) * thermal_derate
        p_ac = p_dc * self.inverter_efficiency

        # Inverter AC saturation cap
        p_ac = np.minimum(p_ac, self.capacity_ac_mw)

        # 4. Clear-Sky Physical Baseline & Ceiling Cap
        t_cell_cs = temp_672 + (poa_cs_672 / 800.0) * (self.noct_c - 20.0)
        thermal_derate_cs = np.clip(1.0 + self.temp_coeff * (t_cell_cs - 25.0), 0.70, 1.10)
        p_cs_dc = self.capacity_dc_mw * (poa_cs_672 / 1000.0) * thermal_derate_cs
        p_cs_ac = np.minimum(p_cs_dc * self.inverter_efficiency, self.capacity_ac_mw)

        # Never exceed 105% of clear-sky envelope (accounts for edge-of-cloud focusing)
        p_final = np.minimum(p_ac, p_cs_ac * 1.05)

        # 5. Strict Nighttime Zeroing
        night_mask = (solar_zenith_672 >= 89.5) | (poa_cs_672 <= 1.0)
        p_final[night_mask] = 0.0

        return np.maximum(np.round(p_final, 2), 0.0)
