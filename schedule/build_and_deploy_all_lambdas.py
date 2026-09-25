import subprocess
import time
import boto3
import base64
import sys

REGION = "ap-south-1"
PROFILE = "intellis-608"
ACCOUNT_ID = "608744602858"
REPO = "intellis-ai-scheduler"
TAG = "intellis-ai-20260925-v1"
ECR_REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE_URI = f"{ECR_REGISTRY}/{REPO}:{TAG}"

ALL_LAMBDAS = [
    "ANDAD-ai-intellis-scheduler",
    "ANJANGOAN-ai-intellis-scheduler",
    "BALAKWADA-ai-intellis-scheduler",
    "BAMKHAL-ai-intellis-scheduler",
    "BHUPALPALLY-ai-intellis-scheduler",
    "CHANDAWASA-ai-intellis-scheduler",
    "CME-ai-intellis-scheduler",
    "GSNP-ai-intellis-scheduler",
    "GUGARIYAKHEDI-ai-intellis-scheduler",
    "JEWLI-ai-intellis-scheduler",
    "JGBPL-ai-intellis-scheduler",
    "KASIPET-ai-intellis-scheduler",
    "KOTHAGUDEM-ai-intellis-scheduler",
    "NANDGAON-ai-intellis-scheduler",
    "OSEPL-ai-intellis-scheduler",
    "SAWDA-ai-intellis-scheduler",
    "SIRMOUR-ai-intellis-scheduler",
    "ZTRIC-ai-intellis-scheduler",
    "REWASPRNG-ai-intellis-scheduler",
    "CLIMATEDETOX-ai-intellis-scheduler",
    "EMIL-ai-intellis-scheduler",
    "UPL-ai-intellis-scheduler",
]

print("==================================================================")
print("BUILDING, PUSHING & DEPLOYING LATEST INTELLIS AI DOCKER IMAGE")
print(f"Target Image:  {IMAGE_URI}")
print(f"Total Lambdas: {len(ALL_LAMBDAS)}")
print("==================================================================")

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
ecr_client = session.client("ecr")
lambda_client = session.client("lambda")

# 1. ECR Login
print("\n[Step 1/4] Logging into Amazon ECR...")
auth_token = ecr_client.get_authorization_token()["authorizationData"][0]["authorizationToken"]
user, pwd = base64.b64decode(auth_token).decode().split(":")

login_cmd = ["docker", "login", "--username", user, "--password-stdin", ECR_REGISTRY]
proc = subprocess.run(login_cmd, input=pwd, capture_output=True, text=True)
if proc.returncode != 0:
    print(f"ECR Login Failed: {proc.stderr}")
    sys.exit(1)
print(f"ECR Login Succeeded: {proc.stdout.strip()}")

# 2. Docker Build
print("\n[Step 2/4] Building Docker Image (--platform linux/amd64 --provenance=false)...")
build_cmd = [
    "docker", "buildx", "build",
    "--platform", "linux/amd64",
    "--provenance=false",
    "--load",
    "-f", "Dockerfile.intellis-ai-lambda",
    "-t", IMAGE_URI,
    "."
]
proc = subprocess.run(build_cmd, cwd=r"d:\14 sept intellis\schedule", capture_output=True, text=True)
if proc.returncode != 0:
    print(f"Docker Build Failed:\n{proc.stderr}\n{proc.stdout}")
    sys.exit(1)
print("Docker Build Succeeded!")

# 3. Docker Push
print(f"\n[Step 3/4] Pushing Docker Image to ECR ({IMAGE_URI})...")
push_cmd = ["docker", "push", IMAGE_URI]
proc = subprocess.run(push_cmd, capture_output=True, text=True)
if proc.returncode != 0:
    print(f"Docker Push Failed:\n{proc.stderr}\n{proc.stdout}")
    sys.exit(1)
print("Docker Push Succeeded!")

# 4. Update Lambda Functions
print(f"\n[Step 4/4] Updating All {len(ALL_LAMBDAS)} Lambda Functions...")
deployment_results = []
for idx, fn in enumerate(ALL_LAMBDAS, 1):
    print(f"\n[{idx}/{len(ALL_LAMBDAS)}] Updating {fn}...")
    try:
        lambda_client.update_function_code(
            FunctionName=fn,
            ImageUri=IMAGE_URI,
        )
        
        # Poll for update completion
        success = False
        for attempt in range(40):
            time.sleep(2)
            meta = lambda_client.get_function(FunctionName=fn)
            status = meta.get("Configuration", {}).get("LastUpdateStatus")
            if status == "Successful":
                print(f"  [OK] {fn} updated successfully.")
                deployment_results.append({"Function": fn, "Status": "SUCCESS"})
                success = True
                break
            elif status == "Failed":
                reason = meta.get("Configuration", {}).get("LastUpdateStatusReason", "Unknown")
                print(f"  [ERROR] {fn} update failed: {reason}")
                deployment_results.append({"Function": fn, "Status": f"FAILED: {reason}"})
                break
            else:
                if attempt % 5 == 0:
                    print(f"  ... waiting for {fn} update (status: {status})")
        if not success and status != "Failed":
            deployment_results.append({"Function": fn, "Status": "TIMEOUT"})
    except Exception as exc:
        print(f"  [EXCEPTION] Failed to update {fn}: {exc}")
        deployment_results.append({"Function": fn, "Status": f"EXCEPTION: {exc}"})

print("\n==================================================================")
print("DEPLOYMENT COMPLETE SUMMARY")
print("==================================================================")
import pandas as pd
df_summary = pd.DataFrame(deployment_results)
print(df_summary.to_string(index=False))
df_summary.to_csv(r"d:\14 sept intellis\scratch\lambda_deployment_summary_20260924.csv", index=False)
