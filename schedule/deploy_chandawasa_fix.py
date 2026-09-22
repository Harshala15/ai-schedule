"""Build, push, and deploy CHANDAWASA-ai-intellis-scheduler Lambda with Enercast alignment fixes."""

import base64
import json
import os
import subprocess
import sys
import boto3
from botocore.exceptions import ClientError

PROFILE = "intellis-608"
REGION = "ap-south-1"
ACCOUNT_ID = "608744602858"
FUNCTION_NAME = "CHANDAWASA-ai-intellis-scheduler"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
REPO_NAME = "intellis-ai-scheduler"
TAG = "intellis-chandawasa-enercast-fix-20260922"
IMAGE_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}:{TAG}"
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
    "OPENMETEO_API_KEY": "jbThkFlLZSXZE3CU",
    "INTELLIS_IDEMPOTENCY_ENABLED": "false",
    "USE_LLM_FOR_WIND": "false",
}

def main():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    ecr_client = session.client("ecr")
    lambda_client = session.client("lambda")

    print(f"=== 1. Docker Login to ECR ===")
    token_resp = ecr_client.get_authorization_token()
    auth_data = token_resp["authorizationData"][0]
    token = base64.b64decode(auth_data["authorizationToken"]).decode("utf-8")
    username, password = token.split(":")
    endpoint = auth_data["proxyEndpoint"]

    subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", endpoint],
        input=password.encode("utf-8"),
        check=True,
    )

    print(f"=== 2. Building Docker Image: {IMAGE_URI} ===")
    subprocess.run(
        [
            "docker", "build",
            "--platform", "linux/amd64",
            "--provenance=false",
            "--sbom=false",
            "-f", "Dockerfile.intellis-ai-lambda",
            "-t", f"{REPO_NAME}:{TAG}",
            ".",
        ],
        check=True,
    )

    print("=== 3. Tagging and Pushing Image ===")
    subprocess.run(["docker", "tag", f"{REPO_NAME}:{TAG}", IMAGE_URI], check=True)
    subprocess.run(["docker", "push", IMAGE_URI], check=True)

    print(f"=== 4. Updating Lambda: {FUNCTION_NAME} ===")
    lambda_client.update_function_code(
        FunctionName=FUNCTION_NAME,
        ImageUri=IMAGE_URI,
    )
    waiter = lambda_client.get_waiter("function_updated")
    print("Waiting for Lambda code update...")
    waiter.wait(FunctionName=FUNCTION_NAME)

    print("Updating Lambda environment configuration...")
    lambda_client.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        Role=ROLE_ARN,
        Timeout=900,
        MemorySize=512,
        Environment={"Variables": ENV_VARS},
    )
    waiter.wait(FunctionName=FUNCTION_NAME)
    print(f"[SUCCESS] {FUNCTION_NAME} deployed successfully with image {IMAGE_URI}!")

if __name__ == "__main__":
    main()
