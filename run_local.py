"""Intellis AI Root CLI Launcher.

Runs the local schedule generator from the repository root.
Example:
    python run_local.py --plant GSNP --time 10:00
"""

import sys
from pathlib import Path

# Add schedule directory to path
schedule_dir = Path(__file__).resolve().parent / "schedule"
if str(schedule_dir) not in sys.path:
    sys.path.insert(0, str(schedule_dir))

from run_local import main

if __name__ == "__main__":
    main()
