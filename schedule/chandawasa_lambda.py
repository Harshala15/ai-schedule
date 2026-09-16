"""Chandawasa 10 MW Wind Plant Intellis AI Schedule Generation Lambda Entrypoint.

Binds SITE_ID to CHANDAWASA and invokes the Intellis AI generic scheduler.
Triggered every 30 minutes from 06:00 to 21:00 IST via EventBridge.
"""

from __future__ import annotations

import os
from typing import Any

from intellis_ai_lambda import lambda_handler as _shared_lambda_handler


def lambda_handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    event = dict(event or {})
    event.setdefault("site_id", "CHANDAWASA")
    os.environ.setdefault("PLANT_NAME", "CHANDAWASA")
    os.environ.setdefault("SITE_ID", "CHANDAWASA")
    os.environ.setdefault("PLANT_TYPE", "WIND")
    return _shared_lambda_handler(event, context)


if __name__ == "__main__":
    test_event = {
        "site_id": "CHANDAWASA",
        "target_date": "2026-09-16",
        "target_time": "06:00",
        "bucket": "vedanjay-schedules-test-608744602858",
    }
    print("Testing Chandawasa Lambda handler locally with event:", test_event)
