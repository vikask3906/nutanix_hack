"""Walkthrough 5: the graph starts the workflows.

Needs the stack running:

    docker compose up -d --scale worker=3
    docker compose exec web python manage.py seed_demo
    docker compose exec web python manage.py seed_graph

Then:

    .venv\\Scripts\\python.exe scripts/walkthrough_05_triggers.py

Scenes:
    1. The graph and the rules over it
    2. Change one attribute - a workflow starts itself
    3. The guards: a no-op write and an unrelated edit fire nothing
    4. The same engine, a completely different entity type
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


def entity(entity_type, external_id):
    return requests.get(f"{API}/entities/{entity_type}/{external_id}/", timeout=10).json()


def patch(entity_type, external_id, attrs):
    return requests.patch(
        f"{API}/entities/{entity_type}/{external_id}/",
        json={"attrs": attrs},
        timeout=10,
    ).json()


def runs_for(workflow):
    data = requests.get(f"{API}/runs/?workflow={workflow}", timeout=10).json()
    return data.get("results", data)


def wait_for_new_run(workflow, before_count, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = runs_for(workflow)
        if len(current) > before_count:
            run = current[0]
            if run["status"] in {"SUCCEEDED", "FAILED"}:
                return run
        time.sleep(0.4)
    return None


try:
    requests.get(f"{API}/entities/", timeout=5)
except requests.RequestException:
    print("Cannot reach the API. Start it with: docker compose up -d --scale worker=3")
    sys.exit(1)

requests.post(f"{NODES}/reset", timeout=10)

# Put the graph back to its starting state so this script can be run over and
# over - which matters when you are recording it and want take four to look
# like take one. Moving e_42 back to Support fires nothing: the onboarding
# predicate only matches a move INTO Engineering.
patch("employee", "e_42", {"department": "Support", "title": "Support Engineer"})
patch("node", "node-3", {"status": "healthy"})
time.sleep(2)


print()
rule("=")
print("CASCADE WALKTHROUGH 5 - THE GRAPH TRIGGERS THE WORK")
rule("=")
print()
print("  Until now every run was started by a POST. Someone had to notice a")
print("  thing happened and ask for the workflow.")
print()
print("  Now the graph itself is the trigger.")


# ---------------------------------------------------------------------------
scene(1, "The graph, and the rules over it")

entities = requests.get(f"{API}/entities/", timeout=10).json()
print()
print("  entities:")
for item in entities:
    summary = {k: v for k, v in item["attrs"].items() if k in
               ("department", "geo", "status", "version")}
    print(f"    {item['ref']:<18} {summary}")

triggers = requests.get(f"{API}/triggers/", timeout=10).json()
triggers = triggers.get("results", triggers)
print()
print("  triggers:")
for trig in triggers:
    print(f"    {trig['name']!r}")
    print(f"        on       : {trig['entity_type']} changes")
    print(f"        reads as : {trig['reads_as']}")
    print(f"        runs     : {trig['workflow']}")
    print(f"        input    : {trig['input_template']}")


# ---------------------------------------------------------------------------
scene(2, "One attribute changes, and a workflow starts itself")

before = entity("employee", "e_42")
print()
print(f"  e_42 today: department={before['attrs']['department']}, "
      f"geo={before['attrs']['geo']}")

count_before = len(runs_for("employee_onboarding"))

print()
print("  PATCH /api/entities/employee/e_42/  {\"department\": \"Engineering\"}")
result = patch("employee", "e_42", {"department": "Engineering"})
print(f"    changed_keys: {result['changed_keys']}   version: {result['version']}")
print()
print("  That request did nothing but write two rows in one transaction:")
print("  the entity, and an outbox record of the change. It started no")
print("  workflow and called no other system.")

run = wait_for_new_run("employee_onboarding", count_before)

if run is None:
    print()
    print("  (no run appeared - is the dispatcher running? docker compose ps)")
else:
    print()
    print(f"  ...and a run appeared on its own:")
    print(f"    workflow : {run['workflow']}")
    print(f"    status   : {run['status']}")
    print(f"    entity   : {run['entity_ref']}")
    print(f"    source   : {run['trigger_source']}")
    print(f"    input    : {run['input']}")
    print()
    print("  The input was not supplied by anyone. The trigger's template")
    print("  {'employee': '{{ entity.external_id }}'} was rendered against the")
    print("  change, using the same expression engine step configs use.")

    accounts = requests.get(f"{NODES}/apps", timeout=10).json()["accounts"]
    devices = requests.get(f"{NODES}/devices", timeout=10).json()["devices"]
    print()
    print("  What actually happened in the downstream systems:")
    for app, holders in accounts.items():
        print(f"    {app:<12} accounts for {list(holders)}")
    print(f"    {'devices':<12} {devices}")
    print()
    print("  Three account provisions ran in parallel - they share a dependency")
    print("  on assign_device but not on each other, and the absence of a needs")
    print("  edge is what makes them concurrent.")


# ---------------------------------------------------------------------------
scene(3, "The guards")

print()
print("  A graph is fed by systems that re-send state. Two things must not")
print("  happen: a re-sync must not re-onboard everyone, and an unrelated")
print("  edit must not either.")

count = len(runs_for("employee_onboarding"))

print()
print("  (a) write department = Engineering again, the value it already has")
same = patch("employee", "e_42", {"department": "Engineering"})
print(f"      changed: {same['changed']}   changed_keys: {same['changed_keys']}")
print("      No attribute moved, so no outbox row exists to dispatch.")

print()
print("  (b) change an unrelated attribute")
other = patch("employee", "e_42", {"title": "Senior Engineer"})
print(f"      changed: {other['changed']}   changed_keys: {other['changed_keys']}")
print("      A change IS recorded - the log stays honest - but the predicate")
print("      says 'changed: [department]', so onboarding does not fire.")

time.sleep(6)
after = len(runs_for("employee_onboarding"))
print()
print(f"  onboarding runs before: {count}    after both writes: {after}")
print(f"  neither write re-onboarded anyone: {after == count}")
print()
print("  This is why the predicate needs 'changed' and not just 'match'.")
print("  'department is Engineering' stays true forever after the promotion,")
print("  so a match-only rule would fire on every subsequent save.")


# ---------------------------------------------------------------------------
scene(4, "The same engine, a completely different world")

print()
print("  The second trigger watches nodes, not employees, and starts the")
print("  cluster upgrade workflow. Same engine, same graph, same dispatcher.")
print("  Neither workflow knows the other exists.")

count_before = len(runs_for("rolling_cluster_upgrade"))

print()
print("  PATCH /api/entities/node/node-3/  {\"status\": \"unhealthy\"}")
patch("node", "node-3", {"status": "unhealthy"})

run = wait_for_new_run("rolling_cluster_upgrade", count_before)

if run is None:
    print()
    print("  (no run appeared - check the dispatcher)")
else:
    print()
    print(f"    workflow : {run['workflow']}")
    print(f"    status   : {run['status']}")
    print(f"    entity   : {run['entity_ref']}")
    print(f"    input    : {run['input']}")
    print()
    print("  The node trigger's template maps entity.external_id -> input.node,")
    print("  because that is the shape this workflow expects. One workflow can")
    print("  be driven by several entity types without knowing about any of them.")

print()
rule("=")
print("  Employee changes department -> device assigned, three accounts")
print("  provisioned, manager notified.")
print("  Node goes unhealthy   -> drained, upgraded, verified, or rolled back.")
print()
print("  One graph. One engine. Nobody polling anything.")
rule("=")
print()
