"""
Module 2: 7-Day Multi-Agency NWP Weather Fetch Engine.
Fetches 168-hour global numerical weather predictions (ECMWF, ICON, GFS) across the 7-day forecast horizon.
"""

from __future__ import annotations
import os
import json
import urllib.parse
import urllib.request
import datetime as dt
from pathlib import Path
from typing import Dict, Any, Tuple
import numpy as np

CUSTOMER_ENSEMBLE_URL = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
PUBLIC_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"


class WANWPFetchEngine:
    """
    Fetches 7 days (168 hourly timestamps) of global NWP ensemble irradiance and temperature data.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.getenv("OPENMETEO_API_KEY", "jbThkFlLZSXZE3CU").strip()

    def get_ensemble_url(self) -> str:
        return CUSTOMER_ENSEMBLE_URL if self.api_key else PUBLIC_ENSEMBLE_URL

    def fetch_7day_weather(
        self,
        latitude: float,
        longitude: float,
        start_date_str: str,
        cache_dir: Path | None = None,
    ) -> Dict[str, Any]:
        """Fetches 7-day weather variables for the target window."""
        start_dt = dt.datetime.strptime(start_date_str, "%Y-%m-%d")
        end_dt = start_dt + dt.timedelta(days=6)
        end_date_str = end_dt.strftime("%Y-%m-%d")

        cache_dir = cache_dir or Path("/tmp/wa_solar_weather_cache") / start_date_str
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"wa_solar_{latitude:.4f}_{longitude:.4f}.json"

        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get("hourly", {}).get("time") and len(data["hourly"]["time"]) >= 168:
                    return data
            except Exception:
                pass

        params = {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "start_date": start_date_str,
            "end_date": end_date_str,
            "hourly": [
                "shortwave_radiation",
                "direct_normal_irradiance",
                "temperature_2m",
                "cloud_cover",
            ],
            "models": "ecmwf_ifs025_ensemble,icon_seamless,gfs_seamless,gem_seamless,bom_access_global_ensemble",
            "timezone": "Asia/Kolkata",
        }

        if self.api_key:
            params["apikey"] = self.api_key

        url = self.get_ensemble_url() + "?" + urllib.parse.urlencode(params, doseq=True)

        req = urllib.request.Request(url, headers={"User-Agent": "IntellisWeekAheadSolar/2.0"})
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        try:
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

        return data

    def extract_ghi_and_temp_matrices(
        self,
        weather_data: Dict[str, Any],
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Extracts hourly GHI matrix of shape (num_members, 168) and mean ambient temperature array (168,).
        """
        hourly = weather_data.get("hourly", {})
        ghi_cols = [k for k in hourly.keys() if k.startswith("shortwave_radiation")]
        if not ghi_cols:
            ghi_cols = [k for k in hourly.keys() if "radiation" in k or "ghi" in k]

        temp_cols = [k for k in hourly.keys() if k.startswith("temperature_2m")]

        member_ghi_list = []
        for col in ghi_cols:
            vals = hourly.get(col, [])
            if vals and len(vals) >= 168:
                member_ghi_list.append([float(v) if v is not None else 0.0 for v in vals[:168]])

        if not member_ghi_list:
            ghi_matrix = np.zeros((1, 168))
        else:
            ghi_matrix = np.array(member_ghi_list)  # (M, 168)

        # Ambient temperature (mean across temperature models)
        temp_vals_list = []
        for col in temp_cols:
            vals = hourly.get(col, [])
            if vals and len(vals) >= 168:
                temp_vals_list.append([float(v) if v is not None else 25.0 for v in vals[:168]])

        if temp_vals_list:
            mean_temp_168 = np.mean(temp_vals_list, axis=0)
        else:
            mean_temp_168 = np.full(168, 25.0)

        return ghi_matrix, mean_temp_168
