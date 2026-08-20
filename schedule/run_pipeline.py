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

Only ONE LLM call happens per run, covering all 8 forecast blocks at
once (not one call per block) -- this keeps cost/latency reasonable.

Called from test_multi_image.py's run_once(), right after screenshots +
video have been captured.
"""

import datetime

import config
import image_feature_extraction
import video_motion_features
import time_features
import feature_builder
import physics_anchor
import similarity_retrieval
import llm_predictor
import validator
import prediction_store
import daily_feedback


def run_prediction_pipeline(image_map: dict, video_path, reference_time: datetime.datetime = None,
                            num_blocks: int = None, output_dir=None, intraday_actuals_text: str = "",
                            intraday_actuals_path=None):
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
        config.NUM_FORECAST_BLOCKS, i.e. 8 blocks / 2 hours). Pass e.g. 4
        for a 1-hour-ahead forecast instead of the usual 2 hours.
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
        far, then feeds that into the LLM prompt and the baseline anchor.
    """
    reference_time = reference_time or datetime.datetime.now()
    bhupalpally_live_only = config.PLANT_NAME.upper() == "BHUPALPALLY"
    intraday_state = (
        daily_feedback.summarize_intraday_state(intraday_actuals_path, reference_time)
        if intraday_actuals_path
        else None
    )
    intraday_state_text = daily_feedback.format_intraday_state_for_prompt(intraday_state) if intraday_state else ""
    if bhupalpally_live_only and not intraday_actuals_text and intraday_actuals_path:
        intraday_actuals_text = daily_feedback.format_intraday_actuals_for_prompt(
            intraday_actuals_path,
            reference_time,
        )

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
    else:
        print("\n[INFO] No video available -- continuing without motion features.")

    # ---- Phase 1: build feature rows + physics anchor for all blocks ----
    print("\nBuilding feature rows and physics anchor for each forecast block...")
    block_times = time_features.get_block_times(
        reference_time, num_blocks=num_blocks or config.NUM_FORECAST_BLOCKS,
    )

    # Empirical, bounded self-correction derived from all real SCADA
    # history synced so far (no-op / 1.0 until enough of it exists --
    # see daily_feedback.compute_anchor_correction_factor()).
    anchor_correction_factor = daily_feedback.compute_anchor_correction_factor()
    if anchor_correction_factor != 1.0:
        print(f"  Applying empirical anchor correction factor: {anchor_correction_factor:.3f} "
              f"(derived from accumulated real SCADA history)")
    if intraday_state:
        print(f"  Live same-day regime: {intraday_state['regime']} "
              f"(residual factor={intraday_state['live_residual_factor']:.3f}, "
              f"fluctuation_flag={intraday_state['fluctuation_flag']})")

    feature_rows_by_time = {}
    anchor_predictions = []
    live_anchor_predictions = []
    feature_columns = None

    for block_index, block_time in enumerate(block_times):
        block_time_feats = time_features.compute_time_features(block_time)
        feature_row = feature_builder.combine_features(motion_features, image_features, block_time_feats)

        if feature_columns is None:
            feature_columns = feature_builder.get_feature_columns(feature_row)

        anchor_mw = physics_anchor.calculate_anchor_mw(feature_row, correction_factor=anchor_correction_factor)
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
        if intraday_state:
            horizon_weight = max(0.35, 1.0 - (block_index * 0.12))
            live_factor = 1.0 + (intraday_state["live_residual_factor"] - 1.0) * horizon_weight
            live_anchor_entry["base_anchor_mw"] = anchor_mw
            live_anchor_entry["anchor_mw"] = round(anchor_mw * live_factor, 3)
            live_anchor_entry["live_residual_factor"] = round(live_factor, 3)
            live_anchor_entry["regime_label"] = intraday_state["regime"]
            live_anchor_entry["fluctuation_flag"] = intraday_state["fluctuation_flag"]
            live_anchor_entry["regime_summary"] = intraday_state["summary"]
        else:
            live_anchor_entry["base_anchor_mw"] = anchor_mw
            live_anchor_entry["live_residual_factor"] = 1.0
            live_anchor_entry["regime_label"] = "no live state"
            live_anchor_entry["fluctuation_flag"] = False
            live_anchor_entry["regime_summary"] = "No live same-day state summary was available."
        live_anchor_predictions.append(live_anchor_entry)
        print(f"  Block {block_number} ({time_label}): physics anchor = {anchor_mw} MW")

    # image/motion features are identical across all 8 blocks in one run
    # (only time changes) -- the first block's row is a fair
    # representative of "the current situation" for retrieval + the LLM.
    current_feature_row = feature_rows_by_time[anchor_predictions[0]["time"]]

    # ---- Phase 2: retrieve similar past cases from the case store ----
    if bhupalpally_live_only:
        print("\n[INFO] Bhupalpally live-only mode: skipping similar-case retrieval.")
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
    print("\nAsking LLM to adjust physics anchor using retrieved evidence...")
    context_text = daily_feedback.format_context_for_prompt()
    llm_predictions = llm_predictor.predict_with_llm(
        live_anchor_predictions, current_feature_row, retrieved_cases_text, context_text, intraday_actuals_text,
        intraday_state_text,
        image_map=image_map_for_llm if bhupalpally_live_only else image_map,
    )

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
        flag = " [ADJUSTED BY VALIDATOR]" if p["was_adjusted"] else ""
        print(f"  Block {p['block_number']} ({p['time']}): anchor={p['anchor_mw']} MW -> "
              f"final={p['validated_mw']} MW (confidence={p['confidence']}){flag}")
        print(f"    Reasoning: {p['reasoning']}")
        if p["was_adjusted"]:
            print(f"    Validator note: {p['adjustment_note']}")

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
        base_anchor_mw = p.get("base_anchor_mw", p["anchor_mw"])

        generation_rows.append((p["block_number"], p["time"], p["anchor_mw"], final_mw, p["reasoning"]))
        features_log_rows.append((p["block_number"], p["time"], feature_row, final_mw))
        trace_rows.append({
            "Block": p["block_number"],
            "Time": p["time"],
            "Physics Anchor MW": p["anchor_mw"],
            "Base Physics Anchor MW": base_anchor_mw,
            "Live Residual Factor": p.get("live_residual_factor", 1.0),
            "Regime": p.get("regime_label", ""),
            "Fluctuation Flag": p.get("fluctuation_flag", False),
            "LLM MW": p["llm_mw"],
            "Validated MW": final_mw,
            "Confidence": p["confidence"],
            "Reasoning": p["reasoning"],
            "Retrieved Cases Count": len(retrieved_cases),
            "Top Retrieved Case": top_case_summary,
            "Context Summary": context_summary,
            "Live State Summary": p.get("regime_summary", intraday_state_text),
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
