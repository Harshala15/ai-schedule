"""
test_hybrid_imputation.py

Verifies that:
1. Valid numeric meter blocks are retained exactly as-is (is_imputed = False).
2. NA/missing blocks with ground POA are imputed from on-site POA (is_imputed = True, source = ground_poa).
3. NA/missing blocks with ground GHI are imputed from on-site GHI (is_imputed = True, source = ground_ghi).
4. NA/missing blocks with no ground sensors are imputed from satellite GTI (is_imputed = True, source = satellite_solar_radiation).
5. 15-minute time series is completely continuous (zero dropped blocks).
6. summarize_intraday_state() flags imputed blocks cleanly.
7. accuracy_tracker.py parses and reports NA rows without crashing.
"""

import csv
import datetime as dt
from pathlib import Path
import config
from modules.feedback import daily_feedback
import accuracy_tracker


def run_test():
    test_csv = Path("scratch/test_sirmour_hybrid_meter.csv")
    test_csv.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "TimeStamp",
        "Active Power (MW)",
        "POA (W/m2)",
        "GHI (W/m2)",
        "AMB TEMP",
        "MOD TEMP",
        "Wind Speed (m/s)",
        "Wind Direction (DEG.)",
        "Humidity",
    ]

    # 12 blocks on 2026-08-16 from 07:00 to 09:45
    # Block 1-6 (07:00 - 08:15): Valid Meter
    # Block 7 (08:30): NA power, Ground POA = 450
    # Block 8 (08:45): NA power, Ground POA = NA, Ground GHI = 500
    # Block 9 (09:00): NA power, Ground POA = NA, Ground GHI = NA (Forces Satellite GTI)
    # Block 10-12 (09:15 - 09:45): Valid Meter
    rows_data = [
        ("2026-08-16 07:00:00", "1.850", "120.0", "110.0", "26.5", "27.0", "2.1", "180.0", "75.0"),
        ("2026-08-16 07:15:00", "2.920", "180.0", "165.0", "27.0", "28.5", "2.3", "182.0", "74.0"),
        ("2026-08-16 07:30:00", "4.100", "250.0", "230.0", "27.5", "30.0", "2.4", "185.0", "73.0"),
        ("2026-08-16 07:45:00", "5.450", "320.0", "295.0", "28.0", "32.0", "2.5", "188.0", "71.0"),
        ("2026-08-16 08:00:00", "6.800", "390.0", "360.0", "28.5", "34.0", "2.6", "190.0", "70.0"),
        ("2026-08-16 08:15:00", "8.250", "460.0", "425.0", "29.0", "36.0", "2.8", "192.0", "68.0"),
        # Block 7: NA power, ground POA present
        ("2026-08-16 08:30:00", "NA", "520.0", "480.0", "29.5", "38.0", "2.9", "195.0", "66.0"),
        # Block 8: NA power, ground POA is NA, ground GHI present
        ("2026-08-16 08:45:00", "NA", "NA", "540.0", "30.0", "39.5", "3.0", "197.0", "65.0"),
        # Block 9: NA power, POA is NA, GHI is NA -> triggers Satellite Solar Radiation
        ("2026-08-16 09:00:00", "NA", "NA", "NA", "30.5", "41.0", "3.1", "200.0", "64.0"),
        # Block 10-12: Valid Meter
        ("2026-08-16 09:15:00", "11.200", "650.0", "600.0", "31.0", "42.5", "3.2", "202.0", "63.0"),
        ("2026-08-16 09:30:00", "12.150", "710.0", "655.0", "31.5", "44.0", "3.3", "205.0", "62.0"),
        ("2026-08-16 09:45:00", "13.050", "760.0", "700.0", "32.0", "45.0", "3.4", "208.0", "60.0"),
    ]

    with open(test_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in rows_data:
            writer.writerow(r)

    print(f"Created synthetic test SCADA file with 12 blocks (3 NA blocks) at {test_csv}")

    ref_time = dt.datetime(2026, 8, 16, 9, 45)
    intraday_rows = daily_feedback._load_intraday_meter_rows(test_csv, ref_time)

    print(f"\n--- 1. Testing _load_intraday_meter_rows ---")
    print(f"Total rows loaded: {len(intraday_rows)} (Expected: 12)")
    assert len(intraday_rows) == 12, f"Expected 12 rows, got {len(intraday_rows)}"

    for idx, row in enumerate(intraday_rows):
        ts_str = row["timestamp"].strftime("%H:%M")
        mw = row["active_power_mw"]
        is_imp = row["is_imputed"]
        src = row["impute_source"]
        print(f"  Block {idx+1:2d} [{ts_str}]: {mw:6.3f} MW | Imputed={str(is_imp):5s} | Source={src}")

    # Assertions for specific blocks
    # Block 1: Valid meter 1.850 MW
    assert intraday_rows[0]["is_imputed"] is False
    assert abs(intraday_rows[0]["active_power_mw"] - 1.850) < 1e-3
    assert intraday_rows[0]["impute_source"] == "meter"

    # Block 6: Valid meter 8.250 MW
    assert intraday_rows[5]["is_imputed"] is False
    assert abs(intraday_rows[5]["active_power_mw"] - 8.250) < 1e-3

    # Block 7 (08:30): Imputed from Ground POA (520 W/m2 -> ~2.113 MW for 5.1 MW plant)
    b7 = intraday_rows[6]
    assert b7["is_imputed"] is True
    assert b7["impute_source"] == "ground_poa"
    assert 1.5 < b7["active_power_mw"] < 3.0, f"Unexpected MW: {b7['active_power_mw']}"

    # Block 8 (08:45): Imputed from Ground GHI (540 W/m2 -> ~2.181 MW for 5.1 MW plant)
    b8 = intraday_rows[7]
    assert b8["is_imputed"] is True
    assert b8["impute_source"] == "ground_ghi"
    assert 1.5 < b8["active_power_mw"] < 3.0, f"Unexpected MW: {b8['active_power_mw']}"

    # Block 9 (09:00): Imputed from Satellite Solar Radiation
    b9 = intraday_rows[8]
    assert b9["is_imputed"] is True
    assert b9["impute_source"] == "satellite_solar_radiation"
    assert 0.2 < b9["active_power_mw"] < 5.1, f"Unexpected MW: {b9['active_power_mw']}"

    # Block 12: Valid meter 13.050 MW
    assert intraday_rows[11]["is_imputed"] is False
    assert abs(intraday_rows[11]["active_power_mw"] - 13.050) < 1e-3

    print("\n--- 2. Testing summarize_intraday_state ---")
    state = daily_feedback.summarize_intraday_state(test_csv, ref_time)
    print(f"Summary: {state['summary']}")
    print(f"Latest MW: {state['latest_mw']:.3f} MW")
    print(f"Peak MW: {state['today_max_mw']:.3f} MW")
    print(f"Imputed count: {state['imputed_blocks_count']}/{state['total_blocks_count']}")
    print(f"Imputed sources: {state['imputed_sources']}")

    assert state["imputed_blocks_count"] == 3
    assert state["total_blocks_count"] == 12
    assert "imputed_blocks=3/12" in state["summary"]

    print("\n--- 3. Testing format_intraday_actuals_for_prompt ---")
    prompt_text = daily_feedback.format_intraday_actuals_for_prompt(test_csv, ref_time)
    print(prompt_text)
    assert "[imputed from" in prompt_text

    print("\n--- 4. Testing accuracy_tracker.py ---")
    # Generate a matching dummy prediction CSV
    pred_csv = Path("scratch/test_predictions.csv")
    with open(pred_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Time", "Predicted Generation (MW)"])
        for r in rows_data:
            ts = r[0][:16]
            pred_val = float(r[1]) if r[1] != "NA" else 9.5
            writer.writerow([ts, str(pred_val + 0.2)])  # slight 0.2 MW error

    acc_result = accuracy_tracker.compute_accuracy(
        predictions_csv=pred_csv,
        actual_csv=test_csv,
        timestamp_column="TimeStamp",
        generation_column="Active Power (MW)",
    )
    print(f"Accuracy tracker result: {acc_result}")
    assert acc_result["matched_points"] == 12
    assert acc_result["imputed_blocks"] == 3
    print("\nAll hybrid imputation checks PASSED successfully!")


if __name__ == "__main__":
    run_test()
