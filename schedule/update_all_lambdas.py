"""update_all_lambdas.py

Updates all 15 Intellis AI Lambda functions to the new v5 container image:
608744602858.dkr.ecr.ap-south-1.amazonaws.com/intellis-ai-scheduler:intellis-ai-20260915-v5
"""

import time
import boto3

IMAGE_URI = "608744602858.dkr.ecr.ap-south-1.amazonaws.com/intellis-ai-scheduler:intellis-ai-20260915-v5"
REGION = "ap-south-1"
PROFILE = "intellis-608"

SITES = [
    "ANDAD",
    "GUGARIYAKHEDI",
    "SAWDA",
    "BALAKWADA",
    "CME",
    "SIRMOUR",
    "BHUPALPALLY",
    "KASIPET",
    "KOTHAGUDEM",
    "OSEPL",
    "ANJANGOAN",
    "BAMKHAL",
    "NANDGAON",
    "GSNP",
    "ZTRIC",
]


def main():
    try:
        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        client = session.client("lambda")
    except Exception:
        client = boto3.client("lambda", region_name=REGION)

    print(f"Deploying image: {IMAGE_URI}")
    for site in SITES:
        fn = f"{site}-ai-intellis-scheduler"
        print(f"\nUpdating {fn}...")
        try:
            res = client.update_function_code(
                FunctionName=fn,
                ImageUri=IMAGE_URI,
            )
            # Wait for update to complete
            for _ in range(30):
                time.sleep(2)
                meta = client.get_function(FunctionName=fn)
                status = meta.get("Configuration", {}).get("LastUpdateStatus")
                if status == "Successful":
                    print(f"  [SUCCESS] {fn} updated to v5 successfully.")
                    break
                elif status == "Failed":
                    reason = meta.get("Configuration", {}).get("LastUpdateStatusReason", "Unknown")
                    print(f"  [ERROR] {fn} update failed: {reason}")
                    break
                else:
                    print(f"  ... waiting for {fn} update (status: {status})")
        except Exception as exc:
            print(f"  [FAIL] Failed to update {fn}: {exc}")


if __name__ == "__main__":
    main()
