import json
import base64
import datetime as dt
from pathlib import Path

path = Path("windy/windy_login.json")
data = json.loads(path.read_text(encoding="utf-8"))
print("=== WINDY PREMIUM SESSION VERIFICATION ===")
for origin in data.get("origins", []):
    for item in origin.get("localStorage", []):
        if item.get("name") == "settings_userToken":
            val = item.get("value", "").strip('"')
            parts = val.split(".")
            payload_b64 = parts[1] + "=="
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
            exp_ts = payload.get("exp")
            exp_dt = dt.datetime.fromtimestamp(exp_ts, tz=dt.timezone.utc)
            print("User ID:", payload.get("userID"))
            print("Subscription:", payload.get("subscriptionTiers"))
            print("Token Expiration UTC:", exp_dt)
            print("Token Expiration IST:", exp_dt + dt.timedelta(hours=5, minutes=30))
