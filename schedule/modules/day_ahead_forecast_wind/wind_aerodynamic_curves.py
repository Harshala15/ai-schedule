"""
Wind Aerodynamic Power Curves & Turbine Profiles.
Jensen's-inequality-safe non-linear turbine gross power conversion.
"""

from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import numpy as np


@dataclass
class WindTurbineProfile:
    plant_name: str = "CHANDAWASA"
    rated_capacity_mw: float = 10.0
    turbine_manufacturer: str = "Gamesa"
    turbine_model: str = "G114/2000"
    rotor_diameter_m: float = 114.0
    num_turbines: int = 5
    hub_height_m: float = 80.0
    v_cut_in: float = 3.0
    v_rated: float = 10.5
    v_cut_out: float = 25.0
    ramp_exponent: float = 2.8
    park_derate_factor: float = 0.89  # wake loss (0.94) * electrical (0.98) * availability (0.97)
    standard_air_density: float = 1.225  # kg/m3 at standard sea level / 15 deg C

    @classmethod
    def from_plant_profile(cls, plant_name: str) -> WindTurbineProfile:
        """Instantiate WindTurbineProfile from plant profile JSON or fallback specs."""
        clean_name = plant_name.upper().strip()
        profile_dict: dict[str, Any] = {}

        # Look in schedule/plant_profiles/
        candidates = [
            Path(__file__).parent.parent.parent / "plant_profiles" / f"{clean_name}.json",
            Path(f"/var/task/plant_profiles/{clean_name}.json"),
            Path(f"schedule/plant_profiles/{clean_name}.json"),
        ]
        for c in candidates:
            if c.exists():
                try:
                    profile_dict = json.loads(c.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass

        cap = float(profile_dict.get("capacity_mw") or profile_dict.get("ac_capacity_mw") or 10.0)

        if "JEWLI" in clean_name:
            cap = max(cap, 100.0)
            return cls(
                plant_name="JEWLI",
                rated_capacity_mw=cap,
                turbine_manufacturer="Siemens Gamesa",
                turbine_model="SG 3.6-145",
                rotor_diameter_m=145.0,
                num_turbines=int(round(cap / 3.6)),
                hub_height_m=133.5,
                v_cut_in=3.0,
                v_rated=11.5,
                v_cut_out=25.0,
                ramp_exponent=2.8,
                park_derate_factor=0.90,
            )
        elif "JGBPL" in clean_name:
            cap = max(cap, 20.0)
            return cls(
                plant_name="JGBPL",
                rated_capacity_mw=cap,
                turbine_manufacturer="Envision",
                turbine_model="EN182-5.0",
                rotor_diameter_m=182.0,
                num_turbines=max(1, int(round(cap / 5.0))),
                hub_height_m=140.0,
                v_cut_in=3.0,
                v_rated=10.5,
                v_cut_out=25.0,
                ramp_exponent=2.8,
                park_derate_factor=0.89,
            )
        elif "CHANDAWASA" in clean_name or "CHANDWASA" in clean_name:
            return cls(
                plant_name="CHANDAWASA",
                rated_capacity_mw=10.0,
                turbine_manufacturer="Gamesa",
                turbine_model="G114/2000",
                rotor_diameter_m=114.0,
                num_turbines=5,
                hub_height_m=80.0,
                v_cut_in=3.0,
                v_rated=10.5,
                v_cut_out=25.0,
                ramp_exponent=2.8,
                park_derate_factor=0.89,
            )

        return cls(
            plant_name=clean_name,
            rated_capacity_mw=cap,
            hub_height_m=float(profile_dict.get("hub_height_m", 80.0)),
        )


# Siemens Gamesa SG 3.6-145 empirical power curve
SG_3_6_145_GROSS_CURVE = [
    (0.0, 0.000), (2.5, 0.000), (3.0, 0.004), (3.5, 0.024), (4.0, 0.075),
    (4.5, 0.125), (5.0, 0.175), (5.5, 0.222), (6.0, 0.295), (6.5, 0.365),
    (7.0, 0.455), (7.5, 0.550), (8.0, 0.660), (8.5, 0.770), (9.0, 0.880),
    (9.5, 0.960), (10.0, 1.000), (11.5, 1.000), (25.0, 1.000),
]
_SG_V = [p[0] for p in SG_3_6_145_GROSS_CURVE]
_SG_F = [p[1] for p in SG_3_6_145_GROSS_CURVE]

# Envision EN182-5.0 MW empirical power curve
ENVISION_EN182_GROSS_CURVE = [
    (0.0, 0.000), (3.0, 0.018), (3.5, 0.038), (4.0, 0.065), (4.5, 0.105),
    (5.0, 0.155), (5.5, 0.215), (6.0, 0.285), (6.5, 0.368), (7.0, 0.460),
    (7.5, 0.565), (8.0, 0.675), (8.5, 0.775), (9.0, 0.865), (9.5, 0.935),
    (10.0, 0.975), (10.5, 1.000), (25.0, 1.000), (25.1, 0.000),
]
_ENV_V = [p[0] for p in ENVISION_EN182_GROSS_CURVE]
_ENV_F = [p[1] for p in ENVISION_EN182_GROSS_CURVE]

# Gamesa G114/2000 empirical power curve
GAMESA_G114_GROSS_CURVE = [
    (0.0, 0.000), (2.0, 0.000), (2.5, 0.008), (3.0, 0.019), (3.5, 0.038),
    (4.0, 0.059), (4.5, 0.088), (5.0, 0.128), (5.5, 0.178), (6.0, 0.240),
    (6.5, 0.315), (7.0, 0.405), (7.5, 0.505), (8.0, 0.620), (8.5, 0.730),
    (9.0, 0.825), (9.5, 0.905), (10.0, 0.970), (10.5, 1.000), (25.0, 1.000), (25.1, 0.000),
]
_G114_V = [p[0] for p in GAMESA_G114_GROSS_CURVE]
_G114_F = [p[1] for p in GAMESA_G114_GROSS_CURVE]


def compute_gross_turbine_power(
    v_eff_ms: float,
    profile: WindTurbineProfile,
) -> float:
    """
    Computes instantaneous gross wind turbine generation (MW) from effective velocity.
    """
    if v_eff_ms < profile.v_cut_in or v_eff_ms >= profile.v_cut_out:
        return 0.0

    p_name = profile.plant_name.upper()

    if "JEWLI" in p_name:
        frac = float(np.interp(v_eff_ms, _SG_V, _SG_F))
        return float(np.clip(profile.rated_capacity_mw * frac, 0.0, profile.rated_capacity_mw))

    if "JGBPL" in p_name:
        frac = float(np.interp(v_eff_ms, _ENV_V, _ENV_F))
        return float(np.clip(profile.rated_capacity_mw * frac, 0.0, profile.rated_capacity_mw))

    if "CHANDAWASA" in p_name or "CHANDWASA" in p_name:
        frac = float(np.interp(v_eff_ms, _G114_V, _G114_F))
        return float(np.clip(profile.rated_capacity_mw * frac, 0.0, profile.rated_capacity_mw))

    # Standard IEC polynomial ramp
    if v_eff_ms < profile.v_rated:
        norm_v = (v_eff_ms - profile.v_cut_in) / max(0.1, (profile.v_rated - profile.v_cut_in))
        return float(np.clip(profile.rated_capacity_mw * (norm_v ** profile.ramp_exponent), 0.0, profile.rated_capacity_mw))
    else:
        return profile.rated_capacity_mw
