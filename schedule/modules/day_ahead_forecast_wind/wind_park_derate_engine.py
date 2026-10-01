"""
Module: Wind Park Derating & Empirical Calibration Multipliers.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Optional, List
import numpy as np


class WindParkDerateEngine:
    """
    Applies wake losses, electrical transmission losses, plant availability factors,
    and site-specific 96-block calibrated transfer multipliers.
    """

    @staticmethod
    def load_site_calibrated_multipliers(plant_name: str) -> Optional[List[float]]:
        """Load empirical 96-block transfer multipliers for wind sites."""
        clean_name = re.sub(r"[^A-Za-z0-9_]", "", plant_name).upper()

        file_map = {
            "CHANDAWASA": "chandawasa_calibrated_weights.json",
            "CHANDWASA": "chandawasa_calibrated_weights.json",
            "JEWLI": "jewli_calibrated_multipliers.json",
            "JGBPL": "jgbpl_calibrated_multipliers.json",
        }

        filename = file_map.get(clean_name)
        if not filename:
            return None

        candidates = [
            Path(__file__).parent.parent.parent / "plant_profiles" / filename,
            Path(f"/var/task/plant_profiles/{filename}"),
            Path(f"schedule/plant_profiles/{filename}"),
        ]

        for cand in candidates:
            if cand.exists():
                try:
                    data = json.loads(cand.read_text(encoding="utf-8"))
                    mults = data.get("calibrated_block_multipliers", [])
                    if len(mults) == 96:
                        return mults
                except Exception:
                    pass
        return None

    @staticmethod
    def apply_park_derate_and_calibration(
        gross_power_mw_96: np.ndarray,
        rated_capacity_mw: float,
        park_derate_factor: float,
        plant_name: str,
    ) -> np.ndarray:
        """
        Derates gross turbine power to net grid-export schedule.
        """
        # 1. Base park derate (wake + electrical + availability)
        net_mw = np.clip(gross_power_mw_96 * park_derate_factor, 0.0, rated_capacity_mw)

        # 2. Site calibrated transfer multipliers
        multipliers = WindParkDerateEngine.load_site_calibrated_multipliers(plant_name)
        if multipliers and len(multipliers) == 96:
            mult_arr = np.array(multipliers)
            net_mw = net_mw * mult_arr

        # 3. Final physical clipping to [0.0, rated_capacity_mw]
        return np.clip(np.round(net_mw, 2), 0.0, rated_capacity_mw)
