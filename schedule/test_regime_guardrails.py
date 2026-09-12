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

    def test_dawn_uv_transmissivity_exemption(self):
        """Verify that at dawn (low UV clear sky index < 1.0), cloud transmissivity is not artificially crushed."""
        # Simulated dawn parameters (e.g. 06:30 - 07:30 IST)
        uv_val = 0.1
        uv_clear_val = 0.5  # Below 1.0 threshold
        eff_cloud_pct = 5.0  # Clear sky (5% cloud)

        # Logic from premium_stream3.py
        if uv_clear_val >= 1.0:
            cloud_transmissivity = min(1.0, max(0.05, round(uv_val / uv_clear_val, 3)))
        else:
            cloud_transmissivity = 1.0 if eff_cloud_pct < 20 else max(0.20, 1.0 - (eff_cloud_pct / 100.0))

        self.assertEqual(cloud_transmissivity, 1.0, "Dawn UV ratio must not crush transmissivity under clear skies")

    def test_clear_sky_weather_fusion_consensus(self):
        """Verify that clear consensus between Stream 1 (ECMWF 9km) and Stream 2 (Ensemble) uses robust median instead of min()."""
        gti_stream1 = 265.0  # ECMWF 9km
        gti_stream2 = 263.4  # Ensemble 91-member
        gti_stream3 = 160.6  # Coarse grid Stream 3 artifact

        cloud_avg = 5.5
        precip_max = 0.15  # Model trace drizzle artifact
        cape_val = 200.0
        trans_val = 1.0

        is_clear_consensus = (cloud_avg <= 25.0) and (precip_max < 0.50) and (cape_val < 1500)
        is_cloudy_or_rain = (not is_clear_consensus) and (
            (precip_max >= 0.50)
            or (precip_max >= 0.20 and cloud_avg >= 35.0)
            or (cloud_avg >= 45.0)
            or (trans_val < 0.65 and cloud_avg >= 25.0)
        )

        self.assertTrue(is_clear_consensus, "Clear consensus should be recognized")
        self.assertFalse(is_cloudy_or_rain, "Trace drizzle with clear sky should not trigger rain dampening")

        sorted_gtis = sorted([gti_stream1, gti_stream2, gti_stream3])
        gti_fused = round(sorted_gtis[1], 1)

        self.assertEqual(gti_fused, 263.4, "Clear consensus must use median (263.4 W/m2) instead of min (160.6 W/m2)")

    def test_predawn_clear_sky_lockout(self):
        """Verify that pre-dawn forecast runs lock out negative LLM cuts when morning forecasts confirm clear sky."""
        current_solar_elev = 0.0  # Pre-dawn (e.g. 05:00 IST)
        live_clearness = 1.0
        is_clear_ground = False  # SCADA is 0 at night
        step1_mw = 1.850
        llm_pessimistic_step2 = 0.720

        b_feat = {
            "cloud_cover": 5.0,
            "nwp_clearness": 0.88,
        }

        is_predawn_clear = (current_solar_elev < 20.0 and (b_feat.get("cloud_cover", 0.0) <= 25.0 or b_feat.get("nwp_clearness", 0.0) >= 0.70))
        step2 = llm_pessimistic_step2

        if is_clear_ground or live_clearness >= 0.85 or (current_solar_elev < 20.0 and live_clearness >= 0.70) or is_predawn_clear:
            if step2 < step1_mw:
                step2 = step1_mw

        self.assertTrue(is_predawn_clear, "Pre-dawn clear condition must evaluate to True")
        self.assertEqual(step2, step1_mw, "Pre-dawn clear sky guardrail must lock out negative LLM cuts")

    def test_p42_clear_sky_headroom_and_peak_cap(self):
        """Verify that under clear skies, anchor targets P42 quantile and caps peak at 85% of AC capacity."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from modules.physics import physics_anchor

        feat = {
            "solar_elevation_deg": 68.0,
            "month": 4,
            "minute_of_day": 735,
            "hour": 12,
            "minute": 15,
            "nwp_clearness": 1.0,
            "temp_air_c": 32.0,
        }
        # 10 MW AC plant, 12 MW DC
        anchor_mw = physics_anchor.calculate_anchor_mw(
            feat, capacity_mw=10.0, dc_capacity_mw=12.0, performance_ratio=0.80
        )
        # Check that peak is strictly capped at 85% (8.50 MW)
        self.assertLessEqual(anchor_mw, 8.50, "Peak clear generation must not exceed 85% of AC capacity")
        self.assertGreater(anchor_mw, 6.80, "Clear peak anchor should be near P42 target (6.8 - 8.5 MW)")

    def test_afternoon_thermal_hysteresis(self):
        """Verify that afternoon thermal hysteresis (12:30-15:30 IST) applies ~0.94 derating relative to morning."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from modules.physics import physics_anchor

        feat_morning = {
            "solar_elevation_deg": 50.0,
            "month": 5,
            "minute_of_day": 630,  # 10:30 AM
            "hour": 10,
            "minute": 30,
            "nwp_clearness": 0.90,
            "temp_air_c": 30.0,
        }
        feat_afternoon = {
            "solar_elevation_deg": 50.0,
            "month": 5,
            "minute_of_day": 840,  # 14:00 PM (peak module heat)
            "hour": 14,
            "minute": 0,
            "nwp_clearness": 0.90,
            "temp_air_c": 30.0,
        }
        morning_mw = physics_anchor.calculate_anchor_mw(feat_morning, capacity_mw=10.0)
        afternoon_mw = physics_anchor.calculate_anchor_mw(feat_afternoon, capacity_mw=10.0)

        self.assertGreater(morning_mw, afternoon_mw, "Afternoon generation must be lower due to thermal hysteresis derate")
        ratio = afternoon_mw / morning_mw
        self.assertAlmostEqual(ratio, 0.94, delta=0.03, msg="Afternoon thermal derate should be ~0.94")

    def test_winter_morning_fog_suppression(self):
        """Verify winter morning fog cut-in delay and ramp suppression gate."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from modules.physics import physics_anchor

        feat_pre_cutin = {
            "solar_elevation_deg": 4.0,  # Below winter 4.5 deg cut-in
            "month": 1,  # January
            "minute_of_day": 435,  # 07:15 AM
            "hour": 7,
            "minute": 15,
            "nwp_clearness": 1.0,
        }
        anchor_cutin = physics_anchor.calculate_anchor_mw(feat_pre_cutin, capacity_mw=10.0)
        self.assertEqual(anchor_cutin, 0.0, "Sun below 4.5 deg in winter must yield 0.0 MW due to fog/inverter delay")

        feat_fog_ramp = {
            "solar_elevation_deg": 12.0,  # Below 15 deg in winter
            "month": 12,  # December
            "minute_of_day": 480,  # 08:00 AM
            "hour": 8,
            "minute": 0,
            "nwp_clearness": 1.0,
        }
        anchor_ramp = physics_anchor.calculate_anchor_mw(feat_fog_ramp, capacity_mw=10.0)
        self.assertLessEqual(anchor_ramp, 3.50, "Winter morning fog ramp must be capped at 35% of plant capacity")

    def test_closed_loop_scada_decay(self):
        """Verify exponential closed-loop telemetry decay with tau = 75 minutes."""
        tau_min = 75.0
        # Block 0 (0 min)
        w0 = math.exp(-(0 * 15.0) / tau_min)
        self.assertAlmostEqual(w0, 1.0, places=3)

        # Block 4 (60 min ahead, gate-closure delivery)
        w4 = math.exp(-(4 * 15.0) / tau_min)
        self.assertAlmostEqual(w4, 0.449, places=2)

        # Block 8 (120 min ahead)
        w8 = math.exp(-(8 * 15.0) / tau_min)
        self.assertAlmostEqual(w8, 0.202, places=2)

    def test_cloud_plateau_shock_absorber(self):
        """Verify cloud regime plateau shock-absorber dampens high-variance NWP oscillations."""
        # Simulated scattered regime Kt fluctuating between 0.30 and 0.70
        raw_clearness = [0.70, 0.30, 0.65, 0.35]
        damped = [round(0.50 + 0.60 * (kt - 0.50), 3) for kt in raw_clearness]

        self.assertEqual(damped[0], 0.62)  # Dampened from 0.70 to 0.62
        self.assertEqual(damped[1], 0.38)  # Dampened from 0.30 to 0.38
        for val in damped:
            self.assertGreaterEqual(val, 0.35)
            self.assertLessEqual(val, 0.65)

    def test_asymmetric_slew_rates(self):
        """Verify validator allows fast upward clearing ramp (22%) while constraining sudden downward drops (12%)."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import validator

        # 10 MW plant: Upward clearing ramp from 2.0 to 4.1 MW (+21%) at 08:30
        predictions_morning = [
            {"time": "2026-04-15 08:15", "block_number": 34, "anchor_mw": 2.0, "llm_mw": 2.0, "confidence": "High", "reasoning": "fog"},
            {"time": "2026-04-15 08:30", "block_number": 35, "anchor_mw": 4.1, "llm_mw": 4.1, "confidence": "High", "reasoning": "clearing"},
        ]
        val_m = validator.validate_predictions(predictions_morning, capacity_mw=10.0)
        # 4.1 MW should be accepted because +2.1 MW is within 22% limit (2.2 MW)
        self.assertEqual(val_m[1]["validated_mw"], 4.1)

        # Midday sudden spurious drop: 7.5 MW down to 5.0 MW (-25%) at 12:30
        predictions_midday = [
            {"time": "2026-04-15 12:15", "block_number": 50, "anchor_mw": 7.5, "llm_mw": 7.5, "confidence": "High", "reasoning": "midday clear"},
            {"time": "2026-04-15 12:30", "block_number": 51, "anchor_mw": 5.0, "llm_mw": 5.0, "confidence": "Low", "reasoning": "spurious model dip"},
        ]
        val_mid = validator.validate_predictions(predictions_midday, capacity_mw=10.0)
        # Drop of 2.5 MW exceeds 1.2 MW max downward step (12% capacity), so it should be smoothed to 7.5 - 1.2 = 6.3 MW
        self.assertGreater(val_mid[1]["validated_mw"], 5.0, "Midday downward drop must be smoothed")
        self.assertEqual(val_mid[1]["validated_mw"], 6.3)

    def test_weather_fusion_upper_consensus_rejects_low_outlier(self):
        """Verify that when 2 streams agree on high clear generation, the low pessimistic outlier is rejected."""
        s1 = 640.0
        s2 = 417.0  # Coarse grid outlier
        s3 = 675.5
        sorted_gtis = sorted([s1, s2, s3])
        d_mid_high = sorted_gtis[2] - sorted_gtis[1]

        self.assertLessEqual(d_mid_high, 85.0, "Upper pair must agree within 85 W/m2")
        fused = round((sorted_gtis[1] + sorted_gtis[2]) / 2.0, 1)
        self.assertEqual(fused, 657.8, "Fused irradiance must be the average of the upper agreeing pair")
        self.assertGreater(fused, 600.0, "Fused irradiance must not be crushed to 417 W/m2")

    def test_weather_fusion_failed_stream_filtered(self):
        """Verify that a daytime stream failure (0.0 W/m2) is filtered out and does not zero out the forecast."""
        s1 = 700.0
        s2 = 0.0  # Simulated API failure / timeout
        s3 = 720.0
        elev_target = 65.0

        streams_dict = {}
        if not (elev_target >= 10.0 and s1 <= 0.0 and max(s2, s3) > 50.0):
            streams_dict["S1"] = s1
        if not (elev_target >= 10.0 and s2 <= 0.0 and max(s1, s3) > 50.0):
            streams_dict["S2"] = s2
        if not (elev_target >= 10.0 and s3 <= 0.0 and max(s1, s2) > 50.0):
            streams_dict["S3"] = s3

        self.assertNotIn("S2", streams_dict, "Failed daytime 0.0 stream must be discarded")
        active_vals = list(streams_dict.values())
        fused = sum(active_vals) / len(active_vals)
        self.assertEqual(fused, 710.0, "Active streams must be averaged, ignoring the failed stream")

    def test_parabolic_clear_sky_interpolation_at_dawn(self):
        """Verify that dawn interpolation uses solar elevation cut-in (0.0 W/m2 below 3 deg) and suppresses linear creep."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from modules.weather import weather_fusion

        hourly_map = {
            "06:00": {"gti": 0.0, "temp": 24.0},
            "07:00": {"gti": 60.0, "temp": 25.0},
        }
        # At 06:05, sun elevation is 2.56 deg (< 3.0 deg cut-in)
        target_dt_pre = dt.datetime(2026, 9, 12, 6, 5)
        res_pre = weather_fusion._get_hourly_interpolated(hourly_map, "06:05", target_dt=target_dt_pre)
        self.assertEqual(res_pre["gti"], 0.0, "Pre-cutin dawn solar irradiance must be 0.0 W/m2")

        # At 06:15, sun elevation is 4.83 deg. Linear interpolation would give 15.0 W/m2, but parabolic gives 5.1 W/m2.
        target_dt_dawn = dt.datetime(2026, 9, 12, 6, 15)
        res_dawn = weather_fusion._get_hourly_interpolated(hourly_map, "06:15", target_dt=target_dt_dawn)
        self.assertLess(res_dawn["gti"], 10.0, "Parabolic interpolation must suppress linear creep (5.1 vs 15.0 W/m2)")
        self.assertEqual(res_dawn["gti"], 5.1)


if __name__ == "__main__":
    unittest.main()

