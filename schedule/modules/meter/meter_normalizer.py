"""
Universal SCADA Meter Data Normalizer for Indian Solar Power Plants.

Normalizes diverse site-specific telemetry (TVM, MFM, Weather SCADA, Fetcher Dumps)
into a canonical 15-minute 96-block schema:
  - Timestamps represent block-ending time.
  - Block 1 = 00:15, Block 2 = 00:30, ..., Block 96 = next-day 00:00 (24:00).
  - Power normalized to both `active_power_kw` and `metered_mw`.
  - Negative and invalid power values converted/clipped to 0.0.
  - Standardized weather/sensor channels: poa_wm2, ghi_wm2, amb_temp, mod_temp,
    wind_speed_ms, wind_direction_deg, humidity.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CANONICAL_COLUMNS = [
    "timestamp",
    "date",
    "block",
    "active_power_kw",
    "metered_mw",
    "poa_wm2",
    "ghi_wm2",
    "amb_temp",
    "mod_temp",
    "wind_speed_ms",
    "wind_direction_deg",
    "humidity",
    "source_file",
]

# Priority candidates for dynamic column detection
TIMESTAMP_CANDIDATES = [
    "block_end",
    "block_start",
    "timestamp",
    "TimeStamp",
    "Timestamp",
    "datetime",
    "Datetime",
    "DateTime",
    "TIME",
    "Time",
    "date_time",
]

KW_POWER_CANDIDATES = [
    "active_power_kw",
    "Active Power (kW)",
    "Active Power-Avg MFM-OUT (KW)",
    "Active Power-Avg MFM-OUT (kW)",
    "TVM Active Power",
    "Solar_Meter_Active_Power(KW)",
    "GSPPL - Meter data (live) (kW)",
    "Metered Power (kW)",
    "metered_kw",
    "power_kw",
    "KW",
]

MW_POWER_CANDIDATES = [
    "metered_mw",
    "Active Power (MW)",
    "power_mw",
    "MW",
    "generation_mw",
    "Total Active Power",
    "Active Power",
    "Active_Power",
]

BLOCK_CANDIDATES = [
    "block_no",
    "block",
    "time_block",
    "block_num",
    "timeblock",
    "block_index",
    "blockno",
]

SENSOR_ALIASES = {
    "poa_wm2": ["poa_wm2", "POA (W/m2)", "POA (W/m²)", "POA", "Irradiance (W/m2)", "GTI (W/m2)"],
    "ghi_wm2": ["ghi_wm2", "GHI_W (W/m2)", "GHI (W/m2)", "GHI (W/m²)", "GHI", "Radiation"],
    "amb_temp": ["amb_temp", "Ambient Temperature (C)", "AMB TEMP", "Ambient Temperature", "amb_temp_c"],
    "mod_temp": ["mod_temp", "Module Temperature (C)", "MOD TEMP", "Module Temperature", "mod_temp_c"],
    "wind_speed_ms": ["wind_speed_ms", "Wind Speed (m/s)", "Wind Speed", "wind_speed"],
    "wind_direction_deg": ["wind_direction_deg", "Wind Direction (DEG.)", "Wind Direction", "wind_dir"],
    "humidity": ["humidity", "Humidity", "Relative Humidity", "relative_humidity"],
}


def _pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Find matching column from candidates, prioritizing the one with the most non-null values."""
    best_candidate: str | None = None
    best_count = -1

    for cand in candidates:
        cand_lower = cand.strip().lower()
        matches = [c for c in df.columns if str(c).strip().lower() == cand_lower]
        if matches:
            # Pick matching column with most non-null values
            for m in matches:
                valid_cnt = int(df[m].notna().sum())
                if valid_cnt > best_count:
                    best_count = valid_cnt
                    best_candidate = m
            if best_candidate is not None and best_count > 0:
                return best_candidate

    return best_candidate


def meter_timestamp_to_date_and_block(
    timestamps: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """
    Return operating dates and 1-based blocks (1..96) for meter end timestamps.

    Rule: Subtract 1 nanosecond so that midnight 00:00 (end of day) resolves to
    23:59:59.999999999 of the preceding operating day in block 96.
    """
    parsed = pd.to_datetime(timestamps, errors="coerce")
    effective_timestamp = parsed - pd.Timedelta(nanoseconds=1)
    operating_date = effective_timestamp.dt.date.astype(str)
    minutes = effective_timestamp.dt.hour * 60 + effective_timestamp.dt.minute
    block = (minutes // 15 + 1).astype("Int64")
    return operating_date, block


def _parse_timestamps(series: pd.Series) -> pd.Series:
    """Robust timestamp parser supporting ISO, DD-MM-YYYY, and YYYY-MM-DD formats."""
    values = series.astype(str).str.strip()
    valid = values[~values.str.lower().isin(["nan", "none", "null", "nat", ""])]
    sample = str(valid.iloc[0]) if not valid.empty else ""
    is_iso = bool(sample[:4].isdigit() and (sample[4:5] in ("-", "/")))
    return pd.to_datetime(values, errors="coerce", dayfirst=not is_iso)


def normalize_meter_dataframe(
    df: pd.DataFrame,
    meter_config: dict[str, Any] | None = None,
    source_file: str = "",
) -> pd.DataFrame:
    """
    Universal normalizer accepting any site raw DataFrame and producing canonical schema.

    Parameters:
        df: Raw pandas DataFrame loaded from SCADA/meter CSV.
        meter_config: Optional site profile configuration with explicit column mappings:
                      - timestamp_column
                      - power_column
                      - power_unit ('kw' or 'mw')
                      - block_column
                      - poa_column, ghi_column, module_temperature_column, ambient_temperature_column
        source_file: Optional source filename for audit tracking.

    Returns:
        pd.DataFrame strictly conforming to CANONICAL_COLUMNS.
    """
    if df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    # De-duplicate columns
    df = df.loc[:, ~df.columns.duplicated()].copy()
    cfg = meter_config or {}

    # 1. Resolve Timestamp column
    ts_col = cfg.get("timestamp_column")
    if not ts_col or ts_col not in df.columns:
        ts_col = _pick_column(df, TIMESTAMP_CANDIDATES)

    # 2. Resolve Block column (if present directly)
    blk_col = cfg.get("block_column")
    if not blk_col or blk_col not in df.columns:
        blk_col = _pick_column(df, BLOCK_CANDIDATES)

    # If no timestamp column found, but block column and date exist, construct timestamp
    if ts_col is None and blk_col is not None:
        date_col = _pick_column(df, ["date", "Date", "DATE"])
        if date_col is not None:
            def _b_to_time(row: pd.Series) -> str:
                try:
                    b_num = int(row[blk_col])
                    d_str = str(row[date_col]).split(" ")[0]
                    total_min = b_num * 15
                    h = total_min // 60
                    m = total_min % 60
                    if h == 24 and m == 0:
                        next_d = pd.to_datetime(d_str) + pd.Timedelta(days=1)
                        return f"{next_d.strftime('%Y-%m-%d')} 00:00:00"
                    return f"{d_str} {h:02d}:{m:02d}:00"
                except Exception:
                    return ""
            df["__constructed_ts"] = df.apply(_b_to_time, axis=1)
            ts_col = "__constructed_ts"

    if ts_col is None:
        raise ValueError(f"Cannot find valid timestamp or block column in meter data ({source_file})")

    # 3. Resolve Power column and Unit
    pwr_col = cfg.get("power_column")
    pwr_unit = str(cfg.get("power_unit", "")).lower()

    if not pwr_col or pwr_col not in df.columns:
        cand_kw = _pick_column(df, KW_POWER_CANDIDATES)
        cand_mw = _pick_column(df, MW_POWER_CANDIDATES)

        if cand_mw is not None:
            pwr_col = cand_mw
            pwr_unit = "mw"
        elif cand_kw is not None:
            pwr_col = cand_kw
            pwr_unit = "kw"
        else:
            for c in df.columns:
                c_low = str(c).lower().strip()
                if "metered" in c_low and ("mw" in c_low or "kw" in c_low):
                    pwr_col = c
                    pwr_unit = "kw" if "kw" in c_low else "mw"
                    break
                elif "active" in c_low and "power" in c_low:
                    pwr_col = c
                    pwr_unit = "kw" if "kw" in c_low else "mw"
                    break
                elif c_low in ("mw", "kw", "power"):
                    pwr_col = c
                    pwr_unit = c_low
                    break

    if pwr_col is None or pwr_col not in df.columns:
        raise ValueError(f"Cannot find valid active power column in meter data ({source_file})")

    raw_vals = pd.to_numeric(df[pwr_col], errors="coerce").fillna(0.0)
    if not pwr_unit:
        max_val = np.nanmax(np.abs(raw_vals.values)) if not raw_vals.empty else 0.0
        pwr_unit = "kw" if max_val > 50.0 or "kw" in str(pwr_col).lower() else "mw"

    # 4. Construct Output DataFrame
    out = pd.DataFrame()
    out["timestamp"] = _parse_timestamps(df[ts_col])

    # Drop rows with invalid or null timestamps immediately
    valid_mask = out["timestamp"].notna()
    out = out[valid_mask].copy()
    df = df[valid_mask].copy()

    op_dates, op_blocks = meter_timestamp_to_date_and_block(out["timestamp"])
    out["date"] = op_dates
    
    if blk_col is not None and blk_col in df.columns:
        raw_b = pd.to_numeric(df[blk_col], errors="coerce").fillna(op_blocks)
        min_b = np.nanmin(raw_b.values) if not raw_b.dropna().empty else 1
        if min_b == 0:
            raw_b = raw_b + 1
        out["block"] = raw_b.fillna(op_blocks).fillna(1).clip(1, 96).astype(int)
    else:
        out["block"] = op_blocks.fillna(1).clip(1, 96).astype(int)

    clean_power = raw_vals.clip(lower=0.0)
    if pwr_unit == "kw":
        out["active_power_kw"] = clean_power.round(3)
        out["metered_mw"] = (clean_power / 1000.0).round(4)
    else:
        out["metered_mw"] = clean_power.round(4)
        out["active_power_kw"] = (clean_power * 1000.0).round(3)

    # 5. Sensor / Weather Columns
    sensor_cfg = {
        "poa_wm2": cfg.get("poa_column"),
        "ghi_wm2": cfg.get("ghi_column"),
        "amb_temp": cfg.get("ambient_temperature_column"),
        "mod_temp": cfg.get("module_temperature_column"),
        "wind_speed_ms": cfg.get("wind_speed_column"),
        "wind_direction_deg": cfg.get("wind_direction_column"),
        "humidity": cfg.get("humidity_column"),
    }

    for target, explicit_name in sensor_cfg.items():
        matched_col = None
        if explicit_name and explicit_name in df.columns:
            matched_col = explicit_name
        else:
            matched_col = _pick_column(df, SENSOR_ALIASES.get(target, []))

        if matched_col and matched_col in df.columns:
            vals = pd.to_numeric(df[matched_col], errors="coerce")
            if target in ("poa_wm2", "ghi_wm2", "wind_speed_ms", "humidity"):
                vals = vals.clip(lower=0.0)
            out[target] = vals.round(2)
        else:
            out[target] = np.nan

    out["source_file"] = source_file or Path(str(ts_col)).name

    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    out = out.drop_duplicates(subset=["date", "block"], keep="last").reset_index(drop=True)

    return out[CANONICAL_COLUMNS]


def load_and_normalize_meter_csv(
    csv_path: str | Path,
    meter_config: dict[str, Any] | None = None,
    delimiter: str | None = None,
) -> pd.DataFrame:
    """Load raw SCADA meter CSV and return normalized DataFrame."""
    p = Path(csv_path)
    if not p.exists():
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    sep = delimiter or (meter_config.get("delimiter") if meter_config else None) or ","
    try:
        df = pd.read_csv(p, sep=sep, encoding="utf-8")
    except Exception:
        df = pd.read_csv(p, sep=None, engine="python", encoding="utf-8", errors="ignore")

    return normalize_meter_dataframe(df, meter_config=meter_config, source_file=p.name)
