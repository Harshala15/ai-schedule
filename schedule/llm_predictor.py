"""
llm_predictor.py

The ONLY module in this pipeline that calls an LLM. Its job is narrow
and constrained on purpose: given a physics-based anchor value for each
of the next 8 forecast blocks, plus the most similar past situations
(from similarity_retrieval.py) and their real outcomes, ask the LLM to
ADJUST each anchor (not invent a number from scratch) and explain why.

WHY THIS DESIGN (vs asking the LLM to just "predict the generation"):
    - The physics anchor keeps every prediction physically grounded
      (never wildly off, always respects day/night and rough cloud
      attenuation) even if the LLM's adjustment is unhelpful.
    - Retrieved similar cases give the LLM concrete historical evidence
      ("in similar cloud conditions, actual generation was X% lower/
      higher than this formula predicted") instead of vague reasoning.
    - A single, small, structured JSON response is far more reliable to
      parse and validate than asking for 8 independent numbers with no
      anchor to sanity-check against.

If the LLM call fails entirely (network, rate limit, bad JSON), this
module falls back to the physics anchor values unchanged -- the pipeline
never produces no output just because the LLM step had a problem.
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
                return models
        return ["gemini-2.5-flash"]

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
                   intraday_state_text: str = "") -> str:
    blocks_text = "\n".join(
        f"{i + 1}. time={p['time']}, physics_anchor_mw={p['anchor_mw']}"
        for i, p in enumerate(anchor_predictions)
    )

    sections = []
    if retrieved_cases_text.strip():
        sections.append(f"Similar historical cases:\n{retrieved_cases_text.strip()}")
    if context_text.strip():
        sections.append(f"Recent context:\n{context_text.strip()}")
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
Adjust the physics anchor for the next {len(anchor_predictions)} blocks.
Keep changes grounded in the evidence below.

Current situation:
{_summarize_current_situation(feature_row)}
{chr(10).join(sections)}

Physics anchor blocks:
{blocks_text}

Rules:
- Prefer same-day actuals over historical cases.
- If a block does not need adjustment, keep the anchor value.
- Keep reasoning short and block-specific.

Return ONLY raw JSON, no markdown or prose.
Array size must be exactly {len(anchor_predictions)}.
Each object must contain:
- "time"
- "adjusted_mw"
- "confidence"
- "reasoning"

Example:
[{{"time":"2026-07-20 13:15","adjusted_mw":2.1,"confidence":"Medium","reasoning":"Similar cloud coverage on 2026-07-18 showed generation about 8% below the anchor."}}]
"""
def _parse_llm_response(raw_text: str, anchor_predictions: list) -> list:
    """
    Parses the LLM's JSON response, falling back per-block to the
    physics anchor (with a note explaining why) if parsing fails or a
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
        print(f"  [WARN] Could not parse LLM JSON response ({e}); using physics anchor for all blocks.")
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
                "LLM adjustment unavailable for this block -- using physics anchor unchanged."
            ),
        }
        for key in ("base_anchor_mw", "live_residual_factor", "regime_label", "fluctuation_flag",
                    "regime_summary", "live_state_summary"):
            if key in anchor:
                result[key] = anchor[key]
        results.append(result)
    return results


def predict_with_llm(anchor_predictions: list, feature_row: dict, retrieved_cases_text: str,
                      context_text: str = "", intraday_actuals_text: str = "",
                      intraday_state_text: str = "", image_map: dict = None) -> list:
    """
    Main entry point.

    anchor_predictions: list of {"time": ..., "block_number": ..., "anchor_mw": ...}
        for the upcoming forecast blocks (from physics_anchor.py).
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
        print("  [WARN] No Gemini API keys configured -- skipping LLM adjustment, using physics anchor for all blocks.")
        return _parse_llm_response("[]", anchor_predictions)

    max_retries = 4
    base_delay = 5
    all_predictions = []
    chunk_size = _llm_chunk_size(anchor_predictions)
    for start in range(0, len(anchor_predictions), chunk_size):
        chunk = anchor_predictions[start:start + chunk_size]
        prompt = _build_prompt(chunk, feature_row, retrieved_cases_text, context_text,
                                intraday_actuals_text, intraday_state_text)

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
                print("  [WARN] Using physics anchor for this block group until a key/quota becomes available.")
            elif _is_key_auth_error(last_error):
                print(f"  [WARN] All configured Gemini keys appear invalid/expired: {last_error}")
                print("  [WARN] Using physics anchor for this block group until a valid key is supplied.")
            else:
                print(f"  [WARN] Gemini call failed on all configured keys ({last_error}) -- using physics anchor for this block group.")

        chunk_predictions = _parse_llm_response(raw_text, chunk)
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
