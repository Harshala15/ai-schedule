"""
multi_generator_engine.py

Master Multi-Generator Asset Scheduling Engine for ZTRIC, ENRICH, and SHAHA.
Generates statutory Day-Ahead (96 blocks) and Week-Ahead (672 blocks / 7 days) schedules
with dynamic asset capacities fetched from DynamoDB table 'multi_generator_plant'
and active control window directives applied from 'plant_control_windows_test'.
"""

from __future__ import annotations

import os
import json
import logging
import datetime as dt
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from decimal import Decimal

import boto3
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Fallback verified asset capacities in MW for multi-generator solar parks
MULTI_GENERATOR_CONFIGS: Dict[str, Dict[str, Any]] = {
    "ZTRIC": {
        "dynamodb_plant_id": "ZETRIC_SOLAR_PARK",
        "display_name": "ZETRIC SOLAR PARK (Chakur 132kV)",
        "lat": 18.0500,
        "lon": 76.2000,
        "assets": {
            "DE_SOLAR": {"ac_cap": 1.950, "dc_cap": 2.500, "buyer": "OA_MSEDCL"},
            "GAJLAXMI": {"ac_cap": 1.475, "dc_cap": 2.000, "buyer": "OA_MSEDCL"},
            "CHAKUR_ONE_BLOCK_1": {"ac_cap": 3.800, "dc_cap": 5.000, "buyer": "OA_MSEDCL"},
            "CHAKUR_ONE_BLOCK_2": {"ac_cap": 4.200, "dc_cap": 5.500, "buyer": "OA_MSEDCL"},
            "POLYBOND": {"ac_cap": 2.300, "dc_cap": 3.000, "buyer": "OA_MSEDCL"},
            "SNHEAT": {"ac_cap": 1.475, "dc_cap": 2.000, "buyer": "OA_MSEDCL"},
            "INTEGRATED": {"ac_cap": 1.000, "dc_cap": 1.500, "buyer": "OA_MSEDCL"},
            "INDIQUBE": {"ac_cap": 2.950, "dc_cap": 3.500, "buyer": "AEML"},
        },
    },
    "ENRICH": {
        "dynamodb_plant_id": "ENRICH",
        "display_name": "ENRICH SOLAR PARK (Akkalkot)",
        "lat": 17.5553,
        "lon": 76.2016,
        "assets": {
            "CLIMATEDETOX": {"ac_cap": 2.500, "dc_cap": 3.500, "buyer": "OA_MSEDCL"},
            "EMIL": {"ac_cap": 3.310, "dc_cap": 4.500, "buyer": "OA_MSEDCL"},
            "UPL": {"ac_cap": 3.000, "dc_cap": 4.000, "buyer": "OA_MSEDCL"},
        },
    },
    "SHAHA": {
        "dynamodb_plant_id": "SHAHA",
        "display_name": "SHAHA SOLAR PARK",
        "lat": 17.5000,
        "lon": 76.1500,
        "assets": {
            "PRANAV": {"ac_cap": 1.680, "dc_cap": 2.200, "buyer": "OA_MSEDCL"},
            "SIDDEHESH": {"ac_cap": 1.680, "dc_cap": 2.200, "buyer": "OA_MSEDCL"},
            "LOKGREENB2": {"ac_cap": 8.810, "dc_cap": 11.800, "buyer": "AEML"},
        },
    },
}


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, Decimal):
        return float(val)
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


class MultiGeneratorEngine:
    """
    Orchestrates Day-Ahead and Week-Ahead schedule generation for ZTRIC, ENRICH, and SHAHA.
    """

    def __init__(
        self,
        s3_bucket: Optional[str] = None,
        aws_profile: Optional[str] = None,
        region_name: str = "ap-south-1",
    ):
        self.s3_bucket = s3_bucket or os.getenv("SCHEDULE_BUCKET", "vedanjay-schedules-test-608744602858")
        self.region_name = region_name
        self.aws_profile = aws_profile or os.getenv("AWS_PROFILE")

        if self.aws_profile:
            try:
                self.session = boto3.Session(profile_name=self.aws_profile, region_name=region_name)
            except Exception:
                self.session = boto3.Session(region_name=region_name)
        else:
            self.session = boto3.Session(region_name=region_name)

        self.s3_client = self.session.client("s3")
        self.dynamodb_res = self.session.resource("dynamodb")

    def get_plant_asset_configs(self, plant_name: str) -> Dict[str, Dict[str, Any]]:
        """
        Fetches live asset AC/DC capacities for a multi-generator plant from DynamoDB 'multi_generator_plant'.
        Falls back to verified default configurations if record is absent.
        """
        clean_name = plant_name.upper().strip()
        if clean_name not in MULTI_GENERATOR_CONFIGS:
            raise ValueError(f"Unknown multi-generator plant '{plant_name}'. Supported: ZTRIC, ENRICH, SHAHA.")

        default_cfg = MULTI_GENERATOR_CONFIGS[clean_name]
        ddb_id = default_cfg["dynamodb_plant_id"]
        assets_map = {k: dict(v) for k, v in default_cfg["assets"].items()}

        table_name = os.getenv("MULTI_GENERATOR_TABLE", "multi_generator_plant")
        try:
            tbl = self.dynamodb_res.Table(table_name)
            resp = tbl.get_item(Key={"plant_id": ddb_id})
            item = resp.get("Item")
            if item:
                mgp_list = item.get("template_config", {}).get("multi_generator_plants", [])
                for plant_entry in mgp_list:
                    if str(plant_entry.get("plantName", "")).strip().upper() in (clean_name, ddb_id, "ZETRIC"):
                        assets_list = plant_entry.get("assets", [])
                        for a in assets_list:
                            a_name = str(a.get("assetName", "")).strip().upper().replace("-", "_").replace(" ", "_")
                            matched_key = None
                            for k in assets_map.keys():
                                if k == a_name or k.replace("_", "") == a_name.replace("_", ""):
                                    matched_key = k
                                    break

                            if matched_key:
                                ac_cap = _to_float(a.get("acCapacityMw") or a.get("schedulingCapacityAcMw"), default=assets_map[matched_key]["ac_cap"])
                                dc_cap = _to_float(a.get("dcCapacityMw") or a.get("totalCapacityDcMw"), default=assets_map[matched_key]["dc_cap"])
                                if ac_cap > 0.0:
                                    assets_map[matched_key]["ac_cap"] = round(ac_cap, 3)
                                    assets_map[matched_key]["dc_cap"] = round(dc_cap, 3)
        except Exception as exc:
            logger.warning("Could not query DynamoDB '%s' for %s assets: %s; using verified defaults.", table_name, clean_name, exc)

        return assets_map

    def compute_solar_unconstrained_base_curve(
        self,
        nominal_total_ac: float,
        target_date_str: str,
    ) -> np.ndarray:
        """
        Computes synthetic 96-block solar generation curve for a target date.
        """
        curve_96 = np.zeros(96)
        for b in range(23, 74):
            # Solar half-sine wave peaking near block 48
            val = nominal_total_ac * 0.85 * np.sin(np.pi * (b - 23) / 50.0)
            curve_96[b] = round(max(0.0, float(val)), 2)
        return curve_96

    def build_da_schedule_dataframe(
        self,
        plant_name: str,
        target_date_str: str,
        unconstrained_total_mw_96: np.ndarray,
    ) -> pd.DataFrame:
        """
        Builds canonical 96-block Day-Ahead DataFrame for a multi-generator plant:
        Columns: Block_No, From, To, <ASSET_1>, ..., <PLANT>_MW, <PLANT>_AvC_MW
        """
        clean_name = plant_name.upper().strip()
        asset_configs = self.get_plant_asset_configs(clean_name)
        asset_keys = list(asset_configs.keys())

        # Total plant nominal AC capacity
        nominal_total_ac = sum(cfg["ac_cap"] for cfg in asset_configs.values())
        if nominal_total_ac <= 0.0:
            nominal_total_ac = 10.0

        # Load active control windows from DynamoDB for site-level or asset-level status changes
        from modules.control_windows import PlantControlWindowEngine
        cw_engine = PlantControlWindowEngine()
        windows = cw_engine.load_active_windows(clean_name, target_date_str)

        start_dt = dt.datetime.strptime(target_date_str, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        rows = []

        for b in range(1, 97):
            block_start = start_dt + dt.timedelta(minutes=(b - 1) * 15)
            block_end = block_start + dt.timedelta(minutes=15)
            from_str = block_start.strftime("%Y-%m-%d %H:%M")
            to_str = block_end.strftime("%Y-%m-%d %H:%M")

            base_raw_mw = float(unconstrained_total_mw_96[b - 1])

            # 1. Compute effective block AC capacity per asset considering asset-specific or site-wide control windows
            asset_effective_caps: Dict[str, float] = {}
            for a_key, a_cfg in asset_configs.items():
                a_ac_cap = a_cfg["ac_cap"]
                a_dc_cap = a_cfg["dc_cap"]

                a_windows = [
                    w for w in windows
                    if str(w.get("asset_id") or w.get("sub_plant_id") or "ALL").strip().upper() in (a_key, "ALL")
                ]
                ctrl_match = cw_engine.match_block_control(b, target_date_str, a_windows, a_ac_cap, a_dc_cap)
                asset_effective_caps[a_key] = float(ctrl_match["effective_control_capacity_ac_mw"])

            # 2. Check site-level park curtailment override
            site_windows = [w for w in windows if str(w.get("asset_id") or w.get("sub_plant_id") or "ALL").strip().upper() == "ALL"]
            site_ctrl = cw_engine.match_block_control(b, target_date_str, site_windows, nominal_total_ac, nominal_total_ac * 1.3)
            site_eff_cap = float(site_ctrl["effective_control_capacity_ac_mw"])

            # 3. Calculate asset-wise DA schedule MW for block b
            asset_mw_map: Dict[str, float] = {}
            active_cap_sum = sum(asset_effective_caps.values())

            for a_key, a_cfg in asset_configs.items():
                eff_cap = asset_effective_caps[a_key]
                if eff_cap <= 0.0 or nominal_total_ac <= 0.0:
                    asset_mw_map[a_key] = 0.0
                else:
                    capacity_share = a_cfg["ac_cap"] / nominal_total_ac
                    raw_asset_mw = base_raw_mw * capacity_share
                    asset_mw_map[a_key] = min(raw_asset_mw, eff_cap)

            total_unconstrained = sum(asset_mw_map.values())
            if site_eff_cap < nominal_total_ac and total_unconstrained > site_eff_cap:
                scale = site_eff_cap / max(0.001, total_unconstrained)
                for a_key in asset_keys:
                    asset_mw_map[a_key] = round(asset_mw_map[a_key] * scale, 2)
            else:
                for a_key in asset_keys:
                    asset_mw_map[a_key] = round(asset_mw_map[a_key], 2)

            total_plant_mw = round(sum(asset_mw_map.values()), 2)
            total_plant_avc = round(min(active_cap_sum, site_eff_cap), 2)

            row_dict = {
                "Block_No": b,
                "From": from_str,
                "To": to_str,
            }
            for a_key in asset_keys:
                row_dict[a_key] = f"{asset_mw_map[a_key]:.2f}"

            row_dict[f"{clean_name}_MW"] = f"{total_plant_mw:.2f}"
            row_dict[f"{clean_name}_AvC_MW"] = f"{total_plant_avc:.2f}"
            rows.append(row_dict)

        return pd.DataFrame(rows)

    def build_wa_schedule_dataframe(
        self,
        plant_name: str,
        start_date_str: str,
        unconstrained_7day_mw_672: np.ndarray,
    ) -> pd.DataFrame:
        """
        Builds statutory 672-block (7 rolling days) Week-Ahead DataFrame:
        Columns: Block_No, From, To, SCH_MW, AvC_MW
        Block_No resets from 1 to 96 for each day.
        """
        clean_name = plant_name.upper().strip()
        asset_configs = self.get_plant_asset_configs(clean_name)
        nominal_total_ac = sum(cfg["ac_cap"] for cfg in asset_configs.values())
        if nominal_total_ac <= 0.0:
            nominal_total_ac = 10.0

        start_dt = dt.datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        rows = []

        for b in range(672):
            block_of_day = (b % 96) + 1
            block_start = start_dt + dt.timedelta(minutes=b * 15)
            block_end = block_start + dt.timedelta(minutes=15)

            from_str = block_start.strftime("%Y-%m-%d %H:%M")
            to_str = block_end.strftime("%Y-%m-%d %H:%M")

            sch_mw = float(unconstrained_7day_mw_672[b])
            sch_mw = min(sch_mw, nominal_total_ac)
            avc_mw = nominal_total_ac

            rows.append({
                "Block_No": block_of_day,
                "From": from_str,
                "To": to_str,
                "SCH_MW": f"{sch_mw:.2f}",
                "AvC_MW": f"{avc_mw:.2f}",
            })

        return pd.DataFrame(rows)

    def generate_and_dispatch_multi_generator_schedules(
        self,
        plant_name: str,
        target_date_str: str,
        today_str: str = "",
        run_tag: str = "da0",
    ) -> Dict[str, Any]:
        """
        Generates statutory DA (96 blocks) and WA (672 blocks) schedules for a multi-generator plant
        and dispatches both CSV files locally under 'data/output/{today_str}/{plant_name}/'.
        """
        clean_name = plant_name.upper().strip()
        if not today_str:
            today_str = (dt.datetime.strptime(target_date_str, "%Y-%m-%d") - dt.timedelta(days=1)).strftime("%Y-%m-%d")

        print(f"\n[MULTI_GENERATOR] Generating statutory DA ({run_tag.upper()}) and WA schedules for '{clean_name}'...")

        asset_configs = self.get_plant_asset_configs(clean_name)
        nominal_total_ac = sum(cfg["ac_cap"] for cfg in asset_configs.values())

        # 1. Base solar forecast curve
        unconstrained_da_96 = self.compute_solar_unconstrained_base_curve(nominal_total_ac, target_date_str)

        # 2. Build canonical Multi-Generator DA DataFrame
        df_da = self.build_da_schedule_dataframe(
            plant_name=clean_name,
            target_date_str=target_date_str,
            unconstrained_total_mw_96=unconstrained_da_96,
        )

        # Lambda can write only to /tmp; local runs keep repository data/output layout.
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT"):
            repo_root = Path("/tmp")
        else:
            repo_root = Path(__file__).parent.parent.parent.parent
        da_dir = repo_root / "data" / "output" / today_str / clean_name / "Dayahead"
        da_dir.mkdir(parents=True, exist_ok=True)
        da_filename = f"{clean_name.lower()}_{target_date_str}_{run_tag}.csv"
        da_local_path = da_dir / da_filename
        df_da.to_csv(da_local_path, index=False)

        print(f"  [SAVED LOCAL] {clean_name} Multi-Generator DA Schedule ({run_tag.upper()}) -> {da_local_path}")

        # S3 Upload for Day-Ahead Schedule
        da_s3_key = f"intellis Dayhead solar/{clean_name}/{target_date_str}/{da_filename}"
        da_s3_uri = f"s3://{self.s3_bucket}/{da_s3_key}"
        da_upload_success = False
        try:
            self.s3_client.upload_file(str(da_local_path), self.s3_bucket, da_s3_key, ExtraArgs={"ContentType": "text/csv"})
            da_upload_success = True
            print(f"  [S3 UPLOAD] {clean_name} Day-Ahead -> {da_s3_uri}")
        except Exception as exc:
            logger.warning("S3 upload failed for %s Day-Ahead CSV: %s", clean_name, exc)

        # 3. Build 672-block WA DataFrame
        unconstrained_wa_672 = np.tile(unconstrained_da_96, 7)
        df_wa = self.build_wa_schedule_dataframe(
            plant_name=clean_name,
            start_date_str=target_date_str,
            unconstrained_7day_mw_672=unconstrained_wa_672,
        )

        wa_dir = repo_root / "data" / "output" / today_str / clean_name / "Weekahead"
        wa_dir.mkdir(parents=True, exist_ok=True)
        wa_filename = f"{clean_name.lower()}_{today_str}_weekahead_WA.csv"
        wa_local_path = wa_dir / wa_filename
        df_wa.to_csv(wa_local_path, index=False)

        print(f"  [SAVED LOCAL] {clean_name} Multi-Generator WA Schedule -> {wa_local_path}")

        # S3 Upload for Week-Ahead Schedule
        wa_s3_key = f"intellis Weekhead solar/{clean_name}/{today_str}/{wa_filename}"
        wa_s3_uri = f"s3://{self.s3_bucket}/{wa_s3_key}"
        wa_upload_success = False
        try:
            self.s3_client.upload_file(str(wa_local_path), self.s3_bucket, wa_s3_key, ExtraArgs={"ContentType": "text/csv"})
            wa_upload_success = True
            print(f"  [S3 UPLOAD] {clean_name} Week-Ahead -> {wa_s3_uri}")
        except Exception as exc:
            logger.warning("S3 upload failed for %s Week-Ahead CSV: %s", clean_name, exc)

        return {
            "plant_name": clean_name,
            "target_date": target_date_str,
            "da_local_path": str(da_local_path),
            "da_s3_uri": da_s3_uri,
            "da_s3_key": da_s3_key,
            "da_columns": list(df_da.columns),
            "wa_local_path": str(wa_local_path),
            "wa_s3_uri": wa_s3_uri,
            "wa_s3_key": wa_s3_key,
            "wa_columns": list(df_wa.columns),
            "upload_success": da_upload_success and wa_upload_success,
            "total_da_blocks": len(df_da),
            "total_wa_blocks": len(df_wa),
        }
