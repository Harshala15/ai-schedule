"""
dual_stream_engine.py

Production Dual-Stream Regime-Switching Forecasting Engine:
Generates two specialized curves for the upcoming forecast horizon:
  1. Stream 1 (Clear / Normal Specialist): Combines the top clear-sky models/members
     with Inverse-MAE Softmax Weighting, Rogue Outlier Rejection, and Morning Ground MOS Calibration.
  2. Stream 2 (Abrupt / Cloud / Rain Specialist): Combines SCADA closed-loop exponential
     decay (alpha=0.70) with top rapid-response cloud/precipitation models and outlier filtering.

Both streams are passed to the LLM Weather Arbiter for intelligent meteorological synthesis,
followed by a 4-Pass Diurnal Filter to eliminate sawteeth and enforce regulatory step limits.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any
import numpy as np


def compute_inverse_mae_weights(
    model_maes: dict[str, float] | list[dict[str, Any]] | list[float],
    method: str = "inverse_mae",
    temperature: float = 0.5,
) -> dict[str, float] | list[float]:
    """
    Computes normalized weights inversely proportional to MAE or via softmax.

    Args:
        model_maes: Dictionary mapping model_name -> mae, list of dicts with 'mae' key,
                    or list of float MAE values.
        method: 'inverse_mae' (w_i ~ 1 / MAE_i) or 'softmax' (w_i ~ exp(-MAE_i / T)).
        temperature: Temperature parameter T for softmax (lower T = sharper peak).

    Returns:
        Same structure as input with normalized weights summing strictly to 1.0.
    """
    is_dict = isinstance(model_maes, dict)
    is_dict_list = isinstance(model_maes, list) and len(model_maes) > 0 and isinstance(model_maes[0], dict)

    if is_dict:
        names = list(model_maes.keys())
        maes = [float(model_maes[k]) for k in names]
    elif is_dict_list:
        names = [d.get("model", f"model_{i}") for i, d in enumerate(model_maes)]
        maes = [float(d.get("historical_mae_mw", d.get("mae", 1.0))) for d in model_maes]
    else:
        names = None
        maes = [float(m) for m in model_maes]

    if not maes:
        return {} if is_dict else []

    n = len(maes)
    if n == 1:
        weights = [1.0]
    elif method == "softmax":
        # Softmax: w_i = exp(-mae_i / T) / sum(exp(-mae_j / T))
        tau = max(temperature, 0.01)
        # Shift for numerical stability
        min_mae = min(maes)
        exp_vals = [math.exp(-(m - min_mae) / tau) for m in maes]
        tot = sum(exp_vals)
        weights = [v / tot for v in exp_vals]
    else:
        # Inverse-MAE: w_i = (1 / max(mae_i, 0.001)) / sum(1 / max(mae_j, 0.001))
        inv_vals = [1.0 / max(m, 0.005) for m in maes]
        tot = sum(inv_vals)
        weights = [v / tot for v in inv_vals]

    # Clean rounding and normalize sum to exactly 1.0
    rounded_weights = [round(w, 4) for w in weights]
    diff = round(1.0 - sum(rounded_weights), 4)
    rounded_weights[0] = round(rounded_weights[0] + diff, 4)

    if is_dict:
        return {names[i]: rounded_weights[i] for i in range(n)}
    elif is_dict_list:
        result = []
        for i, d in enumerate(model_maes):
            item = dict(d)
            item["weight"] = rounded_weights[i]
            result.append(item)
        return result
    return rounded_weights


def filter_outliers_and_aggregate(
    predictions: list[float],
    weights: list[float] | None = None,
    capacity_mw: float = 10.0,
    max_abs_dev_ratio: float = 0.25,
    max_rel_spread: float = 0.35,
) -> tuple[float, list[int]]:
    """
    Rogue Outlier Rejection (Trimmed / Median Rejection):
    Rejects any ensemble member whose forecast diverges excessively from the ensemble median.

    Args:
        predictions: List of member predictions (MW) for a single block.
        weights: Corresponding weights for each member (sums to ~1.0).
        capacity_mw: Plant capacity in MW for relative bounding.
        max_abs_dev_ratio: Maximum absolute divergence from median as fraction of capacity (default 25%).
        max_rel_spread: Maximum relative divergence from median (default 35%).

    Returns:
        (aggregated_mw, list_of_rejected_member_indices)
    """
    if not predictions:
        return 0.0, []

    n = len(predictions)
    if n == 1:
        return max(0.0, min(capacity_mw, float(predictions[0]))), []

    if weights is None or len(weights) != n:
        weights = [1.0 / n] * n

    preds = [float(p) for p in predictions]
    med = float(np.median(preds))

    # Determine dynamic outlier threshold
    abs_thresh = max_abs_dev_ratio * capacity_mw
    if med > 0.05 * capacity_mw:
        # Daylight hours: combined relative and absolute threshold
        threshold = max(abs_thresh, max_rel_spread * med)
    else:
        # Dawn / dusk / night: tighter absolute threshold
        threshold = max(0.05 * capacity_mw, abs_thresh * 0.5)

    valid_indices = []
    rejected_indices = []
    for idx, p in enumerate(preds):
        if abs(p - med) <= threshold:
            valid_indices.append(idx)
        else:
            rejected_indices.append(idx)

    # Safety: if too many members got flagged (e.g. split consensus), retain at least top 2 closest to median
    if len(valid_indices) < 2:
        distances = [(idx, abs(preds[idx] - med)) for idx in range(n)]
        distances.sort(key=lambda x: x[1])
        valid_indices = [distances[0][0], distances[1][0]]
        rejected_indices = [idx for idx in range(n) if idx not in valid_indices]

    # Re-normalize weights of valid members
    valid_weights = [weights[i] for i in valid_indices]
    tot_weight = sum(valid_weights)
    if tot_weight > 0:
        norm_weights = [w / tot_weight for w in valid_weights]
    else:
        norm_weights = [1.0 / len(valid_indices)] * len(valid_indices)

    aggregated = sum(norm_weights[i] * preds[valid_indices[i]] for i in range(len(valid_indices)))
    return max(0.0, min(capacity_mw, round(aggregated, 3))), rejected_indices


def calculate_morning_mos_bias(
    morning_actual_mw: list[float] | None = None,
    morning_model_mw: list[float] | None = None,
    live_poa: float | None = None,
    model_gti: float | None = None,
    intraday_residual_factor: float | None = None,
    current_hour: float | None = None,
    min_bias: float = 0.85,
    max_bias: float = 1.10,
) -> float:
    """
    Computes the Morning Ground MOS (Model Output Statistics) bias factor.
    Calibrates afternoon clear-sky models to account for today's soiling, ambient
    temperature derating, or regional haze.

    Returns:
        bias_factor clamped strictly to [min_bias, max_bias].
    """
    bias_candidates: list[float] = []

    # 1. From morning SCADA actuals vs clear model generation
    if morning_actual_mw and morning_model_mw:
        valid_pairs = [
            (act, mod)
            for act, mod in zip(morning_actual_mw, morning_model_mw)
            if mod is not None and act is not None and mod > 0.10
        ]
        if len(valid_pairs) >= 3:
            tot_act = sum(p[0] for p in valid_pairs)
            tot_mod = sum(p[1] for p in valid_pairs)
            if tot_mod > 0.2:
                bias_candidates.append(tot_act / tot_mod)

    # 2. From real-time pyranometer POA vs model GTI
    if live_poa is not None and model_gti is not None and model_gti > 50.0 and live_poa > 20.0:
        bias_candidates.append(live_poa / model_gti)

    # 3. From intraday residual factor
    if intraday_residual_factor is not None and 0.20 <= intraday_residual_factor <= 1.80:
        bias_candidates.append(intraday_residual_factor)

    if not bias_candidates:
        return 1.0

    raw_bias = float(np.mean(bias_candidates))

    # Early morning dampening: before 08:30 AM, sun elevation is low and inverter wake-up
    # produces erratic ratios; damp raw bias towards 1.0.
    if current_hour is not None and current_hour < 8.5:
        progress = max(0.0, (current_hour - 6.0) / 2.5)  # 0 at 06:00, 1 at 08:30
        raw_bias = 1.0 + progress * (raw_bias - 1.0)

    # Strictly clamp to safe operational bounds
    clamped_bias = max(min_bias, min(max_bias, raw_bias))
    return round(clamped_bias, 3)


def load_dual_stream_config(site_name: str) -> dict:
    """Load the dual-stream configuration for the specified site."""
    config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "plant_profiles")
    config_path = os.path.join(config_dir, f"{site_name.upper()}_dual_stream_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"  [WARN] Failed to read {config_path}: {exc}")

    # Fallback to calibration module defaults
    from modules.calibration.rank_models_by_regime import _default_stream_config
    return _default_stream_config(site_name)


def generate_dual_streams(
    site_name: str,
    forecast_times: list[str],
    block_numbers: list[int],
    base_nwp_mw: list[float],
    live_scada_mw: float | None = None,
    live_poa: float | None = None,
    clearsky_poa: list[float] | None = None,
    weather_features: dict[str, list[float]] | None = None,
    capacity_mw: float = 10.0,
    member_predictions_stream1: dict[str, list[float]] | None = None,
    member_predictions_stream2: dict[str, list[float]] | None = None,
    intraday_state: dict[str, Any] | None = None,
    morning_mos_factor: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    Generates Stream 1 (Clear Specialist) and Stream 2 (Abrupt Specialist) forecast blocks.
    
    Incorporates:
      1. Inverse-MAE Softmax Weighting across top models
      2. Rogue Outlier Rejection per forecast block
      3. Morning Ground MOS Calibration factor for Stream 1
      4. SCADA Closed-Loop Exponential Decay (alpha=0.70) for Stream 2

    Returns:
      (stream1_blocks, stream2_blocks, regime_meta)
    """
    config = load_dual_stream_config(site_name)
    n_blocks = len(forecast_times)
    weather = weather_features or {}
    precip_list = weather.get("precip_mm", [0.0] * n_blocks)
    cloud_list = weather.get("cloud_pct", [20.0] * n_blocks)

    # -------------------------------------------------------------------------
    # 1. Compute Morning Ground MOS Bias Factor
    # -------------------------------------------------------------------------
    first_hour = None
    if forecast_times and len(forecast_times[0]) >= 16:
        try:
            first_hour = float(forecast_times[0].split(" ")[-1].split(":")[0]) + float(forecast_times[0].split(":")[-1]) / 60.0
        except Exception:
            first_hour = None

    if morning_mos_factor is not None:
        mos_bias = max(0.85, min(1.10, float(morning_mos_factor)))
    else:
        residual_fac = float(intraday_state.get("live_residual_factor")) if intraday_state and intraday_state.get("live_residual_factor") is not None else None
        model_gti_ref = None
        if clearsky_poa and len(clearsky_poa) > 0 and clearsky_poa[0] > 30.0:
            model_gti_ref = clearsky_poa[0]

        mos_bias = calculate_morning_mos_bias(
            live_poa=live_poa,
            model_gti=model_gti_ref,
            intraday_residual_factor=residual_fac,
            current_hour=first_hour,
            min_bias=0.85,
            max_bias=1.10,
        )

    # -------------------------------------------------------------------------
    # 2. Stream 1 (Clear Sky Specialist) Generation
    # -------------------------------------------------------------------------
    stream1_models_cfg = config.get("stream_1_clear_models", [])
    # Compute inverse-MAE weights if MAEs are present
    if stream1_models_cfg and "historical_mae_mw" in stream1_models_cfg[0]:
        s1_weighted_cfg = compute_inverse_mae_weights(stream1_models_cfg, method="inverse_mae")
    else:
        s1_weighted_cfg = stream1_models_cfg

    s1_weights = [item.get("weight", 1.0 / max(len(s1_weighted_cfg), 1)) for item in s1_weighted_cfg]
    if s1_weights and sum(s1_weights) > 0:
        tot_w = sum(s1_weights)
        s1_weights = [w / tot_w for w in s1_weights]

    stream1_mw = []
    s1_outlier_count = 0

    for i in range(n_blocks):
        cs_mw = 0.0
        if clearsky_poa and i < len(clearsky_poa) and clearsky_poa[i] > 10.0:
            cs_mw = (clearsky_poa[i] / 1000.0) * capacity_mw * 0.78

        nwp_val = base_nwp_mw[i] if i < len(base_nwp_mw) else cs_mw

        # If explicit member predictions are provided for Stream 1
        if member_predictions_stream1 and len(member_predictions_stream1) >= 2:
            block_preds = [
                member_predictions_stream1[k][i]
                for k in member_predictions_stream1
                if i < len(member_predictions_stream1[k])
            ]
            agg_val, rejections = filter_outliers_and_aggregate(
                block_preds,
                weights=s1_weights[:len(block_preds)] if len(s1_weights) >= len(block_preds) else None,
                capacity_mw=capacity_mw,
            )
            s1_outlier_count += len(rejections)
            raw_s1 = agg_val
        else:
            # Multi-model clear candidate synthetic ensemble:
            # 1. High-Res Synoptic NWP
            # 2. Clear-sky Physical upper envelope
            # 3. Blended high-solar NWP
            # 4. GFS/Global synoptic baseline
            # 5. Physics-guided envelope
            candidates = [
                nwp_val,
                cs_mw if cs_mw > 0 else nwp_val,
                0.70 * nwp_val + 0.30 * (cs_mw if cs_mw > 0 else nwp_val),
                0.85 * nwp_val + 0.15 * (cs_mw if cs_mw > 0 else nwp_val),
                cs_mw * 0.95 if cs_mw > 0 else nwp_val,
            ]
            agg_val, rejections = filter_outliers_and_aggregate(
                candidates,
                weights=s1_weights if len(s1_weights) == 5 else None,
                capacity_mw=capacity_mw,
            )
            s1_outlier_count += len(rejections)
            raw_s1 = agg_val

        # Apply Morning Ground MOS Calibration to Clear Stream
        calibrated_s1 = raw_s1 * mos_bias
        # Physical clamp
        stream1_mw.append(max(0.0, min(capacity_mw, round(float(calibrated_s1), 3))))

    # -------------------------------------------------------------------------
    # 3. Stream 2 (Abrupt / Cloud / Rain Specialist) Generation
    # -------------------------------------------------------------------------
    stream2_models_cfg = config.get("stream_2_abrupt_models", [])
    if stream2_models_cfg and "historical_mae_mw" in stream2_models_cfg[0]:
        s2_weighted_cfg = compute_inverse_mae_weights(stream2_models_cfg, method="inverse_mae")
    else:
        s2_weighted_cfg = stream2_models_cfg

    s2_weights = [item.get("weight", 1.0 / max(len(s2_weighted_cfg), 1)) for item in s2_weighted_cfg]
    if s2_weights and sum(s2_weights) > 0:
        tot_w = sum(s2_weights)
        s2_weights = [w / tot_w for w in s2_weights]

    stream2_mw = []
    s2_outlier_count = 0
    alpha = 0.70  # Decay rate per 15-minute block
    last_known_mw = live_scada_mw if live_scada_mw is not None else (base_nwp_mw[0] if base_nwp_mw else 0.0)

    for i in range(n_blocks):
        decay_weight = alpha ** (i + 1)
        rain_attenuation = 0.40 if precip_list[i] > 0.1 else (0.75 if cloud_list[i] > 60.0 else 1.0)
        base_val = base_nwp_mw[i] if i < len(base_nwp_mw) else 0.0
        damped_nwp = base_val * rain_attenuation

        # If explicit member predictions are provided for Stream 2
        if member_predictions_stream2 and len(member_predictions_stream2) >= 2:
            block_preds = [
                member_predictions_stream2[k][i]
                for k in member_predictions_stream2
                if i < len(member_predictions_stream2[k])
            ]
            agg_nwp, rejections = filter_outliers_and_aggregate(
                block_preds,
                weights=s2_weights[:len(block_preds)] if len(s2_weights) >= len(block_preds) else None,
                capacity_mw=capacity_mw,
            )
            s2_outlier_count += len(rejections)
        else:
            # Multi-model abrupt ensemble:
            # Combines rapid-drop sensitivity, cloud dampening, and convective attenuation
            candidates = [
                damped_nwp,
                base_val * (0.35 if precip_list[i] > 0.1 else 0.70),
                damped_nwp * 0.90,
                base_val * 0.50 if cloud_list[i] > 50.0 else damped_nwp,
                damped_nwp,
            ]
            agg_nwp, rejections = filter_outliers_and_aggregate(
                candidates,
                weights=s2_weights if len(s2_weights) == 5 else None,
                capacity_mw=capacity_mw,
            )
            s2_outlier_count += len(rejections)

        # SCADA Closed-Loop Exponential Decay Fusion:
        # Near horizon is heavily anchored to live SCADA meter; decays smoothly to abrupt NWP
        s2_val = decay_weight * last_known_mw + (1.0 - decay_weight) * agg_nwp
        stream2_mw.append(max(0.0, min(capacity_mw, round(float(s2_val), 3))))

    # -------------------------------------------------------------------------
    # 4. Assemble Block Objects & Metadata
    # -------------------------------------------------------------------------
    stream1_blocks = []
    stream2_blocks = []
    for i in range(n_blocks):
        stream1_blocks.append({
            "time": forecast_times[i],
            "block_number": block_numbers[i],
            "stream1_mw": stream1_mw[i],
            "role": "Clear_Sky_Specialist",
        })
        stream2_blocks.append({
            "time": forecast_times[i],
            "block_number": block_numbers[i],
            "stream2_mw": stream2_mw[i],
            "role": "Abrupt_Weather_Specialist",
        })

    # Ground state assessment
    live_kt = 1.0
    if live_poa is not None and clearsky_poa and len(clearsky_poa) > 0 and clearsky_poa[0] > 20.0:
        live_kt = round(live_poa / clearsky_poa[0], 3)

    is_rain_incoming = any(p > 0.1 for p in precip_list[:4])
    is_heavy_cloud = any(c > 65.0 for c in cloud_list[:4])
    ground_regime = "ABRUPT" if (live_kt < 0.75 or is_rain_incoming or is_heavy_cloud) else "CLEAR"

    regime_meta = {
        "site": site_name,
        "live_kt": live_kt,
        "ground_regime": ground_regime,
        "is_rain_incoming": is_rain_incoming,
        "is_heavy_cloud": is_heavy_cloud,
        "last_known_mw": last_known_mw,
        "mos_bias_factor": mos_bias,
        "stream1_outliers_rejected": s1_outlier_count,
        "stream2_outliers_rejected": s2_outlier_count,
    }

    return stream1_blocks, stream2_blocks, regime_meta

