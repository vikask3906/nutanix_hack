"""The dispatcher: entity changes become workflow runs.

This is the piece that turns Cascade from a workflow engine into a platform.
Without it, someone has to notice a thing happened and POST a run. With it, the
graph itself is the trigger - change an employee's department and the right
workflows start on their own, across every system that cares.

That is Rippling's central idea. One graph, one change, and everything
downstream reacts, because nothing downstream has to poll.

Mechanically it is the same pattern as the worker loop, pointed at a different
table: claim an unprocessed row with FOR UPDATE SKIP LOCKED, act, mark it done.
Several dispatchers can run safely, and one being down for an hour loses
nothing - the outbox rows simply wait.
"""

import logging

from django.db import transaction

from core.tenancy import tenant_context
from engine.expressions import ExpressionError, render
from engine.service import start_run
from graph.models import EntityChange, Trigger
from graph.predicates import matches
from graph.service import change_context

log = logging.getLogger("cascade.dispatcher")


def dispatch_one():
    """Process a single unprocessed change. Returns (change, runs) or None."""
    with transaction.atomic():
        change = (
            EntityChange.all_tenants.select_for_update(skip_locked=True)
            .select_related("entity")
            .filter(processed_at__isnull=True)
            .order_by("id")
            .first()
        )

        if change is None:
            return None

        context = change_context(change)
        started = []

        triggers = Trigger.all_tenants.select_related("definition").filter(
            tenant_id=change.tenant_id,
            entity_type=change.entity_type,
            enabled=True,
        )

        # The claim spans tenants - one shared outbox - but everything the
        # triggers touch belongs to this change's tenant, so scope it.
        with tenant_context(change.tenant):
            started = _fire_triggers(change, triggers, context)

        from django.utils import timezone

        change.processed_at = timezone.now()
        change.save(update_fields=["processed_at"])

    return change, started


def _fire_triggers(change, triggers, context):
    started = []

    for trigger in triggers:
            try:
                if not matches(trigger.predicate, context):
                    continue
            except Exception:  # noqa: BLE001
                # One malformed predicate must not stop every other trigger, or
                # block the outbox behind a row that can never be processed.
                log.exception("trigger %s: predicate evaluation failed", trigger.name)
                continue

            try:
                run_input = render(trigger.input_template or {}, context)
            except ExpressionError as exc:
                log.error("trigger %s: input_template failed: %s", trigger.name, exc)
                continue

            run = start_run(
                trigger.definition,
                run_input=run_input,
                entity_ref=change.entity.ref,
                trigger_source=f"trigger:{trigger.id}",
            )
            started.append((trigger, run))

            log.info(
                "trigger %r fired on %s -> run %s",
                trigger.name, change.entity.ref, run.id,
            )

    # The caller marks the change processed inside the same transaction that
    # started these runs. If it rolls back, the change stays unprocessed and is
    # retried - at-least-once, matching the rest of the system.
    return started


def dispatch_pending(limit=None):
    """Drain the outbox. Returns a list of (change, [(trigger, run), ...])."""
    processed = []

    while limit is None or len(processed) < limit:
        result = dispatch_one()
        if result is None:
            break
        processed.append(result)

    return processed
