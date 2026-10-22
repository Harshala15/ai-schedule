import boto3
import json
import time

REGION = "ap-south-1"
TARGET_DATE = "2026-10-21"
PLANT = "REWASPRNG"
FUNCTION_NAME = "REWASPRNG-ai-intellis-scheduler"

session = boto3.Session(region_name=REGION)
lambda_client = session.client("lambda")

with open("schedule/plant_schedules_and_revisions.json", "r", encoding="utf-8") as f:
    matrix = json.load(f)

slots = matrix.get(PLANT, {}).get("intraday", [])
# Only run revisions up to current time (e.g. up to 16:30)
current_time_slot = "16:30"
actionable_slots = [s for s in slots if s <= current_time_slot]

print("=" * 80)
print(f"INVOKING REVISIONS FOR {PLANT} ON {TARGET_DATE} UP TO {current_time_slot}")
print(f"Total Slots: {len(actionable_slots)} -> {actionable_slots}")
print("=" * 80)

results = []
for s in actionable_slots:
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

print("=" * 80)
successful = sum(1 for r in results if r["status"] in ("ok", "skipped"))
print(f"COMPLETED {PLANT}: {successful}/{len(actionable_slots)} revisions executed successfully.")
print("=" * 80)
