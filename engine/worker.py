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

from engine import retry
from engine.expressions import ExpressionError, render
from engine.heartbeat import LeaseHeartbeat
from engine.models import EventType
from engine.service import (
    append_event,
    claim_task,
    close_run_if_finished,
    enqueue_ready_steps,
    finish_task,
    finish_task_if_owned,
    load_context,
    schedule_retry,
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
        # Definitions are immutable and validated, so this should be
        # unreachable. Fail loudly rather than silently dropping the task.
        _fail_terminally(
            task, run, spec, f"step {task.step_id!r} is not in this workflow definition"
        )
        return

    context = load_context(run)

    log.info(
        "[%s] claim  %-14s run=%s attempt=%s",
        _short(worker_id), task.step_id, _short(str(run.id)), task.attempt,
    )

    append_event(run, EventType.STEP_STARTED, task.step_id, {"attempt": task.attempt})

    # ---- execute OUTSIDE any transaction ---------------------------------
    # Slow: subprocesses, network calls, arbitrary latency. Holding a row lock
    # across this is how you deadlock a worker pool. The heartbeat keeps our
    # lease alive for as long as we are genuinely working.
    with LeaseHeartbeat(task, worker_id) as beat:
        ok, result = _execute_step(step, context, task.idempotency_key)
    # ----------------------------------------------------------------------

    # Fencing. If our lease expired mid-step the reaper has already handed this
    # work to someone else. Recording our result now would write a second,
    # stale outcome for a step another worker may have already completed.
    if beat.lost or not finish_task_if_owned(task, worker_id):
        log.warning(
            "[%s] ABANDON %-13s lease lost mid-step; discarding result",
            _short(worker_id), task.step_id,
        )
        return

    if ok:
        log.info("[%s] done   %-14s", _short(worker_id), task.step_id)
        _record_success(task, run, spec, result)
        return

    error = result.get("error", "")

    if retry.should_retry(step, task.attempt):
        log.warning(
            "[%s] retry  %-14s attempt %s/%s: %s",
            _short(worker_id), task.step_id, task.attempt,
            retry.max_attempts(step), error,
        )
        with transaction.atomic():
            schedule_retry(run, step, task, error)
            sync_run_projection(run, spec, load_context(run))
        return

    log.warning("[%s] FAILED %-14s %s", _short(worker_id), task.step_id, error)
    _record_terminal_failure(task, run, spec, error, result.get("details"))


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
        _advance(run, spec)


def _record_terminal_failure(task, run, spec, error, details=None):
    with transaction.atomic():
        append_event(
            run,
            EventType.STEP_FAILED,
            task.step_id,
            {"error": error, "details": details or {}, "attempt": task.attempt},
        )
        _advance(run, spec)


def _fail_terminally(task, run, spec, error):
    """Failure paths that bypass the retry logic entirely."""
    with transaction.atomic():
        append_event(run, EventType.STEP_FAILED, task.step_id, {"error": error})
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
