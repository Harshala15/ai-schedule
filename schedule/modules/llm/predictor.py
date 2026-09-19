"""
llm_predictor.py

The ONLY module in this pipeline that calls an LLM. Its job is narrow
and constrained on purpose: given a Step 1 scaffold for each of the
next 12 forecast blocks, plus the most similar past situations (from
similarity_retrieval.py) and their real outcomes, ask the LLM to
ADJUST each block and explain why.

WHY THIS DESIGN (vs asking the LLM to just "predict the generation"):
    - The Step 1 scaffold keeps every prediction grounded in the
      current meter state even if the LLM's adjustment is unhelpful.
    - Retrieved similar cases give the LLM concrete historical evidence
      ("in similar cloud conditions, actual generation was X% lower/
      higher than this formula predicted") instead of vague reasoning.
    - A single, small, structured JSON response is far more reliable to
      parse and validate than asking for 8 independent numbers with no
      anchor to sanity-check against.

If the LLM call fails entirely (network, rate limit, bad JSON), this
module falls back to the scaffold values unchanged -- the pipeline never
produces no output just because the LLM step had a problem.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

import config


def _plant_env_suffix() -> str:
    plant = (config.PLANT_NAME or "").strip().upper()
    return f"_{plant}" if plant else ""


def _llm_chunk_size(anchor_predictions: list) -> int:
    """Return how many blocks to send in one LLM request.

    All plants are handled as a single request per run so the model can
    adjust the entire remaining horizon in one pass.
    """
    return max(1, len(anchor_predictions))




def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _is_llm_disabled_for_plant() -> tuple[bool, str]:
    """Check if LLM execution is explicitly disabled for the active plant.

    Hard Mandate: For JEWLI wind power plant, do NOT use LLM under any circumstances
    until the user explicitly commands it via USE_LLM_JEWLI=true and ENABLE_JEWLI_LLM=true.
    """
    plant = (getattr(config, "PLANT_NAME", "") or os.getenv("PLANT_NAME", "") or os.getenv("SITE_ID", "")).strip().upper()

    # Strict Guardrail: Do NOT use LLM for Jewli until explicitly instructed
    if plant == "JEWLI":
        allow_jewli = _env_flag("USE_LLM_JEWLI", "false") and _env_flag("ENABLE_JEWLI_LLM", "false")
        if not allow_jewli:
            return True, "JEWLI"
        return False, ""

    if config.is_wind_plant(plant):
        if not _env_flag("USE_LLM_FOR_WIND", "false"):
            return True, plant or "WIND"

    return False, ""

def _llm_max_retries() -> int:
    """Return max OpenRouter HTTP attempts per key/model for one schedule run."""
    raw = os.getenv("OPENROUTER_MAX_RETRIES", "1").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _llm_retry_base_delay_seconds() -> int:
    raw = os.getenv("OPENROUTER_RETRY_BASE_DELAY_SECONDS", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _load_openrouter_api_keys() -> list[tuple[str, str]]:
    """Return OpenRouter API keys in priority order."""
    keys: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(source: str, value: str) -> None:
        value = (value or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        keys.append((source, value))

    for env_name in (
        f"OPENROUTER_API_KEYS{_plant_env_suffix()}",
        "OPENROUTER_API_KEYS",
    ):
        raw_group = os.getenv(env_name, "").strip()
        if raw_group:
            for index, candidate in enumerate(re.split(r"[,\n;]+", raw_group), start=1):
                _add(f"{env_name}[{index}]", candidate)
            break

    plant_key_name = f"OPENROUTER_API_KEY{_plant_env_suffix()}"
    _add(plant_key_name, os.getenv(plant_key_name, ""))
    _add("OPENROUTER_API_KEY", os.getenv("OPENROUTER_API_KEY", getattr(config, "OPENROUTER_API_KEY", "")))
    return keys


def _load_openrouter_model_names() -> list[str]:
    """Return OpenRouter model candidates in priority order (defaulting strictly to openai/gpt-5.6-luna)."""
    raw = os.getenv(f"OPENROUTER_MODEL_CANDIDATES{_plant_env_suffix()}", "").strip() or os.getenv("OPENROUTER_MODEL_CANDIDATES", "").strip() or getattr(config, "OPENROUTER_MODEL_CANDIDATES", "")
    if raw:
        models = [item.strip() for item in re.split(r"[,\n;]+", raw) if item.strip()]
        if models:
            return models
    single = os.getenv(f"OPENROUTER_MODEL{_plant_env_suffix()}", "").strip() or getattr(config, "OPENROUTER_MODEL", "")
    if single:
        return [single]
    return ["openai/gpt-5.6-luna"]


def _call_openrouter_with_key(
    api_key: str,
    key_label: str,
    prompt: str,
    max_retries: int,
    base_delay: int,
    model_names: list[str],
) -> tuple[str, Exception | None]:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ai-forecasting-system",
        "X-Title": "AI Solar Forecaster",
        "User-Agent": "AISolarForecaster/2.0",
    }

    for attempt in range(1, max_retries + 1):
        last_model_error = None
        for model_name in model_names:
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an expert solar power grid dispatch scheduling model. Output strictly valid JSON without extra markdown formatting.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            }
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    content = resp_data["choices"][0]["message"]["content"]
                    return (content or "").strip(), None
            except urllib.error.HTTPError as http_err:
                err_body = ""
                try:
                    err_body = http_err.read().decode("utf-8")
                except Exception:
                    pass
                last_model_error = RuntimeError(f"OpenRouter HTTP {http_err.code} for {model_name}: {http_err.reason} - {err_body}")
                print(f"  [WARN] OpenRouter HTTP error for {key_label} ({model_name}): {last_model_error}")
                if http_err.code in (404, 400):
                    continue
                if http_err.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"  [WARN] OpenRouter transient error ({http_err.code}) for {key_label}. Retrying in {delay}s...")
                    time.sleep(delay)
                    break
                return "", last_model_error
            except Exception as ex:
                last_model_error = ex
                print(f"  [WARN] OpenRouter error for {key_label} ({model_name}): {ex}")
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    print(f"  Retrying in {delay}s...")
                    time.sleep(delay)
                    break
                return "", last_model_error
        if last_model_error is not None and attempt == max_retries:
            return "", last_model_error

    return "", last_model_error


def _call_llm_text(
    prompt: str,
    vision_parts: list | None = None,
    max_retries: int = 4,
    base_delay: int = 5,
) -> tuple[str, Exception | None]:
    """Execute LLM call using OpenRouter GPT-5.6 Luna."""
    openrouter_keys = _load_openrouter_api_keys()
    if not openrouter_keys:
        return "", RuntimeError("No OpenRouter API keys configured.")

    if not _env_flag("OPENROUTER_ALLOW_KEY_FALLBACK"):
        openrouter_keys = openrouter_keys[:1]

    model_names = _load_openrouter_model_names()
    if not _env_flag("OPENROUTER_ALLOW_MODEL_FALLBACK"):
        model_names = model_names[:1]

    last_error = None
    for key_index, (key_label, api_key) in enumerate(openrouter_keys, start=1):
        raw_text, err = _call_openrouter_with_key(
            api_key=api_key,
            key_label=key_label,
            prompt=prompt,
            max_retries=max_retries,
            base_delay=base_delay,
            model_names=model_names,
        )
        if raw_text:
            return raw_text, None
        last_error = err
        if key_index < len(openrouter_keys):
            print(f"  [WARN] Switching from {key_label} to next configured OpenRouter key.")

    return "", last_error


def _summarize_current_situation(feature_row: dict) -> str:
    """
    Builds a short, human-readable summary of the CURRENT feature row for
    the prompt -- deliberately NOT dumping all 30+ raw feature values, to
    keep the prompt small, cheap, and easy for the LLM to reason over.
    Only the features that are actually meaningful to a person (and to
    the LLM's reasoning) are included.
    """
    lines = []
    elevation = feature_row.get("solar_elevation_deg")
    if elevation is not None:
        lines.append(f"Solar elevation: {elevation} deg")

    # Inverted real-time ground-truth irradiance and clearness index
    meter_mw = feature_row.get("latest_mw", feature_row.get("active_power_mw"))
    gti_meter = feature_row.get("meter_gti_wm2")
    kt_meter = feature_row.get("meter_kt")
    is_clipping = feature_row.get("is_inverter_clipped", False)

    if meter_mw is not None:
        lines.append(f"Latest SCADA meter generation: {meter_mw:.3f} MW")
    if gti_meter is not None:
        lines.append(f"Inverted real ground-truth POA irradiance: {gti_meter:.1f} W/m²")
    if kt_meter is not None:
        lines.append(f"Live ground clearness index (Kt = Actual / ClearSky): {kt_meter:.3f}")
    if is_clipping:
        lines.append("CRITICAL: Plant is currently in INVERTER AC SATURATION / CLIPPING (generation at or near AC ceiling). Real irradiance exceeds inverter limits -- do NOT cut schedule for perceived flatline!")

    direction_deg = feature_row.get("motion_direction_deg")
    if direction_deg is not None:
        lines.append(f"Cloud motion direction (degrees, -1 = stationary/negligible): {direction_deg}")

    motion_score = feature_row.get("motion_score", feature_row.get("motion_speed_kmh"))
    if motion_score is not None:
        lines.append(f"Relative cloud-motion score: {motion_score} (not physical km/h)")

    coverage_end = feature_row.get("motion_coverage_end_pct")
    if coverage_end is not None:
        lines.append(f"Cloud coverage over plant (video-derived): {coverage_end}%")

    for layer in ("clouds", "satellite", "rain", "solarpower", "wind"):
        brightness_key = f"{layer}_bright_pixel_pct"
        if brightness_key in feature_row and feature_row[brightness_key] is not None:
            lines.append(f"{layer.capitalize()} layer bright-pixel %: {feature_row[brightness_key]}")

    slot_diag = feature_row.get("slot_candidates_diagnostics")
    if slot_diag and isinstance(slot_diag, dict):
        lines.append(f"\n--- TOP SLOT-RANKED MODELS (Diurnal Slot: {str(slot_diag.get('current_slot', 'N/A')).upper()}) ---")
        lines.append(f"Live Inverted Ground POA: {slot_diag.get('live_actual_poa_wm2', 0.0)} W/m² | ClearSky POA: {slot_diag.get('clearsky_poa_wm2', 0.0)} W/m²")
        for c in slot_diag.get("candidates", []):
            lines.append(
                f"  • {c.get('family', 'MODEL')} ({c.get('model_key', '')}): 60-min live bias = {c.get('last_60min_bias_wm2', 0.0):+.1f} W/m² | "
                f"Base 7-day weight = {c.get('base_weight', 0.20):.2f} | Next blocks GTI = {c.get('forecast_gti_next_blocks', [])[:4]}"
            )

    return "\n".join(lines) if lines else "(no readable feature summary available)"


def _build_prompt(anchor_predictions: list, feature_row: dict, retrieved_cases_text: str,
                   context_text: str, intraday_actuals_text: str = "",
                   intraday_state_text: str = "", weather_text: str = "",
                   video_text: str = "", prompt_subject: str = "base forecast") -> str:
    blocks_text = "\n".join(
        f"{i + 1}. time={p['time']}, base_mw={p['anchor_mw']}"
        for i, p in enumerate(anchor_predictions)
    )

    sections = []
    if retrieved_cases_text.strip():
        sections.append(f"Similar historical cases:\n{retrieved_cases_text.strip()}")
    if context_text.strip():
        sections.append(f"Recent context:\n{context_text.strip()}")
    step2_parts = []
    if video_text.strip():
        step2_parts.append(
            "Windy video OpenCV features for the same revision window:\n"
            f"{video_text.strip()}"
        )
    if weather_text.strip():
        step2_parts.append(
            "ECMWF / Open-Meteo weather forecast for the next revision horizon:\n"
            f"{weather_text.strip()}"
        )
    if step2_parts:
        sections.append("STEP 2 -- video + weather adjustment evidence:\n" + "\n\n".join(step2_parts))
    if intraday_actuals_text.strip():
        sections.append(
            "STEP 3 -- today's own actual generation so far (STRONGEST evidence, when given):\n"
            f"{intraday_actuals_text.strip()}"
        )
    if intraday_state_text.strip():
        sections.append(
            "STEP 4 -- live same-day regime / fluctuation summary:\n"
            f"{intraday_state_text.strip()}"
        )

    plant_name = str(getattr(config, "PLANT_NAME", "PLANT")).upper()
    prof_dict = getattr(config, "PLANT_PROFILE", {}) or {}
    cap_mw = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
    band_pct = float(prof_dict.get("band_percentage", 0.10 if plant_name == "JEWLI" else 0.15))
    tol_mw = round(cap_mw * band_pct, 2)
    reg_name = str(prof_dict.get("penalty_regulation", "Maharashtra (MERC)" if plant_name == "JEWLI" else "State CERC"))
    tariff_val = float(prof_dict.get("ppa_rate_inr_per_kwh", 3.275 if plant_name == "JEWLI" else 5.65))

    is_wind = getattr(config, "is_wind_plant", lambda p: False)(plant_name)

    if is_wind:
        rules_and_format = f"""CRITICAL RULES FOR WIND GRID ACCURACY & MERC PENALTY MINIMIZATION:
1. Two-Sided Penalty Band Mandate (+-{band_pct*100:.0f}% of Capacity = +-{tol_mw} MW):
   - Under {reg_name} regulations for {plant_name}, deviations within +-{band_pct*100:.0f}% of plant capacity (+-{tol_mw} MW) carry ZERO penalty.
   - BOTH Over-forecasting (>+{tol_mw} MW) and Under-forecasting (<-{tol_mw} MW) trigger severe financial deviation penalties (PPA tariff Rs. {tariff_val:.3f}/kWh).
   - ZERO-PENALTY SAFE CORRIDOR: Keep forecasts strictly within [Actual - {tol_mw} MW, Actual + {tol_mw} MW].
   - DO NOT excessively haircut generation into severe under-forecasting (<-{tol_mw} MW).
   - DO NOT inflate generation into severe over-forecasting (>+{tol_mw} MW).
2. 24-Hour Continuous Operation & Nocturnal Low-Level Jet (NO NIGHT ZEROING):
   - Wind power is continuous 24/7. NEVER zero out night forecast blocks.
   - Nocturnal LLJ (20:00 - 05:30): Strongest winds of the day; generation is high (50.0 to 95.0 MW).
   - Morning Thermal Decoupling (05:30 - 08:30): Solar heating erodes inversion; generation descends from ~70 MW to ~15 MW.
   - Daytime Thermal Lull (08:30 - 18:15): Convective mixing dampens winds; output is 0.5 to 7.0 MW.
   - Evening Transition Ramp (18:15 - 19:45): Rapid surge jumping from ~4 MW to >65 MW in 30-45 minutes. Align ramp timing precisely to avoid crossing the +-{tol_mw} MW corridor.
3. Hub-Height Aerodynamics (133.5m) & Power Curve Response:
   - 28x Siemens Gamesa SG 3.6-145 (Rated 11.5 m/s, Cut-in 3.0 m/s, Cut-out 25.0 m/s).
   - Wind power rises cubically between 3.0 and 11.5 m/s, and plateaus at rated capacity 100.8 MW.
   - Surface winds underestimate hub-height winds due to vertical shear.
4. Ramp Smoothness & Telemetry Anchor:
   - Anchor the earliest forecast blocks tightly to latest SCADA meter telemetry.
   - Prevent unrealistic sawtooth jumping between adjacent 15-minute blocks.

Return ONLY raw JSON, no markdown or prose.
Array size must be exactly {len(anchor_predictions)}.
Each object must contain:
- "time"
- "adjusted_mw": final schedule generation in MW
- "confidence": High, Medium, or Low
- "reasoning": physical justification referencing MERC +-{tol_mw} MW corridor

Example:
[{{"time":"2026-09-18 20:30","adjusted_mw":64.50,"confidence":"High","reasoning":"Nocturnal LLJ strengthening aligned with 133.5m ECMWF wind speed, centered comfortably inside MERC +-{tol_mw} MW safe corridor."}}]"""
    else:
        rules_and_format = f"""CRITICAL RULES FOR GRID ACCURACY & PENALTY MINIMIZATION:
1. Two-Sided Penalty Band Mandate (+-{band_pct*100:.0f}% of Capacity = +-{tol_mw} MW):
   - Under {reg_name} regulations for {plant_name}, deviations within +-{band_pct*100:.0f}% of plant capacity (+-{tol_mw} MW) carry ZERO penalty.
   - BOTH Over-forecasting (>+{band_pct*100:.0f}%) and Under-forecasting (<-{band_pct*100:.0f}%) trigger severe financial deviation penalties (at tariff Rs. {tariff_val:.2f}/kWh).
   - DO NOT excessively haircut generation into severe under-forecasting (<-{band_pct*100:.0f}%). Maintain schedules strictly within the safe [Actual - {tol_mw} MW, Actual + {tol_mw} MW] corridor.
   - During clear sky / steady wind conditions, never deviate wildly from physical base. Attenuate smoothly without over-slashing.
2. Inverter AC Clipping Protection:
   - When plant generation reaches >= 0.88 * AC Capacity (e.g. at solar noon), the plant is in INVERTER SATURATION.
   - Flatlining generation at midday is NOT cloud attenuation; real irradiance is >= 850 W/m².
   - Set Kt = 1.00 and maintain the full plateau; NEVER slash generation during solar noon due to flat SCADA readings.
3. Multi-NWP Model Consensus Arbitration:
   - Compare morning SCADA actuals against what ECMWF, ICON, and GEFS predicted.
   - If morning meter confirms high irradiance, heavily weight the clear-sky models and disregard phantom convective cloud warnings.
   - Output both "kt" (clearness index 0.0 to 1.05) and "adjusted_mw".
4. Physical Ramp & Monotonic Geometry (NO SAWTOOTH / JITTER):
   - Morning (06:30 - 11:30): Must be strictly non-decreasing, matching the rising solar trajectory.
   - Midday Apex (11:45 - 13:15): Smooth Enercast-style plateau or dome without artificial dips.
   - Afternoon (13:30 - 17:30): Strictly non-increasing diurnal descent.
   - Pre-dawn / Post-dusk: If base_mw is 0.0, adjusted_mw MUST be strictly 0.0.

Return ONLY raw JSON, no markdown or prose.
Array size must be exactly {len(anchor_predictions)}.
Each object must contain:
- "time"
- "kt": dimensionless clearness factor (0.00 to 1.05, where 1.0 = clear sky)
- "predicted_gti": estimated Global Tilted Irradiance in W/m²
- "adjusted_mw": final schedule generation in MW
- "confidence": High, Medium, or Low
- "reasoning": physical justification

Example:
[{{"time":"2026-09-01 13:15","kt":0.92,"predicted_gti":740.0,"adjusted_mw":2.85,"confidence":"High","reasoning":"Smooth diurnal afternoon decay tracking conservative lower bound of cloud risk envelope."}}]"""

    return f"""
Adjust the {prompt_subject} for the next {len(anchor_predictions)} blocks.
Keep changes grounded in the physical and telemetry evidence below.

Current situation:
{_summarize_current_situation(feature_row)}
{chr(10).join(sections)}

Base forecast blocks:
{blocks_text}

{rules_and_format}
"""


def _parse_llm_response(raw_text: str, anchor_predictions: list) -> list:
    """
    Parses the LLM's JSON response, falling back per-block to the
    base forecast (with a note explaining why) if parsing fails or a
    block is missing/malformed -- so one bad response never loses the
    whole run's predictions.
    """
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except Exception as e:
        print(f"  [WARN] Could not parse LLM JSON response ({e}); using base forecast for all blocks.")
        data = []

    def _normalize_time_label(value):
        """Normalize model-returned timestamps to the pipeline's canonical minute label."""
        if value is None:
            return None
        text = str(value).strip()
        if len(text) >= 16:
            return text[:16]
        return text or None

    def _extract_adjusted_mw(item: dict, anchor: dict):
        """Extract adjusted MW or reconstruct from Kt physical synthesis."""
        # Check Kt first for physical ground-truth binding
        for kt_key in ("kt", "clearness_index", "clearness_factor", "clearness_ratio"):
            if kt_key in item:
                try:
                    kt_val = float(item[kt_key])
                    if 0.05 <= kt_val <= 1.25:
                        base_val = anchor.get("base_anchor_mw", anchor.get("anchor_mw", 0.0))
                        if base_val > 0.0:
                            # Re-synthesize MW from Kt and physical clear-sky base
                            return round(base_val * min(1.05, kt_val), 3)
                except (TypeError, ValueError):
                    pass

        # Check explicit MW fields
        for key in ("adjusted_mw", "llm_mw", "forecast_mw", "predicted_mw", "adjusted_value", "value", "mw"):
            if key not in item:
                continue
            try:
                val = float(item[key])
                return val
            except (TypeError, ValueError):
                continue
        return None

    by_time = {}
    if isinstance(data, dict):
        for key in ("predictions", "results", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            normalized_time = _normalize_time_label(item.get("time"))
            if normalized_time:
                by_time[normalized_time] = item

    results = []
    for anchor in anchor_predictions:
        item = by_time.get(_normalize_time_label(anchor["time"]))
        adjusted_mw = _extract_adjusted_mw(item, anchor) if item else None
        if adjusted_mw is None:
            adjusted_mw = anchor["anchor_mw"]
            item = None

        result = {
            "time": anchor["time"],
            "block_number": anchor["block_number"],
            "anchor_mw": anchor["anchor_mw"],
            "llm_mw": adjusted_mw,
            "confidence": (item or {}).get("confidence", "Low"),
            "reasoning": (item or {}).get(
                "reasoning",
                "LLM adjustment unavailable for this block -- using scaffold unchanged."
            ),
        }
        if item:
            for extra_k in ("predicted_gti", "kt", "winning_model", "model_weights"):
                if extra_k in item:
                    result[extra_k] = item[extra_k]

        for key in (
            "base_anchor_mw",
            "live_residual_factor",
            "regime_label",
            "fluctuation_flag",
            "regime_summary",
            "live_state_summary",
            "step1_mw",
            "step2_mw",
            "step2_confidence",
            "step2_reasoning",
            "step3_mw",
        ):
            if key in anchor:
                result[key] = anchor[key]
        results.append(result)
    return results


def _fallback_predictions(base_predictions: list, reasoning: str) -> list:
    """Return a schema-compatible fallback result list from base predictions."""
    results = []
    for base in base_predictions:
        result = {
            "time": base["time"],
            "block_number": base["block_number"],
            "anchor_mw": base["anchor_mw"],
            "llm_mw": base["anchor_mw"],
            "confidence": "Low",
            "reasoning": reasoning,
        }
        for key in (
            "base_anchor_mw",
            "live_residual_factor",
            "regime_label",
            "fluctuation_flag",
            "regime_summary",
            "live_state_summary",
            "step1_mw",
            "step2_mw",
            "step2_confidence",
            "step2_reasoning",
            "step3_mw",
        ):
            if key in base:
                result[key] = base[key]
        results.append(result)
    return results


def predict_with_llm(anchor_predictions: list, feature_row: dict, retrieved_cases_text: str,
                      context_text: str = "", intraday_actuals_text: str = "",
                      intraday_state_text: str = "", weather_text: str = "",
                      video_text: str = "", image_map: dict = None,
                      fallback_anchor_predictions: list | None = None,
                      prompt_subject: str = "base forecast") -> list:
    """
    Main entry point.

    anchor_predictions: list of {"time": ..., "block_number": ..., "anchor_mw": ...}
        for the upcoming forecast blocks (from the base forecast stage).
    feature_row: the current (most recent capture's) feature dict.
    retrieved_cases_text: output of similarity_retrieval.format_cases_for_prompt().
    context_text: output of daily_feedback.format_context_for_prompt() -- the
        rolling last few days' error/pattern analysis from real meter data.
    intraday_actuals_text: output of
        daily_feedback.format_intraday_actuals_for_prompt() -- optional,
        "how generation went earlier TODAY" evidence (used by
        manual_prediction.py). Empty string if not applicable.
    image_map is accepted for backward compatibility but ignored. The
        LLM now reasons from the structured feature summary, similar
        historical cases, rolling context, and any intraday actuals/state
        text only.

    Returns a list of dicts, one per block:
        {"time", "block_number", "anchor_mw", "llm_mw", "confidence", "reasoning"}

    If the LLM is unavailable or fails after retries, every block falls
    back to llm_mw == anchor_mw with confidence "Low" and an explanatory
    reasoning string -- the pipeline always produces a full set of
    predictions.
    """
    disabled, disabled_plant = _is_llm_disabled_for_plant()
    if disabled:
        print(f"  [INFO] LLM explicitly disabled for {disabled_plant} -- using calibrated physical wind ensemble forecast.")
        base_predictions = fallback_anchor_predictions or anchor_predictions
        return _fallback_predictions(
            base_predictions,
            f"Calibrated physical wind ensemble forecast (LLM disabled for {disabled_plant}).",
        )

    if not _load_openrouter_api_keys():
        print("  [WARN] No OpenRouter API keys configured -- skipping LLM adjustment, using fallback values for all blocks.")
        base_predictions = fallback_anchor_predictions or anchor_predictions
        return _fallback_predictions(
            base_predictions,
            "LLM adjustment unavailable for this block -- using fallback forecast unchanged.",
        )

    max_retries = _llm_max_retries()
    base_delay = _llm_retry_base_delay_seconds()
    all_predictions = []
    chunk_size = _llm_chunk_size(anchor_predictions)
    for start in range(0, len(anchor_predictions), chunk_size):
        chunk = anchor_predictions[start:start + chunk_size]
        prompt = _build_prompt(chunk, feature_row, retrieved_cases_text, context_text,
                                intraday_actuals_text, intraday_state_text, weather_text, video_text, prompt_subject)

        raw_text, last_error = _call_llm_text(
            prompt=prompt,
            vision_parts=[],
            max_retries=max_retries,
            base_delay=base_delay,
        )

        if not raw_text:
            if last_error:
                print(f"  [WARN] LLM call failed on all configured providers/keys ({last_error}) -- using scaffold forecast for this block group.")
            chunk_predictions = _fallback_predictions(
                fallback_anchor_predictions[start:start + chunk_size] if fallback_anchor_predictions else chunk,
                "LLM adjustment unavailable for this block -- using fallback forecast unchanged.",
            )
        else:
            chunk_predictions = _parse_llm_response(raw_text, chunk)
            if fallback_anchor_predictions and all(
                prediction["reasoning"].startswith("LLM adjustment unavailable")
                for prediction in chunk_predictions
            ):
                chunk_predictions = _fallback_predictions(
                    fallback_anchor_predictions[start:start + chunk_size],
                    "LLM adjustment unavailable for this block -- using fallback forecast unchanged.",
                )
        missing_count = sum(
            1 for prediction in chunk_predictions
            if prediction["reasoning"].startswith("LLM adjustment unavailable")
        )
        if missing_count:
            print(
                f"  [WARN] LLM returned no usable adjustment for {missing_count}/{len(chunk)} "
                f"block(s) from {chunk[0]['time']} to {chunk[-1]['time']}."
            )
        all_predictions.extend(chunk_predictions)

    return all_predictions


def _build_stepwise_prompt(base_predictions: list, feature_row: dict, step1_inputs_text: str,
                           context_text: str = "", intraday_state_text: str = "",
                           step4_feedback_text: str = "", weather_text: str = "", video_text: str = "",
                           prompt_subject: str = "revision forecast") -> str:
    blocks_text = "\n".join(
        f"{i + 1}. time={p['time']}, meter_base_mw={p['anchor_mw']}"
        for i, p in enumerate(base_predictions)
    )

    sections = []
    if step1_inputs_text.strip():
        sections.append(
            "STEP 1 -- current live meter data + pvlib clear-sky evidence:\n"
            f"{step1_inputs_text.strip()}"
        )
    if weather_text.strip():
        sections.append(
            "STEP 2 -- ECMWF / Open-Meteo weather forecast (Global Tilted Irradiance & Cloud Cover %):\n"
            f"{weather_text.strip()}"
        )
    if intraday_state_text.strip():
        sections.append(
            "LIVE SAME-DAY SCADA STATE:\n"
            f"{intraday_state_text.strip()}"
        )

    plant_name = getattr(config, "PLANT_NAME", "Solar Plant")
    cap_mw = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
    dc_mw = float(getattr(config, "PLANT_DC_CAPACITY_MW", cap_mw))
    peak_mw_est = round(min(cap_mw, dc_mw * float(getattr(config, "PERFORMANCE_RATIO", 0.78)) * 1.05), 2)
    reg_name = getattr(config, "PLANT_PENALTY_REGULATION", "CERC")
    tol_band_pct = float(getattr(config, "PLANT_TOLERANCE_BAND_PCT", getattr(config, "get_plant_tolerance_band_pct", lambda *_: 15.0)(reg_name, plant_name)))
    tol_band_mw = round(cap_mw * (tol_band_pct / 100.0), 3)
    return f"""
Generate the {plant_name} forecast in ONE JSON response. Plant AC Capacity is {cap_mw:.1f} MW (DC: {dc_mw:.1f} MW).
State Regulatory Regime: {reg_name} (Strict DSM Tolerance Band: ±{tol_band_pct:.0f}% -> ±{tol_band_mw:.2f} MW).

CRITICAL FORECAST & AI ADJUSTMENT RULES (NO FIXED WEIGHTS):
1. Dynamic Evidence-Based Adjustment with Strict State Tolerance Band (±{tol_band_pct:.0f}% -> ±{tol_band_mw:.2f} MW):
   - Under {reg_name} grid regulations, any deviation exceeding ±{tol_band_pct:.0f}% of capacity (±{tol_band_mw:.2f} MW) incurs direct DSM cash penalties!
   - You must evaluate all available evidence: Live SCADA Clearness Ratio (Kt = Actual / ClearSky), Ground POA, 15-minute Ramp Rate (dP/dt), and ALL 3 Independent Weather Streams (Stream 1 ECMWF, Stream 2 91-Member Ensemble, Stream 3 5-Agency Consensus + CAPE + Transmissivity).
   - Reason dynamically: Decide (a) whether an adjustment is needed, (b) the direction (positive, negative, or neutral), and (c) the exact adjustment magnitude (Delta MW) justified by the evidence so that the schedule remains safely within the ±{tol_band_mw:.2f} MW band.
   - Explicitly document your physical reasoning and why the chosen magnitude was selected in the "reasoning" field.

2. Regime-Governed Adjustment Decisions:
   - CLEAR-SKY REGIME: When ground SCADA / pyranometer confirms clear sky (Kt >= 0.85, high POA, or smooth morning ascent), isolated NWP weather model rain drops are diagnosed as spatial-resolution phantom artifacts. Downward cuts below Step 1 Base are STRICTLY PROHIBITED (Step 2 MW >= Step 1 MW). Reason how much positive meter pull (+MW) to apply to track live SCADA ramp and inverter performance.
   - OVERCAST BREAKOUT & CLOUD DISSIPATION REGIME: When live ground SCADA breaks through an earlier cloud ceiling (Kt rising above 0.65 or SCADA surging), the sustained overcast ceiling is RELEASED IMMEDIATELY. Do not trap future blocks in a stale overcast ceiling based on lagging weather models! Follow the real clearing trajectory back towards Step 1 Base.
   - CONFIRMED OVERCAST REGIME: When multi-stream weather models agree AND ground telemetry confirms cloud attenuation (Kt < 0.65 and low POA), downward weather adjustment is approved. Sizing the downward magnitude should match the verified ground attenuation to prevent grid over-injection penalties.
   - CONVECTIVE STORM THREAT: When Stream 3 shows high CAPE (> 1500 J/kg) or sudden ground generation drop during morning ramp (dP/dt < -0.3 MW), apply a cautious downward adjustment down to the diffuse irradiance floor.

3. Solar Geometry, Curve-Shape & Physical Bounds (STRICT CONTINUITY & ANTI-ZIGZAG):
   - Never alternate schedule direction unnecessarily: DO NOT generate alternating up/down zig-zag behavior (e.g. 10.0 -> 12.0 -> 10.5 -> 13.0).
   - Morning Ascent (06:30 - 11:30): Enforce a smooth, convex increasing curve matching rising solar elevation. Avoid erratic dips unless verified by sustained ground cloud shading.
   - Midday Apex (11:30 - 13:30): Enforce a stable plateau or smooth dome without step jumps.
   - Afternoon Descent (13:30 - 18:00): Enforce a smooth, monotonic concave decline; never create late afternoon re-spikes.
   - Boundary Continuity & Tapering: Connect Block 1 smoothly to current live measured generation. If applying a short-term telemetry correction, taper it gradually toward the multi-model ensemble baseline over 4-6 blocks.
   - Avoid chasing isolated single-block weather or model spikes. Prefer the value with the highest probability of remaining inside the ±{tol_band_mw:.2f} MW DSM tolerance band.
   - Pre-dawn / Post-dusk: If solar elevation < 3.0 deg, generation is strictly 0.00 MW. Between 3.0 deg and 7.5 deg, output small diffuse dawn/dusk power (0.05 to 0.25 MW) if irradiance > 25 W/m².
   - Physical Diffuse Floor: In India during daylight hours (solar elevation >= 45 deg), diffuse irradiance physically yields at least 25-35% of capacity; generation never drops below this unless torrential rain is confirmed on site.
   - Upper Ceiling: Step 2 MW must never exceed plant maximum AC export limit of {cap_mw:.2f} MW.

5. Inverter AC Clipping Protection & Enercast Midday Stability:
   - When generation reaches >= 0.88 * Plant AC Capacity ({cap_mw * 0.88:.2f} MW), inverters are operating at full capacity.
   - Flatlining generation at solar noon is an electrical AC inverter saturation artifact, NOT cloud attenuation!
   - Set Kt = 1.00 and maintain the full convex plateau between 11:30 and 13:30; NEVER slash midday generation for perceived flatlining.
   - Under scattered cloud uncertainty, mimic Enercast's proven regulatory discipline by positioning schedules near 85-90% of clear sky to stay within the ±{tol_band_mw:.2f} MW 0% penalty safe band.

6. Output Fields:
   - Return both "kt" (clearness factor 0.00 to 1.05) and "llm_mw" for each block.

Return ONLY raw JSON, no markdown or prose.
Array size must be exactly {len(base_predictions)}.

Current situation:
{_summarize_current_situation(feature_row)}

Evidence:
{chr(10).join(sections)}

Forecast blocks:
{blocks_text}

Return a valid JSON object formatted with a "predictions" array:
{{
  "predictions": [
    {{
      "time": "2026-08-21 15:45",
      "kt": 0.92,
      "predicted_gti": 650.0,
      "step1_mw": 4.2,
      "step2_mw": 3.8,
      "llm_mw": 3.8,
      "confidence": "Medium",
      "reasoning": "ECMWF weather irradiance and live SCADA meter trend adjust the clear-sky baseline downward."
    }}
  ]
}}
"""


def _parse_stepwise_llm_response(raw_text: str, base_predictions: list) -> list:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except Exception as e:
        print(f"  [WARN] Could not parse stepwise JSON response ({e}); using meter base for all blocks.")
        data = []

    def _normalize_time_label(value):
        if value is None:
            return None
        text_value = str(value).strip()
        if len(text_value) >= 16:
            return text_value[:16]
        return text_value or None

    def _extract_numeric(item: dict, keys: tuple[str, ...]):
        if not isinstance(item, dict):
            return None
        for key in keys:
            if key not in item:
                continue
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
        return None

    by_time = {}
    if isinstance(data, dict):
        for key in ("predictions", "results", "items", "data", "forecast", "schedule", "blocks", "forecast_blocks", "schedule_blocks"):
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                data = value
                break
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    data = v
                    break

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            normalized_time = _normalize_time_label(item.get("time"))
            if normalized_time:
                by_time[normalized_time] = item

    results = []
    for base in base_predictions:
        item = by_time.get(_normalize_time_label(base["time"]))
        step1_mw = _extract_numeric(item, ("step1_mw", "meter_base_mw", "base_mw", "adjusted_mw"))
        step2_mw = _extract_numeric(item, ("step2_mw", "weather_mw", "video_mw", "llm_mw", "forecast_mw", "predicted_mw", "adjusted_value", "value", "mw"))
        llm_mw = _extract_numeric(item, ("llm_mw", "step2_mw", "final_mw", "forecast_mw", "predicted_mw", "adjusted_value", "value", "mw"))

        # Physical Kt validation: if Kt is present, verify grounding
        kt_val = _extract_numeric(item, ("kt", "clearness_index", "clearness_factor", "clearness_ratio"))
        if kt_val is not None and 0.05 <= kt_val <= 1.25 and base["anchor_mw"] > 0.0:
            phys_mw = round(base["anchor_mw"] * min(1.05, kt_val), 3)
            if step2_mw is None:
                step2_mw = phys_mw
            if llm_mw is None:
                llm_mw = phys_mw

        if step1_mw is None:
            step1_mw = base["anchor_mw"]
        if step2_mw is None:
            step2_mw = step1_mw
        if llm_mw is None:
            llm_mw = step2_mw

        # Step 3 and Step 4 are set equal to Step 2 / LLM MW (no bias distortion)
        step3_mw = step2_mw
        step4_mw = llm_mw

        result = {
            "time": base["time"],
            "block_number": base["block_number"],
            "anchor_mw": base["anchor_mw"],
            "step1_mw": step1_mw,
            "step2_mw": step2_mw,
            "step3_mw": step3_mw,
            "step4_mw": step4_mw,
            "llm_mw": llm_mw,
            "confidence": (item or {}).get("confidence", "Medium"),
            "reasoning": (item or {}).get(
                "reasoning",
                "Weather and solar irradiance adjust the physical baseline.",
            ),
        }
        if item:
            for extra_k in ("predicted_gti", "kt", "winning_model", "model_weights"):
                if extra_k in item:
                    result[extra_k] = item[extra_k]
        results.append(result)
    return results


def predict_stepwise_with_llm(base_predictions: list, feature_row: dict, step1_inputs_text: str,
                              context_text: str, intraday_state_text: str = "",
                              step4_feedback_text: str = "", weather_text: str = "", video_text: str = "",
                              fallback_base_predictions: list | None = None,
                              prompt_subject: str = "Bhupalpally revision forecast") -> list:
    """Single-call Bhupalpally path that returns step1/step2/step3/LLM outputs together."""
    disabled, disabled_plant = _is_llm_disabled_for_plant()
    if disabled:
        print(f"  [INFO] LLM explicitly disabled for {disabled_plant} -- using calibrated physical wind ensemble forecast.")
        base = fallback_base_predictions or base_predictions
        return _fallback_predictions(
            base,
            f"Physical calibrated wind ensemble forecast (LLM disabled for {disabled_plant}).",
        )

    if not _load_openrouter_api_keys():
        print("  [WARN] No OpenRouter API keys configured -- skipping stepwise LLM adjustment, using meter base values.")
        base = fallback_base_predictions or base_predictions
        return _fallback_predictions(
            base,
            "LLM adjustment unavailable for this block -- using meter base forecast unchanged.",
        )

    prompt = _build_stepwise_prompt(
        base_predictions,
        feature_row,
        step1_inputs_text,
        context_text,
        intraday_state_text,
        step4_feedback_text,
        weather_text,
        video_text,
        prompt_subject=prompt_subject,
    )

    max_retries = _llm_max_retries()
    base_delay = _llm_retry_base_delay_seconds()
    raw_text, last_error = _call_llm_text(
        prompt=prompt,
        vision_parts=[],
        max_retries=max_retries,
        base_delay=base_delay,
    )

    if not raw_text:
        if last_error:
            print(f"  [WARN] LLM call failed on all configured providers/keys ({last_error}) -- using meter base forecast.")
        return _fallback_predictions(
            fallback_base_predictions or base_predictions,
            "LLM adjustment unavailable for this block -- using meter base forecast unchanged.",
        )

    stepwise_predictions = _parse_stepwise_llm_response(raw_text, base_predictions)
    if fallback_base_predictions and all(
        prediction["reasoning"].startswith("LLM adjustment unavailable")
        for prediction in stepwise_predictions
    ):
        return _fallback_predictions(
            fallback_base_predictions,
            "LLM adjustment unavailable for this block -- using meter base forecast unchanged.",
        )

    missing_count = sum(
        1 for prediction in stepwise_predictions
        if prediction["reasoning"].startswith("LLM adjustment unavailable")
    )
    if missing_count:
        print(
            f"  [WARN] LLM returned no usable stepwise adjustment for {missing_count}/{len(base_predictions)} "
            f"block(s) from {base_predictions[0]['time']} to {base_predictions[-1]['time']}."
        )
    return stepwise_predictions



