"""
deploy_kothagudem_aws.py

End-to-end automated deployment script for KOTHAGUDEM in AWS:
1. ECR repositories setup
2. Docker images build & push (Fetcher, Windy Capture, Forecast Scheduler, Context Generator)
3. Lambda functions creation & configuration
4. EventBridge schedules and rules provisioning
"""

import boto3
import json
import subprocess
import time
from pathlib import Path

AWS_ACCOUNT_ID = "429694361053"
REGION = "ap-south-1"
ECR_BASE = f"{AWS_ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
S3_BUCKET = "ai-forecasting-storage-429694361053"

ecr = boto3.client("ecr", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
events = boto3.client("events", region_name=REGION)
scheduler = boto3.client("scheduler", region_name=REGION)

# Get reference credentials from existing Lambdas
bhpl_fetcher_cfg = lambda_client.get_function_configuration(FunctionName="bhupalpally-fetcher-image")
bhpl_fetcher_env = bhpl_fetcher_cfg.get("Environment", {}).get("Variables", {})

bhpl_scheduler_cfg = lambda_client.get_function_configuration(FunctionName="bhupalpally-forecast-scheduler")
bhpl_scheduler_env = bhpl_scheduler_cfg.get("Environment", {}).get("Variables", {})

def ensure_ecr_repo(repo_name):
    try:
        ecr.describe_repositories(repositoryNames=[repo_name])
        print(f"[ECR] Repository exists: {repo_name}")
    except ecr.exceptions.RepositoryNotFoundException:
        print(f"[ECR] Creating repository: {repo_name}")
        ecr.create_repository(repositoryName=repo_name)

def build_and_push(dockerfile, context_dir, repo_name, tag="latest"):
    image_uri = f"{ECR_BASE}/{repo_name}:{tag}"
    print(f"\n[DOCKER] Building {repo_name} from {dockerfile}...")
    subprocess.run(
        [
            "docker", "build",
            "--platform", "linux/amd64",
            "--provenance=false",
            "-f", str(dockerfile),
            "-t", f"{repo_name}:latest",
            str(context_dir),
        ],
        check=True
    )
    subprocess.run(["docker", "tag", f"{repo_name}:latest", image_uri], check=True)
    print(f"[DOCKER] Pushing {image_uri}...")
    subprocess.run(["docker", "push", image_uri], check=True)
    return image_uri

def create_or_update_lambda(fn_name, image_uri, role_arn, env_vars, memory=3008, timeout=900, image_config=None):
    try:
        lambda_client.get_function(FunctionName=fn_name)
        print(f"[LAMBDA] Updating code for existing function: {fn_name}")
        lambda_client.update_function_code(FunctionName=fn_name, ImageUri=image_uri)
        time.sleep(5)
        update_kwargs = {
            "FunctionName": fn_name,
            "Role": role_arn,
            "Timeout": timeout,
            "MemorySize": memory,
            "Environment": {"Variables": env_vars}
        }
        if image_config:
            update_kwargs["ImageConfig"] = image_config
        lambda_client.update_function_configuration(**update_kwargs)
        print(f"[LAMBDA] Updated configuration for: {fn_name}")
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"[LAMBDA] Creating new function: {fn_name}")
        create_kwargs = {
            "FunctionName": fn_name,
            "PackageType": "Image",
            "Code": {"ImageUri": image_uri},
            "Role": role_arn,
            "Timeout": timeout,
            "MemorySize": memory,
            "Environment": {"Variables": env_vars}
        }
        if image_config:
            create_kwargs["ImageConfig"] = image_config
        lambda_client.create_function(**create_kwargs)
        print(f"[LAMBDA] Created: {fn_name}")

print("=== 1. ENSURING ECR REPOSITORIES ===")
repos = [
    "kothagudem-fetcher",
    "kothagudem-windy-capture",
    "kothagudem-forecast-scheduler",
    "kothagudem-context-generator"
]
for r in repos:
    ensure_ecr_repo(r)

print("\n=== 2. BUILDING AND PUSHING DOCKER IMAGES ===")
fetcher_uri = build_and_push("schedule/kothagudem_fetcher/Dockerfile", "schedule", "kothagudem-fetcher")
windy_uri = build_and_push("windy/Dockerfile.windy-capture-lambda", "windy", "kothagudem-windy-capture")
scheduler_uri = build_and_push("schedule/kothagudem_forecast_scheduler/Dockerfile", "schedule", "kothagudem-forecast-scheduler")
context_uri = build_and_push("schedule/context_generator/Dockerfile", "schedule", "kothagudem-context-generator")

print("\n=== 3. DEPLOYING LAMBDA FUNCTIONS ===")
# 3.1 Fetcher Lambda
fetcher_env = {
    "S3_BUCKET": S3_BUCKET,
    "S3_PREFIX_BASE": "raw/vedanjay/KOTHAGUDEM",
    "SFTP_HOST": bhpl_fetcher_env.get("SFTP_HOST", "ftp.enercast.de"),
    "SFTP_PORT": bhpl_fetcher_env.get("SFTP_PORT", "21"),
    "SFTP_USERNAME": bhpl_fetcher_env.get("SFTP_USERNAME", "adani_mundra_solar"),
    "SFTP_PASSWORD": bhpl_fetcher_env["SFTP_PASSWORD"],
    "SFTP_REMOTE_DIR": "/incoming/powerdata_realtime/",
    "SFTP_FILENAME_PREFIX": "kothagudem_"
}
fetcher_role = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/kothagudem-fetcher-lambda-role"
create_or_update_lambda("kothagudem-fetcher", fetcher_uri, fetcher_role, fetcher_env, memory=512, timeout=300)

# 3.2 Windy Capture Lambda
windy_env = {
    "S3_BUCKET": S3_BUCKET,
    "SITE_ID": "KOTHAGUDEM",
    "PLANT_ID": "vedanjay"
}
windy_role = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/ai-site-windy-capture-lambda-role"
create_or_update_lambda(
    "KOTHAGUDEM-windy-capture",
    windy_uri,
    windy_role,
    windy_env,
    memory=3008,
    timeout=900,
    image_config={"Command": ["kothagudem_lambda.lambda_handler"]}
)

# 3.3 Forecast Scheduler Lambda
scheduler_env = dict(bhpl_scheduler_env)
scheduler_env.update({
    "PLANT_NAME": "KOTHAGUDEM",
    "PLANT_LAT": "17.52500925",
    "PLANT_LON": "80.64616743",
    "PLANT_CAPACITY_MW": "37.0",
    "S3_BUCKET": S3_BUCKET,
    "S3_CAPTURE_PREFIX": "raw/vedanjay/KOTHAGUDEM",
    "S3_METER_PREFIX": "raw/vedanjay/KOTHAGUDEM",
    "S3_SCHEDULE_PREFIX": "generated/KOTHAGUDEM",
    "S3_STATE_PREFIX": "state/vedanjay/KOTHAGUDEM",
    "SIMOUR_STORAGE_ROOT": "/tmp/kothagudem_scheduler",
    "TZ": "Asia/Kolkata",
    "ENABLE_S3_STATE_SYNC": "0"
})
scheduler_role = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/kothagudem-scheduler-lambda-role"
create_or_update_lambda("kothagudem-forecast-scheduler", scheduler_uri, scheduler_role, scheduler_env, memory=3008, timeout=900)

# 3.4 Context Generator Lambda
context_env = {
    "PLANT_NAME": "KOTHAGUDEM",
    "PLANT_PROFILE_PATH": "plant_profiles/KOTHAGUDEM.json",
    "S3_BUCKET": S3_BUCKET,
    "TZ": "Asia/Kolkata"
}
create_or_update_lambda(
    "kothagudem-context-generator",
    context_uri,
    scheduler_role,
    context_env,
    memory=2048,
    timeout=900,
    image_config={"Command": ["context_generator.kothagudem.lambda_handler.lambda_handler"]}
)

print("\n=== 4. PROVISIONING EVENTBRIDGE RULES & SCHEDULES ===")

# 4.1 Fetcher 15-Minute Rule
rule_name = "KOTHAGUDEM-fetcher-every-15min"
fetcher_arn = f"arn:aws:lambda:{REGION}:{AWS_ACCOUNT_ID}:function:kothagudem-fetcher"
events.put_rule(
    Name=rule_name,
    ScheduleExpression="cron(3,18,33,48 * * * ? *)",
    State="ENABLED",
    Description="Trigger Kothagudem FTP Fetcher every 15 minutes"
)
events.put_targets(
    Rule=rule_name,
    Targets=[{"Id": "1", "Arn": fetcher_arn, "Input": json.dumps({"site_id": "KOTHAGUDEM"})}]
)
try:
    lambda_client.add_permission(
        FunctionName="kothagudem-fetcher",
        StatementId="EventBridgeInvokePermission",
        Action="lambda:InvokeFunction",
        Principal="events.amazonaws.com",
        SourceArn=f"arn:aws:events:{REGION}:{AWS_ACCOUNT_ID}:rule/{rule_name}"
    )
except Exception:
    pass
print(f"[EVENTS] Provisioned {rule_name}")

# 4.2 Scheduler v2 Schedules
eb_scheduler_role = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/kothagudem-eventbridge-scheduler-role"
windy_eb_role = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/EventBridgeSchedulerInvokeWindyLambdaRole"
windy_arn = f"arn:aws:lambda:{REGION}:{AWS_ACCOUNT_ID}:function:KOTHAGUDEM-windy-capture"
sched_arn = f"arn:aws:lambda:{REGION}:{AWS_ACCOUNT_ID}:function:kothagudem-forecast-scheduler"
ctx_arn = f"arn:aws:lambda:{REGION}:{AWS_ACCOUNT_ID}:function:kothagudem-context-generator"

def put_v2_schedule(sched_name, cron_expr, target_arn, target_role, input_payload=None):
    target_obj = {
        "Arn": target_arn,
        "RoleArn": target_role,
        "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 3600}
    }
    if input_payload:
        target_obj["Input"] = json.dumps(input_payload)
    else:
        target_obj["Input"] = "{}"
    try:
        scheduler.get_schedule(Name=sched_name)
        scheduler.update_schedule(
            Name=sched_name,
            ScheduleExpression=cron_expr,
            ScheduleExpressionTimezone="Asia/Kolkata",
            State="ENABLED",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target=target_obj
        )
        print(f"[SCHEDULER v2] Updated: {sched_name}")
    except scheduler.exceptions.ResourceNotFoundException:
        scheduler.create_schedule(
            Name=sched_name,
            ScheduleExpression=cron_expr,
            ScheduleExpressionTimezone="Asia/Kolkata",
            State="ENABLED",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target=target_obj
        )
        print(f"[SCHEDULER v2] Created: {sched_name}")

# Windy Capture Schedules (5 mins before revision)
windy_times = [
    ("windy-capture-kothagudem-0550-ist", "cron(50 5 * * ? *)"),
    ("windy-capture-kothagudem-0640-ist", "cron(40 6 * * ? *)"),
    ("windy-capture-kothagudem-0810-ist", "cron(10 8 * * ? *)"),
    ("windy-capture-kothagudem-0940-ist", "cron(40 9 * * ? *)"),
    ("windy-capture-kothagudem-1110-ist", "cron(10 11 * * ? *)"),
    ("windy-capture-kothagudem-1410-ist", "cron(10 14 * * ? *)"),
    ("windy-capture-kothagudem-1540-ist", "cron(40 15 * * ? *)"),
]
for sname, scron in windy_times:
    put_v2_schedule(sname, scron, windy_arn, windy_eb_role)

# Forecast Revision Schedules
revision_times = [
    ("kothagudem-forecast-0600", "cron(0 6 * * ? *)", {"target_time": "06:00"}),
    ("kothagudem-forecast-0645", "cron(45 6 * * ? *)", {"target_time": "06:45"}),
    ("kothagudem-forecast-0815", "cron(15 8 * * ? *)", {"target_time": "08:15"}),
    ("kothagudem-forecast-0945", "cron(45 9 * * ? *)", {"target_time": "09:45"}),
    ("kothagudem-forecast-1115", "cron(15 11 * * ? *)", {"target_time": "11:15"}),
    ("kothagudem-forecast-1245", "cron(45 12 * * ? *)", {"target_time": "12:45"}),
    ("kothagudem-forecast-1415", "cron(15 14 * * ? *)", {"target_time": "14:15"}),
    ("kothagudem-forecast-1545", "cron(45 15 * * ? *)", {"target_time": "15:45"}),
]
for sname, scron, sinp in revision_times:
    put_v2_schedule(sname, scron, sched_arn, eb_scheduler_role, sinp)

# Context Generator Schedule (20:00 IST)
put_v2_schedule("kothagudem-context-generator-2000", "cron(0 20 * * ? *)", ctx_arn, eb_scheduler_role, {})

print("\nSUCCESS! ALL KOTHAGUDEM LAMBDAS, IMAGES, AND EVENTBRIDGE SCHEDULES DEPLOYED!")
