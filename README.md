# Cascade

**A durable, graph-triggered workflow execution engine.**

Nutanix Hackathon, IIT Guwahati — Problem Statement #4 (Workflow Execution Engine).

---

Running a list of steps is easy. Running them *reliably* is the entire problem. Cascade
takes a JSON workflow, executes shell and REST steps, and survives the things that break
naive runners:

- **The worker dies mid-run** → run state is an append-only event log; another worker
  replays it and resumes at the exact step.
- **A step is retried after a timeout** → per-attempt idempotency keys, so side effects
  never fire twice.
- **Step 7 of 9 fails** → saga compensation unwinds the completed steps in reverse,
  leaving a consistent system rather than a half-mutated one.
- **A step must wait three days** → durable timers that survive a full restart.
- **Ten thousand concurrent runs** → N stateless workers claiming work with
  `SELECT … FOR UPDATE SKIP LOCKED`, with lease expiry for crash recovery.

On top of the engine sits a **typed entity graph**: workflows aren't only submitted via
API, they *fire* when the graph changes. Declarative predicates over entity mutations
trigger runs automatically — so a cluster node going unhealthy, or an employee changing
department, kicks off the right workflow on its own.

## Why it matters

The same engine drives two very different worlds without a line of code changing between
them:

- **Rolling cluster upgrade** — pre-flight, drain, upgrade, verify, node by node. When a
  health check fails, it rolls back cleanly.
- **Employee onboarding** — a department change cascades into device assignment, app
  provisioning and notifications.

That's the point: it's a platform, not a pipeline.

## Stack

Django · Django REST Framework · PostgreSQL · vanilla JS dashboard

Postgres is the state store, the event log, the work queue *and* the durable timer.
No Kafka, no Celery, no separate scheduler.

## Running it

```bash
docker compose up -d --scale worker=3
docker compose exec web python manage.py seed_demo
docker compose exec web python manage.py seed_graph
docker compose exec web python manage.py seed_joiner
```

Then open `http://localhost:8000/` for the live view, or run the whole story
end to end:

```bash
python scripts/demo.py
```

Individual mechanisms have their own walkthroughs in [`scripts/`](scripts/) —
replay, the worker loop, crash recovery, rollback, graph triggers, durable
waits and tenant isolation, each runnable on its own.

The test suite needs no database:

```bash
python -m unittest discover -s . -p "test_*.py"
```

## Documentation

- [`DESIGN.md`](DESIGN.md) — architecture, data model, execution algorithms, Django
  mapping, multi-tenancy
- [`BUILD_LOG.md`](BUILD_LOG.md) — what each part does, why it is shaped that way,
  and what was verified

## Team

Vikas Kathuria || Kushal Tiwari || Dakshin Gautham

