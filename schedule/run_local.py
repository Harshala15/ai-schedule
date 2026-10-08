"""Intellis AI Local Schedule Runner.

A unified CLI utility for developers and team members to test and generate
revision schedules locally for any of the 23 solar or wind plants without
requiring AWS Lambda or cloud infrastructure.

Usage Examples:
    # 1. Generate GSNP solar schedule for 10:00 AM today:
    python run_local.py --plant GSNP --time 10:00

    # 2. Generate Kothagudem solar schedule for a specific historical date:
    python run_local.py --plant KOTHAGUDEM --date 2026-09-26 --time 14:15

    # 3. Generate Jewli Wind Farm schedule:
    python run_local.py --plant JEWLI --time 11:30

    # 4. Save output to custom CSV:
    python run_local.py --plant SIRMOUR --time 09:45 --output my_test_sched.csv
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

# Add schedule directory to Python path
SCHEDULE_ROOT = Path(__file__).resolve().parent
if str(SCHEDULE_ROOT) not in sys.path:
    sys.path.insert(0, str(SCHEDULE_ROOT))

import config


def run_local_schedule(
    plant_name: str,
    target_date: str | None = None,
    target_time: str = "10:00",
    output_path: Path | None = None,
    live_meter_csv: Path | None = None,
) -> dict:
    """Run the end-to-end forecasting pipeline locally."""
    plant_name = plant_name.strip().upper()
    target_date = target_date or dt.date.today().isoformat()
    output_path = output_path or (SCHEDULE_ROOT / "scratch" / f"{plant_name}_{target_date}_{target_time.replace(':', '')}_local.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"RUNNING INTELLIS AI SCHEDULE LOCALLY")
    print(f"Plant:       {plant_name}")
    print(f"Date:        {target_date}")
    print(f"Time:        {target_time}")
    print(f"Output File: {output_path}")
    print("=" * 70)

    # Load plant configuration
    config.load_plant_profile(plant_name)
    is_wind = config.is_wind_plant()

    if is_wind:
        print(f"\n[Wind Pipeline] Running 24-hr Calibrated Multi-Model Wind Ensemble for {plant_name}...")
        from modules.weather.strategies.intellis_wind import calculate_wind_schedule_96block, WindTurbineProfile
        wind_prof = WindTurbineProfile.from_plant_profile(getattr(config, "PLANT_PROFILE", {}) or plant_name)
        t_hr, t_min = [int(p) for p in target_time.split(":")[:2]]
        curr_block = ((t_hr * 60 + t_min) // 15) + 1

        wind_sched = calculate_wind_schedule_96block(
            latitude=config.PLANT_LAT,
            longitude=config.PLANT_LON,
            target_date_str=target_date,
            profile=wind_prof,
            scada_actuals=live_meter_csv,
            current_block=curr_block,
            enable_slot_selection=True,
            enable_bias_correction=True,
            enable_telemetry_blending=True,
        )

        import csv
        fieldnames = ["Block", "Time Interval (15 minute interval)", "wind_speed_hub_m_s", "air_density_kg_m3", "intellis_gti", "intellis_mw", "schedule_mw"]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for b in wind_sched["blocks"]:
                writer.writerow({
                    "Block": b["block"],
                    "Time Interval (15 minute interval)": b["time_interval"],
                    "wind_speed_hub_m_s": b.get("wind_speed_hub_m_s"),
                    "air_density_kg_m3": b.get("air_density_kg_m3"),
                    "intellis_gti": b.get("intellis_gti", 0.0),
                    "intellis_mw": b["intellis_mw"],
                    "schedule_mw": b["schedule_mw"],
                })
        blocks = wind_sched["blocks"]
    else:
        print(f"\n[Solar Pipeline] Running 143-Model Ensemble GTI AI + Arbiter for {plant_name}...")
        from modules.solar_schedule.solar_scheduler import SolarScheduleEngine, load_plant_profile
        prof = load_plant_profile(plant_name)
        ai_engine = SolarScheduleEngine(plant_profile=prof)

        sched_res = ai_engine.generate_revision_schedule_csv(
            target_date_str=target_date,
            target_time_str=target_time,
            output_csv_path=output_path,
            live_meter_csv_path=live_meter_csv,
        )
        blocks = sched_res.get("blocks", [])

    print(f"\n[SUCCESS] Successfully generated 96-block schedule for {plant_name}!")
    print(f"Output saved to: {output_path}")

    # Display sample output around current block
    t_hr, t_min = [int(p) for p in target_time.split(":")[:2]]
    target_block = ((t_hr * 60 + t_min) // 15)
    sample_blocks = [b for b in blocks if (target_block - 2) <= b["block"] <= (target_block + 6)]

    if sample_blocks:
        print("\nSchedule Snapshot around Current Revision Horizon:")
        print(f"{'Block':<8} {'Time Interval':<18} {'Scheduled (MW)':<16} {'GTI (W/m2)':<12}")
        print("-" * 56)
        for b in sample_blocks:
            print(f"{b['block']:<8} {b['time_interval']:<18} {b['schedule_mw']:<16} {b.get('intellis_gti', 0.0):<12}")
        print("-" * 56)

    return {"status": "ok", "plant": plant_name, "date": target_date, "output_path": str(output_path)}


def main():
    parser = argparse.ArgumentParser(description="Intellis AI Local Schedule Runner")
    parser.add_argument("--plant", "-p", required=True, help="Plant name (e.g., GSNP, KOTHAGUDEM, JEWLI, SIRMOUR)")
    parser.add_argument("--date", "-d", default=None, help="Target date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--time", "-t", default="10:00", help="Target revision time in HH:MM format (default: 10:00)")
    parser.add_argument("--output", "-o", default=None, help="Custom output CSV file path")
    parser.add_argument("--meter", "-m", default=None, help="Path to live meter CSV file (optional)")

    args = parser.parse_args()
    output_p = Path(args.output) if args.output else None
    meter_p = Path(args.meter) if args.meter else None

    run_local_schedule(
        plant_name=args.plant,
        target_date=args.date,
        target_time=args.time,
        output_path=output_p,
        live_meter_csv=meter_p,
    )


if __name__ == "__main__":
    main()
