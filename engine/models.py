"""The execution engine's schema.

Four tables carry the whole engine:

    WorkflowDef  immutable, versioned recipe
    Run          one execution of a recipe (a *projection*, not the truth)
    RunEvent     append-only log; this IS the truth
    Task         the work queue AND the durable timer

If you only remember one thing: a Run row can be deleted and rebuilt exactly by
replaying its RunEvents. The Run row is a cache we keep for fast querying.
"""

import uuid

from django.db import models

from core.models import TenantScopedModel, TimestampedModel


class WorkflowDef(TenantScopedModel, TimestampedModel):
    """An immutable, versioned workflow definition.

    Definitions are never edited in place. Editing produces version N+1, and runs
    already in flight keep executing the version they started on. A workflow that
    changes underneath a running execution is a genuine source of corruption in
    real orchestrators.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    spec = models.JSONField()
    description = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name", "version"],
                name="uniq_workflowdef_tenant_name_version",
            )
        ]
        ordering = ["name", "-version"]

    def __str__(self):
        return f"{self.name} v{self.version}"

    @property
    def steps(self):
        return self.spec.get("steps", [])

    def step(self, step_id):
        for s in self.steps:
            if s.get("id") == step_id:
                return s
        return None


class RunStatus(models.TextChoices):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"           # blocked on a timer or a human approval
    COMPENSATING = "COMPENSATING"  # a step failed; unwinding in reverse
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Run(TenantScopedModel, TimestampedModel):
    """One execution of a workflow.

    ``context`` and ``status`` are a materialised fold of this run's RunEvents.
    They are a cache for querying and for the UI - never the source of truth. Any
    worker can throw them away and rebuild them by replaying the log.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        WorkflowDef, on_delete=models.PROTECT, related_name="runs"
    )
    status = models.CharField(
        max_length=20, choices=RunStatus.choices, default=RunStatus.PENDING
    )
    input = models.JSONField(default=dict, blank=True)
    context = models.JSONField(default=dict, blank=True)

    # Highest event seq applied to this run. Workers use it for optimistic
    # concurrency: append at last_seq + 1 and let the unique constraint arbitrate.
    last_seq = models.PositiveIntegerField(default=0)

    # e.g. "node:node-3" or "employee:e_42". Used to serialise runs that touch the
    # same real-world thing, via a Postgres advisory lock on this string.
    entity_ref = models.CharField(max_length=200, blank=True, default="")

    # "api" | "trigger:<uuid>" | "schedule"
    trigger_source = models.CharField(max_length=100, default="api")

    error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "status", "-created_at"]),
            models.Index(fields=["tenant", "entity_ref"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.definition.name} [{self.status}] {self.id}"

    @property
    def is_terminal(self):
        return self.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }


class EventType(models.TextChoices):
    RUN_STARTED = "RUN_STARTED"
    STEP_SCHEDULED = "STEP_SCHEDULED"
    STEP_STARTED = "STEP_STARTED"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    STEP_FAILED = "STEP_FAILED"
    STEP_RETRY_SCHEDULED = "STEP_RETRY_SCHEDULED"
    TIMER_SET = "TIMER_SET"
    TIMER_FIRED = "TIMER_FIRED"
    COMPENSATION_STARTED = "COMPENSATION_STARTED"
    STEP_COMPENSATED = "STEP_COMPENSATED"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"


class RunEvent(TenantScopedModel):
    """Append-only. Never updated, never deleted.

    ``UNIQUE(run, seq)`` is the single most important constraint in the project.
    Two workers racing to advance the same run both try to write seq N; Postgres
    lets exactly one succeed. The loser catches IntegrityError, re-reads, retries.

    That gives us optimistic concurrency control with no distributed lock, no
    consensus protocol and no lock service.
    """

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="events")
    seq = models.PositiveIntegerField()
    type = models.CharField(max_length=32, choices=EventType.choices)
    step_id = models.CharField(max_length=200, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "seq"], name="uniq_runevent_run_seq")
        ]
        indexes = [models.Index(fields=["run", "seq"])]
        ordering = ["run_id", "seq"]

    def __str__(self):
        return f"#{self.seq} {self.type} {self.step_id}".strip()


class TaskStatus(models.TextChoices):
    READY = "READY"
    LEASED = "LEASED"
    DONE = "DONE"


class TaskKind(models.TextChoices):
    EXECUTE = "EXECUTE"
    COMPENSATE = "COMPENSATE"


class Task(TenantScopedModel):
    """The work queue and the durable timer, in one table.

    A step ready to run now is a row with ``run_after = now()``.
    A step retrying in 4s is a row with ``run_after = now() + 4s``.
    A step sleeping for 3 days is a row with ``run_after = now() + 3 days``.

    All three are the same mechanism, which is why Cascade needs no scheduler
    process and no separate timer service. Workers claim with:

        SELECT ... WHERE status='READY' AND run_after <= now()
        ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1

    SKIP LOCKED is what lets N workers drain this table concurrently without
    coordinating with each other at all.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="tasks")
    step_id = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=16, choices=TaskKind.choices, default=TaskKind.EXECUTE
    )
    attempt = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16, choices=TaskStatus.choices, default=TaskStatus.READY
    )

    run_after = models.DateTimeField()

    lease_owner = models.CharField(max_length=200, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    # sha256(run_id | step_id | kind | attempt). Unique, so a step can never be
    # double-enqueued, and passed to side-effecting steps as an Idempotency-Key.
    idempotency_key = models.CharField(max_length=64, unique=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # The claim query's index. Leading with tenant keeps it useful once
            # multiple tenants share the table.
            models.Index(fields=["tenant", "status", "run_after"]),
            models.Index(fields=["status", "lease_expires_at"]),  # the reaper's index
            models.Index(fields=["run", "step_id"]),
        ]
        ordering = ["run_after"]

    def __str__(self):
        return f"{self.step_id} [{self.status}] attempt {self.attempt}"
