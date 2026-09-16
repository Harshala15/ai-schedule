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

REMAINING_MP_PLANTS = [
    ('GSNP', 'GSNP-ai-intellis-scheduler'),
    ('GUGARIYAKHEDI', 'GUGARIYAKHEDI-ai-intellis-scheduler'),
    ('NANDGAON', 'NANDGAON-ai-intellis-scheduler'),
    ('SAWDA', 'SAWDA-ai-intellis-scheduler'),
    ('ANDAD', 'ANDAD-ai-intellis-scheduler')
]

# Generate 15-min timestamps from 05:00 to 15:00 (41 revisions)
times_15min = []
for h in range(5, 16):
    for m in [0, 15, 30, 45]:
        if h == 15 and m > 0:
            break
        times_15min.append(f"{h:02d}:{m:02d}")

print("=== Starting Remaining MP State Plants 15-Min Revisions (05:00 to 15:00) ===")
print(f"Date: {DATE}")
print(f"Plants: {[p[0] for p in REMAINING_MP_PLANTS]}")
print(f"Total Revisions per Site: {len(times_15min)}")

all_records = []

for site_id, fn_name in REMAINING_MP_PLANTS:
    print(f"\n=======================================================")
    print(f"Processing Site: {site_id} ({fn_name})")
    print(f"=======================================================")
    
    prefix = f"generated/vedanjay_ai_intellis/{site_id}/outputs/{DATE}"
    
    # Clean idempotency locks
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        keys_to_del = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{prefix}/.idempotency/"):
            for obj in page.get('Contents', []):
                keys_to_del.append({'Key': obj['Key']})
        if keys_to_del:
            s3_client.delete_objects(Bucket=BUCKET, Delete={'Objects': keys_to_del})
            print(f"Cleaned {len(keys_to_del)} idempotency locks for {site_id}")
    except Exception as e:
        print(f"Notice on idempotency cleanup: {e}")

    for idx, r_time in enumerate(times_15min, 1):
        slug = r_time.replace(':', '-')
        payload = {
            "site_id": site_id,
            "target_date": DATE,
            "target_time": r_time,
            "force": True,
            "recompute": True
        }
        
        print(f"[{idx}/{len(times_15min)}] Invoking {site_id} at {r_time}...")
        success = False
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.time()
                resp = lambda_client.invoke(
                    FunctionName=fn_name,
                    InvocationType='RequestResponse',
                    Payload=json.dumps(payload).encode('utf-8')
                )
                elapsed = time.time() - t0
                payload_resp = json.loads(resp['Payload'].read().decode('utf-8'))
                
                if resp.get('FunctionError'):
                    print(f"  [Attempt {attempt}] Error: {payload_resp}")
                    time.sleep(3)
                    continue
                    
                print(f"  [Attempt {attempt}] [OK] Successful in {elapsed:.1f}s")
                
                # S3 Archival
                current_final_key = f"{prefix}/{site_id}_{DATE}_current_final_schedule.csv"
                penalty_key = f"{prefix}/{site_id}_{DATE}_penalty_schedule.csv"
                archive_cf = f"{prefix}/revisions/{site_id}_{DATE}_current_final_{slug}.csv"
                archive_pen = f"{prefix}/revisions/{site_id}_{DATE}_penalty_{slug}.csv"
                
                tot_pen = 0.0
                safe_cnt = 0
                cols_valid = False
                try:
                    s3_client.copy_object(Bucket=BUCKET, CopySource={'Bucket': BUCKET, 'Key': current_final_key}, Key=archive_cf)
                    s3_client.copy_object(Bucket=BUCKET, CopySource={'Bucket': BUCKET, 'Key': penalty_key}, Key=archive_pen)
                    
                    obj = s3_client.get_object(Bucket=BUCKET, Key=penalty_key)
                    df_p = pd.read_csv(io.BytesIO(obj['Body'].read()))
                    tot_pen = float(df_p['block_penalty_inr'].sum())
                    safe_cnt = int((df_p['dsm_slab'] == '0% Safe').sum())
                    
                    cf_obj = s3_client.get_object(Bucket=BUCKET, Key=current_final_key)
                    df_cf = pd.read_csv(io.BytesIO(cf_obj['Body'].read()))
                    cols_valid = (df_cf.columns.tolist() == ['Block', 'Time Interval (15 minute interval)', 'intellis_gti', 'intellis_mw', 'schedule_mw'])
                except Exception as e:
                    print(f"  Archive notice: {e}")

                all_records.append({
                    'Site': site_id,
                    'Revision': r_time,
                    'Status': 'SUCCESS',
                    'Safe_Blocks': f"{safe_cnt}/96",
                    'Penalty_INR': f"Rs. {tot_pen:,.2f}",
                    'Canonical_Cols': 'VALID' if cols_valid else 'MISMATCH',
                    'Elapsed': f"{elapsed:.1f}s"
                })
                success = True
                break
            except Exception as exc:
                print(f"  [Attempt {attempt}] Exception: {exc}")
                time.sleep(3 * attempt)

        if not success:
            all_records.append({
                'Site': site_id,
                'Revision': r_time,
                'Status': 'FAILED',
                'Safe_Blocks': '-',
                'Penalty_INR': '-',
                'Canonical_Cols': '-',
                'Elapsed': '-'
            })

        time.sleep(2)

print("\n=======================================================")
print(f"REMAINING MP PLANTS 15-MIN REVISIONS COMPLETE")
print("=======================================================")
df_res = pd.DataFrame(all_records)
print(df_res.to_string(index=False))
df_res.to_csv("remaining_mp_plants_15min_summary.csv", index=False)
