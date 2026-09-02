import sys
import time
import json
import base64
import datetime as dt
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_PATH = Path(__file__).resolve().parent / "windy_login.json"
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "schedule" / "windy_login.json"

def main():
    email = "code.vedanjaypower@gmail.com"
    password = "Code@123"

    print("=" * 60)
    print("WINDY PREMIUM ONE-CLICK LOGIN")
    print("=" * 60)
    print("Opening visible browser...")

    with sync_playwright() as p:
        # Launch non-headless browser
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()

        print("Navigating to https://account.windy.com/login ...")
        page.goto("https://account.windy.com/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # Pre-fill credentials automatically
        try:
            inputs = page.locator('input').all()
            if len(inputs) >= 2:
                inputs[0].fill(email)
                page.wait_for_timeout(400)
                inputs[1].fill(password)
                print("[OK] Pre-filled Email and Password!")
                page.wait_for_timeout(500)
                page.locator('button[type="submit"]').first.click()
                print("[OK] Clicked Sign In!")
        except Exception as e:
            print(f"[WARN] Auto-fill note: {e}")

        print("\n[ACTION NEEDED] If Cloudflare asks to verify you are human, please check the box on screen.")
        print("[INFO] Waiting for login completion and redirect...")

        # Wait until logged in
        for i in range(120): # wait up to 2 minutes
            time.sleep(1)
            if "account.windy.com/login" not in page.url:
                print(f"\n[SUCCESS] Login verified! Redirected to: {page.url}")
                break

        page.wait_for_timeout(3000)

        # Navigate to windy.com main map to sync app localStorage & Premium token
        print("Syncing Premium storage with main windy.com...")
        try:
            page.goto("https://www.windy.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
        except Exception:
            pass

        # Save session
        context.storage_state(path=str(OUTPUT_PATH))
        print(f"\n[OK] Saved fresh login session to {OUTPUT_PATH}")
        if SCHEDULE_PATH.parent.exists():
            context.storage_state(path=str(SCHEDULE_PATH))
            print(f"[OK] Synced fresh login session to {SCHEDULE_PATH}")

        browser.close()

    # Validate saved token
    try:
        data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        for origin in data.get("origins", []):
            for item in origin.get("localStorage", []):
                if item.get("name") == "settings_userToken":
                    val = item.get("value", "").strip('"')
                    parts = val.split(".")
                    if len(parts) >= 2:
                        payload_b64 = parts[1] + "=="
                        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
                        exp_ts = payload.get("exp")
                        if exp_ts:
                            exp_dt = dt.datetime.fromtimestamp(exp_ts, tz=dt.timezone.utc)
                            print(f"\n[VALIDATION] New Token Expiration: {exp_dt}")
                            print(f"[VALIDATION] User ID: {payload.get('userID')}")
                            print(f"[VALIDATION] Subscription: {payload.get('subscriptionTiers')}")
    except Exception as ex:
        print(f"[WARN] Validation check: {ex}")

if __name__ == "__main__":
    main()
