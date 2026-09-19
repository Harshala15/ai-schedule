"""Jewli 100.8 MW Wind Power Plant Intellis AI Schedule Generation Lambda Entrypoint.

Binds SITE_ID to JEWLI and invokes the Intellis AI wind scheduler.
Runs the high-precision calibrated multi-model wind ensemble (Jensen-safe, slot-selected,
rolling bias corrected, and SCADA telemetry blended) without LLM intervention.
Continuous 24-hour wind operation under Maharashtra MERC regulations.
"""

from __future__ import annotations

import os
from typing import Any

from intellis_ai_lambda import lambda_handler as _shared_lambda_handler


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    event = dict(event or {})
    event.setdefault("site_id", "JEWLI")
    os.environ.setdefault("PLANT_NAME", "JEWLI")
    os.environ.setdefault("SITE_ID", "JEWLI")
    os.environ.setdefault("PLANT_TYPE", "WIND")
    # Strict Guardrail: Do NOT use LLM for Jewli until explicitly enabled
    os.environ["USE_LLM_FOR_WIND"] = "false"
    os.environ["USE_LLM_JEWLI"] = "false"
    return _shared_lambda_handler(event, context)


if __name__ == "__main__":
    test_event = {
        "site_id": "JEWLI",
        "target_date": "2026-09-16",
        "target_time": "18:15",
        "bucket": "vedanjay-schedules-test-608744602858",
    }
    print("Testing Jewli Lambda handler locally with event:", test_event)
