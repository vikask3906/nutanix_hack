# Cascade — a durable, graph-triggered workflow execution engine

> Nutanix Hackathon IIT-G · Problem Statement #4 (Workflow Execution Engine)
>
> Stack: **Django + DRF + Postgres**. This file is the specification the implementation
> follows — schema, state machine, algorithms and the Django mapping (§10).

---

## 1. Thesis

Running a list of steps is easy. Running them **reliably** is the entire problem:

| Failure mode | What a naive runner does | What Cascade does |
|---|---|---|
| Worker dies mid-run | Run is lost, state unknown | Resumes at the exact step from the event log |
| Step retried after timeout | Side effect fires twice | Idempotency key per attempt |
| Step 7 of 9 fails | Half-mutated system, no cleanup | Saga compensation, reverse order |
| Step must wait 3 days | Process holds a thread, dies on deploy | Durable timer in the queue |
| Two runs touch one entity | Lost update / corruption | Per-entity serialization |
| Ten thousand runs | Single process, no scaling | N stateless workers, lease-based claim |

Cascade takes a JSON workflow, executes shell + REST steps, and survives all of the above.

Layered on top: workflows are not only POSTed in — they **fire from changes to a typed
entity graph**. That is Rippling's `Employee Graph -> Workflow Automator` model,
generalised to any entity type.

---

## 2. Architecture

```
   PATCH /entities/employee/e_42
   POST  /runs  ──────────────┐
                              ▼
   ┌───────────────┐   ┌────────────────┐   ┌──────────────┐
   │ Entity Graph  │──▶│ Change Outbox  │──▶│  Dispatcher  │
   │ entities+edges│   │ (transactional)│   │ (predicates) │
   └───────────────┘   └────────────────┘   └──────┬───────┘
                                                   │ creates runs
                                                   ▼
   ┌──────────────────────────────────────────────────────┐
   │            Postgres = queue + log + state            │
   │   workflow_defs · runs · run_events · tasks          │
   └───────┬──────────────────────────────────────────────┘
           │  SELECT … FOR UPDATE SKIP LOCKED
   ┌───────▼────────┐ ┌───────────────┐ ┌───────────────┐
   │    Worker 1    │ │   Worker 2    │ │   Worker N    │
   │ lease+heartbeat│ │               │ │               │
   └───────┬────────┘ └───────────────┘ └───────────────┘
           │ step plugins: http · shell · wait · branch
           ▼
   ┌───────────────┐
   │ Target systems│                    SSE ──▶ React DAG UI
   └───────────────┘
```

**One load-bearing insight:** Postgres is the queue *and* the durable timer *and* the
event log. A `wait 3 days` step is just a queue row with `run_after = now() + 3d`.
No Kafka, no Celery, no separate scheduler. Say this out loud in the demo.

---

## 3. Data model

### 3.1 Workflow definitions (immutable, versioned)

```
workflow_defs
  id            uuid pk
  name          text
  version       int            -- a run pins its version at start; defs never mutate
  spec          jsonb
  created_at    timestamptz
  unique (name, version)
```

A running workflow must never change under you. New edits create version N+1;
in-flight runs keep executing version N. This is a real durable-execution concern
and a good slide.

### 3.2 Runs — a *projection*, not the truth

```
runs
  id              uuid pk
  def_id          uuid fk
  status          text   -- PENDING RUNNING WAITING COMPENSATING SUCCEEDED FAILED CANCELLED
  input           jsonb
  context         jsonb  -- materialised fold of run_events; cache, not source of truth
  last_seq        int
  entity_ref      text   -- 'employee:e_42' — used for per-entity serialization
  trigger_source  text   -- api | trigger:<id> | schedule
  created_at, updated_at
```

### 3.3 Event log — the source of truth

```
run_events
  id          bigserial pk
  run_id      uuid fk
  seq         int
  type        text
  step_id     text
  payload     jsonb
  created_at  timestamptz
  unique (run_id, seq)      -- the concurrency guard
```

Event types:
`RUN_STARTED, STEP_SCHEDULED, STEP_STARTED, STEP_SUCCEEDED, STEP_FAILED,
STEP_RETRY_SCHEDULED, TIMER_SET, TIMER_FIRED, COMPENSATION_STARTED,
STEP_COMPENSATED, RUN_SUCCEEDED, RUN_FAILED`

`UNIQUE(run_id, seq)` is doing enormous work: two workers racing to advance the same
run both try to write seq N; exactly one wins, the loser re-reads and retries. That is
optimistic concurrency control for free, with no distributed lock.

**Rebuilding state = replay.** `context = fold(run_events where run_id=X order by seq)`.
Crash recovery, the time-travel UI, and the audit log are all the same mechanism.

### 3.4 Tasks — queue and timers, unified

```
tasks
  id                uuid pk
  run_id            uuid fk
  step_id           text
  attempt           int
  kind              text   -- EXECUTE | COMPENSATE
  status            text   -- READY | LEASED | DONE
  run_after         timestamptz   -- backoff AND durable sleep both live here
  lease_owner       text
  lease_expires_at  timestamptz
  idempotency_key   text unique
  index on (status, run_after)
```

Claim loop:

```
BEGIN;
  SELECT * FROM tasks
   WHERE status = 'READY' AND run_after <= now()
   ORDER BY run_after
   FOR UPDATE SKIP LOCKED
   LIMIT 1;
  UPDATE tasks SET status='LEASED', lease_owner=:me,
                   lease_expires_at = now() + interval '30 seconds';
COMMIT;
```

`SKIP LOCKED` is what makes N workers scale linearly with zero coordination. It is a
genuine production pattern, not a hackathon shortcut — worth a slide of its own.

### 3.5 Entity graph (the Rippling layer)

```
entities        id, type, external_id, attrs jsonb, version, updated_at
                unique(type, external_id); GIN index on attrs
entity_edges    src_id, rel, dst_id            -- employee -owns-> device
entity_changes  id, entity_id, type, before jsonb, after jsonb,
                changed_keys text[], created_at, processed_at   -- transactional outbox
triggers        id, name, entity_type, predicate jsonb, def_id, enabled
```

Every entity mutation writes the row **and** an `entity_changes` row in one
transaction (outbox pattern — no dual-write inconsistency). A dispatcher polls
unprocessed changes with the same `SKIP LOCKED` claim, evaluates trigger predicates,
and creates runs.

Predicate shape (this is your "Supergroup"):

```json
{ "changed": ["department"],
  "match": { "department": "Engineering", "geo": "IN" } }
```

---

## 4. Workflow spec format

```json
{
  "name": "rolling_cluster_upgrade",
  "version": 1,
  "steps": [
    { "id": "preflight",
      "type": "http",
      "config": { "method": "GET", "url": "http://node-{{ input.node }}/health" },
      "retry": { "max": 3, "backoff": "exponential", "base_ms": 500 } },

    { "id": "drain",
      "type": "http",
      "needs": ["preflight"],
      "config": { "method": "POST", "url": "http://node-{{ input.node }}/drain" },
      "compensate": { "type": "http",
                      "config": { "method": "POST",
                                  "url": "http://node-{{ input.node }}/undrain" } } },

    { "id": "upgrade", "type": "shell", "needs": ["drain"],
      "config": { "cmd": "./upgrade.sh {{ input.node }}", "timeout_s": 60 } },

    { "id": "settle", "type": "wait", "needs": ["upgrade"],
      "config": { "duration_s": 10 } },

    { "id": "verify", "type": "http", "needs": ["settle"],
      "config": { "method": "GET", "url": "http://node-{{ input.node }}/health" },
      "on_error": "compensate" }
  ]
}
```

- **DAG via `needs`** — gives you sequencing *and* fan-out/join from one field.
- **Templating** — `{{ input.x }}`, `{{ steps.preflight.output.y }}`. Do **not** build a
  language. A ~40-line dotted-path resolver over the context dict is the whole feature.
- **`on_error`** — `fail` (default) | `compensate` | `continue`.

---

## 5. The four algorithms

### 5.1 Advance the run

1. Claim a task. Load run, replay events -> context.
2. Append `STEP_STARTED` at `last_seq + 1`. If the unique constraint fires, another
   worker won: abandon, re-read.
3. Execute the step plugin.
4. Append `STEP_SUCCEEDED` (with output) or `STEP_FAILED` (with error).
5. Recompute which steps now have all `needs` satisfied; insert a `tasks` row for each.
6. If no steps remain and none failed -> `RUN_SUCCEEDED`.

### 5.2 Retry

On `STEP_FAILED` with `attempt < retry.max`: insert a new task with `attempt+1`,
`run_after = now() + base_ms * 2^attempt` plus jitter, and a fresh idempotency key.
Jitter matters — without it, N retrying workers stampede in lockstep.

### 5.3 Crash recovery (your demo moment)

A reaper loop runs every 10s:

```
UPDATE tasks SET status='READY', lease_owner=NULL
 WHERE status='LEASED' AND lease_expires_at < now();
```

That is the whole mechanism. A dead worker's lease simply expires, another worker
claims the task, replays the event log, and continues. **`kill -9` a worker on camera
and let the run finish. That single moment is worth more than any three features.**

### 5.4 Saga compensation

On terminal failure of a step whose `on_error` is `compensate`:

1. Run status -> `COMPENSATING`; append `COMPENSATION_STARTED`.
2. Scan the event log backwards for `STEP_SUCCEEDED` whose step def has a
   `compensate` block.
3. Enqueue those as `kind='COMPENSATE'` tasks, strictly in reverse order.
4. Each appends `STEP_COMPENSATED`. When drained -> `RUN_FAILED` (cleanly rolled back).

Almost nobody implements rollback at a hackathon. This is your differentiator.

---

## 6. Extensibility (the PS asks for it explicitly)

A step type is a plugin satisfying one contract:

```
name: str
validate(config) -> None                            # raises on a bad spec, at definition time
execute(config, context, idem_key) -> output dict   # raises on failure
```

Register in a dict; the engine never learns about new step types. Ship `http`, `shell`,
`wait`, `branch`; show the contract on a slide and note that `slack`, `k8s`, `sql`,
`approval` are each ~20 lines. That is what "extensible" means concretely, and judges
will ask.

Other clean seams to name in the deck: the trigger predicate evaluator, the storage
interface (Postgres today, anything with compare-and-swap tomorrow), and the
expression resolver.

---

## 7. Concurrency and safety

- **Per-entity serialization** — before advancing a run, take
  `pg_advisory_xact_lock(hashtext(entity_ref))`. Guarantees only one workflow mutates a
  given node/employee at a time. Two lines of code, prevents the classic lost update.
- **Idempotency key** = `sha256(run_id || step_id || attempt)`. Sent as an
  `Idempotency-Key` header on HTTP steps and used as the `tasks` uniqueness guard so a
  step can never be double-enqueued.
- **Shell sandboxing** — MVP: `subprocess` with a hard timeout, no `shell=True`, a
  restricted cwd, an allowlisted env. Note the production design (container per step,
  cgroup limits, no network by default) on a slide; do not build it tomorrow.
- **Secrets** — MVP: `{{ secrets.NAME }}` resolved from env at execution time and
  redacted from all logged payloads. Redaction is cheap and reads as maturity.

---

## 8. Demo scenarios — one engine, two worlds

### A. Rolling cluster upgrade (the Nutanix story — lead with this)

Five fake node services in Docker Compose, each with `/health`, `/drain`, `/undrain`,
`/upgrade`, and a `/fail-next` switch so you can make node 3 fail its health check on
demand. The workflow upgrades nodes one at a time; node 3's `verify` fails; the engine
compensates — undrain, restore — and the cluster ends in a consistent state.

Then `kill -9` a worker mid-upgrade of node 4. It resumes.

### B. Employee onboarding (the Rippling story — 30 seconds at the end)

`PATCH /entities/employee/e_42 {"department": "Engineering"}` -> the change lands in the
outbox -> the trigger predicate matches -> a workflow fires that assigns a device,
provisions three app accounts, and notifies a manager. **Same engine, same graph, zero
code changes.**

Scenario B is what proves the abstraction is real rather than a single hardcoded
pipeline. It is the difference between "a script runner" and "a platform".

---

## 9. Post-hackathon extension path

Deliberately designed so these slot in without a rewrite:

- Sub-workflows and `map`-over-query fan-out (the queue already supports it)
- Human approval steps (`WAITING` status + a resume endpoint — the timer machinery is there)
- Circuit breakers and per-target rate limits in the http plugin
- Real sandboxing: one container per shell step
- Cross-system identity mapping (Rippling's actual hard problem: the same human across
  Okta, Slack, GitHub, payroll)
- Workflow versioning with live migration of in-flight runs
- A reporting layer over the event log — every run is already fully audited

---

## 10. Implementation mapping: Django

The stack is **Django + DRF + Postgres**, chosen because every primitive Cascade needs
already exists in Django, and because it is the stack Rippling runs on.

| Cascade concept | Django mechanism |
|---|---|
| `workflow_defs`, `runs`, `run_events`, `tasks`, `entities` | Models + migrations |
| Claim a task | `Task.objects.select_for_update(skip_locked=True).filter(...)[:1]` |
| Append event atomically | `transaction.atomic()` with the `UNIQUE(run_id, seq)` constraint as the guard |
| Transactional outbox | Write the entity and the change row in one `atomic()`; fire the dispatcher via `transaction.on_commit()` |
| Entity attributes | `JSONField` with a GIN index; predicates compile to `attrs__department="Engineering"` lookups |
| Per-entity serialization | Raw `pg_advisory_xact_lock(hashtext(%s))` inside `atomic()` |
| API | DRF `ModelViewSet` for entities and runs; a plain `APIView` for SSE |
| Worker | `manage.py run_worker` — a `BaseCommand` running the claim loop |
| Reaper / dispatcher | The same, as `manage.py run_reaper` / `run_dispatcher` |
| Demo UI, free | Django admin over entities, runs and the event log |

**Transaction discipline** (the thing that will bite you): three short transactions per
step, never one long one.

1. `atomic()` — claim the task, set the lease. Commit.
2. **No transaction** — execute the step. This is slow and does network I/O; holding a
   row lock across it will deadlock your workers.
3. `atomic()` — append the result event, enqueue the next ready steps, mark the task done.

If step 3 never runs because the worker died, the lease expires and the reaper recovers
it. That is exactly why the design is safe without long transactions.

**Do not reach for Celery.** The `tasks` table *is* the queue, and building it yourself is
the entire point of the learning exercise. Mention in the deck that you deliberately did
not use Celery/Temporal and can say why — that answer is worth more than the feature.

---

## 11. Multi-tenancy (the Rippling-shaped addition)

Rippling is one Employee Graph serving thousands of companies at once. Cascade should be
too — it is cheap and it changes what the project *is*.

- `tenant_id` (FK to `Tenant`) on every table: entities, edges, changes, triggers,
  workflow_defs, runs, run_events, tasks.
- A `TenantScopedManager` as the model's **default** manager, so an unscoped query is
  impossible by accident. Keep the unscoped one available as `all_tenants` for the worker.
- Tenant resolved from a header or subdomain in DRF middleware, held in a
  `ContextVar` for the request; the worker sets it per task from `task.tenant_id`.
- Composite indexes lead with `tenant_id` — `(tenant_id, status, run_after)` on tasks.
- Demo it: two tenants, same workflow definition name, different versions and different
  entity graphs, running concurrently, fully isolated.

This is the difference between "a workflow engine" and "a multi-tenant workflow platform",
and it is precisely the problem shape you would be working on at Rippling.
