"""
Master Week-Ahead Solar Forecasting Engine.
Generates 7-day rolling 672-block statutory schedules using multi-day NWP physics.
"""

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Dict, Any
import numpy as np

from .wa_sun_trajectory_engine import WASunTrajectoryEngine
from .wa_nwp_fetch_engine import WANWPFetchEngine
from .wa_spline_kt_engine import WASplineKtEngine
from .wa_power_derate_engine import WAPowerDerateEngine
from .wa_formatter_s3_engine import WAFormatterS3Engine


class WASolarEngine:
    """
    Coordinates the 7-day Week-Ahead solar scheduling pipeline.
    """

    def __init__(
        self,
        s3_bucket: str = "vedanjay-schedules-test-608744602858",
        api_key: str = "",
    ):
        self.s3_bucket = s3_bucket
        self.api_key = api_key or os.getenv("OPENMETEO_API_KEY", "jbThkFlLZSXZE3CU").strip()

    def _load_plant_specifications(self, plant_name: str) -> Dict[str, Any]:
        """Loads coordinates, capacity, and tilt geometry from plant profile."""
        clean_name = plant_name.upper().strip()
        candidates = [
            Path(__file__).parent.parent.parent / "plant_profiles" / f"{clean_name}.json",
            Path(f"/var/task/plant_profiles/{clean_name}.json"),
            Path(f"schedule/plant_profiles/{clean_name}.json"),
        ]
        spec = {}
        for c in candidates:
            if c.exists():
                try:
                    spec = json.loads(c.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass

        lat = float(spec.get("lat") or spec.get("latitude") or 17.50)
        lon = float(spec.get("lon") or spec.get("longitude") or 80.60)
        cap_ac = float(spec.get("ac_capacity_mw") or spec.get("capacity_mw") or 10.0)
        cap_dc = float(spec.get("dc_capacity_mw") or (cap_ac * 1.30))
        tilt = float(spec.get("tilt") or spec.get("panel_tilt") or 15.0)
        azimuth = float(spec.get("azimuth") or 180.0)

        return {
            "plant_name": clean_name,
            "lat": lat,
            "lon": lon,
            "capacity_ac_mw": cap_ac,
            "capacity_dc_mw": cap_dc,
            "tilt": tilt,
            "azimuth": azimuth,
            "elevation_m": float(spec.get("elevation_m") or 450.0),
        }

    def run_pipeline(
        self,
        plant_name: str,
        start_date_str: str,
        today_str: str = "",
    ) -> Dict[str, Any]:
        """Executes full 7-day Week-Ahead solar scheduling."""
        print(f"[{plant_name}] Initiating Week-Ahead Solar Forecast starting {start_date_str} (7 Days / 672 Blocks)...")

        spec = self._load_plant_specifications(plant_name)
        print(f"  [1/5] Specifications: {spec['capacity_ac_mw']} MW AC ({spec['capacity_dc_mw']} MW DC) | Lat: {spec['lat']:.4f}, Lon: {spec['lon']:.4f} | Tilt: {spec['tilt']}°")

        # 1. 7-Day Sun Trajectory & Clear-Sky POA
        sun_engine = WASunTrajectoryEngine(
            lat=spec["lat"],
            lon=spec["lon"],
            elevation_m=spec["elevation_m"],
            surface_tilt=spec["tilt"],
            surface_azimuth=spec["azimuth"],
        )
        midpoints_672, poa_cs_672, ghi_cs_672, solar_zenith_672 = sun_engine.compute_7day_clearsky_trajectory(
            start_date_str=start_date_str,
        )
        print(f"  [2/5] Sun trajectory computed (peak clear-sky POA: {np.max(poa_cs_672):.1f} W/m2).")

        # 2. 7-Day Multi-Model NWP Weather Fetch
        nwp_engine = WANWPFetchEngine(api_key=self.api_key)
        weather_data = nwp_engine.fetch_7day_weather(
            latitude=spec["lat"],
            longitude=spec["lon"],
            start_date_str=start_date_str,
        )
        ghi_matrix_168, mean_temp_168 = nwp_engine.extract_ghi_and_temp_matrices(weather_data)
        print(f"  [3/5] Weather fetched: {ghi_matrix_168.shape[0]} ensemble members over 168 hours.")

        # 3. 7-Day Spline Clearness Index Transposition
        spline_engine = WASplineKtEngine()
        poa_matrix_672 = spline_engine.compute_7day_poa_matrix(
            nwp_hourly_ghi=ghi_matrix_168,
            poa_cs_672=poa_cs_672,
            ghi_cs_672=ghi_cs_672,
        )

        # 4. Thermal Derate & Guardrails
        power_engine = WAPowerDerateEngine(
            capacity_ac_mw=spec["capacity_ac_mw"],
            capacity_dc_mw=spec["capacity_dc_mw"],
        )
        final_schedule_mw_672 = power_engine.convert_poa_to_power(
            poa_matrix_672=poa_matrix_672,
            poa_cs_672=poa_cs_672,
            ambient_temp_168=mean_temp_168,
            solar_zenith_672=solar_zenith_672,
        )
        print(f"  [4/5] Power converted: scheduled peak across 7 days: {np.max(final_schedule_mw_672):.2f} MW.")

        # 5. Formatter & S3 Upload
        formatter_engine = WAFormatterS3Engine(s3_bucket=self.s3_bucket)
        df_schedule = formatter_engine.build_schedule_dataframe(
            final_schedule_mw_672=final_schedule_mw_672,
            start_date_str=start_date_str,
            capacity_ac_mw=spec["capacity_ac_mw"],
        )
        export_meta = formatter_engine.export_and_upload(
            df_schedule=df_schedule,
            plant_name=plant_name,
            start_date_str=start_date_str,
            today_str=today_str,
        )
        print(f"  [5/5] S3 export completed -> {export_meta['s3_uri']} ({export_meta['total_blocks']} rows)")

        return {
            **export_meta,
            "capacity_ac_mw": spec["capacity_ac_mw"],
            "max_scheduled_mw": round(float(np.max(final_schedule_mw_672)), 2),
        }


def generate_solar_week_ahead_schedule(
    plant_name: str,
    start_date_str: str,
    today_str: str = "",
    s3_bucket: str = "vedanjay-schedules-test-608744602858",
) -> Dict[str, Any]:
    """Top-level functional API for Week-Ahead Solar Scheduling."""
    engine = WASolarEngine(s3_bucket=s3_bucket)
    return engine.run_pipeline(
        plant_name=plant_name,
        start_date_str=start_date_str,
        today_str=today_str,
    )
