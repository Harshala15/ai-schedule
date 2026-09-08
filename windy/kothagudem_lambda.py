"""
Dedicated AWS Lambda handler for KOTHAGUDEM Windy capture.

This module keeps the shared capture pipeline intact, but forces the
site_id to KOTHAGUDEM so you can deploy a separate Lambda function for
this plant.
"""

from test_multi_image import lambda_handler as _shared_lambda_handler


def lambda_handler(event, context):
    event = dict(event or {})
    event["site_id"] = "KOTHAGUDEM"
    return _shared_lambda_handler(event, context)
