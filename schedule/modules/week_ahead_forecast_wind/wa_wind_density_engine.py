"""
Module 2: 7-Day Dynamic Atmospheric Air Density Correction (IEC 61400-12).
"""

from __future__ import annotations
import numpy as np


class WAWindDensityEngine:
    """
    Computes ambient air density (kg/m3) over 168 hours, interpolates to 672 blocks,
    and applies IEC 61400-12 kinetic speed normalization: v_eff = v_hub * (rho / rho_0)^(1/3).
    """

    R_SPEC = 287.05  # J/(kg * K) for dry air
    STANDARD_DENSITY = 1.225  # kg/m3

    @staticmethod
    def calculate_air_density(surface_pressure_hpa: float, temperature_c: float) -> float:
        """Ideal gas law air density."""
        p_pa = max(600.0, min(1100.0, surface_pressure_hpa)) * 100.0
        t_k = max(240.0, min(330.0, temperature_c + 273.15))
        return round(p_pa / (WAWindDensityEngine.R_SPEC * t_k), 4)

    @staticmethod
    def calculate_effective_velocity(
        v_hub_ms: float,
        air_density: float,
        standard_density: float = STANDARD_DENSITY,
    ) -> float:
        """IEC 61400-12 kinetic energy density normalization."""
        ratio = max(0.70, min(1.30, air_density / standard_density))
        return float(v_hub_ms * (ratio ** (1.0 / 3.0)))

    @staticmethod
    def interpolate_to_672_blocks(hourly_values: list[float]) -> np.ndarray:
        """Interpolates 168 hourly values to 672 15-minute intervals."""
        h_idx = np.arange(0, 168, 1.0)
        b_idx = np.arange(0, 168, 0.25)
        vals = hourly_values[:168]
        if len(vals) < 168:
            vals += [vals[-1] if vals else 1.225] * (168 - len(vals))
        return np.interp(b_idx, h_idx, vals)
