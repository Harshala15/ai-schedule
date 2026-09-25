"""strategic_arbiter.py

LLM Strategic Decision Layer for Renewable Energy Scheduling.

Role:
- Acts as Chief Meteorological Scheduling Strategist and DSM Risk Arbiter.
- Operates strictly on meta-decisions (Regime, Risk Quantile, Agency Weighting, Anomaly Discrimination).
- Does NOT predict raw megawatts (MW); mathematical generation is computed by IntellisEnsembleGTIAI.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from modules.llm import predictor


@dataclass
class StrategicAdvice:
    regime: str
    quantile_bias_factor: float
    preferred_agency: str
    is_trip_or_curtailment: bool
    reasoning: str
    block_predictions: dict[str, float] = field(default_factory=dict)
    source: str = "LLM_STRATEGIC_ARBITER"


class LLMStrategicArbiter:
    """Evaluates atmospheric conditions, SCADA telemetry, and DSM regulatory risk."""

    def __init__(self, plant_profile: Any = None):
        self.profile = plant_profile

    def get_strategic_guidance(
        self,
        target_date_str: str,
        target_time_str: str,
        weather_indicators: dict[str, Any] | None = None,
        live_telemetry: dict[str, Any] | None = None,
        next_12_blocks: list[dict[str, Any]] | None = None,
    ) -> StrategicAdvice:
        """Consults the LLM for strategic advice and 12-block prediction, with immediate deterministic fallback."""
        default_advice = StrategicAdvice(
            regime="CLEAR_SKY",
            quantile_bias_factor=1.0,
            preferred_agency="BALANCED",
            is_trip_or_curtailment=False,
            reasoning="Default physics baseline (deterministic mode).",
            block_predictions={},
            source="PHYSICS_BASELINE",
        )

        # Check if LLM is enabled
        enabled = os.getenv("ENABLE_LLM_STRATEGIC_ARBITER", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return default_advice

        api_keys = predictor._load_openrouter_api_keys()
        if not api_keys:
            return default_advice

        weather_indicators = weather_indicators or {}
        live_telemetry = live_telemetry or {}

        plant_name = getattr(self.profile, "plant_name", "SOLAR") if self.profile else "SOLAR"
        ac_cap = getattr(self.profile, "ac_capacity_mw", 10.0) if self.profile else 10.0
        ppa_rate = getattr(self.profile, "ppa_rate_inr_per_kwh", 5.0) if self.profile else 5.0
        tol_band = getattr(self.profile, "band_percentage", 0.15) if self.profile else 0.15

        blocks_formatted = ""
        if next_12_blocks:
            lines = []
            for b in next_12_blocks:
                lines.append(
                    f"  - Block {b['block']} ({b.get('time_interval', '')}): "
                    f"Physics Baseline = {float(b.get('predicted_mw', 0.0)):.2f} MW | "
                    f"GTI = {float(b.get('gti_wm2', 0.0)):.1f} W/m2"
                )
            blocks_formatted = "\n".join(lines)
        else:
            blocks_formatted = "  (No specific block list provided; recommend global bias factor)"

        is_non_meter = bool(live_telemetry.get("is_non_meter_site", False))
        telemetry_source = live_telemetry.get("telemetry_source", "PHYSICAL_SCADA")

        if is_non_meter:
            telemetry_section = f"""ESTIMATED SATELLITE TELEMETRY & MODEL RESIDUALS (Non-meter site / SCADA offline):
- Telemetry Source: {telemetry_source} (Physical SCADA meter is not present / offline)
- Current Generation Estimate: {live_telemetry.get('latest_mw', 'N/A')} MW
- Physics Baseline Forecast at this time: {live_telemetry.get('physics_predicted_mw', 'N/A')} MW
- Tracking Residual (Estimate - Physics Baseline): {live_telemetry.get('residual_mw', 'N/A')} MW
- Solar Elevation Angle: {live_telemetry.get('solar_elevation_deg', 0.0):.1f} deg
- Clearness Ratio (Kt): {live_telemetry.get('clearness_ratio', 1.0):.2f}"""
            trip_instruction = """- IMPORTANT: This is a designated NON-METER site. Zero or missing SCADA meter values DO NOT mean an electrical trip or plant outage.
- Set "is_trip_or_curtailment" strictly to false.
- DO NOT artificially pull down or flatten the midday solar schedule into a table-top shelf. Follow the natural convex parabolic physics baseline curve."""
        else:
            poa_line = f"\n- On-Site SCADA Pyranometer POA: {live_telemetry['live_poa_wm2']:.1f} W/m2" if live_telemetry.get("live_poa_wm2") is not None else ""
            telemetry_section = f"""LIVE SCADA TELEMETRY & MODEL RESIDUALS (at revision cutoff):
- Telemetry Source: {telemetry_source}
- Latest SCADA Meter Generation: {live_telemetry.get('latest_mw', 'N/A')} MW{poa_line}
- Physics Baseline Forecast at this time: {live_telemetry.get('physics_predicted_mw', 'N/A')} MW
- Real Clearness Ratio (Kt): {live_telemetry.get('clearness_ratio', 1.0):.2f}"""
            trip_instruction = """- PURE METEOROLOGICAL SCHEDULING (ENERCAST / INDUSTRY STANDARD):
  * Do NOT predict hardware inverter trips or equipment outages. The schedule strictly reflects available meteorological generation potential.
  * Set "is_trip_or_curtailment" strictly to false.

- DIRECTIONAL INTEGRITY RULES (PHYSICAL GUARDRAILS):
  * CLEAR-SKY PRESERVATION: If ground conditions are CLEAR (Kt >= 0.85, cloud cover <= 25%), DO NOT make negative downward cuts below the physics baseline! Clear-sky generation must follow the unobstructed physical solar curve.
  * OVERCAST BOUNDING: If cloud cover is high (>= 80%) or rain is active, DO NOT predict high clear-sky generation. Bound predictions strictly to the overcast diffuse envelope."""

        prompt = f"""You are the Chief Renewable Energy Scheduling Strategist and Forecaster for {plant_name} Solar Power Plant.
Target Date: {target_date_str} | Revision Time: {target_time_str}
Capacity: {ac_cap:.1f} MW AC | PPA Tariff: Rs {ppa_rate:.2f}/kWh | Tolerance Band: {tol_band * 100:.1f}% (+/- {ac_cap * tol_band:.2f} MW)

ATMOSPHERIC & WEATHER INDICATORS:
- Cloud Cover Mean: {weather_indicators.get('cloud_cover_pct', 'N/A')}%
- Maximum CAPE (Thunderstorm instability): {weather_indicators.get('cape_j_kg', 0)} J/kg
- Precipitation Forecast: {weather_indicators.get('precip_mm', 0.0)} mm
- 2m Ambient Temperature: {weather_indicators.get('temp_c', 28.0)} C

{telemetry_section}

NEXT 12 ACTIONABLE DISPATCH BLOCKS (Physics Baseline Anchor):
{blocks_formatted}

REGULATORY INCENTIVE & RISK INSTRUCTION:
Under Indian CERC/State DSM rules:
- Under-generation shortfall penalties are severe and punitive (up to 2x PPA tariff).
- Mild over-generation inside the tolerance band (0% to +{tol_band * 100:.0f}%) is safe or credit-earning.
{trip_instruction}

TASKS:
1. Classify the day's meteorological regime: ["CLEAR_SKY", "PARTLY_CLOUDY", "CONVECTIVE_MONSOON", "OVERCAST"].
2. Predict the generation (in MW) for EACH of the next 12 blocks in "block_predictions":
   - Use the physics baseline as the core anchor.
   - For non-meter sites, follow the physical solar irradiance curve without artificial plateauing or flatlining.
   - Adjust each block realistically:
     * If plant is over-performing (residual > 0) with high clearness (Kt >= 0.75), increase by 0.1 to 0.3 MW above baseline to capture higher actuals.
     * If plant is under-performing (residual < 0) or clouds are increasing, adjust down by 0.1 to 0.4 MW to avoid shortfall penalty.
   - Maintain a smooth physical solar ramp (no abrupt jumps > 0.5 MW between consecutive 15-min blocks).
   - Keep each block within physical bounds: 0.0 <= MW <= {ac_cap:.1f} MW.
3. Recommend preferred ensemble agency: ["BALANCED", "ECMWF", "ICON", "GEFS"].
4. Flag if live drop is equipment trip/curtailment vs actual clouds.

Respond ONLY with a JSON object in this exact schema:
{{
  "regime": "CLEAR_SKY",
  "quantile_bias_factor": 1.0,
  "preferred_agency": "BALANCED",
  "is_trip_or_curtailment": false,
  "reasoning": "Brief operational rationale for the 12-block prediction",
  "block_predictions": {{
    "55": 5.25,
    "56": 5.15
  }}
}}"""

        model_names = predictor._load_openrouter_model_names()
        max_retries = predictor._llm_max_retries()
        base_delay = predictor._llm_retry_base_delay_seconds()

        raw_response = ""
        for key_label, api_key in api_keys:
            raw_response, err = predictor._call_openrouter_with_key(
                api_key=api_key,
                key_label=key_label,
                prompt=prompt,
                max_retries=max_retries,
                base_delay=base_delay,
                model_names=model_names,
            )
            if raw_response:
                break

        if not raw_response:
            return default_advice

        try:
            # Clean possible markdown wrapping
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            data = json.loads(cleaned)

            regime = str(data.get("regime", "CLEAR_SKY")).upper().strip()
            is_trip = False  # Hardware trips are excluded from automated forecasting (Enercast alignment)

            bias = float(data.get("quantile_bias_factor", 1.0))
            if regime == "CLEAR_SKY" or float(live_telemetry.get("clearness_ratio", 1.0)) >= 0.85:
                # Clear-sky directional guardrail: forbid downward bias cuts below 1.0
                bias = max(1.0, min(1.08, bias))
            else:
                bias = max(0.60, min(1.08, bias))  # Sanity clamp allowing down to 0.60 for convective weather

            agency = str(data.get("preferred_agency", "BALANCED")).upper().strip()
            reasoning = str(data.get("reasoning", "")).strip()

            raw_preds = data.get("block_predictions", {})
            block_preds: dict[str, float] = {}
            if isinstance(raw_preds, dict):
                for k, v in raw_preds.items():
                    try:
                        val = float(v)
                        val = max(0.0, min(ac_cap, val))
                        block_preds[str(k).strip()] = round(val, 2)
                    except (ValueError, TypeError):
                        continue

            return StrategicAdvice(
                regime=regime,
                quantile_bias_factor=bias,
                preferred_agency=agency,
                is_trip_or_curtailment=is_trip,
                reasoning=reasoning,
                block_predictions=block_preds,
                source="LLM_STRATEGIC_ARBITER",
            )
        except Exception as parse_err:
            print(f"  [WARN] LLM Strategic Arbiter JSON parse error: {parse_err}; using default baseline.")
            return default_advice
