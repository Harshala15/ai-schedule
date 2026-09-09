"""
test_balakwada.py

Comprehensive test suite for plant BALAKWADA (7.5 MW AC / 7.6 MW DC):
1. Plant profile and configuration verification.
2. Satellite Solar Radiation Virtual Meter live retrieval for BALAKWADA coordinates.
3. Hybrid Block-Level Imputation using BALAKWADA's SCADA format (Block End / metered_mw) with NA values.
4. Intraday state summarization and prompt formatting.
5. Accuracy evaluation via accuracy_tracker.py.
"""

import csv
import datetime as dt
import os
from pathlib import Path

# Set plant environment to BALAKWADA before importing config
os.environ["PLANT_NAME"] = "BALAKWADA"

import config
import accuracy_tracker
from modules.feedback import daily_feedback
from modules.weather import satellite_virtual_meter


def test_balakwada_configuration():
    print("\n=======================================================")
    print("STEP 1: Verifying BALAKWADA Configuration & Profile")
    print("=======================================================")
    print(f"Plant Name:        {config.PLANT_NAME}")
    print(f"Latitude:          {config.PLANT_LAT}")
    print(f"Longitude:         {config.PLANT_LON}")
    print(f"AC Capacity (MW):  {config.PLANT_CAPACITY_MW}")
    print(f"DC Capacity (MW):  {config.PLANT_DC_CAPACITY_MW}")
    print(f"Max Feed-in (MW):  {config.PLANT_MAX_FEED_IN_MW}")

    assert config.PLANT_NAME == "BALAKWADA"
    assert abs(config.PLANT_LAT - 22.00583333) < 1e-4
    assert abs(config.PLANT_LON - 75.52333333) < 1e-4
    assert abs(config.PLANT_CAPACITY_MW - 7.5) < 1e-3
    assert abs(config.PLANT_DC_CAPACITY_MW - 7.6) < 1e-3
    print("  [PASS] BALAKWADA plant profile verified.")


def test_balakwada_satellite_virtual_meter():
    print("\n=======================================================")
    print("STEP 2: Testing Satellite Virtual Meter for BALAKWADA")
    print("=======================================================")
    ref_time = dt.datetime(2026, 8, 16, 12, 0)  # Solar noon during monsoon
    print(f"Reference Time: {ref_time}")

    sat_info = satellite_virtual_meter.fetch_satellite_irradiance_at_cutoff(
        reference_time=ref_time,
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
        tilt=getattr(config, "PLANT_TILT_DEG", 20.0),
        azimuth=getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 0.0),
    )
    print(f"Satellite Fetch Status: {sat_info.get('status')}")
    print(f"Satellite GTI:          {sat_info.get('gti')} W/m2")
    print(f"Satellite Temperature:  {sat_info.get('temperature')} C")
    print(f"Satellite Cloud Cover:  {sat_info.get('cloud_cover')}%")

    p_virtual = satellite_virtual_meter.calculate_virtual_generation_mw(
        gti_w_per_m2=sat_info.get("gti", 0.0),
        temperature_c=sat_info.get("temperature", 25.0),
        plant_capacity_mw=config.PLANT_CAPACITY_MW,
        dc_capacity_mw=config.PLANT_DC_CAPACITY_MW,
    )
    print(f"Synthetic P_virtual:    {p_virtual:.3f} MW (Capacity Cap = {config.PLANT_CAPACITY_MW} MW)")
    assert 0.0 <= p_virtual <= config.PLANT_CAPACITY_MW

    state = satellite_virtual_meter.build_satellite_virtual_intraday_state(
        reference_time=ref_time,
        latitude=config.PLANT_LAT,
        longitude=config.PLANT_LON,
        tilt=getattr(config, "PLANT_TILT_DEG", 20.0),
        azimuth=getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 0.0),
        plant_capacity_mw=config.PLANT_CAPACITY_MW,
        dc_capacity_mw=config.PLANT_DC_CAPACITY_MW,
    )
    print(f"Virtual State Regime:   {state.get('regime')}")
    print(f"Live Residual Factor:   {state.get('live_residual_factor')}")
    print(f"Summary:                {state.get('summary')}")

    assert state.get("is_virtual_meter") is True
    assert 0.20 <= state.get("live_residual_factor") <= 1.05
    print("  [PASS] Satellite Virtual Meter for BALAKWADA verified.")


def test_balakwada_hybrid_imputation():
    print("\n=======================================================")
    print("STEP 3: Testing Hybrid Imputation on BALAKWADA SCADA Format")
    print("=======================================================")
    # BALAKWADA SCADA format: uses 'Block End' and 'metered_mw' (or 'Active Power (MW)')
    test_csv = Path("scratch/test_balakwada_scada.csv")
    test_csv.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "Block End",
        "metered_mw",
        "POA (W/m2)",
        "GHI (W/m2)",
        "AMB TEMP",
        "MOD TEMP",
    ]

    # 10 blocks on 2026-08-16 from 08:00 to 10:15
    # Blocks 1-4: Valid meter power
    # Block 5: NA power, ground POA present
    # Block 6: NA power, ground POA is NA, ground GHI present
    # Block 7: NA power, all ground sensors NA (satellite GTI fallback)
    # Blocks 8-10: Valid meter power
    scada_data = [
        ("2026-08-16 08:00:00", "2.850", "380.0", "350.0", "28.0", "34.0"),
        ("2026-08-16 08:15:00", "3.420", "440.0", "410.0", "28.5", "36.0"),
        ("2026-08-16 08:30:00", "4.150", "510.0", "470.0", "29.0", "38.0"),
        ("2026-08-16 08:45:00", "4.850", "580.0", "530.0", "29.5", "40.0"),
        # Block 5: NA power, ground POA = 640 W/m2
        ("2026-08-16 09:00:00", "NA", "640.0", "590.0", "30.0", "42.0"),
        # Block 6: NA power, ground POA is NA, ground GHI = 670 W/m2
        ("2026-08-16 09:15:00", "NA", "NA", "670.0", "30.5", "43.0"),
        # Block 7: NA power, all ground sensors NA -> satellite solar radiation
        ("2026-08-16 09:30:00", "NA", "NA", "NA", "31.0", "44.0"),
        # Blocks 8-10: Valid meter
        ("2026-08-16 09:45:00", "6.200", "780.0", "720.0", "31.5", "45.0"),
        ("2026-08-16 10:00:00", "6.650", "830.0", "760.0", "32.0", "46.0"),
        ("2026-08-16 10:15:00", "7.050", "870.0", "800.0", "32.5", "47.0"),
    ]

    with open(test_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in scada_data:
            writer.writerow(row)

    print(f"Created BALAKWADA test SCADA CSV at: {test_csv}")

    ref_time = dt.datetime(2026, 8, 16, 10, 15)
    rows = daily_feedback._load_intraday_meter_rows(test_csv, ref_time)

    print(f"Total rows parsed: {len(rows)} (Expected: 10)")
    assert len(rows) == 10, f"Expected 10 rows, got {len(rows)}"

    for idx, r in enumerate(rows):
        ts_str = r["timestamp"].strftime("%H:%M")
        mw = r["active_power_mw"]
        is_imp = r["is_imputed"]
        src = r["impute_source"]
        print(f"  Block {idx+1:2d} [{ts_str}]: {mw:6.3f} MW | Imputed={str(is_imp):5s} | Source={src}")

    # Verify Block 1: Real meter 2.850 MW
    assert rows[0]["is_imputed"] is False
    assert abs(rows[0]["active_power_mw"] - 2.850) < 1e-3
    assert rows[0]["impute_source"] == "meter"

    # Verify Block 5: Imputed from Ground POA (~3.4 MW for BALAKWADA 7.5 MW capacity)
    b5 = rows[4]
    assert b5["is_imputed"] is True
    assert b5["impute_source"] == "ground_poa"
    assert 2.5 < b5["active_power_mw"] < 4.5, f"Unexpected MW: {b5['active_power_mw']}"

    # Verify Block 6: Imputed from Ground GHI (~3.5 MW for BALAKWADA)
    b6 = rows[5]
    assert b6["is_imputed"] is True
    assert b6["impute_source"] == "ground_ghi"
    assert 2.5 < b6["active_power_mw"] < 4.5, f"Unexpected MW: {b6['active_power_mw']}"

    # Verify Block 7: Imputed from Satellite Solar Radiation
    b7 = rows[6]
    assert b7["is_imputed"] is True
    assert b7["impute_source"] == "satellite_solar_radiation"
    assert 0.5 < b7["active_power_mw"] < 7.5, f"Unexpected MW: {b7['active_power_mw']}"

    # Verify Block 10: Real meter 7.050 MW
    assert rows[9]["is_imputed"] is False
    assert abs(rows[9]["active_power_mw"] - 7.050) < 1e-3

    print("\nIntraday State Summary for BALAKWADA:")
    state = daily_feedback.summarize_intraday_state(test_csv, ref_time)
    print(f"  Latest MW:      {state['latest_mw']:.3f} MW")
    print(f"  Today Peak:     {state['today_max_mw']:.3f} MW")
    print(f"  Imputed Blocks: {state['imputed_blocks_count']}/{state['total_blocks_count']}")
    print(f"  Summary:        {state['summary']}")

    assert state["imputed_blocks_count"] == 3
    assert state["total_blocks_count"] == 10
    assert "imputed_blocks=3/10" in state["summary"]

    prompt_context = daily_feedback.format_intraday_actuals_for_prompt(test_csv, ref_time)
    print(f"\nPrompt Context Snippet:\n{prompt_context}")
    assert "[imputed from" in prompt_context
    print("  [PASS] Hybrid Imputation on BALAKWADA SCADA format verified.")


def test_balakwada_accuracy_tracker():
    print("\n=======================================================")
    print("STEP 4: Testing Accuracy Tracker on BALAKWADA Data")
    print("=======================================================")
    actual_csv = Path("scratch/test_balakwada_scada.csv")
    pred_csv = Path("scratch/test_balakwada_predictions.csv")

    # Generate sample schedule predictions with a realistic ~0.2 MW offset
    with open(actual_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    with open(pred_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Time", "Predicted Generation (MW)"])
        for r in rows:
            ts = r["Block End"][:16]
            raw_mw = r["metered_mw"]
            val = float(raw_mw) if raw_mw != "NA" else 3.8
            writer.writerow([ts, f"{val + 0.15:.3f}"])

    res = accuracy_tracker.compute_accuracy(
        predictions_csv=pred_csv,
        actual_csv=actual_csv,
        timestamp_column="Block End",
        generation_column="metered_mw",
    )
    print("Accuracy Result:", res)
    assert res["matched_points"] == 10
    assert res["imputed_blocks"] == 3
    assert res["mae"] < 0.6  # Good tracking error
    print("  [PASS] Accuracy Tracker for BALAKWADA verified.")


if __name__ == "__main__":
    test_balakwada_configuration()
    test_balakwada_satellite_virtual_meter()
    test_balakwada_hybrid_imputation()
    test_balakwada_accuracy_tracker()
    print("\n=======================================================")
    print("ALL BALAKWADA TESTS COMPLETED SUCCESSFULLY!")
    print("=======================================================")
