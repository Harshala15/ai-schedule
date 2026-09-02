import sys
import os
import time
import json
import base64
import argparse
import datetime as dt
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_PATH = Path(__file__).resolve().parent / "windy_login.json"
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "schedule" / "windy_login.json"

def auto_login(email: str, password: str, headless: bool = False) -> bool:
    print("=" * 60)
    print("AUTOMATED WINDY LOGIN")
    print("=" * 60)
    print(f"Logging in with email: {email}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()

        print("Navigating to https://account.windy.com/login ...")
        page.goto("https://account.windy.com/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Target exact autocomplete attributes
        email_inp = page.locator('input[autocomplete="email"]').first
        if not email_inp.is_visible(timeout=5000):
            email_inp = page.locator('input').all()[0]

        pw_inp = page.locator('input[autocomplete="current-password"]').first
        if not pw_inp.is_visible(timeout=5000):
            pw_inp = page.locator('input[type="password"]').first

        email_inp.click()
        email_inp.fill(email)
        print("  [OK] Filled email.")

        page.wait_for_timeout(500)
        pw_inp.click()
        pw_inp.fill(password)
        print("  [OK] Filled password.")

        page.wait_for_timeout(500)

        # Click submit
        sign_btn = page.locator('button:has-text("Sign in")').first
        if sign_btn.is_visible(timeout=3000):
            sign_btn.click()
            print("  [OK] Clicked 'Sign in' button.")
        else:
            pw_inp.press("Enter")
            print("  [INFO] Pressed Enter on password input.")

        print("Waiting for authentication & redirect...")
        for i in range(25):
            page.wait_for_timeout(1000)
            if "account.windy.com/login" not in page.url:
                print(f"  [OK] Redirected to {page.url}!")
                break

        page.wait_for_timeout(5000)

        # Navigate to main windy.com to collect app storage & subscription token
        print("Navigating to https://www.windy.com/ to sync Premium localStorage...")
        try:
            page.goto("https://www.windy.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
        except Exception as e:
            print(f"  [WARN] Navigation to windy.com: {e}")

        # Save session
        context.storage_state(path=str(OUTPUT_PATH))
        print(f"\n[OK] Saved fresh session to {OUTPUT_PATH}")
        if SCHEDULE_PATH.parent.exists():
            context.storage_state(path=str(SCHEDULE_PATH))
            print(f"[OK] Synced fresh session to {SCHEDULE_PATH}")

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
                            print(f"[VALIDATION] Token Expiration: {exp_dt}")
                            print(f"[VALIDATION] User ID: {payload.get('userID')}")
                            print(f"[VALIDATION] Subscription: {payload.get('subscriptionTiers')}")
                            return True
    except Exception as ex:
        print(f"[WARN] Token check: {ex}")

    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated Windy Premium login")
    parser.add_argument("--email", default=os.getenv("WINDY_EMAIL", "code.vedanjaypower@gmail.com"))
    parser.add_argument("--password", default=os.getenv("WINDY_PASSWORD", "Code@123"))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    success = auto_login(args.email, args.password, headless=args.headless)
    sys.exit(0 if success else 1)
