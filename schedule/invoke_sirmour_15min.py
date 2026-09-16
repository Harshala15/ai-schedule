import json
import time
import boto3
import pandas as pd
import io

session = boto3.Session(profile_name='intellis-608')
lambda_client = session.client('lambda', region_name='ap-south-1')
s3_client = session.client('s3')

BUCKET = 'vedanjay-schedules-test-608744602858'
DATE = '2026-09-15'
SITE = 'SIRMOUR'
FN_NAME = 'SIRMOUR-ai-intellis-scheduler'

# Generate 15-min intervals from 05:00 to 15:00
revision_times = []
for h in range(5, 16):
    for m in [0, 15, 30, 45]:
        if h == 15 and m > 0:
            break
        revision_times.append(f"{h:02d}:{m:02d}")

print(f"=== Starting SIRMOUR 15-Minute Revisions (05:00 to 15:00) ===")
print(f"Total Revisions to run: {len(revision_times)}")
print(f"Revisions: {revision_times}")

prefix = f"generated/vedanjay_ai_intellis/{SITE}/outputs/{DATE}"

# Clean existing idempotency locks for 2026-09-15
try:
    paginator = s3_client.get_paginator('list_objects_v2')
    keys_to_del = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{prefix}/.idempotency/"):
        for obj in page.get('Contents', []):
            keys_to_del.append({'Key': obj['Key']})
    if keys_to_del:
        s3_client.delete_objects(Bucket=BUCKET, Delete={'Objects': keys_to_del})
        print(f"Cleaned {len(keys_to_del)} idempotency locks for {SITE}")
except Exception as e:
    print(f"Notice on idempotency cleanup: {e}")

results = []

for idx, r_time in enumerate(revision_times, 1):
    slug = r_time.replace(':', '-')
    payload = {
        "site_id": SITE,
        "target_date": DATE,
        "target_time": r_time,
        "force": True,
        "recompute": True
    }
    
    print(f"\n[{idx}/{len(revision_times)}] Invoking {SITE} at {r_time}...")
    success = False
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            t0 = time.time()
            resp = lambda_client.invoke(
                FunctionName=FN_NAME,
                InvocationType='RequestResponse',
                Payload=json.dumps(payload).encode('utf-8')
            )
            elapsed = time.time() - t0
            payload_resp = json.loads(resp['Payload'].read().decode('utf-8'))
            
            if resp.get('FunctionError'):
                print(f"  [Attempt {attempt}] Lambda error: {payload_resp}")
                time.sleep(3)
                continue
                
            print(f"  [Attempt {attempt}] [OK] Successful in {elapsed:.1f}s")
            
            # Archive outputs
            current_final_key = f"{prefix}/{SITE}_{DATE}_current_final_schedule.csv"
            penalty_key = f"{prefix}/{SITE}_{DATE}_penalty_schedule.csv"
            archive_cf = f"{prefix}/revisions/{SITE}_{DATE}_current_final_{slug}.csv"
            archive_pen = f"{prefix}/revisions/{SITE}_{DATE}_penalty_{slug}.csv"
            
            tot_pen = 0.0
            safe_cnt = 0
            try:
                s3_client.copy_object(Bucket=BUCKET, CopySource={'Bucket': BUCKET, 'Key': current_final_key}, Key=archive_cf)
                s3_client.copy_object(Bucket=BUCKET, CopySource={'Bucket': BUCKET, 'Key': penalty_key}, Key=archive_pen)
                
                # Check penalty
                obj = s3_client.get_object(Bucket=BUCKET, Key=penalty_key)
                df_p = pd.read_csv(io.BytesIO(obj['Body'].read()))
                tot_pen = float(df_p['block_penalty_inr'].sum())
                safe_cnt = int((df_p['dsm_slab'] == '0% Safe').sum())
            except Exception as e:
                print(f"  Archive notice: {e}")

            results.append({
                'Revision': r_time,
                'Status': 'SUCCESS',
                'Safe_Blocks': safe_cnt,
                'Penalty_INR': f"Rs. {tot_pen:,.2f}",
                'Elapsed': f"{elapsed:.1f}s"
            })
            success = True
            break
        except Exception as exc:
            print(f"  [Attempt {attempt}] Exception: {exc}")
            time.sleep(3 * attempt)

    if not success:
        results.append({
            'Revision': r_time,
            'Status': 'FAILED',
            'Safe_Blocks': '-',
            'Penalty_INR': '-',
            'Elapsed': '-'
        })

    # Cooldown between invocations to respect Lambda concurrency limits
    time.sleep(2)

print("\n=======================================================")
print(f"SIRMOUR 15-MIN REVISIONS COMPLETE ({DATE})")
print("=======================================================")
df_res = pd.DataFrame(results)
print(df_res.to_string(index=False))
df_res.to_csv("sirmour_15min_revisions_summary.csv", index=False)
