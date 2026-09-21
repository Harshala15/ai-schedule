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
from dataclasses import dataclass
from typing import Any

from modules.llm import predictor


@dataclass
class StrategicAdvice:
    regime: str
    quantile_bias_factor: float
    preferred_agency: str
    is_trip_or_curtailment: bool
    reasoning: str
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
    ) -> StrategicAdvice:
        """Consults the LLM for strategic advice, with immediate deterministic fallback."""
        default_advice = StrategicAdvice(
            regime="CLEAR_SKY",
            quantile_bias_factor=1.0,
            preferred_agency="BALANCED",
            is_trip_or_curtailment=False,
            reasoning="Default physics baseline (deterministic mode).",
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

        prompt = f"""You are the Chief Renewable Energy Scheduling Strategist for {plant_name} Solar Power Plant.
Target Date: {target_date_str} | Revision Time: {target_time_str}
Capacity: {ac_cap:.1f} MW AC | PPA Tariff: Rs {ppa_rate:.2f}/kWh | Tolerance Band: {tol_band * 100:.1f}%

ATMOSPHERIC & WEATHER INDICATORS:
- Cloud Cover Mean: {weather_indicators.get('cloud_cover_pct', 'N/A')}%
- Maximum CAPE (Thunderstorm instability): {weather_indicators.get('cape_j_kg', 0)} J/kg
- Precipitation Forecast: {weather_indicators.get('precip_mm', 0.0)} mm
- 2m Ambient Temperature: {weather_indicators.get('temp_c', 28.0)} C

LIVE SCADA TELEMETRY (up to revision time):
- Latest Meter Generation: {live_telemetry.get('latest_mw', 'N/A')} MW
- Solar Elevation Angle: {live_telemetry.get('solar_elevation_deg', 0.0):.1f} deg
- Ground Pyranometer POA: {live_telemetry.get('pyranometer_poa_wm2', 'N/A')} W/m2
- Inverter Wake-up / Clearness Ratio: {live_telemetry.get('clearness_ratio', 1.0):.2f}

REGULATORY INCENTIVE & RISK INSTRUCTION:
Under Indian CERC/State DSM rules:
- Under-generation shortfall penalties are severe and punitive (up to 2x PPA tariff).
- Mild over-generation inside the tolerance band (0% to +{tol_band * 100:.0f}%) is safe or credit-earning.
- If live meter is 0.0 MW while ground pyranometer POA is high (>400 W/m2), flag an electrical trip/curtailment (NOT weather cloud).

TASKS:
1. Classify the day's meteorological regime: ["CLEAR_SKY", "PARTLY_CLOUDY", "CONVECTIVE_MONSOON", "OVERCAST"].
2. Recommend an asymmetric quantile positioning factor:
   - 1.00 = standard unbiased median.
   - 0.95 to 0.98 = conservative risk shield (position slightly lower to avoid punitive shortfall penalties on volatile days).
   - 1.02 to 1.05 = aggressive high-insolation capture.
3. Recommend preferred ensemble agency: ["BALANCED", "ECMWF", "ICON", "GEFS"].
4. Flag if live drop is equipment trip/curtailment vs actual clouds.

Respond ONLY with a JSON object in this exact schema:
{{
  "regime": "CLEAR_SKY",
  "quantile_bias_factor": 1.0,
  "preferred_agency": "BALANCED",
  "is_trip_or_curtailment": false,
  "reasoning": "Brief 1-2 sentence operational rationale"
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
            bias = float(data.get("quantile_bias_factor", 1.0))
            bias = max(0.85, min(1.08, bias))  # Sanity clamp

            agency = str(data.get("preferred_agency", "BALANCED")).upper().strip()
            is_trip = bool(data.get("is_trip_or_curtailment", False))
            reasoning = str(data.get("reasoning", "")).strip()

            return StrategicAdvice(
                regime=regime,
                quantile_bias_factor=bias,
                preferred_agency=agency,
                is_trip_or_curtailment=is_trip,
                reasoning=reasoning,
                source="LLM_STRATEGIC_ARBITER",
            )
        except Exception as parse_err:
            print(f"  [WARN] LLM Strategic Arbiter JSON parse error: {parse_err}; using default baseline.")
            return default_advice
