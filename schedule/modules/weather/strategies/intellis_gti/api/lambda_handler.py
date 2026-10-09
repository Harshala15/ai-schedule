"""AWS Lambda adapter entrypoint for Intellis GTI Commercial API."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure schedule directory is on python path
_SCHEDULE_DIR = Path(__file__).resolve().parents[5]
if str(_SCHEDULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SCHEDULE_DIR))

from modules.weather.strategies.intellis_gti.api.app import app

try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    def handler(event, context):
        return {
            "statusCode": 500,
            "body": "Mangum is not installed in the environment. Install 'mangum' to run on AWS Lambda.",
        }
