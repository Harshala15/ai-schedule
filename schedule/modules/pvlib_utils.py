"""Compact pvlib-based solar physics helpers for Step 1 prompts."""

from __future__ import annotations

import datetime as dt

import config


def _as_timezone_aware_timestamp(value: dt.datetime, timezone: str):
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize(timezone)
    return ts.tz_convert(timezone)


def build_pvlib_block_summary(
    reference_time: dt.datetime,
    num_blocks: int,
    *,
    latitude: float = config.PLANT_LAT,
    longitude: float = config.PLANT_LON,
    timezone: str = "Asia/Kolkata",
    tilt_deg: float | None = None,
    azimuth_deg: float | None = None,
    capacity_mw: float | None = None,
    performance_ratio: float | None = None,
    block_minutes: int | None = None,
) -> str:
    """Return a compact block-by-block pvlib summary for prompt use.

    The summary is intentionally small and human-readable so the LLM can
    use it as physics evidence without being flooded by raw irradiance
    tables.
    """
    try:
        import pandas as pd
        from pvlib.location import Location
        from pvlib import irradiance
    except Exception as exc:  # pragma: no cover - dependency may be absent locally
        return f"pvlib physics summary unavailable ({exc})."

    block_minutes = block_minutes or config.BLOCK_MINUTES
    tilt_deg = float(tilt_deg if tilt_deg is not None else getattr(config, "PLANT_TILT_DEG", 20.0))
    azimuth_deg = float(
        azimuth_deg
        if azimuth_deg is not None
        else 180.0 + float(getattr(config, "PLANT_ORIENTATION_FROM_SOUTH_DEG", 0.0))
    )
    capacity_mw = float(capacity_mw if capacity_mw is not None else getattr(config, "PLANT_CAPACITY_MW", 0.0))
    performance_ratio = float(
        performance_ratio if performance_ratio is not None else getattr(config, "PERFORMANCE_RATIO", 0.78)
    )

    base_ts = _as_timezone_aware_timestamp(reference_time, timezone)
    times = pd.date_range(
        start=base_ts,
        periods=num_blocks,
        freq=f"{block_minutes}min",
        tz=timezone,
    )

    location = Location(latitude=latitude, longitude=longitude, tz=timezone)
    solar_position = location.get_solarposition(times)
    clearsky = location.get_clearsky(times, model="ineichen")
    poa = irradiance.get_total_irradiance(
        surface_tilt=tilt_deg,
        surface_azimuth=azimuth_deg,
        solar_zenith=solar_position["apparent_zenith"],
        solar_azimuth=solar_position["azimuth"],
        dni=clearsky["dni"],
        ghi=clearsky["ghi"],
        dhi=clearsky["dhi"],
    )

    poa_global = poa["poa_global"].fillna(0.0).clip(lower=0.0)
    estimated_mw = (poa_global / 1000.0) * capacity_mw * performance_ratio

    lines = [
        (
            "pvlib physics summary "
            f"(lat={latitude}, lon={longitude}, tilt={tilt_deg} deg, azimuth={azimuth_deg} deg, "
            f"capacity={capacity_mw} MW, PR={performance_ratio}):"
        )
    ]

    for ts, zenith, elev, ghi, poa_w, mw in zip(
        times,
        solar_position["apparent_zenith"].fillna(0.0),
        solar_position["apparent_elevation"].fillna(0.0),
        clearsky["ghi"].fillna(0.0),
        poa_global,
        estimated_mw,
    ):
        local_label = ts.tz_convert(timezone).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"- {local_label}: solar_elev={float(elev):.2f} deg, "
            f"zenith={float(zenith):.2f} deg, clear_sky_ghi={float(ghi):.1f} W/m2, "
            f"poa={float(poa_w):.1f} W/m2, est_step1_mw={max(0.0, float(mw)):.3f}"
        )

    return "\n".join(lines)

