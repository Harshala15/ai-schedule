"""Comprehensive Unit Test Suite for CHANDAWASA / CHANDWASA 10 MW Wind Plant.

Verifies:
1. Plant profile loading and configuration binding.
2. IEC Class III wind turbine power curve physics & dynamic air density scaling.
3. Open-Meteo Multi-Model Super-Ensemble wind schedule generation across 96 blocks.
4. Active 24-hour non-zero generation (bypassing solar daytime masks).
5. Canonical 5-column CSV schedule integrity.
"""

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import config
from modules.weather.wind_ensemble import (
    WindTurbineProfile,
    calculate_wind_schedule_96block,
    compute_air_density,
    turbine_power_curve,
)
from modules import schedule_utils


class TestChandawasaWindPlant(unittest.TestCase):
    def setUp(self):
        self.target_date = "2026-09-16"
        self.plant_name = "CHANDAWASA"

    def test_01_plant_profile_loading(self):
        """Verify plant profile loads with correct 10 MW wind parameters, Gamesa G114 80m, and 10% MPERC band."""
        profile = config.load_plant_profile("CHANDAWASA")
        self.assertEqual(config.PLANT_NAME, "CHANDAWASA")
        self.assertEqual(config.PLANT_CAPACITY_MW, 10.0)
        self.assertEqual(config.PLANT_TYPE, "WIND")
        self.assertTrue(config.is_wind_plant("CHANDAWASA"))
        self.assertEqual(config.PLANT_TOLERANCE_BAND_PCT, 10.0)
        self.assertEqual(config.PLANT_TOLERANCE_BAND_MW, 1.00)

        # Verify Gamesa G114/2000 80m turbine hardware
        self.assertEqual(profile.get("turbine_manufacturer"), "Gamesa")
        self.assertEqual(profile.get("turbine_model"), "G114/2000")
        self.assertEqual(profile.get("hub_height_m"), 80.0)
        self.assertEqual(profile.get("rotor_diameter_m"), 114.0)
        self.assertEqual(profile.get("num_turbines"), 5)
        self.assertEqual(profile.get("individual_turbine_capacity_kw"), 2000.0)
        self.assertEqual(profile.get("freeze_lag_minutes"), 90)

        # Verify 30-min revision schedule: 06:00 to 21:00 (31 slots)
        rev = profile.get("revision_schedule", {})
        self.assertEqual(rev.get("start_time"), "06:00")
        self.assertEqual(rev.get("end_time"), "21:00")
        self.assertEqual(rev.get("frequency_minutes"), 30)
        self.assertEqual(rev.get("effective_lag_minutes"), 90)
        self.assertEqual(len(rev.get("target_times", [])), 31)

        # Also verify alias spelling CHANDWASA
        alias_profile = config.load_plant_profile("CHANDWASA")
        self.assertEqual(config.PLANT_NAME, "CHANDWASA")
        self.assertEqual(config.PLANT_CAPACITY_MW, 10.0)
        self.assertEqual(config.PLANT_TYPE, "WIND")
        self.assertTrue(config.is_wind_plant("CHANDWASA"))
        self.assertEqual(alias_profile.get("hub_height_m"), 80.0)
        self.assertEqual(alias_profile.get("turbine_model"), "G114/2000")

    def test_02_air_density_calculation(self):
        """Verify dynamic air density computation against thermodynamic ideal gas law."""
        # Standard sea level: 1013.25 hPa, 15°C -> ~1.225 kg/m³
        rho_std = compute_air_density(1013.25, 15.0)
        self.assertAlmostEqual(rho_std, 1.225, delta=0.01)

        # Chandwasa elevation (~450m asl): ~960 hPa, 30°C -> lighter air (~1.10 kg/m³)
        rho_chand = compute_air_density(960.0, 30.0)
        self.assertTrue(1.05 <= rho_chand <= 1.15)

    def test_03_turbine_power_curve_physics(self):
        """Verify Gamesa G114/2000 power curve cut-in, ramp, rated, and cut-out behavior."""
        # Gamesa G114/2000: v_cut_in=3.0 m/s, v_rated=10.5 m/s, v_cut_out=25.0 m/s
        prof = WindTurbineProfile(
            rated_capacity_mw=10.0,
            v_cut_in=3.0,
            v_rated=10.5,
            v_cut_out=25.0,
            hub_height_m=80.0,
            park_derate_factor=0.89,
        )

        # 1. Below cut-in speed
        p_calm = turbine_power_curve(2.5, 1.225, prof)
        self.assertEqual(p_calm, 0.0)

        # 2. In ramp region (e.g. 7.0 m/s)
        p_ramp = turbine_power_curve(7.0, 1.225, prof)
        self.assertTrue(0.0 < p_ramp < 10.0)

        # 3. At and above rated speed (10.5 m/s)
        p_rated = turbine_power_curve(10.5, 1.225, prof)
        self.assertEqual(p_rated, 10.0)

        p_above_rated = turbine_power_curve(15.0, 1.225, prof)
        self.assertEqual(p_above_rated, 10.0)

        # 4. Storm cut-out protection (25.0 m/s cut-out)
        p_cutout = turbine_power_curve(25.1, 1.225, prof)
        self.assertEqual(p_cutout, 0.0)

        # 5. Density sensitivity: higher density air should yield higher power at same speed
        p_cold = turbine_power_curve(7.0, 1.25, prof)
        p_hot = turbine_power_curve(7.0, 1.10, prof)
        self.assertGreater(p_cold, p_hot)

    def test_04_ensemble_wind_schedule_96block(self):
        """Verify full 96-block 24-hour wind schedule generation using Open-Meteo ensemble at 80m."""
        prof = WindTurbineProfile(
            plant_name="CHANDAWASA",
            rated_capacity_mw=10.0,
            hub_height_m=80.0,
            v_cut_in=3.0,
            v_rated=10.5,
            v_cut_out=25.0,
        )
        result = calculate_wind_schedule_96block(
            latitude=24.166208,
            longitude=75.459684,
            target_date_str=self.target_date,
            profile=prof,
        )

        self.assertEqual(result["total_blocks"], 96)
        self.assertEqual(len(result["blocks"]), 96)
        self.assertGreater(result["total_ensemble_members"], 50)
        self.assertTrue(0.0 < result["peak_mw"] <= 10.0)

        # Verify nighttime blocks have active wind power (NOT forced to zero like solar!)
        night_block_1 = result["blocks"][0]   # 00:00 - 00:15
        night_block_96 = result["blocks"][95] # 23:45 - 24:00

        self.assertEqual(night_block_1["block"], 1)
        self.assertEqual(night_block_96["block"], 96)

        # When wind blows above cut-in, night generation must be > 0.0
        if night_block_1["wind_speed_80m"] >= 3.5:
            self.assertGreater(night_block_1["schedule_mw"], 0.0)
        if night_block_96["wind_speed_80m"] >= 3.5:
            self.assertGreater(night_block_96["schedule_mw"], 0.0)

    def test_05_schedule_utils_wind_nighttime_bypass(self):
        """Verify schedule_utils does not zero out nighttime blocks for wind plants."""
        config.load_plant_profile("CHANDAWASA")
        self.assertTrue(config.is_wind_plant())

        test_dir = config.STORAGE_ROOT / "test_wind_run"
        test_dir.mkdir(parents=True, exist_ok=True)
        latest_csv = test_dir / "latest_test.csv"
        current_final_csv = test_dir / "current_final_test.csv"

        # Create synthetic rows with night generation
        fieldnames = ["Block", "Time Interval (15 minute interval)", "intellis_gti", "intellis_mw", "schedule_mw"]
        rows = [
            {"Block": 1, "Time Interval (15 minute interval)": "00:00 - 00:15", "intellis_gti": "10.5", "intellis_mw": "4.50", "schedule_mw": "4.50"},
            {"Block": 48, "Time Interval (15 minute interval)": "11:45 - 12:00", "intellis_gti": "12.0", "intellis_mw": "6.80", "schedule_mw": "6.80"},
            {"Block": 96, "Time Interval (15 minute interval)": "23:45 - 24:00", "intellis_gti": "11.2", "intellis_mw": "5.10", "schedule_mw": "5.10"},
        ]
        schedule_utils.write_csv(latest_csv, fieldnames, rows)

        # Write current final schedule
        schedule_utils.write_current_final_schedule(
            latest_csv=latest_csv,
            current_final_csv=current_final_csv,
            target_date=self.target_date,
            target_time="00:00",
            block_minutes=15,
            freeze_lag_minutes=0,
        )

        _, read_rows = schedule_utils.read_csv_rows(current_final_csv)
        row_map = {int(r["Block"]): r for r in read_rows}

        # Verify nighttime blocks 1 and 96 were PRESERVED, not zeroed to "0.0"!
        self.assertEqual(row_map[1]["intellis_mw"], "4.50")
        self.assertEqual(row_map[96]["intellis_mw"], "5.10")

    def test_06_mperc_30min_revision_with_90min_effective_lag(self):
        """Verify 30-min revision cycle (06:00 to 21:00) with 90-minute freeze lag and 10% MPERC band."""
        config.load_plant_profile("CHANDAWASA")
        self.assertEqual(config.PLANT_TOLERANCE_BAND_PCT, 10.0)
        self.assertEqual(config.PLANT_TOLERANCE_BAND_MW, 1.00)

        # Test revision at 06:00 with 90-minute lag:
        # Effective freeze datetime: 06:00 + 90 min = 07:30
        freeze_dt = schedule_utils.freeze_from_datetime(self.target_date, "06:00", block_minutes=15)
        self.assertEqual(freeze_dt.strftime("%H:%M"), "07:30")

        # 07:30 corresponds to start of Block 31 (07:30 - 07:45).
        # Blocks 1 through 30 remain frozen; Block 31 onward are updated.
        test_dir = config.STORAGE_ROOT / "test_wind_revision"
        test_dir.mkdir(parents=True, exist_ok=True)
        latest_csv = test_dir / "latest_0600.csv"
        current_final_csv = test_dir / "current_final_0600.csv"

        fieldnames = ["Block", "Time Interval (15 minute interval)", "intellis_gti", "intellis_mw", "schedule_mw"]
        initial_rows = [
            {"Block": b, "Time Interval (15 minute interval)": f"{(b-1)//4:02d}:{((b-1)%4)*15:02d} - {((b-1)//4 if (b%4)!=0 else b//4):02d}:{(b%4)*15 if (b%4)!=0 else 0:02d}", "intellis_gti": "5.0", "intellis_mw": "2.00", "schedule_mw": "2.00"}
            for b in range(1, 97)
        ]
        schedule_utils.write_csv(current_final_csv, fieldnames, initial_rows)

        # New forecast generated at 06:00 with higher wind power 4.00 MW
        new_forecast_rows = [
            {"Block": b, "Time Interval (15 minute interval)": f"{(b-1)//4:02d}:{((b-1)%4)*15:02d} - {((b-1)//4 if (b%4)!=0 else b//4):02d}:{(b%4)*15 if (b%4)!=0 else 0:02d}", "intellis_gti": "8.0", "intellis_mw": "4.00", "schedule_mw": "4.00"}
            for b in range(1, 97)
        ]
        schedule_utils.write_csv(latest_csv, fieldnames, new_forecast_rows)

        # Execute revision at 06:00 (90 min freeze lag)
        schedule_utils.write_current_final_schedule(
            latest_csv=latest_csv,
            current_final_csv=current_final_csv,
            target_date=self.target_date,
            target_time="06:00",
            block_minutes=15,
            freeze_lag_minutes=90,
        )

        _, updated_rows = schedule_utils.read_csv_rows(current_final_csv)
        row_map = {int(r["Block"]): r for r in updated_rows}

        # Blocks 1..30 (before 07:30) must remain frozen at 2.00 MW
        self.assertEqual(row_map[25]["schedule_mw"], "2.00") # 06:00-06:15
        self.assertEqual(row_map[30]["schedule_mw"], "2.00") # 07:15-07:30

        # Blocks 31..96 (at and after 07:30) must be updated to 4.00 MW
        self.assertEqual(row_map[31]["schedule_mw"], "4.00") # 07:30-07:45
        self.assertEqual(row_map[50]["schedule_mw"], "4.00") # 12:15-12:30

        # Also test the last revision at 21:00 (night 9)
        # 21:00 + 90 min lag = 22:30 -> start of Block 91 (22:30 - 22:45)
        freeze_2100 = schedule_utils.freeze_from_datetime(self.target_date, "21:00", block_minutes=15)
        self.assertEqual(freeze_2100.strftime("%H:%M"), "22:30")

    def test_07_site_empirical_calibration(self):
        """Verify empirical site calibration produces close convergence to Enercast behavior without live Enercast."""
        config.load_plant_profile("CHANDAWASA")
        prof = WindTurbineProfile(plant_name="CHANDAWASA")
        res = calculate_wind_schedule_96block(
            latitude=24.166208,
            longitude=75.459684,
            target_date_str=self.target_date,
            profile=prof,
        )

        # Calibrated peak and mean must align with real operating envelope (mean ~1.5-3.5 MW, peak <= 5.5 MW)
        self.assertTrue(1.0 <= res["mean_daily_mw"] <= 3.8)
        self.assertTrue(2.0 <= res["peak_mw"] <= 5.5)

        # Verify block 76 (19:00 dusk lull) is appropriately damped relative to midday convection
        b76 = res["blocks"][75]  # 18:45-19:00
        b48 = res["blocks"][47]  # 11:45-12:00
        self.assertLess(b76["schedule_mw"], b48["schedule_mw"])


if __name__ == "__main__":
    unittest.main()
