from __future__ import annotations

from context_generator._shared import bootstrap_environment


bootstrap_environment("BHUPALPALLY")

from context_generator._runner import run_context_generation  # noqa: E402


def lambda_handler(event, context):
    return run_context_generation("BHUPALPALLY", event)
