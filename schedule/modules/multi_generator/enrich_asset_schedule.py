"""
enrich_asset_schedule.py

Handles asset-wise schedule generation and formatting for the ENRICH multi-generator park (Solapur / Osmanabad).
Reads live asset capacities from DynamoDB table 'multi_generator_plant' (ZETRIC_SOLAR_PARK -> ENRICH) and outputs
the consolidated 7-column penalty schedule CSV:
  block,start_time,end_time,CLIMATEDETOX,EMIL,UPL,total_ai_schedule_mw
as well as individual asset schedules for backward compatibility with QCA ingestion.
"""

from __future__ import annotations

import csv
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

# Fallback verified asset capacities in MW for ENRICH
DEFAULT_ENRICH_ASSET_CAPACITIES: Dict[str, Dict[str, Any]] = {
    "CLIMATEDETOX": {
        "ac_cap": 5.000,
        "dc_cap": 7.000,
        "aliases": ["CLIMATEDETOX", "CLIMATE DETOX", "CLIMATE_DETOX", "CLIMATE-DETOX"],
    },
    "EMIL": {
        "ac_cap": 1.620,
        "dc_cap": 1.620,
        "aliases": ["EMIL", "E.M.I.L", "EMIL_SOLAR"],
    },
    "UPL": {
        "ac_cap": 1.000,
        "dc_cap": 1.500,
        "aliases": ["UPL", "U.P.L", "UPL_SOLAR"],
    },
}

ENRICH_ASSET_COLUMNS = [
    "CLIMATEDETOX",
    "EMIL",
    "UPL",
]

ENRICH_CSV_FIELDNAMES = [
    "block",
    "start_time",
    "end_time",
    "CLIMATEDETOX",
    "EMIL",
    "UPL",
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


def load_enrich_asset_capacities() -> Dict[str, Dict[str, Any]]:
    """
    Fetch active asset AC capacities for ENRICH from DynamoDB table 'multi_generator_plant'.
    Falls back to verified default schema if DynamoDB is unavailable.
    """
    table_name = os.getenv("MULTI_GENERATOR_TABLE", "multi_generator_plant")
    region_name = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")

    capacities: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in DEFAULT_ENRICH_ASSET_CAPACITIES.items()}

    try:
        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        table = dynamodb.Table(table_name)
        resp = table.get_item(Key={"plant_id": "ZETRIC_SOLAR_PARK"})
        item = resp.get("Item")
        if not item:
            logger.info("ZETRIC_SOLAR_PARK record not found in %s; using verified ENRICH defaults.", table_name)
            return capacities

        mgp_list = item.get("template_config", {}).get("multi_generator_plants", [])
        for plant_entry in mgp_list:
            if str(plant_entry.get("plantName", "")).strip().upper() == "ENRICH":
                assets = plant_entry.get("assets", [])
                for a in assets:
                    raw_name = str(a.get("assetName", "")).strip()
                    clean_name = raw_name.upper().replace(".", "").replace("-", " ").replace("_", " ")
                    ac_cap = _to_float(a.get("acCapacityMw"), default=0.0)
                    dc_cap = _to_float(a.get("dcCapacityMw"), default=ac_cap)

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

                    if matched_key and ac_cap > 0.0:
                        capacities[matched_key]["ac_cap"] = ac_cap
                        capacities[matched_key]["dc_cap"] = dc_cap
                        logger.info("Updated ENRICH asset '%s' capacity to %.3f MW AC", matched_key, ac_cap)

    except Exception as exc:
        logger.warning("Could not query DynamoDB '%s' for ENRICH assets: %s; using defaults.", table_name, exc)

    return capacities


def disaggregate_enrich_block_mw(
    total_mw: float,
    asset_capacities: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    """
    Disaggregates master park MW for a single 15-minute block into the 3 assets
    proportionally by available AC capacity, with individual inverter capacity clipping.
    """
    total_mw = max(0.0, float(total_mw))
    if total_mw < 0.005:
        res = {col: 0.0 for col in ENRICH_ASSET_COLUMNS}
        res["total_ai_schedule_mw"] = 0.0
        return res

    total_cap = sum(meta["ac_cap"] for meta in asset_capacities.values())
    if total_cap <= 0.0:
        total_cap = 7.620

    # Multi-pass allocation with individual capacity clipping
    allocated: Dict[str, float] = {}
    remaining_mw = total_mw
    active_assets = set(ENRICH_ASSET_COLUMNS)

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
            allocated_so_far = sum(allocated.values())
            remaining_mw = max(0.0, total_mw - allocated_so_far)
        else:
            break

    # Cap strictly at rating
    for col in ENRICH_ASSET_COLUMNS:
        allocated.setdefault(col, 0.0)
        allocated[col] = min(allocated[col], asset_capacities[col]["ac_cap"])

    actual_total = sum(allocated[col] for col in ENRICH_ASSET_COLUMNS)

    result: Dict[str, float] = {}
    for col in ENRICH_ASSET_COLUMNS:
        result[col] = round(allocated[col], 3)
    result["total_ai_schedule_mw"] = round(actual_total, 3)

    return result


def write_enrich_asset_penalty_csv(
    schedule_by_block: Dict[int, Dict[str, Any]],
    input_csv_path: Path,
    output_csv_path: Path,
    target_date_str: str,
    total_blocks: int = 96,
) -> Dict[str, Any]:
    """
    Generates and writes the consolidated 7-column asset-wise penalty schedule CSV for ENRICH:
      block,start_time,end_time,CLIMATEDETOX,EMIL,UPL,total_ai_schedule_mw
    Also generates individual asset CSVs for seamless backward compatibility.
    """
    asset_capacities = load_enrich_asset_capacities()

    from modules.control_windows.control_window_engine import PlantControlWindowEngine
    cw_engine = PlantControlWindowEngine()
    windows = cw_engine.load_active_windows("ENRICH", target_date_str)

    rows: List[Dict[str, Any]] = []
    total_generated_mw = 0.0

    # Prepare data containers for individual asset files
    individual_asset_rows: Dict[str, List[Dict[str, Any]]] = {
        col: [] for col in ENRICH_ASSET_COLUMNS
    }

    nom_ac = sum(c["ac_cap"] for c in asset_capacities.values())
    nom_dc = sum(c.get("dc_cap", c["ac_cap"]) for c in asset_capacities.values())

    for block in range(1, total_blocks + 1):
        start_min = (block - 1) * 15
        end_min = block * 15
        s_hr, s_min = divmod(start_min, 60)
        e_hr, e_min = divmod(end_min, 60)
        start_time_str = f"{s_hr:02d}:{s_min:02d}"
        end_time_str = "00:00" if e_hr == 24 else f"{e_hr:02d}:{e_min:02d}"
        time_interval_str = f"{start_time_str} - {end_time_str}"

        b_data = schedule_by_block.get(block, {})
        raw_mw = float(b_data.get("intellis_mw", 0.0) or b_data.get("schedule_mw", 0.0) or 0.0)

        # Solar night zeroing (Blocks 1-23 and 75-96)
        if block < 24 or block > 74:
            raw_mw = 0.0

        # 1. Compute dynamic effective block capacity per asset considering active control windows
        block_capacities = {k: dict(v) for k, v in asset_capacities.items()}
        for a_key, a_cfg in asset_capacities.items():
            a_ac_cap = a_cfg["ac_cap"]
            a_dc_cap = a_cfg.get("dc_cap", a_ac_cap)
            a_clean = a_key.upper().strip()
            a_aliases = [al.upper().strip() for al in a_cfg.get("aliases", [a_key])]
            if a_clean not in a_aliases:
                a_aliases.append(a_clean)
            clean_al_set = set(a_aliases) | {a.replace("_", "").replace(" ", "").replace("-", "") for a in a_aliases}

            a_windows = [
                w for w in windows
                if (
                    str(w.get("asset_scope", "")).lower() == "asset"
                    and (
                        str(w.get("asset_id") or "").strip().upper() in clean_al_set
                        or str(w.get("asset_name") or "").strip().upper() in clean_al_set
                        or any(al in str(w.get("asset_id") or "").strip().upper() for al in clean_al_set)
                        or any(str(w.get("asset_id") or "").strip().upper() in al for al in clean_al_set)
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

        asset_vals = disaggregate_enrich_block_mw(raw_mw, block_capacities)

        # Scale down if site curtailment is lower than sum of assets
        tot_unconstrained = asset_vals["total_ai_schedule_mw"]
        if site_eff_cap < nom_ac and tot_unconstrained > site_eff_cap:
            scale = site_eff_cap / max(0.001, tot_unconstrained)
            for col in ENRICH_ASSET_COLUMNS:
                asset_vals[col] = round(asset_vals[col] * scale, 3)
            asset_vals["total_ai_schedule_mw"] = round(sum(asset_vals[c] for c in ENRICH_ASSET_COLUMNS), 3)

        total_generated_mw += asset_vals["total_ai_schedule_mw"]

        row = {
            "block": block,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "CLIMATEDETOX": asset_vals["CLIMATEDETOX"],
            "EMIL": asset_vals["EMIL"],
            "UPL": asset_vals["UPL"],
            "total_ai_schedule_mw": asset_vals["total_ai_schedule_mw"],
        }
        rows.append(row)

        # Individual asset records
        for asset_name in ENRICH_ASSET_COLUMNS:
            individual_asset_rows[asset_name].append({
                "Block": block,
                "Time Interval (15 minute interval)": time_interval_str,
                "intellis_gti": round(float(b_data.get("intellis_gti", 0.0)), 2),
                "intellis_mw": asset_vals[asset_name],
                "schedule_mw": asset_vals[asset_name],
            })

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Write consolidated 7-column CSV
    with open(output_csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENRICH_CSV_FIELDNAMES)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    # 2. Write backward-compatible individual asset CSVs in the same output directory
    for asset_name, a_rows in individual_asset_rows.items():
        asset_out_path = output_csv_path.parent / f"{asset_name}_{target_date_str}_current_final_schedule.csv"
        fieldnames = ["Block", "Time Interval (15 minute interval)", "intellis_gti", "intellis_mw", "schedule_mw"]
        with open(asset_out_path, "w", newline="", encoding="utf-8") as a_handle:
            a_writer = csv.DictWriter(a_handle, fieldnames=fieldnames)
            a_writer.writeheader()
            for ar in a_rows:
                a_writer.writerow(ar)

    logger.info(
        "[ENRICH] Written 7-column asset-wise schedule to '%s' (total energy: %.2f MWh)",
        output_csv_path,
        total_generated_mw * 0.25,
    )

    return {
        "input_csv": str(input_csv_path),
        "output_csv": str(output_csv_path),
        "total_blocks": total_blocks,
        "format": "ENRICH_MULTI_GENERATOR_ASSET_WISE",
        "assets_included": ENRICH_ASSET_COLUMNS,
        "total_daylight_mw": round(total_generated_mw, 3),
    }
