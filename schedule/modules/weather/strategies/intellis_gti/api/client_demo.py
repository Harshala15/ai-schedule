"""Reference Client Demonstration Script for Intellis GTI API.

Demonstrates how commercial clients call the /v1/gti endpoint using Python.
Supports direct in-memory testing or live HTTP requests against a running API server.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add schedule directory to path for in-memory testing
_SCHEDULE_DIR = Path(__file__).resolve().parents[5]
if str(_SCHEDULE_DIR) not in sys.path:
    sys.path.insert(0, str(_SCHEDULE_DIR))


def run_demo(api_url: str | None = None, plant: str = "GSNP", date: str = "2026-10-09", client_key: str = "demo-key-2026"):
    headers = {"X-API-Key": client_key}
    params = {"plant": plant, "date": date}

    if api_url:
        import requests
        print(f"Calling LIVE endpoint: {api_url}/v1/gti with plant={plant}, date={date}...")
        resp = requests.get(f"{api_url.rstrip('/')}/v1/gti", headers=headers, params=params, timeout=30)
        status_code = resp.status_code
        payload = resp.json()
    else:
        # In-memory test using FastAPI TestClient (no server setup needed)
        from fastapi.testclient import TestClient
        from modules.weather.strategies.intellis_gti.api.app import app

        print(f"Running IN-MEMORY test for plant={plant}, date={date}...")
        client = TestClient(app)
        resp = client.get("/v1/gti", headers=headers, params=params)
        status_code = resp.status_code
        payload = resp.json()

    print("\n" + "=" * 60)
    print(f"HTTP Status: {status_code}")
    print("=" * 60)

    if status_code != 200:
        print("Error Response:", payload)
        return

    print(f"Plant:                  {payload.get('plant_name')}")
    print(f"Target Date:            {payload.get('target_date')}")
    print(f"Open-Meteo Plan Tier:   {payload.get('metadata', {}).get('plan_tier')}")
    print(f"Endpoint Used:          {payload.get('metadata', {}).get('openmeteo_endpoint')}")
    print(f"Peak GTI:               {payload.get('peak_gti_wm2')} W/m²")
    print(f"Total Daily Energy:     {payload.get('total_daylight_kwh_m2')} kWh/m²")
    print(f"Total Blocks:           {len(payload.get('blocks_96', []))}")

    print("\nSample Daylight Blocks (10:00 AM to 02:00 PM):")
    print(f"{'Block':<8} {'Time Interval':<18} {'GTI (W/m²)':<14} {'ClearSky POA (W/m²)'}")
    print("-" * 60)
    for b in payload.get("blocks_96", [])[40:56]:
        print(f"{b['block']:<8} {b['time_interval']:<18} {b['gti_wm2']:<14} {b['clearsky_poa_wm2']}")
    print("-" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Intellis GTI Commercial API")
    parser.add_argument("--url", default=None, help="Live API URL (e.g., http://127.0.0.1:8000). Omit for in-memory test.")
    parser.add_argument("--plant", default="GSNP", help="Plant name (default: GSNP)")
    parser.add_argument("--date", default="2026-10-09", help="Target date YYYY-MM-DD")
    parser.add_argument("--key", default="demo-key-2026", help="Client API key")
    args = parser.parse_args()

    run_demo(api_url=args.url, plant=args.plant, date=args.date, client_key=args.key)
