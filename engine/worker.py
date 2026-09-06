"""The worker loop.

    claim a task -> replay the log -> execute one step -> record the result
                 -> enqueue whatever is now ready -> repeat

That is the entire engine at runtime. Everything clever already happened in
replay.py; this module is the plumbing that connects it to a database and to the
outside world.

The worker is deliberately stateless. Between two iterations it remembers
nothing. Kill it at any point and the only consequence is a lease that expires.
"""

import logging

from django.db import transaction

from engine.expressions import ExpressionError, render
from engine.models import EventType
from engine.service import (
    close_run_if_finished,
    enqueue_ready_steps,
    finish_task,
    append_event,
    claim_task,
    load_context,
    sync_run_projection,
)
from engine.spec import steps_by_id
from engine.steps import StepError, get_plugin

log = logging.getLogger("cascade.worker")


def run_once(worker_id):
    """Claim and process a single task. Returns True if there was work."""
    task = claim_task(worker_id)
    if task is None:
        return False

    execute_task(task, worker_id)
    return True


def execute_task(task, worker_id):
    run = task.run
    spec = run.definition.spec
    step = steps_by_id(spec).get(task.step_id)

    if step is None:
        # The definition is immutable and was validated, so this should be
        # unreachable. Fail loudly rather than silently dropping the task.
        _record_failure(
            task, run, spec, f"step {task.step_id!r} is not in this workflow definition"
        )
        return

    context = load_context(run)

    log.info(
        "[%s] claim  %-14s run=%s attempt=%s",
        _short(worker_id), task.step_id, _short(str(run.id)), task.attempt,
    )

    append_event(
        run, EventType.STEP_STARTED, task.step_id, {"attempt": task.attempt}
    )

    # ---- execute OUTSIDE any transaction ---------------------------------
    # This is the slow part: subprocesses, network calls, arbitrary latency.
    # Holding a row lock across it is how you deadlock a worker pool.
    ok, result = _execute_step(step, context, task.idempotency_key)
    # ----------------------------------------------------------------------

    if ok:
        log.info("[%s] done   %-14s", _short(worker_id), task.step_id)
        _record_success(task, run, spec, result)
    else:
        log.warning(
            "[%s] FAILED %-14s %s", _short(worker_id), task.step_id, result.get("error")
        )
        _record_failure(task, run, spec, result.get("error", ""), result.get("details"))


def _execute_step(step, context, idem_key):
    """Run one step. Returns (ok, result). Never raises."""
    try:
        config = render(step.get("config", {}), context)
    except ExpressionError as exc:
        return False, {"error": str(exc)}

    try:
        plugin = get_plugin(step["type"])
        output = plugin.execute(config, context, idem_key)
        return True, output
    except StepError as exc:
        return False, {"error": str(exc), "details": exc.details}
    except Exception as exc:  # noqa: BLE001 - a plugin bug must not kill the worker
        log.exception("plugin %r raised an unexpected error", step.get("type"))
        return False, {"error": f"{type(exc).__name__}: {exc}"}


def _record_success(task, run, spec, output):
    with transaction.atomic():
        append_event(run, EventType.STEP_SUCCEEDED, task.step_id, {"output": output})
        finish_task(task)
        _advance(run, spec)


def _record_failure(task, run, spec, error, details=None):
    # Block 3 turns this into a retry when the step declares one. For now every
    # failure is terminal.
    with transaction.atomic():
        append_event(
            run,
            EventType.STEP_FAILED,
            task.step_id,
            {"error": error, "details": details or {}},
        )
        finish_task(task)
        _advance(run, spec)


def _advance(run, spec):
    """Re-derive state from the log, enqueue what is now ready, close if done."""
    context = load_context(run)
    enqueue_ready_steps(run, spec, context)

    if close_run_if_finished(run, spec, context):
        context = load_context(run)

    sync_run_projection(run, spec, context)


def _short(value):
    """Trim an identifier for readable logs."""
    return value.split(":")[-1][:8] if ":" in value else value[:8]
