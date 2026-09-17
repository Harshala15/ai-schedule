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
NON_METER_SITES = {"ANDAD", "GUGARIYAKHEDI", "SAWDA", "BALAKWADA", "CME"}


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
                "temperature_2m",
                "wind_speed_10m",
                "cloud_cover",
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
                b_idx = np.arange(96)
                noon_dist = np.abs(b_idx - 48.5) * 0.25
                cos_elev = np.maximum(0.0, np.cos(noon_dist * (np.pi / 12.0)))
                est_cell = 25.0 + 10.0 * (cos_elev ** 0.8) + (cos_elev * 900.0 * 0.031)
                temp_factor = np.clip(1.0 - 0.004 * (est_cell - 25.0), 0.82, 1.05)
                poa_arr = mw_arr / max(1e-6, self.profile.transfer_ratio * temp_factor)

            return mw_arr, poa_arr
        except Exception:
            return np.zeros(96, dtype=float), np.zeros(96, dtype=float)

    def calibrate_plant_pr(self, target_date_str: str, lookback_days: int = 5) -> float:
        """
        Dynamically learn plant-specific Performance Ratio (PR) from historical meter telemetry.
        Inspects up to `lookback_days` before `target_date_str` to calculate robust ratio
        during high-irradiance daylight blocks. Clamps within physical limits [0.70, 0.88].
        """
        if self.profile.plant_name.upper() in NON_METER_SITES:
            learned_pr = getattr(self.profile, "calibrated_pr", None) or getattr(config, "PERFORMANCE_RATIO", 0.78)
            self.profile.calibrated_pr = round(learned_pr, 4)
            self.profile.transfer_ratio = round((self.profile.dc_capacity_mw * self.profile.calibrated_pr) / 1000.0, 6)
            return self.profile.calibrated_pr

        target_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        valid_prs = []

        for offset in range(1, lookback_days + 1):
            day_str = (target_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
            d_mw, d_poa = self.load_meter_actuals_with_poa(day_str)
            if np.max(d_mw) < 0.10 * self.profile.ac_capacity_mw:
                continue

            mask = (d_poa >= 300.0) & (d_mw >= 0.10 * self.profile.ac_capacity_mw)
            if np.sum(mask) >= 4:
                pr_vals = (d_mw[mask] * 1000.0) / (d_poa[mask] * self.profile.dc_capacity_mw)
                valid_prs.extend(pr_vals.tolist())

        if len(valid_prs) >= 6:
            learned_pr = float(np.percentile(valid_prs, 75))
            learned_pr = max(0.70, min(0.88, learned_pr))
        else:
            learned_pr = getattr(self.profile, "calibrated_pr", None) or getattr(config, "PERFORMANCE_RATIO", 0.78)

        self.profile.calibrated_pr = round(learned_pr, 4)
        self.profile.transfer_ratio = round((self.profile.dc_capacity_mw * self.profile.calibrated_pr) / 1000.0, 6)
        return self.profile.calibrated_pr

    # -------------------------------------------------------------------------
    # 4. Clearness Index (Kt) Regime Classifier
    # -------------------------------------------------------------------------
    def classify_weather_regime(self, gti_daylight_wm2: float, cs_daylight_wm2: float) -> str:
        """Classify daily atmospheric condition into OVERCAST, MIXED, or CLEAR."""
        if cs_daylight_wm2 <= 0.0:
            return "MIXED"
        kt = gti_daylight_wm2 / cs_daylight_wm2
        if kt < 0.50:
            return "OVERCAST"
        elif kt >= 0.75:
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
    ) -> tuple[list[str], dict[str, float], pd.DataFrame]:
        """
        Evaluate candidate models over lookback days against actual SCADA meter POA.
        Can evaluate over full day or a specific diurnal slot (morning/midday/afternoon).
        Applies:
        - Exponential decay weighting: w_d = exp(-offset / tau).
        - Multi-family stratified regime matching.
        - Tri-Agency Quota: Top ICON + Top ECMWF + Top GEFS.
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

        # Multi-family stratified sampling for today's regime (prevents single-agency overcast bias)
        sample_keys = []
        for fam in ["icon", "ecmwf", "gefs", "gem"]:
            f_keys = [k for k in canonical_keys if fam in k.lower()]
            sample_keys.extend(f_keys[:2])
        if not sample_keys:
            sample_keys = canonical_keys[:8]

        today_prelim_gti = [self.extract_member_96block_gti(today_weather, k, target_date_str) for k in sample_keys]
        today_mean_gti_sum = float(np.sum(np.mean(today_prelim_gti, axis=0)[b_start:b_end])) if today_prelim_gti else 0.0
        today_regime = self.classify_weather_regime(today_mean_gti_sum, cs_daylight_sum)

        model_weighted_mse: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_weighted_bias: dict[str, float] = {k: 0.0 for k in canonical_keys}
        model_weight_sums: dict[str, float] = {k: 0.0 for k in canonical_keys}

        valid_days_evaluated = []
        for offset, d_str in enumerate(eval_dates, start=1):
            meter_mw, meter_poa = self.load_meter_actuals_with_poa(d_str)
            if np.max(meter_mw) < 0.5 and np.max(meter_poa) < 50.0:
                continue

            if np.max(meter_poa) <= 0.0:
                b_idx = np.arange(96)
                noon_dist = np.abs(b_idx - 48.5) * 0.25
                cos_elev = np.maximum(0.0, np.cos(noon_dist * (np.pi / 12.0)))
                est_cell = 25.0 + 10.0 * (cos_elev ** 0.8) + (cos_elev * 900.0 * 0.031)
                temp_factor = np.clip(1.0 - 0.004 * (est_cell - 25.0), 0.82, 1.05)
                meter_poa = meter_mw / max(1e-6, self.profile.transfer_ratio * temp_factor)

            day_meter_sum = float(np.sum(meter_poa[b_start:b_end]))
            day_regime = self.classify_weather_regime(day_meter_sum, cs_daylight_sum)

            w_d = math.exp(-offset / max(0.5, tau_decay_days))
            if use_regime_matching and day_regime == today_regime:
                w_d *= 1.25  # Balanced regime weighting (avoids overcast confirmation bias)

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
                model_weighted_mse[k] += w_d * mse
                model_weighted_bias[k] += w_d * bias
                model_weight_sums[k] += w_d

        records = []
        for k in canonical_keys:
            if model_weight_sums[k] <= 0.0:
                continue
            rmse_poa = math.sqrt(model_weighted_mse[k] / model_weight_sums[k])
            bias_poa = abs(model_weighted_bias[k] / model_weight_sums[k])
            rmse_mw = rmse_poa * self.profile.transfer_ratio
            bias_mw = bias_poa * self.profile.transfer_ratio
            # Composite loss penalizes both dispersion (RMSE) and persistent directional drift (Bias)
            composite_loss = (rmse_poa + 0.40 * bias_poa) * self.profile.transfer_ratio

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
            return avail, {k: round(1.0 / len(avail), 4) for k in avail}, pd.DataFrame()

        df_rank = pd.DataFrame(records).sort_values("composite_loss")

        # Tri-Agency Diversity Selection
        top_icon = df_rank[df_rank["family"] == "ICON"].head(icon_quota)
        top_ecmwf = df_rank[df_rank["family"] == "ECMWF"].head(ecmwf_quota)
        top_gefs = df_rank[df_rank["family"] == "GEFS"].head(gefs_quota)

        selected_parts = [top_icon, top_ecmwf]
        if not top_gefs.empty:
            selected_parts.append(top_gefs)
        
        selected_df = pd.concat(selected_parts)
        selected_keys = selected_df["key"].tolist()

        score_col = "composite_loss" if "composite_loss" in selected_df.columns else "rmse_mw"
        raw_w = [1.0 / max(1e-4, row[score_col]) for _, row in selected_df.iterrows()]
        norm_w = [round(w / sum(raw_w), 4) for w in raw_w]
        weights_map = {k: w for k, w in zip(selected_keys, norm_w)}

        return selected_keys, weights_map, df_rank

    def benchmark_and_select_slot_models(
        self,
        target_date_str: str,
        lookback_days: int = 5,
    ) -> dict[str, tuple[list[str], dict[str, float]]]:
        """Benchmark and select top models independently for each diurnal time slot."""
        slot_results = {}
        for slot_name, slot_range in self.DIURNAL_SLOTS.items():
            if slot_name == "morning":
                # Morning: 2 GEFS (fast clear-sky ramp) + 2 ICON + 1 ECMWF
                keys, weights, _ = self.benchmark_and_select_models(
                    target_date_str,
                    lookback_days=lookback_days,
                    icon_quota=2,
                    ecmwf_quota=1,
                    gefs_quota=2,
                    block_range=slot_range,
                )
            elif slot_name == "midday":
                # Midday: 2 ICON (non-hydrostatic convection) + 2 ECMWF + 1 GEFS
                keys, weights, _ = self.benchmark_and_select_models(
                    target_date_str,
                    lookback_days=lookback_days,
                    icon_quota=2,
                    ecmwf_quota=2,
                    gefs_quota=1,
                    block_range=slot_range,
                )
            else:
                # Afternoon: 2 ECMWF (synoptic clearing) + 2 ICON + 1 GEFS
                keys, weights, _ = self.benchmark_and_select_models(
                    target_date_str,
                    lookback_days=lookback_days,
                    icon_quota=2,
                    ecmwf_quota=2,
                    gefs_quota=1,
                    block_range=slot_range,
                )
            slot_results[slot_name] = (keys, weights)
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
        slot_keys, base_weights = slot_selections.get(slot_name, ([], {}))

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

        weather = self.fetch_ensemble_weather(target_date_str)
        meter_mw = self.load_meter_actuals(target_date_str)
        cs_poa = self.compute_clearsky_poa_96block(target_date_str)

        if not selected_keys or not weights_map:
            slot_selections = self.benchmark_and_select_slot_models(target_date_str)
            all_selected = []
            all_weights = {}
            for s_name, (s_keys, s_w) in slot_selections.items():
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

        # Compute Clearness Index (Kt) array for each slot
        kt_slots = {}
        for s_name, (s_keys, s_w) in slot_selections.items():
            slot_kt = np.zeros(96, dtype=float)
            total_w = sum(s_w.values()) if s_w else 1.0
            for k in s_keys:
                w_k = s_w.get(k, 1.0 / max(1, len(s_keys))) / total_w
                m_gti = self.extract_member_96block_gti(weather, k, target_date_str)
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

        # Ambient temperature derating from Open-Meteo Premium
        hourly = weather.get("hourly", {})
        if "temperature_2m" in hourly and hourly["temperature_2m"]:
            h_t = [float(v) if v is not None else 28.0 for v in hourly["temperature_2m"][:24]]
            amb_temp = np.interp(np.arange(0, 24, 0.25), np.arange(0, 24, 1.0), h_t)
        else:
            amb_temp = 25.0 + 10.0 * np.sin(np.pi * np.maximum(0, np.arange(96) - 24) / 56.0)

        cell_temp = amb_temp + fused_gti * 0.031
        temp_factor = np.clip(1.0 - 0.004 * (cell_temp - 25.0), 0.82, 1.06)

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
    ) -> dict[str, Any]:
        """
        Closed-loop physical relaxation filter for intraday schedule revisions.
        Anchors forward blocks smoothly using continuous Clearness Index (Kt)
        telemetry without discrete binary switches, aligned with regulatory lag.
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
                "BAMKHAL", "CHANDAWASA", "GSNP", "GSPPL", "GUGARIYAKHEDI", "NANDGAON", "SAWDA"
            }
        )
        freeze_lag_blocks = 6 if is_90min_site else 3
        actionable_block = current_block + freeze_lag_blocks + 1

        # Theoretical clear-sky power for the plant at current block (bounded by AC inverter limit)
        cs_theoretical = cs_poa[curr_idx] * self.profile.transfer_ratio
        cs_curr_mw = min(self.profile.ac_capacity_mw, cs_theoretical)

        # Early morning inverter wakeup guardrail:
        # Before 07:15 AM (block 30) or during low sun angles (<10% capacity),
        # inverters exhibit startup latency and horizon shadowing.
        # Do not allow early dawn transient to pull down forward 08:30-10:00 schedule.
        if current_block < 30 or cs_curr_mw < (0.10 * self.profile.ac_capacity_mw):
            kt_obs = 0.85
        elif live_meter_mw >= (0.88 * self.profile.ac_capacity_mw):
            # Inverter clipping ceiling: plant generating at max inverter rating
            kt_obs = 1.00
        elif cs_curr_mw > 0.3:
            kt_obs = max(0.65, min(1.05, live_meter_mw / cs_curr_mw))
        else:
            kt_obs = 0.85 if live_meter_mw > 0.2 else 0.65

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
                            kts.append(max(0.65, min(1.05, hist_mw / hist_cs)))
            if kts:
                kt_obs = float(np.median(kts))

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
                # Future actionable blocks: continuous Kt relaxation into meteorological ensemble
                delta_blocks = b_idx - (actionable_block - 1)
                decay = math.exp(-delta_blocks / max(1.0, tau_blocks))

                fcst_gti = b_dict["predicted_gti_wm2"]
                fcst_kt = min(1.05, fcst_gti / max(15.0, cs_poa[b_idx]))

                eff_kt = decay * kt_obs + (1.0 - decay) * fcst_kt
                # Ensure daytime clearness does not drop into penalty band during daytime
                if 28 <= b_dict["block"] <= 68:
                    eff_kt = max(0.70, eff_kt)

                target_poa = eff_kt * cs_poa[b_idx]
                raw_mw = target_poa * self.profile.transfer_ratio

                adj_mw = min(self.profile.ac_capacity_mw, max(0.0, raw_mw))
                adj_mw = round(adj_mw, 2)
                if b_dict["block"] < 24 or b_dict["block"] > 76:
                    adj_mw = 0.0

                # Ramp continuity against previous block
                if updated_blocks:
                    prev_adj = updated_blocks[-1]["intellis_mw"]
                    hr = (b_dict["block"] * 15) // 60
                    max_delta = max(0.35, self.profile.ac_capacity_mw * 0.06) if 11 <= hr <= 14 else max(0.50, self.profile.ac_capacity_mw * 0.12)
                    if abs(adj_mw - prev_adj) > max_delta:
                        adj_mw = round(prev_adj + (max_delta if adj_mw > prev_adj else -max_delta), 2)
                        adj_mw = min(self.profile.ac_capacity_mw, max(0.0, adj_mw))
                if b_dict["block"] < 24 or b_dict["block"] > 76:
                    adj_mw = 0.0

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
                if self.profile.transfer_ratio > 0:
                    b_copy["intellis_gti"] = round(adj_mw / self.profile.transfer_ratio, 1)
                    b_copy["predicted_gti_wm2"] = b_copy["intellis_gti"]
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
    ) -> dict[str, Any]:
        """Generate revision schedule CSV directly with intellis_gti and intellis_mw."""
        import csv
        sched = self.predict_96block_schedule(target_date_str)

        # If live meter data is provided, apply real-time SCADA feedback
        if live_meter_csv_path and Path(live_meter_csv_path).exists():
            try:
                norm_meter = load_and_normalize_meter_csv(
                    Path(live_meter_csv_path),
                    meter_config=self.profile.meter_data,
                )
                t_hr, t_min = [int(p) for p in target_time_str.split(":")[:2]]
                curr_block = ((t_hr * 60 + t_min) // 15)
                day_meter = norm_meter[norm_meter["date"] == target_date_str]
                if not day_meter.empty:
                    prior_rows = day_meter[day_meter["block"] <= curr_block].sort_values("block")
                    if not prior_rows.empty:
                        live_mw = float(prior_rows["metered_mw"].iloc[-1])
                        recent_list = prior_rows["metered_mw"].tolist()
                        sched = self.apply_intraday_scada_feedback(
                            sched,
                            current_block=curr_block,
                            live_meter_mw=live_mw,
                            recent_meter_mw_list=recent_list,
                        )
            except Exception as exc:
                print(f"  [WARN] Intraday SCADA feedback failed: {exc}; using base meteorological forecast.")
        elif self.profile.plant_name.upper() in NON_METER_SITES:
            # For non-meter sites, use satellite virtual meter up to revision cutoff time
            try:
                from modules.weather.satellite_virtual_meter import fetch_satellite_96block_profile
                t_hr, t_min = [int(p) for p in target_time_str.split(":")[:2]]
                curr_block = ((t_hr * 60 + t_min) // 15)
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
                if curr_block >= 1 and np.max(virt_mw[:curr_block]) > 0.05:
                    live_mw = float(virt_mw[curr_block - 1])
                    recent_list = virt_mw[:curr_block].tolist()
                    sched = self.apply_intraday_scada_feedback(
                        sched,
                        current_block=curr_block,
                        live_meter_mw=live_mw,
                        recent_meter_mw_list=recent_list,
                    )
            except Exception as exc:
                print(f"  [WARN] Intraday satellite virtual feedback failed: {exc}; using base meteorological forecast.")

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
