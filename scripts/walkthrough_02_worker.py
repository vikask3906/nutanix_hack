"""Walkthrough 2: the worker loop, end to end.

Needs the stack running:

    docker compose up --scale worker=3
    docker compose exec web python manage.py seed_demo

Then:

    .venv\\Scripts\\python.exe scripts/walkthrough_02_worker.py

Four scenes:
    1. The API does no work     - POST returns before anything has executed
    2. The log fills in          - watch events appear as workers act
    3. Three workers, one DAG    - parallel branches, zero coordination
    4. Failure stops the branch  - downstream steps are never scheduled
"""

import sys
import time

import requests

API = "http://localhost:8000/api"
WIDTH = 78


def rule(char="-"):
    print(char * WIDTH)


def scene(number, title):
    print()
    rule("=")
    print(f"SCENE {number}: {title}")
    rule("=")


def start(workflow, **payload):
    response = requests.post(f"{API}/runs/", json={"workflow": workflow, **payload}, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch(run_id):
    return requests.get(f"{API}/runs/{run_id}/", timeout=10).json()


def wait_for_terminal(run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = fetch(run_id)
        if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return run
        time.sleep(0.3)
    raise TimeoutError(f"run {run_id} did not finish within {timeout}s")


try:
    requests.get(f"{API}/workflows/", timeout=5)
except requests.RequestException:
    print("Cannot reach the API at http://localhost:8000.")
    print("Start it with:  docker compose up --scale worker=3")
    sys.exit(1)


print()
rule("=")
print("CASCADE WALKTHROUGH 2 - THE WORKER LOOP")
rule("=")

types = requests.get(f"{API}/workflows/step_types/", timeout=5).json()
print()
print("  Step types this deployment can execute:", ", ".join(types["step_types"]))
print("  (that list comes straight from the plugin registry - adding a step")
print("   type means writing one class, not touching the engine)")


# ---------------------------------------------------------------------------
scene(1, "The API does no work")

t0 = time.perf_counter()
run = start("demo_linear")
elapsed_ms = (time.perf_counter() - t0) * 1000

print()
print(f"  POST /api/runs/ returned in {elapsed_ms:.0f} ms")
print(f"  run id     : {run['id']}")
print(f"  status     : {run['status']}")
print(f"  last_seq   : {run['last_seq']}  (RUN_STARTED + STEP_SCHEDULED)")
print()
print("  Nothing has executed yet. The request only wrote rows saying work")
print("  needs doing. That is why a 3-second workflow and a 3-day workflow")
print("  are the same code path and the same response time.")


# ---------------------------------------------------------------------------
scene(2, "The log fills in as workers act")

print()
seen = 0
deadline = time.time() + 30
while time.time() < deadline:
    detail = fetch(run["id"])
    for event in detail["events"][seen:]:
        print(f"    [{event['seq']:>2}] {event['type']:<22} {event['step_id']}")
    seen = len(detail["events"])
    if detail["status"] in {"SUCCEEDED", "FAILED"}:
        break
    time.sleep(0.25)

final = fetch(run["id"])
print()
print(f"  final status: {final['status']}")

transform = final["context"]["steps"]["transform"]["output"]
print()
print("  The 'transform' step's command was:")
print("      print('transformed {{ steps.fetch.output.records }} records')")
print(f"  'fetch' had produced: {final['context']['steps']['fetch']['output']}")
print(f"  so the command actually run printed: {transform['stdout'].strip()!r}")
print()
print("  The template was resolved against state rebuilt from the event log,")
print("  not from anything the previous worker handed over.")


# ---------------------------------------------------------------------------
scene(3, "Three workers, one DAG, no coordination")

print()
print("  demo_diamond fans out into three branches that each sleep 2 seconds,")
print("  then joins. Run serially that is 6 seconds.")
print()

t0 = time.perf_counter()
diamond = start("demo_diamond")
finished = wait_for_terminal(diamond["id"])
wall = time.perf_counter() - t0

detail = fetch(diamond["id"])
starts = {}
ends = {}
for event in detail["events"]:
    if event["type"] == "STEP_STARTED":
        starts[event["step_id"]] = event["created_at"]
    elif event["type"] == "STEP_SUCCEEDED":
        ends[event["step_id"]] = event["created_at"]

print("  step         started at                    finished at")
for step_id in ["start", "branch_a", "branch_b", "branch_c", "join"]:
    if step_id in starts:
        print(f"    {step_id:<11}  {starts[step_id][11:23]}                  {ends.get(step_id, '')[11:23]}")

print()
print(f"  status     : {finished['status']}")
print(f"  wall clock : {wall:.1f}s for three 2-second branches")
print()
print("  The three branch_* rows overlap. Three separate worker processes each")
print("  claimed a different task with SELECT ... FOR UPDATE SKIP LOCKED - no")
print("  broker, no coordinator, no messages between them. Each simply locked a")
print("  row and skipped whatever was already locked.")
print()
print("  See who did what:  docker compose logs worker | grep claim")


# ---------------------------------------------------------------------------
scene(4, "Failure stops the branch")

print()
failing = start("demo_failing")
result = wait_for_terminal(failing["id"])
detail = fetch(failing["id"])

for event in detail["events"]:
    print(f"    [{event['seq']:>2}] {event['type']:<22} {event['step_id']}")

print()
print(f"  status : {result['status']}")
print(f"  error  : {result['error']}")
print()
never = detail["context"]["steps"].get("never_runs")
print(f"  'never_runs' in context: {never if never else 'absent - it was never scheduled'}")
print()
print("  It depends on 'risky', which failed, so ready_steps never offered it")
print("  and no task row was ever created. Nothing had to explicitly cancel it.")

print()
rule("=")
print("Next: Block 3 adds retries with backoff, and the lease reaper that")
print("makes 'kill -9 a worker mid-step' recoverable.")
rule("=")
print()
