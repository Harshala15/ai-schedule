# Plant Onboarding Guide: Adding a New Solar or Wind Site

This document outlines the standard 3-step process for adding a new renewable energy asset to the **Intellis AI** platform.

---

## Step 1: Create the Plant Profile JSON

Create a new file in `schedule/plant_profiles/<PLANT_NAME>.json`.

### A. Template for Solar Plants
```json
{
  "plant_name": "MY_SOLAR_PLANT",
  "latitude": 24.123456,
  "longitude": 75.654321,
  "maximum_feed_in_ac_kw": 20000,
  "dc_capacity_kw": 24000,
  "tilt_deg": 15.0,
  "orientation_deg_from_south": 0.0,
  "tracker_type": "None",
  "eeg_id": "MY_SOLAR_PLANT_GRID_ID",
  "ppa_rate_inr_per_kwh": 3.05,
  "penalty_regulation": "Madhya Pradesh",
  "tolerance_band_mw": 2.0,
  "band_percentage": 0.10,
  "tolerance_band_percent": 0.10,
  "meter_data": {
    "is_non_meter_site": false,
    "s3_prefix": "raw/vedanjay/MY_SOLAR_PLANT/{date}/metered_data/",
    "file_name_regex": "^my_solar_plant_\\d{8}\\.csv$",
    "delimiter": ",",
    "timestamp_column": "block_end",
    "power_column": "metered_mw",
    "power_unit": "mw",
    "poa_column": "poa_wm2",
    "ghi_column": "ghi_wm2"
  }
}
```

### B. Template for Wind Plants
```json
{
  "plant_name": "MY_WIND_FARM",
  "plant_type": "WIND",
  "latitude": 17.875620,
  "longitude": 76.363880,
  "maximum_feed_in_ac_kw": 50000,
  "eeg_id": "MY_WIND_GRID_ID",
  "ppa_rate_inr_per_kwh": 3.275,
  "penalty_regulation": "Maharashtra",
  "tolerance_band_mw": 7.5,
  "band_percentage": 0.15,
  "turbine_count": 25,
  "rated_power_mw_per_turbine": 2.0,
  "hub_height_m": 120.0,
  "rotor_diameter_m": 110.0,
  "cut_in_speed_m_s": 3.0,
  "rated_speed_m_s": 11.5,
  "cut_out_speed_m_s": 25.0,
  "meter_data": {
    "s3_prefix": "raw/vedanjay/MY_WIND_FARM/{date}/metered_data/",
    "file_name_regex": "^my_wind_\\d{8}\\.csv$",
    "delimiter": ",",
    "timestamp_column": "timestamp",
    "power_column": "generation_mw",
    "wind_speed_column": "wind_speed_avg"
  }
}
```

---

## Step 2: Register Regulatory Parameters in `schedule/config.py`

Open `schedule/config.py` and register the site under `_PLANT_FALLBACKS` and the state regulatory set:

```python
# 1. Add to _PLANT_FALLBACKS dictionary:
"MY_SOLAR_PLANT": {
    "latitude": 24.123456,
    "longitude": 75.654321,
    "capacity_mw": 20.0,
    "dc_capacity_mw": 24.0,
    "max_feed_in_mw": 20.0,
    "tilt_deg": 15.0,
    "orientation_deg_from_south": 0.0,
    "ppa_rate_inr_per_kwh": 3.05,
    "penalty_regulation": "Madhya Pradesh",
    "eeg_id": "MY_SOLAR_PLANT_GRID_ID",
},

# 2. Add to the appropriate state set in get_plant_tolerance_band_pct():
mp_plants = {..., "MY_SOLAR_PLANT"}    # For 90-min lag, 10% band
# OR
tg_plants = {..., "MY_SOLAR_PLANT"}    # For 45-min lag, 15% band
# OR
mh_plants = {..., "MY_SOLAR_PLANT"}    # For 45-min lag, 10% band
```

---

## Step 3: Test Locally & Register for Deployment

### A. Run a Local Verification Test
```bash
python run_local.py --plant MY_SOLAR_PLANT --time 10:00
```
Verify that all 96 blocks are populated with smooth, non-zero values during daylight hours.

### B. Add to Deployment Script
Open `schedule/build_and_deploy_all_lambdas.py` and add the Lambda name to `ALL_LAMBDAS`:
```python
ALL_LAMBDAS = [
    ...,
    "MY_SOLAR_PLANT-ai-intellis-scheduler",
]
```

Deploy to production:
```bash
python schedule/build_and_deploy_all_lambdas.py
```
