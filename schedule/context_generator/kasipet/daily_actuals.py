from __future__ import annotations

from context_generator._shared import bootstrap_environment


bootstrap_environment("KASIPET")

from modules.feedback import daily_feedback  # noqa: E402


def main() -> None:
    analyzed_dates = daily_feedback.process_actuals_inbox()
    if not analyzed_dates:
        print("No new KASIPET files found in the local manual feedback folder (or no matching predictions yet).")
        return
    print(f"Analyzed: {', '.join(analyzed_dates)}")
    print(daily_feedback.format_context_for_prompt())


if __name__ == "__main__":
    main()

