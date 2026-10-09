"""Automated AWS Lambda & HTTP API Gateway Deployment for Intellis GTI Commercial API."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
import requests

REGION = "ap-south-1"
PROFILE = "intellis-608"
ACCOUNT_ID = "608744602858"
FUNCTION_NAME = "intellis-gti-public-api"
API_NAME = "intellis-gti-api"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/global1-lambda-role"
REPO_NAME = "intellis-ai-scheduler"
TAG = "gti-api-v1"
ECR_REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE_URI = f"{ECR_REGISTRY}/{REPO_NAME}:{TAG}"

SCHEDULE_DIR = Path(__file__).resolve().parents[5]
DOCKERFILE_PATH = Path(__file__).resolve().parent / "Dockerfile"


def main():
    print("=" * 70)
    print("DEPLOYING INTELLIS GTI COMMERCIAL API TO AWS")
    print(f"Region:        {REGION}")
    print(f"Function Name: {FUNCTION_NAME}")
    print(f"Image URI:     {IMAGE_URI}")
    print("=" * 70)

    session = boto3.Session(profile_name=PROFILE)
    ecr = session.client("ecr", region_name=REGION)
    lam = session.client("lambda", region_name=REGION)
    apigw = session.client("apigatewayv2", region_name=REGION)

    # 1. ECR Login
    print("\n[Step 1/5] Logging into Amazon ECR...")
    auth_data = ecr.get_authorization_token()["authorizationData"][0]
    token = auth_data["authorizationToken"]
    user, pwd = base64.b64decode(token).decode().split(":")
    login_proc = subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", ECR_REGISTRY],
        input=pwd,
        capture_output=True,
        text=True,
    )
    if login_proc.returncode != 0:
        raise RuntimeError(f"ECR login failed: {login_proc.stderr}")
    print("  [OK] Successfully authenticated with ECR.")

    # 2. Build Docker Image
    print("\n[Step 2/5] Building Docker image for Linux/amd64 Lambda...")
    build_cmd = [
        "docker", "build",
        "--platform", "linux/amd64",
        "--provenance=false",
        "-f", str(DOCKERFILE_PATH),
        "-t", IMAGE_URI,
        ".",
    ]
    build_proc = subprocess.run(build_cmd, cwd=str(SCHEDULE_DIR), capture_output=False)
    if build_proc.returncode != 0:
        raise RuntimeError("Docker build failed.")
    print("  [OK] Docker image built successfully.")

    # 3. Push Docker Image to ECR
    print("\n[Step 3/5] Pushing Docker image to ECR...")
    push_proc = subprocess.run(["docker", "push", IMAGE_URI], capture_output=False)
    if push_proc.returncode != 0:
        raise RuntimeError("Docker push failed.")
    print("  [OK] Docker image pushed successfully to ECR.")

    # 4. Create or Update Lambda Function
    print("\n[Step 4/5] Deploying Lambda Function...")
    env_vars = {
        "OPENMETEO_API_KEY": "jbThkFlLZSXZE3CU",  # Open-Meteo Customer Premium Commercial License
        "SIMOUR_STORAGE_ROOT": "/tmp/intellis_ai_scheduler",
    }

    try:
        fn_meta = lam.get_function(FunctionName=FUNCTION_NAME)
        function_arn = fn_meta["Configuration"]["FunctionArn"]
        print(f"  Function {FUNCTION_NAME} exists. Updating code to new image...")
        lam.update_function_code(FunctionName=FUNCTION_NAME, ImageUri=IMAGE_URI)

        # Wait for update to complete
        for _ in range(60):
            time.sleep(3)
            status = lam.get_function(FunctionName=FUNCTION_NAME).get("Configuration", {}).get("LastUpdateStatus")
            if status == "Successful":
                print("  [OK] Function code update successful.")
                break
            elif status == "Failed":
                raise RuntimeError("Lambda code update failed.")

        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Timeout=60,
            MemorySize=2048,
            Environment={"Variables": env_vars},
        )
    except lam.exceptions.ResourceNotFoundException:
        print(f"  Function {FUNCTION_NAME} not found. Creating new Lambda function...")
        resp = lam.create_function(
            FunctionName=FUNCTION_NAME,
            PackageType="Image",
            Code={"ImageUri": IMAGE_URI},
            Role=ROLE_ARN,
            Timeout=60,
            MemorySize=2048,
            Environment={"Variables": env_vars},
            Description="Public REST API for Intellis GTI Solar Forecasting",
        )
        function_arn = resp["FunctionArn"]
        print(f"  [OK] Created Lambda: {function_arn}")

    # Wait for function to become Active
    for _ in range(40):
        time.sleep(2)
        state = lam.get_function(FunctionName=FUNCTION_NAME).get("Configuration", {}).get("State")
        if state == "Active":
            print(f"  [OK] Function state: {state}")
            break

    # 5. Create or Get HTTP API Gateway
    print("\n[Step 5/5] Configuring AWS HTTP API Gateway...")
    api_endpoint = None
    apis = apigw.get_apis().get("Items", [])
    target_api = next((a for a in apis if a["Name"] == API_NAME), None)

    if target_api:
        api_id = target_api["ApiId"]
        api_endpoint = target_api["ApiEndpoint"]
        print(f"  Using existing HTTP API Gateway (ID: {api_id}): {api_endpoint}")
    else:
        print(f"  Creating new HTTP API Gateway: {API_NAME}...")
        api_resp = apigw.create_api(
            Name=API_NAME,
            ProtocolType="HTTP",
            Target=function_arn,
            Description="Public HTTP API Gateway for Intellis GTI Commercial Forecasting",
        )
        api_id = api_resp["ApiId"]
        api_endpoint = api_resp["ApiEndpoint"]
        print(f"  [OK] Created HTTP API Gateway: {api_endpoint}")

        # Add Lambda permission for API Gateway invoke
        try:
            lam.add_permission(
                FunctionName=FUNCTION_NAME,
                StatementId="apigateway-public-access",
                Action="lambda:InvokeFunction",
                Principal="apigateway.amazonaws.com",
                SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{api_id}/*/*",
            )
            print("  [OK] Added permission for API Gateway to invoke Lambda.")
        except lam.exceptions.ResourceConflictException:
            pass

    print("\n" + "=" * 70)
    print("DEPLOYMENT COMPLETE! TESTING LIVE PUBLIC ENDPOINT...")
    print(f"Base Public URL: {api_endpoint}")
    print(f"Swagger Docs:    {api_endpoint}/docs")
    print(f"Health Probe:    {api_endpoint}/v1/health")
    print(f"GTI Regime:     {api_endpoint}/v1/intellis_gti_regime?plant=GSNP&date=2026-10-08")
    print(f"GTI Alias:      {api_endpoint}/v1/gti?plant=GSNP&date=2026-10-08")
    print("=" * 70)

    # Smoke Test Live Endpoint
    time.sleep(3)
    try:
        print("\nInvoking Live Public Health Check...")
        h_resp = requests.get(f"{api_endpoint}/v1/health", timeout=15)
        print(f"Health Status Code: {h_resp.status_code}")
        print(f"Health Response:    {h_resp.text}")

        print("\nInvoking Live Public GTI Regime Forecast for GSNP...")
        g_resp = requests.get(f"{api_endpoint}/v1/intellis_gti_regime?plant=GSNP&date=2026-10-08", timeout=30)
        print(f"GTI Status Code: {g_resp.status_code}")
        if g_resp.status_code == 200:
            payload = g_resp.json()
            print(f"Plant:              {payload.get('plant_name')}")
            print(f"Target Date:        {payload.get('target_date')}")
            print(f"Peak GTI:           {payload.get('peak_gti_wm2')} W/m²")
            print(f"Open-Meteo Plan:    {payload.get('metadata', {}).get('plan_tier')}")
            print(f"Blocks Received:    {len(payload.get('blocks_96', []))}")
            print("\n>>> SUCCESS! THE ENDPOINT IS 100% LIVE AND READY FOR CLIENTS. <<<")
    except Exception as exc:
        print(f"  [WARN] Smoke test encountered an issue: {exc}")


if __name__ == "__main__":
    main()
