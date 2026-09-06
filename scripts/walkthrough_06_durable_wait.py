"""Walkthrough 6: a wait that survives the entire stack being destroyed.

Needs the stack running:

    docker compose up -d --scale worker=3
    docker compose exec web python manage.py seed_demo

Then:

    .venv\\Scripts\\python.exe scripts/walkthrough_06_durable_wait.py

What happens:
    1. Start a workflow whose middle step waits 45 seconds
    2. Show that the wait is one database row, not a sleeping process
    3. docker compose down - every container, every process, the network
    4. Bring it back and watch the run resume and finish by itself

The claim being tested: nothing about a paused workflow lives in memory.
"""

import subprocess
import sys
import time

import requests

API = "http://localhost:8000/api"
WIDTH = 78


def rule(char="-"):
    print(char * WIDTH)


def stamp(message):
    print(f"  {time.strftime('%H:%M:%S')}  {message}")


def fetch(run_id):
    return requests.get(f"{API}/runs/{run_id}/", timeout=10).json()


def compose(*args, timeout=180):
    return subprocess.run(
        ["docker", "compose", *args], capture_output=True, text=True, timeout=timeout
    )


def wait_for_api(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get("http://localhost:8000/healthz", timeout=3)
            return True
        except requests.RequestException:
            time.sleep(1)
    return False


try:
    requests.get(f"{API}/workflows/", timeout=5)
except requests.RequestException:
    print("Cannot reach the API. Start it with: docker compose up -d --scale worker=3")
    sys.exit(1)


print()
rule("=")
print("CASCADE WALKTHROUGH 6 - A DURABLE WAIT")
rule("=")
print()
print("  Workflows routinely need to pause. Wait for a health check to settle,")
print("  wait out a cooldown, wait three days for a probation period to end.")
print()
print("  A step that called sleep() would pin a worker for the whole duration")
print("  and lose the wait entirely on the next deploy. Cascade does not sleep.")


# ---------------------------------------------------------------------------
rule("=")
print("STEP 1 - start a workflow with a 45 second wait")
rule("=")

run = requests.post(f"{API}/runs/", json={"workflow": "demo_wait"}, timeout=10).json()
run_id = run["id"]
stamp(f"run {run_id[:8]} started")

time.sleep(6)
detail = fetch(run_id)

print()
for event in detail["events"]:
    extra = ""
    if event["type"] == "TIMER_SET":
        extra = f"   resume at {event['payload'].get('resume_at', '')[11:19]}"
    print(f"    [{event['seq']:>2}] {event['type']:<20} {event['step_id']}{extra}")

print()
print(f"  run status: {detail['status']}")
print()
print("  WAITING, not RUNNING. A run parked on a three-day timer looks exactly")
print("  like a wedged one otherwise, and someone eventually 'fixes' it by hand.")


# ---------------------------------------------------------------------------
print()
rule("=")
print("STEP 2 - the wait, as it actually exists")
rule("=")
print()

for task in detail["tasks"]:
    if task["status"] == "READY":
        print(f"    step       : {task['step_id']}")
        print(f"    status     : {task['status']}")
        print(f"    run_after  : {task['run_after']}")
        print(f"    attempt    : {task['attempt']}")

print()
print("  That is the entire wait. One row in the same tasks table that holds")
print("  ready work and backoff retries - the only difference is a run_after")
print("  in the future.")
print()
print("  No scheduler process. No timer service. No Celery beat. Nothing is")
print("  counting down anywhere, so there is nothing to lose.")


# ---------------------------------------------------------------------------
print()
rule("=")
print("STEP 3 - destroy everything")
rule("=")
print()
print("  Not a restart. 'docker compose down' removes every container - web,")
print("  all three workers, the reaper, the dispatcher - and the network too.")
print("  Only the database volume survives, as it would across a real deploy.")
print()

result = compose("down")
if result.returncode != 0:
    print(f"  docker compose down failed: {result.stderr.strip()[:300]}")
    sys.exit(1)

stamp("every process is gone")
time.sleep(3)

print()
print("  Anything holding this workflow in memory has just been destroyed.")


# ---------------------------------------------------------------------------
print()
rule("=")
print("STEP 4 - bring it back")
rule("=")
print()

result = compose("up", "-d", "--scale", "worker=3", timeout=300)
if result.returncode != 0:
    print(f"  docker compose up failed: {result.stderr.strip()[:300]}")
    sys.exit(1)

stamp("containers up, waiting for the API")

if not wait_for_api():
    print("  API did not come back in time")
    sys.exit(1)

stamp("API is back - these are brand new processes that have never seen this run")

print()
print("  Waiting for the timer to come due...")

deadline = time.time() + 120
seen = len(fetch(run_id)["events"])

while time.time() < deadline:
    detail = fetch(run_id)
    for event in detail["events"][seen:]:
        stamp(f"[{event['seq']:>2}] {event['type']:<20} {event['step_id']}")
    seen = len(detail["events"])
    if detail["status"] in {"SUCCEEDED", "FAILED"}:
        break
    time.sleep(0.5)

final = fetch(run_id)


# ---------------------------------------------------------------------------
print()
rule("=")
print("RESULT")
rule("=")
print()
print(f"  run status : {final['status']}")

after = final["context"]["steps"].get("after", {}).get("output", {})
print(f"  final step : {after.get('stdout', '').strip()!r}")
print()
print("  A worker that did not exist when this workflow started claimed a row")
print("  whose run_after had come due, replayed the log, saw the step was")
print("  WAITING, and fired the timer.")
print()
print("  Nothing was handed over, because nothing was ever held. The same")
print("  property that survives kill -9 survives a full redeploy - and it")
print("  would survive restoring the database onto a different machine.")
print()
print("  Change duration_s to 259200 and this is a three-day wait, with no")
print("  other change to the system.")
rule("=")
print()
