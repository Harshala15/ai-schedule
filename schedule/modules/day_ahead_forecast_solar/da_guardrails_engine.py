"""
Module 9: Four Physical Guardrail Screening & Final Day-Ahead Arbitration Engine.
"""

import numpy as np

class DAPhysicalGuardrailsEngine:
    """
    Screens forecast predictions against 4 physical guardrails.
    """

    def __init__(self, p_cap_ac: float):
        self.p_cap_ac = p_cap_ac

    def screen_guardrails(
        self,
        raw_pred: np.ndarray,
        p_clearsky: np.ndarray,
        p_mos_derated: np.ndarray,
        synoptic_regime: str,
        p_avail: np.ndarray = None
    ) -> np.ndarray:
        """
        Applies Guardrails 1 through 4 and clips output to available capacity envelope.
        """
        p_final = np.copy(raw_pred)
        if p_avail is None:
            p_avail = np.full(96, self.p_cap_ac)

        # GUARDRAIL 1: Solar Geometry Hard Cutoff (Night Zeroing)
        # Blocks 1-23 (00:00 to 05:45 IST) and Blocks 75-96 (18:30 to 24:00 IST)
        p_final[0:23] = 0.0
        p_final[74:96] = 0.0

        # GUARDRAIL 2: Clear-Sky Floor Lock (Midday Clear-Sky Protection)
        for b in range(23, 74):
            if synoptic_regime == "CLEAR_SKY":
                if p_final[b] < p_mos_derated[b]:
                    p_final[b] = p_mos_derated[b]

        # GUARDRAIL 3: Overcast Optical Cloud Ceiling (Monsoon / Heavy Cloud Lock)
        for b in range(23, 74):
            if synoptic_regime in ("MONSOON_OVERCAST", "RAIN"):
                max_ceiling = max(p_mos_derated[b] * 1.10, self.p_cap_ac * 0.25)
                if p_final[b] > max_ceiling:
                    p_final[b] = max_ceiling

        # GUARDRAIL 4: Ramp-Rate Continuity Filter
        max_ramp = max(0.50, self.p_cap_ac * 0.10)
        for b in range(24, 74):
            diff = p_final[b] - p_final[b-1]
            if abs(diff) > max_ramp:
                p_final[b] = p_final[b-1] + np.sign(diff) * max_ramp

        # Hardware Available Capacity Clipping
        p_final = np.clip(p_final, 0.0, p_avail)
        return p_final
