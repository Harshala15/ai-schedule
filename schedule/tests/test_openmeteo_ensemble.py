"""Test script for Open-Meteo 51-member ensemble module."""

import datetime as dt
import json
import sys
from pathlib import Path

# Add schedule directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from modules.weather import openmeteo_ensemble, ecmwf_weather


def run_tests():
    print("=" * 60)
    print("Testing Open-Meteo 51-Member Ensemble Module")
    print("=" * 60)

    test_dt = dt.datetime(2026, 8, 22, 14, 15)
    plant_name = "BHUPALPALLY"
    config.PLANT_NAME = plant_name
    config.PLANT_LAT = 18.447931
    config.PLANT_LON = 79.877263
    config.PLANT_CAPACITY_MW = 10.0
    config.PERFORMANCE_RATIO = 0.78

    print(f"\n1. Testing 51-Member Ensemble Fetch & Calibration for {plant_name} at {test_dt}...")
    report = openmeteo_ensemble.fetch_openmeteo_ensemble_calibrated_summary(
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
        reference_time=test_dt,
        hours_ahead=3,
        plant_name=plant_name,
        top_k=15,
    )

    print(f"  Source: {report.get('source')}")
    print(f"  Rows count: {len(report.get('rows', []))}")
    print("\n  Summary text preview:")
    print("  " + "\n  ".join(report.get("summary", "").splitlines()[:6]))

    selection_report = report.get("selection_report", {})
    print(f"\n  Selection report:")
    print(f"    Available: {selection_report.get('available')}")
    print(f"    Days used: {selection_report.get('days_used')}")
    print(f"    Top K selected: {selection_report.get('top_k')}")
    if selection_report.get("selected_members"):
        print(f"    Sample selected members: {selection_report['selected_members'][:5]}")

    print("\n2. Testing 15-Minute Clear-Sky Index (k_t) Interpolation...")
    hourly_times = ["2026-08-22T14:00", "2026-08-22T15:00", "2026-08-22T16:00", "2026-08-22T17:00"]
    hourly_gti = [650.0, 520.0, 340.0, 150.0]
    target_15min = [
        dt.datetime(2026, 8, 22, 14, 15),
        dt.datetime(2026, 8, 22, 14, 30),
        dt.datetime(2026, 8, 22, 14, 45),
        dt.datetime(2026, 8, 22, 15, 0),
    ]
    interp_15min = openmeteo_ensemble.interpolate_15min_clearsky_index(
        hourly_times,
        hourly_gti,
        target_15min,
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
    )
    print(f"  Hourly GTI input: {hourly_gti}")
    print(f"  Interpolated 15-min GTI output: {interp_15min}")
    assert len(interp_15min) == 4, "Interpolation output count mismatch"
    assert all(val > 0 for val in interp_15min), "Interpolated GTI must be positive"

    print("\n3. Testing ecmwf_weather.fetch_ecmwf_weather_summary() integration...")
    weather_summary = ecmwf_weather.fetch_ecmwf_weather_summary(
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
        reference_time=test_dt,
        hours_ahead=3,
    )
    print(f"  Source: {weather_summary.get('source')}")
    print(f"  Rows count: {len(weather_summary.get('rows', []))}")
    print("\n  Prompt text preview:")
    print("  " + "\n  ".join(weather_summary.get("prompt_text", "").splitlines()[:7]))

    print("\n" + "=" * 60)
    print("ALL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    run_tests()
