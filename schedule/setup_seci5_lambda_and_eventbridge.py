import json
import boto3

REGION = "ap-south-1"
ACCOUNT_ID = "552496640388"
LAMBDA_NAME = "SECI5-ai-intellis-scheduler"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/vedanjay-test-lambda-execution-role"
IMAGE_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/intellis-ai-scheduler:intellis-wind325-night-rollover-20261022-v2"

lam = boto3.client("lambda", region_name=REGION)
events = boto3.client("events", region_name=REGION)

print(f"--- 1. Ensuring Lambda {LAMBDA_NAME} exists ---")
lambda_arn = None
try:
    f = lam.get_function(FunctionName=LAMBDA_NAME)
    lambda_arn = f["Configuration"]["FunctionArn"]
    print(f"Lambda already exists: {lambda_arn}")
except lam.exceptions.ResourceNotFoundException:
    print(f"Creating Lambda {LAMBDA_NAME}...")
    env_vars = {
        "SITE_ID": "SECI5",
        "PLANT_NAME": "SECI5",
        "PLANT_TYPE": "WIND",
        "PLANT_LAT": "23.08315869",
        "PLANT_LON": "69.20961708",
        "PLANT_CAPACITY_MW": "300.0",
        "PLANT_MAX_FEED_IN_MW": "300.0",
        "PLANT_DC_CAPACITY_MW": "302.4",
        "PLANT_TILT_DEG": "0.0",
        "PLANT_ORIENTATION_DEG_FROM_SOUTH": "0.0",
        "PLANT_PENALTY_REGULATION": "Gujarat",
        "S3_BUCKET": "vedanjay-schedules-test-552496640388",
        "BUCKET": "vedanjay-schedules-test-552496640388",
        "S3_RAW_OWNER": "vedanjay",
        "S3_OUTPUT_PREFIX": "generated/vedanjay_ai_intellis",
        "USE_GTI_API": "true",
        "USE_LLM_FOR_WIND": "true",
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", ""),
        "OPENMETEO_API_KEY": os.getenv("OPENMETEO_API_KEY", "jbThkFlLZSXZE3CU"),
        "INTELLIS_GTI_API_URL": "https://2qoics4vad.execute-api.ap-south-1.amazonaws.com/v1/intellis_gti_regime",
        "INTELLIS_IDEMPOTENCY_ENABLED": "false",
        "SIMOUR_STORAGE_ROOT": "/tmp/intellis_ai_scheduler",
    }
    resp = lam.create_function(
        FunctionName=LAMBDA_NAME,
        PackageType="Image",
        Code={"ImageUri": IMAGE_URI},
        Role=ROLE_ARN,
        Timeout=900,
        MemorySize=512,
        Environment={"Variables": env_vars},
        Description="SECI5 Gujarat 300MW Wind Intellis AI Scheduler",
    )
    lambda_arn = resp["FunctionArn"]
    print(f"Created Lambda: {lambda_arn}")

print(f"\n--- 2. Creating EventBridge Rules for {LAMBDA_NAME} ---")

rules = [
    {
        "name": f"{LAMBDA_NAME}-da-2215ist",
        "cron": "cron(45 16 * * ? *)",
        "desc": "Day-Ahead 10:15 PM IST (16:45 UTC) baseline schedule for tomorrow",
        "payload": {
            "site_id": "SECI5",
            "bucket": "vedanjay-schedules-test-552496640388",
            "output_prefix": "generated/vedanjay_ai_intellis",
            "target_date": "tomorrow",
        },
    },
    {
        "name": f"{LAMBDA_NAME}-cron-00",
        "cron": "cron(30 * * * ? *)",
        "desc": "Intraday revisions hourly on the hour (fires :30 UTC = :00 IST next hr)",
        "payload": {
            "site_id": "SECI5",
            "bucket": "vedanjay-schedules-test-552496640388",
            "output_prefix": "generated/vedanjay_ai_intellis",
        },
    },
    {
        "name": f"{LAMBDA_NAME}-cron-30",
        "cron": "cron(0 0-16,18-23 * * ? *)",
        "desc": "Intraday revisions hourly on the half hour (fires :00 UTC = :30 IST)",
        "payload": {
            "site_id": "SECI5",
            "bucket": "vedanjay-schedules-test-552496640388",
            "output_prefix": "generated/vedanjay_ai_intellis",
        },
    },
]

for r in rules:
    print(f"\nConfiguring rule: {r['name']}")
    r_resp = events.put_rule(
        Name=r["name"],
        ScheduleExpression=r["cron"],
        State="ENABLED",
        Description=r["desc"],
    )
    rule_arn = r_resp["RuleArn"]
    print(f"  Rule ARN: {rule_arn}")

    events.put_targets(
        Rule=r["name"],
        Targets=[
            {
                "Id": f"{r['name']}-target-1",
                "Arn": lambda_arn,
                "Input": json.dumps(r["payload"]),
            }
        ],
    )
    print(f"  Target attached with input payload.")

    sid = f"AllowEvents-{r['name']}"
    try:
        lam.add_permission(
            FunctionName=LAMBDA_NAME,
            StatementId=sid,
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        print(f"  Permission added: {sid}")
    except lam.exceptions.ResourceConflictException:
        print(f"  Permission {sid} already exists.")

print("\n--- SECI5 EventBridge setup successfully completed! ---")
