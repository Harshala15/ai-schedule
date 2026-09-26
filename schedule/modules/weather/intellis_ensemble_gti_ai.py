"""Intellis Ensemble GTI AI Module (Ultra-Accuracy Edition).

Predicts highly accurate Global Tilted Irradiance (GTI) and scheduled MW for solar plants
by combining:
1. Plant Profile Hardware Parameters (Tilt, Azimuth, DC/AC capacities, PPA tariff, transfer ratio).
2. Open-Meteo Premium 143-Member Multi-Agency Super-Ensemble:
   - DWD ICON-Seamless (40 members, 13 km)
   - ECMWF IFS-EPS (51 members, 25 km)
   - NOAA GEFS-EPS (31 members, 25 km)
   - Canada GEM-EPS (21 members, 39 km)
3. 7-Day Exponential Time-Decay Rolling Benchmark (tau = 3.5 days).
4. Atmospheric Regime-Conditioned Analog Matching (Clearness Index Kt).
5. Tri-Engine Quota Selection (Top 3 ICON + Top 2 ECMWF + Top 2 GEFS) with Bayesian Inverse-Variance weights.
6. Phase-Resolved Diurnal Clear-Sky Index (kt) Blending.
7. Closed-Loop Real-Time Intraday SCADA Telemetry Relaxation (T+4 Nudge).
8. CERC / State DSM Regulatory Slabs & Penalty Calculations.
"""

from __future__ import annotations

import argparse
import datetime as dt
from datetime import datetime, timedelta
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    from pvlib.location import Location
    from pvlib import irradiance
    HAS_PVLIB = True
except ImportError:
    HAS_PVLIB = False

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

_MODULE_DIR = Path(__file__).resolve().parent
_SCHEDULE_DIR = _MODULE_DIR.parent.parent
_WORKSPACE_ROOT = _SCHEDULE_DIR.parent if (_SCHEDULE_DIR.parent / "openmeteo_premium_data").exists() else _SCHEDULE_DIR

if str(_SCHEDULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SCHEDULE_DIR))

try:
    import config
except ImportError:
    config = None

# Production Open-Meteo Premium API Configuration
DEFAULT_API_KEY = "jbThkFlLZSXZE3CU"
CUSTOMER_ENSEMBLE_URL = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
PUBLIC_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Explicit non-meter sites where physical SCADA telemetry is absent
# and satellite solar radiation API (GTI) acts as the virtual meter input
NON_METER_SITES = {"ANDAD", "GUGARIYAKHEDI", "SAWDA", "BALAKWADA", "CME", "CLIMATEDETOX", "EMIL", "UPL", "REWASEIT"}


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

    tilt = float(data.get("tilt_deg") or cfg_profile.get("tilt_deg") or getattr(config, "PLANT_TILT_DEG", 15.0))
    orient = float(data.get("orientation_deg_from_south") or cfg_profile.get("orientation_deg_from_south") or getattr(config, "PLANT_ORIENTATION_DEG_FROM_SOUTH", 8.0))

    az_om = orient
    az_pvlib = 180.0 + orient

    # Plant-specific base PR
    base_pr = float(data.get("performance_ratio") or cfg_profile.get("performance_ratio") or getattr(config, "PERFORMANCE_RATIO", 0.78))
    transfer_ratio = round((dc_mw * base_pr) / 1000.0, 6)

    ppa = float(data.get("ppa_rate_inr_per_kwh") or cfg_profile.get("ppa_rate_inr_per_kwh") or getattr(config, "PPA_RATE_INR_PER_KWH", 6.97))
    reg = str(data.get("penalty_regulation") or cfg_profile.get("penalty_regulation") or "Madhya Pradesh")

    # State-Aware DSM Tolerance Band
    reg_lower = reg.lower()
    if any(s in reg_lower for s in ["maharashtra", "merc", "karnataka", "kerc", "telangana", "tserc"]):
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
    meter_data = data.get("meter_data", {})

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
        calibrated_pr=None,
        meter_data=meter_data,
    )


class IntellisEnsembleGTIAI:
    """Intellis AI Engine for 143-Model Ensemble Global Tilted Irradiance (GTI) Forecasting."""

    def __init__(
        self,
        plant_profile: PlantProfile | None = None,
        plant_name: str = "GSNP",
        api_key: str | None = None,
        cache_dir: Path | None = None,
    ):
        self.profile = plant_profile or load_plant_profile(plant_name)
        self.api_key = api_key or getattr(config, "OPENMETEO_API_KEY", "") or os.getenv("OPENMETEO_API_KEY", "") or DEFAULT_API_KEY
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        elif os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT"):
            self.cache_dir = Path("/tmp/openmeteo_premium_data")
        else:
            self.cache_dir = _WORKSPACE_ROOT / "openmeteo_premium_data"

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.cache_dir = Path("/tmp/openmeteo_premium_data")
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.tz = ZoneInfo("Asia/Kolkata")

    def get_api_url(self) -> str:
        return CUSTOMER_ENSEMBLE_URL if self.api_key else PUBLIC_ENSEMBLE_URL

    # -------------------------------------------------------------------------
    # 1. Fetch 143-Member Weather Data (ICON + ECMWF + GEFS + GEM)
    # -------------------------------------------------------------------------
    def fetch_ensemble_weather(self, target_date_str: str) -> dict[str, Any]:
        """Fetch up to 143 ensemble members from Open-Meteo Premium API with site tilt & azimuth."""
        cache_file = self.cache_dir / f"premium_gti_{self.profile.plant_name}_{target_date_str}.json"
        if not cache_file.exists():
            legacy_cache = self.cache_dir / f"premium_gti_{target_date_str}.json"
            if legacy_cache.exists() and self.profile.plant_name in ("GSNP", "GSPPL"):
                cache_file = legacy_cache
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                    # Check if cached data already contains multi-family models
                    if "hourly" in cached_data and len(cached_data["hourly"].keys()) > 50:
                        return cached_data
            except Exception:
                pass

        params: dict[str, Any] = {
            "latitude": self.profile.latitude,
            "longitude": self.profile.longitude,
            "start_date": target_date_str,
            "end_date": target_date_str,
            "hourly": [
                "global_tilted_irradiance_instant",
                "global_tilted_irradiance",
                "shortwave_radiation",
                "direct_normal_irradiance",
                "diffuse_radiation",
                "temperature_2m",
                "wind_speed_10m",
                "cloud_cover",
                "cloud_cover_low",
                "cloud_cover_mid",
                "cloud_cover_high",
                "cape",
            ],
            # Query all 4 major global meteorological families
            "models": "icon_seamless,ecmwf_ifs025,gfs025,gem_global",
            "timezone": "Asia/Kolkata",
            "tilt": self.profile.tilt_deg,
            "azimuth": self.profile.azimuth_openmeteo,
        }
        if self.api_key:
            params["apikey"] = self.api_key

        url = f"{self.get_api_url()}?{urlencode(params, doseq=True)}"
        req = Request(url, headers={"User-Agent": "IntellisEnsembleGTIAI/2.0"})
        try:
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # Fallback to standard 2-family if full 4-family call times out
            params["models"] = "ecmwf_ifs025_ensemble,icon_seamless"
            url_fb = f"{self.get_api_url()}?{urlencode(params, doseq=True)}"
            req_fb = Request(url_fb, headers={"User-Agent": "IntellisEnsembleGTIAI/2.0"})
            with urlopen(req_fb, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

        return data

    # -------------------------------------------------------------------------
    # 2. Extract 96-Block GTI for a Given Model Member
    # -------------------------------------------------------------------------
    def extract_member_96block_gti(
        self,
        raw_weather: dict[str, Any],
        member_key: str,
        target_date_str: str,
    ) -> np.ndarray:
        """Extract or transpose 15-minute 96-block GTI for an ensemble member."""
        hourly = raw_weather.get("hourly", {})
        hourly_idx = np.arange(0, 24, 1.0)
        b_idx = np.arange(0, 24, 0.25)

        # Check native global_tilted_irradiance_instant first (exact instantaneous time, eliminates morning lag)
        clean_base = member_key.replace("global_tilted_irradiance_instant", "shortwave_radiation").replace("global_tilted_irradiance", "shortwave_radiation")
        gti_inst_k = clean_base.replace("shortwave_radiation", "global_tilted_irradiance_instant")
        if gti_inst_k in hourly and hourly[gti_inst_k]:
            h_vals = [float(v) if v is not None else 0.0 for v in hourly[gti_inst_k][:24]]
            return np.interp(b_idx, hourly_idx, h_vals)

        # Check native global_tilted_irradiance (interval average -> center by -0.5 hr to eliminate 30-minute lag)
        gti_k = clean_base.replace("shortwave_radiation", "global_tilted_irradiance")
        if gti_k in hourly and hourly[gti_k]:
            h_vals = [float(v) if v is not None else 0.0 for v in hourly[gti_k][:24]]
            centered_idx = np.arange(0, 24, 1.0) - 0.5
            centered_idx[0] = 0.0
            return np.interp(b_idx, centered_idx, h_vals)

        # Fallback to PVLib Transposition if DNI & GHI exist
        dni_k = member_key.replace("shortwave_radiation", "direct_normal_irradiance")
        sw_k = member_key
        if HAS_PVLIB and dni_k in hourly and sw_k in hourly:
            times = pd.date_range(f"{target_date_str} 00:00", f"{target_date_str} 23:45", freq="15min", tz="Asia/Kolkata")
            loc = Location(self.profile.latitude, self.profile.longitude, tz="Asia/Kolkata")
            sp = loc.get_solarposition(times)
            cos_zen = np.maximum(0.0, np.cos(np.radians(sp["apparent_zenith"].values)))

            sw_h = [float(v) if v is not None else 0.0 for v in hourly[sw_k][:24]]
            dni_h = [float(v) if v is not None else 0.0 for v in hourly[dni_k][:24]]

            g_96 = np.interp(b_idx, hourly_idx, sw_h)
            d_96 = np.interp(b_idx, hourly_idx, dni_h)
            dh_96 = np.maximum(0.0, g_96 - d_96 * cos_zen)

            poa = irradiance.get_total_irradiance(
                self.profile.tilt_deg,
                self.profile.azimuth_pvlib,
                sp["apparent_zenith"],
                sp["azimuth"],
                d_96,
                g_96,
                dh_96,
            )
            return poa["poa_global"].fillna(0.0).clip(lower=0.0).values

        # Flat interpolation fallback
        if sw_k in hourly:
            h_vals = [float(v) if v is not None else 0.0 for v in hourly[sw_k][:24]]
            return np.interp(b_idx, hourly_idx, h_vals)

        return np.zeros(96, dtype=float)

    # -------------------------------------------------------------------------
    # 3. Load SCADA Meter Actuals (AWS S3 or Local)
    # -------------------------------------------------------------------------
    def load_meter_actuals(self, target_date_str: str) -> np.ndarray:
        """Load 96-block generation (MW) from SCADA boundary meter files."""
        mw, _ = self.load_meter_actuals_with_poa(target_date_str)
        return mw

    def load_meter_actuals_with_poa(self, target_date_str: str) -> tuple[np.ndarray, np.ndarray]:
        """Load 96-block active generation (MW) and POA sensor irradiance (W/m²)."""
        p_name = self.profile.plant_name
        # For designated non-meter sites, use satellite solar radiation API as the virtual meter input
        if p_name.upper() in NON_METER_SITES:
            try:
                from modules.weather.satellite_virtual_meter import fetch_satellite_96block_profile
                mw_arr, poa_arr = fetch_satellite_96block_profile(
                    target_date=target_date_str,
                    latitude=self.profile.latitude,
                    longitude=self.profile.longitude,
                    tilt=self.profile.tilt_deg,
                    azimuth=self.profile.azimuth_openmeteo,
                    plant_capacity_mw=self.profile.ac_capacity_mw,
                    dc_capacity_mw=self.profile.dc_capacity_mw,
                    performance_ratio=getattr(self.profile, "calibrated_pr", 0.78),
                )
                return mw_arr, poa_arr
            except Exception as exc:
                print(f"  [WARN] Failed to fetch satellite virtual meter profile for {p_name} ({exc})")
                return np.zeros(96, dtype=float), np.zeros(96, dtype=float)

        aliases = [p_name, p_name.lower(), p_name.upper()]
        if p_name.upper() == "GSNP":
            aliases.extend(["GSPPL", "gsppl"])
        elif p_name.upper() == "GSPPL":
            aliases.extend(["GSNP", "gsnp"])
        elif "ANJANGAON" in p_name.upper():
            aliases.extend(["ANJANGOAN", "anjangoan"])
        elif "ANJANGOAN" in p_name.upper():
            aliases.extend(["ANJANGAON", "anjangaon"])

        date_compact = target_date_str.replace("-", "")
        candidates = []
        for folder_a in aliases:
            for file_a in aliases:
                candidates.extend([
                    _WORKSPACE_ROOT / f"s3_downloads/{folder_a}/{target_date_str}/raw/vedanjay/{folder_a}/{target_date_str}/metered_data/{file_a}_FORECAST_{target_date_str}.csv",
                    _WORKSPACE_ROOT / f"s3_downloads/{folder_a}/{target_date_str}/{file_a}_FORECAST_{target_date_str}.csv",
                    _WORKSPACE_ROOT / f"s3_downloads/{folder_a}/{target_date_str}/{file_a.lower()}_{date_compact}.csv",
                    _WORKSPACE_ROOT / f"s3_downloads/{folder_a}/{target_date_str}/{file_a.lower()}_{target_date_str}.csv",
                    _WORKSPACE_ROOT / f"daily_actuals_inbox/{folder_a}/{target_date_str}.csv",
                    _SCHEDULE_DIR / f"daily_actuals_inbox/{folder_a}/{target_date_str}.csv",
                    _WORKSPACE_ROOT / f"meter_history/{folder_a}_{target_date_str}.csv",
                    _SCHEDULE_DIR / f"meter_history/{folder_a}_{target_date_str}.csv",
                ])

        found_file: Path | None = None
        for p in candidates:
            if p.exists():
                found_file = p
                break

        if not found_file:
            for a in aliases:
                target_dir = _WORKSPACE_ROOT / f"s3_downloads/{a}/{target_date_str}"
                if target_dir.exists():
                    csvs = sorted(target_dir.glob("**/*.csv"))
                    # Prioritize files in metered_data or with forecast / meter / inv keywords
                    meter_csvs = [c for c in csvs if any(k in str(c).lower() for k in ["meter", "forecast", "inv", "mfm", "solar"])]
                    cand_list = meter_csvs + [c for c in csvs if c not in meter_csvs]
                    for c in cand_list:
                        try:
                            with open(c, "r", encoding="utf-8", errors="ignore") as f:
                                header = f.readline().lower()
                                if any(kw in header for kw in ["active", "power", "tvm", "mfm", "mw", "kw"]):
                                    found_file = c
                                    break
                        except Exception:
                            continue
                    if found_file:
                        break

            try:
                import boto3
                try:
                    session = boto3.Session(profile_name="intellis-608")
                    s3 = session.client("s3")
                except Exception:
                    s3 = boto3.client("s3")
                bucket = os.getenv("S3_BUCKET") or os.getenv("BUCKET") or "vedanjay-schedules-test-608744602858"

                # Check custom prefix from plant profile meter_data if configured
                s3_custom_prefix = self.profile.meter_data.get("s3_prefix") if self.profile.meter_data else None
                candidate_prefixes = []
                if s3_custom_prefix:
                    for a in aliases:
                        try:
                            candidate_prefixes.append(s3_custom_prefix.format(site_id=a, date=target_date_str))
                        except Exception:
                            pass
                for a in aliases:
                    candidate_prefixes.append(f"raw/vedanjay/{a}/{target_date_str}/metered_data/")
                    candidate_prefixes.append(f"raw/vedanjay/{a}/{target_date_str}/")

                for prefix in candidate_prefixes:
                    res = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
                    contents = res.get("Contents", [])
                    for obj in contents:
                        if obj["Key"].endswith(".csv"):
                            local_path = self.cache_dir / f"s3_meter_{self.profile.plant_name}_{target_date_str}.csv"
                            s3.download_file(bucket, obj["Key"], str(local_path))
                            found_file = local_path
                            break
                    if found_file:
                        break
            except Exception:
                pass

        if not found_file:
            return np.zeros(96, dtype=float), np.zeros(96, dtype=float)

        try:
            norm_df = load_and_normalize_meter_csv(found_file, meter_config=self.profile.meter_data)
            if norm_df.empty:
                return np.zeros(96, dtype=float), np.zeros(96, dtype=float)

            mw_arr = np.zeros(96, dtype=float)
            poa_arr = np.zeros(96, dtype=float)

            for _, row in norm_df.iterrows():
                b_idx = int(row["block"]) - 1
                if 0 <= b_idx < 96:
                    mw_arr[b_idx] = max(0.0, float(row["metered_mw"]))
                    poa_val = row["poa_wm2"]
                    if pd.notna(poa_val) and not math.isnan(float(poa_val)):
                        poa_arr[b_idx] = max(0.0, float(poa_val))

            if np.max(poa_arr) <= 0.0:
                # Astronomical clear-sky fallback when site has no physical on-site pyranometer sensor
                cs_poa = self.compute_clearsky_poa_96block(target_date_str)
                poa_arr = cs_poa.copy()

            return mw_arr, poa_arr
        except Exception:
            return np.zeros(96, dtype=float), np.zeros(96, dtype=float)

    def calibrate_plant_pr(self, target_date_str: str, lookback_days: int = 5) -> float:
        """
        Dynamically learn plant-specific STC Performance Ratio (PR) from historical meter telemetry.
        Inspects up to `lookback_days` before `target_date_str` to calculate robust ratio
        during high-irradiance daylight blocks by normalizing for cell temperature.
        Clamps within physical limits [0.78, 0.95].
        """
        if self.profile.plant_name.upper() in NON_METER_SITES:
            learned_pr = getattr(self.profile, "calibrated_pr", None) or getattr(config, "PERFORMANCE_RATIO", 0.78)
            self.profile.calibrated_pr = round(learned_pr, 4)
            self.profile.transfer_ratio = round((self.profile.dc_capacity_mw * self.profile.calibrated_pr) / 1000.0, 6)
            return self.profile.calibrated_pr

        target_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        valid_prs = []

        # Synthetic cell temperature factor for lookback normalization
        b_idx = np.arange(96)
        noon_dist = np.abs(b_idx - 48.5) * 0.25
        cos_elev = np.maximum(0.0, np.cos(noon_dist * (np.pi / 12.0)))
        est_cell = 25.0 + 10.0 * (cos_elev ** 0.8) + (cos_elev * 900.0 * 0.031)
        temp_factor = np.clip(1.0 - 0.0038 * (est_cell - 25.0), 0.82, 1.05)

        for offset in range(1, lookback_days + 1):
            day_str = (target_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
            d_mw, d_poa = self.load_meter_actuals_with_poa(day_str)
            if np.max(d_mw) < 0.10 * self.profile.ac_capacity_mw:
                continue

            mask = (d_poa >= 350.0) & (d_mw >= 0.15 * self.profile.ac_capacity_mw)
            if np.sum(mask) >= 4:
                # Normalize observed power by temperature factor to learn true STC PR
                pr_stc_vals = (d_mw[mask] * 1000.0) / (d_poa[mask] * self.profile.dc_capacity_mw * temp_factor[mask])
                valid_prs.extend(pr_stc_vals.tolist())

        if len(valid_prs) >= 6:
            # 70th percentile captures clear high-performance generation while filtering out cloud drops
            learned_pr = float(np.percentile(valid_prs, 70))
            learned_pr = max(0.78, min(0.95, learned_pr))
        else:
            # STC default (yields ~0.77-0.78 operating PR at noon under 0.88 temp_factor)
            learned_pr = getattr(self.profile, "calibrated_pr", None) or 0.8800

        self.profile.calibrated_pr = round(learned_pr, 4)
        self.profile.transfer_ratio = round((self.profile.dc_capacity_mw * self.profile.calibrated_pr) / 1000.0, 6)
        return self.profile.calibrated_pr

    # -------------------------------------------------------------------------
    # 4. Clearness Index (Kt) Regime Classifier & Atmospheric Cloud Metrics
    # -------------------------------------------------------------------------
    def get_cloud_and_atmospheric_metrics_96block(self, raw_weather: dict[str, Any]) -> dict[str, np.ndarray]:
        """
        Extract 96-block multi-model ensemble interpolated cloud, precipitation, and CAPE series.
        """
        hourly = raw_weather.get("hourly", {})
        hourly_idx = np.arange(0, 24, 1.0)
        b_idx = np.arange(0, 24, 0.25)

        # 1. Total Cloud Cover (%)
        cloud_tot_keys = [k for k in hourly.keys() if k == "cloud_cover" or (k.startswith("cloud_cover_") and not any(k.startswith(f"cloud_cover_{layer}") for layer in ["low", "mid", "high"]))]
        tot_matrix = [[float(v) if v is not None else 0.0 for v in hourly[k][:24]] for k in cloud_tot_keys if hourly[k] and len(hourly[k]) >= 24]
        mean_tot_h = np.mean(tot_matrix, axis=0) if tot_matrix else np.zeros(24)
        tot_cloud_96 = np.clip(np.interp(b_idx, hourly_idx, mean_tot_h), 0.0, 100.0)

        # 2. Low Cloud Cover (%) - thickest optical depth (stratus, nimbostratus)
        cloud_low_keys = [k for k in hourly.keys() if "cloud_cover_low" in k]
        low_matrix = [[float(v) if v is not None else 0.0 for v in hourly[k][:24]] for k in cloud_low_keys if hourly[k] and len(hourly[k]) >= 24]
        mean_low_h = np.mean(low_matrix, axis=0) if low_matrix else np.zeros(24)
        low_cloud_96 = np.clip(np.interp(b_idx, hourly_idx, mean_low_h), 0.0, 100.0)

        # 3. Precipitation (mm/hr)
        precip_keys = [k for k in hourly.keys() if "precipitation" in k]
        precip_matrix = [[float(v) if v is not None else 0.0 for v in hourly[k][:24]] for k in precip_keys if hourly[k] and len(hourly[k]) >= 24]
        mean_precip_h = np.mean(precip_matrix, axis=0) if precip_matrix else np.zeros(24)
        precip_96 = np.maximum(0.0, np.interp(b_idx, hourly_idx, mean_precip_h))

        return {
            "tot_cloud_96": tot_cloud_96,
            "low_cloud_96": low_cloud_96,
            "precip_96": precip_96,
        }

    def classify_weather_regime(
        self,
        gti_daylight_wm2: float,
        cs_daylight_wm2: float,
        mean_cloud_cover_pct: float | None = None,
        mean_low_cloud_pct: float | None = None,
        mean_precip_mm: float | None = None,
    ) -> str:
        """Classify daily atmospheric condition into OVERCAST, MIXED, or CLEAR."""
        # Strong physical cloud indicators override raw NWP diffuse inflation
        if mean_cloud_cover_pct is not None and mean_cloud_cover_pct >= 80.0:
            return "OVERCAST"
        if mean_low_cloud_pct is not None and mean_low_cloud_pct >= 45.0:
            return "OVERCAST"
        if mean_precip_mm is not None and mean_precip_mm > 0.10:
            return "OVERCAST"

        if cs_daylight_wm2 <= 0.0:
            return "MIXED"
        kt = gti_daylight_wm2 / cs_daylight_wm2
        if kt < 0.50:
            return "OVERCAST"
        elif kt >= 0.75 and (mean_cloud_cover_pct is None or mean_cloud_cover_pct <= 25.0):
            return "CLEAR"
        return "MIXED"

    # -------------------------------------------------------------------------
    # 5. Astronomical Clear-Sky POA Calculation
    # -------------------------------------------------------------------------
    def compute_clearsky_poa_96block(self, target_date_str: str) -> np.ndarray:
        """
        Compute theoretical 96-block Plane-of-Array (POA) Clear-Sky irradiance (W/m²).
        Uses PVLib Ineichen/Haurwitz clear sky with plant tilt & azimuth if available,
        with robust physical astronomical elevation fallback.
        """
        if HAS_PVLIB:
            try:
                times = pd.date_range(f"{target_date_str} 00:00", f"{target_date_str} 23:45", freq="15min", tz="Asia/Kolkata")
                loc = Location(self.profile.latitude, self.profile.longitude, tz="Asia/Kolkata")
                sp = loc.get_solarposition(times)
                cs = loc.get_clearsky(times)
                poa = irradiance.get_total_irradiance(
                    self.profile.tilt_deg,
                    self.profile.azimuth_pvlib,
                    sp["apparent_zenith"],
                    sp["azimuth"],
                    cs["dni"],
                    cs["ghi"],
                    cs["dhi"],
                )
                poa_vals = poa["poa_global"].fillna(0.0).clip(lower=0.0).values
                if np.max(poa_vals) > 400.0:
                    return poa_vals
            except Exception:
                pass

        # Robust physical solar geometry fallback
        b_idx = np.arange(96)
        noon_dist = np.abs(b_idx - 48.5) * 0.25
        cos_elev = np.maximum(0.0, np.cos(noon_dist * (np.pi / 12.0)))
        cs_poa = np.where((b_idx >= 24) & (b_idx <= 75), 980.0 * (cos_elev ** 1.15), 0.0)
        return cs_poa

    # -------------------------------------------------------------------------
    # 6. Multi-Agency Diverse Model Selection per Slot
    # -------------------------------------------------------------------------
    DIURNAL_SLOTS = {
        "morning": (24, 40),    # Blocks 25 to 40 (06:00 to 10:00)
        "midday": (40, 56),     # Blocks 41 to 56 (10:00 to 14:00)
        "afternoon": (56, 75),  # Blocks 57 to 75 (14:00 to 18:45)
    }

    def benchmark_and_select_models(
        self,
        target_date_str: str,
        lookback_days: int = 7,
        icon_quota: int = 2,
        ecmwf_quota: int = 2,
        gefs_quota: int = 2,
        tau_decay_days: float = 3.5,
        use_regime_matching: bool = True,
        block_range: tuple[int, int] | None = None,
        return_calibration_gains: bool = False,
    ) -> tuple[list[str], dict[str, float], pd.DataFrame] | tuple[list[str], dict[str, float], pd.DataFrame, dict[str, float]]:
        """
        Evaluate candidate models over lookback days against actual SCADA meter POA.
        Can evaluate over full day or a specific diurnal slot (morning/midday/afternoon).
        Applies:
        - Exponential decay weighting: w_d = exp(-offset / tau).
        - Multi-family stratified regime matching.
        - Asymmetric DSM shortfall loss.
        - Capped Family Diversity Quota: Min 1 from ECMWF, ICON, GEFS; max 3 per agency.
        - Empirical Diurnal MOS linear calibration gains.
        """
        target_dt = dt.date.fromisoformat(target_date_str)
        eval_dates = [(target_dt - dt.timedelta(days=i)).isoformat() for i in range(1, lookback_days + 1)]

        today_weather = self.fetch_ensemble_weather(target_date_str)
        hourly_today = today_weather.get("hourly", {})
        
        b_start, b_end = block_range if block_range else (24, 76)

        cs_poa_96 = self.compute_clearsky_poa_96block(target_date_str)
        cs_daylight_sum = float(np.sum(cs_poa_96[b_start:b_end]))

        all_member_keys = [
            k for k in hourly_today.keys()
            if k.startswith("shortwave_radiation") or k.startswith("global_tilted_irradiance")
        ]
        canonical_keys = sorted(list(set([
            k.replace("global_tilted_irradiance_instant", "shortwave_radiation").replace("global_tilted_irradiance", "shortwave_radiation")
            for k in all_member_keys
        ])))

        # Multi-family stratified sampling across all 4 major agencies (5 ICON + 5 ECMWF + 5 GEFS + 5 GEM)
        sample_keys = []
        for fam in ["icon", "ecmwf", "gefs", "gem"]:
            f_keys = [k for k in canonical_keys if fam in k.lower()]
            sample_keys.extend(f_keys[:5])
        if not sample_keys:
            sample_keys = canonical_keys[:20]

        # Extract atmospheric indicators for today to robustly detect overcast / cloud fronts
        cloud_metrics_today = self.get_cloud_and_atmospheric_metrics_96block(today_weather)
        tot_c_day = float(np.mean(cloud_metrics_today["tot_cloud_96"][b_start:b_end])) if len(cloud_metrics_today["tot_cloud_96"]) > b_end else 0.0
        low_c_day = float(np.mean(cloud_metrics_today["low_cloud_96"][b_start:b_end])) if len(cloud_metrics_today["low_cloud_96"]) > b_end else 0.0
        prec_day = float(np.mean(cloud_metrics_today["precip_96"][b_start:b_end])) if len(cloud_metrics_today["precip_96"]) > b_end else 0.0

        today_prelim_gti = [self.extract_member_96block_gti(today_weather, k, target_date_str) for k in sample_keys]
        # Use robust multi-family median across ensemble members
        today_mean_gti_sum = float(np.sum(np.median(today_prelim_gti, axis=0)[b_start:b_end])) if today_prelim_gti else 0.0
        today_regime = self.classify_weather_regime(
            today_mean_gti_sum,
            cs_daylight_sum,
            mean_cloud_cover_pct=tot_c_day,
            mean_low_cloud_pct=low_c_day,
            mean_precip_mm=prec_day,
        )

        model_weighted_mse: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_weighted_bias: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_weighted_corr: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_weight_sums: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_sim_sq: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_cross: dict[str, float] = {k: 0.0 for k in canonical_keys}

        valid_days_evaluated = []
        for offset, d_str in enumerate(eval_dates, start=1):
            meter_mw, meter_poa = self.load_meter_actuals_with_poa(d_str)
            if np.max(meter_mw) < 0.5 and np.max(meter_poa) < 50.0:
                continue

            if np.max(meter_poa) <= 0.0:
                meter_poa = self.compute_clearsky_poa_96block(d_str)

            day_meter_sum = float(np.sum(meter_poa[b_start:b_end]))
            day_regime = self.classify_weather_regime(day_meter_sum, cs_daylight_sum)

            w_d = math.exp(-offset / max(0.5, tau_decay_days))
            if use_regime_matching:
                if today_regime == "OVERCAST":
                    if day_regime == "OVERCAST":
                        w_d *= 3.5  # Strong boost for genuine overcast actuals
                    elif day_regime == "MIXED":
                        w_d *= 1.2
                    else:  # CLEAR
                        w_d *= 0.15 # Heavily suppress sunny lookback days from biasing overcast forecast
                elif today_regime == "CLEAR":
                    if day_regime == "CLEAR":
                        w_d *= 2.5
                    elif day_regime == "MIXED":
                        w_d *= 0.8
                    else:  # OVERCAST
                        w_d *= 0.15
                else:  # MIXED
                    if day_regime == "MIXED":
                        w_d *= 1.8
                    else:
                        w_d *= 0.7

            try:
                weather_d = self.fetch_ensemble_weather(d_str)
            except Exception:
                continue

            # Verify that weather_d has valid non-zero data
            test_h = weather_d.get("hourly", {})
            sample_val = None
            for tk in canonical_keys[:3]:
                if tk in test_h and test_h[tk] and any(v is not None and v > 0 for v in test_h[tk]):
                    sample_val = True
                    break
            if not sample_val:
                continue

            valid_days_evaluated.append(d_str)

            for k in canonical_keys:
                gti_96 = self.extract_member_96block_gti(weather_d, k, d_str)
                errs = meter_poa[b_start:b_end] - gti_96[b_start:b_end]
                mse = float(np.mean(errs ** 2))
                bias = float(np.mean(errs))

                # Diurnal shape Pearson correlation
                s_poa = float(np.std(meter_poa[b_start:b_end]))
                s_sim = float(np.std(gti_96[b_start:b_end]))
                if s_poa > 1e-3 and s_sim > 1e-3:
                    corr_val = float(np.corrcoef(meter_poa[b_start:b_end], gti_96[b_start:b_end])[0, 1])
                else:
                    corr_val = 0.5

                model_weighted_mse[k] += w_d * mse
                model_weighted_bias[k] += w_d * bias
                model_weighted_corr[k] += w_d * max(0.0, corr_val)
                model_weight_sums[k] += w_d

                sim_sub = gti_96[b_start:b_end]
                act_sub = meter_poa[b_start:b_end]
                model_sim_sq[k] += float(np.sum(sim_sub ** 2))
                model_cross[k] += float(np.sum(act_sub * sim_sub))

        records = []
        for k in canonical_keys:
            if model_weight_sums[k] <= 0.0:
                continue
            rmse_poa = math.sqrt(model_weighted_mse[k] / model_weight_sums[k])
            raw_bias_poa = model_weighted_bias[k] / model_weight_sums[k]
            bias_poa = abs(raw_bias_poa)
            corr_poa = model_weighted_corr[k] / model_weight_sums[k]
            rmse_mw = rmse_poa * self.profile.transfer_ratio
            bias_mw = bias_poa * self.profile.transfer_ratio

            # Regulatory Asymmetric DSM Loss:
            # Over-forecasting (sim > meter => raw_bias < 0) triggers severe under-generation shortfall penalty (up to 2x PPA).
            # Under-forecasting (sim < meter => raw_bias > 0) injection has zero penalty in the tolerance band.
            shortfall_risk = max(0.0, -raw_bias_poa) * 0.50
            composite_loss = (rmse_poa + 0.40 * bias_poa + shortfall_risk + 0.15 * (1.0 - corr_poa) * 100.0) * self.profile.transfer_ratio

            k_lower = k.lower()
            if "icon" in k_lower:
                family = "ICON"
                label = f"ICON #{k.split('_')[-1]}"
            elif "ecmwf" in k_lower or "ifs" in k_lower:
                family = "ECMWF"
                label = f"ECMWF #{k.split('_')[-1]}"
            elif "gfs" in k_lower or "gefs" in k_lower:
                family = "GEFS"
                label = f"GEFS #{k.split('_')[-1]}"
            elif "gem" in k_lower:
                family = "GEM"
                label = f"GEM #{k.split('_')[-1]}"
            else:
                family = "OTHER"
                label = k

            records.append({
                "key": k,
                "label": label,
                "family": family,
                "rmse_poa_wm2": round(rmse_poa, 2),
                "bias_poa_wm2": round(bias_poa, 2),
                "corr_poa": round(corr_poa, 3),
                "rmse_mw": round(rmse_mw, 3),
                "bias_mw": round(bias_mw, 3),
                "composite_loss": round(composite_loss, 4),
            })

        if not records:
            # High-accuracy calibrated multi-family defaults
            default_keys = [
                "shortwave_radiation_member05_icon_seamless_eps",
                "shortwave_radiation_member13_icon_seamless_eps",
                "shortwave_radiation_member09_ecmwf_ifs025_ensemble",
                "shortwave_radiation_member48_ecmwf_ifs025_ensemble",
                "shortwave_radiation_member03_ncep_gefs025",
                "shortwave_radiation_member05_ncep_gefs025",
            ]
            avail = [k for k in default_keys if any(k in hk for hk in hourly_today.keys())]
            if not avail:
                avail = canonical_keys[:6]
            if return_calibration_gains:
                return avail, {k: round(1.0 / len(avail), 4) for k in avail}, pd.DataFrame(), {k: 1.0 for k in avail}
            return avail, {k: round(1.0 / len(avail), 4) for k in avail}, pd.DataFrame()

        df_rank = pd.DataFrame(records).sort_values("composite_loss")

        # Capped Family Diversity Selection with Outlier Agency Rejection
        best_global_loss = df_rank.iloc[0]["composite_loss"] if not df_rank.empty else 1.0
        floor_models = []
        for fam in ["ECMWF", "ICON", "GEFS"]:
            sub = df_rank[df_rank["family"] == fam]
            if not sub.empty:
                fam_best = sub.iloc[0]
                # Reject outlier family if its best member is > 2.2x worse than best global model
                if fam_best["composite_loss"] <= (best_global_loss * 2.2):
                    floor_models.append(fam_best)
                else:
                    print(f"  [OUTLIER REJECTED] Agency {fam} excluded from mandatory floor (loss {fam_best['composite_loss']} > 2.2 * {best_global_loss:.4f})")

        selected_df = pd.DataFrame(floor_models) if floor_models else pd.DataFrame([df_rank.iloc[0]])
        used_keys = set(selected_df["key"].tolist())
        fam_counts = selected_df["family"].value_counts().to_dict()

        # Fill remaining slots up to 6 with lowest composite_loss models where fam_count < 3
        remaining_candidates = df_rank[~df_rank["key"].isin(used_keys)].sort_values("composite_loss")
        for _, row in remaining_candidates.iterrows():
            if len(selected_df) >= 6:
                break
            fam = row["family"]
            if fam_counts.get(fam, 0) < 3:
                selected_df = pd.concat([selected_df, pd.DataFrame([row])])
                fam_counts[fam] = fam_counts.get(fam, 0) + 1

        selected_df = selected_df.sort_values("composite_loss")
        selected_keys = selected_df["key"].tolist()

        score_col = "composite_loss" if "composite_loss" in selected_df.columns else "rmse_mw"
        raw_w = [1.0 / max(1e-4, row[score_col]) for _, row in selected_df.iterrows()]
        norm_w = [round(w / sum(raw_w), 4) for w in raw_w]
        weights_map = {k: w for k, w in zip(selected_keys, norm_w)}

        # Compute empirical Diurnal MOS Linear Calibration Gain (alpha_k) in single pass
        calibration_gains = {}
        for k in selected_keys:
            if model_sim_sq.get(k, 0.0) > 1e-4:
                alpha = float(model_cross[k] / model_sim_sq[k])
                alpha = float(np.clip(alpha, 0.85, 1.15))
            else:
                alpha = 1.0
            calibration_gains[k] = round(alpha, 4)

        if return_calibration_gains:
            return selected_keys, weights_map, df_rank, calibration_gains
        return selected_keys, weights_map, df_rank

    def benchmark_and_select_slot_models(
        self,
        target_date_str: str,
        lookback_days: int = 5,
    ) -> dict[str, tuple[list[str], dict[str, float], dict[str, float]]]:
        """Benchmark and select top models independently for each diurnal time slot (Capped Diversity per slot)."""
        slot_results = {}
        for slot_name, slot_range in self.DIURNAL_SLOTS.items():
            keys, weights, _, gains = self.benchmark_and_select_models(
                target_date_str,
                lookback_days=lookback_days,
                block_range=slot_range,
                return_calibration_gains=True,
            )
            slot_results[slot_name] = (keys, weights, gains)
        return slot_results

    def get_slot_candidate_diagnostics(
        self,
        target_date_str: str,
        current_block: int,
        live_actual_poa: float,
        lookback_blocks: int = 4,
        horizon_blocks: int = 12,
    ) -> dict[str, Any]:
        """
        Extract top-performing candidate models for the current diurnal slot along with
        their real-time tracking error (last 60 minutes) against live SCADA meter ground truth.
        Used by the LLM via OpenRouter for dynamic model consensus arbitration.
        """
        if current_block < 24 or current_block > 75:
            slot_name = "night"
        elif current_block <= 40:
            slot_name = "morning"
        elif current_block <= 56:
            slot_name = "midday"
        else:
            slot_name = "afternoon"

        slot_selections = self.benchmark_and_select_slot_models(target_date_str)
        slot_data = slot_selections.get(slot_name, ([], {}, {}))
        slot_keys = slot_data[0]
        base_weights = slot_data[1]

        weather = self.fetch_ensemble_weather(target_date_str)
        cs_poa = self.compute_clearsky_poa_96block(target_date_str)

        start_eval = max(0, current_block - lookback_blocks)
        end_eval = current_block

        candidates = []
        for k in slot_keys:
            m_gti_96 = self.extract_member_96block_gti(weather, k, target_date_str)
            bias_val = round(float(m_gti_96[current_block]) - live_actual_poa, 1) if (0 <= current_block < 96) else 0.0

            h_end = min(96, current_block + horizon_blocks)
            future_gti = [round(float(v), 1) for v in m_gti_96[current_block:h_end]]

            k_lower = k.lower()
            if "icon" in k_lower:
                family = "ICON"
            elif "ecmwf" in k_lower or "ifs" in k_lower:
                family = "ECMWF"
            elif "gefs" in k_lower or "gfs" in k_lower:
                family = "GEFS"
            else:
                family = "GEM"

            candidates.append({
                "model_key": k,
                "family": family,
                "base_weight": base_weights.get(k, 0.20),
                "last_60min_bias_wm2": bias_val,
                "forecast_gti_next_blocks": future_gti,
            })

        return {
            "current_slot": slot_name,
            "current_block": current_block,
            "live_actual_poa_wm2": round(live_actual_poa, 1),
            "clearsky_poa_wm2": round(float(cs_poa[current_block]), 1) if current_block < 96 else 0.0,
            "candidates": candidates,
        }

    # -------------------------------------------------------------------------
    # 7. Diurnal 3-Slot Clearness Index (Kt) Blending
    # -------------------------------------------------------------------------
    def predict_96block_schedule(
        self,
        target_date_str: str,
        selected_keys: list[str] | None = None,
        weights_map: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """
        Generate 96-block GTI and scheduled MW using Diurnal 3-Slot Model Selection
        and Phase-Resolved Clearness Index (Kt) physical decomposition.
        """
        if getattr(self.profile, "calibrated_pr", None) is None:
            self.calibrate_plant_pr(target_date_str)

        site_upper = (self.profile.plant_name or "").upper()
        if site_upper in NON_METER_SITES:
            from modules.weather.satellite_virtual_meter import fetch_satellite_day_profile
            try:
                target_d = dt.date.fromisoformat(target_date_str)
            except Exception:
                target_d = dt.date.today()
            sat_hourly = fetch_satellite_day_profile(
                target_date=target_d,
                latitude=self.profile.latitude,
                longitude=self.profile.longitude,
                tilt=self.profile.tilt_deg,
                azimuth=self.profile.azimuth_openmeteo,
            )
            times = sat_hourly.get("time", [])
            gtis = sat_hourly.get("global_tilted_irradiance", [])
            hour_gti = {int(t.split("T")[1].split(":")[0]): float(g) for t, g in zip(times, gtis) if "T" in t}

            fused_gti = np.zeros(96, dtype=float)
            pred_mw = np.zeros(96, dtype=float)
            tr = self.profile.transfer_ratio

            for b in range(1, 97):
                if b < 24 or b > 75:
                    continue
                mid_min = (b - 0.5) * 15
                h = int(mid_min // 60)
                frac = (mid_min % 60) / 60.0
                curr_g = hour_gti.get(h, 0.0)
                nxt_g = hour_gti.get(min(23, h + 1), curr_g)
                g_interp = max(0.0, curr_g + frac * (nxt_g - curr_g))
                fused_gti[b - 1] = round(g_interp, 1)
                pred_mw[b - 1] = round(min(self.profile.ac_capacity_mw, g_interp * tr), 3)

            meter_mw = np.zeros(96, dtype=float)
        else:
            weather = self.fetch_ensemble_weather(target_date_str)
            meter_mw = self.load_meter_actuals(target_date_str)
            cs_poa = self.compute_clearsky_poa_96block(target_date_str)

            if not selected_keys or not weights_map:
                slot_selections = self.benchmark_and_select_slot_models(target_date_str)
                all_selected = []
                all_weights = {}
                for s_name, slot_data in slot_selections.items():
                    s_keys = slot_data[0]
                    s_w = slot_data[1]
                    all_selected.extend(s_keys)
                    all_weights.update(s_w)
                selected_keys = list(dict.fromkeys(all_selected))
                weights_map = {k: all_weights.get(k, round(1.0 / len(selected_keys), 4)) for k in selected_keys}
                w_sum = sum(weights_map.values())
                weights_map = {k: round(v / w_sum, 4) for k, v in weights_map.items()}
            else:
                slot_selections = {
                    "morning": (selected_keys, weights_map),
                    "midday": (selected_keys, weights_map),
                    "afternoon": (selected_keys, weights_map),
                }

            # Physical Cloud Optical Attenuation Ceiling (Kasten-Czeplak formulation)
            cloud_metrics = self.get_cloud_and_atmospheric_metrics_96block(weather)
            tot_cloud = cloud_metrics["tot_cloud_96"]
            low_cloud = cloud_metrics["low_cloud_96"]
            precip = cloud_metrics["precip_96"]

            t_cloud = 1.0 - 0.75 * ((tot_cloud / 100.0) ** 3.4)
            eta_low = 1.0 - 0.50 * (low_cloud / 100.0)
            eta_rain = np.where(precip > 0.5, 0.50, np.where(precip > 0.05, 0.70, 1.0))
            cloud_cap = cs_poa * t_cloud * eta_low * eta_rain

            # Compute Clearness Index (Kt) array for each slot
            kt_slots = {}
            for s_name, slot_data in slot_selections.items():
                s_keys = slot_data[0]
                s_w = slot_data[1]
                s_gains = slot_data[2] if len(slot_data) > 2 else {}
                slot_kt = np.zeros(96, dtype=float)
                total_w = sum(s_w.values()) if s_w else 1.0
                for k in s_keys:
                    w_k = s_w.get(k, 1.0 / max(1, len(s_keys))) / total_w
                    gain_k = s_gains.get(k, 1.0)
                    m_gti = self.extract_member_96block_gti(weather, k, target_date_str) * gain_k
                    # Physically cap inflated diffuse irradiance under overcast / thick cloud regimes
                    m_gti = np.minimum(m_gti, np.maximum(35.0, cloud_cap))
                    denom = np.maximum(15.0, cs_poa)
                    m_kt = np.clip(m_gti / denom, 0.0, 1.15)
                    slot_kt += w_k * m_kt
                kt_slots[s_name] = slot_kt

            # Smooth cosine spline blending across diurnal slots
            blended_kt = np.zeros(96, dtype=float)
            for b in range(96):
                if b < 24 or b >= 76:
                    blended_kt[b] = 0.0
                elif b < 38:
                    blended_kt[b] = kt_slots["morning"][b]
                elif b <= 42:
                    # Transition Morning -> Midday (Blocks 39 to 43)
                    alpha = 0.5 * (1.0 - math.cos(math.pi * (b - 38) / 4.0))
                    blended_kt[b] = (1.0 - alpha) * kt_slots["morning"][b] + alpha * kt_slots["midday"][b]
                elif b < 54:
                    blended_kt[b] = kt_slots["midday"][b]
                elif b <= 58:
                    # Transition Midday -> Afternoon (Blocks 55 to 59)
                    alpha = 0.5 * (1.0 - math.cos(math.pi * (b - 54) / 4.0))
                    blended_kt[b] = (1.0 - alpha) * kt_slots["midday"][b] + alpha * kt_slots["afternoon"][b]
                else:
                    blended_kt[b] = kt_slots["afternoon"][b]

            # Re-synthesize fused GTI from physical Clearness Index and astronomical Clear-Sky curve
            fused_gti = np.round(blended_kt * cs_poa, 1)
            fused_gti[:23] = 0.0
            fused_gti[76:] = 0.0
            fused_gti = np.maximum(0.0, fused_gti)

            # Ambient temperature & Wind speed convective derating from Open-Meteo Premium
            hourly = weather.get("hourly", {})
            if "temperature_2m" in hourly and hourly["temperature_2m"]:
                h_t = [float(v) if v is not None else 28.0 for v in hourly["temperature_2m"][:24]]
                amb_temp = np.interp(np.arange(0, 24, 0.25), np.arange(0, 24, 1.0), h_t)
            else:
                amb_temp = 25.0 + 10.0 * np.sin(np.pi * np.maximum(0, np.arange(96) - 24) / 56.0)

            if "wind_speed_10m" in hourly and hourly["wind_speed_10m"]:
                h_ws = [float(v) if v is not None else 2.5 for v in hourly["wind_speed_10m"][:24]]
                wind_speed = np.interp(np.arange(0, 24, 0.25), np.arange(0, 24, 1.0), h_ws)
            else:
                wind_speed = np.full(96, 2.5)

            # Faiman / Sandia convective module temperature model
            # Tcell = Tamb + GTI / (u0 + u1 * WindSpeed), where u0=25.0, u1=1.2 for open-rack modules
            cell_temp = amb_temp + fused_gti / (25.0 + 1.2 * wind_speed)
            temp_factor = np.clip(1.0 - 0.0038 * (cell_temp - 25.0), 0.82, 1.06)

            # Predicted MW = GTI * transfer_ratio * temp_factor clipped to AC capacity
            pred_mw = np.round(np.clip(fused_gti * self.profile.transfer_ratio * temp_factor, 0.0, self.profile.ac_capacity_mw), 2)
            pred_mw[:23] = 0.0
            pred_mw[76:] = 0.0

        # Calculate DSM Penalties against meter actuals if available
        blocks_data = []
        tot_pen = 0.0
        safe_count = 0
        has_actuals = np.max(meter_mw) > 0.1

        for b in range(96):
            end_minutes = (b + 1) * 15
            start_minutes = b * 15
            s_hr, s_min = divmod(start_minutes, 60)
            e_hr, e_min = divmod(end_minutes, 60)
            if e_hr == 24:
                t_str = "00:00"
                t_interval = f"{s_hr:02d}:{s_min:02d} - 24:00"
            else:
                t_str = f"{e_hr:02d}:{e_min:02d}"
                t_interval = f"{s_hr:02d}:{s_min:02d} - {e_hr:02d}:{e_min:02d}"

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

    # -------------------------------------------------------------------------
    # 8. Real-Time Intraday SCADA Telemetry Relaxation (T+4 Nudge)
    # -------------------------------------------------------------------------
    def apply_intraday_scada_feedback(
        self,
        schedule_result: dict[str, Any],
        current_block: int,
        live_meter_mw: float,
        tau_blocks: float = 3.5,
        recent_meter_mw_list: list[float] | None = None,
        live_pyranometer_poa: float | None = None,
    ) -> dict[str, Any]:
        """
        Closed-loop physical relaxation filter for intraday schedule revisions.
        Anchors forward blocks smoothly using continuous Clearness Index (Kt)
        telemetry without discrete binary switches, aligned with regulatory lag.
        Incorporates direct on-site SCADA pyranometer POA telemetry when present.
        """
        curr_idx = current_block - 1
        if curr_idx < 0 or curr_idx >= 96:
            return schedule_result

        target_date_str = schedule_result.get("target_date", dt.date.today().isoformat())
        cs_poa = self.compute_clearsky_poa_96block(target_date_str)

        site_upper = (self.profile.plant_name or "").upper()
        is_90min_site = (
            self.profile.penalty_regulation == "Madhya Pradesh"
            or site_upper in {
                "SIRMOUR", "ANJANGOAN", "ANJANGAON", "ANDAD", "BALAKWADA",
                "BAMKHAL", "CHANDAWASA", "GSNP", "GSPPL", "GUGARIYAKHEDI", "NANDGAON", "SAWDA", "REWASPRNG", "REWASEIT"
            }
        )
        freeze_lag_blocks = 6 if is_90min_site else 3
        actionable_block = current_block + freeze_lag_blocks + 1

        # Theoretical clear-sky power for the plant at current block (bounded by AC inverter limit)
        cs_theoretical = cs_poa[curr_idx] * self.profile.transfer_ratio
        cs_curr_mw = min(self.profile.ac_capacity_mw, cs_theoretical)
        curr_cs_poa = cs_poa[curr_idx]

        # Pyranometer-derived ground-truth clearness index
        kt_poa: float | None = None
        if live_pyranometer_poa is not None and live_pyranometer_poa > 20.0 and curr_cs_poa > 30.0:
            kt_poa = max(0.15, min(1.10, live_pyranometer_poa / curr_cs_poa))

        # Early morning inverter wakeup guardrail:
        # Extend to block < 30 (before 07:30 IST) to protect the 90-min freeze zone for MP sites.
        # The 06:45 revision (block 27) and 08:15 revision (block 33) would otherwise propagate
        # a low morning Kt into blocks 37-44 which gets frozen before it can be corrected.
        morning_guardrail_limit = 30 if is_90min_site else 28
        if current_block < morning_guardrail_limit or cs_curr_mw < (0.05 * self.profile.ac_capacity_mw):
            if kt_poa is not None and current_block >= 25:
                # Use ground truth pyranometer instead of static 0.85 assumption
                kt_obs = kt_poa
            else:
                kt_obs = 0.85
        elif live_meter_mw >= (0.88 * self.profile.ac_capacity_mw):
            # Inverter clipping ceiling: plant generating at max inverter rating
            kt_obs = 1.00
        elif cs_curr_mw > 0.3:
            kt_from_meter = max(0.15, min(1.05, live_meter_mw / cs_curr_mw))
            if kt_poa is not None:
                # Ground-truth pyranometer blend (75% POA sensor, 25% electrical generation)
                kt_obs = 0.75 * kt_poa + 0.25 * kt_from_meter
            else:
                kt_obs = kt_from_meter
        else:
            if kt_poa is not None:
                kt_obs = kt_poa
            else:
                kt_obs = 0.85 if live_meter_mw > 0.2 else 0.40

        # Anti-Trench 60-min Rolling Window: Use rolling median of recent meter values
        # to filter out transient cloud shadow dips (< 15-30 min)
        if recent_meter_mw_list and len(recent_meter_mw_list) >= 2:
            kts = []
            lookback_blocks = min(len(recent_meter_mw_list), 4)  # 60 minutes
            for offset in range(lookback_blocks):
                hist_idx = curr_idx - offset
                hist_mw = recent_meter_mw_list[-1 - offset]
                if hist_idx >= 0:
                    hist_cs = min(self.profile.ac_capacity_mw, cs_poa[hist_idx] * self.profile.transfer_ratio)
                    if hist_cs > 0.3:
                        if hist_mw >= (0.88 * self.profile.ac_capacity_mw):
                            kts.append(1.00)
                        else:
                            kts.append(max(0.15, min(1.05, hist_mw / hist_cs)))
            if kts:
                kt_obs = float(np.median(kts))

        # Pre-Freeze Telemetry Momentum Trend Extrapolation (dKt/dt)
        # Catches abrupt cloud fronts / sudden clearing 30-45 minutes ahead of gate closure
        trend_momentum = 0.0
        if recent_meter_mw_list and len(recent_meter_mw_list) >= 2 and cs_curr_mw > 0.3:
            prev_idx = max(0, curr_idx - 1)
            prev_cs = cs_poa[prev_idx] * self.profile.transfer_ratio
            if prev_cs > 0.3:
                prev_kt = recent_meter_mw_list[-2] / prev_cs
                curr_kt = live_meter_mw / cs_curr_mw
                d_kt = curr_kt - prev_kt
                # Extrapolate momentum across gate-closure horizon (bounded within safe limits)
                trend_momentum = max(-0.25, min(0.20, d_kt * 1.5))

        updated_blocks = []
        tot_pen = 0.0
        safe_count = 0

        for b_dict in schedule_result["blocks"]:
            b_idx = b_dict["block"] - 1
            if b_idx < (actionable_block - 1):
                # Past or frozen gate closure blocks remain unchanged
                b_copy = dict(b_dict)
                updated_blocks.append(b_copy)
                if b_copy.get("dsm_slab") == "0% Safe":
                    safe_count += 1
                tot_pen += b_copy.get("block_penalty_inr", 0.0)
            else:
                # Actionable dispatch horizon architecture:
                # 1. Blocks 1-2 (immediate 30 min, delta_blocks 0 & 1): Meter-Adjusted to instantly catch equipment trips and grid curtailments.
                # 2. Block 3 (30-45 min, delta_blocks 2): Smooth transition bridge (50% Meter, 50% Pure Weather).
                # 3. Block 4+ (beyond 45 min, delta_blocks >= 3): 100% Pure Weather, anchored strictly to meteorological physics.
                delta_blocks = b_idx - (actionable_block - 1)

                fcst_gti = b_dict["predicted_gti_wm2"]
                fcst_kt = min(1.05, fcst_gti / max(15.0, cs_poa[b_idx]))

                if delta_blocks == 0:
                    # Block 1 (+00 to +15 min): 100% Meter-Adjusted telemetry
                    eff_kt = max(0.15, min(1.05, kt_obs + trend_momentum))
                    target_poa = eff_kt * cs_poa[b_idx]
                    b_hr = (b_dict["block"] * 15) // 60
                    amb_t = 28.0 + (4.0 if 11 <= b_hr <= 15 else 0.0)
                    cell_t = amb_t + target_poa * 0.031
                    temp_factor = np.clip(1.0 - 0.004 * (cell_t - 25.0), 0.82, 1.06)
                    raw_mw = target_poa * self.profile.transfer_ratio * temp_factor
                    adj_mw = min(self.profile.ac_capacity_mw, max(0.0, raw_mw))
                    adj_mw = round(adj_mw, 2)
                elif delta_blocks == 1:
                    # Block 2 (+15 to +30 min): 100% Meter-Adjusted telemetry
                    eff_kt = max(0.15, min(1.05, kt_obs + trend_momentum * 0.50))
                    target_poa = eff_kt * cs_poa[b_idx]
                    b_hr = (b_dict["block"] * 15) // 60
                    amb_t = 28.0 + (4.0 if 11 <= b_hr <= 15 else 0.0)
                    cell_t = amb_t + target_poa * 0.031
                    temp_factor = np.clip(1.0 - 0.004 * (cell_t - 25.0), 0.82, 1.06)
                    raw_mw = target_poa * self.profile.transfer_ratio * temp_factor
                    adj_mw = min(self.profile.ac_capacity_mw, max(0.0, raw_mw))
                    adj_mw = round(adj_mw, 2)
                elif delta_blocks == 2:
                    # Block 3 (+30 to +45 min): Smooth transition bridge (50% Meter, 50% Pure Weather)
                    eff_kt = max(0.15, min(1.05, 0.50 * kt_obs + 0.50 * fcst_kt))
                    target_poa = eff_kt * cs_poa[b_idx]
                    b_hr = (b_dict["block"] * 15) // 60
                    amb_t = 28.0 + (4.0 if 11 <= b_hr <= 15 else 0.0)
                    cell_t = amb_t + target_poa * 0.031
                    temp_factor = np.clip(1.0 - 0.004 * (cell_t - 25.0), 0.82, 1.06)
                    raw_mw = target_poa * self.profile.transfer_ratio * temp_factor
                    adj_mw = min(self.profile.ac_capacity_mw, max(0.0, raw_mw))
                    adj_mw = round(adj_mw, 2)
                else:
                    # Block 4+ (beyond 45 min): 100% Pure Weather Ensemble
                    target_poa = fcst_gti
                    adj_mw = b_dict["predicted_mw"]

                if b_dict["block"] < 24 or b_dict["block"] > 76:
                    adj_mw = 0.0
                    target_poa = 0.0

                # Ramp continuity against previous block
                if updated_blocks:
                    prev_adj = updated_blocks[-1]["intellis_mw"]
                    # At the gate-closure boundary (first actionable block after freeze lag),
                    # allow stepping down to actual collapsed generation if plant dropped
                    if b_idx == (actionable_block - 1) and adj_mw < prev_adj and kt_obs < 0.65:
                        max_delta = max(prev_adj - adj_mw, max(0.50, self.profile.ac_capacity_mw * 0.10))
                    else:
                        max_delta = max(0.50, self.profile.ac_capacity_mw * 0.10)
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
        import csv

        # 1. Atmospheric Indicators for LLM Strategic Arbiter (Aggregating multi-member ensemble keys)
        weather = self.fetch_ensemble_weather(target_date_str)
        hourly = weather.get("hourly", {})
        
        # Open-Meteo ensemble returns multi-model keys: e.g. cloud_cover_icon_seamless_eps, cape_..., etc.
        cloud_vals = []
        for k, v_list in hourly.items():
            if k == "cloud_cover" or (k.startswith("cloud_cover_") and not any(k.startswith(f"cloud_cover_{layer}") for layer in ("low", "mid", "high"))):
                if isinstance(v_list, list):
                    # Daytime operating hours (06:00 to 18:00) if 24-hr vector
                    if len(v_list) >= 24:
                        cloud_vals.extend([float(v) for v in v_list[6:19] if v is not None and not np.isnan(v)])
                    else:
                        cloud_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
        if not cloud_vals:
            for k, v_list in hourly.items():
                if k.startswith("cloud_cover") and isinstance(v_list, list):
                    cloud_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
        mean_cloud = round(float(np.mean(cloud_vals)), 1) if cloud_vals else 0.0

        cape_vals = []
        for k, v_list in hourly.items():
            if (k == "cape" or k.startswith("cape_")) and isinstance(v_list, list):
                cape_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
        max_cape = round(float(np.max(cape_vals)), 1) if cape_vals else 0.0

        precip_vals = []
        for k, v_list in hourly.items():
            if (k == "precipitation" or k.startswith("precipitation_")) and isinstance(v_list, list):
                precip_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
        tot_precip = round(float(np.sum(precip_vals)), 2) if precip_vals else 0.0

        temp_vals = []
        for k, v_list in hourly.items():
            if (k == "temperature_2m" or k.startswith("temperature_2m_")) and isinstance(v_list, list):
                temp_vals.extend([float(v) for v in v_list if v is not None and not np.isnan(v)])
        mean_temp = round(float(np.mean(temp_vals)), 1) if temp_vals else 28.0

        weather_ind = {
            "cloud_cover_pct": mean_cloud,
            "cape_j_kg": max_cape,
            "precip_mm": tot_precip,
            "temp_c": mean_temp,
        }

        # 2. Generate 96-block physical schedule
        sched = self.predict_96block_schedule(target_date_str)

        # 3. Live SCADA Telemetry Ingestion & Real Clearness Ratio Computation
        live_mw = 0.0
        recent_list: list[float] = []
        has_live_scada = False
        live_poa: float | None = None
        t_hr, t_min = [int(p) for p in target_time_str.split(":")[:2]]
        curr_block = ((t_hr * 60 + t_min) // 15)
        curr_idx = curr_block - 1

        site_upper = (self.profile.plant_name or "").upper()
        is_non_meter_site = site_upper in NON_METER_SITES
        is_90min_site = (
            self.profile.penalty_regulation == "Madhya Pradesh"
            or site_upper in {
                "SIRMOUR", "ANJANGOAN", "ANJANGAON", "ANDAD", "BALAKWADA",
                "BAMKHAL", "CHANDAWASA", "GSNP", "GSPPL", "GUGARIYAKHEDI", "NANDGAON", "SAWDA", "REWASPRNG", "REWASEIT"
            }
        )
        freeze_lag_blocks = 6 if is_90min_site else 3
        actionable_block = max(1, curr_block + freeze_lag_blocks + 1)

        # Attempt to load physical SCADA meter telemetry only for genuine metered sites
        if live_meter_csv_path and Path(live_meter_csv_path).exists() and not is_non_meter_site:
            try:
                norm_meter = load_and_normalize_meter_csv(
                    Path(live_meter_csv_path),
                    meter_config=self.profile.meter_data,
                )
                day_meter = norm_meter[norm_meter["date"] == target_date_str]
                if not day_meter.empty:
                    prior_rows = day_meter[day_meter["block"] <= curr_block].sort_values("block")
                    vals = [float(v) for v in prior_rows["metered_mw"] if v is not None and not np.isnan(v)]
                    # Must contain non-zero generation (not just all zeros or #NA) to qualify as active SCADA
                    if vals and max(vals) > (0.02 * self.profile.ac_capacity_mw):
                        live_mw = float(vals[-1])
                        recent_list = vals
                        has_live_scada = True
                    # Also extract on-site pyranometer POA telemetry if available
                    if "poa_wm2" in prior_rows.columns:
                        poa_vals = [float(v) for v in prior_rows["poa_wm2"] if v is not None and not np.isnan(v)]
                        if poa_vals and poa_vals[-1] > 5.0:
                            live_poa = float(poa_vals[-1])
            except Exception as exc:
                print(f"  [WARN] Live meter load failed: {exc}")

        # Fallback to Satellite Virtual Meter if non-meter site or physical SCADA is offline / missing (#NA)
        if not has_live_scada:
            telemetry_source = "SATELLITE_VIRTUAL_METER"
        elif "telemetry_source" not in locals():
            telemetry_source = "PHYSICAL_SCADA"
        is_non_meter_or_missing_scada = is_non_meter_site or not has_live_scada

        if not has_live_scada:
            try:
                from modules.weather.satellite_virtual_meter import fetch_satellite_96block_profile
                target_dt = dt.datetime.strptime(f"{target_date_str} {target_time_str}", "%Y-%m-%d %H:%M")
                virt_mw, _ = fetch_satellite_96block_profile(
                    target_date=target_date_str,
                    latitude=self.profile.latitude,
                    longitude=self.profile.longitude,
                    tilt=self.profile.tilt_deg,
                    azimuth=self.profile.azimuth_openmeteo,
                    plant_capacity_mw=self.profile.ac_capacity_mw,
                    dc_capacity_mw=self.profile.dc_capacity_mw,
                    performance_ratio=getattr(self.profile, "calibrated_pr", 0.78),
                    cutoff_time=target_dt,
                )
                if curr_block >= 1 and np.max(virt_mw[:curr_block]) > (0.02 * self.profile.ac_capacity_mw):
                    live_mw = float(virt_mw[curr_block - 1])
                    recent_list = virt_mw[:curr_block].tolist()
            except Exception as exc:
                print(f"  [WARN] Intraday satellite virtual feedback failed: {exc}; using base meteorological forecast.")

        # Compute solar elevation at revision time
        try:
            from modules.weather import time_features
            t_dt = dt.datetime.strptime(f"{target_date_str} {target_time_str}", "%Y-%m-%d %H:%M")
            ref_elev = time_features.compute_time_features(t_dt, self.profile.latitude, self.profile.longitude)["solar_elevation_deg"]
        except Exception:
            ref_elev = 30.0

        # Compute real clear sky and model residual at revision cutoff
        cs_poa = self.compute_clearsky_poa_96block(target_date_str)
        cs_curr_mw = 0.0
        real_clearness_ratio = 1.0
        if 0 <= curr_idx < 96:
            cs_theoretical = cs_poa[curr_idx] * self.profile.transfer_ratio
            cs_curr_mw = min(self.profile.ac_capacity_mw, cs_theoretical)
            if cs_curr_mw > 0.3:
                real_clearness_ratio = round(min(1.25, max(0.0, live_mw / cs_curr_mw)), 2)
            else:
                real_clearness_ratio = 1.0 if live_mw > 0.1 else 0.85

        physics_curr_mw = 0.0
        residual_mw = 0.0
        if 0 <= curr_idx < 96:
            physics_curr_mw = round(float(sched["blocks"][curr_idx].get("predicted_mw", 0.0)), 2)
            residual_mw = round(live_mw - physics_curr_mw, 2)

        # For non-meter sites OR when satellite/SCADA telemetry is absent/failed:
        # If we have zero live_mw but physics expects real generation at high sun angle,
        # do NOT treat this as a plant collapse — it is missing telemetry, not a trip.
        # Restore live_mw to physics prediction so downstream trip logic is not falsely triggered.
        if live_mw <= 0.05 and physics_curr_mw > 0.3 and ref_elev >= 20.0:
            # Covers: non-meter sites, metered sites with SCADA delay, satellite API failure
            live_mw = physics_curr_mw
            residual_mw = 0.0
            real_clearness_ratio = 1.0

        telemetry_ind = {
            "latest_mw": round(live_mw, 2),
            "physics_predicted_mw": physics_curr_mw,
            "residual_mw": residual_mw,
            "solar_elevation_deg": round(ref_elev, 1),
            "clearness_ratio": real_clearness_ratio,
            "is_non_meter_site": is_non_meter_or_missing_scada,
            "telemetry_source": telemetry_source,
            "live_poa_wm2": round(live_poa, 1) if live_poa is not None else None,
        }

        # 4. Apply Real-Time SCADA Telemetry Relaxation (Only for plants with active physical SCADA)
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
                print(f"  [WARN] Intraday SCADA feedback failed: {exc}; using strategic forecast.")

        # Extract next 12 actionable dispatch blocks for the LLM to forecast
        next_12_blocks = []
        for b in sched["blocks"]:
            if actionable_block <= b["block"] < actionable_block + 12 and 24 <= b["block"] <= 76:
                next_12_blocks.append({
                    "block": b["block"],
                    "time_interval": b["time_interval"],
                    "predicted_mw": b["schedule_mw"],
                    "gti_wm2": b["intellis_gti"],
                })

        # 5. Consult LLM Strategic Arbiter (for metered and non-metered sites)
        try:
            from modules.llm.strategic_arbiter import LLMStrategicArbiter
            arbiter = LLMStrategicArbiter(plant_profile=self.profile)
            advice = arbiter.get_strategic_guidance(
                target_date_str=target_date_str,
                target_time_str=target_time_str,
                weather_indicators=weather_ind,
                live_telemetry=telemetry_ind,
                next_12_blocks=next_12_blocks,
            )
            print(f"  [LLM STRATEGY] Regime: {advice.regime} | Risk Quantile: {advice.quantile_bias_factor:.3f} | Agency: {advice.preferred_agency} | Trip Flag: {advice.is_trip_or_curtailment}")
            print(f"                 Reasoning: {advice.reasoning}")
        except Exception as arb_err:
            print(f"  [WARN] LLM Strategic Arbiter invocation skipped: {arb_err}")
            from modules.llm.strategic_arbiter import StrategicAdvice
            source_lbl = "SATELLITE_PHYSICS" if is_non_meter_site else "PHYSICS_BASELINE"
            advice = StrategicAdvice(
                regime="CLEAR_SKY",
                quantile_bias_factor=1.0,
                preferred_agency="SATELLITE_PHYSICS" if is_non_meter_site else "BALANCED",
                is_trip_or_curtailment=False,
                reasoning=f"Deterministic fallback physics mode ({arb_err})",
                block_predictions={},
                source=source_lbl,
            )

        # Note: Hardware trip / inverter outage logic is disabled per industry standard (Enercast alignment).
        # Electrical breaker resets cannot be predicted in advance; scheduling strictly reflects meteorological potential.
        advice.is_trip_or_curtailment = False

        # 6. Apply LLM Predictions for next 12 blocks, and pure Intellis GTI up to 19:00 (Block 76 / night 7)
        is_clear_sky = (
            
            real_clearness_ratio >= 0.85
            or advice.regime in ("CLEAR", "CLEAR_SKY")
            or (weather_ind and weather_ind.get("cloud_cover_pct", 100) <= 30.0)
        )
        is_overcast = (
            advice.regime in ("OVERCAST", "RAIN", "STORMY")
            or real_clearness_ratio < 0.50
            or (weather_ind and weather_ind.get("cloud_cover_pct", 0) >= 75.0)
        )

        if advice.block_predictions:
            print(f"  [LLM 12-BLOCK PREDICTIONS] Applying direct LLM predictions to {len(advice.block_predictions)} blocks...")
            prev_val = None
            max_llm_block = max(int(k) for k in advice.block_predictions.keys()) if advice.block_predictions else (actionable_block + 11)
            for b in sched["blocks"]:
                b_str = str(b["block"])
                if b["block"] >= actionable_block and b_str in advice.block_predictions:
                    pred_mw = float(advice.block_predictions[b_str])
                    pred_mw = max(0.0, min(self.profile.ac_capacity_mw, pred_mw))
                    b_idx = b["block"] - 1
                    physics_base_mw = float(b["schedule_mw"])

                    # --- PHYSICAL GUARDRAIL 1: Solar Geometry Hard Cutoff ---
                    if b["block"] > 74 or b["block"] < 24:
                        pred_mw = 0.0

                    # --- PHYSICAL GUARDRAIL 2: Clear-Sky Negative Cut Lockout ---
                    # If ground telemetry, weather, or regime confirms clear sky,
                    # forbid LLM from slashing predictions below the physics baseline.
                    if is_clear_sky:
                        if pred_mw < physics_base_mw:
                            pred_mw = physics_base_mw

                    # --- PHYSICAL GUARDRAIL 3: Overcast Optical Cloud Ceiling Upper Bound ---
                    # If today is overcast, forbid LLM from inflating generation above the cloud ceiling
                    if is_overcast:
                        overcast_max = max(physics_base_mw * 1.10, self.profile.ac_capacity_mw * 0.25)
                        if pred_mw > overcast_max:
                            pred_mw = overcast_max

                    # --- PHYSICAL GUARDRAIL 4: Ramp-Rate Continuity Filter ---
                    # Only apply downward ramp clamping if we are NOT locked to clear-sky physics baseline
                    if prev_val is not None and 24 <= b["block"] <= 74:
                        if is_clear_sky and pred_mw >= physics_base_mw and not advice.is_trip_or_curtailment:
                            # Physics baseline is already smooth and continuous
                            pred_mw = max(pred_mw, physics_base_mw)
                        else:
                            if b["block"] == actionable_block and (advice.is_trip_or_curtailment or real_clearness_ratio < 0.65):
                                max_ramp = max(abs(pred_mw - prev_val), max(0.50, self.profile.ac_capacity_mw * 0.10))
                            else:
                                max_ramp = max(0.50, self.profile.ac_capacity_mw * 0.10)
                            if abs(pred_mw - prev_val) > max_ramp:
                                pred_mw = prev_val + (max_ramp if pred_mw > prev_val else -max_ramp)
                    pred_mw = round(pred_mw, 2)
                    b["predicted_mw"] = pred_mw
                    b["intellis_mw"] = pred_mw
                    b["schedule_mw"] = pred_mw
                    if self.profile.transfer_ratio > 0:
                        b["intellis_gti"] = round(pred_mw / self.profile.transfer_ratio, 1)
                        b["predicted_gti_wm2"] = b["intellis_gti"]
                elif b["block"] > max_llm_block and b["block"] <= 76:
                    # After 12 LLM blocks up to 19:00 (Block 76 / night 7), use pure physical Intellis GTI
                    raw_gti_mw = float(b.get("predicted_mw", b.get("intellis_mw", 0.0)))
                    raw_gti_mw = max(0.0, min(self.profile.ac_capacity_mw, raw_gti_mw))
                    if b["block"] > 72:  # Sunset transition (18:00 - 19:00)
                        raw_gti_mw = min(raw_gti_mw, max(0.0, round((76 - b["block"]) * 0.03, 2)))
                    b["schedule_mw"] = raw_gti_mw
                    b["intellis_mw"] = raw_gti_mw
                    b["predicted_mw"] = raw_gti_mw
                elif b["block"] > 76 or b["block"] < 24:
                    # Night hours past 19:00 (Block 76) are strictly 0.0 MW
                    b["schedule_mw"] = 0.0
                    b["intellis_mw"] = 0.0
                    b["intellis_gti"] = 0.0
                    b["predicted_gti_wm2"] = 0.0
                prev_val = float(b["schedule_mw"])
        elif abs(advice.quantile_bias_factor - 1.0) > 0.005:
            q_factor = advice.quantile_bias_factor
            # Under confirmed clear sky, lock out negative cuts below 1.0
            if (real_clearness_ratio >= 0.85 or today_regime == "CLEAR") and not advice.is_trip_or_curtailment:
                q_factor = max(1.0, q_factor)
            # Under overcast conditions, lock out upward inflation above 1.02
            elif (today_regime == "OVERCAST" or real_clearness_ratio < 0.50) and not advice.is_trip_or_curtailment:
                q_factor = min(1.02, q_factor)

            for b in sched["blocks"]:
                # NEVER touch past blocks; only adjust forward actionable blocks
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

        output_csv_path = Path(output_csv_path)
        output_csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "Block",
            "Time Interval (15 minute interval)",
            "intellis_gti",
            "intellis_mw",
            "schedule_mw",
        ]
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
    lookback_days: int = 7,
) -> dict[str, Any]:
    """Convenience functional interface for IntellisEnsembleGTIAI."""
    if not target_date_str:
        target_date_str = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    ai = IntellisEnsembleGTIAI(plant_name=plant_name)
    return ai.predict_96block_schedule(target_date_str)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Intellis Ensemble GTI AI Predictor")
    parser.add_argument("--plant", default="GSNP", help="Plant Name (e.g. GSNP, KASIPET, SIRMOUR)")
    parser.add_argument("--date", default="2026-09-14", help="Target Date (YYYY-MM-DD)")
    args = parser.parse_args()

    print(f"=== Running Intellis Ensemble GTI AI for {args.plant} on {args.date} ===")
    ai = IntellisEnsembleGTIAI(plant_name=args.plant)
    keys, weights, rank_df = ai.benchmark_and_select_models(args.date)
    print("\nSelected Diverse Models (Tri-Engine Quota):")
    for k in keys:
        label = rank_df[rank_df["key"] == k]["label"].values[0] if not rank_df.empty and k in rank_df["key"].values else k
        score = rank_df[rank_df["key"] == k]["rmse_mw"].values[0] if not rank_df.empty and k in rank_df["key"].values else 0.0
        print(f" - {label:15s} (Key: {k}) | 7-Day RMSE: {score:.3f} MW | Weight: {weights[k]:.4f}")

    res = ai.predict_96block_schedule(args.date, keys, weights)
    print(f"\nResults for {args.date}:")
    print(f"Daylight MAE: {res['daylight_mae_mw']} MW")
    if res['safe_blocks'] is not None:
        print(f"Safe Blocks: {res['safe_blocks']} / 96")
        print(f"Total DSM Penalty: Rs. {res['total_dsm_penalty_inr']:.2f}")

    print("\nSolar Noon Sample Blocks:")
    print(f"{'Blk':3s} | {'Time':5s} | {'Pred GTI':8s} | {'Pred MW':8s} | {'Meter MW':8s} | {'Dev':6s} | {'Slab':9s} | {'Penalty':8s}")
    print("-" * 75)
    for b in res["blocks"][44:56]:
        print(f"{b['block']:3d} | {b['time']:5s} | {b['predicted_gti_wm2']:8.1f} | {b['predicted_mw']:8.2f} | {b['meter_mw']:8.2f} | {b['dev_mw']:+6.2f} | {b['dsm_slab']:9s} | Rs. {b['block_penalty_inr']:6.2f}")
