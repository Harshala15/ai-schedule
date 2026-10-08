"""plant_profile.py

Defines the PlantProfile configuration container and profile loading logic
for all utility-scale solar and wind plants.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEDULE_DIR = Path(__file__).resolve().parent.parent.parent

try:
    import config
except ImportError:
    config = None

# Explicit non-meter sites where physical SCADA telemetry is absent
NON_METER_SITES = {
    "ANDAD", "GUGARIYAKHEDI", "SAWDA", "BALAKWADA", "CME", "CLIMATEDETOX",
    "EMIL", "UPL", "REWASEIT", "SIDDEHESH", "PRANAV", "LOKGREENB2", "LGEPL",
    "CHANDWASA", "CHANDAWASA"
}


@dataclass
class PlantProfile:
    """Plant hardware and regulatory configuration."""
    plant_name: str
    latitude: float
    longitude: float
    dc_capacity_mw: float
    ac_capacity_mw: float
    tilt_deg: float
    orientation_deg_from_south: float
    azimuth_openmeteo: float
    azimuth_pvlib: float
    transfer_ratio: float  # MW per W/m²
    ppa_rate_inr_per_kwh: float = 6.97
    penalty_regulation: str = "Madhya Pradesh"
    tolerance_band_mw: float = 2.0  # 10% or 15% of AC capacity based on state regulations
    band_percentage: float = 0.10
    calibrated_pr: float | None = None
    meter_data: dict[str, Any] = field(default_factory=dict)


_ENRICH_DYNAMODB_CACHE: dict[str, dict[str, float]] | None = None
_ENRICH_DYNAMODB_CACHE_TS: float = 0.0


def _fetch_enrich_live_capacities_from_dynamodb() -> dict[str, dict[str, float]]:
    """Fetch active asset AC and DC capacities for ENRICH sub-plants (EMIL, UPL, CLIMATEDETOX)
    directly from DynamoDB table 'multi_generator_plant'.
    """
    global _ENRICH_DYNAMODB_CACHE, _ENRICH_DYNAMODB_CACHE_TS
    import time
    now = time.time()
    if _ENRICH_DYNAMODB_CACHE is not None and (now - _ENRICH_DYNAMODB_CACHE_TS) < 60.0:
        return _ENRICH_DYNAMODB_CACHE

    capacities: dict[str, dict[str, float]] = {}
    table_name = os.getenv("MULTI_GENERATOR_TABLE", "multi_generator_plant")
    region_name = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")

    try:
        import boto3
        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        table = dynamodb.Table(table_name)
        resp = table.get_item(Key={"plant_id": "ZETRIC_SOLAR_PARK"})
        item = resp.get("Item")
        if item:
            mgp_list = item.get("template_config", {}).get("multi_generator_plants", [])
            for plant_entry in mgp_list:
                if str(plant_entry.get("plantName", "")).strip().upper() == "ENRICH":
                    tot_ac = plant_entry.get("schedulingCapacityAcMw") or plant_entry.get("totalCapacityAcMw")
                    tot_dc = plant_entry.get("schedulingCapacityDcMw") or plant_entry.get("totalCapacityDcMw")
                    if tot_ac is not None and tot_dc is not None:
                        capacities["ENRICH"] = {
                            "ac_capacity_mw": float(tot_ac),
                            "dc_capacity_mw": float(tot_dc),
                        }

                    for a in plant_entry.get("assets", []):
                        raw_name = str(a.get("assetName", "")).strip().upper()
                        clean_name = raw_name.replace(" ", "").replace("_", "").replace("-", "")
                        matched_key = None
                        if "EMIL" in clean_name:
                            matched_key = "EMIL"
                        elif "UPL" in clean_name:
                            matched_key = "UPL"
                        elif "CLIMATE" in clean_name or "DETOX" in clean_name:
                            matched_key = "CLIMATEDETOX"

                        if matched_key:
                            ac_val = float(a.get("acCapacityMw", 0.0))
                            dc_val = float(a.get("dcCapacityMw", ac_val))
                            capacities[matched_key] = {
                                "ac_capacity_mw": ac_val,
                                "dc_capacity_mw": dc_val,
                            }
    except Exception:
        pass

    _ENRICH_DYNAMODB_CACHE = capacities
    _ENRICH_DYNAMODB_CACHE_TS = now
    return capacities


def load_plant_profile(plant_name: str = "GSNP") -> PlantProfile:
    """Load plant profile from JSON file or config.py fallback."""
    name_upper = plant_name.upper().strip()
    profile_path = _SCHEDULE_DIR / "plant_profiles" / f"{name_upper}.json"
    
    data: dict[str, Any] = {}
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    fallbacks = getattr(config, "_PLANT_FALLBACKS", {})
    cfg_profile = fallbacks.get(name_upper, {})
    if not cfg_profile and str(getattr(config, "PLANT_NAME", "")).upper() == name_upper:
        cfg_profile = getattr(config, "PLANT_PROFILE", {})

    lat = float(data.get("latitude") or cfg_profile.get("latitude") or getattr(config, "PLANT_LAT", 24.077752))
    lon = float(data.get("longitude") or cfg_profile.get("longitude") or getattr(config, "PLANT_LON", 75.337636))

    dc_kw = data.get("dc_capacity_kw")
    if dc_kw is not None:
        dc_mw = float(dc_kw) / 1000.0
    else:
        dc_mw = float(cfg_profile.get("dc_capacity_mw") or getattr(config, "PLANT_DC_CAPACITY_MW", 23.6016))

    ac_kw = data.get("maximum_feed_in_ac_kw")
    if ac_kw is not None:
        ac_mw = float(ac_kw) / 1000.0
    else:
        ac_mw = float(cfg_profile.get("capacity_mw") or getattr(config, "PLANT_CAPACITY_MW", 20.0))

    if name_upper in ("EMIL", "UPL", "CLIMATEDETOX", "ENRICH"):
        enrich_live = _fetch_enrich_live_capacities_from_dynamodb()
        if name_upper in enrich_live:
            live_ac = enrich_live[name_upper].get("ac_capacity_mw")
            live_dc = enrich_live[name_upper].get("dc_capacity_mw")
            if live_ac is not None and live_ac > 0.0:
                ac_mw = live_ac
            if live_dc is not None and live_dc > 0.0:
                dc_mw = live_dc

    tilt = float(data.get("tilt_deg") or cfg_profile.get("tilt_deg") or getattr(config, "PLANT_TILT_DEG", 15.0))
    orient = float(data.get("orientation_deg_from_south") or cfg_profile.get("orientation_deg_from_south") or getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 8.0))

    az_om = orient
    az_pvlib = 180.0 + orient

    meter_data = data.get("meter_data", {})
    is_non_meter = (
        name_upper in NON_METER_SITES
        or bool(meter_data.get("is_non_meter_site", False))
        or bool(meter_data.get("is_virtual", False))
        or bool(data.get("is_non_meter_site", False))
    )

    if is_non_meter:
        explicit_pr = data.get("performance_ratio") or cfg_profile.get("performance_ratio")
        base_pr = float(explicit_pr) if explicit_pr is not None else 0.8300
        transfer_ratio = round((dc_mw * base_pr) / 1000.0, 6)
    else:
        explicit_pr = data.get("performance_ratio") or cfg_profile.get("performance_ratio")
        base_pr = float(explicit_pr) if explicit_pr is not None else float(getattr(config, "PERFORMANCE_RATIO", 0.78))
        transfer_ratio = round((dc_mw * base_pr) / 1000.0, 6)

    ppa = float(data.get("ppa_rate_inr_per_kwh") or cfg_profile.get("ppa_rate_inr_per_kwh") or getattr(config, "PPA_RATE_INR_PER_KWH", 6.97))
    reg = str(data.get("penalty_regulation") or cfg_profile.get("penalty_regulation") or "Madhya Pradesh")

    reg_lower = reg.lower()
    if any(s in reg_lower for s in ["maharashtra", "merc"]):
        band_pct = 0.10
    elif any(s in reg_lower for s in ["telangana", "tserc", "karnataka", "kerc"]):
        band_pct = 0.15
    else:
        band_pct = 0.10

    if "tolerance_band_percent" in data:
        raw_pct = float(data["tolerance_band_percent"])
        band_pct = raw_pct / 100.0 if raw_pct > 1.0 else raw_pct
    elif "band_percentage" in data:
        raw_pct = float(data["band_percentage"])
        band_pct = raw_pct / 100.0 if raw_pct > 1.0 else raw_pct

    if "tolerance_band_mw" in data:
        tol_mw = float(data["tolerance_band_mw"])
    else:
        tol_mw = round(ac_mw * band_pct, 3)

    return PlantProfile(
        plant_name=name_upper,
        latitude=lat,
        longitude=lon,
        dc_capacity_mw=dc_mw,
        ac_capacity_mw=ac_mw,
        tilt_deg=tilt,
        orientation_deg_from_south=orient,
        azimuth_openmeteo=az_om,
        azimuth_pvlib=az_pvlib,
        transfer_ratio=transfer_ratio,
        ppa_rate_inr_per_kwh=ppa,
        penalty_regulation=reg,
        tolerance_band_mw=tol_mw,
        band_percentage=band_pct,
        calibrated_pr=base_pr if is_non_meter else None,
        meter_data=meter_data,
    )
