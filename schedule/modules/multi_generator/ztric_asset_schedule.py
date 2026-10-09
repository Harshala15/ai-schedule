"""
ztric_asset_schedule.py

Handles asset-wise schedule generation and formatting for the ZTRIC multi-generator plant (Chakur 132kV).
Reads live asset capacities from DynamoDB table 'multi_generator_plant' (ZETRIC_SOLAR_PARK) and outputs
the specialized 14-column penalty schedule CSV:
  block,start_time,end_time,DE_SOLAR,GAJLAXMI,CHAKUR_ONE_BLOCK_1,CHAKUR_ONE_BLOCK_2,POLYBOND,SNHEAT,INTEGRATED,INDIQUBE,OA_MSEDCL,AEML,total_ai_schedule_mw
"""

from __future__ import annotations

import csv
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger(__name__)

# Fallback verified asset capacities in MW for ZTRIC (ZETRIC_SOLAR_PARK)
DEFAULT_ZTRIC_ASSET_CAPACITIES: Dict[str, Dict[str, Any]] = {
    "DE_SOLAR": {"ac_cap": 1.950, "buyer": "OA_MSEDCL", "aliases": ["DE SOLAR", "DE_SOLAR", "DE-SOLAR"]},
    "GAJLAXMI": {"ac_cap": 1.475, "buyer": "OA_MSEDCL", "aliases": ["GAJLAXMI"]},
    "CHAKUR_ONE_BLOCK_1": {"ac_cap": 3.800, "buyer": "OA_MSEDCL", "aliases": ["CHAKUR ONE BLOCK 1", "CHAKUR_ONE_BLOCK_1", "CHAKUR-ONE-BLOCK-1"]},
    "CHAKUR_ONE_BLOCK_2": {"ac_cap": 4.200, "buyer": "OA_MSEDCL", "aliases": ["CHAKUR ONE BLOCK 2", "CHAKUR_ONE_BLOCK_2", "CHAKUR-ONE-BLOCK-2"]},
    "POLYBOND": {"ac_cap": 2.300, "buyer": "OA_MSEDCL", "aliases": ["POLYBOND"]},
    "SNHEAT": {"ac_cap": 1.475, "buyer": "OA_MSEDCL", "aliases": ["S.N.HEAT", "SNHEAT", "S.N. HEAT", "SN_HEAT"]},
    "INTEGRATED": {"ac_cap": 1.000, "buyer": "OA_MSEDCL", "aliases": ["INTEGRATED"]},
    "INDIQUBE": {"ac_cap": 2.950, "buyer": "AEML", "aliases": ["INDIQUBE"]},
}

ZTRIC_ASSET_COLUMNS = [
    "DE_SOLAR",
    "GAJLAXMI",
    "CHAKUR_ONE_BLOCK_1",
    "CHAKUR_ONE_BLOCK_2",
    "POLYBOND",
    "SNHEAT",
    "INTEGRATED",
    "INDIQUBE",
]

ZTRIC_CSV_FIELDNAMES = [
    "block",
    "start_time",
    "end_time",
    "DE_SOLAR",
    "GAJLAXMI",
    "CHAKUR_ONE_BLOCK_1",
    "CHAKUR_ONE_BLOCK_2",
    "POLYBOND",
    "SNHEAT",
    "INTEGRATED",
    "INDIQUBE",
    "OA_MSEDCL",
    "AEML",
    "total_ai_schedule_mw",
]


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


def load_ztric_asset_capacities() -> Dict[str, Dict[str, Any]]:
    """
    Fetch active asset AC capacities for ZTRIC from DynamoDB table 'multi_generator_plant'.
    Falls back to verified default schema if DynamoDB is unavailable.
    """
    table_name = os.getenv("MULTI_GENERATOR_TABLE", "multi_generator_plant")
    region_name = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")

    capacities: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in DEFAULT_ZTRIC_ASSET_CAPACITIES.items()}

    try:
        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        table = dynamodb.Table(table_name)
        resp = table.get_item(Key={"plant_id": "ZETRIC_SOLAR_PARK"})
        item = resp.get("Item")
        if not item:
            logger.info("ZETRIC_SOLAR_PARK record not found in %s; using verified defaults.", table_name)
            return capacities

        mgp_list = item.get("template_config", {}).get("multi_generator_plants", [])
        for plant_entry in mgp_list:
            if str(plant_entry.get("plantName", "")).strip().upper() in ("ZETRIC", "ZTRIC"):
                assets = plant_entry.get("assets", [])
                for a in assets:
                    raw_name = str(a.get("assetName", "")).strip()
                    clean_name = raw_name.upper().replace(".", "").replace("-", " ").replace("_", " ")
                    ac_cap = _to_float(a.get("acCapacityMw"), default=0.0)
                    buyer = str(a.get("buyer", "")).strip()
                    buyer_col = "AEML" if "AEML" in buyer.upper() else "OA_MSEDCL"

                    # Match to canonical column
                    matched_key = None
                    for col_key, meta in capacities.items():
                        for alias in meta["aliases"]:
                            clean_alias = alias.upper().replace(".", "").replace("-", " ").replace("_", " ")
                            if clean_name == clean_alias or clean_name in clean_alias or clean_alias in clean_name:
                                matched_key = col_key
                                break
                        if matched_key:
                            break

                    if matched_key:
                        capacities[matched_key]["ac_cap"] = ac_cap
                        capacities[matched_key]["buyer"] = buyer_col
                        logger.info("Updated ZTRIC asset '%s' capacity to %.3f MW (%s)", matched_key, ac_cap, buyer_col)

    except Exception as exc:
        logger.warning("Could not query DynamoDB '%s' for ZTRIC assets: %s; using defaults.", table_name, exc)

    return capacities


def disaggregate_ztric_block_mw(
    total_mw: float,
    asset_capacities: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    """
    Disaggregates master plant MW for a single 15-minute block into the 8 assets
    proportionally by available capacity, with individual inverter capacity clipping.
    """
    total_mw = max(0.0, float(total_mw))
    if total_mw < 0.005:
        res = {col: 0.0 for col in ZTRIC_ASSET_COLUMNS}
        res["OA_MSEDCL"] = 0.0
        res["AEML"] = 0.0
        res["total_ai_schedule_mw"] = 0.0
        return res

    total_cap = sum(meta["ac_cap"] for meta in asset_capacities.values())
    if total_cap <= 0.0:
        total_cap = 19.15

    # Multi-pass allocation with individual capacity clipping
    allocated: Dict[str, float] = {}
    remaining_mw = total_mw
    active_assets = set(ZTRIC_ASSET_COLUMNS)

    for _ in range(4):  # Convergence in max 2-3 passes
        if not active_assets or remaining_mw <= 0.0001:
            break
        current_active_cap = sum(asset_capacities[c]["ac_cap"] for c in active_assets)
        if current_active_cap <= 0.0:
            break

        newly_capped = set()
        for col in list(active_assets):
            cap = asset_capacities[col]["ac_cap"]
            share = (cap / current_active_cap) * remaining_mw
            tentative = allocated.get(col, 0.0) + share
            if tentative >= cap:
                allocated[col] = cap
                newly_capped.add(col)
            else:
                allocated[col] = tentative

        if newly_capped:
            active_assets -= newly_capped
            # Recalculate remaining MW for uncapped
            allocated_so_far = sum(allocated.values())
            remaining_mw = max(0.0, total_mw - allocated_so_far)
        else:
            break

    # If any residual unallocated due to total plant clipping, fill remaining capacity proportionally
    for col in ZTRIC_ASSET_COLUMNS:
        allocated.setdefault(col, 0.0)
        # Cap strictly at rating
        allocated[col] = min(allocated[col], asset_capacities[col]["ac_cap"])

    # Compute buyer subtotals
    oa_mw = sum(allocated[col] for col in ZTRIC_ASSET_COLUMNS if asset_capacities[col]["buyer"] == "OA_MSEDCL")
    aeml_mw = sum(allocated[col] for col in ZTRIC_ASSET_COLUMNS if asset_capacities[col]["buyer"] == "AEML")
    actual_total = oa_mw + aeml_mw

    result: Dict[str, float] = {}
    for col in ZTRIC_ASSET_COLUMNS:
        result[col] = round(allocated[col], 2)
    result["OA_MSEDCL"] = round(oa_mw, 2)
    result["AEML"] = round(aeml_mw, 2)
    result["total_ai_schedule_mw"] = round(actual_total, 2)

    return result


def write_ztric_asset_penalty_csv(
    schedule_by_block: Dict[int, Dict[str, Any]],
    input_csv_path: Path,
    output_csv_path: Path,
    target_date_str: str,
    total_blocks: int = 96,
) -> Dict[str, Any]:
    """
    Generates and writes the 14-column asset-wise penalty schedule CSV for ZTRIC:
      block,start_time,end_time,DE_SOLAR,GAJLAXMI,CHAKUR_ONE_BLOCK_1,CHAKUR_ONE_BLOCK_2,POLYBOND,SNHEAT,INTEGRATED,INDIQUBE,OA_MSEDCL,AEML,total_ai_schedule_mw
    """
    asset_capacities = load_ztric_asset_capacities()

    from modules.control_windows.control_window_engine import PlantControlWindowEngine
    cw_engine = PlantControlWindowEngine()
    windows = cw_engine.load_active_windows("ZTRIC", target_date_str)

    rows: List[Dict[str, Any]] = []
    total_generated_mw = 0.0

    nom_ac = sum(c["ac_cap"] for c in asset_capacities.values())
    nom_dc = 23.0  # nominal DC for ZTRIC 19.15-23 MW

    for block in range(1, total_blocks + 1):
        # 15-minute start and end time
        start_min = (block - 1) * 15
        end_min = block * 15
        s_hr, s_min = divmod(start_min, 60)
        e_hr, e_min = divmod(end_min, 60)
        start_time_str = f"{s_hr:02d}:{s_min:02d}"
        end_time_str = "00:00" if e_hr == 24 else f"{e_hr:02d}:{e_min:02d}"

        b_data = schedule_by_block.get(block, {})
        raw_mw = float(b_data.get("intellis_mw", 0.0) or b_data.get("schedule_mw", 0.0) or 0.0)

        # Solar night zeroing (Blocks 1-23 and 75-96)
        if block < 24 or block > 74:
            raw_mw = 0.0

        # 1. Compute dynamic effective block capacity per asset considering active control windows
        block_capacities = {k: dict(v) for k, v in asset_capacities.items()}
        for a_key, a_cfg in asset_capacities.items():
            a_ac_cap = a_cfg["ac_cap"]
            a_dc_cap = a_cfg.get("dc_cap", a_ac_cap * 1.3)
            a_clean = a_key.upper().strip()
            a_clean_no_underscore = a_clean.replace("_", "")

            a_windows = [
                w for w in windows
                if (
                    str(w.get("asset_scope", "")).lower() == "asset"
                    and (
                        str(w.get("asset_id") or "").strip().upper() in (a_clean, a_clean_no_underscore)
                        or str(w.get("asset_name") or "").strip().upper() in (a_clean, a_clean_no_underscore)
                        or a_clean in str(w.get("asset_id") or "").strip().upper()
                    )
                )
            ]
            ctrl_match = cw_engine.match_block_control(block, target_date_str, a_windows, a_ac_cap, a_dc_cap)
            block_capacities[a_key]["ac_cap"] = float(ctrl_match["effective_control_capacity_ac_mw"])

        # 2. Site-level / combined curtailment windows
        site_windows = [
            w for w in windows
            if (
                str(w.get("asset_scope", "")).lower() == "combined"
                or str(w.get("asset_id") or "").strip().upper() in ("COMBINED", "ALL")
            )
        ]
        site_ctrl = cw_engine.match_block_control(block, target_date_str, site_windows, nom_ac, nom_dc)
        site_eff_cap = float(site_ctrl["effective_control_capacity_ac_mw"])

        asset_vals = disaggregate_ztric_block_mw(raw_mw, block_capacities)

        # Scale down if site curtailment is lower than sum of assets
        tot_unconstrained = asset_vals["total_ai_schedule_mw"]
        if site_eff_cap < nom_ac and tot_unconstrained > site_eff_cap:
            scale = site_eff_cap / max(0.001, tot_unconstrained)
            for col in ZTRIC_ASSET_COLUMNS:
                asset_vals[col] = round(asset_vals[col] * scale, 2)
            # Recompute buyer subtotals
            oa_mw = sum(asset_vals[c] for c in ZTRIC_ASSET_COLUMNS if block_capacities[c]["buyer"] == "OA_MSEDCL")
            aeml_mw = sum(asset_vals[c] for c in ZTRIC_ASSET_COLUMNS if block_capacities[c]["buyer"] == "AEML")
            asset_vals["OA_MSEDCL"] = round(oa_mw, 2)
            asset_vals["AEML"] = round(aeml_mw, 2)
            asset_vals["total_ai_schedule_mw"] = round(oa_mw + aeml_mw, 2)

        total_generated_mw += asset_vals["total_ai_schedule_mw"]

        row = {
            "block": block,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "DE_SOLAR": asset_vals["DE_SOLAR"],
            "GAJLAXMI": asset_vals["GAJLAXMI"],
            "CHAKUR_ONE_BLOCK_1": asset_vals["CHAKUR_ONE_BLOCK_1"],
            "CHAKUR_ONE_BLOCK_2": asset_vals["CHAKUR_ONE_BLOCK_2"],
            "POLYBOND": asset_vals["POLYBOND"],
            "SNHEAT": asset_vals["SNHEAT"],
            "INTEGRATED": asset_vals["INTEGRATED"],
            "INDIQUBE": asset_vals["INDIQUBE"],
            "OA_MSEDCL": asset_vals["OA_MSEDCL"],
            "AEML": asset_vals["AEML"],
            "total_ai_schedule_mw": asset_vals["total_ai_schedule_mw"],
        }
        rows.append(row)

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ZTRIC_CSV_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    logger.info(
        "[ZTRIC] Written 14-column asset-wise penalty schedule to '%s' (total energy: %.2f MWh)",
        output_csv_path,
        total_generated_mw * 0.25,
    )

    return {
        "input_csv": str(input_csv_path),
        "output_csv": str(output_csv_path),
        "total_blocks": total_blocks,
        "format": "ZTRIC_MULTI_GENERATOR_ASSET_WISE",
        "assets_included": ZTRIC_ASSET_COLUMNS,
        "buyers_included": ["OA_MSEDCL", "AEML"],
        "total_daylight_mw": round(total_generated_mw, 2),
    }
