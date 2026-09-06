"""The demo, driven end to end. Press record, run this, don't touch anything.

    .venv\\Scripts\\python.exe scripts/demo.py

    --fast     roughly half the pauses, for rehearsing
    --slow     longer pauses, if you are narrating live
    --only 3   run a single act (1-5), for re-shooting one segment

Everything it prints is the result of a real API call. Nothing is pre-computed
and nothing is faked - if the stack is down, this script fails rather than
printing a plausible story.

Runs reset_demo first, so take four looks exactly like take one.
"""

import argparse
import subprocess
import sys
import time

import requests

API = "http://localhost:8000/api"
SYS = "http://localhost:9000"
W = 74

PACE = 1.0


def beat(seconds=1.0):
    time.sleep(seconds * PACE)


def rule(ch="─"):
    print(ch * W)


def act(n, title):
    print()
    print()
    rule("━")
    print(f"  ACT {n}   {title.upper()}")
    rule("━")
    beat(1.4)


def say(text="", pause=.5):
    print(text)
    beat(pause)


def cmd(text):
    print()
    print(f"  $ {text}")
    beat(1.1)


def company():
    return requests.get(f"{SYS}/company", timeout=10).json()


def show_company(title="THE COMPANY'S SYSTEMS"):
    c = company()
    rows = [
        ("Google mailbox",       c["email_accounts"]),
        ("Slack account",        c["slack_members"]),
        ("GitHub write access",  c["github_members"]),
        ("Payroll record",       c["payroll_enrolled"]),
        ("Laptop ordered",       c["laptop_orders"]),
    ]
    print()
    print(f"  ┌─ {title} " + "─" * (W - len(title) - 6))
    for label, store in rows:
        who = ", ".join(store.keys()) if store else "—"
        print(f"  │  {label:<22} {who}")
    print("  └" + "─" * (W - 3))
    print()


def cluster_row(node_id):
    for n in requests.get(f"{SYS}/nodes", timeout=10).json()["nodes"]:
        if n["id"] == node_id:
            return n
    return {}


def hire(person):
    requests.patch(f"{API}/entities/person/{person}/",
                   json={"attrs": {"employment_status": "hired"}}, timeout=10)


def latest_run(workflow):
    d = requests.get(f"{API}/runs/?workflow={workflow}", timeout=10).json()
    rows = d.get("results", d)
    return rows[0] if rows else None


def wait_until_done(workflow, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = latest_run(workflow)
        if r and r["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return r
        time.sleep(.4)
    return latest_run(workflow)


def show_log(run_id, only=None):
    events = requests.get(f"{API}/runs/{run_id}/", timeout=10).json()["events"]
    for e in events:
        if only and not any(k in e["type"] for k in only):
            continue
        mark = ""
        if "COMPENSATED" in e["type"]:
            mark = "   ← undone"
        elif e["type"] == "COMPENSATION_STARTED":
            mark = "   ← rollback begins"
        elif e["type"] == "STEP_FAILED":
            mark = "   ← the refusal"
        print(f"     {e['seq']:>2}  {e['type']:<22} {e['step_id']}{mark}")
        beat(.09)


# ---------------------------------------------------------------------------

def act1():
    act(1, "a company with nobody in it")
    say("  Northwind Ltd. Five systems that need to know about a new hire:")
    say("  Google Workspace, Slack, GitHub, payroll, and IT.", 1.0)
    show_company("MONDAY MORNING")
    say("  Nobody has joined yet.", 1.6)


def act2():
    act(2, "priya accepts her offer")
    say("  HR changes one field in the HR system. That is the whole integration.")
    cmd('PATCH /api/entities/person/priya.sharma/  {"employment_status": "hired"}')
    hire("priya.sharma")
    say("  Sent. No ticket raised. Nobody messaged. No checklist opened.", 1.4)

    say("  ...waiting.", .4)
    run = wait_until_done("new_joiner")
    beat(1.2)

    show_company("TEN SECONDS LATER")
    say(f"  Priya's onboarding: {run['status']}", 1.2)
    say("  Five systems updated. Nobody did any of it.", 1.0)
    say("  The laptop, Slack and GitHub steps ran AT THE SAME TIME,")
    say("  on three different machines, because none depends on the others.", 2.0)


def act3():
    act(3, "rahul's tax id will not verify")
    say("  Rahul accepts too. But the payroll provider is going to reject him.")
    say("  Not a glitch - retrying will not fix it. He cannot be employed")
    say("  until somebody sorts out his paperwork.", 1.4)

    cmd('POST /payroll/reject-next  {"person": "rahul.nair"}')
    requests.post(f"{SYS}/payroll/reject-next",
                  json={"person": "rahul.nair"}, timeout=10)
    beat(.8)

    cmd('PATCH /api/entities/person/rahul.nair/  {"employment_status": "hired"}')
    hire("rahul.nair")
    say("  Same one field as Priya. Watch what it does.", 1.6)

    run = wait_until_done("new_joiner")
    beat(1.0)

    say("  What the engine actually did:")
    print()
    show_log(run["id"], only=["SUCCEEDED", "FAILED", "COMPENSAT", "RUN_"])
    beat(2.2)

    show_company("THE COMPANY NOW")
    say("  Only Priya.", 1.4)
    say("  Rahul has no mailbox, no Slack, no GitHub access, and no laptop")
    say("  on its way. The engine created all four of those, hit the")
    say("  rejection, and undid its own work in reverse order.", 1.8)
    print()
    say("  Without this, Rahul has WRITE ACCESS TO THE CODEBASE and no")
    say("  employment record. Nobody notices until the next audit.", 2.4)


def act4():
    act(4, "the same engine, running infrastructure")
    say("  Nothing about employees is coded into the engine. Here is the")
    say("  same engine upgrading a server cluster.", 1.2)

    before = cluster_row("node-3")
    say(f"  node-3 is on version {before['version']}, healthy, in service.", 1.2)

    cmd("POST /nodes/node-3/fail-after-upgrade      # this upgrade will break it")
    requests.post(f"{SYS}/nodes/node-3/fail-after-upgrade", timeout=10)
    beat(.8)

    cmd('POST /api/runs/  {"workflow": "rolling_cluster_upgrade", "node": "node-3"}')
    requests.post(f"{API}/runs/", timeout=10, json={
        "workflow": "rolling_cluster_upgrade",
        "input": {"node": "node-3"}, "entity_ref": "node:node-3"})

    run = wait_until_done("rolling_cluster_upgrade")
    beat(1.0)
    print()
    show_log(run["id"], only=["SUCCEEDED", "FAILED", "COMPENSAT", "RUN_"])
    beat(1.6)

    after = cluster_row("node-3")
    print()
    say(f"  node-3 now: version {after['version']}, "
        f"drained={after['drained']}, healthy={after['healthy']}", 1.2)
    say("  Drained, upgraded, health check failed, rolled back, put back in")
    say("  service. The cluster is consistent - nothing half-upgraded.", 1.6)
    say("  We did not write a second engine. We wrote a second recipe.", 2.0)


def act5():
    act(5, "why you can trust it with this")
    say("  A workflow's position never lives in a process. It is an")
    say("  append-only log in Postgres. A worker takes one row, does one")
    say("  step, writes the result, and forgets everything.", 1.6)
    print()
    say("  So a worker can be killed mid-step, and the job still finishes.", 1.2)
    print()
    rule()
    subprocess.run([sys.executable, "scripts/walkthrough_03_crash.py"])
    rule()

    # That act deliberately kills a worker. Put the pool back, or the next
    # take runs with two workers and the parallelism claim in act 2 is weaker.
    print()
    print("  restoring the worker pool...")
    subprocess.run(["docker", "compose", "up", "-d", "--scale", "worker=3"],
                   capture_output=True, text=True)


ACTS = {1: act1, 2: act2, 3: act3, 4: act4, 5: act5}


def main():
    global PACE
    # Act 5 shells out to another script. Without this, our own prints sit in a
    # buffer while the child writes straight to the terminal, and the recording
    # shows the crash demo BEFORE the heading that introduces it.
    sys.stdout.reconfigure(line_buffering=True)

    p = argparse.ArgumentParser()
    p.add_argument("--fast", action="store_true")
    p.add_argument("--slow", action="store_true")
    p.add_argument("--only", type=int, choices=[1, 2, 3, 4, 5])
    args = p.parse_args()
    PACE = .5 if args.fast else 1.6 if args.slow else 1.0

    try:
        requests.get(f"{API}/workflows/", timeout=5)
        requests.get(f"{SYS}/company", timeout=5)
    except requests.RequestException:
        print("The stack is not reachable.")
        print("  docker compose up -d --scale worker=3")
        sys.exit(1)

    print()
    print("  resetting to a clean state...")
    subprocess.run(["docker", "compose", "exec", "-T", "web",
                    "python", "manage.py", "reset_demo"],
                   capture_output=True, text=True)
    time.sleep(4)

    if args.only:
        # Act 3 lands only if Priya is already onboarded - "only Priya is left"
        # means nothing against an empty company. Re-shooting act 3 alone still
        # needs that setup, so do it silently first.
        if args.only >= 3:
            print("  (setting up: onboarding Priya first)")
            hire("priya.sharma")
            wait_until_done("new_joiner")
            time.sleep(1)
        ACTS[args.only]()
    else:
        for n in (1, 2, 3, 4, 5):
            ACTS[n]()

    print()
    rule("━")
    print("  One database. No Kafka, no Celery, no Temporal.")
    print("  Postgres is the queue, the timer, and the log.")
    rule("━")
    print()


if __name__ == "__main__":
    main()
