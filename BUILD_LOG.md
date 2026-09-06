# Build log

A running account of what each block adds, why it exists, and how to watch it work.
Read alongside [`DESIGN.md`](DESIGN.md) (the target architecture) and
[`PLAN.md`](PLAN.md) (the schedule).

Each block ends with a **checkpoint** — a concrete thing you can run that proves the
block is done. Don't move on until it's true.

| Block | What it adds | Status |
|---|---|---|
| 0 | Django scaffold, data model, worker stub | done |
| 1 | Replay and DAG readiness — the engine's brain | done |
| 2 | The worker loop, step plugins, REST API | done |
| 3 | Retries, backoff, the lease reaper | next |
| 4 | Parallel fan-out, durable timers | |
| 5 | Saga compensation | |
| 6 | Entity graph, outbox, triggers | |
| 7 | Demo scenarios, fake node services, SSE | |

---

## Block 0 — Scaffold and data model

### What was added

A Django project (`cascade/`) and three apps, split by responsibility rather than by
layer:

- **`core/`** — `Tenant`, plus the `TenantScopedModel` and `TimestampedModel` abstract
  bases every other model inherits from.
- **`engine/`** — the four tables that *are* the execution engine: `WorkflowDef`,
  `Run`, `RunEvent`, `Task`.
- **`graph/`** — the entity graph: `Entity`, `EntityEdge`, `EntityChange`, `Trigger`.

Plus `docker-compose.yml` (Postgres + web + scalable worker), the `/healthz` endpoint,
Django admin registered over everything, and a `run_worker` management command stub.

### Why it's shaped this way

**Three apps, not one.** The engine knows nothing about employees or clusters; the
graph knows nothing about task leases. That boundary is what lets the same engine drive
a cluster upgrade and an employee onboarding without either knowing about the other.

**`UNIQUE(run_id, seq)` on `RunEvent`.** The most important line in the schema. Two
workers racing to advance the same run both attempt to write sequence number N;
Postgres lets exactly one win and the loser retries. That's optimistic concurrency
control for free — no distributed lock, no consensus protocol, no lock service.

**One `Task` table for three concepts.** A step ready now, a step retrying in 4
seconds, and a step sleeping for 3 days are all one row with a different `run_after`.
This is why Cascade needs no scheduler process, no Celery beat, and no timer service.

**`Run.context` is a cache, not state.** It's a materialised fold of the run's events.
You could wipe that column across the whole table and lose nothing. Holding this line
strictly is what makes crash recovery fall out for free rather than being a feature you
have to build.

**Postgres on host port 5433.** A native Postgres already owns 5432 on the dev machine.
Containers still talk to each other on `db:5432` internally.

### How to see it

```bash
docker compose up --build --scale worker=3
```

```bash
curl http://localhost:8000/healthz
```

Expect `{"status": "ok", "database": "ok"}` — that one response proves Django booted,
migrations applied, and Postgres is reachable.

```bash
docker compose exec web python manage.py createsuperuser
```

Then browse `http://localhost:8000/admin`. Every table is there. From Block 2 onward
this is also your debugger: open a run and read its event log inline.

### Checkpoint

`docker compose up` gives a live database with every table, and three workers announce
themselves with distinct identities in the logs.

---

## Block 1 — Replay: the engine's brain

### What was added

Two modules, both **pure Python with zero Django imports**:

- **`engine/spec.py`** — workflow spec validation and DAG analysis. Duplicate ids,
  dangling dependencies, cycle detection (naming the actual cycle), retry and
  compensate block shapes, topological ordering.
- **`engine/replay.py`** — `replay(events) -> context`, `ready_steps(spec, context)`,
  `compensation_order(spec, context)`, `next_run_state(spec, context)`.

Plus 45 unit tests and a walkthrough script.

### Why it's shaped this way

**The contract that everything rests on:**

> `replay(events)` is a pure fold. Same events in, same context out. Always.
> No clock, no randomness, no network, no reads of anything outside the list passed in.

Because of that, any worker on any machine at any time can reconstruct the exact state
of a run from its log alone. There is no handover between workers, no shared memory, no
sticky sessions. A worker that dies mid-step took nothing with it, because it never
held anything the log didn't already contain.

If you're ever tempted to put something in the context that can't be derived from the
events — a timestamp from the clock, a random id, a cached lookup — **stop**. That is
the exact moment the crash-recovery guarantee breaks.

**No Django imports, deliberately.** Not stylistic. It means the core logic tests in 5
milliseconds with no database and no Docker, and it makes the point that the engine's
intelligence is independent of its plumbing. `apply_event` is the single place that
knows how each event type changes state; adding a new event type means adding one
branch there and nowhere else.

**`ready_steps` recomputes from scratch every time.** It doesn't walk a plan or track a
cursor. It asks "which steps have every dependency satisfied and haven't been claimed?"
against state that came from the log. That's why two workers can ask this question
concurrently and neither needs to know the other exists.

**Compensation uses reverse *completion* order, not reverse spec order.** With parallel
branches, the order things actually finished in is the only order that's safe to undo.
`completion_order` is accumulated during replay for exactly this.

**Unknown event types are ignored, not fatal.** An older worker replaying a log written
by a newer one should degrade, not crash.

### How to see it

```bash
.venv\Scripts\python.exe scripts/walkthrough_01_replay.py
```

Three scenes: state accumulating event by event, a second worker resuming a
half-finished log with zero handover, and a failure unwinding in reverse order.

Run the tests:

```bash
.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py"
```

45 tests, no database required. The ones worth reading:

- `ReplayPurityTests` — determinism, order-independence, non-mutation. These are the
  guarantees, written down as assertions.
- `CrashRecoveryTests` — a worker dies mid-run; a fresh one reaches exactly the right
  conclusion from the log alone.
- `CompensationTests` — rollback order follows actual finish order.

### Checkpoint

You can hand `replay()` a partial event log and it tells you precisely which step runs
next. Shuffle the events and the answer doesn't change.

### What this unlocks

Block 2's worker loop is now mostly plumbing: claim a task, call `replay`, execute one
step, append an event, call `ready_steps`, insert the next tasks. The hard thinking is
already done and already tested.

---

## Block 2 — The worker loop

### What was added

- **`engine/expressions.py`** — `{{ input.x }}` and `{{ steps.a.output.b }}` resolution.
- **`engine/steps/`** — the plugin contract and registry, plus `noop`, `shell`, `http`.
- **`engine/service.py`** — every transactional operation: append event, enqueue,
  claim, start run, sync projection.
- **`engine/worker.py`** — claim, replay, execute, record, enqueue next.
- **`engine/views.py` + `serializers.py` + `urls.py`** — the REST API.
- **`manage.py seed_demo`** — three demo workflows that need no external services.

### Why it is shaped this way

**The transaction rule.** Claim in one short transaction, execute *outside any
transaction*, record in another. Executing inside the claim transaction holds a row
lock across network I/O, and with several workers that deadlocks within minutes. If a
worker dies between claiming and recording, the lease expires and Block 3's reaper
recovers the task — which is exactly why long transactions are unnecessary.

**Optimistic sequence allocation.** `append_event` reads `last_seq`, tries to write
`last_seq + 1`, and lets `UNIQUE(run_id, seq)` arbitrate. On `IntegrityError` it
re-reads and retries. That is the concurrency control for the whole engine: no advisory
lock, no lock service, no consensus — one unique index.

**Enqueue collisions are the design working, not an error.** When two workers finish
parallel branches at the same instant, both compute that the join step is ready and
both try to enqueue it. The unique idempotency key means exactly one row is created and
`enqueue_step` returns `None` for the loser. Silently correct.

**The API executes nothing.** `POST /api/runs/` writes rows and returns — measured at
159 ms, with `last_seq: 2`. A worker picks the first task up within its poll interval.
That separation is why a 3-second workflow and a 3-day workflow are the same code path.

**`shell=False` and `shlex.split`.** Step configs are rendered from run input, which in
production comes from outside. Handing that to a shell is a command injection. Note
that this is *not* a sandbox — real isolation means a container per step. Do not
describe it as sandboxed.

**Whole-string expressions preserve type.** `"{{ input.count }}"` yields `3`, while
`"node-{{ input.count }}"` yields `"node-3"`. Without that rule every templated value
silently becomes a string and steps expecting numbers break.

**Pagination on list endpoints.** Runs accumulate without bound; an unpaginated list is
a slow outage waiting to happen once a demo has been running an hour.

### How to see it

```bash
docker compose exec web python manage.py seed_demo
```

```bash
.venv\Scripts\python.exe scripts/walkthrough_02_worker.py
```

Then watch which worker did what:

```bash
docker compose logs worker | grep claim
```

### Verified

- **Linear run**: `POST` returned in 159 ms with `status: RUNNING`; workers drove it to
  `SUCCEEDED` through 14 events.
- **Templating across steps**: `fetch` produced `{"records": 3}`; `transform`'s shell
  command rendered to `transformed 3 records` — resolved from state rebuilt from the
  log, not handed over by the previous worker.
- **Real parallelism**: the three diamond branches ran on three *different* workers with
  overlapping timestamps (`58.277-00.299`, `58.288-00.310`, `58.796-00.815`). 3.1s wall
  clock for three 2-second branches. `join` ran only after all three landed.
- **Failure isolation**: `risky` failed, run went `FAILED`, and `never_runs` was never
  scheduled — no task row was ever created for it. Nothing had to explicitly cancel it.
- 60 unit tests still pass with no database.

### Checkpoint

`POST /api/runs/` starts a run and three workers cooperatively drain it, with the
event log showing exactly what happened.

### Known gaps, closed in Block 3

- Every step failure is terminal. `retry` blocks are validated but not honoured yet.
- No lease heartbeat and no reaper, so a `kill -9` mid-step currently strands the task
  in `LEASED` forever. This is the next thing to fix, and it is the demo moment.
- `wait` is spec-valid but has no plugin — it becomes a durable timer in Block 4.
