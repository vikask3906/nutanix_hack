# Cascade — 24-hour execution plan

**Hard deadline: Sun Sep 7, 2026, 11:00 AM.** Nothing accepted after 12 PM.
Deliverable is a **≤5-min demo video + a PowerPoint** — not a repo link. Plan backwards
from the video, not forwards from the code.

Clock times below assume a ~10:30 AM Sep 6 start. Adjust the offsets, keep the order.

---

## The single rule

**Features stop at H+15 (≈ 01:30 AM). No exceptions.** Everything after that is
recording, slides and rehearsal. Teams lose this hackathon by coding until 10:55 AM and
submitting a video shot in one panicked take. The judging criteria are 50% presentation
and completeness — a smaller thing, demoed beautifully, beats a bigger thing demoed badly.

---

## Build order

The order is chosen so that **you have a submittable project at every checkpoint.**
If you stop at H+9 you still have a durable workflow engine that fully satisfies PS #4.
The entity graph is the last thing added because it is the most cuttable.

| Block | Time | Work | Checkpoint — do not proceed until this is true |
|---|---|---|---|
| **0** | H+0 → H+1 | Repo skeleton, docker-compose (postgres + api), schema DDL, migrations, health endpoint | `docker compose up` gives you a live DB with every table |
| **1** | H+1 → H+3 | Spec parser + validation, DAG readiness (`needs` resolution), event append with `seq` uniqueness, replay-to-context | You can create a run, hand-append events, and rebuild identical state from the log |
| **2** | H+3 → H+5.5 | Worker loop: `SKIP LOCKED` claim, lease, heartbeat; `http` and `shell` plugins; enqueue-next-ready | A 4-step linear workflow runs end to end from `POST /runs` |
| **3** | H+5.5 → H+7 | Retries with exponential backoff + jitter, idempotency keys, the lease reaper | **`kill -9` a worker mid-step and another finishes the run.** Screen-record this the moment it works |
| **4** | H+7 → H+8.5 | Parallel fan-out/join, `wait` (durable timer), `branch` | Two steps run concurrently and join; a 10s wait survives a full restart |
| **5** | H+8.5 → H+10 | **Saga compensation** | Rolling-upgrade workflow rolls back cleanly when node 3 fails `verify` |
| **6** | H+10 → H+11.5 | Entity graph, outbox, dispatcher, trigger predicates | `PATCH` an employee's department → a workflow fires by itself |
| **7** | H+11.5 → H+13 | Fake node services with `/fail-next`, seed data, both workflow JSONs, SSE endpoint | Both demo scenarios run start to finish, repeatably, from a clean `docker compose up` |
| **8** | H+13 → H+15 | Buffer. Bugs only. Zero new features. | Both scenarios run three times in a row without a hitch |
| **9** | H+15 → H+18 | **Record video. Build deck. Rehearse.** | Submitted |

**Sleep between H+13 and H+15 if you need it — but only after Block 7 is green.**

Your friend works in parallel from H+3 (once the API shape exists) on the React Flow DAG
UI consuming SSE. If they do not finish, you fall back to a split-screen terminal with
`watch`-style output — which is honestly fine on video, and you should build that fallback
yourself at H+12 regardless.

---

## Cut list, in the order things get cut

If you are behind, cut from the top of this list. Do not improvise the order in the moment.

1. Branch/conditional steps → hardcode the path
2. The React UI → terminal + a static architecture diagram
3. Entity graph → keep the outbox and one hardcoded trigger predicate; drop edges and the query API
4. Durable timers → keep `wait` but with short durations only
5. Parallel fan-out → linear DAG only

**Never cut, at any cost:** the event log, lease-based claim + reaper (crash resume),
retries with idempotency, saga compensation. That set *is* the project. A linear,
UI-less engine that survives `kill -9` and rolls back cleanly beats a pretty engine
that does neither.

---

## Deck outline (9 slides, ~1 slide/25s if you present live)

1. **Cascade** — "A durable workflow engine where infrastructure changes trigger themselves."
2. **The problem** — the failure-mode table from `DESIGN.md` §1. Lead with this. Everyone
   understands "run these steps"; your value is showing you understand why that is the easy half.
3. **Architecture** — the diagram from §2.
4. **Idea 1: state is an event log** — replay gives you crash recovery, audit, and time
   travel from one mechanism.
5. **Idea 2: Postgres is the queue** — `SKIP LOCKED` + leases. N stateless workers, zero
   coordination. Note that it is also the durable timer.
6. **Idea 3: sagas** — partial failure leaves a consistent system, not a half-mutated one.
7. **The graph layer** — triggers on entity change, not polling. Name Rippling's Employee
   Graph as prior art; showing you know the lineage reads as sophistication, not derivation.
8. **Demo** — screenshots + the `kill -9` moment.
9. **Extensibility + what's next** — the 3-method plugin contract, and §9's roadmap.

Add a numbers slide if you have time to measure anything: runs/sec with 1 vs 4 workers,
p50/p99 step latency, recovery time after `kill -9`. Even rough numbers separate you from
teams with none.

---

## Video script (5:00, hard cap)

| Time | Beat |
|---|---|
| 0:00–0:30 | The problem. One sentence on what breaks when a workflow runner is not durable. |
| 0:30–1:15 | Architecture diagram, narrated. Name the three ideas. |
| 1:15–3:15 | **Rolling cluster upgrade.** Nodes 1–2 upgrade cleanly. Node 3 fails `verify` → watch it compensate and roll back. Then `kill -9` a worker mid-node-4 → watch it resume and finish. |
| 3:15–4:15 | *Same engine, different world.* `PATCH` the employee → onboarding workflow fires itself. Zero code changes. |
| 4:15–4:45 | Scale: 4 workers, throughput number, the plugin contract. |
| 4:45–5:00 | What is next. |

Record in segments and cut them together — do not attempt one take. Record each segment
**the moment that feature works**, not at the end. Narrate over a screen capture rather
than talking to camera; it is faster to redo and easier to keep under time.

---

## Risks worth pre-empting

- **Docker on Windows will cost you 45 minutes at the worst moment.** Get Block 0 green
  before you write any engine code, and never touch the compose file again after H+1.
- **Postgres advisory locks + long transactions deadlock easily.** Keep every transaction
  short: claim in one, execute *outside* any transaction, append in another.
- **The `kill -9` demo needs ≥2 workers running.** Bake `--scale worker=3` into compose now
  so it is never a live surprise.
- **Judges will ask "why not just use Temporal/Airflow?"** Have the answer ready: you built
  the primitives to understand them; and the graph-trigger layer is the part neither gives you.
