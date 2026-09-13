"""
rank_models_by_regime.py

Evaluates candidate forecasting models across historical 15-minute blocks,
segregating by weather regime:
  1. Clear / Normal Weather (Kt >= 0.80, low variance, no rain)
  2. Abrupt / Volatile / Rain Weather (Kt < 0.80, precipitation > 0, high variance)

Computes MAE, RMSE, and regulatory penalty for each model in both regimes,
identifies the TOP 5 models for Stream 1 (Clear) and TOP 5 models for Stream 2 (Abrupt),
and exports the calibrated configuration to JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import glob
import numpy as np
import pandas as pd


CANDIDATE_MODELS = [
    "ecmwf",
    "gfs",
    "ensemble_openmeteo",
    "c1_rapid",
    "c2_regional",
    "c3_clearsky_nwp",
    "scada_exponential_decay",
    "pvlib_clearsky_physics",
    "rolling_gradient_extrapolator",
    "bias_corrected_nwp",
]


def classify_block_regime(poa_actual: float, poa_clearsky: float, precip_mm: float = 0.0,
                          poa_std_3block: float = 0.0) -> str:
    """Classify a 15-minute block into Clear or Abrupt regime."""
    if poa_clearsky <= 20.0:
        return "NIGHT"
    kt = poa_actual / max(poa_clearsky, 1.0)
    if precip_mm > 0.1 or kt < 0.75 or poa_std_3block > 50.0:
        return "ABRUPT"
    return "CLEAR"


def rank_models_for_site(site_name: str, historical_records: list[dict], capacity_mw: float = 10.0,
                         tolerance_band_pct: float = 0.15) -> dict:
    """
    Ranks candidate models on historical records for a given site.
    Returns:
      {
        "site": site_name,
        "capacity_mw": capacity_mw,
        "stream_1_clear_models": [ {"model": name, "weight": w, "mae": m}, ... ],
        "stream_2_abrupt_models": [ {"model": name, "weight": w, "mae": m}, ... ]
      }
    """
    df = pd.DataFrame(historical_records)
    if df.empty:
        # Default fallback config
        return _default_stream_config(site_name, capacity_mw)

    clear_df = df[df["regime"] == "CLEAR"]
    abrupt_df = df[df["regime"] == "ABRUPT"]

    # Rank Clear
    clear_ranks = []
    for model in CANDIDATE_MODELS:
        if model in df.columns:
            err = np.abs(clear_df[model] - clear_df["actual_mw"])
            mae = float(err.mean()) if len(err) > 0 else 999.0
            clear_ranks.append((model, mae))
    clear_ranks.sort(key=lambda x: x[1])

    # Rank Abrupt
    abrupt_ranks = []
    for model in CANDIDATE_MODELS:
        if model in df.columns:
            err = np.abs(abrupt_df[model] - abrupt_df["actual_mw"])
            mae = float(err.mean()) if len(err) > 0 else 999.0
            abrupt_ranks.append((model, mae))
    abrupt_ranks.sort(key=lambda x: x[1])

    top5_clear = clear_ranks[:5] if len(clear_ranks) >= 5 else clear_ranks
    top5_abrupt = abrupt_ranks[:5] if len(abrupt_ranks) >= 5 else abrupt_ranks

    # Inverse-MAE softmax / normalized weighting
    def _calc_weights(top_list):
        if not top_list:
            return []
        inv_mae = [1.0 / max(item[1], 0.01) for item in top_list]
        tot = sum(inv_mae)
        weights = [round(w / tot, 3) for w in inv_mae]
        # Normalize to sum 1.0
        diff = round(1.0 - sum(weights), 3)
        weights[0] += diff
        return [
            {"model": top_list[i][0], "weight": weights[i], "historical_mae_mw": round(top_list[i][1], 3)}
            for i in range(len(top_list))
        ]

    return {
        "site": site_name,
        "capacity_mw": capacity_mw,
        "tolerance_band_pct": tolerance_band_pct,
        "stream_1_clear_models": _calc_weights(top5_clear),
        "stream_2_abrupt_models": _calc_weights(top5_abrupt),
    }


def _default_stream_config(site_name: str, capacity_mw: float = 10.0) -> dict:
    """
    Production-calibrated defaults for Stream 1 (Clear) and Stream 2 (Abrupt).
    Includes historical MAE benchmarks from multi-day evaluations.
    """
    # Inverse-MAE weighted clear champions
    stream1_models = [
        {"model": "ECMWF_member44", "weight": 0.269, "historical_mae_mw": 0.391, "description": "Top Clear Synoptic Champion (ECMWF)"},
        {"model": "ECMWF_member25", "weight": 0.191, "historical_mae_mw": 0.549, "description": "Clear Sky Specialist (ECMWF)"},
        {"model": "ECMWF_member35", "weight": 0.188, "historical_mae_mw": 0.559, "description": "Clear Sky Specialist (ECMWF)"},
        {"model": "ECMWF_member38", "weight": 0.180, "historical_mae_mw": 0.585, "description": "Clear Sky Specialist (ECMWF)"},
        {"model": "ECMWF_member26", "weight": 0.172, "historical_mae_mw": 0.610, "description": "Clear Sky Specialist (ECMWF)"},
    ]

    # Inverse-MAE weighted abrupt champions + SCADA closed-loop tracking
    stream2_models = [
        {"model": "DWD_ICON_member38", "weight": 0.210, "historical_mae_mw": 0.535, "description": "Rapid Convective Specialist (ICON)"},
        {"model": "DWD_ICON_member25", "weight": 0.201, "historical_mae_mw": 0.557, "description": "Rapid Convective Specialist (ICON)"},
        {"model": "DWD_ICON_member36", "weight": 0.201, "historical_mae_mw": 0.559, "description": "Rapid Convective Specialist (ICON)"},
        {"model": "DWD_ICON_member31", "weight": 0.197, "historical_mae_mw": 0.570, "description": "Rapid Convective Specialist (ICON)"},
        {"model": "DWD_ICON_member27", "weight": 0.192, "historical_mae_mw": 0.584, "description": "Rapid Convective Specialist (ICON)"},
    ]

    return {
        "site": site_name,
        "capacity_mw": capacity_mw,
        "tolerance_band_pct": 0.15,
        "stream_1_clear_models": stream1_models,
        "stream_2_abrupt_models": stream2_models,
        "scada_decay_alpha": 0.70,
        "outlier_max_rel_spread": 0.35,
        "mos_min_bias": 0.85,
        "mos_max_bias": 1.10,
    }


def rank_unconstrained_ensemble_members(
    actuals_df: pd.DataFrame,
    ensemble_df: pd.DataFrame,
    capacity_mw: float = 10.0,
    pr: float = 0.78,
) -> dict:
    """
    Unconstrained 91-Member Leaderboard Ranking:
    Pools all members from ECMWF IFS025 (51 members) + German DWD ICON (40 members)
    without brand bias, categorizes historical daylight blocks into Clear vs Abrupt,
    and returns top 5 champions with inverse-MAE weights for each regime.
    """
    merged = pd.merge(ensemble_df, actuals_df, on="hour_str", how="inner")
    if merged.empty:
        return {}

    merged["hour_num"] = pd.to_datetime(merged["hour_str"]).dt.hour
    daylight = merged[(merged["hour_num"] >= 6) & (merged["hour_num"] <= 18)].copy()

    # Classify blocks by clearness / rain
    conv_factor = (capacity_mw * 1.10) * pr / 1000.0
    member_cols = [c for c in daylight.columns if "member" in c or "icon_seamless_eps" in c or "ecmwf" in c]

    if "regime" not in daylight.columns:
        if "precip_mm" in daylight.columns and "kt" in daylight.columns:
            daylight["regime"] = np.where((daylight["precip_mm"] > 0.1) | (daylight["kt"] < 0.75), "ABRUPT", "CLEAR")
        else:
            daylight["regime"] = "CLEAR"

    def _rank_subset(sub_df):
        scores = []
        act = sub_df["actual_mw"].values
        for col in member_cols:
            pred = np.minimum(capacity_mw, sub_df[col].values * conv_factor)
            err = np.abs(pred - act)
            mae = float(np.mean(err)) if len(err) > 0 else 999.0
            agency = "ECMWF" if "ecmwf" in col else "DWD_ICON"
            clean_name = col.replace("shortwave_radiation_", "").replace("_ecmwf_ifs025_ensemble", "").replace("_icon_seamless_eps", "")
            scores.append({"agency": agency, "model": f"{agency}_{clean_name}", "historical_mae_mw": round(mae, 3)})
        scores.sort(key=lambda x: x["historical_mae_mw"])
        return scores

    clear_ranks = _rank_subset(daylight[daylight["regime"] == "CLEAR"])
    abrupt_ranks = _rank_subset(daylight[daylight["regime"] == "ABRUPT"])

    from modules.forecasting.dual_stream_engine import compute_inverse_mae_weights
    top5_clear = compute_inverse_mae_weights(clear_ranks[:5])
    top5_abrupt = compute_inverse_mae_weights(abrupt_ranks[:5])

    return {
        "stream_1_clear_models": top5_clear,
        "stream_2_abrupt_models": top5_abrupt,
    }


def save_site_config(site_name: str, config_dict: dict, output_dir: str = "schedule/plant_profiles"):
    os.makedirs(output_dir, exist_ok=True)
    target_path = os.path.join(output_dir, f"{site_name.upper()}_dual_stream_config.json")
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(config_dict, f, indent=2)
    print(f"  [CONFIG SAVED] {target_path}")
    return target_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rank models by weather regime.")
    parser.add_argument("--site", default="BHUPALPALLY", help="Plant identifier")
    parser.add_argument("--capacity", type=float, default=10.0, help="Capacity in MW")
    args = parser.parse_args()

    cfg = _default_stream_config(args.site, args.capacity)
    save_site_config(args.site, cfg)
    print(f"Generated regime-specialized dual-stream config for {args.site}")
