"""
test_multigen_da_wa_pipeline.py

End-to-end statutory Day-Ahead (DA) and Week-Ahead (WA) pipeline verification test
for all 3 multiple generator solar plants:
- ENRICH: 3 sub-assets (CLIMATEDETOX, EMIL, UPL)
- ZTRIC: 8 sub-assets (DE_SOLAR, GAJLAXMI, CHAKUR_ONE_BLOCK_1, CHAKUR_ONE_BLOCK_2, POLYBOND, SNHEAT, INTEGRATED, INDIQUBE)
- SHAHA: 3 sub-assets (PRANAV, SIDDEHESH, LOKGREENB2)

Verifies:
1. Canonical statutory DA columns (Block_No, From, To, <SUB_ASSETS...>, <PLANT>_MW, <PLANT>_AvC_MW)
2. Day-night solar physics zeroing (blocks 1-24 & 75-96)
3. Plant status change & outage arbitration (asset-level & park-level)
4. 672-block statutory WA format (Block_No, From, To, SCH_MW, AvC_MW)
5. Seamless routing through generate_solar_day_ahead_schedule and intellis_dayahead_lambda
"""

import os
import csv
import datetime as dt
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import pandas as pd

from modules.multi_generator.multi_generator_engine import (
    MultiGeneratorEngine,
    generate_multi_generator_day_ahead_schedule,
)
from modules.day_ahead_forecast_solar.solar_da_engine import generate_solar_day_ahead_schedule
import intellis_dayahead_lambda


def test_enrich_day_ahead_statutory_schema():
    """Verify statutory DA column schema and 96-block generation for ENRICH."""
    engine = MultiGeneratorEngine()
    target_date = "2026-10-15"
    today_str = "2026-10-14"

    res = engine.generate_and_dispatch_multi_generator_schedules(
        plant_name="ENRICH",
        target_date_str=target_date,
        today_str=today_str,
        run_tag="da0",
    )

    assert res["total_da_blocks"] == 96
    expected_cols = ["Block_No", "From", "To", "CLIMATEDETOX", "EMIL", "UPL", "ENRICH_MW", "ENRICH_AvC_MW"]
    assert res["da_columns"] == expected_cols

    df = pd.read_csv(res["da_local_path"])
    assert len(df) == 96
    assert list(df.columns) == expected_cols

    # Night blocks 1-20 must be zero
    assert (df.loc[0:20, "ENRICH_MW"] == 0.0).all()
    # Noon blocks 40-55 must be positive
    assert (df.loc[40:55, "ENRICH_MW"] > 0.0).all()
    # Available capacity must equal total nominal AC (7.62 MW)
    assert (df["ENRICH_AvC_MW"] == 7.62).all()


def test_ztric_day_ahead_statutory_schema():
    """Verify statutory DA column schema and 96-block generation for ZTRIC."""
    engine = MultiGeneratorEngine()
    target_date = "2026-10-15"
    today_str = "2026-10-14"

    res = engine.generate_and_dispatch_multi_generator_schedules(
        plant_name="ZTRIC",
        target_date_str=target_date,
        today_str=today_str,
        run_tag="da0",
    )

    assert res["total_da_blocks"] == 96
    expected_cols = [
        "Block_No", "From", "To",
        "DE_SOLAR", "GAJLAXMI", "CHAKUR_ONE_BLOCK_1", "CHAKUR_ONE_BLOCK_2",
        "POLYBOND", "SNHEAT", "INTEGRATED", "INDIQUBE",
        "ZTRIC_MW", "ZTRIC_AvC_MW",
    ]
    assert res["da_columns"] == expected_cols

    df = pd.read_csv(res["da_local_path"])
    assert len(df) == 96
    assert list(df.columns) == expected_cols
    assert (df["ZTRIC_AvC_MW"] == 19.15).all()


def test_shaha_day_ahead_statutory_schema():
    """Verify statutory DA column schema and 96-block generation for SHAHA."""
    engine = MultiGeneratorEngine()
    target_date = "2026-10-15"
    today_str = "2026-10-14"

    res = engine.generate_and_dispatch_multi_generator_schedules(
        plant_name="SHAHA",
        target_date_str=target_date,
        today_str=today_str,
        run_tag="da0",
    )

    assert res["total_da_blocks"] == 96
    expected_cols = ["Block_No", "From", "To", "PRANAV", "SIDDEHESH", "LOKGREENB2", "SHAHA_MW", "SHAHA_AvC_MW"]
    assert res["da_columns"] == expected_cols

    df = pd.read_csv(res["da_local_path"])
    assert len(df) == 96
    assert list(df.columns) == expected_cols
    assert (df["SHAHA_AvC_MW"] == 12.17).all()


def test_enrich_da_outage_accommodation():
    """Verify that an active outage on CLIMATEDETOX zeroes only that sub-asset and recalibrates park AvC."""
    target_date = "2026-10-15"
    mock_windows = [
        {
            "window_id": "MOCK-CD-OUTAGE",
            "asset_scope": "asset",
            "asset_id": "CLIMATEDETOX",
            "start_time": f"{target_date}T00:00:00+05:30",
            "end_time": f"{target_date}T23:59:00+05:30",
            "plant_status": "SHUTDOWN",
            "control_type": "SHUTDOWN",
            "action": "SHUTDOWN",
            "control_capacity_ac_mw": 0.0,
            "control_capacity_dc_mw": 0.0,
        }
    ]

    with patch("modules.control_windows.PlantControlWindowEngine.load_active_windows", return_value=mock_windows):
        engine = MultiGeneratorEngine()
        df = engine.build_da_schedule_dataframe(
            plant_name="ENRICH",
            target_date_str=target_date,
            unconstrained_total_mw_96=engine.compute_solar_unconstrained_base_curve(7.62, target_date),
        )

        assert len(df) == 96
        # CLIMATEDETOX must be 0.00 across all 96 blocks
        assert (df["CLIMATEDETOX"].astype(float) == 0.0).all()
        # Active capacity must be derated by 5.0 MW (7.62 - 5.00 = 2.62 MW)
        assert (df["ENRICH_AvC_MW"].astype(float) == 2.62).all()
        # Other assets (EMIL: 1.62 MW, UPL: 1.0 MW) continue to generate during daytime
        assert (df.loc[45:50, "EMIL"].astype(float) > 0.0).all()
        assert (df.loc[45:50, "UPL"].astype(float) > 0.0).all()


def test_week_ahead_statutory_672_blocks():
    """Verify statutory 7-day 672-block Week-Ahead schedule for ENRICH."""
    engine = MultiGeneratorEngine()
    start_date = "2026-10-15"

    df_wa = engine.build_wa_schedule_dataframe(
        plant_name="ENRICH",
        start_date_str=start_date,
        unconstrained_7day_mw_672=engine.compute_solar_unconstrained_base_curve(7.62, start_date).repeat(7),
    )

    assert len(df_wa) == 672
    assert list(df_wa.columns) == ["Block_No", "From", "To", "SCH_MW", "AvC_MW"]

    # Block_No must reset to 1 on each day
    assert int(df_wa.iloc[0]["Block_No"]) == 1
    assert int(df_wa.iloc[95]["Block_No"]) == 96
    assert int(df_wa.iloc[96]["Block_No"]) == 1
    assert int(df_wa.iloc[671]["Block_No"]) == 96


def test_solar_da_engine_routes_multi_generator():
    """Verify that generate_solar_day_ahead_schedule routes ENRICH to statutory multi-generator format."""
    res = generate_solar_day_ahead_schedule(
        plant_name="ENRICH",
        target_date_str="2026-10-15",
        run_tag="da0",
    )
    assert res["plant_name"] == "ENRICH"
    expected_cols = ["Block_No", "From", "To", "CLIMATEDETOX", "EMIL", "UPL", "ENRICH_MW", "ENRICH_AvC_MW"]
    assert res["columns"] == expected_cols
    assert Path(res["local_csv_path"]).exists()


def test_day_ahead_lambda_routes_multi_generator():
    """Verify that intellis_dayahead_lambda handles ENRICH with MULTI_GENERATOR_STATUTORY regime."""
    event = {
        "plant": "ENRICH",
        "date": "2026-10-15",
        "run_type": "da0",
    }
    summary = intellis_dayahead_lambda.lambda_handler(event)
    assert summary["status"] == "OK"
    assert summary["successful"] == 1
    res = summary["results"][0]
    assert res["plant_name"] == "ENRICH"
    assert res["regime"] == "MULTI_GENERATOR_STATUTORY"
    assert res["columns"] == ["Block_No", "From", "To", "CLIMATEDETOX", "EMIL", "UPL", "ENRICH_MW", "ENRICH_AvC_MW"]
