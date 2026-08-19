from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bootstrap_environment(plant_name: str) -> Path:
    root = repo_root()
    os.environ.setdefault("SIMOUR_STORAGE_ROOT", str(root))
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

