"""
Playwright automation for Windy capture.

This script logs into Windy Premium, captures the configured map-layer
screenshots, and records the satellite animation clip.

Run:
    python test_multi_image.py
"""

import re
import argparse
import time
import datetime
import json
import mimetypes
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright
try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - optional if you only want local capture
    boto3 = None
    ClientError = Exception

from config import (
    SITES,
    PLANT_NAME, PLANT_LAT, PLANT_LON, ZOOM_LEVEL, VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
    LAYERS, RECORD_ANIMATION_VIDEO, ANIMATION_LAYER,
    VIDEO_DIR, STORAGE_STATE_PATH, SCREENSHOT_DIR, RUN_INTERVAL_SECONDS,
    LAMBDA_GATED_VIDEO_SITES, REVISION_TIMES, LAMBDA_CAPTURE_OFFSET_MINUTES,
    LAMBDA_CAPTURE_WINDOW_MINUTES,
    S3_BUCKET_NAME, S3_REGION, S3_PREFIX, AUTO_CREATE_S3_BUCKET,
    OUTPUT_ROOT, IS_LAMBDA, PLANT_ID, SITE_ID, PLANT_CAPACITY_MW,
)

IST_TIMEZONE = "Asia/Kolkata"
IST = ZoneInfo(IST_TIMEZONE)


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _lambda_capture_window(site_name: str, current_time: datetime.datetime | None = None) -> tuple[bool, str]:
    """
    Lambda capture gate for revision-scheduled sites.

    Gated site videos should only run before the configured revision times.
    Other sites are left un-gated by this helper.
    """
    normalized_site_name = site_name.strip().upper()
    if normalized_site_name not in LAMBDA_GATED_VIDEO_SITES:
        return True, "no revision window gating configured"

    current_time = current_time or now_ist()
    capture_offset = datetime.timedelta(minutes=LAMBDA_CAPTURE_OFFSET_MINUTES)
    capture_window = datetime.timedelta(minutes=LAMBDA_CAPTURE_WINDOW_MINUTES)

    for revision_time in REVISION_TIMES:
        rev_hour, rev_minute = (int(part) for part in revision_time.split(":"))
        revision_dt = current_time.replace(
            hour=rev_hour,
            minute=rev_minute,
            second=0,
            microsecond=0,
        )
        capture_time = revision_dt - capture_offset
        window_end = capture_time + capture_window

        if capture_time <= current_time < window_end:
            return True, (
                f"matched capture window for revision {revision_time} "
                f"({capture_time.strftime('%H:%M')} - {window_end.strftime('%H:%M')} IST)"
            )

    capture_times = []
    for revision_time in REVISION_TIMES:
        rev_hour, rev_minute = (int(part) for part in revision_time.split(":"))
        capture_time = (current_time.replace(
            hour=rev_hour,
            minute=rev_minute,
            second=0,
            microsecond=0,
        ) - capture_offset).strftime("%H:%M")
        window_end = (
            current_time.replace(
                hour=rev_hour,
                minute=rev_minute,
                second=0,
                microsecond=0,
            )
        ).strftime("%H:%M")
        capture_times.append(f"{capture_time}-{window_end}")

    return False, (
        f"outside {normalized_site_name} capture window; allowed capture windows are "
        + ", ".join(capture_times)
    )


def set_active_site(site: dict) -> None:
    global PLANT_NAME, PLANT_LAT, PLANT_LON, S3_PREFIX, SCREENSHOT_DIR, PLANT_CAPACITY_MW

    PLANT_NAME = site["name"]
    PLANT_LAT = site["lat"]
    PLANT_LON = site["lon"]
    S3_PREFIX = site["s3_prefix"]
    PLANT_CAPACITY_MW = site.get("capacity_mw")
    SCREENSHOT_DIR = OUTPUT_ROOT / "windy_screenshots" / f"{PLANT_LAT}_{PLANT_LON}"
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _find_site(site_id: str) -> dict:
    normalized_site_id = site_id.strip().upper()
    for site in SITES:
        if site["name"].upper() == normalized_site_id:
            return site
    valid_sites = ", ".join(site["name"] for site in SITES)
    raise ValueError(f"Unknown SITE_ID '{site_id}'. Valid SITE_ID values: {valid_sites}")


def launch_chromium(playwright, headless: bool = True):
    if not IS_LAMBDA:
        return playwright.chromium.launch(headless=headless)

    return playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            "--no-zygote",
        ],
    )


def ensure_login():
    """First run only: opens a VISIBLE browser so you can log in to your
    Windy Premium account. Saves the session so future runs are headless
    and still use your premium access."""
    if STORAGE_STATE_PATH.exists():
        return

    print("No saved login found.")
    print("A browser window will open -- please log in to your Windy")
    print("PREMIUM account there, then come back here and press Enter.")

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=False)
        context = browser.new_context(timezone_id=IST_TIMEZONE)
        page = context.new_page()
        page.goto("https://www.windy.com/", wait_until="domcontentloaded", timeout=60000)
        input("Press Enter here once you are logged in on the browser window... ")
        context.storage_state(path=str(STORAGE_STATE_PATH))
        browser.close()

    print("Login saved to windy_login.json. Future runs will be automatic.\n")


def dismiss_popups(page):
    """Tries to close any cookie-consent / install-app / promo popups that
    Windy sometimes shows, since these can sit on top of the map/panel in
    the screenshot."""
    possible_texts = [
        "Accept", "I agree", "Got it", "Agree", "Close", "OK", "Allow all",
        # Windy's "How should we serve you a location forecast?" chooser --
        # click the classic option so map behavior/layout stays what the
        # rest of this script expects, instead of the AI-enhanced view.
        "Classic forecast",
    ]
    for text in possible_texts:
        try:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible(timeout=1000):
                btn.click(timeout=1000)
                page.wait_for_timeout(500)
        except Exception:
            pass

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def set_weather_picker_point(page):
    """
    Right-clicks the plant's location on the map (map center, since the
    page is already centered on PLANT_LAT/PLANT_LON) to open Windy's
    right-click context menu, then clicks "Show weather picker" from that
    menu. This drops Windy's own weather-picker point exactly on the
    plant's coordinates -- this is what should be visible in the
    screenshot, instead of a leftover/older picked point.
    """
    center_x = VIEWPORT_WIDTH // 2
    center_y = VIEWPORT_HEIGHT // 2

    try:
        page.mouse.click(center_x, center_y, button="right")
        print(f"  Right-clicked map center ({center_x}, {center_y}) to open context menu.")
        page.wait_for_timeout(1000)

        picker_option = page.get_by_text("Show weather picker", exact=False).first
        if picker_option.is_visible(timeout=3000):
            picker_option.click(timeout=3000)
            print("  Clicked 'Show weather picker' from context menu.")
            page.wait_for_timeout(1500)  # let the weather-picker point render
        else:
            print("  [WARN] 'Show weather picker' option not visible in context menu.")
    except Exception as e:
        print(f"  [WARN] Could not open weather picker via right-click: {e}")

    # Safety net: make sure the right-click context menu isn't still
    # sitting open over the map. If it remains visible, it can obscure the
    # screenshot and capture the wrong map state.
    try:
        if page.get_by_text("Show weather picker", exact=False).first.is_visible(timeout=500):
            print("  [WARN] Context menu still open after picker click -- forcing it closed.")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
    except Exception:
        pass


def _make_s3_key(run_timestamp: str, asset_kind: str, filename: str) -> str:
    date_part = run_timestamp[:10]
    if IS_LAMBDA:
        lambda_asset_kind = "screenshots" if asset_kind == "images" else asset_kind
        return f"raw/{PLANT_ID}/{PLANT_NAME}/{date_part}/windy/{lambda_asset_kind}/{filename}"
    return f"{S3_PREFIX}/{date_part}/{asset_kind}/{filename}"


def _s3_client():
    if boto3 is None:
        return None
    return boto3.client("s3", region_name=S3_REGION)


def _ensure_s3_bucket() -> bool:
    client = _s3_client()
    if client is None or not S3_BUCKET_NAME:
        return False

    # If auto-create is disabled, we assume the bucket already exists and
    # avoid a head-bucket probe that can fail with AccessDenied even when
    # the bucket is present. Upload attempts will provide the real signal.
    if not AUTO_CREATE_S3_BUCKET:
        print(f"  [INFO] Using existing S3 bucket: {S3_BUCKET_NAME}")
        return True

    try:
        client.head_bucket(Bucket=S3_BUCKET_NAME)
        return True
    except ClientError as e:
        print(f"  [WARN] Could not verify S3 bucket {S3_BUCKET_NAME}: {e}")

    try:
        if S3_REGION == "us-east-1":
            client.create_bucket(Bucket=S3_BUCKET_NAME)
        else:
            client.create_bucket(
                Bucket=S3_BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": S3_REGION},
            )
        print(f"  [OK] Created S3 bucket: {S3_BUCKET_NAME}")
        return True
    except ClientError as e:
        print(f"  [WARN] Could not create S3 bucket {S3_BUCKET_NAME}: {e}")
        return False


def _upload_file_to_s3(local_path: Path, s3_key: str) -> None:
    client = _s3_client()
    if client is None:
        print("  [WARN] boto3 is not installed -- skipping S3 upload.")
        return
    if not S3_BUCKET_NAME:
        print("  [WARN] S3_BUCKET_NAME is not set -- skipping S3 upload.")
        return

    content_type, _ = mimetypes.guess_type(str(local_path))
    extra_args = {"ContentType": content_type} if content_type else None

    for attempt in range(1, 4):
        try:
            if extra_args:
                client.upload_file(str(local_path), S3_BUCKET_NAME, s3_key, ExtraArgs=extra_args)
            else:
                client.upload_file(str(local_path), S3_BUCKET_NAME, s3_key)
            print(f"  [OK] Uploaded to s3://{S3_BUCKET_NAME}/{s3_key}")
            return
        except Exception as e:
            print(f"  [WARN] Upload attempt {attempt}/3 failed for {local_path.name}: {e}")
            if attempt < 3:
                time.sleep(2)

    print(f"  [WARN] Failed to upload {local_path.name} to S3 after 3 attempts.")


def capture_all_layers() -> dict:
    """
    Opens Windy.com for each layer in LAYERS (using the saved premium
    session). Putting lat/lon in the URL PATH (not just the query string)
    makes Windy treat it as a searched/picked location -- the same as
    manually typing coordinates into the search box -- which auto-opens
    the bottom wide hourly-forecast panel. No manual click is needed.
    Returns a dict of {filepath: description}.

    Each run's screenshots are saved into their OWN timestamped local
    subfolder (windy_screenshots/<lat>_<lon>/<YYYY-MM-DD_HH-MM-SS>/<layer>.png)
    for feature extraction and debugging.
    """
    captured = {}
    run_timestamp = now_ist().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = SCREENSHOT_DIR / run_timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            timezone_id=IST_TIMEZONE,
        )
        page = context.new_page()

        for overlay, description in LAYERS.items():
            url = (
                f"https://www.windy.com/{PLANT_LAT}/{PLANT_LON}"
                f"?{overlay},{PLANT_LAT},{PLANT_LON},{ZOOM_LEVEL},p:cities"
            )
            print(f"Opening layer '{overlay}' -> {url}")

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)  # let map tiles fully render

            dismiss_popups(page)
            page.wait_for_timeout(2000)  # let bottom forecast panel finish animating in

            # Right-click the plant's location and select "Show weather
            # picker" so the correct point is set on the map before the
            # screenshot is taken (instead of an old/leftover picked point).
            set_weather_picker_point(page)

            if overlay == "wind":
                # Windy's "What's New" promo panel shows up under the
                # Premium badge (top-right) on the wind layer -- click its
                # X to close it before the screenshot is taken.
                close_x = int(VIEWPORT_WIDTH * 0.63)
                close_y = int(VIEWPORT_HEIGHT * 0.095)
                page.mouse.click(close_x, close_y)
                page.wait_for_timeout(500)

            out_path = run_dir / f"{overlay}.png"
            page.screenshot(path=str(out_path))
            captured[str(out_path)] = description
            print(f"  [OK] Saved {out_path}")

            s3_name = f"{PLANT_NAME}_{run_timestamp}_{overlay}.png"
            _upload_file_to_s3(out_path, _make_s3_key(run_timestamp, "images", s3_name))

        browser.close()

    return captured


def dismiss_timeline_overlay(page):
    """Some layers (e.g. satellite nowcast) show a white info box
    on top of the timeline -- things like '6:43 PM - 5h ago', '24h ago /
    6h ago / 1h ago / Next 1h', 'Overlay with radar', 'Blue / Visible /
    Infra', 'More options...'. This box can sit on top of (or hide) the
    play button, so it needs to be dismissed first. Since the exact class
    name can vary by layer, this tries several likely selectors in order
    and moves on quietly if none match (nothing to close = fine)."""
    candidates = [
        "div.closing-x",
        "[aria-label='Close']",
        "[title='Close']",
        "div.timeline-info .close",
        "div.nowcast-info .close",
        "svg[class*='close']",
        "div[class*='closing']",
    ]
    for sel in candidates:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                el.click(timeout=1000)
                print(f"  Closed overlay using selector: {sel}")
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    print("  [INFO] No closable overlay found (may already be closed) -- continuing.")
    return False


def seek_timeline_to_one_hour_ago(page) -> bool:
    """
    Clicks the '1h ago' label/tick on the nowcast timeline so the playhead
    jumps back to an hour in the past BEFORE play is pressed. Combined
    with clicking play afterwards and "Play with forecast" being enabled,
    this makes the recorded animation move from ~1 hour ago, through
    'now', and on to the forecast frames -- instead of only showing
    forecast frames starting from wherever 'now' happened to be.
    """
    candidates_text = ["1h ago", "1 h ago", "-1h", "1hr ago"]
    for text in candidates_text:
        try:
            el = page.get_by_text(text, exact=False).first
            if el.is_visible(timeout=2000):
                el.click(timeout=2000)
                print(f"  Seeked timeline back using label: '{text}'")
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue

    print("  [WARN] Could not seek timeline to '1h ago' automatically -- "
          "the animation may start from 'now' instead of an hour in the "
          "past. Inspect the timeline's '1h ago' tick/label (right-click "
          "-> Inspect) and add its exact selector to "
          "seek_timeline_to_one_hour_ago().")
    return False


def click_play_button(page, attempts: int = 3, per_try_timeout: int = 4000) -> bool:
    """Tries several likely selectors for the play (>) button, since
    different Windy layers (clouds vs satellite nowcast) use
    different play controls. Confirmed working selector for the Clouds
    layer is 'div.play-pause' -- the others are fallbacks for layers
    (like Satellite) that use a different widget.

    Retries the whole candidate list up to `attempts` times with a short
    pause in between, since the button can simply not be rendered yet on
    slower page loads -- a longer per-try timeout and a couple of retries
    fixes most "could not find play button" flakiness without needing an
    exact selector."""
    candidates = [
        "div.play-pause",
        "[title='Play']",
        "[aria-label='Play']",
        "svg[class*='play']",
        "div[class*='play']",
        ".ecmwf-timeline .play",
        "button[class*='play']",
    ]
    for attempt in range(1, attempts + 1):
        for sel in candidates:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=per_try_timeout):
                    el.click(timeout=per_try_timeout)
                    print(f"  Clicked play using selector: {sel} (attempt {attempt})")
                    return True
            except Exception:
                continue
        if attempt < attempts:
            print(f"  [INFO] Play button not found yet (attempt {attempt}/{attempts}) -- waiting and retrying...")
            page.wait_for_timeout(2500)
    print("  [WARN] Could not find any play button automatically -- "
          "the video will still record, but the map may stay static. "
          "Inspect the real play button (right-click -> Inspect) and "
          "add its exact selector to the 'candidates' list above.")
    return False


def click_play_with_forecast(page) -> bool:
    """
    Enables the 'Play with forecast' toggle that sits next to the
    play/pause button on Satellite/nowcast layers (visible in your
    screenshot, to the right of the timeline). Since the exact class name
    isn't confirmed via inspect yet, this tries a few reasonable ways to
    find and click it, in order:
      1. Click directly on the "Play with forecast" text label -- on most
         sites clicking a toggle's label also flips the toggle.
      2. If that doesn't work, look for a toggle/checkbox/switch element
         that sits immediately next to that text and click it directly.
    """
    try:
        label = page.get_by_text("Play with forecast", exact=False).first
        if label.is_visible(timeout=3000):
            label.click(timeout=3000)
            print("  Clicked 'Play with forecast' label to enable it.")
            page.wait_for_timeout(500)
            return True
    except Exception as e:
        print(f"  [DEBUG] Clicking 'Play with forecast' label failed: {e}")

    # Fallback: try clicking a toggle/switch/checkbox element sitting right
    # next to the label (covers the case where the label itself isn't
    # clickable and the actual switch is a separate sibling element).
    fallback_selectors = [
        "xpath=//*[contains(text(),'Play with forecast')]/following-sibling::*[1]",
        "xpath=//*[contains(text(),'Play with forecast')]/parent::*//*[contains(@class,'checkbox')]",
        "xpath=//*[contains(text(),'Play with forecast')]/parent::*//*[contains(@class,'switch')]",
        "xpath=//*[contains(text(),'Play with forecast')]/parent::*//*[contains(@class,'toggle')]",
    ]
    for sel in fallback_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click(timeout=1500)
                print(f"  Clicked 'Play with forecast' toggle using fallback selector: {sel}")
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue

    print("  [WARN] Could not enable 'Play with forecast' automatically -- "
          "inspect the actual toggle element (right-click -> Inspect) next "
          "to that text and add its exact selector to click_play_with_forecast().")
    return False


def click_slow_animation_speed(page) -> bool:
    """
    Selects the SLOWEST playback speed. Windy shows this as a row of
    THREE ICON-ONLY buttons (turtle, rabbit, llama -- no visible text
    label like "Speed"), sitting immediately to the LEFT of the
    "Play with forecast" toggle on the same row. Because there's no text
    to search for, this locates "Play with forecast" first (which IS
    text, and already works reliably elsewhere in this script), then
    clicks at a pixel offset to its left where the turtle (slowest, i.e.
    first/leftmost) icon sits.
    """
    try:
        label = page.get_by_text("Play with forecast", exact=False).first
        box = label.bounding_box(timeout=3000)
        if box:
            # The 3 icons (turtle, rabbit, llama) sit just to the left of
            # "Play with forecast", each roughly ~28-30px wide with small
            # gaps between them. The turtle (slowest) is the FIRST/
            # leftmost of the three, so it's roughly 2.5 icon-widths away
            # from the left edge of the "Play with forecast" label.
            icon_width = 30
            turtle_x = box["x"] - (icon_width * 2.5)
            turtle_y = box["y"] + box["height"] / 2

            page.mouse.click(turtle_x, turtle_y)
            print(f"  Clicked slow (turtle) speed icon at approx ({turtle_x:.0f}, {turtle_y:.0f}), "
                  f"based on position relative to 'Play with forecast'.")
            page.wait_for_timeout(500)
            return True
        else:
            print("  [WARN] Could not get bounding box for 'Play with forecast' label.")
    except Exception as e:
        print(f"  [DEBUG] Position-based speed click failed: {e}")

    # Fallback: in case some Windy layer/version DOES expose usable
    # title/aria-label attributes on these icons.
    fallback_selectors = [
        "[title*='slow' i]",
        "[aria-label*='slow' i]",
        "[title*='turtle' i]",
        "[aria-label*='turtle' i]",
    ]
    for sel in fallback_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click(timeout=1500)
                print(f"  Selected slow animation speed using fallback selector: {sel}")
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue

    print("  [WARN] Could not select slow animation speed automatically -- "
          "the animation will play at whatever speed is currently selected.")
    return False


def click_plant_marker(page):

    """
    Clicks the center of the map viewport once, so Windy drops a pin/
    pointer marker exactly on the plant's coordinates. Since the page URL
    already centers the map on (PLANT_LAT, PLANT_LON), the center of the
    viewport IS the plant's location -- so a plain center-click places
    the marker there without needing to search/type coordinates again.
    """
    try:
        center_x = VIEWPORT_WIDTH // 2
        center_y = VIEWPORT_HEIGHT // 2
        page.mouse.click(center_x, center_y)
        print(f"  Clicked map center ({center_x}, {center_y}) to drop a pointer on the plant location.")
        page.wait_for_timeout(1000)  # let the marker/pin render
    except Exception as e:
        print(f"  [WARN] Could not click map to place pointer: {e}")


TIMELINE_LABEL_SELECTOR = r"text=/\d{1,2}:\d{2}\s*(AM|PM)\s*-\s*(\d+\s*m\s*ago|in\s)/i"


def read_timeline_offset_minutes(page):
    """
    Reads Windy's own on-screen animation frame-time label (the small
    bubble that reads e.g. "10:20 AM - 8m ago" or "10:25 AM - in 40m")
    and returns how many minutes that displayed frame is from "now"
    (negative = in the past, positive = in the future). Returns None if
    the label isn't readable/parseable right now (e.g. mid re-render).

    This is real page text (confirmed via Playwright, not an image), so
    it's read directly -- no OCR, no pixel-brightness guessing.
    """
    try:
        text = page.locator(TIMELINE_LABEL_SELECTOR).first.text_content(timeout=500)
    except Exception:
        return None
    if not text:
        return None

    ago_match = re.search(r"(\d+)\s*m\s*ago", text)
    if ago_match:
        return -int(ago_match.group(1))

    in_match = re.search(r"in\s*(?:(\d+)\s*h\s*)?(\d+)\s*m", text)
    if in_match:
        hours = int(in_match.group(1)) if in_match.group(1) else 0
        minutes = int(in_match.group(2))
        return hours * 60 + minutes

    return None


def record_cloud_animation() -> Path | None:
    """
    Records a short video of the animated Clouds layer (time-lapse cloud
    movement) around the plant, using Playwright's built-in video
    recording. Returns the path to the saved video, or None if recording
    failed.

    This uses a SEPARATE browser context from capture_all_layers() because
    Playwright only starts recording once a context is created with
    record_video_dir set, and only finalizes/saves the file once that
    context is closed.
    """
    if not RECORD_ANIMATION_VIDEO:
        return None

    print(f"\nRecording animation -- watching Windy's on-screen frame-time label for "
          f"'{ANIMATION_LAYER}' to find exactly one clean loop of playback...")

    with sync_playwright() as p:
        browser = launch_chromium(p, headless=True)
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            timezone_id=IST_TIMEZONE,
            record_video_dir=str(VIDEO_DIR),
            record_video_size={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        )

        # Playwright starts recording the video the moment this context is
        # created (not from page.goto()) -- so THIS is the true t=0 for the
        # video file. Capturing it here lets us later compute exactly how
        # many real seconds our setup steps (page load, popups, seek, play,
        # speed/forecast toggles, marker) actually took, instead of
        # guessing a fixed number -- that real number is what tells us
        # WHERE in the raw video actual playback begins.
        video_start_time = time.time()

        page = context.new_page()

        # IMPORTANT: some layers (e.g. Satellite) only get a real
        # animated timeline + play button on Windy's DEDICATED nowcast
        # page (URL pattern "/-<Layer>-<layer>?..."). The generic
        # "/{lat}/{lon}?{layer},..." URL used for screenshots instead opens
        # the normal static forecast page for that layer, which has no
        # play button at all -- that was why the recording stayed static.
        DEDICATED_NOWCAST_URLS = {
            "satellite": "https://www.windy.com/-Satellite-satellite?satellite,{lat},{lon},{zoom},p:cities",
        }

        if ANIMATION_LAYER in DEDICATED_NOWCAST_URLS:
            url = DEDICATED_NOWCAST_URLS[ANIMATION_LAYER].format(
                lat=PLANT_LAT, lon=PLANT_LON, zoom=ZOOM_LEVEL
            )
        else:
            url = (
                f"https://www.windy.com/{PLANT_LAT}/{PLANT_LON}"
                f"?{ANIMATION_LAYER},{PLANT_LAT},{PLANT_LON},{ZOOM_LEVEL},p:cities"
            )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait long enough for the map tiles AND the timeline/animation
        # frames to fully load before we click play. Clicking play too
        # early (while frames are still loading) causes the animation to
        # already be partway through by the time it's actually playing
        # smoothly -- this longer wait fixes that.
        page.wait_for_timeout(15000)
        dismiss_popups(page)
        page.wait_for_timeout(1500)

        # Step 1: close any overlay/info box (e.g. the white timeline info
        # box that the Satellite layer shows) that may be sitting on top
        # of, or hiding, the play button.
        dismiss_timeline_overlay(page)

        # Step 1b: seek the playhead back to "1h ago" BEFORE pressing play,
        # so the recorded animation covers roughly the last hour through
        # to the next hour, instead of starting from "now" onward only.
        # If the label isn't found yet (page/timeline still rendering),
        # retry a few times.
        for seek_attempt in range(1, 4):
            if seek_timeline_to_one_hour_ago(page):
                break
            if seek_attempt < 3:
                print(f"  Retrying timeline seek (attempt {seek_attempt + 1}/3)...")
                page.wait_for_timeout(1500)

        # Step 2: click the play button to start the time-lapse animation.
        click_play_button(page)

        # Step 2b: select the slowest playback speed so the recorded clip
        # plays back smoothly instead of jumping through frames too fast.
        click_slow_animation_speed(page)

        # Step 2c: also enable "Play with forecast" so the animation
        # continues seamlessly from the nowcast into the forecast frames.
        click_play_with_forecast(page)

        # Step 3: click on the map at the plant's coordinates once, so a
        # pointer/marker is dropped there for the recording.
        click_plant_marker(page)

        # ---- Precisely capture exactly one full "-1h..+1h" animation
        # sweep, using Windy's OWN on-screen frame-time label as ground
        # truth -- instead of guessing how many wall-clock seconds that
        # takes (unreliable: depends on network/tile-load speed, and
        # confirmed by inspecting real captures to sometimes land
        # mid-sweep rather than exactly at "-1h ago"). The animation
        # loops, and each loop restart shows up as the label jumping
        # BACKWARD by far more than normal per-tick movement -- e.g.
        # "10:45 AM - in 1h 0m" -> "8:47 AM - 58m ago". We watch for that
        # jump happening twice; the real elapsed time between those two
        # jumps is exactly one clean, complete sweep, with no guessing.
        JUMP_MINUTES_THRESHOLD = 20  # a real loop restart jumps back by far more than one tick ever does
        POLL_INTERVAL_MS = 200
        MAX_POLL_SECONDS = 30  # safety cap in case the label can't be read at all this run

        jump_times = []
        prev_offset = None
        poll_deadline = time.time() + MAX_POLL_SECONDS
        while time.time() < poll_deadline and len(jump_times) < 2:
            offset = read_timeline_offset_minutes(page)
            if offset is not None:
                if prev_offset is not None and (offset - prev_offset) < -JUMP_MINUTES_THRESHOLD:
                    jump_times.append(time.time())
                    print(f"  Timeline loop restart #{len(jump_times)} detected via on-screen "
                          f"label ({prev_offset:+d}m -> {offset:+d}m).")
                prev_offset = offset
            page.wait_for_timeout(POLL_INTERVAL_MS)

        sweep_start_time = sweep_end_time = None
        if len(jump_times) == 2:
            sweep_start_time, sweep_end_time = jump_times
            print(f"  Found one full, clean timeline sweep: "
                  f"{sweep_end_time - sweep_start_time:.1f}s of real playback.")
        else:
            print(f"  [WARN] Could not detect a full timeline sweep from the on-screen "
                  f"label within {MAX_POLL_SECONDS}s -- falling back to the fixed-duration trim.")

        page.wait_for_timeout(1000)  # small buffer so ffmpeg has full frames right at the boundary

        video_obj = page.video
        print("  [INFO] Finalizing Playwright video context...")
        context.close()  # finalizes and writes the video file
        browser.close()

        if video_obj is None:
            return None

        print("  [INFO] Resolving raw video file path...")
        raw_path = Path(video_obj.path())

    # Rename from Playwright's random hash filename to something readable
    timestamp = now_ist().strftime("%Y-%m-%d_%H-%M-%S")
    full_path = VIDEO_DIR / f"{PLANT_NAME}_{ANIMATION_LAYER}_{timestamp}_full.webm"
    try:
        raw_path.rename(full_path)
    except Exception:
        full_path = raw_path  # fall back to the original name if rename fails

    print(f"  [OK] Full video saved: {full_path.resolve()}")
    print("  [INFO] Uploading raw full video to S3...")
    _upload_file_to_s3(full_path, _make_s3_key(timestamp, "videos", full_path.name))

    # Trim the raw recording down to exactly one clean sweep. Preferred
    # path: use the DOM-verified sweep_start_time/sweep_end_time (real
    # timestamps of the two on-screen-label loop restarts) for a precise,
    # ground-truth cut -- no guessing. Falls back to the old wall-clock
    # estimate (setup time + a fixed 11s window) only if the label
    # couldn't be read this run. Requires ffmpeg to be installed and on
    # PATH -- if it isn't, we fall back to using the full video instead.
    if sweep_start_time is not None:
        skip_seconds = round(sweep_start_time - video_start_time, 2)
        clip_seconds = round(sweep_end_time - sweep_start_time, 2)
    else:
        EXTRA_SKIP_SECONDS = 14
        skip_seconds = round(time.time() - video_start_time) + EXTRA_SKIP_SECONDS
        clip_seconds = 11

    clean_path = VIDEO_DIR / f"{PLANT_NAME}_{ANIMATION_LAYER}_{timestamp}_clean.mp4"
    try:
        import subprocess
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(full_path),
                "-ss", str(skip_seconds),
                "-t", str(clip_seconds),
                str(clean_path),
            ],
            check=True,
            capture_output=True,
        )
        print(f"  [OK] Clean trimmed clip saved: {clean_path.resolve()}")
        final_video_path = clean_path
    except Exception as e:
        print(f"  [WARN] Could not trim video with ffmpeg ({e}). "
              f"Falling back to the full untrimmed video.")
        final_video_path = full_path

    if final_video_path != full_path:
        print("  [INFO] Uploading trimmed video to S3...")
        _upload_file_to_s3(final_video_path, _make_s3_key(timestamp, "videos", final_video_path.name))
    return final_video_path


def run_once():
    """Captures screenshots and records the cloud-animation video."""
    print("Step 1: Capturing screenshots (satellite + wind + solarpower + clouds + rain)...\n")
    capture_all_layers()

    print("\nStep 2: Recording cloud movement animation...")
    record_cloud_animation()


def lambda_run_once() -> dict:
    """Lambda capture path: one site, video and metadata only, then exit."""
    print("Step 1: Skipping Lambda screenshots by design.\n")
    screenshots = []

    print("\nStep 2: Recording Lambda animation video...")
    video_path = record_cloud_animation()

    run_timestamp = now_ist().strftime("%Y-%m-%d_%H-%M-%S")
    metadata = {
        "site_id": PLANT_NAME,
        "plant_id": PLANT_ID,
        "capacity_mw": PLANT_CAPACITY_MW,
        "lat": PLANT_LAT,
        "lon": PLANT_LON,
        "captured_at": run_timestamp,
        "screenshots": screenshots,
        "video": str(video_path) if video_path else None,
    }
    metadata_dir = OUTPUT_ROOT / "windy_metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = metadata_dir / f"{PLANT_NAME}_{run_timestamp}.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    _upload_file_to_s3(
        metadata_path,
        _make_s3_key(run_timestamp, "metadata", metadata_path.name),
    )
    return metadata


RUN_INTERVAL = RUN_INTERVAL_SECONDS


def main(run_once_only: bool = False):
    ensure_login()
    _ensure_s3_bucket()

    run_count = 0

    while True:
        run_count += 1
        start_time = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        print("\n" + "#" * 60)
        print(f"# RUN {run_count} -- started at {start_time}")
        print("#" * 60)

        for site in SITES:
            set_active_site(site)
            print(f"\nSite: {PLANT_NAME} ({PLANT_LAT}, {PLANT_LON})")

            try:
                run_once()
            except Exception as e:
                print(f"\n[ERROR] Run {run_count} failed for {PLANT_NAME}: {e}")

        next_run_time = (
            now_ist() + datetime.timedelta(seconds=RUN_INTERVAL)
        ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"\nWaiting {RUN_INTERVAL // 60} minutes... next run at approximately {next_run_time}")
        print("(Press Ctrl+C to stop the script.)")

        if run_once_only:
            break

        time.sleep(RUN_INTERVAL)


def lambda_handler(event, context):
    """AWS Lambda entry point. EventBridge should handle the schedule."""
    global S3_BUCKET_NAME, PLANT_ID

    event = event or {}
    event_site_id = str(event.get("site_id") or SITE_ID or "").strip().upper()
    event_plant_id = str(event.get("plant_id") or PLANT_ID or "vedanjay").strip()
    event_bucket = str(event.get("bucket") or os.getenv("S3_BUCKET") or S3_BUCKET_NAME).strip()
    force_capture = _as_bool(event.get("force_capture") or os.getenv("FORCE_CAPTURE"), default=False)

    if not event_site_id:
        raise ValueError("SITE_ID is required as a Lambda environment variable or event field 'site_id'.")

    PLANT_ID = event_plant_id
    S3_BUCKET_NAME = event_bucket

    run_count = getattr(context, "aws_request_id", "lambda")
    started_at = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "#" * 60)
    print(f"# LAMBDA RUN {run_count} -- started at {started_at}")
    print("#" * 60)

    site = _find_site(event_site_id)
    set_active_site(site)
    print(f"\nSite: {PLANT_NAME} ({PLANT_LAT}, {PLANT_LON})")

    should_capture, capture_reason = _lambda_capture_window(PLANT_NAME)
    if not should_capture and not force_capture:
        print(f"\n[INFO] Skipping Lambda capture for {PLANT_NAME}: {capture_reason}")
        return {
            "ok": True,
            "skipped": True,
            "started_at": started_at,
            "site": PLANT_NAME,
            "reason": capture_reason,
        }
    if force_capture and not should_capture:
        print(f"\n[INFO] Force capture enabled for {PLANT_NAME}; bypassing gate: {capture_reason}")

    ensure_login()
    _ensure_s3_bucket()

    try:
        metadata = lambda_run_once()
        return {
            "ok": True,
            "started_at": started_at,
            "site": PLANT_NAME,
            "force_capture": force_capture,
            "metadata": metadata,
        }
    except Exception as e:
        print(f"\n[ERROR] Lambda run failed for {PLANT_NAME}: {e}")
        return {"ok": False, "started_at": started_at, "site": PLANT_NAME, "error": str(e)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capture Windy screenshots and animation video.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one capture cycle and exit instead of looping forever.",
    )
    args = parser.parse_args()
    main(run_once_only=args.once)
