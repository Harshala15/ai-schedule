"""
Master Day-Ahead Prompt Builder Module for Solar Power Forecasting.

Assembles system constraints, synoptic atmospheric indicators, 24h lead-time MOS accuracy rankings,
a 96-block comparison data matrix, and target JSON output schema into a structured prompt
for the OpenRouter Strategic Day-Ahead LLM Arbiter.
"""

from typing import Dict, Any, List
import numpy as np

class DAMasterPromptBuilder:
    """
    Constructs Day-Ahead Master Prompt for OpenRouter GPT-5.6 Luna.
    """

    def __init__(self, site_config: Dict[str, Any]):
        self.site_config = site_config

    def build_prompt(
        self,
        target_date_str: str,
        p_clearsky: np.ndarray,
        p_mos_derated: np.ndarray,
        synoptic_indicators: Dict[str, Any],
        mos_audit_info: Dict[str, Any]
    ) -> str:
        site_id = self.site_config.get("site_id", "SOLAR_PLANT")
        p_cap_ac = self.site_config.get("P_CAP_AC", 10.0)
        p_cap_dc = self.site_config.get("P_CAP_DC", 12.5)

        prompt_lines = []
        prompt_lines.append(f"=== DAY-AHEAD SOLAR GENERATION SCHEDULING MASTER PROMPT ===")
        prompt_lines.append(f"SITE ID: {site_id} | TARGET DATE (Day T+1): {target_date_str}")
        prompt_lines.append(f"SYSTEM CONSTRAINTS: AC Interconnect Capacity = {p_cap_ac:.2f} MW | DC Installed Capacity = {p_cap_dc:.2f} MWp\n")

        prompt_lines.append("SECTION 1: SYNOPTIC ATMOSPHERIC INDICATORS (Day T+1)")
        prompt_lines.append(f"• Cloud Cover Expected: {synoptic_indicators.get('cloud_cover_pct', 25):.1f}%")
        prompt_lines.append(f"• Convective CAPE: {synoptic_indicators.get('cape_j_kg', 150):.1f} J/kg")
        prompt_lines.append(f"• Relative Humidity: {synoptic_indicators.get('humidity_pct', 45):.1f}%")
        prompt_lines.append(f"• Aerosol Optical Depth (AOD): {synoptic_indicators.get('aod', 0.25):.2f}")
        prompt_lines.append(f"• Model Divergence Rating: {synoptic_indicators.get('divergence', 'LOW')}\n")

        prompt_lines.append("SECTION 2: 24h LEAD-TIME MOS ACCURACY & PORTFOLIO RANKINGS")
        for slot_name, info in mos_audit_info.items():
            champions = info.get("champions", [])
            prompt_lines.append(f"• {slot_name}: Top Selected Champions = {champions}")
        prompt_lines.append("")

        prompt_lines.append("SECTION 3: 96-BLOCK FORECAST COMPARISON MATRIX")
        prompt_lines.append("Block | Time Interval | ClearSky (MW) | MOS Consensus (MW)")
        prompt_lines.append("-" * 60)

        for b in range(1, 97):
            hour = (b - 1) * 15 // 60
            minute = ((b - 1) * 15) % 60
            time_str = f"{hour:02d}:{minute:02d}"
            prompt_lines.append(f"{b:02d}    | {time_str}           | {p_clearsky[b-1]:12.2f} | {p_mos_derated[b-1]:16.2f}")

        prompt_lines.append("\nSECTION 4: STRICT JSON OUTPUT SCHEMA REQUIREMENT")
        prompt_lines.append("Return ONLY a valid JSON object matching the following structure:")
        prompt_lines.append("""{
  "synoptic_regime": "CLEAR_SKY" | "HIGH_CIRRUS" | "MONSOON_OVERCAST" | "LOCAL_CONVECTIVE",
  "quantile_bias_factor": float, // in range [0.70, 1.08]
  "preferred_nwp_family": "ECMWF" | "ICON" | "GFS" | "GEM",
  "is_maintenance_outage": false,
  "reasoning": "Detailed meteorological reasoning for Day T+1 positioning...",
  "block_predictions": {
    "1": 0.0, ..., "96": 0.0
  }
}""")

        return "\n".join(prompt_lines)
