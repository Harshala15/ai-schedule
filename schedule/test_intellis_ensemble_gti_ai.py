"""Unit and integration tests for Intellis Ensemble GTI AI Engine (Ultra-Accuracy)."""

import unittest
from pathlib import Path
import numpy as np
import pandas as pd

from modules.weather.intellis_ensemble_gti_ai import (
    IntellisEnsembleGTIAI,
    PlantProfile,
    load_plant_profile,
    predict_plant_gti_and_power,
)


class TestIntellisEnsembleGTIAI(unittest.TestCase):
    """Test suite for IntellisEnsembleGTIAI."""

    def test_01_load_plant_profile_gsnp(self):
        """Test loading GSNP plant profile from plant_profiles/GSNP.json."""
        prof = load_plant_profile("GSNP")
        self.assertEqual(prof.plant_name, "GSNP")
        self.assertAlmostEqual(prof.latitude, 24.077752, places=4)
        self.assertAlmostEqual(prof.longitude, 75.337636, places=4)
        self.assertAlmostEqual(prof.ac_capacity_mw, 20.0, places=1)
        self.assertAlmostEqual(prof.dc_capacity_mw, 23.6016, places=2)
        self.assertEqual(prof.tilt_deg, 15.0)
        self.assertEqual(prof.orientation_deg_from_south, 8.0)
        self.assertAlmostEqual(prof.transfer_ratio, 0.018408, places=5)
        self.assertEqual(prof.ppa_rate_inr_per_kwh, 6.97)
        self.assertEqual(prof.tolerance_band_mw, 2.0)

    def test_02_load_plant_profile_other_plants(self):
        """Test loading other plant profiles (KASIPET, SIRMOUR)."""
        kasipet = load_plant_profile("KASIPET")
        self.assertEqual(kasipet.plant_name, "KASIPET")
        self.assertAlmostEqual(kasipet.ac_capacity_mw, 15.0, places=1)
        self.assertEqual(kasipet.tilt_deg, 20.0)

        sirmour = load_plant_profile("SIRMOUR")
        self.assertEqual(sirmour.plant_name, "SIRMOUR")
        self.assertAlmostEqual(sirmour.ac_capacity_mw, 5.1, places=1)

    def test_03_meter_actuals_ingestion_tvm_active_power(self):
        """Test SCADA meter actuals loading and TVM Active Power conversion."""
        ai = IntellisEnsembleGTIAI(plant_name="GSNP")
        meter_mw = ai.load_meter_actuals("2026-09-14")
        self.assertEqual(len(meter_mw), 96)
        peak_mw = float(np.max(meter_mw))
        self.assertGreater(peak_mw, 8.0)
        self.assertLess(peak_mw, 10.0)
        self.assertEqual(meter_mw[0], 0.0)
        self.assertEqual(meter_mw[95], 0.0)

    def test_04_benchmark_and_cross_family_selection(self):
        """Test 7-day rolling benchmark with regime matching and diverse quota."""
        ai = IntellisEnsembleGTIAI(plant_name="GSNP")
        selected_keys, weights_map, rank_df = ai.benchmark_and_select_models(
            target_date_str="2026-09-14",
            lookback_days=7,
            icon_quota=3,
            ecmwf_quota=2,
            gefs_quota=2,
            tau_decay_days=3.5,
            use_regime_matching=True,
        )
        self.assertGreaterEqual(len(selected_keys), 5)
        icon_keys = [k for k in selected_keys if "icon" in k.lower()]
        ecmwf_keys = [k for k in selected_keys if "ecmwf" in k.lower() or "ifs" in k.lower()]
        self.assertGreaterEqual(len(icon_keys), 3)
        self.assertGreaterEqual(len(ecmwf_keys), 2)
        self.assertAlmostEqual(sum(weights_map.values()), 1.0, places=2)

    def test_05_predict_96block_schedule_gsnp_14sept(self):
        """Test full 96-block GTI and MW schedule prediction for 14-Sept."""
        ai = IntellisEnsembleGTIAI(plant_name="GSNP")
        result = ai.predict_96block_schedule("2026-09-14")

        self.assertEqual(result["plant_name"], "GSNP")
        self.assertEqual(result["target_date"], "2026-09-14")
        self.assertEqual(len(result["blocks"]), 96)

        # Daytime MAE should be low
        self.assertLess(result["daylight_mae_mw"], 1.45)

        # Safe blocks should be >= 80 out of 96
        self.assertGreaterEqual(result["safe_blocks"], 80)

        # Total DSM penalty should be well below baseline (baseline was Rs. 5,771)
        self.assertLess(result["total_dsm_penalty_inr"], 3200.0)

        # Check block 56 (13:45 IST) where submitted schedule spiked to 11.95 MW
        blk56 = result["blocks"][55]  # 0-indexed 55 = block 56
        self.assertEqual(blk56["block"], 56)
        self.assertEqual(blk56["dsm_slab"], "0% Safe")
        self.assertEqual(blk56["block_penalty_inr"], 0.0)

    def test_06_regime_classification(self):
        """Test atmospheric regime classifier."""
        ai = IntellisEnsembleGTIAI(plant_name="GSNP")
        self.assertEqual(ai.classify_weather_regime(200.0, 1000.0), "OVERCAST")
        self.assertEqual(ai.classify_weather_regime(600.0, 1000.0), "MIXED")
        self.assertEqual(ai.classify_weather_regime(850.0, 1000.0), "CLEAR")

    def test_07_intraday_scada_feedback(self):
        """Test closed-loop intraday SCADA telemetry feedback nudge."""
        ai = IntellisEnsembleGTIAI(plant_name="GSNP")
        base = ai.predict_96block_schedule("2026-09-14")
        rev = ai.apply_intraday_scada_feedback(base, current_block=40, live_meter_mw=1.88, tau_blocks=4.0)

        self.assertEqual(len(rev["blocks"]), 96)
        # Past blocks (before block 44) should match exactly
        self.assertEqual(rev["blocks"][0]["predicted_mw"], base["blocks"][0]["predicted_mw"])
        self.assertEqual(rev["blocks"][35]["predicted_mw"], base["blocks"][35]["predicted_mw"])

    def test_08_convenience_function(self):
        """Test predict_plant_gti_and_power convenience function."""
        res = predict_plant_gti_and_power("GSNP", "2026-09-14")
        self.assertEqual(len(res["blocks"]), 96)
        self.assertIn("daylight_mae_mw", res)


if __name__ == "__main__":
    unittest.main()
