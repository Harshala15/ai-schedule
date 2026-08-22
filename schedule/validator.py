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

Every adjustment made here is recorded (was_adjusted + adjustment_note),
so nothing is silently changed -- you can always see what the validator
did and why when reviewing predictions.
"""

import config

# Maximum allowed deviation of the LLM's adjustment from the physics
# anchor, as a fraction of the anchor value (e.g. 0.4 = LLM may adjust
# the anchor up/down by at most 40%). Tune this based on how much you
# trust the LLM's adjustments vs the anchor as you gather more data.
MAX_DEVIATION_FRACTION = 0.40

# Maximum MW change allowed between consecutive 15-minute blocks. Set
# relative to plant capacity by default; override if you have a better
# sense of realistic ramp rates for your plant.
MAX_STEP_CHANGE_MW = config.PLANT_CAPACITY_MW * 0.35


def _clip_and_check_deviation(prediction: dict, capacity_mw: float, max_deviation_fraction: float) -> dict:
    """Applies checks 1 and 2 (range clip + deviation limit) to a single
    block's prediction dict (output of llm_predictor.predict_with_llm)."""
    anchor_mw = prediction["anchor_mw"]
    llm_mw = prediction["llm_mw"]
    notes = []

    # ---- Check 1: range clip ----
    clipped_mw = max(0.0, min(capacity_mw, llm_mw))
    if clipped_mw != llm_mw:
        notes.append(f"clipped from {llm_mw} to stay within [0, {capacity_mw}]")

    # ---- Check 2: deviation limit vs anchor ----
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

    result = dict(prediction)
    result["validated_mw"] = round(clipped_mw, 3)
    result["was_adjusted"] = bool(notes)
    result["adjustment_note"] = "; ".join(notes) if notes else "no adjustment needed"
    return result


def validate_predictions(llm_predictions: list, capacity_mw: float = config.PLANT_CAPACITY_MW,
                          max_deviation_fraction: float = MAX_DEVIATION_FRACTION) -> list:
    """
    Main entry point. Takes the list of per-block dicts from
    llm_predictor.predict_with_llm() and returns the same list with an
    added "validated_mw" (the final, safe-to-use number), plus
    "was_adjusted" and "adjustment_note" fields explaining any changes.

    Input list is assumed to be in chronological block order (as
    produced by run_pipeline.py) -- required for the smoothness check.

    max_deviation_fraction: overrides MAX_DEVIATION_FRACTION for this call
    -- see daily_feedback.suggested_max_deviation_fraction(), which widens
    this when multiple recent days show the SAME consistent bias
    direction (repeated real evidence the anchor needs a bigger nudge
    than the default allows).
    """
    if not llm_predictions:
        return []

    # ---- Checks 1 + 2: per-block range clip and deviation limit ----
    hard_capacity_mw = getattr(config, "PLANT_MAX_FEED_IN_MW", capacity_mw)
    checked = [_clip_and_check_deviation(p, hard_capacity_mw, max_deviation_fraction) for p in llm_predictions]

    # ---- Check 3: smoothness across consecutive blocks ----
    for i in range(1, len(checked)):
        prev_mw = checked[i - 1]["validated_mw"]
        curr_mw = checked[i]["validated_mw"]
        change = curr_mw - prev_mw

        if abs(change) > MAX_STEP_CHANGE_MW:
            smoothed = prev_mw + MAX_STEP_CHANGE_MW * (1 if change > 0 else -1)
            smoothed = max(0.0, min(hard_capacity_mw, smoothed))
            note = (
                f"step change of {round(change, 3)} MW from previous block exceeded "
                f"max allowed ({MAX_STEP_CHANGE_MW:.3f} MW) -- smoothed to {round(smoothed, 3)}"
            )
            checked[i]["validated_mw"] = round(smoothed, 3)
            checked[i]["was_adjusted"] = True
            existing_note = checked[i]["adjustment_note"]
            checked[i]["adjustment_note"] = (
                note if existing_note == "no adjustment needed" else f"{existing_note}; {note}"
            )

    return checked


if __name__ == "__main__":
    # Quick manual test: a deliberately unrealistic LLM output to see
    # the validator pull it back in line.
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
