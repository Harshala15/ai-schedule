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
TARGET_TIME = '14:15'
SLUG = '14-15'

SITES = [
    ('KASIPET', 'KASIPET-ai-intellis-scheduler'),
    ('KOTHAGUDEM', 'KOTHAGUDEM-ai-intellis-scheduler'),
    ('BHUPALPALLY', 'BHUPALPALLY-ai-intellis-scheduler')
]

print(f"=== Invoking Telangana Plants for Revision {TARGET_TIME} ({DATE}) ===")

results = []

for site_id, fn_name in SITES:
    print(f"\nInvoking {site_id} ({fn_name}) at {TARGET_TIME}...")
    prefix = f"generated/vedanjay_ai_intellis/{site_id}/outputs/{DATE}"
    
    # Clean idempotency lock for this specific revision
    lock_key = f"{prefix}/.idempotency/{site_id}__{DATE}__{SLUG}.json"
    try:
        s3_client.delete_object(Bucket=BUCKET, Key=lock_key)
    except Exception:
        pass

    payload = {
        "site_id": site_id,
        "target_date": DATE,
        "target_time": TARGET_TIME,
        "force": True,
        "recompute": True
    }
    
    success = False
    for attempt in range(1, 4):
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
                time.sleep(4)
                continue
                
            print(f"  [Attempt {attempt}] [OK] Successful in {elapsed:.1f}s")
            
            # Archive
            current_final_key = f"{prefix}/{site_id}_{DATE}_current_final_schedule.csv"
            penalty_key = f"{prefix}/{site_id}_{DATE}_penalty_schedule.csv"
            archive_cf = f"{prefix}/revisions/{site_id}_{DATE}_current_final_{SLUG}.csv"
            archive_pen = f"{prefix}/revisions/{site_id}_{DATE}_penalty_{SLUG}.csv"
            
            tot_pen = 0.0
            safe_cnt = 0
            try:
                s3_client.copy_object(Bucket=BUCKET, CopySource={'Bucket': BUCKET, 'Key': current_final_key}, Key=archive_cf)
                s3_client.copy_object(Bucket=BUCKET, CopySource={'Bucket': BUCKET, 'Key': penalty_key}, Key=archive_pen)
                
                obj = s3_client.get_object(Bucket=BUCKET, Key=penalty_key)
                df_p = pd.read_csv(io.BytesIO(obj['Body'].read()))
                tot_pen = float(df_p['block_penalty_inr'].sum())
                safe_cnt = int((df_p['dsm_slab'] == '0% Safe').sum())
            except Exception as e:
                print(f"  Archive notice: {e}")

            results.append({
                'Site': site_id,
                'Revision': TARGET_TIME,
                'Status': 'SUCCESS',
                'Safe_Blocks': f"{safe_cnt}/96",
                'Penalty_INR': f"Rs. {tot_pen:,.2f}",
                'Elapsed': f"{elapsed:.1f}s"
            })
            success = True
            break
        except Exception as exc:
            print(f"  [Attempt {attempt}] Exception: {exc}")
            time.sleep(4 * attempt)

    if not success:
        results.append({
            'Site': site_id,
            'Revision': TARGET_TIME,
            'Status': 'FAILED',
            'Safe_Blocks': '-',
            'Penalty_INR': '-',
            'Elapsed': '-'
        })
    time.sleep(3)

print("\n=======================================================")
print(f"TELANGANA {TARGET_TIME} REVISION RESULTS")
print("=======================================================")
df_res = pd.DataFrame(results)
print(df_res.to_string(index=False))
