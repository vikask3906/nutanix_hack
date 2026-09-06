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
| 4 | Durable timers | done |
| 5 | Saga compensation + fake cluster | done |
| 6 | Entity graph, outbox, triggers | done |
| 7 | SSE and the live DAG UI | done |

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


---

## Block 6 — The entity graph triggers the work

The Rippling half. Until now every run was started by a POST: someone had to notice a
thing happened and ask for the workflow. Now the graph itself is the trigger.

### What was added

- **`graph/predicates.py`** — pure predicate evaluation with `changed` / `match` / `was`
  clauses and operators (`in`, `ne`, `gt`, `exists`, `contains`, ...).
- **`graph/service.py`** — entity upsert writing the entity and its outbox row in one
  transaction.
- **`graph/dispatcher.py`** + `manage.py run_dispatcher` + a compose service.
- **`Trigger.input_template`** — how a change becomes a run's input.
- Graph REST API: entities, edges, changes (the outbox), triggers.
- `manage.py seed_graph` — the `employee_onboarding` workflow, entities, two triggers.
- App/device provisioning endpoints on the fake service.

### Why it is shaped this way

**The outbox is written in the same transaction as the entity.** The tempting
alternative — save the entity, then publish an event — is a dual write, and it is wrong
in both directions. Crash between the two and the graph has changed with nothing
downstream knowing; publish first and fail to save, and you have triggered workflows for
a change that never happened. Both rows, one COMMIT.

**A write that changes nothing produces no change row.** Systems that sync into a graph
re-send unchanged state constantly. Without diffing, every full sync would re-trigger
every onboarding workflow in the company.

**Predicates need `changed`, not just `match`.** "department is Engineering" stays true
forever after the promotion, so a match-only rule fires on every subsequent save of that
employee. `changed` is what makes it fire on the *transition*. There is a test named for
exactly this: `test_match_alone_fires_on_every_later_write`.

**`was` expresses the transition's origin.** Without it, an employee already in
Engineering whose department field is rewritten gets onboarded a second time.

**`input_template` decouples the workflow from the graph.** The node trigger maps
`{{ entity.external_id }}` into `input.node` because that is the shape
`rolling_cluster_upgrade` expects. One workflow can be driven by several entity types
without knowing anything about any of them. It reuses the step-config expression engine.

**Predicates are validated when a trigger is saved, not when it fires.** The dispatcher
must never be where a predicate is discovered to be broken — by then it is on the hot
path of every change to that entity type.

**Type errors in comparison are a non-match, not a crash.** Attributes are schemaless,
so a string turns up where a number was meant. One bad row must not block the outbox
behind a change that can never be processed.

### How to see it

```bash
docker compose exec web python manage.py seed_graph
```

```bash
.venv\Scripts\python.exe scripts/walkthrough_05_triggers.py
```

The script resets the graph to its starting state on each run, so take four looks like
take one.

### Verified

`PATCH /api/entities/employee/e_42/ {"department": "Engineering"}` — a request that
writes two rows and calls nothing — produced, unprompted:

- run `employee_onboarding`, `trigger_source: trigger:2406f493...`, input
  `{"employee": "e_42", "manager": "m_07"}` rendered from the template
- device assigned; github, slack and pagerduty accounts provisioned in parallel
- status `SUCCEEDED`

Both guards hold: re-writing `department` to the value it already had produced
`changed: False` and no outbox row; changing `title` produced a change row but did not
match the predicate. Onboarding runs stayed at 2.

`PATCH /api/entities/node/node-3/ {"status": "unhealthy"}` fired
`rolling_cluster_upgrade` with input `{"node": "node-3"}` — the same engine, a
completely different entity type, neither workflow aware of the other.

100 unit tests pass with no database.

### Known gaps

- `wait` has no plugin; durable timers (Block 4) remain unbuilt and are cuttable.
- Multi-tenancy is designed (`DESIGN.md` §11) but `current_tenant()` still returns the
  single default tenant. It is behind one function, so the change lands in one place.
- No SSE or DAG UI; the admin and these scripts are the interface.


---

## Block 4 — Durable timers

Taken last, after the demo-critical blocks. It closes the `wait` gap and makes the
"Postgres is the queue AND the timer" claim concrete.

### What was added

- **`StepPlugin.defers` / `resume_at()`** — deferral as a first-class plugin capability.
- **`WaitPlugin`** — `{"duration_s": 45}` or `{"until": "2026-09-08T09:00:00Z"}`.
- **`worker._handle_deferral`** — sets the timer on first arrival, fires it on the second.
- **`RunState.WAITING`**, plus `waiting_steps()` and `running_steps()`.
- `demo_wait` workflow and walkthrough 6.

### Why it is shaped this way

**Nothing sleeps.** A step that called `sleep()` would pin a worker for the whole
duration and lose the wait entirely on the next deploy. Instead the step records a timer
and queues itself for a future moment; the worker is released immediately. The wait *is*
a `tasks` row with a future `run_after` — the same row shape as ready work and as a
backoff retry.

That is why Cascade has no scheduler process, no timer service and no Celery beat.
Nothing is counting down anywhere, so there is nothing to lose.

**Two visits to the same step, and which visit we are on is read from the log.** A step
already marked `WAITING` is one whose timer came due; anything else is a first arrival.
No flag is held anywhere.

**Deferral is a plugin capability, not a special case for `wait`.** A human-approval
step is the same shape: defer with `resume_at` returning `None` (indefinitely) and let
an API call queue it. That lands without touching the worker.

**`WAITING` is distinguished from `RUNNING`** — but only when nothing else is moving; one
branch parked while another executes is still a running run. A run sitting on a
three-day timer otherwise looks identical to a wedged one, and someone will eventually
"fix" it by hand.

**Naive timestamps in `until` are treated as UTC.** Adopting the worker's local clock
would make the same workflow resume at different moments depending on which host ran it.

### How to see it

```bash
.venv\Scripts\python.exe scripts/walkthrough_06_durable_wait.py
```

The script runs `docker compose down` on your behalf and brings the stack back.

### Verified

A run with a 45-second wait reported `WAITING`, with the entire wait visible as one row:

```
step: settle   status: READY   run_after: 2026-09-06T06:45:08Z   attempt: 2
```

Then **every container was destroyed** — web, all three workers, the reaper, the
dispatcher, and the network — at 12:17:11, and restored at 12:17:20. Brand new processes
that had never seen the run:

```
12:17:48  [ 7] TIMER_FIRED          settle
12:17:48  [10] STEP_SUCCEEDED       after
12:17:48  [11] RUN_SUCCEEDED
```

Final step output: `resumed after the wait`.

The same property that survives `kill -9` survives a full redeploy, and would survive
restoring the database onto a different machine. Change `duration_s` to `259200` and it
is a three-day wait with no other change to the system.

116 unit tests pass with no database.

### Remaining gaps

- Multi-tenancy is designed (`DESIGN.md` §11) but `current_tenant()` still returns the
  single default tenant. It is behind one function, so the change lands in one place.
- No SSE or DAG UI; the admin and the walkthrough scripts are the interface.
- Human-approval steps are unbuilt, but the deferral machinery they need now exists.


---

## Block 7 — SSE and the live DAG

### What was added

- **`engine/streaming.py`** — `GET /api/runs/<id>/stream/`, server-sent events.
- **`templates/dashboard.html`** — the live DAG at `/`. One file, vanilla JS, inline
  SVG. No npm, no build step, no dependency that can fail to install at 3am.

### Why it is shaped this way

**Polling, not LISTEN/NOTIFY, and the comment in the source says so.** Postgres can push
these events with no polling at all and it is the better answer at scale. It is not what
is here because `LISTEN` needs a dedicated connection held open outside Django's pooling
for the life of the stream, and getting that wrong leaks connections until Postgres
refuses new ones. A 300ms indexed query on `(run_id, seq)` costs nothing at demo scale
and cannot leak. The seam is `_events_since` — swapping it touches one function.

**`?since=<seq>` resume.** A client only ever needs "everything after seq N", so a
reconnecting browser picks up exactly where it left off — no snapshot, no missed events,
no duplicates. That falls out of the log being the source of truth; a mutable-state
design would need a snapshot-plus-delta protocol here.

**The browser does not fold events.** It re-reads derived state from the server after
each event rather than maintaining its own `replay()`. Two independent folds of the same
log will eventually disagree, and then you are debugging a UI that shows a state the
engine was never in.

**`close_old_connections()` in a `finally`.** The generator outlives the normal request
cycle, so Django's usual cleanup has already run by the time it yields. Without this,
every stream leaks a connection.

**Heartbeat comment frames.** A run parked on a three-day timer emits nothing for three
days; without a keepalive, proxies and browsers decide the stream is dead.

### How to see it

Open `http://localhost:8000/` and click a workflow to launch it.

### Verified

- `demo_diamond` streamed 12+ frames live, including the three `STEP_STARTED` events for
  the parallel branches arriving together.
- The DAG renders left-to-right by longest-path depth, nodes coloured by status, running
  steps pulsing, completed edges turning green.
- A forced rollback rendered exactly as intended: `preflight` green, `drain` and
  `upgrade` orange `COMPENSATED`, `verify` red `FAILED`, the 503 in the header, and
  `COMPENSATION_STARTED → STEP_COMPENSATED upgrade → STEP_COMPENSATED drain → RUN_FAILED`
  in the log.
- The run list refreshes every 3s, so runs started by the graph dispatcher appear on
  their own, marked `auto`.

116 unit tests still pass.

### Remaining gaps

- Multi-tenancy is designed (`DESIGN.md` §11) but `current_tenant()` still returns the
  single default tenant. It sits behind one function.
- Human-approval steps are unbuilt; the Block 4 deferral machinery already supports them.
- The dev server is single-process; each open stream holds a thread. Fine for a demo,
  and the fix is gunicorn with async workers.

