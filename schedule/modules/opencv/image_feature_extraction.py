"""
image_feature_extraction.py

Extracts NUMERIC color + brightness features from each of the captured
Windy layer screenshots (satellite, wind, solarpower, clouds, rain).

WHY: Instead of asking an LLM to "look at" the screenshots and describe
them, we compute simple, deterministic image statistics -- average
brightness, average color saturation/hue, and the % of "bright" pixels
in a region centered on the plant. These numbers become inputs to the
ML model (see ml_forecast_model.py), the same way video_motion_features.py
turns the video into numbers instead of raw pixels.

NOTE: an OCR-based extension (reading exact numbers off the marker popup
and bottom forecast panel via Tesseract) was tried and removed for now,
since Tesseract-OCR isn't installed yet and the priority right now is a
STABLE feature schema for continuous data collection. The color/brightness
stats below are the only features this module produces -- no external
OCR dependency, no risk of the schema changing again while you collect
this week's training data. OCR can be re-added later as a separate step
once you're ready, without disturbing this file's current column set.

Usage:
    from image_feature_extraction import extract_image_features
    features = extract_image_features(image_map)   # image_map: {filepath: description}
"""

from pathlib import Path

import cv2
import numpy as np

# The Windy map does not fill the browser screenshot: headers and the
# timeline occupy its top/bottom edges. These fractions describe the map
# area for the fixed 1600x1000 capture viewport in config.py.
MAP_TOP_FRACTION = 0.08
MAP_BOTTOM_FRACTION = 0.24
ROI_WIDTH_FRACTION = 0.30
ROI_HEIGHT_FRACTION = 0.34
PLANT_MAP_Y_FRACTION = 0.64

# Pixels brighter than this (0-255, grayscale) count as "bright" -- used
# as a generic cloud/precipitation-intensity proxy across all layers.
BRIGHT_PIXEL_THRESHOLD = 150


def _get_roi_box(width, height):
    """Return a map-only ROI around the plant, excluding page controls."""
    map_top = int(height * MAP_TOP_FRACTION)
    map_bottom = int(height * (1.0 - MAP_BOTTOM_FRACTION))
    map_height = max(1, map_bottom - map_top)
    box_w = int(width * ROI_WIDTH_FRACTION)
    box_h = int(map_height * ROI_HEIGHT_FRACTION)
    x1 = (width - box_w) // 2
    plant_y = map_top + int(map_height * PLANT_MAP_Y_FRACTION)
    y1 = max(map_top, min(map_bottom - box_h, plant_y - box_h // 2))
    return x1, y1, x1 + box_w, y1 + box_h


def _layer_name_from_path(filepath: str) -> str:
    """windy_screenshots/.../satellite.png -> 'satellite'"""
    return Path(filepath).stem


def _extract_single_image_stats(filepath: str) -> dict:
    """
    Returns {avg_brightness, brightness_std, avg_saturation, avg_hue_deg,
    bright_pixel_pct} computed over the centered ROI of one screenshot.
    Returns None-filled dict if the image can't be read.

    brightness_std (standard deviation of grayscale pixel values in the
    ROI) is a patchiness/texture signal: a uniform overcast or uniform
    clear sky both give a LOW std (all pixels similar), while broken/
    scattered clouds -- which cause the rapid, intermittent generation
    swings that plain averages can't distinguish from steady haze -- give
    a HIGH std (a mix of bright and dark patches in the same ROI).
    """
    img = cv2.imread(filepath)  # BGR
    if img is None:
        return {
            "avg_brightness": None,
            "brightness_std": None,
            "avg_saturation": None,
            "avg_hue_deg": None,
            "bright_pixel_pct": None,
        }

    height, width = img.shape[:2]
    x1, y1, x2, y2 = _get_roi_box(width, height)
    roi = img[y1:y2, x1:x2]

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    avg_brightness = float(np.mean(gray))
    brightness_std = float(np.std(gray))
    avg_saturation = float(np.mean(hsv[..., 1]))

    # Hue is circular (0-179 in OpenCV, representing 0-360 degrees) --
    # average it as a circular mean (via unit vectors) instead of a
    # plain arithmetic mean, otherwise e.g. 350 deg and 10 deg would
    # wrongly average to 180 deg instead of 0 deg.
    hue_rad = hsv[..., 0].astype(np.float64) * (2 * np.pi / 180.0)
    mean_sin = float(np.mean(np.sin(hue_rad)))
    mean_cos = float(np.mean(np.cos(hue_rad)))
    avg_hue_deg = float(np.degrees(np.arctan2(mean_sin, mean_cos)) % 360)

    # Achromatic cloud detection (high brightness, low color saturation) to exclude terrain glare
    bright_mask = (gray > BRIGHT_PIXEL_THRESHOLD) & (hsv[..., 1] < 80)
    bright_pixels = int(np.count_nonzero(bright_mask))
    bright_pixel_pct = 100.0 * bright_pixels / gray.size

    return {
        "avg_brightness": round(avg_brightness, 2),
        "brightness_std": round(brightness_std, 2),
        "avg_saturation": round(avg_saturation, 2),
        "avg_hue_deg": round(avg_hue_deg, 2),
        "bright_pixel_pct": round(bright_pixel_pct, 2),
    }


def extract_image_features(image_map: dict) -> dict:
    """
    image_map: {filepath: description} as produced by capture_all_layers()
    in test_multi_image.py.

    Returns a FLAT dict with one set of 4 stats per layer, prefixed by
    layer name, e.g.:
        {
          "satellite_avg_brightness": 132.4,
          "satellite_avg_saturation": 18.2,
          "satellite_avg_hue_deg": 95.3,
          "satellite_bright_pixel_pct": 41.7,
          "clouds_avg_brightness": ...,
          ...
        }
    """
    features = {}
    for filepath in image_map:
        layer = _layer_name_from_path(filepath)
        stats = _extract_single_image_stats(filepath)
        for stat_name, value in stats.items():
            features[f"{layer}_{stat_name}"] = value
    return features


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python image_feature_extraction.py <screenshot_dir>")
        sys.exit(1)

    shot_dir = Path(sys.argv[1])
    fake_map = {str(p): p.stem for p in shot_dir.glob("*.png")}
    result = extract_image_features(fake_map)
    for k, v in result.items():
        print(f"{k}: {v}")
