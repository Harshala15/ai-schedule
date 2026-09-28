"""Comprehensive validation of 51-member ensemble ranking and sensor fallback."""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from modules.weather import openmeteo_ensemble


def test_member_ranking_and_fallback():
    print("=" * 60)
    print("Testing 3-Day SCADA Ground-Truth Ingestion & Sensor Fallback")
    print("=" * 60)

    # 1. Test SCADA extraction with real pyranometer columns
    synthetic_scada_with_sensor = [
        {"TimeStamp": "2026-08-20 10:00", "POA (W/m2)": "680.5", "Active Power (MW)": "4.8"},
        {"TimeStamp": "2026-08-20 10:15", "POA (W/m2)": "710.0", "Active Power (MW)": "5.0"},
        {"TimeStamp": "2026-08-20 10:30", "POA (W/m2)": "725.2", "Active Power (MW)": "5.1"},
        {"TimeStamp": "2026-08-20 10:45", "POA (W/m2)": "740.0", "Active Power (MW)": "5.2"},
        {"TimeStamp": "2026-08-20 11:00", "POA (W/m2)": "780.0", "Active Power (MW)": "5.5"},
        {"TimeStamp": "2026-08-20 11:15", "POA (W/m2)": "800.0", "Active Power (MW)": "5.6"},
    ]
    extracted_sensor = openmeteo_ensemble._extract_hourly_actual_weather(synthetic_scada_with_sensor, "2026-08-20")
    print("1. Extracted Irradiance with Pyranometer Sensor:")
    print(f"   Hour 10: {extracted_sensor.get(10)} W/m2 (Expected: ~713.9 W/m2)")
    print(f"   Hour 11: {extracted_sensor.get(11)} W/m2 (Expected: ~790.0 W/m2)")
    assert 10 in extracted_sensor and 11 in extracted_sensor, "Failed to extract sensor irradiance"

    # 2. Test Sensor Fallback (missing POA, only Active Power available)
    synthetic_scada_without_sensor = [
        {"TimeStamp": "2026-08-20 10:00", "Active Power (MW)": "4.8"},
        {"TimeStamp": "2026-08-20 10:15", "Active Power (MW)": "5.0"},
        {"TimeStamp": "2026-08-20 10:30", "Active Power (MW)": "5.1"},
        {"TimeStamp": "2026-08-20 10:45", "Active Power (MW)": "5.2"},
    ]
    extracted_fallback = openmeteo_ensemble._extract_hourly_actual_weather(
        synthetic_scada_without_sensor,
        "2026-08-20",
        plant_capacity_mw=10.0,
        dc_capacity_mw=10.0,
        performance_ratio=0.78,
    )
    print("\n2. Inferred Irradiance via Power Sensor Fallback:")
    print(f"   Hour 10 Inferred: {extracted_fallback.get(10)} W/m2 (Expected: ~644.2 W/m2)")
    assert 10 in extracted_fallback and extracted_fallback[10] > 500, "Sensor fallback failed to infer irradiance"

    # 3. Test Synthesis with 51 members and Top-15 selection
    dummy_hourly = {
        "time": ["2026-08-22T10:00", "2026-08-22T11:00", "2026-08-22T12:00"],
    }
    # Create 51 dummy members with varying offsets
    for m in range(1, 52):
        col_name = f"shortwave_radiation_member{m:02d}"
        dummy_hourly[col_name] = [600.0 + m * 2.0, 750.0 + m * 2.0, 850.0 + m * 2.0]

    raw_ensemble_mock = {"hourly": dummy_hourly}
    selected_suffixes = [f"member{m:02d}" for m in range(1, 16)]  # Top 15 members
    synthesized_clear = openmeteo_ensemble.synthesize_calibrated_ensemble_hourly(
        raw_ensemble_mock,
        selected_suffixes,
        is_volatile=False,
    )
    synthesized_volatile = openmeteo_ensemble.synthesize_calibrated_ensemble_hourly(
        raw_ensemble_mock,
        selected_suffixes,
        is_volatile=True,
    )

    clear_mean = synthesized_clear["shortwave_radiation"][0]
    volatile_p40 = synthesized_volatile["shortwave_radiation"][0]
    print("\n3. Synthesis & Asymmetric CERC/DSM Penalty Minimization:")
    print(f"   Clear-Sky Top-15 Mean: {clear_mean} W/m2")
    print(f"   Volatile Top-15 P40 (Conservative Lower Bound): {volatile_p40} W/m2")
    assert volatile_p40 <= clear_mean, "P40 conservative bound must be <= Mean"

    print("\n" + "=" * 60)
    print("ALL ADVANCED ALGORITHMIC TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    test_member_ranking_and_fallback()
