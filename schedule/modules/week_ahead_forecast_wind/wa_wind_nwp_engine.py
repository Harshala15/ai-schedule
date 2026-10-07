"""
Module 1: 7-Day Multi-Agency Hub-Height NWP Weather Fetch Engine for Wind.
Fetches 168 hours of hub-height wind speed, surface pressure, and temperature data.
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


class WAWindNWPEngine:
    """
    Fetches 7 days (168 hourly timestamps) of global NWP ensemble wind forecasts at turbine hub height.
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
        hub_height_m: float = 100.0,
        cache_dir: Path | None = None,
    ) -> Dict[str, Any]:
        """Fetches 7-day weather variables for the target window."""
        start_dt = dt.datetime.strptime(start_date_str, "%Y-%m-%d")
        end_dt = start_dt + dt.timedelta(days=6)
        end_date_str = end_dt.strftime("%Y-%m-%d")

        cache_dir = cache_dir or Path("/tmp/wa_wind_weather_cache") / start_date_str
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"wa_wind_{latitude:.4f}_{longitude:.4f}.json"

        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get("hourly", {}).get("time") and len(data["hourly"]["time"]) >= 168:
                    return data
            except Exception:
                pass

        h_param = "wind_speed_100m" if hub_height_m >= 90 else "wind_speed_80m"
        dir_param = "wind_direction_100m" if hub_height_m >= 90 else "wind_direction_80m"

        params = {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "start_date": start_date_str,
            "end_date": end_date_str,
            "hourly": [
                h_param,
                dir_param,
                "wind_speed_10m",
                "temperature_2m",
                "surface_pressure",
            ],
            "models": "ecmwf_ifs025_ensemble,icon_seamless,gfs_seamless,gem_seamless,bom_access_global_ensemble",
            "timezone": "Asia/Kolkata",
            "wind_speed_unit": "ms",
        }

        if self.api_key:
            params["apikey"] = self.api_key

        url = self.get_ensemble_url() + "?" + urllib.parse.urlencode(params, doseq=True)

        req = urllib.request.Request(url, headers={"User-Agent": "IntellisWeekAheadWind/2.0"})
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        try:
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass

        return data

    def extract_member_hub_speeds_672(
        self,
        weather_data: Dict[str, Any],
        hub_height_m: float = 100.0,
    ) -> Tuple[Dict[str, np.ndarray], list[float], list[float]]:
        """
        Extracts 672-block (7 days * 96 blocks) interpolated hub-height wind speeds
        for all ensemble members, plus hourly temps and pressures.
        """
        hourly = weather_data.get("hourly", {})
        h_prefix = "wind_speed_100m" if hub_height_m >= 90 else "wind_speed_80m"
        wind_cols = [k for k in hourly.keys() if k.startswith(h_prefix) or k.startswith("wind_speed_10m")]
        if not wind_cols:
            wind_cols = [k for k in hourly.keys() if "wind_speed" in k]

        temps = [float(v) if v is not None else 25.0 for v in hourly.get("temperature_2m", [25.0] * 168)][:168]
        pressures = [float(v) if v is not None else 960.0 for v in hourly.get("surface_pressure", [960.0] * 168)][:168]

        h_idx = np.arange(0, 168, 1.0)
        b_idx = np.arange(0, 168, 0.25)

        shear_factor = (hub_height_m / 100.0) ** 0.143 if hub_height_m > 100.0 else 1.0

        member_speeds_672: Dict[str, np.ndarray] = {}

        for col in wind_cols:
            raw_vals = hourly.get(col, [])
            if not raw_vals:
                continue
            vals = [v for v in raw_vals if v is not None]
            if len(vals) < 168:
                continue

            is_10m = "10m" in col
            hourly_speeds = []
            for h_total in range(168):
                v_raw = raw_vals[h_total]
                if v_raw is None:
                    hourly_speeds.append(0.0)
                    continue
                v_ms = float(v_raw)
                if is_10m:
                    h_of_day = h_total % 24
                    alpha = 0.29 if (h_of_day >= 21 or h_of_day <= 5) else (0.12 if (8 <= h_of_day <= 17) else 0.20)
                    v_hub = v_ms * ((hub_height_m / 10.0) ** alpha)
                else:
                    v_hub = v_ms * shear_factor
                hourly_speeds.append(round(v_hub, 2))

            b_speeds = np.interp(b_idx, h_idx, hourly_speeds)
            member_name = col.replace("wind_speed_100m_", "").replace("wind_speed_80m_", "")
            member_speeds_672[member_name] = b_speeds

        return member_speeds_672, temps, pressures
