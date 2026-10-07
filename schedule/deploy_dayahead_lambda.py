"""
deploy_dayahead_lambda.py

Provisions and configures AWS Lambda 'intellis-dayhead-forcast-solar'
and sets up two EventBridge rules for statutory Day-Ahead scheduling:
  1. Morning Trigger: 04:30 AM IST (23:00 UTC previous day) -> {"run_type": "da0"}
  2. Night Trigger:   10:30 PM IST (17:00 UTC)              -> {"run_type": "da1"}

Configuration:
  - Memory: 512 MB
  - Timeout: 900 seconds (15 minutes)
  - Role: arn:aws:iam::608744602858:role/global1-lambda-role
  - S3 Destination: s3://vedanjay-schedules-test-608744602858/intellis Dayhead solar/
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
FUNCTION_NAME = "intellis-dayhead-forcast-solar"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
REPO_NAME = "intellis-ai-scheduler"
TAG = "intellis-dayahead-20261003-v3"
ECR_REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE_URI = f"{ECR_REGISTRY}/{REPO_NAME}:{TAG}"
CMD_OVERRIDE = ["intellis_dayahead_lambda.lambda_handler"]
S3_BUCKET = "vedanjay-schedules-test-608744602858"

MORNING_RULE_NAME = "intellis-dayhead-morning-0430"
AFTERNOON_RULE_NAME = "intellis-dayhead-afternoon-1330"
NIGHT_RULE_NAME = "intellis-dayhead-night-2230"


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


def deploy_dayahead_infrastructure():
    print("=" * 70)
    print("PROVISIONING 'intellis-dayhead-forcast-solar' ON AWS")
    print(f"Region: {REGION} | Account: {ACCOUNT_ID}")
    print(f"Memory: 512 MB | Timeout: 900s (15 min)")
    print("=" * 70)

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    ecr_login(session)
    build_and_push_image()

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

    # -------------------------------------------------------------
    # 1. Create or Update Lambda Function
    # -------------------------------------------------------------
    function_arn = None
    try:
        existing = lam.get_function(FunctionName=FUNCTION_NAME)
        function_arn = existing["Configuration"]["FunctionArn"]
        print(f"\n[1/3] Function {FUNCTION_NAME} already exists: {function_arn}")
        print("      Updating code ImageUri and ImageConfig...")
        lam.update_function_code(
            FunctionName=FUNCTION_NAME,
            ImageUri=IMAGE_URI,
        )
        print("      Waiting for function code update to complete...")
        for _ in range(40):
            time.sleep(3)
            status = lam.get_function(FunctionName=FUNCTION_NAME).get("Configuration", {}).get("LastUpdateStatus")
            if status == "Successful":
                print("      [OK] Code update completed successfully.")
                break
            elif status == "Failed":
                raise RuntimeError(f"Lambda code update failed: {status}")
        
        print("      Updating configuration (Memory: 512 MB, Timeout: 900s, Command: intellis_dayahead_lambda.lambda_handler)...")
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Timeout=900,
            MemorySize=512,
            ImageConfig={"Command": CMD_OVERRIDE},
            Environment={"Variables": env_vars},
        )

    except lam.exceptions.ResourceNotFoundException:
        print(f"\n[1/3] Creating function {FUNCTION_NAME}...")
        resp = lam.create_function(
            FunctionName=FUNCTION_NAME,
            PackageType="Image",
            Code={"ImageUri": IMAGE_URI},
            ImageConfig={"Command": CMD_OVERRIDE},
            Role=ROLE_ARN,
            Timeout=900,
            MemorySize=512,
            Environment={"Variables": env_vars},
            Description="Intellis Pure Physics & MOS Day-Ahead Scheduler for Solar & Wind Power Plants (04:30 AM & 10:30 PM triggers)",
        )
        function_arn = resp["FunctionArn"]
        print(f"      Successfully created: {function_arn}")

    # Wait for function to become Active
    print("      Waiting for function update to finalize...")
    for _ in range(40):
        time.sleep(2)
        meta = lam.get_function(FunctionName=FUNCTION_NAME)
        state = meta.get("Configuration", {}).get("State", "")
        update_status = meta.get("Configuration", {}).get("LastUpdateStatus", "Successful")
        if state == "Active" and update_status == "Successful":
            print("      [OK] Function is Active and ready!")
            break

    # -------------------------------------------------------------
    # 2. Setup EventBridge Rule 1: Morning Trigger (04:30 AM IST)
    # 04:30 IST is 23:00 UTC previous day -> cron(0 23 * * ? *)
    # -------------------------------------------------------------
    print(f"\n[2/3] Configuring Morning EventBridge Rule: {MORNING_RULE_NAME} (04:30 AM IST)...")
    cron_morning = "cron(0 23 * * ? *)"
    r1 = events.put_rule(
        Name=MORNING_RULE_NAME,
        ScheduleExpression=cron_morning,
        State="ENABLED",
        Description="Triggers Day-Ahead Morning forecast (da0) at 04:30 AM IST",
    )
    r1_arn = r1["RuleArn"]

    events.put_targets(
        Rule=MORNING_RULE_NAME,
        Targets=[{
            "Id": "TriggerDayAheadMorning",
            "Arn": function_arn,
            "Input": json.dumps({"run_type": "da0"}),
        }]
    )

    # Permission for EventBridge to invoke Lambda
    try:
        lam.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=f"{MORNING_RULE_NAME}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=r1_arn,
        )
        print("      [OK] Morning rule permission added.")
    except lam.exceptions.ResourceConflictException:
        print("      [OK] Morning rule permission already exists.")

    # -------------------------------------------------------------
    # 3. Setup EventBridge Rule 2: Afternoon Trigger (01:30 PM IST) for LGEPL
    # 13:30 IST is 08:00 UTC -> cron(0 8 * * ? *)
    # -------------------------------------------------------------
    print(f"\n[3/4] Configuring Afternoon EventBridge Rule: {AFTERNOON_RULE_NAME} (01:30 PM IST)...")
    cron_afternoon = "cron(0 8 * * ? *)"
    r_aft = events.put_rule(
        Name=AFTERNOON_RULE_NAME,
        ScheduleExpression=cron_afternoon,
        State="ENABLED",
        Description="Triggers Day-Ahead Afternoon forecast (da_afternoon) at 01:30 PM IST for LGEPL",
    )
    r_aft_arn = r_aft["RuleArn"]

    events.put_targets(
        Rule=AFTERNOON_RULE_NAME,
        Targets=[{
            "Id": "TriggerDayAheadAfternoon",
            "Arn": function_arn,
            "Input": json.dumps({"run_type": "da_afternoon", "plants": ["LGEPL"]}),
        }]
    )

    try:
        lam.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=f"{AFTERNOON_RULE_NAME}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=r_aft_arn,
        )
        print("      [OK] Afternoon rule permission added.")
    except lam.exceptions.ResourceConflictException:
        print("      [OK] Afternoon rule permission already exists.")

    # -------------------------------------------------------------
    # 4. Setup EventBridge Rule 3: Night Trigger (10:30 PM IST)
    # 22:30 IST is 17:00 UTC -> cron(0 17 * * ? *)
    # -------------------------------------------------------------
    print(f"\n[4/4] Configuring Night EventBridge Rule: {NIGHT_RULE_NAME} (10:30 PM IST)...")
    cron_night = "cron(0 17 * * ? *)"
    r2 = events.put_rule(
        Name=NIGHT_RULE_NAME,
        ScheduleExpression=cron_night,
        State="ENABLED",
        Description="Triggers Day-Ahead Night forecast (da1) at 10:30 PM IST",
    )
    r2_arn = r2["RuleArn"]

    events.put_targets(
        Rule=NIGHT_RULE_NAME,
        Targets=[{
            "Id": "TriggerDayAheadNight",
            "Arn": function_arn,
            "Input": json.dumps({"run_type": "da1"}),
        }]
    )

    try:
        lam.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=f"{NIGHT_RULE_NAME}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=r2_arn,
        )
        print("      [OK] Night rule permission added.")
    except lam.exceptions.ResourceConflictException:
        print("      [OK] Night rule permission already exists.")

    print("\n" + "=" * 70)
    print("DAY-AHEAD PROVISIONING SUCCESSFULLY COMPLETED!")
    print(f"Function:       {FUNCTION_NAME} (512 MB, 900s)")
    print(f"Morning (da0):   04:30 AM IST -> {cron_morning}")
    print(f"Afternoon (da1): 01:30 PM IST -> {cron_afternoon}")
    print(f"Night (da2):     10:30 PM IST -> {cron_night}")
    print(f"S3 Output:      s3://{S3_BUCKET}/intellis Dayhead solar/{{site}}/{{target_date}}/")
    print("=" * 70)


if __name__ == "__main__":
    deploy_dayahead_infrastructure()

