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

    return f"""
Adjust the {prompt_subject} for the next {len(anchor_predictions)} blocks.
Keep changes grounded in the physical and telemetry evidence below.

Current situation:
{_summarize_current_situation(feature_row)}
{chr(10).join(sections)}

Base forecast blocks:
{blocks_text}

CRITICAL RULES FOR GRID ACCURACY & PENALTY MINIMIZATION:
1. Two-Sided Penalty Band Mandate (+-15% of Available Capacity):
   - Under Telangana (TSERC) and CERC regulations, deviations within +-15% of plant capacity carry ZERO penalty.
   - BOTH Over-forecasting (>+15%) and Under-forecasting (<-15%) trigger severe financial deviation penalties (at plant PPA rate, e.g. Rs. 5.65/kWh in Telangana).
   - DO NOT excessively haircut generation into severe under-forecasting (<-15%). Maintain schedules within the safe +-15% corridor.
   - During clear sky ground conditions, never cut below Step 1 Base. During overcast or rain, attenuate smoothly without over-slashing.
2. Physical Ramp & Monotonic Geometry (NO SAWTOOTH / JITTER):
   - Morning (06:30 - 11:30): Must be strictly non-decreasing, matching the rising solar trajectory.
   - Midday Apex (11:45 - 12:30): Smooth parabolic apex without artificial flat tabletop clipping.
   - Afternoon (12:45 - 17:30): Strictly non-increasing diurnal descent.
   - Convective / Storm Window Continuity: If high CAPE (>1500 J/kg), rain cells, or storm instability are detected in the weather table, maintain a smooth, contiguous attenuated envelope across all affected 15-min blocks. NEVER create single-block alternating spikes or sawtooth drops.
   - Pre-dawn / Post-dusk: If base_mw is 0.0, adjusted_mw MUST be strictly 0.0.
3. Telemetry Grounding:
   - Prefer today's same-day actual generation telemetry and clearness trend over historical cases.
   - If evidence shows steady clear sky, follow the natural solar curve; if clouds or volatility are detected, attenuate smoothly.

Return ONLY raw JSON, no markdown or prose.
Array size must be exactly {len(anchor_predictions)}.
Each object must contain:
- "time"
- "adjusted_mw"
- "confidence"
- "reasoning"

Example:
[{{"time":"2026-09-01 13:15","adjusted_mw":2.85,"confidence":"High","reasoning":"Smooth diurnal afternoon decay tracking conservative lower bound of cloud risk envelope."}}]
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

    def _extract_adjusted_mw(item: dict):
        """Accept a few common JSON field names so minor model formatting changes do not
        trigger a full fallback."""
        for key in ("adjusted_mw", "llm_mw", "forecast_mw", "predicted_mw", "adjusted_value", "value", "mw"):
            if key not in item:
                continue
            try:
                return float(item[key])
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
        adjusted_mw = _extract_adjusted_mw(item) if item else None
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
    return f"""
Generate the {plant_name} forecast in ONE JSON response. Plant AC Capacity is {cap_mw:.1f} MW (DC: {dc_mw:.1f} MW).

CRITICAL FORECAST RULES:
1. Two-Step Physics & Weather Grounding (No artificial historical bias uplifts):
   - Step 1 (PVLib Base): Astronomical clear-sky trajectory anchored to current live SCADA meter readings (last 1-2 hours).
   - Step 2 (Weather Adjustment MW): Adjust Step 1 using ECMWF global tilted irradiance, direct/diffuse ratio, cloud cover %, and live SCADA momentum.
   - DIRECTIONAL DSM ERROR GUARDRAIL: When ground SCADA / pyranometer confirms clear ground conditions (live POA >= 80% of clear sky or recent clear sky ratio >= 0.85), Step 2 MUST NOT be adjusted downward below Step 1 Base! If actual is ramping strongly above Step 1 Base, Step 2 adjustment must be POSITIVE or ZERO (+MW), NEVER negative (-MW). Downward adjustments are strictly prohibited unless verified cloud attenuation (ground POA dropping below clear-sky levels or confirmed thick overcast > 60%) is present.
   - Live SCADA Momentum & MOS: If live SCADA generation shows the plant is ramping strongly under clear ground conditions, blend 75% Live SCADA Ground Ramp with 25% Weather Model to eliminate morning lag. If REAL-TIME GROUND SENSOR CALIBRATION (MOS) indicates ground irradiance is outperforming weather models, scale forward weather expectations upward to track ground truth.
2. Weather & Overcast Enforcement:
   - When ECMWF weather shows cloud cover (>50%) or reduced irradiance (<600 W/m²), follow the attenuated weather irradiance curve.
   - REAL CLOUD ATTENUATION: If ground SCADA / pyranometer indicates a cloud drop (POA or power dropping > 25% below clear sky), scale down the immediate forward blocks (next 2-4 blocks) proportionally to match observed ground attenuation.
   - DAWN EXEMPTION RULE: When solar elevation is < 20.0 deg (before 07:30 AM), low generation (< 2 MW) is normal due to inverter wake-up and low sun angles. DO NOT treat dawn low generation as heavy overcast! If ECMWF weather shows clear/rising irradiance (> 100 W/m²), follow the rising morning ramp toward full clear-sky capacity for forward blocks (07:30 - 11:00).
   - SUSTAINED OVERCAST RULE: If ground SCADA shows low generation (< 35% of clear sky) between 10:30 and 14:00 (solar elevation >= 45 deg), DO NOT assume rapid recovery to clear sky based solely on NWP weather models. Overcast cloud decks in monsoon regimes persist for 2-4 hours. Step 2 Weather Adjustment MUST NOT exceed 1.25x the live SCADA generation during overcast regimes.
   - NEVER apply positive historical bias to inflate forecasts during cloudy, monsoon, or overcast conditions.
   - Favor the conservative lower bound during overcast conditions to avoid DSM penalties.
3. Solar Geometry & Ramping:
   - Pre-dawn / Post-dusk: If solar elevation < 3.0 deg, generation is strictly 0.00 MW. Between 3.0 deg and 7.5 deg, output small diffuse dawn/dusk power (0.10 to 0.45 MW) if irradiance > 25 W/mÂ².
   - Morning (06:30 - 11:30): Smooth monotonic ascent tracking solar elevation and live meter momentum.
   - Midday Apex (11:45 - 12:45): Smooth apex capped by weather irradiance without flat tabletop clipping.
   - Afternoon (13:00 - 17:45): Smooth diurnal decay tracking afternoon irradiance down to 0 MW.
4. Gate Closure: Revisions take effect with a 4-block lag. Output clean, reliable values.

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
      "step1_mw": 4.2,
      "step2_mw": 3.8,
      "llm_mw": 3.8,
      "confidence": "Medium",
      "reasoning": "ECMWF weather irradiance (563 W/m2) and live SCADA meter trend adjust the clear-sky baseline downward."
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
        results.append(result)
    return results


def predict_stepwise_with_llm(base_predictions: list, feature_row: dict, step1_inputs_text: str,
                              context_text: str, intraday_state_text: str = "",
                              step4_feedback_text: str = "", weather_text: str = "", video_text: str = "",
                              fallback_base_predictions: list | None = None,
                              prompt_subject: str = "Bhupalpally revision forecast") -> list:
    """Single-call Bhupalpally path that returns step1/step2/step3/LLM outputs together."""
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



