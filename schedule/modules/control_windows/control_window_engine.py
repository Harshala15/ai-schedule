"""
Plant Control Window Engine.
Reads active plant control windows from DynamoDB and applies them block-wise
to 96-block schedules for Intraday and Day-Ahead forecasting pipelines.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Indian Standard Time (UTC+05:30)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _to_float(val: Any, default: float = 0.0) -> float:
    """Safely cast numeric, Decimal, or string to float."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, Decimal):
        return float(val)
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


def parse_iso_ist(date_str: str) -> Optional[dt.datetime]:
    """Parse ISO datetime string and normalize to IST."""
    if not date_str or not str(date_str).strip():
        return None
    raw = str(date_str).strip()
    try:
        # Handle trailing Z or offsets
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Assume IST if naive
            parsed = parsed.replace(tzinfo=IST)
        else:
            parsed = parsed.astimezone(IST)
        return parsed
    except Exception as exc:
        logger.warning("Could not parse datetime string '%s': %s", date_str, exc)
        return None


class PlantControlWindowEngine:
    """
    Evaluates and applies active plant control windows (Curtailment, Shutdown, Partial DC Derate)
    to final 96-block schedules in the guardrails layer.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
    ):
        self.table_name = table_name or os.getenv("CONTROL_WINDOWS_TABLE", "plant_control_windows_test")
        self.region_name = region_name or os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
        self.plant_id = str(os.getenv("PLANT_ID", "vedanjay") or "vedanjay").strip()
        self._dynamodb_resource = None
        self._dynamodb_client = None

    @property
    def dynamodb_resource(self):
        if self._dynamodb_resource is None:
            self._dynamodb_resource = boto3.resource("dynamodb", region_name=self.region_name)
        return self._dynamodb_resource

    @property
    def dynamodb_client(self):
        if self._dynamodb_client is None:
            self._dynamodb_client = boto3.client("dynamodb", region_name=self.region_name)
        return self._dynamodb_client

    def describe_key_schema(self) -> Tuple[str, Optional[str]]:
        """Identify partition key and sort key names for the control windows table."""
        try:
            resp = self.dynamodb_client.describe_table(TableName=self.table_name)
            key_schema = resp.get("Table", {}).get("KeySchema", [])
            partition_key = "site_id"
            sort_key = None
            for key_def in key_schema:
                if key_def.get("KeyType") == "HASH":
                    partition_key = key_def.get("AttributeName", "site_id")
                elif key_def.get("KeyType") == "RANGE":
                    sort_key = key_def.get("AttributeName")
            return partition_key, sort_key
        except Exception as exc:
            logger.warning("Failed to describe DynamoDB table '%s': %s; falling back to Schema A", self.table_name, exc)
            return "site_id", "window_id"

    def load_active_windows(self, site_id: str, target_date_str: str) -> List[Dict[str, Any]]:
        """
        Load active control windows from DynamoDB for a specific site_id and target_date.
        Supports Schema A (site_id + window_id) and Schema B (plant_id + window_id).
        Excludes expired, past, or inactive records.
        """
        if not self.table_name:
            return []

        clean_site = str(site_id).strip().upper()
        # Parse target schedule day boundaries in IST
        try:
            d_target = dt.date.fromisoformat(target_date_str)
        except Exception:
            d_target = dt.datetime.now(IST).date()

        day_start = dt.datetime.combine(d_target, dt.time.min).replace(tzinfo=IST)
        day_end = dt.datetime.combine(d_target + dt.timedelta(days=1), dt.time.min).replace(tzinfo=IST)

        raw_items: List[Dict[str, Any]] = []
        try:
            partition_key, _ = self.describe_key_schema()
            table = self.dynamodb_resource.Table(self.table_name)

            if partition_key == "site_id":
                # Schema A: Query site_id = clean_site, and also site_id = 'ALL'
                for query_val in (clean_site, "ALL"):
                    try:
                        resp = table.query(
                            KeyConditionExpression=Key("site_id").eq(query_val)
                        )
                        raw_items.extend(resp.get("Items", []))
                    except Exception as q_err:
                        logger.warning("DynamoDB query failed for site_id='%s': %s", query_val, q_err)
            else:
                # Schema B: Partitioned by plant_id (normal scheduler style) or another grouping key.
                # Query the plant/group partition first, then filter by site/site_id below.
                # This avoids missing records stored as plant_id=vedanjay, site=KOTHAGUDEM.
                query_values: list[str] = []
                if partition_key == "plant_id":
                    query_values.extend([self.plant_id, self.plant_id.upper(), self.plant_id.lower()])
                query_values.extend([clean_site, "ALL"])

                seen_query_values: set[str] = set()
                for query_val in query_values:
                    query_val = str(query_val or "").strip()
                    if not query_val or query_val in seen_query_values:
                        continue
                    seen_query_values.add(query_val)
                    try:
                        resp = table.query(
                            KeyConditionExpression=Key(partition_key).eq(query_val)
                        )
                        raw_items.extend(resp.get("Items", []))
                    except Exception as q_err:
                        logger.warning("DynamoDB query failed for %s='%s': %s", partition_key, query_val, q_err)

                if not raw_items:
                    # Last-resort compatibility fallback for unexpected table schemas.
                    resp = table.scan()
                    raw_items.extend(resp.get("Items", []))

        except Exception as exc:
            logger.warning(
                "[CONTROL_WINDOWS] Could not load control windows from '%s' (%s). Continuing with standard forecast.",
                self.table_name,
                exc,
            )
            return []

        # Filter and validate items
        validated_windows: List[Dict[str, Any]] = []
        for item in raw_items:
            # 1. Site matching check
            item_site = str(item.get("site") or item.get("site_id") or "").strip().upper()
            if item_site not in (clean_site, "ALL"):
                continue

            # 2. Active status check
            active_val = item.get("active")
            is_active = (active_val is True) or (str(active_val).strip().lower() in ("true", "1", "yes"))
            if not is_active:
                continue

            # 3. Plant status check: must be CURTAILMENT or SHUTDOWN
            status = str(item.get("plant_status", "")).strip().upper()
            if status not in ("CURTAILMENT", "SHUTDOWN"):
                continue

            # 4. Parse start_time and end_time
            start_dt = parse_iso_ist(item.get("start_time"))
            end_dt = parse_iso_ist(item.get("end_time"))
            if start_dt is None or end_dt is None or end_dt <= start_dt:
                continue

            # 5. Date overlap check with target schedule day
            # If window ends before day_start (yesterday or older), or starts after day_end, discard!
            if end_dt <= day_start or start_dt >= day_end:
                continue

            # Extract reduction / capacity parameters
            raw_payload = item.get("raw_payload") if isinstance(item.get("raw_payload"), dict) else {}
            control_mode = str(item.get("control_mode") or raw_payload.get("control_mode") or "").strip().upper()

            shutdown_reduction_mw = _to_float(
                item.get("shutdown_reduction_mw") or raw_payload.get("shutdown_reduction_mw") or raw_payload.get("mw"),
                default=0.0,
            )

            curtailment_capacity = item.get("curtailment_capacity") or item.get("curtailment_capacity_mw")
            if curtailment_capacity is None and "curtailment_capacity" in raw_payload:
                curtailment_capacity = raw_payload.get("curtailment_capacity")
            curtailment_capacity_mw = _to_float(curtailment_capacity, default=None) if curtailment_capacity is not None else None

            asset_scope = str(item.get("asset_scope") or raw_payload.get("asset_scope") or "").strip().lower()
            asset_id = str(item.get("asset_id") or item.get("sub_plant_id") or raw_payload.get("asset_id") or "COMBINED").strip().upper()
            asset_name = str(item.get("asset_name") or raw_payload.get("asset_name") or "").strip()
            mg_plant_id = str(item.get("multi_generator_plant_id") or raw_payload.get("multi_generator_plant_id") or "").strip()

            validated_windows.append({
                "window_id": str(item.get("window_id", "")),
                "site_id": item_site,
                "asset_scope": asset_scope,
                "asset_id": asset_id,
                "asset_name": asset_name,
                "multi_generator_plant_id": mg_plant_id,
                "plant_status": status,
                "control_mode": control_mode,
                "shutdown_reduction_mw": shutdown_reduction_mw,
                "curtailment_capacity_mw": curtailment_capacity_mw,
                "start_time_dt": start_dt,
                "end_time_dt": end_dt,
                "start_time_iso": start_dt.isoformat(),
                "end_time_iso": end_dt.isoformat(),
                "last_message": str(item.get("last_message", "")),
            })

        logger.info(
            "[CONTROL_WINDOWS] Loaded %d active window(s) for site '%s' on target date '%s'",
            len(validated_windows),
            clean_site,
            target_date_str,
        )
        return validated_windows

    @staticmethod
    def calculate_window_capacity(
        window: Dict[str, Any],
        site_ac_capacity_mw: float,
        site_dc_capacity_mw: float,
    ) -> Tuple[float, str]:
        """
        Calculate effective AC capacity and control_type for a single window.
        Returns:
            (effective_capacity_ac_mw, control_type)
        """
        ac_cap = max(0.001, float(site_ac_capacity_mw))
        dc_cap = max(0.001, float(site_dc_capacity_mw))
        dc_ac_ratio = dc_cap / ac_cap

        status = window.get("plant_status", "NORMAL").upper()
        mode = window.get("control_mode", "").upper()

        if status == "SHUTDOWN":
            if mode == "DC":
                # Case 2: Partial DC shutdown
                reduction = float(window.get("shutdown_reduction_mw") or 0.0)
                remaining_dc = max(dc_cap - reduction, 0.0)
                effective_ac = remaining_dc / dc_ac_ratio
                effective_ac = min(effective_ac, ac_cap)
                return round(effective_ac, 3), "PARTIAL_SHUTDOWN"
            else:
                # Case 1: Full shutdown
                return 0.0, "SHUTDOWN"

        elif status == "CURTAILMENT":
            # Case 3: Curtailment
            curtail_cap = window.get("curtailment_capacity_mw")
            if curtail_cap is None:
                curtail_cap = ac_cap
            effective_ac = min(float(curtail_cap), ac_cap)
            effective_ac = max(0.0, effective_ac)
            return round(effective_ac, 3), "CURTAILMENT"

        # Case 4: Normal
        return ac_cap, "NORMAL"

    def match_block_control(
        self,
        block_idx: int,
        target_date_str: str,
        windows: List[Dict[str, Any]],
        site_ac_capacity_mw: float,
        site_dc_capacity_mw: float,
    ) -> Dict[str, Any]:
        """
        Match 15-minute block against active windows with multi-window arbitration.
        Block index is 1-indexed (1 to 96).
        """
        ac_cap = max(0.001, float(site_ac_capacity_mw))
        try:
            d_target = dt.date.fromisoformat(target_date_str)
        except Exception:
            d_target = dt.datetime.now(IST).date()

        # Compute block temporal boundaries in IST
        block_start = dt.datetime.combine(d_target, dt.time.min).replace(tzinfo=IST) + dt.timedelta(minutes=(block_idx - 1) * 15)
        block_end = block_start + dt.timedelta(minutes=15)

        # Find overlapping windows
        overlapping: List[Dict[str, Any]] = []
        for w in windows:
            if w["start_time_dt"] < block_end and w["end_time_dt"] > block_start:
                overlapping.append(w)

        if not overlapping:
            return {
                "block": block_idx,
                "control_applied": False,
                "block_control_status": "NORMAL",
                "block_control_mode": "NONE",
                "block_control_type": "NORMAL",
                "effective_control_capacity_ac_mw": ac_cap,
                "selected_window_id": None,
            }

        # Multi-Window Priority Arbitration:
        # Priority 1: Full SHUTDOWN wins immediately (0.0 MW)
        # Priority 2: Lowest effective AC capacity wins among curtailments / partial shutdowns
        selected_effective_cap = None
        selected_type = "NORMAL"
        selected_mode = "NONE"
        selected_status = "NORMAL"
        selected_window_id = None

        for w in overlapping:
            cap, c_type = self.calculate_window_capacity(w, ac_cap, site_dc_capacity_mw)

            if c_type == "SHUTDOWN":
                return {
                    "block": block_idx,
                    "control_applied": True,
                    "block_control_status": "SHUTDOWN",
                    "block_control_mode": w.get("control_mode") or "FULL",
                    "block_control_type": "SHUTDOWN",
                    "effective_control_capacity_ac_mw": 0.0,
                    "selected_window_id": w.get("window_id"),
                }

            if selected_effective_cap is None or cap < selected_effective_cap:
                selected_effective_cap = cap
                selected_type = c_type
                selected_mode = w.get("control_mode") or ("AC" if c_type == "CURTAILMENT" else "DC")
                selected_status = w.get("plant_status", "NORMAL")
                selected_window_id = w.get("window_id")

        return {
            "block": block_idx,
            "control_applied": True,
            "block_control_status": selected_status,
            "block_control_mode": selected_mode,
            "block_control_type": selected_type,
            "effective_control_capacity_ac_mw": selected_effective_cap if selected_effective_cap is not None else ac_cap,
            "selected_window_id": selected_window_id,
        }

    @staticmethod
    def scale_forecast_mw(
        raw_mw: float,
        effective_capacity_ac: float,
        site_ac_capacity_mw: float,
    ) -> float:
        """
        Apply proportional scaling formula:
            scale = effective_capacity_ac / site_ac_capacity_mw
            final = min(raw_forecast * scale, effective_capacity_ac)
        """
        raw_val = max(0.0, float(raw_mw))
        ac_cap = max(0.001, float(site_ac_capacity_mw))
        eff_cap = max(0.0, float(effective_capacity_ac))

        if eff_cap <= 0.0:
            return 0.0

        scale = eff_cap / ac_cap
        controlled = raw_val * scale
        final_mw = min(controlled, eff_cap)
        return round(final_mw, 2)

    def apply_to_blocks(
        self,
        raw_forecast_mw_96: List[float] | np.ndarray,
        site_id: str,
        target_date_str: str,
        site_ac_capacity_mw: float,
        site_dc_capacity_mw: float,
        freeze_end_block: int = 0,
        frozen_mw_96: Optional[List[float] | np.ndarray] = None,
    ) -> Tuple[np.ndarray, List[Dict[str, Any]], Dict[str, Any]]:
        """
        Apply control windows across all 96 blocks respecting the freeze horizon.

        Args:
            raw_forecast_mw_96: Array of 96 raw forecast MW values.
            site_id: Plant identifier (e.g. KOTHAGUDEM).
            target_date_str: Target date string (YYYY-MM-DD).
            site_ac_capacity_mw: Rated AC capacity in MW.
            site_dc_capacity_mw: Rated DC capacity in MW.
            freeze_end_block: 1-indexed block up to which blocks are frozen.
            frozen_mw_96: Optional committed schedule array to preserve for frozen blocks.

        Returns:
            (final_schedule_mw_96, per_block_audit, summary_meta)
        """
        raw_arr = np.array(raw_forecast_mw_96, dtype=float)
        if len(raw_arr) != 96:
            raise ValueError(f"Expected 96 forecast blocks, got {len(raw_arr)}")

        windows = self.load_active_windows(site_id, target_date_str)
        ac_cap = max(0.001, float(site_ac_capacity_mw))
        dc_cap = max(0.001, float(site_dc_capacity_mw))
        dc_ac_ratio = dc_cap / ac_cap

        final_arr = np.copy(raw_arr)
        per_block_audit: List[Dict[str, Any]] = []
        applied_window_ids = set()

        for b in range(1, 97):
            idx = b - 1
            raw_mw = float(raw_arr[idx])

            # Check freeze horizon (Intraday lag protection)
            if b <= freeze_end_block:
                if frozen_mw_96 is not None and len(frozen_mw_96) >= b:
                    preserved_mw = float(frozen_mw_96[idx])
                else:
                    preserved_mw = raw_mw

                final_arr[idx] = preserved_mw
                per_block_audit.append({
                    "Block": b,
                    "raw_forecast_mw": round(raw_mw, 2),
                    "final_schedule_mw": round(preserved_mw, 2),
                    "block_control_status": "FROZEN",
                    "block_control_mode": "NONE",
                    "block_control_type": "FROZEN",
                    "effective_control_capacity_ac_mw": ac_cap,
                    "control_applied": False,
                    "frozen": True,
                })
                continue

            # Actionable block: match overlapping control window
            match = self.match_block_control(b, target_date_str, windows, ac_cap, dc_cap)

            if not match["control_applied"]:
                final_arr[idx] = round(raw_mw, 2)
                per_block_audit.append({
                    "Block": b,
                    "raw_forecast_mw": round(raw_mw, 2),
                    "final_schedule_mw": round(raw_mw, 2),
                    "block_control_status": "NORMAL",
                    "block_control_mode": "NONE",
                    "block_control_type": "NORMAL",
                    "effective_control_capacity_ac_mw": ac_cap,
                    "control_applied": False,
                    "frozen": False,
                })
            else:
                eff_cap = match["effective_control_capacity_ac_mw"]
                final_val = self.scale_forecast_mw(raw_mw, eff_cap, ac_cap)
                final_arr[idx] = final_val
                if match.get("selected_window_id"):
                    applied_window_ids.add(match["selected_window_id"])

                per_block_audit.append({
                    "Block": b,
                    "raw_forecast_mw": round(raw_mw, 2),
                    "final_schedule_mw": final_val,
                    "block_control_status": match["block_control_status"],
                    "block_control_mode": match["block_control_mode"],
                    "block_control_type": match["block_control_type"],
                    "effective_control_capacity_ac_mw": round(eff_cap, 3),
                    "control_applied": True,
                    "frozen": False,
                    "window_id": match["selected_window_id"],
                })

        # Summary audit block for .meta.json
        summary_meta = {
            "table_name": self.table_name,
            "target_date": target_date_str,
            "site_id": site_id,
            "site_ac_capacity_mw": round(ac_cap, 2),
            "site_dc_capacity_mw": round(dc_cap, 2),
            "dc_ac_ratio": round(dc_ac_ratio, 3),
            "windows_loaded": len(windows),
            "windows_applied": len(applied_window_ids),
            "freeze_end_block": freeze_end_block,
            "applied_windows": [w for w in windows if w.get("window_id") in applied_window_ids],
        }

        return final_arr, per_block_audit, summary_meta
