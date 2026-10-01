"""
Master Week-Ahead Wind Forecasting Engine.
Generates 7-day rolling 672-block statutory schedules using 168h hub-height NWP ensemble,
IEC 61400-12 density correction, and non-linear power curve transformation.
"""

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Dict, Any
import numpy as np

from .wa_wind_aerodynamics_engine import WAWindTurbineProfile, compute_gross_turbine_power
from .wa_wind_density_engine import WAWindDensityEngine
from .wa_wind_nwp_engine import WAWindNWPEngine
from .wa_wind_park_derate_engine import WAWindParkDerateEngine
from .wa_wind_formatter_s3_engine import WAWindFormatterS3Engine


class WAWindEngine:
    """
    Coordinates the 7-day Week-Ahead wind scheduling pipeline.
    """

    def __init__(
        self,
        s3_bucket: str = "vedanjay-schedules-test-608744602858",
        api_key: str = "",
    ):
        self.s3_bucket = s3_bucket
        self.api_key = api_key or os.getenv("OPENMETEO_API_KEY", "jbThkFlLZSXZE3CU").strip()
        self.nwp_engine = WAWindNWPEngine(api_key=self.api_key)
        self.density_engine = WAWindDensityEngine()
        self.park_engine = WAWindParkDerateEngine()
        self.formatter_engine = WAWindFormatterS3Engine(s3_bucket=self.s3_bucket)

    def _resolve_coordinates(self, plant_name: str) -> tuple[float, float]:
        """Resolves latitude and longitude for wind plants."""
        clean_name = plant_name.upper().strip()
        coords_map = {
            "JEWLI": (17.3850, 76.8200),
            "JGBPL": (23.8200, 77.1500),
            "CHANDAWASA": (24.166208, 75.459684),
            "CHANDWASA": (24.166208, 75.459684),
        }
        if clean_name in coords_map:
            return coords_map[clean_name]

        candidates = [
            Path(__file__).parent.parent.parent / "plant_profiles" / f"{clean_name}.json",
            Path(f"/var/task/plant_profiles/{clean_name}.json"),
            Path(f"schedule/plant_profiles/{clean_name}.json"),
        ]
        for c in candidates:
            if c.exists():
                try:
                    data = json.loads(c.read_text(encoding="utf-8"))
                    lat = float(data.get("lat") or data.get("latitude") or 0.0)
                    lon = float(data.get("lon") or data.get("longitude") or 0.0)
                    if lat != 0.0 and lon != 0.0:
                        return lat, lon
                except Exception:
                    pass
        return 17.3850, 76.8200

    def run_pipeline(
        self,
        plant_name: str,
        start_date_str: str,
        today_str: str = "",
    ) -> Dict[str, Any]:
        """Executes full 7-day Week-Ahead wind scheduling."""
        print(f"[{plant_name}] Initiating Week-Ahead Wind Forecast starting {start_date_str} (7 Days / 672 Blocks)...")

        # 1. Profile & Coordinates
        profile = WAWindTurbineProfile.from_plant_profile(plant_name)
        lat, lon = self._resolve_coordinates(plant_name)
        print(f"  [1/5] Specifications: {profile.turbine_manufacturer} {profile.turbine_model} | {profile.rated_capacity_mw} MW | Hub: {profile.hub_height_m}m")

        # 2. 7-Day Hub-Height NWP Weather Fetch
        weather_data = self.nwp_engine.fetch_7day_weather(
            latitude=lat,
            longitude=lon,
            start_date_str=start_date_str,
            hub_height_m=profile.hub_height_m,
        )
        member_speeds_672, temps_168, pressures_168 = self.nwp_engine.extract_member_hub_speeds_672(
            weather_data=weather_data,
            hub_height_m=profile.hub_height_m,
        )
        print(f"  [2/5] Weather fetched: {len(member_speeds_672)} ensemble members over 168 hours.")

        # 3. Dynamic Atmospheric Air Density & 672-block interpolation
        hourly_densities = [
            self.density_engine.calculate_air_density(pressures_168[i], temps_168[i])
            for i in range(168)
        ]
        b_densities_672 = self.density_engine.interpolate_to_672_blocks(hourly_densities)

        # 4. Member-Level Power Conversion across all 672 blocks (Jensen's Inequality Protection)
        member_powers_672: list[np.ndarray] = []
        for m_name, speeds_672 in member_speeds_672.items():
            m_powers = np.zeros(672)
            for b in range(672):
                v_eff = self.density_engine.calculate_effective_velocity(
                    v_hub_ms=speeds_672[b],
                    air_density=b_densities_672[b],
                    standard_density=profile.standard_air_density,
                )
                m_powers[b] = compute_gross_turbine_power(v_eff, profile)
            member_powers_672.append(m_powers)

        if member_powers_672:
            consensus_gross_mw_672 = np.mean(member_powers_672, axis=0)
        else:
            consensus_gross_mw_672 = np.zeros(672)

        print(f"  [3/5] Consensus gross generation computed (peak: {np.max(consensus_gross_mw_672):.2f} MW).")

        # 5. Park Derate & Empirical Multipliers across all 7 days
        final_schedule_mw_672 = self.park_engine.apply_park_derate_672(
            gross_power_672=consensus_gross_mw_672,
            rated_capacity_mw=profile.rated_capacity_mw,
            park_derate_factor=profile.park_derate_factor,
            plant_name=plant_name,
        )
        print(f"  [4/5] Park derating & calibration applied (scheduled peak: {np.max(final_schedule_mw_672):.2f} MW).")

        # 6. Formatter & S3 Upload under 'intellis Weekhead wind/'
        df_schedule = self.formatter_engine.build_schedule_dataframe(
            final_schedule_mw_672=final_schedule_mw_672,
            start_date_str=start_date_str,
            rated_capacity_mw=profile.rated_capacity_mw,
        )
        export_meta = self.formatter_engine.export_and_upload(
            df_schedule=df_schedule,
            plant_name=plant_name,
            start_date_str=start_date_str,
            today_str=today_str,
        )
        print(f"  [5/5] S3 export completed -> {export_meta['s3_uri']} ({export_meta['total_blocks']} rows)")

        return {
            **export_meta,
            "rated_capacity_mw": profile.rated_capacity_mw,
            "max_scheduled_mw": round(float(np.max(final_schedule_mw_672)), 2),
        }


def generate_wind_week_ahead_schedule(
    plant_name: str,
    start_date_str: str,
    today_str: str = "",
    s3_bucket: str = "vedanjay-schedules-test-608744602858",
) -> Dict[str, Any]:
    """Top-level functional API for Week-Ahead Wind Scheduling."""
    engine = WAWindEngine(s3_bucket=s3_bucket)
    return engine.run_pipeline(
        plant_name=plant_name,
        start_date_str=start_date_str,
        today_str=today_str,
    )
