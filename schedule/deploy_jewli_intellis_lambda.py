"""Deploy JEWLI-ai-intellis-scheduler Lambda and EventBridge Rules in AWS.

Provisions:
1. Lambda: JEWLI-ai-intellis-scheduler
   - Uses ECR image: intellis-ai-scheduler:intellis-jewli-wind-no-llm-20260918
   - Role: arn:aws:iam::608744602858:role/global1-lambda-role
   - Timeout: 900s, Memory: 512 MB
   - Handlers: jewli_lambda.lambda_handler / intellis_ai_lambda.lambda_handler
   - LLM Disabled: USE_LLM_FOR_WIND=false, USE_LLM_JEWLI=false, ENABLE_JEWLI_LLM=false
2. EventBridge Rules:
   - JEWLI-ai-intellis-scheduler-cron-00: cron(30 0-15 * * ? *)
     Runs at minute 30 of UTC hours 0-15 -> 06:00, 07:00, ..., 21:00 IST (every hour on the hour).
   - JEWLI-ai-intellis-scheduler-cron-30: cron(0 1-15 * * ? *)
     Runs at minute 00 of UTC hours 1-15 -> 06:30, 07:30, ..., 20:30 IST (every hour on the half-hour).
   Together: Every 30 minutes from 06:00 to 21:00 IST (31 slots).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import boto3
from botocore.exceptions import ClientError

PROFILE = "intellis-608"
REGION = "ap-south-1"
ACCOUNT_ID = "608744602858"
FUNCTION_NAME = "JEWLI-ai-intellis-scheduler"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
REPO_NAME = "intellis-ai-scheduler"
TAG = "intellis-jewli-wind-no-llm-20260919"
IMAGE_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}:{TAG}"
S3_BUCKET = "vedanjay-schedules-test-608744602858"

ENV_VARS = {
    "SITE_ID": "JEWLI",
    "PLANT_NAME": "JEWLI",
    "PLANT_TYPE": "WIND",
    "S3_BUCKET": S3_BUCKET,
    "BUCKET": S3_BUCKET,
    "S3_RAW_OWNER": "vedanjay",
    "S3_OUTPUT_PREFIX": "generated/vedanjay_ai_intellis",
    "SIMOUR_STORAGE_ROOT": "/tmp/intellis_ai_scheduler",
    "OPENMETEO_API_KEY": "jbThkFlLZSXZE3CU",
    "INTELLIS_IDEMPOTENCY_ENABLED": "false",
    "USE_LLM_FOR_WIND": "false",
    "USE_LLM_JEWLI": "false",
    "ENABLE_JEWLI_LLM": "false",
}


def build_and_push_image(session: boto3.Session) -> str:
    print(f"\n=== 1. Building and Pushing Docker Image: {IMAGE_URI} ===")
    ecr_client = session.client("ecr")
    
    # Authenticate Docker to ECR
    token_resp = ecr_client.get_authorization_token()
    auth_data = token_resp["authorizationData"][0]
    token = base64.b64decode(auth_data["authorizationToken"]).decode("utf-8")
    username, password = token.split(":")
    endpoint = auth_data["proxyEndpoint"]
    
    print(f"  Logging into ECR: {endpoint}...")
    login_proc = subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", endpoint],
        input=password.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if login_proc.returncode != 0:
        print(f"  [ERROR] Docker login failed: {login_proc.stderr.decode()}")
        sys.exit(1)
    print("  [OK] Docker logged in successfully.")

    # Build image
    print("  Building Docker image (platform linux/amd64)...")
    build_cmd = [
        "docker", "build",
        "--platform", "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "-f", "Dockerfile.intellis-ai-lambda",
        "-t", f"{REPO_NAME}:{TAG}",
        ".",
    ]
    build_proc = subprocess.run(build_cmd, capture_output=False, check=False)
    if build_proc.returncode != 0:
        print(f"  [ERROR] Docker build failed with code {build_proc.returncode}")
        sys.exit(1)
    print("  [OK] Docker image built.")

    # Tag and push
    print(f"  Tagging and pushing to {IMAGE_URI}...")
    subprocess.run(["docker", "tag", f"{REPO_NAME}:{TAG}", IMAGE_URI], check=True)
    push_proc = subprocess.run(["docker", "push", IMAGE_URI], capture_output=False, check=False)
    if push_proc.returncode != 0:
        print(f"  [ERROR] Docker push failed with code {push_proc.returncode}")
        sys.exit(1)
    print(f"  [OK] Pushed image successfully: {IMAGE_URI}")
    return IMAGE_URI


def deploy_lambda_and_rules(image_uri: str):
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    lambda_client = session.client("lambda")
    events_client = session.client("events")

    print(f"\n=== 2. Provisioning / Updating Lambda: {FUNCTION_NAME} ===")
    fn_arn = None
    try:
        cfg = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)
        fn_arn = cfg["FunctionArn"]
        print(f"  Lambda exists: {fn_arn}")
        print("  Updating code image...")
        lambda_client.update_function_code(
            FunctionName=FUNCTION_NAME,
            ImageUri=image_uri,
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
            ImageConfig={"Command": ["jewli_lambda.lambda_handler"]},
        )
        waiter.wait(FunctionName=FUNCTION_NAME)
        print(f"  [SUCCESS] {FUNCTION_NAME} updated successfully.")
    except ClientError as e:
        if e.response["Error"]["Code"] in {"ResourceNotFoundException", "404"}:
            print(f"  Lambda does not exist. Creating {FUNCTION_NAME}...")
            res = lambda_client.create_function(
                FunctionName=FUNCTION_NAME,
                PackageType="Image",
                Code={"ImageUri": image_uri},
                Role=ROLE_ARN,
                Timeout=900,
                MemorySize=512,
                Environment={"Variables": ENV_VARS},
                ImageConfig={"Command": ["jewli_lambda.lambda_handler"]},
                Description="Jewli 100.8 MW Wind Power Plant Intellis AI Schedule Generator (MERC 10% Band, Non-LLM Physical Calibrated Ensemble)",
            )
            fn_arn = res["FunctionArn"]
            print("  Waiting for function active...")
            waiter = lambda_client.get_waiter("function_active")
            waiter.wait(FunctionName=FUNCTION_NAME)
            print(f"  [SUCCESS] {FUNCTION_NAME} created successfully.")
        else:
            raise

    if not fn_arn:
        fn_arn = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{FUNCTION_NAME}"

    print("\n=== 3. Provisioning EventBridge 6-Block Revision Rules (45-min effective lead time) ===")
    rules = [
        {
            "name": f"{FUNCTION_NAME}-6block-odd",
            "cron": "cron(45 1,4,7,10,13,16,19,22 * * ? *)",
            "desc": "JEWLI wind 6-block odd revisions: Blocks 6, 18, 30, 42, 54, 66, 78, 90 at 01:15, 04:15, 07:15, 10:15, 13:15, 16:15, 19:15, 22:15 IST",
        },
        {
            "name": f"{FUNCTION_NAME}-6block-even",
            "cron": "cron(15 0,3,6,9,12,15,18,21 * * ? *)",
            "desc": "JEWLI wind 6-block even revisions: Blocks 12, 24, 36, 48, 60, 72, 84, 96 at 02:45, 05:45, 08:45, 11:45, 14:45, 17:45, 20:45, 23:45 IST",
        },
    ]

    target_input = json.dumps({
        "site_id": "JEWLI",
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
    print("JEWLI Intellis AI Wind Scheduler Deployment Complete!")
    print(f"Lambda: {FUNCTION_NAME}")
    print(f"ARN: {fn_arn}")
    print(f"Image: {image_uri}")
    print("Non-LLM Enforced: USE_LLM_FOR_WIND=false, USE_LLM_JEWLI=false, ENABLE_JEWLI_LLM=false")
    print("EventBridge 30-min Schedule:")
    print("  1. 06:00 to 21:00 IST on the hour (cron(30 0-15 * * ? *))")
    print("  2. 06:30 to 20:30 IST on the half-hour (cron(0 1-15 * * ? *))")
    print("============================================================")


def main():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    image_uri = build_and_push_image(session)
    deploy_lambda_and_rules(image_uri)


if __name__ == "__main__":
    main()
