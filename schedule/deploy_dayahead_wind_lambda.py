"""
deploy_dayahead_wind_lambda.py

Provisions and configures dedicated AWS Lambda 'intellis-dayhead-forcast-wind'
and sets up two EventBridge rules for statutory Day-Ahead wind scheduling:
  1. Morning Trigger: 04:30 AM IST (23:00 UTC previous day) -> {"run_type": "da0"} (CHANDAWASA, JEWLI, JGBPL)
  2. Night Trigger:   10:30 PM IST (17:00 UTC)              -> {"run_type": "da1"} (JEWLI, JGBPL)

Configuration:
  - Memory: 512 MB
  - Timeout: 900 seconds (15 minutes)
  - Role: arn:aws:iam::608744602858:role/global1-lambda-role
  - S3 Destination: s3://vedanjay-schedules-test-608744602858/intellis Dayhead wind/
"""

import os
import sys
import json
import time
import base64
import subprocess
import boto3

REGION = "ap-south-1"
PROFILE = "intellis-608"
ACCOUNT_ID = "608744602858"

SOLAR_FUNCTION_NAME = "intellis-dayhead-forcast-solar"
WIND_FUNCTION_NAME = "intellis-dayhead-forcast-wind"

ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
REPO_NAME = "intellis-ai-scheduler"
TAG = "intellis-dayahead-20261001-v2"
ECR_REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE_URI = f"{ECR_REGISTRY}/{REPO_NAME}:{TAG}"

WIND_CMD_OVERRIDE = ["intellis_dayahead_wind_lambda.lambda_handler"]
SOLAR_CMD_OVERRIDE = ["intellis_dayahead_lambda.lambda_handler"]
S3_BUCKET = "vedanjay-schedules-test-608744602858"

WIND_MORNING_RULE_NAME = "intellis-dayhead-wind-morning-0430"
WIND_NIGHT_RULE_NAME = "intellis-dayhead-wind-night-2230"


def ecr_login(session):
    print("\n[Step 1/5] Logging into Amazon ECR...")
    ecr_client = session.client("ecr")
    auth_data = ecr_client.get_authorization_token()["authorizationData"][0]
    token = auth_data["authorizationToken"]
    user, pwd = base64.b64decode(token).decode().split(":")

    login_cmd = ["docker", "login", "--username", user, "--password-stdin", ECR_REGISTRY]
    proc = subprocess.run(login_cmd, input=pwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ECR Login Failed: {proc.stderr}")
    print(f"      [OK] ECR Login Succeeded: {proc.stdout.strip()}")


def build_and_push_image():
    print(f"\n[Step 2/5] Building Docker Image ({IMAGE_URI})...")
    cwd = os.path.dirname(os.path.abspath(__file__))
    build_cmd = [
        "docker", "buildx", "build",
        "--platform", "linux/amd64",
        "--provenance=false",
        "--load",
        "-f", "Dockerfile.intellis-ai-lambda",
        "-t", IMAGE_URI,
        "."
    ]
    proc = subprocess.run(build_cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Docker Build Failed:\n{proc.stderr}\n{proc.stdout}")
    print("      [OK] Docker Build Succeeded!")

    print(f"\n[Step 3/5] Pushing Docker Image to ECR ({IMAGE_URI})...")
    push_cmd = ["docker", "push", IMAGE_URI]
    proc = subprocess.run(push_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Docker Push Failed:\n{proc.stderr}\n{proc.stdout}")
    print("      [OK] Docker Push Succeeded!")


def deploy_wind_lambda_and_triggers(session):
    lam = session.client("lambda")
    events = session.client("events")

    env_vars = {
        "SCHEDULE_BUCKET": S3_BUCKET,
        "BUCKET": S3_BUCKET,
        "S3_BUCKET": S3_BUCKET,
        "TZ": "Asia/Kolkata",
        "INTELLIS_IDEMPOTENCY_ENABLED": "true",
        "OPENMETEO_API_KEY": "jbThkFlLZSXZE3CU",
    }

    print(f"\n[Step 4/5] Deploying Wind Lambda '{WIND_FUNCTION_NAME}' (512 MB, 900s timeout)...")
    function_arn = None
    try:
        existing = lam.get_function(FunctionName=WIND_FUNCTION_NAME)
        function_arn = existing["Configuration"]["FunctionArn"]
        print(f"      Function {WIND_FUNCTION_NAME} exists: {function_arn}")
        print(f"      Updating code image to {IMAGE_URI}...")
        lam.update_function_code(
            FunctionName=WIND_FUNCTION_NAME,
            ImageUri=IMAGE_URI,
        )
        time.sleep(3)
        print("      Updating configuration (Memory: 512 MB, Timeout: 900s, Command: intellis_dayahead_wind_lambda.lambda_handler)...")
        lam.update_function_configuration(
            FunctionName=WIND_FUNCTION_NAME,
            Timeout=900,
            MemorySize=512,
            ImageConfig={"Command": WIND_CMD_OVERRIDE},
            Environment={"Variables": env_vars},
        )
    except lam.exceptions.ResourceNotFoundException:
        print(f"      Creating new function {WIND_FUNCTION_NAME}...")
        resp = lam.create_function(
            FunctionName=WIND_FUNCTION_NAME,
            PackageType="Image",
            Code={"ImageUri": IMAGE_URI},
            ImageConfig={"Command": WIND_CMD_OVERRIDE},
            Role=ROLE_ARN,
            Timeout=900,
            MemorySize=512,
            Environment={"Variables": env_vars},
            Description="Intellis Pure Aerodynamics & Hub-Height NWP Day-Ahead Scheduler for Wind Power Plants (CHANDAWASA, JEWLI, JGBPL)",
        )
        function_arn = resp["FunctionArn"]
        print(f"      Successfully created: {function_arn}")

    # Wait for function to become Active
    print("      Waiting for Wind function update to finalize...")
    for _ in range(40):
        time.sleep(2)
        meta = lam.get_function(FunctionName=WIND_FUNCTION_NAME)
        state = meta.get("Configuration", {}).get("State", "")
        update_status = meta.get("Configuration", {}).get("LastUpdateStatus", "Successful")
        if state == "Active" and update_status == "Successful":
            print("      [OK] Wind function is Active and ready!")
            break

    # Also update Solar Lambda with latest image so it uses clean solar code
    try:
        print(f"      Syncing Solar Lambda '{SOLAR_FUNCTION_NAME}' to latest image {IMAGE_URI}...")
        lam.update_function_code(FunctionName=SOLAR_FUNCTION_NAME, ImageUri=IMAGE_URI)
    except Exception as e:
        print(f"      [WARN] Could not update solar lambda image: {e}")

    # -------------------------------------------------------------
    # EventBridge Rule 1: Morning Trigger (04:30 AM IST)
    # 04:30 IST is 23:00 UTC previous day -> cron(0 23 * * ? *)
    # -------------------------------------------------------------
    print(f"\n[Step 5/5] Configuring Wind EventBridge Triggers...")
    print(f"      Configuring Morning Wind Rule: {WIND_MORNING_RULE_NAME} (04:30 AM IST)...")
    cron_morning = "cron(0 23 * * ? *)"
    r1 = events.put_rule(
        Name=WIND_MORNING_RULE_NAME,
        ScheduleExpression=cron_morning,
        State="ENABLED",
        Description="Triggers Day-Ahead Wind Morning forecast (da0) at 04:30 AM IST",
    )
    r1_arn = r1["RuleArn"]

    events.put_targets(
        Rule=WIND_MORNING_RULE_NAME,
        Targets=[{
            "Id": "TriggerDayAheadWindMorning",
            "Arn": function_arn,
            "Input": json.dumps({"run_type": "da0"}),
        }]
    )

    try:
        lam.add_permission(
            FunctionName=WIND_FUNCTION_NAME,
            StatementId=f"{WIND_MORNING_RULE_NAME}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=r1_arn,
        )
        print("      [OK] Morning wind rule permission added.")
    except lam.exceptions.ResourceConflictException:
        print("      [OK] Morning wind rule permission already exists.")

    # -------------------------------------------------------------
    # EventBridge Rule 2: Night Trigger (10:30 PM IST)
    # 22:30 IST is 17:00 UTC -> cron(0 17 * * ? *)
    # -------------------------------------------------------------
    print(f"      Configuring Night Wind Rule: {WIND_NIGHT_RULE_NAME} (10:30 PM IST)...")
    cron_night = "cron(0 17 * * ? *)"
    r2 = events.put_rule(
        Name=WIND_NIGHT_RULE_NAME,
        ScheduleExpression=cron_night,
        State="ENABLED",
        Description="Triggers Day-Ahead Wind Night forecast (da1) at 10:30 PM IST",
    )
    r2_arn = r2["RuleArn"]

    events.put_targets(
        Rule=WIND_NIGHT_RULE_NAME,
        Targets=[{
            "Id": "TriggerDayAheadWindNight",
            "Arn": function_arn,
            "Input": json.dumps({"run_type": "da1"}),
        }]
    )

    try:
        lam.add_permission(
            FunctionName=WIND_FUNCTION_NAME,
            StatementId=f"{WIND_NIGHT_RULE_NAME}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=r2_arn,
        )
        print("      [OK] Night wind rule permission added.")
    except lam.exceptions.ResourceConflictException:
        print("      [OK] Night wind rule permission already exists.")

    print("\n" + "=" * 70)
    print("WIND DAY-AHEAD PROVISIONING SUCCESSFULLY COMPLETED!")
    print(f"Function:    {WIND_FUNCTION_NAME} (512 MB, 900s)")
    print(f"ECR Image:   {IMAGE_URI}")
    print(f"Morning (da0): 04:30 AM IST -> {cron_morning}")
    print(f"Night (da1):   10:30 PM IST -> {cron_night}")
    print(f"S3 Output:   s3://{S3_BUCKET}/intellis Dayhead wind/{{site}}/{{target_date}}/")
    print("=" * 70)


def main():
    print("=" * 70)
    print("PROVISIONING 'intellis-dayhead-forcast-wind' ON AWS")
    print(f"Region: {REGION} | Account: {ACCOUNT_ID}")
    print(f"Memory: 512 MB | Timeout: 900s (15 min)")
    print("=" * 70)

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    ecr_login(session)
    build_and_push_image()
    deploy_wind_lambda_and_triggers(session)


if __name__ == "__main__":
    main()
