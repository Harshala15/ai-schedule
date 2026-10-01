"""
Module 4: 7-Day Wind Park Derating & Empirical Multipliers Engine.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Optional, List
import numpy as np


class WAWindParkDerateEngine:
    """
    Applies wake losses, electrical losses, availability derating, and
    calibrated 96-block transfer multipliers tiled across all 7 days (672 blocks).
    """

    @staticmethod
    def load_site_calibrated_multipliers(plant_name: str) -> Optional[List[float]]:
        """Load empirical 96-block transfer multipliers."""
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
    def apply_park_derate_672(
        gross_power_672: np.ndarray,
        rated_capacity_mw: float,
        park_derate_factor: float,
        plant_name: str,
    ) -> np.ndarray:
        """
        Derates gross 672-block generation to net grid-export power.
        """
        # 1. Base park factor
        net_mw = np.clip(gross_power_672 * park_derate_factor, 0.0, rated_capacity_mw)

        # 2. Site calibrated transfer multipliers tiled across 7 days
        multipliers = WAWindParkDerateEngine.load_site_calibrated_multipliers(plant_name)
        if multipliers and len(multipliers) == 96:
            mults_672 = np.tile(np.array(multipliers), 7)
            net_mw = net_mw * mults_672

        # 3. Final physical bounds
        return np.clip(np.round(net_mw, 2), 0.0, rated_capacity_mw)
