import boto3
import json
import time
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed

REGION = "ap-south-1"
TARGET_DATE = "2026-10-17"
BUCKET = "vedanjay-schedules-test-552496640388"
OUTPUT_PREFIX = "generated/vedanjay_ai_intellis"

PLANTS = [
    "ANDAD", "ANJANGOAN", "BALAKWADA", "BAMKHAL", "BHUPALPALLY",
    "CHANDAWASA", "CLIMATEDETOX", "CME", "EMIL", "ENRICH",
    "GSNP", "GUGARIYAKHEDI", "JEWLI", "JGBPL", "KASIPET",
    "KOTHAGUDEM", "LGEPL", "LOKGREENB2", "NANDGAON", "OSEPL",
    "PRANAV", "REWASEIT", "REWASPRNG", "SAWDA", "SHAHA",
    "SIDDEHESH", "SIRMOUR", "UPL", "ZTRIC"
]

STANDARD_SLOTS = ["05:15", "06:45", "08:15", "09:45", "11:15", "12:45", "14:15"]

boto_config = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    max_pool_connections=50
)

s3 = boto3.client("s3", region_name=REGION)

print(f"Scanning S3 for existing revisions on {TARGET_DATE} across {len(PLANTS)} plants...")
tasks = []

for p in PLANTS:
    prefix = f"{OUTPUT_PREFIX}/{p}/outputs/{TARGET_DATE}/"
    res = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    files = [o["Key"].split("/")[-1] for o in res.get("Contents", []) if o["Key"].endswith(".csv") and "schedule_from_" in o["Key"]]
    slots = []
    for f in sorted(files):
        parts = f.split("t")
        if len(parts) == 2:
            t_str = parts[1][:4]
            slots.append(f"{t_str[:2]}:{t_str[2:]}")
    if not slots:
        slots = STANDARD_SLOTS
    for s in slots:
        tasks.append((p, s))

print(f"Total plant revision tasks to update: {len(tasks)}")

def invoke_revision(plant, slot):
    lam = boto3.client("lambda", region_name=REGION, config=boto_config)
    fn_name = f"{plant}-ai-intellis-scheduler"
    
    # Calculate curr_block from slot HH:MM
    parts = slot.split(":")
    h, m = int(parts[0]), int(parts[1])
    curr_blk = (h * 60 + m) // 15 + 1
    
    payload = {
        "site_id": plant,
        "plant_name": plant,
        "target_date": TARGET_DATE,
        "target_time": slot,
        "curr_block": curr_blk,
        "force": True,
        "use_llm": True
    }
    
    t0 = time.time()
    try:
        resp = lam.invoke(
            FunctionName=fn_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload)
        )
        dur = round(time.time() - t0, 1)
        raw = resp["Payload"].read().decode("utf-8")
        try:
            res_json = json.loads(raw)
            status = res_json.get("status", "ok")
        except Exception:
            status = "raw_ok" if "ok" in raw.lower() else "unknown"
        return plant, slot, status, dur, None
    except Exception as exc:
        dur = round(time.time() - t0, 1)
        return plant, slot, "ERROR", dur, str(exc)

# Execute concurrently across 8 threads
print("\nExecuting batch updates...")
results = []
success_count = 0
error_count = 0

with ThreadPoolExecutor(max_workers=8) as executor:
    future_to_task = {executor.submit(invoke_revision, p, s): (p, s) for p, s in tasks}
    for idx, future in enumerate(as_completed(future_to_task), 1):
        p, s, status, dur, err = future.result()
        if status in ("ok", "raw_ok"):
            success_count += 1
            print(f"[{idx}/{len(tasks)}] [OK] {p:<14} @ {s} ({dur}s)")
        else:
            error_count += 1
            print(f"[{idx}/{len(tasks)}] [{status}] {p:<14} @ {s} ({dur}s) - Error: {err}")
        results.append((p, s, status, dur, err))

print("\n" + "=" * 60)
print(f"REVISION BATCH UPDATE COMPLETED ON {TARGET_DATE}")
print(f"Total: {len(tasks)} | Success: {success_count} | Errors: {error_count}")
print("=" * 60)
