"""
Solar Day-Ahead Generation Forecast Engine Master Orchestrator.

Orchestrates Modules 1 through 10 for Day-Ahead (Day T+1) 96-block schedule generation
prior to the statutory 10:00 AM gate closure, incorporating dynamic IST date resolution,
multi-agency enforcing algorithms, and physical guardrail screening.
"""

import os
import json
from datetime import datetime, timedelta
import pytz
import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple, List

from .da_member_selection import DAMemberSelectionEngine
from .da_prompt_builder import DAMasterPromptBuilder
from .da_llm_arbiter import DASolarLLMArbiter

class SolarDayAheadEngine:
    """
    Master Day-Ahead Solar Schedule Generation Pipeline.
    """

    def __init__(self, site_config: Dict[str, Any], openrouter_api_keys: List[str] = None):
        self.site_config = site_config
        self.site_id = site_config.get("site_id", "SOLAR_PLANT")
        self.p_cap_ac = site_config.get("P_CAP_AC", 10.0)
        self.p_cap_dc = site_config.get("P_CAP_DC", 12.5)

        self.mos_engine = DAMemberSelectionEngine()
        self.prompt_builder = DAMasterPromptBuilder(site_config)
        self.llm_arbiter = DASolarLLMArbiter(api_keys=openrouter_api_keys if openrouter_api_keys else [])

    def resolve_dates(self) -> Tuple[datetime.date, datetime.date]:
        """
        Dynamically resolves current execution date in Asia/Kolkata timezone (D_today)
        and target Day-Ahead forecast date (D_target = D_today + 1 day).
        """
        tz_ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(tz_ist)
        d_today = now_ist.date()
        d_target = d_today + timedelta(days=1)
        return d_today, d_target

    def apply_physical_guardrails(
        self,
        raw_pred: np.ndarray,
        p_clearsky: np.ndarray,
        p_mos_derated: np.ndarray,
        synoptic_regime: str,
        p_avail: np.ndarray = None
    ) -> np.ndarray:
        """
        Screens Day-Ahead predictions against 4 physical guardrails.
        """
        p_final = np.copy(raw_pred)
        if p_avail is None:
            p_avail = np.full(96, self.p_cap_ac)

        # Guardrail 1: Solar Geometry Night Zeroing (Blocks 1-23 and 75-96)
        p_final[0:23] = 0.0
        p_final[74:96] = 0.0

        # Guardrail 2: Clear-Sky Floor Lock
        for b in range(23, 74):
            if synoptic_regime == "CLEAR_SKY":
                if p_final[b] < p_mos_derated[b]:
                    p_final[b] = p_mos_derated[b]

        # Guardrail 3: Overcast Optical Cloud Ceiling
        for b in range(23, 74):
            if synoptic_regime in ("MONSOON_OVERCAST", "RAIN"):
                max_ceiling = max(p_mos_derated[b] * 1.10, self.p_cap_ac * 0.25)
                if p_final[b] > max_ceiling:
                    p_final[b] = max_ceiling

        # Guardrail 4: Ramp Continuity & Capacity Clip
        max_ramp = max(0.50, self.p_cap_ac * 0.10)
        for b in range(24, 74):
            diff = p_final[b] - p_final[b-1]
            if abs(diff) > max_ramp:
                p_final[b] = p_final[b-1] + np.sign(diff) * max_ramp

        # Capacity Clip to Available Hardware Threshold
        p_final = np.clip(p_final, 0.0, p_avail)
        return p_final

    def run_day_ahead_pipeline(
        self,
        p_clearsky: np.ndarray,
        physics_base_matrix_da: np.ndarray, # (143, 96) for D_target
        physics_base_history: np.ndarray,   # (7, 143, 96) past 7 days
        scada_history_7d: np.ndarray,       # (7, 96) past 7 days actuals
        synoptic_indicators: Dict[str, Any],
        maintenance_outage_mw: float = 0.0
    ) -> Dict[str, Any]:
        """
        Executes end-to-end 10-module Day-Ahead forecast pipeline.
        Returns complete execution result dictionary containing final 96-block schedule.
        """
        # Module 1: Dynamic Date Resolution
        d_today, d_target = self.resolve_dates()
        target_date_str = d_target.strftime("%Y-%m-%d")

        # Module 5: 24h Lead-Time MOS Evaluation & Diurnal Selection
        mae_matrix = self.mos_engine.evaluate_24h_lead_time_mae(physics_base_history, scada_history_7d)
        p_mos_da, mos_audit = self.mos_engine.generate_fused_day_ahead_ensemble(physics_base_matrix_da, mae_matrix)

        # Module 6: Planned Maintenance Derate
        p_avail = np.full(96, max(0.0, self.p_cap_ac - maintenance_outage_mw))
        p_mos_derated = np.minimum(p_mos_da, p_avail)

        # Module 7: Master Day-Ahead Prompt Construction
        prompt_text = self.prompt_builder.build_prompt(
            target_date_str=target_date_str,
            p_clearsky=p_clearsky,
            p_mos_derated=p_mos_derated,
            synoptic_indicators=synoptic_indicators,
            mos_audit_info=mos_audit
        )

        # Module 8: OpenRouter Strategic LLM Arbitration
        llm_result = self.llm_arbiter.execute_arbitration(
            prompt_text=prompt_text,
            fallback_mos_schedule=p_mos_derated,
            p_cap_ac=self.p_cap_ac
        )

        raw_llm_vector = llm_result.get("validated_schedule_vector", p_mos_derated)
        regime = llm_result.get("synoptic_regime", "CLEAR_SKY")

        # Module 9: Physical Guardrails & Schedule Arbitration
        p_da_final = self.apply_physical_guardrails(
            raw_pred=raw_llm_vector,
            p_clearsky=p_clearsky,
            p_mos_derated=p_mos_derated,
            synoptic_regime=regime,
            p_avail=p_avail
        )

        # Module 10: Formatting & Canonical 7-Column CSV Assembly
        df_schedule = pd.DataFrame({
            "Block": range(1, 97),
            "Time Interval": [f"{(b-1)*15//60:02d}:{((b-1)*15)%60:02d}" for b in range(1, 97)],
            "clearsky_poa_w_m2": np.round(p_clearsky, 2),
            "mos_consensus_mw": np.round(p_mos_derated, 2),
            "llm_quantile_mw": np.round(raw_llm_vector, 2),
            "da_schedule_mw": np.round(p_da_final, 2),
            "active_capacity_mw": np.round(p_avail, 2)
        })

        return {
            "site_id": self.site_id,
            "today_date": d_today.strftime("%Y-%m-%d"),
            "target_date": target_date_str,
            "da_schedule_df": df_schedule,
            "da_schedule_mw": p_da_final.tolist(),
            "llm_result": llm_result,
            "mos_audit": mos_audit
        }
