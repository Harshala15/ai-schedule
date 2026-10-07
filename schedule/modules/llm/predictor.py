"""
llm/predictor.py

OpenRouter LLM communication client for Intellis AI 2.0.
Provides resilient API key fallback, model retries, and plant-level LLM guardrails
for the LLMStrategicArbiter pipeline.
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
    val = os.getenv(name)
    if val is None and hasattr(config, "_read_env_value"):
        val = config._read_env_value(name)
    if not val:
        val = default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


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
    plant_val = os.getenv(plant_key_name, "")
    if not plant_val and hasattr(config, "_read_env_value"):
        plant_val = config._read_env_value(plant_key_name)
    _add(plant_key_name, plant_val)
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
                        "content": "You are an expert renewable energy power grid dispatch scheduling model. Output strictly valid JSON without extra markdown formatting.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": int(os.getenv("OPENROUTER_MAX_TOKENS", "3500")),
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
