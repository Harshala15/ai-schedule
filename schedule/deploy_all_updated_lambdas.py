import subprocess
import time
import boto3
import base64
import sys
import json

REGION = "ap-south-1"
ACCOUNT_ID = "552496640388"
REPO = "intellis-ai-scheduler"
TAG = "intellis-rewasprng-bell-curve-20261022-v8"
ECR_REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
IMAGE_URI = f"{ECR_REGISTRY}/{REPO}:{TAG}"

ALL_LAMBDAS = [
    "ANDAD-ai-intellis-scheduler",
    "ANJANGOAN-ai-intellis-scheduler",
    "BALAKWADA-ai-intellis-scheduler",
    "BAMKHAL-ai-intellis-scheduler",
    "BHUPALPALLY-ai-intellis-scheduler",
    "CHANDAWASA-ai-intellis-scheduler",
    "CLIMATEDETOX-ai-intellis-scheduler",
    "CME-ai-intellis-scheduler",
    "EMIL-ai-intellis-scheduler",
    "ENRICH-ai-intellis-scheduler",
    "GSNP-ai-intellis-scheduler",
    "GUGARIYAKHEDI-ai-intellis-scheduler",
    "JEWLI-ai-intellis-scheduler",
    "JGBPL-ai-intellis-scheduler",
    "KASIPET-ai-intellis-scheduler",
    "KOTHAGUDEM-ai-intellis-scheduler",
    "LGEPL-ai-intellis-scheduler",
    "LOKGREENB2-ai-intellis-scheduler",
    "NANDGAON-ai-intellis-scheduler",
    "OSEPL-ai-intellis-scheduler",
    "PRANAV-ai-intellis-scheduler",
    "REWAATHENA-ai-intellis-scheduler",
    "REWASEIT-ai-intellis-scheduler",
    "REWASPRNG-ai-intellis-scheduler",
    "SAWDA-ai-intellis-scheduler",
    "SHAHA-ai-intellis-scheduler",
    "SIDDEHESH-ai-intellis-scheduler",
    "SIRMOUR-ai-intellis-scheduler",
    "UPL-ai-intellis-scheduler",
    "WIND325-ai-intellis-scheduler",
    "SECI5-ai-intellis-scheduler",
    "ZTRIC-ai-intellis-scheduler",
    "intellis-dayhead-forcast-solar",
    "intellis-weekhead-forcast-solar",
    "intellis-dayhead-forcast-wind",
    "intellis-weekhead-forcast-wind",
]

print("=" * 70)
print("BUILDING, PUSHING & DEPLOYING LATEST INTELLIS AI DOCKER IMAGE")
print(f"Target Image:  {IMAGE_URI}")
print(f"Total Lambdas: {len(ALL_LAMBDAS)}")
print("=" * 70)

session = boto3.Session(region_name=REGION)
ecr_client = session.client("ecr")
lambda_client = session.client("lambda")

# 1. ECR Login
print("\n[Step 1/5] Logging into Amazon ECR...")
auth_token = ecr_client.get_authorization_token()["authorizationData"][0]["authorizationToken"]
user, pwd = base64.b64decode(auth_token).decode().split(":")

login_cmd = ["docker", "login", "--username", user, "--password-stdin", ECR_REGISTRY]
proc = subprocess.run(login_cmd, input=pwd, capture_output=True, text=True)
if proc.returncode != 0:
    print(f"ECR Login Failed: {proc.stderr}")
    sys.exit(1)
print(f"ECR Login Succeeded: {proc.stdout.strip()}")

# 2. Docker Build
print("\n[Step 2/5] Building Docker Image (--platform linux/amd64 --provenance=false)...")
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
print(f"\n[Step 3/5] Pushing Docker Image to ECR ({IMAGE_URI})...")
push_cmd = ["docker", "push", IMAGE_URI]
proc = subprocess.run(push_cmd, capture_output=True, text=True)
if proc.returncode != 0:
    print(f"Docker Push Failed:\n{proc.stderr}\n{proc.stdout}")
    sys.exit(1)
print("Docker Push Succeeded!")

# 4. Update Lambda Functions
print(f"\n[Step 4/5] Updating All {len(ALL_LAMBDAS)} Lambda Functions...")
deployment_results = []
for idx, fn in enumerate(ALL_LAMBDAS, 1):
    print(f"[{idx}/{len(ALL_LAMBDAS)}] Triggering update for {fn}...")
    try:
        lambda_client.update_function_code(
            FunctionName=fn,
            ImageUri=IMAGE_URI,
        )
        deployment_results.append({"Function": fn, "Status": "TRIGGERED"})
    except Exception as exc:
        print(f"  [EXCEPTION] Failed to trigger update for {fn}: {exc}")
        deployment_results.append({"Function": fn, "Status": f"EXCEPTION: {exc}"})

print("\nWaiting for all Lambda updates to reach 'Successful'...")
final_results = []
for item in deployment_results:
    fn = item["Function"]
    st = item["Status"]
    if st != "TRIGGERED":
        final_results.append(item)
        continue
    success = False
    for attempt in range(40):
        time.sleep(2)
        try:
            meta = lambda_client.get_function(FunctionName=fn)
            status = meta.get("Configuration", {}).get("LastUpdateStatus")
            if status == "Successful":
                print(f"  [OK] {fn} updated successfully.")
                final_results.append({"Function": fn, "Status": "SUCCESS"})
                success = True
                break
            elif status == "Failed":
                reason = meta.get("Configuration", {}).get("LastUpdateStatusReason", "Unknown")
                print(f"  [ERROR] {fn} update failed: {reason}")
                final_results.append({"Function": fn, "Status": f"FAILED: {reason}"})
                break
        except Exception as e:
            print(f"  [ERROR] querying {fn}: {e}")
            final_results.append({"Function": fn, "Status": f"ERROR: {e}"})
            break
    if not success and status != "Failed":
        final_results.append({"Function": fn, "Status": "TIMEOUT"})

print("\n" + "=" * 70)
print("DEPLOYMENT COMPLETE SUMMARY")
print("=" * 70)
for r in final_results:
    print(f"{r['Function']:<40} : {r['Status']}")

# 5. Live AWS Environment Verification Test
print("\n" + "=" * 70)
print("[Step 5/5] TESTING LATEST CHANGES IN LIVE AWS LAMBDA ENVIRONMENT")
print("=" * 70)

test_sample = [
    ("BHUPALPALLY-ai-intellis-scheduler", "BHUPALPALLY"),
    ("GSNP-ai-intellis-scheduler", "GSNP"),
    ("KASIPET-ai-intellis-scheduler", "KASIPET"),
    ("CHANDAWASA-ai-intellis-scheduler", "CHANDAWASA"),
]

for fn, site in test_sample:
    print(f"\nInvoking live AWS Lambda: {fn} for site {site}...")
    payload = {
        "site_id": site,
        "target_date": "2026-10-21",
        "target_time": "12:00",
        "force": True,
    }
    try:
        resp = lambda_client.invoke(
            FunctionName=fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        status_code = resp.get("StatusCode")
        resp_payload = json.loads(resp["Payload"].read().decode("utf-8"))
        print(f"  HTTP StatusCode: {status_code}")
        print(f"  Response Status: {resp_payload.get('status', 'unknown')}")
        if resp_payload.get("status") in ("ok", "skipped"):
            print(f"  [PASSED] Live invocation successful in AWS environment!")
        else:
            print(f"  [WARN] Unexpected response: {resp_payload}")
    except Exception as exc:
        print(f"  [FAILED] Invocation failed: {exc}")
