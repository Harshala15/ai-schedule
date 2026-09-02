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
from google import genai
from google.genai import types

import config

def _plant_env_suffix() -> str:
    plant = (config.PLANT_NAME or "").strip().upper()
    return f"_{plant}" if plant else ""


def _load_model_names() -> list[str]:
    """Return Gemini model names in priority order.

    The primary model remains gemini-3.6-flash, but we keep lower flash
    models as automatic fallbacks so the pipeline can still return an LLM
    adjustment when the newest model is temporarily unavailable.
    """
    if (config.PLANT_NAME or "").strip().upper() == "KASIPET":
        raw = os.getenv("GEMINI_MODEL_CANDIDATES_KASIPET", "").strip()
        if raw:
            models = [item.strip() for item in re.split(r"[,\n;]+", raw) if item.strip()]
            if models:
                if "gemini-3.6-flash" not in models:
                    models.insert(0, "gemini-3.6-flash")
                return models
        return ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash"]

    for env_name in (
        f"GEMINI_MODEL_CANDIDATES{_plant_env_suffix()}",
        "GEMINI_MODEL_CANDIDATES",
    ):
        raw = os.getenv(env_name, "").strip()
        if raw:
            models = [item.strip() for item in re.split(r"[,\n;]+", raw) if item.strip()]
            if models:
                return models
    return [
        "gemini-3.6-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]


def _load_gemini_api_keys() -> list[tuple[str, str]]:
    """Return Gemini API keys in priority order.

    Supports:
    - GEMINI_API_KEY_<PLANT>=...
    - GEMINI_API_KEYS="key1,key2,key3"
    - GEMINI_API_KEY_1 / GEMINI_API_KEY_2 / ...
    - GEMINI_API_KEY as the default fallback
    """
    if (config.PLANT_NAME or "").strip().upper() == "KASIPET":
        key = os.getenv("GEMINI_API_KEY_KASIPET", "").strip()
        return [("GEMINI_API_KEY_KASIPET", key)] if key else []

    keys: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(source: str, value: str) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        keys.append((source, value))

    for env_name in (
        f"GEMINI_API_KEYS{_plant_env_suffix()}",
        "GEMINI_API_KEYS",
    ):
        raw_group = os.getenv(env_name, "").strip()
        if raw_group:
            for index, candidate in enumerate(re.split(r"[,\n;]+", raw_group), start=1):
                _add(f"{env_name}[{index}]", candidate)
            break

    for index in range(1, 11):
        _add(f"GEMINI_API_KEY_{index}", os.getenv(f"GEMINI_API_KEY_{index}", ""))

    plant_key_name = f"GEMINI_API_KEY{_plant_env_suffix()}"
    _add(plant_key_name, os.getenv(plant_key_name, ""))
    _add("GEMINI_API_KEY", config.GEMINI_API_KEY)
    return keys


def _llm_chunk_size(anchor_predictions: list) -> int:
    """Return how many blocks to send in one Gemini request.

    All plants are handled as a single request per run so the model can
    adjust the entire remaining horizon in one pass.
    """
    return max(1, len(anchor_predictions))


def is_transient_gemini_error(e) -> bool:
    """Same retry-worthy-error check used throughout this project:
    503/UNAVAILABLE, 429/RESOURCE_EXHAUSTED, 500/INTERNAL are worth
    retrying; anything else (bad key, bad request) is not."""
    msg = str(e)
    transient_markers = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL")
    return any(marker in msg for marker in transient_markers)


def _is_quota_or_key_limit_error(e) -> bool:
    """Returns True when Gemini is refusing the request due to quota, rate
    limit, or API-key-associated usage limits."""
    msg = str(e).lower()
    markers = (
        "resource_exhausted",
        "quota exceeded",
        "rate limit",
        "too many requests",
        "api key",
        "billing details",
    )
    return any(marker in msg for marker in markers)


def _is_key_auth_error(e) -> bool:
    """Returns True when the current API key is expired, invalid, or unauthorized."""
    msg = str(e).lower()
    markers = (
        "invalid api key",
        "expired",
        "unauthorized",
        "unauthenticated",
        "forbidden",
        "permission denied",
        "api key not valid",
        "api key",
    )
    return any(marker in msg for marker in markers)


def _is_model_unavailable_error(e) -> bool:
    """Returns True when the requested Gemini model is missing or unavailable."""
    msg = str(e).lower()
    markers = (
        "model not found",
        "not found",
        "unsupported model",
        "model unavailable",
        "does not exist",
        "404",
    )
    return any(marker in msg for marker in markers)


def _call_gemini_with_key(api_key: str, key_label: str, prompt: str, vision_parts: list,
                          max_retries: int, base_delay: int, model_names: list[str]):
    client = genai.Client(api_key=api_key, vertexai=False)
    for attempt in range(1, max_retries + 1):
        try:
            last_model_error = None
            for model_name in model_names:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[prompt, *vision_parts],
                        config=types.GenerateContentConfig(response_mime_type="application/json"),
                    )
                    return (response.text or "").strip(), None
                except Exception as model_error:
                    last_model_error = model_error
                    if _is_model_unavailable_error(model_error):
                        print(f"  [WARN] Gemini model unavailable for {key_label} ({model_name}): {model_error}")
                        continue
                    raise
            if last_model_error is not None:
                raise last_model_error
        except Exception as e:
            if is_transient_gemini_error(e) and attempt < max_retries and not _is_key_auth_error(e):
                delay = base_delay * (2 ** (attempt - 1))
                if _is_quota_or_key_limit_error(e):
                    print(
                        f"  [WARN] Gemini quota/API-key limit reached for {key_label} "
                        f"(attempt {attempt}/{max_retries}): {e}"
                    )
                    print(
                        "  [WARN] Will try another configured API key if one is available."
                    )
                else:
                    print(
                        f"  [WARN] Gemini API busy for {key_label} "
                        f"(attempt {attempt}/{max_retries}): {e}"
                    )
                    print(f"  Retrying in {delay}s...")
                    time.sleep(delay)
                    continue
            return "", e


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
1. Asymmetric CERC/DSM Loss Function:
   - Under-forecasting by 5-10% carries ZERO penalty under grid regulations.
   - Over-forecasting by >15% into passing cloud dips triggers severe financial deviation penalties (up to Rs. 331/block).
   - Therefore, during overcast, monsoon, or volatile cloud regimes, always favor the conservative lower bound of the envelope.
2. Physical Ramp & Monotonic Geometry:
   - Morning (06:30 - 11:30): Must be strictly non-decreasing, matching the rising solar trajectory.
   - Midday Apex (11:45 - 12:30): Smooth parabolic apex without artificial flat tabletop clipping.
   - Afternoon (12:45 - 17:30): Strictly non-increasing diurnal descent.
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
[{"time":"2026-09-01 13:15","adjusted_mw":2.85,"confidence":"High","reasoning":"Smooth diurnal afternoon decay tracking conservative lower bound of cloud risk envelope."}]
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
    api_keys = _load_gemini_api_keys()
    model_names = _load_model_names()
    if not api_keys:
        print("  [WARN] No Gemini API keys configured -- skipping LLM adjustment, using fallback values for all blocks.")
        base_predictions = fallback_anchor_predictions or anchor_predictions
        return _fallback_predictions(
            base_predictions,
            "LLM adjustment unavailable for this block -- using fallback forecast unchanged.",
        )

    max_retries = 4
    base_delay = 5
    all_predictions = []
    chunk_size = _llm_chunk_size(anchor_predictions)
    for start in range(0, len(anchor_predictions), chunk_size):
        chunk = anchor_predictions[start:start + chunk_size]
        prompt = _build_prompt(chunk, feature_row, retrieved_cases_text, context_text,
                                intraday_actuals_text, intraday_state_text, weather_text, video_text, prompt_subject)

        raw_text = ""
        last_error = None
        for key_index, (key_label, api_key) in enumerate(api_keys, start=1):
            raw_text, last_error = _call_gemini_with_key(
                api_key=api_key,
                key_label=key_label,
                prompt=prompt,
                vision_parts=[],
                max_retries=max_retries,
                base_delay=base_delay,
                model_names=model_names,
            )
            if raw_text:
                break
            if last_error is None:
                continue
            if key_index < len(api_keys):
                print(f"  [WARN] Switching from {key_label} to next configured Gemini key.")
                continue
            if _is_quota_or_key_limit_error(last_error):
                print(f"  [WARN] Gemini quota/API-key limit exhausted on all configured keys: {last_error}")
                print("  [WARN] Using scaffold forecast for this block group until a key/quota becomes available.")
            elif _is_key_auth_error(last_error):
                print(f"  [WARN] All configured Gemini keys appear invalid/expired: {last_error}")
                print("  [WARN] Using scaffold forecast for this block group until a valid key is supplied.")
            else:
                print(f"  [WARN] Gemini call failed on all configured keys ({last_error}) -- using scaffold forecast for this block group.")

        if not raw_text:
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
                f"  [WARN] Gemini returned no usable adjustment for {missing_count}/{len(chunk)} "
                f"block(s) from {chunk[0]['time']} to {chunk[-1]['time']}."
            )
        all_predictions.extend(chunk_predictions)

    return all_predictions


def _build_stepwise_prompt(base_predictions: list, feature_row: dict, step1_inputs_text: str,
                           context_text: str, intraday_state_text: str = "",
                           step4_feedback_text: str = "", weather_text: str = "", video_text: str = "",
                           prompt_subject: str = "Bhupalpally revision forecast") -> str:
    blocks_text = "\n".join(
        f"{i + 1}. time={p['time']}, meter_base_mw={p['anchor_mw']}"
        for i, p in enumerate(base_predictions)
    )

    sections = []
    if step1_inputs_text.strip():
        sections.append(
            "STEP 1 -- current meter data + last 3 days meter JSON + pvlib evidence:\n"
            f"{step1_inputs_text.strip()}"
        )
    if weather_text.strip() or video_text.strip():
        step2_parts = []
        if weather_text.strip():
            step2_parts.append(
                "ECMWF / Open-Meteo weather forecast for the next revision horizon:\n"
                f"{weather_text.strip()}"
            )
        if video_text.strip():
            step2_parts.append(
                "Windy video OpenCV features for the same revision window:\n"
                f"{video_text.strip()}"
            )
        sections.append("STEP 2 -- weather + video evidence:\n" + "\n\n".join(step2_parts))
    if context_text.strip():
        sections.append(
            "STEP 3 -- rolling context / prior-day feedback:\n"
            f"{context_text.strip()}"
        )
    if intraday_state_text.strip():
        sections.append(
            "LIVE SAME-DAY STATE:\n"
            f"{intraday_state_text.strip()}"
        )
    if step4_feedback_text.strip():
        sections.append(
            "STEP 4 -- revision feedback evidence (recent 3-day multi-day baseline + today's live intra-day revisions):\n"
            f"{step4_feedback_text.strip()}"
        )

    plant_name = getattr(config, "PLANT_NAME", "Solar Plant")
    return f"""
Generate the {plant_name} forecast in ONE JSON response.

Important rules:
- Use the current meter data, the cached 3-day meter JSON, and pvlib evidence as Step 1.
- Step 1 is the physical anchor. Do NOT compound multiple sequential negative subtractions across Steps 1 to 4 on clear or partly sunny hours.
- If solar elevation < 7.5 deg (pre-dawn or post-dusk), generation is strictly 0.00 MW.
- MIDDAY RISK-NEUTRAL CEILING: Anchor peak clear-sky generation strictly around 0.69-0.705x plant capacity (e.g. 3.52-3.59 MW for 5.1 MW plants, 6.9-7.05 MW for 10 MW plants) to match Enercast's risk-minimized profile and avoid severe over-forecasting penalties (>4.0 MW) when midday monsoon clouds pass.
- MORNING MOMENTUM TRACKING: When live meter data between 08:30 and 10:30 AM shows strong clear-sky generation (e.g. ~2.0-2.3 MW at 08:45, ~3.0-3.3 MW at 10:00), follow the strong upward live meter momentum without lagging.
- AFTERNOON MONSOON CLOUD DISSIPATION: In the afternoon (12:45 to 15:30), smoothly taper generation from ~3.4 MW down to ~2.0-2.4 MW to maintain a safe envelope that stays within the +-10% band even when passing monsoon clouds reduce irradiance.
- AFTERNOON PERSISTENCE FLOOR: In the late afternoon (15:00 to 16:30), do not collapse the forecast below historical daytime levels (1.4-2.2 MW on 5.1 MW plant) due to short 15-minute transient cloud dips.
- Adjust Step 1 using weather + Windy/OpenCV features for Step 2.
- Adjust Step 2 using rolling context / feedback for Step 3.
- Use Step 3 as the base for Step 4.
- Step 4 integrates both the recent 3-day multi-day feedback (for baseline time-of-day bias) and today's live intra-day feedback (for same-day drift correction).
- LIVE OVERCAST RULE: If today's live same-day weather is overcast/rain (live residual factor < 0.65 or meter < 40% clear sky), NEVER apply positive historical bias to inflate afternoon blocks. Maintain a smooth, dampened overcast curve.
- When today has completed revisions (Revision 2 onwards), today's live feedback takes primary precedence to correct same-day drift, while the 3-day history acts as a stabilizing sanity check.
- When today has no earlier revisions (Revision 1 morning run), use the 3-day multi-day feedback as the primary grounding signal.
- Return the final forecast in step4_mw and llm_mw.
- Keep the output grounded in the provided evidence; do not invent unrelated values.
- Return ONLY raw JSON, no markdown or prose.
- Array size must be exactly {len(base_predictions)}.

Current situation:
{_summarize_current_situation(feature_row)}

Evidence:
{chr(10).join(sections)}

Forecast blocks:
{blocks_text}

Return each object with these fields:
- "time"
- "step1_mw"
- "step2_mw"
- "step3_mw"
- "step4_mw"
- "llm_mw"
- "confidence"
- "reasoning"

Example:
[{{"time":"2026-08-21 15:45","step1_mw":4.2,"step2_mw":4.0,"step3_mw":3.9,"step4_mw":3.8,"llm_mw":3.8,"confidence":"Medium","reasoning":"Meter history is strong; clouds and context suggest a slight downward adjustment."}}]
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
        print(f"  [WARN] Could not parse Bhupalpally stepwise JSON response ({e}); using meter base for all blocks.")
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
    for base in base_predictions:
        item = by_time.get(_normalize_time_label(base["time"]))
        step1_mw = _extract_numeric(item, ("step1_mw", "meter_base_mw", "base_mw", "adjusted_mw"))
        step2_mw = _extract_numeric(item, ("step2_mw", "weather_mw", "video_mw"))
        step3_mw = _extract_numeric(item, ("step3_mw", "context_mw"))
        step4_mw = _extract_numeric(item, ("step4_mw", "revision_feedback_mw", "feedback_mw"))
        llm_mw = _extract_numeric(item, ("llm_mw", "final_mw", "forecast_mw", "predicted_mw", "adjusted_value", "value", "mw"))

        if step1_mw is None:
            step1_mw = base["anchor_mw"]
        if step2_mw is None:
            step2_mw = step1_mw
        if step3_mw is None:
            step3_mw = step2_mw
        if step4_mw is None:
            step4_mw = step3_mw
        if llm_mw is None:
            llm_mw = step4_mw

        result = {
            "time": base["time"],
            "block_number": base["block_number"],
            "anchor_mw": base["anchor_mw"],
            "step1_mw": step1_mw,
            "step2_mw": step2_mw,
            "step3_mw": step3_mw,
            "step4_mw": step4_mw,
            "llm_mw": llm_mw,
            "confidence": (item or {}).get("confidence", "Low"),
            "reasoning": (item or {}).get(
                "reasoning",
                "LLM adjustment unavailable for this block -- using meter base forecast unchanged.",
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
    api_keys = _load_gemini_api_keys()
    model_names = _load_model_names()
    if not api_keys:
        print("  [WARN] No Gemini API keys configured -- skipping Bhupalpally LLM adjustment, using meter base values.")
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

    max_retries = 4
    base_delay = 5
    raw_text = ""
    last_error = None
    for key_index, (key_label, api_key) in enumerate(api_keys, start=1):
        raw_text, last_error = _call_gemini_with_key(
            api_key=api_key,
            key_label=key_label,
            prompt=prompt,
            vision_parts=[],
            max_retries=max_retries,
            base_delay=base_delay,
            model_names=model_names,
        )
        if raw_text:
            break
        if last_error is None:
            continue
        if key_index < len(api_keys):
            print(f"  [WARN] Switching from {key_label} to next configured Gemini key.")
            continue
        if _is_quota_or_key_limit_error(last_error):
            print(f"  [WARN] Gemini quota/API-key limit exhausted on all configured keys: {last_error}")
            print("  [WARN] Using meter base forecast until a key/quota becomes available.")
        elif _is_key_auth_error(last_error):
            print(f"  [WARN] All configured Gemini keys appear invalid/expired: {last_error}")
            print("  [WARN] Using meter base forecast until a valid key is supplied.")
        else:
            print(f"  [WARN] Gemini call failed on all configured keys ({last_error}) -- using meter base forecast.")

    if not raw_text:
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
            f"  [WARN] Gemini returned no usable stepwise adjustment for {missing_count}/{len(base_predictions)} "
            f"block(s) from {base_predictions[0]['time']} to {base_predictions[-1]['time']}."
        )
    return stepwise_predictions
