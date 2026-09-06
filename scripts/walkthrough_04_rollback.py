"""Walkthrough 4: a rolling cluster upgrade that goes wrong, and unwinds.

The headline demo. A real workflow acting on a real (fake) five-node cluster.

Needs the stack running:

    docker compose up -d --scale worker=3
    docker compose exec web python manage.py seed_demo

Then:

    .venv\\Scripts\\python.exe scripts/walkthrough_04_rollback.py

Scenes:
    1. Upgrade node-1 cleanly
    2. Arm node-3 so the upgrade breaks it, then upgrade it - watch the engine
       roll back in reverse order and leave the cluster consistent
    3. The cluster afterwards: nothing half-upgraded, nothing left drained
"""

import sys
import time

import requests

API = "http://localhost:8000/api"
NODES = "http://localhost:9000"
WIDTH = 78


def rule(char="-"):
    print(char * WIDTH)


def scene(number, title):
    print()
    rule("=")
    print(f"SCENE {number}: {title}")
    rule("=")


def cluster():
    return requests.get(f"{NODES}/nodes", timeout=10).json()["nodes"]


def show_cluster(highlight=()):
    print()
    print("    node      version   drained   healthy")
    for node in cluster():
        mark = " <--" if node["id"] in highlight else ""
        print(
            f"    {node['id']:<9} {node['version']:<9} "
            f"{str(node['drained']):<9} {str(node['healthy']):<8}{mark}"
        )


def upgrade(node_id):
    return requests.post(
        f"{API}/runs/",
        json={
            "workflow": "rolling_cluster_upgrade",
            "input": {"node": node_id},
            "entity_ref": f"node:{node_id}",
        },
        timeout=10,
    ).json()


def wait_for_terminal(run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = requests.get(f"{API}/runs/{run_id}/", timeout=10).json()
        if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return run
        time.sleep(0.3)
    raise TimeoutError(f"run {run_id} did not finish in {timeout}s")


def show_log(run):
    for event in run["events"]:
        marker = ""
        if event["type"] == "COMPENSATION_STARTED":
            marker = "   <-- rollback begins here"
        elif event["type"] == "STEP_COMPENSATED":
            marker = "   <-- undone"
        print(f"    [{event['seq']:>2}] {event['type']:<24} {event['step_id']}{marker}")


try:
    requests.get(f"{NODES}/nodes", timeout=5)
    requests.get(f"{API}/workflows/", timeout=5)
except requests.RequestException:
    print("Cannot reach the API or the node service.")
    print("Start them with: docker compose up -d --scale worker=3")
    sys.exit(1)


print()
rule("=")
print("CASCADE WALKTHROUGH 4 - ROLLING UPGRADE WITH ROLLBACK")
rule("=")

requests.post(f"{NODES}/reset", timeout=10)
print()
print("  Cluster reset. Five nodes, all on 7.0, all in service.")
show_cluster()

print()
print("  The workflow, per node:")
print("      preflight -> drain -> upgrade -> verify -> return_to_service")
print()
print("  'drain' and 'upgrade' each declare a compensating action:")
print("      drain    undone by  undrain")
print("      upgrade  undone by  rollback")
print()
print("  'verify' declares on_error: compensate.")


# ---------------------------------------------------------------------------
scene(1, "A clean upgrade - node-1")

run = wait_for_terminal(upgrade("node-1")["id"])
detail = requests.get(f"{API}/runs/{run['id']}/", timeout=10).json()

print()
show_log(detail)
print()
print(f"  status: {run['status']}")
show_cluster(highlight=("node-1",))
print()
print("  node-1 is on 7.1 and back in service. Nothing to undo.")


# ---------------------------------------------------------------------------
scene(2, "A bad upgrade - node-3")

print()
print("  Arming node-3: this upgrade will break it. The failure fires only")
print("  AFTER the version changes, so the pre-upgrade preflight still passes")
print("  and it is the post-upgrade verify that catches it - which is how a")
print("  bad upgrade actually behaves.")

requests.post(f"{NODES}/nodes/node-3/fail-after-upgrade", timeout=10)
print()
print("  armed. Upgrading node-3...")

run = wait_for_terminal(upgrade("node-3")["id"])
detail = requests.get(f"{API}/runs/{run['id']}/", timeout=10).json()

print()
show_log(detail)

print()
print(f"  status : {run['status']}")
print(f"  error  : {run['error']}")
print()
print("  Read the order of the two STEP_COMPENSATED events:")
print()
print("      completed:  preflight -> drain -> upgrade")
print("      undone:                  upgrade -> drain")
print()
print("  Reverse order of COMPLETION, not reverse order of the spec. With")
print("  parallel branches those differ, and only one of them is safe:")
print("  undoing a drain before undoing the upgrade that depended on it")
print("  leaves the node in a state nobody designed for.")
print()
print("  'preflight' is absent from the rollback - it succeeded, but it")
print("  declares no compensating action, so there is nothing to undo.")


# ---------------------------------------------------------------------------
scene(3, "The cluster afterwards")

show_cluster(highlight=("node-1", "node-3"))

state = {n["id"]: n for n in cluster()}
node3 = state["node-3"]

print()
print("  node-1  upgraded and in service   (the upgrade that worked)")
print("  node-3  back on 7.0, undrained, healthy   (the upgrade that did not)")
print()

consistent = (
    node3["version"] == "7.0"
    and not node3["drained"]
    and node3["healthy"]
)
print(f"  node-3 fully restored: {consistent}")
print()
print("  This is the difference a saga makes. Without compensation node-3")
print("  would be sitting on a broken 7.1, drained, out of the cluster, with")
print("  a FAILED run and an engineer paged at 3am to work out how far it got.")
print("  The event log would still tell them - but the machine already fixed it.")

print()
rule("=")
print("Next: Block 6 makes changes to the entity graph trigger these workflows")
print("by themselves, instead of someone POSTing a run per node.")
rule("=")
print()
