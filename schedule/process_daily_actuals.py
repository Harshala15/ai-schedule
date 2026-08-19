"""
process_daily_actuals.py

Standalone entry point -- run this manually anytime after dropping the
day's actual meter-export CSV into the local manual feedback folder.
Does NOT need test_multi_image.py or a browser/Windy capture to run.

Runs the same feedback pipeline that would otherwise only fire as part
of a full capture run (see daily_feedback.process_actuals_inbox()):
    1. Merges every new CSV from the local manual feedback folder into
       historic_cases/merged_scada_data.csv.
    2. Syncs those actuals into features_log.csv (the case store).
    3. For every day touched, computes error metrics (MAE/RMSE/MAPE/Bias),
       time-of-day bias, and the worst-forecast block -- and folds that
       into the rolling prediction-context file (prediction_context/,
       capped at config.CONTEXT_WINDOW_DAYS days) that gets fed into the
       LLM prompt on the next prediction run.
    4. Appends a row to accuracy_reports/<PLANT>_daily_accuracy.csv (the
       full, non-rolling accuracy history).
    5. Archives each processed file into the local processed folder.

Usage:
    python process_daily_actuals.py
"""

import daily_feedback


def main():
    analyzed_dates = daily_feedback.process_actuals_inbox()

    if not analyzed_dates:
        print("No new files found in the local manual feedback folder (or no matching predictions yet for any date found).")
        return

    print(f"\nDone -- analyzed: {', '.join(analyzed_dates)}")
    print("\nCurrent rolling context (this is what gets fed to the LLM on the next prediction run):")
    print(daily_feedback.format_context_for_prompt())


if __name__ == "__main__":
    main()
