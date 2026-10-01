"""
Module: Dynamic Atmospheric Air Density Correction (IEC 61400-12 Standard).
"""

import numpy as np


class WindDensityIECEngine:
    """
    Evaluates ambient air density rho (kg/m3) from surface pressure and ambient temperature
    and normalizes hub-height wind speed according to IEC 61400-12 power performance standards.
    """

    R_SPEC = 287.05  # J/(kg * K) for dry air
    STANDARD_DENSITY = 1.225  # kg/m3

    @staticmethod
    def calculate_air_density(surface_pressure_hpa: float, temperature_c: float) -> float:
        """Ideal gas law air density calculation."""
        p_pa = max(600.0, min(1100.0, surface_pressure_hpa)) * 100.0
        t_kelvin = max(240.0, min(330.0, temperature_c + 273.15))
        return round(p_pa / (WindDensityIECEngine.R_SPEC * t_kelvin), 4)

    @staticmethod
    def calculate_effective_velocity(
        v_hub_ms: float,
        air_density: float,
        standard_density: float = STANDARD_DENSITY,
    ) -> float:
        """
        IEC 61400-12 kinetic energy density normalization:
        v_eff = v_hub * (rho / rho_0)^(1/3)
        """
        ratio = max(0.70, min(1.30, air_density / standard_density))
        return float(v_hub_ms * (ratio ** (1.0 / 3.0)))

    @staticmethod
    def interpolate_to_96_blocks(hourly_values: list[float]) -> np.ndarray:
        """Interpolates 24-point hourly array to 96 15-minute statutory blocks."""
        h_idx = np.arange(0, 24, 1.0)
        b_idx = np.arange(0, 24, 0.25)
        vals = hourly_values[:24]
        if len(vals) < 24:
            vals += [vals[-1] if vals else 1.225] * (24 - len(vals))
        return np.interp(b_idx, h_idx, vals)
