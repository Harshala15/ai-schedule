import boto3
import json
import time
from botocore.config import Config
from concurrent.futures import ThreadPoolExecutor, as_completed

REGION = "ap-south-1"
TARGET_DATE = "2026-10-17"
BUCKET = "vedanjay-schedules-test-552496640388"
OUTPUT_PREFIX = "generated/vedanjay_ai_intellis"

# All Non-Meter sites + GUGARIYAKHEDI
TARGET_PLANTS = [
    "CHANDAWASA",
    "CLIMATEDETOX",
    "EMIL",
    "LGEPL",
    "LOKGREENB2",
    "PRANAV",
    "REWASEIT",
    "SHAHA",
    "SIDDEHESH",
    "UPL",
    "GUGARIYAKHEDI",
]

STANDARD_SLOTS = ["05:15", "06:45", "08:15", "09:45", "11:15", "12:45", "14:15", "15:45"]

boto_config = Config(
    retries={"max_attempts": 8, "mode": "adaptive"},
    max_pool_connections=25
)

s3 = boto3.client("s3", region_name=REGION)
print(f"Compiling revision task list for {len(TARGET_PLANTS)} non-meter plants on {TARGET_DATE}...")

tasks = []
for p in TARGET_PLANTS:
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
    # also ensure 15:45 is included if daylight
    if "15:45" not in slots:
        slots.append("15:45")
    for s in slots:
        tasks.append((p, s))

print(f"Total revision tasks to execute: {len(tasks)}")

def update_single_slot(plant, slot):
    lam = boto3.client("lambda", region_name=REGION, config=boto_config)
    fn_name = f"{plant}-ai-intellis-scheduler"
    parts = slot.split(":")
    curr_blk = (int(parts[0]) * 60 + int(parts[1])) // 15 + 1
    
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

# Execute with max 4 workers to respect concurrent rate limits
print("\nStarting execution with 4 concurrent workers...")
results = []
success = 0
fail = 0

with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(update_single_slot, p, s): (p, s) for p, s in tasks}
    for idx, fut in enumerate(as_completed(futures), 1):
        p, s, st, dur, err = fut.result()
        if st in ("ok", "raw_ok"):
            success += 1
            print(f"[{idx}/{len(tasks)}] [OK] {p:<14} @ {s} ({dur}s)")
        else:
            fail += 1
            print(f"[{idx}/{len(tasks)}] [{st}] {p:<14} @ {s} ({dur}s) - {err}")
        results.append((p, s, st, dur, err))
        time.sleep(0.5)

print("\n" + "=" * 65)
print(f"NON-METER REVISION S3 UPDATE COMPLETED: {success}/{len(tasks)} OK, {fail} ERRORS")
print("=" * 65)
