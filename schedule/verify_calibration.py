"""Verify calibration output for Chandawasa."""

import sys
sys.path.insert(0, r"d:\14 sept intellis\schedule")
sys.path.insert(0, r"d:\14 sept intellis")
from modules.weather.wind_ensemble import calculate_wind_schedule_96block

res = calculate_wind_schedule_96block(24.166208, 75.459684, "2026-09-16")
print("Plant:", res["plant_name"])
print("Mean daily MW:", res["mean_daily_mw"], "Peak MW:", res["peak_mw"])
print("\nSample blocks on Sept 16:")
for b in [1, 25, 48, 68, 76, 88, 92, 96]:
    blk = res["blocks"][b-1]
    print(f"Block {b:02d} ({blk['time']}) -> Hub Wind: {blk['wind_speed_hub_m_s']} m/s, Calibrated MW: {blk['intellis_mw']} MW")
