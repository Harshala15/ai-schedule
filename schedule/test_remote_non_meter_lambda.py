"""test_remote_non_meter_lambda.py

Invokes ANDAD, BALAKWADA, and CME Lambdas for a test revision on 2026-09-15
and inspects the resulting schedule on S3 to verify the 5 canonical columns.
"""

import json
import boto3

PROFILE = "intellis-608"
REGION = "ap-south-1"
BUCKET = "vedanjay-schedules-test-608744602858"


def main():
    try:
        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        lambda_client = session.client("lambda")
        s3_client = session.client("s3")
    except Exception:
        lambda_client = boto3.client("lambda", region_name=REGION)
        s3_client = boto3.client("s3")

    test_sites = ["BALAKWADA", "ANDAD", "CME"]
    target_date = "2026-09-15"
    target_time = "11:15"

    for site in test_sites:
        fn = f"{site}-ai-intellis-scheduler"
        payload = {
            "site_id": site,
            "target_date": target_date,
            "target_time": target_time,
            "force": True,
            "s3_bucket": BUCKET,
        }
        print(f"\n--- Invoking {fn} for {target_date} {target_time} ---")
        try:
            resp = lambda_client.invoke(
                FunctionName=fn,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )
            payload_resp = json.loads(resp["Payload"].read().decode("utf-8"))
            print(f"  Response Status: {payload_resp.get('status')}")
            snapshot_key = payload_resp.get("snapshot_csv_key")
            print(f"  Snapshot Key: {snapshot_key}")

            if snapshot_key:
                obj = s3_client.get_object(Bucket=BUCKET, Key=snapshot_key)
                content = obj["Body"].read().decode("utf-8").splitlines()
                header = content[0] if content else ""
                print(f"  S3 Header: {header}")
                expected = "Block,Time Interval (15 minute interval),intellis_gti,intellis_mw,schedule_mw"
                assert header == expected, f"Header mismatch! Expected: {expected}, Got: {header}"
                print("  [PASS] Canonical 5 columns verified on S3 output!")
                # Sample block 48 (12:00)
                if len(content) > 48:
                    print(f"  Sample Row (Block 48): {content[48]}")
        except Exception as exc:
            print(f"  [ERROR] Invocation failed for {fn}: {exc}")


if __name__ == "__main__":
    main()
