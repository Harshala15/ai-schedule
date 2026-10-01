"""
Module: Wind Day-Ahead Statutory Formatter and S3 Exporter.
Destination: s3://{s3_bucket}/intellis Dayhead wind/{site}/{target_date}/
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Dict, Any
import boto3
import numpy as np
import pandas as pd


class WindFormatterS3Engine:
    """
    Builds the regulatory 96-block 15-minute Day-Ahead CSV format
    and uploads to S3 bucket under dedicated prefix 'intellis Dayhead wind/'.
    """

    def __init__(self, s3_bucket: str = "vedanjay-schedules-test-608744602858"):
        self.s3_bucket = s3_bucket
        self.s3_client = boto3.client("s3")

    def build_schedule_dataframe(
        self,
        final_schedule_mw_96: np.ndarray,
        hub_wind_speed_96: np.ndarray,
        air_density_96: np.ndarray,
        rated_capacity_mw: float,
    ) -> pd.DataFrame:
        """Constructs statutory 96-block DataFrame."""
        rows = []
        for b in range(96):
            b_num = b + 1
            end_min = b_num * 15
            start_min = end_min - 15
            s_hr, s_min = divmod(start_min, 60)
            e_hr, e_min = divmod(end_min, 60)
            t_str = "24:00" if e_hr == 24 else f"{e_hr:02d}:{e_min:02d}"
            t_interval = f"{s_hr:02d}:{s_min:02d} - {t_str}"

            rows.append({
                "Block": b_num,
                "Time Interval": t_interval,
                "wind_speed_hub_m_s": round(float(hub_wind_speed_96[b]), 2),
                "air_density_kg_m3": round(float(air_density_96[b]), 3),
                "da_schedule_mw": round(float(final_schedule_mw_96[b]), 2),
                "active_capacity_mw": round(float(rated_capacity_mw), 2),
            })
        return pd.DataFrame(rows)

    def export_and_upload(
        self,
        df_schedule: pd.DataFrame,
        plant_name: str,
        target_date_str: str,
        run_tag: str = "da0",
    ) -> Dict[str, Any]:
        """Saves CSV locally and uploads to dedicated S3 wind folder."""
        local_dir = Path("/tmp/day_ahead_wind") / target_date_str
        local_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{plant_name}_{target_date_str}_{run_tag}.csv"
        local_path = local_dir / filename
        df_schedule.to_csv(local_path, index=False)

        # Dedicated Wind S3 folder: 'intellis Dayhead wind/'
        s3_key = f"intellis Dayhead wind/{plant_name}/{target_date_str}/{filename}"
        s3_uri = f"s3://{self.s3_bucket}/{s3_key}"

        upload_success = False
        try:
            self.s3_client.upload_file(
                str(local_path),
                self.s3_bucket,
                s3_key,
                ExtraArgs={"ContentType": "text/csv"},
            )
            upload_success = True
        except Exception as exc:
            print(f"  [ERROR] S3 upload failed for {plant_name}: {exc}")

        return {
            "plant_name": plant_name,
            "target_date": target_date_str,
            "run_tag": run_tag,
            "s3_uri": s3_uri,
            "s3_key": s3_key,
            "local_path": str(local_path),
            "upload_success": upload_success,
            "total_24h_generation_mwh": round(float(df_schedule["da_schedule_mw"].sum() * 0.25), 2),
        }
