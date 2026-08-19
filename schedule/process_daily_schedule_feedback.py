"""
process_daily_schedule_feedback.py

Standalone entry point for the schedule-vs-meter feedback path.

Use this at the end of the day when you already have:
  - the forecast schedule CSV for that day
  - the actual meter CSV for that same day

It compares:
  - LLM Schedule (MW)
  - Active Power (kW) converted to MW

Blocks with missing or unparseable values are skipped.
The resulting day summary is written into prediction_context/<PLANT>_context.json
and mirrored to S3 if state sync is enabled.

Example:
    python process_daily_schedule_feedback.py ^
      "C:\\path\\to\\current_final_schedule.csv" ^
      "C:\\path\\to\\2026_08_18_SOLAR_INV.csv"
"""

from __future__ import annotations

import argparse
from pathlib import Path

import daily_feedback


def main() -> None:
    parser = argparse.ArgumentParser(description="Build day-level context from a forecast schedule and actual meter CSV.")
    parser.add_argument("schedule_csv", help="Path to the forecast schedule CSV containing LLM Schedule (MW).")
    parser.add_argument("actual_meter_csv", help="Path to the actual meter CSV containing Active Power (kW).")
    args = parser.parse_args()

    entry = daily_feedback.process_schedule_feedback(
        Path(args.schedule_csv),
        Path(args.actual_meter_csv),
        source_label="manual schedule feedback",
    )

    if not entry:
        print("No context entry was created.")
        return

    print("\nCreated/updated context entry:")
    print(entry["summary"])
    print("\nCurrent rolling context:")
    print(daily_feedback.format_context_for_prompt())


if __name__ == "__main__":
    main()
