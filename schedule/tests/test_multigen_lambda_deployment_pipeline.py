"""
test_multigen_lambda_deployment_pipeline.py

End-to-end pipeline verification test for all 3 multiple generator plants:
- ENRICH: CLIMATEDETOX shutdown accommodation
- SHAHA: SIDDEHESH shutdown accommodation
- ZTRIC: Open-ended outage accommodation through 96th block

Validates that when the current code is deployed to AWS Lambda:
1. Routing in write_full_block_schedule_from_llm_schedule works for each plant
2. Correct CSV schemas are produced (7 cols for ENRICH/SHAHA, 14 cols for ZTRIC)
3. PlantControlWindowEngine applies outages block-wise
4. Affected sub-assets are strictly 0.00 MW
5. Park totals are recalibrated without data corruption
"""

import csv
import datetime as dt
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import config
from modules import schedule_utils
from modules.control_windows.control_window_engine import PlantControlWindowEngine
from modules.multi_generator.enrich_asset_schedule import ENRICH_CSV_FIELDNAMES
from modules.multi_generator.shaha_asset_schedule import SHAHA_CSV_FIELDNAMES
from modules.multi_generator.ztric_asset_schedule import ZTRIC_CSV_FIELDNAMES


def _create_mock_input_schedule(csv_path: Path, target_date: str = "2026-10-09"):
    """Generates a realistic 96-block daytime solar bell curve for testing."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "Block", "Time Interval (15 minute interval)", "intellis_gti", "intellis_mw", "schedule_mw"
        ])
        writer.writeheader()
        for b in range(1, 97):
            s_min = (b - 1) * 15
            e_min = b * 15
            sh, sm = divmod(s_min, 60)
            eh, em = divmod(e_min, 60)
            eh_str = "00:00" if eh == 24 else f"{eh:02d}:{em:02d}"
            interval = f"{sh:02d}:{sm:02d} - {eh_str}"

            # Bell curve during daylight (blocks 24 to 74)
            if 24 <= b <= 74:
                # Peak around block 48
                dist = abs(b - 48)
                mw = max(0.0, round(5.0 * (1.0 - (dist / 26.0) ** 2), 3))
            else:
                mw = 0.0

            writer.writerow({
                "Block": b,
                "Time Interval (15 minute interval)": interval,
                "intellis_gti": mw * 150.0,
                "intellis_mw": mw,
                "schedule_mw": mw,
            })


def test_enrich_lambda_pipeline_shutdown(tmp_path):
    """Verify ENRICH scheduler produces 7-column CSV with CLIMATEDETOX shut down during outage."""
    input_csv = tmp_path / "enrich_input.csv"
    output_csv = tmp_path / "ENRICH_2026-10-09_penalty_schedule.csv"
    _create_mock_input_schedule(input_csv)

    # Mock DynamoDB control window for ENRICH CLIMATEDETOX from 14:30 to 16:00 (Blocks 59 to 64)
    mock_windows = [
        {
            "site_id": "ENRICH",
            "window_id": "ENRICH#SHUTDOWN#2026-10-09#14-30#1",
            "asset_scope": "asset",
            "asset_id": "CLIMATEDETOX",
            "asset_name": "CLIMATEDETOX",
            "plant_status": "SHUTDOWN",
            "control_mode": "FULL",
            "active": True,
            "start_time": "2026-10-09T14:30:00+05:30",
            "end_time": "2026-10-09T16:00:00+05:30",
            "is_open_ended": False,
        }
    ]

    with patch.object(config, "PLANT_NAME", "ENRICH"), \
         patch.object(PlantControlWindowEngine, "load_active_windows", return_value=mock_windows):
        
        summary = schedule_utils.write_full_block_schedule_from_llm_schedule(
            input_csv_path=input_csv,
            output_csv_path=output_csv,
            target_date="2026-10-09",
        )

        assert summary["format"] == "ENRICH_MULTI_GENERATOR_ASSET_WISE"
        assert output_csv.exists()

        with open(output_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == ENRICH_CSV_FIELDNAMES
            rows = list(reader)
            assert len(rows) == 96

            # Blocks 59 to 64 (14:30 to 16:00) must have CLIMATEDETOX = 0.000
            for r in rows:
                b = int(r["block"])
                cd_mw = float(r["CLIMATEDETOX"])
                emil_mw = float(r["EMIL"])
                upl_mw = float(r["UPL"])
                tot_mw = float(r["total_ai_schedule_mw"])

                if 59 <= b <= 64:
                    assert cd_mw == 0.0, f"Block {b} CLIMATEDETOX must be 0.00 during shutdown"
                    assert round(emil_mw + upl_mw, 3) == tot_mw, f"Block {b} total must match EMIL + UPL"
                elif 35 <= b <= 55:
                    assert cd_mw > 0.0, f"Block {b} daylight CLIMATEDETOX should be active"


def test_shaha_lambda_pipeline_shutdown(tmp_path):
    """Verify SHAHA scheduler produces 7-column CSV with SIDDEHESH shut down during outage."""
    input_csv = tmp_path / "shaha_input.csv"
    output_csv = tmp_path / "SHAHA_2026-10-09_penalty_schedule.csv"
    _create_mock_input_schedule(input_csv)

    # Mock DynamoDB control window for SHAHA SIDDEHESH from 14:30 to 16:00 (Blocks 59 to 64)
    mock_windows = [
        {
            "site_id": "SHAHA",
            "window_id": "SHAHA#SHUTDOWN#2026-10-09#14-30#1",
            "asset_scope": "asset",
            "asset_id": "SIDDEHESH",
            "asset_name": "SIDDEHESH",
            "plant_status": "SHUTDOWN",
            "control_mode": "FULL",
            "active": True,
            "start_time": "2026-10-09T14:30:00+05:30",
            "end_time": "2026-10-09T16:00:00+05:30",
            "is_open_ended": False,
        }
    ]

    with patch.object(config, "PLANT_NAME", "SHAHA"), \
         patch.object(PlantControlWindowEngine, "load_active_windows", return_value=mock_windows):
        
        summary = schedule_utils.write_full_block_schedule_from_llm_schedule(
            input_csv_path=input_csv,
            output_csv_path=output_csv,
            target_date="2026-10-09",
        )

        assert summary["format"] == "SHAHA_MULTI_GENERATOR_ASSET_WISE"
        assert output_csv.exists()

        with open(output_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == SHAHA_CSV_FIELDNAMES
            rows = list(reader)
            assert len(rows) == 96

            # Blocks 59 to 64 (14:30 to 16:00) must have SIDDEHESH = 0.000
            for r in rows:
                b = int(r["block"])
                sidd_mw = float(r["SIDDEHESH"])
                pran_mw = float(r["PRANAV"])
                lok_mw = float(r["LOKGREENB2"])
                tot_mw = float(r["total_ai_schedule_mw"])

                if 59 <= b <= 64:
                    assert sidd_mw == 0.0, f"Block {b} SIDDEHESH must be 0.00 during shutdown"
                    assert round(pran_mw + lok_mw, 3) == tot_mw, f"Block {b} total must match active assets"
                elif 35 <= b <= 55:
                    assert sidd_mw > 0.0, f"Block {b} daylight SIDDEHESH should be active"


def test_ztric_lambda_pipeline_open_ended_outage(tmp_path):
    """Verify ZTRIC scheduler handles open-ended outage extending through Block 96."""
    input_csv = tmp_path / "ztric_input.csv"
    output_csv = tmp_path / "ZTRIC_2026-10-09_penalty_schedule.csv"
    _create_mock_input_schedule(input_csv)

    # Open-ended outage starting at 12:00 IST (Block 49) with end_time None and is_open_ended True
    mock_windows = [
        {
            "site_id": "ZTRIC",
            "window_id": "ZTRIC#SHUTDOWN#2026-10-09#12-00#1",
            "asset_scope": "asset",
            "asset_id": "CHAKUR_ONE_BLOCK_1",
            "asset_name": "CHAKUR_ONE_BLOCK_1",
            "plant_status": "SHUTDOWN",
            "control_mode": "FULL",
            "active": True,
            "start_time": "2026-10-09T12:00:00+05:30",
            "end_time": None,
            "is_open_ended": True,
        }
    ]

    with patch.object(config, "PLANT_NAME", "ZTRIC"), \
         patch.object(PlantControlWindowEngine, "load_active_windows", return_value=mock_windows):
        
        summary = schedule_utils.write_full_block_schedule_from_llm_schedule(
            input_csv_path=input_csv,
            output_csv_path=output_csv,
            target_date="2026-10-09",
        )

        assert summary["format"] == "ZTRIC_MULTI_GENERATOR_ASSET_WISE"
        assert output_csv.exists()

        with open(output_csv, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == ZTRIC_CSV_FIELDNAMES
            rows = list(reader)
            assert len(rows) == 96

            # From Block 49 through Block 96, CHAKUR_ONE_BLOCK_1 must be strictly 0.00
            for r in rows:
                b = int(r["block"])
                c1_mw = float(r["CHAKUR_ONE_BLOCK_1"])
                if b >= 49:
                    assert c1_mw == 0.0, f"Block {b} CHAKUR_ONE_BLOCK_1 must be 0.00 for open-ended outage"
                elif 35 <= b < 49:
                    assert c1_mw > 0.0, f"Block {b} prior to 12:00 should have normal positive generation"
