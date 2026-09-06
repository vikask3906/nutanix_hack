# Build log

A running account of what each block adds, why it exists, and how to watch it work.
Read alongside [`DESIGN.md`](DESIGN.md) (the target architecture) and
[`PLAN.md`](PLAN.md) (the schedule).

Each block ends with a **checkpoint** â€” a concrete thing you can run that proves the
block is done. Don't move on until it's true.

| Block | What it adds | Status |
|---|---|---|
| 0 | Django scaffold, data model, worker stub | done |
| 1 | Replay and DAG readiness â€” the engine's brain | done |
| 2 | The worker loop, step plugins, REST API | done |
| 3 | Retries, backoff, the lease reaper | done |
| 4 | Durable timers (parallel already works) | next |
| 5 | Saga compensation + fake cluster | done |
| 6 | Entity graph, outbox, triggers | next |
| 7 | SSE and the DAG UI | |

---

## Block 0 â€” Scaffold and data model

### What was added

A Django project (`cascade/`) and three apps, split by responsibility rather than by
layer:

- **`core/`** â€” `Tenant`, plus the `TenantScopedModel` and `TimestampedModel` abstract
  bases every other model inherits from.
- **`engine/`** â€” the four tables that *are* the execution engine: `WorkflowDef`,
  `Run`, `RunEvent`, `Task`.
- **`graph/`** â€” the entity graph: `Entity`, `EntityEdge`, `EntityChange`, `Trigger`.

Plus `docker-compose.yml` (Postgres + web + scalable worker), the `/healthz` endpoint,
Django admin registered over everything, and a `run_worker` management command stub.

### Why it's shaped this way

**Three apps, not one.** The engine knows nothing about employees or clusters; the
graph knows nothing about task leases. That boundary is what lets the same engine drive
a cluster upgrade and an employee onboarding without either knowing about the other.

**`UNIQUE(run_id, seq)` on `RunEvent`.** The most important line in the schema. Two
workers racing to advance the same run both attempt to write sequence number N;
Postgres lets exactly one win and the loser retries. That's optimistic concurrency
control for free â€” no distributed lock, no consensus protocol, no lock service.

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

Expect `{"status": "ok", "database": "ok"}` â€” that one response proves Django booted,
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

## Block 1 â€” Replay: the engine's brain

### What was added

Two modules, both **pure Python with zero Django imports**:

- **`engine/spec.py`** â€” workflow spec validation and DAG analysis. Duplicate ids,
  dangling dependencies, cycle detection (naming the actual cycle), retry and
  compensate block shapes, topological ordering.
- **`engine/replay.py`** â€” `replay(events) -> context`, `ready_steps(spec, context)`,
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
events â€” a timestamp from the clock, a random id, a cached lookup â€” **stop**. That is
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

- `ReplayPurityTests` â€” determinism, order-independence, non-mutation. These are the
  guarantees, written down as assertions.
- `CrashRecoveryTests` â€” a worker dies mid-run; a fresh one reaches exactly the right
  conclusion from the log alone.
- `CompensationTests` â€” rollback order follows actual finish order.

### Checkpoint

You can hand `replay()` a partial event log and it tells you precisely which step runs
next. Shuffle the events and the answer doesn't change.

### What this unlocks

Block 2's worker loop is now mostly plumbing: claim a task, call `replay`, execute one
step, append an event, call `ready_steps`, insert the next tasks. The hard thinking is
already done and already tested.

---

## Block 2 â€” The worker loop

### What was added

- **`engine/expressions.py`** â€” `{{ input.x }}` and `{{ steps.a.output.b }}` resolution.
- **`engine/steps/`** â€” the plugin contract and registry, plus `noop`, `shell`, `http`.
- **`engine/service.py`** â€” every transactional operation: append event, enqueue,
  claim, start run, sync projection.
- **`engine/worker.py`** â€” claim, replay, execute, record, enqueue next.
- **`engine/views.py` + `serializers.py` + `urls.py`** â€” the REST API.
- **`manage.py seed_demo`** â€” three demo workflows that need no external services.

### Why it is shaped this way

**The transaction rule.** Claim in one short transaction, execute *outside any
transaction*, record in another. Executing inside the claim transaction holds a row
lock across network I/O, and with several workers that deadlocks within minutes. If a
worker dies between claiming and recording, the lease expires and Block 3's reaper
recovers the task â€” which is exactly why long transactions are unnecessary.

**Optimistic sequence allocation.** `append_event` reads `last_seq`, tries to write
`last_seq + 1`, and lets `UNIQUE(run_id, seq)` arbitrate. On `IntegrityError` it
re-reads and retries. That is the concurrency control for the whole engine: no advisory
lock, no lock service, no consensus â€” one unique index.

**Enqueue collisions are the design working, not an error.** When two workers finish
parallel branches at the same instant, both compute that the join step is ready and
both try to enqueue it. The unique idempotency key means exactly one row is created and
`enqueue_step` returns `None` for the loser. Silently correct.

**The API executes nothing.** `POST /api/runs/` writes rows and returns â€” measured at
159 ms, with `last_seq: 2`. A worker picks the first task up within its poll interval.
That separation is why a 3-second workflow and a 3-day workflow are the same code path.

**`shell=False` and `shlex.split`.** Step configs are rendered from run input, which in
production comes from outside. Handing that to a shell is a command injection. Note
that this is *not* a sandbox â€” real isolation means a container per step. Do not
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
  command rendered to `transformed 3 records` â€” resolved from state rebuilt from the
  log, not handed over by the previous worker.
- **Real parallelism**: the three diamond branches ran on three *different* workers with
  overlapping timestamps (`58.277-00.299`, `58.288-00.310`, `58.796-00.815`). 3.1s wall
  clock for three 2-second branches. `join` ran only after all three landed.
- **Failure isolation**: `risky` failed, run went `FAILED`, and `never_runs` was never
  scheduled â€” no task row was ever created for it. Nothing had to explicitly cancel it.
- 60 unit tests still pass with no database.

### Checkpoint

`POST /api/runs/` starts a run and three workers cooperatively drain it, with the
event log showing exactly what happened.

### Known gaps, closed in Block 3

- Every step failure is terminal. `retry` blocks are validated but not honoured yet.
- No lease heartbeat and no reaper, so a `kill -9` mid-step currently strands the task
  in `LEASED` forever. This is the next thing to fix, and it is the demo moment.
- `wait` is spec-valid but has no plugin â€” it becomes a durable timer in Block 4.

---

## Block 3 â€” Retries, backoff, and the lease reaper

### What was added

- **`engine/retry.py`** â€” pure backoff computation: exponential, linear, fixed;
  capped; full jitter.
- **`engine/heartbeat.py`** â€” `LeaseHeartbeat`, a context manager that renews a lease
  in a background thread while a step runs.
- **`engine/service.py`** â€” `renew_lease`, `finish_task_if_owned`, `schedule_retry`,
  `reap_expired_leases`.
- **`manage.py run_reaper`** + a `reaper` compose service.
- Two more demo workflows: `demo_slow` (a 25s step) and `demo_flaky` (fails ~2 in 3).

### Why it is shaped this way

**Jitter is not a micro-optimisation.** An upstream service falls over and two hundred
steps fail in the same second. Without jitter every one computes an identical delay and
retries in lockstep â€” so the service comes back, gets hit by two hundred simultaneous
requests, and falls over again. Its own clients hold it down. Full jitter (uniform over
`[0, delay]`) spreads them smoothly.

**The heartbeat is what makes a short lease safe.** Lease is 10s here; the demo step
takes 25s. Without heartbeating, the lease would have to exceed the slowest step you
can imagine, and a genuinely crashed worker's task would sit unrecoverable that long.
With it, the lease stays short (fast recovery) and slow steps still work. The whole
failure detector is one rule:

> Heartbeat still arriving â†’ the worker is alive, however slow the step is.
> Heartbeat stopped â†’ the worker is gone; recover its work.

No health checks, no membership protocol, no consensus about who is up.

**Fencing â€” `finish_task_if_owned`.** This closes a real hole. Suppose a worker stalls
long enough for its lease to expire: a long GC pause, a hung syscall, a paused
container. The reaper reassigns the task, another worker runs the step, and *then* the
original wakes up and tries to record its result. Without a conditional
`WHERE lease_owner = me AND status = LEASED`, the log gains a second, stale outcome for
a step someone else already finished. Losing that race is not an error â€” it means the
system correctly decided we were gone, and the right response is to discard our work
silently.

**Recovered tasks get a new attempt number**, therefore a new idempotency key. A
downstream system can tell the retry apart from the original delivery.

**The poison-task guard.** A step that reliably *kills* the worker executing it would
otherwise be recovered forever, taking a worker down each time â€” a crash loop dressed
up as resilience. `MAX_TASK_ATTEMPTS` (10) is an absolute ceiling regardless of what the
step's retry block says.

**Threads close their own connection.** Each thread gets its own database connection;
leaking one per task exhausts Postgres' connection limit within a few hundred tasks.

### How to see it

```bash
.venv\Scripts\python.exe scripts/walkthrough_03_crash.py
```

Restore the pool afterwards:

```bash
docker compose up -d --scale worker=3
```

### Verified

**Retry:** `demo_flaky`'s `unreliable` step failed attempt 1, backed off a jittered
0.195s, succeeded on attempt 2, run `SUCCEEDED`.

**Crash recovery, end to end:**

| Time | Event |
|---|---|
| 11:40:05 | worker claims `long_task`, lease expires 06:10:15 |
| 11:40:11 | heartbeat renewed the lease to 06:10:21 |
| 11:40:12 | `docker kill --signal=KILL` â€” no cleanup, no handover |
| 11:40:23 | reaper: `recovered ... from dead worker 6ded273dbcdd:1:398f65cb (attempt 2)` |
| 11:40:23 | a different worker starts `long_task` attempt 2 |
| 11:40:48 | `RUN_SUCCEEDED` |

Detection to requeue: **11 seconds**. Total run: 43s. No human intervened, nothing was
resubmitted.

74 unit tests pass with no database.

### Checkpoint

SIGKILL a worker mid-step and the run still finishes. This is the demo â€” record it now.

### Known gaps, closed later

- `wait` is spec-valid but has no plugin â€” it becomes a durable timer in Block 4.
- `on_error: compensate` is validated and `compensation_order` is tested, but nothing
  executes compensating steps yet. That is Block 5.


---

## Block 5 — Saga compensation (and the fake cluster)

Taken before Block 4 deliberately: rollback is the differentiator and the second
half of the headline demo, while durable timers are a nice-to-have that could be cut.

### What was added

- **`fake_nodes/server.py`** — a five-node cluster, standard library only, so rollback
  is observable against real state rather than only in the event log.
- **`rolling_cluster_upgrade`** workflow: `preflight → drain → upgrade → verify →
  return_to_service`, with compensating actions on `drain` and `undrain`.
- **`service.start_compensation` / `enqueue_next_compensation`**.
- **`worker._run_compensation`** — executes a step's `compensate` block.
- `COMPENSATION_FAILED` event type, plus `on_error: continue` semantics.

### Why it is shaped this way

**Compensation is strictly sequential.** Forward steps fan out wherever the DAG allows;
rollback must not. `enqueue_next_compensation` queues exactly one step, and each
completed compensation triggers the next. Undoing a drain before undoing the upgrade
that depended on it leaves a node in a state nobody designed for.

**Reverse order of *completion*, not of the spec.** With parallel branches those differ,
and only one is safe. `completion_order` is accumulated during replay for this.

**No `STEP_SCHEDULED` event for compensation tasks.** That event sets a step's status to
`SCHEDULED` during replay — which would clobber the `SUCCEEDED` status of the very step
being unwound, and `SUCCEEDED` is what marks it as needing rollback. The task row is
enough. This was caught by reasoning about replay, not by a test, and it is the kind of
bug that only shows up as "rollback silently stopped after one step".

**Rollback can itself fail.** `COMPENSATION_FAILED` marks the step, removes it from the
queue so it is not retried forever, and sets `needs_manual_intervention`. The run ends
`FAILED` with the system partially unwound — the honest outcome rather than a hidden
one, and the log records exactly how far it got. Expect a judge to ask this.

**`on_error: continue`** records the failure but does not condemn the run. Dependents
are blocked anyway, since readiness requires `SUCCEEDED`, so the branch just stops.

**`fail-after-upgrade`, not `fail-next`.** The first version armed the *next* health
check — but `preflight` runs one before the upgrade and ate it, so the interesting path
never ran. Arming a failure that fires only once the version has changed is both
deterministic and more realistic: the upgrade is what broke the node.

### How to see it

```bash
.venv\Scripts\python.exe scripts/walkthrough_04_rollback.py
```

### Verified

Clean upgrade of node-1: `SUCCEEDED`, node on 7.1, back in service.

Bad upgrade of node-3:

```
[13] STEP_FAILED            verify
[14] COMPENSATION_STARTED   verify
[15] STEP_COMPENSATED       upgrade    <- undone first
[16] STEP_COMPENSATED       drain      <- undone second
[17] RUN_FAILED
```

Completed `preflight → drain → upgrade`; undone `upgrade → drain`. `preflight` is absent
because it declares no compensating action. Final state: node-3 back on **7.0,
undrained, healthy** — the cluster is consistent, not half-upgraded.

80 unit tests pass with no database.

### Gotcha worth remembering

Worker containers do not hot-reload. The source is volume-mounted and the Django dev
server restarts itself, but `manage.py run_worker` is a plain Python process — so after
touching engine code:

```bash
docker compose restart worker reaper
```

This cost a confusing debugging cycle where compensation appeared not to fire at all.

### Known gaps

- `wait` is spec-valid but has no plugin — durable timers are Block 4, still unbuilt.
- Runs are still started by hand, one POST per node. Block 6 makes graph changes
  trigger them.

