"""
OpenRouter Strategic Day-Ahead LLM Arbiter & Resilient Multi-Key Failover Gateway.

Handles HTTPS POST requests to OpenRouter API Gateway targeting openai/gpt-5.6-luna,
with automatic exponential backoff key rotation across 3 API keys and deterministic MOS fallback.
"""

import json
import time
import requests
from typing import Dict, Any, List, Optional
import numpy as np

class DASolarLLMArbiter:
    """
    Day-Ahead Strategic LLM Arbiter for OpenRouter API Gateway.
    """

    OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
    MODEL_NAME = "openai/gpt-5.6-luna"

    def __init__(self, api_keys: List[str], timeout_seconds: int = 60):
        self.api_keys = api_keys if api_keys else ["MOCK_KEY"]
        self.timeout = timeout_seconds

    def execute_arbitration(
        self,
        prompt_text: str,
        fallback_mos_schedule: np.ndarray,
        p_cap_ac: float
    ) -> Dict[str, Any]:
        """
        Executes Day-Ahead LLM strategic arbitration with key rotation.
        Returns validated JSON payload or deterministic fallback on failure.
        """
        for attempt, key in enumerate(self.api_keys):
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }

            payload = {
                "model": self.MODEL_NAME,
                "temperature": 0.20,
                "max_tokens": 2500,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "You are an expert Day-Ahead Solar Power Dispatch Scheduler. Your goal is to maximize schedule accuracy and minimize regulatory DSM shortfall penalties."
                    },
                    {
                        "role": "user",
                        "content": prompt_text
                    }
                ]
            }

            try:
                response = requests.post(
                    self.OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout
                )

                if response.status_code == 200:
                    data = response.json()
                    raw_content = data['choices'][0]['message']['content']
                    
                    # Strip markdown fences if present
                    clean_content = raw_content.replace("```json", "").replace("```", "").strip()
                    parsed_json = json.loads(clean_content)
                    
                    # Sanitize block predictions
                    block_preds = parsed_json.get("block_predictions", {})
                    pred_vector = np.zeros(96)
                    for b in range(1, 97):
                        str_b = str(b)
                        val = float(block_preds.get(str_b, fallback_mos_schedule[b-1]))
                        val = max(0.0, min(val, p_cap_ac))
                        pred_vector[b-1] = val
                    
                    parsed_json["validated_schedule_vector"] = pred_vector
                    parsed_json["source"] = "OPENROUTER_GPT_5.6_LUNA"
                    return parsed_json

                elif response.status_code in (429, 503):
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                else:
                    break

            except Exception as e:
                time.sleep(1)
                continue

        # Deterministic Fallback Path (0ms SLA)
        return {
            "synoptic_regime": "FALLBACK_PHYSICS",
            "quantile_bias_factor": 1.00,
            "preferred_nwp_family": "MOS_CONSENSUS",
            "is_maintenance_outage": False,
            "reasoning": "API key pool exhausted or request timed out; fell back deterministically to Module 5 Day-Ahead MOS Consensus.",
            "validated_schedule_vector": fallback_mos_schedule,
            "source": "DAY_AHEAD_MOS_FALLBACK"
        }
