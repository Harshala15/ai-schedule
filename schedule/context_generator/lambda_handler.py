from __future__ import annotations

from context_generator._runner import resolve_plant_name, run_context_generation


def lambda_handler(event, context):
    plant = resolve_plant_name(event)
    return run_context_generation(plant, event)

