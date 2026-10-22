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
import config


@dataclass
class StrategicAdvice:
    regime: str
    quantile_bias_factor: float
    preferred_agency: str
    is_trip_or_curtailment: bool
    reasoning: str
    block_predictions: dict[str, float] = field(default_factory=dict)
    dispatch_advisory: dict[str, Any] = field(default_factory=dict)
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
        actionable_blocks: list[dict[str, Any]] | None = None,
    ) -> StrategicAdvice:
        """Consults the LLM for strategic advice and predictions up to 7:00 PM (Block 76), with immediate deterministic fallback."""
        default_advice = StrategicAdvice(
            regime="CLEAR_SKY",
            quantile_bias_factor=1.0,
            preferred_agency="BALANCED",
            is_trip_or_curtailment=False,
            reasoning="Default physics baseline (deterministic mode).",
            block_predictions={},
            source="PHYSICS_BASELINE",
        )

        weather_indicators = weather_indicators or {}
        live_telemetry = live_telemetry or {}

        plant_name = str(getattr(self.profile, "plant_name", getattr(config, "PLANT_NAME", "SOLAR"))).strip().upper()
        state = str(getattr(self.profile, "penalty_regulation", "State")).strip()
        ac_cap = float(getattr(self.profile, "ac_capacity_mw", getattr(config, "PLANT_CAPACITY_MW", 10.0)))
        ppa_rate = float(getattr(self.profile, "ppa_rate_inr_per_kwh", getattr(config, "PLANT_PPA_RATE_INR_PER_KWH", 5.0)))
        tol_band = float(getattr(self.profile, "band_percentage", getattr(config, "PLANT_TOLERANCE_BAND_PCT", 15.0) / 100.0))
        tol_mw = getattr(self.profile, "tolerance_band_mw", None)
        if tol_mw is None:
            tol_mw = round(ac_cap * tol_band, 3)
        else:
            tol_mw = float(tol_mw)

        cloud_pct = weather_indicators.get("cloud_cover_pct", 0) if weather_indicators else 0
        temp_c = weather_indicators.get("temp_c", 30.0) if weather_indicators else 30.0
        default_advice.dispatch_advisory = {
            "sky_condition": "Clear Sky" if cloud_pct < 25 else "Partly Cloudy",
            "cloud_risk": "LOW" if cloud_pct < 25 else ("HIGH" if cloud_pct >= 60 else "MEDIUM"),
            "generation_trend": "STABLE" if cloud_pct < 25 else "VARIABLE",
            "action_recommendation": f"Maintain physics baseline within +/-{tol_mw:.2f} MW tolerance band.",
            "advisory_text": (
                f"Currently, {plant_name} has {cloud_pct}% cloud cover at {temp_c:.1f}°C ambient temperature. "
                f"Over the upcoming daylight blocks after the effective time, skies will remain stable with no severe cloud build-up expected. "
                f"Generation will track the normal diurnal curve safely within {state}'s +/-{tol_band * 100:.1f}% (+/-{tol_mw:.2f} MW) tolerance band."
            ),
        }

        # Check if LLM is enabled
        enabled = os.getenv("ENABLE_LLM_STRATEGIC_ARBITER", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return default_advice

        api_keys = predictor._load_openrouter_api_keys()
        if not api_keys:
            return default_advice

        target_blocks = actionable_blocks if actionable_blocks is not None else (next_12_blocks or [])
        num_blocks = len(target_blocks)
        blocks_formatted = ""
        if target_blocks:
            lines = []
            for b in target_blocks:
                lines.append(
                    f"  - Block {b['block']} ({b.get('time_interval', '')}): "
                    f"Physics Baseline = {float(b.get('predicted_mw', 0.0)):.2f} MW | "
                    f"GTI = {float(b.get('gti_wm2', 0.0)):.1f} W/m2"
                )
            blocks_formatted = "\n".join(lines)
            start_b = target_blocks[0]['block']
            end_b = target_blocks[-1]['block']
            header_desc = f"ACTIONABLE DISPATCH BLOCKS UP TO 7:00 PM (Blocks {start_b} to {end_b} - {num_blocks} Blocks Total - Physics Baseline Anchor):"
            horizon_desc = f"over the actionable daylight horizon from Block {start_b} up to 7:00 PM (Block {end_b})"
            sample_blocks = f'"{start_b}": <predicted_mw_for_block_{start_b}>,\n    "{end_b}": <predicted_mw_for_block_{end_b}>'
        else:
            blocks_formatted = "  (No specific block list provided; recommend global bias factor)"
            header_desc = "ACTIONABLE DISPATCH BLOCKS (Physics Baseline Anchor):"
            horizon_desc = "over the actionable daylight horizon up to 7:00 PM"
            sample_blocks = '"55": 5.25,\n    "56": 5.15'

        advisory_task = (
            f"5. OPERATOR WEATHER ADVISORY ({plant_name}):\n"
            "   Write 'dispatch_advisory.advisory_text' as a continuous single paragraph of MAXIMUM THREE LINES in plain English.\n"
            "   It must seamlessly connect three things in continuous flow:\n"
            "   - Current weather condition at the site (temperature, cloud cover).\n"
            "   - Future weather condition after the effective revision time (incoming clouds, rain threat, or clear continuity).\n"
            f"   - Future generation impact (expected peak MW, trend like rising/steady/dropping) and scheduling advice under {state}'s +/-{tol_band * 100:.1f}% (+/-{tol_mw:.2f} MW) band.\n"
            "   Do NOT use bullet points or numbered lists; write exactly one coherent, continuous 2-to-3 sentence paragraph.\n"
        )
        advisory_schema = (
            ',\n  "dispatch_advisory": {\n'
            '    "sky_condition": "Clear Sky / Scattered Clouds / Overcast",\n'
            '    "cloud_risk": "LOW / MEDIUM / HIGH",\n'
            '    "generation_trend": "RISING / STABLE / DROPPING",\n'
            '    "action_recommendation": "ACTION: Submit schedule as generated; no manual cuts needed.",\n'
            f'    "advisory_text": "Currently, {plant_name} has clear skies with mild {temp_c:.1f}°C ambient temperature and low cloud cover ({cloud_pct}%). Over the afternoon after {target_time_str}, skies will remain mostly sunny with no convective clouds or rain approaching the site. As a result, generation will steadily rise to a peak across midday blocks, so schedulers should submit the full baseline schedule within the safe +/-{tol_mw:.2f} MW tolerance band."\n'
            '  }'
        )

        prompt = f"""You are the Chief Renewable Energy Meteorological Scheduling Strategist for {plant_name} Solar Power Plant.
Target Date: {target_date_str} | Revision Time: {target_time_str}
Capacity: {ac_cap:.1f} MW AC | PPA Tariff: Rs {ppa_rate:.2f}/kWh | Tolerance Band: {tol_band * 100:.1f}% (+/- {tol_mw:.2f} MW)

ATMOSPHERIC & WEATHER ENSEMBLE INDICATORS:
- Total Cloud Cover Mean: {weather_indicators.get('cloud_cover_pct', 'N/A')}%
- Low Cloud Cover: {weather_indicators.get('cloud_cover_low_pct', 'N/A')}%
- Maximum CAPE (Thunderstorm / Convective instability): {weather_indicators.get('cape_j_kg', 0)} J/kg
- Precipitation Forecast: {weather_indicators.get('precip_mm', 0.0)} mm
- 2m Ambient Temperature: {weather_indicators.get('temp_c', 28.0)} C

{header_desc}
{blocks_formatted}

PURE METEOROLOGICAL SCHEDULING & REGULATORY DSM RISK INSTRUCTIONS:
- You operate strictly on macro-atmospheric science and solar geometry {horizon_desc}. Do NOT assume or rely on short-term ground meter noise; immediate 30-minute electrical dispatch is already governed by deterministic SCADA logic.
- Under Indian CERC and State DSM Rules (Deviation Settlement Mechanism):
  * Allowed Safe Band: Deviations within +/- {tol_band * 100:.1f}% (+/- {tol_mw:.2f} MW) are safe and penalty-free.
  * Shortfall / Under-generation (< -{tol_mw:.2f} MW): Actual generation below schedule minus tolerance band incurs severe DSM shortfall penalties (up to 2x PPA tariff).
  * Over-generation / Surplus (> +{tol_mw:.2f} MW): Actual generation above schedule plus tolerance band receives ZERO PPA PAYMENT (energy is forfeited/uncompensated) AND incurs DSM over-generation deviation penalty charges.
  * Symmetrical Accuracy Mandate: Because both over-generation and under-generation beyond +/- {tol_mw:.2f} MW are heavily penalized, you must accurately target the true expected physical generation. Avoid artificial downward or upward skew.
- CLEAR-SKY PRESERVATION: If atmospheric indicators indicate CLEAR (cloud cover <= 25%), preserve the full convex solar arc at full capability (quantile_bias_factor = 1.00). DO NOT apply artificial downward cuts below the physics baseline!
- CONVECTIVE CLOUD POSITIONING: Under moderate/uncertain clouds (cloud cover 30-70% or CAPE > 1000 J/kg), adjust realistically based on diffuse irradiance while keeping predictions centered within the +/- {tol_mw:.2f} MW safe band.
- OVERCAST BOUNDING: If cloud cover >= 80% or rain is active, bound predictions strictly to the overcast diffuse envelope.

TASKS:
1. Classify the day's meteorological regime: ["CLEAR_SKY", "PARTLY_CLOUDY", "CONVECTIVE_MONSOON", "OVERCAST"].
2. Predict the generation (in MW) for EACH of the {num_blocks} blocks in "block_predictions":
   - Use the physics baseline as the core anchor.
   - Adjust realistically according to atmospheric cloudiness and convective risk.
   - Maintain a smooth physical solar diurnal arc (monotonically rising in the morning to solar noon, and decaying smoothly toward sunset without erratic sawtooth oscillation).
   - Keep each block within physical bounds: 0.0 <= MW <= {ac_cap:.1f} MW.
3. Recommend preferred ensemble agency: ["BALANCED", "ECMWF", "ICON", "GEFS"].
4. Set "is_trip_or_curtailment" to false (electrical trips are handled outside meteorology).
{advisory_task}
Respond ONLY with a JSON object in this exact schema:
{{
  "regime": "CLEAR_SKY",
  "quantile_bias_factor": 1.0,
  "preferred_agency": "BALANCED",
  "is_trip_or_curtailment": false,
  "reasoning": "Brief operational rationale for the prediction based on atmospheric indicators",
  "block_predictions": {{
    {sample_blocks}
  }}{advisory_schema}
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

            is_non_meter = bool((live_telemetry or {}).get("is_non_meter_site", False))
            bias = float(data.get("quantile_bias_factor", 1.0))
            if is_non_meter:
                # Tightly bounded risk envelope for non-meter sites (0.85 to 1.05) to ensure safety without SCADA feedback
                if regime == "CLEAR_SKY" or float(live_telemetry.get("clearness_ratio", 1.0)) >= 0.85:
                    bias = max(1.0, min(1.05, bias))
                else:
                    bias = max(0.85, min(1.05, bias))
            elif regime == "CLEAR_SKY" or float(live_telemetry.get("clearness_ratio", 1.0)) >= 0.85:
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
                        b_num = int(str(k).strip())
                        if not (1 <= b_num <= 96):
                            continue
                        val = float(v)
                        val = max(0.0, min(ac_cap, val))
                        block_preds[str(b_num)] = round(val, 2)
                    except (ValueError, TypeError):
                        continue

            dispatch_advisory: dict[str, Any] = {}
            raw_advisory = data.get("dispatch_advisory")
            if isinstance(raw_advisory, dict) and raw_advisory.get("advisory_text"):
                dispatch_advisory = {
                    "sky_condition": str(raw_advisory.get("sky_condition", regime.replace("_", " ").title())),
                    "cloud_risk": str(raw_advisory.get("cloud_risk", "LOW")).upper(),
                    "generation_trend": str(raw_advisory.get("generation_trend", "STABLE")).upper(),
                    "action_recommendation": str(raw_advisory.get("action_recommendation", "")),
                    "advisory_text": str(raw_advisory.get("advisory_text", "")),
                }
            else:
                trend = "rising smoothly toward midday peak" if cloud_pct < 30 else ("dropping due to cloud cover" if cloud_pct >= 60 else "stable")
                risk = "LOW" if cloud_pct < 30 else ("HIGH" if cloud_pct >= 60 else "MEDIUM")
                dispatch_advisory = {
                    "sky_condition": f"{regime.replace('_', ' ').title()}",
                    "cloud_risk": risk,
                    "generation_trend": "STABLE" if cloud_pct < 30 else ("DROPPING" if cloud_pct >= 60 else "VARIABLE"),
                    "action_recommendation": f"Maintain schedule baseline within +/-{tol_mw:.2f} MW tolerance band.",
                    "advisory_text": (
                        f"Currently, {plant_name} has {regime.replace('_', ' ').lower()} conditions with {temp_c:.1f}°C temperature and {cloud_pct}% cloud cover. "
                        f"Over upcoming daylight blocks after the effective time, skies will remain consistent with no severe storm development expected. "
                        f"Generation will be {trend}, allowing schedulers to maintain baseline within {state}'s +/-{tol_band * 100:.1f}% (+/-{tol_mw:.2f} MW) tolerance band."
                    ),
                }

            return StrategicAdvice(
                regime=regime,
                quantile_bias_factor=bias,
                preferred_agency=agency,
                is_trip_or_curtailment=is_trip,
                reasoning=reasoning,
                block_predictions=block_preds,
                dispatch_advisory=dispatch_advisory,
                source="LLM_STRATEGIC_ARBITER",
            )
        except Exception as parse_err:
            print(f"  [WARN] LLM Strategic Arbiter JSON parse error: {parse_err}; using default baseline.")
            return default_advice

    def get_wind_strategic_guidance(
        self,
        target_date_str: str,
        target_time_str: str,
        wind_indicators: dict[str, Any] | None = None,
        next_12_blocks: list[dict[str, Any]] | None = None,
    ) -> StrategicAdvice:
        """Consults the LLM for wind strategic advice and next-12-block predictions with immediate deterministic fallback."""
        default_advice = StrategicAdvice(
            regime="STEADY_WIND",
            quantile_bias_factor=1.0,
            preferred_agency="BALANCED",
            is_trip_or_curtailment=False,
            reasoning="Default aero-dynamic wind physics baseline.",
            block_predictions={},
            source="WIND_PHYSICS_BASELINE",
        )

        enabled = os.getenv("ENABLE_LLM_STRATEGIC_ARBITER", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return default_advice

        is_disabled, _ = predictor._is_llm_disabled_for_plant()
        if is_disabled:
            return default_advice

        api_keys = predictor._load_openrouter_api_keys()
        if not api_keys:
            return default_advice

        wind_indicators = wind_indicators or {}
        plant_name = str(getattr(self.profile, "plant_name", getattr(config, "PLANT_NAME", "WIND"))).strip().upper()
        rated_cap = float(
            getattr(self.profile, "rated_capacity_mw", None)
            or getattr(self.profile, "capacity_mw", None)
            or getattr(config, "PLANT_CAPACITY_MW", 10.0)
        )
        ppa_rate = float(
            getattr(self.profile, "ppa_rate_inr_per_kwh", None)
            or getattr(config, "PLANT_PPA_RATE_INR_PER_KWH", 4.0)
        )
        tol_band_mw = float(
            getattr(self.profile, "tolerance_band_mw", None)
            or getattr(config, "PLANT_TOLERANCE_BAND_MW", round(rated_cap * 0.10, 3))
        )
        tol_band_pct = float(
            getattr(self.profile, "band_percentage", None)
            or (getattr(config, "PLANT_TOLERANCE_BAND_PCT", 10.0) / 100.0)
        )
        hub_h = float(getattr(self.profile, "hub_height_m", 100.0))
        turbine_model = str(getattr(self.profile, "turbine_model", "Standard Wind Turbine"))
        n_turb = int(getattr(self.profile, "num_turbines", 1))

        wind_speed = wind_indicators.get("mean_wind_speed", 0.0)
        turb_risk = str(wind_indicators.get("turbulence_risk", "LOW")).upper()
        temp_c = float(wind_indicators.get("temp_c", 26.0))
        default_advice.dispatch_advisory = {
            "sky_condition": "Wind Aerodynamic",
            "cloud_risk": turb_risk,
            "generation_trend": "STABLE",
            "action_recommendation": f"Maintain wind aerodynamic baseline within +/-{tol_band_mw:.2f} MW band.",
            "advisory_text": (
                f"Currently, {plant_name} has hub wind speeds averaging {wind_speed} m/s at {temp_c:.1f}°C ambient temperature. "
                f"Over upcoming dispatch blocks after the effective time, winds will remain consistent with {turb_risk.lower()} turbulence risk. "
                f"Generation will track aerodynamic curve safely within +/-{tol_band_mw:.2f} MW tolerance band."
            ),
        }

        blocks_formatted = ""
        if next_12_blocks:
            lines = []
            for b in next_12_blocks:
                lines.append(
                    f"  - Block {b['block']} ({b.get('time_interval', '')}): "
                    f"Physics Baseline = {float(b.get('schedule_mw', b.get('intellis_mw', 0.0))):.2f} MW | "
                    f"Hub Wind Speed = {float(b.get('wind_speed_hub_m_s', 0.0)):.2f} m/s | "
                    f"Air Density = {float(b.get('air_density_kg_m3', 1.15)):.3f} kg/m3"
                )
            blocks_formatted = "\n".join(lines)
        else:
            blocks_formatted = "  (No specific block list provided; recommend global bias factor)"

        wind_advisory_task = (
            f"5. OPERATOR WEATHER ADVISORY ({plant_name}):\n"
            "   Write 'dispatch_advisory.advisory_text' as a continuous single paragraph of MAXIMUM THREE LINES in plain English.\n"
            "   It must seamlessly connect three things in continuous flow:\n"
            "   - Current wind and weather condition at the site (wind speed, temperature, turbulence).\n"
            "   - Future wind trend after the effective revision time (increasing breeze, thermal drops, or gust risk).\n"
            f"   - Future generation impact (expected MW trend like rising/steady/dropping) and scheduling advice under +/-{tol_band_mw:.2f} MW band.\n"
            "   Do NOT use bullet points or numbered lists; write exactly one coherent, continuous 2-to-3 sentence paragraph.\n"
        )
        wind_advisory_schema = (
            ',\n  "dispatch_advisory": {\n'
            '    "sky_condition": "Wind Aerodynamic",\n'
            '    "cloud_risk": "LOW / MEDIUM / HIGH",\n'
            '    "generation_trend": "RISING / STABLE / DROPPING",\n'
            f'    "action_recommendation": "ACTION: Maintain baseline within +/-{tol_band_mw:.2f} MW tolerance band.",\n'
            f'    "advisory_text": "Currently, {plant_name} has steady hub wind speeds near {wind_speed} m/s at {temp_c:.1f}°C. Over upcoming dispatch blocks after {target_time_str}, wind velocities are projected to remain steady with low turbulence risk. Generation will follow the aerodynamic curve within the +/-{tol_band_mw:.2f} MW tolerance band."\n'
            '  }'
        )

        prompt = f"""You are the Chief Renewable Energy Meteorological Scheduling Strategist for {plant_name} Wind Power Plant.
Target Date: {target_date_str} | Revision Time: {target_time_str}
Capacity: {rated_cap:.1f} MW Rated | PPA Tariff: Rs {ppa_rate:.2f}/kWh | Tolerance Band: {tol_band_pct * 100:.1f}% (+/- {tol_band_mw:.2f} MW)
Turbines: {n_turb} units ({turbine_model}) | Hub Height: {hub_h}m

ATMOSPHERIC & WIND ENSEMBLE DYNAMICS:
- Mean Hub Wind Speed: {wind_indicators.get('mean_wind_speed', 'N/A')} m/s
- Mean Air Density: {wind_indicators.get('mean_air_density', 1.15)} kg/m3
- Surface Temperature: {wind_indicators.get('temp_c', 26.0)} C
- Gust Factor / Turbulence Risk: {wind_indicators.get('turbulence_risk', 'LOW')}

NEXT 12 ACTIONABLE DISPATCH BLOCKS (Physics Aero-Power Baseline):
{blocks_formatted}

DSM RISK & WIND SCHEDULING INSTRUCTIONS:
- You operate strictly on atmospheric wind shear, boundary-layer turbulence, and DSM penalty minimization over a 3-hour horizon.
- Under Indian CERC and State DSM Rules (Deviation Settlement Mechanism):
  * Allowed Safe Band: Deviations within +/- {tol_band_pct * 100:.1f}% (+/- {tol_band_mw:.2f} MW) are safe and penalty-free.
  * Shortfall / Under-generation (< -{tol_band_mw:.2f} MW): Actual generation below schedule minus tolerance band incurs severe shortfall penalties (up to 2x PPA tariff).
  * Over-generation / Surplus (> +{tol_band_mw:.2f} MW): Actual generation above schedule plus tolerance band receives ZERO PAYMENT (energy is forfeited for free) AND incurs DSM deviation penalty charges.
  * Symmetrical Accuracy Mandate: Both over-generation and under-generation beyond +/- {tol_band_mw:.2f} MW are penalized. Keep forecasts centered on expected aero-power output without artificial downward skew.
- When wind speeds hover near cut-in velocity (3.0 m/s), maintain conservative positioning to prevent severe shortfall penalties from calm dropouts.
- When wind speeds are steady and moderate (6 to 11 m/s), preserve the aerodynamic power curve (quantile_bias_factor centered near 1.00).
- Ensure smooth aerodynamic ramp rates (avoid abrupt jumps > {max(1.0, rated_cap * 0.15):.1f} MW between consecutive 15-minute blocks).
- Keep each block within physical bounds: 0.0 <= MW <= {rated_cap:.1f} MW.

TASKS:
1. Classify the wind atmospheric regime: ["STEADY_HIGH_WIND", "DIURNAL_THERMAL_BREEZE", "GUSTY_TURBULENT", "LOW_WIND_CUTIN_RISK"].
2. Predict the generation (in MW) for EACH of the next 12 blocks in "block_predictions".
3. Recommend quantile_bias_factor (between 0.85 and 1.10).
4. Provide operational reasoning.
{wind_advisory_task}
Respond ONLY with a JSON object in this exact schema:
{{
  "regime": "STEADY_HIGH_WIND",
  "quantile_bias_factor": 1.0,
  "preferred_agency": "BALANCED",
  "is_trip_or_curtailment": false,
  "reasoning": "Operational meteorological rationale for wind generation forecast",
  "block_predictions": {{
    "58": 24.50,
    "59": 25.10
  }}{wind_advisory_schema}
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
            cleaned = raw_response.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            data = json.loads(cleaned)

            regime = str(data.get("regime", "STEADY_HIGH_WIND")).upper().strip()
            bias = float(data.get("quantile_bias_factor", 1.0))
            bias = max(0.70, min(1.15, bias))  # Sanity clamp for wind

            agency = str(data.get("preferred_agency", "BALANCED")).upper().strip()
            reasoning = str(data.get("reasoning", "")).strip()

            raw_preds = data.get("block_predictions", {})
            block_preds: dict[str, float] = {}
            if isinstance(raw_preds, dict):
                for k, v in raw_preds.items():
                    try:
                        b_num = int(str(k).strip())
                        if not (1 <= b_num <= 96):
                            continue
                        val = float(v)
                        val = max(0.0, min(rated_cap, val))
                        block_preds[str(b_num)] = round(val, 2)
                    except (ValueError, TypeError):
                        continue

            wind_dispatch_advisory: dict[str, Any] = {}
            raw_advisory = data.get("dispatch_advisory")
            if isinstance(raw_advisory, dict) and raw_advisory.get("advisory_text"):
                wind_dispatch_advisory = {
                    "sky_condition": str(raw_advisory.get("sky_condition", "Wind Aerodynamic")),
                    "cloud_risk": str(raw_advisory.get("cloud_risk", turb_risk)).upper(),
                    "generation_trend": str(raw_advisory.get("generation_trend", "STABLE")).upper(),
                    "action_recommendation": str(raw_advisory.get("action_recommendation", "")),
                    "advisory_text": str(raw_advisory.get("advisory_text", "")),
                }
            else:
                wind_dispatch_advisory = {
                    "sky_condition": "Wind Aerodynamic",
                    "cloud_risk": turb_risk,
                    "generation_trend": "STABLE",
                    "action_recommendation": f"Maintain baseline within +/-{tol_band_mw:.2f} MW band.",
                    "advisory_text": (
                        f"Currently, {plant_name} has hub wind speeds averaging {wind_speed} m/s at {temp_c:.1f}°C. "
                        f"Over upcoming dispatch blocks after the effective time, winds will remain consistent with {turb_risk.lower()} turbulence risk. "
                        f"Generation will track aerodynamic curve safely within +/-{tol_band_mw:.2f} MW tolerance band."
                    ),
                }

            return StrategicAdvice(
                regime=regime,
                quantile_bias_factor=bias,
                preferred_agency=agency,
                is_trip_or_curtailment=False,
                reasoning=reasoning,
                block_predictions=block_preds,
                dispatch_advisory=wind_dispatch_advisory,
                source="WIND_LLM_STRATEGIC_ARBITER",
            )
        except Exception as parse_err:
            print(f"  [WARN] Wind LLM Strategic Arbiter JSON parse error: {parse_err}; using default baseline.")
            return default_advice
