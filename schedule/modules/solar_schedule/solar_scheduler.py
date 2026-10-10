"""solar_scheduler.py

Master Solar Schedule Generation & Market Revision Pipeline Engine.

Responsibilities:
1. Ingests 96-block GTI from dedicated weather strategies (Meter NWP Ensemble vs Non-Meter Satellite).
2. Converts GTI to scheduled MW via Faiman/Sandia convective cell temperature derating.
3. Applies anti-plateau solar curvature guardrails (enforces non-identical consecutive blocks).
4. Calculates CERC / State DSM regulatory slabs & penalties.
5. Manages statutory revision gate closures (Madhya Pradesh 90-min lag / 6 blocks vs 45-min / 3 blocks).
6. Applies closed-loop intraday SCADA telemetry relaxation (T+4 nudges) and live POA feedback.
7. Integrates LLM strategic arbiter and publishes revision CSV schedules for S3.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import logging
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import config
except ImportError:
    config = None

from modules.plant.plant_profile import (
    PlantProfile,
    load_plant_profile,
    NON_METER_SITES,
    _fetch_enrich_live_capacities_from_dynamodb,
)
from modules.weather.strategies.intellis_gti import (
    get_gti_strategy,
    BaseGTIStrategy,
    GTIForecastResult,
)
from modules.weather.strategies.intellis_gti.base_gti_strategy import (
    compute_time_features,
)

try:
    from schedule.modules.meter.meter_normalizer import (
        CANONICAL_COLUMNS,
        load_and_normalize_meter_csv,
        normalize_meter_dataframe,
    )
except ImportError:
    from modules.meter.meter_normalizer import (
        CANONICAL_COLUMNS,
        load_and_normalize_meter_csv,
        normalize_meter_dataframe,
    )


def enforce_anti_plateau_curvature(mw_arr: np.ndarray, cs_poa: np.ndarray, min_step: float = 0.01) -> np.ndarray:
    """Ensures no consecutive daylight blocks (blocks 24 to 75, > 0.05 MW) have identical generation values.
    Shapes flat runs to strictly follow astronomical clear-sky solar curvature.
    """
    arr = np.copy(mw_arr)
    for _ in range(10):
        changed = False
        i = 23
        while i < 75:
            if arr[i] > 0.05:
                j = i
                while j + 1 < 76 and arr[j + 1] == arr[i] and arr[i] > 0.05:
                    j += 1
                if j > i:
                    changed = True
                    sub_cs = cs_poa[i : j + 1]
                    peak_local = int(np.argmax(sub_cs))
                    peak_idx = i + peak_local
                    for k in range(peak_idx - 1, i - 1, -1):
                        if arr[k] >= arr[k + 1]:
                            arr[k] = round(max(0.0, arr[k + 1] - min_step), 2)
                    for k in range(peak_idx + 1, j + 1):
                        if arr[k] >= arr[k - 1]:
                            arr[k] = round(max(0.0, arr[k - 1] - min_step), 2)
                    i = j + 1
                else:
                    i += 1
            else:
                i += 1
        if not changed:
            break

    for b in range(23, 75):
        if arr[b] > 0.05 and arr[b] == arr[b + 1]:
            if cs_poa[b + 1] >= cs_poa[b]:
                arr[b] = round(max(0.0, arr[b + 1] - min_step), 2)
            else:
                arr[b + 1] = round(max(0.0, arr[b] - min_step), 2)
    return arr


class SolarScheduleEngine:
    """Master Solar Schedule Generation & Market Revision Pipeline Engine."""

    def __init__(
        self,
        plant_profile: PlantProfile | str | None = None,
        plant_name: str = "GSNP",
        api_key: str | None = None,
        cache_dir: Path | None = None,
        use_api: bool | None = None,
    ):
        if isinstance(plant_profile, str):
            self.profile = load_plant_profile(plant_profile)
        elif plant_profile is not None:
            self.profile = plant_profile
        else:
            self.profile = load_plant_profile(plant_name)
        self.api_key = api_key or getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "jbThkFlLZSXZE3CU").strip()
        self.cache_dir = cache_dir or (Path("/tmp/openmeteo_premium_data") if (os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT")) else Path("openmeteo_premium_data"))
        self.tz = ZoneInfo("Asia/Kolkata")
        self.use_api = use_api

    def predict_gti(
        self,
        target_date_str: str,
        selected_keys: list[str] | None = None,
        weights_map: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> GTIForecastResult:
        """Compute 96-block GTI curve via dedicated strategy (Meter vs Non-Meter Satellite)."""
        strategy = get_gti_strategy(
            self.profile,
            use_api=self.use_api,
            api_key=self.api_key,
            cache_dir=self.cache_dir,
            engine_delegate=self,
        )
        return strategy.compute_gti(
            target_date_str=target_date_str,
            selected_keys=selected_keys,
            weights_map=weights_map,
            **kwargs,
        )

    def get_strategy(self) -> BaseGTIStrategy:
        return get_gti_strategy(
            self.profile,
            use_api=self.use_api,
            api_key=self.api_key,
            cache_dir=self.cache_dir,
            engine_delegate=self,
        )

    def fetch_ensemble_weather(self, target_date_str: str) -> dict[str, Any]:
        strat = self.get_strategy()
        if hasattr(strat, "fetch_ensemble_weather"):
            return strat.fetch_ensemble_weather(target_date_str)
        return {}

    def compute_clearsky_poa_96block(self, target_date_str: str) -> np.ndarray:
        return self.get_strategy().compute_clearsky_poa_96block(target_date_str)

    def extract_member_96block_gti(self, raw_weather: dict[str, Any], member_key: str, target_date_str: str) -> np.ndarray:
        strat = self.get_strategy()
        if hasattr(strat, "extract_member_96block_gti"):
            return strat.extract_member_96block_gti(raw_weather, member_key, target_date_str)
        return np.zeros(96, dtype=float)

    def get_cloud_and_atmospheric_metrics_96block(self, raw_weather: dict[str, Any]) -> dict[str, np.ndarray]:
        strat = self.get_strategy()
        if hasattr(strat, "get_cloud_and_atmospheric_metrics_96block"):
            return strat.get_cloud_and_atmospheric_metrics_96block(raw_weather)
        return {"tot_cloud_96": np.zeros(96), "low_cloud_96": np.zeros(96), "precip_96": np.zeros(96)}

    def benchmark_and_select_models(self, target_date_str: str, **kwargs: Any) -> Any:
        strat = self.get_strategy()
        if hasattr(strat, "benchmark_and_select_models"):
            return strat.benchmark_and_select_models(target_date_str, **kwargs)
        return [], {}, pd.DataFrame()

    def benchmark_and_select_slot_models(self, target_date_str: str, **kwargs: Any) -> dict[str, Any]:
        strat = self.get_strategy()
        if hasattr(strat, "benchmark_and_select_slot_models"):
            return strat.benchmark_and_select_slot_models(target_date_str, **kwargs)
        return {}

    def classify_weather_regime(self, *args: Any, **kwargs: Any) -> str:
        strat = self.get_strategy()
        if hasattr(strat, "classify_weather_regime"):
            return strat.classify_weather_regime(*args, **kwargs)
        return "MIXED"

    def get_slot_candidate_diagnostics(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        strat = self.get_strategy()
        if hasattr(strat, "get_slot_candidate_diagnostics"):
            return strat.get_slot_candidate_diagnostics(*args, **kwargs)
        return {}

    def load_meter_actuals(self, target_date_str: str) -> np.ndarray:
        """Load 96-block generation (MW) from SCADA boundary meter files or virtual meter fallback."""
        mw, _ = self.load_meter_actuals_with_poa(target_date_str)
        return mw

    def load_meter_actuals_with_poa(self, target_date_str: str) -> tuple[np.ndarray, np.ndarray]:
        """Load 96-block active generation (MW) and POA sensor irradiance (W/m²)."""
        strat = self.get_strategy()
        if hasattr(strat, "load_meter_actuals_with_poa"):
            return strat.load_meter_actuals_with_poa(target_date_str)
        return np.zeros(96, dtype=float), np.zeros(96, dtype=float)

    def calibrate_plant_pr(self, target_date_str: str, lookback_days: int = 5) -> float:
        """Dynamically learn plant-specific Performance Ratio (PR) from historical telemetry."""
        strat = self.get_strategy()
        if hasattr(strat, "calibrate_plant_pr"):
            pr = strat.calibrate_plant_pr(target_date_str, lookback_days=lookback_days)
            self.profile.calibrated_pr = strat.profile.calibrated_pr
            self.profile.transfer_ratio = strat.profile.transfer_ratio
            return pr
        return getattr(self.profile, "calibrated_pr", 0.78)

    def predict_96block_schedule(
        self,
        target_date_str: str,
        selected_keys: list[str] | None = None,
        weights_map: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Generate 96-block GTI and scheduled MW using dedicated strategy engines."""
        if getattr(self.profile, "calibrated_pr", None) is None:
            self.calibrate_plant_pr(target_date_str)

        site_upper = (self.profile.plant_name or "").upper()
        is_non_meter = (
            site_upper in NON_METER_SITES
            or bool(self.profile.meter_data.get("is_non_meter_site", False))
            or bool(self.profile.meter_data.get("is_virtual", False))
        )
        meter_mw = self.load_meter_actuals(target_date_str)

        gti_strategy = get_gti_strategy(
            self.profile,
            use_api=self.use_api,
            api_key=self.api_key,
            cache_dir=self.cache_dir,
            engine_delegate=self,
        )
        gti_result = gti_strategy.compute_gti(
            target_date_str=target_date_str,
            selected_keys=selected_keys,
            weights_map=weights_map,
        )
        fused_gti = gti_result.gti_96
        cs_poa = gti_result.cs_poa_96
        amb_temp = gti_result.amb_temp_96
        wind_speed = gti_result.wind_speed_96

        if not selected_keys and "selected_keys" in gti_result.metadata:
            selected_keys = gti_result.metadata["selected_keys"]
        if not weights_map and "weights_map" in gti_result.metadata:
            weights_map = gti_result.metadata["weights_map"]

        cell_temp = amb_temp + fused_gti / (25.0 + 1.2 * wind_speed)
        temp_factor = np.clip(1.0 - 0.0038 * (cell_temp - 25.0), 0.82, 1.06)

        if is_non_meter:
            target_pr = getattr(self.profile, "calibrated_pr", 0.8300) or 0.8300
            base_bop = target_pr / 0.905
            dc_cap = getattr(self.profile, "dc_capacity_mw", None) or self.profile.ac_capacity_mw
            tr_dynamic = (dc_cap * base_bop) / 1000.0
            pred_mw = np.round(np.clip(fused_gti * tr_dynamic * temp_factor, 0.0, self.profile.ac_capacity_mw), 2)
        else:
            pred_mw = np.round(np.clip(fused_gti * self.profile.transfer_ratio * temp_factor, 0.0, self.profile.ac_capacity_mw), 2)

        pred_mw[:23] = 0.0
        pred_mw[76:] = 0.0
        pred_mw = enforce_anti_plateau_curvature(pred_mw, cs_poa)

        blocks_data = []
        tot_pen = 0.0
        safe_count = 0
        has_actuals = np.max(meter_mw) > 0.1

        for b in range(96):
            end_minutes = (b + 1) * 15
            start_minutes = b * 15
            s_hr, s_min = divmod(start_minutes, 60)
            e_hr, e_min = divmod(end_minutes, 60)
            t_str = "00:00" if e_hr == 24 else f"{e_hr:02d}:{e_min:02d}"
            t_interval = f"{s_hr:02d}:{s_min:02d} - {t_str if t_str != '00:00' else '24:00'}"

            m_val = float(meter_mw[b])
            p_val = float(pred_mw[b])
            gti_val = float(fused_gti[b])
            dev = round(m_val - p_val, 2)
            abs_dev = abs(dev)

            slab = "0% Safe"
            blk_pen = 0.0
            if has_actuals:
                if abs_dev <= self.profile.tolerance_band_mw:
                    safe_count += 1
                elif abs_dev <= (self.profile.tolerance_band_mw * 2.0):
                    slab = "10% Slab"
                    blk_pen = (abs_dev - self.profile.tolerance_band_mw) * 250.0 * 0.10 * self.profile.ppa_rate_inr_per_kwh
                elif abs_dev <= (self.profile.tolerance_band_mw * 3.0):
                    slab = "20% Slab"
                    blk_pen = (self.profile.tolerance_band_mw * 250.0 * 0.10 * self.profile.ppa_rate_inr_per_kwh) + \
                              ((abs_dev - self.profile.tolerance_band_mw * 2.0) * 250.0 * 0.20 * self.profile.ppa_rate_inr_per_kwh)
                else:
                    slab = ">30% Slab"
                    blk_pen = (self.profile.tolerance_band_mw * 250.0 * 0.10 * self.profile.ppa_rate_inr_per_kwh) + \
                              (self.profile.tolerance_band_mw * 250.0 * 0.20 * self.profile.ppa_rate_inr_per_kwh) + \
                              ((abs_dev - self.profile.tolerance_band_mw * 3.0) * 250.0 * 0.30 * self.profile.ppa_rate_inr_per_kwh)

            tot_pen += blk_pen
            blocks_data.append({
                "block": b + 1,
                "time": t_str,
                "time_interval": t_interval,
                "intellis_gti": gti_val,
                "intellis_mw": p_val,
                "predicted_gti_wm2": gti_val,
                "predicted_mw": p_val,
                "schedule_mw": p_val,
                "meter_mw": m_val,
                "dev_mw": dev,
                "dsm_slab": slab,
                "block_penalty_inr": round(blk_pen, 2),
                "cumulative_penalty_inr": round(tot_pen, 2),
            })

        daylight_devs = [abs(b["dev_mw"]) for b in blocks_data if 24 <= b["block"] <= 76]
        day_mae = round(float(np.mean(daylight_devs)), 3) if daylight_devs else 0.0

        return {
            "plant_name": self.profile.plant_name,
            "target_date": target_date_str,
            "selected_models": selected_keys,
            "model_weights": weights_map,
            "daylight_mae_mw": day_mae,
            "safe_blocks": safe_count if has_actuals else None,
            "total_dsm_penalty_inr": round(tot_pen, 2) if has_actuals else None,
            "blocks": blocks_data,
        }

    def apply_intraday_scada_feedback(
        self,
        schedule_result: dict[str, Any],
        current_block: int,
        live_meter_mw: float,
        tau_blocks: float = 3.5,
        recent_meter_mw_list: list[float] | None = None,
        live_pyranometer_poa: float | None = None,
    ) -> dict[str, Any]:
        """Closed-loop intraday SCADA telemetry relaxation (T+4 Nudge)."""
        target_date_str = schedule_result["target_date"]
        cs_poa = get_gti_strategy(self.profile, use_api=self.use_api, api_key=self.api_key, cache_dir=self.cache_dir).compute_clearsky_poa_96block(target_date_str)

        curr_idx = current_block - 1
        cs_theoretical = cs_poa[curr_idx] * self.profile.transfer_ratio if 0 <= curr_idx < 96 else 0.0
        cs_curr_mw = min(self.profile.ac_capacity_mw, cs_theoretical)

        site_upper = self.profile.plant_name.upper()
        is_90min_site = (
            self.profile.penalty_regulation == "Madhya Pradesh"
            or site_upper in {
                "SIRMOUR", "ANJANGOAN", "ANJANGAON", "ANDAD", "BALAKWADA",
                "BAMKHAL", "CHANDAWASA", "CHANDWASA", "GSNP", "GSPPL", "GUGARIYAKHEDI", "NANDGAON", "SAWDA", "REWASPRNG", "REWASEIT"
            }
        )
        freeze_lag_blocks = 6 if is_90min_site else 3
        actionable_block = max(1, current_block + freeze_lag_blocks + 1)

        kt_poa = None
        if live_pyranometer_poa is not None and live_pyranometer_poa > 5.0 and cs_poa[curr_idx] > 10.0:
            kt_poa = max(0.15, min(1.05, live_pyranometer_poa / cs_poa[curr_idx]))

        morning_guardrail_limit = 30 if is_90min_site else 28
        if current_block < morning_guardrail_limit or cs_curr_mw < (0.05 * self.profile.ac_capacity_mw):
            kt_obs = kt_poa if (kt_poa is not None and current_block >= 25) else 0.85
        elif live_meter_mw >= (0.88 * self.profile.ac_capacity_mw):
            kt_obs = 1.00
        elif cs_curr_mw > 0.3:
            kt_from_meter = max(0.15, min(1.05, live_meter_mw / cs_curr_mw))
            kt_obs = (0.75 * kt_poa + 0.25 * kt_from_meter) if kt_poa is not None else kt_from_meter
        else:
            kt_obs = kt_poa if kt_poa is not None else (0.85 if live_meter_mw > 0.2 else 0.40)

        if recent_meter_mw_list and len(recent_meter_mw_list) >= 2:
            kts = []
            lookback_blocks = min(len(recent_meter_mw_list), 4)
            for offset in range(lookback_blocks):
                hist_idx = curr_idx - offset
                hist_mw = recent_meter_mw_list[-1 - offset]
                if hist_idx >= 0:
                    hist_cs = min(self.profile.ac_capacity_mw, cs_poa[hist_idx] * self.profile.transfer_ratio)
                    if hist_cs > 0.3:
                        kts.append(1.00 if hist_mw >= (0.88 * self.profile.ac_capacity_mw) else max(0.15, min(1.05, hist_mw / hist_cs)))
            if kts:
                kt_obs = float(np.median(kts))

        trend_momentum = 0.0
        if recent_meter_mw_list and len(recent_meter_mw_list) >= 2 and cs_curr_mw > 0.3:
            prev_idx = max(0, curr_idx - 1)
            prev_cs = cs_poa[prev_idx] * self.profile.transfer_ratio
            if prev_cs > 0.3:
                prev_kt = recent_meter_mw_list[-2] / prev_cs
                curr_kt = live_meter_mw / cs_curr_mw
                trend_momentum = max(-0.25, min(0.20, (curr_kt - prev_kt) * 1.5))

        updated_blocks = []
        tot_pen = 0.0
        safe_count = 0

        for b_dict in schedule_result["blocks"]:
            b_idx = b_dict["block"] - 1
            if b_idx < (actionable_block - 1):
                b_copy = dict(b_dict)
                updated_blocks.append(b_copy)
                if b_copy.get("dsm_slab") == "0% Safe":
                    safe_count += 1
                tot_pen += b_copy.get("block_penalty_inr", 0.0)
            else:
                elapsed_blocks = b_idx - curr_idx
                fcst_gti = b_dict["predicted_gti_wm2"]
                fcst_kt = min(1.05, fcst_gti / max(15.0, cs_poa[b_idx]))

                if elapsed_blocks <= 1:
                    eff_kt = max(0.15, min(1.05, kt_obs + trend_momentum))
                elif elapsed_blocks == 2:
                    eff_kt = max(0.15, min(1.05, kt_obs + trend_momentum * 0.50))
                elif elapsed_blocks == 3:
                    eff_kt = max(0.15, min(1.05, 0.50 * kt_obs + 0.50 * fcst_kt))
                else:
                    eff_kt = fcst_kt

                target_poa = fcst_gti if elapsed_blocks >= 4 else (eff_kt * cs_poa[b_idx])
                b_hr = (b_dict["block"] * 15) // 60
                amb_t = 28.0 + (4.0 if 11 <= b_hr <= 15 else 0.0)
                cell_t = amb_t + target_poa * 0.031
                temp_factor = np.clip(1.0 - 0.004 * (cell_t - 25.0), 0.82, 1.06)
                adj_mw = b_dict["predicted_mw"] if elapsed_blocks >= 4 else round(min(self.profile.ac_capacity_mw, max(0.0, target_poa * self.profile.transfer_ratio * temp_factor)), 2)

                if b_dict["block"] < 24 or b_dict["block"] > 76:
                    adj_mw = 0.0
                    target_poa = 0.0

                if updated_blocks:
                    prev_adj = updated_blocks[-1]["intellis_mw"]
                    max_delta = max(prev_adj - adj_mw, max(0.50, self.profile.ac_capacity_mw * 0.10)) if (b_idx == (actionable_block - 1) and adj_mw < prev_adj and kt_obs < 0.65) else max(0.50, self.profile.ac_capacity_mw * 0.10)
                    if abs(adj_mw - prev_adj) > max_delta:
                        adj_mw = round(prev_adj + (max_delta if adj_mw > prev_adj else -max_delta), 2)
                        adj_mw = min(self.profile.ac_capacity_mw, max(0.0, adj_mw))

                if b_dict["block"] < 24 or b_dict["block"] > 76:
                    adj_mw = 0.0
                    target_poa = 0.0

                m_val = b_dict["meter_mw"]
                dev = round(m_val - adj_mw, 2)
                abs_dev = abs(dev)

                slab = "0% Safe"
                blk_pen = 0.0
                if abs_dev <= self.profile.tolerance_band_mw:
                    safe_count += 1
                elif abs_dev <= (self.profile.tolerance_band_mw * 2.0):
                    slab = "10% Slab"
                    blk_pen = (abs_dev - self.profile.tolerance_band_mw) * 250.0 * 0.10 * self.profile.ppa_rate_inr_per_kwh
                elif abs_dev <= (self.profile.tolerance_band_mw * 3.0):
                    slab = "20% Slab"
                    blk_pen = (self.profile.tolerance_band_mw * 250.0 * 0.10 * self.profile.ppa_rate_inr_per_kwh) + \
                              ((abs_dev - self.profile.tolerance_band_mw * 2.0) * 250.0 * 0.20 * self.profile.ppa_rate_inr_per_kwh)
                else:
                    slab = ">30% Slab"
                    blk_pen = (self.profile.tolerance_band_mw * 250.0 * 0.10 * self.profile.ppa_rate_inr_per_kwh) + \
                              (self.profile.tolerance_band_mw * 250.0 * 0.20 * self.profile.ppa_rate_inr_per_kwh) + \
                              ((abs_dev - self.profile.tolerance_band_mw * 3.0) * 250.0 * 0.30 * self.profile.ppa_rate_inr_per_kwh)

                tot_pen += blk_pen
                b_copy = dict(b_dict)
                b_copy["predicted_mw"] = adj_mw
                b_copy["intellis_mw"] = adj_mw
                b_copy["schedule_mw"] = adj_mw
                b_copy["intellis_gti"] = round(target_poa, 1)
                b_copy["predicted_gti_wm2"] = round(target_poa, 1)
                b_copy["dev_mw"] = dev
                b_copy["dsm_slab"] = slab
                b_copy["block_penalty_inr"] = round(blk_pen, 2)
                b_copy["cumulative_penalty_inr"] = round(tot_pen, 2)
                updated_blocks.append(b_copy)

        res = dict(schedule_result)
        res["blocks"] = updated_blocks
        res["safe_blocks"] = safe_count
        res["total_dsm_penalty_inr"] = round(tot_pen, 2)
        daylight_devs = [abs(b["dev_mw"]) for b in updated_blocks if 24 <= b["block"] <= 76]
        res["daylight_mae_mw"] = round(float(np.mean(daylight_devs)), 3) if daylight_devs else 0.0
        return res

    def generate_revision_schedule_csv(
        self,
        target_date_str: str,
        target_time_str: str,
        output_csv_path: Path,
        live_meter_csv_path: Path | None = None,
        enercast_intraday_csv_path: Path | None = None,
    ) -> dict[str, Any]:
        """Generate revision schedule CSV directly with intellis_gti and intellis_mw."""
        strategy = get_gti_strategy(self.profile, use_api=self.use_api, api_key=self.api_key, cache_dir=self.cache_dir)
        gti_res = strategy.compute_gti(target_date_str)
        cs_poa = gti_res.cs_poa_96

        sched = self.predict_96block_schedule(target_date_str)

        t_hr, t_min = [int(p) for p in target_time_str.split(":")[:2]]
        curr_block = ((t_hr * 60 + t_min) // 15)
        curr_idx = curr_block - 1

        site_upper = (self.profile.plant_name or "").upper()
        is_non_meter_site = (
            site_upper in NON_METER_SITES
            or bool(self.profile.meter_data.get("is_non_meter_site", False))
            or bool(self.profile.meter_data.get("is_virtual", False))
        )
        is_90min_site = (
            self.profile.penalty_regulation == "Madhya Pradesh"
            or site_upper in {
                "SIRMOUR", "ANJANGOAN", "ANJANGAON", "ANDAD", "BALAKWADA",
                "BAMKHAL", "CHANDAWASA", "CHANDWASA", "GSNP", "GSPPL", "GUGARIYAKHEDI", "NANDGAON", "SAWDA", "REWASPRNG", "REWASEIT"
            }
        )
        freeze_lag_blocks = 6 if is_90min_site else 3
        actionable_block = max(1, curr_block + freeze_lag_blocks + 1)

        live_mw = 0.0
        recent_list: list[float] = []
        has_live_scada = False
        live_poa: float | None = None

        if live_meter_csv_path and Path(live_meter_csv_path).exists() and not is_non_meter_site:
            try:
                norm_meter = load_and_normalize_meter_csv(Path(live_meter_csv_path), meter_config=self.profile.meter_data)
                day_meter = norm_meter[norm_meter["date"] == target_date_str]
                if not day_meter.empty:
                    prior_rows = day_meter[day_meter["block"] <= curr_block].sort_values("block")
                    vals = [float(v) for v in prior_rows["metered_mw"] if v is not None and not np.isnan(v)]
                    if vals and max(vals) > (0.02 * self.profile.ac_capacity_mw):
                        live_mw = float(vals[-1])
                        recent_list = vals
                        has_live_scada = True
                    if "poa_wm2" in prior_rows.columns:
                        poa_vals = [float(v) for v in prior_rows["poa_wm2"] if v is not None and not np.isnan(v)]
                        if poa_vals and poa_vals[-1] > 5.0:
                            live_poa = float(poa_vals[-1])
            except Exception as exc:
                print(f"  [WARN] Live meter load failed: {exc}")

        telemetry_source = "PHYSICAL_SCADA" if has_live_scada else "SATELLITE_VIRTUAL_METER"
        if not has_live_scada:
            try:
                from modules.weather.strategies.intellis_gti.non_meter_gti_strategy import fetch_satellite_96block_profile
                target_dt = dt.datetime.strptime(f"{target_date_str} {target_time_str}", "%Y-%m-%d %H:%M")
                virt_mw, _ = fetch_satellite_96block_profile(
                    target_date=target_date_str,
                    latitude=self.profile.latitude,
                    longitude=self.profile.longitude,
                    tilt=self.profile.tilt_deg,
                    azimuth=self.profile.azimuth_openmeteo,
                    plant_capacity_mw=self.profile.ac_capacity_mw,
                    dc_capacity_mw=self.profile.ac_capacity_mw if is_non_meter_site else self.profile.dc_capacity_mw,
                    performance_ratio=getattr(self.profile, "calibrated_pr", 0.8300 if is_non_meter_site else 0.78),
                    cutoff_time=target_dt,
                    is_non_meter=is_non_meter_site,
                )
                if curr_block >= 1 and np.max(virt_mw[:curr_block]) > (0.02 * self.profile.ac_capacity_mw):
                    live_mw = float(virt_mw[curr_block - 1])
                    recent_list = virt_mw[:curr_block].tolist()
            except Exception as exc:
                print(f"  [WARN] Intraday satellite virtual feedback failed: {exc}")

        try:
            t_dt = dt.datetime.strptime(f"{target_date_str} {target_time_str}", "%Y-%m-%d %H:%M")
            ref_elev = compute_time_features(t_dt, self.profile.latitude, self.profile.longitude)["solar_elevation_deg"]
        except Exception:
            ref_elev = 30.0

        cs_curr_mw = min(self.profile.ac_capacity_mw, cs_poa[curr_idx] * self.profile.transfer_ratio) if 0 <= curr_idx < 96 else 0.0
        real_clearness_ratio = round(min(1.25, max(0.0, live_mw / cs_curr_mw)), 2) if cs_curr_mw > 0.3 else 1.0

        physics_curr_mw = round(float(sched["blocks"][curr_idx].get("predicted_mw", 0.0)), 2) if 0 <= curr_idx < 96 else 0.0
        residual_mw = round(live_mw - physics_curr_mw, 2)
        if live_mw <= 0.05 and physics_curr_mw > 0.3 and ref_elev >= 20.0:
            live_mw = physics_curr_mw
            residual_mw = 0.0
            real_clearness_ratio = 1.0

        telemetry_ind = {
            "latest_mw": round(live_mw, 2),
            "physics_predicted_mw": physics_curr_mw,
            "residual_mw": residual_mw,
            "solar_elevation_deg": round(ref_elev, 1),
            "clearness_ratio": real_clearness_ratio,
            "is_non_meter_site": is_non_meter_site or not has_live_scada,
            "telemetry_source": telemetry_source,
            "live_poa_wm2": round(live_poa, 1) if live_poa is not None else None,
        }

        if has_live_scada and not is_non_meter_site:
            try:
                sched = self.apply_intraday_scada_feedback(
                    sched,
                    current_block=curr_block,
                    live_meter_mw=live_mw,
                    recent_meter_mw_list=recent_list,
                    live_pyranometer_poa=live_poa,
                )
            except Exception as exc:
                print(f"  [WARN] Intraday SCADA feedback failed: {exc}")

        solar_actionable_blocks = []
        for b in sched["blocks"]:
            if actionable_block <= b["block"] <= 76 and b["block"] >= 24:
                solar_actionable_blocks.append({
                    "block": b["block"],
                    "time_interval": b["time_interval"],
                    "predicted_mw": b["schedule_mw"],
                    "gti_wm2": b["intellis_gti"],
                })

        mean_cloud = float(gti_res.metadata.get("cloud_cover_daily_avg", 15.0))
        max_cape = 0.0
        tot_precip = 0.0
        mean_temp = float(np.mean(gti_res.amb_temp_96[24:76])) if len(gti_res.amb_temp_96) >= 76 else 28.0

        try:
            if hasattr(strategy, "fetch_ensemble_weather"):
                raw_weather = strategy.fetch_ensemble_weather(target_date_str)
                hourly = raw_weather.get("hourly", {}) if isinstance(raw_weather, dict) else {}
                if hourly:
                    cloud_vals = []
                    for k, v_list in hourly.items():
                        if k.startswith("cloud_cover") and isinstance(v_list, list):
                            cloud_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
                    if cloud_vals:
                        mean_cloud = round(float(np.mean(cloud_vals)), 1)

                    cape_vals = []
                    for k, v_list in hourly.items():
                        if (k == "cape" or k.startswith("cape_")) and isinstance(v_list, list):
                            cape_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
                    if cape_vals:
                        max_cape = round(float(np.max(cape_vals)), 1)

                    precip_vals = []
                    for k, v_list in hourly.items():
                        if (k == "precipitation" or k.startswith("precipitation_")) and isinstance(v_list, list):
                            precip_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
                    if precip_vals:
                        tot_precip = round(float(np.sum(precip_vals)), 2)

                    temp_vals = []
                    for k, v_list in hourly.items():
                        if (k == "temperature_2m" or k.startswith("temperature_2m_")) and isinstance(v_list, list):
                            temp_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
                    if temp_vals:
                        mean_temp = round(float(np.mean(temp_vals)), 1)
        except Exception as w_err:
            logger.debug("Could not extract extended atmospheric indicators from NWP ensemble: %s", w_err)

        weather_ind = {
            "cloud_cover_pct": mean_cloud,
            "cape_j_kg": max_cape,
            "precip_mm": tot_precip,
            "temp_c": mean_temp,
        }

        if not solar_actionable_blocks:
            from modules.llm.strategic_arbiter import StrategicAdvice
            advice = StrategicAdvice(
                regime="NIGHT",
                quantile_bias_factor=1.0,
                preferred_agency="PHYSICS_BASELINE",
                is_trip_or_curtailment=False,
                reasoning="Night hours (no actionable daylight blocks remaining up to 7:00 PM).",
                block_predictions={},
                source="NIGHT_PHYSICS_BASELINE",
            )
        else:
            try:
                from modules.llm.strategic_arbiter import LLMStrategicArbiter
                arbiter = LLMStrategicArbiter(plant_profile=self.profile)
                advice = arbiter.get_strategic_guidance(
                    target_date_str=target_date_str,
                    target_time_str=target_time_str,
                    weather_indicators=weather_ind,
                    live_telemetry=telemetry_ind,
                    actionable_blocks=solar_actionable_blocks,
                    next_12_blocks=solar_actionable_blocks,
                )
                print(f"  [LLM STRATEGY] Regime: {advice.regime} | Risk Quantile: {advice.quantile_bias_factor:.3f} | Agency: {advice.preferred_agency} | Trip Flag: {advice.is_trip_or_curtailment}")
                print(f"                 Reasoning: {advice.reasoning}")
            except Exception as arb_err:
                from modules.llm.strategic_arbiter import StrategicAdvice
                advice = StrategicAdvice(
                    regime="CLEAR_SKY",
                    quantile_bias_factor=1.0,
                    preferred_agency="SATELLITE_PHYSICS" if is_non_meter_site else "BALANCED",
                    is_trip_or_curtailment=False,
                    reasoning=f"Deterministic fallback physics mode ({arb_err})",
                    block_predictions={},
                    source="SATELLITE_PHYSICS" if is_non_meter_site else "PHYSICS_BASELINE",
                )

        advice.is_trip_or_curtailment = False
        is_clear_sky = real_clearness_ratio >= 0.85 or advice.regime in ("CLEAR", "CLEAR_SKY")
        is_overcast = advice.regime in ("OVERCAST", "RAIN", "STORMY") or real_clearness_ratio < 0.50

        if advice.block_predictions:
            print(f"  [LLM SOLAR HORIZON PREDICTIONS] Applying direct LLM predictions up to 7 PM to {len(advice.block_predictions)} blocks...")
            prev_val = None
            max_llm_block = max((int(k) for k in advice.block_predictions.keys() if str(k).strip().isdigit()), default=76)
            for b in sched["blocks"]:
                b_str = str(b["block"])
                if b["block"] >= actionable_block:
                    if b_str in advice.block_predictions:
                        pred_mw = float(advice.block_predictions[b_str])
                        pred_mw = max(0.0, min(self.profile.ac_capacity_mw, pred_mw))
                        physics_base_mw = float(b["schedule_mw"])

                        if b["block"] > 74 or b["block"] < 24:
                            pred_mw = 0.0
                        if is_clear_sky and pred_mw < physics_base_mw:
                            pred_mw = physics_base_mw
                        if is_overcast:
                            overcast_max = max(physics_base_mw * 1.10, self.profile.ac_capacity_mw * 0.25)
                            if pred_mw > overcast_max:
                                pred_mw = overcast_max

                        if prev_val is not None and 24 <= b["block"] <= 74:
                            if is_clear_sky and pred_mw >= physics_base_mw and not advice.is_trip_or_curtailment:
                                pred_mw = max(pred_mw, physics_base_mw)
                            else:
                                max_ramp = max(0.50, self.profile.ac_capacity_mw * 0.10)
                                if abs(pred_mw - prev_val) > max_ramp:
                                    pred_mw = prev_val + (max_ramp if pred_mw > prev_val else -max_ramp)
                                if b["block"] >= 50 and not is_clear_sky and pred_mw > prev_val + 0.05:
                                    pred_mw = prev_val

                        pred_mw = round(pred_mw, 2)
                        b["predicted_mw"] = pred_mw
                        b["intellis_mw"] = pred_mw
                        b["schedule_mw"] = pred_mw
                        if self.profile.transfer_ratio > 0:
                            b["intellis_gti"] = round(pred_mw / self.profile.transfer_ratio, 1)
                            b["predicted_gti_wm2"] = b["intellis_gti"]
                    elif b["block"] > max_llm_block and b["block"] <= 76:
                        raw_gti_mw = float(b.get("predicted_mw", b.get("intellis_mw", 0.0)))
                        raw_gti_mw = max(0.0, min(self.profile.ac_capacity_mw, raw_gti_mw))
                        if b["block"] > 72:
                            raw_gti_mw = min(raw_gti_mw, max(0.0, round((76 - b["block"]) * 0.03, 2)))
                        b["schedule_mw"] = raw_gti_mw
                        b["intellis_mw"] = raw_gti_mw
                        b["predicted_mw"] = raw_gti_mw
                    elif b["block"] > 76 or b["block"] < 24:
                        b["schedule_mw"] = 0.0
                        b["intellis_mw"] = 0.0
                        b["intellis_gti"] = 0.0
                        b["predicted_gti_wm2"] = 0.0
                prev_val = float(b["schedule_mw"])
        elif abs(advice.quantile_bias_factor - 1.0) > 0.005:
            q_factor = advice.quantile_bias_factor
            if is_clear_sky and not advice.is_trip_or_curtailment:
                q_factor = max(1.0, q_factor)
            elif is_overcast and not advice.is_trip_or_curtailment:
                q_factor = min(1.02, q_factor)

            for b in sched["blocks"]:
                if b["block"] >= actionable_block and 24 <= b["block"] <= 76:
                    adj_mw = round(min(self.profile.ac_capacity_mw, max(0.0, float(b["schedule_mw"]) * q_factor)), 2)
                    b["predicted_mw"] = adj_mw
                    b["intellis_mw"] = adj_mw
                    b["schedule_mw"] = adj_mw
                    if self.profile.transfer_ratio > 0:
                        b["intellis_gti"] = round(adj_mw / self.profile.transfer_ratio, 1)
                        b["predicted_gti_wm2"] = b["intellis_gti"]

        sched["llm_strategy"] = {
            "regime": advice.regime,
            "quantile_bias_factor": advice.quantile_bias_factor,
            "preferred_agency": advice.preferred_agency,
            "is_trip_or_curtailment": advice.is_trip_or_curtailment,
            "reasoning": advice.reasoning,
            "source": advice.source,
        }

        mw_vals = np.array([float(b.get("schedule_mw", 0.0)) for b in sched["blocks"]])
        fixed_vals = enforce_anti_plateau_curvature(mw_vals, cs_poa)
        for idx, b in enumerate(sched["blocks"]):
            b["schedule_mw"] = fixed_vals[idx]
            b["intellis_mw"] = fixed_vals[idx]
            b["predicted_mw"] = fixed_vals[idx]
            if self.profile.transfer_ratio > 0 and fixed_vals[idx] > 0:
                b["intellis_gti"] = round(fixed_vals[idx] / self.profile.transfer_ratio, 1)
                b["predicted_gti_wm2"] = b["intellis_gti"]

        output_csv_path = Path(output_csv_path)
        output_csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = ["Block", "Time Interval (15 minute interval)", "intellis_gti", "intellis_mw", "schedule_mw"]
        with open(output_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for b in sched["blocks"]:
                writer.writerow({
                    "Block": b["block"],
                    "Time Interval (15 minute interval)": b["time_interval"],
                    "intellis_gti": b["intellis_gti"],
                    "intellis_mw": b["intellis_mw"],
                    "schedule_mw": b["intellis_mw"],
                })

        return sched


def predict_plant_gti_and_power(
    plant_name: str = "GSNP",
    target_date_str: str | None = None,
) -> dict[str, Any]:
    """Convenience functional interface for SolarScheduleEngine."""
    if not target_date_str:
        target_date_str = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    engine = SolarScheduleEngine(plant_name=plant_name)
    return engine.predict_96block_schedule(target_date_str)


# Alias for backward compatibility
IntellisEnsembleGTIAI = SolarScheduleEngine

__all__ = [
    "SolarScheduleEngine",
    "IntellisEnsembleGTIAI",
    "enforce_anti_plateau_curvature",
    "predict_plant_gti_and_power",
    "PlantProfile",
    "load_plant_profile",
    "NON_METER_SITES",
    "_fetch_enrich_live_capacities_from_dynamodb",
]
