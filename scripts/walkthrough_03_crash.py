"""Walkthrough 3: kill a worker mid-step and watch the run finish anyway.

This is the demo. Everything else in Cascade exists to make this scene boring.

Needs the stack running:

    docker compose up -d --scale worker=3
    docker compose exec web python manage.py seed_demo

Then:

    .venv\\Scripts\\python.exe scripts/walkthrough_03_crash.py

What happens:
    1. Start a run whose middle step takes ~25 seconds
    2. Wait until a worker claims that step and is genuinely executing it
    3. SIGKILL that worker's container - no cleanup, no handover, no goodbye
    4. Watch the lease expire, the reaper requeue the task, and a different
       worker finish the run

Afterwards, restore the pool:

    docker compose up -d --scale worker=3
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


def leased_task(run):
    for task in run.get("tasks", []):
        if task["status"] == "LEASED":
            return task
    return None


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60
    )


try:
    requests.get(f"{API}/workflows/", timeout=5)
except requests.RequestException:
    print("Cannot reach the API. Start it with: docker compose up -d --scale worker=3")
    sys.exit(1)

if docker("version", "--format", "{{.Server.Version}}").returncode != 0:
    print("Docker is not reachable from this shell.")
    sys.exit(1)


print()
rule("=")
print("CASCADE WALKTHROUGH 3 - CRASH RECOVERY")
rule("=")
print()
print("  Lease        : 10s   (a worker's claim expires this long after its")
print("                        last heartbeat)")
print("  Heartbeat    : 3s    (renewed while a step is genuinely running)")
print("  Reaper sweep : 2s")
print()
print("  A short lease is only safe because workers heartbeat. Without it the")
print("  lease would have to exceed the slowest step, and a crashed worker's")
print("  task would sit unrecoverable for that long.")


# ---------------------------------------------------------------------------
rule("=")
print("STEP 1 - start a run with a long middle step")
rule("=")

run = requests.post(f"{API}/runs/", json={"workflow": "demo_slow"}, timeout=10).json()
run_id = run["id"]
started = time.time()
stamp(f"run {run_id[:8]} started")


# ---------------------------------------------------------------------------
print()
rule("=")
print("STEP 2 - wait for a worker to claim the long step")
rule("=")

victim = None
deadline = time.time() + 60

while time.time() < deadline:
    detail = fetch(run_id)
    task = leased_task(detail)
    if task and task["step_id"] == "long_task":
        victim = task
        break
    time.sleep(0.3)

if victim is None:
    print("  No worker claimed long_task within 60s - is the worker pool running?")
    sys.exit(1)

owner = victim["lease_owner"]
container = owner.split(":")[0]  # host:pid:random - the host IS the container id

stamp(f"long_task claimed by worker {owner}")
stamp(f"lease expires at {victim['lease_expires_at'][11:19]} unless renewed")
print()
print("  Letting it run for a few seconds so the heartbeat has to renew...")
time.sleep(6)

detail = fetch(run_id)
task = leased_task(detail)
if task:
    stamp(f"lease renewed, now expires {task['lease_expires_at'][11:19]} (heartbeat working)")


# ---------------------------------------------------------------------------
print()
rule("=")
print("STEP 3 - SIGKILL that worker")
rule("=")
print()
print("  Not a graceful shutdown. SIGKILL cannot be caught, so the process gets")
print("  no chance to finish, clean up, or tell anyone. Exactly like power loss.")
print()

result = docker("kill", "--signal=KILL", container)
if result.returncode != 0:
    print(f"  docker kill failed: {result.stderr.strip()}")
    sys.exit(1)

killed_at = time.time()
stamp(f"killed container {container}")
stamp("that worker was mid-step, holding the task, and said nothing")


# ---------------------------------------------------------------------------
print()
rule("=")
print("STEP 4 - watch the system notice and recover")
rule("=")
print()

seen = len(fetch(run_id)["events"])
recovered_at = None
deadline = time.time() + 120

while time.time() < deadline:
    detail = fetch(run_id)

    for event in detail["events"][seen:]:
        note = ""
        if event["payload"].get("reason") == "lease expired":
            note = f"  <- reaper: {event['payload'].get('dead_worker', '')[:12]} was gone"
            recovered_at = time.time()
        elif event["payload"].get("attempt"):
            note = f"  attempt {event['payload']['attempt']}"
        stamp(f"[{event['seq']:>2}] {event['type']:<22} {event['step_id']}{note}")

    seen = len(detail["events"])

    if detail["status"] in {"SUCCEEDED", "FAILED"}:
        break

    time.sleep(0.4)

final = fetch(run_id)


# ---------------------------------------------------------------------------
print()
rule("=")
print("RESULT")
rule("=")
print()
print(f"  run status     : {final['status']}")
print(f"  total wall time: {time.time() - started:.0f}s")
if recovered_at:
    print(f"  detect+requeue : {recovered_at - killed_at:.0f}s after the kill")
print()

long_task = final["context"]["steps"].get("long_task", {})
print(f"  long_task attempts : {long_task.get('attempts')}")
print(f"  long_task output   : {long_task.get('output', {}).get('stdout', '').strip()!r}")
print()
print("  The run completed. No human intervened, nothing was resubmitted, and")
print("  the worker that died handed nothing over - because it never held")
print("  anything the event log did not already contain.")
print()
print("  Note the attempt number went up. That means a fresh idempotency key,")
print("  so a downstream system can tell the retry apart from the original.")
print()
print("  Restore the pool with:  docker compose up -d --scale worker=3")
rule("=")
print()
