from __future__ import annotations

from context_generator._shared import bootstrap_environment


bootstrap_environment("KOTHAGUDEM")

from modules.feedback import schedule_feedback  # noqa: E402


def main() -> None:
    analyzed_dates = schedule_feedback.process_daily_schedule_feedback()
    if not analyzed_dates:
        print("No new KOTHAGUDEM full-day evaluation schedules found (or missing matching meter actuals).")
        return
    print(f"Analyzed: {', '.join(analyzed_dates)}")
    print(schedule_feedback.format_schedule_feedback_for_prompt())


if __name__ == "__main__":
    main()
