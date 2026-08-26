"""Helpers for building compact plant-performance memory summaries."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import config
from modules.feedback import daily_feedback
from modules import schedule_utils as shared_schedule_utils


def _plant_performance_cache_path(target_date: str) -> Path:
    return config.PLANT_PERFORMANCE_DIR / config.PLANT_NAME / target_date / "summary.json"


def _load_cached_plant_performance_payload(target_date: str) -> dict | None:
    cache_path = _plant_performance_cache_path(target_date)
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "plant_performance_memory":
        return None
    if not payload.get("summary_text"):
        return None
    return payload


def _clear_directory_except(path: Path, keep_names: set[str]) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.name in keep_names:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def build_recent_plant_performance_text(
    storage_module,
    bucket: str,
    meter_prefix: str,
    target_date: str,
    work_root: Path,
    *,
    days: int = 8,
) -> str:
    """Download recent meter files and summarize them as plant behavior memory."""
    cache_path = _plant_performance_cache_path(target_date)
    cached_payload = _load_cached_plant_performance_payload(target_date)
    if cached_payload:
        return str(cached_payload["summary_text"])

    performance_dir = cache_path.parent
    performance_dir.mkdir(parents=True, exist_ok=True)

    # Keep the cache folder clean: only summary.json should live here.
    _clear_directory_except(performance_dir, {"summary.json"})

    temp_download_dir = work_root / "_plant_performance_tmp" / config.PLANT_NAME / target_date
    if temp_download_dir.exists():
        shutil.rmtree(temp_download_dir)
    temp_download_dir.mkdir(parents=True, exist_ok=True)

    try:
        recent_paths = shared_schedule_utils.download_recent_meter_history_files(
            storage_module,
            bucket,
            meter_prefix,
            target_date,
            temp_download_dir,
            days=days,
        )
        if not recent_paths:
            return f"No prior {days}-day plant performance history is available yet."

        combined_rows: list[dict] = []
        lines = [f"Recent {days}-day plant performance memory (oldest first):"]
        for path in recent_paths:
            day_label = path.name[:10] if len(path.name) >= 10 else path.stem
            try:
                rows = daily_feedback._load_intraday_meter_rows(path, dt.datetime.max)  # type: ignore[attr-defined]
            except Exception:
                rows = []
            combined_rows.extend(rows)
            lines.append(f"- {daily_feedback.summarize_meter_csv_for_prompt(path, label=day_label)}")

        if combined_rows:
            lines.insert(
                1,
                f"- Overall {days}-day aggregate: "
                f"{daily_feedback.summarize_meter_rows_for_prompt(combined_rows, label='aggregate')}",
            )

        summary_text = "\n".join(lines)
        summary_payload = {
            "type": "plant_performance_memory",
            "plant_name": config.PLANT_NAME,
            "target_date": target_date,
            "days": days,
            "recent_files": [path.name for path in recent_paths],
            "summary_lines": lines,
            "summary_text": summary_text,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        cache_path.write_text(json.dumps(summary_payload, indent=2, default=str), encoding="utf-8")
        _clear_directory_except(performance_dir, {"summary.json"})
        return summary_text
    finally:
        if temp_download_dir.exists():
            shutil.rmtree(temp_download_dir)
