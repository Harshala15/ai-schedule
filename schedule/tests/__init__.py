"""Test package initialization ensuring schedule root is in sys.path."""

import sys
from pathlib import Path

SCHEDULE_ROOT = Path(__file__).resolve().parents[1]
if str(SCHEDULE_ROOT) not in sys.path:
    sys.path.insert(0, str(SCHEDULE_ROOT))
