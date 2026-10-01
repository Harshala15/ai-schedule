"""
Module 5: Week-Ahead Wind Statutory 672-Block CSV Formatter and S3 Exporter.
Headers: S. No,From,To,SCH_MW,AvC_MW
Destination: s3://{s3_bucket}/intellis Weekhead wind/{site}/{start_date}/{plant}_{start_date}_weekahead_WA.csv
"""

from __future__ import annotations
import os
import datetime as dt
from pathlib import Path
from typing import Dict, Any
import boto3
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo


class WAWindFormatterS3Engine:
    """
    Builds statutory 7-day (672-row) Week-Ahead schedule CSV for Wind and uploads to S3.
    """

    def __init__(self, s3_bucket: str = "vedanjay-schedules-test-608744602858"):
        self.s3_bucket = s3_bucket
        self.s3_client = boto3.client("s3")

    def build_schedule_dataframe(
        self,
        final_schedule_mw_672: np.ndarray,
        start_date_str: str,
        rated_capacity_mw: float,
    ) -> pd.DataFrame:
        """
        Builds the 672-row DataFrame: S. No,From,To,SCH_MW,AvC_MW.
        S. No resets from 1 to 96 for each of the 7 days.
        """
        start_dt = dt.datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        rows = []

        for b in range(672):
            day_idx = b // 96
            block_of_day = (b % 96) + 1

            block_start = start_dt + dt.timedelta(minutes=b * 15)
            block_end = block_start + dt.timedelta(minutes=15)

            from_str = block_start.strftime("%Y-%m-%d %H:%M")
            to_str = block_end.strftime("%Y-%m-%d %H:%M")

            sch_mw = float(final_schedule_mw_672[b])
            avc_mw = float(rated_capacity_mw)

            rows.append({
                "S. No": block_of_day,
                "From": from_str,
                "To": to_str,
                "SCH_MW": f"{sch_mw:.2f}",
                "AvC_MW": f"{avc_mw:.2f}",
            })

        return pd.DataFrame(rows)

    def export_and_upload(
        self,
        df_schedule: pd.DataFrame,
        plant_name: str,
        start_date_str: str,
        today_str: str = "",
    ) -> Dict[str, Any]:
        """
        Saves CSV locally and uploads to S3 under 'intellis Weekhead wind/{site}/{today_str}/'.
        """
        if not today_str:
            today_str = (dt.datetime.strptime(start_date_str, "%Y-%m-%d") - dt.timedelta(days=1)).strftime("%Y-%m-%d")

        local_dir = Path("/tmp/week_ahead_wind") / today_str
        local_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{plant_name.lower()}_{start_date_str}_weekahead_WA.csv"
        local_path = local_dir / filename
        df_schedule.to_csv(local_path, index=False)

        # S3 Key: 'intellis Weekhead wind/{SITE}/{TODAY_DATE}/{FILENAME}'
        site_folder = plant_name.upper()
        s3_key = f"intellis Weekhead wind/{site_folder}/{today_str}/{filename}"
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

        total_mwh = float(df_schedule["SCH_MW"].astype(float).sum() * 0.25)

        return {
            "plant_name": plant_name,
            "start_date": start_date_str,
            "filename": filename,
            "s3_uri": s3_uri,
            "s3_key": s3_key,
            "local_path": str(local_path),
            "upload_success": upload_success,
            "total_7day_generation_mwh": round(total_mwh, 2),
            "total_blocks": len(df_schedule),
        }
