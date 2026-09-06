# Demo script — 5 minutes

Everything here is verified working. Record in **five separate segments** and cut them
together; do not attempt one take.

---

## Before you start

```bash
docker compose up -d --scale worker=3
```

```bash
docker compose exec web python manage.py reset_demo
```

Run `reset_demo` **between every take.** A trigger fires on a *transition*, so if Priya
is already `hired`, take two silently does nothing and you will not know why.

**Screen layout for segments 1–3:**

| Left half | Right half |
|---|---|
| Browser: `http://localhost:9000/company` | Terminal |

**For segments 4–5:** browser on `http://localhost:8000/`.

---

## Segment 1 — The problem (0:00–0:35)

*No screen recording needed. Slide 1 or 2 over voice.*

> "Every company has a wiki page called 'how to onboard a new engineer.' Create their
> mailbox, order a laptop, add them to Slack, give them GitHub access, put them on
> payroll.
>
> Someone follows that page by hand. And halfway down, at step five, the payroll
> provider rejects their tax ID.
>
> That person now has **write access to your source code** and **no employment record**.
> Nobody goes back and undoes steps one to four. Nobody ever does.
>
> We built the thing that does."

---

## Segment 2 — It works (0:35–1:30)

Show `/company` — five empty lists. Then:

```bash
curl -X PATCH http://localhost:8000/api/entities/person/priya.sharma/ -H "Content-Type: application/json" -d "{\"attrs\":{\"employment_status\":\"hired\"}}"
```

> "That's HR marking Priya as hired. One field. That's the entire integration — no
> ticket, no checklist, nobody messaged."

Wait ~10 seconds. Refresh `/company`.

> "Mailbox created. Laptop ordered. Slack invited. GitHub access granted. Payroll
> enrolled. Nobody did any of that.
>
> And three of those happened **at the same time**, on three different machines,
> because none of them depends on the others."

---

## Segment 3 — It undoes itself (1:30–2:45) ← **the money shot**

```bash
curl -X POST http://localhost:9000/payroll/reject-next -H "Content-Type: application/json" -d "{\"person\":\"rahul.nair\"}"
```

> "Rahul's tax ID won't verify with the authority. Not a glitch — retrying won't fix it.
> He genuinely cannot be employed until someone sorts out his paperwork."

```bash
curl -X PATCH http://localhost:8000/api/entities/person/rahul.nair/ -H "Content-Type: application/json" -d "{\"attrs\":{\"employment_status\":\"hired\"}}"
```

> "Same one field. Watch what it does."

Wait ~15 seconds. Refresh `/company`.

**Pause here. Let them read it.**

> "Only Priya. Rahul has no mailbox, no Slack, no GitHub access, no laptop on its way.
>
> The engine created all four of those, hit the payroll rejection, and **undid its own
> work** — GitHub first, then Slack, then the laptop, then the mailbox. Exactly the
> reverse of the order it did them in.
>
> Without this, Rahul has commit access to your codebase and isn't an employee. You find
> out at the next security audit."

---

## Segment 4 — Same engine, infrastructure (2:45–3:30)

Switch to `http://localhost:8000/`. Click **rolling_cluster_upgrade**, let it go green.

> "Same engine, completely different world. This is a rolling node upgrade — drain the
> node, upgrade it, health-check it, put it back."

Then, in the terminal:

```bash
curl -X POST http://localhost:9000/nodes/node-3/fail-after-upgrade
```

```bash
curl -X POST http://localhost:8000/api/runs/ -H "Content-Type: application/json" -d "{\"workflow\":\"rolling_cluster_upgrade\",\"input\":{\"node\":\"node-3\"}}"
```

Click the new run. Let it fail and unwind.

> "The upgrade broke node-3. Green, green, green — then verify goes red, and the upgrade
> and the drain go amber as they're undone. The node ends up back on the old version,
> back in service, healthy.
>
> **We did not write a second engine for this.** We wrote a second recipe."

---

## Segment 5 — Why it's trustworthy (3:30–4:40)

Kill a worker mid-run:

```bash
.venv\Scripts\python.exe scripts/walkthrough_03_crash.py
```

> "A workflow's position never lives in a process — it's an append-only log in Postgres.
> The worker is a temporary interpreter: it takes one row, does one step, writes the
> result, and forgets everything.
>
> So I can `kill -9` a worker mid-step." *(point at the timestamp)* "Eleven seconds later
> a different worker picked it up, replayed the log, and finished the job. Nothing was
> handed over, because nothing was ever held.
>
> Same property means a step can wait three days for an approval and survive a full
> redeploy. There's no scheduler and no timer — the wait is one row with a future
> timestamp."

---

## Segment 6 — Close (4:40–5:00)

> "One database. No Kafka, no Celery, no Temporal. Postgres is the queue, the timer and
> the log.
>
> A hundred and thirty-three tests, and every run leaves a complete ordered record of
> exactly what happened.
>
> You already wrote the runbook. We make the machine follow it — including the part
> everyone skips at three in the morning, which is putting things back."

---

# What you are pitching

**One sentence:**

> A workflow engine where a change to your systems-of-record triggers the work
> automatically — and if anything fails halfway, it unwinds cleanly instead of leaving
> you half-done.

**Three claims, in order of how much they matter:**

| Claim | Proof on screen |
|---|---|
| **It undoes itself.** Partial failure leaves a consistent system, not a mess | Rahul has nothing; node-3 back on 7.0 |
| **It runs itself.** Your systems-of-record are the trigger | One PATCH → five systems updated |
| **It survives anything.** Crash, redeploy, three-day wait | `kill -9`, recovered in 11s |

**Do not lead with the architecture.** Lead with Rahul.

---

# Questions they will ask

**"Why not Temporal / Airflow / Celery?"**
> "We built the primitives to understand them — durable execution, leases, sagas. And the
> graph-trigger layer isn't in any of them: none makes a change to your employee or
> infrastructure records the thing that starts the work."

**"Why Postgres as a queue instead of Kafka?"**
> "Because the queue, the timer and the event log then commit in one transaction. With a
> broker you get a dual write — the row says one thing, the message says another. At our
> scale `SELECT FOR UPDATE SKIP LOCKED` costs nothing and it can't disagree with itself."

**"What if the rollback itself fails?"**
> "We stop, mark the run as needing manual intervention, and leave the log showing
> exactly how far the unwind got. A partially unwound system that says so beats a crash
> loop that keeps retrying."

**"How do you know a step didn't run twice?"**
> "Every attempt carries an idempotency key derived from run, step and attempt number. We
> send it as a header, so the downstream system can deduplicate a delivery we were forced
> to repeat. We guarantee at-least-once; the key makes it exactly-once in practice."

**"Is this multi-tenant?"**
> "Yes — the scoped manager is the *default*, so a forgotten filter raises an error
> instead of returning another customer's rows. Exactly four places query across tenants
> and each enters the tenant context immediately."

**"How is the UI not just a mock?"**
> "There's no colour stored anywhere. Every box is derived from the event log — you can
> wipe the cached context column, reload, and it rebuilds identically. And the side
> effects land in a separate process whose logs you can read."

---

# If you need to rebuild the portal

The dashboard is **one file**: `templates/dashboard.html` (~330 lines, no build step, no
npm, no dependencies). It's served by `core/views.py → dashboard()` at `/`.

It consumes four endpoints and nothing else:

| Endpoint | Used for |
|---|---|
| `GET /api/workflows/` | the launch buttons |
| `GET /api/runs/?limit=60` | the run list, refreshed every 3s |
| `GET /api/runs/<id>/` | spec + context + events → the DAG and log |
| `GET /api/runs/<id>/stream/` | SSE, live updates |
| `GET/POST /api/runs/<id>/approvals/`, `/approve/` | the approval panel |

**If it breaks and you have no time to fix it**, delete the `path("", dashboard)` line
from `cascade/urls.py` and demo from the terminal — segments 1–3 and 5 don't need it at
all, and `/admin` shows every run with its event log inline. The walkthrough scripts in
`scripts/` cover every beat without a browser.

**Colour is derived, never stored.** `renderDag()` reads
`context.steps[<id>].status` and maps it through the `TONE` table. If a box is the wrong
colour, the bug is in `engine/replay.py`, not in the page.
