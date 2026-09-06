"""Transactional operations against the engine's tables.

Everything that writes to Postgres lives here. The worker loop and the API both
call into this module, so the concurrency rules are stated once.

THE TRANSACTION RULE, which is the thing most likely to bite you:

    Claim in one short transaction.
    Execute OUTSIDE any transaction.
    Record in another short transaction.

Executing a step inside the claim transaction would hold a row lock across
network I/O. With several workers that deadlocks or starves within minutes. If a
worker dies between claiming and recording, the lease expires and the reaper
recovers the task - that is precisely why long transactions are unnecessary.
"""

import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Tenant
from engine.models import (
    EventType,
    Run,
    RunEvent,
    RunStatus,
    Task,
    TaskKind,
    TaskStatus,
    WorkflowDef,
)
from engine import retry
from engine.replay import (
    compensation_order,
    next_run_state,
    ready_steps,
    replay,
)
from engine.spec import validate_spec

log = logging.getLogger("cascade.service")

MAX_SEQ_CONTENTION_RETRIES = 8


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


def default_tenant():
    """The single tenant used until Block 6 introduces real tenant resolution."""
    tenant, _ = Tenant.objects.get_or_create(
        slug="default", defaults={"name": "Default"}
    )
    return tenant


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def create_definition(tenant, spec, description=""):
    """Validate a spec and store it as the next version of its name.

    Validation happens here, once, and never again during execution. A definition
    that exists is a definition that can run.
    """
    validate_spec(spec)

    with transaction.atomic():
        latest = (
            WorkflowDef.objects.filter(tenant=tenant, name=spec["name"])
            .order_by("-version")
            .first()
        )
        version = (latest.version + 1) if latest else 1

        stored = dict(spec)
        stored["version"] = version

        return WorkflowDef.objects.create(
            tenant=tenant,
            name=spec["name"],
            version=version,
            spec=stored,
            description=description,
        )


# ---------------------------------------------------------------------------
# The event log
# ---------------------------------------------------------------------------


def append_event(run, event_type, step_id="", payload=None):
    """Append one event at the next sequence number.

    Optimistic, not locked. We read ``last_seq``, try to write ``last_seq + 1``,
    and let ``UNIQUE(run_id, seq)`` arbitrate. If another worker got there first
    we take an IntegrityError, re-read and try again.

    This is the concurrency control for the entire engine. No advisory lock, no
    lock service, no consensus - one unique index.
    """
    for _ in range(MAX_SEQ_CONTENTION_RETRIES):
        run.refresh_from_db(fields=["last_seq"])
        seq = run.last_seq + 1

        try:
            with transaction.atomic():
                event = RunEvent.objects.create(
                    tenant_id=run.tenant_id,
                    run=run,
                    seq=seq,
                    type=event_type,
                    step_id=step_id or "",
                    payload=payload or {},
                )
                Run.objects.filter(pk=run.pk).update(last_seq=seq)
        except IntegrityError:
            # Another worker claimed this sequence number. Re-read and retry.
            continue

        run.last_seq = seq
        return event

    raise RuntimeError(
        f"could not append {event_type} to run {run.id} after "
        f"{MAX_SEQ_CONTENTION_RETRIES} attempts - unexpected contention"
    )


def load_context(run):
    """Rebuild a run's context from its event log."""
    return replay(list(run.events.all()))


# ---------------------------------------------------------------------------
# The task queue
# ---------------------------------------------------------------------------


def idempotency_key(run_id, step_id, kind, attempt):
    """Stable across retries of the same attempt, distinct across attempts.

    Two purposes at once: the unique index makes double-enqueue impossible, and
    the value is handed to side-effecting steps so downstream systems can
    deduplicate a delivery we were forced to repeat.
    """
    raw = f"{run_id}:{step_id}:{kind}:{attempt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def enqueue_step(run, step_id, kind=TaskKind.EXECUTE, attempt=1, run_after=None):
    """Insert a task, or do nothing if an identical one already exists.

    Returning None on collision is not an error path - it is the design working.
    When two workers finish parallel branches at the same instant, both compute
    that the join step is now ready and both try to enqueue it. The unique
    idempotency key means exactly one row is created.
    """
    key = idempotency_key(run.id, step_id, kind, attempt)

    try:
        with transaction.atomic():
            return Task.objects.create(
                tenant_id=run.tenant_id,
                run=run,
                step_id=step_id,
                kind=kind,
                attempt=attempt,
                status=TaskStatus.READY,
                run_after=run_after or timezone.now(),
                idempotency_key=key,
            )
    except IntegrityError:
        return None


def enqueue_ready_steps(run, spec, context):
    """Enqueue every step whose dependencies are now satisfied."""
    created = []

    for step_id in ready_steps(spec, context):
        task = enqueue_step(run, step_id)
        if task is not None:
            append_event(run, EventType.STEP_SCHEDULED, step_id)
            created.append(task)

    return created


def claim_task(worker_id, now=None):
    """Claim one due task, or return None.

    ``FOR UPDATE SKIP LOCKED`` is the whole scaling story: each worker locks a
    different row and skips anything already locked, so N workers drain the queue
    concurrently without talking to each other or to a coordinator.

    Kept deliberately tiny - this transaction must not span any I/O.
    """
    now = now or timezone.now()

    with transaction.atomic():
        task = (
            Task.objects.select_for_update(skip_locked=True)
            .filter(status=TaskStatus.READY, run_after__lte=now)
            .order_by("run_after")
            .first()
        )

        if task is None:
            return None

        task.status = TaskStatus.LEASED
        task.lease_owner = worker_id
        task.lease_expires_at = now + timedelta(seconds=settings.TASK_LEASE_SECONDS)
        task.save(update_fields=["status", "lease_owner", "lease_expires_at"])

        return task


def finish_task(task):
    task.status = TaskStatus.DONE
    task.lease_owner = ""
    task.lease_expires_at = None
    task.save(update_fields=["status", "lease_owner", "lease_expires_at"])


def renew_lease(task, worker_id, now=None):
    """Extend a lease we still hold. Returns False if we have lost it.

    The WHERE clause is the point: we only extend if we are still the recorded
    owner and the task is still LEASED. If the reaper decided we were dead and
    handed the work to someone else, this updates zero rows and tells us so.
    """
    now = now or timezone.now()

    updated = Task.objects.filter(
        pk=task.pk, lease_owner=worker_id, status=TaskStatus.LEASED
    ).update(lease_expires_at=now + timedelta(seconds=settings.TASK_LEASE_SECONDS))

    return updated == 1


def finish_task_if_owned(task, worker_id):
    """Mark a task done, but only if we still hold its lease.

    This is a fencing check, and it closes a genuine hole. Suppose a worker
    stalls long enough for its lease to expire - a long GC pause, a hung syscall,
    a paused container. The reaper reassigns the task, another worker runs the
    step, and then the original worker wakes up and tries to record ITS result.
    Without this check the log would gain a second, stale result for a step
    someone else already completed.

    Losing the race here is not an error. It means the system correctly decided
    we were gone, and the right response is to discard our work silently.
    """
    updated = Task.objects.filter(
        pk=task.pk, lease_owner=worker_id, status=TaskStatus.LEASED
    ).update(status=TaskStatus.DONE, lease_owner="", lease_expires_at=None)

    return updated == 1


def schedule_retry(run, step, task, error):
    """Record a failed attempt and queue the next one after a jittered delay."""
    delay = retry.delay_seconds(task.attempt, step.get("retry"))
    next_attempt = task.attempt + 1
    run_after = timezone.now() + timedelta(seconds=delay)

    append_event(
        run,
        EventType.STEP_FAILED,
        task.step_id,
        {"error": error, "attempt": task.attempt},
    )
    append_event(
        run,
        EventType.STEP_RETRY_SCHEDULED,
        task.step_id,
        {
            "error": error,
            "attempt": task.attempt,
            "next_attempt": next_attempt,
            "delay_seconds": round(delay, 3),
        },
    )

    enqueue_step(
        run,
        task.step_id,
        kind=task.kind,
        attempt=next_attempt,
        run_after=run_after,
    )

    log.info(
        "retry %s step=%s attempt=%s -> %s in %.2fs",
        run.id, task.step_id, task.attempt, next_attempt, delay,
    )
    return delay


# ---------------------------------------------------------------------------
# Compensation (sagas)
# ---------------------------------------------------------------------------


def start_compensation(run, spec, context, failed_step, error):
    """Begin unwinding a run whose step failed with ``on_error: compensate``.

    Returns False if there is nothing to unwind - every completed step either
    declares no compensating action or has already been rolled back - in which
    case the run just fails normally.
    """
    pending = compensation_order(spec, context)
    if not pending:
        return False

    append_event(
        run,
        EventType.COMPENSATION_STARTED,
        failed_step,
        {"error": error, "failed_step": failed_step, "will_compensate": pending},
    )

    log.warning(
        "compensating %s after %s failed - unwinding %s",
        run.id, failed_step, " then ".join(pending),
    )
    return True


def enqueue_next_compensation(run, spec, context):
    """Queue exactly ONE compensating step: the next one to unwind.

    Strictly sequential, unlike forward execution. Forward steps run in
    parallel wherever the DAG allows, but rollback must not - undoing a drain
    before undoing the upgrade that depended on it puts the system in a state
    neither the workflow nor the operator expects.

    Each completed compensation triggers the next, so the chain walks backwards
    one step at a time.
    """
    pending = compensation_order(spec, context)
    if not pending:
        return None

    step_id = pending[0]

    # Deliberately no STEP_SCHEDULED event here. That event sets a step's status
    # to SCHEDULED during replay, which would clobber the SUCCEEDED status of the
    # very step we are unwinding - and SUCCEEDED is what marks it as needing
    # rollback in the first place. The task row is enough; COMPENSATION_STARTED
    # already recorded the full plan.
    return enqueue_step(run, step_id, kind=TaskKind.COMPENSATE)


# ---------------------------------------------------------------------------
# The reaper
# ---------------------------------------------------------------------------


def reap_expired_leases(now=None):
    """Return tasks whose holder stopped heartbeating to the ready queue.

    This is the entire crash-recovery mechanism. There is no failure detector,
    no health check, no membership protocol - a worker that stops renewing its
    lease is, by definition, gone. If it was merely slow, ``finish_task_if_owned``
    stops it writing a stale result when it comes back.

    Recovered tasks get a fresh attempt number, which means a fresh idempotency
    key, so a downstream system can tell the retry apart from the original.
    """
    now = now or timezone.now()
    recovered = []

    while True:
        with transaction.atomic():
            task = (
                Task.objects.select_for_update(skip_locked=True)
                .filter(status=TaskStatus.LEASED, lease_expires_at__lt=now)
                .order_by("lease_expires_at")
                .first()
            )

            if task is None:
                break

            dead_owner = task.lease_owner
            next_attempt = task.attempt + 1

            if next_attempt > settings.MAX_TASK_ATTEMPTS:
                # Poison-task guard: this step has taken down a worker too many
                # times. Stop feeding it workers and fail the run.
                task.status = TaskStatus.DONE
                task.lease_owner = ""
                task.lease_expires_at = None
                task.save(update_fields=["status", "lease_owner", "lease_expires_at"])
                _fail_poisoned_step(task, dead_owner)
                recovered.append((task, "poisoned"))
                continue

            task.attempt = next_attempt
            task.status = TaskStatus.READY
            task.lease_owner = ""
            task.lease_expires_at = None
            task.run_after = now
            task.idempotency_key = idempotency_key(
                task.run_id, task.step_id, task.kind, next_attempt
            )
            task.save(
                update_fields=[
                    "attempt", "status", "lease_owner",
                    "lease_expires_at", "run_after", "idempotency_key",
                ]
            )

        log.warning(
            "recovered %s step=%s from dead worker %s (attempt %s)",
            task.run_id, task.step_id, dead_owner or "?", next_attempt,
        )

        append_event(
            task.run,
            EventType.STEP_RETRY_SCHEDULED,
            task.step_id,
            {
                "reason": "lease expired",
                "dead_worker": dead_owner,
                "next_attempt": next_attempt,
            },
        )
        recovered.append((task, "recovered"))

    return recovered


def _fail_poisoned_step(task, dead_owner):
    run = task.run
    spec = run.definition.spec
    error = (
        f"step exceeded {settings.MAX_TASK_ATTEMPTS} attempts; "
        f"last worker to die holding it was {dead_owner or 'unknown'}"
    )

    append_event(run, EventType.STEP_FAILED, task.step_id, {"error": error})
    context = load_context(run)
    close_run_if_finished(run, spec, context)
    sync_run_projection(run, spec, load_context(run))
    log.error("poisoned %s step=%s - %s", run.id, task.step_id, error)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def start_run(definition, run_input=None, entity_ref="", trigger_source="api"):
    """Create a run and enqueue its first steps.

    Note what this does NOT do: execute anything. The API's job ends at "rows
    exist saying work needs doing". That is why a three-second workflow and a
    three-day workflow are the same code path, and why this returns in
    milliseconds.
    """
    with transaction.atomic():
        run = Run.objects.create(
            tenant=definition.tenant,
            definition=definition,
            status=RunStatus.RUNNING,
            input=run_input or {},
            entity_ref=entity_ref,
            trigger_source=trigger_source,
        )

        append_event(run, EventType.RUN_STARTED, payload={"input": run_input or {}})

        context = load_context(run)
        enqueue_ready_steps(run, definition.spec, context)
        sync_run_projection(run, definition.spec, load_context(run))

    log.info("run %s started (%s)", run.id, definition)
    return run


def sync_run_projection(run, spec, context):
    """Write the derived state onto the Run row.

    ``status`` and ``context`` are a cache of the event log, never an independent
    source of truth. If this row and the log ever disagree, the log is right.
    """
    run.status = next_run_state(spec, context)
    run.context = context
    run.error = context.get("error", "")
    run.save(update_fields=["status", "context", "error", "updated_at"])
    return run


def close_run_if_finished(run, spec, context):
    """Append a terminal event once the run has nothing left to do.

    Guarded against double-appending: the context already carries the terminal
    status if a previous pass recorded it.
    """
    state = next_run_state(spec, context)

    if state == RunStatus.SUCCEEDED and context["status"] != RunStatus.SUCCEEDED:
        append_event(run, EventType.RUN_SUCCEEDED)
        return True

    if state == RunStatus.FAILED and context["status"] != RunStatus.FAILED:
        append_event(
            run,
            EventType.RUN_FAILED,
            payload={"error": _first_error(context)},
        )
        return True

    return False


def _first_error(context):
    for step_id, slot in context.get("steps", {}).items():
        if slot.get("error"):
            return f"{step_id}: {slot['error']}"
    return context.get("error", "")
