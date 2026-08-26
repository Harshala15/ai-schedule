from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bootstrap_environment(plant_name: str) -> Path:
    root = repo_root()
    # Lambda containers cannot write under /var/task, so use /tmp there.
    storage_root = "/tmp" if os.getenv("AWS_LAMBDA_FUNCTION_NAME") else str(root)
    os.environ.setdefault("SIMOUR_STORAGE_ROOT", storage_root)
    os.environ["PLANT_NAME"] = plant_name
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def add_repo_root_to_path() -> Path:
    root = repo_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root

