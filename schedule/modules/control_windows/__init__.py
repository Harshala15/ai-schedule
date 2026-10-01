"""
Plant Control Windows Module.
Handles loading active plant control windows from DynamoDB (shutdown, curtailment, partial DC derating)
and applying deterministic guardrails block-wise to 96-block schedules.
"""

from .control_window_engine import PlantControlWindowEngine

__all__ = ["PlantControlWindowEngine"]
