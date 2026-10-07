"""
deploy_weekahead_wind_lambda.py

Provisions and configures AWS Lambda 'intellis-weekhead-forcast-wind'
and sets up EventBridge trigger for statutory 7-day Week-Ahead wind scheduling:
  Morning Trigger: 04:30 AM IST (23:00 UTC previous day) -> cron(0 23 * * ? *)

Configuration:
  - Memory: 512 MB
  - Timeout: 900 seconds (15 minutes)
  - Role: arn:aws:iam::608744602858:role/global1-lambda-role
  - S3 Destination: s3://vedanjay-schedules-test-608744602858/intellis Weekhead wind/
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

FUNCTION_NAME = "intellis-weekhead-forcast-wind"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
REPO_NAME = "intellis-ai-scheduler"
TAG = "intellis-weekahead-wind-20261001-v1"
ECR_REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE_URI = f"{ECR_REGISTRY}/{REPO_NAME}:{TAG}"

CMD_OVERRIDE = ["intellis_weekahead_wind_lambda.lambda_handler"]
S3_BUCKET = "vedanjay-schedules-test-608744602858"
MORNING_RULE_NAME = "intellis-weekhead-wind-morning-0430"


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


def deploy_weekahead_wind_lambda_and_triggers(session):
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

    print(f"\n[Step 4/5] Deploying Week-Ahead Wind Lambda '{FUNCTION_NAME}' (512 MB, 900s timeout)...")
    function_arn = None
    try:
        existing = lam.get_function(FunctionName=FUNCTION_NAME)
        function_arn = existing["Configuration"]["FunctionArn"]
        print(f"      Function {FUNCTION_NAME} exists: {function_arn}")
        print(f"      Updating code image to {IMAGE_URI}...")
        lam.update_function_code(
            FunctionName=FUNCTION_NAME,
            ImageUri=IMAGE_URI,
        )
        print("      Waiting for code update to settle...")
        for _ in range(30):
            time.sleep(2)
            st = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"].get("LastUpdateStatus")
            if st == "Successful":
                break
        print("      Updating configuration (Memory: 512 MB, Timeout: 900s, Command: intellis_weekahead_wind_lambda.lambda_handler)...")
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Timeout=900,
            MemorySize=512,
            ImageConfig={"Command": CMD_OVERRIDE},
            Environment={"Variables": env_vars},
        )
    except lam.exceptions.ResourceNotFoundException:
        print(f"      Creating new function {FUNCTION_NAME}...")
        resp = lam.create_function(
            FunctionName=FUNCTION_NAME,
            PackageType="Image",
            Code={"ImageUri": IMAGE_URI},
            ImageConfig={"Command": CMD_OVERRIDE},
            Role=ROLE_ARN,
            Timeout=900,
            MemorySize=512,
            Environment={"Variables": env_vars},
            Description="Intellis 7-Day Week-Ahead Wind Forecast Scheduler (04:30 AM Morning Trigger, 672 Blocks, JEWLI, JGBPL)",
        )
        function_arn = resp["FunctionArn"]
        print(f"      Successfully created: {function_arn}")

    # Wait for function to become Active
    print("      Waiting for Week-Ahead Wind function update to finalize...")
    for _ in range(40):
        time.sleep(2)
        meta = lam.get_function(FunctionName=FUNCTION_NAME)
        state = meta.get("Configuration", {}).get("State", "")
        update_status = meta.get("Configuration", {}).get("LastUpdateStatus", "Successful")
        if state == "Active" and update_status == "Successful":
            print("      [OK] Week-Ahead Wind function is Active and ready!")
            break

    # -------------------------------------------------------------
    # EventBridge Rule: Morning Trigger (04:30 AM IST)
    # 04:30 IST is 23:00 UTC previous day -> cron(0 23 * * ? *)
    # -------------------------------------------------------------
    print(f"\n[Step 5/5] Configuring Week-Ahead Wind EventBridge Trigger...")
    print(f"      Configuring Morning Rule: {MORNING_RULE_NAME} (04:30 AM IST)...")
    cron_morning = "cron(0 23 * * ? *)"
    r1 = events.put_rule(
        Name=MORNING_RULE_NAME,
        ScheduleExpression=cron_morning,
        State="ENABLED",
        Description="Triggers 7-day Week-Ahead Wind forecast at 04:30 AM IST",
    )
    r1_arn = r1["RuleArn"]

    events.put_targets(
        Rule=MORNING_RULE_NAME,
        Targets=[{
            "Id": "TriggerWeekAheadWindMorning",
            "Arn": function_arn,
            "Input": json.dumps({"trigger": "scheduled_morning_0430"}),
        }]
    )

    try:
        lam.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=f"{MORNING_RULE_NAME}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=r1_arn,
        )
        print("      [OK] Morning Week-Ahead Wind rule permission added.")
    except lam.exceptions.ResourceConflictException:
        print("      [OK] Morning Week-Ahead Wind rule permission already exists.")

    print("\n" + "=" * 70)
    print("WEEK-AHEAD WIND PROVISIONING SUCCESSFULLY COMPLETED!")
    print(f"Function:    {FUNCTION_NAME} (512 MB, 900s)")
    print(f"ECR Image:   {IMAGE_URI}")
    print(f"Trigger:     04:30 AM IST -> {cron_morning}")
    print(f"S3 Output:   s3://{S3_BUCKET}/intellis Weekhead wind/{{site}}/{{start_date}}/")
    print("=" * 70)


def main():
    print("=" * 70)
    print("PROVISIONING 'intellis-weekhead-forcast-wind' ON AWS")
    print(f"Region: {REGION} | Account: {ACCOUNT_ID}")
    print(f"Memory: 512 MB | Timeout: 900s (15 min)")
    print("=" * 70)

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    ecr_login(session)
    build_and_push_image()
    deploy_weekahead_wind_lambda_and_triggers(session)


if __name__ == "__main__":
    main()
