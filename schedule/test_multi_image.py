"""
FULL AUTOMATION (PREMIUM): Playwright captures Windy screenshots using your
logged-in Premium account, records a short animated video of the cloud
movement, then runs a LOCAL feature-extraction + ML pipeline (NO LLM) to
predict 15-minute-block solar generation.

Pipeline (see run_pipeline.py for the orchestration):
    Screenshots --> image_feature_extraction.py --\\
                                                     >-- feature_builder.py -> ml_forecast_model.py -> prediction_store.py
    Video       --> video_motion_features.py ------/

Setup:
1. pip install -r requirements.txt
2. playwright install chromium
3. Update PLANT_NAME / PLANT_LAT / PLANT_LON / PLANT_CAPACITY_MW in
   config.py with your actual plant details.

Run:
    python test_multi_image.py
"""

import time
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from config import (
    PLANT_NAME, PLANT_LAT, PLANT_LON, ZOOM_LEVEL, VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
    LAYERS, RECORD_ANIMATION_VIDEO, ANIMATION_LAYER, ANIMATION_RECORD_SECONDS as _CONFIG_ANIMATION_RECORD_SECONDS,
    VIDEO_DIR, STORAGE_STATE_PATH, SCREENSHOT_DIR, CAPTURE_TIMES,
)
from run_pipeline import run_prediction_pipeline

# Override: the final clean clip should be 25 seconds long, regardless of
# whatever value is set in config.py. If you'd rather control this from
# config.py instead, just set ANIMATION_RECORD_SECONDS = 25 there and
# remove this override line.
ANIMATION_RECORD_SECONDS = 25
IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST)


def now_ist_naive() -> datetime.datetime:
    return now_ist().replace(tzinfo=None)


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
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
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
    possible_texts = ["Accept", "I agree", "Got it", "Agree", "Close", "OK", "Allow all"]
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
    try:
        center_x = VIEWPORT_WIDTH // 2
        center_y = VIEWPORT_HEIGHT // 2

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


def capture_all_layers() -> dict:
    """
    Opens Windy.com for each layer in LAYERS (using the saved premium
    session). Putting lat/lon in the URL PATH (not just the query string)
    makes Windy treat it as a searched/picked location -- the same as
    manually typing coordinates into the search box -- which auto-opens
    the bottom wide hourly-forecast panel. No manual click is needed.
    Returns a dict of {filepath: description}.

    Each run's screenshots are saved into their OWN timestamped subfolder
    (windy_screenshots/<lat>_<lon>/<YYYY-MM-DD_HH-MM-SS>/<layer>.png)
    instead of a fixed filename, so a new run no longer overwrites and
    permanently deletes the previous run's screenshots. This doesn't
    change what goes into features_log.csv -- feature extraction already
    happened before the old file would've been overwritten -- it only
    means past screenshots are now kept around for debugging/reference.
    """
    captured = {}
    run_timestamp = now_ist().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = SCREENSHOT_DIR / run_timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
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

            out_path = run_dir / f"{overlay}.png"
            page.screenshot(path=str(out_path))
            captured[str(out_path)] = description
            print(f"  [OK] Saved {out_path}")

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

    print(f"\nRecording animation -- final clean clip will be {ANIMATION_RECORD_SECONDS}s of '{ANIMATION_LAYER}'...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
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
        seek_timeline_to_one_hour_ago(page)

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

        # Extra warm-up buffer so Windy has time to actually fetch/cache
        # the animation frames before we finish this recording session.
        CACHE_WARMUP_SECONDS = 6
        page.wait_for_timeout(CACHE_WARMUP_SECONDS * 1000)

        # skip_seconds = the REAL measured time it took to get through all
        # setup (page load, popups, seek, play, speed/forecast toggles,
        # marker, warm-up) -- i.e. exactly where real playback begins in
        # the raw video -- plus 14 more seconds trimmed off the start of
        # that playback (4s + another 10s).
        EXTRA_SKIP_SECONDS = 14
        skip_seconds = round(time.time() - video_start_time) + EXTRA_SKIP_SECONDS
        print(f"  Setup took ~{skip_seconds - EXTRA_SKIP_SECONDS}s before playback began -- "
              f"will trim the full recording at {skip_seconds}s and keep the next "
              f"{ANIMATION_RECORD_SECONDS}s as the clean clip.")

        time.sleep(ANIMATION_RECORD_SECONDS)

        video_obj = page.video
        context.close()  # finalizes and writes the video file
        browser.close()

        if video_obj is None:
            return None

        raw_path = Path(video_obj.path())

    # Rename from Playwright's random hash filename to something readable
    timestamp = now_ist().strftime("%Y-%m-%d_%H-%M-%S")
    full_path = VIDEO_DIR / f"{PLANT_NAME}_{ANIMATION_LAYER}_{timestamp}_full.webm"
    try:
        raw_path.rename(full_path)
    except Exception:
        full_path = raw_path  # fall back to the original name if rename fails

    print(f"  [OK] Full video saved: {full_path.resolve()}")

    # Trim starting right after real setup finished (skip_seconds, measured
    # above). Only ~11s of real footage remains after that skip (raw video
    # is setup_time + ANIMATION_RECORD_SECONDS, minus the skip), and we
    # additionally cut the last 1s off that, so the final clean clip is
    # ~10s. Requires ffmpeg to be installed and on PATH -- if it isn't, we
    # fall back to using the full (untrimmed) video instead.
    FINAL_CLIP_SECONDS = 10
    clean_path = VIDEO_DIR / f"{PLANT_NAME}_{ANIMATION_LAYER}_{timestamp}_clean.mp4"
    try:
        import subprocess
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(full_path),
                "-ss", str(skip_seconds),
                "-t", str(FINAL_CLIP_SECONDS),
                str(clean_path),
            ],
            check=True,
            capture_output=True,
        )
        print(f"  [OK] Clean trimmed clip saved: {clean_path.resolve()}")
        return clean_path
    except Exception as e:
        print(f"  [WARN] Could not trim video with ffmpeg ({e}). "
              f"Falling back to the full untrimmed video.")
        return full_path


def run_once():
    """Captures screenshots, records a cloud-animation video, and runs
    the LOCAL feature-extraction + ML prediction pipeline (no LLM)."""
    print("Step 1: Capturing screenshots (satellite + wind + solarpower + clouds + rain)...\n")
    image_map = capture_all_layers()

    print("\nStep 2: Recording cloud movement animation...")
    video_path = record_cloud_animation()

    print("\nStep 3: Running local feature-extraction + ML prediction pipeline (no LLM)...")
    run_prediction_pipeline(image_map, video_path, reference_time=now_ist_naive())


def _next_capture_datetime(now: datetime.datetime) -> datetime.datetime:
    """Returns the next scheduled capture time (today or tomorrow) from
    config.CAPTURE_TIMES, whichever comes first after `now`."""
    today_candidates = []
    for time_str in CAPTURE_TIMES:
        hour, minute = map(int, time_str.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            today_candidates.append(candidate)

    if today_candidates:
        return min(today_candidates)

    # All of today's capture times have passed -- schedule the first one tomorrow.
    hour, minute = map(int, CAPTURE_TIMES[0].split(":"))
    return (now + datetime.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def main():
    ensure_login()

    run_count = 0

    while True:
        next_run = _next_capture_datetime(now_ist())
        wait_seconds = (next_run - now_ist()).total_seconds()

        print(f"\nNext capture scheduled at {next_run.strftime('%Y-%m-%d %H:%M:%S')} "
              f"(waiting {wait_seconds / 60:.1f} minutes)...")
        print("(Press Ctrl+C to stop the script.)")
        time.sleep(max(0, wait_seconds))

        run_count += 1
        start_time = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        print("\n" + "#" * 60)
        print(f"# RUN {run_count} -- started at {start_time}")
        print("#" * 60)

        try:
            run_once()
        except Exception as e:
            print(f"\n[ERROR] Run {run_count} failed: {e}")


if __name__ == "__main__":
    main()
