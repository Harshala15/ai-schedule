# Context Generator

This folder contains plant-specific wrappers for the shared feedback logic in `daily_feedback.py`.

Use these scripts to build rolling day-level context JSON from:
- a forecast schedule CSV
- the matching actual meter CSV

Supported plants:
- `SIRMOUR`
- `KASIPET`
- `BHUPALPALLY`

For Lambda, point each function at the matching handler:
- `context_generator.simour.lambda_handler.lambda_handler`
- `context_generator.kasipet.lambda_handler.lambda_handler`
- `context_generator.bhupalpally.lambda_handler.lambda_handler`

If you keep a single generic function, pass `{"plant_name":"KASIPET"}` or the equivalent plant name in the event payload.

The scripts:
- read `Schedule MW` from the schedule CSV when available, otherwise fall back to the earlier forecast columns
- read actual power from `Active Power (kW)` or the plant's equivalent meter column
- convert actual kW to MW
- skip rows where either side is missing
- write/update `prediction_context/<PLANT>_context.json`

Examples:

```powershell
python context_generator\simour\schedule_feedback.py "C:\path\current_final_schedule.csv" "C:\path\2026_08_18_SOLAR_INV.csv"
python context_generator\kasipet\schedule_feedback.py "C:\path\current_final_schedule.csv" "C:\path\kasipet_20260818.csv"
python context_generator\bhupalpally\schedule_feedback.py "C:\path\current_final_schedule.csv" "C:\path\bhupalpally_20260818.csv"
```

For end-of-day actual-meter-only feedback:

```powershell
python context_generator\simour\daily_actuals.py
python context_generator\kasipet\daily_actuals.py
python context_generator\bhupalpally\daily_actuals.py
```

