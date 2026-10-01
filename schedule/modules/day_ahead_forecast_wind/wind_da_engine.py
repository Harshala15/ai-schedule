"""
Wind Day-Ahead Forecasting Engine (Master Coordinator).
Statutory 96-block continuous 24h schedule using hub-height NWP ensemble,
IEC 61400-12 density correction, and non-linear power curve transformation.
"""

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional
import numpy as np

from .wind_aerodynamic_curves import WindTurbineProfile, compute_gross_turbine_power
from .wind_density_iec_engine import WindDensityIECEngine
from .wind_nwp_hub_engine import WindNWPHubEngine
from .wind_park_derate_engine import WindParkDerateEngine
from .wind_formatter_s3_engine import WindFormatterS3Engine


class WindDAEngine:
    """
    Executes the complete Day-Ahead wind power scheduling pipeline:
    1. Loads turbine profile & plant coordinates.
    2. Fetches multi-model NWP weather ensemble at hub height.
    3. Computes atmospheric air density (IEC 61400-12).
    4. Evaluates non-linear turbine power curves per ensemble member (Jensen's inequality protection).
    5. Calculates consensus aerodynamic gross generation.
    6. Applies park derating (wake, electrical, availability) & empirical block multipliers.
    7. Exports to statutory 96-block CSV and uploads to S3 'intellis Dayhead wind/'.
    """

    def __init__(
        self,
        s3_bucket: str = "vedanjay-schedules-test-608744602858",
        api_key: str = "",
    ):
        self.s3_bucket = s3_bucket
        self.nwp_engine = WindNWPHubEngine(api_key=api_key)
        self.density_engine = WindDensityIECEngine()
        self.park_engine = WindParkDerateEngine()
        self.formatter_engine = WindFormatterS3Engine(s3_bucket=s3_bucket)

    def _resolve_coordinates(self, plant_name: str) -> tuple[float, float]:
        """Resolves latitude and longitude from plant profiles or fallback dict."""
        clean_name = plant_name.upper().strip()

        # Known coordinates table
        coords_map = {
            "CHANDAWASA": (24.166208, 75.459684),
            "CHANDWASA": (24.166208, 75.459684),
            "JEWLI": (17.3850, 76.8200),
            "JGBPL": (23.8200, 77.1500),
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

        return 24.1662, 75.4597  # Fallback Chandawasa coords

    def run_pipeline(
        self,
        plant_name: str,
        target_date_str: str,
        run_tag: str = "da0",
    ) -> Dict[str, Any]:
        """Runs the complete 7-stage Day-Ahead Wind forecast."""
        print(f"[{plant_name}] Initiating Day-Ahead Wind Forecast for {target_date_str} ({run_tag.upper()})...")

        # 1. Turbine Profile & Coordinates
        profile = WindTurbineProfile.from_plant_profile(plant_name)
        lat, lon = self._resolve_coordinates(plant_name)
        print(f"  [1/6] Profile loaded: {profile.turbine_manufacturer} {profile.turbine_model} | {profile.rated_capacity_mw} MW | Hub: {profile.hub_height_m}m")

        # 2. Multi-Model NWP Hub-Height Weather Fetch
        weather_data = self.nwp_engine.fetch_weather_ensemble(
            latitude=lat,
            longitude=lon,
            target_date_str=target_date_str,
            hub_height_m=profile.hub_height_m,
        )
        member_speeds_96, temps_24, pressures_24 = self.nwp_engine.extract_member_hub_speeds_96(
            weather_data=weather_data,
            hub_height_m=profile.hub_height_m,
        )
        print(f"  [2/6] Weather fetched: {len(member_speeds_96)} ensemble members available.")

        # 3. Dynamic Atmospheric Air Density & 96-block interpolation
        hourly_densities = [
            self.density_engine.calculate_air_density(pressures_24[i], temps_24[i])
            for i in range(24)
        ]
        b_densities = self.density_engine.interpolate_to_96_blocks(hourly_densities)
        print(f"  [3/6] Air density computed (mean: {np.mean(b_densities):.3f} kg/m3).")

        # 4. Member-Level Power Conversion (Jensen's Inequality Protection)
        member_powers_96: list[np.ndarray] = []
        for m_name, speeds_96 in member_speeds_96.items():
            m_powers = np.zeros(96)
            for b in range(96):
                v_eff = self.density_engine.calculate_effective_velocity(
                    v_hub_ms=speeds_96[b],
                    air_density=b_densities[b],
                    standard_density=profile.standard_air_density,
                )
                m_powers[b] = compute_gross_turbine_power(v_eff, profile)
            member_powers_96.append(m_powers)

        # 5. Consensus Aerodynamic Power & Mean Hub Wind Speed
        if member_powers_96:
            consensus_gross_mw_96 = np.mean(member_powers_96, axis=0)
            consensus_wind_speed_96 = np.mean(list(member_speeds_96.values()), axis=0)
        else:
            # Fallback synthetic profile
            consensus_wind_speed_96 = np.array([5.0 + 2.0 * np.sin(2 * np.pi * (b + 12) / 96.0) for b in range(96)])
            consensus_gross_mw_96 = np.zeros(96)
            for b in range(96):
                v_eff = self.density_engine.calculate_effective_velocity(consensus_wind_speed_96[b], b_densities[b])
                consensus_gross_mw_96[b] = compute_gross_turbine_power(v_eff, profile)

        print(f"  [4/6] Consensus gross power computed (peak: {np.max(consensus_gross_mw_96):.2f} MW).")

        # 6. Park Derate, Wake Losses & Calibrated Multipliers
        final_schedule_mw_96 = self.park_engine.apply_park_derate_and_calibration(
            gross_power_mw_96=consensus_gross_mw_96,
            rated_capacity_mw=profile.rated_capacity_mw,
            park_derate_factor=profile.park_derate_factor,
            plant_name=plant_name,
        )
        print(f"  [5/6] Park derating & calibration applied (scheduled peak: {np.max(final_schedule_mw_96):.2f} MW).")

        # 7. Formatter & S3 Export under 'intellis Dayhead wind/'
        df_schedule = self.formatter_engine.build_schedule_dataframe(
            final_schedule_mw_96=final_schedule_mw_96,
            hub_wind_speed_96=consensus_wind_speed_96,
            air_density_96=b_densities,
            rated_capacity_mw=profile.rated_capacity_mw,
        )
        export_meta = self.formatter_engine.export_and_upload(
            df_schedule=df_schedule,
            plant_name=plant_name,
            target_date_str=target_date_str,
            run_tag=run_tag,
        )
        print(f"  [6/6] S3 export completed -> {export_meta['s3_uri']}")

        return {
            **export_meta,
            "rated_capacity_mw": profile.rated_capacity_mw,
            "max_scheduled_mw": round(float(np.max(final_schedule_mw_96)), 2),
            "mean_scheduled_mw": round(float(np.mean(final_schedule_mw_96)), 2),
        }


def generate_wind_day_ahead_schedule(
    plant_name: str,
    target_date_str: str,
    run_tag: str = "da0",
    s3_bucket: str = "vedanjay-schedules-test-608744602858",
) -> Dict[str, Any]:
    """Top-level functional API for Day-Ahead Wind Scheduling."""
    engine = WindDAEngine(s3_bucket=s3_bucket)
    return engine.run_pipeline(
        plant_name=plant_name,
        target_date_str=target_date_str,
        run_tag=run_tag,
    )
