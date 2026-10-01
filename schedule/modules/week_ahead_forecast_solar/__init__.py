"""
Week-Ahead Forecast Module for Solar Power Plants.
Generates 7-day rolling 672-block statutory schedules using multi-day NWP ensemble and physics guardrails.
"""

from .wa_solar_engine import generate_solar_week_ahead_schedule, WASolarEngine

__all__ = ["generate_solar_week_ahead_schedule", "WASolarEngine"]
