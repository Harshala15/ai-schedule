"""
Module 6: Planned Hardware Maintenance & Availability Derate Engine for Day-Ahead Scheduling.
"""

import numpy as np
from typing import Dict, Any, Tuple

class DAMaintenanceEngine:
    """
    Ingests scheduled maintenance registry for Day T+1, calculates active available interconnect
    capacity envelope P_avail(b), and derates active power forecast vector.
    """

    def __init__(self, p_cap_ac: float, total_inverters: int = 10):
        self.p_cap_ac = p_cap_ac
        self.total_inverters = max(1, total_inverters)

    def compute_available_capacity_envelope(
        self,
        maintenance_logs: Dict[str, Any]
    ) -> np.ndarray:
        """
        Computes 96-block active available capacity array P_avail[1..96].
        """
        p_avail = np.full(96, self.p_cap_ac)
        
        # Outage dictionary: block_idx -> out_inverters count
        outages = maintenance_logs.get("inverter_outages", {})
        for b_str, count in outages.items():
            b = int(b_str) - 1
            if 0 <= b < 96:
                avail_ratio = max(0.0, (self.total_inverters - count) / self.total_inverters)
                p_avail[b] = self.p_cap_ac * avail_ratio

        return p_avail

    def apply_capacity_derate(
        self,
        p_mos_da: np.ndarray,
        p_avail: np.ndarray
    ) -> np.ndarray:
        """
        Derates forecast vector strictly to active capacity ceiling.
        """
        return np.minimum(p_mos_da, p_avail)
