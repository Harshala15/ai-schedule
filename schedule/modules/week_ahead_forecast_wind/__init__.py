"""
Week-Ahead Forecast Module for Wind Power Plants.
Generates 7-day rolling 672-block statutory schedules using 168h hub-height NWP ensemble,
IEC 61400-12 density correction, and member-level aerodynamic power curves.
"""

from .wa_wind_engine import generate_wind_week_ahead_schedule, WAWindEngine

__all__ = ["generate_wind_week_ahead_schedule", "WAWindEngine"]
