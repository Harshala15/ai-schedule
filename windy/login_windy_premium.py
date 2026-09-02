import sys
import time
import json
import base64
import datetime as dt
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_PATH = Path(__file__).resolve().parent / "windy_login.json"
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "schedule" / "windy_login.json"

def is_premium_session(state: dict) -> bool:
    for origin in state.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == "settings_userToken":
                val = item.get("value", "").strip('"')
                parts = val.split(".")
                if len(parts) >= 2:
                    try:
                        payload_b64 = parts[1] + "=="
                        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
                        tiers = payload.get("subscriptionTiers", [])
                        if tiers and "premium" in tiers:
                            return True
                    except Exception:
                        pass
            if item.get("name") == "settings_subscription":
                if "premium" in str(item.get("value", "")).lower():
                    return True
    return False

def main():
    print("=" * 60)
    print("WINDY PREMIUM INTERACTIVE LOGIN GENERATOR")
    print("=" * 60)
    print("\n1. Opening Windy login page...")
    print("2. Enter your Windy Premium email & password in the opened browser window.")
    print("3. Once logged in, come back to this terminal and press Enter.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        page.goto("https://account.windy.com/login", wait_until="domcontentloaded", timeout=60000)

        print("[WAITING] Please log in to Windy Premium on the browser window.")
        print("[INFO] After logging in and seeing your profile / Windy map, press ENTER here...")

        try:
            input(">>> Press ENTER here when logged in: ")
        except (EOFError, KeyboardInterrupt):
            time.sleep(10)

        # Give 3 seconds to ensure cookies & storage are flushed
        page.wait_for_timeout(3000)

        # Navigate to main windy.com to collect full app localStorage
        try:
            page.goto("https://www.windy.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
        except Exception:
            pass

        state = context.storage_state()
        context.storage_state(path=str(OUTPUT_PATH))
        print(f"\n[OK] Saved session to {OUTPUT_PATH}")
        if SCHEDULE_PATH.parent.exists():
            context.storage_state(path=str(SCHEDULE_PATH))
            print(f"[OK] Synced session to {SCHEDULE_PATH}")

        browser.close()

    # Detailed session check
    has_premium = is_premium_session(state)
    print(f"\n[CHECK] Is Premium Active in Saved Session?: {has_premium}")
    if not has_premium:
        print("[WARN] Premium tier was not detected in the token payload.")
        print("Please check if your Windy subscription is active under this account.")
    else:
        print("[SUCCESS] Windy Premium session verified and saved successfully!")

if __name__ == "__main__":
    main()
