"""
Solar Day-Ahead Generation Forecast Engine Master Orchestrator.

Orchestrates pure physics and multi-agency MOS consensus for Day-Ahead (Day T+1) 96-block schedule generation
prior to the statutory 10:00 AM gate closure, incorporating dynamic IST date resolution,
astronomical clear-sky transposition, 143-member NWP spline interpolation, and physical guardrail screening.
(Note: Day-Ahead LLM dependency has been completely removed in favor of deterministic physics).
"""

import os
import json
from datetime import datetime, timedelta
import pytz
import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple, List
from pathlib import Path

from .da_member_selection import DAMemberSelectionEngine

class SolarDayAheadEngine:
    """
    Master Day-Ahead Solar Schedule Generation Pipeline (Pure Physics & MOS Consensus).
    """

    def __init__(self, site_config: Dict[str, Any]):
        self.site_config = site_config
        self.site_id = site_config.get("site_id", "SOLAR_PLANT")
        self.p_cap_ac = site_config.get("P_CAP_AC", 10.0)
        self.p_cap_dc = site_config.get("P_CAP_DC", 12.5)

        self.mos_engine = DAMemberSelectionEngine()

    def resolve_dates(self) -> Tuple[datetime.date, datetime.date]:
        """
        Dynamically resolves current execution date in Asia/Kolkata timezone (D_today)
        and target Day-Ahead forecast date (D_target = D_today + 1 day).
        """
        tz_ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(tz_ist)
        d_today = now_ist.date()
        d_target = d_today + timedelta(days=1)
        return d_today, d_target

    def apply_physical_guardrails(
        self,
        raw_pred: np.ndarray,
        p_clearsky: np.ndarray,
        p_mos_derated: np.ndarray,
        synoptic_regime: str,
        p_avail: np.ndarray = None
    ) -> np.ndarray:
        """
        Screens Day-Ahead predictions against 4 physical guardrails.
        """
        p_final = np.copy(raw_pred)
        if p_avail is None:
            p_avail = np.full(96, self.p_cap_ac)

        # Guardrail 1: Solar Geometry Night Zeroing (Blocks 1-23 and 75-96)
        p_final[0:23] = 0.0
        p_final[74:96] = 0.0

        # Guardrail 2: Clear-Sky Floor Lock
        for b in range(23, 74):
            if synoptic_regime == "CLEAR_SKY":
                if p_final[b] < p_mos_derated[b]:
                    p_final[b] = p_mos_derated[b]

        # Guardrail 3: Overcast Optical Cloud Ceiling
        for b in range(23, 74):
            if synoptic_regime in ("MONSOON_OVERCAST", "RAIN"):
                max_ceiling = max(p_mos_derated[b] * 1.10, self.p_cap_ac * 0.25)
                if p_final[b] > max_ceiling:
                    p_final[b] = max_ceiling

        # Guardrail 4: Ramp Continuity & Capacity Clip
        max_ramp = max(0.50, self.p_cap_ac * 0.10)
        for b in range(24, 74):
            diff = p_final[b] - p_final[b-1]
            if abs(diff) > max_ramp:
                p_final[b] = p_final[b-1] + np.sign(diff) * max_ramp

        # Capacity Clip to Available Hardware Threshold
        p_final = np.clip(p_final, 0.0, p_avail)
        return p_final

    def run_day_ahead_pipeline(
        self,
        p_clearsky: np.ndarray,
        physics_base_matrix_da: np.ndarray, # (143, 96) for D_target
        physics_base_history: np.ndarray,   # (7, 143, 96) past 7 days
        scada_history_7d: np.ndarray,       # (7, 96) past 7 days actuals
        synoptic_indicators: Dict[str, Any],
        maintenance_outage_mw: float = 0.0
    ) -> Dict[str, Any]:
        """
        Executes end-to-end 10-module Day-Ahead forecast pipeline.
        Returns complete execution result dictionary containing final 96-block schedule.
        """
        # Module 1: Dynamic Date Resolution
        d_today, d_target = self.resolve_dates()
        target_date_str = d_target.strftime("%Y-%m-%d")

        # Module 5: 24h Lead-Time MOS Evaluation & Diurnal Selection
        mae_matrix = self.mos_engine.evaluate_24h_lead_time_mae(physics_base_history, scada_history_7d)
        p_mos_da, mos_audit = self.mos_engine.generate_fused_day_ahead_ensemble(physics_base_matrix_da, mae_matrix)

        # Module 6: Planned Maintenance Derate
        p_avail = np.full(96, max(0.0, self.p_cap_ac - maintenance_outage_mw))
        p_mos_derated = np.minimum(p_mos_da, p_avail)

        # Deterministic Meteorological Synoptic Regime Classification (No LLM Required)
        cloud_cover = float(synoptic_indicators.get("cloud_cover_pct", 25.0))
        precip = float(synoptic_indicators.get("precip_mm", 0.0))
        cape = float(synoptic_indicators.get("cape_j_kg", 0.0))
        if precip > 1.0 or cloud_cover >= 80.0:
            synoptic_regime = "MONSOON_OVERCAST"
        elif cloud_cover <= 25.0:
            synoptic_regime = "CLEAR_SKY"
        elif cape > 1000.0 or cloud_cover >= 60.0:
            synoptic_regime = "LOCAL_CONVECTIVE"
        else:
            synoptic_regime = "PARTLY_CLOUDY"

        # Module 7: Physical Guardrails & Schedule Arbitration (Directly applied to MOS Consensus)
        p_da_final = self.apply_physical_guardrails(
            raw_pred=p_mos_derated,
            p_clearsky=p_clearsky,
            p_mos_derated=p_mos_derated,
            synoptic_regime=synoptic_regime,
            p_avail=p_avail
        )

        # Module 8: Formatting & Canonical Regulatory CSV Assembly
        df_schedule = pd.DataFrame({
            "Block": range(1, 97),
            "Time Interval": [f"{(b-1)*15//60:02d}:{((b-1)*15)%60:02d}" for b in range(1, 97)],
            "clearsky_poa_w_m2": np.round(p_clearsky, 2),
            "mos_consensus_mw": np.round(p_mos_derated, 2),
            "da_schedule_mw": np.round(p_da_final, 2),
            "active_capacity_mw": np.round(p_avail, 2)
        })

        return {
            "site_id": self.site_id,
            "today_date": d_today.strftime("%Y-%m-%d"),
            "target_date": target_date_str,
            "synoptic_regime": synoptic_regime,
            "da_schedule_df": df_schedule,
            "da_schedule_mw": p_da_final.tolist(),
            "mos_audit": mos_audit,
            "source": "PHYSICS_MOS_CONSENSUS_DA"
        }


def generate_solar_day_ahead_schedule(
    plant_name: str,
    target_date_str: str,
    run_tag: str = "da0",
    s3_bucket: str = "vedanjay-schedules-test-608744602858",
    block_no: int | None = None,
) -> Dict[str, Any]:

    """
    Generates statutory 96-block Day-Ahead forecast for a solar plant using
    pure multi-agency NWP physics and 24h MOS consensus, then uploads to S3.
    """
    from modules.solar_schedule.solar_scheduler import SolarScheduleEngine, load_plant_profile
    import boto3

    prof = load_plant_profile(plant_name)
    ai_engine = SolarScheduleEngine(plant_profile=prof)

    # 1. 143-Member NWP Multi-Model Physics Forecast
    sched_result = ai_engine.predict_96block_schedule(target_date_str)
    cs_poa = ai_engine.compute_clearsky_poa_96block(target_date_str)
    p_mos = np.array([float(b.get("predicted_mw", b.get("schedule_mw", 0.0))) for b in sched_result["blocks"]])

    # 2. Atmospheric Indicators & Synoptic Regime
    weather = ai_engine.fetch_ensemble_weather(target_date_str)
    hourly = weather.get("hourly", {})
    cloud_vals = [float(v) for v in hourly.get("cloud_cover", []) if v is not None and not np.isnan(v)]
    mean_cloud = float(np.mean(cloud_vals[6:19])) if len(cloud_vals) >= 19 else (float(np.mean(cloud_vals)) if cloud_vals else 25.0)

    cape_vals = [float(v) for v in hourly.get("cape", []) if v is not None and not np.isnan(v)]
    max_cape = float(np.max(cape_vals)) if cape_vals else 0.0

    precip_vals = [float(v) for v in hourly.get("precipitation", []) if v is not None and not np.isnan(v)]
    tot_precip = float(np.sum(precip_vals)) if precip_vals else 0.0

    if tot_precip > 1.0 or mean_cloud >= 80.0:
        synoptic_regime = "MONSOON_OVERCAST"
    elif mean_cloud <= 25.0:
        synoptic_regime = "CLEAR_SKY"
    elif max_cape > 1000.0 or mean_cloud >= 60.0:
        synoptic_regime = "LOCAL_CONVECTIVE"
    else:
        synoptic_regime = "PARTLY_CLOUDY"

    # 3. Physical Guardrails Enforcement
    p_final = np.copy(p_mos)
    # Guardrail 1: Night Zeroing (Blocks 1-23 and Blocks 75-96)
    p_final[0:23] = 0.0
    p_final[74:96] = 0.0

    # Guardrail 2: Clear-sky floor lock
    if synoptic_regime == "CLEAR_SKY":
        for b in range(23, 74):
            p_final[b] = max(p_final[b], p_mos[b])

    # Guardrail 3: Overcast optical cloud ceiling
    if synoptic_regime == "MONSOON_OVERCAST":
        for b in range(23, 74):
            max_ceiling = max(p_mos[b] * 1.10, prof.ac_capacity_mw * 0.25)
            p_final[b] = min(p_final[b], max_ceiling)

    # Guardrail 4: Ramp continuity filter
    max_ramp = max(0.50, prof.ac_capacity_mw * 0.10)
    for b in range(24, 74):
        diff = p_final[b] - p_final[b - 1]
        if abs(diff) > max_ramp:
            p_final[b] = p_final[b - 1] + np.sign(diff) * max_ramp

    p_final = np.clip(np.round(p_final, 2), 0.0, prof.ac_capacity_mw)

    # 3b. Active Plant Control Windows Guardrail (DynamoDB)
    control_summary: Dict[str, Any] = {}
    try:
        from modules.control_windows import PlantControlWindowEngine
        control_engine = PlantControlWindowEngine()
        dc_cap = getattr(prof, "dc_capacity_mw", prof.ac_capacity_mw)
        p_final_controlled, block_audit, control_summary = control_engine.apply_to_blocks(
            raw_forecast_mw_96=p_final,
            site_id=plant_name,
            target_date_str=target_date_str,
            site_ac_capacity_mw=prof.ac_capacity_mw,
            site_dc_capacity_mw=dc_cap,
            freeze_end_block=0,
        )
    except Exception as cw_err:
        print(f"  [WARN] PlantControlWindowEngine failed for {plant_name}: {cw_err}")
        p_final_controlled = p_final
        block_audit = [
            {
                "Block": b + 1,
                "raw_forecast_mw": float(p_final[b]),
                "final_schedule_mw": float(p_final[b]),
                "block_control_status": "NORMAL",
                "block_control_mode": "NONE",
                "block_control_type": "NORMAL",
                "effective_control_capacity_ac_mw": prof.ac_capacity_mw,
                "control_applied": False,
            }
            for b in range(96)
        ]

    # 4. Canonical 96-Block Regulatory DataFrame
    time_intervals = [
        f"{(b * 15) // 60:02d}:{(b * 15) % 60:02d} - {((b + 1) * 15) // 60:02d}:00"
        if ((b + 1) * 15) % 60 == 0 and ((b + 1) * 15) // 60 == 24
        else f"{(b * 15) // 60:02d}:{(b * 15) % 60:02d} - {((b + 1) * 15) // 60:02d}:{((b + 1) * 15) % 60:02d}"
        for b in range(96)
    ]

    df_schedule = pd.DataFrame({
        "Block": range(1, 97),
        "Time Interval": time_intervals,
        "clearsky_poa_w_m2": np.round(cs_poa, 2),
        "mos_consensus_mw": np.round(p_mos, 2),
        "raw_forecast_mw": p_final,
        "da_schedule_mw": p_final_controlled,
        "active_capacity_mw": np.full(96, prof.ac_capacity_mw),
        "block_control_status": [a["block_control_status"] for a in block_audit],
        "block_control_mode": [a["block_control_mode"] for a in block_audit],
        "block_control_type": [a["block_control_type"] for a in block_audit],
        "effective_control_capacity_ac_mw": [a["effective_control_capacity_ac_mw"] for a in block_audit],
        "control_applied": [a["control_applied"] for a in block_audit],
    })

    # 5. Export and S3 Dispatch
    local_dir = Path("/tmp") / "day_ahead_solar"
    local_dir.mkdir(parents=True, exist_ok=True)

    lgepl_replica_key = None
    if plant_name == "LGEPL":
        # Exclusively upload LGEPL_DD-MM-YYYY_<BLOCK>_<TAG>.csv (no duplicate files)
        try:
            d_obj = datetime.strptime(target_date_str, "%Y-%m-%d")
            dd_mm_yyyy = d_obj.strftime("%d-%m-%Y")
        except Exception:
            dd_mm_yyyy = target_date_str

        tag_upper = run_tag.upper()  # e.g., DA0, DA1, DA2
        da_block_map = {
            "DA0": 18,  # Morning: 04:30 AM IST (Block 18)
            "DA1": 54,  # Afternoon: 01:30 PM IST (Block 54)
            "DA2": 90,  # Night: 10:30 PM IST (Block 90)
        }
        if block_no is not None:
            current_block = int(block_no)
        else:
            current_block = da_block_map.get(tag_upper)
            if current_block is None:
                tz_ist = pytz.timezone("Asia/Kolkata")
                now_ist = datetime.now(tz_ist)
                current_block = max(1, min(96, (now_ist.hour * 60 + now_ist.minute) // 15))

        filename = f"LGEPL_{dd_mm_yyyy}_{current_block}_{tag_upper}.csv"
        local_path = local_dir / filename
        df_schedule.to_csv(local_path, index=False)

        s3_key = f"intellis Dayhead solar/LGEPL/{target_date_str}/{filename}"
        s3_uri = f"s3://{s3_bucket}/{s3_key}"

        try:
            s3 = boto3.client("s3")
            s3.upload_file(str(local_path), s3_bucket, s3_key, ExtraArgs={"ContentType": "text/csv"})
            lgepl_replica_key = s3_key
            upload_success = True
            print(f"  [LGEPL] Uploaded Day-Ahead CSV -> {s3_uri}")
        except Exception as exc:
            print(f"  [WARN] S3 upload failed for LGEPL: {exc}")
            upload_success = False
    else:
        # Standard format for all other 20+ plants: PLANT_YYYY-MM-DD_da*.csv
        filename = f"{plant_name}_{target_date_str}_{run_tag}.csv"
        local_path = local_dir / filename
        df_schedule.to_csv(local_path, index=False)

        s3_key = f"intellis Dayhead solar/{plant_name}/{target_date_str}/{filename}"
        s3_uri = f"s3://{s3_bucket}/{s3_key}"

        try:
            s3 = boto3.client("s3")
            s3.upload_file(str(local_path), s3_bucket, s3_key, ExtraArgs={"ContentType": "text/csv"})
            upload_success = True
        except Exception as exc:
            print(f"  [WARN] S3 upload failed for {plant_name}: {exc}")
            upload_success = False


    return {
        "plant_name": plant_name,
        "target_date": target_date_str,
        "run_tag": run_tag,
        "synoptic_regime": synoptic_regime,
        "s3_uri": s3_uri,
        "lgepl_replica_key": lgepl_replica_key,
        "upload_success": upload_success,
        "local_csv_path": str(local_path),
        "total_daylight_mw": float(np.sum(p_final_controlled)),
        "control_windows": control_summary,
    }

