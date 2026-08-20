from __future__ import annotations

import argparse
from pathlib import Path

from context_generator._shared import bootstrap_environment


bootstrap_environment("BHUPALPALLY")

import daily_feedback  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BHUPALPALLY context from schedule CSV + actual meter CSV.")
    parser.add_argument("schedule_csv", help="Path to the forecast schedule CSV.")
    parser.add_argument("actual_meter_csv", help="Path to the actual meter CSV.")
    args = parser.parse_args()

    entry = daily_feedback.process_schedule_feedback(
        Path(args.schedule_csv),
        Path(args.actual_meter_csv),
        source_label="bhupalpally schedule feedback",
    )

    if not entry:
        print("No context entry was created.")
        return

    print(entry["summary"])
    print(daily_feedback.format_context_for_prompt())


if __name__ == "__main__":
    main()

