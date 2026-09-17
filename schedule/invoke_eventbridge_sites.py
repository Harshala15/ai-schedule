"""invoke_eventbridge_sites.py

Invokes the 6 requested sites according to their configured AWS EventBridge schedule rules:
- BHUPALPALLY (Telangana 8 revisions: 06:00, 06:45, 08:15, 09:45, 11:15, 12:45, 14:15, 15:45)
- KOTHAGUDEM  (Telangana 8 revisions: 06:00, 06:45, 08:15, 09:45, 11:15, 12:45, 14:15, 15:45)
- NANDGAON    (MP 30-min revisions: 05:00 to 15:30 every 30 mins)
- ANJANGOAN   (MP 30-min revisions: 05:00 to 15:30 every 30 mins)
- GSNP        (MP 30-min revisions: 05:00 to 15:30 every 30 mins)
- SIRMOUR     (MP 30-min revisions: 05:00 to 15:30 every 30 mins)

Runs directly against the deployed AWS Lambda functions using the intellis-608 profile.
"""

import datetime as dt
import io
import json
import time
import boto3
import pandas as pd

PROFILE = "intellis-608"
REGION = "ap-south-1"
BUCKET = "vedanjay-schedules-test-608744602858"
DATE = "2026-09-16"

# Define sites and their EventBridge revision schedules
TELANGANA_REVISIONS = ["06:00", "06:45", "08:15", "09:45", "11:15", "12:45", "14:15", "15:45"]

MP_30MIN_REVISIONS = [
    f"{h:02d}:{m:02d}"
    for h in range(5, 16)
    for m in (0, 30)
    if not (h == 15 and m > 30)
]

SITES_CONFIG = [
    {
        "site_id": "NANDGAON",
        "fn_name": "NANDGAON-ai-intellis-scheduler",
        "revisions": MP_30MIN_REVISIONS,
        "type": "MP 30-Min",
    },
    {
        "site_id": "ANJANGOAN",
        "fn_name": "ANJANGOAN-ai-intellis-scheduler",
        "revisions": MP_30MIN_REVISIONS,
        "type": "MP 30-Min",
    },
    {
        "site_id": "BHUPALPALLY",
        "fn_name": "BHUPALPALLY-ai-intellis-scheduler",
        "revisions": TELANGANA_REVISIONS,
        "type": "Telangana 8-Gate",
    },
    {
        "site_id": "KOTHAGUDEM",
        "fn_name": "KOTHAGUDEM-ai-intellis-scheduler",
        "revisions": TELANGANA_REVISIONS,
        "type": "Telangana 8-Gate",
    },
    {
        "site_id": "GSNP",
        "fn_name": "GSNP-ai-intellis-scheduler",
        "revisions": MP_30MIN_REVISIONS,
        "type": "MP 30-Min",
    },
    {
        "site_id": "SIRMOUR",
        "fn_name": "SIRMOUR-ai-intellis-scheduler",
        "revisions": MP_30MIN_REVISIONS,
        "type": "MP 30-Min",
    },
]


def main():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    lambda_client = session.client("lambda")
    s3_client = session.client("s3")

    print(f"=== Starting Multi-Site EventBridge Revisions for Date: {DATE} ===")
    total_sites = len(SITES_CONFIG)
    total_invocations = sum(len(cfg["revisions"]) for cfg in SITES_CONFIG)
    print(f"Total sites: {total_sites} | Total revisions across all sites: {total_invocations}")

    all_results = []

    for site_idx, cfg in enumerate(SITES_CONFIG, 1):
        site_id = cfg["site_id"]
        fn_name = cfg["fn_name"]
        revisions = cfg["revisions"]
        sched_type = cfg["type"]

        print(f"\n{'='*70}")
        print(f"[{site_idx}/{total_sites}] Processing Site: {site_id} ({fn_name}) - {sched_type}")
        print(f"Revisions ({len(revisions)}): {revisions}")
        print(f"{'='*70}")

        prefix = f"generated/vedanjay_ai_intellis/{site_id}/outputs/{DATE}"

        # Clean idempotency locks so all target revisions compute freshly
        try:
            paginator = s3_client.get_paginator("list_objects_v2")
            keys_to_del = []
            for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{prefix}/.idempotency/"):
                for obj in page.get("Contents", []):
                    keys_to_del.append({"Key": obj["Key"]})
            if keys_to_del:
                s3_client.delete_objects(Bucket=BUCKET, Delete={"Objects": keys_to_del})
                print(f"  Cleaned {len(keys_to_del)} existing idempotency locks in S3.")
        except Exception as exc:
            print(f"  Notice on idempotency lock cleanup: {exc}")

        for rev_idx, r_time in enumerate(revisions, 1):
            slug = r_time.replace(":", "-")
            payload = {
                "site_id": site_id,
                "target_date": DATE,
                "target_time": r_time,
                "bucket": BUCKET,
                "output_prefix": "generated/vedanjay_ai_intellis",
                "force": True,
                "recompute": True,
            }

            print(f"  [{rev_idx}/{len(revisions)}] Invoking {fn_name} at {r_time} IST ...", end=" ", flush=True)

            success = False
            for attempt in range(1, 4):
                try:
                    t0 = time.time()
                    resp = lambda_client.invoke(
                        FunctionName=fn_name,
                        InvocationType="RequestResponse",
                        Payload=json.dumps(payload).encode("utf-8"),
                    )
                    elapsed = time.time() - t0
                    payload_resp = json.loads(resp["Payload"].read().decode("utf-8"))

                    if resp.get("FunctionError"):
                        print(f"FAIL (attempt {attempt}): {payload_resp}")
                        time.sleep(2)
                        continue

                    # S3 archival of current_final and penalty
                    current_final_key = f"{prefix}/{site_id}_{DATE}_current_final_schedule.csv"
                    penalty_key = f"{prefix}/{site_id}_{DATE}_penalty_schedule.csv"
                    archive_cf = f"{prefix}/revisions/{site_id}_{DATE}_current_final_{slug}.csv"
                    archive_pen = f"{prefix}/revisions/{site_id}_{DATE}_penalty_{slug}.csv"

                    tot_pen = 0.0
                    safe_cnt = 0
                    try:
                        s3_client.copy_object(
                            Bucket=BUCKET,
                            CopySource={"Bucket": BUCKET, "Key": current_final_key},
                            Key=archive_cf,
                        )
                        s3_client.copy_object(
                            Bucket=BUCKET,
                            CopySource={"Bucket": BUCKET, "Key": penalty_key},
                            Key=archive_pen,
                        )
                        pen_obj = s3_client.get_object(Bucket=BUCKET, Key=penalty_key)
                        df_p = pd.read_csv(io.BytesIO(pen_obj["Body"].read()))
                        if "block_penalty_inr" in df_p.columns:
                            tot_pen = float(df_p["block_penalty_inr"].sum())
                        if "dsm_slab" in df_p.columns:
                            safe_cnt = int((df_p["dsm_slab"] == "0% Safe").sum())
                    except Exception as arch_exc:
                        pass

                    print(f"OK ({elapsed:.1f}s) | Safe: {safe_cnt}/96 | Pen: Rs. {tot_pen:,.2f}")
                    all_results.append({
                        "Site": site_id,
                        "Type": sched_type,
                        "Revision": r_time,
                        "Status": "SUCCESS",
                        "Safe_Blocks": f"{safe_cnt}/96",
                        "Penalty_INR": f"Rs. {tot_pen:,.2f}",
                        "Elapsed_Sec": f"{elapsed:.1f}",
                    })
                    success = True
                    break
                except Exception as exc:
                    print(f"ERR (attempt {attempt}): {exc}")
                    time.sleep(3)

            if not success:
                all_results.append({
                    "Site": site_id,
                    "Type": sched_type,
                    "Revision": r_time,
                    "Status": "FAILED",
                    "Safe_Blocks": "-",
                    "Penalty_INR": "-",
                    "Elapsed_Sec": "-",
                })

            time.sleep(1)

    print("\n" + "=" * 70)
    print("ALL REQUESTED SITES EVENTBRIDGE REVISIONS COMPLETED")
    print("=" * 70)
    df_all = pd.DataFrame(all_results)
    print(df_all.to_string(index=False))
    df_all.to_csv("schedule/eventbridge_invocations_summary.csv", index=False)
    print("\nSaved summary to schedule/eventbridge_invocations_summary.csv")


if __name__ == "__main__":
    main()
