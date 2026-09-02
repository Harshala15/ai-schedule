import time
import json
import base64
import datetime as dt
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_PATH = Path(__file__).resolve().parent / "windy_login.json"
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "schedule" / "windy_login.json"

email = "code.vedanjaypower@gmail.com"
password = "Code@123"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        viewport={"width": 1400, "height": 900},
        timezone_id="Asia/Kolkata",
    )
    page = context.new_page()
    print("Navigating to https://www.windy.com/ ...")
    page.goto("https://www.windy.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    # Dismiss any cookie popups
    for t in ["Accept", "I agree", "Got it", "Agree", "Close", "OK", "Allow all"]:
        try:
            b = page.get_by_text(t, exact=False).first
            if b.is_visible(timeout=1000):
                b.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

    # Click Login in top right
    try:
        login_btn = page.locator("text=Login").first
        if login_btn.is_visible(timeout=3000):
            login_btn.click()
            print("[OK] Clicked 'Login' button on main page.")
            page.wait_for_timeout(3000)
    except Exception as e:
        print(f"[WARN] Clicking login: {e}")

    # Fill email & password in the popup modal
    try:
        inputs = page.locator("input").all()
        for inp in inputs:
            if inp.is_visible():
                t = inp.get_attribute("type") or "text"
                if t in ["text", "email"] and not inp.input_value():
                    inp.fill(email)
                    print("[OK] Filled email.")
                    page.wait_for_timeout(400)
                elif t == "password":
                    inp.fill(password)
                    print("[OK] Filled password.")
                    page.wait_for_timeout(400)

        # Click submit inside modal
        for sel in ["button:has-text('Log in')", "button:has-text('Sign in')", "button[type='submit']"]:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                print(f"[OK] Clicked {sel}")
                break
    except Exception as e:
        print(f"[WARN] Filling modal: {e}")

    print("\nWaiting for authentication to sync...")
    for i in range(30):
        time.sleep(1)
        state = context.storage_state()
        for origin in state.get("origins", []):
            for item in origin.get("localStorage", []):
                if item.get("name") == "settings_userToken":
                    val = item.get("value", "").strip('"')
                    parts = val.split(".")
                    if len(parts) >= 2:
                        try:
                            payload_b64 = parts[1] + "=="
                            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
                            if payload.get("userID"):
                                print(f"[SUCCESS] Active User Token detected! User ID: {payload.get('userID')}, Subscription: {payload.get('subscriptionTiers')}")
                                context.storage_state(path=str(OUTPUT_PATH))
                                if SCHEDULE_PATH.parent.exists():
                                    context.storage_state(path=str(SCHEDULE_PATH))
                                browser.close()
                                sys.exit(0)
                        except Exception:
                            pass

    print("[INFO] Saving captured session...")
    context.storage_state(path=str(OUTPUT_PATH))
    if SCHEDULE_PATH.parent.exists():
        context.storage_state(path=str(SCHEDULE_PATH))
    browser.close()
