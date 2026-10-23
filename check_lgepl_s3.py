import boto3
import json

s3 = boto3.client('s3')
bucket = 'vedanjay-schedules-test-552496640388'

for date in ['2026-10-21', '2026-10-22', '2026-10-23']:
    print(f'=== Date: {date} ===')
    prefix = f'raw/vedanjay/LGEPL/{date}/'
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    contents = resp.get('Contents', [])
    print(f'Raw meter objects ({prefix}): {len(contents)}')
    for obj in contents[:5]:
        print(f"  {obj['Key']} ({obj['Size']} bytes, modified: {obj['LastModified']})")
    
    out_prefix = f'generated/vedanjay_ai_intellis/LGEPL/outputs/{date}/'
    out_resp = s3.list_objects_v2(Bucket=bucket, Prefix=out_prefix)
    out_contents = out_resp.get('Contents', [])
    print(f'Outputs ({out_prefix}): {len(out_contents)}')
    for obj in out_contents[:10]:
        print(f"  {obj['Key']} ({obj['Size']} bytes)")
