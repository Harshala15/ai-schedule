"""
Day-Ahead Model Output Statistics (MOS) & Diurnal Portfolio Selection Engine.

Implements 24h-48h lead-time historical performance evaluation, 3-slot diurnal candidate
ranking, Mandatory Agency Diversity Capping (max 2 models per NWP family), Bayesian inverse-loss
weighting (weighted percentage vs simple mean), and raised-cosine slot cross-fading.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any

class DAMemberSelectionEngine:
    """
    Day-Ahead Candidate Selection and MOS Weighting Pipeline.
    Evaluates 143 NWP ensemble candidates on 24h lead-time accuracy.
    """

    NWP_AGENCIES = {
        "ECMWF": range(0, 35),      # Members 0..34 (35 members)
        "ICON": range(35, 75),      # Members 35..74 (40 members)
        "GFS": range(75, 105),      # Members 75..104 (30 members)
        "GEM": range(105, 143)      # Members 105..142 (38 members)
    }

    SLOTS = {
        "SLOT_1_MORNING": (23, 39),   # Blocks 24-40 (06:00 - 10:00 IST, 0-indexed 23..39)
        "SLOT_2_MIDDAY": (40, 55),    # Blocks 41-56 (10:00 - 14:00 IST, 0-indexed 40..55)
        "SLOT_3_EVENING": (56, 73)    # Blocks 57-74 (14:00 - 18:30 IST, 0-indexed 56..73)
    }

    def __init__(self, max_models_per_agency: int = 2, top_k_slot_models: int = 6):
        self.max_agency_cap = max_models_per_agency
        self.top_k = top_k_slot_models

    def _get_agency_for_member(self, member_idx: int) -> str:
        for agency, rng in self.NWP_AGENCIES.items():
            if member_idx in rng:
                return agency
        return "UNKNOWN"

    def evaluate_24h_lead_time_mae(
        self,
        physics_base_history: np.ndarray, # Shape: (days, 143, 96)
        scada_actuals_history: np.ndarray  # Shape: (days, 96)
    ) -> np.ndarray:
        """
        Calculates Mean Absolute Error across 24h-48h forecast lead times
        over rolling lookback window (e.g. past 7 days).
        Returns MAE array of shape (143, 96).
        """
        num_days, num_members, num_blocks = physics_base_history.shape
        mae_matrix = np.zeros((num_members, num_blocks))

        for m in range(num_members):
            for b in range(num_blocks):
                valid_mask = ~np.isnan(scada_actuals_history[:, b])
                if np.sum(valid_mask) > 0:
                    errors = np.abs(physics_base_history[valid_mask, m, b] - scada_actuals_history[valid_mask, b])
                    mae_matrix[m, b] = np.mean(errors)
                else:
                    mae_matrix[m, b] = 999.0  # Penalty default for unrated / missing actuals
        return mae_matrix

    def select_top_6_slot_champions(
        self,
        mae_matrix: np.ndarray,
        slot_name: str
    ) -> List[int]:
        """
        Ranks all 143 members in a given diurnal slot by ascending MAE and applies
        MANDATORY AGENCY DIVERSITY CAPPING (max 2 members per NWP agency family).
        
        Why? Prevents single-model family bias (e.g. 6 ECMWF members dominating the pool
        due to shared systematic bias), preserving multi-agency ensemble diversity.
        """
        b_start, b_end = self.SLOTS[slot_name]
        # Average MAE across slot blocks
        slot_mae = np.mean(mae_matrix[:, b_start:b_end+1], axis=1)

        # Rank candidate indices by ascending MAE
        ranked_indices = np.argsort(slot_mae)

        selected_members = []
        agency_counts = {agency: 0 for agency in self.NWP_AGENCIES.keys()}

        # Pass 1: Enforce strict max_agency_cap (default: 2)
        for m_idx in ranked_indices:
            agency = self._get_agency_for_member(m_idx)
            if agency_counts.get(agency, 0) < self.max_agency_cap:
                selected_members.append(int(m_idx))
                agency_counts[agency] += 1
                if len(selected_members) >= self.top_k:
                    break

        # Pass 2: Fallback relaxation if 6 members not filled (e.g. cap relaxed to 3)
        if len(selected_members) < self.top_k:
            for m_idx in ranked_indices:
                if int(m_idx) not in selected_members:
                    selected_members.append(int(m_idx))
                    if len(selected_members) >= self.top_k:
                        break

        return selected_members

    def compute_bayesian_inverse_loss_weights(
        self,
        selected_members: List[int],
        mae_matrix: np.ndarray,
        slot_name: str
    ) -> np.ndarray:
        """
        Computes Bayesian Inverse-Loss Weights (Weighted Percentage) for selected candidates.
        
        Simple mean is strictly rejected because equal-weighting treats an inaccurate member
        (MAE = 1.2 MW) identically to a high-accuracy member (MAE = 0.3 MW).
        
        Formula: w_i = (1 / MAE_i) / sum(1 / MAE_j)
        """
        b_start, b_end = self.SLOTS[slot_name]
        slot_maes = np.array([np.mean(mae_matrix[m, b_start:b_end+1]) for m in selected_members])

        # Avoid divide-by-zero
        slot_maes = np.maximum(slot_maes, 1e-4)
        inv_maes = 1.0 / slot_maes
        weights = inv_maes / np.sum(inv_maes)
        return weights

    def generate_fused_day_ahead_ensemble(
        self,
        physics_base_da: np.ndarray,  # Shape: (143, 96) for Day T+1
        mae_matrix: np.ndarray
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """
        Produces 96-block Day-Ahead fused MOS consensus power forecast
        with Bayesian weighted percentage aggregation and raised-cosine slot cross-fading.
        """
        fused_forecast = np.zeros(96)
        slot_selection_audit = {}

        # 1. Compute raw slot blended forecasts
        slot_forecasts = {}
        for slot_name, (b_start, b_end) in self.SLOTS.items():
            champions = self.select_top_6_slot_champions(mae_matrix, slot_name)
            weights = self.compute_bayesian_inverse_loss_weights(champions, mae_matrix, slot_name)
            
            # Weighted percentage sum for slot
            slot_blend = np.zeros(96)
            for idx, m_idx in enumerate(champions):
                slot_blend += weights[idx] * physics_base_da[m_idx, :]
            
            slot_forecasts[slot_name] = slot_blend
            slot_selection_audit[slot_name] = {
                "champions": champions,
                "weights": weights.tolist(),
                "champion_agencies": [self._get_agency_for_member(c) for c in champions]
            }

        # 2. Assemble 96-block forecast with raised-cosine cross-fading
        fused_forecast[0:23] = 0.0  # Night hours

        # Slot 1 Morning (Blocks 24 to 40, index 23..39)
        fused_forecast[23:40] = slot_forecasts["SLOT_1_MORNING"][23:40]

        # Transition 1 to 2 (Blocks 39 to 42 - 4-block raised-cosine fade, index 38..41)
        for i, b in enumerate(range(38, 42)):
            w_fade = 0.5 * (1.0 - np.cos(np.pi * i / 3.0))
            fused_forecast[b] = (1.0 - w_fade) * slot_forecasts["SLOT_1_MORNING"][b] + w_fade * slot_forecasts["SLOT_2_MIDDAY"][b]

        # Slot 2 Midday (Blocks 43 to 56, index 42..55)
        fused_forecast[42:56] = slot_forecasts["SLOT_2_MIDDAY"][42:56]

        # Transition 2 to 3 (Blocks 55 to 58 - 4-block raised-cosine fade, index 54..57)
        for i, b in enumerate(range(54, 58)):
            w_fade = 0.5 * (1.0 - np.cos(np.pi * i / 3.0))
            fused_forecast[b] = (1.0 - w_fade) * slot_forecasts["SLOT_2_MIDDAY"][b] + w_fade * slot_forecasts["SLOT_3_EVENING"][b]

        # Slot 3 Evening (Blocks 59 to 74, index 58..73)
        fused_forecast[58:74] = slot_forecasts["SLOT_3_EVENING"][58:74]

        # Night zeroing (Blocks 75 to 96, index 74..95)
        fused_forecast[74:96] = 0.0

        return fused_forecast, slot_selection_audit
