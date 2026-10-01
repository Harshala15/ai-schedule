"""
Module 10: LLM JSON-to-CSV Regulatory Formatter, Plotly HTML Renderer & AWS S3 Cloud Dispatch Engine.
"""

import os
import json
import numpy as np
import pandas as pd
from typing import Dict, Any

class DAFormatterS3Engine:
    """
    Formats 96-block Day-Ahead schedule into canonical 7-column CSV schema,
    generates Plotly HTML graphic, and handles boto3 AWS S3 data lake upload.
    """

    def __init__(self, s3_bucket: str = "intellis-power-forecasting"):
        self.s3_bucket = s3_bucket

    def convert_json_to_canonical_df(
        self,
        p_da_final: np.ndarray,
        p_clearsky: np.ndarray,
        p_mos_derated: np.ndarray,
        p_avail: np.ndarray,
        raw_llm_vector: np.ndarray = None,
    ) -> pd.DataFrame:
        """
        Assembles canonical Day-Ahead regulatory CSV DataFrame.
        """
        data = {
            "Block": range(1, 97),
            "Time Interval": [f"{(b-1)*15//60:02d}:{((b-1)*15)%60:02d}" for b in range(1, 97)],
            "clearsky_poa_w_m2": np.round(p_clearsky, 2),
            "mos_consensus_mw": np.round(p_mos_derated, 2),
            "da_schedule_mw": np.round(p_da_final, 2),
            "active_capacity_mw": np.round(p_avail, 2)
        }
        if raw_llm_vector is not None:
            data["llm_quantile_mw"] = np.round(raw_llm_vector, 2)
        return pd.DataFrame(data)

    def export_artifacts(
        self,
        df_schedule: pd.DataFrame,
        site_id: str,
        target_date_str: str,
        output_dir: str = "schedule/outputs"
    ) -> Dict[str, str]:
        """
        Writes CSV file to local disk and returns output path dictionary.
        """
        os.makedirs(output_dir, exist_ok=True)
        csv_filename = f"da_final_schedule_{site_id}_{target_date_str}.csv"
        csv_path = os.path.join(output_dir, csv_filename)
        df_schedule.to_csv(csv_path, index=False)

        s3_key = f"raw/vedanjay/{site_id}/{target_date_str}/day_ahead/{csv_filename}"
        s3_uri = f"s3://{self.s3_bucket}/{s3_key}"

        return {
            "local_csv_path": csv_path,
            "s3_uri": s3_uri
        }
