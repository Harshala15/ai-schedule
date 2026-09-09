"""
accuracy_tracker.py

Joins your predictions CSV against your ACTUAL meter/SCADA export (like
the 2026_07_15_SOLAR_INV.csv you showed) on timestamp, and computes
error metrics (MAE, RMSE, MAPE). This is the "Compare vs actual" +
"Retrain model" boxes from your diagram.

IMPORTANT: your actual meter CSV's column names/positions are specific
to your SCADA export -- update TIMESTAMP_COLUMN and GENERATION_COLUMN
below (or pass them in) to match your file before running this.

Usage:
    python accuracy_tracker.py path/to/predictions.csv path/to/actual_meter.csv
"""

import csv
import datetime
import sys
from pathlib import Path

import config

# ---- Update these to match your actual meter CSV's real column names ----
DEFAULT_TIMESTAMP_COLUMN = "Timestamp"
DEFAULT_GENERATION_COLUMN = "Active Power (MW)"

MAPE_RETRAIN_THRESHOLD = 15.0  # if MAPE goes above this, flag for retraining


def _prediction_time(row):
    if row.get("Time"):
        return row["Time"]
    interval = row.get("Time Interval (15 minute interval)", "")
    if " - " in interval:
        return interval.split(" - ", 1)[0]
    return ""


def _prediction_mw(row):
    return row.get("LLM Schedule (MW)", row.get("Predicted Generation (MW)", ""))


def _parse_predictions(csv_path):
    """Returns {datetime: predicted_mw} from our own energy_generation.csv."""
    out = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.datetime.strptime(_prediction_time(row), "%Y-%m-%d %H:%M")
                mw = float(_prediction_mw(row))
                out[dt] = mw
            except (ValueError, KeyError):
                continue  # skip "not visible" or malformed rows
    return out


def _parse_actual(csv_path, timestamp_column, generation_column, impute_missing_with_solar: bool = True):
    """
    Returns ({datetime: actual_mw}, {datetime: bool is_imputed}) from the plant's meter export.
    If a block has NA or missing power, it is dynamically imputed from solar radiation
    (on-site pyranometer POA/GHI or satellite solar radiation GTI).
    """
    out = {}
    imputed_flags = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if timestamp_column not in reader.fieldnames or generation_column not in reader.fieldnames:
            raise ValueError(
                f"Columns not found. Available columns: {reader.fieldnames}\n"
                f"Update DEFAULT_TIMESTAMP_COLUMN / DEFAULT_GENERATION_COLUMN "
                f"in accuracy_tracker.py to match your actual file."
            )
        for row in reader:
            raw_ts = (row.get(timestamp_column) or "").strip()
            dt = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"):
                try:
                    dt = datetime.datetime.strptime(raw_ts, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                continue

            raw_gen = (row.get(generation_column) or "").strip()
            mw = None
            if raw_gen and raw_gen.upper() not in ("NA", "NAN", "NULL", "-", "--"):
                try:
                    mw = float(raw_gen)
                except ValueError:
                    mw = None

            if mw is not None:
                out[dt] = max(0.0, mw)
                imputed_flags[dt] = False
            elif impute_missing_with_solar:
                # Active power is NA / missing: impute from solar radiation
                try:
                    from modules.weather import satellite_virtual_meter
                    poa_val = None
                    for c in ("POA (W/m2)", "POA", "Plane of Array (W/m2)"):
                        if c in row and row[c]:
                            try:
                                poa_val = float(row[c])
                                break
                            except ValueError:
                                pass
                    ghi_val = None
                    for c in ("GHI (W/m2)", "GHI_W (W/m2)", "GHI_W", "GHI"):
                        if c in row and row[c]:
                            try:
                                ghi_val = float(row[c])
                                break
                            except ValueError:
                                pass
                    amb_temp = None
                    for c in ("AMB TEMP", "Ambient Temperature (C)", "Ambient Temp"):
                        if c in row and row[c]:
                            try:
                                amb_temp = float(row[c])
                                break
                            except ValueError:
                                pass
                    mod_temp = None
                    for c in ("MOD TEMP", "Module Temperature (C)", "Module Temp"):
                        if c in row and row[c]:
                            try:
                                mod_temp = float(row[c])
                                break
                            except ValueError:
                                pass
                    imputed_mw, _ = satellite_virtual_meter.impute_missing_generation_from_radiation(
                        timestamp=dt,
                        ground_poa=poa_val,
                        ground_ghi=ghi_val,
                        ambient_temp=amb_temp,
                        module_temp=mod_temp,
                    )
                    out[dt] = imputed_mw
                    imputed_flags[dt] = True
                except Exception:
                    continue

    return out, imputed_flags


def compute_accuracy(predictions_csv, actual_csv,
                      timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
                      generation_column=DEFAULT_GENERATION_COLUMN,
                      impute_missing_with_solar: bool = True):
    """
    Returns a dict: {mae, rmse, mape, matched_points, report_path}
    Also writes a plain-text report to accuracy_reports/.
    """
    predicted = _parse_predictions(Path(predictions_csv))
    actual, imputed_flags = _parse_actual(Path(actual_csv), timestamp_column, generation_column, impute_missing_with_solar=impute_missing_with_solar)

    matched = []
    for dt, pred_mw in predicted.items():
        if dt in actual:
            matched.append((dt, pred_mw, actual[dt]))

    if not matched:
        print("[WARN] No matching timestamps found between predictions and actual data.")
        return None

    errors = [(pred - act) for _, pred, act in matched]
    abs_errors = [abs(e) for e in errors]
    sq_errors = [e ** 2 for e in errors]

    mae = sum(abs_errors) / len(abs_errors)
    rmse = (sum(sq_errors) / len(sq_errors)) ** 0.5

    # MAPE: skip near-zero actuals (e.g. night-time) to avoid divide-by-
    # tiny-number blowing up the percentage.
    pct_errors = [
        abs(pred - act) / act * 100.0
        for _, pred, act in matched if act > 0.05
    ]
    mape = sum(pct_errors) / len(pct_errors) if pct_errors else float("nan")

    imputed_count = sum(1 for dt, _, _ in matched if imputed_flags.get(dt))

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    report_path = config.ACCURACY_REPORTS_DIR / f"accuracy_report_{timestamp}.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Accuracy report -- {config.PLANT_NAME}\n")
        f.write(f"Matched points: {len(matched)}\n")
        if imputed_count > 0:
            f.write(f"Points imputed from solar radiation: {imputed_count}/{len(matched)}\n")
        f.write(f"MAE:  {mae:.4f} MW\n")
        f.write(f"RMSE: {rmse:.4f} MW\n")
        f.write(f"MAPE: {mape:.2f} %\n\n")
        f.write("Time                 Predicted(MW)  Actual(MW)  Error(MW)  Source\n")
        for dt, pred, act in sorted(matched):
            src_tag = "Imputed (Solar)" if imputed_flags.get(dt) else "Meter SCADA"
            f.write(f"{dt}  {pred:>12.3f}  {act:>10.3f}  {pred - act:>9.3f}  {src_tag}\n")

    info_str = f"Matched {len(matched)} points | MAE={mae:.3f} MW | RMSE={rmse:.3f} MW | MAPE={mape:.2f}%"
    if imputed_count > 0:
        info_str += f" | ({imputed_count} blocks imputed from solar radiation)"
    print(info_str)
    print(f"Full report saved to: {report_path.resolve()}")

    return {
        "mae": mae, "rmse": rmse, "mape": mape,
        "matched_points": len(matched), "report_path": report_path,
        "imputed_blocks": imputed_count,
    }


def should_retrain(metrics: dict, mape_threshold: float = MAPE_RETRAIN_THRESHOLD) -> bool:
    if metrics is None:
        return False
    mape = metrics.get("mape")
    if mape is None or mape != mape:  # NaN check
        return False
    return mape > mape_threshold


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python accuracy_tracker.py <predictions_csv> <actual_meter_csv> "
              "[timestamp_column] [generation_column]")
        sys.exit(1)

    pred_csv, act_csv = sys.argv[1], sys.argv[2]
    ts_col = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_TIMESTAMP_COLUMN
    gen_col = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_GENERATION_COLUMN

    result = compute_accuracy(pred_csv, act_csv, ts_col, gen_col)
    if result and should_retrain(result):
        print(f"\n[FLAG] MAPE ({result['mape']:.2f}%) exceeds threshold "
              f"({MAPE_RETRAIN_THRESHOLD}%) -- consider running train_model.py.")
