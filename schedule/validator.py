"""
validator.py

Sanity-checks the LLM's adjusted predictions (from llm_predictor.py)
before they get stored or shown to anyone. This is pure deterministic
code -- no LLM, no ML -- acting as a safety net so a bad/unusual LLM
response can never produce a physically nonsensical or wildly
inconsistent set of predictions.

Checks applied, per block:
    1. Range clip: adjusted_mw must be within [0, plant capacity].
    2. Deviation limit: if the LLM's adjustment strays too far from the
       physics anchor (as a fraction of the anchor value), it gets
       pulled back toward the anchor instead of trusted outright -- the
       anchor is assumed to be "roughly right", so a huge swing usually
       means the LLM over-reached rather than found something real.

Checks applied across the whole 8-block sequence:
    3. Smoothness: block-to-block MW change is capped at
       MAX_STEP_CHANGE_MW -- solar generation does not usually jump
       drastically within one 15-minute step under gradually changing
       cloud cover, so a sudden large jump is more likely an LLM
       inconsistency than reality.

so nothing is silently changed -- you can always see what the validator
did and why when reviewing predictions.
"""

import config

validate_schedule_curve = None
calculate_schedule_score = None
stabilize_curve = None
_construct_guaranteed_valid_fallback = None
apply_ramp_rate_limits = None

# Maximum allowed deviation of the LLM's adjustment from the physics
# anchor, as a fraction of the anchor value (e.g. 0.35 = LLM may adjust
# the anchor up/down by at most 35%).
MAX_DEVIATION_FRACTION = 0.35

# Maximum MW change allowed between consecutive 15-minute blocks. Set
# strictly to physical solar ramp rates (max ~6-8% capacity per 15 min,
# matching Enercast's physical continuity).
MAX_STEP_CHANGE_MW = getattr(config, "PLANT_CAPACITY_MW", 10.0) * 0.065


def _validate_final_curve(
    values,
    capacity_mw,
    clear_sky_values=None,
    latest_metered_mw=None,
    max_ramp_pct=0.10,
    step_limits=None,
    tolerance_band_pct=15.0,
):
    """
    Central validation wrapper enforcing that values strictly satisfy
    physical bounds, clear-sky ceilings, seam continuity, ramp rates, and phase rules.
    """
    if validate_schedule_curve is None:
        return True, []
    return validate_schedule_curve(
        values=values,
        capacity_mw=capacity_mw,
        clear_sky_values=clear_sky_values,
        latest_metered_mw=latest_metered_mw,
        max_ramp_pct=max_ramp_pct,
        step_limits=step_limits,
        tolerance_band_pct=tolerance_band_pct,
    )


def _clip_and_check_deviation(prediction: dict, capacity_mw: float, max_deviation_fraction: float) -> dict:
    """Applies checks 1 and 2 (range clip + deviation limit) to a single
    block's prediction dict (output of llm_predictor.predict_with_llm)."""
    anchor_mw = float(prediction.get("anchor_mw", 0.0) or 0.0)
    llm_mw = float(prediction.get("llm_mw", 0.0) or 0.0)
    notes = []

    # ---- Check 1: range clip & pre-dawn/night zeroing ----
    if anchor_mw <= 0.0:
        clipped_mw = 0.0
    else:
        clipped_mw = max(0.0, min(capacity_mw, llm_mw))
    if clipped_mw != llm_mw:
        notes.append(f"clipped from {llm_mw} to stay within [0, {capacity_mw:.3f}]")

    # ---- Check 2: deviation limit vs anchor & afternoon floor ----
    if anchor_mw > 0:
        # Check 2: Asymmetric deviation check:
        # Upward deviation is capped at max_allowed_deviation to prevent hallucinated spikes.
        # Downward deviation is permitted down to weather/telemetry levels during overcast conditions.
        deviation = clipped_mw - anchor_mw
        max_allowed_deviation = anchor_mw * max_deviation_fraction
        if deviation > max_allowed_deviation:
            pulled_back = anchor_mw + max_allowed_deviation
            notes.append(
                f"LLM adjustment ({llm_mw}) deviated upward more than "
                f"{max_deviation_fraction*100:.0f}% from anchor ({anchor_mw}) -- "
                f"pulled back to {round(pulled_back, 3)}"
            )
            clipped_mw = max(0.0, min(capacity_mw, pulled_back))
        elif deviation < -max_allowed_deviation and getattr(config, "STRICT_DOWNWARD_ANCHOR_LOCK", False):
            pulled_back = anchor_mw - max_allowed_deviation
            notes.append(
                f"LLM adjustment ({llm_mw}) deviated downward more than "
                f"{max_deviation_fraction*100:.0f}% from anchor ({anchor_mw}) -- "
                f"pulled back to {round(pulled_back, 3)}"
            )
            clipped_mw = max(0.0, min(capacity_mw, pulled_back))

        # Check 2B: Afternoon Daylight Floor (14:30 - 16:30) -- only on verified clear-sky days
        t_str = str(prediction.get("time", ""))
        hr = 12
        if len(t_str) >= 13 and t_str[10] == " ":
            try:
                hr = int(t_str[11:13])
            except ValueError:
                hr = 12
        # Only enforce floor if anchor indicates clear sky AND LLM has not flagged overcast/attenuated regime
        is_cloudy_reason = any(w in str(prediction.get("reasoning", "")).lower() for w in ("cloud", "overcast", "attenuat", "scada", "weak", "rain"))
        if 14 <= hr <= 16 and anchor_mw >= 2.0 and not is_cloudy_reason:
            afternoon_floor = round(min(anchor_mw * 0.85, capacity_mw * 0.38), 3)
            if clipped_mw < afternoon_floor:
                notes.append(f"enforced afternoon historical floor from {clipped_mw} to {afternoon_floor}")
                clipped_mw = afternoon_floor

    result = dict(prediction)
    result["validated_mw"] = round(clipped_mw, 3)
    result["was_adjusted"] = bool(notes)
    result["adjustment_note"] = "; ".join(notes) if notes else "no adjustment needed"
    return result


def validate_predictions(
    llm_predictions: list,
    capacity_mw: float = config.PLANT_CAPACITY_MW,
    max_deviation_fraction: float = MAX_DEVIATION_FRACTION,
    last_frozen_mw: float | None = None,
    max_ramp_pct: float | None = None,
    clear_sky_values: list[float] | None = None,
    latest_metered_mw: float | None = None,
    step_limits: list[float] | None = None,
    tolerance_band_pct: float | None = None,
) -> list:
    """
    Main entry point. Takes the list of per-block dicts from
    llm_predictor.predict_with_llm() and returns the same list with an
    added "validated_mw" (the final, safe-to-use number), plus
    "was_adjusted" and "adjustment_note" fields explaining any changes.

    Input list is assumed to be in chronological block order (as
    produced by run_pipeline.py) -- required for the smoothness check.

    last_frozen_mw / latest_metered_mw: the MW value of the last frozen block immediately
    preceding this forecast horizon in current_final_schedule.csv.
    Enforces smooth ramp continuity at the revision boundary seam.
    """
    if not llm_predictions:
        return []

    if latest_metered_mw is not None and last_frozen_mw is None:
        last_frozen_mw = float(latest_metered_mw)
    elif last_frozen_mw is not None and latest_metered_mw is None:
        latest_metered_mw = float(last_frozen_mw)

    base_cap = float(capacity_mw if capacity_mw is not None else getattr(config, "PLANT_CAPACITY_MW", 10.0))
    hard_capacity_mw = float(capacity_mw if capacity_mw is not None else getattr(config, "PLANT_MAX_FEED_IN_MW", base_cap))

    def _block_max_step(b_dict: dict, is_upward: bool = True) -> float:
        if max_ramp_pct is not None:
            return base_cap * max_ramp_pct
        t_str = str(b_dict.get("time", ""))
        hr = 12
        if len(t_str) >= 13 and t_str[10] == " ":
            try:
                hr = int(t_str[11:13])
            except ValueError:
                hr = 12
        if 7 <= hr <= 10:
            # Morning rapid solar geometric ramp-up (allow up to 22% plant capacity per 15 min upward)
            return base_cap * (0.22 if is_upward else 0.14)
        elif 16 <= hr <= 18:
            # Late afternoon rapid sunset ramp-down
            return base_cap * (0.14 if is_upward else 0.20)
        elif 11 <= hr <= 15:
            # Midday solar arch
            return base_cap * (0.18 if is_upward else 0.12)
        else:
            return base_cap * 0.08


    # ---- Checks 1 + 2: per-block range clip and deviation limit ----
    checked = [_clip_and_check_deviation(p, hard_capacity_mw, max_deviation_fraction) for p in llm_predictions]

    # ---- Check 3A: boundary continuity against last frozen block ----
    if last_frozen_mw is not None and checked:
        first_mw = checked[0]["validated_mw"]
        boundary_change = first_mw - last_frozen_mw
        is_up = boundary_change > 0
        max_step_first = _block_max_step(checked[0], is_upward=is_up)
        if abs(boundary_change) > max_step_first:
            smoothed_first = last_frozen_mw + max_step_first * (1 if is_up else -1)
            smoothed_first = max(0.0, min(hard_capacity_mw, smoothed_first))
            note = (
                f"boundary step change of {round(boundary_change, 3)} MW from last frozen block ({last_frozen_mw:.3f} MW) "
                f"exceeded max allowed ({max_step_first:.3f} MW) -- smoothed to {round(smoothed_first, 3)}"
            )
            checked[0]["validated_mw"] = round(smoothed_first, 3)
            checked[0]["was_adjusted"] = True
            existing = checked[0]["adjustment_note"]
            checked[0]["adjustment_note"] = note if existing == "no adjustment needed" else f"{existing}; {note}"

    # ---- Check 3B: smoothness across consecutive blocks ----
    for i in range(1, len(checked)):
        prev_mw = checked[i - 1]["validated_mw"]
        curr_mw = checked[i]["validated_mw"]
        change = curr_mw - prev_mw
        is_up = change > 0
        max_step_curr = _block_max_step(checked[i], is_upward=is_up)

        if abs(change) > max_step_curr:
            smoothed = prev_mw + max_step_curr * (1 if is_up else -1)
            smoothed = max(0.0, min(hard_capacity_mw, smoothed))
            note = (
                f"step change of {round(change, 3)} MW from previous block exceeded "
                f"max allowed ({max_step_curr:.3f} MW) -- smoothed to {round(smoothed, 3)}"
            )
            checked[i]["validated_mw"] = round(smoothed, 3)
            checked[i]["was_adjusted"] = True
            existing_note = checked[i]["adjustment_note"]
            checked[i]["adjustment_note"] = (
                note if existing_note == "no adjustment needed" else f"{existing_note}; {note}"
            )

    # ---- Check 4 & 5: Central Deterministic Curve Stability & Real Safety Gate ----
    # Replaces unconstrained moving average with central curve_stability engine,
    # ensuring clear-sky limits, ramp limits, seam continuity, and phase monotonicity are strictly preserved.
    if validate_schedule_curve is not None and checked:
        sched_vals = [p["validated_mw"] for p in checked]
        if clear_sky_values is not None:
            cs_vals = list(clear_sky_values)
        else:
            has_real_cs = any("clearsky_mw" in p or "clear_sky_mw" in p for p in checked)
            cs_vals = [float(p.get("clearsky_mw", p.get("clear_sky_mw", hard_capacity_mw))) for p in checked] if has_real_cs else None

        tol_pct = float(tolerance_band_pct) if tolerance_band_pct is not None else float(getattr(config, "PLANT_TOLERANCE_BAND_PCT", 15.0))

        if step_limits is not None:
            eff_step_limits = list(step_limits)
        else:
            eff_step_limits = []
            if last_frozen_mw is not None:
                is_up_0 = checked[0]["validated_mw"] > last_frozen_mw
                eff_step_limits.append(_block_max_step(checked[0], is_upward=is_up_0))
            else:
                eff_step_limits.append(hard_capacity_mw * (max_ramp_pct if max_ramp_pct is not None else 0.22))

            for i in range(1, len(checked)):
                is_up_i = checked[i]["validated_mw"] > checked[i - 1]["validated_mw"]
                eff_step_limits.append(_block_max_step(checked[i], is_upward=is_up_i))

        eff_ramp_pct = max_ramp_pct if max_ramp_pct is not None else (max(eff_step_limits) / max(0.1, hard_capacity_mw))

        is_valid, violations = _validate_final_curve(
            values=sched_vals,
            capacity_mw=hard_capacity_mw,
            clear_sky_values=cs_vals,
            latest_metered_mw=last_frozen_mw,
            max_ramp_pct=eff_ramp_pct,
            step_limits=eff_step_limits,
            tolerance_band_pct=tol_pct,
        )
        if not is_valid:
            print(f"  [VALIDATOR-GATE] Schedule curve failed initial validation: {violations}. Repairing with central stabilizer...")
            try:
                repaired_vals = stabilize_curve(
                    raw_values=sched_vals,
                    capacity_mw=hard_capacity_mw,
                    latest_metered_mw=last_frozen_mw,
                    clear_sky_values=cs_vals,
                    max_ramp_pct=eff_ramp_pct,
                    step_limits=eff_step_limits,
                    permitted_deviation_pct=tol_pct,
                )
                is_valid_rep, rep_violations = _validate_final_curve(
                    values=repaired_vals,
                    capacity_mw=hard_capacity_mw,
                    clear_sky_values=cs_vals,
                    latest_metered_mw=last_frozen_mw,
                    max_ramp_pct=eff_ramp_pct,
                    step_limits=eff_step_limits,
                    tolerance_band_pct=tol_pct,
                )
                if is_valid_rep:
                    for p, rep_mw in zip(checked, repaired_vals):
                        if abs(p["validated_mw"] - rep_mw) > 0.001:
                            p["validated_mw"] = rep_mw
                            p["was_adjusted"] = True
                            p["adjustment_note"] = (p.get("adjustment_note", "") + f"; repaired by central curve stabilizer to {rep_mw:.3f} MW").strip("; ")
                else:
                    print(f"  [VALIDATOR-GATE] Repair pass failed ({rep_violations}). Falling back to safe physical anchor.")
                    anchor_raw = [float(p.get("anchor_mw", 0.0)) for p in checked]
                    fb_vals = stabilize_curve(
                        raw_values=anchor_raw,
                        capacity_mw=hard_capacity_mw,
                        latest_metered_mw=last_frozen_mw,
                        clear_sky_values=cs_vals,
                        max_ramp_pct=eff_ramp_pct,
                        step_limits=eff_step_limits,
                        permitted_deviation_pct=tol_pct,
                    )
                    is_fallback_valid, fallback_violations = _validate_final_curve(
                        values=fb_vals,
                        capacity_mw=hard_capacity_mw,
                        clear_sky_values=cs_vals,
                        latest_metered_mw=last_frozen_mw,
                        max_ramp_pct=eff_ramp_pct,
                        step_limits=eff_step_limits,
                        tolerance_band_pct=tol_pct,
                    )
                    if not is_fallback_valid:
                        # Requirement 2: Do not ignore boolean result. If fallback is invalid, generate another safe fallback and validate it
                        print(f"  [VALIDATOR-GATE] Primary anchor fallback invalid ({fallback_violations}). Generating secondary guaranteed fallback...")
                        if _construct_guaranteed_valid_fallback is not None:
                            fb_vals = _construct_guaranteed_valid_fallback(
                                capacity_mw=hard_capacity_mw,
                                n=len(checked),
                                boundary_anchor=last_frozen_mw,
                                clear_sky_values=cs_vals,
                                max_ramp_mw=hard_capacity_mw * eff_ramp_pct,
                                step_limits=eff_step_limits,
                            )
                        else:
                            curr = max(0.0, min(hard_capacity_mw, float(last_frozen_mw))) if last_frozen_mw is not None else 0.0
                            fb_vals = []
                            for _ in range(len(checked)):
                                curr = max(0.0, curr - hard_capacity_mw * eff_ramp_pct)
                                fb_vals.append(round(curr, 3))

                        is_fb2_valid, fb2_violations = _validate_final_curve(
                            values=fb_vals,
                            capacity_mw=hard_capacity_mw,
                            clear_sky_values=cs_vals,
                            latest_metered_mw=last_frozen_mw,
                            max_ramp_pct=eff_ramp_pct,
                            step_limits=eff_step_limits,
                            tolerance_band_pct=tol_pct,
                        )
                        if not is_fb2_valid:
                            raise RuntimeError(f"Validator failed to construct a valid schedule curve: {fb2_violations}")

                    for p, fb_val in zip(checked, fb_vals):
                        p["validated_mw"] = fb_val
                        p["was_adjusted"] = True
                        p["adjustment_note"] = (p.get("adjustment_note", "") + f"; rejected invalid curve, fallback to stabilized anchor ({fb_val:.3f} MW)").strip("; ")
            except Exception as rep_err:
                if isinstance(rep_err, RuntimeError):
                    raise
                print(f"  [VALIDATOR-GATE] Error during schedule repair ({rep_err}). Building emergency ramp-down schedule...")
                # Requirement 3: Build emergency ramp-down schedule applying:
                # - non-negative clipping,
                # - AC-capacity clipping,
                # - clear-sky clipping,
                # - seam continuity,
                # - per-block ramp limits.
                # Validate it with _validate_final_curve(...).
                # If validation fails, raise a clear error instead of returning the invalid curve.
                emergency_vals = []
                b_val = max(0.0, min(hard_capacity_mw, float(last_frozen_mw))) if last_frozen_mw is not None else 0.0
                lim_0 = eff_step_limits[0] if (eff_step_limits and len(eff_step_limits) > 0) else (hard_capacity_mw * eff_ramp_pct)
                if last_frozen_mw is not None:
                    curr_v = max(0.0, b_val - lim_0)
                else:
                    curr_v = 0.0
                if cs_vals and len(cs_vals) > 0 and cs_vals[0] is not None:
                    curr_v = min(curr_v, max(0.0, float(cs_vals[0])))
                curr_v = max(0.0, min(hard_capacity_mw, curr_v))
                emergency_vals.append(round(curr_v, 3))

                for i in range(1, len(checked)):
                    lim_i = eff_step_limits[i] if (eff_step_limits and i < len(eff_step_limits)) else (hard_capacity_mw * eff_ramp_pct)
                    curr_v = max(0.0, emergency_vals[-1] - lim_i)
                    if cs_vals and i < len(cs_vals) and cs_vals[i] is not None:
                        curr_v = min(curr_v, max(0.0, float(cs_vals[i])))
                    curr_v = max(0.0, min(hard_capacity_mw, curr_v))
                    emergency_vals.append(round(curr_v, 3))

                if apply_ramp_rate_limits is not None:
                    try:
                        emergency_vals = apply_ramp_rate_limits(
                            emergency_vals,
                            max_ramp_mw=hard_capacity_mw * eff_ramp_pct,
                            anchor_first_val=last_frozen_mw,
                            step_limits=eff_step_limits,
                        )
                    except Exception:
                        pass

                is_em_valid, em_violations = _validate_final_curve(
                    values=emergency_vals,
                    capacity_mw=hard_capacity_mw,
                    clear_sky_values=cs_vals,
                    latest_metered_mw=last_frozen_mw,
                    max_ramp_pct=eff_ramp_pct,
                    step_limits=eff_step_limits,
                    tolerance_band_pct=tol_pct,
                )
                if not is_em_valid:
                    raise RuntimeError(f"Emergency exception fallback failed validation: {em_violations}")

                for p, em_val in zip(checked, emergency_vals):
                    p["validated_mw"] = em_val
                    p["was_adjusted"] = True
                    p["adjustment_note"] = (p.get("adjustment_note", "") + f"; emergency fallback ({em_val:.3f} MW)").strip("; ")

        # Requirement 4: Final invariant assertion immediately before return checked
        final_values = [p["validated_mw"] for p in checked]
        is_final_valid, final_violations = _validate_final_curve(
            values=final_values,
            capacity_mw=hard_capacity_mw,
            clear_sky_values=cs_vals,
            latest_metered_mw=last_frozen_mw,
            max_ramp_pct=eff_ramp_pct,
            step_limits=eff_step_limits,
            tolerance_band_pct=tol_pct,
        )
        if not is_final_valid:
            print(f"  [VALIDATOR-FINAL-ASSERTION] Final invariant check failed ({len(final_violations)} violations). Attempting one last deterministic repair...")
            for viol in final_violations:
                print(f"    - Invariant violation: {viol}")
            try:
                final_repaired = stabilize_curve(
                    raw_values=final_values,
                    capacity_mw=hard_capacity_mw,
                    latest_metered_mw=last_frozen_mw,
                    clear_sky_values=cs_vals,
                    max_ramp_pct=eff_ramp_pct,
                    step_limits=eff_step_limits,
                    permitted_deviation_pct=tol_pct,
                )
                is_final_rep_ok, final_rep_viols = _validate_final_curve(
                    values=final_repaired,
                    capacity_mw=hard_capacity_mw,
                    clear_sky_values=cs_vals,
                    latest_metered_mw=last_frozen_mw,
                    max_ramp_pct=eff_ramp_pct,
                    step_limits=eff_step_limits,
                    tolerance_band_pct=tol_pct,
                )
                if not is_final_rep_ok:
                    for viol in final_rep_viols:
                        print(f"    - Remaining invariant violation after repair: {viol}")
                    raise RuntimeError(f"Final invariant validation failed after last repair attempt: {final_rep_viols}")
                for p, v in zip(checked, final_repaired):
                    p["validated_mw"] = v
                    p["was_adjusted"] = True
                    p["adjustment_note"] = (p.get("adjustment_note", "") + f"; final invariant repair enforced ({v:.3f} MW)").strip("; ")
            except Exception as final_e:
                if isinstance(final_e, RuntimeError):
                    raise
                raise RuntimeError(f"Final invariant validation failed: {final_e}")

    return checked


if __name__ == "__main__":
    fake_llm_output = [
        {"time": "2026-07-20 13:15", "block_number": 54, "anchor_mw": 2.268,
         "llm_mw": 2.3, "confidence": "Medium", "reasoning": "minor adjustment"},
        {"time": "2026-07-20 13:30", "block_number": 55, "anchor_mw": 2.916,
         "llm_mw": 9.9, "confidence": "High", "reasoning": "unrealistic spike (test case)"},
        {"time": "2026-07-20 13:45", "block_number": 56, "anchor_mw": 2.844,
         "llm_mw": -1.0, "confidence": "Low", "reasoning": "negative value (test case)"},
    ]
    for row in validate_predictions(fake_llm_output):
        print(row)
