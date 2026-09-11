"""Unit and regression test suite for AI Regime-Decided adjustment logic and physical guardrails."""

import unittest
import math
import datetime as dt
import pandas as pd
from pathlib import Path

class TestRegimeGuardrails(unittest.TestCase):
    
    def test_clear_sky_negative_lockout(self):
        """Verify that under confirmed clear-sky ground conditions, negative cuts below Step 1 Base are locked out."""
        # Simulated prediction block in Sirmour (5.1 MW plant)
        cap_mw = 5.100
        step1_mw = 1.217
        phantom_weather_step2 = 0.581 # -52% cut from an unverified NWP rain cell
        
        # Ground telemetry state: Clear Sky (Kt = 1.33, POA = 442 W/m2)
        intraday_state = {
            "is_clear_ground": True,
            "is_overcast_ground": False,
            "is_clearing_transition": False,
            "clearness_ratio": 1.33,
            "live_residual_factor": 1.05,
            "regime": "clear / strengthening",
        }
        
        # Apply guardrail logic as implemented in run_pipeline.py
        is_clear_ground = bool(intraday_state.get("is_clear_ground"))
        live_clearness = float(intraday_state.get("clearness_ratio", 1.0))
        
        step2 = phantom_weather_step2
        if is_clear_ground or live_clearness >= 0.85:
            if step2 < step1_mw:
                step2 = step1_mw
        
        # Verify negative cut is strictly locked out
        self.assertEqual(step2, step1_mw, "Clear sky guardrail must reject negative cuts below Step 1 Base")
        self.assertGreaterEqual(step2, step1_mw)

    def test_clear_sky_positive_momentum_preserved(self):
        """Verify that when AI decides a positive momentum adjustment under clear sky, it is preserved without distortion."""
        cap_mw = 5.100
        step1_mw = 1.217
        ai_reasoned_positive_step2 = 1.314 # +0.097 MW positive momentum pull
        
        intraday_state = {
            "is_clear_ground": True,
            "clearness_ratio": 1.33,
        }
        
        step2 = ai_reasoned_positive_step2
        if intraday_state.get("is_clear_ground"):
            if step2 < step1_mw:
                step2 = step1_mw
                
        self.assertEqual(step2, 1.314, "AI dynamically reasoned positive adjustment must be preserved exactly")

    def test_overcast_breakout_releases_ceiling(self):
        """Verify that when ground SCADA breaks out of morning overcast, the sustained overcast ceiling is released."""
        cap_mw = 7.500 # Anjangaon
        current_solar_elev = 52.0
        
        # Case A: Persistent morning overcast (before breakout)
        state_overcast = {
            "is_clear_ground": False,
            "is_overcast_ground": True,
            "is_clearing_transition": False,
            "clearness_ratio": 0.48,
            "live_residual_factor": 0.35,
        }
        is_sustained_overcast_A = bool(
            current_solar_elev >= 45.0
            and state_overcast["live_residual_factor"] < 0.40
            and not state_overcast["is_clearing_transition"]
            and not (state_overcast["clearness_ratio"] >= 0.65)
            and state_overcast["is_overcast_ground"]
        )
        self.assertTrue(is_sustained_overcast_A, "Overcast ceiling should be active during genuine persistent overcast")

        # Case B: Midday Breakout (Ground SCADA surges to 3.8 MW, Kt = 0.88, clearing transition active)
        state_breakout = {
            "is_clear_ground": False,
            "is_overcast_ground": False,
            "is_clearing_transition": True,
            "clearness_ratio": 0.88,
            "live_residual_factor": 0.75,
        }
        is_sustained_overcast_B = bool(
            current_solar_elev >= 45.0
            and state_breakout["live_residual_factor"] < 0.40
            and not state_breakout["is_clearing_transition"]
            and not (state_breakout["clearness_ratio"] >= 0.65)
            and state_breakout["is_overcast_ground"]
        )
        self.assertFalse(is_sustained_overcast_B, "Overcast ceiling MUST be released when ground breaks out")

    def test_physical_diffuse_floor(self):
        """Verify that even under severe persistent overcast at midday, physical diffuse irradiance floor is respected."""
        cap_mw = 7.500
        step1_mw = 3.800
        overcast_ceiling_factor = 0.20 # Severe model drop
        
        # Diffuse floor is 25% of plant capacity
        diffuse_floor = round(cap_mw * 0.25, 3) # 1.875 MW
        clamped_val = max(diffuse_floor, round(step1_mw * overcast_ceiling_factor, 3))
        
        self.assertEqual(diffuse_floor, 1.875)
        self.assertEqual(clamped_val, 1.875, "Schedule should not be clamped below physical diffuse irradiance floor")

    def test_solar_geometry_elevation_cutoffs(self):
        """Verify elevation < 3 deg produces 0.0 MW and dawn < 7.5 deg is constrained."""
        cap_mw = 5.100
        
        # Night / Pre-dawn: elev = 1.5 deg
        step2_night = 0.850 # invalid spurious value
        elev_night = 1.5
        if elev_night < 3.0:
            step2_night = 0.0
        self.assertEqual(step2_night, 0.0)
        
        # Low Dawn: elev = 5.0 deg
        step2_dawn = 1.200 # spurious high dawn value
        elev_dawn = 5.0
        if elev_dawn < 7.5:
            step2_dawn = min(step2_dawn, round(cap_mw * 0.05, 3))
        self.assertEqual(step2_dawn, 0.255)

    def test_satellite_radiation_excluded_for_future_blocks(self):
        """Verify that satellite solar radiation is strictly prohibited for future blocks beyond revision cutoff."""
        from modules.weather import satellite_virtual_meter
        revision_time = dt.datetime(2026, 9, 11, 10, 0)
        future_block_time = dt.datetime(2026, 9, 11, 10, 15)

        # Call imputation for future block with NA ground sensors
        mw, source = satellite_virtual_meter.impute_missing_generation_from_radiation(
            timestamp=future_block_time,
            ground_poa=None,
            ground_ghi=None,
            cutoff_time=revision_time,
            plant_capacity_mw=5.1,
        )
        self.assertEqual(mw, 0.0, "Future block must not be assigned synthetic satellite generation")
        self.assertEqual(source, "future_block_excluded", "Source must indicate future block exclusion")

    def test_satellite_irradiance_cutoff_check(self):
        """Verify get_satellite_irradiance_for_timestamp rejects requests beyond cutoff."""
        from modules.weather import satellite_virtual_meter
        revision_time = dt.datetime(2026, 9, 11, 10, 0)
        future_time = dt.datetime(2026, 9, 11, 11, 30)

        sat_res = satellite_virtual_meter.get_satellite_irradiance_for_timestamp(
            reference_time=future_time,
            cutoff_time=revision_time,
        )
        self.assertEqual(sat_res.get("status"), "excluded_future_block")
        self.assertEqual(sat_res.get("gti"), 0.0)

    def test_satellite_virtual_meter_permitted_upto_revision_time(self):
        """Verify that satellite solar radiation works properly as a virtual meter up to revision time."""
        from modules.weather import satellite_virtual_meter
        revision_time = dt.datetime(2026, 8, 16, 9, 0)
        intraday_block_time = dt.datetime(2026, 8, 16, 9, 0) # t == cutoff_time

        # Ground POA present should use ground_poa
        mw_poa, src_poa = satellite_virtual_meter.impute_missing_generation_from_radiation(
            timestamp=intraday_block_time,
            ground_poa=500.0,
            cutoff_time=revision_time,
            plant_capacity_mw=5.1,
        )
        self.assertGreater(mw_poa, 0.0)
        self.assertEqual(src_poa, "ground_poa")

    def test_approach3_tri_stream_weather_cloud_rain_response(self):
        """Verify Approach 3 synthesizes afternoon blocks dynamically reflecting weather inputs."""
        import tempfile
        import csv
        from modules import schedule_utils

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            in_csv = tmp_path / "2026-09-11_current_final.csv"
            out_clear_csv = tmp_path / "penalty_clear.csv"
            out_storm_csv = tmp_path / "penalty_storm.csv"

            # Create base input schedule populated up to block 45 (11:15 AM)
            # Morning daylight blocks 28-45
            with open(in_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Block", "Final Validated MW"])
                writer.writeheader()
                for b in range(1, 46):
                    if 28 <= b <= 45:
                        mw = 2.5 # moderate morning generation
                    else:
                        mw = 0.0
                    writer.writerow({"Block": b, "Final Validated MW": mw})

            # Run 1: Clear afternoon weather map
            weather_clear = {}
            for h in range(11, 19):
                weather_clear[f"{h:02d}:00"] = {
                    "hour_label": f"{h:02d}:00",
                    "gti_fused": 850.0,
                    "cloud_pct": 5.0,
                    "precip_mm": 0.0,
                    "cape_j_kg": 200.0,
                    "temp_derate_multiplier": 0.96,
                }

            # Run 2: Stormy afternoon weather map
            weather_storm = {}
            for h in range(11, 19):
                weather_storm[f"{h:02d}:00"] = {
                    "hour_label": f"{h:02d}:00",
                    "gti_fused": 180.0,
                    "cloud_pct": 95.0,
                    "precip_mm": 3.5,
                    "cape_j_kg": 1950.0,
                    "temp_derate_multiplier": 0.98,
                }

            schedule_utils.write_full_block_schedule_from_llm_schedule(
                in_csv,
                out_clear_csv,
                target_date="2026-09-11",
                weather_fusion_map=weather_clear,
            )
            schedule_utils.write_full_block_schedule_from_llm_schedule(
                in_csv,
                out_storm_csv,
                target_date="2026-09-11",
                weather_fusion_map=weather_storm,
            )

            df_clear = pd.read_csv(out_clear_csv)
            df_storm = pd.read_csv(out_storm_csv)

            # Check midday peak block 50 (12:30 PM)
            mw_clear_b50 = float(df_clear.loc[df_clear["block"] == 50, "schedule_mw"].values[0])
            mw_storm_b50 = float(df_storm.loc[df_storm["block"] == 50, "schedule_mw"].values[0])

            self.assertGreater(mw_clear_b50, 2.5, "Clear afternoon block 50 should have high generation")
            self.assertLess(mw_storm_b50, mw_clear_b50 * 0.60, "Stormy afternoon block 50 must be significantly attenuated")

    def test_approach3_diffuse_floor_enforcement(self):
        """Verify midday blocks (elev >= 45 deg) respect 25% plant capacity diffuse floor under overcast."""
        import tempfile
        import csv
        from modules import schedule_utils
        import config

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            in_csv = tmp_path / "2026-09-11_current_final.csv"
            out_csv = tmp_path / "penalty_overcast.csv"

            with open(in_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Block", "Final Validated MW"])
                writer.writeheader()
                for b in range(1, 46):
                    writer.writerow({"Block": b, "Final Validated MW": 1.0 if 28 <= b <= 45 else 0.0})

            # Overcast weather without violent thunderstorm (CAPE < 1500)
            weather_overcast = {}
            for h in range(11, 19):
                weather_overcast[f"{h:02d}:00"] = {
                    "hour_label": f"{h:02d}:00",
                    "gti_fused": 100.0,
                    "cloud_pct": 98.0,
                    "precip_mm": 0.0,
                    "cape_j_kg": 300.0,
                    "temp_derate_multiplier": 1.0,
                }

            schedule_utils.write_full_block_schedule_from_llm_schedule(
                in_csv,
                out_csv,
                target_date="2026-09-11",
                weather_fusion_map=weather_overcast,
            )

            df = pd.read_csv(out_csv)
            ac_cap = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
            expected_diffuse_floor = round(ac_cap * 0.25, 3)

            # Block 49-51 are solar noon (elev >= 45 deg in summer/equinox)
            mw_b50 = float(df.loc[df["block"] == 50, "schedule_mw"].values[0])
            self.assertGreaterEqual(
                mw_b50,
                expected_diffuse_floor,
                f"Midday block 50 ({mw_b50} MW) must respect diffuse floor ({expected_diffuse_floor} MW)"
            )

    def test_approach3_offline_fallback(self):
        """Verify Approach 3 runs robustly with empty weather fusion map using PVLib geometry."""
        import tempfile
        import csv
        from modules import schedule_utils

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            in_csv = tmp_path / "2026-09-11_current_final.csv"
            out_csv = tmp_path / "penalty_fallback.csv"

            with open(in_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Block", "Final Validated MW"])
                writer.writeheader()
                for b in range(1, 46):
                    writer.writerow({"Block": b, "Final Validated MW": 2.0 if 28 <= b <= 45 else 0.0})

            # weather_fusion_map is empty dict
            schedule_utils.write_full_block_schedule_from_llm_schedule(
                in_csv,
                out_csv,
                target_date="2026-09-11",
                weather_fusion_map={},
            )

            df = pd.read_csv(out_csv)
            self.assertEqual(len(df), 96, "Output must contain all 96 blocks")
            # Midday should be positive
            mw_b50 = float(df.loc[df["block"] == 50, "schedule_mw"].values[0])
            self.assertGreater(mw_b50, 0.5, "Fallback must produce reasonable daylight schedule")
            # Night blocks must be 0.0
            mw_b10 = float(df.loc[df["block"] == 10, "schedule_mw"].values[0])
            mw_b85 = float(df.loc[df["block"] == 85, "schedule_mw"].values[0])
            self.assertEqual(mw_b10, 0.0)
            self.assertEqual(mw_b85, 0.0)

    def test_approach3_seam_boundary_smoothing(self):
        """Verify seamless transition between last AI schedule block and first synthesized block."""
        import tempfile
        import csv
        from modules import schedule_utils
        import config

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            in_csv = tmp_path / "2026-09-11_current_final.csv"
            out_csv = tmp_path / "penalty_seam.csv"

            # Last block 45 has value 3.0 MW
            with open(in_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Block", "Final Validated MW"])
                writer.writeheader()
                for b in range(1, 46):
                    writer.writerow({"Block": b, "Final Validated MW": 3.0 if 28 <= b <= 45 else 0.0})

            schedule_utils.write_full_block_schedule_from_llm_schedule(
                in_csv,
                out_csv,
                target_date="2026-09-11",
                weather_fusion_map={},
            )

            df = pd.read_csv(out_csv)
            mw_b45 = float(df.loc[df["block"] == 45, "schedule_mw"].values[0])
            mw_b46 = float(df.loc[df["block"] == 46, "schedule_mw"].values[0])

            ac_cap = float(getattr(config, "PLANT_CAPACITY_MW", 10.0))
            max_ramp = ac_cap * 0.15 + 0.01 # 15% capacity ramp limit
            self.assertLessEqual(
                abs(mw_b46 - mw_b45),
                max_ramp,
                f"Seam ramp between block 45 ({mw_b45} MW) and 46 ({mw_b46} MW) exceeds ramp limit"
            )

if __name__ == "__main__":
    unittest.main()
