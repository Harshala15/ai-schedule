import boto3
import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

REGION = "ap-south-1"
TARGET_DATE = "2026-10-21"
CUTOFF_TIME = "13:15"

session = boto3.Session(region_name=REGION)
lambda_client = session.client("lambda")

with open("plant_schedules_and_revisions.json", "r", encoding="utf-8") as f:
    revision_matrix = json.load(f)

ALL_PLANTS = [
    "ANDAD", "ANJANGOAN", "BALAKWADA", "BAMKHAL", "BHUPALPALLY",
    "CHANDAWASA", "CLIMATEDETOX", "CME", "EMIL", "ENRICH",
    "GSNP", "GUGARIYAKHEDI", "JEWLI", "JGBPL", "KASIPET",
    "KOTHAGUDEM", "LGEPL", "LOKGREENB2", "NANDGAON", "OSEPL",
    "PRANAV", "REWASEIT", "REWASPRNG", "SAWDA", "SHAHA",
    "SIDDEHESH", "SIRMOUR", "UPL", "ZTRIC"
]

DEFAULT_NON_METER_SLOTS = ["05:15", "06:45", "08:15", "09:45", "11:15", "12:45"]

# Build task list per plant
plant_slot_map = {}
total_slots = 0

for p in ALL_PLANTS:
    info = revision_matrix.get(p, {})
    intraday = info.get("intraday", [])
    da0 = info.get("dayahead", {}).get("da0")
    slots = []
    if da0 and da0 != "absent" and da0 <= CUTOFF_TIME:
        slots.append(da0)
    for s in intraday:
        if s <= CUTOFF_TIME:
            slots.append(s)
    if not slots:
        slots = [s for s in DEFAULT_NON_METER_SLOTS if s <= CUTOFF_TIME]
    slots = sorted(list(set(slots)))
    plant_slot_map[p] = slots
    total_slots += len(slots)

print("=" * 80)
print(f"INVOKING FLEET REVISIONS FOR {len(ALL_PLANTS)} PLANTS UP TO {CUTOFF_TIME} ON {TARGET_DATE}")
print(f"Total Revision Slots to Execute: {total_slots}")
print("=" * 80)

def process_plant_revisions(plant_name: str, slots: list[str]) -> dict:
    fn_name = f"{plant_name}-ai-intellis-scheduler"
    results = []
    for s in slots:
        payload = {
            "site_id": plant_name,
            "target_date": TARGET_DATE,
            "target_time": s,
            "force": True,
        }
        t0 = time.time()
        try:
            resp = lambda_client.invoke(
                FunctionName=fn_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )
            code = resp.get("StatusCode")
            res_payload = json.loads(resp["Payload"].read().decode("utf-8"))
            st = res_payload.get("status", "unknown")
            dur = time.time() - t0
            print(f"  [{plant_name:<13}] Slot {s} -> HTTP {code} ({st}) in {dur:.1f}s")
            results.append({"slot": s, "status": st, "code": code})
        except Exception as exc:
            dur = time.time() - t0
            print(f"  [{plant_name:<13}] Slot {s} -> FAILED: {exc} ({dur:.1f}s)")
            results.append({"slot": s, "status": f"ERROR: {exc}", "code": 500})
    return {"plant": plant_name, "results": results}

start_time = time.time()
plant_summaries = {}

# Execute up to 8 plants concurrently, with each plant processing its slots sequentially
with ThreadPoolExecutor(max_workers=8) as executor:
    futures = {
        executor.submit(process_plant_revisions, p, plant_slot_map[p]): p
        for p in ALL_PLANTS
    }
    for fut in as_completed(futures):
        res = fut.result()
        p = res["plant"]
        plant_summaries[p] = res["results"]
        ok_count = sum(1 for r in res["results"] if r.get("status") in ("ok", "skipped"))
        print(f"[COMPLETED] {p}: {ok_count}/{len(plant_slot_map[p])} slots executed successfully.")

total_elapsed = time.time() - start_time
print("\n" + "=" * 80)
print(f"FLEET REVISION RUN COMPLETE in {total_elapsed:.1f}s ({total_elapsed/60:.2f} min)")
print("=" * 80)

total_success = 0
total_failed = 0
for p in ALL_PLANTS:
    res = plant_summaries.get(p, [])
    ok_cnt = sum(1 for r in res if r.get("status") in ("ok", "skipped"))
    fail_cnt = len(res) - ok_cnt
    total_success += ok_cnt
    total_failed += fail_cnt
    print(f"{p:<15} : {ok_cnt}/{len(res)} SUCCESS" + (f" ({fail_cnt} FAILED)" if fail_cnt else ""))

print("-" * 80)
print(f"GRAND TOTAL: {total_success}/{total_slots} SLOTS SUCCEEDED | {total_failed} FAILED")
print("=" * 80)
