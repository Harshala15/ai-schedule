"""JGBPL 50.0 MW Wind Power Plant Intellis AI Schedule Generation Lambda Entrypoint.

Binds SITE_ID to JGBPL and invokes the Intellis AI wind scheduler.
Runs the high-precision calibrated multi-model wind ensemble (Jensen-safe, slot-selected,
rolling bias corrected, and empirical shape calibrated) without LLM intervention.
Continuous 24-hour wind operation under Maharashtra MERC regulations.
"""

from __future__ import annotations

import os
from typing import Any

from intellis_ai_lambda import lambda_handler as _shared_lambda_handler


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    event = dict(event or {})
    event.setdefault("site_id", "JGBPL")
    os.environ.setdefault("PLANT_NAME", "JGBPL")
    os.environ.setdefault("SITE_ID", "JGBPL")
    os.environ.setdefault("PLANT_TYPE", "WIND")
    # Strict Guardrail: Do NOT use LLM for wind plants
    os.environ["USE_LLM_FOR_WIND"] = "false"
    os.environ["USE_LLM_JEWLI"] = "false"
    return _shared_lambda_handler(event, context)


if __name__ == "__main__":
    test_event = {
        "site_id": "JGBPL",
        "target_date": "2026-09-22",
        "target_time": "13:15",
        "bucket": "vedanjay-schedules-test-608744602858",
    }
    print("Testing JGBPL Lambda handler locally with event:", test_event)
    res = lambda_handler(test_event, None)
    print("Status:", res.get("status"))
