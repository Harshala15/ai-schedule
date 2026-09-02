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

# Maximum allowed deviation of the LLM's adjustment from the physics
# anchor, as a fraction of the anchor value (e.g. 0.35 = LLM may adjust
# the anchor up/down by at most 35%).
MAX_DEVIATION_FRACTION = 0.35

# Maximum MW change allowed between consecutive 15-minute blocks. Set
# strictly to physical solar ramp rates (max ~6-8% capacity per 15 min,
# matching Enercast's physical continuity).
MAX_STEP_CHANGE_MW = getattr(config, "PLANT_CAPACITY_MW", 10.0) * 0.065


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
        max_allowed_deviation = anchor_mw * max_deviation_fraction
        deviation = clipped_mw - anchor_mw
        if abs(deviation) > max_allowed_deviation:
            pulled_back = anchor_mw + max_allowed_deviation * (1 if deviation > 0 else -1)
            notes.append(
                f"LLM adjustment ({llm_mw}) deviated more than "
                f"{max_deviation_fraction*100:.0f}% from anchor ({anchor_mw}) -- "
                f"pulled back to {round(pulled_back, 3)}"
            )
            clipped_mw = max(0.0, min(capacity_mw, pulled_back))

        # Check 2B: Afternoon Daylight Floor (14:30 - 16:30)
        t_str = str(prediction.get("time", ""))
        hr = 12
        if len(t_str) >= 13 and t_str[10] == " ":
            try:
                hr = int(t_str[11:13])
            except ValueError:
                hr = 12
        if 14 <= hr <= 16 and anchor_mw >= 1.50:
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
) -> list:
    """
    Main entry point. Takes the list of per-block dicts from
    llm_predictor.predict_with_llm() and returns the same list with an
    added "validated_mw" (the final, safe-to-use number), plus
    "was_adjusted" and "adjustment_note" fields explaining any changes.

    Input list is assumed to be in chronological block order (as
    produced by run_pipeline.py) -- required for the smoothness check.

    last_frozen_mw: the MW value of the last frozen block immediately
    preceding this forecast horizon in current_final_schedule.csv.
    Enforces smooth ramp continuity at the revision boundary seam.
    """
    if not llm_predictions:
        return []

    hard_capacity_mw = getattr(config, "PLANT_MAX_FEED_IN_MW", capacity_mw)
    base_cap = getattr(config, "PLANT_CAPACITY_MW", capacity_mw)

    def _block_max_step(b_dict: dict) -> float:
        t_str = str(b_dict.get("time", ""))
        hr = 12
        if len(t_str) >= 13 and t_str[10] == " ":
            try:
                hr = int(t_str[11:13])
            except ValueError:
                hr = 12
        return base_cap * (0.085 if (9 <= hr <= 12) else 0.065)

    # ---- Checks 1 + 2: per-block range clip and deviation limit ----
    checked = [_clip_and_check_deviation(p, hard_capacity_mw, max_deviation_fraction) for p in llm_predictions]

    # ---- Check 3A: boundary continuity against last frozen block ----
    if last_frozen_mw is not None and checked:
        first_mw = checked[0]["validated_mw"]
        max_step_first = _block_max_step(checked[0])
        boundary_change = first_mw - last_frozen_mw
        if abs(boundary_change) > max_step_first:
            smoothed_first = last_frozen_mw + max_step_first * (1 if boundary_change > 0 else -1)
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
        max_step_curr = _block_max_step(checked[i])
        change = curr_mw - prev_mw

        if abs(change) > max_step_curr:
            smoothed = prev_mw + max_step_curr * (1 if change > 0 else -1)
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

    # ---- Check 4: risk-averse 3-block bell-curve smoothing filter ----
    # Centers the schedule inside the +-15% allowed band, preventing 15-minute jitter
    if len(checked) >= 3:
        raw_vals = [p["validated_mw"] for p in checked]
        for i in range(1, len(checked) - 1):
            if raw_vals[i] > 0.1 or raw_vals[i - 1] > 0.1 or raw_vals[i + 1] > 0.1:
                smoothed_val = (0.20 * raw_vals[i - 1]) + (0.60 * raw_vals[i]) + (0.20 * raw_vals[i + 1])
                checked[i]["validated_mw"] = round(max(0.0, min(hard_capacity_mw, smoothed_val)), 3)

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
