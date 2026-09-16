"""Deploy CHANDAWASA-ai-intellis-scheduler Lambda and EventBridge Rules in AWS.

Provisions:
1. Lambda: CHANDAWASA-ai-intellis-scheduler
   - Uses ECR image: intellis-ai-20260915-v5
   - Role: arn:aws:iam::608744602858:role/global1-lambda-role
   - Timeout: 900s, Memory: 512 MB
2. EventBridge Rules:
   - CHANDAWASA-ai-intellis-scheduler-cron-00: cron(30 0-15 * * ? *)
     Runs at minute 30 of UTC hours 0-15 -> 06:00, 07:00, ..., 21:00 IST (every hour on the hour).
   - CHANDAWASA-ai-intellis-scheduler-cron-30: cron(0 1-15 * * ? *)
     Runs at minute 00 of UTC hours 1-15 -> 06:30, 07:30, ..., 20:30 IST (every hour on the half-hour).
   Together: Every 30 minutes from 06:00 to 21:00 IST (31 slots).
"""

from __future__ import annotations

import json
import time
import boto3
from botocore.exceptions import ClientError

PROFILE = "intellis-608"
REGION = "ap-south-1"
ACCOUNT_ID = "608744602858"
FUNCTION_NAME = "CHANDAWASA-ai-intellis-scheduler"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
IMAGE_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/intellis-ai-scheduler:intellis-ai-20260915-v5"
S3_BUCKET = "vedanjay-schedules-test-608744602858"

ENV_VARS = {
    "SITE_ID": "CHANDAWASA",
    "PLANT_NAME": "CHANDAWASA",
    "PLANT_TYPE": "WIND",
    "S3_BUCKET": S3_BUCKET,
    "BUCKET": S3_BUCKET,
    "S3_RAW_OWNER": "vedanjay",
    "S3_OUTPUT_PREFIX": "generated/vedanjay_ai_intellis",
    "SIMOUR_STORAGE_ROOT": "/tmp/intellis_ai_scheduler",
    "OPENMETEO_API_KEY": os.getenv("OPENMETEO_API_KEY", ""),
    "LLM_PROVIDER": "openrouter",
    "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", ""),
}


def deploy():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    lambda_client = session.client("lambda")
    events_client = session.client("events")

    print(f"=== 1. Checking Lambda: {FUNCTION_NAME} ===")
    fn_arn = None
    try:
        cfg = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)
        fn_arn = cfg["FunctionArn"]
        print(f"  Lambda exists: {fn_arn}")
        print("  Updating code image...")
        lambda_client.update_function_code(
            FunctionName=FUNCTION_NAME,
            ImageUri=IMAGE_URI,
        )
        print("  Waiting for code update...")
        waiter = lambda_client.get_waiter("function_updated")
        waiter.wait(FunctionName=FUNCTION_NAME)
        print("  Updating environment configuration...")
        lambda_client.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Role=ROLE_ARN,
            Timeout=900,
            MemorySize=512,
            Environment={"Variables": ENV_VARS},
        )
        waiter.wait(FunctionName=FUNCTION_NAME)
        print("  [SUCCESS] Lambda updated successfully.")
    except ClientError as e:
        if e.response["Error"]["Code"] in {"ResourceNotFoundException", "404"}:
            print(f"  Lambda does not exist. Creating {FUNCTION_NAME}...")
            res = lambda_client.create_function(
                FunctionName=FUNCTION_NAME,
                PackageType="Image",
                Code={"ImageUri": IMAGE_URI},
                Role=ROLE_ARN,
                Timeout=900,
                MemorySize=512,
                Environment={"Variables": ENV_VARS},
                Description="Chandawasa 10 MW Wind Plant Intellis AI Schedule Generator",
            )
            fn_arn = res["FunctionArn"]
            print("  Waiting for function creation...")
            waiter = lambda_client.get_waiter("function_active")
            waiter.wait(FunctionName=FUNCTION_NAME)
            print("  [SUCCESS] Lambda created successfully.")
        else:
            raise

    if not fn_arn:
        fn_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{FUNCTION_NAME}"

    print("\n=== 2. Provisioning EventBridge 30-Minute Rules (06:00 to 21:00 IST) ===")
    rules = [
        {
            "name": f"{FUNCTION_NAME}-cron-00",
            "cron": "cron(30 0-15 * * ? *)",
            "desc": "CHANDAWASA wind schedule 30-min runs on the hour (06:00 to 21:00 IST)",
        },
        {
            "name": f"{FUNCTION_NAME}-cron-30",
            "cron": "cron(0 1-15 * * ? *)",
            "desc": "CHANDAWASA wind schedule 30-min runs on the half-hour (06:30 to 20:30 IST)",
        },
    ]

    target_input = json.dumps({
        "site_id": "CHANDAWASA",
        "bucket": S3_BUCKET,
        "output_prefix": "generated/vedanjay_ai_intellis",
    })

    for r in rules:
        rule_name = r["name"]
        cron_expr = r["cron"]
        rule_desc = r["desc"]

        print(f"\nSetting up rule: {rule_name}")
        print(f"  Schedule: {cron_expr} -> {rule_desc}")
        rule_res = events_client.put_rule(
            Name=rule_name,
            ScheduleExpression=cron_expr,
            State="ENABLED",
            Description=rule_desc,
        )
        rule_arn = rule_res["RuleArn"]

        events_client.put_targets(
            Rule=rule_name,
            Targets=[{
                "Id": f"{FUNCTION_NAME}-target",
                "Arn": fn_arn,
                "Input": target_input,
            }],
        )
        print(f"  Target attached to {fn_arn}")

        stmt_id = f"EventBridgeInvoke-{rule_name}"
        try:
            lambda_client.add_permission(
                FunctionName=FUNCTION_NAME,
                StatementId=stmt_id,
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceArn=rule_arn,
            )
            print(f"  [PERMISSION] Granted invoke permission for {rule_name}")
        except ClientError as pe:
            if pe.response["Error"]["Code"] == "ResourceConflictException":
                print(f"  [PERMISSION] Permission statement {stmt_id} already exists.")
            else:
                print(f"  [WARN] Permission exception: {pe}")

    print("\n============================================================")
    print("CHANDAWASA Intellis AI Deployment Complete!")
    print(f"Lambda: {FUNCTION_NAME}")
    print("EventBridge 30-min Schedule:")
    print("  1. 06:00 to 21:00 IST on the hour (cron(30 0-15 * * ? *))")
    print("  2. 06:30 to 20:30 IST on the half-hour (cron(0 1-15 * * ? *))")
    print("============================================================")


if __name__ == "__main__":
    deploy()
