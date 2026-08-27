"""
run_pipeline.py

REPLACES analyze_with_gemini() from the old pipeline, and completes the
move to the new hybrid architecture:

    Screenshot capture ----> Image feature extraction -----\\
                                                              >-- Combine features -> Physics anchor
    Satellite video ----> Video processing (optical flow) --/                            |
                                                                                            v
                                                                          Similarity retrieval (top-K
                                                                          similar past cases from the
                                                                          case store / features_log.csv)
                                                                                            |
                                                                                            v
                                                                          LLM reasoning (adjusts anchor
                                                                          using retrieved evidence,
                                                                          explains why)
                                                                                            |
                                                                                            v
                                                                          Validator (range/deviation/
                                                                          smoothness safety checks)
                                                                                            |
                                                                                            v
                                                                          Store prediction (features_log.csv
                                                                          becomes tomorrow's case store)

Only ONE LLM call happens per run, covering all 12 forecast blocks at
once (not one call per block) -- this keeps cost/latency reasonable.

Called from test_multi_image.py's run_once(), right after screenshots +
video have been captured.
"""

import datetime
import json

import config
from modules.opencv import image_feature_extraction
from modules.opencv import video_motion_features
from modules.physics import physics_anchor
from modules.weather import time_features
from modules.features import feature_builder
from modules.retrieval import similarity_retrieval
from modules.llm import predictor as llm_predictor
import validator
from modules.storage import prediction_store
from modules.feedback import daily_feedback


def run_prediction_pipeline(image_map: dict, video_path, reference_time: datetime.datetime = None,
                            num_blocks: int = None, output_dir=None, intraday_actuals_text: str = "",
                            intraday_actuals_path=None, weather_text: str = "",
                            context_text: str = "", video_text: str = "",
                            meter_history_text: str = "", pvlib_text: str = "",
                            plant_performance_text: str = ""):
    """
    image_map: {filepath: description} from capture_all_layers()
    video_path: Path to the recorded/trimmed video, or None if recording failed
    reference_time: the moment the screenshots/video were captured -- the
        forecast covers the blocks AFTER this. Defaults to right now
        (the normal automated-capture case); pass this explicitly when
        feeding in a capture from earlier (see manual_prediction.py),
        so "predict the schedule going forward" means forward from the
        capture time, not from whenever this happens to run.
    num_blocks: how many 15-min blocks ahead to forecast (defaults to
        config.NUM_FORECAST_BLOCKS, i.e. 12 blocks / 3 hours). Pass e.g. 4
        for a 1-hour-ahead forecast instead of the usual 3 hours.
    output_dir: when given, BOTH the prediction CSV and the features log
        are written here instead of config.PREDICTIONS_DIR /
        config.FEATURES_LOG_DIR -- used by manual_prediction.py so test
        runs never mix into the real per-day production files or the CBR
        case store.
    intraday_actuals_text: optional "how generation went earlier TODAY"
        text (see daily_feedback.format_intraday_actuals_for_prompt()),
        fed into the LLM prompt alongside the usual CBR evidence and
        day-level context. Empty string if not applicable.
    intraday_actuals_path: optional raw meter-export CSV path for the
        same day. When provided, the pipeline derives a live regime and
        residual correction factor from the actual generation observed so
        far, then feeds that into the LLM prompt and the Step 1 scaffold.
    weather_text: optional prompt-ready ECMWF/Open-Meteo weather summary
        for the forecast horizon. Bhupalpally uses this to shape the LLM
        adjustment from the revision time forward.
    context_text: optional override for the rolling prediction context
        prompt text. When omitted, the shared day-level context is loaded
        from prediction_context/<PLANT>_context.json as before.
    video_text: optional prompt-ready summary of the Windy video/OpenCV
        features. Bhupalpally uses this in the weather-adjustment step,
        rather than feeding the video motion features into the Step 1 scaffold.
    meter_history_text: optional prompt-ready summary of the prior three
        days of meter behavior. Used as behavioral memory for Step 1.
    pvlib_text: optional prompt-ready pvlib physics summary for the
        forecast horizon. Used as a compact Step 1 physics hint.
    plant_performance_text: optional prompt-ready 8-day plant performance
        summary. Used as Step 3 in the stepwise prompt.
    """
    reference_time = reference_time or datetime.datetime.now()
    stepwise_live_plants = {"BHUPALPALLY", "KASIPET", "SIRMOUR"}
    stepwise_live_only = config.PLANT_NAME.upper() in stepwise_live_plants
    intraday_state = (
        daily_feedback.summarize_intraday_state(intraday_actuals_path, reference_time)
        if intraday_actuals_path
        else None
    )
    intraday_state_text = daily_feedback.format_intraday_state_for_prompt(intraday_state) if intraday_state else ""
    if stepwise_live_only and not intraday_actuals_text and intraday_actuals_path:
        intraday_actuals_text = daily_feedback.format_intraday_actuals_for_prompt(
            intraday_actuals_path,
            reference_time,
        )

    def _load_recent_meter_history_payload(text: str) -> dict | None:
        text = (text or "").strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not payload.get("days_data"):
            return None
        return payload

    # Make sure the rolling day-level context file exists before any S3
    # sync step runs on a fresh Lambda/container.
    daily_feedback.ensure_prediction_context_exists()

    # Make newly available SCADA outcomes usable for retrieval without a
    # separate manual join step. Only timestamp-matched feature rows are
    # enriched, so future forecasts can never leak their own outcomes.
    synced_actuals = daily_feedback.sync_historic_case_actuals()
    if synced_actuals:
        print(f"\nSynced {synced_actuals} historical SCADA actual(s) into the case store.")

    # Pick up any completed raw-meter CSVs already available locally:
    # merge them into the case store, run error/pattern analysis for each
    # affected day, and fold the results into the rolling prediction context.
    analyzed_dates = daily_feedback.process_actuals_inbox()
    if analyzed_dates:
        print(f"\nProcessed new actual-meter data for: {', '.join(analyzed_dates)}")

    print("\nExtracting image features (color + brightness per layer)...")
    image_features = image_feature_extraction.extract_image_features(image_map)
    print(f"  [OK] Extracted {len(image_features)} image-derived features.")

    motion_features = None
    if video_path is not None and video_path.exists():
        print(f"\nExtracting video motion features: {video_path}")
        motion_features = video_motion_features.analyze_video(video_path)
        if motion_features is None:
            print("  [WARN] Video feature extraction failed -- continuing without motion features.")
        else:
            print(f"  [OK] {motion_features['summary_text']}")
            if not video_text:
                video_text = motion_features["summary_text"]
    else:
        print("\n[INFO] No video available -- continuing without motion features.")

    # ---- Phase 1: build feature rows + Step 1 scaffold for all blocks ----
    print("\nBuilding feature rows and Step 1 scaffold for each forecast block...")
    block_times = time_features.get_block_times(
        reference_time, num_blocks=num_blocks or config.NUM_FORECAST_BLOCKS,
    )

    if intraday_state:
        print(f"  Live same-day regime: {intraday_state['regime']} "
              f"(residual factor={intraday_state['live_residual_factor']:.3f}, "
              f"fluctuation_flag={intraday_state['fluctuation_flag']})")

    recent_meter_history_payload = _load_recent_meter_history_payload(meter_history_text)
    if recent_meter_history_payload:
        print("  Loaded recent 3-day meter history JSON for Step 1 LLM input.")

    feature_rows_by_time = {}
    anchor_predictions = []
    live_anchor_predictions = []
    feature_columns = None

    for block_index, block_time in enumerate(block_times):
        block_time_feats = time_features.compute_time_features(block_time)
        if stepwise_live_only:
            feature_row = feature_builder.combine_features(None, {}, block_time_feats)
        else:
            feature_row = feature_builder.combine_features(motion_features, image_features, block_time_feats)

        if feature_columns is None:
            feature_columns = feature_builder.get_feature_columns(feature_row)

        if stepwise_live_only:
            live_residual_factor = 1.0
            if intraday_state and intraday_state.get("live_residual_factor") is not None:
                live_residual_factor = float(intraday_state.get("live_residual_factor", 1.0))
            anchor_mw = round(
                physics_anchor.calculate_anchor_mw(feature_row, correction_factor=live_residual_factor),
                3,
            )
        else:
            anchor_mw = round(
                float(intraday_state["latest_mw"]) if intraday_state and intraday_state.get("latest_mw") is not None else 0.0,
                3,
            )
        block_number = time_features.block_number_for_time(block_time)
        time_label = block_time.strftime("%Y-%m-%d %H:%M")

        feature_rows_by_time[time_label] = feature_row
        anchor_entry = {
            "time": time_label,
            "block_number": block_number,
            "anchor_mw": anchor_mw,
        }
        anchor_predictions.append(anchor_entry)

        live_anchor_entry = dict(anchor_entry)
        live_anchor_entry["base_anchor_mw"] = anchor_mw
        if intraday_state:
            live_anchor_entry["live_residual_factor"] = round(float(intraday_state.get("live_residual_factor", 1.0)), 3)
            live_anchor_entry["regime_label"] = intraday_state["regime"]
            live_anchor_entry["fluctuation_flag"] = intraday_state["fluctuation_flag"]
            live_anchor_entry["regime_summary"] = intraday_state["summary"]
        else:
            live_anchor_entry["live_residual_factor"] = 1.0
            live_anchor_entry["regime_label"] = "no live state"
            live_anchor_entry["fluctuation_flag"] = False
            live_anchor_entry["regime_summary"] = "No live same-day state summary was available."
        live_anchor_predictions.append(live_anchor_entry)
        print(f"  Block {block_number} ({time_label}): step1 scaffold = {anchor_mw} MW")

    # The first block's row is a fair representative of "the current
    # situation" for retrieval + the LLM.
    current_feature_row = feature_rows_by_time[anchor_predictions[0]["time"]]

    # ---- Phase 2: retrieve similar past cases from the case store ----
    if stepwise_live_only:
        print("\n[INFO] Stepwise live-only mode: skipping similar-case retrieval.")
        retrieved_cases = []
        retrieved_cases_text = ""
        image_map_for_llm = None
    else:
        print("\nRetrieving similar past cases from the case store...")
        retrieved_cases = similarity_retrieval.get_top_k_similar_cases(
            current_feature_row, k=config.CBR_TOP_K, exclude_time=anchor_predictions[0]["time"],
        )
        retrieved_cases_text = similarity_retrieval.format_cases_for_prompt(retrieved_cases)
        print(f"  {retrieved_cases_text}")
        image_map_for_llm = image_map

    # ---- Phase 3: LLM adjusts the anchor using the retrieved evidence ----
    print("\nAsking LLM to adjust the base forecast using retrieved evidence, live meter state, video, and weather...")
    context_text = context_text or daily_feedback.format_context_for_prompt()
    daily_revision_feedback_text = daily_feedback.format_daily_revision_feedback_for_prompt(reference_time)
    if stepwise_live_only:
        step1_input_parts = []
        if intraday_actuals_text.strip():
            step1_input_parts.append(
                "Current meter data up to revision time:\n"
                f"{intraday_actuals_text.strip()}"
            )
        if meter_history_text.strip():
            step1_input_parts.append(
                "Cached last 3 days meter JSON:\n"
                f"{meter_history_text.strip()}"
            )
        if pvlib_text.strip():
            step1_input_parts.append(
                "pvlib physics summary:\n"
                f"{pvlib_text.strip()}"
            )
        step1_inputs_text = "\n\n".join(step1_input_parts)
        meter_step_predictions = [
            {
                "time": anchor["time"],
                "block_number": anchor["block_number"],
                "anchor_mw": anchor["anchor_mw"],
            }
            for anchor in live_anchor_predictions
        ]
        base_forecast_by_time = {anchor["time"]: anchor["anchor_mw"] for anchor in live_anchor_predictions}
        base_reference_by_time = {anchor["time"]: anchor.get("base_anchor_mw", anchor["anchor_mw"]) for anchor in live_anchor_predictions}
        stepwise_context_parts = [
            plant_performance_text.strip(),
            context_text.strip(),
        ]
        stepwise_context_text = "\n\n".join(part for part in stepwise_context_parts if part)
        llm_predictions = llm_predictor.predict_stepwise_with_llm(
            meter_step_predictions,
            current_feature_row,
            step1_inputs_text,
            stepwise_context_text,
            intraday_state_text=intraday_state_text,
            step4_feedback_text=daily_revision_feedback_text,
            weather_text=weather_text,
            video_text=video_text,
            fallback_base_predictions=meter_step_predictions,
            prompt_subject=f"{config.PLANT_NAME} one-call revision forecast",
        )
        for p in llm_predictions:
            base_mw = base_forecast_by_time.get(p["time"], p.get("anchor_mw", 0.0))
            base_ref_mw = base_reference_by_time.get(p["time"], base_mw)
            p["base_anchor_mw"] = base_ref_mw
            p["step1_mw"] = p.get("step1_mw", p.get("anchor_mw", base_mw))
            p["step2_mw"] = p.get("step2_mw", p.get("step1_mw", p.get("anchor_mw", base_mw)))
            p["step3_mw"] = p.get("step3_mw", p.get("step2_mw", p.get("llm_mw", base_mw)))
            p["step4_mw"] = p.get("step4_mw", p.get("step3_mw", p.get("llm_mw", base_mw)))
            p["step2_confidence"] = p.get("confidence", "")
            p["step2_reasoning"] = p.get("reasoning", "")
    else:
        llm_predictions = llm_predictor.predict_with_llm(
            live_anchor_predictions, current_feature_row, retrieved_cases_text, context_text, intraday_actuals_text,
            intraday_state_text, weather_text, video_text,
            image_map=image_map_for_llm if stepwise_live_only else image_map,
        )
        for p in llm_predictions:
            base_mw = p.get("anchor_mw")
            p["step1_mw"] = p.get("base_anchor_mw", base_mw)
            p["step2_mw"] = p.get("llm_mw", base_mw)
            p["step3_mw"] = p.get("llm_mw", base_mw)
            p["step4_mw"] = p.get("llm_mw", base_mw)

    def _stage_factor_value(step_factors: dict, key: str) -> float:
        value = step_factors.get(key, 1.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    def _apply_stepwise_corrections(predictions: list) -> None:
        for p in predictions:
            step_factors = daily_feedback.recommend_stepwise_correction_factors(
                p["time"],
                intraday_state,
            )
            p["time_of_day_bucket"] = step_factors["bucket"]
            p["stepwise_base_factor"] = step_factors["base_factor"]
            p["bucket_bias"] = step_factors["bucket_bias"]
            p["step1_factor"] = step_factors["step1_factor"]
            p["step2_factor"] = step_factors["step2_factor"]
            p["step3_factor"] = step_factors["step3_factor"]
            p["step4_factor"] = step_factors["step4_factor"]

            raw_step1 = float(p.get("step1_mw", p.get("anchor_mw", 0.0)) or 0.0)
            raw_step2 = float(p.get("step2_mw", raw_step1) or raw_step1)
            raw_step3 = float(p.get("step3_mw", raw_step2) or raw_step2)
            raw_step4 = float(p.get("step4_mw", raw_step3) or raw_step3)
            raw_llm = float(p.get("llm_mw", raw_step4) or raw_step4)

            p["raw_step1_mw"] = round(raw_step1, 3)
            p["raw_step2_mw"] = round(raw_step2, 3)
            p["raw_step3_mw"] = round(raw_step3, 3)
            p["raw_step4_mw"] = round(raw_step4, 3)
            p["raw_llm_mw"] = round(raw_llm, 3)

            p["step1_mw"] = round(max(0.0, raw_step1 * _stage_factor_value(step_factors, "step1_factor")), 3)
            p["step2_mw"] = round(max(0.0, raw_step2 * _stage_factor_value(step_factors, "step2_factor")), 3)
            p["step3_mw"] = round(max(0.0, raw_step3 * _stage_factor_value(step_factors, "step3_factor")), 3)
            p["step4_mw"] = round(max(0.0, raw_step4 * _stage_factor_value(step_factors, "step4_factor")), 3)
            p["llm_mw"] = p["step4_mw"]
            p["correction_note"] = (
                f"bucket={step_factors['bucket']}; "
                f"base_factor={step_factors['base_factor']}; "
                f"factors=({step_factors['step1_factor']}, {step_factors['step2_factor']}, "
                f"{step_factors['step3_factor']}, {step_factors['step4_factor']})"
            )

    _apply_stepwise_corrections(llm_predictions)

    # ---- Phase 4: validate (range/deviation/smoothness safety checks) ----
    print("\nValidating LLM-adjusted predictions...")
    max_deviation_fraction = daily_feedback.suggested_max_deviation_fraction()
    if intraday_state and intraday_state["fluctuation_flag"]:
        max_deviation_fraction = min(0.85, max_deviation_fraction + 0.10)
        print("  Live same-day data looks choppy or shifting quickly -- widening the validator window slightly.")
    if max_deviation_fraction != validator.MAX_DEVIATION_FRACTION:
        print(f"  Recent accuracy history shows a consistent bias -- allowing up to "
              f"{max_deviation_fraction*100:.0f}% deviation this run (default is "
              f"{validator.MAX_DEVIATION_FRACTION*100:.0f}%).")
    validated_predictions = validator.validate_predictions(llm_predictions, max_deviation_fraction=max_deviation_fraction)
    for p in validated_predictions:
        clamp_factor, bucket_name = daily_feedback.recommend_final_clamp_factor(
            p["time"],
            intraday_state if stepwise_live_only else None,
        )
        p["time_of_day_bucket"] = bucket_name
        p["revision_clamp_factor"] = clamp_factor
        p["final_stage_cap_mw"] = round(float(p.get("step4_mw", p["validated_mw"])) * clamp_factor, 3)
        if clamp_factor < 1.0 and p["final_stage_cap_mw"] < p["validated_mw"]:
            previous_final = p["validated_mw"]
            p["validated_mw"] = round(min(p["validated_mw"], p["final_stage_cap_mw"]), 3)
            p["was_adjusted"] = True
            clamp_note = (
                f"final-stage clamp for {bucket_name} using live meter / same-bucket memory "
                f"(factor {clamp_factor:.3f}) pulled {previous_final} to {p['validated_mw']}"
            )
            existing_note = p.get("adjustment_note", "no adjustment needed")
            p["adjustment_note"] = (
                clamp_note if existing_note == "no adjustment needed" else f"{existing_note}; {clamp_note}"
            )
        if p.get("correction_note"):
            existing_note = p.get("adjustment_note", "no adjustment needed")
            p["adjustment_note"] = (
                p["correction_note"] if existing_note == "no adjustment needed" else f"{existing_note}; {p['correction_note']}"
            )
        flag = " [ADJUSTED BY VALIDATOR]" if p["was_adjusted"] else ""
        print(f"  Block {p['block_number']} ({p['time']}): anchor={p['anchor_mw']} MW -> "
              f"final={p['validated_mw']} MW (confidence={p['confidence']}){flag}")
        print(f"    Reasoning: {p['reasoning']}")
        if p["was_adjusted"]:
            print(f"    Validator note: {p['adjustment_note']}")
        if p.get("correction_note"):
            print(f"    Step corrections: {p['correction_note']}")

    def _compact_feature_snapshot(feature_row: dict) -> str:
        parts = []
        for label, key in (
            ("solar_elevation_deg", "solar_elevation_deg"),
            ("clouds_bright_pixel_pct", "clouds_bright_pixel_pct"),
            ("satellite_bright_pixel_pct", "satellite_bright_pixel_pct"),
            ("motion_coverage_end_pct", "motion_coverage_end_pct"),
            ("motion_score", "motion_score"),
            ("motion_direction_deg", "motion_direction_deg"),
        ):
            value = feature_row.get(key)
            if value is not None and value != "":
                parts.append(f"{label}={value}")
        return "; ".join(parts) if parts else "no compact feature snapshot available"

    top_case_summary = "no retrieved historical cases"
    if retrieved_cases:
        top_case = retrieved_cases[0]
        top_case_parts = [f"time={top_case.get('time', 'unknown')}"]
        if top_case.get("predicted_mw") != "":
            top_case_parts.append(f"predicted_mw={top_case.get('predicted_mw')}")
        if top_case.get("actual_mw") != "":
            top_case_parts.append(f"actual_mw={top_case.get('actual_mw')}")
        if top_case.get("error_mw") != "":
            top_case_parts.append(f"error_mw={top_case.get('error_mw')}")
        top_case_parts.append(f"distance={top_case.get('distance', '')}")
        top_case_summary = "; ".join(top_case_parts)

    context_summary = " ".join(line.strip() for line in context_text.splitlines()[:2] if line.strip()) or "No recent day-level accuracy history is available yet."

    # ---- Phase 5: store predictions + case store (features_log) ----
    generation_rows = []
    features_log_rows = []
    trace_rows = []
    for p in validated_predictions:
        final_mw = p["validated_mw"]
        feature_row = feature_rows_by_time[p["time"]]
        step1_scaffold_mw = p.get("base_anchor_mw", p["anchor_mw"])
        base_anchor_mw = p.get("base_anchor_mw", step1_scaffold_mw)
        step1_mw = p.get("step1_mw", p.get("base_anchor_mw", p["anchor_mw"]))
        step2_mw = p.get("step2_mw", p.get("llm_mw", final_mw))
        step3_mw = p.get("step3_mw", p.get("llm_mw", final_mw))
        step4_mw = p.get("step4_mw", p.get("llm_mw", final_mw))

        generation_rows.append({
            "block_number": p["block_number"],
            "time": p["time"],
            "step1_mw": step1_mw,
            "step2_mw": step2_mw,
            "step3_mw": step3_mw,
            "step4_mw": step4_mw,
            "llm_mw": p["llm_mw"],
            "final_mw": final_mw,
            "reasoning": p["reasoning"],
        })
        features_log_rows.append((p["block_number"], p["time"], feature_row, final_mw))
        trace_rows.append({
            "Block": p["block_number"],
            "Time": p["time"],
            "Step 1 Scaffold MW": step1_scaffold_mw,
            "Base Anchor MW": base_anchor_mw,
            "Raw Step 1 MW": p.get("raw_step1_mw", ""),
            "Raw Step 2 MW": p.get("raw_step2_mw", ""),
            "Raw Step 3 MW": p.get("raw_step3_mw", ""),
            "Raw Step 4 MW": p.get("raw_step4_mw", ""),
            "Raw LLM MW": p.get("raw_llm_mw", ""),
            "Step 1 LLM MW": step1_mw,
            "Step 2 Weather + Video MW": step2_mw,
            "Step 3 Plant Performance MW": step3_mw,
            "Step 4 Revision Feedback MW": step4_mw,
            "Stepwise Base Factor": p.get("stepwise_base_factor", ""),
            "Step 1 Factor": p.get("step1_factor", ""),
            "Step 2 Factor": p.get("step2_factor", ""),
            "Step 3 Factor": p.get("step3_factor", ""),
            "Step 4 Factor": p.get("step4_factor", ""),
            "Final Stage Cap MW": p.get("final_stage_cap_mw", ""),
            "Revision Clamp Factor": p.get("revision_clamp_factor", ""),
            "Time of Day Bucket": p.get("time_of_day_bucket", ""),
            "Correction Note": p.get("correction_note", ""),
            "Live Residual Factor": p.get("live_residual_factor", 1.0),
            "Regime": p.get("regime_label", ""),
            "Fluctuation Flag": p.get("fluctuation_flag", False),
            "Step 2 Confidence": p.get("step2_confidence", ""),
            "Step 2 Reasoning": p.get("step2_reasoning", ""),
            "LLM MW": p["llm_mw"],
            "Validated MW": final_mw,
            "Confidence": p["confidence"],
            "Reasoning": p["reasoning"],
            "Retrieved Cases Count": len(retrieved_cases),
            "Top Retrieved Case": top_case_summary,
            "Context Summary": context_summary,
            "Live State Summary": p.get("regime_summary", intraday_state_text),
            "Weather Summary": weather_text,
            "Feature Snapshot": _compact_feature_snapshot(feature_row),
        })

    csv_paths = prediction_store.save_generation_csv(generation_rows, output_dir=output_dir)
    print(f"\nEnergy generation predictions saved to: {', '.join(str(p.resolve()) for p in csv_paths)}")

    features_log_paths = prediction_store.save_features_log(features_log_rows, feature_columns, output_dir=output_dir)
    print(f"Feature rows logged to: {', '.join(str(p.resolve()) for p in features_log_paths)} "
          f"(these are the per-day case store files that similarity_retrieval.py searches "
          f"across, and that daily_feedback.py enriches with actual generation each evening)")

    trace_paths = prediction_store.save_forecast_trace_csv(trace_rows, output_dir=output_dir)
    print(f"Forecast trace written to: {', '.join(str(p.resolve()) for p in trace_paths)}")
