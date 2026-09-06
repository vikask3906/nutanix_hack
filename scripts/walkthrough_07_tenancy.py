"""Walkthrough 7: two companies, one engine, one database.

Needs the stack running, seeded, and a second tenant:

    docker compose up -d --scale worker=3
    docker compose exec web python manage.py seed_demo
    docker compose exec web python manage.py seed_graph
    docker compose exec web python manage.py seed_tenant

Then:

    .venv\\Scripts\\python.exe scripts/walkthrough_07_tenancy.py

Scenes:
    1. The same URL returns different worlds
    2. The same employee id is a different person
    3. A change in one tenant fires only that tenant's triggers
    4. The workers are shared; the data is not
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


def get(path, tenant):
    return requests.get(f"{API}{path}", headers={"X-Tenant": tenant}, timeout=10).json()


def patch(path, tenant, attrs):
    return requests.patch(
        f"{API}{path}", headers={"X-Tenant": tenant}, json={"attrs": attrs}, timeout=10
    ).json()


def rows(payload):
    return payload.get("results", payload) if isinstance(payload, dict) else payload


try:
    if not get("/workflows/", "acme").get("count"):
        print("The 'acme' tenant has no workflows.")
        print("Run: docker compose exec web python manage.py seed_tenant")
        sys.exit(1)
except requests.RequestException:
    print("Cannot reach the API. Start it with: docker compose up -d --scale worker=3")
    sys.exit(1)


print()
rule("=")
print("CASCADE WALKTHROUGH 7 - MULTI-TENANCY")
rule("=")
print()
print("  Rippling runs one Employee Graph for thousands of companies. The hard")
print("  part is not storing a tenant column - it is making sure a forgotten")
print("  filter is a loud error rather than one customer reading another's data.")
print()
print("  In Cascade the SCOPED manager is the default:")
print()
print("      Model.objects      raises if no tenant context is set")
print("      Model.all_tenants  the deliberate, greppable escape hatch")


# ---------------------------------------------------------------------------
scene(1, "The same URL, two different worlds")

for tenant in ("default", "acme"):
    data = get("/workflows/", tenant)
    names = [w["name"] for w in rows(data)]
    print()
    print(f"  GET /api/workflows/   X-Tenant: {tenant}")
    print(f"    {len(names)} workflows: {', '.join(names)}")

print()
print("  Note both tenants have a workflow called 'employee_onboarding'.")
print("  Same name, different definitions, no collision - the uniqueness")
print("  constraint is on (tenant, name, version), not on name.")


# ---------------------------------------------------------------------------
scene(2, "The same employee id, two different people")

for tenant in ("default", "acme"):
    e = get("/entities/employee/e_42/", tenant)
    print()
    print(f"  GET /api/entities/employee/e_42/   X-Tenant: {tenant}")
    print(f"    {e['attrs'].get('name')} - {e['attrs'].get('department')}, "
          f"{e['attrs'].get('geo')}")

print()
print("  Identical URL, identical external id, unrelated rows. External ids")
print("  come from a customer's own systems, so two customers WILL collide -")
print("  planning for that is the difference between a platform and a demo.")


# ---------------------------------------------------------------------------
scene(3, "A change in one tenant fires only that tenant's triggers")

before = {t: len(rows(get("/runs/?workflow=employee_onboarding", t))) for t in ("default", "acme")}
print()
print(f"  onboarding runs before   default: {before['default']}   acme: {before['acme']}")

# Reset then move, so this is re-runnable.
patch("/entities/employee/e_99/", "acme", {"department": "Sales"})
time.sleep(2)
print()
print("  PATCH /api/entities/employee/e_99/  {\"department\": \"Engineering\"}")
print("  X-Tenant: acme")
patch("/entities/employee/e_99/", "acme", {"department": "Engineering"})

deadline = time.time() + 45
while time.time() < deadline:
    after = {t: len(rows(get("/runs/?workflow=employee_onboarding", t))) for t in ("default", "acme")}
    if after["acme"] > before["acme"]:
        break
    time.sleep(0.5)

print()
print(f"  onboarding runs after    default: {after['default']}   acme: {after['acme']}")
print()
print(f"  acme gained a run  : {after['acme'] > before['acme']}")
print(f"  default unchanged  : {after['default'] == before['default']}")

latest = rows(get("/runs/?workflow=employee_onboarding", "acme"))[0]
print()
print(f"    status : {latest['status']}")
print(f"    entity : {latest['entity_ref']}")
print(f"    input  : {latest['input']}")
print()
print("  The input came from ACME's trigger template, not the default")
print("  tenant's - note the 'company' key, which the default tenant's")
print("  onboarding workflow does not have.")


# ---------------------------------------------------------------------------
scene(4, "The workers are shared; the data is not")

print()
print("  Exactly four places in the codebase query across tenants, and each")
print("  one is deliberate:")
print()
print("    claim_task           a worker takes whatever work is due, for anyone")
print("    reap_expired_leases  the reaper does not care whose lease expired")
print("    dispatch_one         one shared outbox")
print("    the admin            an operator is explicitly looking across tenants")
print()
print("  Each enters the row's tenant context immediately, so everything")
print("  downstream is scoped again. The context is reset on the way out - a")
print("  worker loops over tasks from many tenants, and leaking one into the")
print("  next is the exact bug this design exists to prevent.")
print()
print("  Foreign-key traversal deliberately is NOT scoped (base_manager_name),")
print("  or a worker holding a task could not load its own run.")

print()
rule("=")
print("  One database. One engine. Three shared workers.")
print("  Two companies that cannot see each other.")
rule("=")
print()
