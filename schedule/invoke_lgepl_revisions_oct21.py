import boto3
import json
import time

REGION = "ap-south-1"
TARGET_DATE = "2026-10-21"
PLANT = "LGEPL"
FUNCTION_NAME = "LGEPL-ai-intellis-scheduler"

session = boto3.Session(region_name=REGION)
lambda_client = session.client("lambda")

slots = [
    "04:30",
    "06:45",
    "08:15",
    "09:45",
    "11:15",
    "12:45",
    "14:15",
    "15:45",
]

print("=" * 80)
print(f"INVOKING ALL REVISIONS FOR {PLANT} ON {TARGET_DATE}")
print(f"Total Slots: {len(slots)} -> {slots}")
print("=" * 80)

results = []
for s in slots:
    payload = {
        "site_id": PLANT,
        "target_date": TARGET_DATE,
        "target_time": s,
        "force": True,
    }
    t0 = time.time()
    try:
        resp = lambda_client.invoke(
            FunctionName=FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        code = resp.get("StatusCode")
        res_payload = json.loads(resp["Payload"].read().decode("utf-8"))
        st = res_payload.get("status", "unknown")
        dur = time.time() - t0
        print(f"  [{PLANT}] Slot {s} -> HTTP {code} (status={st}) in {dur:.1f}s")
        results.append({"slot": s, "status": st, "code": code})
    except Exception as exc:
        dur = time.time() - t0
        print(f"  [{PLANT}] Slot {s} -> FAILED: {exc} ({dur:.1f}s)")
        results.append({"slot": s, "status": f"ERROR: {exc}", "code": 500})
    time.sleep(1)

print("=" * 80)
successful = sum(1 for r in results if r["status"] in ("ok", "skipped"))
print(f"COMPLETED {PLANT}: {successful}/{len(slots)} revisions executed successfully.")
print("=" * 80)
